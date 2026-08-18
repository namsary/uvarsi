from datetime import date, timedelta


def current_monday(today: date | None = None) -> str:
    today = today or date.today()
    return (today - timedelta(days=today.weekday())).isoformat()


def offers_for_current_week(con, stores: list[str], today: date | None = None):
    if not stores:
        return []

    marks = ",".join("?" for _ in stores)
    return con.execute(
        f"SELECT * FROM akcie WHERE tyzden=? AND obchod IN ({marks}) ORDER BY cena",
        (current_monday(today), *stores),
    ).fetchall()
