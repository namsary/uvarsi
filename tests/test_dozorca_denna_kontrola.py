"""Denná kontrola letákov v dozorcovi: zadarmo, raz za deň, zverejní iba overenú zmenu."""
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
        "UVARSI_DENNA_KONTROLA_OD": "0",
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


def test_unchanged_leaflets_cost_nothing_and_run_once_a_day(server):
    first, calls = run(server)

    assert "--check-sources" in calls
    assert "--store" not in calls
    assert "refresh_blocek.py" not in calls
    assert "letáky sa nezmenili" in first.stdout
    assert (server["dir"] / ".denna_kontrola_zdrojov").read_text().strip() == TODAY

    _, calls = run(server)
    assert calls.count("--check-sources") == 1


def test_changed_leaflet_is_read_again_and_published_through_staging(server):
    result, calls = run(server, TEST_ZMENENE="tesco", TEST_PRIPRAVENE="kaufland")

    assert "zbierac_akcii.py --store tesco" in calls
    refresh = [line for line in calls.splitlines() if "refresh_blocek.py" in line]
    # Zverejnenie ide cez overený staging, nie cez prestavbu zo starých dát.
    assert refresh and "--active-current-verified" not in refresh[0]
    assert "zmenený leták je overený a zverejnený" in result.stdout


def test_free_kaufland_change_publishes_without_paid_collection(server):
    _, calls = run(server, TEST_PRIPRAVENE="kaufland")

    assert "--store" not in calls
    assert any("refresh_blocek.py" in line for line in calls.splitlines())


def test_failed_reading_keeps_published_offers_and_notifies(server):
    result, calls = run(server, TEST_ZMENENE="lidl", TEST_ZBER_RC="1")

    assert "zbierac_akcii.py --store lidl" in calls
    assert "refresh_blocek.py" not in calls
    assert "zverejnené akcie ostávajú" in result.stdout
    assert "zmena letáku" in server["notifications"].read_text(encoding="utf-8")


def test_check_waits_until_morning(server):
    _, calls = run(server, UVARSI_DENNA_KONTROLA_OD="24")

    assert "--check-sources" not in calls
