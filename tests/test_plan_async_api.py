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
        "retry_allowed": True,
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
    with server.db() as con:
        cached = build_personal_plan(con, model_plan(), ["Lidl"], 2, 4)
        cached = server.osobny_plan_na_ulozenie(cached)
        con.execute(
            "INSERT INTO plany (user_id, tyzden, json) VALUES (?, ?, ?)",
            (1, server.monday(), json.dumps(cached, ensure_ascii=False)),
        )
        con.commit()

    submitted = assert_pending(plan_client(server, 1).post("/api/plan/generuj?force=1"))
    polled = assert_pending(plan_client(server, 1).get("/api/plan"))

    assert polled["job_id"] == submitted["job_id"]


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
