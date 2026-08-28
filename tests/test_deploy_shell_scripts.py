"""Shell skripty prenesené cez scp musia mať na serveri LF konce riadkov.

Regresia 20. 8. 2026: `dozorca.sh` sa nahral binárne z Windows (CRLF), takže
shebang bol `#!/bin/bash\\r`. Linux potom hľadá interpret `/bin/bash\\r` a hlási
mätúce `cannot execute: required file not found` — chýba interpret, nie skript.

Predošlá oprava (tests/test_deploy_line_endings.py) riešila len text posielaný
rúrou cez ssh. Súbory kopírované cez scp neriešila.
"""
import re
from pathlib import Path


DEPLOY = Path("nasad.ps1")


def _shell_scripts_in_manifest(script: str) -> list[str]:
    return re.findall(r'r = "(/opt/uvarsi/[^"]+\.sh)"', script)


def test_manifest_transfers_at_least_one_shell_script():
    """Poistka, aby test ticho neprešiel naprázdno."""
    assert _shell_scripts_in_manifest(DEPLOY.read_text(encoding="utf-8")), (
        "ocakavam aspon jeden .sh subor v deploy manifeste"
    )


def test_deploy_normalises_line_endings_of_shell_scripts_on_server():
    script = DEPLOY.read_text(encoding="utf-8")
    live_normalisation = script.index(
        'ssh jarvis "sed -i', script.index('Ok "staging je kompletne')
    )
    command = script[live_normalisation:script.index("Vyzaduj", live_normalisation)]
    for remote_path in _shell_scripts_in_manifest(script):
        assert remote_path in command, (
            f"deploy musi po prenose odstranit CR zo {remote_path}, inak "
            "shebang obsahuje \\r a skript sa neda spustit"
        )
    assert "/opt/uvarsi/*.sh" not in command, (
        "normalizacia nesmie menit ine zive skripty, ktore rollback nezalohuje"
    )


def test_normalisation_happens_before_scripts_are_used():
    """Normalizacia musi predchadzat cron/spustenie, inak je zbytocna."""
    script = DEPLOY.read_text(encoding="utf-8")
    normalizacia = script.find("/opt/uvarsi/dozorca.sh /opt/uvarsi/zaloha.sh")
    cron = script.find("crontab")
    assert normalizacia != -1, "normalizacia CR sa v deployi nenasla"
    assert cron == -1 or normalizacia < cron, (
        "CR sa musia odstranit skor, nez sa skripty zaradia do cronu"
    )
