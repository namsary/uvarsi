"""Čerstvá databáza musí appku rozbehať, nie ju zabiť.

Regresia: server.SCHEMA vytvára pouzivatelia/spajza/plany/tokeny/sedenia, ale NIE
`akcie`. db() potom volá migrate_akcie_schema(), ktorej `PRAGMA table_info(akcie)`
na chýbajúcej tabuľke vráti nula riadkov (bez chyby) a následné
`ALTER TABLE akcie ...` padne na `no such table`. db() je jediný vstupný bod,
takže na čerstvej databáze zlyhá KAŽDÝ endpoint — vrátane prihlásenia.

Tieto testy bežia proti prázdnej tmp_path databáze, presne ako čerstvý server.
"""
import datetime
import importlib
import sys
import time
from contextlib import closing
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def fresh_server(monkeypatch, tmp_path):
    """server.py nad úplne prázdnou databázou (žiadna tabuľka neexistuje)."""
    database = tmp_path / "cerstva.db"
    assert not database.exists()
    monkeypatch.setenv("UVARSI_DB", str(database))
    monkeypatch.setenv("UVARSI_URL", "https://uvar.si")
    monkeypatch.setenv("UVARSI_VERSION_FILE", str(ROOT / "VERSION"))
    monkeypatch.setenv("UVARSI_STATIC", str(tmp_path / "static"))
    monkeypatch.setenv(
        "UVARSI_SUPERVISOR_SUCCESS_STATE", str(tmp_path / "supervisor-success")
    )
    monkeypatch.syspath_prepend(str(ROOT / "app"))
    sys.modules.pop("server", None)
    module = importlib.import_module("server")
    yield module
    sys.modules.pop("server", None)


def test_db_creates_akcie_table_on_a_fresh_database(fresh_server):
    with closing(fresh_server.db()) as con:
        tabulky = {
            row[0]
            for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    assert "akcie" in tabulky, (
        "server.SCHEMA musí vytvoriť tabuľku akcie; bez nej migrate_akcie_schema() "
        "padne na `no such table` a s ňou spadne každý endpoint"
    )


def test_db_creates_akcie_indexes_on_a_fresh_database(fresh_server):
    with closing(fresh_server.db()) as con:
        indexy = {
            row[0]
            for row in con.execute("SELECT name FROM sqlite_master WHERE type='index'")
        }
    assert {"idx_akcie_tyzden", "idx_akcie_kat"} <= indexy, (
        "akcie sa čítajú podľa týždňa a kategórie; indexy musia byť rovnaké "
        "ako v zbierac_akcii.py, inak sa schémy rozídu"
    )


def test_akcie_table_has_every_column_the_writer_uses(fresh_server):
    """Zbierač zapisuje presne tieto stĺpce — server ich musí vedieť vytvoriť."""
    ocakavane = {
        "tyzden", "obchod", "nazov", "kategoria", "cena", "povodna", "zlava",
        "jednotka", "source_url", "source_page", "valid_from", "valid_to",
        "offer_key",
    }
    with closing(fresh_server.db()) as con:
        stlpce = {row[1] for row in con.execute("PRAGMA table_info(akcie)")}
    assert ocakavane <= stlpce, f"chýbajú stĺpce: {sorted(ocakavane - stlpce)}"


def test_pocet_akcii_returns_200_on_a_fresh_database(fresh_server):
    client = TestClient(fresh_server.app, raise_server_exceptions=False)
    response = client.get("/api/akcie/pocet")
    assert response.status_code == 200, (
        "na čerstvej databáze musí endpoint odpovedať 200 s nulou akcií, "
        f"nie {response.status_code}"
    )
    assert response.json()["pocet"] == 0


def test_login_endpoints_do_not_500_on_a_fresh_database(fresh_server):
    """db() je jediný vstupný bod — ak padne, nedá sa ani prihlásiť."""
    client = TestClient(fresh_server.app, raise_server_exceptions=False)
    for cesta in ("/api/me", "/api/health"):
        assert client.get(cesta).status_code == 200, f"{cesta} spadlo na prázdnej DB"
    overenie = client.post(
        "/api/auth/verify",
        json={"token": "nic"},
        headers={"Origin": "https://uvar.si"},
    )
    assert overenie.status_code == 400, (
        "neplatný token má byť 400 (nie 500 z chýbajúcej tabuľky)"
    )


def test_second_open_of_the_same_database_is_idempotent(fresh_server):
    """db() sa volá pri každej požiadavke; opakovaný beh nesmie padnúť."""
    with closing(fresh_server.db()):
        pass
    with closing(fresh_server.db()) as con:
        assert con.execute("SELECT COUNT(*) FROM akcie").fetchone()[0] == 0


# ------------------------------------------------------------------ /api/health
def test_health_reports_release_week_and_offer_count(fresh_server):
    client = TestClient(fresh_server.app, raise_server_exceptions=False)
    response = client.get("/api/health")
    assert response.status_code == 200
    data = response.json()
    assert data["vydanie"] == (ROOT / "VERSION").read_text(encoding="utf-8").strip(), (
        "health musí hlásiť config.release_id(); nasadenie tým odhalí čiastočne "
        "prenesený scp"
    )
    assert data["tyzden"] == fresh_server.monday()
    assert data["pocet"] == 0
    assert data["ponuky_podla_obchodu"] == {
        "Kaufland": 0,
        "Tesco": 0,
        "Lidl": 0,
    }


def test_health_makes_missing_supervisor_run_visible(fresh_server):
    data = TestClient(fresh_server.app).get("/api/health").json()

    assert data["dozorca"] == {
        "last_success_at": None,
        "age_seconds": None,
        "fresh": False,
    }


def test_supervisor_health_reports_a_recent_success(fresh_server):
    now = int(time.time())
    today = fresh_server.bratislava_day(
        datetime.datetime.fromtimestamp(now, datetime.timezone.utc)
    ).isoformat()
    Path(fresh_server.SUPERVISOR_SUCCESS_STATE).write_text(
        f"{today} {now - 60}\n", encoding="utf-8"
    )

    assert fresh_server._supervisor_health(now) == {
        "last_success_at": datetime.datetime.fromtimestamp(
            now - 60, datetime.timezone.utc
        ).isoformat(timespec="seconds"),
        "age_seconds": 60,
        "fresh": True,
    }


def test_supervisor_health_rejects_a_malformed_or_future_marker(fresh_server):
    marker = Path(fresh_server.SUPERVISOR_SUCCESS_STATE)
    marker.write_text("not-a-valid-marker\n", encoding="utf-8")
    unavailable = {
        "last_success_at": None,
        "age_seconds": None,
        "fresh": False,
    }
    assert fresh_server._supervisor_health(1_800_000_000) == unavailable

    marker.write_text("2027-01-15 1800000001\n", encoding="utf-8")
    assert fresh_server._supervisor_health(1_800_000_000) == unavailable


def test_supervisor_health_accepts_last_evening_run_during_scheduled_night_gap(
        fresh_server):
    zone = ZoneInfo("Europe/Bratislava")
    success = datetime.datetime(2026, 9, 18, 21, 0, tzinfo=zone)
    overnight = datetime.datetime(2026, 9, 19, 4, 30, tzinfo=zone)
    Path(fresh_server.SUPERVISOR_SUCCESS_STATE).write_text(
        f"2026-09-18 {int(success.timestamp())}\n", encoding="utf-8"
    )

    status = fresh_server._supervisor_health(int(overnight.timestamp()))

    assert status["age_seconds"] == 7 * 60 * 60 + 30 * 60
    assert status["fresh"] is True


def test_supervisor_health_requires_a_new_run_after_morning_schedule_starts(
        fresh_server):
    zone = ZoneInfo("Europe/Bratislava")
    success = datetime.datetime(2026, 9, 18, 21, 0, tzinfo=zone)
    morning = datetime.datetime(2026, 9, 19, 6, 1, tzinfo=zone)
    Path(fresh_server.SUPERVISOR_SUCCESS_STATE).write_text(
        f"2026-09-18 {int(success.timestamp())}\n", encoding="utf-8"
    )

    assert fresh_server._supervisor_health(int(morning.timestamp()))["fresh"] is False


def test_health_offer_count_matches_pocet_akcii(fresh_server):
    client = TestClient(fresh_server.app, raise_server_exceptions=False)
    assert client.get("/api/health").json()["pocet"] == (
        client.get("/api/akcie/pocet").json()["pocet"]
    )


def test_health_does_not_leak_secrets(fresh_server):
    client = TestClient(fresh_server.app, raise_server_exceptions=False)
    telo = client.get("/api/health").text
    for zakazane in ("API_KEY", "sk-ant", "re_"):
        assert zakazane not in telo, f"health nesmie vypísať {zakazane}"
