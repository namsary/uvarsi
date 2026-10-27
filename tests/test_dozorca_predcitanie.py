"""Predčítanie letákov v dozorcovi: beží v ustálenom stave, raz za interval,
a pred zajtrajším chýbajúcim letákom upozorní majiteľa raz za deň."""
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BASH = Path(os.environ.get("UVARSI_TEST_BASH") or shutil.which("bash") or "C:/Program Files/Git/bin/bash.exe")
TODAY = "2026-08-19"

pytestmark = pytest.mark.skipif(os.name == "nt", reason="testy dozorcu bežia v bashi na Linuxe")


@pytest.fixture
def server(tmp_path):
    (tmp_path / "app").mkdir()
    calls = tmp_path / "calls.txt"
    fake_python = tmp_path / "python"
    fake_python.write_text(
        "#!/bin/sh\n"
        "if [ \"$1\" = \"-c\" ]; then exit 0; fi\n"  # bloček je aktuálny
        f"printf '%s\\n' \"$*\" >> '{calls}'\n"
        "case \"$*\" in\n"
        "  *--check-sources*)\n"
        "    echo \"ZMENENE_ZDROJE=${TEST_ZMENENE:-}\"\n"
        "    echo \"PRIPRAVENE_ZDROJE=${TEST_PRIPRAVENE:-}\"\n"
        "    exit 0 ;;\n"
        "  *--prefetch*)\n"
        "    echo \"PRECITANE_LETAKY=${TEST_PRECITANE:-}\"\n"
        "    echo \"NEPRIPRAVENE_ZAJTRA=${TEST_NEPRIPRAVENE:-}\"\n"
        "    exit \"${TEST_PREFETCH_RC:-0}\" ;;\n"
        "  *zbierac_akcii.py*--store*) exit \"${TEST_ZBER_RC:-0}\" ;;\n"
        "esac\n"
        "exit 0\n",
        encoding="utf-8",
    )
    fake_python.chmod(0o755)
    fake_sqlite = tmp_path / "sqlite3"
    fake_sqlite.write_text(
        "#!/bin/sh\ncase \"$*\" in *\"SELECT COUNT(*) FROM (\"*) echo 0 ;; *) echo 60 ;; esac\n",
        encoding="utf-8",
    )
    fake_sqlite.chmod(0o755)
    notifications = tmp_path / "notifications.txt"
    fake_curl = tmp_path / "curl"
    fake_curl.write_text(
        "#!/bin/sh\n"
        f"case \"$*\" in *ntfy.sh*) printf '%s\\n' \"$*\" >> '{notifications}' ;; esac\n"
        "exit 0\n",
        encoding="utf-8",
    )
    fake_curl.chmod(0o755)
    environment = os.environ | {
        "UVARSI_DIR": str(tmp_path),
        "UVARSI_LANDING_DATA": str(tmp_path / "landing_data.json"),
        "UVARSI_PUBLIC_INDEX": str(tmp_path / "index.html"),
        "UVARSI_PY": str(fake_python),
        "UVARSI_CURL": str(fake_curl),
        "UVARSI_TODAY": TODAY,
        "UVARSI_DOZORCA_LOCKED": "1",
        "UVARSI_TEST_BRIDGE_PREFLIGHT": "/usr/bin/true",
        "UVARSI_DENNA_KONTROLA": "0",
        "UVARSI_PREDCITANIE_ALERT_OD": "0",
        "UVARSI_HEALTH_ATTEMPTS": "1",
        "UVARSI_HEALTH_RETRY_DELAY_SECONDS": "0",
        "PATH": f"{tmp_path}:/usr/bin:/bin",
    }
    return {"dir": tmp_path, "calls": calls, "notifications": notifications, "env": environment}


def run(server, **extra):
    result = subprocess.run(
        [str(BASH), str(ROOT / "hetzner" / "dozorca.sh")],
        cwd=str(ROOT),
        env=server["env"] | extra,
        text=True,
        capture_output=True,
        check=False,
    )
    calls = server["calls"].read_text(encoding="utf-8") if server["calls"].exists() else ""
    return result, calls


def notifications(server):
    """Iba upozornenia o letákoch; zvyšok dozorcu tu nie je predmetom testu."""
    path = server["notifications"]
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    return "".join(line + "\n" for line in text.splitlines() if "leták" in line)


def test_prefetch_runs_in_steady_state_and_then_waits_for_interval(server):
    result, calls = run(server, TEST_PRECITANE="lidl")

    assert "--prefetch" in calls
    assert "nový leták je prečítaný vopred — lidl" in result.stdout
    assert notifications(server) == ""

    _, calls = run(server)
    assert calls.count("--prefetch") == 1


def test_owner_is_warned_once_a_day_when_tomorrows_leaflet_is_missing(server):
    result, _ = run(server, TEST_NEPRIPRAVENE="lidl:neohlaseny")

    assert "zajtrajší leták nie je pripravený" in result.stdout
    assert "nezverejnil" in notifications(server)

    run(server, TEST_NEPRIPRAVENE="lidl:neohlaseny", UVARSI_PREDCITANIE_INTERVAL="0")
    assert notifications(server).count("zajtrajší leták") == 1


def test_warning_waits_for_the_evening(server):
    run(server, TEST_NEPRIPRAVENE="tesco:neprecitany", UVARSI_PREDCITANIE_ALERT_OD="24")

    assert notifications(server) == ""


def test_failed_prefetch_is_retried_next_hour(server):
    run(server, TEST_PREFETCH_RC="1")
    _, calls = run(server)

    assert calls.count("--prefetch") == 2


def test_prefetch_can_be_switched_off(server):
    _, calls = run(server, UVARSI_PREDCITANIE="0")

    assert "--prefetch" not in calls
