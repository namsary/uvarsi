"""V0 (oprava): staré prihlásenie (UVARSI_AUTH_V3=0) netvrdí, že e-mail ide k neznámej adrese.

Pri `UVARSI_AUTH_V3=0` server zámerne odpovie rovnako na známy aj neznámy
e-mail (ochrana pred zisťovaním účtov) a neznámej adrese neodošle nič. Karta
po odoslaní preto nesmie tvrdiť bezpodmienečne, že žiadosť o e-mail prešla, a
musí povedať, čo robiť, keď e-mail nepríde.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.test_diet_frontend_contract import function_source
from tests.test_auth import install_provider, load_auth_server

APP = Path("app/static/app.html")
NODE = os.environ.get("UVARSI_NODE") or shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="node runtime is not available")


def test_server_neznamemu_emailu_nic_neposle_a_odpovie_rovnako(monkeypatch, tmp_path):
    server, _ = load_auth_server(monkeypatch, tmp_path)
    monkeypatch.setenv("RESEND_API_KEY", "test-only-key")
    odoslane = []
    install_provider(monkeypatch, calls=odoslane)
    with server.db() as con:
        con.execute("INSERT INTO pouzivatelia (email) VALUES ('znamy@example.com')")
        con.commit()
    hlavicky = {"Origin": "https://uvar.si"}
    c1 = TestClient(server.app, headers=hlavicky, client=("198.51.100.31", 4000))
    c2 = TestClient(server.app, headers=hlavicky, client=("198.51.100.32", 4000))
    znamy = c1.post("/api/auth/request", json={"email": "znamy@example.com"})
    neznamy = c2.post("/api/auth/request", json={"email": "nikto@example.com"})
    assert znamy.status_code == neznamy.status_code == 200
    assert znamy.json() == neznamy.json()
    # odpoveď servera je rovnaká, takže pravdu o účte musí povedať text na karte
    assert len(odoslane) == 1  # iba pre známy účet
    assert "nikto@example.com" not in json.dumps(odoslane[0][1], default=str)


@needs_node
def test_karta_po_odoslani_je_podmienena_a_ma_cestu_dalej(tmp_path):
    html = APP.read_text(encoding="utf-8")
    script = tmp_path / "login.js"
    script.write_text(
        function_source(html, "viewLegacyLogin")
        + "\nconst M = {innerHTML: ''}, nodes = {};\n"
        "function $(s) { return nodes[s] || (nodes[s] = {classList: {add() {}}}); }\n"
        "function clearAuthenticatedState() {} function forgetProfile() {}\n"
        "function armAuthResend() {}\n"
        "viewLegacyLogin(true, 'a@b.sk');\n"
        "process.stdout.write(JSON.stringify(M.innerHTML));\n",
        encoding="utf-8",
    )
    r = subprocess.run([NODE, str(script)], capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 0, r.stderr
    karta = json.loads(r.stdout)
    assert "Žiadosť je prijatá" not in karta
    assert "ak k adrese existuje účet" in karta
    assert "Odkaz platí 60 minút" in karta
    assert "Kontakt" in karta and "Nový účet tu zatiaľ nevznikne" in karta
