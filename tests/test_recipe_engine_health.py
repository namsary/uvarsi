"""Fail-closed health and local synthetic smoke for the recipe rollout."""

from __future__ import annotations

from contextlib import closing
from datetime import date, datetime, timezone
import json
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


def test_health_requires_complete_current_offers_from_all_three_stores(
    monkeypatch, tmp_path
):
    rows = [row for row in _offer_rows() if row[2] != "Lidl"]
    server, state = _load(monkeypatch, tmp_path, rows=rows)
    _write(state, _passing_smoke(server))

    engine = TestClient(server.app).get("/api/health").json()["recipe_engine"]

    assert engine["ready"] is False
    assert "incomplete_offers" in engine["blockers"]


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
    with closing(server.db()) as con:
        before = {
            "users": con.execute("SELECT COUNT(*) FROM pouzivatelia").fetchone()[0],
            "plans": con.execute("SELECT COUNT(*) FROM plany").fetchone()[0],
            "shared": con.execute("SELECT COUNT(*) FROM plany_zdielane").fetchone()[0],
            "jobs": con.execute("SELECT COUNT(*) FROM plan_jobs").fetchone()[0],
            "costs": con.execute("SELECT COUNT(*) FROM naklady").fetchone()[0],
        }

    result = server.run_recipe_engine_synthetic_smoke(state_path=state)

    assert result["ok"] is True
    assert result["http_status"] == 200
    assert result["latency_ms"] < 2_000
    assert result["jobs_delta"] == result["ai_costs_delta"] == 0
    assert result["payments_enabled"] is False
    assert result["plan_engine"] == "deterministic"
    assert result["auth_scope"] == "server_local"
    with closing(server.db()) as con:
        after = {
            "users": con.execute("SELECT COUNT(*) FROM pouzivatelia").fetchone()[0],
            "plans": con.execute("SELECT COUNT(*) FROM plany").fetchone()[0],
            "shared": con.execute("SELECT COUNT(*) FROM plany_zdielane").fetchone()[0],
            "jobs": con.execute("SELECT COUNT(*) FROM plan_jobs").fetchone()[0],
            "costs": con.execute("SELECT COUNT(*) FROM naklady").fetchone()[0],
        }
    assert after == before
    durable = state.read_text(encoding="utf-8")
    assert "@" not in durable
    assert "token" not in durable.casefold()
    assert "jedla" not in durable


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
