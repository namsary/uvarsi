"""Server is the authority for remembered and effective diet modes."""
import json
import sqlite3
import sys
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from test_server import (
    build_personal_plan,
    current_plan_rows,
    grant_premium,
    insert_hashed_session,
    load_server,
    model_plan,
)


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
            "INSERT INTO pouzivatelia (id, email, obchody) VALUES (?, ?, ?)",
            (user_id, email, "Lidl"),
        )
        insert_hashed_session(server, con, "diet-session", user_id)
        con.commit()
    client = TestClient(server.app)
    client.cookies.set(server.COOKIE, "diet-session")
    return client


def availability_profile(adults):
    return {
        "osoby": adults,
        "dospeli": adults,
        "deti": 0,
        "frekvencia": 2,
        "obchody": "Lidl",
        "stravovanie": "standard",
    }


def availability_rows(server):
    return tuple(
        {"offer_key": f"availability-{index}", "obchod": "Lidl"}
        for index in range(server.MIN_OFFERS_FOR_PLAN)
    )


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
    assert body["stravovanie_dostupne"] == ["standard"]


def test_me_exposes_only_diet_modes_proven_for_the_current_week(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("UVARSI_RECIPE_ENGINE", "on")
    server = load_server(monkeypatch, tmp_path, [])
    server.recipe_engine_mode.cache_clear()
    monkeypatch.setattr(
        server,
        "recipe_engine_profile_available_modes",
        lambda _con, _profile, _premium, today=None, rows=None: (
            "standard", "high_protein", "vegetarian"
        ),
    )
    client = authenticated_client(server)

    body = client.get("/api/me").json()

    assert body["stravovanie_moznosti"] == list(ALL_MODES)
    assert body["stravovanie_dostupne"] == [
        "standard", "high_protein", "vegetarian"
    ]
    assert "vegan" not in body["stravovanie_dostupne"]


def test_partial_shadow_success_does_not_hide_a_mode_the_profile_can_build(
        monkeypatch, tmp_path):
    monkeypatch.setenv("UVARSI_RECIPE_ENGINE", "on")
    server = load_server(monkeypatch, tmp_path, current_plan_rows())
    server.recipe_engine_mode.cache_clear()
    client = authenticated_client(server)
    grant_premium(server, 1)
    monkeypatch.setattr(
        server,
        "recipe_engine_shadow_status",
        lambda _con, today=None: {
            "eligible": True,
            "available_modes": ["standard"],
        },
    )

    def build_for_profile(**kwargs):
        if kwargs["mode"] != "vegetarian":
            raise server.NoCompatiblePlan("diet_too_strict", ("add_store",))
        return {"jedla": []}

    monkeypatch.setattr(
        server, "match_offers", lambda _rows, _catalog: ("matched-offer",),
        raising=False,
    )
    monkeypatch.setattr(
        server,
        "_deterministic_mode_is_feasible",
        lambda **kwargs: kwargs["mode"] == "vegetarian",
        raising=False,
    )
    monkeypatch.setattr(server, "build_deterministic_plan", build_for_profile)

    body = client.get("/api/me").json()

    assert body["stravovanie_dostupne"] == ["vegetarian"]


def test_me_checks_each_entitled_mode_once_without_building_complete_plans(
        monkeypatch, tmp_path):
    from test_deterministic_plan_invariants import WEEK, make_fixture

    fixture = make_fixture(offer_count=24, template_count=12)
    fields = (
        "tyzden", "nazov", "obchod", "cena", "povodna", "zlava",
        "jednotka", "kategoria", "source_url", "source_page",
        "valid_from", "valid_to",
    )
    fixture_rows = [
        tuple(
            WEEK if field == "tyzden"
            else "trvanlive" if field == "kategoria"
            else row.get(field)
            for field in fields
        )
        for row in fixture.rows
    ]
    monkeypatch.setenv("UVARSI_RECIPE_ENGINE", "on")
    server = load_server(monkeypatch, tmp_path, fixture_rows)
    server.recipe_engine_mode.cache_clear()
    client = authenticated_client(server)
    grant_premium(server, 1)
    with server.db() as con:
        con.execute(
            "UPDATE pouzivatelia SET obchody='Kaufland,Tesco,Lidl' WHERE id=1"
        )
        con.commit()
    checks = []
    complete_builds = []
    match_calls = []
    real_match_offers = server.match_offers
    real_feasibility_check = server._deterministic_mode_is_feasible

    def match_once(rows, catalog):
        match_calls.append(rows)
        return real_match_offers(rows, catalog)

    monkeypatch.setattr(server, "match_offers", match_once)
    monkeypatch.setattr(server, "load_recipe_catalog", lambda _catalog: fixture.recipes)

    def check_mode(**kwargs):
        checks.append(kwargs)
        return real_feasibility_check(**kwargs)

    def complete_build(**kwargs):
        complete_builds.append(kwargs)
        raise AssertionError("/api/me must not build a complete weekly plan")

    monkeypatch.setattr(
        server, "_deterministic_mode_is_feasible", check_mode,
    )
    monkeypatch.setattr(server, "build_deterministic_plan", complete_build)

    body = client.get("/api/me").json()
    repeated = client.get("/api/me").json()

    assert body["stravovanie_dostupne"] == list(ALL_MODES)
    assert repeated["stravovanie_dostupne"] == body["stravovanie_dostupne"]
    assert complete_builds == []
    assert len(match_calls) == 1
    assert {call["mode"] for call in checks} == set(ALL_MODES)
    assert len(checks) == len(ALL_MODES)
    assert all(call["stores"] == ["Kaufland", "Tesco", "Lidl"] for call in checks)
    assert all(call["offers"] for call in checks)
    assert all(call["adults"] == 4 and call["children"] == 0 for call in checks)
    assert all(call["frequency"] == 2 for call in checks)


def test_profile_mode_cache_evicts_only_the_least_recently_used_entry(
        monkeypatch, tmp_path):
    monkeypatch.setenv("UVARSI_RECIPE_ENGINE", "on")
    server = load_server(monkeypatch, tmp_path, [])
    server.recipe_engine_mode.cache_clear()
    server._PROFILE_MODE_CACHE_MAX = 2
    checks = []
    rows = availability_rows(server)
    monkeypatch.setattr(
        server, "match_offers", lambda _rows, _catalog: ("matched-offer",),
    )

    def feasible(**kwargs):
        checks.append((kwargs["adults"], kwargs["mode"]))
        return True

    monkeypatch.setattr(server, "_deterministic_mode_is_feasible", feasible)

    first = availability_profile(1)
    second = availability_profile(2)
    third = availability_profile(3)
    server.recipe_engine_profile_available_modes(None, first, True, rows=rows)
    server.recipe_engine_profile_available_modes(None, second, True, rows=rows)
    server.recipe_engine_profile_available_modes(None, first, True, rows=rows)
    server.recipe_engine_profile_available_modes(None, third, True, rows=rows)
    server.recipe_engine_profile_available_modes(None, first, True, rows=rows)

    assert len(checks) == 3 * len(ALL_MODES)


def test_concurrent_profile_requests_share_one_mode_feasibility_check(
        monkeypatch, tmp_path):
    monkeypatch.setenv("UVARSI_RECIPE_ENGINE", "on")
    server = load_server(monkeypatch, tmp_path, [])
    server.recipe_engine_mode.cache_clear()
    checks = []
    checks_lock = threading.Lock()
    start_together = threading.Barrier(8)
    yield_to_competitors = threading.Event()
    rows = availability_rows(server)
    profile_row = availability_profile(4)
    monkeypatch.setattr(
        server, "match_offers", lambda _rows, _catalog: ("matched-offer",),
    )

    def feasible(**kwargs):
        with checks_lock:
            checks.append(kwargs["mode"])
        yield_to_competitors.wait(0.03)
        return True

    monkeypatch.setattr(server, "_deterministic_mode_is_feasible", feasible)

    def available_modes():
        start_together.wait()
        return server.recipe_engine_profile_available_modes(
            None, profile_row, True, rows=rows,
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = tuple(pool.map(lambda _index: available_modes(), range(8)))

    assert results == (ALL_MODES,) * 8
    assert len(checks) == len(ALL_MODES)


def test_failed_offer_matching_releases_profile_mode_waiters(monkeypatch, tmp_path):
    monkeypatch.setenv("UVARSI_RECIPE_ENGINE", "on")
    server = load_server(monkeypatch, tmp_path, [])
    server.recipe_engine_mode.cache_clear()

    def fail_matching(_rows, _catalog):
        raise ValueError("broken offer row")

    monkeypatch.setattr(server, "match_offers", fail_matching)

    result = server.recipe_engine_profile_available_modes(
        None, availability_profile(4), True, rows=availability_rows(server),
    )

    assert result == ()
    assert server._PROFILE_MODE_PENDING == {}


def test_unavailable_saved_diet_is_not_silently_changed_to_meat_mode(
        monkeypatch, tmp_path):
    monkeypatch.setenv("UVARSI_RECIPE_ENGINE", "on")
    server = load_server(monkeypatch, tmp_path, current_plan_rows())
    server.recipe_engine_mode.cache_clear()
    client = authenticated_client(server)
    grant_premium(server, 1)
    with server.db() as con:
        con.execute("UPDATE pouzivatelia SET stravovanie='vegan' WHERE id=1")
        con.commit()
    monkeypatch.setattr(
        server,
        "recipe_engine_profile_available_modes",
        lambda _con, _profile, _premium, today=None, rows=None: (
            "standard", "vegetarian"
        ),
    )

    body = client.get("/api/me").json()

    assert body["stravovanie"] == "vegan"
    assert body["stravovanie_ulozene"] == "vegan"
    assert "vegan" not in body["stravovanie_dostupne"]


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


def _signature(server, *, mode="standard", pantry=(), pantry_driven=False):
    rows = server.akcie_pre(["Lidl"])
    return server.podpis_planu(
        server.monday(), ["Lidl"], 2, rows, pantry,
        adults=4, children=0, zo_spajze=pantry_driven, stravovanie=mode,
    )


@pytest.mark.parametrize("pantry_driven", [False, True])
def test_regular_and_pantry_personal_cache_store_and_require_the_exact_signature(
        monkeypatch, tmp_path, pantry_driven):
    server = load_server(monkeypatch, tmp_path, current_plan_rows())
    pantry = ["ryža"] if pantry_driven else []
    signature = _signature(
        server, pantry=pantry, pantry_driven=pantry_driven,
    )

    stored = server.osobny_plan_na_ulozenie(
        {"jedla": []}, pantry, pantry_driven, podpis=signature,
    )

    assert stored[server.PLAN_META_KEY]["plan_signature"] == signature
    assert server.osobna_cache_plati(stored, pantry, podpis=signature)
    assert not server.osobna_cache_plati(
        stored, pantry, podpis=f"{signature}-iny",
    )


def test_recipe_library_version_change_invalidates_personal_cache(
        monkeypatch, tmp_path):
    server = load_server(monkeypatch, tmp_path, current_plan_rows())
    client = authenticated_client(server)
    with server.db() as con:
        plan = build_personal_plan(con, model_plan(), ["Lidl"], 2, 4)
        plan["cache_marker"] = "old-library"
        old_signature = _signature(server)
        plan = server.osobny_plan_na_ulozenie(plan, podpis=old_signature)
        con.execute(
            "INSERT INTO plany (user_id, tyzden, json) VALUES (1, ?, ?)",
            (server.monday(), json.dumps(plan)),
        )
        con.commit()

    plan_data = sys.modules[server.plan_signature.__module__]
    monkeypatch.setattr(
        plan_data, "RECIPE_LIBRARY_VERSION", plan_data.RECIPE_LIBRARY_VERSION + 1,
    )
    response = client.get("/api/plan")

    assert response.status_code == 200
    assert response.json() == {
        "prazdny": True,
        "vyzaduje_akciu": True,
        "dovod": "plan_zastaral",
        "obnovit_cez": "/api/plan/generuj",
    }
    with server.db() as con:
        assert con.execute(
            "SELECT COUNT(*) FROM plany WHERE user_id=1"
        ).fetchone()[0] == 0


@pytest.mark.parametrize("stored_mode", PRO_MODES)
def test_premium_free_premium_never_reuses_standard_plan_as_stored_pro_mode(
        monkeypatch, tmp_path, stored_mode):
    server = load_server(monkeypatch, tmp_path, current_plan_rows())
    with server.db() as con:
        con.executemany(
            "INSERT INTO pouzivatelia (id, email, obchody, stravovanie) "
            "VALUES (?, ?, 'Lidl', ?)",
            [(1, "cycle@uvar.si", stored_mode), (2, "other@uvar.si", "standard")],
        )
        insert_hashed_session(server, con, "cycle-session", 1)
        insert_hashed_session(server, con, "other-session", 2)
        con.commit()
    grant_premium(server, 1)
    with server.db() as con:
        con.execute("UPDATE naroky SET stav='vrateny' WHERE user_id=1")
        standard_plan = build_personal_plan(con, model_plan(), ["Lidl"], 2, 4)
        standard_plan["cache_marker"] = "standard-while-free"
        standard_plan = server.osobny_plan_na_ulozenie(
            standard_plan, podpis=_signature(server),
        )
        con.executemany(
            "INSERT INTO plany (user_id, tyzden, json) VALUES (?, ?, ?)",
            [
                (1, server.monday(), json.dumps(standard_plan)),
                (2, server.monday(), '{"unrelated":true}'),
            ],
        )
        con.commit()
    grant_premium(server, 1, order_id="cycle-premium-restored")
    client = TestClient(server.app)
    client.cookies.set(server.COOKIE, "cycle-session")

    response = client.get("/api/plan")

    assert response.status_code == 200
    assert response.json().get("cache_marker") != "standard-while-free"
    assert response.json()["dovod"] == "plan_zastaral"
    with server.db() as con:
        assert con.execute(
            "SELECT COUNT(*) FROM plany WHERE user_id=1"
        ).fetchone()[0] == 0
        assert con.execute(
            "SELECT json FROM plany WHERE user_id=2"
        ).fetchone()[0] == '{"unrelated":true}'
        assert con.execute("SELECT COUNT(*) FROM sessions_v2").fetchone()[0] == 2


def test_legacy_personal_plan_without_signature_fails_closed_for_only_its_owner(
        monkeypatch, tmp_path):
    server = load_server(monkeypatch, tmp_path, current_plan_rows())
    with server.db() as con:
        con.executemany(
            "INSERT INTO pouzivatelia (id, email, obchody) VALUES (?, ?, 'Lidl')",
            [(1, "legacy-cache@uvar.si"), (2, "untouched@uvar.si")],
        )
        insert_hashed_session(server, con, "legacy-cache-session", 1)
        insert_hashed_session(server, con, "untouched-session", 2)
        legacy = build_personal_plan(con, model_plan(), ["Lidl"], 2, 4)
        legacy["cache_marker"] = "unsigned-legacy"
        legacy = server.osobny_plan_na_ulozenie(legacy)
        legacy[server.PLAN_META_KEY].pop("plan_signature", None)
        con.executemany(
            "INSERT INTO plany (user_id, tyzden, json) VALUES (?, ?, ?)",
            [
                (1, server.monday(), json.dumps(legacy)),
                (2, server.monday(), '{"unrelated":true}'),
            ],
        )
        con.commit()
    client = TestClient(server.app)
    client.cookies.set(server.COOKIE, "legacy-cache-session")

    response = client.get("/api/plan")

    assert response.status_code == 200
    assert response.json()["dovod"] == "plan_zastaral"
    with server.db() as con:
        assert con.execute(
            "SELECT COUNT(*) FROM plany WHERE user_id=1"
        ).fetchone()[0] == 0
        assert con.execute(
            "SELECT json FROM plany WHERE user_id=2"
        ).fetchone()[0] == '{"unrelated":true}'
        assert con.execute("SELECT COUNT(*) FROM sessions_v2").fetchone()[0] == 2
