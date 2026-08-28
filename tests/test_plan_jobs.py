import datetime
import sqlite3

import pytest

from app import naklady, plan_jobs
from app.plan_jobs import JobRequest, enqueue


NOW = datetime.datetime(2026, 8, 28, 9, 0, 0)


@pytest.fixture
def con(tmp_path, monkeypatch):
    monkeypatch.setenv("UVARSI_URL", "https://uvar.si")
    from app import server

    connection = sqlite3.connect(tmp_path / "uvarsi.db")
    connection.row_factory = sqlite3.Row
    server.migruj_schemu(connection)
    yield connection
    connection.close()


@pytest.fixture(autouse=True)
def ciste_prostredie(monkeypatch):
    for name in naklady.PREMENNE_PROSTREDIA:
        monkeypatch.delenv(name, raising=False)


def request(**changes):
    values = {
        "job_key": "regular:abc:0",
        "signature": "abc",
        "variant": 0,
        "kind": "regular",
        "user_id": 1,
        "week": "2026-08-24",
        "priority": 100,
        "payload": {},
        "regeneration_limit": 2,
        "regeneration_day": "2026-08-28",
    }
    values.update(changes)
    return JobRequest(**values)


def precompute_request(**changes):
    values = {
        "kind": "precompute",
        "user_id": None,
        "job_key": "pre:abc:0",
        "regeneration_limit": None,
        "regeneration_day": None,
    }
    values.update(changes)
    return request(**values)


def test_server_migration_installs_plan_job_tables(con):
    tables = {
        row[0] for row in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    assert {"plan_jobs", "plan_worker_state"} <= tables


def test_health_treats_legacy_naive_timestamps_as_utc(con):
    enqueue(con, request(), now=NOW - datetime.timedelta(seconds=181))
    plan_jobs.heartbeat(
        con, "legacy-worker", None, now=NOW - datetime.timedelta(seconds=61)
    )

    result = plan_jobs.health(con, now=NOW.replace(tzinfo=datetime.timezone.utc))

    assert result["oldest_seconds"] == 181
    assert result["heartbeat_seconds"] == 61


def test_health_never_reports_negative_age_for_future_aware_timestamps(con):
    local_time = datetime.timezone(datetime.timedelta(hours=2))
    now = NOW.replace(tzinfo=datetime.timezone.utc)
    enqueue(
        con,
        request(),
        now=(now + datetime.timedelta(seconds=10)).astimezone(local_time),
    )
    plan_jobs.heartbeat(
        con,
        "future-worker",
        None,
        now=(now + datetime.timedelta(seconds=5)).astimezone(local_time),
    )

    result = plan_jobs.health(con, now=NOW)

    assert result["oldest_seconds"] == 0
    assert result["heartbeat_seconds"] == 0


def test_enqueue_is_idempotent_for_one_active_key(con):
    first = enqueue(con, request(), now=NOW)
    second = enqueue(con, request(), now=NOW)

    assert first.created is True
    assert second.created is False
    assert second.job.id == first.job.id


def test_regular_and_pantry_jobs_never_collide(con):
    regular = request(job_key="regular:abc:0", kind="regular")
    pantry = request(job_key="pantry:1:abc:0", kind="pantry")

    assert enqueue(con, regular, now=NOW).job.id != enqueue(con, pantry, now=NOW).job.id


@pytest.mark.parametrize("kind", ("regular", "pantry"))
def test_regular_and_pantry_jobs_require_both_regeneration_fields(kind):
    with pytest.raises(ValueError, match="regeneration"):
        request(kind=kind, regeneration_limit=None, regeneration_day=None)

    with pytest.raises(ValueError, match="regeneration"):
        request(kind=kind, regeneration_limit=None, regeneration_day="2026-08-28")


def test_precompute_rejects_user_or_regeneration_reservation():
    with pytest.raises(ValueError, match="precompute"):
        precompute_request(user_id=1)

    with pytest.raises(ValueError, match="precompute"):
        precompute_request(regeneration_limit=2, regeneration_day="2026-08-28")


def test_regeneration_reservation_round_trips_through_job_storage(con):
    created = enqueue(con, request(regeneration_limit=3, regeneration_day="2026-08-28"), now=NOW).job
    claimed = plan_jobs.claim_next(con, "worker-a", now=NOW)

    assert created.regeneration_limit == 3
    assert created.regeneration_day == "2026-08-28"
    assert claimed.regeneration_limit == 3
    assert claimed.regeneration_day == "2026-08-28"


def test_enqueue_reserves_one_user_regeneration_only_when_creating_a_job(con):
    live = request(regeneration_limit=2, regeneration_day="2026-08-28")

    assert enqueue(con, live, now=NOW).created is True
    assert enqueue(con, live, now=NOW).created is False
    assert con.execute(
        "SELECT pocet FROM prepocty WHERE user_id=? AND den=?", (1, "2026-08-28")
    ).fetchone()[0] == 1


def test_precompute_does_not_reserve_a_user_regeneration(con):
    job = precompute_request()

    enqueue(con, job, now=NOW)

    assert con.execute("SELECT COUNT(*) FROM prepocty").fetchone()[0] == 0


def test_outstanding_queue_reservations_block_a_second_job(con, monkeypatch):
    monkeypatch.setenv("UVARSI_DENNY_STROP_EUR", "0.20")
    enqueue(con, request(job_key="first", reserved_eur=0.12), now=NOW)

    with pytest.raises(naklady.RozpocetVycerpany) as failure:
        enqueue(con, request(job_key="second", reserved_eur=0.12), now=NOW)

    assert failure.value.kod == naklady.KOD_DENNY


def test_persisted_credit_exhaustion_rejects_enqueue_before_reserving_a_daily_slot(con):
    naklady.zapamataj_kredit(con, ucel="plan", teraz=NOW)

    with pytest.raises(naklady.KreditVycerpany):
        enqueue(con, request(), now=NOW)

    assert con.execute("SELECT COUNT(*) FROM plan_jobs").fetchone()[0] == 0
    assert con.execute("SELECT COUNT(*) FROM prepocty").fetchone()[0] == 0


def test_claimed_job_excludes_its_own_reservation_but_keeps_other_jobs_protected(
        con, monkeypatch):
    monkeypatch.setenv("UVARSI_DENNY_STROP_EUR", "0.24")
    first = enqueue(con, request(job_key="first", reserved_eur=0.12), now=NOW).job
    enqueue(con, request(job_key="second", reserved_eur=0.12), now=NOW)

    class Model:
        def __init__(self):
            self.messages = self
            self.calls = 0

        def create(self, **_kwargs):
            self.calls += 1
            return type("Response", (), {"usage": None})()

    model = Model()
    guarded = naklady.strazeny_klient(
        con,
        model,
        "plan",
        odhad_eur=first.reserved_eur,
        teraz=NOW,
        rezervovane_eur=lambda: plan_jobs.active_reservations_eur(
            con, exclude_job_id=first.id,
        ),
    )

    guarded.messages.create(model="claude-sonnet-5", max_tokens=1, messages=[])

    assert model.calls == 1
    assert plan_jobs.active_reservations_eur(con, exclude_job_id=first.id) == pytest.approx(0.12)
    assert con.execute("SELECT COUNT(*) FROM naklady").fetchone()[0] == 1


def test_enqueue_never_rewrites_existing_cost_rows(con):
    naklady.zapis(con, "plan", "claude-sonnet-5", None, teraz=NOW)
    before = tuple(con.execute("SELECT id, eur, odhad FROM naklady").fetchone())

    enqueue(con, request(), now=NOW)

    after = tuple(con.execute("SELECT id, eur, odhad FROM naklady").fetchone())
    assert after == before


def test_claim_prefers_live_priority_and_recovers_expired_lease(con):
    enqueue(con, request(job_key="pre", priority=20), now=NOW)
    live = enqueue(con, request(job_key="live", priority=100), now=NOW).job

    claimed = plan_jobs.claim_next(con, "worker-a", now=NOW, lease_seconds=150)
    recovered = plan_jobs.claim_next(
        con, "worker-b", now=NOW + datetime.timedelta(seconds=151)
    )

    assert claimed.id == live.id
    assert recovered.id == live.id
    assert recovered.lease_owner == "worker-b"


def test_heartbeat_extends_a_claimed_lease_and_records_worker_state(con):
    job = enqueue(con, request(), now=NOW).job
    plan_jobs.claim_next(con, "worker-a", now=NOW)

    plan_jobs.heartbeat(con, "worker-a", job.id, now=NOW + datetime.timedelta(seconds=100))

    assert plan_jobs.claim_next(con, "worker-b", now=NOW + datetime.timedelta(seconds=151)) is None
    worker = con.execute("SELECT worker_id, job_id FROM plan_worker_state").fetchone()
    assert tuple(worker) == ("worker-a", job.id)


def test_dispatched_job_is_not_automatically_requeued(con):
    job = enqueue(con, request(), now=NOW).job
    plan_jobs.claim_next(con, "worker-a", now=NOW)
    assert plan_jobs.mark_dispatched(con, job.id, worker_id="worker-a", now=NOW) is True

    assert plan_jobs.mark_failed(
        con, job.id, "provider_timeout", retryable_before_dispatch=False,
        worker_id="worker-a", now=NOW,
    ) is True

    status = plan_jobs.status_for_key(con, job.job_key)
    assert status.state == "failed"
    assert status.error_code == "provider_timeout"


def test_expired_dispatched_job_fails_instead_of_being_claimed_again(con):
    job = enqueue(con, request(), now=NOW).job
    plan_jobs.claim_next(con, "worker-a", now=NOW)
    assert plan_jobs.mark_dispatched(con, job.id, worker_id="worker-a", now=NOW) is True

    assert plan_jobs.claim_next(con, "worker-b", now=NOW + datetime.timedelta(seconds=151)) is None
    status = plan_jobs.status_for_key(con, job.job_key)
    assert status.state == "failed"
    assert status.error_code == "worker_lost_after_dispatch"


def test_failure_before_dispatch_returns_the_same_job_to_the_queue(con):
    job = enqueue(con, request(), now=NOW).job
    plan_jobs.claim_next(con, "worker-a", now=NOW)

    assert plan_jobs.mark_failed(
        con, job.id, "network_before_send", retryable_before_dispatch=True,
        worker_id="worker-a", now=NOW,
    ) is True

    retried = plan_jobs.claim_next(con, "worker-b", now=NOW)
    assert retried.id == job.id
    assert retried.attempts == 2


def test_mark_ready_is_a_terminal_state(con):
    job = enqueue(con, request(), now=NOW).job
    plan_jobs.claim_next(con, "worker-a", now=NOW)

    assert plan_jobs.mark_ready(con, job.id, worker_id="worker-a", now=NOW) is True

    assert plan_jobs.status_for_key(con, job.job_key).state == "ready"


def test_stale_worker_cannot_mutate_a_reclaimed_job(con):
    job = enqueue(con, request(), now=NOW).job
    plan_jobs.claim_next(con, "worker-a", now=NOW, lease_seconds=1)
    reclaimed = plan_jobs.claim_next(con, "worker-b", now=NOW + datetime.timedelta(seconds=2))

    assert reclaimed.id == job.id
    assert plan_jobs.mark_dispatched(
        con, job.id, worker_id="worker-a", now=NOW + datetime.timedelta(seconds=2)
    ) is False
    assert plan_jobs.mark_ready(
        con, job.id, worker_id="worker-a", now=NOW + datetime.timedelta(seconds=2)
    ) is False
    assert plan_jobs.mark_failed(
        con, job.id, "stale", retryable_before_dispatch=True,
        worker_id="worker-a", now=NOW + datetime.timedelta(seconds=2),
    ) is False

    row = con.execute("SELECT state, lease_owner, dispatched_at FROM plan_jobs WHERE id=?", (job.id,)).fetchone()
    assert tuple(row) == ("running", "worker-b", None)


def test_duplicate_mark_dispatched_cannot_authorize_another_dispatch(con):
    job = enqueue(con, request(), now=NOW).job
    plan_jobs.claim_next(con, "worker-a", now=NOW)

    assert plan_jobs.mark_dispatched(con, job.id, worker_id="worker-a", now=NOW) is True
    assert plan_jobs.mark_dispatched(con, job.id, worker_id="worker-a", now=NOW) is False


def test_second_worker_cannot_claim_while_another_lease_is_live(con):
    first = enqueue(con, request(job_key="first", priority=100), now=NOW).job
    enqueue(con, request(job_key="second", priority=20), now=NOW)

    claimed = plan_jobs.claim_next(con, "worker-a", now=NOW)
    competing = plan_jobs.claim_next(con, "worker-b", now=NOW + datetime.timedelta(seconds=1))

    assert claimed.id == first.id
    assert competing is None
    assert plan_jobs.status_for_key(con, "second").state == "queued"


def test_enqueue_rejects_a_dirty_connection_without_committing_caller_changes(con):
    con.execute("INSERT INTO prepocty (user_id, den, pocet) VALUES (99, '2026-08-28', 1)")

    with pytest.raises(plan_jobs.DirtyConnectionError, match="clean connection"):
        enqueue(con, request(), now=NOW)

    assert con.in_transaction is True
    con.rollback()
    assert con.execute("SELECT COUNT(*) FROM prepocty WHERE user_id=99").fetchone()[0] == 0


def test_claim_rejects_a_dirty_connection_without_committing_caller_changes(con):
    job = enqueue(con, request(), now=NOW).job
    con.execute("INSERT INTO prepocty (user_id, den, pocet) VALUES (99, '2026-08-28', 1)")

    with pytest.raises(plan_jobs.DirtyConnectionError, match="clean connection"):
        plan_jobs.claim_next(con, "worker-a", now=NOW)

    assert con.in_transaction is True
    con.rollback()
    assert con.execute("SELECT COUNT(*) FROM prepocty WHERE user_id=99").fetchone()[0] == 0
    assert plan_jobs.status_for_key(con, job.job_key).state == "queued"
