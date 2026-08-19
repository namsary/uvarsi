import math
import sqlite3

import pytest

from app.offer_data import migrate_akcie_schema, offer_key_for, replace_store_week, validate_offer


def valid_offer(**overrides):
    offer = {
        "obchod": "Lidl",
        "nazov": "Plnotučné mlieko",
        "kategoria": "mliecne",
        "cena": 1.19,
        "povodna": 1.49,
        "zlava": "-20 %",
        "jednotka": "1 l",
        "source_url": "https://example.test/lidl-letak-2.jpg",
        "source_page": 2,
        "valid_from": "2026-08-17",
        "valid_to": "2026-08-23",
    }
    offer.update(overrides)
    return offer


def legacy_connection():
    con = sqlite3.connect(":memory:")
    con.execute(
        """CREATE TABLE akcie (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tyzden TEXT NOT NULL,
            obchod TEXT NOT NULL,
            nazov TEXT NOT NULL,
            kategoria TEXT,
            cena REAL,
            povodna REAL,
            zlava TEXT,
            jednotka TEXT
        )"""
    )
    return con


def test_migration_adds_nullable_provenance_and_validity_columns_idempotently():
    con = legacy_connection()

    migrate_akcie_schema(con)
    migrate_akcie_schema(con)

    columns = {row[1]: row for row in con.execute("PRAGMA table_info(akcie)")}
    assert {name: columns[name][2] for name in ("source_url", "source_page", "valid_from", "valid_to")} == {
        "source_url": "TEXT",
        "source_page": "INTEGER",
        "valid_from": "TEXT",
        "valid_to": "TEXT",
    }
    assert all(columns[name][3] == 0 for name in ("source_url", "source_page", "valid_from", "valid_to"))


def test_migration_adds_nullable_offer_key_without_backfilling_a_legacy_guess():
    con = legacy_connection()
    con.execute(
        "INSERT INTO akcie (tyzden, obchod, nazov, cena) VALUES (?, ?, ?, ?)",
        ("2026-08-17", "Lidl", "Legacy mlieko", 1.19),
    )

    migrate_akcie_schema(con)

    column = {row[1]: row for row in con.execute("PRAGMA table_info(akcie)")}["offer_key"]
    assert column[2] == "TEXT"
    assert column[3] == 0
    assert con.execute("SELECT offer_key FROM akcie").fetchone() == (None,)


def stored_offer_key(week="2026-08-17", **overrides):
    con = legacy_connection()
    replace_store_week(con, week, overrides.get("obchod", "Lidl"), [valid_offer(**overrides)])
    return con.execute("SELECT offer_key FROM akcie WHERE tyzden=?", (week,)).fetchone()[0]


@pytest.mark.parametrize(
    "week, overrides",
    [
        ("2026-08-24", {}),
        ("2026-08-17", {"obchod": "Tesco"}),
        ("2026-08-17", {"source_url": "https://example.test/other.jpg"}),
        ("2026-08-17", {"source_page": 3}),
        ("2026-08-17", {"valid_from": "2026-08-16"}),
        ("2026-08-17", {"valid_to": "2026-08-24"}),
        ("2026-08-17", {"nazov": "Polotučné mlieko"}),
        ("2026-08-17", {"jednotka": "500 ml"}),
        ("2026-08-17", {"cena": 1.20}),
        ("2026-08-17", {"povodna": 1.50}),
        ("2026-08-17", {"kategoria": "trvanlive"}),
        ("2026-08-17", {"zlava": "-19 %"}),
    ],
)
def test_ingestion_offer_key_is_deterministic_and_changes_with_every_trusted_fact(week, overrides):
    baseline = stored_offer_key()

    assert isinstance(baseline, str) and baseline.startswith("offer_")
    assert stored_offer_key() == baseline
    assert stored_offer_key(week, **overrides) != baseline


def test_offer_key_survives_sqlite_real_round_trip_for_integer_prices():
    con = legacy_connection()
    con.row_factory = sqlite3.Row
    replace_store_week(con, "2026-08-17", "Lidl", [valid_offer(cena=1, povodna=2)])

    stored = dict(con.execute("SELECT * FROM akcie").fetchone())

    assert stored["offer_key"] == offer_key_for(stored["tyzden"], stored)


@pytest.mark.parametrize(
    "overrides",
    [
        {"obchod": "Billa"},
        {"source_url": ""},
        {"source_url": " https://example.test/lidl-letak-2.jpg"},
        {"source_page": 0},
        {"valid_from": "not-a-date"},
        {"valid_from": "2026-08-24", "valid_to": "2026-08-23"},
        {"nazov": "   "},
        {"jednotka": ""},
        {"cena": math.inf},
        {"cena": 0},
        {"povodna": 1.18},
    ],
)
def test_validator_rejects_offers_without_trustworthy_required_data(overrides):
    with pytest.raises(ValueError):
        validate_offer(valid_offer(**overrides))


def test_validator_accepts_a_complete_allowlisted_offer():
    offer = valid_offer()

    validate_offer(offer)


def test_invalid_replacement_leaves_previous_store_week_rows_untouched():
    con = legacy_connection()
    migrate_akcie_schema(con)
    con.execute(
        """INSERT INTO akcie
           (tyzden, obchod, nazov, cena, povodna, jednotka, source_url, source_page, valid_from, valid_to)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        ("2026-08-17", "Lidl", "Predchádzajúce mlieko", 1.19, 1.49, "1 l",
         "https://example.test/old.jpg", 1, "2026-08-17", "2026-08-23"),
    )
    con.commit()

    with pytest.raises(ValueError):
        replace_store_week(
            con,
            "2026-08-17",
            "Lidl",
            [valid_offer(nazov="Nové mlieko"), valid_offer(nazov="Chybné", cena=0)],
        )

    rows = con.execute("SELECT nazov FROM akcie WHERE tyzden=? AND obchod=?", ("2026-08-17", "Lidl")).fetchall()
    assert rows == [("Predchádzajúce mlieko",)]


def test_autocommit_insertion_failure_restores_previous_store_week_rows():
    con = legacy_connection()
    migrate_akcie_schema(con)
    con.execute(
        """INSERT INTO akcie
           (tyzden, obchod, nazov, cena, povodna, jednotka, source_url, source_page, valid_from, valid_to)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        ("2026-08-17", "Lidl", "Predchádzajúce mlieko", 1.19, 1.49, "1 l",
         "https://example.test/old.jpg", 1, "2026-08-17", "2026-08-23"),
    )
    con.execute(
        """CREATE TRIGGER reject_new_offer
           BEFORE INSERT ON akcie WHEN NEW.nazov = 'Nové mlieko'
           BEGIN SELECT RAISE(FAIL, 'simulated insertion failure'); END"""
    )
    con.isolation_level = None

    with pytest.raises(sqlite3.IntegrityError, match="simulated insertion failure"):
        replace_store_week(con, "2026-08-17", "Lidl", [valid_offer(nazov="Nové mlieko")])

    rows = con.execute(
        "SELECT nazov FROM akcie WHERE tyzden=? AND obchod=?",
        ("2026-08-17", "Lidl"),
    ).fetchall()
    assert rows == [("Predchádzajúce mlieko",)]
