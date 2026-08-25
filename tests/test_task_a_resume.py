"""Regresie Task A: invalidovaný plán musí zachovať svoj pôvod a správne CTA."""
import re
import sys
from pathlib import Path

from tests.test_server import (
    fake_anthropic,
    model_plan,
    plan_client,
    shared_plan_server,
)


def test_changed_pantry_plan_names_the_reason_and_resumes_through_pantry_endpoint(
        monkeypatch, tmp_path):
    server = shared_plan_server(monkeypatch, tmp_path, users=(1,), pantry={1: ["soľ"]})
    constructors = []
    monkeypatch.setitem(
        sys.modules, "anthropic",
        fake_anthropic(model_plan(pantry=["soľ"]), constructors),
    )
    client = plan_client(server, 1)
    assert client.post("/api/plan/zo-spajze").status_code == 200

    assert client.post("/api/spajza", json={"polozky": ["soľ", "vajcia"]}).status_code == 200
    response = client.get("/api/plan")

    assert response.status_code == 200
    assert response.json() == {
        "prazdny": True,
        "vyzaduje_akciu": True,
        "dovod": "spajza_zmenena",
        "obnovit_cez": "/api/plan/zo-spajze",
    }
    assert len(constructors) == 1, "GET ani invalidácia nesmú samy volať model"


def test_an_account_that_never_had_a_plan_gets_no_false_invalidation_reason(
        monkeypatch, tmp_path):
    server = shared_plan_server(monkeypatch, tmp_path, users=(1,), pantry={1: ["soľ"]})

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
