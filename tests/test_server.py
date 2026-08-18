import importlib
import sqlite3
import sys
from pathlib import Path

import pytest
from fastapi import HTTPException


def load_server(monkeypatch, tmp_path, rows):
    database = tmp_path / "uvarsi.db"
    con = sqlite3.connect(database)
    con.execute(
        """CREATE TABLE akcie (
            tyzden TEXT, nazov TEXT, obchod TEXT, cena REAL, povodna REAL,
            zlava TEXT, jednotka TEXT, kategoria TEXT
        )"""
    )
    con.executemany("INSERT INTO akcie VALUES (?, ?, ?, ?, ?, ?, ?, ?)", rows)
    con.commit()
    con.close()

    monkeypatch.setenv("UVARSI_DB", str(database))
    monkeypatch.setenv("UVARSI_URL", "https://uvar.si")
    monkeypatch.syspath_prepend(str(Path("app").resolve()))
    sys.modules.pop("server", None)
    return importlib.import_module("server")


def test_akcie_pre_never_returns_previous_week_prices(monkeypatch, tmp_path):
    server = load_server(
        monkeypatch,
        tmp_path,
        [("2026-08-10", "Mlieko", "Lidl", 1.0, 1.5, "-33 %", "1 l", "mliecne")],
    )
    monkeypatch.setattr(server, "monday", lambda: "2026-08-17")

    assert server.akcie_pre(["Lidl"]) == []


def test_plan_is_503_when_only_previous_week_exists(monkeypatch, tmp_path):
    server = load_server(
        monkeypatch,
        tmp_path,
        [("2026-08-10", "Mlieko", "Lidl", 1.0, 1.5, "-33 %", "1 l", "mliecne")],
    )
    monkeypatch.setattr(server, "monday", lambda: "2026-08-17")
    monkeypatch.setattr(
        server,
        "require_user",
        lambda _: {"id": 1, "obchody": "Lidl", "osoby": 2, "frekvencia": 2},
    )

    with pytest.raises(HTTPException) as error:
        server.generuj_plan(None, force=1)

    assert error.value.status_code == 503
    assert error.value.detail == "Aktuálne letákové dáta sa obnovujú. Skús to o chvíľu."
