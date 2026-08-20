"""Deploy nesmie preniesť Windows konce riadkov (CRLF) na Linux server.

PowerShell pri `$text | ssh ... "cat > subor"` zapíše CRLF. Linux potom берie
`\r` ako súčasť príkazu/hodnoty:
  * bash: `$'echo\\r': command not found`
  * Caddy: `caddy validate` zlyhá a config sa nenačíta
  * systemd: Environment=... dostane hodnotu s `\r` na konci

Každý presun viacriadkového textu cez rúru preto musí `\r` odstrániť.
"""
import re
from pathlib import Path


DEPLOY = Path("nasad.ps1")


def _piped_ssh_targets(script: str) -> list[str]:
    """Riadky, kde sa viacriadkový text posiela cez rúru do ssh."""
    return [line.strip() for line in script.splitlines()
            if re.search(r"\|\s*ssh\s+jarvis", line)]


def test_deploy_script_has_piped_transfers():
    """Poistka: ak sa spôsob prenosu zmení, test nesmie ticho prejsť naprázdno."""
    assert _piped_ssh_targets(DEPLOY.read_text(encoding="utf-8")), (
        "očakávam aspoň jeden prenos textu cez rúru do ssh"
    )


def test_every_piped_transfer_strips_carriage_returns():
    for line in _piped_ssh_targets(DEPLOY.read_text(encoding="utf-8")):
        assert "tr -d" in line and "\\r" in line, (
            "prenos cez rúru musí odstrániť CR (tr -d '\\r'), inak sa na server "
            f"dostanú CRLF konce riadkov:\n  {line}"
        )
