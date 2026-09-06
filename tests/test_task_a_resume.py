"""Regresie Task A: invalidovaný plán musí zachovať svoj pôvod a správne CTA."""
import re
from pathlib import Path

from tests.test_deterministic_plan_api import _server as deterministic_server
from tests.test_server import plan_client


def test_changed_pantry_plan_names_the_reason_and_resumes_through_pantry_endpoint(
        monkeypatch, tmp_path):
    server = deterministic_server(
        monkeypatch, tmp_path, pantry=(("ryža", 950, "g"),),
    )
    client = plan_client(server, 1)
    assert client.post("/api/plan/zo-spajze").status_code == 200

    assert client.post("/api/spajza", json={"polozky": ["ryža", "vajcia"]}).status_code == 200
    response = client.get("/api/plan")

    assert response.status_code == 200
    assert response.json() == {
        "prazdny": True,
        "vyzaduje_akciu": True,
        "dovod": "spajza_zmenena",
        "obnovit_cez": "/api/plan/zo-spajze",
    }
    with server.db() as con:
        assert con.execute("SELECT COUNT(*) FROM plan_jobs").fetchone()[0] == 0
        assert con.execute("SELECT COUNT(*) FROM naklady").fetchone()[0] == 0


def test_an_account_that_never_had_a_plan_gets_no_false_invalidation_reason(
        monkeypatch, tmp_path):
    server = deterministic_server(
        monkeypatch, tmp_path, pantry=(("ryža", 950, "g"),),
    )

    response = plan_client(server, 1).get("/api/plan")

    assert response.status_code == 200
    assert response.json() == {"prazdny": True}


def _function(html, name):
    match = re.search(rf"(?:async )?function {name}\([^)]*\) \{{.*?\n\}}", html, re.S)
    assert match, f"chýba funkcia {name}"
    return match.group(0)


def test_frontend_uses_the_server_resume_marker_instead_of_guessing_from_prazdny():
    html = Path("app/static/app.html").read_text(encoding="utf-8")
    refresh = _function(html, "refreshPlanAfterPantrySave")
    resume = _function(html, "resumeInvalidatedPlan")

    assert "obnovit_cez" in refresh
    assert "PLAN_RESUME_ENDPOINT" in refresh
    assert "plan.prazdny" not in refresh or "plan.obnovit_cez" in refresh
    assert "navrhniZoSpajze()" in resume
    assert "PLAN_RESUME_ENDPOINT === '/api/plan/zo-spajze'" in resume
    assert "/api/plan/zo-spajze" in html
    assert "nacitajPlan(true)" in resume, "verzia algoritmu sa obnovuje bežným explicitným POST"


def test_invalidated_plan_cta_calls_the_resume_dispatcher_not_generic_generation():
    html = Path("app/static/app.html").read_text(encoding="utf-8")
    plan_view = _function(html, "vPlan")

    assert "resumeInvalidatedPlan()" in plan_view
    invalidated = plan_view.split("if (PLAN_NEEDS_REGEN)", 1)[1].split("return;", 1)[0]
    assert "nacitajPlan(true)" not in invalidated
