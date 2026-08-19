import math
import sqlite3

import pytest

from app.offer_data import migrate_akcie_schema, replace_store_week, validate_offer


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
