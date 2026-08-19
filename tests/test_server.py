import importlib
import sqlite3
import sys
import types
from datetime import date, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.landing_data import write_landing_data_atomic
from app.weekly_data import current_monday


ROOT = Path(__file__).resolve().parents[1]


def load_server(monkeypatch, tmp_path, rows, landing_data=None):
    database = tmp_path / "uvarsi.db"
    con = sqlite3.connect(database)
    con.execute(
        """CREATE TABLE akcie (
            tyzden TEXT, nazov TEXT, obchod TEXT, cena REAL, povodna REAL,
            zlava TEXT, jednotka TEXT, kategoria TEXT, source_url TEXT,
            source_page INTEGER, valid_from TEXT, valid_to TEXT
        )"""
    )
    con.executemany(
        "INSERT INTO akcie VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [tuple(row) + (None,) * (12 - len(row)) for row in rows],
    )
    con.commit()
    con.close()

    monkeypatch.setenv("UVARSI_DB", str(database))
    monkeypatch.setenv("UVARSI_URL", "https://uvar.si")
    if landing_data is not None:
        landing_path = tmp_path / "landing_data.json"
        write_landing_data_atomic(landing_path, landing_data)
        monkeypatch.setenv("UVARSI_LANDING_DATA", str(landing_path))
    monkeypatch.syspath_prepend(str(ROOT / "app"))
    sys.modules.pop("server", None)
    return importlib.import_module("server")


def landing_payload(week=None):
    return {
        "schema_version": 1,
        "generated_at": "2026-08-18T05:02:20+02:00",
        "week": week or current_monday(),
        "week_label": "17.–23. 8. 2026",
        "sources": [],
        "receipt": {
            "meals": [{"day": "PO", "name": "Test", "items": []}],
            "nakup_spolu": "1,00",
            "bezne": "2,00",
            "usetris": "1,00",
        },
    }


def test_akcie_pre_never_returns_previous_week_prices(monkeypatch, tmp_path):
    server = load_server(
        monkeypatch,
        tmp_path,
        [("2026-08-10", "Mlieko", "Lidl", 1.0, 1.5, "-33 %", "1 l", "mliecne")],
    )
    monkeypatch.setattr(server, "monday", lambda: "2026-08-17")

    assert server.akcie_pre(["Lidl"]) == []


def test_akcie_pre_delegates_selection_to_current_week_helper(monkeypatch, tmp_path):
    server = load_server(monkeypatch, tmp_path, [])
    calls = []

    def current_week_only(connection, stores, today):
        calls.append((connection, stores, today))
        return []

    monkeypatch.setattr(server, "offers_for_current_week", current_week_only, raising=False)
    server.akcie_pre(["Lidl"])

    assert calls[0][1] == ["Lidl"]
    assert calls[0][2] == date.today()


def test_plan_is_503_when_only_previous_week_exists(monkeypatch, tmp_path):
    server = load_server(
        monkeypatch,
        tmp_path,
        [("2026-08-10", "Mlieko", "Lidl", 1.0, 1.5, "-33 %", "1 l", "mliecne")],
    )
    with server.db() as con:
        con.execute("INSERT INTO pouzivatelia (id, email, obchody) VALUES (1, 'test@uvar.si', 'Lidl')")
        con.execute("INSERT INTO sedenia (token, user_id) VALUES ('session-token', 1)")
        con.commit()

    constructors = []

    class ForbiddenAnthropic:
        def __init__(self, *args, **kwargs):
            constructors.append((args, kwargs))

    monkeypatch.setitem(sys.modules, "anthropic", types.SimpleNamespace(Anthropic=ForbiddenAnthropic))
    client = TestClient(server.app)
    client.cookies.set(server.COOKIE, "session-token")
    response = client.post("/api/plan/generuj?force=1")

    assert response.status_code == 503
    assert response.json()["detail"] == "Aktuálne letákové dáta sa obnovujú. Skús to o chvíľu."
    assert constructors == []


def test_plan_is_503_without_constructing_anthropic_when_current_week_offers_are_expired(monkeypatch, tmp_path):
    today = date.today()
    week = current_monday(today)
    server = load_server(
        monkeypatch,
        tmp_path,
        [(week, "Mlieko", "Lidl", 1.0, 1.5, "-33 %", "1 l", "mliecne",
          "https://example.test/lidl.jpg", 1, (today - timedelta(days=7)).isoformat(),
          (today - timedelta(days=1)).isoformat())],
    )
    with server.db() as con:
        con.execute("INSERT INTO pouzivatelia (id, email, obchody) VALUES (1, 'test@uvar.si', 'Lidl')")
        con.execute("INSERT INTO sedenia (token, user_id) VALUES ('session-token', 1)")
        con.commit()

    constructors = []

    class ForbiddenAnthropic:
        def __init__(self, *args, **kwargs):
            constructors.append((args, kwargs))

    monkeypatch.setitem(sys.modules, "anthropic", types.SimpleNamespace(Anthropic=ForbiddenAnthropic))
    client = TestClient(server.app)
    client.cookies.set(server.COOKIE, "session-token")

    response = client.post("/api/plan/generuj?force=1")

    assert response.status_code == 503
    assert constructors == []


@pytest.mark.parametrize("offer_kind", ["expired", "legacy"])
def test_cached_plan_is_503_when_current_week_has_no_verified_offers(monkeypatch, tmp_path, offer_kind):
    today = date.today()
    week = current_monday(today)
    if offer_kind == "expired":
        rows = [
            (week, "Mlieko", "Lidl", 1.0, 1.5, "-33 %", "1 l", "mliecne",
             "https://example.test/lidl.jpg", 1, (today - timedelta(days=7)).isoformat(),
             (today - timedelta(days=1)).isoformat()),
        ]
    else:
        rows = [(week, "Mlieko", "Lidl", 1.0, 1.5, "-33 %", "1 l", "mliecne")]

    server = load_server(monkeypatch, tmp_path, rows)
    with server.db() as con:
        con.execute("INSERT INTO pouzivatelia (id, email, obchody) VALUES (1, 'test@uvar.si', 'Lidl')")
        con.execute("INSERT INTO sedenia (token, user_id) VALUES ('session-token', 1)")
        con.execute(
            "INSERT INTO plany (user_id, tyzden, json) VALUES (?, ?, ?)",
            (1, week, '{"cached": true}'),
        )
        con.commit()

    constructors = []

    class ForbiddenAnthropic:
        def __init__(self, *args, **kwargs):
            constructors.append((args, kwargs))

    monkeypatch.setitem(sys.modules, "anthropic", types.SimpleNamespace(Anthropic=ForbiddenAnthropic))
    client = TestClient(server.app)
    client.cookies.set(server.COOKIE, "session-token")

    response = client.post("/api/plan/generuj")

    assert response.status_code == 503
    assert constructors == []


def test_offer_count_includes_only_current_verified_offers(monkeypatch, tmp_path):
    today = date.today()
    week = current_monday(today)
    rows = [
        (week, "Overené mlieko", "Lidl", 1.0, 1.5, "-33 %", "1 l", "mliecne",
         "https://example.test/current.jpg", 1, (today - timedelta(days=1)).isoformat(),
         (today + timedelta(days=1)).isoformat()),
        (week, "Expirované mlieko", "Lidl", 1.0, 1.5, "-33 %", "1 l", "mliecne",
         "https://example.test/expired.jpg", 2, (today - timedelta(days=8)).isoformat(),
         (today - timedelta(days=1)).isoformat()),
        (week, "Legacy mlieko", "Lidl", 1.0, 1.5, "-33 %", "1 l", "mliecne"),
    ]
    server = load_server(monkeypatch, tmp_path, rows)

    response = TestClient(server.app).get("/api/akcie/pocet")

    assert response.status_code == 200
    assert response.json() == {"tyzden": week, "pocet": 1}


def test_public_landing_serves_only_valid_current_data(monkeypatch, tmp_path):
    server = load_server(monkeypatch, tmp_path, [], landing_payload())

    response = TestClient(server.app).get("/api/public/landing")

    assert response.status_code == 200
    assert response.json()["week"] == current_monday()


def test_public_landing_is_503_for_stale_data(monkeypatch, tmp_path):
    server = load_server(monkeypatch, tmp_path, [], landing_payload("2026-08-10"))

    response = TestClient(server.app).get("/api/public/landing")

    assert response.status_code == 503
    assert response.json()["detail"] == "Aktuálne letákové dáta sa obnovujú."
