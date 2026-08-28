import datetime
import json
import sqlite3
import sys
import types
from concurrent.futures import ThreadPoolExecutor

from app import naklady, plan_jobs
from app.plan_data import build_personal_plan
from tests.test_server import (
    SHARED_VARIANT_USERS,
    current_plan_rows,
    model_plan,
    plan_client as legacy_plan_client,
    shared_plan_server,
)


PENDING_MESSAGE = "Plán pripravujeme. Pokojne pokračuj inde."


def plan_client(server, user_id):
    return legacy_plan_client(server, user_id, wait_for_worker=False)


def forbid_model_construction(monkeypatch):
    calls = []

    class ForbiddenAnthropic:
        def __init__(self, **kwargs):
            calls.append(kwargs)
            raise AssertionError("a cold HTTP request must not construct the model client")

    monkeypatch.setitem(
        sys.modules, "anthropic", types.SimpleNamespace(Anthropic=ForbiddenAnthropic)
    )
    return calls


def assert_pending(response):
    assert response.status_code == 202
    payload = response.json()
    assert payload == {
        "prazdny": True,
        "status": "preparing",
        "job_id": payload["job_id"],
        "retry_after": 4,
        "message": PENDING_MESSAGE,
    }
    assert isinstance(payload["job_id"], int)
    return payload


def job_row(server, job_id):
    with server.db() as con:
        return con.execute("SELECT * FROM plan_jobs WHERE id=?", (job_id,)).fetchone()


def publish_shared_plan(server, job_id):
    with server.db() as con:
        job = con.execute("SELECT * FROM plan_jobs WHERE id=?", (job_id,)).fetchone()
        payload = json.loads(job["payload_json"])
        plan = build_personal_plan(
            con,
            model_plan(),
            payload["stores"],
            payload["frequency"],
            None,
            adults=payload["adults"],
            children=payload["children"],
        )
        server.uloz_zdielany_plan(
            con, job["signature"], job["variant"], job["week"], plan
        )
        con.execute(
            "UPDATE plan_jobs SET state='ready', finished_at=?, updated_at=? WHERE id=?",
            (datetime.datetime.now().isoformat(), datetime.datetime.now().isoformat(), job_id),
        )
        con.commit()


def cache_current_regular_plan(server, user_id=1):
    with server.db() as con:
        cached = build_personal_plan(con, model_plan(), ["Lidl"], 2, 4)
        cached = server.osobny_plan_na_ulozenie(cached)
        con.execute(
            "INSERT INTO plany (user_id, tyzden, json) VALUES (?, ?, ?)",
            (user_id, server.monday(), json.dumps(cached, ensure_ascii=False)),
        )
        con.commit()


def fail_job(server, job_id, code):
    with server.db() as con:
        con.execute(
            "UPDATE plan_jobs SET state='failed', error_code=?, finished_at=CURRENT_TIMESTAMP "
            "WHERE id=?",
            (code, job_id),
        )
        con.commit()


def test_cold_post_returns_202_without_calling_model(monkeypatch, tmp_path):
    server = shared_plan_server(monkeypatch, tmp_path, users=(1,), premium=False)
    model_calls = forbid_model_construction(monkeypatch)

    payload = assert_pending(plan_client(server, 1).post("/api/plan/generuj"))

    assert model_calls == []
    with server.db() as con:
        job = con.execute("SELECT * FROM plan_jobs WHERE id=?", (payload["job_id"],)).fetchone()
        assert (job["state"], job["priority"], job["kind"]) == ("queued", 100, "regular")


def test_same_signature_from_two_users_creates_one_job(monkeypatch, tmp_path):
    first, second = SHARED_VARIANT_USERS
    server = shared_plan_server(
        monkeypatch, tmp_path, users=(first, second), pantry={first: [], second: []}
    )
    forbid_model_construction(monkeypatch)

    a = assert_pending(plan_client(server, first).post("/api/plan/generuj"))
    b = assert_pending(plan_client(server, second).post("/api/plan/generuj"))

    assert a["job_id"] == b["job_id"]
    with server.db() as con:
        assert con.execute(
            "SELECT COUNT(*) FROM plan_jobs WHERE state IN ('queued', 'running')"
        ).fetchone()[0] == 1
        assert con.execute("SELECT SUM(pocet) FROM prepocty").fetchone()[0] == 1


def test_both_joiners_can_poll_and_adopt_one_shared_regular_job(monkeypatch, tmp_path):
    first, second = SHARED_VARIANT_USERS
    server = shared_plan_server(
        monkeypatch, tmp_path, users=(first, second), pantry={first: [], second: []}
    )
    forbid_model_construction(monkeypatch)
    first_client = plan_client(server, first)
    second_client = plan_client(server, second)

    submitted = assert_pending(first_client.post("/api/plan/generuj"))
    joined = assert_pending(second_client.post("/api/plan/generuj"))

    assert joined["job_id"] == submitted["job_id"]
    assert assert_pending(first_client.get("/api/plan"))["job_id"] == submitted["job_id"]
    assert assert_pending(second_client.get("/api/plan"))["job_id"] == submitted["job_id"]

    publish_shared_plan(server, submitted["job_id"])
    first_ready = first_client.get("/api/plan")
    second_ready = second_client.get("/api/plan")
    assert first_ready.json()["jedla"]
    assert second_ready.json()["jedla"]
    assert "status" not in first_ready.json()
    assert "status" not in second_ready.json()


def test_both_joiners_see_failure_of_one_shared_regular_job(monkeypatch, tmp_path):
    first, second = SHARED_VARIANT_USERS
    server = shared_plan_server(
        monkeypatch, tmp_path, users=(first, second), pantry={first: [], second: []}
    )
    first_client = plan_client(server, first)
    second_client = plan_client(server, second)
    submitted = assert_pending(first_client.post("/api/plan/generuj"))
    assert_pending(second_client.post("/api/plan/generuj"))
    fail_job(server, submitted["job_id"], "provider_timeout")

    first_failed = first_client.get("/api/plan")
    second_failed = second_client.get("/api/plan")

    assert first_failed.json()["status"] == "failed"
    assert second_failed.json()["status"] == "failed"
    assert first_failed.json()["job_id"] == second_failed.json()["job_id"] == submitted["job_id"]


def test_cache_hit_still_returns_plan_directly(monkeypatch, tmp_path):
    server = shared_plan_server(monkeypatch, tmp_path, users=(1,), premium=False)
    forbid_model_construction(monkeypatch)
    with server.db() as con:
        cached = build_personal_plan(con, model_plan(), ["Lidl"], 2, 4)
        cached = server.osobny_plan_na_ulozenie(cached)
        con.execute(
            "INSERT INTO plany (user_id, tyzden, json) VALUES (?, ?, ?)",
            (1, server.monday(), json.dumps(cached, ensure_ascii=False)),
        )
        con.commit()

    response = plan_client(server, 1).get("/api/plan")

    assert response.status_code == 200
    assert response.json()["jedla"]
    assert "status" not in response.json()
    with sqlite3.connect(server.DB) as con:
        assert con.execute("SELECT COUNT(*) FROM plan_jobs").fetchone()[0] == 0


def test_get_reports_the_active_regular_job(monkeypatch, tmp_path):
    server = shared_plan_server(monkeypatch, tmp_path, users=(1,), premium=False)
    forbid_model_construction(monkeypatch)
    submitted = assert_pending(plan_client(server, 1).post("/api/plan/generuj"))

    polled = assert_pending(plan_client(server, 1).get("/api/plan"))

    assert polled["job_id"] == submitted["job_id"]


def test_get_adopts_completed_shared_plan_without_a_new_reservation(monkeypatch, tmp_path):
    server = shared_plan_server(monkeypatch, tmp_path, users=(1,), premium=False)
    forbid_model_construction(monkeypatch)
    submitted = assert_pending(plan_client(server, 1).post("/api/plan/generuj"))
    publish_shared_plan(server, submitted["job_id"])

    response = plan_client(server, 1).get("/api/plan")

    assert response.status_code == 200
    assert response.json()["jedla"]
    assert "status" not in response.json()
    with server.db() as con:
        assert con.execute("SELECT pocet FROM prepocty WHERE user_id=1").fetchone()[0] == 1
        assert con.execute("SELECT COUNT(*) FROM plany WHERE user_id=1").fetchone()[0] == 1


def test_get_reports_retrying_pre_dispatch_failure_as_preparing(monkeypatch, tmp_path):
    server = shared_plan_server(monkeypatch, tmp_path, users=(1,), premium=False)
    submitted = assert_pending(plan_client(server, 1).post("/api/plan/generuj"))
    now = datetime.datetime.now()
    with server.db() as con:
        claimed = plan_jobs.claim_next(con, "worker-a", now=now)
        assert claimed.id == submitted["job_id"]
        assert plan_jobs.mark_failed(
            con, claimed.id, "network_before_send", True,
            worker_id="worker-a", now=now,
        )

    assert_pending(plan_client(server, 1).get("/api/plan"))


def test_get_reports_terminal_failure_with_stable_code(monkeypatch, tmp_path):
    server = shared_plan_server(monkeypatch, tmp_path, users=(1,), premium=False)
    submitted = assert_pending(plan_client(server, 1).post("/api/plan/generuj"))
    now = datetime.datetime.now()
    with server.db() as con:
        claimed = plan_jobs.claim_next(con, "worker-a", now=now)
        assert plan_jobs.mark_dispatched(
            con, claimed.id, worker_id="worker-a", now=now
        )
        assert plan_jobs.mark_failed(
            con, claimed.id, "provider_timeout", False,
            worker_id="worker-a", now=now,
        )

    response = plan_client(server, 1).get("/api/plan")

    assert response.status_code == 200
    assert response.json() == {
        "prazdny": True,
        "status": "failed",
        "job_id": submitted["job_id"],
        "code": "provider_timeout",
        "message": "Plán sa nepodarilo pripraviť. Skús to znova.",
        "retry_allowed": False,
    }


def test_pantry_job_is_user_scoped_and_get_checks_current_pantry_signature(monkeypatch, tmp_path):
    server = shared_plan_server(
        monkeypatch, tmp_path, users=(1,), pantry={1: ["soľ"]}, premium=True
    )
    forbid_model_construction(monkeypatch)

    submitted = assert_pending(plan_client(server, 1).post("/api/plan/zo-spajze"))
    row = job_row(server, submitted["job_id"])
    assert row["kind"] == "pantry" and row["user_id"] == 1
    assert row["job_key"].startswith("pantry:1:")
    assert_pending(plan_client(server, 1).get("/api/plan"))

    assert plan_client(server, 1).post(
        "/api/spajza", json={"polozky": ["soľ", "vajcia"]}
    ).status_code == 200
    changed = plan_client(server, 1).get("/api/plan")
    assert changed.status_code == 200
    assert changed.json() == {"prazdny": True}


def test_force_double_click_joins_one_user_scoped_sequence(monkeypatch, tmp_path):
    server = shared_plan_server(monkeypatch, tmp_path, users=(1,), premium=True)
    forbid_model_construction(monkeypatch)
    client = plan_client(server, 1)

    first = assert_pending(client.post("/api/plan/generuj?force=1"))
    second = assert_pending(client.post("/api/plan/generuj?force=1"))

    assert first["job_id"] == second["job_id"]
    row = job_row(server, first["job_id"])
    assert row["job_key"].startswith("force:1:")
    with server.db() as con:
        assert con.execute("SELECT pocet FROM prepocty WHERE user_id=1").fetchone()[0] == 1
        assert con.execute("SELECT COUNT(*) FROM plan_jobs").fetchone()[0] == 1


def test_force_after_terminal_completion_creates_the_next_sequence(monkeypatch, tmp_path):
    server = shared_plan_server(monkeypatch, tmp_path, users=(1,), premium=True)
    client = plan_client(server, 1)
    first = assert_pending(client.post("/api/plan/generuj?force=1"))
    with server.db() as con:
        con.execute("UPDATE plan_jobs SET state='failed', error_code='invalid_output' WHERE id=?", (first["job_id"],))
        con.commit()

    second = assert_pending(client.post("/api/plan/generuj?force=1"))

    assert second["job_id"] != first["job_id"]
    with server.db() as con:
        keys = [row[0] for row in con.execute("SELECT job_key FROM plan_jobs ORDER BY id")]
        assert len(set(keys)) == 2
        assert con.execute("SELECT pocet FROM prepocty WHERE user_id=1").fetchone()[0] == 2


def test_active_force_job_takes_precedence_over_the_previous_cached_plan(monkeypatch, tmp_path):
    server = shared_plan_server(monkeypatch, tmp_path, users=(1,), premium=True)
    forbid_model_construction(monkeypatch)
    cache_current_regular_plan(server)

    submitted = assert_pending(plan_client(server, 1).post("/api/plan/generuj?force=1"))
    polled = assert_pending(plan_client(server, 1).get("/api/plan"))

    assert polled["job_id"] == submitted["job_id"]


def test_force_double_click_joins_after_pantry_reserves_an_interleaved_regeneration(
        monkeypatch, tmp_path):
    server = shared_plan_server(
        monkeypatch, tmp_path, users=(1,), pantry={1: ["soľ"]}, premium=True
    )
    forbid_model_construction(monkeypatch)
    client = plan_client(server, 1)

    first_force = assert_pending(client.post("/api/plan/generuj?force=1"))
    pantry = assert_pending(client.post("/api/plan/zo-spajze"))
    repeated_force = assert_pending(client.post("/api/plan/generuj?force=1"))

    assert repeated_force["job_id"] == first_force["job_id"]
    assert pantry["job_id"] != first_force["job_id"]
    with server.db() as con:
        assert con.execute("SELECT COUNT(*) FROM plan_jobs").fetchone()[0] == 2
        assert con.execute("SELECT pocet FROM prepocty WHERE user_id=1").fetchone()[0] == 2


def test_force_does_not_join_an_active_ordinary_regular_job(monkeypatch, tmp_path):
    server = shared_plan_server(monkeypatch, tmp_path, users=(1,), premium=True)
    forbid_model_construction(monkeypatch)
    client = plan_client(server, 1)

    ordinary = assert_pending(client.post("/api/plan/generuj"))
    forced = assert_pending(client.post("/api/plan/generuj?force=1"))
    repeated_force = assert_pending(client.post("/api/plan/generuj?force=1"))

    assert forced["job_id"] != ordinary["job_id"]
    assert repeated_force["job_id"] == forced["job_id"]
    with server.db() as con:
        rows = con.execute(
            "SELECT id, is_force FROM plan_jobs ORDER BY id"
        ).fetchall()
        assert [tuple(row) for row in rows] == [
            (ordinary["job_id"], 0),
            (forced["job_id"], 1),
        ]


def test_active_force_remains_visible_and_joinable_after_regeneration_day_changes(
        monkeypatch, tmp_path):
    server = shared_plan_server(monkeypatch, tmp_path, users=(1,), premium=True)
    forbid_model_construction(monkeypatch)
    day = {"value": "2026-08-27"}
    monkeypatch.setattr(server, "dnesok", lambda _today=None: day["value"])
    client = plan_client(server, 1)

    submitted = assert_pending(client.post("/api/plan/generuj?force=1"))
    day["value"] = "2026-08-28"

    polled = assert_pending(client.get("/api/plan"))
    repeated = assert_pending(client.post("/api/plan/generuj?force=1"))

    assert polled["job_id"] == repeated["job_id"] == submitted["job_id"]
    with server.db() as con:
        assert con.execute("SELECT COUNT(*) FROM plan_jobs").fetchone()[0] == 1


def test_failed_force_request_supersedes_old_valid_cache_without_deleting_it(
        monkeypatch, tmp_path):
    server = shared_plan_server(monkeypatch, tmp_path, users=(1,), premium=True)
    cache_current_regular_plan(server)
    submitted = assert_pending(plan_client(server, 1).post("/api/plan/generuj?force=1"))
    fail_job(server, submitted["job_id"], "provider_timeout")

    response = plan_client(server, 1).get("/api/plan")

    assert response.json()["status"] == "failed"
    assert response.json()["job_id"] == submitted["job_id"]
    with server.db() as con:
        assert con.execute("SELECT COUNT(*) FROM plany WHERE user_id=1").fetchone()[0] == 1


def test_failed_pantry_request_never_falls_back_to_old_regular_cache(monkeypatch, tmp_path):
    server = shared_plan_server(
        monkeypatch, tmp_path, users=(1,), pantry={1: ["soľ"]}, premium=True
    )
    cache_current_regular_plan(server)
    submitted = assert_pending(plan_client(server, 1).post("/api/plan/zo-spajze"))
    fail_job(server, submitted["job_id"], "invalid_model_output")

    response = plan_client(server, 1).get("/api/plan")

    assert response.json()["status"] == "failed"
    assert response.json()["job_id"] == submitted["job_id"]


def test_ready_cache_newer_than_failed_request_takes_precedence(monkeypatch, tmp_path):
    server = shared_plan_server(monkeypatch, tmp_path, users=(1,), premium=True)
    cache_current_regular_plan(server)
    submitted = assert_pending(plan_client(server, 1).post("/api/plan/generuj?force=1"))
    fail_job(server, submitted["job_id"], "provider_timeout")
    with server.db() as con:
        con.execute(
            "UPDATE plan_jobs SET created='2026-08-28T14:00:00+02:00' WHERE id=?",
            (submitted["job_id"],),
        )
        con.execute(
            "UPDATE plany SET vytvoreny='2026-08-28 12:01:00' WHERE user_id=1"
        )
        con.commit()

    response = plan_client(server, 1).get("/api/plan")

    assert response.json()["jedla"]
    assert "status" not in response.json()


def test_failed_explicit_job_never_leaks_to_another_user(monkeypatch, tmp_path):
    server = shared_plan_server(
        monkeypatch, tmp_path, users=(1, 2), pantry={1: ["soľ"], 2: ["soľ"]}, premium=True
    )
    submitted = assert_pending(plan_client(server, 1).post("/api/plan/zo-spajze"))
    fail_job(server, submitted["job_id"], "provider_timeout")

    other = plan_client(server, 2).get("/api/plan")

    assert other.json() == {"prazdny": True}


def test_other_user_sees_shared_ordinary_status_but_not_later_force_or_pantry(
        monkeypatch, tmp_path):
    first, second = SHARED_VARIANT_USERS
    server = shared_plan_server(
        monkeypatch,
        tmp_path,
        users=(first, second),
        pantry={first: ["soľ"], second: ["soľ"]},
        premium=True,
    )
    first_client = plan_client(server, first)
    shared = assert_pending(first_client.post("/api/plan/generuj"))
    forced = assert_pending(first_client.post("/api/plan/generuj?force=1"))
    pantry = assert_pending(first_client.post("/api/plan/zo-spajze"))

    other = assert_pending(plan_client(server, second).get("/api/plan"))

    assert other["job_id"] == shared["job_id"]
    assert other["job_id"] not in {forced["job_id"], pantry["job_id"]}


def test_retry_allowed_reflects_remaining_daily_regeneration(monkeypatch, tmp_path):
    server = shared_plan_server(monkeypatch, tmp_path, users=(1,), premium=True)
    submitted = assert_pending(plan_client(server, 1).post("/api/plan/generuj?force=1"))
    fail_job(server, submitted["job_id"], "provider_timeout")

    available = plan_client(server, 1).get("/api/plan")
    assert available.json()["retry_allowed"] is True

    with server.db() as con:
        con.execute(
            "UPDATE prepocty SET pocet=? WHERE user_id=1 AND den=?",
            (server.LIMIT_PREPOCTOV_PREMIUM, server.dnesok()),
        )
        con.commit()
    exhausted = plan_client(server, 1).get("/api/plan")
    assert exhausted.json()["retry_allowed"] is False


def test_non_retryable_terminal_code_never_promises_retry(monkeypatch, tmp_path):
    server = shared_plan_server(monkeypatch, tmp_path, users=(1,), premium=True)
    submitted = assert_pending(plan_client(server, 1).post("/api/plan/generuj?force=1"))
    fail_job(server, submitted["job_id"], "invalid_profile")

    response = plan_client(server, 1).get("/api/plan")

    assert response.json()["retry_allowed"] is False


def test_retry_is_not_promised_when_current_budget_policy_would_reject_it(
        monkeypatch, tmp_path):
    server = shared_plan_server(monkeypatch, tmp_path, users=(1,), premium=True)
    submitted = assert_pending(plan_client(server, 1).post("/api/plan/generuj?force=1"))
    fail_job(server, submitted["job_id"], "provider_timeout")
    monkeypatch.setenv("UVARSI_MESACNY_STROP_EUR", "0.10")

    response = plan_client(server, 1).get("/api/plan")

    assert response.json()["retry_allowed"] is False


def test_twenty_concurrent_identical_requests_create_one_job(monkeypatch, tmp_path):
    server = shared_plan_server(monkeypatch, tmp_path, users=(1,), premium=False)
    forbid_model_construction(monkeypatch)

    def submit(_index):
        return plan_client(server, 1).post("/api/plan/generuj")

    with ThreadPoolExecutor(max_workers=20) as pool:
        responses = list(pool.map(submit, range(20)))

    payloads = [assert_pending(response) for response in responses]
    assert len({payload["job_id"] for payload in payloads}) == 1
    with server.db() as con:
        assert con.execute("SELECT COUNT(*) FROM plan_jobs").fetchone()[0] == 1
        assert con.execute("SELECT pocet FROM prepocty WHERE user_id=1").fetchone()[0] == 1


def test_two_different_signatures_create_two_jobs(monkeypatch, tmp_path):
    first, second = SHARED_VARIANT_USERS
    server = shared_plan_server(monkeypatch, tmp_path, users=(first, second), premium=True)
    with server.db() as con:
        con.execute("UPDATE pouzivatelia SET frekvencia=3 WHERE id=?", (second,))
        con.commit()

    a = assert_pending(plan_client(server, first).post("/api/plan/generuj"))
    b = assert_pending(plan_client(server, second).post("/api/plan/generuj"))

    assert a["job_id"] != b["job_id"]
    with server.db() as con:
        assert con.execute("SELECT COUNT(DISTINCT signature) FROM plan_jobs").fetchone()[0] == 2


def test_monthly_cap_rejects_enqueue_without_touching_ledgers(monkeypatch, tmp_path):
    monkeypatch.setenv("UVARSI_MESACNY_STROP_EUR", "0.10")
    server = shared_plan_server(monkeypatch, tmp_path, users=(1,), premium=False)

    response = plan_client(server, 1).post("/api/plan/generuj")

    assert response.status_code == 503
    assert response.json()["kod"] == naklady.KOD_MESACNY
    with server.db() as con:
        assert con.execute("SELECT COUNT(*) FROM plan_jobs").fetchone()[0] == 0
        assert con.execute("SELECT COUNT(*) FROM prepocty").fetchone()[0] == 0
        assert con.execute("SELECT COUNT(*) FROM naklady").fetchone()[0] == 0


def test_unreadable_budget_rejects_enqueue_without_touching_ledgers(monkeypatch, tmp_path):
    monkeypatch.setenv("UVARSI_MESACNY_STROP_EUR", "not-a-number")
    server = shared_plan_server(monkeypatch, tmp_path, users=(1,), premium=False)

    response = plan_client(server, 1).post("/api/plan/generuj")

    assert response.status_code == 503
    assert response.json()["kod"] == naklady.KOD_NECITATELNY
    with server.db() as con:
        assert con.execute("SELECT COUNT(*) FROM plan_jobs").fetchone()[0] == 0
        assert con.execute("SELECT COUNT(*) FROM prepocty").fetchone()[0] == 0
