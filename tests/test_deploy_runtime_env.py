"""Deploy nesmie vyrobiť systemd jednotku, ktorá spadne pri importe.

server.py volá config.public_base_url() na module-level (BASE_URL). Bez
UVARSI_URL=https://uvar.si služba pri štarte spadne — deploy „prejde“, ale
appka je mŕtva. Tento test drží deploy manifest a runtime prostredie v súlade.
"""
from pathlib import Path

import pytest


REQUIRED_ENV = {
    "UVARSI_URL": "https://uvar.si",
    "UVARSI_LANDING_DATA": "/var/lib/uvarsi/landing_data.json",
    "UVARSI_VERSION_FILE": "/opt/uvarsi/VERSION",
}


@pytest.fixture(scope="module")
def deploy_script() -> str:
    return Path("nasad.ps1").read_text(encoding="utf-8")


@pytest.mark.parametrize("name,value", sorted(REQUIRED_ENV.items()))
def test_service_unit_declares_required_runtime_env(deploy_script, name, value):
    assert f"Environment={name}={value}" in deploy_script, (
        f"systemd jednotka musí obsahovať Environment={name}={value}, "
        "inak služba spadne pri importe server.py"
    )


def test_landing_data_directory_is_created_before_service_start(deploy_script):
    assert "/var/lib/uvarsi" in deploy_script, (
        "deploy musí vytvoriť /var/lib/uvarsi, inak nie je kam zapísať landing_data.json"
    )


def test_sqlite3_cli_is_installed_for_dozorca(deploy_script):
    assert "sqlite3" in deploy_script, (
        "dozorca.sh volá sqlite3; bez neho tichо preskočí kontrolu akcií"
    )


def test_version_file_is_deployed(deploy_script):
    assert '"$B\\VERSION"' in deploy_script, (
        "config.release_id() číta VERSION; súbor musí byť nasadený"
    )


def test_service_health_is_verified_with_retry_not_single_shot(deploy_script):
    """Po restarte trvá uvicorn ~1–3 s. Jednorazový curl vyrobí falošný poplach."""
    assert "for" in deploy_script and "8090" in deploy_script, (
        "kontrola po nasadení musí na službu počkať v cykle, nie skúsiť raz"
    )
