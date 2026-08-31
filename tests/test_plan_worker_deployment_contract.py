import datetime
from pathlib import Path

from fastapi.testclient import TestClient

from app import plan_jobs
from app.plan_jobs import JobRequest
from test_server import insert_hashed_session, load_server


ROOT = Path(__file__).resolve().parents[1]
SAMOPULL = (ROOT / "hetzner" / "samopull.sh").read_text(encoding="utf-8")
NASAD = (ROOT / "nasad.ps1").read_text(encoding="utf-8")


def queue_request():
    return JobRequest(
        job_key="regular:health:0",
        signature="health",
        variant=0,
        kind="regular",
        user_id=1,
        week="2026-08-24",
        priority=100,
        payload={},
        regeneration_limit=3,
        regeneration_day="2026-08-28",
    )


def test_health_and_cost_overview_report_truthful_queue_and_worker_state(monkeypatch, tmp_path):
    """Changing queue metrics or omitting either endpoint must break this contract."""
    monkeypatch.setenv("UVARSI_ADMIN_EMAILS", "owner@uvar.si")
    server = load_server(monkeypatch, tmp_path, [])
    now = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0)
    local_time = datetime.timezone(datetime.timedelta(hours=2))
    with server.db() as con:
        plan_jobs.enqueue(
            con,
            queue_request(),
            now=(now - datetime.timedelta(seconds=181)).astimezone(local_time),
        )
        plan_jobs.heartbeat(
            con,
            "worker-a",
            None,
            now=(now - datetime.timedelta(seconds=61)).astimezone(local_time),
        )
        con.execute("INSERT INTO pouzivatelia (id, email) VALUES (1, 'owner@uvar.si')")
        insert_hashed_session(server, con, "owner-session", 1)
        con.commit()

    client = TestClient(server.app)
    client.cookies.set(server.COOKIE, "owner-session")

    health = client.get("/api/health").json()["plan_queue"]
    costs = client.get("/api/naklady").json()["plan_queue"]

    for payload in (health, costs):
        assert payload["queued"] == 1
        assert payload["oldest_seconds"] >= 181
        assert payload["worker_alive"] is False
        assert payload["heartbeat_seconds"] >= 61
        assert payload["heartbeat_at"].endswith("+00:00")
        assert payload["last_ready"] is None
        assert payload["failed"] == 0
        assert payload["blocking_code"] == "worker_heartbeat_stale"


def test_release_installs_and_restarts_worker_without_touching_other_app():
    """A worker-less deployment or a unit coupled to taktik-mapa is unsafe."""
    unit = (ROOT / "hetzner" / "uvarsi-plan-worker.service").read_text(encoding="utf-8")

    assert "ExecStart=/opt/uvarsi/venv/bin/python -u plan_worker.py" in unit
    assert "Environment=TZ=Europe/Bratislava" in unit
    assert "Environment=UVARSI_URL=https://uvar.si" in unit
    assert unit.count("EnvironmentFile=-/opt/uvarsi/uvarsi-recipe-engine.env") == 1
    app_unit = (ROOT / "hetzner" / "uvarsi.service").read_text(encoding="utf-8")
    assert app_unit.count("EnvironmentFile=-/opt/uvarsi/uvarsi-recipe-engine.env") == 1
    assert "/opt/uvarsi/uvarsi.env" not in unit
    assert "/opt/uvarsi/uvarsi.env" not in app_unit
    assert "taktik-mapa" not in unit
    assert "uvarsi-plan-worker.service" in SAMOPULL
    assert "app/plan_shortlist.py" in SAMOPULL
    assert "systemctl restart uvarsi-plan-worker" in SAMOPULL
    assert "systemctl is-active --quiet uvarsi-plan-worker" in SAMOPULL
    assert "uvarsi-plan-worker.service" in NASAD
    assert "systemctl enable uvarsi-plan-worker" in NASAD
    assert "systemctl restart uvarsi-plan-worker" in NASAD
    assert "taktik-mapa" not in unit


def test_manual_deploy_checks_heartbeat_and_restores_prior_app_and_unit():
    """A live process without a fresh heartbeat must trigger a recoverable rollback."""
    assert 'queue.get("worker_alive") is True' in NASAD
    assert "uvarsi_snapshot /opt/uvarsi/releases/manual-predosle" in NASAD
    assert "uvarsi_restore /opt/uvarsi/releases/manual-predosle" in NASAD
    assert "uvarsi_wait_fresh_heartbeat" in NASAD
    assert "$script:LiveMutationStarted" in NASAD
    assert NASAD.count("VratPredosleUvarsi") >= 2
