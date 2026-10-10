"""End-to-end hlavný zákaznícky tok: návšteva → prihlásenie → plán → nákupný zoznam.

Beží celý offline: dočasná databáza, týždenné akcie z `VERIFIED_WEEKLY_OFFERS`,
falošný poskytovateľ e-mailov (`requests` v `sys.modules`, žiadna sieť) a bez
skutočných kľúčov. Platby ostávajú vypnuté. Klient používa `https://testserver`,
aby sa produkčné cookie (`secure=True`) uložilo a vrátilo bez zmeny kódu servera.

Pokrýva oba režimy prihlasovania:

* `UVARSI_AUTH_V3=1` — registrácia e-mailom a heslom, potvrdenie odkazom z
  e-mailu (`/api/auth/register` → `/api/auth/confirm`), potom heslový login.
* `UVARSI_AUTH_V3=0` — nové účty sa týmto režimom nevytvárajú; existujúci účet
  bez hesla prejde migračným mostom (`/api/auth/request` → `/api/auth/verify`
  → `/api/auth/password/set`).
"""

from __future__ import annotations

import json
import re

import pytest

from tests.test_auth import ProviderResponse, install_provider
from tests.test_deterministic_plan_api import (
    _bomb_model_modules,
    _clear_recipe_engine_flag_cache,  # noqa: F401  (autouse fixture)
    _realistic_offer_rows,
)
from tests.test_server import ROOT, load_server

ORIGIN = {"Origin": "https://uvar.si"}
PASSWORD = "Dlhe-testovacie-heslo-42"
STORE = "Lidl"  # bezplatný účet smie mať jeden obchod


def _client(server):
    from fastapi.testclient import TestClient

    return TestClient(server.app, base_url="https://testserver", headers=ORIGIN)


def _server(monkeypatch, tmp_path, *, auth_v3):
    monkeypatch.setenv("UVARSI_AUTH_V3", "1" if auth_v3 else "0")
    monkeypatch.setenv("PLATBY_ZAPNUTE", "0")
    monkeypatch.setenv("UVARSI_RECIPE_ENGINE", "on")
    monkeypatch.setenv("RESEND_API_KEY", "test-only-not-a-real-key")
    monkeypatch.setenv("UVARSI_STATIC", str(ROOT / "app" / "static"))
    server = load_server(
        monkeypatch, tmp_path, _realistic_offer_rows(store_override=STORE)
    )
    server.recipe_engine_mode.cache_clear()
    server.ENV_FILE = str(tmp_path / "missing.env")
    _bomb_model_modules(monkeypatch)  # plán nesmie zavolať žiadny LLM klient
    return server


def _mail_link(calls, pattern):
    """Odkaz z posledného odoslaného (falošného) e-mailu."""
    assert calls, "žiadny e-mail neodišiel poskytovateľovi"
    body = calls[-1][1]["json"]["text"]
    match = re.search(pattern, body)
    assert match, body
    return match.group(1)


def _visit(client):
    """Návšteva: appka sa načíta, verejné právne stránky fungujú, nik nie je prihlásený."""
    page = client.get("/app")
    assert page.status_code == 200
    assert "text/html" in page.headers["content-type"]
    assert '<html lang="sk"' in page.text
    for path in ("/vop", "/ochrana-osobnych-udajov", "/cookies"):
        assert client.get(path).status_code == 200, path
    me = client.get("/api/me")
    assert me.status_code == 200
    assert me.json()["prihlaseny"] is False
    assert client.get("/api/plan").status_code == 401


def _assert_session(client, server, email):
    assert client.cookies.get(server.COOKIE), "server nevydal session cookie"
    me = client.get("/api/me").json()
    assert me["prihlaseny"] is True
    assert me["email"] == email
    assert me["premium"] is False
    assert me["platby_zapnute"] is False


def _plan_and_shopping_list(client, server):
    saved = client.post(
        "/api/profil",
        json={"adults": 2, "children": 2, "frekvencia": 2, "obchody": [STORE]},
    )
    assert saved.status_code == 200, saved.text
    assert client.get("/api/me").json()["onboarding"] is True

    generated = client.post("/api/plan/generuj")
    assert generated.status_code == 200, generated.text
    plan = generated.json()

    # Plán z overených akcií, deterministicky, bez modelu.
    assert plan["meta"]["engine"] == "deterministic"
    assert plan["tyzden"] == server.monday()
    meals = plan["jedla"]
    assert meals
    assert sum(meal["pokryva_dni"] for meal in meals) == 7
    for meal in meals:
        recipe = meal["recept"]
        assert meal["nazov"].strip()
        assert recipe["davky"], meal["nazov"]
        assert len(recipe["kroky"]) >= 2, meal["nazov"]
        assert recipe["min"] > 0

    # Nákupný zoznam je odvodený z plánu: akciové položky len zo zvoleného
    # obchodu a s cenou z akcie; skupina „Dokúpiť bežne“ je bez ceny.
    groups = plan["nakupny_zoznam"]
    by_store = {group["obchod"]: group["polozky"] for group in groups}
    assert set(by_store) <= {STORE, "Dokúpiť bežne"}
    offer_items = by_store[STORE]
    assert offer_items
    for item in offer_items:
        assert item["nazov"].strip()
        assert item["mnozstvo"] >= 1
        assert item["offer_key"].startswith("offer_")
        assert float(item["cena"].replace(",", ".")) > 0
    for item in by_store.get("Dokúpiť bežne", []):
        assert item["nazov"].strip()
        assert item["cena"] is None
    seeded_keys = {
        row["offer_key"]
        for row in _db_rows(server, "SELECT offer_key FROM akcie WHERE obchod=?", STORE)
    }
    assert {item["offer_key"] for item in offer_items} <= seeded_keys
    total = sum(float(item["cena"].replace(",", ".")) for item in offer_items)
    assert float(plan["nakup_spolu"].replace(",", ".")) == pytest.approx(total, abs=0.05)

    # Plán sa uložil a ten istý obsah vráti aj opätovné načítanie.
    reloaded = client.get("/api/plan")
    assert reloaded.status_code == 200
    again = reloaded.json()
    assert [m["nazov"] for m in again["jedla"]] == [m["nazov"] for m in meals]
    assert again["nakupny_zoznam"] == groups
    return plan


def _db_rows(server, sql, *params):
    from contextlib import closing

    with closing(server.db()) as con:
        return [dict(row) for row in con.execute(sql, params).fetchall()]


def test_v3_visit_register_confirm_plan_shopping_list(monkeypatch, tmp_path):
    server = _server(monkeypatch, tmp_path, auth_v3=True)
    calls = []
    install_provider(monkeypatch, calls=calls)
    client = _client(server)
    email = "novy.zakaznik@example.com"

    _visit(client)
    assert client.get("/api/me").json()["auth_v3"] is True

    registered = client.post(
        "/api/auth/register", json={"email": email, "password": PASSWORD}
    )
    assert registered.status_code == 200, registered.text
    assert len(calls) == 1
    assert calls[0][1]["json"]["to"] == [email]
    # Do potvrdenia účet neexistuje a nie je prihlásený.
    assert client.cookies.get(server.COOKIE) is None
    assert client.get("/api/me").json()["prihlaseny"] is False

    token = _mail_link(calls, r"/api/auth/pages/potvrdenie#token=([A-Za-z0-9_-]+)")
    confirmed = client.post("/api/auth/confirm", json={"token": token})
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["redirect"] == "/app"
    _assert_session(client, server, email)
    assert client.get("/api/me").json()["password_configured"] is True

    # Odkaz je jednorazový.
    assert client.post("/api/auth/confirm", json={"token": token}).status_code == 400

    # Po odhlásení sa vrátime heslom — druhé zariadenie je nový klient.
    assert client.post("/api/auth/logout").status_code == 200
    assert client.get("/api/me").json()["prihlaseny"] is False
    assert client.get("/api/plan").status_code == 401
    assert (
        _client(server)
        .post("/api/auth/login", json={"email": email, "password": "zle-heslo-12345"})
        .status_code
        == 401
    )
    returning = _client(server)
    login = returning.post("/api/auth/login", json={"email": email, "password": PASSWORD})
    assert login.status_code == 200, login.text
    _assert_session(returning, server, email)

    _plan_and_shopping_list(returning, server)
    assert len(calls) == 1  # plán žiadne ďalšie e-maily neposiela


def test_v0_legacy_account_without_password_magic_link_bridge_plan_shopping_list(
    monkeypatch, tmp_path
):
    server = _server(monkeypatch, tmp_path, auth_v3=False)
    calls = []
    install_provider(monkeypatch, calls=calls)
    client = _client(server)
    email = "stary.ucet@example.com"
    with server.db() as con:
        con.execute("INSERT INTO pouzivatelia (email) VALUES (?)", (email,))
        con.commit()

    _visit(client)
    assert "auth_v3" not in client.get("/api/me").json()
    # Heslový login a registrácia patria do v3; pri vypnutom príznaku neexistujú.
    assert client.post("/api/auth/login", json={"email": email, "password": PASSWORD}).status_code == 404
    assert client.post("/api/auth/register", json={"email": email, "password": PASSWORD}).status_code == 404

    requested = client.post("/api/auth/request", json={"email": email})
    assert requested.status_code == 200, requested.text
    assert len(calls) == 1
    assert calls[0][1]["json"]["to"] == [email]
    token = _mail_link(calls, r"/prihlasenie#token=([A-Za-z0-9_-]+)")

    # Odkaz ešte neprihlasuje; vydá len obmedzený setup cookie a vedie na heslo.
    verified = client.post("/api/auth/verify", json={"token": token})
    assert verified.status_code == 200, verified.text
    assert verified.json()["redirect"] == "/api/auth/pages/heslo"
    assert client.cookies.get(server.COOKIE) is None
    assert client.cookies.get(server.SETUP_COOKIE)
    assert client.get("/api/plan").status_code == 401
    # Jednorazovosť odkazu.
    assert _client(server).post("/api/auth/verify", json={"token": token}).status_code == 400

    short = client.post("/api/auth/password/set", json={"password": "kratke"})
    assert short.status_code == 400
    done = client.post("/api/auth/password/set", json={"password": PASSWORD})
    assert done.status_code == 200, done.text
    _assert_session(client, server, email)
    assert client.cookies.get(server.SETUP_COOKIE) is None

    _plan_and_shopping_list(client, server)
    assert len(calls) == 1


def test_v0_unknown_address_gets_no_login_mail(monkeypatch, tmp_path):
    """Pri `UVARSI_AUTH_V3=0` nový zákazník účet nezíska — nedostane ani e-mail.

    Zámerne iba dokumentuje súčasné správanie (migračný most pre staré účty);
    oprava, ak je potrebná, je produktové rozhodnutie mimo tejto úlohy.
    """
    server = _server(monkeypatch, tmp_path, auth_v3=False)
    calls = []
    install_provider(monkeypatch, calls=calls)
    client = _client(server)

    response = client.post("/api/auth/request", json={"email": "uplne.novy@example.com"})

    assert response.status_code == 200
    assert calls == []
    assert _db_rows(server, "SELECT id FROM pouzivatelia") == []
    assert client.cookies.get(server.COOKIE) is None


def test_data_unavailable_state_is_a_clear_slovak_503_after_login(monkeypatch, tmp_path):
    """Bez akcií na tento týždeň appka nehádže plán, ale povie prečo."""
    server = _server(monkeypatch, tmp_path, auth_v3=True)
    install_provider(monkeypatch, calls=[])
    client = _client(server)
    with server.db() as con:
        con.execute("DELETE FROM akcie")
        con.execute("DELETE FROM zber_stav")
        con.commit()
    _visit(client)
    from tests.test_server import insert_hashed_session

    with server.db() as con:
        con.execute(
            "INSERT INTO pouzivatelia (id,email,osoby,dospeli,deti,frekvencia,obchody)"
            " VALUES (1,'bez.dat@example.com',4,2,2,2,?)",
            (STORE,),
        )
        insert_hashed_session(server, con, "session-1", 1)
        con.commit()
    client.cookies.set(server.COOKIE, "session-1")

    plan = client.get("/api/plan")

    assert plan.status_code == 503
    assert plan.json()["detail"].strip()
    assert client.post("/api/plan/generuj").status_code == 503
