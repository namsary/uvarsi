"""Fail-closed health and local synthetic smoke for the recipe rollout."""

from __future__ import annotations

from contextlib import closing
from datetime import date, datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import types

from fastapi.testclient import TestClient

from tests.test_recipe_engine_shadow import _offer_rows
from tests.test_server import load_server


MODES = ("standard", "high_protein", "vegetarian", "vegan")


def _load(monkeypatch, tmp_path, *, mode="on", rows=None):
    state = tmp_path / "recipe-engine-smoke.json"
    monkeypatch.setenv("UVARSI_RECIPE_ENGINE", mode)
    monkeypatch.setenv("UVARSI_RECIPE_SMOKE_STATE", str(state))
    server = load_server(monkeypatch, tmp_path, _offer_rows() if rows is None else rows)
    # Production is already migrated before the standalone guardian starts.
    # The smoke itself must never use this writable migration path.
    with closing(server.db()):
        pass
    server.recipe_engine_mode.cache_clear()
    return server, state


def _passing_smoke(server, **changes):
    payload = {
        "schema_version": 1,
        "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "week": server.monday(),
        "release": server.release_id(),
        "engine_mode": "on",
        "ok": True,
        "http_status": 200,
        "latency_ms": 125.0,
        "jobs_delta": 0,
        "ai_costs_delta": 0,
        "payments_enabled": False,
        "plan_engine": "deterministic",
        "auth_scope": "server_local",
        "blockers": [],
    }
    payload.update(changes)
    return payload


def _write(path, payload):
    path.write_text(json.dumps(payload), encoding="utf-8")


def _all_table_counts(server):
    with closing(sqlite3.connect(server.DB)) as con:
        tables = [
            row[0]
            for row in con.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        return {
            table: con.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
            for table in tables
        }


def test_on_health_exposes_typed_recipe_engine_readiness(monkeypatch, tmp_path):
    server, state = _load(monkeypatch, tmp_path)
    _write(state, _passing_smoke(server))

    payload = TestClient(server.app).get("/api/health").json()["recipe_engine"]

    assert payload["mode"] == "on"
    assert type(payload["library_version"]) is int
    assert type(payload["active_templates"]) is int
    assert payload["active_templates"] >= 60
    assert set(payload["coverage"]) == set(MODES)
    assert all(type(payload["coverage"][mode]) is int for mode in MODES)
    assert payload["last_shadow"] is None or type(payload["last_shadow"]) is dict
    assert payload["p95_ms"] is None or type(payload["p95_ms"]) in (int, float)
    assert payload["ready"] is True
    assert payload["blockers"] == []


def test_on_health_fails_closed_without_a_current_passing_smoke(monkeypatch, tmp_path):
    server, state = _load(monkeypatch, tmp_path)

    missing = TestClient(server.app).get("/api/health").json()["recipe_engine"]
    assert missing["ready"] is False
    assert missing["blockers"] == ["smoke_missing"]

    _write(state, _passing_smoke(server, jobs_delta=True))
    malformed = TestClient(server.app).get("/api/health").json()["recipe_engine"]
    assert malformed["ready"] is False
    assert malformed["blockers"] == ["smoke_invalid"]

    _write(state, _passing_smoke(server, ok=False, payments_enabled=True,
                                  blockers=["payments_enabled"]))
    failed = TestClient(server.app).get("/api/health").json()["recipe_engine"]
    assert failed["ready"] is False
    assert "smoke_failed" in failed["blockers"]
    assert "@" not in json.dumps(failed)
    assert "token" not in json.dumps(failed).casefold()


def test_smoke_allows_two_full_routes_over_two_seconds_but_still_caps_at_five(
    monkeypatch, tmp_path
):
    server, state = _load(monkeypatch, tmp_path)

    _write(state, _passing_smoke(server, latency_ms=2_500.0))
    _payload, blocker = server._load_recipe_smoke_state(state)
    assert blocker is None

    _write(state, _passing_smoke(server, latency_ms=5_000.0))
    _payload, blocker = server._load_recipe_smoke_state(state)
    assert blocker == "smoke_failed"


def test_authenticated_me_exposes_only_the_validated_public_engine_mode(
    monkeypatch, tmp_path
):
    server, _state = _load(monkeypatch, tmp_path, mode="shadow")
    with closing(server.db()) as con:
        con.execute("INSERT INTO pouzivatelia (id,email) VALUES (1,'me@example.test')")
        from tests.test_server import insert_hashed_session
        insert_hashed_session(server, con, "me-session", 1)
        con.commit()
    client = TestClient(server.app)
    client.cookies.set(server.COOKIE, "me-session")

    payload = client.get("/api/me").json()

    assert payload["recipe_engine"] == "shadow"
    assert set(value for value in payload.values() if value in ("off", "shadow", "on")) == {
        "shadow"
    }
    assert "UVARSI_RECIPE_ENGINE" not in json.dumps(payload)


def test_health_reports_catalog_failure_instead_of_returning_500(monkeypatch, tmp_path):
    server, state = _load(monkeypatch, tmp_path)
    _write(state, _passing_smoke(server))
    monkeypatch.setattr(
        server, "load_recipe_catalog",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError("broken catalog")),
    )

    response = TestClient(server.app).get("/api/health")

    assert response.status_code == 200
    engine = response.json()["recipe_engine"]
    assert engine["ready"] is False
    assert engine["library_version"] is None
    assert engine["active_templates"] == 0
    assert engine["coverage"] == {mode: 0 for mode in MODES}
    assert engine["blockers"] == ["catalog_load_failed"]
    assert "broken catalog" not in response.text


def test_health_stays_ready_with_enough_current_offers_when_one_store_is_missing(
    monkeypatch, tmp_path
):
    rows = [row for row in _offer_rows() if row[2] != "Lidl"]
    server, state = _load(monkeypatch, tmp_path, rows=rows)
    _write(state, _passing_smoke(server))

    engine = TestClient(server.app).get("/api/health").json()["recipe_engine"]

    assert engine["ready"] is True
    assert "incomplete_offers" not in engine["blockers"]


def test_synthetic_smoke_uses_available_stores_when_one_flyer_is_missing(
    monkeypatch, tmp_path
):
    rows = [row for row in _offer_rows() if row[2] != "Lidl"]
    server, state = _load(monkeypatch, tmp_path, rows=rows)

    result = server.run_recipe_engine_synthetic_smoke(state_path=state)

    assert result["ok"] is True, json.dumps(result, sort_keys=True)
    assert result["plan_engine"] == "deterministic"
    assert result["blockers"] == []


def test_health_and_smoke_still_fail_closed_below_safe_offer_minimum(
    monkeypatch, tmp_path
):
    rows = _offer_rows()[:14]
    server, state = _load(monkeypatch, tmp_path, rows=rows)
    _write(state, _passing_smoke(server))

    engine = TestClient(server.app).get("/api/health").json()["recipe_engine"]
    smoke = server.run_recipe_engine_synthetic_smoke(state_path=state)

    assert engine["ready"] is False
    assert "incomplete_offers" in engine["blockers"]
    assert smoke["ok"] is False
    assert smoke["blockers"] == ["incomplete_offers"]


def test_shadow_health_requires_fresh_activation_evidence_but_not_on_smoke(
    monkeypatch, tmp_path
):
    server, _state = _load(monkeypatch, tmp_path, mode="shadow")

    engine = TestClient(server.app).get("/api/health").json()["recipe_engine"]

    assert engine["ready"] is False
    assert "shadow_not_ready" in engine["blockers"]
    assert "smoke_missing" not in engine["blockers"]


def test_off_health_does_not_require_shadow_or_synthetic_smoke(monkeypatch, tmp_path):
    server, _state = _load(monkeypatch, tmp_path, mode="off")

    engine = TestClient(server.app).get("/api/health").json()["recipe_engine"]

    assert engine["ready"] is True
    assert engine["blockers"] == []


def test_on_health_ignores_idle_legacy_worker_when_engine_guarantees_pass(
    monkeypatch, tmp_path
):
    server, state = _load(monkeypatch, tmp_path)
    _write(state, _passing_smoke(server))
    monkeypatch.setattr(
        server.plan_jobs,
        "health",
        lambda *_args, **_kwargs: {
            "queued": 0,
            "oldest_seconds": None,
            "worker_alive": False,
            "heartbeat_seconds": None,
            "heartbeat_at": None,
            "last_ready": None,
            "failed": 0,
            "blocking_code": "worker_heartbeat_stale",
        },
    )

    payload = TestClient(server.app).get("/api/health").json()

    assert payload["plan_queue"]["worker_alive"] is False
    assert payload["recipe_engine"]["ready"] is True


def test_local_synthetic_smoke_is_non_public_read_only_and_model_free(
    monkeypatch, tmp_path
):
    server, state = _load(monkeypatch, tmp_path)
    production_db = Path(server.DB)
    production_digest = hashlib.sha256(production_db.read_bytes()).hexdigest()
    observed = []

    @server.app.middleware("http")
    async def observe_authenticated_plan_route(request, call_next):
        if request.url.path == "/api/plan/generuj":
            isolated_db = Path(server.DB)
            with closing(sqlite3.connect(isolated_db)) as con:
                observed.append(
                    {
                        "method": request.method,
                        "database": isolated_db,
                        "mode": isolated_db.stat().st_mode & 0o777,
                        "has_session": bool(request.cookies.get(server.COOKIE)),
                        "users": con.execute(
                            "SELECT COUNT(*) FROM pouzivatelia"
                        ).fetchone()[0],
                        "sessions": con.execute(
                            "SELECT COUNT(*) FROM sessions_v2"
                        ).fetchone()[0],
                        "offers": con.execute("SELECT COUNT(*) FROM akcie").fetchone()[0],
                    }
                )
        return await call_next(request)

    class ForbiddenClient:
        def __init__(self, *_args, **_kwargs):
            raise AssertionError("synthetic smoke constructed a model client")

    poison = types.SimpleNamespace(
        Anthropic=ForbiddenClient,
        AsyncAnthropic=ForbiddenClient,
        OpenAI=ForbiddenClient,
        AsyncOpenAI=ForbiddenClient,
        Client=ForbiddenClient,
    )
    monkeypatch.setitem(sys.modules, "anthropic", poison)
    monkeypatch.setitem(sys.modules, "openai", poison)
    before = _all_table_counts(server)
    original_prepare = server.priprav_databazu

    def isolated_prepare_only(path=None):
        target = Path(path or server.DB)
        if target == production_db:
            raise AssertionError("synthetic smoke attempted a live database migration")
        return original_prepare(path)

    monkeypatch.setattr(server, "priprav_databazu", isolated_prepare_only)

    result = server.run_recipe_engine_synthetic_smoke(state_path=state)

    assert result["ok"] is True, json.dumps(result, sort_keys=True)
    assert result["http_status"] == 200
    assert result["latency_ms"] < 2_000
    assert result["jobs_delta"] == result["ai_costs_delta"] == 0
    assert result["payments_enabled"] is False
    assert result["plan_engine"] == "deterministic"
    assert result["auth_scope"] == "server_local"
    assert len(observed) == 2
    assert all(item["method"] == "POST" for item in observed)
    assert all(item["has_session"] is True for item in observed)
    assert all(item["database"] != production_db for item in observed)
    assert all(item["users"] == item["sessions"] == 1 for item in observed)
    assert all(item["offers"] >= server.MIN_OFFERS_FOR_PLAN for item in observed)
    assert observed[0]["database"] == observed[1]["database"]
    if os.name != "nt":
        assert all(item["mode"] == 0o600 for item in observed)
    isolated_db = observed[0]["database"]
    assert not isolated_db.exists()
    after = _all_table_counts(server)
    assert after == before
    assert hashlib.sha256(production_db.read_bytes()).hexdigest() == production_digest
    durable = state.read_text(encoding="utf-8")
    assert "@" not in durable
    assert "token" not in durable.casefold()
    assert "jedla" not in durable


def test_synthetic_smoke_fails_when_temporary_database_cannot_be_deleted(
    monkeypatch, tmp_path
):
    server, state = _load(monkeypatch, tmp_path)
    original_unlink = server.Path.unlink
    refused = []

    def refuse_smoke_cleanup(path, *args, **kwargs):
        if path.name.startswith("uvarsi-recipe-smoke-"):
            refused.append(path)
            raise OSError("cleanup refused")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(server.Path, "unlink", refuse_smoke_cleanup)

    result = server.run_recipe_engine_synthetic_smoke(state_path=state)

    for path in refused:
        original_unlink(path, missing_ok=True)

    assert result["ok"] is False
    assert result["http_status"] == 503
    assert result["blockers"] == ["internal_error"]


def test_synthetic_smoke_fails_closed_when_payments_are_enabled(monkeypatch, tmp_path):
    server, state = _load(monkeypatch, tmp_path)
    monkeypatch.setenv("PLATBY_ZAPNUTE", "1")

    result = server.run_recipe_engine_synthetic_smoke(state_path=state)

    assert result["ok"] is False
    assert result["blockers"] == ["payments_enabled"]
    assert result["payments_enabled"] is True
    assert result["jobs_delta"] == result["ai_costs_delta"] == 0


def test_on_health_rechecks_current_payment_switch_not_only_old_smoke(
    monkeypatch, tmp_path
):
    server, state = _load(monkeypatch, tmp_path)
    _write(state, _passing_smoke(server))
    monkeypatch.setenv("PLATBY_ZAPNUTE", "1")

    engine = TestClient(server.app).get("/api/health").json()["recipe_engine"]

    assert engine["ready"] is False
    assert engine["blockers"] == ["payments_enabled"]


def test_server_cli_runs_only_the_local_smoke_contract(monkeypatch, tmp_path, capsys):
    server, _state = _load(monkeypatch, tmp_path)
    target = tmp_path / "cli-smoke.json"
    calls = []
    monkeypatch.setattr(
        server,
        "run_recipe_engine_synthetic_smoke",
        lambda *, state_path: calls.append(state_path) or {
            "ok": True, "http_status": 200, "blockers": []
        },
    )

    code = server.main(["--recipe-engine-smoke", "--state", str(target)])

    assert code == 0
    assert calls == [str(target)]
    output = capsys.readouterr().out
    assert '"ok":true' in output
    assert "@" not in output and "token" not in output.casefold()


def test_failed_preflight_cli_sends_only_aggregate_diagnostics(
    monkeypatch, tmp_path, capsys
):
    server, _state = _load(monkeypatch, tmp_path)
    target = tmp_path / "recipe-engine-preflight-smoke.json"
    sent = []
    monkeypatch.setattr(
        server,
        "run_recipe_engine_synthetic_smoke",
        lambda *, state_path: {
            "ok": False,
            "blockers": ["too_slow"],
            "latency_ms": 6_250.0,
            "engine_mode": "on",
            "plan_engine": "deterministic",
            "jobs_delta": 0,
            "ai_costs_delta": 0,
        },
    )

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    def capture(request, timeout):
        sent.append((request, timeout))
        return Response()

    monkeypatch.setattr(server, "urlopen", capture)

    code = server.main(["--recipe-engine-smoke", "--state", str(target)])

    assert code == 1
    assert len(sent) == 1
    request, timeout = sent[0]
    body = request.data.decode("ascii")
    assert timeout == 3
    assert '"blockers":["too_slow"]' in body
    assert '"latency_ms":6250.0' in body
    assert "@" not in body
    assert "token" not in body.casefold()
    assert "recipe" not in body.casefold()
    assert "Uvar.si preflight detail" == request.headers["Title"]
    assert "too_slow" in capsys.readouterr().out


def test_isolated_smoke_reports_only_stable_route_error_code(monkeypatch, tmp_path):
    server, _state = _load(monkeypatch, tmp_path)
    with closing(server._readonly_database()) as con:
        rows, complete = server._complete_recipe_offers(
            con, server.bratislava_day()
        )
    assert complete is True

    def reject_plan(**_kwargs):
        raise server.NoCompatiblePlan(
            "unmeasurable_packages", ("wait_for_complete_flyer_refresh",)
        )

    monkeypatch.setattr(server, "build_deterministic_plan", reject_plan)

    result = server._authenticated_isolated_recipe_smoke(
        rows, now=datetime.now(timezone.utc)
    )

    assert result["valid"] is False
    assert result["response_statuses"] == [503, 503]
    assert result["error_codes"] == ["unmeasurable_packages", "unmeasurable_packages"]
    assert "detail" not in json.dumps(result).casefold()


def test_synthetic_smoke_adds_route_code_without_response_content(
    monkeypatch, tmp_path
):
    server, state = _load(monkeypatch, tmp_path)
    monkeypatch.setattr(
        server,
        "_authenticated_isolated_recipe_smoke",
        lambda *_args, **_kwargs: {
            "valid": False,
            "status": 503,
            "plan_engine": "",
            "jobs_delta": 0,
            "costs_delta": 0,
            "response_statuses": [503, 503],
            "error_codes": ["unmeasurable_packages", "unmeasurable_packages"],
        },
    )

    result = server.run_recipe_engine_synthetic_smoke(state_path=state)

    assert result["blockers"] == [
        "invalid_output",
        "route_unmeasurable_packages",
    ]
    assert "detail" not in json.dumps(result).casefold()
