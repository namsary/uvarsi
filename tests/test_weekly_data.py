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
    con.execute(
        """CREATE TABLE akcie (
            tyzden TEXT, obchod TEXT, nazov TEXT, cena REAL,
            source_url TEXT, source_page INTEGER, valid_from TEXT, valid_to TEXT
        )"""
    )
    con.executemany(
        "INSERT INTO akcie VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [
            ("2026-08-17", "Lidl", "Mlieko", 1.0, "https://example.test/lidl.jpg", 2, "2026-08-17", "2026-08-23"),
            ("2026-08-17", "Tesco", "Chlieb", 1.2, "https://example.test/tesco.jpg", 3, "2026-08-17", "2026-08-23"),
            ("2026-08-10", "Lidl", "Maslo", 0.9, "https://example.test/old.jpg", 1, "2026-08-10", "2026-08-16"),
        ],
    )

    rows = offers_for_current_week(con, ["Lidl"], date(2026, 8, 18))

    assert [row[2] for row in rows] == ["Mlieko"]


def test_current_query_excludes_unproven_and_outside_validity_offers():
    con = sqlite3.connect(":memory:")
    con.execute(
        """CREATE TABLE akcie (
            tyzden TEXT, obchod TEXT, nazov TEXT, cena REAL,
            source_url TEXT, source_page INTEGER, valid_from TEXT, valid_to TEXT
        )"""
    )
    con.executemany(
        "INSERT INTO akcie VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [
            ("2026-08-17", "Lidl", "Overená", 1.0, "https://example.test/verified.jpg", 1, "2026-08-17", "2026-08-23"),
            ("2026-08-17", "Lidl", "Bez URL", 1.0, None, 1, "2026-08-17", "2026-08-23"),
            ("2026-08-17", "Lidl", "Bez strany", 1.0, "https://example.test/no-page.jpg", None, "2026-08-17", "2026-08-23"),
            ("2026-08-17", "Lidl", "Po platnosti", 1.0, "https://example.test/expired.jpg", 1, "2026-08-10", "2026-08-17"),
            ("2026-08-17", "Lidl", "Pred platnosťou", 1.0, "https://example.test/future.jpg", 1, "2026-08-19", "2026-08-25"),
            ("2026-08-10", "Lidl", "Minulý týždeň", 1.0, "https://example.test/old.jpg", 1, "2026-08-10", "2026-08-23"),
        ],
    )

    rows = offers_for_current_week(con, ["Lidl"], date(2026, 8, 18))

    assert [row[2] for row in rows] == ["Overená"]
