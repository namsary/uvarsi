import importlib
import json
import sqlite3
import sys
import types
from datetime import date, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.landing_data import write_landing_data_atomic
from app.plan_data import build_personal_plan
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
        """INSERT INTO akcie
           (tyzden, nazov, obchod, cena, povodna, zlava, jednotka, kategoria,
            source_url, source_page, valid_from, valid_to)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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


def current_plan_rows(count=16):
    today = date.today()
    week = current_monday(today)
    return [
        (week, f"Ponuka {index}", "Lidl", 1.0 + index / 100, 2.0 + index / 100,
         "-50 %", "1 ks", "trvanlive", f"https://example.test/{index}.jpg", index,
         (today - timedelta(days=1)).isoformat(), (today + timedelta(days=1)).isoformat())
        for index in range(1, count + 1)
    ]


def model_plan(first_offer_id=1):
    return {
        "meals": [
            {"day": "PO", "name": "Prvé jedlo", "instructions": ["Uvar prvé jedlo."],
             "items": [{"offer_id": first_offer_id, "quantity": 2}], "pantry_ingredients": ["soľ"]},
            {"day": "ST", "name": "Druhé jedlo", "instructions": ["Uvar druhé jedlo."],
             "items": [{"offer_id": 2, "quantity": 1}]},
            {"day": "PI", "name": "Tretie jedlo", "instructions": ["Uvar tretie jedlo."],
             "items": [{"offer_id": 3, "quantity": 1}]},
        ]
    }


def fake_anthropic(model_output, constructors):
    class Messages:
        def create(self, **kwargs):
            return types.SimpleNamespace(content=[types.SimpleNamespace(type="text", text=json.dumps(model_output))])

    class Anthropic:
        def __init__(self, **kwargs):
            constructors.append(kwargs)
            self.messages = Messages()

    return types.SimpleNamespace(Anthropic=Anthropic)


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


def test_material_profile_change_invalidates_only_that_users_current_cached_plan(monkeypatch, tmp_path):
    server = load_server(monkeypatch, tmp_path, [])
    week = current_monday()
    with server.db() as con:
        con.executemany(
            "INSERT INTO pouzivatelia (id, email, osoby, frekvencia, obchody) VALUES (?, ?, ?, ?, ?)",
            [(1, "first@uvar.si", 4, 2, "Lidl"), (2, "second@uvar.si", 4, 2, "Lidl")],
        )
        con.execute("INSERT INTO sedenia (token, user_id) VALUES ('first-session', 1)")
        con.executemany("INSERT INTO plany (user_id, tyzden, json) VALUES (?, ?, ?)", [
            (1, week, '{"cached":"first"}'), (2, week, '{"cached":"second"}'),
        ])
        con.commit()
    client = TestClient(server.app)
    client.cookies.set(server.COOKIE, "first-session")

    response = client.post("/api/profil", json={"osoby": 5, "frekvencia": 2, "obchody": ["Lidl"]})

    assert response.status_code == 200
    with server.db() as con:
        rows = con.execute("SELECT user_id FROM plany WHERE tyzden=? ORDER BY user_id", (week,)).fetchall()
        assert [row[0] for row in rows] == [2]


def test_material_pantry_change_invalidates_only_that_users_current_cached_plan(monkeypatch, tmp_path):
    server = load_server(monkeypatch, tmp_path, [])
    week = current_monday()
    with server.db() as con:
        con.executemany("INSERT INTO pouzivatelia (id, email) VALUES (?, ?)", [(1, "first@uvar.si"), (2, "second@uvar.si")])
        con.execute("INSERT INTO sedenia (token, user_id) VALUES ('first-session', 1)")
        con.executemany("INSERT INTO plany (user_id, tyzden, json) VALUES (?, ?, ?)", [
            (1, week, '{"cached":"first"}'), (2, week, '{"cached":"second"}'),
        ])
        con.commit()
    client = TestClient(server.app)
    client.cookies.set(server.COOKIE, "first-session")

    response = client.post("/api/spajza", json={"polozky": ["soľ"]})

    assert response.status_code == 200
    with server.db() as con:
        rows = con.execute("SELECT user_id FROM plany WHERE tyzden=? ORDER BY user_id", (week,)).fetchall()
        assert [row[0] for row in rows] == [2]


def test_plan_route_persists_only_reconstructed_server_commerce(monkeypatch, tmp_path):
    server = load_server(monkeypatch, tmp_path, current_plan_rows())
    with server.db() as con:
        con.execute("INSERT INTO pouzivatelia (id, email, obchody) VALUES (1, 'test@uvar.si', 'Lidl')")
        con.execute("INSERT INTO sedenia (token, user_id) VALUES ('session-token', 1)")
        con.execute("INSERT INTO spajza (user_id, nazov) VALUES (1, 'soľ')")
        con.commit()
    constructors = []
    monkeypatch.setitem(sys.modules, "anthropic", fake_anthropic(model_plan(), constructors))
    client = TestClient(server.app)
    client.cookies.set(server.COOKIE, "session-token")

    response = client.post("/api/plan/generuj?force=1")

    assert response.status_code == 200
    payload = response.json()
    assert payload["jedla"][0]["suroviny"] == [
        {"offer_id": 1, "nazov": "Ponuka 1", "obchod": "Lidl", "jednotka": "1 ks",
         "mnozstvo": 2, "cena": "2,02", "povodna": "4,02", "zlava": "-50 %"},
        {"spajza": "soľ"},
    ]
    assert payload["nakupny_zoznam"][0]["polozky"][0]["nazov"] == "Ponuka 1"
    assert constructors
    with server.db() as con:
        assert json.loads(con.execute("SELECT json FROM plany WHERE user_id=1").fetchone()[0]) == payload


def test_invalid_model_plan_does_not_replace_existing_valid_cache(monkeypatch, tmp_path):
    server = load_server(monkeypatch, tmp_path, current_plan_rows())
    with server.db() as con:
        con.execute("INSERT INTO pouzivatelia (id, email, obchody) VALUES (1, 'test@uvar.si', 'Lidl')")
        con.execute("INSERT INTO sedenia (token, user_id) VALUES ('session-token', 1)")
        con.execute("INSERT INTO spajza (user_id, nazov) VALUES (1, 'soľ')")
        current = build_personal_plan(con, model_plan(), ["Lidl"], 2, pantry=["soľ"])
        con.execute("INSERT INTO plany (user_id, tyzden, json) VALUES (?, ?, ?)", (1, current_monday(), json.dumps(current)))
        con.commit()
    constructors = []
    monkeypatch.setitem(sys.modules, "anthropic", fake_anthropic(model_plan(first_offer_id=999), constructors))
    client = TestClient(server.app)
    client.cookies.set(server.COOKIE, "session-token")

    response = client.post("/api/plan/generuj?force=1")

    assert response.status_code == 500
    with server.db() as con:
        assert json.loads(con.execute("SELECT json FROM plany WHERE user_id=1").fetchone()[0]) == current


@pytest.mark.parametrize("method, path", [("get", "/api/plan"), ("post", "/api/plan/generuj")])
def test_cached_plan_is_503_when_one_selected_offer_is_no_longer_current(monkeypatch, tmp_path, method, path):
    server = load_server(monkeypatch, tmp_path, current_plan_rows())
    with server.db() as con:
        con.execute("INSERT INTO pouzivatelia (id, email, obchody) VALUES (1, 'test@uvar.si', 'Lidl')")
        con.execute("INSERT INTO sedenia (token, user_id) VALUES ('session-token', 1)")
        con.execute("INSERT INTO spajza (user_id, nazov) VALUES (1, 'soľ')")
        cached = build_personal_plan(con, model_plan(), ["Lidl"], 2, pantry=["soľ"])
        con.execute("INSERT INTO plany (user_id, tyzden, json) VALUES (?, ?, ?)", (1, current_monday(), json.dumps(cached)))
        con.execute("DELETE FROM akcie WHERE rowid=1")
        con.commit()
    constructors = []
    monkeypatch.setitem(sys.modules, "anthropic", fake_anthropic(model_plan(), constructors))
    client = TestClient(server.app)
    client.cookies.set(server.COOKIE, "session-token")

    response = getattr(client, method)(path)

    assert response.status_code == 503
    assert constructors == []
