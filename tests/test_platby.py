"""Platby: infraštruktúra pre Zakladajúceho člena — vypnutá, ale celá otestovaná.

Peniaze sa nesmú hýbať skôr, než majiteľ vedome zapne PLATBY_ZAPNUTE. Tieto testy
držia tri veci: vypínač naozaj vypína, podpis webhooku sa overuje konštantne
a nárok sa odvodzuje výhradne z uložených udalostí poskytovateľa — nikdy z klienta.
"""
import hashlib
import hmac
import importlib
import json
import sys
import threading
from contextlib import closing
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


ROOT = Path(__file__).resolve().parents[1]

TAJOMSTVO = "tajny-webhook-podpisovy-kluc"
CHECKOUT = "https://uvarsi.lemonsqueezy.com/buy/11111111-2222-3333-4444-555555555555"

PLATBY_ENV = ("PLATBY_ZAPNUTE", "LEMON_WEBHOOK_SECRET", "LEMON_CHECKOUT_URL", "LEMON_VARIANT_ID")


def load_server(monkeypatch, tmp_path, **prostredie):
    """server.py nad čerstvou databázou a s presne určeným platobným prostredím."""
    database = tmp_path / "uvarsi.db"
    monkeypatch.setenv("UVARSI_DB", str(database))
    monkeypatch.setenv("UVARSI_URL", "https://uvar.si")
    monkeypatch.setenv("UVARSI_VERSION_FILE", str(ROOT / "VERSION"))
    monkeypatch.setenv("UVARSI_STATIC", str(tmp_path / "static"))
    for name in PLATBY_ENV:
        monkeypatch.delenv(name, raising=False)
    for name, value in prostredie.items():
        if value is not None:
            monkeypatch.setenv(name, value)
    monkeypatch.syspath_prepend(str(ROOT / "app"))
    sys.modules.pop("server", None)
    module = importlib.import_module("server")
    # ENV_FILE na vývojárskom stroji neexistuje; nech sa nikdy nečíta z /opt.
    monkeypatch.setattr(module, "ENV_FILE", str(tmp_path / "neexistuje.env"))
    return module


def zapnute_platby(monkeypatch, tmp_path, **prostredie):
    prostredie.setdefault("PLATBY_ZAPNUTE", "1")
    prostredie.setdefault("LEMON_WEBHOOK_SECRET", TAJOMSTVO)
    prostredie.setdefault("LEMON_CHECKOUT_URL", CHECKOUT)
    return load_server(monkeypatch, tmp_path, **prostredie)


def vytvor_pouzivatela(server, user_id=1, email="test@uvar.si", session="session-token"):
    now = server.AUTH_CLOCK()
    with closing(server.db()) as con:
        con.execute("INSERT INTO pouzivatelia (id, email) VALUES (?, ?)", (user_id, email))
        if session:
            con.execute(
                """INSERT INTO sessions_v2 (token_hash, user_id, expires_at, created_at)
                   VALUES (?, ?, ?, ?)""",
                (hashlib.sha256(session.encode()).hexdigest(), user_id,
                 now + 30 * 24 * 60 * 60, now),
            )
        con.commit()


def prihlaseny(server, session="session-token"):
    client = TestClient(server.app, raise_server_exceptions=False)
    client.cookies.set(server.COOKIE, session)
    return client


def objednavka(user_id=1, order_id="ord-1", udalost="order_created", total=1900,
               mena="EUR", webhook_id=None, variant_id=None, typ="orders"):
    attributes = {"total": total, "currency": mena, "status": "paid"}
    if variant_id is not None:
        attributes["first_order_item"] = {"variant_id": variant_id}
    if typ == "subscriptions":
        attributes["order_id"] = order_id
    meta = {"event_name": udalost, "custom_data": {"user_id": str(user_id)}}
    if webhook_id is not None:
        meta["webhook_id"] = webhook_id
    return {"meta": meta, "data": {"id": str(order_id), "type": typ, "attributes": attributes}}


def podpis(telo: bytes, tajomstvo=TAJOMSTVO) -> str:
    return hmac.new(tajomstvo.encode("utf-8"), telo, hashlib.sha256).hexdigest()


def posli_webhook(client, payload, tajomstvo=TAJOMSTVO, hlavicka=None):
    telo = json.dumps(payload).encode("utf-8")
    hlavicky = {"Content-Type": "application/json"}
    if hlavicka is not None:
        hlavicky["X-Signature"] = hlavicka
    elif tajomstvo is not None:
        hlavicky["X-Signature"] = podpis(telo, tajomstvo)
    return client.post("/api/platba/webhook", content=telo, headers=hlavicky)


def naroky(server):
    with closing(server.db()) as con:
        return [dict(row) for row in con.execute("SELECT * FROM naroky ORDER BY id")]


def aktivne(server):
    return [row for row in naroky(server) if row["stav"] == "aktivny"]


# ------------------------------------------------------------------ vypínač
def test_platby_su_v_predvolenom_stave_vypnute(monkeypatch, tmp_path):
    server = load_server(monkeypatch, tmp_path)
    assert server.platby_zapnute(server.env("PLATBY_ZAPNUTE")) is False


@pytest.mark.parametrize("hodnota", ["1", "true", "TRUE", "ano", "áno", "yes", "on"])
def test_vypinac_zapina_len_jednoznacnymi_hodnotami(monkeypatch, tmp_path, hodnota):
    server = load_server(monkeypatch, tmp_path)
    assert server.platby_zapnute(hodnota) is True


@pytest.mark.parametrize("hodnota", [None, "", "0", "false", "nie", "off", "mozno", " "])
def test_vypinac_zostava_vypnuty_pri_hocicom_inom(monkeypatch, tmp_path, hodnota):
    server = load_server(monkeypatch, tmp_path)
    assert server.platby_zapnute(hodnota) is False


def test_start_je_503_a_nikdy_nesiaha_na_poskytovatela_ked_su_platby_vypnute(monkeypatch, tmp_path):
    server = load_server(monkeypatch, tmp_path, LEMON_CHECKOUT_URL=CHECKOUT,
                         LEMON_WEBHOOK_SECRET=TAJOMSTVO)
    vytvor_pouzivatela(server)

    def zakazany_env(key, default=None):
        assert not key.startswith("LEMON"), f"vypnuté platby nesmú čítať {key}"
        return "" if key == "PLATBY_ZAPNUTE" else default

    monkeypatch.setattr(server, "env", zakazany_env)
    response = prihlaseny(server).post("/api/platba/start")

    assert response.status_code == 503
    assert response.json()["detail"] == "Platby zatiaľ nie sú spustené."
    assert naroky(server) == []


def test_webhook_je_503_a_nemeni_stav_ked_su_platby_vypnute(monkeypatch, tmp_path):
    server = load_server(monkeypatch, tmp_path, LEMON_WEBHOOK_SECRET=TAJOMSTVO)
    vytvor_pouzivatela(server)

    def zakazany_env(key, default=None):
        assert not key.startswith("LEMON"), f"vypnuté platby nesmú čítať {key}"
        return "" if key == "PLATBY_ZAPNUTE" else default

    monkeypatch.setattr(server, "env", zakazany_env)
    client = TestClient(server.app, raise_server_exceptions=False)
    response = posli_webhook(client, objednavka())

    assert response.status_code == 503
    assert response.json()["detail"] == "Platby zatiaľ nie sú spustené."
    assert naroky(server) == []


def test_stav_hovori_zrozumitelne_ze_platby_este_nebezia(monkeypatch, tmp_path):
    server = load_server(monkeypatch, tmp_path)
    vytvor_pouzivatela(server)

    response = prihlaseny(server).get("/api/platba/stav")

    assert response.status_code == 200
    data = response.json()
    assert data["platby_zapnute"] is False
    assert data["ma_narok"] is False
    assert data["volne_miesta"] == 250
    assert data["sprava"] == "Platby zatiaľ nie sú spustené."


# ------------------------------------------------------------------ prihlásenie
@pytest.mark.parametrize("metoda, cesta", [("post", "/api/platba/start"), ("get", "/api/platba/stav")])
def test_platobne_endpointy_odmietnu_neprihlaseneho(monkeypatch, tmp_path, metoda, cesta):
    server = zapnute_platby(monkeypatch, tmp_path)
    vytvor_pouzivatela(server)
    client = TestClient(server.app, raise_server_exceptions=False)

    response = getattr(client, metoda)(cesta)

    assert response.status_code == 401
    assert response.json()["detail"] == "Neprihlásený"


# ------------------------------------------------------------------ checkout
def test_start_vrati_checkout_url_s_id_pouzivatela_v_custom_data(monkeypatch, tmp_path):
    server = zapnute_platby(monkeypatch, tmp_path)
    vytvor_pouzivatela(server, user_id=7, email="clen@uvar.si")

    response = prihlaseny(server).post("/api/platba/start")

    assert response.status_code == 200
    data = response.json()
    assert data["url"].startswith(CHECKOUT + "?")
    assert "checkout%5Bcustom%5D%5Buser_id%5D=7" in data["url"]
    assert data["volne_miesta"] == 250
    assert naroky(server) == [], "start nesmie sám nič udeliť"


def test_start_odmietne_pouzivatela_ktory_uz_narok_ma(monkeypatch, tmp_path):
    server = zapnute_platby(monkeypatch, tmp_path)
    vytvor_pouzivatela(server)
    client = prihlaseny(server)
    assert posli_webhook(client, objednavka()).status_code == 200

    response = client.post("/api/platba/start")

    assert response.status_code == 409
    assert response.json()["detail"] == "Zakladajúce členstvo už máš aktívne."


def test_start_odmietne_ked_je_vsetkych_250_miest_obsadenych(monkeypatch, tmp_path):
    server = zapnute_platby(monkeypatch, tmp_path)
    vytvor_pouzivatela(server, user_id=999, email="neskoro@uvar.si")
    naplnit_miesta(server, 250)

    response = prihlaseny(server).post("/api/platba/start")

    assert response.status_code == 409
    assert response.json()["detail"] == "Všetkých 250 zakladajúcich miest je obsadených."


def test_start_je_503_ked_chyba_adresa_pokladne(monkeypatch, tmp_path):
    server = zapnute_platby(monkeypatch, tmp_path, LEMON_CHECKOUT_URL=None)
    vytvor_pouzivatela(server)

    response = prihlaseny(server).post("/api/platba/start")

    assert response.status_code == 503
    assert response.json()["detail"] == "Platobná brána zatiaľ nie je nastavená."


@pytest.mark.parametrize("adresa", ["http://uvarsi.lemonsqueezy.com/buy/x", "javascript:alert(1)", "", "   "])
def test_checkout_url_odmietne_nedoveryhodnu_adresu(monkeypatch, tmp_path, adresa):
    server = load_server(monkeypatch, tmp_path)
    with pytest.raises(server.PlatbyNenastavene):
        server.checkout_url(adresa, user_id=1, email="a@uvar.si")


# ------------------------------------------------------------------ podpis
def test_webhook_bez_podpisu_je_401_a_nemeni_stav(monkeypatch, tmp_path):
    server = zapnute_platby(monkeypatch, tmp_path)
    vytvor_pouzivatela(server)
    client = TestClient(server.app, raise_server_exceptions=False)

    response = posli_webhook(client, objednavka(), tajomstvo=None)

    assert response.status_code == 401
    assert response.json()["detail"] == "Neplatný podpis."
    assert naroky(server) == []


@pytest.mark.parametrize("falosny", [
    "0" * 64, "nie-hex", "", podpis(b"{}", "iny-kluc"), "  ", "a" * 300,
])
def test_webhook_s_neplatnym_podpisom_je_401_a_nemeni_stav(monkeypatch, tmp_path, falosny):
    server = zapnute_platby(monkeypatch, tmp_path)
    vytvor_pouzivatela(server)
    client = TestClient(server.app, raise_server_exceptions=False)

    response = posli_webhook(client, objednavka(), hlavicka=falosny)

    assert response.status_code == 401
    assert naroky(server) == []


def test_webhook_odmietne_aj_podpis_spraveny_nad_inym_telom(monkeypatch, tmp_path):
    server = zapnute_platby(monkeypatch, tmp_path)
    vytvor_pouzivatela(server)
    client = TestClient(server.app, raise_server_exceptions=False)
    cudzi = podpis(json.dumps(objednavka(order_id="ord-9")).encode("utf-8"))

    response = posli_webhook(client, objednavka(), hlavicka=cudzi)

    assert response.status_code == 401
    assert naroky(server) == []


def test_webhook_je_401_ked_majitel_nenastavil_tajomstvo(monkeypatch, tmp_path):
    server = zapnute_platby(monkeypatch, tmp_path, LEMON_WEBHOOK_SECRET=None)
    vytvor_pouzivatela(server)
    client = TestClient(server.app, raise_server_exceptions=False)

    response = posli_webhook(client, objednavka(), hlavicka="0" * 64)

    assert response.status_code == 401
    assert naroky(server) == []


@pytest.mark.parametrize("falosny", [None, "", 12345, "ľščťž" * 8, b"0" * 64, "0" * 300])
def test_overenie_podpisu_odmietne_kazdy_nehexovy_vstup(monkeypatch, tmp_path, falosny):
    server = load_server(monkeypatch, tmp_path)
    assert server.overit_podpis(tajomstvo=TAJOMSTVO, telo=b"{}", podpis=falosny) is False


@pytest.mark.parametrize("tajomstvo", [None, "", 12345])
def test_bez_tajomstva_nie_je_ziadny_podpis_platny(monkeypatch, tmp_path, tajomstvo):
    server = load_server(monkeypatch, tmp_path)
    assert server.overit_podpis(tajomstvo=tajomstvo, telo=b"{}", podpis="0" * 64) is False


def test_spravny_podpis_prejde_aj_velkymi_pismenami(monkeypatch, tmp_path):
    server = load_server(monkeypatch, tmp_path)
    telo = b'{"meta":{}}'
    assert server.overit_podpis(
        tajomstvo=TAJOMSTVO, telo=telo, podpis=podpis(telo).upper()
    ) is True


def test_prehnane_velke_telo_sa_odmietne_pred_overovanim_podpisu(monkeypatch, tmp_path):
    server = zapnute_platby(monkeypatch, tmp_path)
    vytvor_pouzivatela(server)
    client = TestClient(server.app, raise_server_exceptions=False)
    telo = b"x" * (server.MAX_TELO_WEBHOOKU + 1)

    response = client.post(
        "/api/platba/webhook", content=telo,
        headers={"X-Signature": podpis(telo), "Content-Type": "application/json"},
    )

    assert response.status_code == 413
    assert naroky(server) == []


def test_overenie_podpisu_pouziva_konstantne_porovnanie():
    zdroj = (ROOT / "app" / "platby.py").read_text(encoding="utf-8")
    assert "hmac.compare_digest" in zdroj, (
        "podpis sa musí porovnávať konštantne, inak sa dá uhádnuť po bajtoch"
    )


def test_odpoved_na_neplatny_podpis_nikdy_neprezradi_tajomstvo(monkeypatch, tmp_path):
    server = zapnute_platby(monkeypatch, tmp_path)
    vytvor_pouzivatela(server)
    client = TestClient(server.app, raise_server_exceptions=False)

    response = posli_webhook(client, objednavka(), hlavicka="0" * 64)

    assert TAJOMSTVO not in response.text
    assert TAJOMSTVO not in str(dict(response.headers))


def test_modul_platby_nikdy_nic_nevypisuje():
    zdroj = (ROOT / "app" / "platby.py").read_text(encoding="utf-8")
    for zakazane in ("print(", "logging", "logger", "sys.stderr", "sys.stdout"):
        assert zakazane not in zdroj, f"platby.py nesmie obsahovať {zakazane}"


def test_tajomstvo_sa_nikdy_neuklada_do_databazy(monkeypatch, tmp_path):
    server = zapnute_platby(monkeypatch, tmp_path)
    vytvor_pouzivatela(server)
    client = prihlaseny(server)
    posli_webhook(client, objednavka())
    client.post("/api/platba/start")

    obsah = (tmp_path / "uvarsi.db").read_bytes()
    assert TAJOMSTVO.encode() not in obsah


# ------------------------------------------------------------------ udelenie
def test_order_created_udeli_narok_s_celym_zaznamom(monkeypatch, tmp_path):
    server = zapnute_platby(monkeypatch, tmp_path)
    vytvor_pouzivatela(server)
    client = TestClient(server.app, raise_server_exceptions=False)

    response = posli_webhook(client, objednavka(order_id="ord-42", total=1900))

    assert response.status_code == 200
    assert response.json()["akcia"] == "udelene"
    zaznam = naroky(server)
    assert len(zaznam) == 1
    assert zaznam[0]["user_id"] == 1
    assert zaznam[0]["produkt"] == "zakladajuci_clen"
    assert zaznam[0]["poskytovatel"] == "lemonsqueezy"
    assert zaznam[0]["objednavka_id"] == "ord-42"
    assert zaznam[0]["suma_centy"] == 1900
    assert zaznam[0]["mena"] == "EUR"
    assert zaznam[0]["stav"] == "aktivny"
    assert zaznam[0]["ziskany_o"] > 0


def test_udeleny_narok_sa_prejavi_v_stave_aj_v_profile(monkeypatch, tmp_path):
    server = zapnute_platby(monkeypatch, tmp_path)
    vytvor_pouzivatela(server)
    client = prihlaseny(server)
    posli_webhook(client, objednavka())

    stav = client.get("/api/platba/stav").json()
    assert stav["ma_narok"] is True
    assert stav["volne_miesta"] == 249
    assert stav["obsadene"] == 1
    assert client.get("/api/me").json()["platiaci"] is True


def test_narok_sa_odvodzuje_z_udalosti_nie_zo_stlpca_platiaci(monkeypatch, tmp_path):
    """Ručne prepnutý `platiaci` nie je dôkaz o platbe — nárok drží tabuľka naroky."""
    server = zapnute_platby(monkeypatch, tmp_path)
    vytvor_pouzivatela(server)
    with closing(server.db()) as con:
        con.execute("UPDATE pouzivatelia SET platiaci=1 WHERE id=1")
        con.commit()

    stav = prihlaseny(server).get("/api/platba/stav").json()

    assert stav["ma_narok"] is False
    assert stav["obsadene"] == 0


def test_zopakovana_udalost_neudeli_narok_druhykrat(monkeypatch, tmp_path):
    server = zapnute_platby(monkeypatch, tmp_path)
    vytvor_pouzivatela(server)
    client = TestClient(server.app, raise_server_exceptions=False)
    payload = objednavka(order_id="ord-42", webhook_id="wh-1")

    prva = posli_webhook(client, payload)
    druha = posli_webhook(client, payload)

    assert prva.json()["akcia"] == "udelene"
    assert druha.status_code == 200
    assert druha.json()["akcia"] == "uz_spracovane"
    assert len(aktivne(server)) == 1


def test_ta_ista_objednavka_pod_inym_id_udalosti_neudeli_druhy_narok(monkeypatch, tmp_path):
    server = zapnute_platby(monkeypatch, tmp_path)
    vytvor_pouzivatela(server)
    client = TestClient(server.app, raise_server_exceptions=False)

    posli_webhook(client, objednavka(order_id="ord-42", webhook_id="wh-1"))
    posli_webhook(client, objednavka(order_id="ord-42", webhook_id="wh-2"))

    assert len(aktivne(server)) == 1


def test_webhook_uklada_surove_id_udalosti(monkeypatch, tmp_path):
    server = zapnute_platby(monkeypatch, tmp_path)
    vytvor_pouzivatela(server)
    client = TestClient(server.app, raise_server_exceptions=False)

    posli_webhook(client, objednavka(order_id="ord-42", webhook_id="wh-abc"))

    with closing(server.db()) as con:
        riadky = [dict(row) for row in con.execute("SELECT * FROM platobne_udalosti")]
    assert [row["event_id"] for row in riadky] == ["wh-abc"]
    assert riadky[0]["typ"] == "order_created"


def test_udalost_pre_nezname_konto_neudeli_nic(monkeypatch, tmp_path):
    server = zapnute_platby(monkeypatch, tmp_path)
    vytvor_pouzivatela(server)
    client = TestClient(server.app, raise_server_exceptions=False)

    response = posli_webhook(client, objednavka(user_id=4242))

    assert response.status_code == 400
    assert naroky(server) == []


def test_udalost_bez_custom_data_neudeli_nic(monkeypatch, tmp_path):
    server = zapnute_platby(monkeypatch, tmp_path)
    vytvor_pouzivatela(server)
    client = TestClient(server.app, raise_server_exceptions=False)
    payload = objednavka()
    payload["meta"].pop("custom_data")

    response = posli_webhook(client, payload)

    assert response.status_code == 400
    assert naroky(server) == []


def test_neznamy_typ_udalosti_neudeli_narok(monkeypatch, tmp_path):
    server = zapnute_platby(monkeypatch, tmp_path)
    vytvor_pouzivatela(server)
    client = TestClient(server.app, raise_server_exceptions=False)

    response = posli_webhook(client, objednavka(udalost="order_created_test"))

    assert response.status_code == 200
    assert response.json()["akcia"] == "ignorovane"
    assert naroky(server) == []


def test_udalost_pre_cudzi_variant_sa_ignoruje(monkeypatch, tmp_path):
    server = zapnute_platby(monkeypatch, tmp_path, LEMON_VARIANT_ID="555")
    vytvor_pouzivatela(server)
    client = TestClient(server.app, raise_server_exceptions=False)

    cudzia = posli_webhook(client, objednavka(order_id="ord-1", variant_id=999))
    spravna = posli_webhook(client, objednavka(order_id="ord-2", variant_id=555))

    assert cudzia.json()["akcia"] == "ignorovane"
    assert spravna.json()["akcia"] == "udelene"
    assert len(aktivne(server)) == 1


def test_pokazene_telo_je_400_a_nemeni_stav(monkeypatch, tmp_path):
    server = zapnute_platby(monkeypatch, tmp_path)
    vytvor_pouzivatela(server)
    client = TestClient(server.app, raise_server_exceptions=False)
    telo = b"{nie je json"

    response = client.post(
        "/api/platba/webhook", content=telo,
        headers={"X-Signature": podpis(telo), "Content-Type": "application/json"},
    )

    assert response.status_code == 400
    assert naroky(server) == []


# ------------------------------------------------------------------ odobranie
@pytest.mark.parametrize("udalost, ocakavany_stav, akcia", [
    ("order_refunded", "vrateny", "vratene"),
    ("subscription_cancelled", "zruseny", "zrusene"),
])
def test_vratenie_a_zrusenie_odoberu_narok(monkeypatch, tmp_path, udalost, ocakavany_stav, akcia):
    server = zapnute_platby(monkeypatch, tmp_path)
    vytvor_pouzivatela(server)
    client = prihlaseny(server)
    posli_webhook(client, objednavka(order_id="ord-42"))
    typ = "subscriptions" if udalost == "subscription_cancelled" else "orders"

    response = posli_webhook(client, objednavka(order_id="ord-42", udalost=udalost, typ=typ))

    assert response.status_code == 200
    assert response.json()["akcia"] == akcia
    assert [row["stav"] for row in naroky(server)] == [ocakavany_stav]
    stav = client.get("/api/platba/stav").json()
    assert stav["ma_narok"] is False
    assert stav["volne_miesta"] == 250
    assert client.get("/api/me").json()["platiaci"] is False


def test_vratenie_uvolni_miesto_pre_dalsieho_clena(monkeypatch, tmp_path):
    server = zapnute_platby(monkeypatch, tmp_path)
    vytvor_pouzivatela(server)
    client = prihlaseny(server)
    posli_webhook(client, objednavka(order_id="ord-42"))
    naplnit_miesta(server, 249, od=100)
    assert client.get("/api/platba/stav").json()["volne_miesta"] == 0

    posli_webhook(client, objednavka(order_id="ord-42", udalost="order_refunded"))

    assert client.get("/api/platba/stav").json()["volne_miesta"] == 1


def test_vratenie_neznamej_objednavky_nic_nerozbije(monkeypatch, tmp_path):
    server = zapnute_platby(monkeypatch, tmp_path)
    vytvor_pouzivatela(server)
    client = TestClient(server.app, raise_server_exceptions=False)

    response = posli_webhook(client, objednavka(order_id="ord-neznama", udalost="order_refunded"))

    assert response.status_code == 200
    assert response.json()["akcia"] == "ignorovane"
    assert naroky(server) == []


# ------------------------------------------------------------------ kapacita
def naplnit_miesta(server, pocet, od=1000):
    """Priame naplnenie udelených nárokov — simuluje už zaplatených členov."""
    now = server.AUTH_CLOCK()
    with closing(server.db()) as con:
        for index in range(pocet):
            user_id = od + index
            con.execute("INSERT INTO pouzivatelia (id, email) VALUES (?, ?)",
                        (user_id, f"clen{user_id}@uvar.si"))
            con.execute(
                """INSERT INTO naroky (user_id, produkt, poskytovatel, objednavka_id,
                                       suma_centy, mena, stav, ziskany_o, zmeneny_o)
                   VALUES (?, 'zakladajuci_clen', 'lemonsqueezy', ?, 1900, 'EUR', 'aktivny', ?, ?)""",
                (user_id, f"seed-{user_id}", now, now),
            )
        con.commit()


def test_stav_hlasi_skutocny_pocet_volnych_miest(monkeypatch, tmp_path):
    server = zapnute_platby(monkeypatch, tmp_path)
    vytvor_pouzivatela(server)
    naplnit_miesta(server, 3)

    stav = prihlaseny(server).get("/api/platba/stav").json()

    assert stav["kapacita"] == 250
    assert stav["obsadene"] == 3
    assert stav["volne_miesta"] == 247


def test_251_platba_sa_zaznamena_ale_neudeli_narok(monkeypatch, tmp_path):
    server = zapnute_platby(monkeypatch, tmp_path)
    vytvor_pouzivatela(server)
    naplnit_miesta(server, 250)
    client = TestClient(server.app, raise_server_exceptions=False)

    response = posli_webhook(client, objednavka(order_id="ord-251"))

    assert response.status_code == 200
    assert response.json()["akcia"] == "nad_kapacitu"
    assert len(aktivne(server)) == 250
    nadbytocny = [row for row in naroky(server) if row["objednavka_id"] == "ord-251"]
    assert nadbytocny[0]["stav"] == "nad_kapacitu", "peniaze sa musia dať dohľadať a vrátiť"


def test_dve_sucasne_platby_neobsadia_miesto_251(monkeypatch, tmp_path):
    """Race: dvaja zaplatia naraz, keď je voľné posledné miesto. Vyhrať smie jeden."""
    server = zapnute_platby(monkeypatch, tmp_path)
    naplnit_miesta(server, 249)
    with closing(server.db()) as con:
        con.executemany("INSERT INTO pouzivatelia (id, email) VALUES (?, ?)",
                        [(1, "prvy@uvar.si"), (2, "druhy@uvar.si")])
        con.commit()

    brana = threading.Barrier(2)
    vysledky = []

    def doruc(user_id):
        payload = objednavka(user_id=user_id, order_id=f"ord-{user_id}")
        brana.wait(timeout=10)
        with closing(server.db()) as con:
            vysledky.append(server.spracuj_udalost(con, payload=payload, now=server.AUTH_CLOCK()))

    vlakna = [threading.Thread(target=doruc, args=(user_id,)) for user_id in (1, 2)]
    for vlakno in vlakna:
        vlakno.start()
    for vlakno in vlakna:
        vlakno.join(timeout=30)

    akcie = sorted(vysledok["akcia"] for vysledok in vysledky)
    assert akcie == ["nad_kapacitu", "udelene"], f"nečakaný výsledok pretekov: {akcie}"
    assert len(aktivne(server)) == 250


def test_databaza_nedovoli_dva_aktivne_naroky_pre_jedno_konto(monkeypatch, tmp_path):
    import sqlite3

    server = zapnute_platby(monkeypatch, tmp_path)
    vytvor_pouzivatela(server)
    now = server.AUTH_CLOCK()

    def vloz(con, objednavka_id):
        con.execute(
            """INSERT INTO naroky (user_id, produkt, poskytovatel, objednavka_id,
                                   suma_centy, mena, stav, ziskany_o, zmeneny_o)
               VALUES (1, 'zakladajuci_clen', 'lemonsqueezy', ?, 1900, 'EUR',
                       'aktivny', ?, ?)""",
            (objednavka_id, now, now),
        )

    with closing(server.db()) as con:
        vloz(con, "a")
        with pytest.raises(sqlite3.IntegrityError):
            vloz(con, "b")


# ------------------------------------------------------------------ schéma
def test_platobna_schema_vznikne_na_cerstvej_databaze(monkeypatch, tmp_path):
    server = load_server(monkeypatch, tmp_path)
    with closing(server.db()) as con:
        tabulky = {row[0] for row in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        stlpce = {row[1] for row in con.execute("PRAGMA table_info(naroky)")}

    assert {"naroky", "platobne_udalosti"} <= tabulky
    assert {
        "user_id", "produkt", "poskytovatel", "objednavka_id", "suma_centy",
        "mena", "stav", "ziskany_o", "zmeneny_o",
    } <= stlpce


def test_opakovane_otvorenie_databazy_je_idempotentne(monkeypatch, tmp_path):
    server = load_server(monkeypatch, tmp_path)
    with closing(server.db()):
        pass
    with closing(server.db()) as con:
        assert con.execute("SELECT COUNT(*) FROM naroky").fetchone()[0] == 0
