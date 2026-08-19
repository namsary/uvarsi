import math
from datetime import date
from urllib.parse import urlparse


ALLOWED_STORES = frozenset({"Lidl", "Kaufland", "Tesco"})

_MIGRATION_COLUMNS = {
    "source_url": "TEXT",
    "source_page": "INTEGER",
    "valid_from": "TEXT",
    "valid_to": "TEXT",
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


def replace_store_week(con, week, store, offers):
    """Atomically replace a single store's captured-week offers after validation."""
    if store not in ALLOWED_STORES:
        raise ValueError("store must be Lidl, Kaufland, or Tesco")

    offers = list(offers)
    for offer in offers:
        if offer.get("obchod") != store:
            raise ValueError("offer store must match replacement store")
        validate_offer(offer)

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
            [tuple([week] + [offer.get(column) for column in _INSERT_COLUMNS[1:]]) for offer in offers],
        )
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
