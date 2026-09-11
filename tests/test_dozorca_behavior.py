import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
BASH = Path("C:/Program Files/Git/bin/bash.exe")
TODAY = "2026-08-18"
WEEK = "2026-08-17"


def bash_path(path):
    return "/c" + Path(path).as_posix()[2:]


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
        "PATH": f"{bash_path(tmp_path)}:/usr/bin",
    }
    return {
        "tmp_path": tmp_path,
        "landing_data": landing_data,
        "original": original,
        "calls": calls,
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


def configure_structural_collection(context):
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
        "  *zbierac_akcii.py*) echo 'ZBER_STRUKTURALNY: malformed source'; exit 1 ;;\n"
        "  *refresh_blocek.py*) exit 99 ;;\n"
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
        "  *'SELECT lower(v.o)'*) echo lidl ;;\n"
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
