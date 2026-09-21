from __future__ import annotations

import os
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "ops" / "repair_tesco_bridge.ps1"
RUNBOOK = ROOT / "docs" / "prevadzka.md"


def _run_wrapper(tmp_path: Path, exit_code: int) -> tuple[subprocess.CompletedProcess[str], str]:
    args_file = tmp_path / "ssh-args.txt"
    fake_ssh = tmp_path / "ssh.cmd"
    fake_ssh.write_text(
        "@echo off\r\n"
        'echo %* > "%UVARSI_SSH_ARGS_FILE%"\r\n'
        "exit /b %UVARSI_SSH_EXIT%\r\n",
        encoding="ascii",
    )
    result = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(SCRIPT),
        ],
        cwd=ROOT,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        timeout=30,
        check=False,
        env={
            **os.environ,
            "PATH": str(tmp_path) + os.pathsep + os.environ.get("PATH", ""),
            "UVARSI_SSH_ARGS_FILE": str(args_file),
            "UVARSI_SSH_EXIT": str(exit_code),
        },
    )
    args = args_file.read_text(encoding="utf-8").strip() if args_file.exists() else ""
    return result, args


def test_operator_bridge_repair_is_an_ssh_only_wrapper() -> None:
    source = SCRIPT.read_text(encoding="utf-8")
    lowered = source.casefold()

    assert "& ssh jarvis 'sudo /opt/uvarsi/uvarsi-deploy-state.sh repair-tesco-bridge'" in source
    for forbidden in (
        "wrangler",
        "whoami",
        "cloudflare_api_token",
        "cf_api_token",
        "node.exe",
        "versions secret bulk",
        "versions deploy",
        "selftest",
        "localpreflight",
    ):
        assert forbidden not in lowered


def test_runbook_uses_scoped_server_token_not_wrangler_oauth() -> None:
    runbook = RUNBOOK.read_text(encoding="utf-8")
    section = runbook.split("## Tesco bridge", 1)[1]

    assert "uvarsi-tesco-bridge" in section
    assert "Editor" in section
    assert "/etc/uvarsi/secrets/cloudflare-worker-token" in section
    assert "install-cloudflare-token" in section
    assert "verify-cloudflare-token" in section
    assert "repair-tesco-bridge" in section
    assert "wrangler login" not in section.casefold()
    assert "npx wrangler secret put" not in section.casefold()


def test_operator_bridge_repair_calls_only_the_server_command(tmp_path: Path) -> None:
    result, args = _run_wrapper(tmp_path, exit_code=0)
    combined = result.stdout + result.stderr

    assert result.returncode == 0, combined
    assert args.replace('"', "") == (
        "jarvis sudo /opt/uvarsi/uvarsi-deploy-state.sh repair-tesco-bridge"
    )
    assert "server" in combined.casefold()


def test_operator_bridge_repair_propagates_failure_and_keeps_payments_off(
    tmp_path: Path,
) -> None:
    result, args = _run_wrapper(tmp_path, exit_code=75)
    combined = result.stdout + result.stderr

    assert result.returncode != 0
    assert args.replace('"', "") == (
        "jarvis sudo /opt/uvarsi/uvarsi-deploy-state.sh repair-tesco-bridge"
    )
    assert "platby zostali vypnute" in combined.casefold()
