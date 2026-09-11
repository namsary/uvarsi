import math
import sqlite3

import pytest

import app.offer_data as offer_data
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


def test_migration_and_atomic_writer_preserve_conditional_loyalty_price_separately():
    """Karta nesmie prepísať cenu, ktorú zaplatí zákazník bez vernostného programu."""
    con = legacy_connection()
    offer = valid_offer(
        obchod="Kaufland",
        nazov="Repkový olej Raciol",
        cena=1.69,
        povodna=2.99,
        zlava="-43 %",
        cena_s_kartou=1.55,
        zlava_s_kartou="-48 %",
        vernostny_program="Kaufland Card",
        minimalny_nakup=20.0,
        podmienka_s_kartou="aktivuj kupón v aplikácii",
    )

    replace_store_week(con, "2026-08-31", "Kaufland", [offer])

    con.row_factory = sqlite3.Row
    row = dict(con.execute("SELECT * FROM akcie").fetchone())
    assert row["cena"] == 1.69
    assert row["cena_s_kartou"] == 1.55
    assert row["zlava"] == "-43 %"
    assert row["zlava_s_kartou"] == "-48 %"
    assert row["vernostny_program"] == "Kaufland Card"
    assert row["minimalny_nakup"] == 20.0
    assert row["podmienka_s_kartou"] == "aktivuj kupón v aplikácii"


@pytest.mark.parametrize(
    "overrides",
    [
        {"cena_s_kartou": 1.55},
        {"cena_s_kartou": 1.69, "vernostny_program": "Kaufland Card"},
        {"cena_s_kartou": 1.75, "vernostny_program": "Kaufland Card"},
        {"vernostny_program": "Kaufland Card"},
        {"minimalny_nakup": 20.0},
        {"podmienka_s_kartou": "aktivuj kupón"},
        {
            "obchod": "Tesco",
            "cena_s_kartou": 1.55,
            "vernostny_program": "Kaufland Card",
        },
    ],
)
def test_validator_rejects_ambiguous_or_mismatched_loyalty_prices(overrides):
    with pytest.raises(ValueError):
        validate_offer(valid_offer(**overrides))


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


def test_store_stage_preserves_every_offer_fact_without_touching_active_rows():
    con = legacy_connection()
    migrate_akcie_schema(con)
    offer_data.migrate_offer_staging_schema(con)
    old = valid_offer(nazov="Aktívne staré mlieko")
    fresh = valid_offer(
        nazov="Čerstvé staged mlieko",
        cena_s_kartou=0.99,
        zlava_s_kartou="-34 %",
        vernostny_program="Lidl Plus",
        podmienka_s_kartou="aktivuj kupón v aplikácii",
    )
    replace_store_week(con, "2026-08-17", "Lidl", [old])

    con.execute("BEGIN IMMEDIATE")
    offer_data.stage_store_week(con, "2026-08-17", "Lidl", [fresh])
    con.commit()

    assert con.execute("SELECT nazov FROM akcie").fetchall() == [("Aktívne staré mlieko",)]
    con.row_factory = sqlite3.Row
    staged = dict(con.execute("SELECT * FROM akcie_staging").fetchone())
    for field in (
        "obchod", "nazov", "kategoria", "cena", "povodna", "zlava",
        "jednotka", "source_url", "source_page", "valid_from", "valid_to",
        "cena_s_kartou", "zlava_s_kartou", "vernostny_program",
        "minimalny_nakup", "podmienka_s_kartou",
    ):
        assert staged[field] == fresh.get(field)
    assert staged["offer_key"] == offer_key_for("2026-08-17", fresh)


def test_store_stage_never_commits_the_callers_outer_transaction():
    con = legacy_connection()
    migrate_akcie_schema(con)
    offer_data.migrate_offer_staging_schema(con)

    con.execute("BEGIN IMMEDIATE")
    offer_data.stage_store_week(con, "2026-08-17", "Lidl", [valid_offer()])
    assert con.in_transaction is True
    con.rollback()

    assert con.execute("SELECT COUNT(*) FROM akcie_staging").fetchone()[0] == 0


def test_failed_store_stage_restores_the_previous_stage_atomically():
    con = legacy_connection()
    migrate_akcie_schema(con)
    offer_data.migrate_offer_staging_schema(con)
    con.execute("BEGIN IMMEDIATE")
    offer_data.stage_store_week(con, "2026-08-17", "Lidl", [valid_offer(nazov="Zdravý stage")])
    con.commit()
    con.execute(
        """CREATE TRIGGER reject_bad_stage
           BEFORE INSERT ON akcie_staging WHEN NEW.nazov = 'Chybný stage'
           BEGIN SELECT RAISE(FAIL, 'simulated stage failure'); END"""
    )

    con.execute("BEGIN IMMEDIATE")
    with pytest.raises(sqlite3.IntegrityError, match="simulated stage failure"):
        offer_data.stage_store_week(
            con,
            "2026-08-17",
            "Lidl",
            [valid_offer(nazov="Nový stage"), valid_offer(nazov="Chybný stage")],
        )
    con.rollback()

    assert con.execute("SELECT nazov FROM akcie_staging").fetchall() == [("Zdravý stage",)]


def test_active_week_copy_uses_all_staged_stores_inside_callers_transaction():
    con = legacy_connection()
    migrate_akcie_schema(con)
    offer_data.migrate_offer_staging_schema(con)
    week = "2026-08-17"
    for store in ("Kaufland", "Tesco", "Lidl"):
        con.execute("BEGIN IMMEDIATE")
        offer_data.stage_store_week(
            con,
            week,
            store,
            [valid_offer(obchod=store, nazov=f"Nové {store}")],
        )
        con.commit()

    con.execute("BEGIN IMMEDIATE")
    offer_data.replace_active_week_from_staging(con, week, ("Kaufland", "Tesco", "Lidl"))
    assert con.in_transaction is True
    con.rollback()

    assert con.execute("SELECT COUNT(*) FROM akcie").fetchone()[0] == 0
