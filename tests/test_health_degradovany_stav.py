"""/api/health nesmie hlásiť zdravý stav, keď systém nie je v poriadku.

Regresia: odpoveď bola vždy 200 bez jediného súhrnného poľa, takže mŕtvy
plánovací worker alebo prázdny týždeň vyzerali ako "všetko v poriadku".
Stavový kód ostáva 200 zámerne: samopull.sh, recipe-engine-rollout.sh
a payment-smoke.py berú nie-200 ako zlyhanie nasadenia. Súhrn je v poli `stav`.
"""
import importlib
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]

ZIVA_FRONTA = {
    "queued": 0, "oldest_seconds": None, "worker_alive": True,
    "heartbeat_seconds": 3, "heartbeat_at": None, "last_ready": None,
    "failed": 0, "blocking_code": None,
}
PONUKA = {"obchod": "Kaufland"}


@pytest.fixture
def server(monkeypatch, tmp_path):
    monkeypatch.setenv("UVARSI_DB", str(tmp_path / "health.db"))
    monkeypatch.setenv("UVARSI_URL", "https://uvar.si")
    monkeypatch.setenv("UVARSI_VERSION_FILE", str(ROOT / "VERSION"))
    monkeypatch.setenv("UVARSI_STATIC", str(tmp_path / "static"))
    monkeypatch.setenv("UVARSI_SUPERVISOR_SUCCESS_STATE", str(tmp_path / "ok"))
    monkeypatch.syspath_prepend(str(ROOT / "app"))
    sys.modules.pop("server", None)
    module = importlib.import_module("server")
    yield module
    sys.modules.pop("server", None)


def _health(server, monkeypatch, *, fronta, ponuky):
    monkeypatch.setattr(server.plan_jobs, "health", lambda con, now: dict(fronta))
    monkeypatch.setattr(server, "offers_for_current_week", lambda con, st, d: list(ponuky))
    response = TestClient(server.app).get("/api/health")
    assert response.status_code == 200
    return response.json()


def test_health_je_ok_ked_worker_zije_a_su_data(server, monkeypatch):
    data = _health(server, monkeypatch, fronta=ZIVA_FRONTA, ponuky=[PONUKA])
    assert data["stav"] == "ok"
    assert data["problemy"] == []


def test_health_nehlasi_ok_ked_worker_nebezi(server, monkeypatch):
    fronta = {**ZIVA_FRONTA, "worker_alive": False, "heartbeat_seconds": None,
              "blocking_code": "worker_heartbeat_stale"}
    data = _health(server, monkeypatch, fronta=fronta, ponuky=[PONUKA])
    assert data["stav"] == "degradovane"
    assert "worker_nebezi" in data["problemy"]
    assert data["plan_queue"]["worker_alive"] is False


def test_health_nehlasi_ok_ked_fronta_je_zaseknuta(server, monkeypatch):
    fronta = {**ZIVA_FRONTA, "blocking_code": "queue_oldest_exceeded"}
    data = _health(server, monkeypatch, fronta=fronta, ponuky=[PONUKA])
    assert data["stav"] == "degradovane"
    assert "fronta_zaseknuta" in data["problemy"]


def test_health_nehlasi_ok_ked_chybaju_data_tyzdna(server, monkeypatch):
    data = _health(server, monkeypatch, fronta=ZIVA_FRONTA, ponuky=[])
    assert data["stav"] == "degradovane"
    assert "chybaju_data_tyzdna" in data["problemy"]
