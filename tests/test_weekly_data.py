import sqlite3
from datetime import date

import pytest

from app.offer_data import offer_key_for
from app.weekly_data import current_monday, offers_for_current_week


FULL_SCHEMA = """CREATE TABLE akcie (
    id INTEGER PRIMARY KEY, tyzden TEXT, obchod TEXT, nazov TEXT, kategoria TEXT,
    cena REAL, povodna REAL, zlava TEXT, jednotka TEXT, source_url TEXT,
    source_page INTEGER, valid_from TEXT, valid_to TEXT, offer_key TEXT
)"""


def full_connection(rows):
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.execute(FULL_SCHEMA)
    con.executemany("INSERT INTO akcie VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
    return con


def offer(offer_id, name, store="Lidl", **overrides):
    values = {
        "tyzden": "2026-08-17",
        "obchod": store,
        "nazov": name,
        "kategoria": "mliecne",
        "cena": 1.0,
        "povodna": 1.5,
        "zlava": "-33 %",
        "jednotka": "1 l",
        "source_url": f"https://example.test/{offer_id}.jpg",
        "source_page": 1,
        "valid_from": "2026-08-17",
        "valid_to": "2026-08-23",
    }
    values.update(overrides)
    trusted = {
        "tyzden": values["tyzden"], "obchod": values["obchod"], "nazov": values["nazov"],
        "kategoria": values["kategoria"], "cena": values["cena"], "povodna": values["povodna"],
        "zlava": values["zlava"], "jednotka": values["jednotka"], "source_url": values["source_url"],
        "source_page": values["source_page"], "valid_from": values["valid_from"], "valid_to": values["valid_to"],
    }
    key = None
    try:
        key = offer_key_for(values["tyzden"], trusted)
    except ValueError:
        pass
    return (
        offer_id, values["tyzden"], values["obchod"], values["nazov"], values["kategoria"],
        values["cena"], values["povodna"], values["zlava"], values["jednotka"],
        values["source_url"], values["source_page"], values["valid_from"], values["valid_to"], key,
    )


def test_current_monday_uses_monday_for_any_day():
    assert current_monday(date(2026, 8, 18)) == "2026-08-17"


def test_never_falls_back_to_last_weeks_prices():
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE akcie (tyzden TEXT, obchod TEXT, nazov TEXT, cena REAL)")
    con.execute("INSERT INTO akcie VALUES ('2026-08-10', 'Lidl', 'Mlieko', 1.0)")

    assert offers_for_current_week(con, ["Lidl"], date(2026, 8, 18)) == []


def test_reused_legacy_rowid_with_null_offer_key_is_not_eligible():
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.execute(FULL_SCHEMA)
    con.execute(
        "INSERT INTO akcie VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (*offer(1, "Pôvodné mlieko")[:-1], None),
    )
    con.execute("DELETE FROM akcie WHERE id=1")
    con.execute(
        "INSERT INTO akcie VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (*offer(1, "Iný výrobok", source_url="https://example.test/reused.jpg")[:-1], None),
    )

    rows = offers_for_current_week(con, ["Lidl"], date(2026, 8, 18))

    assert rows == []


def test_returns_only_offers_from_current_week_and_selected_stores():
    con = full_connection([
        offer(1, "Mlieko", source_page=2),
        offer(2, "Chlieb", store="Tesco", cena=1.2, povodna=1.8, source_page=3),
        offer(3, "Maslo", tyzden="2026-08-10", cena=0.9, valid_from="2026-08-10", valid_to="2026-08-16"),
    ])

    rows = offers_for_current_week(con, ["Lidl"], date(2026, 8, 18))

    assert [row["nazov"] for row in rows] == ["Mlieko"]


def test_current_query_excludes_unproven_and_outside_validity_offers():
    con = full_connection([
        offer(1, "Overená"),
        offer(2, "Bez URL", source_url=None),
        offer(3, "Bez strany", source_page=None),
        offer(4, "Po platnosti", valid_from="2026-08-10", valid_to="2026-08-17"),
        offer(5, "Pred platnosťou", valid_from="2026-08-19", valid_to="2026-08-25"),
        offer(6, "Minulý týždeň", tyzden="2026-08-10", valid_from="2026-08-10"),
    ])

    rows = offers_for_current_week(con, ["Lidl"], date(2026, 8, 18))

    assert [row["nazov"] for row in rows] == ["Overená"]


@pytest.mark.parametrize(
    "overrides",
    [
        {"source_url": ""},
        {"source_url": "ftp://example.test/flyer.jpg"},
        {"source_page": 0},
        {"valid_from": "2026-08-017"},
        {"cena": 0},
        {"cena": -1},
        {"povodna": 0},
        {"povodna": 0.5},
    ],
)
def test_current_offer_reader_excludes_malformed_non_null_rows(overrides):
    con = full_connection([offer(1, "Overená"), offer(2, "Chybná", **overrides)])

    rows = offers_for_current_week(con, ["Lidl"], date(2026, 8, 18))

    assert [row["nazov"] for row in rows] == ["Overená"]
