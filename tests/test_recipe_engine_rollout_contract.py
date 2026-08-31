"""Executable contract for the mode-aware autonomous recipe guardian."""

from __future__ import annotations

from datetime import date
import json
import os
from pathlib import Path
import subprocess
import sys

from app.landing_data import write_landing_data_atomic


ROOT = Path(__file__).resolve().parents[1]
BASH = Path("C:/Program Files/Git/bin/bash.exe")


def _bash(path):
    return "/c" + Path(path).as_posix()[2:]


def _landing(path):
    write_landing_data_atomic(
        path,
        {
            "schema_version": 1,
            "generated_at": "2026-08-31T05:00:00+02:00",
            "week": "2026-08-31",
            "week_label": "31. 8.–6. 9. 2026",
            "sources": [],
            "receipt": {
                "meals": [{"day": "PO", "name": "Test", "items": []}],
                "nakup_spolu": "1,00",
                "bezne": "2,00",
                "usetris": "1,00",
            },
        },
    )


def _health(mode, *, ready, blockers=(), worker_alive=True, queued=0):
    return json.dumps(
        {
            "recipe_engine": {
                "mode": mode,
                "library_version": 1,
                "active_templates": 60,
                "coverage": {
                    "standard": 50,
                    "high_protein": 24,
                    "vegetarian": 20,
                    "vegan": 12,
                },
                "last_shadow": None,
                "p95_ms": None,
                "ready": ready,
                "blockers": list(blockers),
            },
            "plan_queue": {
                "queued": queued,
                "oldest_seconds": None if queued == 0 else 240,
                "worker_alive": worker_alive,
                "heartbeat_seconds": None if not worker_alive else 1,
                "heartbeat_at": None if not worker_alive else "2026-08-31T05:00:00+00:00",
                "last_ready": None,
                "failed": 0,
                "blocking_code": None if worker_alive else "worker_heartbeat_stale",
            },
        },
        separators=(",", ":"),
    )


def _run(tmp_path, health_values, *, smoke_exit=0):
    (tmp_path / "app").mkdir(exist_ok=True)
    landing = tmp_path / "landing_data.json"
    _landing(landing)
    calls = tmp_path / "calls.txt"
    notifications = tmp_path / "notifications.txt"
    health_counter = tmp_path / "health-counter.txt"
    health_file = tmp_path / "health-values.txt"
    health_file.write_text("\n".join(health_values) + "\n", encoding="utf-8", newline="\n")

    fake_python = tmp_path / "python"
    fake_python.write_text(
        "#!/bin/sh\n"
        "if [ \"$1\" = \"-c\" ]; then exit 0; fi\n"
        f"printf '%s\\n' \"$*\" >> '{_bash(calls)}'\n"
        "case \"$*\" in\n"
        f"  *--recipe-engine-smoke*) exit {smoke_exit} ;;\n"
        "esac\n"
        "exit 0\n",
        encoding="utf-8",
        newline="\n",
    )
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
        f"  *ntfy.sh/*) printf '%s\\n' \"$*\" >> '{_bash(notifications)}'; exit 0 ;;\n"
        "  *api/health*)\n"
        f"    N=$(cat '{_bash(health_counter)}' 2>/dev/null || echo 0); N=$((N+1)); echo \"$N\" > '{_bash(health_counter)}';\n"
        f"    sed -n \"${{N}}p\" '{_bash(health_file)}'; exit 0 ;;\n"
        "esac\n"
        "exit 0\n",
        encoding="utf-8",
        newline="\n",
    )
    fake_curl.chmod(0o755)
    env = os.environ | {
        "UVARSI_DIR": _bash(tmp_path),
        "UVARSI_LANDING_DATA": _bash(landing),
        "UVARSI_PY": _bash(fake_python),
        "UVARSI_HEALTH_PY": _bash(Path(sys.executable)),
        "UVARSI_CURL": _bash(fake_curl),
        "UVARSI_TODAY": "2026-08-31",
        "UVARSI_DOZORCA_LOCKED": "1",
        "UVARSI_PLAN_QUEUE_HEALTH_URL": "http://127.0.0.1:8090/api/health",
        "UVARSI_RECIPE_SMOKE_STATE": _bash(tmp_path / "recipe-smoke.json"),
        "PATH": f"{_bash(tmp_path)}:/usr/bin",
    }
    result = subprocess.run(
        [str(BASH), _bash(ROOT / "hetzner" / "dozorca.sh")],
        cwd=ROOT,
        env=env,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
    )
    return result, calls, notifications


def test_on_mode_runs_one_local_non_public_smoke_then_requires_ready_health(tmp_path):
    result, calls, notifications = _run(
        tmp_path,
        [
            _health("on", ready=False, blockers=("smoke_missing",), worker_alive=False),
            _health("on", ready=True, worker_alive=False),
        ],
    )

    assert result.returncode == 0, result.stdout + result.stderr
    recorded = calls.read_text(encoding="utf-8")
    assert recorded.count("--recipe-engine-smoke") == 1
    assert "@" not in recorded
    assert "token" not in recorded.casefold()
    assert not notifications.exists()


def test_on_mode_notifies_immediately_when_synthetic_smoke_fails_and_keeps_cache(
    tmp_path,
):
    cache = tmp_path / "cached-plan.json"
    cache.write_text("keep-me", encoding="utf-8")
    result, calls, notifications = _run(
        tmp_path,
        [_health("on", ready=False, blockers=("smoke_missing",), worker_alive=False)],
        smoke_exit=7,
    )

    assert result.returncode != 0
    assert calls.read_text(encoding="utf-8").count("--recipe-engine-smoke") == 1
    assert notifications.read_text(encoding="utf-8").count("Uvar.si: receptový engine") == 1
    assert cache.read_text(encoding="utf-8") == "keep-me"


def test_on_mode_does_not_alert_for_idle_legacy_worker_when_queue_is_empty(tmp_path):
    result, _calls, notifications = _run(
        tmp_path, [_health("on", ready=True, worker_alive=False)]
    )

    assert result.returncode == 0
    assert not notifications.exists()


def test_off_and_shadow_keep_existing_monitoring_without_running_synthetic_smoke(
    tmp_path,
):
    for mode in ("off", "shadow"):
        case = tmp_path / mode
        case.mkdir()
        result, calls, _notifications = _run(
            case,
            [_health(mode, ready=(mode == "off"), worker_alive=True)],
        )
        assert result.returncode == 0
        recorded = calls.read_text(encoding="utf-8") if calls.exists() else ""
        assert "--recipe-engine-smoke" not in recorded


def test_malformed_recipe_health_types_fail_closed_before_any_smoke(tmp_path):
    malformed = json.loads(_health("on", ready=True))
    malformed["recipe_engine"]["ready"] = 1
    result, calls, notifications = _run(
        tmp_path, [json.dumps(malformed, separators=(",", ":"))]
    )

    assert result.returncode != 0
    recorded = calls.read_text(encoding="utf-8") if calls.exists() else ""
    assert "--recipe-engine-smoke" not in recorded
    assert notifications.read_text(encoding="utf-8").count("Uvar.si: receptový engine") == 1
    assert "CHYBA" in result.stdout


def test_non_smoke_blocker_is_never_masked_by_smoke_missing(tmp_path):
    result, calls, notifications = _run(
        tmp_path,
        [
            _health(
                "on", ready=False,
                blockers=("catalog_load_failed", "smoke_missing"),
                worker_alive=False,
            )
        ],
    )

    assert result.returncode != 0
    recorded = calls.read_text(encoding="utf-8") if calls.exists() else ""
    assert "--recipe-engine-smoke" not in recorded
    assert notifications.read_text(encoding="utf-8").count("Uvar.si: receptový engine") == 1
