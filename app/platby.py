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
import secrets
import sqlite3
from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

try:
    from .operator_profile import LEGAL_VERSION
except ImportError:  # server.py imports platby as a top-level production module
    from operator_profile import LEGAL_VERSION


POSKYTOVATEL = "lemonsqueezy"
# Nárok, ktorý neudelila platba, ale majiteľ pri databáze. Kým je vypínač
# vypnutý, je to jediná cesta k Premium — a v tabuľke je na prvý pohľad vidieť,
# že sa zaň neplatilo (nulová suma, žiadna mena).
POSKYTOVATEL_RUCNE = "rucne"
PRODUKT_ZAKLADAJUCI = "zakladajuci_clen"
PRODUKT_PREMIUM_ROCNY = "premium_annual"
KAPACITA_ZAKLADAJUCICH = 50
CENA_ZAKLADAJUCI_CENTY = 3900
CENA_PRVY_ROK_CENTY = 3900
CENA_OBNOVA_CENTY = 4900
MENA_ZAKLADAJUCI = "EUR"
INTERVAL_ROK = "year"
CHECKOUT_ATTEMPT_TTL_SECONDS = 60 * 60
ANNUAL_CONSENT_FIELDS = (
    "accept_terms",
    "accept_automatic_renewal",
    "request_immediate_activation",
    "acknowledge_withdrawal_proration",
)

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
-- Doklad o tom, čo človek odsúhlasil pred odchodom do pokladne. Neobsahuje
-- kartu, heslo, session token ani celé hlavičky prehliadača.
CREATE TABLE IF NOT EXISTS checkout_attempts (
  public_id TEXT PRIMARY KEY,
  user_id INTEGER NOT NULL,
  product TEXT NOT NULL,
  amount_cents INTEGER NOT NULL,
  renewal_amount_cents INTEGER,
  currency TEXT NOT NULL,
  billing_interval TEXT,
  auto_renews INTEGER,
  founder INTEGER,
  discount_id TEXT,
  discount_code TEXT,
  legal_version TEXT NOT NULL,
  privacy_version TEXT NOT NULL,
  consent_json TEXT,
  accepted_at REAL NOT NULL,
  expires_at REAL NOT NULL,
  founder_reserved_until REAL,
  test_mode INTEGER,
  status TEXT NOT NULL,
  provider_checkout_id TEXT,
  provider_order_id TEXT
);
CREATE INDEX IF NOT EXISTS checkout_attempts_user_idx
  ON checkout_attempts(user_id, accepted_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS checkout_attempts_provider_order_idx
  ON checkout_attempts(provider_order_id) WHERE provider_order_id IS NOT NULL;
-- Citlivý pracovný zoznam pre majiteľa. Verejné upozornenie odkazuje iba na
-- počet otvorených prípadov; konkrétne ID objednávky zostáva iba tu.
CREATE TABLE IF NOT EXISTS payment_cases (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  case_type TEXT NOT NULL,
  provider_order_id TEXT,
  user_id INTEGER,
  status TEXT NOT NULL,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL,
  UNIQUE(case_type, provider_order_id)
);
CREATE INDEX IF NOT EXISTS payment_cases_open_idx
  ON payment_cases(status, case_type, created_at);
"""


class PlatbyNenastavene(RuntimeError):
    """Majiteľ ešte nedodal platnú konfiguráciu poskytovateľa."""


class UdalostNepouzitelna(RuntimeError):
    """Podpísaná udalosť sa nedá priradiť k účtu alebo objednávke."""


class CheckoutAlreadyActive(RuntimeError):
    """A provider-backed checkout is still payable and cannot be recovered."""

    def __init__(self, *, public_id: str, expires_at: float):
        self.public_id = public_id
        self.expires_at = expires_at
        super().__init__(
            "Platobná pokladňa je už aktívna. "
            "Dokonči ju alebo počkaj do jej expirácie."
        )


@dataclass(frozen=True)
class CheckoutAttempt:
    """Immutable annual checkout terms captured before provider I/O."""

    public_id: str
    user_id: int
    product: str
    amount_cents: int
    renewal_amount_cents: int
    currency: str
    billing_interval: str
    auto_renews: bool
    founder: bool
    discount_id: str | None
    discount_code: str | None
    legal_version: str
    privacy_version: str
    consent: Mapping[str, object]
    accepted_at: float
    expires_at: float
    founder_reserved_until: float | None
    test_mode: bool
    provider_checkout_id: str | None = None


class ProviderCheckoutURL(str):
    """Ephemeral signed URL carrying only its validated non-secret checkout ID."""

    def __new__(cls, value: str, *, provider_checkout_id: str):
        instance = super().__new__(cls, value)
        instance.provider_checkout_id = provider_checkout_id
        return instance


def migrate_platby_schema(con) -> None:
    """Aditívne vytvorí platobné tabuľky; na existujúcej databáze nič neprepíše."""
    con.executescript(PLATBY_SCHEMA)
    _doplni_stlpec(con, "platobne_udalosti", "zdroj", "TEXT")
    for column, definition in (
        ("renewal_amount_cents", "INTEGER"),
        ("billing_interval", "TEXT"),
        ("auto_renews", "INTEGER"),
        ("founder", "INTEGER"),
        ("discount_id", "TEXT"),
        ("discount_code", "TEXT"),
        ("consent_json", "TEXT"),
        ("founder_reserved_until", "REAL"),
        ("test_mode", "INTEGER"),
        ("provider_checkout_id", "TEXT"),
    ):
        _doplni_stlpec(con, "checkout_attempts", column, definition)
    con.execute(
        """CREATE UNIQUE INDEX IF NOT EXISTS checkout_attempts_provider_checkout_idx
           ON checkout_attempts(provider_checkout_id)
           WHERE provider_checkout_id IS NOT NULL"""
    )
    con.execute(
        """CREATE INDEX IF NOT EXISTS checkout_attempts_founder_reservation_idx
           ON checkout_attempts(status, founder, founder_reserved_until)"""
    )
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


def _required_text(value, message: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PlatbyNenastavene(message)
    stripped = value.strip()
    if len(stripped) > 256 or "\r" in stripped or "\n" in stripped:
        raise PlatbyNenastavene(message)
    return stripped


def _annual_consent_json(consent, *, legal_version: str) -> str:
    if not isinstance(consent, dict):
        raise ValueError("neplatný súhlas s ročným predplatným")
    if any(consent.get(field) is not True for field in ANNUAL_CONSENT_FIELDS):
        raise ValueError("neúplný súhlas s ročným predplatným")
    recorded_version = consent.get("legal_version")
    if recorded_version is not None and recorded_version != legal_version:
        raise ValueError("súhlas má inú právnu verziu")
    if not all(isinstance(key, str) for key in consent):
        raise ValueError("neplatný súhlas s ročným predplatným")
    try:
        return json.dumps(
            consent, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
    except (TypeError, ValueError):
        raise ValueError("neplatný súhlas s ročným predplatným") from None


def validate_annual_checkout_consent(consent, *, legal_version: str) -> None:
    """Reject implicit, incomplete, or stale annual checkout consent."""
    if legal_version != LEGAL_VERSION:
        raise ValueError("neplatná právna verzia")
    _annual_consent_json(consent, legal_version=legal_version)


def founder_places_used(con, *, test_mode: bool = False) -> int:
    """Count successful first founder payments, including ended subscriptions."""
    if type(test_mode) is not bool:
        raise ValueError("neplatný režim pokladne")
    row = con.execute(
        """SELECT COUNT(*) FROM subscriptions
           WHERE founder=1 AND initial_payment_verified=1 AND test_mode=?""",
        (int(test_mode),),
    ).fetchone()
    return int(row[0]) if row else 0


def _pending_founder_places(con, *, now: float, test_mode: bool) -> int:
    row = con.execute(
        """SELECT COUNT(*) FROM checkout_attempts
           WHERE founder=1 AND status='pending'
             AND test_mode=?
             AND founder_reserved_until IS NOT NULL
             AND founder_reserved_until>?""",
        (int(test_mode), now),
    ).fetchone()
    return int(row[0]) if row else 0


def create_subscription_checkout_attempt(
    con,
    *,
    user_id,
    legal_version,
    consent,
    now,
    founder_discount_id=None,
    founder_discount_code=None,
    test_mode=False,
) -> CheckoutAttempt:
    """Atomically reserve founder capacity and persist the annual contract."""
    _over_id_pouzivatela(user_id)
    if legal_version != LEGAL_VERSION:
        raise ValueError("neplatná právna verzia")
    if type(test_mode) is not bool:
        raise ValueError("neplatný režim pokladne")
    accepted_at = _cas(now)
    expires_at = accepted_at + CHECKOUT_ATTEMPT_TTL_SECONDS
    consent_json = _annual_consent_json(consent, legal_version=legal_version)
    if (founder_discount_id is None) != (founder_discount_code is None):
        raise ValueError("neúplná konfigurácia zakladajúcej zľavy")
    if founder_discount_id is not None:
        founder_discount_id = _required_text(
            founder_discount_id, "neplatný identifikátor zakladajúcej zľavy"
        )
        founder_discount_code = _required_text(
            founder_discount_code, "neplatný kód zakladajúcej zľavy"
        )
    if con.in_transaction:
        raise RuntimeError("rezervácia zakladajúceho miesta vyžaduje čisté spojenie")

    try:
        con.execute("BEGIN IMMEDIATE")
        if con.execute(
            "SELECT 1 FROM pouzivatelia WHERE id=?", (user_id,)
        ).fetchone() is None:
            raise ValueError("neznámy používateľ")
        con.execute(
            """UPDATE checkout_attempts
                  SET status='expired', founder_reserved_until=NULL
                WHERE status='pending' AND test_mode=? AND expires_at<=?""",
            (int(test_mode), accepted_at),
        )
        active = con.execute(
            """SELECT public_id,expires_at FROM checkout_attempts
               WHERE user_id=? AND status='pending' AND test_mode=?
                 AND expires_at>?
               ORDER BY accepted_at DESC LIMIT 1""",
            (user_id, int(test_mode), accepted_at),
        ).fetchone()
        if active is not None:
            raise CheckoutAlreadyActive(
                public_id=str(active[0]), expires_at=float(active[1])
            )
        founder = (
            founder_places_used(con, test_mode=test_mode)
            + _pending_founder_places(
                con, now=accepted_at, test_mode=test_mode
            )
            < KAPACITA_ZAKLADAJUCICH
        )
        amount_cents = CENA_PRVY_ROK_CENTY if founder else CENA_OBNOVA_CENTY
        discount_id = founder_discount_id if founder else None
        discount_code = founder_discount_code if founder else None
        reserved_until = expires_at if founder else None
        public_id = None
        for _ in range(3):
            candidate = secrets.token_urlsafe(32)
            try:
                con.execute(
                    """INSERT INTO checkout_attempts
                       (public_id,user_id,product,amount_cents,
                        renewal_amount_cents,currency,billing_interval,
                        auto_renews,founder,discount_id,discount_code,
                        legal_version,privacy_version,consent_json,accepted_at,
                        expires_at,founder_reserved_until,test_mode,status,
                        provider_checkout_id,provider_order_id)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,
                               'pending',NULL,NULL)""",
                    (
                        candidate,
                        user_id,
                        PRODUKT_PREMIUM_ROCNY,
                        amount_cents,
                        CENA_OBNOVA_CENTY,
                        MENA_ZAKLADAJUCI,
                        INTERVAL_ROK,
                        1,
                        int(founder),
                        discount_id,
                        discount_code,
                        legal_version,
                        legal_version,
                        consent_json,
                        accepted_at,
                        expires_at,
                        reserved_until,
                        int(test_mode),
                    ),
                )
                public_id = candidate
                break
            except sqlite3.IntegrityError as error:
                if "UNIQUE constraint failed: checkout_attempts.public_id" not in str(error):
                    raise
        if public_id is None:
            raise RuntimeError("nepodarilo sa vytvoriť bezpečný pokus objednávky")
        con.commit()
    except Exception:
        con.rollback()
        raise

    return CheckoutAttempt(
        public_id=public_id,
        user_id=user_id,
        product=PRODUKT_PREMIUM_ROCNY,
        amount_cents=amount_cents,
        renewal_amount_cents=CENA_OBNOVA_CENTY,
        currency=MENA_ZAKLADAJUCI,
        billing_interval=INTERVAL_ROK,
        auto_renews=True,
        founder=founder,
        discount_id=discount_id,
        discount_code=discount_code,
        legal_version=legal_version,
        privacy_version=legal_version,
        consent=MappingProxyType(json.loads(consent_json)),
        accepted_at=accepted_at,
        expires_at=expires_at,
        founder_reserved_until=reserved_until,
        test_mode=test_mode,
    )


def _provider_expiry(value) -> float:
    if not isinstance(value, str) or not value.strip():
        raise PlatbyNenastavene("poskytovateľ nepotvrdil expiráciu pokladne")
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = f"{normalized[:-1]}+00:00"
    try:
        parsed = datetime.datetime.fromisoformat(normalized)
    except ValueError:
        raise PlatbyNenastavene(
            "poskytovateľ nepotvrdil expiráciu pokladne"
        ) from None
    if parsed.utcoffset() is None:
        raise PlatbyNenastavene("poskytovateľ nepotvrdil expiráciu pokladne")
    return parsed.timestamp()


def _signed_checkout_url(value) -> str:
    if not isinstance(value, str):
        raise PlatbyNenastavene("poskytovateľ nevrátil bezpečnú pokladňu")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        raise PlatbyNenastavene(
            "poskytovateľ nevrátil bezpečnú pokladňu"
        ) from None
    hostname = parsed.hostname
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    if (
        parsed.scheme != "https"
        or not isinstance(hostname, str)
        or not (
            hostname == "lemonsqueezy.com"
            or hostname.endswith(".lemonsqueezy.com")
        )
        or parsed.username is not None
        or parsed.password is not None
        or port is not None
        or not parsed.path.startswith("/checkout/")
        or parsed.fragment
        or not query.get("expires")
        or not query.get("signature")
    ):
        raise PlatbyNenastavene("poskytovateľ nevrátil bezpečnú pokladňu")
    return value


def create_provider_subscription_checkout(
    *, attempt: CheckoutAttempt, email, provider, test_mode
) -> str:
    """Create and verify one short-lived annual checkout; never retain its URL."""
    if not isinstance(attempt, CheckoutAttempt):
        raise ValueError("neplatný pokus objednávky")
    if type(test_mode) is not bool:
        raise ValueError("neplatný režim pokladne")
    if attempt.test_mode is not test_mode:
        raise PlatbyNenastavene("pokus má iný režim pokladne")
    email = _required_text(email, "chýba e-mail pokladne")
    store_id = _required_text(
        getattr(provider, "store_id", None), "chýba obchod pokladne"
    )
    variant_id = _required_text(
        getattr(provider, "variant_id", None), "chýba ročný variant pokladne"
    )
    checkout_data = {
        "email": email,
        "custom": {"attempt_id": attempt.public_id},
    }
    if attempt.founder:
        provider_discount_id = _required_text(
            getattr(provider, "founder_discount_id", None),
            "chýba identifikátor zakladajúcej zľavy",
        )
        provider_discount_code = _required_text(
            getattr(provider, "founder_discount_code", None),
            "chýba kód zakladajúcej zľavy",
        )
        if attempt.discount_id != provider_discount_id:
            raise PlatbyNenastavene("pokus má inú zakladajúcu zľavu")
        if attempt.discount_code != provider_discount_code:
            raise PlatbyNenastavene("pokus má iný kód zakladajúcej zľavy")
        checkout_data["discount_code"] = provider_discount_code
    elif attempt.discount_id is not None or attempt.discount_code is not None:
        raise PlatbyNenastavene("bežná pokladňa nesmie použiť zakladajúcu zľavu")

    expires_at = datetime.datetime.fromtimestamp(
        attempt.expires_at, datetime.timezone.utc
    ).isoformat().replace("+00:00", "Z")
    payload = {
        "data": {
            "type": "checkouts",
            "attributes": {
                "test_mode": test_mode,
                "product_options": {"enabled_variants": [variant_id]},
                "checkout_options": {
                    "discount": False,
                    "skip_trial": True,
                    "subscription_preview": True,
                },
                "checkout_data": checkout_data,
                "expires_at": expires_at,
                "preview": True,
            },
            "relationships": {
                "store": {"data": {"type": "stores", "id": store_id}},
                "variant": {"data": {"type": "variants", "id": variant_id}},
            },
        }
    }
    create_checkout = getattr(provider, "create_checkout", None)
    if not callable(create_checkout):
        raise PlatbyNenastavene("poskytovateľ nevie vytvoriť pokladňu")
    response = create_checkout(payload)
    data = response.get("data") if isinstance(response, dict) else None
    if not isinstance(data, dict) or data.get("type") != "checkouts":
        raise PlatbyNenastavene("poskytovateľ vrátil neplatnú pokladňu")
    checkout_id = _required_text(
        data.get("id"), "poskytovateľ nevrátil identifikátor pokladne"
    )
    attributes = data.get("attributes")
    if not isinstance(attributes, dict):
        raise PlatbyNenastavene("poskytovateľ vrátil neplatnú pokladňu")
    if str(attributes.get("store_id")) != store_id:
        raise PlatbyNenastavene("poskytovateľ vrátil pokladňu iného obchodu")
    if str(attributes.get("variant_id")) != variant_id:
        raise PlatbyNenastavene("poskytovateľ vrátil iný variant pokladne")
    if attributes.get("test_mode") is not test_mode:
        raise PlatbyNenastavene("poskytovateľ vrátil pokladňu v inom režime")
    if abs(_provider_expiry(attributes.get("expires_at")) - attempt.expires_at) > 0.001:
        raise PlatbyNenastavene("poskytovateľ vrátil inú expiráciu pokladne")
    preview = attributes.get("preview")
    if not isinstance(preview, dict) or preview.get("currency") != attempt.currency:
        raise PlatbyNenastavene("poskytovateľ nepotvrdil menu pokladne")
    if preview.get("total") != attempt.amount_cents:
        raise PlatbyNenastavene("poskytovateľ nepotvrdil schválenú sumu pokladne")
    checkout_url = _signed_checkout_url(attributes.get("url"))
    return ProviderCheckoutURL(checkout_url, provider_checkout_id=checkout_id)


def record_provider_checkout(
    con,
    *,
    attempt: CheckoutAttempt,
    provider_checkout_id,
    test_mode,
    discount_id,
    discount_code,
) -> None:
    """Store only the provider ID after validation; the signed URL has no sink."""
    if not isinstance(attempt, CheckoutAttempt):
        raise ValueError("neplatný pokus objednávky")
    if type(test_mode) is not bool or attempt.test_mode is not test_mode:
        raise PlatbyNenastavene("pokus má iný režim pokladne")
    if attempt.founder:
        discount_id = _required_text(
            discount_id, "chýba identifikátor zakladajúcej zľavy"
        )
        discount_code = _required_text(
            discount_code, "chýba kód zakladajúcej zľavy"
        )
    elif discount_id is not None or discount_code is not None:
        raise PlatbyNenastavene("bežná pokladňa nesmie použiť zakladajúcu zľavu")
    if attempt.discount_id != discount_id:
        raise PlatbyNenastavene("pokus má inú zakladajúcu zľavu")
    if attempt.discount_code != discount_code:
        raise PlatbyNenastavene("pokus má iný kód zakladajúcej zľavy")
    checkout_id = _required_text(
        provider_checkout_id, "chýba identifikátor pokladne"
    )
    stored = con.execute(
        """SELECT test_mode,discount_id,discount_code FROM checkout_attempts
           WHERE public_id=? AND status='pending'
             AND provider_checkout_id IS NULL""",
        (attempt.public_id,),
    ).fetchone()
    if stored is None:
        raise ValueError("pokus objednávky už nemožno priradiť k pokladni")
    if (
        stored[0] != int(test_mode)
        or stored[1] != discount_id
        or stored[2] != discount_code
    ):
        raise PlatbyNenastavene("uložený pokus má iné nemenné podmienky")
    cursor = con.execute(
        """UPDATE checkout_attempts
              SET provider_checkout_id=?
            WHERE public_id=? AND status='pending'
              AND provider_checkout_id IS NULL""",
        (checkout_id, attempt.public_id),
    )
    if cursor.rowcount != 1:
        raise ValueError("pokus objednávky už nemožno priradiť k pokladni")


# ------------------------------------------------------ súhlas pred platbou
def create_checkout_attempt(con, *, user_id, legal_version, now) -> str:
    """Persist one current, one-time Founder offer accepted by one account."""
    _over_id_pouzivatela(user_id)
    if legal_version != LEGAL_VERSION:
        raise ValueError("neplatná právna verzia")
    if con.execute(
        "SELECT 1 FROM pouzivatelia WHERE id=?", (user_id,)
    ).fetchone() is None:
        raise ValueError("neznámy používateľ")
    accepted_at = _cas(now)
    expires_at = accepted_at + CHECKOUT_ATTEMPT_TTL_SECONDS
    for _ in range(3):
        public_id = secrets.token_urlsafe(32)
        try:
            con.execute(
                """INSERT INTO checkout_attempts
                   (public_id, user_id, product, amount_cents, currency,
                    legal_version, privacy_version, accepted_at, expires_at,
                    status, provider_order_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', NULL)""",
                (
                    public_id,
                    user_id,
                    PRODUKT_ZAKLADAJUCI,
                    CENA_ZAKLADAJUCI_CENTY,
                    MENA_ZAKLADAJUCI,
                    LEGAL_VERSION,
                    LEGAL_VERSION,
                    accepted_at,
                    expires_at,
                ),
            )
            return public_id
        except sqlite3.IntegrityError as error:
            # A random-ID collision is the only retryable insert failure.
            if "UNIQUE constraint failed: checkout_attempts.public_id" not in str(error):
                raise
    raise RuntimeError("nepodarilo sa vytvoriť bezpečný pokus objednávky")


def get_checkout_attempt(con, public_id, *, now=None):
    """Return a pending, unexpired attempt; expire stale attempts fail-closed."""
    public_id = _bezpecne_id(public_id)
    if public_id is None or len(public_id) < 43:
        return None
    row = con.execute(
        "SELECT * FROM checkout_attempts WHERE public_id=?", (public_id,)
    ).fetchone()
    if row is None:
        return None
    names = [column[0] for column in con.execute(
        "SELECT * FROM checkout_attempts LIMIT 0"
    ).description]
    attempt = dict(zip(names, row))
    checked_at = _cas(now if now is not None else datetime.datetime.now(datetime.timezone.utc))
    if attempt["status"] == "pending" and checked_at > float(attempt["expires_at"]):
        con.execute(
            "UPDATE checkout_attempts SET status='expired' WHERE public_id=? AND status='pending'",
            (public_id,),
        )
        return None
    if attempt["status"] != "pending":
        return None
    return attempt


def mark_checkout_paid(con, *, public_id, user_id, provider_order_id, now) -> dict:
    """Consume one matching pending attempt exactly once inside caller's transaction."""
    attempt = get_checkout_attempt(con, public_id, now=now)
    order_id = _bezpecne_id(provider_order_id)
    if attempt is None or order_id is None:
        raise UdalostNepouzitelna("neplatný alebo použitý pokus objednávky")
    if int(attempt["user_id"]) != user_id:
        raise UdalostNepouzitelna("pokus objednávky patrí inému účtu")
    if (
        attempt["product"] != PRODUKT_ZAKLADAJUCI
        or int(attempt["amount_cents"]) != CENA_ZAKLADAJUCI_CENTY
        or attempt["currency"] != MENA_ZAKLADAJUCI
        or attempt["legal_version"] != LEGAL_VERSION
        or attempt["privacy_version"] != LEGAL_VERSION
    ):
        raise UdalostNepouzitelna("pokus objednávky nezodpovedá aktuálnej ponuke")
    cursor = con.execute(
        """UPDATE checkout_attempts
              SET status='paid', provider_order_id=?
            WHERE public_id=? AND status='pending'""",
        (order_id, public_id),
    )
    if cursor.rowcount != 1:
        raise UdalostNepouzitelna("pokus objednávky už bol použitý")
    return {**attempt, "status": "paid", "provider_order_id": order_id}


def create_payment_case(
    con, *, case_type, provider_order_id=None, user_id=None, now
) -> int:
    """Keep actionable identifiers in protected SQLite, never in public alerts."""
    if case_type not in DRUHY_UPOZORNENI:
        raise ValueError("neplatný typ platobného prípadu")
    order_id = _bezpecne_id(provider_order_id)
    if provider_order_id is not None and order_id is None:
        raise ValueError("neplatné id objednávky")
    if user_id is not None:
        _over_id_pouzivatela(user_id)
    timestamp = _cas(now)
    con.execute(
        """INSERT OR IGNORE INTO payment_cases
           (case_type, provider_order_id, user_id, status, created_at, updated_at)
           VALUES (?, ?, ?, 'open', ?, ?)""",
        (case_type, order_id, user_id, timestamp, timestamp),
    )
    con.commit()
    row = con.execute(
        """SELECT id FROM payment_cases
            WHERE case_type=? AND provider_order_id IS ?""",
        (case_type, order_id),
    ).fetchone()
    if row is None:
        raise RuntimeError("platobný prípad sa nepodarilo uložiť")
    return int(row[0])


def count_open_payment_cases(con, case_type=None) -> int:
    if case_type is None:
        row = con.execute(
            "SELECT COUNT(*) FROM payment_cases WHERE status='open'"
        ).fetchone()
    else:
        if case_type not in DRUHY_UPOZORNENI:
            raise ValueError("neplatný typ platobného prípadu")
        row = con.execute(
            "SELECT COUNT(*) FROM payment_cases WHERE status='open' AND case_type=?",
            (case_type,),
        ).fetchone()
    return int(row[0]) if row else 0


def close_payment_cases_for_refund(con, *, provider_order_id, now) -> int:
    order_id = _bezpecne_id(provider_order_id)
    if order_id is None:
        raise ValueError("neplatné id objednávky")
    cursor = con.execute(
        """UPDATE payment_cases SET status='resolved',updated_at=?
            WHERE provider_order_id=? AND status='open'""",
        (_cas(now), order_id),
    )
    return int(cursor.rowcount)


# ---------------------------------------------------------------- vypínač
def platby_zapnute(hodnota) -> bool:
    """Vypnuté, kým majiteľ nenapíše jednoznačné áno. Čokoľvek iné = vypnuté."""
    if not isinstance(hodnota, str):
        return False
    return hodnota.strip().casefold() in _PRAVDIVE


# ---------------------------------------------------------------- pokladňa
def checkout_url(zaklad, *, user_id: int, attempt_id, email=None) -> str:
    """Build checkout URL with account and audited attempt, never a session token."""
    if not isinstance(zaklad, str) or not zaklad.strip():
        raise PlatbyNenastavene("chýba adresa pokladne")
    casti = urlsplit(zaklad.strip())
    if casti.scheme != "https" or not casti.netloc:
        raise PlatbyNenastavene("adresa pokladne musí byť https")
    if not isinstance(user_id, int) or isinstance(user_id, bool) or user_id <= 0:
        raise ValueError("neplatné id používateľa")
    attempt_id = _bezpecne_id(attempt_id)
    if attempt_id is None or len(attempt_id) < 43:
        raise ValueError("neplatný pokus objednávky")
    parametre = [
        (kluc, hodnota)
        for kluc, hodnota in parse_qsl(casti.query, keep_blank_values=True)
        if not kluc.startswith("checkout[custom]")
    ]
    parametre.append(("checkout[custom][user_id]", str(user_id)))
    parametre.append(("checkout[custom][produkt]", PRODUKT_ZAKLADAJUCI))
    parametre.append(("checkout[custom][checkout_attempt]", attempt_id))
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


def pocet_zaplatenych_zakladajucich(con) -> int:
    """Počet aktívnych miest získaných platbou, bez ručných testovacích nárokov."""
    riadok = con.execute(
        """SELECT COUNT(*) FROM naroky
           WHERE produkt=? AND stav=? AND poskytovatel=?""",
        (PRODUKT_ZAKLADAJUCI, STAV_AKTIVNY, POSKYTOVATEL),
    ).fetchone()
    return int(riadok[0]) if riadok else 0


def volne_miesta(con, *, test_mode=None, now=None) -> int:
    if test_mode is None:
        return max(
            0, KAPACITA_ZAKLADAJUCICH - pocet_zaplatenych_zakladajucich(con)
        )
    if type(test_mode) is not bool or now is None:
        raise ValueError("neplatný režim alebo čas kapacity pokladne")
    checked_at = _cas(now)
    occupied = founder_places_used(con, test_mode=test_mode)
    reserved = _pending_founder_places(
        con, now=checked_at, test_mode=test_mode
    )
    return max(0, KAPACITA_ZAKLADAJUCICH - occupied - reserved)


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
    obsadene = pocet_zaplatenych_zakladajucich(con)
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


def custom_checkout_attempt(payload):
    custom = _meta(payload).get("custom_data")
    if not isinstance(custom, dict):
        return None
    attempt = _bezpecne_id(custom.get("checkout_attempt"))
    return attempt if attempt is not None and len(attempt) >= 43 else None


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


def refund_kind(payload) -> str:
    """Classify LemonSqueezy's shared full/partial `order_refunded` event."""
    attributes = _atributy(payload)
    total = attributes.get("total")
    refunded_amount = attributes.get("refunded_amount")
    status = attributes.get("status")
    status = status.strip().casefold() if isinstance(status, str) else ""
    full_flag = attributes.get("refunded") is True
    valid_amounts = all(
        isinstance(value, int) and not isinstance(value, bool) and value >= 0
        for value in (total, refunded_amount)
    )
    if full_flag or (
        valid_amounts and total > 0 and refunded_amount >= total
        and status == "refunded"
    ):
        return "full"
    if status == "partial_refund" or (
        valid_amounts and 0 < refunded_amount < total
    ):
        return "partial"
    return "unknown"


def _variant(payload):
    atributy = _atributy(payload)
    polozka = atributy.get("first_order_item")
    if isinstance(polozka, dict):
        kandidat = _bezpecne_id(polozka.get("variant_id"))
        if kandidat:
            return kandidat
    return _bezpecne_id(atributy.get("variant_id"))


# ---------------------------------------------------------------- spracovanie
def spracuj_udalost(
    con, *, payload, now, variant_id=None, zdroj=ZDROJ_WEBHOOK,
    expected_test_mode=None,
) -> dict:
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
    if expected_test_mode not in (None, True, False):
        raise ValueError("expected_test_mode musí byť bool alebo None")
    test_mode = _atributy(payload).get("test_mode")
    if expected_test_mode is not None and test_mode is not expected_test_mode:
        raise UdalostNepouzitelna("udalosť je v nesprávnom platobnom režime")
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
                    "stav": None, "zdroj": zdroj, "test_mode": test_mode}
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
            "test_mode": test_mode,
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
        # Tá istá objednávka už riadok má — nič nové sa nestalo.
        return {"akcia": AKCIA_UZ_UDELENE, "objednavka": objednavka, "user_id": user_id}

    suma, mena = _suma(payload)
    # Ostrá konfigurácia vždy obsahuje variant. Vtedy už nestačí user_id z
    # prehliadača: objednávka musí spotrebovať presne ten krátkodobý pokus, pri
    # ktorom používateľ potvrdil aktuálne podmienky a cenu.
    if ocakavany_variant:
        attempt_id = custom_checkout_attempt(payload)
        if attempt_id is None:
            raise UdalostNepouzitelna("chýba pokus objednávky")
        if suma != CENA_ZAKLADAJUCI_CENTY or mena != MENA_ZAKLADAJUCI:
            raise UdalostNepouzitelna("platba nezodpovedá odsúhlasenej cene")
        status = _atributy(payload).get("status")
        if not isinstance(status, str) or status.strip().casefold() != "paid":
            raise UdalostNepouzitelna("objednávka nie je zaplatená")
        mark_checkout_paid(
            con,
            public_id=attempt_id,
            user_id=user_id,
            provider_order_id=objednavka,
            now=now,
        )

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

    vypredane = pocet_zaplatenych_zakladajucich(con) >= KAPACITA_ZAKLADAJUCICH
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
        vypredane = pocet_zaplatenych_zakladajucich(con) >= KAPACITA_ZAKLADAJUCICH
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
    # Po vypredaní sa nevydávajú ani ďalšie testovacie prístupy. Testovací
    # prístup udelený skôr však neukrojí miesto zo zakladajúcej ponuky.
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
    if typ == "order_refunded" and refund_kind(payload) != "full":
        # LemonSqueezy posiela rovnakú udalosť pri čiastočnom aj úplnom
        # vrátení. Čiastočné vrátenie nesmie potichu zrušiť celý prístup.
        raise UdalostNepouzitelna("refundácia nie je úplná")
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
    if akcia == AKCIA_VRATENE:
        close_payment_cases_for_refund(
            con, provider_order_id=objednavka, now=now
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
        "obsadene": pocet_zaplatenych_zakladajucich(con),
        "kapacita": KAPACITA_ZAKLADAJUCICH,
        "cakajucich_tiel": pocet_cakajucich(con),
        "nevybavene_vratky": int(nevybavene),
    }


def spracuj_odlozene(
    con, *, tajomstvo, now, variant_id=None, limit=MAX_ODLOZENYCH,
    expected_test_mode=None,
) -> dict:
    """Dobehni telá, ktoré čakali. Každé prejde overením podpisu, ako by prišlo teraz.

    Beží mimo requestu (rekonciliačný skript), takže tu už tajomstvo k dispozícii
    je. Telo s podpisom, ktorý nesedí, sa neudelí a označí sa — je to buď smeť
    z internetu, alebo majiteľ nastavil iné tajomstvo, než akým poskytovateľ
    podpisuje.

    Produkčný job posiela ``expected_test_mode=False``. Testovacie aj neoznačené
    udalosti tak nechá nedotknuté pre samostatný payment smoke alebo ručnú
    kontrolu. Nejasný režim nesmie meniť produkčné nároky.
    """
    if expected_test_mode not in (None, True, False):
        raise ValueError("expected_test_mode musí byť bool alebo None")
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
        try:
            payload = json.loads(telo)
        except (ValueError, UnicodeDecodeError):
            payload = None
        if isinstance(payload, dict):
            test_mode = _atributy(payload).get("test_mode")
            if expected_test_mode is False and test_mode is not False:
                continue
            if expected_test_mode is True and test_mode is not True:
                continue
        vysledok = None
        if not overit_podpis(tajomstvo=tajomstvo, telo=telo, podpis=riadok[2]):
            vysledok = "neplatny_podpis"
            suhrn["neplatny_podpis"] += 1
        elif payload is None:
            vysledok = "pokazene_telo"
            suhrn["pokazene"] += 1
        else:
            try:
                udalost = spracuj_udalost(
                    con, payload=payload, now=now, variant_id=variant_id,
                    zdroj=ZDROJ_ODLOZENE,
                    expected_test_mode=expected_test_mode,
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


def spracuj_odlozene_pre_smoke(
    con, *, tajomstvo, now, objednavka_id, event_type, variant_id=None
) -> dict:
    """Process only one smoke order/event and leave every unrelated row intact.

    The payment smoke runs while public payments are off, so the real webhook
    endpoint stores signed bodies for later processing.  A smoke must prove
    that its own webhook arrived; API reconciliation is deliberately not a
    substitute.  We therefore locate an exact order/event pair, verify every
    matching signature before changing anything, and never mark unrelated or
    malformed queue rows as processed.
    """
    order_ref = _bezpecne_id(objednavka_id)
    if not order_ref:
        raise ValueError("neplatná objednávka smoke testu")
    if event_type not in {UDALOST_UDELUJUCA, "order_refunded"}:
        raise ValueError("neplatný typ udalosti smoke testu")
    now = _cas(now)
    matches = []
    rows = con.execute(
        """SELECT id, telo, podpis FROM platobne_odlozene
           WHERE spracovane_o IS NULL ORDER BY id"""
    ).fetchall()
    for row in rows:
        body = bytes(row[1])
        try:
            payload = json.loads(body)
        except (ValueError, UnicodeDecodeError):
            continue
        if typ_udalosti(payload) != event_type:
            continue
        if objednavka_ref(payload) != order_ref:
            continue
        if _atributy(payload).get("test_mode") is not True:
            continue
        matches.append((row, body, payload))

    if not matches:
        return {"spracovane": 0, "udalost": None}
    if any(
        not overit_podpis(tajomstvo=tajomstvo, telo=body, podpis=row[2])
        for row, body, _payload in matches
    ):
        raise UdalostNepouzitelna("podpis smoke webhooku nesedí")

    primary = None
    processed = 0
    for row, _body, payload in matches:
        event = spracuj_udalost(
            con,
            payload=payload,
            now=now,
            variant_id=variant_id,
            zdroj=ZDROJ_ODLOZENE,
            expected_test_mode=True,
        )
        con.execute(
            """UPDATE platobne_odlozene
                  SET spracovane_o=?, vysledok=?
                WHERE id=? AND spracovane_o IS NULL""",
            (now, event["akcia"], row[0]),
        )
        con.commit()
        processed += 1
        if primary is None or primary.get("akcia") == AKCIA_UZ_SPRACOVANE:
            primary = event
    return {"spracovane": processed, "udalost": primary}


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
# prísť e-mail, token, úryvok logu ani identifikátor objednávky. Konkrétne
# referencie patria výhradne do chránenej tabuľky payment_cases.
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
    typ = _cislo(typ)
    pocet = _pocet(pocet)
    den = den if isinstance(den, str) and den else "?"
    if druh == DRUH_NAD_KAPACITU:
        return {
            "kluc": f"{DRUH_NAD_KAPACITU}:{den}:{pocet}",
            "titul": "Uvar.si: platba nad kapacitu — treba vrátiť peniaze",
            "sprava": (
                f"Počet nevyriešených platieb nad kapacitu: {pocet}. "
                "Zákazník nedostal členstvo a treba mu vrátiť celú platbu. "
                "Konkrétnu objednávku otvor v chránenej evidencii platieb."
            ),
        }
    if druh == DRUH_DUPLICITA:
        return {
            "kluc": f"{DRUH_DUPLICITA}:{den}:{pocet}",
            "titul": "Uvar.si: druhá platba toho istého účtu",
            "sprava": (
                f"Počet nevyriešených duplicitných platieb: {pocet}. "
                "Zákazník už členstvo mal a platbu navyše treba vrátiť. "
                "Konkrétnu objednávku otvor v chránenej evidencii platieb."
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
                "v chránenej evidencii platieb."
            ),
        }
    if druh == DRUH_NEPOUZITELNA:
        return {
            "kluc": f"{DRUH_NEPOUZITELNA}:{den}:{pocet}",
            "titul": "Uvar.si: podpísaná platba sa nedá priradiť k účtu",
            "sprava": (
                "Prišla platba s platným podpisom, ktorú appka nevie priradiť "
                "k žiadnemu účtu (chýbajúce custom_data alebo neznáme id). "
                "Peniaze teda mohli prísť a nikto za ne nič nedostal. "
                f"Otvorených prípadov je {pocet}; podrobnosti sú iba v "
                "chránenej evidencii platieb."
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
            "kluc": f"{DRUH_BEZ_UCTU}:{den}:{pocet}",
            "titul": "Uvar.si: zaplatené objednávky bez účtu",
            "sprava": (
                f"Rekonciliácia našla {pocet} zaplatených objednávok, ktoré sa "
                "nedajú priradiť k žiadnemu účtu (iná e-mailová adresa pri "
                "platbe než pri prihlásení). Konkrétne objednávky otvor v "
                "chránenej evidencii platieb."
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


def checkout_attempt_z_objednavky(objednavka):
    """Audited checkout token returned by the provider API, if present."""
    atributy = _atributy_objednavky(objednavka)
    kandidati = [atributy.get("custom_data")]
    polozka = atributy.get("first_order_item")
    if isinstance(polozka, dict):
        kandidati.append(polozka.get("custom_data"))
    for custom in kandidati:
        if not isinstance(custom, dict):
            continue
        attempt = _bezpecne_id(custom.get("checkout_attempt"))
        if attempt is not None and len(attempt) >= 43:
            return attempt
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
        "test_mode": atributy.get("test_mode"),
    }
    for field in ("refunded", "refunded_amount", "refunded_at"):
        if field in atributy:
            prepis[field] = atributy.get(field)
    if isinstance(polozka, dict) and polozka.get("variant_id") is not None:
        prepis["first_order_item"] = {"variant_id": polozka.get("variant_id")}
    meta = {"event_name": typ}
    if user_id is not None:
        custom = {"user_id": str(int(user_id)),
                  "produkt": PRODUKT_ZAKLADAJUCI}
        attempt = checkout_attempt_z_objednavky(objednavka)
        if attempt is not None:
            custom["checkout_attempt"] = attempt
        meta["custom_data"] = custom
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
