"""Server is the authority for remembered and effective diet modes."""
import sqlite3

import pytest
from fastapi.testclient import TestClient

from test_server import grant_premium, insert_hashed_session, load_server


PRO_MODES = ("high_protein", "vegetarian", "vegan")
ALL_MODES = ("standard", *PRO_MODES)


def profile(stravovanie=None, **overrides):
    payload = {
        "adults": 2,
        "children": 1,
        "frekvencia": 2,
        "obchody": ["Lidl"],
    }
    if stravovanie is not None:
        payload["stravovanie"] = stravovanie
    payload.update(overrides)
    return payload


def authenticated_client(server, *, user_id=1, email="diet@uvar.si"):
    with server.db() as con:
        con.execute(
            "INSERT INTO pouzivatelia (id, email) VALUES (?, ?)",
            (user_id, email),
        )
        insert_hashed_session(server, con, "diet-session", user_id)
        con.commit()
    client = TestClient(server.app)
    client.cookies.set(server.COOKIE, "diet-session")
    return client


def test_additive_migration_preserves_existing_account_and_session(monkeypatch, tmp_path):
    server = load_server(monkeypatch, tmp_path, [])
    con = sqlite3.connect(":memory:")
    con.execute(
        "CREATE TABLE pouzivatelia (id INTEGER PRIMARY KEY, email TEXT NOT NULL)"
    )
    con.execute(
        "CREATE TABLE sessions_v2 (token_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL)"
    )
    con.execute("INSERT INTO pouzivatelia VALUES (7, 'stary@uvar.si')")
    con.execute("INSERT INTO sessions_v2 VALUES ('session-hash', 7)")

    server.migrate_diet_schema(con)
    server.migrate_diet_schema(con)

    row = con.execute(
        "SELECT id, email, stravovanie FROM pouzivatelia WHERE id=7"
    ).fetchone()
    session = con.execute("SELECT token_hash, user_id FROM sessions_v2").fetchone()
    assert row == (7, "stary@uvar.si", "standard")
    assert session == ("session-hash", 7)


def test_me_returns_effective_and_remembered_default_with_strict_options(
        monkeypatch, tmp_path):
    server = load_server(monkeypatch, tmp_path, [])
    client = authenticated_client(server)

    response = client.get("/api/me")

    assert response.status_code == 200
    body = response.json()
    assert body["stravovanie"] == "standard"
    assert body["stravovanie_ulozene"] == "standard"
    assert body["stravovanie_moznosti"] == list(ALL_MODES)


@pytest.mark.parametrize("mode", PRO_MODES)
def test_free_user_cannot_persist_pro_diet_mode(monkeypatch, tmp_path, mode):
    server = load_server(monkeypatch, tmp_path, [])
    client = authenticated_client(server)

    response = client.post("/api/profil", json=profile(mode, adults=5, children=0))

    assert response.status_code == 403
    assert response.json()["kod"] == "stravovanie_premium"
    with server.db() as con:
        row = con.execute(
            "SELECT dospeli, deti, stravovanie FROM pouzivatelia WHERE id=1"
        ).fetchone()
    assert tuple(row) == (4, 0, "standard"), "odmietnutie nesmie uložiť pol profilu"


@pytest.mark.parametrize("mode", [None, "", "vegan ", "Vegan", "keto", 1, True, []])
def test_profile_rejects_every_explicit_mode_outside_the_allowed_tuple(
        monkeypatch, tmp_path, mode):
    server = load_server(monkeypatch, tmp_path, [])
    client = authenticated_client(server)
    payload = profile()
    payload["stravovanie"] = mode

    response = client.post("/api/profil", json=payload)

    assert response.status_code == 422
    with server.db() as con:
        assert con.execute(
            "SELECT stravovanie FROM pouzivatelia WHERE id=1"
        ).fetchone()[0] == "standard"


@pytest.mark.parametrize("mode", ALL_MODES)
def test_premium_user_can_persist_every_allowed_mode(monkeypatch, tmp_path, mode):
    server = load_server(monkeypatch, tmp_path, [])
    client = authenticated_client(server)
    grant_premium(server, 1)

    response = client.post("/api/profil", json=profile(mode))
    me = client.get("/api/me")

    assert response.status_code == 200
    assert me.json()["stravovanie"] == mode
    assert me.json()["stravovanie_ulozene"] == mode


def test_losing_premium_suspends_but_does_not_erase_the_preference(
        monkeypatch, tmp_path):
    server = load_server(monkeypatch, tmp_path, [])
    client = authenticated_client(server)
    grant_premium(server, 1)
    assert client.post("/api/profil", json=profile("vegan")).status_code == 200

    with server.db() as con:
        con.execute("UPDATE naroky SET stav='vrateny' WHERE user_id=1")
        con.commit()

    suspended = client.get("/api/me").json()
    legacy_save = client.post("/api/profil", json=profile(adults=3, children=0))
    after_legacy_save = client.get("/api/me").json()

    assert suspended["stravovanie"] == "standard"
    assert suspended["stravovanie_ulozene"] == "vegan"
    assert legacy_save.status_code == 200
    assert after_legacy_save["stravovanie"] == "standard"
    assert after_legacy_save["stravovanie_ulozene"] == "vegan"

    grant_premium(server, 1, order_id="diet-restored")
    restored = client.get("/api/me").json()
    assert restored["stravovanie"] == "vegan"
    assert restored["stravovanie_ulozene"] == "vegan"


def test_explicit_standard_is_the_only_diet_change_free_user_may_save(
        monkeypatch, tmp_path):
    server = load_server(monkeypatch, tmp_path, [])
    client = authenticated_client(server)
    grant_premium(server, 1)
    assert client.post("/api/profil", json=profile("vegetarian")).status_code == 200
    with server.db() as con:
        con.execute("UPDATE naroky SET stav='vrateny' WHERE user_id=1")
        con.commit()

    response = client.post("/api/profil", json=profile("standard"))

    assert response.status_code == 200
    me = client.get("/api/me").json()
    assert me["stravovanie"] == "standard"
    assert me["stravovanie_ulozene"] == "standard"


def test_changing_diet_mode_invalidates_only_the_users_current_plan(
        monkeypatch, tmp_path):
    server = load_server(monkeypatch, tmp_path, [])
    client = authenticated_client(server)
    grant_premium(server, 1)
    with server.db() as con:
        con.execute(
            "INSERT INTO pouzivatelia (id, email) VALUES (2, 'other@uvar.si')"
        )
        con.executemany(
            "INSERT INTO plany (user_id, tyzden, json) VALUES (?, ?, ?)",
            [(1, server.monday(), '{}'), (2, server.monday(), '{}')],
        )
        con.commit()

    response = client.post("/api/profil", json=profile("high_protein"))

    assert response.status_code == 200
    with server.db() as con:
        remaining = con.execute(
            "SELECT user_id FROM plany WHERE tyzden=? ORDER BY user_id",
            (server.monday(),),
        ).fetchall()
    assert [row[0] for row in remaining] == [2]
