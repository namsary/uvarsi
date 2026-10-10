"""E2 (oprava): Nákupný zoznam bez plánu nesmie byť slepá ulička.

Keď plán nie je (napr. letákové dáta tohto týždňa chýbajú), karta Nákup mala
iba text „Najprv si vygeneruj plán." bez akcie, hoci plán sa vygenerovať nedá.
Test berie skutočný stav servera (`/api/plan` na databáze bez akcií) a spúšťa
reálnu funkciu `vZoznam` z `app.html`.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.test_customer_flow_e2e import _client
from tests.test_diet_frontend_contract import function_source
from tests.test_onboarding_dovod_prazdnych_rezimov import _server
from tests.test_server import insert_hashed_session

APP = Path("app/static/app.html")
NODE = os.environ.get("UVARSI_NODE") or shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="node runtime is not available")


def _plan_bez_dat(monkeypatch, tmp_path):
    from tests.test_customer_flow_e2e import STORE

    tmp_path.mkdir(parents=True, exist_ok=True)
    server = _server(monkeypatch, tmp_path, letak=STORE)
    client = _client(server)
    with server.db() as con:
        con.execute("DELETE FROM akcie")
        con.execute(
            "INSERT INTO pouzivatelia (id,email,osoby,dospeli,deti,frekvencia,obchody)"
            " VALUES (1,'zoznam@example.com',4,2,2,2,?)",
            (STORE,),
        )
        insert_hashed_session(server, con, "session-1", 1)
        con.commit()
    client.cookies.set(server.COOKIE, "session-1")
    return client.get("/api/plan")


@needs_node
def test_zoznam_bez_planu_ma_vysvetlenie_a_tlacidlo(monkeypatch, tmp_path):
    odpoved = _plan_bez_dat(monkeypatch, tmp_path / "srv")
    # server plán nevrátil: klient teda nemá z čoho zoznam postaviť
    telo = odpoved.json()
    assert not (isinstance(telo, dict) and telo.get("nakupny_zoznam"))

    html = APP.read_text(encoding="utf-8")
    script = tmp_path / "zoznam.js"
    script.write_text(
        function_source(html, "vZoznam")
        + "\nlet PLAN = " + json.dumps(telo if isinstance(telo, dict) and telo.get("nakupny_zoznam") else None)
        + ";\nlet TAB = 'zoznam', rendered = 0;\n"
        "const M = {innerHTML: ''}, nodes = {};\n"
        "function $(sel) { return nodes[sel] || (nodes[sel] = {}); }\n"
        "function render() { rendered++; }\n"
        "vZoznam();\n"
        "const m = M.innerHTML.match(/<button[^>]*onclick=\"([^\"]+)\"/);\n"
        "let out = {html: M.innerHTML, button: m && m[1]};\n"
        "if (m) { eval(m[1].replace(/&#39;|&apos;/g, \"'\")); }\n"
        "out.tab = TAB; out.rendered = rendered;\n"
        "process.stdout.write(JSON.stringify(out));\n",
        encoding="utf-8",
    )
    r = subprocess.run([NODE, str(script)], capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert out["button"], "karta Nákup bez plánu musí mať tlačidlo ďalšieho kroku"
    assert out["tab"] == "plan" and out["rendered"] == 1
    assert "Najprv si vygeneruj plán" not in out["html"]
    assert "jedálniček" in out["html"] and "Na jedálniček" in out["html"]
