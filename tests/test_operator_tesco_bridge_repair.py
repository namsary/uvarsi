from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "ops" / "repair_tesco_bridge.ps1"


def test_operator_bridge_repair_self_test_is_offline_and_safe() -> None:
    result = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(SCRIPT),
            "-SelfTest",
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
        env={
            **os.environ,
            "ComSpec": r"Z:\definitely-missing-uvarsi-cmd.exe",
        },
    )

    combined = result.stdout + result.stderr
    assert result.returncode == 0, combined
    assert "SELFTEST_OK" in combined
    assert "RECOVERY_STATES_OK" in combined
    assert "VERSION_BASE_OK" in combined
    assert "WHOAMI_RETRY_OK" in combined
    assert "NATIVE_STDERR_RETRY_OK" in combined
    assert "Cloudflare kontrola docasne zlyhala" not in combined
    assert "unit-secret-material" not in combined


def test_operator_bridge_repair_local_preflight_uses_verified_toolchain() -> None:
    result = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(SCRIPT),
            "-LocalPreflight",
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
        env={
            **os.environ,
            "NODE_OPTIONS": r"--require Z:\definitely-missing-uvarsi-hook.js",
            "NODE_PATH": r"Z:\definitely-missing-uvarsi-modules",
        },
    )

    combined = result.stdout + result.stderr
    assert result.returncode == 0, combined
    assert "LOCAL_PREFLIGHT_OK" in combined
    assert "jarvis" not in combined.lower()


def test_embedded_server_updater_preserves_env_and_locks_payments(tmp_path: Path) -> None:
    source = SCRIPT.read_text(encoding="utf-8")
    match = re.search(r"<<'PY'\r?\n(?P<body>.*?)\r?\nPY", source, re.DOTALL)
    assert match is not None

    release = "0123456789abcdef0123456789abcdef01234567"
    secret = release + ".unit-secret-material-0123456789abcdef"
    env_file = tmp_path / "uvarsi.env"
    env_file.write_text(
        "UNRELATED=value\n"
        "UVARSI_ENV=staging\n"
        "UVARSI_TESCO_BRIDGE_SECRET=old-secret\n"
        "UVARSI_TESCO_BRIDGE_SECRET=duplicate-old-secret\n"
        "PLATBY_ZAPNUTE=1\n"
        "UVARSI_PAYMENTS_ENABLED=1\n",
        encoding="utf-8",
    )
    payload_file = tmp_path / "payload.json"
    payload_file.write_text(
        json.dumps(
            {
                "environment": "production",
                "url": "https://uvarsi-tesco-bridge.example.workers.dev",
                "host_name": "uvarsi-tesco-bridge.example.workers.dev",
                "release": release,
                "version_id": "22222222-2222-4222-8222-222222222222",
                "secret": secret,
            }
        ),
        encoding="utf-8",
    )
    backup_dir = tmp_path / "backups"

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            match.group("body"),
            str(env_file),
            str(payload_file),
            str(backup_dir),
        ],
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )

    combined = result.stdout + result.stderr
    assert result.returncode == 0, combined
    assert "SERVER_ENV_SYNCED" in combined
    assert secret not in combined
    updated = env_file.read_text(encoding="utf-8")
    assert "UNRELATED=value" in updated
    assert updated.count("UVARSI_TESCO_BRIDGE_SECRET=") == 1
    assert f"UVARSI_TESCO_BRIDGE_SECRET={secret}" in updated
    assert updated.count("PLATBY_ZAPNUTE=0") == 1
    assert updated.count("UVARSI_PAYMENTS_ENABLED=0") == 1
    assert "PLATBY_ZAPNUTE=1" not in updated
    assert "UVARSI_PAYMENTS_ENABLED=1" not in updated
    if os.name != "nt":
        assert os.stat(env_file).st_mode & 0o777 == 0o600
    backups = list(backup_dir.iterdir())
    assert len(backups) == 1
    assert "duplicate-old-secret" in backups[0].read_text(encoding="utf-8")
