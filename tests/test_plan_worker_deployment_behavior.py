import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
BASH = Path("C:/Program Files/Git/bin/bash.exe")
LIBRARY = ROOT / "hetzner" / "uvarsi-deploy-state.sh"


def bash_path(path):
    return "/c" + Path(path).as_posix()[2:]


def write_executable(path, text):
    path.write_text(text, encoding="utf-8", newline="\n")
    path.chmod(0o755)


@pytest.fixture
def deployment(tmp_path):
    live = tmp_path / "live"
    app = live / "app"
    systemd = tmp_path / "systemd"
    web = tmp_path / "web"
    state = tmp_path / "state"
    app.mkdir(parents=True)
    systemd.mkdir()
    web.mkdir()
    state.mkdir()
    (app / "marker.txt").write_text("old-app", encoding="utf-8")
    (live / "VERSION").write_text("old-version", encoding="utf-8")
    with sqlite3.connect(live / "uvarsi.db") as con:
        con.execute(
            "CREATE TABLE plan_worker_state (singleton INTEGER PRIMARY KEY, heartbeat_at TEXT)"
        )
        con.execute(
            "INSERT INTO plan_worker_state VALUES (1, ?)",
            ("2026-08-28T16:10:20+00:00",),
        )
    unit = systemd / "uvarsi-plan-worker.service"
    unit.write_text("old-unit", encoding="utf-8")
    app_unit = systemd / "uvarsi.service"
    app_unit.write_text("old-app-unit", encoding="utf-8")
    (web / "index.html").write_bytes(b"old-index\r\n")
    (web / "sw.js").write_bytes(b"old-service-worker\n")
    for name in ("refresh_blocek.py", "dozorca.sh", "zaloha.sh",
                 "uvarsi-deploy-state.sh"):
        (live / name).write_bytes(f"old-{name}\n".encode())
    (state / "enabled").write_text("0", encoding="ascii")
    (state / "active").write_text("0", encoding="ascii")
    (state / "app-enabled").write_text("1", encoding="ascii")
    (state / "app-active").write_text("1", encoding="ascii")
    health = state / "health.json"
    health.write_text(
        json.dumps({
            "plan_queue": {
                "queued": 0,
                "oldest_seconds": None,
                "worker_alive": True,
                "heartbeat_seconds": 1,
                "heartbeat_at": "2026-08-28T16:10:20+00:00",
                "last_ready": None,
                "failed": 0,
                "blocking_code": None,
            }
        }),
        encoding="utf-8",
    )

    systemctl = tmp_path / "systemctl"
    write_executable(
        systemctl,
        "#!/bin/sh\n"
        "set -u\n"
        "cmd=$1\n"
        "shift\n"
        "[ \"${1:-}\" != \"--quiet\" ] || shift\n"
        "service=${1:-}\n"
        "enabled=enabled\n"
        "active=active\n"
        "[ \"$service\" != uvarsi ] || { enabled=app-enabled; active=app-active; }\n"
        "case \"$cmd\" in\n"
        "  is-enabled) [ \"$(cat \"$UVARSI_FAKE_STATE/$enabled\")\" = 1 ] ;;\n"
        "  is-active) [ \"$(cat \"$UVARSI_FAKE_STATE/$active\")\" = 1 ] ;;\n"
        "  enable) printf 1 > \"$UVARSI_FAKE_STATE/$enabled\" ;;\n"
        "  disable) printf 0 > \"$UVARSI_FAKE_STATE/$enabled\" ;;\n"
        "  start|restart) printf 1 > \"$UVARSI_FAKE_STATE/$active\" ;;\n"
        "  stop) printf 0 > \"$UVARSI_FAKE_STATE/$active\" ;;\n"
        "  daemon-reload)\n"
        "    if [ -f \"$UVARSI_FAKE_STATE/fail-reload-after\" ]; then\n"
        "      remaining=$(cat \"$UVARSI_FAKE_STATE/fail-reload-after\")\n"
        "      if [ \"$remaining\" -eq 0 ]; then\n"
        "        rm -f \"$UVARSI_FAKE_STATE/fail-reload-after\"\n"
        "        exit 9\n"
        "      fi\n"
        "      printf '%s' \"$((remaining - 1))\" > \"$UVARSI_FAKE_STATE/fail-reload-after\"\n"
        "    fi\n"
        "    [ ! -f \"$UVARSI_FAKE_STATE/fail-reload\" ]\n"
        "    ;;\n"
        "  *) exit 64 ;;\n"
        "esac\n",
    )
    curl = tmp_path / "curl"
    write_executable(curl, "#!/bin/sh\ncat \"$UVARSI_HEALTH_FILE\"\n")
    sleep = tmp_path / "sleep"
    write_executable(sleep, "#!/bin/sh\nexit 0\n")
    cp = tmp_path / "cp"
    write_executable(
        cp,
        "#!/bin/sh\n"
        "case \"$*\" in\n"
        "  *uvarsi-plan-worker.service*)\n"
        "    if [ -f \"$UVARSI_FAKE_STATE/fail-unit-copy-once\" ]; then\n"
        "      rm -f \"$UVARSI_FAKE_STATE/fail-unit-copy-once\"\n"
        "      exit 7\n"
        "    fi\n"
        "    ;;\n"
        "esac\n"
        "exec /usr/bin/cp \"$@\"\n",
    )
    env = os.environ | {
        "UVARSI_DIR": bash_path(live),
        "UVARSI_SYSTEMD_DIR": bash_path(systemd),
        "UVARSI_WEB_DIR": bash_path(web),
        "UVARSI_SYSTEMCTL": bash_path(systemctl),
        "UVARSI_CURL": bash_path(curl),
        "UVARSI_HEALTH_PY": bash_path(Path(sys.executable)),
        "UVARSI_HEALTH_FILE": bash_path(health),
        "UVARSI_SLEEP": bash_path(sleep),
        "UVARSI_CP": bash_path(cp),
        "UVARSI_FAKE_STATE": bash_path(state),
        "UVARSI_HEARTBEAT_ATTEMPTS": "1",
        "UVARSI_TEST_SNAPSHOT": "snapshot",
        "UVARSI_TEST_RELEASE": "release",
    }
    return {
        "base": tmp_path,
        "live": live,
        "app": app,
        "systemd": systemd,
        "web": web,
        "state": state,
        "health": health,
        "unit": unit,
        "app_unit": app_unit,
        "env": env,
        "snapshot": tmp_path / "snapshot",
        "release": tmp_path / "release",
    }


def run_library(deployment, command):
    return subprocess.run(
        [str(BASH), "-c", f'. "{bash_path(LIBRARY)}"\n{command}'],
        cwd=str(deployment["base"]),
        env=deployment["env"],
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
    )


@pytest.mark.parametrize(
    ("unit_exists", "enabled", "active"),
    [(True, False, False), (True, True, False), (True, True, True),
     (False, False, False)],
)
def test_snapshot_restore_preserves_exact_prior_worker_state(
        deployment, unit_exists, enabled, active):
    if not unit_exists:
        deployment["unit"].unlink()
    deployment["state"].joinpath("enabled").write_text(str(int(enabled)), encoding="ascii")
    deployment["state"].joinpath("active").write_text(str(int(active)), encoding="ascii")
    saved = run_library(deployment, 'uvarsi_snapshot "$UVARSI_TEST_SNAPSHOT"')
    assert saved.returncode == 0, saved.stdout + saved.stderr

    deployment["app"].joinpath("marker.txt").write_text("new-app", encoding="utf-8")
    deployment["unit"].write_text("new-unit", encoding="utf-8")
    deployment["state"].joinpath("enabled").write_text("1", encoding="ascii")
    deployment["state"].joinpath("active").write_text("1", encoding="ascii")
    restored = run_library(deployment, 'uvarsi_restore "$UVARSI_TEST_SNAPSHOT"')

    assert restored.returncode == 0, restored.stdout + restored.stderr
    assert deployment["app"].joinpath("marker.txt").read_text(encoding="utf-8") == "old-app"
    assert deployment["unit"].exists() is unit_exists
    if unit_exists:
        assert deployment["unit"].read_text(encoding="utf-8") == "old-unit"
    assert deployment["state"].joinpath("enabled").read_text(encoding="ascii") == str(int(enabled))
    assert deployment["state"].joinpath("active").read_text(encoding="ascii") == str(int(active))


def test_snapshot_failure_aborts_and_restore_failure_propagates(deployment):
    deployment["app"].rename(deployment["live"] / "missing-app")
    failed_backup = run_library(
        deployment, 'uvarsi_snapshot "$UVARSI_TEST_SNAPSHOT"'
    )
    assert failed_backup.returncode != 0

    deployment["live"].joinpath("missing-app").rename(deployment["app"])
    assert run_library(
        deployment, 'uvarsi_snapshot "$UVARSI_TEST_SNAPSHOT"'
    ).returncode == 0
    deployment["state"].joinpath("fail-reload").touch()
    failed_restore = run_library(
        deployment, 'uvarsi_restore "$UVARSI_TEST_SNAPSHOT"'
    )
    assert failed_restore.returncode != 0


def test_partial_live_mutation_rolls_back_before_returning_failure(deployment):
    assert run_library(
        deployment, 'uvarsi_snapshot "$UVARSI_TEST_SNAPSHOT"'
    ).returncode == 0
    release = deployment["release"]
    (release / "app").mkdir(parents=True)
    (release / "app" / "marker.txt").write_text("new-app", encoding="utf-8")
    (release / "VERSION").write_text("new-version", encoding="utf-8")
    (release / "hetzner").mkdir()
    (release / "hetzner" / "uvarsi-plan-worker.service").write_text("new-unit", encoding="utf-8")
    deployment["state"].joinpath("fail-unit-copy-once").touch()

    result = run_library(
        deployment,
        'uvarsi_install_core "$UVARSI_TEST_RELEASE" "$UVARSI_TEST_SNAPSHOT"',
    )

    assert result.returncode != 0
    assert deployment["app"].joinpath("marker.txt").read_text(encoding="utf-8") == "old-app"
    assert deployment["unit"].read_text(encoding="utf-8") == "old-unit"


def test_fresh_heartbeat_rejects_old_marker_and_accepts_strictly_newer(deployment):
    before = "2026-08-28T16:10:20+00:00"
    stale = run_library(deployment, f'uvarsi_wait_fresh_heartbeat "{before}"')
    assert stale.returncode != 0

    payload = json.loads(deployment["health"].read_text(encoding="utf-8"))
    payload["plan_queue"]["heartbeat_at"] = "2026-08-28T18:10:21+02:00"
    deployment["health"].write_text(json.dumps(payload), encoding="utf-8")
    fresh = run_library(deployment, f'uvarsi_wait_fresh_heartbeat "{before}"')

    assert fresh.returncode == 0, fresh.stdout + fresh.stderr


@pytest.mark.parametrize(
    ("unit_exists", "enabled", "active"),
    [(True, False, False), (True, True, True), (False, False, False)],
)
def test_manual_failure_restores_every_mutated_file_and_app_service_state(
        deployment, unit_exists, enabled, active):
    if not unit_exists:
        deployment["app_unit"].unlink()
    deployment["state"].joinpath("app-enabled").write_text(
        str(int(enabled)), encoding="ascii"
    )
    deployment["state"].joinpath("app-active").write_text(
        str(int(active)), encoding="ascii"
    )
    expected_files = {
        deployment["web"] / "index.html": b"old-index\r\n",
        deployment["web"] / "sw.js": b"old-service-worker\n",
        deployment["live"] / "refresh_blocek.py": b"old-refresh_blocek.py\n",
        deployment["live"] / "dozorca.sh": b"old-dozorca.sh\n",
        deployment["live"] / "zaloha.sh": b"old-zaloha.sh\n",
        deployment["live"] / "uvarsi-deploy-state.sh": b"old-uvarsi-deploy-state.sh\n",
    }
    release = deployment["release"]
    (release / "app").mkdir(parents=True)
    (release / "app" / "marker.txt").write_text("new-app", encoding="utf-8")
    (release / "VERSION").write_text("new-version", encoding="utf-8")
    (release / "index.html").write_bytes(b"new-index")
    (release / "sw.js").write_bytes(b"new-service-worker")
    (release / "hetzner").mkdir()
    (release / "hetzner" / "uvarsi-plan-worker.service").write_text(
        "new-worker-unit", encoding="utf-8"
    )
    (release / "hetzner" / "uvarsi.service").write_text(
        "new-app-unit", encoding="utf-8"
    )
    for name in ("refresh_blocek.py", "recepty.py", "dozorca.sh", "zaloha.sh",
                 "uvarsi-deploy-state.sh"):
        (release / "hetzner" / name).write_bytes(f"new-{name}".encode())

    assert run_library(
        deployment, 'uvarsi_snapshot "$UVARSI_TEST_SNAPSHOT"'
    ).returncode == 0
    deployment["state"].joinpath("fail-reload-after").write_text(
        "1", encoding="ascii"
    )
    result = run_library(
        deployment,
        'uvarsi_install_manual_release "$UVARSI_TEST_RELEASE" '
        '"$UVARSI_TEST_SNAPSHOT"',
    )

    assert result.returncode != 0
    assert not deployment["state"].joinpath("fail-reload-after").exists()
    assert deployment["app"].joinpath("marker.txt").read_text() == "old-app"
    for path, expected in expected_files.items():
        assert path.read_bytes() == expected
    assert not (deployment["live"] / "recepty.py").exists()
    assert deployment["app_unit"].exists() is unit_exists
    if unit_exists:
        assert deployment["app_unit"].read_text(encoding="utf-8") == "old-app-unit"
    assert deployment["state"].joinpath("app-enabled").read_text() == str(int(enabled))
    assert deployment["state"].joinpath("app-active").read_text() == str(int(active))
