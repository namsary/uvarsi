"""Additive SQLite migration coverage for quantified pantry rows."""

import importlib
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def server(monkeypatch, tmp_path):
    monkeypatch.setenv("UVARSI_DB", str(tmp_path / "uvarsi.db"))
    monkeypatch.setenv("UVARSI_URL", "https://uvar.si")
    monkeypatch.syspath_prepend(str(ROOT / "app"))
    sys.modules.pop("server", None)
    module = importlib.import_module("server")
    yield module
    sys.modules.pop("server", None)


@pytest.fixture
def connection():
    with closing(sqlite3.connect(":memory:")) as con:
        con.row_factory = sqlite3.Row
        con.execute(
            """CREATE TABLE spajza (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                nazov TEXT NOT NULL
            )"""
        )
        yield con


def test_migration_preserves_legacy_pantry_rows(server, connection):
    connection.execute("INSERT INTO spajza(user_id,nazov) VALUES(1,'ryža')")

    server.migrate_pantry_schema(connection)

    row = connection.execute(
        "SELECT nazov,mnozstvo,jednotka FROM spajza WHERE user_id=1"
    ).fetchone()
    assert tuple(row) == ("ryža", None, None)


def test_migration_adds_nullable_quantity_columns(server, connection):
    server.migrate_pantry_schema(connection)

    columns = {
        row["name"]: row
        for row in connection.execute("PRAGMA table_info(spajza)").fetchall()
    }
    assert columns["mnozstvo"]["type"] == "REAL"
    assert columns["mnozstvo"]["notnull"] == 0
    assert columns["jednotka"]["type"] == "TEXT"
    assert columns["jednotka"]["notnull"] == 0


def test_migration_is_idempotent(server, connection):
    server.migrate_pantry_schema(connection)
    server.migrate_pantry_schema(connection)

    column_names = [
        row["name"]
        for row in connection.execute("PRAGMA table_info(spajza)").fetchall()
    ]
    assert column_names.count("mnozstvo") == 1
    assert column_names.count("jednotka") == 1


@pytest.mark.parametrize(
    ("amount", "unit"),
    [(250, "g"), (500.5, "ml"), (2, "piece")],
)
def test_migration_accepts_supported_quantities(server, connection, amount, unit):
    server.migrate_pantry_schema(connection)

    connection.execute(
        "INSERT INTO spajza(user_id,nazov,mnozstvo,jednotka) VALUES(1,'ryža',?,?)",
        (amount, unit),
    )


@pytest.mark.parametrize(
    ("amount", "unit"),
    [
        (None, "g"),
        (1, None),
        (0, "g"),
        (-1, "g"),
        (1, "kg"),
        (1, "G"),
        ("veľa", "g"),
    ],
)
def test_migration_rejects_invalid_quantities(server, connection, amount, unit):
    server.migrate_pantry_schema(connection)

    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "INSERT INTO spajza(user_id,nazov,mnozstvo,jednotka) "
            "VALUES(1,'ryža',?,?)",
            (amount, unit),
        )


def test_migration_rejects_invalid_quantity_updates(server, connection):
    server.migrate_pantry_schema(connection)
    connection.execute(
        "INSERT INTO spajza(user_id,nazov,mnozstvo,jednotka) "
        "VALUES(1,'ryža',250,'g')"
    )

    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "UPDATE spajza SET jednotka=NULL WHERE user_id=1"
        )
