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
        "app\\landing_static.py",
        "app\\receipt_data.py",
        "app\\plan_data.py",
        "app\\server.py",
        "app\\zbierac_akcii.py",
        "hetzner\\refresh_blocek.py",
        "hetzner\\recepty.py",
        "hetzner\\dozorca.sh",
        "hetzner\\uvarsi-deploy-state.sh",
        "hetzner\\uvarsi_cloudflare_worker.py",
        "hetzner\\uvarsi_tesco_bridge_repair.py",
        "hetzner\\uvarsi-plan-worker.service",
        "cloudflare\\tesco-bridge\\src\\worker.js",
        "index.html",
    ):
        assert f'"$B\\{relative_path}"' in script

    assert '"$B\\app\\static\\*"' in script
    assert 'r = "/opt/uvarsi/tesco-bridge-worker.js"' in script
    assert "manual-stage/cloudflare/tesco-bridge/src/worker.js" in script


def test_deploy_manifest_never_transfers_cloudflare_token():
    script = Path("nasad.ps1").read_text(encoding="utf-8")

    assert "/etc/uvarsi/secrets/cloudflare-worker-token" not in script
    assert "CLOUDFLARE_API_TOKEN" not in script
    assert "CF_API_TOKEN" not in script
