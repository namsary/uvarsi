import json
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
STRUCTURAL_SOURCE_IDENTITY = ":".join(["a" * 64] * 3)


@pytest.fixture(autouse=True)
def _successful_bridge_preflight_for_supervisor_contracts(monkeypatch):
    monkeypatch.setenv("UVARSI_TEST_BRIDGE_PREFLIGHT", "/usr/bin/true")


def health_json(plan_queue=None):
    return json.dumps(
        {
            "plan_queue": plan_queue or {
                "queued": 0,
                "oldest_seconds": None,
                "worker_alive": True,
                "heartbeat_seconds": 1,
                "heartbeat_at": "2026-08-28T16:10:20+00:00",
                "last_ready": None,
                "failed": 0,
                "blocking_code": None,
            },
            "recipe_engine": {
                "mode": "off",
                "library_version": 1,
                "active_templates": 60,
                "coverage": {
                    "standard": 60,
                    "high_protein": 35,
                    "vegetarian": 37,
                    "vegan": 23,
                },
                "last_shadow": None,
                "p95_ms": None,
                "ready": True,
                "blockers": [],
            },
        },
        separators=(",", ":"),
    )


def test_dozorca_reads_health_json_even_when_readiness_http_status_is_503():
    """503 znamená nepripravenosť produktu, nie nedostupné diagnostické JSON."""
    source = (ROOT / "hetzner" / "dozorca.sh").read_text(encoding="utf-8")
    assert "nacitaj_health()" in source
    assert '"$CURL" -sS --max-time 1 "$PLAN_QUEUE_HEALTH_URL"' in source
    assert '"$CURL" -fsS --max-time 1 "$PLAN_QUEUE_HEALTH_URL"' not in source


@pytest.fixture(autouse=True)
def offline_queue_health(monkeypatch, tmp_path):
    """Existing Dozorca cases do not need a real local FastAPI service."""
    monkeypatch.setenv("UVARSI_PLAN_QUEUE_HEALTH_URL", "http://health.test/api/health")
    monkeypatch.setenv("UVARSI_HEALTH_PY", bash_path(Path(sys.executable)))
    monkeypatch.setenv("UVARSI_TEST_HEALTH", health_json())
    fake_curl = tmp_path / "curl"
    fake_curl.write_text(
        "#!/bin/sh\n"
        "case \"$*\" in\n"
        "  *api/health*) printf '%s\\n' \"$UVARSI_TEST_HEALTH\" ;;\n"
        "esac\n"
        "exit 0\n",
        encoding="utf-8",
        newline="\n",
    )
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


def run_dozorca(tmp_path, landing_data, **extra_env):
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
        } | {key: str(value) for key, value in extra_env.items()},
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
    )


def test_dozorca_uses_bratislava_day_after_local_midnight_on_utc_server(tmp_path):
    (tmp_path / "app").mkdir()
    landing_data = tmp_path / "landing_data.json"
    write_landing_data_atomic(landing_data, payload("2026-08-31"))
    calls = tmp_path / "calls.txt"
    child_timezone = tmp_path / "child-timezone.txt"

    fake_date = tmp_path / "date"
    fake_date.write_text(
        "#!/bin/sh\n"
        "case \"$1\" in\n"
        "  +%F) [ \"$TZ\" = \"Europe/Bratislava\" ] && echo 2026-09-07 || echo 2026-09-06 ;;\n"
        "  *) echo '2026-09-07 00:30:00' ;;\n"
        "esac\n",
        encoding="utf-8",
        newline="\n",
    )
    fake_date.chmod(0o755)

    fake_python = tmp_path / "python"
    fake_python.write_text(
        "#!/bin/sh\n"
        "if [ \"$1\" = \"-c\" ]; then\n"
        "  case \"$2\" in *'from datetime import date'*) echo 2026-09-07; exit 0 ;; esac\n"
        "  exit 1\n"
        "fi\n"
        f"printf '%s\\n' \"$TZ\" > '{bash_path(child_timezone)}'\n"
        f"printf '%s\\n' \"$*\" >> '{bash_path(calls)}'\n"
        "exit 1\n",
        encoding="utf-8",
        newline="\n",
    )
    fake_python.chmod(0o755)

    fake_sqlite = tmp_path / "sqlite3"
    fake_sqlite.write_text(
        "#!/bin/sh\n"
        "case \"$*\" in\n"
        "  *'SELECT lower(v.o)'*2026-09-07*) echo lidl ;;\n"
        "  *'SELECT COUNT(*) FROM ('*2026-09-07*) echo 1 ;;\n"
        "  *'SELECT COUNT(*) FROM ('*) echo 0 ;;\n"
        "  *MAX*) echo 0 ;;\n"
        "  *) echo 466 ;;\n"
        "esac\n",
        encoding="utf-8",
        newline="\n",
    )
    fake_sqlite.chmod(0o755)

    result = subprocess.run(
        [str(BASH), bash_path(ROOT / "hetzner" / "dozorca.sh")],
        cwd=str(ROOT),
        env=os.environ | {
            "UVARSI_DIR": bash_path(tmp_path),
            "UVARSI_LANDING_DATA": bash_path(landing_data),
            "UVARSI_PY": bash_path(fake_python),
            "UVARSI_DATE": bash_path(fake_date),
            "UVARSI_DOZORCA_LOCKED": "1",
            "TZ": "UTC",
            "PATH": f"{bash_path(tmp_path)}:/usr/bin",
        },
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
    )

    assert "zbierac_akcii.py --store lidl" in calls.read_text(encoding="utf-8")
    assert child_timezone.read_text(encoding="utf-8").strip() == "Europe/Bratislava"


def test_dozorca_stops_retrying_a_structural_failure_until_the_data_changes(tmp_path):
    landing_data = tmp_path / "landing_data.json"
    write_landing_data_atomic(landing_data, payload("2026-08-10"))
    calls = tmp_path / "calls.txt"
    (tmp_path / ".nasadene_sha").write_text("a" * 40 + "\n", encoding="ascii")
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
    fake_sqlite.write_text(
        "#!/bin/sh\n"
        "case \"$*\" in\n"
        f"  *source_fingerprint*) echo '{STRUCTURAL_SOURCE_IDENTITY}' ;;\n"
        "  *) echo 431 ;;\n"
        "esac\n",
        encoding="utf-8",
        newline="\n",
    )
    fake_sqlite.chmod(0o755)

    first = run_dozorca(tmp_path, landing_data)
    second = run_dozorca(tmp_path, landing_data)

    assert first.returncode == 3
    assert second.returncode == 3
    assert "ŠTRUKTURÁLNA" in first.stdout
    assert calls.read_text(encoding="utf-8").count("refresh_blocek.py") == 1
    assert (tmp_path / ".dozorca_state").read_text(encoding="utf-8").split() == [
        "2026-08-18", "0", "431:431:431", STRUCTURAL_SOURCE_IDENTITY, "a" * 40,
    ]


def test_structural_block_reports_stored_collection_failure_detail_once(
    monkeypatch, tmp_path
):
    landing_data = tmp_path / "landing_data.json"
    write_landing_data_atomic(landing_data, payload("2026-08-10"))
    notifications = tmp_path / "notifications.txt"
    (tmp_path / ".nasadene_sha").write_text("a" * 40 + "\n", encoding="ascii")

    fake_python = tmp_path / "python"
    fake_python.write_text(
        "#!/bin/sh\nif [ \"$1\" = \"-c\" ]; then exit 1; fi\nexit 3\n",
        encoding="utf-8",
        newline="\n",
    )
    fake_python.chmod(0o755)

    fake_sqlite = tmp_path / "sqlite3"
    fake_sqlite.write_text(
        "#!/bin/sh\n"
        "case \"$*\" in\n"
        f"  *source_fingerprint*) echo '{STRUCTURAL_SOURCE_IDENTITY}' ;;\n"
        "  *group_concat*) echo 'Kaufland: cena_s_kartou must be lower than cena' ;;\n"
        "  *MAX*) echo 123 ;;\n"
        "  *\"SELECT COUNT(*) FROM (\"*) echo 3 ;;\n"
        "  *) echo 881 ;;\n"
        "esac\n",
        encoding="utf-8",
        newline="\n",
    )
    fake_sqlite.chmod(0o755)

    fake_curl = tmp_path / "curl"
    fake_curl.write_text(
        "#!/bin/sh\n"
        "case \"$*\" in\n"
        "  *api/health*) printf '%s\\n' \"$UVARSI_TEST_HEALTH\" ;;\n"
        f"  *ntfy.sh/*) printf '%s\\n' \"$*\" >> '{bash_path(notifications)}' ;;\n"
        "esac\n"
        "exit 0\n",
        encoding="utf-8",
        newline="\n",
    )
    fake_curl.chmod(0o755)
    monkeypatch.setenv("UVARSI_CURL", bash_path(fake_curl))

    first = run_dozorca(tmp_path, landing_data)
    second = run_dozorca(tmp_path, landing_data)

    assert (first.returncode, second.returncode) == (3, 3)
    sent = notifications.read_text(encoding="utf-8")
    assert sent.count("cena_s_kartou must be lower than cena") == 1


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
    (tmp_path / "app").mkdir()
    landing_data = tmp_path / "landing_data.json"
    write_landing_data_atomic(landing_data, payload("2026-08-10"))
    calls = tmp_path / "calls.txt"
    notifications = tmp_path / "notify.txt"
    fake_python = tmp_path / "python"
    fake_python.write_text(
        "#!/bin/sh\n"
        "if [ \"$1\" = \"-c\" ]; then exit 1; fi\n"
        f"printf '%s|%s\\n' \"${{UVARSI_DEPLOY_CREDIT_PROBE:-0}}\" \"$*\" >> '{bash_path(calls)}'\n"
        "echo 'KREDIT_VYCERPANY: na účte došiel kredit' >&2\n"
        "exit 3\n",
        encoding="utf-8",
    )
    fake_python.chmod(0o755)
    fake_sqlite = tmp_path / "sqlite3"
    fake_sqlite.write_text(
        "#!/bin/sh\n"
        "case \"$*\" in\n"
        "  *'SELECT lower(v.o)'*) echo lidl ;;\n"
        "  *'SELECT COUNT(*) FROM ('*) echo 1 ;;\n"
        "  *MAX*) echo 0 ;;\n"
        "  *) echo 431 ;;\n"
        "esac\n",
        encoding="utf-8",
        newline="\n",
    )
    fake_sqlite.chmod(0o755)
    fake_curl = tmp_path / "curl"
    fake_curl.write_text(
        "#!/bin/sh\n"
        "case \"$*\" in *api/health*) printf '%s\\n' \"$UVARSI_TEST_HEALTH\"; exit 0 ;; esac\n"
        f"printf '%s\\n' \"$*\" >> '{bash_path(notifications)}'\n",
        encoding="utf-8",
        newline="\n",
    )
    fake_curl.chmod(0o755)
    return landing_data, calls, notifications


def test_dozorca_zachyti_nulovy_kredit_uz_v_zberaci_a_nepusti_refresh(tmp_path):
    """Kredit môže dôjsť už pri čítaní Lidlu; bloček sa vtedy nesmie spustiť."""
    landing_data, calls, _ = _credit_exhausted_environment(tmp_path)

    first = run_dozorca(tmp_path, landing_data, UVARSI_NOW_EPOCH=1_000_000)

    assert first.returncode == 3
    assert calls.read_text(encoding="utf-8").count("zbierac_akcii.py") == 1
    assert "refresh_blocek.py" not in calls.read_text(encoding="utf-8")
    assert (tmp_path / ".dozorca_state").read_text(encoding="utf-8").split() == [
        "2026-08-18", "0", "KREDIT", "1000000"
    ]
    assert "KREDIT" in first.stdout


def test_dozorca_po_kredite_skusa_najviac_raz_za_hodinu(tmp_path):
    landing_data, calls, _ = _credit_exhausted_environment(tmp_path)

    first = run_dozorca(tmp_path, landing_data, UVARSI_NOW_EPOCH=1_000_000)
    early = run_dozorca(tmp_path, landing_data, UVARSI_NOW_EPOCH=1_003_599)
    hourly = run_dozorca(tmp_path, landing_data, UVARSI_NOW_EPOCH=1_003_600)

    assert (first.returncode, early.returncode, hourly.returncode) == (3, 3, 3)
    assert calls.read_text(encoding="utf-8").count("zbierac_akcii.py") == 2
    assert "refresh_blocek.py" not in calls.read_text(encoding="utf-8")
    assert (tmp_path / ".dozorca_state").read_text(encoding="utf-8").split() == [
        "2026-08-18", "0", "KREDIT", "1003600"
    ]


def test_nove_vydanie_overi_dobity_kredit_bez_cakania_na_cooldown(tmp_path):
    """Úspešný deploy smie raz overiť kredit; rovnaké vydanie stále čaká hodinu."""
    landing_data, calls, _ = _credit_exhausted_environment(tmp_path)
    release_sha = tmp_path / ".nasadene_sha"
    release_sha.write_text(("a" * 40) + "\n", encoding="utf-8")

    first = run_dozorca(tmp_path, landing_data, UVARSI_NOW_EPOCH=1_000_000)
    release_sha.write_text(("b" * 40) + "\n", encoding="utf-8")
    after_deploy = run_dozorca(tmp_path, landing_data, UVARSI_NOW_EPOCH=1_000_060)
    same_release = run_dozorca(tmp_path, landing_data, UVARSI_NOW_EPOCH=1_000_120)

    assert (first.returncode, after_deploy.returncode, same_release.returncode) == (3, 3, 3)
    assert calls.read_text(encoding="utf-8").count("zbierac_akcii.py") == 2
    assert calls.read_text(encoding="utf-8").splitlines()[1].startswith("1|")
    assert (tmp_path / ".dozorca_state").read_text(encoding="utf-8").split() == [
        "2026-08-18", "0", "KREDIT", "1000060", "b" * 40
    ]


def test_receptovy_smoke_predchadza_kreditovemu_skratu():
    """Letákový kredit nesmie po vydaní nechať deterministické recepty vypnuté."""
    source = (ROOT / "hetzner" / "dozorca.sh").read_text(encoding="utf-8")
    zaciatok_behu = source.index("MON_ISO=")
    smoke = source.index("skontroluj_recipe_engine", zaciatok_behu)
    kreditovy_skrat = source.index('if [ "$BLOKNUTE_NA" = "KREDIT" ]', zaciatok_behu)
    assert smoke < kreditovy_skrat


def test_dozorca_does_not_send_a_second_credit_notification(tmp_path):
    """Upozornenie posiela naklady.py práve raz — dozorca ho nesmie zdvojiť."""
    landing_data, _, notifications = _credit_exhausted_environment(tmp_path)

    for epoch in (1_000_000, 1_003_600, 1_007_200):
        run_dozorca(tmp_path, landing_data, UVARSI_NOW_EPOCH=epoch)

    assert not notifications.exists(), (
        "dozorca pri nulovom kredite neposiela vlastnú notifikáciu"
    )


def test_dozorca_credit_block_does_not_leak_into_the_next_day(tmp_path):
    """Zajtra sa to skúsi znova — kredit mohol medzitým pribudnúť."""
    landing_data, calls, _ = _credit_exhausted_environment(tmp_path)
    run_dozorca(tmp_path, landing_data, UVARSI_NOW_EPOCH=1_000_000)

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

    assert calls.read_text(encoding="utf-8").count("zbierac_akcii.py") == 2
    assert "refresh_blocek.py" not in calls.read_text(encoding="utf-8")


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


def test_dozorca_reactivates_recipe_engine_after_current_data(
    monkeypatch, tmp_path
):
    landing_data = tmp_path / "landing_data.json"
    write_landing_data_atomic(landing_data, payload("2026-08-17"))
    marker = tmp_path / "recipe-rollout-ran"

    fake_python = tmp_path / "python"
    fake_python.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8", newline="\n")
    fake_python.chmod(0o755)

    fake_sqlite = tmp_path / "sqlite3"
    fake_sqlite.write_text(
        "#!/bin/sh\n"
        "case \"$*\" in\n"
        "  *MAX*) echo 123 ;;\n"
        "  *zber_stav*) echo 0 ;;\n"
        "  *) echo 60 ;;\n"
        "esac\n",
        encoding="utf-8",
        newline="\n",
    )
    fake_sqlite.chmod(0o755)

    rollout = tmp_path / "recipe-engine-rollout.sh"
    rollout.write_text(
        f"#!/bin/sh\nprintf activated > '{bash_path(marker)}'\nexit 0\n",
        encoding="utf-8",
        newline="\n",
    )
    rollout.chmod(0o755)

    health_on = json.loads(health_json())
    health_on["recipe_engine"].update(mode="on", ready=True, blockers=[])
    fake_curl = tmp_path / "curl"
    fake_curl.write_text(
        "#!/bin/sh\n"
        "case \"$*\" in\n"
        "  *api/health*)\n"
        f"    if [ -f '{bash_path(marker)}' ]; then printf '%s\\n' '{json.dumps(health_on, separators=(',', ':'))}'; "
        f"else printf '%s\\n' '{health_json()}'; fi ;;\n"
        "esac\n"
        "exit 0\n",
        encoding="utf-8",
        newline="\n",
    )
    fake_curl.chmod(0o755)
    monkeypatch.setenv("UVARSI_CURL", bash_path(fake_curl))

    result = run_dozorca(tmp_path, landing_data)

    assert result.returncode == 0, result.stdout + result.stderr
    assert marker.read_text(encoding="utf-8") == "activated"
    assert "receptový engine je znova aktívny" in result.stdout


def test_dozorca_revalidates_on_engine_after_offer_fingerprint_changes(
    monkeypatch, tmp_path,
):
    """Nový leták zneplatní starý shadow; dozorca musí spustiť revalidáciu."""
    (tmp_path / "app").mkdir()
    landing_data = tmp_path / "landing_data.json"
    write_landing_data_atomic(landing_data, payload("2026-08-17"))
    marker = tmp_path / "recipe-rollout-ran"

    fake_python = tmp_path / "python"
    fake_python.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8", newline="\n")
    fake_python.chmod(0o755)
    fake_sqlite = tmp_path / "sqlite3"
    fake_sqlite.write_text(
        "#!/bin/sh\n"
        "case \"$*\" in\n"
        "  *MAX*) echo 123 ;;\n"
        "  *zber_stav*) echo 0 ;;\n"
        "  *) echo 60 ;;\n"
        "esac\n",
        encoding="utf-8",
        newline="\n",
    )
    fake_sqlite.chmod(0o755)

    rollout = tmp_path / "recipe-engine-rollout.sh"
    rollout.write_text(
        f"#!/bin/sh\nprintf revalidated > '{bash_path(marker)}'\nexit 0\n",
        encoding="utf-8",
        newline="\n",
    )
    rollout.chmod(0o755)

    stale = json.loads(health_json())
    stale["recipe_engine"].update(
        mode="on", ready=False, blockers=["shadow_not_ready"],
    )
    healthy = json.loads(health_json())
    healthy["recipe_engine"].update(mode="on", ready=True, blockers=[])
    fake_curl = tmp_path / "curl"
    fake_curl.write_text(
        "#!/bin/sh\n"
        "case \"$*\" in\n"
        "  *api/health*)\n"
        f"    if [ -f '{bash_path(marker)}' ]; then printf '%s\\n' '{json.dumps(healthy, separators=(',', ':'))}'; "
        f"else printf '%s\\n' '{json.dumps(stale, separators=(',', ':'))}'; fi ;;\n"
        "esac\n"
        "exit 0\n",
        encoding="utf-8",
        newline="\n",
    )
    fake_curl.chmod(0o755)
    monkeypatch.setenv("UVARSI_CURL", bash_path(fake_curl))

    result = run_dozorca(tmp_path, landing_data)

    assert result.returncode == 0, result.stdout + result.stderr
    assert marker.read_text(encoding="utf-8") == "revalidated"
    assert "revalid" in result.stdout.lower()


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


def test_dozorca_retries_only_the_store_whose_current_flyer_is_missing(tmp_path):
    """Pokazený Lidl nesmie znovu spustiť platené čítanie Tesca a Kauflandu."""
    (tmp_path / "app").mkdir()
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
        newline="\n",
    )
    fake_python.chmod(0o755)
    fake_sqlite = tmp_path / "sqlite3"
    fake_sqlite.write_text(
        "#!/bin/sh\n"
        "case \"$*\" in\n"
        "  *\"SELECT lower(v.o)\"*) echo lidl ;;\n"
        "  *\"SELECT COUNT(*) FROM (\"*) echo 1 ;;\n"
        "  *) echo 40 ;;\n"
        "esac\n",
        encoding="utf-8",
        newline="\n",
    )
    fake_sqlite.chmod(0o755)

    subprocess.run(
        [str(BASH), bash_path(ROOT / "hetzner" / "dozorca.sh")],
        cwd=str(ROOT),
        env=os.environ | {
            "UVARSI_DIR": bash_path(tmp_path),
            "UVARSI_LANDING_DATA": bash_path(landing_data),
            "UVARSI_PY": bash_path(fake_python),
            "UVARSI_TODAY": "2026-09-03",
            "UVARSI_DOZORCA_LOCKED": "1",
            "PATH": f"{bash_path(tmp_path)}:/usr/bin",
        },
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
    )

    collection_calls = [line for line in calls.read_text(encoding="utf-8").splitlines()
                        if "zbierac_akcii.py" in line]
    assert collection_calls == ["-u zbierac_akcii.py --store lidl"]


def test_dozorca_repairs_kaufland_from_official_source_before_paid_collector(tmp_path):
    (tmp_path / "app").mkdir()
    landing_data = tmp_path / "landing_data.json"
    write_landing_data_atomic(landing_data, payload("2026-08-10"))
    calls = tmp_path / "calls.txt"
    (tmp_path / ".nasadene_sha").write_text("a" * 40 + "\n", encoding="ascii")
    fake_python = tmp_path / "python"
    fake_python.write_text(
        "#!/bin/sh\n"
        "if [ \"$1\" = \"-c\" ]; then\n"
        "  case \"$2\" in *landing_data_is_current*) exit 1 ;; esac\n"
        "  case \"$2\" in *'from datetime import date'*) echo 2026-08-17; exit 0 ;; esac\n"
        "  exit 1\n"
        "fi\n"
        f"printf '%s\\n' \"$*\" >> '{bash_path(calls)}'\n"
        "case \"$*\" in\n"
        "  *--official-kaufland-only*) exit 0 ;;\n"
        "  *refresh_blocek.py*) exit 3 ;;\n"
        "esac\n"
        "exit 0\n",
        encoding="utf-8",
        newline="\n",
    )
    fake_python.chmod(0o755)
    fake_sqlite = tmp_path / "sqlite3"
    fake_sqlite.write_text(
        "#!/bin/sh\n"
        f"DONE=0; grep -q -- --official-kaufland-only '{bash_path(calls)}' "
        "2>/dev/null && DONE=1\n"
        "case \"$*\" in\n"
        f"  *source_fingerprint*) echo '{STRUCTURAL_SOURCE_IDENTITY}' ;;\n"
        "  *\"SELECT lower(v.o)\"*) [ \"$DONE\" -eq 0 ] && echo kaufland ;;\n"
        "  *\"SELECT COUNT(*) FROM (\"*) [ \"$DONE\" -eq 1 ] && echo 0 || echo 1 ;;\n"
        "  *MAX*) echo 0 ;;\n"
        "  *) echo 40 ;;\n"
        "esac\n",
        encoding="utf-8",
        newline="\n",
    )
    fake_sqlite.chmod(0o755)

    result = run_dozorca(tmp_path, landing_data)
    recorded = calls.read_text(encoding="utf-8").splitlines()

    assert result.returncode == 3
    assert recorded.count("-u zbierac_akcii.py --official-kaufland-only") == 1
    assert not any(
        "zbierac_akcii.py --store" in call for call in recorded
    ), "úspešný bezplatný zber nesmie spustiť platený Vision fallback"


def test_dozorca_repairs_kaufland_free_when_multiple_stores_are_missing(tmp_path):
    (tmp_path / "app").mkdir()
    landing_data = tmp_path / "landing_data.json"
    write_landing_data_atomic(landing_data, payload("2026-08-10"))
    calls = tmp_path / "calls.txt"
    (tmp_path / ".nasadene_sha").write_text("a" * 40 + "\n", encoding="ascii")
    fake_python = tmp_path / "python"
    fake_python.write_text(
        "#!/bin/sh\n"
        "if [ \"$1\" = \"-c\" ]; then\n"
        "  case \"$2\" in *landing_data_is_current*) exit 1 ;; esac\n"
        "  case \"$2\" in *'from datetime import date'*) echo 2026-08-17; exit 0 ;; esac\n"
        "  exit 1\n"
        "fi\n"
        f"printf '%s\\n' \"$*\" >> '{bash_path(calls)}'\n"
        "case \"$*\" in\n"
        "  *--official-kaufland-only*) exit 0 ;;\n"
        "  *refresh_blocek.py*) exit 3 ;;\n"
        "esac\n"
        "exit 0\n",
        encoding="utf-8",
        newline="\n",
    )
    fake_python.chmod(0o755)
    fake_sqlite = tmp_path / "sqlite3"
    fake_sqlite.write_text(
        "#!/bin/sh\n"
        f"DONE=0; grep -q -- --official-kaufland-only '{bash_path(calls)}' "
        "2>/dev/null && DONE=1\n"
        "case \"$*\" in\n"
        f"  *source_fingerprint*) echo '{STRUCTURAL_SOURCE_IDENTITY}' ;;\n"
        "  *\"SELECT lower(v.o)\"*) "
        "if [ \"$DONE\" -eq 0 ]; then printf 'kaufland\\nlidl\\n'; else echo lidl; fi ;;\n"
        "  *\"SELECT COUNT(*) FROM (\"*) [ \"$DONE\" -eq 1 ] && echo 1 || echo 2 ;;\n"
        "  *MAX*) echo 0 ;;\n"
        "  *) echo 40 ;;\n"
        "esac\n",
        encoding="utf-8",
        newline="\n",
    )
    fake_sqlite.chmod(0o755)

    result = run_dozorca(tmp_path, landing_data)
    recorded = calls.read_text(encoding="utf-8").splitlines()

    assert result.returncode == 3
    assert recorded.count("-u zbierac_akcii.py --official-kaufland-only") == 1
    paid = [call for call in recorded if "zbierac_akcii.py --store" in call]
    assert paid == ["-u zbierac_akcii.py --store lidl"]


def test_dozorca_neplati_rovnaky_strukturalny_zber_opakovane_a_release_ho_odomkne(
    tmp_path,
):
    """Rovnaká AI chyba sa smie zaplatiť raz; oprava kódu smie skúsiť zber znova."""
    (tmp_path / "app").mkdir()
    landing_data = tmp_path / "landing_data.json"
    write_landing_data_atomic(landing_data, payload("2026-08-10"))
    calls = tmp_path / "calls.txt"
    release_sha = tmp_path / ".nasadene_sha"
    release_sha.write_text(("a" * 40) + "\n", encoding="utf-8")

    fake_python = tmp_path / "python"
    fake_python.write_text(
        "#!/bin/sh\n"
        "if [ \"$1\" = \"-c\" ]; then exit 1; fi\n"
        f"printf '%s\\n' \"$*\" >> '{bash_path(calls)}'\n"
        "case \"$*\" in\n"
        "  *zbierac_akcii.py*) echo 'ZBER_STRUKTURALNY: chybná položka'; exit 1 ;;\n"
        "esac\n"
        "exit 3\n",
        encoding="utf-8",
        newline="\n",
    )
    fake_python.chmod(0o755)
    fake_sqlite = tmp_path / "sqlite3"
    fake_sqlite.write_text(
        "#!/bin/sh\n"
        "case \"$*\" in\n"
        "  *\"SELECT lower(v.o)\"*) echo kaufland ;;\n"
        "  *\"SELECT COUNT(*) FROM (\"*) echo 1 ;;\n"
        f"  *MAX*) if grep -q zbierac_akcii.py '{bash_path(calls)}' 2>/dev/null; "
        "then echo 1; else echo 0; fi ;;\n"
        "  *) echo 40 ;;\n"
        "esac\n",
        encoding="utf-8",
        newline="\n",
    )
    fake_sqlite.chmod(0o755)

    first = run_dozorca(tmp_path, landing_data)
    repeated = run_dozorca(tmp_path, landing_data)
    release_sha.write_text(("b" * 40) + "\n", encoding="utf-8")
    after_release = run_dozorca(tmp_path, landing_data)

    collection_calls = [
        line
        for line in calls.read_text(encoding="utf-8").splitlines()
        if "zbierac_akcii.py --store" in line
    ]
    assert (first.returncode, repeated.returncode, after_release.returncode) == (3, 3, 3)
    assert len(collection_calls) == 2
    assert "nezmenil" in repeated.stdout


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


def test_dozorca_pri_obsadenom_zamku_vrati_bezpecny_odlisny_stav(tmp_path):
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

    assert result.returncode == 75
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
        f"  *ntfy.sh/*) printf '%s\\n' \"$*\" >> '{bash_path(notifications)}' ;;\n"
        "  *api/health*) printf '%s\\n' \"$UVARSI_TEST_HEALTH\" ;;\n"
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
        "UVARSI_TEST_HEALTH": health_json({"queued": 1, "oldest_seconds": 181,
            "worker_alive": False, "heartbeat_seconds": 61,
            "heartbeat_at": "2026-08-28T16:10:20+00:00", "last_ready": None,
            "failed": 0, "blocking_code": "worker_heartbeat_stale"}),
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
        health_json({"worker_alive": True}),
        health_json({"queued": False, "oldest_seconds": None, "worker_alive": True,
            "heartbeat_seconds": 1, "heartbeat_at": "2026-08-28T16:10:20+00:00",
            "last_ready": None, "failed": 0, "blocking_code": None}),
        health_json({"queued": 0, "oldest_seconds": 12, "worker_alive": True,
            "heartbeat_seconds": None, "heartbeat_at": "2026-08-28T16:10:20+00:00",
            "last_ready": None, "failed": 0, "blocking_code": None}),
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

    environment["UVARSI_TEST_HEALTH"] = health_json()
    recovered = subprocess.run(
        [str(BASH), bash_path(ROOT / "hetzner" / "dozorca.sh")],
        cwd=str(ROOT), env=environment, text=True, encoding="utf-8",
        errors="replace", capture_output=True, check=False,
    )

    assert recovered.returncode == 0
    assert not marker.exists()


def test_dozorca_retries_http_500_queue_notification_then_suppresses_after_success(tmp_path):
    (tmp_path / "app").mkdir()
    landing_data = tmp_path / "landing_data.json"
    write_landing_data_atomic(landing_data, payload("2026-08-17"))
    attempts = tmp_path / "notification-attempts.txt"
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
        f"  *ntfy.sh/*) printf '500\\n' >> '{bash_path(attempts)}'; "
        f"COUNT=$(wc -l < '{bash_path(attempts)}'); "
        "if [ \"$COUNT\" -eq 1 ]; then "
        "  case \"$*\" in *-fsS*) exit 22 ;; *) exit 0 ;; esac; "
        "fi; "
        f"sed -i '$s/500/200/' '{bash_path(attempts)}'; exit 0 ;;\n"
        "  *api/health*) printf '%s\\n' \"$UVARSI_TEST_HEALTH\" ;;\n"
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
        "UVARSI_TEST_HEALTH": health_json({"queued": 1, "oldest_seconds": 181,
            "worker_alive": False, "heartbeat_seconds": 61,
            "heartbeat_at": "2026-08-28T16:10:20+00:00", "last_ready": None,
            "failed": 0, "blocking_code": "worker_heartbeat_stale"}),
        "PATH": f"{bash_path(tmp_path)}:/usr/bin",
    }
    marker = tmp_path / ".plan_queue_alert_state"

    runs = []
    for _ in range(3):
        runs.append(subprocess.run(
            [str(BASH), bash_path(ROOT / "hetzner" / "dozorca.sh")],
            cwd=ROOT,
            env=environment,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            check=False,
        ))

    assert [run.returncode for run in runs] == [0, 0, 0]
    assert attempts.read_text(encoding="utf-8").splitlines() == ["500", "200"]
    assert marker.exists()
