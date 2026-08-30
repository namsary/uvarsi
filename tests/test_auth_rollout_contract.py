from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]
SMOKE_HELPER = ROOT / "nastroje" / "over_auth_v3.ps1"
RUNBOOK = ROOT / "docs" / "prevadzka.md"


def _read_required(path: Path) -> str:
    assert path.is_file(), f"required rollout artifact is missing: {path.relative_to(ROOT)}"
    return path.read_text(encoding="utf-8")


def _powershell_braced_block(source: str, marker: str) -> str:
    marker_at = source.find(marker)
    assert marker_at >= 0, f"PowerShell gate is missing: {marker}"
    opening = source.find("{", marker_at + len(marker))
    assert opening >= 0, f"PowerShell gate has no body: {marker}"
    depth = 0
    for index in range(opening, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[opening + 1:index]
    raise AssertionError(f"PowerShell gate is not closed: {marker}")


def test_rollout_artifacts_exist_at_the_canonical_paths():
    """Removing either shipped rollout artifact must make Task 10 incomplete."""
    assert SMOKE_HELPER.is_file()
    assert RUNBOOK.is_file()


def test_smoke_helper_is_read_only_until_mutation_is_explicitly_authorized():
    """A default invocation must not create login sessions or send registration mail."""
    script = _read_required(SMOKE_HELPER)

    assert re.search(r"\[switch\]\s*\$AllowMutation\b", script, re.I)
    assert re.search(
        r"if\s*\(\s*-not\s+\$AllowMutation\s*\)\s*\{.*?\bexit\s+0\b.*?\}",
        script,
        re.I | re.S,
    )
    assert "MUTATING" in script
    assert re.search(r"Write-Warning\s+['\"].*MUTATING", script, re.I)

    registration_gate = _powershell_braced_block(
        script, "if ($AllowDisposableRegistrationProbe)"
    )
    assert "/api/auth/register" in registration_gate
    assert re.search(r"\[switch\]\s*\$AllowDisposableRegistrationProbe\b", script, re.I)
    assert re.search(
        r"\[System\.Management\.Automation\.PSCredential\]\s*"
        r"\$DisposableRegistrationCredential\b",
        script,
        re.I,
    )


def test_smoke_helper_keeps_credentials_and_sessions_in_memory_without_echoing_them():
    """Adding secret-bearing output or disk cookie storage must fail review."""
    script = _read_required(SMOKE_HELPER)

    assert re.search(
        r"\[System\.Management\.Automation\.PSCredential\]\s*\$Credential\b",
        script,
        re.I,
    )
    assert len(re.findall(r"New-Object\s+Microsoft\.PowerShell\.Commands\.WebRequestSession", script, re.I)) >= 2

    forbidden_persistence = (
        "Start-Transcript",
        "Export-Clixml",
        "ConvertFrom-SecureString",
        "cookiejar",
        "cookie-jar",
        "cookies.txt",
        "-OutFile",
        "Add-Content",
        "Set-Content",
    )
    for construct in forbidden_persistence:
        assert construct.lower() not in script.lower()

    sensitive_output = re.compile(
        r"(?im)^\s*(?:Write-(?:Host|Output|Warning|Error|Verbose|Debug)|Out-Host)\b"
        r"[^\r\n]*(?:\$Credential|\$Password|\$Body|\$Response|\$Result|\$Email|\$_)"
    )
    assert not sensitive_output.search(script)
    assert re.search(r"catch\s*\{\s*throw\s+\[[^\]]*\]::new\([^$]*\)\s*\}", script, re.I | re.S)


def test_smoke_helper_checks_origin_registration_and_multi_session_password_flow():
    """Dropping a smoke phase must be visible before the controller uses the helper."""
    script = _read_required(SMOKE_HELPER)

    for required in (
        "$ExpectedOrigin",
        "GetLeftPart",
        "/api/health",
        "/api/auth/register",
        "/api/auth/login",
        "/api/me",
        "/api/auth/logout",
        "registration-response-shape",
        "login-session-a",
        "login-session-b",
        "identity-session-a",
        "identity-session-b",
        "logout-current-session",
        "other-session-survives",
        "password-fallback",
    ):
        assert required in script

    assert re.search(r"\.auth_v3\s*-ne\s*\$true", script, re.I)
    assert re.search(r"\.password_configured\s*-ne\s*\$true", script, re.I)
    assert re.search(r"\.prihlaseny\s*-ne\s*\$true", script, re.I)
    assert re.search(r"\.id\s*-ne\s*\$", script, re.I)
    assert re.search(r"\.email\s*-ne\s*\$", script, re.I)


def test_runbook_has_staged_stop_gates_and_non_secret_evidence():
    """Removing a rollout gate must leave the operational procedure unsafe."""
    runbook = _read_required(RUNBOOK).lower()

    for required in (
        "preflight",
        "requirements-auth.txt",
        "150–350 ms",
        "memory pressure",
        "sqlite online backup",
        "pragma integrity_check",
        "pouzivatelia",
        "naroky",
        "sessions_v2",
        "plany",
        "uvarsi_auth_v3=0",
        "/api/health",
        "/co-varit-tento-tyzden",
        "fresh heartbeat",
        "existing old session",
        "second hosted app",
        "uvarsi_auth_v3=1",
        "desktop",
        "mobile",
        "pwa",
        "payments stay off",
        "taktik-mapa",
        "caddy",
        "evidence checklist",
        "without secrets",
    ):
        assert required in runbook

    assert runbook.count("stop gate") >= 6
    assert "counts only" in runbook
    assert "no pii" in runbook


def test_runbook_activation_and_rollback_touch_only_uvarsi_and_never_restore_db():
    """Broad service control or data rollback must never enter the auth-v3 runbook."""
    runbook = _read_required(RUNBOOK).lower()

    assert runbook.count("systemctl restart uvarsi") >= 2
    dangerous_service_action = re.compile(
        r"systemctl\s+(?:restart|start|stop|reload|try-restart)\s+"
        r"(?:caddy|taktik[^\s]*|uvarsi-plan-worker|\*|--all)\b",
        re.I,
    )
    assert not dangerous_service_action.search(runbook)
    service_actions = re.findall(
        r"systemctl\s+(?:restart|start|stop|reload|try-restart)\s+([^\s`]+)",
        runbook,
        re.I,
    )
    assert service_actions and set(service_actions) == {"uvarsi"}

    for forbidden in (
        "systemctl daemon-reload",
        "service --status-all",
        "pkill",
        "killall",
        "crontab -",
        "db rollback",
        "restore the database",
        "sqlite3 /opt/uvarsi/uvarsi.db <",
    ):
        assert forbidden not in runbook

    assert "rollback is flag off only" in runbook
    assert "never roll back the database" in runbook
    assert "restart only uvarsi" in runbook
