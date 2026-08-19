from datetime import date, timedelta

try:
    from .offer_data import ALLOWED_STORES, migrate_akcie_schema
except ImportError:
    from offer_data import ALLOWED_STORES, migrate_akcie_schema


def current_monday(today: date | None = None) -> str:
    today = today or date.today()
    return (today - timedelta(days=today.weekday())).isoformat()


def offers_for_current_week(con, stores: list[str], today: date | None = None):
    stores = [store for store in stores if store in ALLOWED_STORES]
    if not stores:
        return []

    migrate_akcie_schema(con)
    today = today or date.today()
    marks = ",".join("?" for _ in stores)
    return con.execute(
        f"""SELECT * FROM akcie
            WHERE tyzden=? AND obchod IN ({marks})
              AND source_url IS NOT NULL AND source_page IS NOT NULL
              AND valid_from IS NOT NULL AND valid_to IS NOT NULL
              AND valid_from<=? AND valid_to>=?
            ORDER BY cena""",
        (current_monday(today), *stores, today.isoformat(), today.isoformat()),
    ).fetchall()
