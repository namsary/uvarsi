"""Release gate: deterministic user plans must never spend model credit."""

from __future__ import annotations

import sys

import pytest

from tests.test_deterministic_plan_api import _server
from tests.test_server import plan_client


_COST_TABLES = (
    "naklady",
    "naklady_behy",
    "naklady_upozornenia",
    "naklady_kredit",
    "naklady_storna",
)


@pytest.fixture(autouse=True)
def _clear_recipe_engine_flag_cache():
    """Do not leak ``on`` into the legacy off-mode cost integration suite."""
    yield
    module = sys.modules.get("config")
    if module is not None and hasattr(module, "recipe_engine_mode"):
        module.recipe_engine_mode.cache_clear()


def _cost_snapshot(server):
    """Return complete, order-stable cost state rather than only row counts."""
    with server.db() as con:
        snapshot = {}
        for table in _COST_TABLES:
            columns = [row[1] for row in con.execute(f"PRAGMA table_info({table})")]
            order = ", ".join(columns)
            snapshot[table] = [
                tuple(row)
                for row in con.execute(f"SELECT * FROM {table} ORDER BY {order}")
            ]
        snapshot["plan_jobs"] = [
            tuple(row)
            for row in con.execute("SELECT * FROM plan_jobs ORDER BY id")
        ]
    return snapshot


def _seed_cost_history(server):
    """A non-empty ledger catches rewrites as well as accidental inserts."""
    with server.db() as con:
        for index, purpose in enumerate(("zber_letakov", "plan", "predpocet"), start=1):
            con.execute(
                """INSERT INTO naklady
                   (cas,den,mesiac,tyzden,ucel,model,vstup,vystup,
                    cache_write,cache_read,eur,odhad,detail)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    f"2026-08-0{index}T07:00:00",
                    f"2026-08-0{index}",
                    "2026-08",
                    "2026-07-27",
                    purpose,
                    "claude-sonnet-5",
                    100 * index,
                    10 * index,
                    0,
                    0,
                    0.01 * index,
                    0,
                    f"historicky-{purpose}",
                ),
            )
        con.execute(
            "INSERT INTO naklady_behy (tyzden,ucel,pocet,updated) VALUES (?,?,?,?)",
            ("2026-07-27", "zber_letakov", 1, "2026-08-01T07:00:00"),
        )
        con.commit()


@pytest.mark.parametrize("engine_mode", ("off", "shadow", "on"))
def test_every_mode_regular_pantry_and_force_do_not_touch_ai_or_cost_state(
    monkeypatch, tmp_path, engine_mode
):
    """Rollback and shadow modes must never revive model-written recipes."""
    server = _server(
        monkeypatch,
        tmp_path,
        pantry=(("ryža", 1000, "g"), ("tofu", 400, "g"), ("cícer", 500, "g")),
    )
    monkeypatch.setattr(server, "recipe_engine_mode", lambda: engine_mode)
    _seed_cost_history(server)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("user plan entered a model or cost-reservation path")

    monkeypatch.setattr(server, "_new_plan_model_client", forbidden)
    monkeypatch.setattr(server.naklady, "skontroluj", forbidden)
    monkeypatch.setattr(server.naklady, "strazeny_klient", forbidden)
    monkeypatch.setattr(server.plan_jobs, "enqueue", forbidden)
    before = _cost_snapshot(server)

    client = plan_client(server, 1, wait_for_worker=False)
    responses = (
        client.post("/api/plan/generuj"),
        client.post("/api/plan/generuj?force=1"),
        client.post("/api/plan/zo-spajze"),
    )

    assert [response.status_code for response in responses] == [200, 200, 200]
    assert all(response.json()["meta"]["engine"] == "deterministic" for response in responses)
    assert _cost_snapshot(server) == before


@pytest.mark.parametrize("engine_mode", ("off", "shadow", "on"))
def test_worker_refuses_legacy_recipe_ai_in_every_mode(engine_mode):
    """Old queued jobs are cancelled after the deterministic cutover."""
    from app import plan_worker

    class Server:
        @staticmethod
        def recipe_engine_mode():
            return engine_mode

    with pytest.raises(plan_worker.EngineReplaced):
        plan_worker._require_legacy_recipe_engine(Server)


def test_all_direct_legacy_recipe_entrypoints_are_hard_disabled(monkeypatch, tmp_path):
    server = _server(monkeypatch, tmp_path)
    from app import predpocet

    with pytest.raises(server.RecipePlanEngineRetired):
        server._new_plan_model_client()
    with pytest.raises(server.RecipePlanEngineRetired):
        server.build_and_store_job(object())
    with pytest.raises(server.RecipePlanEngineRetired):
        server.poskladaj_novy_plan(None, None, None, None, None, None, None)
    with pytest.raises(RuntimeError, match="paid recipe precompute is retired"):
        predpocet._poskladaj(None, server, (), object())
