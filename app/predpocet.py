#!/usr/bin/env python3
"""Uvar.si — PREDPOČET: najžiadanejšie jedálničky poskladané v noci dopredu.

PREČO. Zbierač letákov (`zbierac_akcii.py`, spúšťa ho `hetzner/dozorca.sh`)
dobehne v pondelok nadránom. Od tej chvíle sú ponuky týždňa dané a KAŽDÝ
zdieľaný plán sa dá poskladať — len o ňom ešte nikto nepožiadal. Prvý človek
s daným profilom preto doteraz čakal 60–120 sekúnd na volanie, ktoré mohlo
pokojne prebehnúť o tretej ráno, keď nikto nečaká. Predpočet to volanie
presunie do noci; ráno sa plán podá z `plany_zdielane` za milisekundy.

ČO SA ZAHRIEVA. Podpis zdieľaného plánu (`plan_data.plan_signature`) tvoria
verzia generátora, týždeň, sada obchodov, počet osôb, frekvencia varenia a
množina `offer_key`. Špajza v ňom (zámerne) nie je, takže sa profil dá
predpočítať bez čohokoľvek osobného. K podpisu patrí ešte VARIANT — tá istá
domácnosť dostane jeden z `server.PLAN_VARIANTS` smerov kuchyne podľa
`user_id % PLAN_VARIANTS`. Jednotkou zahrievania je preto jeden plán, teda
jedna dvojica (podpis, variant) = jedno volanie modelu = jeden riadok cache.
Zahriať profil „naozaj celý“ znamená zahriať všetky jeho varianty; pri
východzích troch variantoch teda `UVARSI_PREDPOCET_PROFILOV=9` znamená tri
úplne pokryté domácnosti.

PODĽA ČOHO SA VYBERÁ. Prvý týždeň niet histórie, tak sa ide podľa rozumného
odhadu (`VYCHODZIE_PROFILY` — východzí profil appky a jeho najbližší susedia).
Od druhého týždňa rozhoduje `dopyt_profilov`: čistý AGREGÁT toho, o čo ľudia
naozaj žiadali. Žiadne user_id, žiadny e-mail, žiadny čas jednotlivej
požiadavky — len počty na (týždeň, obchody, osoby, frekvencia, variant) a
`HISTORIA_TYZDNOV` týždňov dozadu. Zoznam sa tak učí sám a nepotrebuje, aby
majiteľ hádal, čo si ľudia vypýtajú.

ČO SA MÍŇA. Cena jedného zahriateho plánu je cena jedného volania Sonnetom
nad promptom, ktorého ~95 % tvorí katalóg ponúk. Ten je pre celý beh rovnaký a
ide s `cache_control`, takže prvé volanie cache zapíše (~0,027 €) a všetky
ďalšie ju len čítajú (~0,018 €). Účtuje sa ako účel „predpocet“, takže je v
/api/naklady vidieť zvlášť od plánov, ktoré si vypýtali ľudia.

TRI OHRANIČENIA, ktoré musia platiť súčasne (a fail closed cez `naklady`):
  1. POČET — `UVARSI_PREDPOCET_PROFILOV` (východzie 9), tvrdo zhora
     `MAX_POCET_PROFILOV`. Pokazená hodnota v prostredí nezhodí beh, len ho
     zúži: predpočet je zrýchlenie, nie povinnosť.
  2. REZERVA — predpočet zastane, keď by z denného stropu ostalo menej než
     `UVARSI_PREDPOCET_REZERVA_EUR`. V pondelok totiž ten istý deň zaplatil aj
     zber letákov (~1,11 €) a bolo by absurdné, keby nočné zahrievanie nechalo
     ranných používateľov bez rozpočtu na ich vlastný plán.
  3. BEHY a TÝŽDENNÝ STROP ÚČELU — `naklady.rezervuj_beh` a
     `stropy().tyzdenny_ucel["predpocet"]`. Rozbehnutý cron narazí na strop
     skôr, než stihne niečo minúť.

ZLYHANIE JE NEŠKODNÉ. Nič z tohto modulu nesmie zhodiť appku ani dozorcu.
Keď sa predpočet vypne, preskočí alebo padne, appka skladá plány naživo presne
ako doteraz — jediný rozdiel oproti dnešku je rýchlosť.

Ručne na serveri:
    cd /opt/uvarsi/app
    UVARSI_URL=https://uvar.si ../venv/bin/python predpocet.py --zahrej
    UVARSI_URL=https://uvar.si ../venv/bin/python predpocet.py --stav
"""
import datetime
import json
import logging
import os
import re
import sqlite3
from collections import namedtuple
from contextlib import closing

try:
    from . import naklady
    from .plan_data import build_personal_plan, personal_plan_messages
except ImportError:                      # beh priamo z adresára app/
    import naklady
    from plan_data import build_personal_plan, personal_plan_messages


LOG = logging.getLogger("uvarsi.predpocet")

UCEL = "predpocet"

# Koľko plánov sa zahrieva. Jednotka je JEDEN plán (podpis + variant), teda
# jedno volanie modelu. Východzích 9 = tri najžiadanejšie domácnosti so
# všetkými troma variantmi, dokopy ~0,17 €.
VYCHODZI_POCET_PROFILOV = 9
# Tvrdý strop nad hodnotou z prostredia. Preklep (900 namiesto 9) sa nesmie
# premeniť na 18 € útraty; pri MAX × cene za profil ostane pod polovicou
# mesačného rozpočtu a zvyšné poistky (rezerva, strop účelu) platia ďalej.
MAX_POCET_PROFILOV = 60

# Koľko z denného stropu musí ostať živým používateľom. V pondelok už z neho
# zaplatil zber letákov, takže bez tejto rezervy by nočné zahrievanie dojedlo
# zvyšok a ráno by si nikto nevedel vypýtať vlastný plán.
VYCHODZIA_REZERVA_EUR = 0.20

# Odkedy dokedy sa pozerá na dopyt. Štyri týždne sú kompromis: dosť dlho na to,
# aby jeden pokojný týždeň zoznam nerozhodil, a dosť krátko na to, aby sa
# zoznam hýbal s tým, ako sa mení, kto appku používa.
HISTORIA_TYZDNOV = 4
# Dokedy sa dopyt vôbec drží. Je to agregát, nie osobný log, ale aj agregát sa
# má sám upratať — držať počty spred roka nemá komu a načo poslúžiť.
DRZANIE_DOPYTU_TYZDNOV = 12

# Musí sedieť so `server.PLAN_VARIANTS` (stráži to test). Keby sa rozišli,
# zahrialo by sa niečo, čo nikto nedostane.
POCET_VARIANTOV = 3

# Koľko stojí jeden zahriaty plán. Nie je to dojem: prompt je ~14 000 znakov
# (katalóg 140 ponúk + pravidlá receptu) ≈ 4 400 tokenov v cachovanej predpone,
# osobný chvost ~250 tokenov a odpoveď ~1 300–1 900 tokenov. Sonnet 5 pri
# EUR = USD × 0,92 → prvé volanie behu (zápis cache) ~0,027 €, každé ďalšie
# (čítanie cache) ~0,018 €. Rovnaké číslo ako odhad pre `plan`, lebo je to tá
# istá operácia — len ju nikto nečaká.
CENA_ZA_PROFIL_EUR = naklady.ODHAD_EUR[UCEL]

PREMENNE_PROSTREDIA = (
    "UVARSI_PREDPOCET",
    "UVARSI_PREDPOCET_PROFILOV",
    "UVARSI_PREDPOCET_REZERVA_EUR",
)

# Dôvody, prečo sa beh skončil. Sú to strojové kódy do /api/naklady; vysvetlenie
# pre človeka je v `VYSVETLENIE`.
DOVOD_HOTOVO = "hotovo"
DOVOD_ROZPOCET = "rozpocet"
DOVOD_BEHY = "behy"
DOVOD_VYPNUTE = "vypnute"
DOVOD_CHYBY = "chyby"

VYSVETLENIE = {
    DOVOD_HOTOVO: "Zahriate všetko, o čo bolo požiadané.",
    DOVOD_ROZPOCET: "Zastavené pred stropom — zvyšok rozpočtu ostáva živým používateľom.",
    DOVOD_BEHY: "Tento týždeň už predpočet bežal dosť často; ďalší beh sa nespúšťa.",
    DOVOD_VYPNUTE: "Predpočet je vypnutý (UVARSI_PREDPOCET=0 alebo 0 profilov).",
    DOVOD_CHYBY: "Príliš veľa neúspešných pokusov po sebe — beh sa ukončil.",
}

# Koľko pádov po sebe sa toleruje, kým sa beh vzdá. Keď model odmieta alebo
# odpovedá nezmyslom, nemá zmysel prejsť celý zoznam a zaplatiť to.
MAX_ZLYHANI_PO_SEBE = 3


Profil = namedtuple("Profil", "obchody osoby frekvencia variant")


# ---------------------------------------------------------------- schéma
SCHEMA = """
-- Čo si ľudia pýtali, v podobe počtov. ZÁMERNE bez user_id a bez e-mailu:
-- na zostavenie zoznamu na zahrievanie stačí, KOĽKO ráz taký profil niekto
-- chcel — nie kto to bol a kedy presne. Riadok je agregát, nie stopa po
-- človeku, a staršie týždne sa samy mažú.
CREATE TABLE IF NOT EXISTS dopyt_profilov (
  tyzden     TEXT NOT NULL,          -- ISO pondelok týždňa požiadavky
  obchody    TEXT NOT NULL,          -- normalizované: zoradené, čiarkou
  osoby      INTEGER NOT NULL,
  frekvencia INTEGER NOT NULL,
  variant    INTEGER NOT NULL,
  pocet      INTEGER NOT NULL DEFAULT 0,
  posledny   TEXT,                   -- deň poslednej požiadavky (bez hodiny)
  PRIMARY KEY (tyzden, obchody, osoby, frekvencia, variant)
);

-- Jeden riadok na týždeň: ako sa predpočtu darilo. `zasahy` je to jediné
-- číslo, ktoré hovorí, či to malo zmysel — koľko živých generovaní sa vďaka
-- zahriatym plánom NEUSKUTOČNILO.
CREATE TABLE IF NOT EXISTS predpocet_behy (
  tyzden       TEXT NOT NULL PRIMARY KEY,
  zaciatok     TEXT,
  koniec       TEXT,
  behov        INTEGER NOT NULL DEFAULT 0,
  zahriatych   INTEGER NOT NULL DEFAULT 0,
  preskocenych INTEGER NOT NULL DEFAULT 0,
  zlyhanych    INTEGER NOT NULL DEFAULT 0,
  eur          REAL NOT NULL DEFAULT 0,
  zasahy       INTEGER NOT NULL DEFAULT 0,
  dovod        TEXT
);
"""


def migrate_predpocet_schema(con) -> None:
    """Tabuľky predpočtu + príznak nad zdieľanými plánmi. Idempotentné.

    `predpocitany` odlišuje riadok poskladaný v noci od riadku, ktorý zaplatil
    živý používateľ. Bez neho by sa nedalo povedať, koľko čakania predpočet
    naozaj ušetril — a to je jediné číslo, podľa ktorého sa dá rozhodnúť, či
    má zmysel zahrievať viac alebo menej.
    """
    con.executescript(SCHEMA)
    stlpce = {riadok[1] for riadok in con.execute("PRAGMA table_info(plany_zdielane)")}
    if stlpce and "predpocitany" not in stlpce:
        con.execute(
            "ALTER TABLE plany_zdielane ADD COLUMN predpocitany INTEGER NOT NULL DEFAULT 0"
        )


# ---------------------------------------------------------------- konfigurácia
def _normalizuj_obchody(obchody) -> tuple:
    """Rovnaká sada obchodov musí dať rovnaký kľúč, nech príde v akomkoľvek poradí."""
    if isinstance(obchody, str):
        obchody = obchody.split(",")
    return tuple(sorted({str(obchod).strip() for obchod in obchody if str(obchod).strip()}))


def je_zapnuty() -> bool:
    """Vypnutie je jedno slovo v prostredí a nič viac sa nestane."""
    hodnota = os.environ.get("UVARSI_PREDPOCET")
    if hodnota is None or not hodnota.strip():
        return True
    return hodnota.strip().lower() not in ("0", "nie", "no", "off", "false")


def pocet_profilov() -> int:
    """Koľko plánov zahriať. Pokazená hodnota beh NEZHODÍ, len ho zúži.

    `naklady` fail closed vyhadzuje výnimku, lebo tam ide o platbu. Tu ide o
    zrýchlenie: keď je hodnota nezmyselná, správne je zahriať východzí počet
    (alebo nič), nie zhodiť nočný cron.
    """
    surove = os.environ.get("UVARSI_PREDPOCET_PROFILOV")
    if surove is None or not surove.strip():
        return VYCHODZI_POCET_PROFILOV
    try:
        hodnota = int(surove.strip())
    except (TypeError, ValueError):
        LOG.warning("UVARSI_PREDPOCET_PROFILOV nie je číslo (%r) — beriem východzích %d",
                    surove, VYCHODZI_POCET_PROFILOV)
        return VYCHODZI_POCET_PROFILOV
    return max(0, min(hodnota, MAX_POCET_PROFILOV))


def rezerva_eur() -> float:
    surove = os.environ.get("UVARSI_PREDPOCET_REZERVA_EUR")
    if surove is None or not surove.strip():
        return VYCHODZIA_REZERVA_EUR
    try:
        hodnota = float(surove.strip().replace(",", "."))
    except (TypeError, ValueError):
        return VYCHODZIA_REZERVA_EUR
    return hodnota if hodnota >= 0 else VYCHODZIA_REZERVA_EUR


# ---------------------------------------------------------------- výber profilov
# Východzí profil appky je Kaufland+Tesco+Lidl, 4 osoby, variť raz za 2 dni —
# presne to má v `pouzivatelia` každý nový účet. Okolo neho sú najbližší
# susedia: menšia domácnosť a iná frekvencia. Poradie je poradie stávok, lebo
# `UVARSI_PREDPOCET_PROFILOV` zoznam odreže zhora.
VSETKY_OBCHODY = ("Kaufland", "Lidl", "Tesco")
VYCHODZIE_DOMACNOSTI = (
    (VSETKY_OBCHODY, 4, 2),
    (VSETKY_OBCHODY, 2, 2),
    (VSETKY_OBCHODY, 4, 3),
    (VSETKY_OBCHODY, 2, 3),
    (VSETKY_OBCHODY, 4, 1),
    (VSETKY_OBCHODY, 1, 2),
    (VSETKY_OBCHODY, 3, 2),
    (VSETKY_OBCHODY, 5, 2),
    (VSETKY_OBCHODY, 2, 1),
    (VSETKY_OBCHODY, 6, 2),
)


def vychodzie_profily() -> list:
    """Stávka na prvý týždeň, kým niet histórie. Domácnosť po domácnosti,
    každá so všetkými variantmi — inak by sa trafili len dve tretiny ľudí."""
    profily = []
    for obchody, osoby, frekvencia in VYCHODZIE_DOMACNOSTI:
        for variant in range(POCET_VARIANTOV):
            profily.append(Profil(_normalizuj_obchody(obchody), osoby, frekvencia, variant))
    return profily


def zaznamenaj_dopyt(con, tyzden, obchody, osoby, frekvencia, variant, dnes=None) -> None:
    """Pripočítaj jednotku k profilu, o ktorý niekto požiadal.

    Nikdy nesmie zhodiť požiadavku používateľa: evidencia dopytu je podklad na
    zahrievanie, nie súčasť odpovede. Keď sa zápis nepodarí, zoznam bude o
    kúsok menej presný a nič viac.
    """
    dnes = dnes or datetime.date.today()
    try:
        con.execute(
            """INSERT INTO dopyt_profilov (tyzden, obchody, osoby, frekvencia, variant,
                                           pocet, posledny)
               VALUES (?, ?, ?, ?, ?, 1, ?)
               ON CONFLICT(tyzden, obchody, osoby, frekvencia, variant) DO UPDATE SET
                 pocet = pocet + 1, posledny = excluded.posledny""",
            (str(tyzden), ",".join(_normalizuj_obchody(obchody)), int(osoby),
             int(frekvencia), int(variant), dnes.isoformat()),
        )
        hranica = (datetime.date.fromisoformat(str(tyzden))
                   - datetime.timedelta(weeks=DRZANIE_DOPYTU_TYZDNOV)).isoformat()
        con.execute("DELETE FROM dopyt_profilov WHERE tyzden < ?", (hranica,))
    except (sqlite3.Error, OSError, ValueError, TypeError):
        LOG.debug("dopyt profilu sa nezapísal", exc_info=True)


def oblubene_profily(con, tyzden, pocet, tyzdnov=HISTORIA_TYZDNOV) -> list:
    """Čo zahriať: najprv to, o čo ľudia žiadali, potom východzie stávky.

    Berie sa dopyt z PREDOŠLÝCH týždňov, nie z tohto: tento sa práve začal a
    jeho čísla by povedali nanajvýš to, kto vstal skoro. Keď história nestačí
    na `pocet` položiek, zvyšok doplnia východzie profily — zoznam teda nikdy
    nie je prázdny len preto, že appka je nová.
    """
    pocet = max(0, int(pocet))
    if not pocet:
        return []
    profily = []
    try:
        od = (datetime.date.fromisoformat(str(tyzden))
              - datetime.timedelta(weeks=tyzdnov)).isoformat()
        riadky = con.execute(
            """SELECT obchody, osoby, frekvencia, variant, SUM(pocet) AS spolu
               FROM dopyt_profilov
               WHERE tyzden < ? AND tyzden >= ?
               GROUP BY obchody, osoby, frekvencia, variant
               ORDER BY spolu DESC, obchody, osoby, frekvencia, variant""",
            (str(tyzden), od),
        ).fetchall()
    except (sqlite3.Error, OSError, ValueError, TypeError):
        LOG.warning("dopyt profilov sa nedá prečítať — beriem východzie", exc_info=True)
        riadky = []
    for riadok in riadky:
        profily.append(Profil(
            _normalizuj_obchody(riadok["obchody"]), int(riadok["osoby"]),
            int(riadok["frekvencia"]), int(riadok["variant"]),
        ))
    for profil in vychodzie_profily():
        if profil not in profily:
            profily.append(profil)
    return profily[:pocet]


# ---------------------------------------------------------------- rozpočet
def _je_miesto_v_rozpocte(con, teraz, rezerva) -> bool:
    """Ostane po tomto volaní ešte rezerva pre živých používateľov?

    Tvrdé stropy (denný, mesačný, týždenný účelový) drží `naklady.skontroluj`
    pri každom volaní. Tento test je nad ním: zastaví predpočet SKÔR, aby to,
    čo ostane, patrilo ľuďom, ktorí naozaj čakajú.
    """
    try:
        limity = naklady.stropy()
        den, mesiac, _ = naklady._obdobia(teraz)
        dnes_eur = naklady.spolu_za_den(con, den)
        mesiac_eur = naklady.spolu_za_mesiac(con, mesiac)
    except (naklady.RozpocetVycerpany, sqlite3.Error, OSError):
        return False                     # nevieme → nemíňame
    odhad = CENA_ZA_PROFIL_EUR
    if dnes_eur + odhad > limity.denny - rezerva:
        return False
    if mesiac_eur + odhad > limity.mesacny - rezerva:
        return False
    return True


# ---------------------------------------------------------------- skladanie
class PlanNepouzitelny(Exception):
    """Model odpovedal tak, že sa z toho plán poskladať nedá."""


def _text_odpovede(msg) -> str:
    if getattr(msg, "stop_reason", None) == "max_tokens":
        raise PlanNepouzitelny("odpoveď sa nestihla dopísať do konca")
    text = "".join(blok.text for blok in msg.content
                   if getattr(blok, "type", None) == "text").strip()
    return re.sub(r"^```(?:json)?|```$", "", text, flags=re.M).strip()


def _poskladaj(con, server, rows, profil, klient=None):
    """Jedno platené volanie — cez ten istý strážený klient ako appka.

    Prompt, model aj strop tokenov sa berú zo `server`, nie z vlastných
    konštánt: keby sa rozišli, zahrialo by sa niečo s iným podpisom a nikto by
    sa do toho netrafil. Špajza sa sem nedostane nikdy — zdieľaný plán je z
    definície bez nej.
    """
    import anthropic

    strazeny = naklady.strazeny_klient(
        con,
        klient or anthropic.Anthropic(
            api_key=server.env("ANTHROPIC_API_KEY"),
            timeout=server.PLAN_TIMEOUT_SECONDS,
            max_retries=server.PLAN_MAX_RETRIES,
        ),
        UCEL,
    )
    blocks = personal_plan_messages(
        rows, profil.frekvencia, (), household_size=profil.osoby,
        variant=profil.variant, pantry_driven=False,
    )
    msg = strazeny.messages.create(
        model=server.MODEL_PLAN, max_tokens=server.PLAN_TOKENS,
        messages=[{"role": "user", "content": blocks}],
    )
    try:
        model_output = json.loads(_text_odpovede(msg))
    except json.JSONDecodeError as chyba:
        raise PlanNepouzitelny("odpoveď nie je platný JSON") from chyba
    try:
        return build_personal_plan(
            con, model_output, list(profil.obchody), profil.frekvencia, profil.osoby)
    except ValueError as chyba:
        raise PlanNepouzitelny(str(chyba)) from chyba


# ---------------------------------------------------------------- beh
def zahrej(*, pocet=None, dnes=None, teraz=None, klient=None) -> dict:
    """Poskladaj dopredu najžiadanejšie plány. Nikdy nevyhodí výnimku.

    Bezpečné spustiť opakovane: podpis, ktorý už v `plany_zdielane` je (a je
    aktuálny voči dnešným ponukám), sa preskočí bez volania modelu. Beh sa
    zastaví sám, keď by ďalšie volanie zjedlo rezervu pre živých používateľov.
    """
    teraz = teraz or datetime.datetime.now()
    dnes = dnes or teraz.date()
    pocet = pocet_profilov() if pocet is None else max(0, min(int(pocet), MAX_POCET_PROFILOV))
    vysledok = {
        "tyzden": None, "profilov": pocet, "zahriatych": 0, "preskocenych": 0,
        "zlyhanych": 0, "eur": 0.0, "dovod": DOVOD_HOTOVO,
    }
    if not je_zapnuty() or not pocet:
        vysledok["dovod"] = DOVOD_VYPNUTE
        return vysledok

    try:
        import server                    # neskoro, aby server smel importovať nás
    except Exception:
        LOG.warning("predpočet sa nespustil: appka sa nedá naimportovať", exc_info=True)
        vysledok["dovod"] = DOVOD_CHYBY
        return vysledok

    tyzden = server.monday(dnes)
    vysledok["tyzden"] = tyzden
    con = None
    try:
        con = server.db()
        migrate_predpocet_schema(con)
        try:
            naklady.rezervuj_beh(con, UCEL, teraz=teraz)
        except naklady.RozpocetVycerpany as odmietnutie:
            LOG.info("predpočet sa nespúšťa: %s", odmietnutie)
            vysledok["dovod"] = DOVOD_BEHY
            _zapis_beh(con, tyzden, teraz, vysledok, zaciatok=False)
            return vysledok

        profily = oblubene_profily(con, tyzden, pocet)
        vysledok["profilov"] = len(profily)
        pred_behom = _minute_na_ucel(con, teraz)
        rezerva = rezerva_eur()
        ponuky = {}
        za_sebou = 0
        for profil in profily:
            if profil.obchody not in ponuky:
                try:
                    ponuky[profil.obchody] = server.akcie_pre(list(profil.obchody))
                except Exception:
                    LOG.warning("ponuky pre %s sa nedajú načítať", profil.obchody, exc_info=True)
                    ponuky[profil.obchody] = []
            rows = ponuky[profil.obchody]
            if len(rows) < server.MIN_OFFERS_FOR_PLAN:
                continue                 # bez letákov niet z čoho skladať
            podpis = server.podpis_planu(
                tyzden, list(profil.obchody), profil.osoby, profil.frekvencia, rows, ())
            hotovy = server.nacitaj_zdielany_plan(con, podpis, profil.variant)
            if hotovy is not None:
                vysledok["preskocenych"] += 1
                continue
            if not _je_miesto_v_rozpocte(con, teraz, rezerva):
                vysledok["dovod"] = DOVOD_ROZPOCET
                break
            try:
                plan = _poskladaj(con, server, rows, profil, klient=klient)
            except naklady.RozpocetVycerpany as odmietnutie:
                LOG.info("predpočet zastavený rozpočtom: %s", odmietnutie)
                vysledok["dovod"] = DOVOD_ROZPOCET
                break
            except Exception as chyba:
                LOG.warning("profil %s sa nepodarilo zahriať: %s", profil, chyba)
                vysledok["zlyhanych"] += 1
                za_sebou += 1
                if za_sebou >= MAX_ZLYHANI_PO_SEBE:
                    vysledok["dovod"] = DOVOD_CHYBY
                    break
                continue
            za_sebou = 0
            server.uloz_zdielany_plan(
                con, podpis, profil.variant, tyzden, plan, predpocitany=True)
            con.commit()
            vysledok["zahriatych"] += 1
        vysledok["eur"] = round(max(0.0, _minute_na_ucel(con, teraz) - pred_behom), 6)
        if not vysledok["eur"] and not vysledok["zahriatych"] and not vysledok["zlyhanych"]:
            # Beh, ktorý nespotreboval ani token (všetko bolo hotové, alebo
            # nebolo z čoho skladať), nesmie zabrať miesto v týždennom počte
            # behov. Strop je poistka proti MÍŇANIU v slučke — a tu sa nemíňalo.
            naklady.uvolni_beh(con, UCEL, teraz=teraz)
        _zapis_beh(con, tyzden, teraz, vysledok)
    except Exception:
        # Predpočet je zrýchlenie. Keď zlyhá čokoľvek nečakané, appka skladá
        # plány naživo presne ako doteraz a dozorca ide ďalej.
        LOG.warning("predpočet skončil chybou", exc_info=True)
        vysledok["dovod"] = DOVOD_CHYBY
    finally:
        if con is not None:
            try:
                con.close()
            except sqlite3.Error:
                pass
    return vysledok


def _minute_na_ucel(con, teraz) -> float:
    try:
        _, _, tyzden = naklady._obdobia(teraz)
        return naklady.spolu_za_ucel_tyzden(con, UCEL, tyzden)
    except (sqlite3.Error, OSError):
        return 0.0


def _zapis_beh(con, tyzden, teraz, vysledok, zaciatok=True) -> None:
    """Zapíš, ako beh dopadol. Súčty sa PRIPOČÍTAVAJÚ — týždeň môže mať viac
    behov a majiteľa zaujíma, čo sa za týždeň zahrialo dokopy."""
    cas = teraz.isoformat(timespec="seconds")
    try:
        with con:
            con.execute(
                """INSERT INTO predpocet_behy
                     (tyzden, zaciatok, koniec, behov, zahriatych, preskocenych,
                      zlyhanych, eur, zasahy, dovod)
                   VALUES (?, ?, ?, 1, ?, ?, ?, ?, 0, ?)
                   ON CONFLICT(tyzden) DO UPDATE SET
                     koniec = excluded.koniec,
                     behov = behov + 1,
                     zahriatych = zahriatych + excluded.zahriatych,
                     preskocenych = preskocenych + excluded.preskocenych,
                     zlyhanych = zlyhanych + excluded.zlyhanych,
                     eur = eur + excluded.eur,
                     dovod = excluded.dovod""",
                (tyzden, cas if zaciatok else None, cas, vysledok["zahriatych"],
                 vysledok["preskocenych"], vysledok["zlyhanych"], vysledok["eur"],
                 vysledok["dovod"]),
            )
            con.execute("DELETE FROM predpocet_behy WHERE tyzden < ?",
                        ((datetime.date.fromisoformat(tyzden)
                          - datetime.timedelta(weeks=DRZANIE_DOPYTU_TYZDNOV)).isoformat(),))
    except (sqlite3.Error, OSError, ValueError):
        LOG.debug("beh predpočtu sa nezapísal", exc_info=True)


def zapocitaj_zasah(con, podpis, variant, tyzden) -> None:
    """Zdieľaný plán sa podal — a bol predpočítaný, takže sa niečo ušetrilo.

    Pripočíta sa len vtedy, keď riadok naozaj vznikol v noci. Diagnostika
    nikdy nesmie zhodiť odpoveď používateľovi, tak sa každá chyba prehltne.
    """
    try:
        con.execute(
            """UPDATE predpocet_behy SET zasahy = zasahy + 1
               WHERE tyzden = ?
                 AND (SELECT predpocitany FROM plany_zdielane
                      WHERE podpis = ? AND variant = ?) = 1""",
            (str(tyzden), podpis, variant),
        )
    except (sqlite3.Error, OSError):
        LOG.debug("zásah do predpočítaného plánu sa nezapočítal", exc_info=True)


# ---------------------------------------------------------------- prehľad
def stav(con, teraz=None) -> dict:
    """Funguje to? Koľko to stálo? Koľko čakania to ušetrilo?

    Diagnostika — nesmie zhodiť /api/health ani /api/naklady, tak sa pokazená
    evidencia prizná v `chyba` namiesto toho, aby predstierala nulu.
    """
    teraz = teraz or datetime.datetime.now()
    tyzden = (teraz.date() - datetime.timedelta(days=teraz.date().weekday())).isoformat()
    prehlad = {
        "tyzden": tyzden,
        "zapnuty": je_zapnuty(),
        "profilov": pocet_profilov(),
        "cena_za_profil_eur": CENA_ZA_PROFIL_EUR,
        # Koľko by stál plný beh pri terajšom nastavení — číslo, podľa ktorého
        # sa majiteľ rozhoduje, či zahrievať viac alebo menej.
        "odhad_plneho_behu_eur": round(pocet_profilov() * CENA_ZA_PROFIL_EUR, 6),
        "zahriatych": 0,
        "preskocenych": 0,
        "zlyhanych": 0,
        "eur": 0.0,
        "skutocna_cena_za_profil_eur": None,
        "usetrenych_generovani": 0,
        "hotovych_planov": 0,
        "posledny_beh": None,
        "dovod": None,
        "vysvetlenie": None,
        "chyba": None,
    }
    try:
        riadok = con.execute(
            "SELECT * FROM predpocet_behy WHERE tyzden = ?", (tyzden,)
        ).fetchone()
        if riadok is not None:
            prehlad.update({
                "zahriatych": int(riadok["zahriatych"]),
                "preskocenych": int(riadok["preskocenych"]),
                "zlyhanych": int(riadok["zlyhanych"]),
                "eur": round(float(riadok["eur"]), 6),
                "usetrenych_generovani": int(riadok["zasahy"]),
                "posledny_beh": riadok["koniec"],
                "dovod": riadok["dovod"],
                "vysvetlenie": VYSVETLENIE.get(riadok["dovod"]),
            })
            if prehlad["zahriatych"]:
                # Nameraná cena, nie odhad — toto je to číslo, ktoré rozhoduje.
                prehlad["skutocna_cena_za_profil_eur"] = round(
                    prehlad["eur"] / prehlad["zahriatych"], 6)
        prehlad["hotovych_planov"] = int(con.execute(
            "SELECT COUNT(*) FROM plany_zdielane WHERE tyzden = ? AND predpocitany = 1",
            (tyzden,),
        ).fetchone()[0])
    except (sqlite3.Error, OSError) as chyba:
        prehlad["chyba"] = f"{type(chyba).__name__}: {chyba}"[:200]
    return prehlad


# ---------------------------------------------------------------- príkazový riadok
NAPOVEDA = """Predpočítanie zdieľaných jedálničkov (zahrievanie cache).

Použitie na serveri:
    cd /opt/uvarsi/app
    UVARSI_URL=https://uvar.si ../venv/bin/python predpocet.py --zahrej
    UVARSI_URL=https://uvar.si ../venv/bin/python predpocet.py --stav

Koľko sa zahrieva: UVARSI_PREDPOCET_PROFILOV (východzie %d, strop %d).
Vypnutie:          UVARSI_PREDPOCET=0
Rezerva pre ľudí:  UVARSI_PREDPOCET_REZERVA_EUR (východzie %.2f €)
""" % (VYCHODZI_POCET_PROFILOV, MAX_POCET_PROFILOV, VYCHODZIA_REZERVA_EUR)


def cli(argv=None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="predpocet.py", description=NAPOVEDA,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--zahrej", action="store_true",
                        help="poskladaj dopredu najžiadanejšie plány")
    parser.add_argument("--stav", action="store_true",
                        help="ako sa predpočtu tento týždeň darilo")
    parser.add_argument("--pocet", type=int, default=None,
                        help="koľko plánov zahriať (inak UVARSI_PREDPOCET_PROFILOV)")
    argumenty = parser.parse_args(argv)

    if argumenty.stav:
        import server
        with closing(server.db()) as con:
            migrate_predpocet_schema(con)
            prehlad = stav(con)
        for kluc, hodnota in prehlad.items():
            print(f"  {kluc}: {hodnota}")
        return 0

    if not argumenty.zahrej:
        parser.print_help()
        return 2

    vysledok = zahrej(pocet=argumenty.pocet)
    print(
        f"Predpočet {vysledok['tyzden']}: zahriatych {vysledok['zahriatych']}, "
        f"preskočených {vysledok['preskocenych']}, zlyhaných {vysledok['zlyhanych']}, "
        f"minuté {vysledok['eur']:.4f} € — {VYSVETLENIE.get(vysledok['dovod'], vysledok['dovod'])}"
    )
    # Návratový kód je 0 aj vtedy, keď sa nezahrialo nič. Predpočet je
    # zrýchlenie, nie povinnosť — jeho neúspech nesmie zhodiť dozorcu.
    return 0


if __name__ == "__main__":
    raise SystemExit(cli())
