from datetime import date, timedelta

try:
    from .offer_data import ALLOWED_STORES, migrate_akcie_schema, validate_offer
except ImportError:
    from offer_data import ALLOWED_STORES, migrate_akcie_schema, validate_offer


def current_monday(today: date | None = None) -> str:
    today = today or date.today()
    return (today - timedelta(days=today.weekday())).isoformat()


def current_verified_offers(con, stores, today: date | None = None):
    stores = [store for store in stores if store in ALLOWED_STORES]
    if not stores:
        return []

    migrate_akcie_schema(con)
    today = today or date.today()
    marks = ",".join("?" for _ in stores)
    cursor = con.execute(
        f"""SELECT rowid AS id, * FROM akcie
            WHERE tyzden=? AND obchod IN ({marks})
            ORDER BY cena""",
        (current_monday(today), *stores),
    )
    columns = [column[0] for column in cursor.description]
    verified = []
    for row in cursor.fetchall():
        offer = dict(row) if hasattr(row, "keys") else dict(zip(columns, row))
        try:
            validate_offer(offer)
        except ValueError:
            continue
        if offer["valid_from"] <= today.isoformat() <= offer["valid_to"]:
            verified.append(row)
    return verified


def offers_for_current_week(con, stores: list[str], today: date | None = None):
    return current_verified_offers(con, stores, today)
