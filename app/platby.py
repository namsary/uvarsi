"""Platobná vrstva Uvar.si — jednorazové Zakladajúce členstvo cez LemonSqueezy.

Tri pravidlá, ktoré tento modul drží:

1. Vypínač. `PLATBY_ZAPNUTE` je v predvolenom stave vypnutý. Kým ho majiteľ
   vedome nezapne, žiadna platba nevznikne a žiadna adresa poskytovateľa sa ani
   nezostaví.
2. Nárok sa odvodzuje výhradne z uložených udalostí poskytovateľa. Klient
   nemôže o svojej platbe povedať nič, čomu by sa verilo — jediný vstup je
   podpísaný webhook.
3. Tajomstvá sa sem odovzdávajú z prostredia ako argumenty, nikdy sa neukladajú
   ani nevypisujú. Modul zámerne neobsahuje žiadny výstup.

LemonSqueezy je merchant of record, takže EU DPH/OSS rieši on. Uvar.si si drží
len záznam o tom, kto má nárok a prečo.
"""
import hashlib
import hmac
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


POSKYTOVATEL = "lemonsqueezy"
PRODUKT_ZAKLADAJUCI = "zakladajuci_clen"
KAPACITA_ZAKLADAJUCICH = 250

STAV_AKTIVNY = "aktivny"
STAV_VRATENY = "vrateny"
STAV_ZRUSENY = "zruseny"
STAV_NAD_KAPACITU = "nad_kapacitu"

AKCIA_UDELENE = "udelene"
AKCIA_UZ_UDELENE = "uz_udelene"
AKCIA_UZ_SPRACOVANE = "uz_spracovane"
AKCIA_VRATENE = "vratene"
AKCIA_ZRUSENE = "zrusene"
AKCIA_NAD_KAPACITU = "nad_kapacitu"
AKCIA_IGNOROVANE = "ignorovane"

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

_PRAVDIVE = frozenset({"1", "true", "ano", "áno", "yes", "on", "zapnute", "zapnuté"})
_MAX_PODPIS = 256
# LemonSqueezy posiela desiatky kB; nad týmto je to buď omyl, alebo útok.
MAX_TELO_WEBHOOKU = 256 * 1024
_ID_ZNAKY = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"
)

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
  prijate_o REAL NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS platobne_udalosti_event_idx
  ON platobne_udalosti(event_id) WHERE event_id IS NOT NULL;
"""


class PlatbyNenastavene(RuntimeError):
    """Majiteľ ešte nedodal platnú konfiguráciu poskytovateľa."""


class UdalostNepouzitelna(RuntimeError):
    """Podpísaná udalosť sa nedá priradiť k účtu alebo objednávke."""


def migrate_platby_schema(con) -> None:
    """Aditívne vytvorí platobné tabuľky; na existujúcej databáze nič neprepíše."""
    con.executescript(PLATBY_SCHEMA)


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


def stav_platieb(con, *, user_id: int, zapnute: bool) -> dict:
    obsadene = pocet_zakladajucich(con)
    volne = max(0, KAPACITA_ZAKLADAJUCICH - obsadene)
    narok = ma_narok(con, user_id)
    if not zapnute:
        sprava = SPRAVA_VYPNUTE
    elif narok:
        sprava = SPRAVA_AKTIVNE
    elif volne == 0:
        sprava = SPRAVA_VYPREDANE
    else:
        sprava = f"Zostáva {volne} z {KAPACITA_ZAKLADAJUCICH} zakladajúcich miest."
    return {
        "platby_zapnute": zapnute,
        "ma_narok": narok,
        "produkt": PRODUKT_ZAKLADAJUCI,
        "kapacita": KAPACITA_ZAKLADAJUCICH,
        "obsadene": obsadene,
        "volne_miesta": volne,
        "sprava": sprava,
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
def spracuj_udalost(con, *, payload, now, variant_id=None) -> dict:
    """Jedna udalosť = jedna transakcia. Idempotentné a bezpečné voči pretekom.

    Celý beh je v BEGIN IMMEDIATE, takže dve súbežné doručenia sa serializujú a
    kontrola kapacity vidí vždy skutočný počet udelených miest.
    """
    if not isinstance(payload, dict):
        raise UdalostNepouzitelna("telo nie je objekt")
    typ = typ_udalosti(payload)
    kluc = udalost_kluc(payload)
    surove = surove_id_udalosti(payload)

    if con.in_transaction:
        con.commit()
    con.execute("BEGIN IMMEDIATE")
    try:
        if _uz_spracovane(con, kluc, surove):
            con.commit()
            return {"akcia": AKCIA_UZ_SPRACOVANE}
        con.execute(
            """INSERT INTO platobne_udalosti (udalost_kluc, event_id, typ, prijate_o)
               VALUES (?, ?, ?, ?)""",
            (kluc, surove, typ, now),
        )
        if typ == UDALOST_UDELUJUCA:
            akcia = _udel(con, payload, now, variant_id)
        elif typ in UDALOSTI_ODOBERAJUCE:
            akcia = _odober(con, payload, now, typ)
        else:
            akcia = AKCIA_IGNOROVANE
        con.commit()
        return {"akcia": akcia}
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
    if ocakavany_variant:
        if _variant(payload) != _bezpecne_id(ocakavany_variant):
            return AKCIA_IGNOROVANE

    objednavka = objednavka_ref(payload)
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
        return AKCIA_UZ_UDELENE
    if ma_narok(con, user_id):
        return AKCIA_UZ_UDELENE

    suma, mena = _suma(payload)
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
        return AKCIA_NAD_KAPACITU
    con.execute("UPDATE pouzivatelia SET platiaci=1 WHERE id=?", (user_id,))
    return AKCIA_UDELENE


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
            riadok = con.execute(
                """SELECT id, user_id FROM naroky
                   WHERE user_id=? AND produkt=? AND stav=? ORDER BY id DESC""",
                (user_id, PRODUKT_ZAKLADAJUCI, STAV_AKTIVNY),
            ).fetchone()
    if riadok is None:
        return AKCIA_IGNOROVANE

    narok_id, user_id = riadok[0], riadok[1]
    con.execute(
        "UPDATE naroky SET stav=?, zmeneny_o=? WHERE id=?", (novy_stav, now, narok_id)
    )
    if not ma_narok(con, user_id):
        con.execute("UPDATE pouzivatelia SET platiaci=0 WHERE id=?", (user_id,))
    return akcia
