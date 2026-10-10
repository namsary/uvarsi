"""E1 (oprava): správa o nedostupnom režime pomenuje skutočnú príčinu.

Prázdne `stravovanie_dostupne` má viac príčin. Server ich rozlišuje poľom
`stravovanie_dostupne_dovod` a klient z neho vyberie text. Vstup týchto testov
je skutočná odpoveď `/api/me` zo servera na dočasnej databáze (`TestClient`),
nie ručne poskladaný objekt.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.test_customer_flow_e2e import STORE, _client
from tests.test_deterministic_plan_api import (
    _bomb_model_modules,
    _clear_recipe_engine_flag_cache,  # noqa: F401  (autouse fixture)
    _realistic_offer_rows,
)
from tests.test_server import ROOT, load_server
from tests.test_diet_frontend_contract import function_source
from tests.test_server import insert_hashed_session

APP = Path("app/static/app.html")
NODE = os.environ.get("UVARSI_NODE") or shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="node runtime is not available")


def _server(monkeypatch, tmp_path, *, letak):
    """Ako v e2e teste, ale letak tohto týždňa patrí obchodu `letak`."""
    monkeypatch.setenv("UVARSI_AUTH_V3", "1")
    monkeypatch.setenv("PLATBY_ZAPNUTE", "0")
    monkeypatch.setenv("UVARSI_RECIPE_ENGINE", "on")
    monkeypatch.setenv("RESEND_API_KEY", "test-only-not-a-real-key")
    monkeypatch.setenv("UVARSI_STATIC", str(ROOT / "app" / "static"))
    server = load_server(
        monkeypatch, tmp_path, _realistic_offer_rows(store_override=letak)
    )
    server.recipe_engine_mode.cache_clear()
    server.ENV_FILE = str(tmp_path / "missing.env")
    _bomb_model_modules(monkeypatch)
    return server


def _me(monkeypatch, tmp_path, *, obchody, ponuky):
    """Skutočná odpoveď `/api/me`; `ponuky`: "vsetky" | "ziadne" | "len_kaufland"."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    server = _server(
        monkeypatch, tmp_path, letak="Kaufland" if ponuky == "len_kaufland" else STORE
    )
    client = _client(server)
    with server.db() as con:
        if ponuky == "ziadne":
            con.execute("DELETE FROM akcie")
        con.execute(
            "INSERT INTO pouzivatelia (id,email,osoby,dospeli,deti,frekvencia,obchody)"
            " VALUES (1,'dovod@example.com',4,2,2,2,?)",
            (obchody,),
        )
        insert_hashed_session(server, con, "session-1", 1)
        con.commit()
    client.cookies.set(server.COOKIE, "session-1")
    me = client.get("/api/me")
    assert me.status_code == 200
    return me.json()


def _sprava(tmp_path, me):
    html = APP.read_text(encoding="utf-8")
    script = tmp_path / "sprava.js"
    script.write_text(
        function_source(html, "spravaNedostupnyRezim")
        + "\nconst me = " + json.dumps(me)
        + ";\nprocess.stdout.write(spravaNedostupnyRezim(me));\n",
        encoding="utf-8",
    )
    r = subprocess.run([NODE, str(script)], capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 0, r.stderr
    return r.stdout


def test_server_rozlisi_chybajuce_data_a_malo_ponuk(monkeypatch, tmp_path):
    bez_dat = _me(monkeypatch, tmp_path / "a", obchody=STORE, ponuky="ziadne")
    assert bez_dat["stravovanie_dostupne"] == []
    assert bez_dat["stravovanie_dostupne_dovod"] == "chybaju_data_tyzdna"

    malo = _me(monkeypatch, tmp_path / "b", obchody="Lidl", ponuky="len_kaufland")
    assert malo["stravovanie_dostupne"] == []
    assert malo["stravovanie_dostupne_dovod"] == "malo_ponuk_vybrane_obchody"


def test_zdravy_ucet_nema_dovod(monkeypatch, tmp_path):
    me = _me(monkeypatch, tmp_path, obchody=STORE, ponuky="vsetky")
    assert me["stravovanie_dostupne"]
    assert me["stravovanie_dostupne_dovod"] is None


@needs_node
def test_spravy_podla_skutocnej_odpovede_servera(monkeypatch, tmp_path):
    bez_dat = _me(monkeypatch, tmp_path / "a", obchody=STORE, ponuky="ziadne")
    text_a = _sprava(tmp_path, bez_dat)
    assert "Letákové dáta" in text_a and "uložené" in text_a
    assert "Pridaj obchod" not in text_a

    malo = _me(monkeypatch, tmp_path / "b", obchody="Lidl", ponuky="len_kaufland")
    text_b = _sprava(tmp_path, malo)
    assert "Pridaj obchod" in text_b
    assert "obnovujú" not in text_b and "Letákové dáta" not in text_b
