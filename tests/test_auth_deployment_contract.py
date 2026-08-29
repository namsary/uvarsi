from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
AUTH_REQUIREMENTS = ROOT / "requirements-auth.txt"
DEV_REQUIREMENTS = ROOT / "requirements-dev.txt"
SAMOPULL = ROOT / "hetzner" / "samopull.sh"

AUTH_PINS = {
    "argon2-cffi==25.1.0",
    "webauthn==3.0.0",
}


def test_auth_dependencies_are_exactly_pinned_for_development_and_production():
    """Removing or drifting either auth pin must block a release-ready build."""
    assert AUTH_REQUIREMENTS.exists(), "requirements-auth.txt must ship with the release"

    auth_lines = set(AUTH_REQUIREMENTS.read_text(encoding="utf-8").splitlines())
    dev_lines = set(DEV_REQUIREMENTS.read_text(encoding="utf-8").splitlines())

    assert auth_lines == AUTH_PINS
    assert AUTH_PINS <= dev_lines


def test_samopull_refuses_to_switch_without_importable_auth_dependencies():
    """Removing or moving the auth import gate after the switch is unsafe."""
    script = SAMOPULL.read_text(encoding="utf-8")
    preflight = '''if ! "$PY" -c "import argon2, webauthn" >/dev/null 2>&1; then
  log "auth závislosti chýbajú — vydanie NEPREPÍNAM"
  exit 1
fi'''

    assert preflight in script
    assert script.index(preflight) < script.index("# --- 3. záloha aktuálneho stavu a prepnutie ---")
