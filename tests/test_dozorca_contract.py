import os
import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest

from app.landing_data import write_landing_data_atomic
from app.landing_data import landing_data_is_current


ROOT = Path(__file__).resolve().parents[1]
BASH = Path("C:/Program Files/Git/bin/bash.exe")


@pytest.fixture(autouse=True)
def offline_queue_health(monkeypatch, tmp_path):
    """Existing Dozorca cases do not need a real local FastAPI service."""
    monkeypatch.setenv("UVARSI_PLAN_QUEUE_HEALTH_URL", "http://127.0.0.1:9")
    fake_curl = tmp_path / "curl"
    fake_curl.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8", newline="\n")
    fake_curl.chmod(0o755)
    monkeypatch.setenv("UVARSI_CURL", bash_path(fake_curl))


def bash_path(path):
    return "/c" + path.as_posix()[2:]


def payload(week):
    return {
        "schema_version": 1,
        "generated_at": "2026-08-18T05:02:20+02:00",
        "week": week,
        "week_label": "17.–23. 8. 2026",
        "sources": [],
        "receipt": {
            "meals": [{"day": "PO", "name": "Test", "items": []}],
            "nakup_spolu": "1,00",
            "bezne": "2,00",
            "usetris": "1,00",
        },
    }


def test_dozorca_refreshes_stale_json_using_only_json_destination(tmp_path):
    landing_data = tmp_path / "landing_data.json"
    write_landing_data_atomic(landing_data, payload("2026-08-10"))
    calls = tmp_path / "calls.txt"
    fake_python = tmp_path / "python"
    fake_python.write_text(
        "#!/bin/sh\n"
        "if [ \"$1\" = \"-c\" ]; then\n"
        "  grep -q '\"week\":\"2026-08-10\"' \"$3\" && exit 1\n"
        "  grep -q '\"schema_version\":1' \"$3\" || exit 1\n"
        "  grep -q '\"meals\":\\[{' \"$3\" || exit 1\n"
        "  exit 0\n"
        "fi\n"
        f"printf '%s\\n' \"$*\" >> '{bash_path(calls)}'\n"
        "printf '{\"schema_version\":1,\"generated_at\":\"2026-08-18T05:02:20+02:00\",\"week\":\"2026-08-17\",\"week_label\":\"17.–23. 8. 2026\",\"sources\":[{\"store\":\"Lidl\",\"url\":\"https://letak.test/lidl\",\"valid_from\":\"2026-08-17\",\"valid_to\":\"2026-08-23\"}],\"receipt\":{\"meals\":[{\"day\":\"PO\",\"name\":\"Test\",\"items\":[]}],\"nakup_spolu\":\"1,00\",\"bezne\":\"2,00\",\"usetris\":\"1,00\"}}' > \"$3\"\n"
        "exit 0\n",
        encoding="utf-8",
    )
    fake_python.chmod(0o755)
    fake_sqlite = tmp_path / "sqlite3"
    fake_sqlite.write_text("#!/bin/sh\necho 30\n", encoding="utf-8")
    fake_sqlite.chmod(0o755)

    environment = os.environ | {
        "UVARSI_DIR": bash_path(tmp_path),
        "UVARSI_LANDING_DATA": bash_path(landing_data),
        "UVARSI_PY": bash_path(fake_python),
        "UVARSI_TODAY": "2026-08-18",
        "UVARSI_DOZORCA_LOCKED": "1",
        "PATH": f"{bash_path(tmp_path)}:/usr/bin",
    }
    result = subprocess.run(
        [str(BASH), bash_path(ROOT / "hetzner" / "dozorca.sh")],
        cwd=str(ROOT),
        env=environment,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0
    assert calls.read_text(encoding="utf-8") == f"-u refresh_blocek.py {bash_path(landing_data)}\n"
    assert "index.html" not in calls.read_text(encoding="utf-8")
    assert landing_data_is_current(landing_data, date(2026, 8, 18))


def run_dozorca(tmp_path, landing_data):
    return subprocess.run(
        [str(BASH), bash_path(ROOT / "hetzner" / "dozorca.sh")],
        cwd=str(ROOT),
        env=os.environ | {
            "UVARSI_DIR": bash_path(tmp_path),
            "UVARSI_LANDING_DATA": bash_path(landing_data),
            "UVARSI_PY": bash_path(tmp_path / "python"),
            "UVARSI_TODAY": "2026-08-18",
            "UVARSI_DOZORCA_LOCKED": "1",
            "PATH": f"{bash_path(tmp_path)}:/usr/bin",
        },
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
    )


def test_dozorca_stops_retrying_a_structural_failure_until_the_data_changes(tmp_path):
    landing_data = tmp_path / "landing_data.json"
    write_landing_data_atomic(landing_data, payload("2026-08-10"))
    calls = tmp_path / "calls.txt"
    fake_python = tmp_path / "python"
    fake_python.write_text(
        "#!/bin/sh\n"
        "if [ \"$1\" = \"-c\" ]; then exit 1; fi\n"
        f"printf '%s\\n' \"$*\" >> '{bash_path(calls)}'\n"
        "exit 3\n",
        encoding="utf-8",
    )
    fake_python.chmod(0o755)
    fake_sqlite = tmp_path / "sqlite3"
    fake_sqlite.write_text("#!/bin/sh\necho 431\n", encoding="utf-8")
    fake_sqlite.chmod(0o755)

    first = run_dozorca(tmp_path, landing_data)
    second = run_dozorca(tmp_path, landing_data)

    assert first.returncode == 3
    assert second.returncode == 3
    assert "ŠTRUKTURÁLNA" in first.stdout
    assert calls.read_text(encoding="utf-8").count("refresh_blocek.py") == 1
    assert (tmp_path / ".dozorca_state").read_text(encoding="utf-8").split() == ["2026-08-18", "0", "431"]


def test_dozorca_keeps_retrying_a_transient_failure(tmp_path):
    landing_data = tmp_path / "landing_data.json"
    write_landing_data_atomic(landing_data, payload("2026-08-10"))
    calls = tmp_path / "calls.txt"
    fake_python = tmp_path / "python"
    fake_python.write_text(
        "#!/bin/sh\n"
        "if [ \"$1\" = \"-c\" ]; then exit 1; fi\n"
        f"printf '%s\\n' \"$*\" >> '{bash_path(calls)}'\n"
        "exit 1\n",
        encoding="utf-8",
    )
    fake_python.chmod(0o755)
    fake_sqlite = tmp_path / "sqlite3"
    fake_sqlite.write_text("#!/bin/sh\necho 431\n", encoding="utf-8")
    fake_sqlite.chmod(0o755)

    first = run_dozorca(tmp_path, landing_data)
    second = run_dozorca(tmp_path, landing_data)

    assert (first.returncode, second.returncode) == (1, 1)
    assert calls.read_text(encoding="utf-8").count("refresh_blocek.py") == 2
    assert (tmp_path / ".dozorca_state").read_text(encoding="utf-8").split() == ["2026-08-18", "2", "-"]


def _credit_exhausted_environment(tmp_path):
    """Presne to, čo produkcia hlásila 24. 8. 2026: kód 3 + značka o kredite."""
    landing_data = tmp_path / "landing_data.json"
    write_landing_data_atomic(landing_data, payload("2026-08-10"))
    calls = tmp_path / "calls.txt"
    notifications = tmp_path / "notify.txt"
    fake_python = tmp_path / "python"
    fake_python.write_text(
        "#!/bin/sh\n"
        "if [ \"$1\" = \"-c\" ]; then exit 1; fi\n"
        f"printf '%s\\n' \"$*\" >> '{bash_path(calls)}'\n"
        "echo 'KREDIT_VYCERPANY: na účte došiel kredit' >&2\n"
        "exit 3\n",
        encoding="utf-8",
    )
    fake_python.chmod(0o755)
    fake_sqlite = tmp_path / "sqlite3"
    fake_sqlite.write_text("#!/bin/sh\necho 431\n", encoding="utf-8")
    fake_sqlite.chmod(0o755)
    fake_curl = tmp_path / "curl"
    fake_curl.write_text(
        "#!/bin/sh\n"
        "case \"$*\" in -fsS*) exit 0 ;; esac\n"
        f"printf '%s\\n' \"$*\" >> '{bash_path(notifications)}'\n",
        encoding="utf-8",
        newline="\n",
    )
    fake_curl.chmod(0o755)
    return landing_data, calls, notifications


def test_dozorca_stops_attempting_when_the_api_credit_ran_out(tmp_path):
    """Nulový kredit nie je dočasná chyba — hodinové pokusy nemajú čo skúšať."""
    landing_data, calls, _ = _credit_exhausted_environment(tmp_path)

    first = run_dozorca(tmp_path, landing_data)
    second = run_dozorca(tmp_path, landing_data)
    third = run_dozorca(tmp_path, landing_data)

    assert (first.returncode, second.returncode, third.returncode) == (3, 3, 3)
    assert calls.read_text(encoding="utf-8").count("refresh_blocek.py") == 1, (
        "po zistení nulového kreditu sa refresh nesmie spustiť znova"
    )
    assert (tmp_path / ".dozorca_state").read_text(encoding="utf-8").split() == [
        "2026-08-18", "0", "KREDIT"
    ]
    assert "KREDIT" in first.stdout


def test_dozorca_does_not_send_a_second_credit_notification(tmp_path):
    """Upozornenie posiela naklady.py práve raz — dozorca ho nesmie zdvojiť."""
    landing_data, _, notifications = _credit_exhausted_environment(tmp_path)

    for _ in range(3):
        run_dozorca(tmp_path, landing_data)

    assert not notifications.exists(), (
        "dozorca pri nulovom kredite neposiela vlastnú notifikáciu"
    )


def test_dozorca_credit_block_does_not_leak_into_the_next_day(tmp_path):
    """Zajtra sa to skúsi znova — kredit mohol medzitým pribudnúť."""
    landing_data, calls, _ = _credit_exhausted_environment(tmp_path)
    run_dozorca(tmp_path, landing_data)

    subprocess.run(
        [str(BASH), bash_path(ROOT / "hetzner" / "dozorca.sh")],
        cwd=str(ROOT),
        env=os.environ | {
            "UVARSI_DIR": bash_path(tmp_path),
            "UVARSI_LANDING_DATA": bash_path(landing_data),
            "UVARSI_PY": bash_path(tmp_path / "python"),
            "UVARSI_TODAY": "2026-08-19",
            "UVARSI_DOZORCA_LOCKED": "1",
            "PATH": f"{bash_path(tmp_path)}:/usr/bin",
        },
        text=True, encoding="utf-8", errors="replace",
        capture_output=True, check=False,
    )

    assert calls.read_text(encoding="utf-8").count("refresh_blocek.py") == 2


def test_dozorca_keeps_warming_plans_even_when_weekly_data_is_already_current(tmp_path):
    (tmp_path / "app").mkdir()
    landing_data = tmp_path / "landing_data.json"
    write_landing_data_atomic(landing_data, payload("2026-08-17"))
    calls = tmp_path / "calls.txt"
    fake_python = tmp_path / "python"
    fake_python.write_text(
        "#!/bin/sh\n"
        "if [ \"$1\" = \"-c\" ]; then exit 0; fi\n"
        f"printf '%s\\n' \"$*\" >> '{bash_path(calls)}'\n"
        "exit 0\n",
        encoding="utf-8",
    )
    fake_python.chmod(0o755)
    fake_sqlite = tmp_path / "sqlite3"
    fake_sqlite.write_text(
        "#!/bin/sh\n"
        "case \"$*\" in\n"
        "  *\"SELECT COUNT(*) FROM (\"*) echo 0 ;;\n"
        "  *) echo 30 ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    fake_sqlite.chmod(0o755)

    environment = os.environ | {
        "UVARSI_DIR": bash_path(tmp_path),
        "UVARSI_LANDING_DATA": bash_path(landing_data),
        "UVARSI_PY": bash_path(fake_python),
        "UVARSI_TODAY": "2026-08-18",
        "UVARSI_DOZORCA_LOCKED": "1",
        "PATH": f"{bash_path(tmp_path)}:/usr/bin",
    }
    result = subprocess.run(
        [str(BASH), bash_path(ROOT / "hetzner" / "dozorca.sh")],
        cwd=str(ROOT),
        env=environment,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0
    recorded = calls.read_text(encoding="utf-8")
    assert "refresh_blocek.py" not in recorded
    assert recorded.count("predpocet.py --zahrej") == 1, (
        "predpočet sa musí skúsiť pri každom behu dozorcu; inak sa po jednom "
        "zlyhaní už do konca týždňa nezotaví"
    )


def test_dozorca_queue_handoff_failure_does_not_break_hourly_recovery(tmp_path):
    (tmp_path / "app").mkdir()
    landing_data = tmp_path / "landing_data.json"
    write_landing_data_atomic(landing_data, payload("2026-08-17"))
    calls = tmp_path / "calls.txt"
    fake_python = tmp_path / "python"
    fake_python.write_text(
        "#!/bin/sh\n"
        "if [ \"$1\" = \"-c\" ]; then exit 0; fi\n"
        f"printf '%s\\n' \"$*\" >> '{bash_path(calls)}'\n"
        "case \"$2\" in predpocet.py) exit 7 ;; esac\n"
        "exit 0\n",
        encoding="utf-8",
    )
    fake_python.chmod(0o755)
    fake_sqlite = tmp_path / "sqlite3"
    fake_sqlite.write_text(
        "#!/bin/sh\n"
        "case \"$*\" in\n"
        "  *\"SELECT COUNT(*) FROM (\"*) echo 0 ;;\n"
        "  *) echo 30 ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    fake_sqlite.chmod(0o755)

    result = run_dozorca(tmp_path, landing_data)

    assert result.returncode == 0
    assert calls.read_text(encoding="utf-8").count("predpocet.py --zahrej") == 1
    assert "zaradiť" in result.stdout


@pytest.mark.parametrize(("pocet", "chybajuce_obchody"), [(29, 0), (30, 1)])
def test_dozorca_nezohrieva_plan_nad_neuplnymi_ponukami(
        tmp_path, pocet, chybajuce_obchody):
    (tmp_path / "app").mkdir()
    landing_data = tmp_path / "landing_data.json"
    write_landing_data_atomic(landing_data, payload("2026-08-17"))
    calls = tmp_path / "calls.txt"
    fake_python = tmp_path / "python"
    fake_python.write_text(
        "#!/bin/sh\n"
        "if [ \"$1\" = \"-c\" ]; then exit 0; fi\n"
        f"printf '%s\\n' \"$*\" >> '{bash_path(calls)}'\n"
        "exit 0\n",
        encoding="utf-8",
    )
    fake_python.chmod(0o755)
    fake_sqlite = tmp_path / "sqlite3"
    fake_sqlite.write_text(
        "#!/bin/sh\n"
        "case \"$*\" in\n"
        f"  *\"SELECT COUNT(*) FROM (\"*) echo {chybajuce_obchody} ;;\n"
        f"  *) echo {pocet} ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    fake_sqlite.chmod(0o755)

    result = subprocess.run(
        [str(BASH), bash_path(ROOT / "hetzner" / "dozorca.sh")],
        cwd=str(ROOT),
        env=os.environ | {
            "UVARSI_DIR": bash_path(tmp_path),
            "UVARSI_LANDING_DATA": bash_path(landing_data),
            "UVARSI_PY": bash_path(fake_python),
            "UVARSI_TODAY": "2026-08-18",
            "UVARSI_DOZORCA_LOCKED": "1",
            "PATH": f"{bash_path(tmp_path)}:/usr/bin",
        },
        text=True, encoding="utf-8", errors="replace",
        capture_output=True, check=False,
    )

    assert result.returncode == 0
    recorded = calls.read_text(encoding="utf-8") if calls.exists() else ""
    assert "predpocet.py" not in recorded


def test_dozorca_pri_stalom_landingu_najprv_obnovi_blocek_a_az_potom_zohrieva(tmp_path):
    (tmp_path / "app").mkdir()
    landing_data = tmp_path / "landing_data.json"
    write_landing_data_atomic(landing_data, payload("2026-08-10"))
    calls = tmp_path / "calls.txt"
    fake_python = tmp_path / "python"
    fake_python.write_text(
        "#!/bin/sh\n"
        "if [ \"$1\" = \"-c\" ]; then\n"
        "  grep -q '\"week\":\"2026-08-17\"' \"$3\" && exit 0\n"
        "  exit 1\n"
        "fi\n"
        f"printf '%s\\n' \"$*\" >> '{bash_path(calls)}'\n"
        "if [ \"$2\" = \"refresh_blocek.py\" ]; then\n"
        "  printf '{\"schema_version\":1,\"generated_at\":\"2026-08-18T05:02:20+02:00\",\"week\":\"2026-08-17\",\"week_label\":\"17.–23. 8. 2026\",\"sources\":[],\"receipt\":{\"meals\":[{\"day\":\"PO\",\"name\":\"Test\",\"items\":[]}],\"nakup_spolu\":\"1,00\",\"bezne\":\"2,00\",\"usetris\":\"1,00\"}}' > \"$3\"\n"
        "fi\n"
        "exit 0\n",
        encoding="utf-8",
    )
    fake_python.chmod(0o755)
    fake_sqlite = tmp_path / "sqlite3"
    fake_sqlite.write_text(
        "#!/bin/sh\ncase \"$*\" in *\"SELECT COUNT(*) FROM (\"*) echo 0 ;; *) echo 30 ;; esac\n",
        encoding="utf-8",
    )
    fake_sqlite.chmod(0o755)

    result = subprocess.run(
        [str(BASH), bash_path(ROOT / "hetzner" / "dozorca.sh")],
        cwd=str(ROOT),
        env=os.environ | {
            "UVARSI_DIR": bash_path(tmp_path),
            "UVARSI_LANDING_DATA": bash_path(landing_data),
            "UVARSI_PY": bash_path(fake_python),
            "UVARSI_TODAY": "2026-08-18",
            "UVARSI_DOZORCA_LOCKED": "1",
            "PATH": f"{bash_path(tmp_path)}:/usr/bin",
        },
        text=True, encoding="utf-8", errors="replace",
        capture_output=True, check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert calls.read_text(encoding="utf-8").splitlines() == [
        f"-u refresh_blocek.py {bash_path(landing_data)}",
        "-u predpocet.py --zahrej",
    ]


def test_dozorca_pri_obsadenom_zamku_druhy_beh_skusene_preskoci(tmp_path):
    fake_flock = tmp_path / "flock"
    fake_flock.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    fake_flock.chmod(0o755)
    fake_python = tmp_path / "python"
    fake_python.write_text("#!/bin/sh\nexit 99\n", encoding="utf-8")
    fake_python.chmod(0o755)

    result = subprocess.run(
        [str(BASH), bash_path(ROOT / "hetzner" / "dozorca.sh")],
        cwd=str(ROOT),
        env=os.environ | {
            "UVARSI_DIR": bash_path(tmp_path),
            "UVARSI_PY": bash_path(fake_python),
            "PATH": f"{bash_path(tmp_path)}:/usr/bin",
        },
        text=True, encoding="utf-8", errors="replace",
        capture_output=True, check=False,
    )

    assert result.returncode == 0
    assert "predchádzajúci beh ešte pracuje" in result.stdout


def test_dozorca_nepredstiera_obsadeny_zamok_ked_lock_subor_nemoze_otvorit(tmp_path):
    not_a_directory = tmp_path / "subor"
    not_a_directory.write_text("x", encoding="utf-8")
    fake_flock = tmp_path / "flock"
    fake_flock.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fake_flock.chmod(0o755)

    result = subprocess.run(
        [str(BASH), bash_path(ROOT / "hetzner" / "dozorca.sh")],
        cwd=str(ROOT),
        env=os.environ | {
            "UVARSI_DIR": bash_path(not_a_directory),
            "PATH": f"{bash_path(tmp_path)}:/usr/bin",
        },
        text=True, encoding="utf-8", errors="replace",
        capture_output=True, check=False,
    )

    assert result.returncode != 0
    assert "zámok sa nedá vytvoriť" in result.stdout


def test_dozorca_alerts_once_for_a_stalled_plan_queue_and_clears_after_recovery(tmp_path):
    """A repeated hourly check must not resend a queue alert after the first one."""
    (tmp_path / "app").mkdir()
    landing_data = tmp_path / "landing_data.json"
    write_landing_data_atomic(landing_data, payload("2026-08-17"))
    notifications = tmp_path / "notifications.txt"
    fake_python = tmp_path / "python"
    fake_python.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8", newline="\n")
    fake_python.chmod(0o755)
    fake_sqlite = tmp_path / "sqlite3"
    fake_sqlite.write_text(
        "#!/bin/sh\ncase \"$*\" in *\"SELECT COUNT(*) FROM (\"*) echo 0 ;; *) echo 30 ;; esac\n",
        encoding="utf-8",
        newline="\n",
    )
    fake_sqlite.chmod(0o755)
    fake_curl = tmp_path / "curl"
    fake_curl.write_text(
        "#!/bin/sh\n"
        "case \"$*\" in\n"
        "  -fsS*api/health*) printf '%s\\n' \"$UVARSI_TEST_HEALTH\" ;;\n"
        f"  *) printf '%s\\n' \"$*\" >> '{bash_path(notifications)}' ;;\n"
        "esac\n",
        encoding="utf-8",
        newline="\n",
    )
    fake_curl.chmod(0o755)

    environment = os.environ | {
        "UVARSI_DIR": bash_path(tmp_path),
        "UVARSI_LANDING_DATA": bash_path(landing_data),
        "UVARSI_PY": bash_path(fake_python),
        "UVARSI_TODAY": "2026-08-18",
        "UVARSI_DOZORCA_LOCKED": "1",
        "UVARSI_PLAN_QUEUE_HEALTH_URL": "http://queue.test/api/health",
        "UVARSI_CURL": bash_path(fake_curl),
        "UVARSI_HEALTH_PY": bash_path(Path(sys.executable)),
        "UVARSI_TEST_HEALTH": '{"plan_queue":{"queued":1,"oldest_seconds":181,"worker_alive":false,"heartbeat_seconds":61,"heartbeat_at":"2026-08-28T16:10:20+00:00","last_ready":null,"failed":0,"blocking_code":"worker_heartbeat_stale"}}',
        "PATH": f"{bash_path(tmp_path)}:/usr/bin",
    }

    for _ in range(2):
        result = subprocess.run(
            [str(BASH), bash_path(ROOT / "hetzner" / "dozorca.sh")],
            cwd=str(ROOT), env=environment, text=True, encoding="utf-8",
            errors="replace", capture_output=True, check=False,
        )
        assert result.returncode == 0

    marker = tmp_path / ".plan_queue_alert_state"
    assert marker.exists(), result.stdout + result.stderr
    assert notifications.read_text(encoding="utf-8").count("Uvar.si: fronta plánov") == 1

    original_marker = marker.read_text(encoding="utf-8")
    unknown_payloads = [
        '{"plan_queue":{"worker_alive":true}}',
        '{"plan_queue":{"queued":false,"oldest_seconds":null,"worker_alive":true,"heartbeat_seconds":1,"heartbeat_at":"2026-08-28T16:10:20+00:00","last_ready":null,"failed":0,"blocking_code":null}}',
        '{"plan_queue":{"queued":0,"oldest_seconds":12,"worker_alive":true,"heartbeat_seconds":null,"heartbeat_at":"2026-08-28T16:10:20+00:00","last_ready":null,"failed":0,"blocking_code":null}}',
    ]
    for health in unknown_payloads:
        environment["UVARSI_TEST_HEALTH"] = health
        unknown = subprocess.run(
            [str(BASH), bash_path(ROOT / "hetzner" / "dozorca.sh")],
            cwd=str(ROOT), env=environment, text=True, encoding="utf-8",
            errors="replace", capture_output=True, check=False,
        )
        assert unknown.returncode == 0
        assert marker.read_text(encoding="utf-8") == original_marker
        assert "UNKNOWN" in unknown.stdout
        assert notifications.read_text(encoding="utf-8").count("Uvar.si: fronta plánov") == 1

    environment["UVARSI_TEST_HEALTH"] = '{"plan_queue":{"queued":0,"oldest_seconds":null,"worker_alive":true,"heartbeat_seconds":1,"heartbeat_at":"2026-08-28T16:10:20+00:00","last_ready":null,"failed":0,"blocking_code":null}}'
    recovered = subprocess.run(
        [str(BASH), bash_path(ROOT / "hetzner" / "dozorca.sh")],
        cwd=str(ROOT), env=environment, text=True, encoding="utf-8",
        errors="replace", capture_output=True, check=False,
    )

    assert recovered.returncode == 0
    assert not marker.exists()
