import os
from pathlib import Path
import re
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
AUTH_REQUIREMENTS = ROOT / "requirements-auth.txt"
DEV_REQUIREMENTS = ROOT / "requirements-dev.txt"
SAMOPULL = ROOT / "hetzner" / "samopull.sh"

AUTH_PINS = {
    "argon2-cffi": "25.1.0",
    "webauthn": "3.0.0",
}
BASH = Path("C:/Program Files/Git/bin/bash.exe")


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


def _bash_path(path):
    return "/c" + path.resolve().as_posix()[2:]


def test_auth_dependencies_are_exactly_pinned_for_development_and_production():
    """Removing or drifting either auth pin must block a release-ready build."""
    assert AUTH_REQUIREMENTS.exists(), "requirements-auth.txt must ship with the release"

    auth_versions, auth_packages = _auth_requirement_versions(AUTH_REQUIREMENTS)
    dev_versions, _ = _auth_requirement_versions(DEV_REQUIREMENTS)

    assert auth_packages == list(AUTH_PINS)
    for package, expected_version in AUTH_PINS.items():
        assert auth_versions[package] == [expected_version]
        assert dev_versions[package] == [expected_version]


@pytest.mark.parametrize("missing_module", ["argon2", "webauthn"])
def test_samopull_refuses_to_switch_when_auth_dependency_cannot_import(
        tmp_path, missing_module):
    """A failed real auth probe must exit before the live-mutation boundary."""
    script = SAMOPULL.read_text(encoding="utf-8")
    configuration = script.split("NTFY=", 1)[0]
    auth_preflight = script.split("# a) všetky moduly sa dajú naimportovať", 1)[1].split(
        'if ! (cd "$CIEL/app"', 1,
    )[0]

    calls = tmp_path / "python-calls"
    switch_sentinel = tmp_path / "switch-reached"
    fake_python = tmp_path / "python"
    fake_python.write_text(
        "#!/bin/sh\n"
        f'printf "%s:%s\\n" "$UVARSI_MISSING_MODULE" "$2" > "{_bash_path(calls)}"\n'
        'case "$2" in\n'
        '  *"$UVARSI_MISSING_MODULE"*) exit 23 ;;\n'
        "  *) exit 0 ;;\n"
        "esac\n",
        encoding="utf-8",
        newline="\n",
    )
    fake_python.chmod(0o755)

    command = (
        f"{configuration}\n"
        'log(){ printf "%s\\n" "$*"; }\n'
        f"{auth_preflight}\n"
        f'printf reached > "{_bash_path(switch_sentinel)}"\n'
    )
    result = subprocess.run(
        [str(BASH), "-c", command],
        env=os.environ | {
            "UVARSI_MISSING_MODULE": missing_module,
            "UVARSI_PY": _bash_path(fake_python),
        },
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
    )

    assert calls.exists(), "samopull did not execute the injected fake Python"
    assert calls.read_text(encoding="utf-8") == (
        f"{missing_module}:import argon2, webauthn\n"
    )
    assert result.returncode == 1
    assert "auth závislosti chýbajú — vydanie NEPREPÍNAM" in result.stdout
    assert not switch_sentinel.exists()
