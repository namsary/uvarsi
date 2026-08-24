import hashlib
import json
import math
from datetime import date
from decimal import Decimal
from urllib.parse import urlparse


ALLOWED_STORES = frozenset({"Lidl", "Kaufland", "Tesco"})

_MIGRATION_COLUMNS = {
    "source_url": "TEXT",
    "source_page": "INTEGER",
    "valid_from": "TEXT",
    "valid_to": "TEXT",
    "offer_key": "TEXT",
}

_INSERT_COLUMNS = (
    "tyzden",
    "obchod",
    "nazov",
    "kategoria",
    "cena",
    "povodna",
    "zlava",
    "jednotka",
    "source_url",
    "source_page",
    "valid_from",
    "valid_to",
    "offer_key",
)

_OFFER_KEY_FIELDS = (
    "tyzden",
    "obchod",
    "source_url",
    "source_page",
    "valid_from",
    "valid_to",
    "nazov",
    "jednotka",
    "cena",
    "povodna",
    "kategoria",
    "zlava",
)


def migrate_akcie_schema(con):
    """Add trust metadata to an existing akcie table without backfilling it."""
    existing = {row[1] for row in con.execute("PRAGMA table_info(akcie)")}
    for name, column_type in _MIGRATION_COLUMNS.items():
        if name not in existing:
            con.execute(f"ALTER TABLE akcie ADD COLUMN {name} {column_type}")


def _validated_iso_date(value, field):
    if not isinstance(value, str):
        raise ValueError(f"{field} must be an ISO date")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{field} must be an ISO date") from exc
    if value != parsed.isoformat():
        raise ValueError(f"{field} must be an ISO date")
    return parsed


def _positive_finite_number(value, field):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field} must be a finite positive number")
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{field} must be a finite positive number")


def validate_offer(offer):
    """Reject any offer that cannot safely be used in a customer plan."""
    if offer.get("obchod") not in ALLOWED_STORES:
        raise ValueError("obchod must be Lidl, Kaufland, or Tesco")

    source_url = offer.get("source_url")
    if not isinstance(source_url, str) or not source_url or source_url != source_url.strip():
        raise ValueError("source_url must be a non-empty exact URL")
    parsed_url = urlparse(source_url)
    if parsed_url.scheme not in ("http", "https") or not parsed_url.netloc:
        raise ValueError("source_url must be a non-empty exact URL")

    source_page = offer.get("source_page")
    if isinstance(source_page, bool) or not isinstance(source_page, int) or source_page <= 0:
        raise ValueError("source_page must be a positive integer")

    valid_from = _validated_iso_date(offer.get("valid_from"), "valid_from")
    valid_to = _validated_iso_date(offer.get("valid_to"), "valid_to")
    if valid_from > valid_to:
        raise ValueError("valid_from must not be after valid_to")

    for field in ("nazov", "jednotka"):
        if not isinstance(offer.get(field), str) or not offer[field].strip():
            raise ValueError(f"{field} must be non-empty")

    _positive_finite_number(offer.get("cena"), "cena")
    original_price = offer.get("povodna")
    if original_price is not None:
        _positive_finite_number(original_price, "povodna")
        if original_price < offer["cena"]:
            raise ValueError("povodna must be at least cena")


OFFER_KEY_PREFIX = "offer_"

# Koľko hexa znakov odtlačku sa v kľúči nechá. Celých 64 znakov stálo v prompte
# 35 tokenov z 60 — dve tretiny riadku ponuky boli identifikátor, ktorý model iba
# zopakoval späť. 12 znakov = 48 bitov: pri pár tisíc riadkoch v `akcie` je
# narodeninová pravdepodobnosť kolízie rádovo 10⁻⁹ za týždeň, a aj tá jediná
# možná kolízia sa dole hlasno odmietne, nikdy nepodá ako cudziu cenu.
# Kratšie (8–10 znakov) už tokeny nešetrí — tokenizér ich zlomí rovnako —
# takže by sa riziko zvyšovalo zadarmo.
OFFER_KEY_DIGEST_CHARS = 12


class OfferKeyCollision(ValueError):
    """Dva rôzne overené výrobky vyšli na ten istý kľúč.

    Toto je jediná chyba, ktorú si tento produkt nesmie dovoliť prehltnúť:
    znamenala by reálnu cenu pripísanú k inému výrobku. Preto je to výnimka,
    nie logovaný varovný riadok — dávka sa odmietne celá a stará ostane ležať.
    """


def _offer_facts(week, offer):
    """Kanonický predobraz kľúča: presne tie fakty, za ktoré appka ručí."""
    _validated_iso_date(week, "tyzden")
    validate_offer(offer)
    facts = {field: (week if field == "tyzden" else offer.get(field)) for field in _OFFER_KEY_FIELDS}
    for field in ("cena", "povodna"):
        if facts[field] is not None:
            facts[field] = format(Decimal(str(facts[field])).normalize(), "f")
    return json.dumps(facts, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(canonical):
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def offer_key_for(week, offer):
    """Build a stable opaque identity from every trusted offer fact."""
    return OFFER_KEY_PREFIX + _digest(_offer_facts(week, offer))[:OFFER_KEY_DIGEST_CHARS]


def legacy_offer_key_for(week, offer):
    """Kľúč v pôvodnej celej dĺžke — tvar, ktorý leží v starých riadkoch `akcie`."""
    return OFFER_KEY_PREFIX + _digest(_offer_facts(week, offer))


def canonical_offer_key(key):
    """Zjednoť starý dlhý aj nový krátky kľúč na jeden tvar.

    Krátky kľúč je predponou dlhého, takže orezanie stačí. Vďaka tomu sa
    uložený plán s dlhými kľúčmi trafí na tie isté ponuky a používateľovi
    neprasknú jedlá len preto, že sme skrátili identifikátor.
    """
    if not isinstance(key, str):
        return key
    return key[:len(OFFER_KEY_PREFIX) + OFFER_KEY_DIGEST_CHARS]


def offer_key_matches(stored, week, offer):
    """Sedí uložený kľúč na overené fakty riadku? Uzná oba formáty.

    Zhovievavosť je len k dĺžke, nie k obsahu: zmenená cena neprejde ani
    v jednom tvare, takže kontrola proti zásahu do databázy ostáva celá.
    """
    if not isinstance(stored, str) or not stored:
        return False
    try:
        canonical = _offer_facts(week, offer)
    except (ValueError, KeyError, TypeError):
        return False
    digest = _digest(canonical)
    return stored in (OFFER_KEY_PREFIX + digest[:OFFER_KEY_DIGEST_CHARS], OFFER_KEY_PREFIX + digest)


def detect_offer_key_collision(records):
    """Zdvihni `OfferKeyCollision`, keď jeden kľúč nesie dva rôzne výrobky.

    `records` sú dvojice (kľúč, kanonický predobraz). Rovnaký predobraz pod
    rovnakým kľúčom je obyčajný duplikát riadku a je v poriadku; dva rôzne
    predobrazy pod jedným kľúčom sú kolízia.
    """
    seen = {}
    for key, canonical in records:
        previous = seen.setdefault(key, canonical)
        if previous != canonical:
            raise OfferKeyCollision(
                f"offer_key {key} pripadol dvom rôznym ponukám; dávka sa neuloží. "
                f"Prvá: {previous}. Druhá: {canonical}."
            )


def _stored_offer_records(con):
    """Kľúč a predobraz pre každý riadok `akcie`, ktorý sa dá overiť."""
    cursor = con.execute("SELECT * FROM akcie")
    columns = [column[0] for column in cursor.description]
    for row in cursor.fetchall():
        offer = dict(row) if hasattr(row, "keys") else dict(zip(columns, row))
        try:
            canonical = _offer_facts(offer.get("tyzden"), offer)
        except (ValueError, KeyError, TypeError):
            continue
        yield canonical_offer_key(offer.get("offer_key")), canonical


def replace_store_week(con, week, store, offers):
    """Atomically replace a single store's captured-week offers after validation."""
    if store not in ALLOWED_STORES:
        raise ValueError("store must be Lidl, Kaufland, or Tesco")

    offers = list(offers)
    prepared = []
    batch = []
    for offer in offers:
        if offer.get("obchod") != store:
            raise ValueError("offer store must match replacement store")
        validate_offer(offer)
        record = {"tyzden": week, **offer}
        record["offer_key"] = offer_key_for(week, offer)
        prepared.append(record)
        batch.append((canonical_offer_key(record["offer_key"]), _offer_facts(week, offer)))

    # Kolízia v samotnej dávke sa musí chytiť ešte pred tým, než sa čokoľvek
    # zmaže — inak by odmietnutá dávka stála predošlý týždeň obchodu.
    detect_offer_key_collision(batch)

    migrate_akcie_schema(con)
    placeholders = ", ".join("?" for _ in _INSERT_COLUMNS)
    use_savepoint = con.in_transaction
    if use_savepoint:
        con.execute("SAVEPOINT replace_store_week")
    else:
        con.execute("BEGIN")
    try:
        con.execute("DELETE FROM akcie WHERE tyzden=? AND obchod=?", (week, store))
        con.executemany(
            f"INSERT INTO akcie ({', '.join(_INSERT_COLUMNS)}) VALUES ({placeholders})",
            [tuple(offer.get(column) for column in _INSERT_COLUMNS) for offer in prepared],
        )
        # A ešte raz proti celej tabuľke: kolízia sa nemusí zrodiť v jednej
        # dávke, môže vzniknúť až voči riadku iného obchodu či týždňa. Beží to
        # vnútri transakcie, takže odmietnutie vráti tabuľku do pôvodného stavu.
        detect_offer_key_collision(_stored_offer_records(con))
    except Exception:
        if use_savepoint:
            con.execute("ROLLBACK TO SAVEPOINT replace_store_week")
            con.execute("RELEASE SAVEPOINT replace_store_week")
        else:
            con.rollback()
        raise
    else:
        if use_savepoint:
            con.execute("RELEASE SAVEPOINT replace_store_week")
        else:
            con.commit()
