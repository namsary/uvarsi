"""E1: uloženie nastavení bez aktuálnych dát nesmie klamať o príčine.

Keď na tento týždeň nie je žiadny režim (`stravovanie_dostupne` je prázdne),
problém nie je v obchodoch ani v režime, ale v chýbajúcich letákových dátach.
Rada „Pridaj obchod alebo vyber iný režim“ by v tom prípade nepomohla.
"""
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.test_diet_frontend_contract import function_source

APP = Path("app/static/app.html")
NODE = os.environ.get("UVARSI_NODE") or shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="node runtime is not available")


def _run(tmp_path, body):
    html = APP.read_text(encoding="utf-8")
    script = tmp_path / "e1.js"
    script.write_text(
        function_source(html, "spravaNedostupnyRezim") + "\n" + body,
        encoding="utf-8",
    )
    return subprocess.run(
        [NODE, str(script)], capture_output=True, text=True, encoding="utf-8"
    )


@needs_node
def test_bez_dat_tyzdna_sprava_nesluby_pridanie_obchodu(tmp_path):
    r = _run(tmp_path, """
for (const me of [{stravovanie_dostupne: []}, {}, null]) {
  const m = spravaNedostupnyRezim(me);
  if (m.includes('Pridaj obchod') || m.includes('iný režim')) process.exit(1);
  if (!m.includes('Letákové dáta') || !m.includes('uložené')) process.exit(2);
}
process.exit(0);
""")
    assert r.returncode == 0, r.stdout + r.stderr


@needs_node
def test_s_datami_ostava_rada_o_obchode_alebo_rezime(tmp_path):
    r = _run(tmp_path, """
const m = spravaNedostupnyRezim({stravovanie_dostupne: ['standard']});
if (m !== 'Z týchto akcií zvolený jedálniček neposkladáme. Pridaj obchod alebo vyber iný režim.') process.exit(1);
process.exit(0);
""")
    assert r.returncode == 0, r.stdout + r.stderr


def test_onboarding_pouziva_pomocnu_funkciu():
    html = APP.read_text(encoding="utf-8")
    assert "spravaNedostupnyRezim(ME)" in html
    assert "message.textContent = 'Z týchto akcií" not in html
