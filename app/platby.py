"""Platobná vrstva Uvar.si — jednorazové Zakladajúce členstvo cez LemonSqueezy.

Tri pravidlá, ktoré tento modul drží:

1. Vypínač. `PLATBY_ZAPNUTE` je v predvolenom stave vypnutý. Kým ho majiteľ
   vedome nezapne, žiadna platba nevznikne a žiadna adresa poskytovateľa sa ani
   nezostaví.
2. Nárok je vždy riadok v tabuľke `naroky` a nikde inde. Z internetu ho vie
   vytvoriť jedine podpísaný webhook; druhá — a jediná ďalšia — cesta je
   `udel_narok_rucne()`, ktorú spustí majiteľ pri databáze, keď si potrebuje
   Premium vyskúšať s vypnutými platbami. Klient o svojom nároku nepovie nič,
   čomu by sa verilo.
3. Tajomstvá sa sem odovzdávajú z prostredia ako argumenty, nikdy sa neukladajú
   ani nevypisujú. Modul zámerne neobsahuje žiadny výstup.

LemonSqueezy je merchant of record, takže EU DPH/OSS rieši on. Uvar.si si drží
len záznam o tom, kto má nárok a prečo.

Štvrté pravidlo pribudlo po audite pred spustením platieb: **peniaze sa nesmú
stratiť ani vtedy, keď zlyhá doručenie.** Webhook, ktorý nedorazí, je bežná vec
a doteraz znamenal natrvalo stratený nárok. Odpoveďou sú tri veci v tomto module:

  * `odloz_webhook()` / `spracuj_odlozene()` — telo požiadavky sa uloží tak, ako
    prišlo (aj s podpisom), aj keď je vypínač vypnutý. Podpis sa overuje až pri
    spracovaní, takže odloženie nič neoslabuje.
  * `payload_z_objednavky()` — objednávka z API poskytovateľa sa prepíše do
    presne toho istého tvaru, v akom chodí webhook, a spracuje sa tou istou
    cestou. Rekonciliácia tak dedí idempotenciu, kontrolu kapacity aj UNIQUE
    obmedzenia; nič sa neobchádza (skript app/rekonciliacia.py).
  * `priprav_upozornenie()` — text pre majiteľa. Modul ho len **poskladá**;
    odosiela ho volajúci. Ntfy kanál je natvrdo v repozitári, takže do týchto
    správ nesmie prísť e-mail, token ani iný osobný údaj.
"""
import datetime
import hashlib
import hmac
import json
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


POSKYTOVATEL = "lemonsqueezy"
# Nárok, ktorý neudelila platba, ale majiteľ pri databáze. Kým je vypínač
# vypnutý, je to jediná cesta k Premium — a v tabuľke je na prvý pohľad vidieť,
# že sa zaň neplatilo (nulová suma, žiadna mena).
POSKYTOVATEL_RUCNE = "rucne"
PRODUKT_ZAKLADAJUCI = "zakladajuci_clen"
KAPACITA_ZAKLADAJUCICH = 250

STAV_AKTIVNY = "aktivny"
STAV_VRATENY = "vrateny"
STAV_ZRUSENY = "zruseny"
STAV_NAD_KAPACITU = "nad_kapacitu"
# Druhá platba toho istého človeka. Nárok už má, takže druhý aktívny riadok by
# neprešiel ani cez UNIQUE index — ale peniaze prišli a musí ich byť vidieť,
# inak ich nemá kto vrátiť.
STAV_DUPLICITNY = "duplicitny"

AKCIA_UDELENE = "udelene"
AKCIA_UZ_UDELENE = "uz_udelene"
AKCIA_UZ_SPRACOVANE = "uz_spracovane"
AKCIA_VRATENE = "vratene"
AKCIA_ZRUSENE = "zrusene"
AKCIA_NAD_KAPACITU = "nad_kapacitu"
AKCIA_IGNOROVANE = "ignorovane"
AKCIA_ODLOZENE = "odlozene"

# Odkiaľ udalosť prišla. Bez toho sa v účtovníctve nedá odlíšiť, čo dorazilo
# webhookom a čo muselo dobehnúť rekonciliáciou — a práve to je miera toho,
# ako spoľahlivo doručovanie funguje.
ZDROJ_WEBHOOK = "webhook"
ZDROJ_ODLOZENE = "odlozene"
ZDROJ_REKONCILIACIA = "rekonciliacia"

UDALOST_UDELUJUCA = "order_created"
UDALOSTI_ODOBERAJUCE = {
    "order_refunded": (STAV_VRATENY, AKCIA_VRATENE),
    "subscription_cancelled": (STAV_ZRUSENY, AKCIA_ZRUSENE),
}

SPRAVA_VYPNUTE = "Platby zatiaľ nie sú spustené."
SPRAVA_NENASTAVENE = "Platobná brána zatiaľ nie je nastavená."
SPRAVA_UZ_MAS = "Zakladajúce členstvo už máš aktívne."
SPRAVA_VYPREDANE = f"Všetkých {KAPACITA_ZAKLADAJUCICH} zakladajúcich miest je obsadených."
SPRAVA_NEPLATNY_PODPIS = "Neplatný podpis."
SPRAVA_VELKE_TELO = "Telo požiadavky je príliš veľké."
SPRAVA_POKAZENE_TELO = "Neplatné telo požiadavky."
SPRAVA_NEPRIRADITELNA = "Udalosť sa nedá priradiť k účtu."
SPRAVA_AKTIVNE = "Máš aktívne zakladajúce členstvo."
# Čo sa dozvie ZÁKAZNÍK, ktorý zaplatil a miesto už nebolo. Peniaze bez
# protihodnoty sú aj podľa európskych pravidiel problém, takže mlčať sa nedá:
# vieme o tom, vraciame to a človek nemusí nič robiť.
SPRAVA_NAD_KAPACITU_ZAKAZNIK = (
    "Tvoja platba dorazila, ale posledné zakladajúce miesto medzitým obsadil "
    "niekto iný. Členstvo ti preto nevieme dať a celú sumu ti vrátime späť na "
    "ten istý spôsob platby — nemusíš nič robiť, ozveme sa ti e-mailom. "
    "Mrzí nás to."
)
SPRAVA_DUPLICITA_ZAKAZNIK = (
    "Zakladajúce členstvo už máš aktívne, no zaevidovali sme od teba ďalšiu "
    "platbu. Je to omyl, ktorý ideme napraviť: sumu navyše ti vrátime späť na "
    "ten istý spôsob platby. Nemusíš nič robiť."
)
MAIL_PREDMET_NAD_KAPACITU = "Uvar.si: platbu ti vraciame"
MAIL_PREDMET_DUPLICITA = "Uvar.si: platbu navyše ti vraciame"

_PRAVDIVE = frozenset({"1", "true", "ano", "áno", "yes", "on", "zapnute", "zapnuté"})
_MAX_PODPIS = 256
# LemonSqueezy posiela desiatky kB; nad týmto je to buď omyl, alebo útok.
MAX_TELO_WEBHOOKU = 256 * 1024
# Do skladu odložených tiel sa ukladá aj neoverené telo (podpis sa dá overiť až
# vtedy, keď majiteľ tajomstvo nastaví), takže strop musí byť prísnejší: nikto
# nesmie vedieť zaplniť disk tým, že appke pošle 256 kB smetí.
MAX_TELO_ODLOZENE = 64 * 1024
MAX_ODLOZENYCH = 200
# Ako dlho sa držia kľúče spracovaných udalostí. Pol roka pokryje každú
# reklamáciu; nárok samotný sa nemaže NIKDY.
UDALOSTI_PONECHAJ_DNI = 180
_ID_ZNAKY = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"
)
_HEX = frozenset("0123456789abcdefABCDEF")

PLATBY_SCHEMA = """
CREATE TABLE IF NOT EXISTS naroky (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL,
  produkt TEXT NOT NULL,
  poskytovatel TEXT NOT NULL,
  objednavka_id TEXT NOT NULL,
  suma_centy INTEGER,
  mena TEXT,
  stav TEXT NOT NULL,
  ziskany_o REAL NOT NULL,
  zmeneny_o REAL NOT NULL,
  UNIQUE(poskytovatel, objednavka_id)
);
CREATE INDEX IF NOT EXISTS naroky_user_idx ON naroky(user_id, produkt, stav);
CREATE UNIQUE INDEX IF NOT EXISTS naroky_jeden_aktivny_idx
  ON naroky(user_id, produkt) WHERE stav='aktivny';
CREATE TABLE IF NOT EXISTS platobne_udalosti (
  udalost_kluc TEXT PRIMARY KEY,
  event_id TEXT,
  typ TEXT NOT NULL,
  prijate_o REAL NOT NULL,
  zdroj TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS platobne_udalosti_event_idx
  ON platobne_udalosti(event_id) WHERE event_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS platobne_udalosti_cas_idx ON platobne_udalosti(prijate_o);
-- Sklad tiel, ktoré sa (zatiaľ) nedali spracovať. Podpis sa NEOVERUJE pri
-- ukladaní — na to treba tajomstvo, ktoré pri vypnutých platbách zámerne
-- nečítame — ale overí sa pred každým spracovaním. Kľúčom je hash tela, takže
-- opakované doručenie tej istej udalosti sklad nezaplní.
CREATE TABLE IF NOT EXISTS platobne_odlozene (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  telo_hash TEXT NOT NULL UNIQUE,
  telo BLOB NOT NULL,
  podpis TEXT,
  dovod TEXT NOT NULL,
  prijate_o REAL NOT NULL,
  spracovane_o REAL,
  vysledok TEXT
);
CREATE INDEX IF NOT EXISTS platobne_odlozene_cakajuce_idx
  ON platobne_odlozene(spracovane_o, id);
-- „Práve raz“ pre upozornenia majiteľovi. Primárny kľúč je celá záruka:
-- druhý pokus o ten istý kľúč sa ticho zahodí a notifikácia už neodíde.
CREATE TABLE IF NOT EXISTS platobne_upozornenia (
  kluc TEXT PRIMARY KEY,
  poslane_o REAL NOT NULL
);
"""


class PlatbyNenastavene(RuntimeError):
    """Majiteľ ešte nedodal platnú konfiguráciu poskytovateľa."""


class UdalostNepouzitelna(RuntimeError):
    """Podpísaná udalosť sa nedá priradiť k účtu alebo objednávke."""


def migrate_platby_schema(con) -> None:
    """Aditívne vytvorí platobné tabuľky; na existujúcej databáze nič neprepíše."""
    con.executescript(PLATBY_SCHEMA)
    _doplni_stlpec(con, "platobne_udalosti", "zdroj", "TEXT")
    _zjednot_casy(con)


def _doplni_stlpec(con, tabulka, stlpec, typ) -> None:
    existujuce = {row[1] for row in con.execute(f"PRAGMA table_info({tabulka})")}
    if stlpec not in existujuce:
        con.execute(f"ALTER TABLE {tabulka} ADD COLUMN {stlpec} {typ}")


# Historická diera: premium_cli.py posielal do REAL stĺpca `datetime`, ktoré
# SQLite prijalo len cez zastaraný adaptér (v novšom Pythone zmizne) a uložilo
# ako ISO text. V jednom stĺpci tak boli float aj text a `ORDER BY ziskany_o`
# ich radil vedľa seba nezmyselne. Prepis je jednorazový a bezpečný: prepisuje
# sa len to, čo SQLite vie prečítať ako čas.
_TEXTOVE_CASY = (
    ("naroky", "ziskany_o"),
    ("naroky", "zmeneny_o"),
    ("platobne_udalosti", "prijate_o"),
)


def _zjednot_casy(con) -> None:
    for tabulka, stlpec in _TEXTOVE_CASY:
        con.execute(
            f"""UPDATE {tabulka}
                   SET {stlpec} = CAST(strftime('%s', {stlpec}) AS REAL)
                 WHERE typeof({stlpec}) = 'text'
                   AND strftime('%s', {stlpec}) IS NOT NULL"""
        )


def _cas(hodnota) -> float:
    """Jeden typ času pre celý modul: sekundy od epochy ako float.

    Volajúci smie poslať epochu aj `datetime` — do databázy ide vždy číslo.
    """
    if isinstance(hodnota, bool):
        raise ValueError("neplatný čas")
    if isinstance(hodnota, (int, float)):
        return float(hodnota)
    if isinstance(hodnota, datetime.datetime):
        return hodnota.timestamp()
    if isinstance(hodnota, datetime.date):
        return datetime.datetime(hodnota.year, hodnota.month, hodnota.day).timestamp()
    if isinstance(hodnota, str):
        try:
            return datetime.datetime.fromisoformat(hodnota.strip()).timestamp()
        except ValueError:
            raise ValueError("neplatný čas")
    raise ValueError("neplatný čas")


def _den(cas: float) -> str:
    return datetime.datetime.fromtimestamp(
        cas, datetime.timezone.utc
    ).date().isoformat()


# ---------------------------------------------------------------- vypínač
def platby_zapnute(hodnota) -> bool:
    """Vypnuté, kým majiteľ nenapíše jednoznačné áno. Čokoľvek iné = vypnuté."""
    if not isinstance(hodnota, str):
        return False
    return hodnota.strip().casefold() in _PRAVDIVE


# ---------------------------------------------------------------- pokladňa
def checkout_url(zaklad, *, user_id: int, email=None) -> str:
    """Zostaví adresu pokladne s id používateľa v custom data (žiadne volanie von)."""
    if not isinstance(zaklad, str) or not zaklad.strip():
        raise PlatbyNenastavene("chýba adresa pokladne")
    casti = urlsplit(zaklad.strip())
    if casti.scheme != "https" or not casti.netloc:
        raise PlatbyNenastavene("adresa pokladne musí byť https")
    if not isinstance(user_id, int) or isinstance(user_id, bool) or user_id <= 0:
        raise ValueError("neplatné id používateľa")
    parametre = [
        (kluc, hodnota)
        for kluc, hodnota in parse_qsl(casti.query, keep_blank_values=True)
        if not kluc.startswith("checkout[custom]")
    ]
    parametre.append(("checkout[custom][user_id]", str(user_id)))
    parametre.append(("checkout[custom][produkt]", PRODUKT_ZAKLADAJUCI))
    if isinstance(email, str) and email:
        parametre.append(("checkout[email]", email))
    return urlunsplit((casti.scheme, casti.netloc, casti.path, urlencode(parametre), ""))


# ---------------------------------------------------------------- podpis
def overit_podpis(*, tajomstvo, telo: bytes, podpis) -> bool:
    """HMAC-SHA256 nad surovým telom, porovnanie v konštantnom čase.

    Chýbajúce tajomstvo znamená „neoverené“ — nie „prejde všetko“.
    """
    if not isinstance(tajomstvo, str) or not tajomstvo:
        return False
    if not isinstance(podpis, str) or not podpis or len(podpis) > _MAX_PODPIS:
        return False
    ocakavany = hmac.new(tajomstvo.encode("utf-8"), bytes(telo), hashlib.sha256).hexdigest()
    prijaty = podpis.strip()
    if not prijaty.isascii():
        return False
    return hmac.compare_digest(ocakavany, prijaty.lower())


# ---------------------------------------------------------------- čítanie stavu
def pocet_zakladajucich(con) -> int:
    """Skutočný počet udelených miest — nikdy odhad."""
    riadok = con.execute(
        "SELECT COUNT(*) FROM naroky WHERE produkt=? AND stav=?",
        (PRODUKT_ZAKLADAJUCI, STAV_AKTIVNY),
    ).fetchone()
    return int(riadok[0]) if riadok else 0


def volne_miesta(con) -> int:
    return max(0, KAPACITA_ZAKLADAJUCICH - pocet_zakladajucich(con))


def ma_narok(con, user_id: int) -> bool:
    riadok = con.execute(
        "SELECT 1 FROM naroky WHERE user_id=? AND produkt=? AND stav=?",
        (user_id, PRODUKT_ZAKLADAJUCI, STAV_AKTIVNY),
    ).fetchone()
    return riadok is not None


def platba_bez_protihodnoty(con, user_id: int):
    """Zaplatil, ale nárok z toho nie je. Vráti stav takého riadku, alebo None.

    Presne toto je situácia, o ktorej sa zákazník MUSÍ dozvedieť: peniaze odišli
    a služba za ne nie je. Riadok existuje práve preto, aby sa dala dohľadať a
    vrátiť — a aby appka vedela povedať pravdu namiesto mlčania.
    """
    riadok = con.execute(
        """SELECT stav FROM naroky
           WHERE user_id=? AND produkt=? AND poskytovatel<>? AND stav IN (?, ?)
           ORDER BY id DESC LIMIT 1""",
        (user_id, PRODUKT_ZAKLADAJUCI, POSKYTOVATEL_RUCNE,
         STAV_NAD_KAPACITU, STAV_DUPLICITNY),
    ).fetchone()
    return riadok[0] if riadok else None


def stav_platieb(con, *, user_id: int, zapnute: bool) -> dict:
    obsadene = pocet_zakladajucich(con)
    volne = max(0, KAPACITA_ZAKLADAJUCICH - obsadene)
    narok = ma_narok(con, user_id)
    bez_protihodnoty = platba_bez_protihodnoty(con, user_id)
    if not zapnute:
        sprava = SPRAVA_VYPNUTE
    elif narok:
        sprava = SPRAVA_AKTIVNE
    elif volne == 0:
        sprava = SPRAVA_VYPREDANE
    else:
        sprava = f"Zostáva {volne} z {KAPACITA_ZAKLADAJUCICH} zakladajúcich miest."
    # Kto zaplatil a nič nedostal, nesmie na obrazovke vidieť „vypredané“ ako
    # ktokoľvek iný. Jeho situácia je iná a text to musí povedať priamo.
    upozornenie = None
    if bez_protihodnoty == STAV_NAD_KAPACITU and not narok:
        upozornenie = SPRAVA_NAD_KAPACITU_ZAKAZNIK
        sprava = SPRAVA_NAD_KAPACITU_ZAKAZNIK
    elif bez_protihodnoty == STAV_DUPLICITNY:
        upozornenie = SPRAVA_DUPLICITA_ZAKAZNIK
    return {
        "platby_zapnute": zapnute,
        "ma_narok": narok,
        "produkt": PRODUKT_ZAKLADAJUCI,
        "kapacita": KAPACITA_ZAKLADAJUCICH,
        "obsadene": obsadene,
        "volne_miesta": volne,
        "sprava": sprava,
        "platba_bez_miesta": bez_protihodnoty == STAV_NAD_KAPACITU and not narok,
        "upozornenie": upozornenie,
    }


# ---------------------------------------------------------------- čítanie udalosti
def _bezpecne_id(hodnota):
    if isinstance(hodnota, int) and not isinstance(hodnota, bool):
        hodnota = str(hodnota)
    if not isinstance(hodnota, str):
        return None
    hodnota = hodnota.strip()
    if not hodnota or len(hodnota) > 128 or not set(hodnota) <= _ID_ZNAKY:
        return None
    return hodnota


def _meta(payload):
    meta = payload.get("meta")
    return meta if isinstance(meta, dict) else {}


def _data(payload):
    data = payload.get("data")
    return data if isinstance(data, dict) else {}


def _atributy(payload):
    atributy = _data(payload).get("attributes")
    return atributy if isinstance(atributy, dict) else {}


def typ_udalosti(payload) -> str:
    nazov = _meta(payload).get("event_name")
    if not isinstance(nazov, str):
        return ""
    nazov = nazov.strip()
    return nazov if len(nazov) <= 64 and nazov.replace("_", "").isalnum() else ""


def objednavka_ref(payload):
    """Id objednávky: pri predplatnom je to pôvodná objednávka, nie id predplatného."""
    data = _data(payload)
    if str(data.get("type") or "").startswith("subscription"):
        return _bezpecne_id(_atributy(payload).get("order_id")) or _bezpecne_id(data.get("id"))
    return _bezpecne_id(data.get("id"))


def surove_id_udalosti(payload):
    meta = _meta(payload)
    for kluc in ("event_id", "webhook_id", "id"):
        kandidat = _bezpecne_id(meta.get(kluc))
        if kandidat:
            return kandidat
    return None


def udalost_kluc(payload) -> str:
    """Kľúč idempotencie: odolný aj keď poskytovateľ zopakuje doručenie s novým id."""
    typ = typ_udalosti(payload)
    ref = objednavka_ref(payload)
    if typ and ref:
        return f"{typ}:{POSKYTOVATEL}:{ref}"
    surove = surove_id_udalosti(payload)
    if typ and surove:
        return f"{typ}:meta:{surove}"
    raise UdalostNepouzitelna("z udalosti sa nedá odvodiť kľúč")


def custom_user_id(payload):
    custom = _meta(payload).get("custom_data")
    if not isinstance(custom, dict):
        return None
    hodnota = custom.get("user_id")
    if isinstance(hodnota, bool):
        return None
    if isinstance(hodnota, str):
        hodnota = hodnota.strip()
        if not hodnota.isdigit() or len(hodnota) > 18:
            return None
        hodnota = int(hodnota)
    if not isinstance(hodnota, int) or hodnota <= 0:
        return None
    return hodnota


def _suma(payload):
    atributy = _atributy(payload)
    total = atributy.get("total")
    suma = total if isinstance(total, int) and not isinstance(total, bool) and 0 <= total <= 10 ** 9 else None
    mena = atributy.get("currency")
    if isinstance(mena, str) and len(mena) == 3 and mena.isascii() and mena.isalpha():
        mena = mena.upper()
    else:
        mena = None
    return suma, mena


def _variant(payload):
    atributy = _atributy(payload)
    polozka = atributy.get("first_order_item")
    if isinstance(polozka, dict):
        kandidat = _bezpecne_id(polozka.get("variant_id"))
        if kandidat:
            return kandidat
    return _bezpecne_id(atributy.get("variant_id"))


# ---------------------------------------------------------------- spracovanie
def spracuj_udalost(con, *, payload, now, variant_id=None, zdroj=ZDROJ_WEBHOOK) -> dict:
    """Jedna udalosť = jedna transakcia. Idempotentné a bezpečné voči pretekom.

    Celý beh je v BEGIN IMMEDIATE, takže dve súbežné doručenia sa serializujú a
    kontrola kapacity vidí vždy skutočný počet udelených miest.

    Idempotencia stojí na `udalost_kluc()`, a ten sa pre udeľujúcu udalosť
    skladá z typu a **id objednávky** — nie z id doručenia. Tá istá objednávka
    má preto ten istý kľúč, nech príde webhookom, opakovaným webhookom alebo
    rekonciliáciou z API. Druhý pokus skončí na `_uz_spracovane` a keby aj
    neskončil (napr. po upratovaní starých kľúčov), `_udel` narazí na UNIQUE
    (poskytovatel, objednavka_id). Dve poistky, nie jedna.

    Vracia okrem akcie aj to, čoho sa týkala — volajúci z toho skladá
    upozornenie majiteľovi a správu zákazníkovi. Von z appky sa z tohto slovníka
    posiela len `akcia`.
    """
    if not isinstance(payload, dict):
        raise UdalostNepouzitelna("telo nie je objekt")
    now = _cas(now)
    typ = typ_udalosti(payload)
    kluc = udalost_kluc(payload)
    surove = surove_id_udalosti(payload)

    if con.in_transaction:
        con.commit()
    con.execute("BEGIN IMMEDIATE")
    try:
        if _uz_spracovane(con, kluc, surove):
            con.commit()
            return {"akcia": AKCIA_UZ_SPRACOVANE, "typ": typ,
                    "objednavka": objednavka_ref(payload), "user_id": None,
                    "stav": None, "zdroj": zdroj}
        con.execute(
            """INSERT INTO platobne_udalosti (udalost_kluc, event_id, typ, prijate_o, zdroj)
               VALUES (?, ?, ?, ?, ?)""",
            (kluc, surove, typ, now, zdroj),
        )
        if typ == UDALOST_UDELUJUCA:
            vysledok = _udel(con, payload, now, variant_id)
        elif typ in UDALOSTI_ODOBERAJUCE:
            vysledok = _odober(con, payload, now, typ)
        else:
            vysledok = {"akcia": AKCIA_IGNOROVANE}
        con.commit()
        return {
            "akcia": vysledok["akcia"],
            "typ": typ,
            "objednavka": vysledok.get("objednavka", objednavka_ref(payload)),
            "user_id": vysledok.get("user_id"),
            "stav": vysledok.get("stav"),
            "zdroj": zdroj,
        }
    except Exception:
        if con.in_transaction:
            con.rollback()
        raise


def _uz_spracovane(con, kluc, surove) -> bool:
    if con.execute(
        "SELECT 1 FROM platobne_udalosti WHERE udalost_kluc=?", (kluc,)
    ).fetchone():
        return True
    if surove and con.execute(
        "SELECT 1 FROM platobne_udalosti WHERE event_id=?", (surove,)
    ).fetchone():
        return True
    return False


def _udel(con, payload, now, ocakavany_variant):
    objednavka = objednavka_ref(payload)
    if ocakavany_variant:
        if _variant(payload) != _bezpecne_id(ocakavany_variant):
            return {"akcia": AKCIA_IGNOROVANE, "objednavka": objednavka}

    if not objednavka:
        raise UdalostNepouzitelna("chýba id objednávky")
    user_id = custom_user_id(payload)
    if user_id is None:
        raise UdalostNepouzitelna("chýba user_id v custom data")
    if con.execute("SELECT 1 FROM pouzivatelia WHERE id=?", (user_id,)).fetchone() is None:
        raise UdalostNepouzitelna("neznámy účet")

    if con.execute(
        "SELECT 1 FROM naroky WHERE poskytovatel=? AND objednavka_id=?",
        (POSKYTOVATEL, objednavka),
    ).fetchone():
        # Tá istá objednávka už riadok má — nič nového sa nestalo.
        return {"akcia": AKCIA_UZ_UDELENE, "objednavka": objednavka, "user_id": user_id}

    suma, mena = _suma(payload)
    if ma_narok(con, user_id):
        # Ten istý človek zaplatil druhýkrát. Doteraz sa nezapísalo nič, takže
        # peniaze navyše v účtovníctve neexistovali a nemal ich kto vrátiť.
        # Riadok je celá oprava: druhý aktívny nárok by aj tak neprešiel cez
        # UNIQUE index, ale platba musí byť vidieť.
        con.execute(
            """INSERT INTO naroky (user_id, produkt, poskytovatel, objednavka_id,
                                   suma_centy, mena, stav, ziskany_o, zmeneny_o)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (user_id, PRODUKT_ZAKLADAJUCI, POSKYTOVATEL, objednavka, suma, mena,
             STAV_DUPLICITNY, now, now),
        )
        return {"akcia": AKCIA_UZ_UDELENE, "objednavka": objednavka,
                "user_id": user_id, "stav": STAV_DUPLICITNY}

    vypredane = pocet_zakladajucich(con) >= KAPACITA_ZAKLADAJUCICH
    stav = STAV_NAD_KAPACITU if vypredane else STAV_AKTIVNY
    con.execute(
        """INSERT INTO naroky (user_id, produkt, poskytovatel, objednavka_id,
                               suma_centy, mena, stav, ziskany_o, zmeneny_o)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (user_id, PRODUKT_ZAKLADAJUCI, POSKYTOVATEL, objednavka, suma, mena, stav, now, now),
    )
    if vypredane:
        # Peniaze prišli, miesto už nie je. Záznam ostáva dohľadateľný na vrátenie.
        return {"akcia": AKCIA_NAD_KAPACITU, "objednavka": objednavka,
                "user_id": user_id, "stav": STAV_NAD_KAPACITU}
    con.execute("UPDATE pouzivatelia SET platiaci=1 WHERE id=?", (user_id,))
    return {"akcia": AKCIA_UDELENE, "objednavka": objednavka, "user_id": user_id,
            "stav": STAV_AKTIVNY}


# ---------------------------------------------------------------- ručný nárok
# Premium sa musí dať vyskúšať aj s vypnutým vypínačom — inak by majiteľ svoj
# vlastný produkt neotestoval. Odpoveď je zámerne tá istá ako pri platbe: riadok
# v `naroky`. Žiadna premenná prostredia, žiadny zoznam e-mailov, žiadna cesta
# z internetu. Kto nemá prístup k databáze, Premium si neudelí.
def udel_narok_rucne(con, *, user_id, now) -> dict:
    """Udelí nárok bez platby. Volá sa ručne, nikdy nie z požiadavky."""
    _over_id_pouzivatela(user_id)
    now = _cas(now)
    if con.in_transaction:
        con.commit()
    con.execute("BEGIN IMMEDIATE")
    try:
        if con.execute("SELECT 1 FROM pouzivatelia WHERE id=?", (user_id,)).fetchone() is None:
            raise UdalostNepouzitelna("neznámy účet")
        if ma_narok(con, user_id):
            con.commit()
            return {"akcia": AKCIA_UZ_UDELENE}
        vypredane = pocet_zakladajucich(con) >= KAPACITA_ZAKLADAJUCICH
        stav = STAV_NAD_KAPACITU if vypredane else STAV_AKTIVNY
        con.execute(
            """INSERT INTO naroky (user_id, produkt, poskytovatel, objednavka_id,
                                   suma_centy, mena, stav, ziskany_o, zmeneny_o)
               VALUES (?, ?, ?, ?, 0, NULL, ?, ?, ?)""",
            (user_id, PRODUKT_ZAKLADAJUCI, POSKYTOVATEL_RUCNE,
             _dalsie_rucne_id(con, user_id), stav, now, now),
        )
        if not vypredane:
            con.execute("UPDATE pouzivatelia SET platiaci=1 WHERE id=?", (user_id,))
        con.commit()
    except Exception:
        if con.in_transaction:
            con.rollback()
        raise
    # Ani majiteľ nedostane 251. miesto: kapacita platí pre všetkých rovnako.
    return {"akcia": AKCIA_NAD_KAPACITU if vypredane else AKCIA_UDELENE}


def zrus_narok_rucne(con, *, user_id, now) -> dict:
    """Vezme späť ručne udelený nárok — aby sa dala vyskúšať aj bezplatná verzia.

    Zaplateného nároku sa nedotkne. Preklep v konzole tak nemôže zobrať Premium
    človeku, ktorý zaň poslal peniaze; na to slúži vrátenie cez poskytovateľa.
    """
    _over_id_pouzivatela(user_id)
    now = _cas(now)
    if con.in_transaction:
        con.commit()
    con.execute("BEGIN IMMEDIATE")
    try:
        riadok = con.execute(
            """SELECT id FROM naroky
               WHERE user_id=? AND produkt=? AND poskytovatel=? AND stav=?
               ORDER BY id DESC""",
            (user_id, PRODUKT_ZAKLADAJUCI, POSKYTOVATEL_RUCNE, STAV_AKTIVNY),
        ).fetchone()
        if riadok is None:
            con.commit()
            return {"akcia": AKCIA_IGNOROVANE}
        con.execute(
            "UPDATE naroky SET stav=?, zmeneny_o=? WHERE id=?", (STAV_ZRUSENY, now, riadok[0])
        )
        if not ma_narok(con, user_id):
            con.execute("UPDATE pouzivatelia SET platiaci=0 WHERE id=?", (user_id,))
        con.commit()
    except Exception:
        if con.in_transaction:
            con.rollback()
        raise
    return {"akcia": AKCIA_ZRUSENE}


def _over_id_pouzivatela(user_id) -> None:
    if not isinstance(user_id, int) or isinstance(user_id, bool) or user_id <= 0:
        raise ValueError("neplatné id používateľa")


def _dalsie_rucne_id(con, user_id) -> str:
    """Vlastné číslo pre každé udelenie, aby po zrušení šlo udeliť znova."""
    poradie = con.execute(
        "SELECT COUNT(*) FROM naroky WHERE user_id=? AND poskytovatel=?",
        (user_id, POSKYTOVATEL_RUCNE),
    ).fetchone()[0]
    return f"rucne-{user_id}-{poradie + 1}"


def _odober(con, payload, now, typ):
    novy_stav, akcia = UDALOSTI_ODOBERAJUCE[typ]
    riadok = None
    objednavka = objednavka_ref(payload)
    if objednavka:
        riadok = con.execute(
            """SELECT id, user_id FROM naroky
               WHERE poskytovatel=? AND objednavka_id=? AND stav=?""",
            (POSKYTOVATEL, objednavka, STAV_AKTIVNY),
        ).fetchone()
    if riadok is None:
        user_id = custom_user_id(payload)
        if user_id is not None:
            # Záložné dohľadanie podľa účtu MUSÍ byť obmedzené na poskytovateľa.
            # Bez toho by vrátenie jednej objednávky zobralo nárok, ktorý udelil
            # majiteľ ručne (poskytovateľ "rucne") — teda niečo, čo s tou
            # platbou nemá nič spoločné.
            riadok = con.execute(
                """SELECT id, user_id FROM naroky
                   WHERE user_id=? AND produkt=? AND poskytovatel=? AND stav=?
                   ORDER BY id DESC""",
                (user_id, PRODUKT_ZAKLADAJUCI, POSKYTOVATEL, STAV_AKTIVNY),
            ).fetchone()
    if riadok is None:
        return {"akcia": AKCIA_IGNOROVANE, "objednavka": objednavka}

    narok_id, user_id = riadok[0], riadok[1]
    con.execute(
        "UPDATE naroky SET stav=?, zmeneny_o=? WHERE id=?", (novy_stav, now, narok_id)
    )
    if not ma_narok(con, user_id):
        con.execute("UPDATE pouzivatelia SET platiaci=0 WHERE id=?", (user_id,))
    return {"akcia": akcia, "objednavka": objednavka, "user_id": user_id,
            "stav": novy_stav}


# ---------------------------------------------------------------- sklad tiel
# Vypnutý (alebo len zle nastavený) vypínač nesmie znamenať stratené peniaze.
# Poskytovateľ pri 503 doručenie pár ráz zopakuje a potom ho ZAHODÍ — zákazník
# zaplatil, appka sa to nikdy nedozvie a majiteľ sa o tom dozvie z reklamácie.
# Telo sa preto uloží tak, ako prišlo, aj s podpisom. Podpis sa overí až pri
# spracovaní, takže sa neoslabuje nič: z odloženého tela nemôže vzniknúť nárok
# skôr, než HMAC sadne.
def hodnoverny_podpis(podpis) -> bool:
    """Vyzerá to ako hlavička od poskytovateľa? (nie overenie, len filter smetí)"""
    if not isinstance(podpis, str):
        return False
    podpis = podpis.strip()
    return 32 <= len(podpis) <= _MAX_PODPIS and set(podpis) <= _HEX


def odloz_webhook(con, *, telo, podpis, now, dovod="platby_vypnute") -> dict:
    """Ulož surové telo na neskôr. Vracia, či pribudlo niečo nové.

    Kľúčom je hash tela: opakované doručenie tej istej udalosti sklad nezaplní.
    Strop `MAX_ODLOZENYCH` je tam preto, že telo sa ukladá NEOVERENÉ — nikto
    nesmie appke zaplniť disk tým, že jej pošle smeti.
    """
    telo = bytes(telo)
    now = _cas(now)
    if not telo or len(telo) > MAX_TELO_ODLOZENE:
        return {"ulozene": False, "nove": False, "dovod": "velke_telo"}
    cakajucich = con.execute(
        "SELECT COUNT(*) FROM platobne_odlozene WHERE spracovane_o IS NULL"
    ).fetchone()[0]
    if cakajucich >= MAX_ODLOZENYCH:
        return {"ulozene": False, "nove": False, "dovod": "plno",
                "cakajucich": cakajucich}
    kurzor = con.execute(
        """INSERT OR IGNORE INTO platobne_odlozene
               (telo_hash, telo, podpis, dovod, prijate_o)
           VALUES (?, ?, ?, ?, ?)""",
        (hashlib.sha256(telo).hexdigest(), telo,
         podpis if isinstance(podpis, str) else None, str(dovod), now),
    )
    con.commit()
    return {"ulozene": True, "nove": kurzor.rowcount == 1, "dovod": str(dovod),
            "cakajucich": cakajucich + (1 if kurzor.rowcount == 1 else 0)}


def pocet_cakajucich(con) -> int:
    return int(con.execute(
        "SELECT COUNT(*) FROM platobne_odlozene WHERE spracovane_o IS NULL"
    ).fetchone()[0])


def stav_dozoru(con) -> dict:
    """Čísla pre /api/health: čo visí a čaká na zásah. Žiadne osobné údaje.

    `nevybavene` je počet platieb, za ktoré zákazník nič nedostal a peniaze mu
    ešte neboli vrátené. Kým to číslo nie je nula, niekomu dlhujeme peniaze —
    a majiteľ to musí vidieť bez SSH.
    """
    nevybavene = con.execute(
        "SELECT COUNT(*) FROM naroky WHERE stav IN (?, ?)",
        (STAV_NAD_KAPACITU, STAV_DUPLICITNY),
    ).fetchone()[0]
    return {
        "obsadene": pocet_zakladajucich(con),
        "kapacita": KAPACITA_ZAKLADAJUCICH,
        "cakajucich_tiel": pocet_cakajucich(con),
        "nevybavene_vratky": int(nevybavene),
    }


def spracuj_odlozene(con, *, tajomstvo, now, variant_id=None, limit=MAX_ODLOZENYCH) -> dict:
    """Dobehni telá, ktoré čakali. Každé prejde overením podpisu, ako by prišlo teraz.

    Beží mimo requestu (rekonciliačný skript), takže tu už tajomstvo k dispozícii
    je. Telo s podpisom, ktorý nesedí, sa neudelí a označí sa — je to buď smeť
    z internetu, alebo majiteľ nastavil iné tajomstvo, než akým poskytovateľ
    podpisuje.
    """
    now = _cas(now)
    suhrn = {"spracovane": 0, "udelene": 0, "neplatny_podpis": 0,
             "nepouzitelne": 0, "pokazene": 0, "akcie": {}, "udalosti": []}
    riadky = con.execute(
        """SELECT id, telo, podpis FROM platobne_odlozene
           WHERE spracovane_o IS NULL ORDER BY id LIMIT ?""",
        (int(limit),),
    ).fetchall()
    for riadok in riadky:
        telo = bytes(riadok[1])
        vysledok = None
        if not overit_podpis(tajomstvo=tajomstvo, telo=telo, podpis=riadok[2]):
            vysledok = "neplatny_podpis"
            suhrn["neplatny_podpis"] += 1
        else:
            try:
                payload = json.loads(telo)
            except (ValueError, UnicodeDecodeError):
                vysledok = "pokazene_telo"
                suhrn["pokazene"] += 1
            else:
                try:
                    udalost = spracuj_udalost(
                        con, payload=payload, now=now, variant_id=variant_id,
                        zdroj=ZDROJ_ODLOZENE,
                    )
                except UdalostNepouzitelna:
                    vysledok = "nepouzitelna"
                    suhrn["nepouzitelne"] += 1
                else:
                    vysledok = udalost["akcia"]
                    suhrn["akcie"][vysledok] = suhrn["akcie"].get(vysledok, 0) + 1
                    suhrn["udalosti"].append(udalost)
                    if vysledok == AKCIA_UDELENE:
                        suhrn["udelene"] += 1
        con.execute(
            "UPDATE platobne_odlozene SET spracovane_o=?, vysledok=? WHERE id=?",
            (now, vysledok, riadok[0]),
        )
        con.commit()
        suhrn["spracovane"] += 1
    return suhrn


# ---------------------------------------------------------------- upratovanie
def uprac_udalosti(con, *, now, ponechaj_dni=UDALOSTI_PONECHAJ_DNI) -> int:
    """Zmaže staré kľúče udalostí a vybavené odložené telá. Nárokov sa nedotkne.

    `platobne_udalosti` inak rastie navždy. Mazať sa smie preto, že idempotencia
    nestojí len na tejto tabuľke: `_udel` narazí na UNIQUE (poskytovatel,
    objednavka_id) aj vtedy, keď kľúč udalosti už neexistuje.
    """
    hranica = _cas(now) - max(1, int(ponechaj_dni)) * 24 * 3600
    zmazane = con.execute(
        "DELETE FROM platobne_udalosti WHERE prijate_o < ?", (hranica,)
    ).rowcount
    con.execute(
        "DELETE FROM platobne_odlozene WHERE spracovane_o IS NOT NULL AND spracovane_o < ?",
        (hranica,),
    )
    con.execute("DELETE FROM platobne_upozornenia WHERE poslane_o < ?", (hranica,))
    con.commit()
    return int(zmazane or 0)


# ---------------------------------------------------------------- upozornenia
# Modul text len POSKLADÁ; odosiela ho volajúci (naklady.posli_ntfy). Ntfy topic
# je natvrdo v repozitári, teda verejne čitateľný — do týchto správ preto nesmie
# prísť e-mail, token, ani úryvok logu. Číslo objednávky áno: bez neho majiteľ
# nevie, čo má vrátiť, a samo o sebe o nikom nič neprezradí.
DRUH_NAD_KAPACITU = "nad_kapacitu"
DRUH_DUPLICITA = "duplicita"
DRUH_IGNOROVANE = "ignorovane"
DRUH_NEPOUZITELNA = "nepouzitelna"
DRUH_ODLOZENE = "odlozene"
DRUH_REKONCILIACIA = "rekonciliacia"
DRUH_BEZ_UCTU = "bez_uctu"
DRUHY_UPOZORNENI = (
    DRUH_NAD_KAPACITU, DRUH_DUPLICITA, DRUH_IGNOROVANE, DRUH_NEPOUZITELNA,
    DRUH_ODLOZENE, DRUH_REKONCILIACIA, DRUH_BEZ_UCTU,
)


def _cislo(hodnota) -> str:
    return str(_bezpecne_id(hodnota) or "?")


def _pocet(hodnota) -> int:
    try:
        return max(0, int(hodnota))
    except (TypeError, ValueError):
        return 0


def priprav_upozornenie(druh, *, den=None, objednavka=None, typ=None, pocet=None) -> dict:
    """Zloží titul, text a kľúč „práve raz“. Žiadne osobné údaje, žiadny log."""
    objednavka = _cislo(objednavka)
    typ = _cislo(typ)
    pocet = _pocet(pocet)
    den = den if isinstance(den, str) and den else "?"
    if druh == DRUH_NAD_KAPACITU:
        return {
            "kluc": f"{DRUH_NAD_KAPACITU}:{objednavka}",
            "titul": "Uvar.si: platba nad kapacitu — treba vrátiť peniaze",
            "sprava": (
                f"Objednávka {objednavka}: zákazník zaplatil, ale všetkých "
                f"{KAPACITA_ZAKLADAJUCICH} zakladajúcich miest je obsadených, "
                "takže členstvo nedostal. V appke aj e-mailom sme mu napísali, "
                "že sumu vrátime. Vráť platbu v LemonSqueezy (Orders → Refund). "
                "Riadok je v tabuľke naroky so stavom 'nad_kapacitu'."
            ),
        }
    if druh == DRUH_DUPLICITA:
        return {
            "kluc": f"{DRUH_DUPLICITA}:{objednavka}",
            "titul": "Uvar.si: druhá platba toho istého účtu",
            "sprava": (
                f"Objednávka {objednavka}: účet zakladajúce členstvo už mal, "
                "takže druhá platba je navyše. Zákazníkovi sme napísali, že mu "
                "ju vrátime. Vráť ju v LemonSqueezy (Orders → Refund); riadok "
                "je v tabuľke naroky so stavom 'duplicitny'."
            ),
        }
    if druh == DRUH_IGNOROVANE:
        return {
            "kluc": f"{DRUH_IGNOROVANE}:{typ}:{den}",
            "titul": "Uvar.si: platobná udalosť sa nespracovala",
            "sprava": (
                f"Udalosť typu '{typ}' prišla, ale appka s ňou nič neurobila "
                "(neznámy typ alebo cudzí variant). Ak to bola objednávka "
                "zakladajúceho členstva, sedí LEMON_VARIANT_ID? Podrobnosti sú "
                "v tabuľke platobne_udalosti. Ďalšie udalosti toho istého typu "
                "dnes už neohlásim."
            ),
        }
    if druh == DRUH_NEPOUZITELNA:
        return {
            "kluc": f"{DRUH_NEPOUZITELNA}:{den}",
            "titul": "Uvar.si: podpísaná platba sa nedá priradiť k účtu",
            "sprava": (
                "Prišla platba s platným podpisom, ktorú appka nevie priradiť "
                "k žiadnemu účtu (chýbajúce custom_data alebo neznáme id). "
                "Peniaze teda prišli a nikto za ne nič nedostal. Telo je "
                "uložené v tabuľke platobne_odlozene — pozri sa naň a nárok "
                "priraď ručne cez premium_cli.py, alebo platbu vráť."
            ),
        }
    if druh == DRUH_ODLOZENE:
        return {
            "kluc": f"{DRUH_ODLOZENE}:{den}",
            "titul": "Uvar.si: platba prišla s vypnutými platbami",
            "sprava": (
                f"Odložených tiel čaká na spracovanie: {pocet}. Poskytovateľ "
                "posiela udalosti, ale PLATBY_ZAPNUTE je vypnuté, takže sa nič "
                "neudeľuje. Nič sa nestratilo — po zapnutí to rekonciliácia "
                "dobehne. Skontroluj PLATBY_ZAPNUTE v /opt/uvarsi/uvarsi.env."
            ),
        }
    if druh == DRUH_BEZ_UCTU:
        return {
            "kluc": f"{DRUH_BEZ_UCTU}:{den}",
            "titul": "Uvar.si: zaplatené objednávky bez účtu",
            "sprava": (
                f"Rekonciliácia našla {pocet} zaplatených objednávok, ktoré sa "
                "nedajú priradiť k žiadnemu účtu (iná e-mailová adresa pri "
                "platbe než pri prihlásení). Nájdi ich v LemonSqueezy a nárok "
                "prideľ ručne: premium_cli.py <email>."
            ),
        }
    if druh == DRUH_REKONCILIACIA:
        return {
            "kluc": f"{DRUH_REKONCILIACIA}:{den}:{pocet}",
            "titul": "Uvar.si: rekonciliácia dobehla chýbajúce platby",
            "sprava": (
                f"Doplnených nárokov: {pocet}. Toľkokrát platba prišla, ale "
                "webhook nie — appka by sa o nej sama nedozvedela. Ak sa to "
                "opakuje, skontroluj v LemonSqueezy nastavenie webhooku "
                "(adresa https://uvar.si/api/platba/webhook a jeho históriu)."
            ),
        }
    raise ValueError("neznámy druh upozornenia")


def zaznamenaj_upozornenie(con, *, kluc, now) -> bool:
    """True práve raz pre daný kľúč. Druhý pokus je ticho — bez lavíny správ."""
    kurzor = con.execute(
        "INSERT OR IGNORE INTO platobne_upozornenia (kluc, poslane_o) VALUES (?, ?)",
        (str(kluc), _cas(now)),
    )
    con.commit()
    return kurzor.rowcount == 1


def upozornenie_raz(con, druh, *, now, **kw):
    """Poskladá upozornenie a vráti ho len vtedy, keď ešte neodišlo. Inak None."""
    sprava = priprav_upozornenie(druh, den=_den(_cas(now)), **kw)
    if not zaznamenaj_upozornenie(con, kluc=sprava["kluc"], now=now):
        return None
    return sprava


# ------------------------------------------------------- objednávka z API
# Rekonciliácia nesmie mať vlastnú cestu k udeleniu nároku — mala by vlastné
# chyby a vlastné diery. Objednávku z API preto len prepíšeme do tvaru, v akom
# chodí webhook, a pošleme ju tou istou `spracuj_udalost`.
def _atributy_objednavky(objednavka):
    atributy = objednavka.get("attributes") if isinstance(objednavka, dict) else None
    return atributy if isinstance(atributy, dict) else {}


def email_z_objednavky(objednavka):
    email = _atributy_objednavky(objednavka).get("user_email")
    if not isinstance(email, str):
        return None
    email = email.strip().lower()
    return email if 3 <= len(email) <= 254 and "@" in email else None


def user_id_z_objednavky(objednavka):
    """Id účtu z custom data, ak ho poskytovateľ v API vôbec vráti."""
    atributy = _atributy_objednavky(objednavka)
    kandidati = [atributy.get("custom_data")]
    polozka = atributy.get("first_order_item")
    if isinstance(polozka, dict):
        kandidati.append(polozka.get("custom_data"))
    for custom in kandidati:
        if isinstance(custom, dict):
            user_id = custom_user_id({"meta": {"custom_data": custom}})
            if user_id is not None:
                return user_id
    return None


def stav_objednavky(objednavka) -> str:
    atributy = _atributy_objednavky(objednavka)
    if atributy.get("refunded") is True:
        return "refunded"
    stav = atributy.get("status")
    return stav.strip().lower() if isinstance(stav, str) else ""


def payload_z_objednavky(objednavka, *, user_id=None, typ=UDALOST_UDELUJUCA):
    """Objednávka z API → presne ten tvar, v akom chodí webhook. Alebo None.

    Prepisujú sa len polia, ktoré appka naozaj číta. Čokoľvek iné, čo API vráti
    alebo v budúcnosti pridá, sa do spracovania nedostane.
    """
    if not isinstance(objednavka, dict):
        return None
    ref = _bezpecne_id(objednavka.get("id"))
    if not ref:
        return None
    atributy = _atributy_objednavky(objednavka)
    polozka = atributy.get("first_order_item")
    prepis = {
        "total": atributy.get("total"),
        "currency": atributy.get("currency"),
        "status": atributy.get("status"),
    }
    if isinstance(polozka, dict) and polozka.get("variant_id") is not None:
        prepis["first_order_item"] = {"variant_id": polozka.get("variant_id")}
    meta = {"event_name": typ}
    if user_id is not None:
        meta["custom_data"] = {"user_id": str(int(user_id)),
                               "produkt": PRODUKT_ZAKLADAJUCI}
    return {"meta": meta,
            "data": {"id": ref, "type": "orders", "attributes": prepis}}


def narok_objednavky(con, objednavka_id):
    """Riadok nároku pre danú objednávku poskytovateľa, ak nejaký je."""
    ref = _bezpecne_id(objednavka_id)
    if not ref:
        return None
    riadok = con.execute(
        "SELECT id, user_id, stav FROM naroky WHERE poskytovatel=? AND objednavka_id=?",
        (POSKYTOVATEL, ref),
    ).fetchone()
    return dict(zip(("id", "user_id", "stav"), riadok)) if riadok else None


def ucet_podla_emailu(con, email):
    """Práve jeden účet, alebo nič. Dva zhodné e-maily radšej neriešime hádaním."""
    if not isinstance(email, str) or "@" not in email:
        return None
    riadky = con.execute(
        "SELECT id FROM pouzivatelia WHERE lower(email)=lower(?) LIMIT 2",
        (email.strip(),),
    ).fetchall()
    return int(riadky[0][0]) if len(riadky) == 1 else None


def email_uctu(con, user_id):
    riadok = con.execute(
        "SELECT email FROM pouzivatelia WHERE id=?", (user_id,)
    ).fetchone()
    return riadok[0] if riadok else None
