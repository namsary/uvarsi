import datetime

import pytest

from app import plan_worker
from app.plan_worker import process_one
from test_plan_worker import (
    FakeModel,
    NOW,
    _grant_premium,
    _job_row,
    _model_output,
    _queued_job,
    app_db,
)


@pytest.fixture(autouse=True)
def reset_recipe_engine_mode(app_db, monkeypatch):
    cached_mode = app_db.server.recipe_engine_mode
    monkeypatch.setenv("UVARSI_RECIPE_ENGINE", "off")
    cached_mode.cache_clear()
    yield
    cached_mode.cache_clear()


def _model_for(app_db):
    rows = app_db.server.akcie_pre(["Lidl", "Kaufland", "Tesco"])
    return FakeModel(_model_output([row["offer_key"] for row in rows[:4]]))


def _set_mode(app_db, monkeypatch, mode):
    monkeypatch.setenv("UVARSI_RECIPE_ENGINE", mode)
    app_db.server.recipe_engine_mode.cache_clear()


@pytest.mark.parametrize("kind", ("regular", "pantry", "precompute"))
def test_on_mode_terminally_retires_claimed_legacy_recipe_jobs_without_ai(
        app_db, monkeypatch, kind):
    if kind == "pantry":
        _grant_premium(app_db)
        with app_db.server.db() as con:
            con.execute("INSERT INTO spajza (user_id, nazov) VALUES (1, 'ryža')")
            con.commit()
    job = _queued_job(app_db, kind=kind)
    _set_mode(app_db, monkeypatch, "on")

    def constructor_bomb():
        raise AssertionError("legacy recipe worker must not construct an AI client")

    monkeypatch.setattr(app_db.server, "_new_plan_model_client", constructor_bomb)

    result = process_one(now=NOW)

    assert (result.job_id, result.status, result.error_code) == (
        job.id, "failed", "engine_replaced",
    )
    row = _job_row(app_db, job.id)
    assert row["state"] == "failed"
    assert row["error_code"] == "engine_replaced"
    assert row["dispatched_at"] is None
    assert row["finished_at"] is not None
    assert row["lease_owner"] is None
    assert row["lease_expires_at"] is None
    assert row["attempts"] == 1
    with app_db.server.db() as con:
        assert con.execute("SELECT COUNT(*) FROM naklady").fetchone()[0] == 0
        assert con.execute("SELECT COUNT(*) FROM plany").fetchone()[0] == 0
        assert con.execute("SELECT COUNT(*) FROM plany_zdielane").fetchone()[0] == 0
        assert con.execute("SELECT COUNT(*) FROM pouzivatelia WHERE id=1").fetchone()[0] == 1
        if kind == "pantry":
            assert con.execute("SELECT nazov FROM spajza WHERE user_id=1").fetchone()[0] == "ryža"


def test_flag_turning_on_after_claim_blocks_client_construction(app_db, monkeypatch):
    job = _queued_job(app_db, kind="regular")
    modes = iter(("shadow", "on"))
    monkeypatch.setattr(app_db.server, "recipe_engine_mode", lambda: next(modes))

    def constructor_bomb():
        raise AssertionError("flag changed before client construction")

    monkeypatch.setattr(app_db.server, "_new_plan_model_client", constructor_bomb)

    result = process_one(now=NOW)

    assert (result.status, result.error_code) == ("failed", "engine_replaced")
    assert _job_row(app_db, job.id)["dispatched_at"] is None


def test_flag_turning_on_immediately_before_dispatch_blocks_model_call(
        app_db, monkeypatch):
    job = _queued_job(app_db, kind="regular")
    modes = iter(("shadow", "shadow", "on"))
    monkeypatch.setattr(app_db.server, "recipe_engine_mode", lambda: next(modes))
    model = _model_for(app_db)

    result = process_one(client=model, now=NOW)

    assert (result.status, result.error_code) == ("failed", "engine_replaced")
    assert model.calls == 0
    assert _job_row(app_db, job.id)["dispatched_at"] is None


def test_flag_turning_on_during_revalidation_blocks_dispatch_in_same_transaction(
        app_db, monkeypatch):
    job = _queued_job(app_db, kind="regular")
    mode = {"value": "shadow"}
    monkeypatch.setattr(
        app_db.server, "recipe_engine_mode", lambda: mode["value"],
    )
    original_revalidate = app_db.server.revalidate_job_context

    def activate_engine_after_revalidation(*args, **kwargs):
        original_revalidate(*args, **kwargs)
        mode["value"] = "on"

    monkeypatch.setattr(
        app_db.server,
        "revalidate_job_context",
        activate_engine_after_revalidation,
    )
    model = _model_for(app_db)

    result = process_one(client=model, now=NOW)

    assert (result.status, result.error_code) == ("failed", "engine_replaced")
    assert model.calls == 0
    row = _job_row(app_db, job.id)
    assert row["state"] == "failed"
    assert row["error_code"] == "engine_replaced"
    assert row["dispatched_at"] is None
    assert row["finished_at"] is not None
    assert row["lease_owner"] is None
    assert row["lease_expires_at"] is None


@pytest.mark.parametrize("mode", ("off", "shadow"))
def test_off_and_shadow_keep_the_existing_recipe_worker_path(
        app_db, monkeypatch, mode):
    job = _queued_job(app_db, kind="regular")
    _set_mode(app_db, monkeypatch, mode)
    model = _model_for(app_db)

    result = process_one(client=model, now=NOW)

    assert result.status == "ready"
    assert model.calls == 1
    assert _job_row(app_db, job.id)["state"] == "ready"


def test_on_mode_idle_worker_keeps_a_healthy_heartbeat(app_db, monkeypatch):
    _set_mode(app_db, monkeypatch, "on")

    result = process_one(now=NOW)

    assert result.status == "empty"
    with app_db.server.db() as con:
        heartbeat = con.execute(
            "SELECT worker_id, job_id, heartbeat_at FROM plan_worker_state WHERE singleton=1"
        ).fetchone()
    assert tuple(heartbeat[:2]) == (plan_worker.WORKER_ID, None)
    assert heartbeat["heartbeat_at"] == NOW.replace(
        tzinfo=datetime.timezone.utc,
    ).isoformat(timespec="seconds")


def test_engine_replaced_job_is_not_retried_on_the_next_worker_cycle(
        app_db, monkeypatch):
    job = _queued_job(app_db, kind="regular")
    _set_mode(app_db, monkeypatch, "on")

    first = process_one(now=NOW)
    second = process_one(now=NOW + datetime.timedelta(seconds=1))

    assert (first.status, first.error_code) == ("failed", "engine_replaced")
    assert second.status == "empty"
    assert _job_row(app_db, job.id)["attempts"] == 1
