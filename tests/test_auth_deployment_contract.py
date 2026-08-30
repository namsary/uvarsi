import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
AUTH_REQUIREMENTS = ROOT / "requirements-auth.txt"
DEV_REQUIREMENTS = ROOT / "requirements-dev.txt"
SAMOPULL = ROOT / "hetzner" / "samopull.sh"
MANUAL_DEPLOY = ROOT / "nasad.ps1"

AUTH_PINS = {
    "argon2-cffi": "25.1.0",
    "webauthn": "3.0.0",
}
PREFLIGHT_SECTION = "# --- 2. overenie PRED prepnutím ---"
SWITCH_BOUNDARY = "# --- 3. záloha aktuálneho stavu a prepnutie ---"


def _auth_requirement_versions(path):
    versions = {name: [] for name in AUTH_PINS}
    package_names = []
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        requirement = raw_line.split("#", 1)[0].strip()
        if not requirement:
            continue
        package, separator, version = requirement.partition("==")
        package = re.sub(r"[-_.]+", "-", package.strip()).lower()
        package_names.append(package)
        if separator and package in versions:
            versions[package].append(version.strip())
    return versions, package_names


def _find_posix_shell():
    for executable in ("bash", "sh"):
        shell = shutil.which(executable)
        if shell:
            return Path(shell)

    git = shutil.which("git")
    if not git:
        return None
    result = subprocess.run(
        [git, "--exec-path"], text=True, capture_output=True, check=False,
    )
    if result.returncode != 0:
        return None
    git_exec_path = Path(result.stdout.strip())
    for parent in (git_exec_path, *git_exec_path.parents):
        for relative in ("bin", "usr/bin"):
            search_path = str(parent / relative)
            for executable in ("bash", "sh"):
                shell = shutil.which(executable, path=search_path)
                if shell:
                    return Path(shell)
    return None


@pytest.fixture(scope="module")
def posix_shell():
    shell = _find_posix_shell()
    if shell is None:
        pytest.skip("no POSIX shell is available")
    return shell


def _shell_path(shell, path):
    result = subprocess.run(
        [
            str(shell),
            "-c",
            'if command -v cygpath >/dev/null 2>&1; then cygpath -u "$1"; '
            'else printf "%s" "$1"; fi',
            "path-converter",
            str(path.resolve()),
        ],
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def _required_release_files(pre_switch):
    match = re.search(r"for f in ([^;\n]+); do", pre_switch)
    assert match, "samopull required-files gate was not found"
    return shlex.split(match.group(1))


def test_auth_dependencies_are_exactly_pinned_for_development_and_production():
    """Removing or drifting either auth pin must block a release-ready build."""
    assert AUTH_REQUIREMENTS.exists(), "requirements-auth.txt must ship with the release"

    auth_versions, auth_packages = _auth_requirement_versions(AUTH_REQUIREMENTS)
    dev_versions, _ = _auth_requirement_versions(DEV_REQUIREMENTS)

    assert auth_packages == list(AUTH_PINS)
    for package, expected_version in AUTH_PINS.items():
        assert auth_versions[package] == [expected_version]
        assert dev_versions[package] == [expected_version]


def test_existing_caddy_contract_proxies_auth_pages_through_api_without_new_routes():
    """Auth HTML must fit the reviewed /api proxy; Caddy must not need a release edit."""
    script = MANUAL_DEPLOY.read_text(encoding="utf-8")
    match = re.search(r'blok = """(.*?)"""', script, re.DOTALL)
    assert match, "manual deploy must contain the generated Caddy site block"
    caddy = match.group(1)

    api = re.search(r"handle /api/\*\s*\{(.*?)\}", caddy, re.DOTALL)
    assert api and "reverse_proxy 127.0.0.1:8090" in api.group(1)
    assert "handle /potvrdenie" not in caddy
    assert "handle /heslo" not in caddy


@pytest.mark.parametrize("missing_module", ["argon2", "webauthn"])
def test_samopull_refuses_to_switch_when_auth_dependency_cannot_import(
        tmp_path, missing_module, posix_shell):
    """A failed real auth probe must exit before the live-mutation boundary."""
    script = SAMOPULL.read_text(encoding="utf-8")
    configuration = script.split("NTFY=", 1)[0]
    before_switch, boundary, _ = script.partition(SWITCH_BOUNDARY)
    assert boundary, "samopull release-switch boundary was not found"
    pre_switch = before_switch.split(PREFLIGHT_SECTION, 1)[1]

    release = tmp_path / "release with spaces"
    for relative in _required_release_files(pre_switch):
        target = release / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("present\n", encoding="utf-8")

    calls = tmp_path / "python-calls"
    switch_sentinel = tmp_path / "switch-reached"
    fake_python = tmp_path / "python"
    fake_python.write_text(
        "#!/bin/sh\n"
        f'printf "%s:%s\\n" "$UVARSI_MISSING_MODULE" "$2" >> '
        f'"{_shell_path(posix_shell, calls)}"\n'
        'if [ "$2" = "import argon2, webauthn" ]; then exit 23; fi\n'
        "exit 0\n",
        encoding="utf-8",
        newline="\n",
    )
    fake_python.chmod(0o755)

    command = (
        f"{configuration}\n"
        'log(){ printf "%s\\n" "$*"; }\n'
        "notify(){ :; }\n"
        f'CIEL="{_shell_path(posix_shell, release)}"\n'
        f'TMP="{_shell_path(posix_shell, tmp_path)}"\n'
        f"{PREFLIGHT_SECTION}\n{pre_switch}\n"
        f"{boundary}\n"
        f'printf reached > "{_shell_path(posix_shell, switch_sentinel)}"\n'
    )
    result = subprocess.run(
        [str(posix_shell), "-c", command],
        env=os.environ | {
            "UVARSI_MISSING_MODULE": missing_module,
            "UVARSI_PY": _shell_path(posix_shell, fake_python),
        },
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
    )

    assert not switch_sentinel.exists(), "samopull reached its real release-switch boundary"
    assert calls.exists(), "samopull did not execute the injected fake Python"
    assert calls.read_text(encoding="utf-8") == (
        f"{missing_module}:import argon2, webauthn\n"
    )
    assert result.returncode == 1
    assert "auth závislosti chýbajú — vydanie NEPREPÍNAM" in result.stdout
