import sqlite3
from datetime import date

from app.weekly_data import current_monday, offers_for_current_week


def test_current_monday_uses_monday_for_any_day():
    assert current_monday(date(2026, 8, 18)) == "2026-08-17"


def test_never_falls_back_to_last_weeks_prices():
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE akcie (tyzden TEXT, obchod TEXT, nazov TEXT, cena REAL)")
    con.execute("INSERT INTO akcie VALUES ('2026-08-10', 'Lidl', 'Mlieko', 1.0)")

    assert offers_for_current_week(con, ["Lidl"], date(2026, 8, 18)) == []


def test_returns_only_offers_from_current_week_and_selected_stores():
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE akcie (tyzden TEXT, obchod TEXT, nazov TEXT, cena REAL)")
    con.executemany(
        "INSERT INTO akcie VALUES (?, ?, ?, ?)",
        [
            ("2026-08-17", "Lidl", "Mlieko", 1.0),
            ("2026-08-17", "Tesco", "Chlieb", 1.2),
            ("2026-08-10", "Lidl", "Maslo", 0.9),
        ],
    )

    rows = offers_for_current_week(con, ["Lidl"], date(2026, 8, 18))

    assert [row[2] for row in rows] == ["Mlieko"]
