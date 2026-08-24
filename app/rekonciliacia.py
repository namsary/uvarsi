#!/usr/bin/env python3
"""Uvar.si — rekonciliácia platieb: čo nepriniesol webhook, dobehne toto.

PREČO VÔBEC. Webhook je jediné, čím sa appka doteraz dozvedela o platbe. Keď
nedorazí — výpadok siete, reštart služby, zle nastavený vypínač, chyba na strane
poskytovateľa — zákazník zaplatil, členstvo nedostal a NIKTO sa to nedozvie.
Poskytovateľ doručenie pár ráz zopakuje a potom ho zahodí. Majiteľ sa o tom
dozvie z reklamácie, ak vôbec.

AKO. Raz za hodinu si vypýtame zoznam objednávok z API poskytovateľa (zdroj
pravdy o peniazoch) a porovnáme ho s tým, čo máme v databáze. Chýbajúce doplníme,
zmeškané vrátenia dobehneme.

ČO SA ZÁMERNE NEROBÍ. Rekonciliácia NEMÁ vlastnú cestu k udeleniu nároku. Každú
objednávku prepíše do presne toho tvaru, v akom chodí webhook, a pošle ju cez
`platby.spracuj_udalost` — teda cez tú istú idempotenciu, tú istú kontrolu
kapacity a tie isté UNIQUE obmedzenia. Preto je bezpečné spustiť ju koľkokrát
treba: kľúč udalosti je odvodený z ID OBJEDNÁVKY, takže druhý beh skončí na
`uz_spracovane` a druhý nárok nevznikne.

PORADIE V JEDNOM BEHU:
  1. odložené telá (webhooky prijaté s vypnutými platbami) — s overením podpisu,
  2. objednávky z API poskytovateľa,
  3. upratanie starých kľúčov udalostí,
  4. upozornenia majiteľovi (ntfy) — bez akéhokoľvek osobného údaju.

VYPÍNAČ. Beží aj s vypnutým PLATBY_ZAPNUTE, a to zámerne: práve zle nastavený
vypínač je jedna z ciest, ako sa peniaze strácajú, a keby ho rekonciliácia
rešpektovala, dieru by nezaplátala. Bránou je namiesto toho prítomnosť kľúčov
v /opt/uvarsi/uvarsi.env — bez nich skript neurobí nič. Nárok tak stále nevie
udeliť nikto z internetu, len majiteľ so svojimi kľúčmi.

Nastavenie (do /opt/uvarsi/uvarsi.env, nikdy nie do repozitára):
    LEMON_API_KEY=...          # API kľúč z LemonSqueezy (Settings → API)
    LEMON_STORE_ID=...         # nepovinné, ale odporúčané: len tvoj obchod
    LEMON_WEBHOOK_SECRET=...   # už je nastavený kvôli webhooku
    LEMON_VARIANT_ID=...       # už je nastavený kvôli webhooku

Beh (cron, každú hodinu — riadok inštaluje nasad.ps1):
    5 * * * * cd /opt/uvarsi/app && /opt/uvarsi/venv/bin/python rekonciliacia.py \
              >> /var/log/uvarsi-platby.log 2>&1
"""
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import db_rezim  # noqa: E402
import naklady  # noqa: E402
import platby  # noqa: E402

DB = os.environ.get("UVARSI_DB", "/opt/uvarsi/uvarsi.db")
ENV_FILE = os.environ.get("UVARSI_ENV_FILE", "/opt/uvarsi/uvarsi.env")
API_URL = "https://api.lemonsqueezy.com/v1/orders"
# Koľko strán po 100 objednávkach sa najviac prezrie za jeden beh. 5 strán
# pokrýva 500 najnovších objednávok, teda dvojnásobok celej kapacity — a zároveň
# to je strop, aby hodinový beh nikdy nebúšil do API donekonečna.
MAX_STRAN = 5
STRANA = 100
CAS_SPOJENIA = 20


def env(kluc, default=None):
    """Tajomstvá výhradne z prostredia alebo env súboru servera — nikdy z kódu."""
    if os.environ.get(kluc):
        return os.environ[kluc]
    try:
        with open(ENV_FILE, "r", encoding="utf-8") as subor:
            for riadok in subor:
                riadok = riadok.strip()
                if not riadok or riadok.startswith("#"):
                    continue
                if riadok.startswith("export "):
                    riadok = riadok[len("export "):]
                meno, _, hodnota = riadok.partition("=")
                if meno.strip() == kluc:
                    return hodnota.strip().strip('"').strip("'")
    except OSError:
        pass
    return default


# ---------------------------------------------------------------- API
def stiahni_stranu(api_key, *, store_id=None, strana=1, otvor=None):
    """Jedna strana objednávok z API poskytovateľa. Vracia (zoznam, je_dalsia)."""
    parametre = {"page[size]": STRANA, "page[number]": strana, "sort": "-createdAt"}
    if store_id:
        parametre["filter[store_id]"] = str(store_id)
    adresa = API_URL + "?" + urllib.parse.urlencode(parametre)
    ziadost = urllib.request.Request(
        adresa,
        headers={
            "Accept": "application/vnd.api+json",
            "Content-Type": "application/vnd.api+json",
            "Authorization": f"Bearer {api_key}",
        },
    )
    otvor = otvor or urllib.request.urlopen
    with otvor(ziadost, timeout=CAS_SPOJENIA) as odpoved:
        telo = json.loads(odpoved.read().decode("utf-8"))
    data = telo.get("data") if isinstance(telo, dict) else None
    if not isinstance(data, list):
        return [], False
    dalsia = bool(((telo.get("links") or {}) if isinstance(telo, dict) else {}).get("next"))
    return data, dalsia


def stiahni_objednavky(api_key, *, store_id=None, max_stran=MAX_STRAN, otvor=None):
    objednavky = []
    for strana in range(1, max_stran + 1):
        davka, dalsia = stiahni_stranu(
            api_key, store_id=store_id, strana=strana, otvor=otvor
        )
        objednavky.extend(davka)
        if not dalsia or not davka:
            break
    return objednavky


# ---------------------------------------------------------------- porovnanie
def rekonciluj(con, *, objednavky, now, variant_id=None, notifikuj=None) -> dict:
    """Doplň, čo webhook nepriniesol. Nič neobchádza — všetko ide cez spracuj_udalost."""
    suhrn = {
        "videne": 0, "udelene": 0, "uz_spracovane": 0, "vratene": 0,
        "bez_uctu": 0, "ignorovane": 0, "nad_kapacitu": 0, "duplicitne": 0,
        "nepouzitelne": 0,
    }
    for objednavka in objednavky or ():
        suhrn["videne"] += 1
        ref = platby._bezpecne_id(
            objednavka.get("id") if isinstance(objednavka, dict) else None
        )
        if not ref:
            continue
        stav = platby.stav_objednavky(objednavka)
        if stav == "refunded":
            _vrat(con, objednavka, ref, now=now, suhrn=suhrn)
            continue
        if stav != "paid":
            # pending / failed: peniaze ešte (alebo už) nie sú naše.
            continue
        user_id = platby.user_id_z_objednavky(objednavka)
        if user_id is None:
            user_id = platby.ucet_podla_emailu(con, platby.email_z_objednavky(objednavka))
        if user_id is None:
            # Zaplatil pod inou adresou, než akou sa prihlasuje. Priradiť to za
            # neho by bolo hádanie; majiteľ to vyrieši cez premium_cli.py.
            if platby.narok_objednavky(con, ref) is None:
                suhrn["bez_uctu"] += 1
            continue
        payload = platby.payload_z_objednavky(objednavka, user_id=user_id)
        if payload is None:
            continue
        try:
            vysledok = platby.spracuj_udalost(
                con, payload=payload, now=now, variant_id=variant_id,
                zdroj=platby.ZDROJ_REKONCILIACIA,
            )
        except platby.UdalostNepouzitelna:
            suhrn["nepouzitelne"] += 1
            continue
        akcia = vysledok["akcia"]
        if akcia == platby.AKCIA_UDELENE:
            suhrn["udelene"] += 1
        elif akcia == platby.AKCIA_UZ_SPRACOVANE:
            suhrn["uz_spracovane"] += 1
        elif akcia == platby.AKCIA_NAD_KAPACITU:
            suhrn["nad_kapacitu"] += 1
        elif akcia == platby.AKCIA_IGNOROVANE:
            suhrn["ignorovane"] += 1
        elif vysledok.get("stav") == platby.STAV_DUPLICITNY:
            suhrn["duplicitne"] += 1
    _ohlas(con, suhrn, now=now, notifikuj=notifikuj)
    return suhrn


def _vrat(con, objednavka, ref, *, now, suhrn) -> None:
    """Zmeškané vrátenie: bez neho by Premium bežalo ďalej za vrátené peniaze."""
    narok = platby.narok_objednavky(con, ref)
    if narok is None or narok["stav"] != platby.STAV_AKTIVNY:
        return
    payload = platby.payload_z_objednavky(
        objednavka, user_id=narok["user_id"], typ="order_refunded"
    )
    if payload is None:
        return
    try:
        vysledok = platby.spracuj_udalost(
            con, payload=payload, now=now, zdroj=platby.ZDROJ_REKONCILIACIA
        )
    except platby.UdalostNepouzitelna:
        suhrn["nepouzitelne"] += 1
        return
    if vysledok["akcia"] == platby.AKCIA_VRATENE:
        suhrn["vratene"] += 1


def _ohlas(con, suhrn, *, now, notifikuj=None) -> None:
    """Upozornenia majiteľovi. Text skladá platby.py — bez osobných údajov."""
    posli = naklady.posli_ntfy if notifikuj is None else notifikuj
    spravy = []
    if suhrn["udelene"]:
        spravy.append(platby.upozornenie_raz(
            con, platby.DRUH_REKONCILIACIA, now=now, pocet=suhrn["udelene"]))
    if suhrn["bez_uctu"]:
        spravy.append(platby.upozornenie_raz(
            con, platby.DRUH_BEZ_UCTU, now=now, pocet=suhrn["bez_uctu"]))
    if suhrn["nepouzitelne"]:
        spravy.append(platby.upozornenie_raz(con, platby.DRUH_NEPOUZITELNA, now=now))
    for sprava in spravy:
        if sprava is None:
            continue
        try:
            posli(sprava)
        except Exception:
            # Upozornenie je najlepšia snaha; nesmie zhodiť dobehnutie platieb.
            pass


# ---------------------------------------------------------------- beh
def main() -> int:
    api_key = env("LEMON_API_KEY")
    tajomstvo = env("LEMON_WEBHOOK_SECRET")
    variant = env("LEMON_VARIANT_ID")
    store_id = env("LEMON_STORE_ID")
    now = time.time()

    if not api_key and not tajomstvo:
        print("REKONCILIACIA: v uvarsi.env nie je LEMON_API_KEY ani "
              "LEMON_WEBHOOK_SECRET — niet čo rekonciliovať, končím.")
        return 0

    con = db_rezim.otvor(DB)
    try:
        platby.migrate_platby_schema(con)
        con.commit()

        if tajomstvo:
            odlozene = platby.spracuj_odlozene(
                con, tajomstvo=tajomstvo, now=now, variant_id=variant
            )
            if odlozene["spracovane"]:
                print("REKONCILIACIA: odložené telá — spracovaných "
                      f"{odlozene['spracovane']}, udelených {odlozene['udelene']}, "
                      f"neplatný podpis {odlozene['neplatny_podpis']}, "
                      f"nepoužiteľných {odlozene['nepouzitelne']}")
        else:
            print("REKONCILIACIA: bez LEMON_WEBHOOK_SECRET nevieme overiť podpisy "
                  "odložených tiel — preskakujem ich.")

        if api_key:
            try:
                objednavky = stiahni_objednavky(api_key, store_id=store_id)
            except (urllib.error.URLError, urllib.error.HTTPError, OSError,
                    ValueError, json.JSONDecodeError) as chyba:
                # Nedostupné API je dôvod skúsiť to o hodinu znova, nie dôvod
                # na paniku. Typ chyby stačí; telo odpovede môže niesť tajomstvá.
                print(f"REKONCILIACIA: API poskytovateľa neodpovedalo "
                      f"({type(chyba).__name__}) — skúsim o hodinu.")
                return 1
            suhrn = rekonciluj(
                con, objednavky=objednavky, now=now, variant_id=variant
            )
            print("REKONCILIACIA: objednávok " + ", ".join(
                f"{kluc} {hodnota}" for kluc, hodnota in sorted(suhrn.items())
            ))
        else:
            print("REKONCILIACIA: bez LEMON_API_KEY sa objednávky nedajú overiť "
                  "— dopĺňam len odložené telá.")

        zmazane = platby.uprac_udalosti(con, now=now)
        if zmazane:
            print(f"REKONCILIACIA: upratanych starych kľúčov udalostí: {zmazane}")
    finally:
        con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
