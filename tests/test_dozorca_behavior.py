import json
import shutil
import os
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
BASH = Path(os.environ.get("UVARSI_TEST_BASH") or shutil.which("bash") or "C:/Program Files/Git/bin/bash.exe")
TODAY = "2026-08-18"
WEEK = "2026-08-17"


def bash_path(path):
    posix = Path(path).as_posix()
    if os.name != "nt":
        return posix
    return "/c" + posix[2:]


def health_json():
    return json.dumps(
        {
            "plan_queue": {
                "queued": 0,
                "oldest_seconds": None,
                "worker_alive": True,
                "heartbeat_seconds": 1,
                "heartbeat_at": "2026-08-18T05:00:00+00:00",
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


def source_identity(letter):
    fingerprints = ":".join(letter * 64 for _ in range(3))
    return fingerprints


@pytest.fixture
def supervisor_environment(tmp_path):
    landing_data = tmp_path / "landing_data.json"
    original = b'{\n  "last_known_good": true, "must_survive": "byte-for-byte"\n}\r\n'
    landing_data.write_bytes(original)
    calls = tmp_path / "calls.txt"
    active_fingerprint = tmp_path / "active-source-fingerprint.txt"
    active_fingerprint.write_text(source_identity("a") + "\n", encoding="ascii")
    staged_fingerprint = tmp_path / "staged-source-fingerprint.txt"
    staged_fingerprint.write_text(source_identity("a") + "\n", encoding="ascii")
    release = tmp_path / ".nasadene_sha"
    release.write_text("a" * 40 + "\n", encoding="ascii")

    fake_python = tmp_path / "python"
    fake_python.write_text(
        "#!/bin/sh\n"
        "if [ \"$1\" = \"-c\" ]; then\n"
        "  case \"$2\" in\n"
        "    *landing_data_is_current*) exit 1 ;;\n"
        f"    *'from datetime import date'*) echo {WEEK}; exit 0 ;;\n"
        "  esac\n"
        "  exit 1\n"
        "fi\n"
        f"printf '%s\\n' \"$*\" >> '{bash_path(calls)}'\n"
        "exit \"${UVARSI_TEST_REFRESH_RC:-3}\"\n",
        encoding="utf-8",
        newline="\n",
    )
    fake_python.chmod(0o755)

    fake_sqlite = tmp_path / "sqlite3"
    fake_sqlite.write_text(
        "#!/bin/sh\n"
        "case \"$*\" in\n"
        f"  *source_fingerprint*zber_staging_stav*) cat '{bash_path(staged_fingerprint)}' ;;\n"
        f"  *source_fingerprint*) cat '{bash_path(active_fingerprint)}' ;;\n"
        "  *'SELECT COUNT(*) FROM ('*) echo 0 ;;\n"
        "  *MAX*) echo 1000 ;;\n"
        "  *) echo 60 ;;\n"
        "esac\n",
        encoding="utf-8",
        newline="\n",
    )
    fake_sqlite.chmod(0o755)

    notifications = tmp_path / "notifications.txt"
    fake_curl = tmp_path / "curl"
    fake_curl.write_text(
        "#!/bin/sh\n"
        "case \"$*\" in\n"
        f"  *ntfy.sh*) printf '%s\\n' \"$*\" >> '{bash_path(notifications)}' ;;\n"
        "  *api/health*)\n"
        "    if [ -n \"${UVARSI_TEST_HEALTH_FAIL_ONCE:-}\" ] && "
        "[ -f \"$UVARSI_TEST_HEALTH_FAIL_ONCE\" ]; then\n"
        "      rm -f \"$UVARSI_TEST_HEALTH_FAIL_ONCE\"; exit 7\n"
        "    fi\n"
        "    printf '%s\\n' \"$UVARSI_TEST_HEALTH\" ;;\n"
        "esac\n"
        "exit 0\n",
        encoding="utf-8",
        newline="\n",
    )
    fake_curl.chmod(0o755)

    environment = os.environ | {
        "UVARSI_DIR": bash_path(tmp_path),
        "UVARSI_LANDING_DATA": bash_path(landing_data),
        "UVARSI_PY": bash_path(fake_python),
        "UVARSI_HEALTH_PY": bash_path(Path(sys.executable)),
        "UVARSI_CURL": bash_path(fake_curl),
        "UVARSI_PLAN_QUEUE_HEALTH_URL": "http://health.test/api/health",
        "UVARSI_TEST_HEALTH": health_json(),
        "UVARSI_TODAY": TODAY,
        "UVARSI_NOW_EPOCH": "1000000",
        "UVARSI_DOZORCA_LOCKED": "1",
        "UVARSI_TEST_BRIDGE_PREFLIGHT": "/usr/bin/true",
        "PATH": f"{bash_path(tmp_path)}:/usr/bin",
    }
    return {
        "tmp_path": tmp_path,
        "landing_data": landing_data,
        "original": original,
        "calls": calls,
        "notifications": notifications,
        "active_fingerprint": active_fingerprint,
        "staged_fingerprint": staged_fingerprint,
        "release": release,
        "environment": environment,
    }


def run_supervisor(context, **extra_environment):
    return subprocess.run(
        [str(BASH), bash_path(ROOT / "hetzner" / "dozorca.sh")],
        cwd=str(ROOT),
        env=context["environment"]
        | {key: str(value) for key, value in extra_environment.items()},
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
    )


def refresh_call_count(context):
    if not context["calls"].exists():
        return 0
    return context["calls"].read_text(encoding="utf-8").count(
        "refresh_blocek.py"
    )


def test_transient_health_gap_is_retried_before_supervisor_gates(
        supervisor_environment):
    marker = supervisor_environment["tmp_path"] / "fail-health-once"
    marker.touch()

    result = run_supervisor(
        supervisor_environment,
        UVARSI_TEST_HEALTH_FAIL_ONCE=bash_path(marker),
        UVARSI_HEALTH_RETRY_DELAY_SECONDS=0,
    )

    assert "UNKNOWN — frontu plánov" not in result.stdout
    assert "health nie je dostupný" not in result.stdout


def test_collection_stops_before_collector_when_last_moment_bridge_check_fails(
        supervisor_environment):
    configure_structural_collection(supervisor_environment, stores="tesco")

    result = run_supervisor(
        supervisor_environment,
        UVARSI_TEST_BRIDGE_PREFLIGHT="/usr/bin/false",
    )

    assert result.returncode != 0
    assert not supervisor_environment["calls"].exists()
    assert "priamo pred zberom" in result.stdout


def test_bridge_check_is_skipped_when_tesco_is_not_collected(
        supervisor_environment):
    configure_structural_collection(supervisor_environment, stores="lidl")

    result = run_supervisor(
        supervisor_environment,
        UVARSI_TEST_BRIDGE_PREFLIGHT="/usr/bin/false",
    )

    calls = supervisor_environment["calls"].read_text(encoding="utf-8")
    assert "zbierac_akcii.py --store lidl" in calls
    assert "priamo pred zberom" not in result.stdout


def test_failed_bridge_drops_only_tesco_and_still_fails_the_run(
        supervisor_environment):
    configure_structural_collection(
        supervisor_environment, stores="tesco\nlidl", collector_rc=0
    )

    result = run_supervisor(
        supervisor_environment,
        UVARSI_TEST_BRIDGE_PREFLIGHT="/usr/bin/false",
    )

    calls = supervisor_environment["calls"].read_text(encoding="utf-8")
    assert "zbierac_akcii.py --store lidl" in calls
    assert "tesco" not in calls
    assert "Tesco vynechávam" in result.stdout
    assert result.returncode == 1, result.stdout + result.stderr
    assert refresh_call_count(supervisor_environment) == 0


def notification_text(context):
    if not context["notifications"].exists():
        return ""
    return context["notifications"].read_text(encoding="utf-8")


def test_bridge_outage_alerts_once_per_day_and_reports_recovery(
        supervisor_environment):
    configure_structural_collection(supervisor_environment, stores="tesco")

    first = run_supervisor(
        supervisor_environment, UVARSI_TEST_BRIDGE_PREFLIGHT="/usr/bin/false"
    )
    second = run_supervisor(
        supervisor_environment, UVARSI_TEST_BRIDGE_PREFLIGHT="/usr/bin/false"
    )

    assert (first.returncode, second.returncode) == (1, 1)
    assert notification_text(supervisor_environment).count("zber odložený") == 1

    run_supervisor(
        supervisor_environment, UVARSI_TEST_BRIDGE_PREFLIGHT="/usr/bin/true"
    )

    assert "bridge opravený" in notification_text(supervisor_environment)
    assert not (supervisor_environment["tmp_path"] / ".bridge_alert_state").exists()


def test_exhausted_run_cap_alerts_once_and_pauses_collection_until_monday(
        supervisor_environment):
    configure_structural_collection(supervisor_environment, stores="lidl")
    fake_python = supervisor_environment["tmp_path"] / "python"
    fake_python.write_text(
        fake_python.read_text(encoding="utf-8").replace(
            "echo 'ZBER_STRUKTURALNY: malformed source'; exit 1",
            "echo 'ZBER_BEHY_VYCERPANE: strop 3x'; exit 1",
        ),
        encoding="utf-8",
        newline="\n",
    )

    first = run_supervisor(supervisor_environment)
    second = run_supervisor(supervisor_environment)

    assert (first.returncode, second.returncode) == (3, 3)
    calls = supervisor_environment["calls"].read_text(encoding="utf-8")
    assert calls.count("zbierac_akcii.py") == 1
    assert notification_text(supervisor_environment).count("stojí do pondelka") == 1
    assert "pondelok" in second.stdout


def test_current_active_offers_rebuild_receipt_without_touching_failed_staging(
        supervisor_environment):
    """A broken scratch candidate must not trigger Tesco or block the receipt."""
    context = supervisor_environment
    ready_marker = context["tmp_path"] / "landing-ready"
    fake_python = context["tmp_path"] / "python"
    fake_python.write_text(
        "#!/bin/sh\n"
        "if [ \"$1\" = \"-c\" ]; then\n"
        "  case \"$2\" in\n"
        f"    *landing_data_is_current*) [ -f '{bash_path(ready_marker)}' ] ; exit $? ;;\n"
        f"    *'from datetime import date'*) echo {WEEK}; exit 0 ;;\n"
        "  esac\n"
        "  exit 1\n"
        "fi\n"
        f"printf '%s\\n' \"$*\" >> '{bash_path(context['calls'])}'\n"
        "case \"$*\" in\n"
        f"  *refresh_blocek.py*) touch '{bash_path(ready_marker)}'; exit 0 ;;\n"
        "  *zbierac_akcii.py*) exit 99 ;;\n"
        "esac\n"
        "exit 0\n",
        encoding="utf-8",
        newline="\n",
    )
    fake_python.chmod(0o755)
    fake_sqlite = context["tmp_path"] / "sqlite3"
    fake_sqlite.write_text(
        "#!/bin/sh\n"
        "case \"$*\" in\n"
        f"  *source_fingerprint*zber_staging_stav*) cat '{bash_path(context['staged_fingerprint'])}' ;;\n"
        f"  *source_fingerprint*) cat '{bash_path(context['active_fingerprint'])}' ;;\n"
        "  *'SELECT COUNT(*) FROM ('*zber_staging*) echo 1 ;;\n"
        "  *'SELECT COUNT(*) FROM ('*) echo 0 ;;\n"
        "  *akcie_staging*) echo 40 ;;\n"
        "  *MAX*) echo 1000 ;;\n"
        "  *) echo 60 ;;\n"
        "esac\n",
        encoding="utf-8",
        newline="\n",
    )
    fake_sqlite.chmod(0o755)

    result = run_supervisor(
        context, UVARSI_TEST_BRIDGE_PREFLIGHT="/usr/bin/false"
    )

    assert result.returncode == 0, result.stdout + result.stderr
    calls = context["calls"].read_text(encoding="utf-8")
    assert "refresh_blocek.py" in calls
    assert "zbierac_akcii.py" not in calls


def configure_structural_collection(context, stores="lidl", collector_rc=None):
    calls = context["calls"]
    fingerprint = context["staged_fingerprint"]
    (context["tmp_path"] / "app").mkdir(exist_ok=True)
    fake_python = context["tmp_path"] / "python"
    fake_python.write_text(
        "#!/bin/sh\n"
        "if [ \"$1\" = \"-c\" ]; then\n"
        "  case \"$2\" in\n"
        "    *landing_data_is_current*) exit 1 ;;\n"
        f"    *'from datetime import date'*) echo {WEEK}; exit 0 ;;\n"
        "  esac\n"
        "  exit 1\n"
        "fi\n"
        f"printf '%s\\n' \"$*\" >> '{bash_path(calls)}'\n"
        "case \"$*\" in\n"
        + (
            "  *zbierac_akcii.py*) echo 'ZBER_STRUKTURALNY: malformed source'; exit 1 ;;\n"
            if collector_rc is None
            else f"  *zbierac_akcii.py*) exit {collector_rc} ;;\n"
        )
        + "  *refresh_blocek.py*) exit 99 ;;\n"
        "esac\n"
        "exit 0\n",
        encoding="utf-8",
        newline="\n",
    )
    fake_python.chmod(0o755)

    fake_sqlite = context["tmp_path"] / "sqlite3"
    fake_sqlite.write_text(
        "#!/bin/sh\n"
        "case \"$*\" in\n"
        f"  *source_fingerprint*) cat '{bash_path(fingerprint)}' ;;\n"
        f"  *'SELECT lower(v.o)'*) printf '{stores}\\n' ;;\n"
        "  *'SELECT COUNT(*) FROM ('*) echo 1 ;;\n"
        "  *MAX*) echo 1000 ;;\n"
        "  *) echo 40 ;;\n"
        "esac\n",
        encoding="utf-8",
        newline="\n",
    )
    fake_sqlite.chmod(0o755)


def test_transient_failure_retries_same_day_and_preserves_last_known_good(
    supervisor_environment,
):
    first = run_supervisor(
        supervisor_environment, UVARSI_TEST_REFRESH_RC=1, UVARSI_NOW_EPOCH=1000000
    )
    second = run_supervisor(
        supervisor_environment, UVARSI_TEST_REFRESH_RC=1, UVARSI_NOW_EPOCH=1003600
    )

    assert (first.returncode, second.returncode) == (1, 1)
    assert refresh_call_count(supervisor_environment) == 2
    assert (
        supervisor_environment["landing_data"].read_bytes()
        == supervisor_environment["original"]
    )


def test_same_verified_fingerprint_and_release_suppresses_recomposition(
    supervisor_environment,
):
    first = run_supervisor(supervisor_environment, UVARSI_NOW_EPOCH=1000000)
    repeated = run_supervisor(supervisor_environment, UVARSI_NOW_EPOCH=1003600)

    assert (first.returncode, repeated.returncode) == (3, 3)
    assert refresh_call_count(supervisor_environment) == 1
    assert "zbierac_akcii.py" not in supervisor_environment["calls"].read_text(
        encoding="utf-8"
    )
    assert (supervisor_environment["tmp_path"] / ".dozorca_state").read_text(
        encoding="utf-8"
    ).split()[3:] == [source_identity("a"), "a" * 40]


def test_changed_source_fingerprint_invalidates_structural_suppression(
    supervisor_environment,
):
    first = run_supervisor(supervisor_environment, UVARSI_NOW_EPOCH=1000000)
    supervisor_environment["staged_fingerprint"].write_text(
        source_identity("b") + "\n", encoding="ascii"
    )
    changed = run_supervisor(supervisor_environment, UVARSI_NOW_EPOCH=1003600)

    assert (first.returncode, changed.returncode) == (3, 3)
    assert refresh_call_count(supervisor_environment) == 2


def test_changed_release_invalidates_structural_suppression(
    supervisor_environment,
):
    first = run_supervisor(supervisor_environment, UVARSI_NOW_EPOCH=1000000)
    supervisor_environment["release"].write_text("b" * 40 + "\n", encoding="ascii")
    changed = run_supervisor(supervisor_environment, UVARSI_NOW_EPOCH=1000060)

    assert (first.returncode, changed.returncode) == (3, 3)
    assert refresh_call_count(supervisor_environment) == 2


def test_unknown_structural_identity_retries_only_up_to_daily_bound(
    supervisor_environment,
):
    supervisor_environment["active_fingerprint"].write_text("\n", encoding="ascii")
    supervisor_environment["staged_fingerprint"].write_text("\n", encoding="ascii")
    supervisor_environment["release"].unlink()

    results = [
        run_supervisor(
            supervisor_environment,
            UVARSI_NOW_EPOCH=1000000 + attempt * 3600,
        )
        for attempt in range(7)
    ]

    assert refresh_call_count(supervisor_environment) == 6
    assert all(result.returncode in {1, 3} for result in results)
    assert "pauza do zajtra" in results[-1].stdout
    assert (
        supervisor_environment["landing_data"].read_bytes()
        == supervisor_environment["original"]
    )


def test_changed_staged_source_fingerprint_unlocks_structural_collection(
    supervisor_environment,
):
    configure_structural_collection(supervisor_environment)

    first = run_supervisor(supervisor_environment, UVARSI_NOW_EPOCH=1000000)
    repeated = run_supervisor(supervisor_environment, UVARSI_NOW_EPOCH=1003600)
    supervisor_environment["staged_fingerprint"].write_text(
        source_identity("b") + "\n", encoding="ascii"
    )
    changed = run_supervisor(supervisor_environment, UVARSI_NOW_EPOCH=1007200)

    collection_calls = [
        call
        for call in supervisor_environment["calls"].read_text(
            encoding="utf-8"
        ).splitlines()
        if "zbierac_akcii.py --store" in call
    ]
    assert (first.returncode, repeated.returncode, changed.returncode) == (3, 3, 3)
    assert len(collection_calls) == 2


def test_complete_verified_stage_skips_collector_and_goes_directly_to_receipt(
    supervisor_environment,
):
    context = supervisor_environment
    (context["tmp_path"] / "app").mkdir(exist_ok=True)
    fake_sqlite = context["tmp_path"] / "sqlite3"
    fake_sqlite.write_text(
        "#!/bin/sh\n"
        "case \"$*\" in\n"
        f"  *source_fingerprint*zber_staging_stav*) cat '{bash_path(context['staged_fingerprint'])}' ;;\n"
        f"  *source_fingerprint*) cat '{bash_path(context['active_fingerprint'])}' ;;\n"
        "  *'SELECT COUNT(*) FROM ('*zber_staging_stav*) echo 0 ;;\n"
        "  *'SELECT lower(v.o)'*zber_staging_stav*) exit 0 ;;\n"
        "  *'SELECT COUNT(*) FROM akcie_staging'*) echo 60 ;;\n"
        "  *'SELECT COUNT(*) FROM ('*) echo 3 ;;\n"
        "  *'SELECT lower(v.o)'*) printf 'tesco\\nlidl\\n' ;;\n"
        "  *'SELECT COUNT(*) FROM akcie'*) echo 0 ;;\n"
        "  *MAX*) echo 1000 ;;\n"
        "  *) echo 0 ;;\n"
        "esac\n",
        encoding="utf-8",
        newline="\n",
    )
    fake_sqlite.chmod(0o755)

    result = run_supervisor(context, UVARSI_NOW_EPOCH=1000000)

    assert result.returncode == 3
    calls = context["calls"].read_text(encoding="utf-8")
    assert "zbierac_akcii.py" not in calls
    assert calls.count("refresh_blocek.py") == 1


def fake_deploy_state(context, body):
    script = context["tmp_path"] / "fake-deploy-state.sh"
    script.write_text(body, encoding="utf-8", newline="\n")
    script.chmod(0o755)
    return bash_path(script)


def test_bridge_alert_tells_operator_what_to_do(supervisor_environment):
    configure_structural_collection(supervisor_environment, stores="tesco")
    script = fake_deploy_state(
        supervisor_environment,
        "_uvarsi_require_tesco_bridge_transport() {\n"
        "  UVARSI_BRIDGE_FAILURE_REASON=request_failed\n"
        "  UVARSI_BRIDGE_FAILURE_DETAIL=auth_rejected\n"
        "  return 1\n"
        "}\n",
    )

    result = run_supervisor(
        supervisor_environment,
        UVARSI_TEST_BRIDGE_PREFLIGHT="",
        UVARSI_DEPLOY_STATE_SCRIPT=script,
    )

    assert result.returncode == 1
    text = notification_text(supervisor_environment)
    assert "auth_rejected" in text
    assert "repair-tesco-bridge" in text


def test_bridge_secret_reaches_only_the_collector(supervisor_environment):
    context = supervisor_environment
    configure_structural_collection(context, stores="tesco", collector_rc=0)
    env_log = context["tmp_path"] / "env-log.txt"
    fake_python = context["tmp_path"] / "python"
    fake_python.write_text(
        fake_python.read_text(encoding="utf-8").replace(
            f"printf '%s\\n' \"$*\" >> '{bash_path(context['calls'])}'\n",
            f"printf '%s\\n' \"$*\" >> '{bash_path(context['calls'])}'\n"
            f"printf '%s secret=%s\\n' \"$2\" \"${{UVARSI_TESCO_BRIDGE_SECRET:-none}}\""
            f" >> '{bash_path(env_log)}'\n",
        ),
        encoding="utf-8",
        newline="\n",
    )
    script = fake_deploy_state(
        context,
        "_uvarsi_require_tesco_bridge_transport() {\n"
        "  UVARSI_TESCO_BRIDGE_URL=https://bridge.example\n"
        "  UVARSI_TESCO_BRIDGE_SECRET=top-secret-value\n"
        "  export UVARSI_TESCO_BRIDGE_URL UVARSI_TESCO_BRIDGE_SECRET\n"
        "  return 0\n"
        "}\n",
    )

    run_supervisor(
        context,
        UVARSI_TEST_BRIDGE_PREFLIGHT="",
        UVARSI_DEPLOY_STATE_SCRIPT=script,
    )

    lines = env_log.read_text(encoding="utf-8").splitlines()
    collector_lines = [line for line in lines if "zbierac_akcii.py" in line]
    other_lines = [line for line in lines if "zbierac_akcii.py" not in line]
    assert collector_lines
    assert all(
        line.endswith("secret=top-secret-value") for line in collector_lines
    )
    assert other_lines, "refresh should run after a successful collection"
    assert all(line.endswith("secret=none") for line in other_lines)


def test_corrupt_failure_counter_does_not_disable_daily_limit(
        supervisor_environment):
    state = supervisor_environment["tmp_path"] / ".dozorca_state"
    state.write_text(f"{TODAY} not-a-number -\n", encoding="utf-8")

    result = run_supervisor(supervisor_environment, UVARSI_TEST_REFRESH_RC=1)

    assert "poškodený" in result.stdout
    fails = state.read_text(encoding="utf-8").split()[1]
    assert fails.isdigit()
