from pathlib import Path


def test_deploy_manifest_is_worktree_rooted_and_runtime_complete():
    script = Path("nasad.ps1").read_text(encoding="utf-8")

    assert "$B = $PSScriptRoot" in script
    for relative_path in (
        "app\\config.py",
        "app\\public_pages.py",
        "app\\weekly_data.py",
        "app\\offer_data.py",
        "app\\landing_data.py",
        "app\\receipt_data.py",
        "app\\plan_data.py",
        "app\\server.py",
        "app\\zbierac_akcii.py",
        "hetzner\\refresh_blocek.py",
        "hetzner\\recepty.py",
        "hetzner\\dozorca.sh",
        "index.html",
    ):
        assert f'"$B\\{relative_path}"' in script

    assert '"$B\\app\\static\\*"' in script
