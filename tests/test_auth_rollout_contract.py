import json
import os
from pathlib import Path
import re
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
SMOKE_HELPER = ROOT / "nastroje" / "over_auth_v3.ps1"
RUNBOOK = ROOT / "docs" / "prevadzka.md"


POWERSHELL_OFFLINE_HARNESS = r'''
param(
  [Parameter(Mandatory = $true)][string]$TargetScript,
  [Parameter(Mandatory = $true)][string]$Mode,
  [switch]$Mutating
)

$global:UvarsiTestMode = $Mode
$global:UvarsiTestSessionLabels = @{}
$global:UvarsiTestSessionStates = @{}
$global:UvarsiTestNextSession = 0
$global:UvarsiTestLogoutCalls = 0

function global:Get-TestSessionLabel($WebSession) {
  if ($null -eq $WebSession) { return "none" }
  $key = [Runtime.CompilerServices.RuntimeHelpers]::GetHashCode($WebSession).ToString()
  if (-not $global:UvarsiTestSessionLabels.ContainsKey($key)) {
    $labels = @("R", "A", "B", "C")
    $label = $labels[$global:UvarsiTestNextSession]
    $global:UvarsiTestNextSession += 1
    $global:UvarsiTestSessionLabels[$key] = $label
    $global:UvarsiTestSessionStates[$label] = $false
  }
  return $global:UvarsiTestSessionLabels[$key]
}

function global:Write-TestCall($Path, $Method, $Label, $MaximumRedirection, $HasMaximum) {
  $entry = [ordered]@{
    path = $Path
    method = $Method
    session = $Label
    maximum_redirection = $(if ($HasMaximum) { $MaximumRedirection } else { $null })
    state_before = $(if ($global:UvarsiTestSessionStates.ContainsKey($Label)) {
      $global:UvarsiTestSessionStates[$Label]
    } else { $false })
  }
  $line = ($entry | ConvertTo-Json -Compress) + "`n"
  [IO.File]::AppendAllText(
    $env:UVARSI_TEST_CALL_LOG,
    $line,
    [Text.UTF8Encoding]::new($false)
  )
}

function global:New-PublicResponse {
  if ($global:UvarsiTestMode -eq "capability_missing") {
    return [pscustomobject]@{ prihlaseny = $false }
  }
  if ($global:UvarsiTestMode -eq "capability_false") {
    return [pscustomobject]@{ prihlaseny = $false; auth_v3 = $false }
  }
  if ($global:UvarsiTestMode -eq "auth_v3_integer") {
    return [pscustomobject]@{ prihlaseny = $false; auth_v3 = 1 }
  }
  if ($global:UvarsiTestMode -eq "auth_v3_string") {
    return [pscustomobject]@{ prihlaseny = $false; auth_v3 = "true" }
  }
  return [pscustomobject]@{ prihlaseny = $false; auth_v3 = $true }
}

function global:New-IdentityResponse {
  $signedIn = $true
  $authV3 = $true
  $passwordConfigured = $true
  if ($global:UvarsiTestMode -eq "prihlaseny_integer") { $signedIn = 1 }
  if ($global:UvarsiTestMode -eq "prihlaseny_string") { $signedIn = "true" }
  if ($global:UvarsiTestMode -eq "identity_auth_v3_integer") { $authV3 = 1 }
  if ($global:UvarsiTestMode -eq "identity_auth_v3_string") { $authV3 = "true" }
  if ($global:UvarsiTestMode -eq "password_configured_integer") { $passwordConfigured = 1 }
  if ($global:UvarsiTestMode -eq "password_configured_string") { $passwordConfigured = "true" }
  return [pscustomobject]@{
    prihlaseny = $signedIn
    auth_v3 = $authV3
    password_configured = $passwordConfigured
    id = 42
    email = "test@example.com"
  }
}

function global:Invoke-RestMethod {
  param(
    $Uri,
    $Method,
    $TimeoutSec,
    $ErrorAction,
    $WebSession,
    $Headers,
    $Body,
    $ContentType,
    $MaximumRedirection
  )

  $path = ([Uri]$Uri).AbsolutePath
  $label = Get-TestSessionLabel $WebSession
  $hasMaximum = $PSBoundParameters.ContainsKey("MaximumRedirection")
  Write-TestCall $path $Method $label $MaximumRedirection $hasMaximum

  if ($global:UvarsiTestMode -eq "request_error_secret" -and $path -eq "/api/health") {
    throw [InvalidOperationException]::new("SECRET_RESPONSE_MARKER")
  }
  if ($global:UvarsiTestMode -eq "redirect_health" -and $path -eq "/api/health") {
    if ($hasMaximum -and $MaximumRedirection -eq 0) {
      throw [InvalidOperationException]::new("redirect blocked")
    }
    return [pscustomobject]@{
      vydanie = "offline-test"
      tyzden = "2026-08-24"
      plan_queue = [pscustomobject]@{ worker_alive = $true }
    }
  }
  if ($path -eq "/api/health") {
    return [pscustomobject]@{
      vydanie = "offline-test"
      tyzden = "2026-08-24"
      plan_queue = [pscustomobject]@{ worker_alive = $true }
    }
  }
  if ($path -eq "/api/me") {
    if ($global:UvarsiTestSessionStates[$label] -eq $true) {
      return New-IdentityResponse
    }
    return New-PublicResponse
  }
  if ($path -eq "/api/auth/login") {
    $global:UvarsiTestSessionStates[$label] = $true
    $ok = $true
    if ($global:UvarsiTestMode -eq "ok_integer") { $ok = 1 }
    if ($global:UvarsiTestMode -eq "ok_string") { $ok = "true" }
    return [pscustomobject]@{ ok = $ok; redirect = "/app" }
  }
  if ($path -eq "/api/auth/logout") {
    $global:UvarsiTestLogoutCalls += 1
    $global:UvarsiTestSessionStates[$label] = $false
    $ok = $true
    if ($global:UvarsiTestMode -eq "logout_ok_integer" -and $global:UvarsiTestLogoutCalls -eq 1) { $ok = 1 }
    return [pscustomobject]@{ ok = $ok }
  }
  if ($path -eq "/api/auth/sessions/logout-others") {
    if ($global:UvarsiTestMode -eq "logout_others_revokes_current") {
      $global:UvarsiTestSessionStates["A"] = $false
      $global:UvarsiTestSessionStates["B"] = $false
    } elseif ($global:UvarsiTestMode -eq "logout_others_keeps_other") {
      $global:UvarsiTestSessionStates["A"] = $true
      $global:UvarsiTestSessionStates["B"] = $true
    } else {
      $global:UvarsiTestSessionStates["A"] = $true
      $global:UvarsiTestSessionStates["B"] = $false
    }
    return [pscustomobject]@{ ok = $true }
  }
  throw [InvalidOperationException]::new("unexpected offline route")
}

$arguments = @{
  BaseUrl = "http://127.0.0.1:1"
  ExpectedOrigin = "http://127.0.0.1:1"
}
if ($Mutating) {
  $secure = [Security.SecureString]::new()
  foreach ($character in "offline-test-password".ToCharArray()) {
    $secure.AppendChar($character)
  }
  $secure.MakeReadOnly()
  $arguments["Credential"] = [Management.Automation.PSCredential]::new(
    "test@example.com", $secure
  )
  $arguments["AllowMutation"] = $true
}
& $TargetScript @arguments
exit $LASTEXITCODE
'''


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


@pytest.fixture(scope="module")
def windows_powershell_51():
    executable = shutil.which("powershell.exe")
    if executable is None:
        system_root = os.environ.get("SystemRoot", r"C:\Windows")
        candidate = Path(system_root) / "System32/WindowsPowerShell/v1.0/powershell.exe"
        if candidate.is_file():
            executable = str(candidate)
    if executable is None:
        pytest.skip("Windows PowerShell 5.1 is unavailable")

    version = subprocess.run(
        [
            executable,
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            "$PSVersionTable.PSVersion.ToString()",
        ],
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
        timeout=15,
    )
    assert version.returncode == 0, version.stderr
    assert version.stdout.strip().startswith("5.1"), version.stdout
    return executable


def _run_offline_smoke(tmp_path, powershell, mode, *, mutating=False):
    harness = tmp_path / "offline-auth-v3-harness.ps1"
    calls_path = tmp_path / "calls.jsonl"
    harness.write_text(POWERSHELL_OFFLINE_HARNESS, encoding="utf-8", newline="\n")
    command = [
        powershell,
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(harness),
        "-TargetScript",
        str(SMOKE_HELPER),
        "-Mode",
        mode,
    ]
    if mutating:
        command.append("-Mutating")
    result = subprocess.run(
        command,
        cwd=ROOT,
        env=os.environ | {"UVARSI_TEST_CALL_LOG": str(calls_path)},
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
        timeout=30,
    )
    calls = []
    if calls_path.is_file():
        calls = [json.loads(line) for line in calls_path.read_text(encoding="utf-8").splitlines()]
    return result, calls


def _output_lines(result):
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def test_rollout_artifacts_exist_at_the_canonical_paths():
    """Removing either shipped rollout artifact must make Task 10 incomplete."""
    assert SMOKE_HELPER.is_file()
    assert RUNBOOK.is_file()


@pytest.mark.parametrize("mode", ["capability_missing", "capability_false"])
def test_read_only_probe_requires_auth_v3_true(tmp_path, windows_powershell_51, mode):
    """A pre-activation or flag-off response must stop the post-activation probe."""
    result, calls = _run_offline_smoke(tmp_path, windows_powershell_51, mode)

    assert result.returncode == 1
    assert _output_lines(result)[-1] == "FAIL [capability-preflight]"
    assert [call["path"] for call in calls] == ["/api/health", "/api/me"]


def test_redirect_is_blocked_without_following_or_false_success(
        tmp_path, windows_powershell_51):
    """Removing MaximumRedirection=0 must let the redirect double false-pass."""
    result, calls = _run_offline_smoke(
        tmp_path, windows_powershell_51, "redirect_health"
    )

    assert result.returncode == 1
    assert _output_lines(result)[-1] == "FAIL [health]"
    assert calls == [
        {
            "path": "/api/health",
            "method": "GET",
            "session": "R",
            "maximum_redirection": 0,
            "state_before": False,
        }
    ]


@pytest.mark.parametrize(
    ("mode", "mutating", "phase"),
    [
        ("auth_v3_integer", False, "capability-preflight"),
        ("auth_v3_string", False, "capability-preflight"),
        ("ok_integer", True, "login-session-a"),
        ("ok_string", True, "login-session-a"),
        ("prihlaseny_integer", True, "identity-session-a"),
        ("prihlaseny_string", True, "identity-session-a"),
        ("identity_auth_v3_integer", True, "identity-session-a"),
        ("identity_auth_v3_string", True, "identity-session-a"),
        ("password_configured_integer", True, "identity-session-a"),
        ("password_configured_string", True, "identity-session-a"),
        ("logout_ok_integer", True, "logout-current-session"),
    ],
)
def test_required_booleans_reject_integer_and_string_coercions(
        tmp_path, windows_powershell_51, mode, mutating, phase):
    """PowerShell truthy coercion must not satisfy an API boolean contract."""
    result, _ = _run_offline_smoke(
        tmp_path, windows_powershell_51, mode, mutating=mutating
    )

    assert result.returncode == 1
    assert _output_lines(result)[-1] == f"FAIL [{phase}]"


def test_safe_default_calls_read_only_routes_only(tmp_path, windows_powershell_51):
    """Removing the default exit must expose a mutation in the offline call trace."""
    result, calls = _run_offline_smoke(
        tmp_path, windows_powershell_51, "success"
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert [(call["method"], call["path"]) for call in calls] == [
        ("GET", "/api/health"),
        ("GET", "/api/me"),
    ]
    assert all(call["maximum_redirection"] == 0 for call in calls)


def test_authorized_flow_executes_and_proves_session_postconditions(
        tmp_path, windows_powershell_51):
    """Dropping any session transition must break the executable smoke contract."""
    result, calls = _run_offline_smoke(
        tmp_path, windows_powershell_51, "success", mutating=True
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert all(call["maximum_redirection"] == 0 for call in calls)
    assert [(call["method"], call["path"], call["session"]) for call in calls] == [
        ("GET", "/api/health", "R"),
        ("GET", "/api/me", "R"),
        ("POST", "/api/auth/login", "A"),
        ("GET", "/api/me", "A"),
        ("POST", "/api/auth/login", "B"),
        ("GET", "/api/me", "B"),
        ("POST", "/api/auth/logout", "A"),
        ("GET", "/api/me", "A"),
        ("GET", "/api/me", "B"),
        ("POST", "/api/auth/login", "A"),
        ("GET", "/api/me", "A"),
        ("POST", "/api/auth/sessions/logout-others", "A"),
        ("GET", "/api/me", "A"),
        ("GET", "/api/me", "B"),
        ("POST", "/api/auth/logout", "A"),
        ("GET", "/api/me", "A"),
    ]
    assert calls[12]["state_before"] is True
    assert calls[13]["state_before"] is False
    assert calls[14]["state_before"] is True
    assert calls[15]["state_before"] is False
    output = _output_lines(result)
    for phase in (
        "logout-current-session",
        "other-session-survives",
        "password-fallback",
        "logout-others-current-survives",
        "logout-others-other-revoked",
        "cleanup-current-session",
        "mutating-smoke-complete",
    ):
        assert f"OK [{phase}]" in output


@pytest.mark.parametrize(
    ("mode", "phase"),
    [
        ("logout_others_revokes_current", "logout-others-current-survives"),
        ("logout_others_keeps_other", "logout-others-other-revoked"),
    ],
)
def test_logout_others_checks_both_postconditions(
        tmp_path, windows_powershell_51, mode, phase):
    """Calling logout-others without checking both sessions must not pass."""
    result, _ = _run_offline_smoke(
        tmp_path, windows_powershell_51, mode, mutating=True
    )

    assert result.returncode == 1
    assert _output_lines(result)[-1] == f"FAIL [{phase}]"


def test_request_errors_expose_only_the_concise_phase(
        tmp_path, windows_powershell_51):
    """A secret-bearing request exception must never reach process output."""
    result, _ = _run_offline_smoke(
        tmp_path, windows_powershell_51, "request_error_secret"
    )

    combined = result.stdout + result.stderr
    assert result.returncode == 1
    assert "SECRET_RESPONSE_MARKER" not in combined
    assert "request failed" not in combined
    assert _output_lines(result)[-1] == "FAIL [health]"


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

    assert re.search(r"MaximumRedirection\s*=\s*0", script, re.I)

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
        "/api/auth/sessions/logout-others",
        "logout-others-current-survives",
        "logout-others-other-revoked",
        "cleanup-current-session",
    ):
        assert required in script

    for property_name in ("auth_v3", "password_configured", "prihlaseny"):
        assert re.search(
            rf'Assert-ExactBoolean\s+\$Response\s+"{property_name}"\s+\$true',
            script,
            re.I,
        )
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
        "passkey on a supported phone is required",
        "logout-current and logout-others are required",
        "webauthn is a manual browser/pwa ceremony",
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
