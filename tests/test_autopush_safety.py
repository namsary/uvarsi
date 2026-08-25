from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = (ROOT / "nastroje" / "autopush.ps1").read_text(encoding="utf-8-sig")
GITIGNORE = (ROOT / ".gitignore").read_text(encoding="utf-8")


def test_autopush_log_is_outside_repository():
    assert 'Join-Path $REPO "nastroje\\autopush.log"' not in SCRIPT
    assert 'Join-Path $env:LOCALAPPDATA "Uvarsi"' in SCRIPT
    assert 'Join-Path $env:TEMP "Uvarsi"' in SCRIPT
    assert 'Join-Path $LOG_ROOT "autopush.log"' in SCRIPT


def test_legacy_autopush_log_cannot_trigger_another_commit():
    assert "nastroje/autopush.log" in GITIGNORE.splitlines()
