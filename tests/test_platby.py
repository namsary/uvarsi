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
from dataclasses import replace
from datetime import date, datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.payment_smoke_marker import create_marker, live_config_fingerprint, sign_marker


ROOT = Path(__file__).resolve().parents[1]

TAJOMSTVO = "tajny-webhook-podpisovy-kluc"
CHECKOUT = "https://uvarsi.lemonsqueezy.com/buy/11111111-2222-3333-4444-555555555555"
CURRENT_LEGAL_VERSION = "2026-09-11-v2"
CONSENT = {"accept_terms": True, "legal_version": CURRENT_LEGAL_VERSION}
SMOKE_NOW = datetime(2026, 9, 11, 12, 0, tzinfo=timezone.utc)

PLATBY_ENV = (
    "PLATBY_ZAPNUTE",
    "LEMON_WEBHOOK_SECRET",
    "LEMON_CHECKOUT_URL",
    "LEMON_STORE_ID",
    "LEMON_VARIANT_ID",
    "LEMON_API_KEY",
    "LEMON_TEST_CHECKOUT_URL",
    "LEMON_TEST_WEBHOOK_SECRET",
    "LEMON_TEST_STORE_ID",
    "LEMON_TEST_VARIANT_ID",
    "LEMON_TEST_API_KEY",
    "UVARSI_VERIFIED_SUPPORT_PHONE",
    "UVARSI_PAYMENT_SMOKE_SIGNING_SECRET",
)


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
    server = load_server(monkeypatch, tmp_path, **prostredie)
    ready = server.PaymentReadiness(
        ready=True,
        blockers=(),
        legal_version=server.LEGAL_VERSION,
        release=server.release_id(),
    )
    monkeypatch.setattr(
        server, "_runtime_payment_readiness", lambda con, **kwargs: ready
    )
    return server


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


def objednavka(user_id=1, order_id="ord-1", udalost="order_created", total=3900,
               mena="EUR", webhook_id=None, variant_id=None, typ="orders",
               attempt_id=None, test_mode=False):
    attributes = {
        "total": total,
        "currency": mena,
        "status": "paid",
        "test_mode": test_mode,
    }
    if udalost == "order_refunded":
        attributes.update(
            status="refunded", refunded=True, refunded_amount=total
        )
    if variant_id is not None:
        attributes["first_order_item"] = {"variant_id": variant_id}
    if typ == "subscriptions":
        attributes["order_id"] = order_id
    meta = {"event_name": udalost, "custom_data": {"user_id": str(user_id)}}
    if attempt_id is not None:
        meta["custom_data"]["checkout_attempt"] = attempt_id
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


def test_payment_smoke_marker_musi_sediet_s_vydanim_obchodom_a_variantom(
        monkeypatch, tmp_path):
    server = load_server(
        monkeypatch,
        tmp_path,
        UVARSI_PAYMENT_SMOKE_SIGNING_SECRET=TAJOMSTVO,
    )
    marker_path = tmp_path / "payment-smoke.json"
    monkeypatch.setattr(server, "PAYMENT_SMOKE_MARKER", str(marker_path))

    assert server._payment_smoke_verified(
        release="release-1", checkout_url=CHECKOUT,
        store_id="store-1", variant_id="variant-1",
        test_store_id="test-store", test_variant_id="test-variant",
        now=SMOKE_NOW,
    ) is False

    marker = create_marker(
        release="release-1",
        live_config_digest=live_config_fingerprint(
            secret=TAJOMSTVO, checkout_url=CHECKOUT,
            store_id="store-1", variant_id="variant-1",
        ),
        test_store_id="test-store",
        test_variant_id="test-variant",
        completed_at="2026-09-11T11:30:00+00:00",
        receipt_email_verified=True,
        test_mode_verified=True,
    )
    marker_path.write_text(
        json.dumps(sign_marker(marker, secret=TAJOMSTVO)), encoding="utf-8"
    )

    assert server._payment_smoke_verified(
        release="release-1", checkout_url=CHECKOUT,
        store_id="store-1", variant_id="variant-1",
        test_store_id="test-store", test_variant_id="test-variant",
        now=SMOKE_NOW,
    ) is True
    assert server._payment_smoke_verified(
        release="release-2", checkout_url=CHECKOUT,
        store_id="store-1", variant_id="variant-1",
        test_store_id="test-store", test_variant_id="test-variant",
        now=SMOKE_NOW,
    ) is False

    assert server._payment_smoke_verified(
        release="release-1", checkout_url=CHECKOUT,
        store_id="store-1", variant_id="variant-1",
        test_store_id="other-test-store", test_variant_id="test-variant",
        now=SMOKE_NOW,
    ) is False


def test_payment_smoke_marker_po_24_hodinach_uz_neodomkne_checkout(
        monkeypatch, tmp_path):
    server = load_server(
        monkeypatch,
        tmp_path,
        UVARSI_PAYMENT_SMOKE_SIGNING_SECRET=TAJOMSTVO,
    )
    marker_path = tmp_path / "payment-smoke.json"
    monkeypatch.setattr(server, "PAYMENT_SMOKE_MARKER", str(marker_path))
    marker = create_marker(
        release="release-1",
        live_config_digest=live_config_fingerprint(
            secret=TAJOMSTVO, checkout_url=CHECKOUT,
            store_id="store-1", variant_id="variant-1",
        ),
        test_store_id="test-store",
        test_variant_id="test-variant",
        completed_at="2026-09-10T11:59:59+00:00",
        receipt_email_verified=True,
        test_mode_verified=True,
    )
    marker_path.write_text(
        json.dumps(sign_marker(marker, secret=TAJOMSTVO)), encoding="utf-8"
    )

    assert server._payment_smoke_verified(
        release="release-1", checkout_url=CHECKOUT,
        store_id="store-1", variant_id="variant-1",
        test_store_id="test-store", test_variant_id="test-variant",
        now=SMOKE_NOW,
    ) is False


def test_nezmeneny_payment_smoke_marker_sa_necita_z_disku_opakovane(
        monkeypatch, tmp_path):
    server = load_server(
        monkeypatch,
        tmp_path,
        UVARSI_PAYMENT_SMOKE_SIGNING_SECRET=TAJOMSTVO,
    )
    marker_path = tmp_path / "payment-smoke.json"
    monkeypatch.setattr(server, "PAYMENT_SMOKE_MARKER", str(marker_path))
    marker = create_marker(
        release="release-1",
        live_config_digest=live_config_fingerprint(
            secret=TAJOMSTVO, checkout_url=CHECKOUT,
            store_id="store-1", variant_id="variant-1",
        ),
        test_store_id="test-store",
        test_variant_id="test-variant",
        completed_at="2026-09-11T11:30:00+00:00",
        receipt_email_verified=True,
        test_mode_verified=True,
    )
    marker_path.write_text(
        json.dumps(sign_marker(marker, secret=TAJOMSTVO)), encoding="utf-8"
    )
    original_read_text = server.Path.read_text
    reads = 0

    def counted_read_text(path, *args, **kwargs):
        nonlocal reads
        reads += 1
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(server.Path, "read_text", counted_read_text)
    arguments = {
        "release": "release-1",
        "checkout_url": CHECKOUT,
        "store_id": "store-1",
        "variant_id": "variant-1",
        "test_store_id": "test-store",
        "test_variant_id": "test-variant",
        "now": SMOKE_NOW,
    }

    assert server._payment_smoke_verified(**arguments) is True
    assert server._payment_smoke_verified(**arguments) is True
    assert reads == 1


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


def test_webhook_s_vypnutymi_platbami_neudeli_nic_a_nesiahne_na_kluce(monkeypatch, tmp_path):
    """Vypínač drží: žiadny nárok a žiadne čítanie LEMON_* premenných.

    Telo sa pritom nezahodí — odloží sa a spracuje neskôr (aj s overením
    podpisu). Podrobne to drží tests/test_platby_rekonciliacia.py.
    """
    server = load_server(monkeypatch, tmp_path, LEMON_WEBHOOK_SECRET=TAJOMSTVO)
    vytvor_pouzivatela(server)

    def zakazany_env(key, default=None):
        assert not key.startswith("LEMON"), f"vypnuté platby nesmú čítať {key}"
        return "" if key == "PLATBY_ZAPNUTE" else default

    monkeypatch.setattr(server, "env", zakazany_env)
    client = TestClient(server.app, raise_server_exceptions=False)
    response = posli_webhook(client, objednavka())

    assert response.status_code == 200
    assert response.json()["akcia"] == "odlozene"
    assert naroky(server) == []


def test_stav_hovori_zrozumitelne_ze_platby_este_nebezia(monkeypatch, tmp_path):
    server = load_server(monkeypatch, tmp_path)
    vytvor_pouzivatela(server)

    response = prihlaseny(server).get("/api/platba/stav")

    assert response.status_code == 200
    data = response.json()
    assert data["platby_zapnute"] is False
    assert data["ma_narok"] is False
    assert data["volne_miesta"] == 50
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

    response = prihlaseny(server).post("/api/platba/start", json=CONSENT)

    assert response.status_code == 200
    data = response.json()
    assert data["url"].startswith(CHECKOUT + "?")
    assert "checkout%5Bcustom%5D%5Buser_id%5D=7" in data["url"]
    assert "checkout%5Bcustom%5D%5Bcheckout_attempt%5D=" in data["url"]
    assert data["volne_miesta"] == 50
    assert naroky(server) == [], "start nesmie sám nič udeliť"
    with closing(server.db()) as con:
        attempt = con.execute(
            "SELECT user_id, product, amount_cents, currency, legal_version, status "
            "FROM checkout_attempts"
        ).fetchone()
    assert tuple(attempt) == (
        7, "zakladajuci_clen", 3900, "EUR", server.LEGAL_VERSION, "pending"
    )


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"accept_terms": False, "legal_version": CURRENT_LEGAL_VERSION},
        {"accept_terms": 1, "legal_version": CURRENT_LEGAL_VERSION},
        {"accept_terms": True},
        {"accept_terms": True, "legal_version": "stara-verzia"},
    ],
)
def test_checkout_vyzaduje_vyslovny_suhlas_s_aktualnou_verziou(
    monkeypatch, tmp_path, body
):
    server = zapnute_platby(monkeypatch, tmp_path)
    vytvor_pouzivatela(server)

    response = prihlaseny(server).post("/api/platba/start", json=body)

    assert response.status_code == 422
    assert response.json()["detail"] == "Pred platbou potvrď aktuálne VOP a ochranu údajov."
    with closing(server.db()) as con:
        assert con.execute("SELECT COUNT(*) FROM checkout_attempts").fetchone()[0] == 0


def test_start_odmietne_pouzivatela_ktory_uz_narok_ma(monkeypatch, tmp_path):
    server = zapnute_platby(monkeypatch, tmp_path)
    vytvor_pouzivatela(server)
    client = prihlaseny(server)
    assert posli_webhook(client, objednavka()).status_code == 200

    response = client.post("/api/platba/start", json=CONSENT)

    assert response.status_code == 409
    assert response.json()["detail"] == "Zakladajúce členstvo už máš aktívne."


def test_start_odmietne_ked_je_vsetkych_50_miest_obsadenych(monkeypatch, tmp_path):
    server = zapnute_platby(monkeypatch, tmp_path)
    vytvor_pouzivatela(server, user_id=999, email="neskoro@uvar.si")
    naplnit_miesta(server, 50)

    response = prihlaseny(server).post("/api/platba/start", json=CONSENT)

    assert response.status_code == 409
    assert response.json()["detail"] == "Všetkých 50 zakladajúcich miest je obsadených."


def test_start_je_503_ked_chyba_adresa_pokladne(monkeypatch, tmp_path):
    server = zapnute_platby(monkeypatch, tmp_path, LEMON_CHECKOUT_URL=None)
    vytvor_pouzivatela(server)

    response = prihlaseny(server).post("/api/platba/start", json=CONSENT)

    assert response.status_code == 503
    assert response.json()["detail"] == "Platobná brána zatiaľ nie je nastavená."


def test_zapnuty_checkout_s_neuplnou_pripravenostou_zlyha_bezpecne_a_upozorni_raz(
    monkeypatch, tmp_path
):
    server = zapnute_platby(monkeypatch, tmp_path)
    vytvor_pouzivatela(server)
    unready = server.PaymentReadiness(
        ready=False,
        blockers=("payment_smoke_missing", "alerts_not_private"),
        legal_version=server.LEGAL_VERSION,
        release=server.release_id(),
    )
    monkeypatch.setattr(
        server, "_runtime_payment_readiness", lambda con, **kwargs: unready
    )
    alerts = []
    monkeypatch.setattr(server, "posli_upozornenie_majitelovi", alerts.append)
    client = prihlaseny(server)

    first = client.post("/api/platba/start")
    second = client.post("/api/platba/start")

    assert first.status_code == second.status_code == 503
    assert first.json()["detail"] == "Platby ešte neprešli bezpečnostnou kontrolou."
    assert len(alerts) == 1
    assert "payment_smoke_missing" not in json.dumps(alerts)
    assert "LEMON" not in json.dumps(alerts)
    assert naroky(server) == []


def test_health_a_prihlaseny_profil_zverejnia_len_bezpecny_stav_pripravenosti(
    monkeypatch, tmp_path
):
    server = zapnute_platby(monkeypatch, tmp_path)
    vytvor_pouzivatela(server)
    expected = server.PaymentReadiness(
        ready=False,
        blockers=("price_source_not_approved",),
        legal_version=server.LEGAL_VERSION,
        release=server.release_id(),
    )
    monkeypatch.setattr(
        server, "_runtime_payment_readiness", lambda con, **kwargs: expected
    )
    client = prihlaseny(server)

    health = client.get("/api/health").json()["payment_readiness"]
    me = client.get("/api/me").json()

    assert health == {
        "ready": False,
        "blockers": ["price_source_not_approved"],
        "legal_version": server.LEGAL_VERSION,
        "release": server.release_id(),
    }
    assert me["platby_zapnute"] is True
    assert me["platby_pripravene"] is False
    assert me["pravna_verzia"] == server.LEGAL_VERSION
    assert me["zakladajuci_cena_centy"] == 3900
    assert me["zakladajuci_mena"] == "EUR"
    assert me["zakladajuci_volne_miesta"] == 50
    forbidden = json.dumps({"health": health, "me": me})
    assert "LEMON_" not in forbidden
    assert TAJOMSTVO not in forbidden


def test_runtime_price_source_gate_reads_only_complete_reviewed_server_rows(
    monkeypatch, tmp_path
):
    server = load_server(monkeypatch, tmp_path)
    today = server.datetime.date(2026, 9, 9)
    with closing(server.db()) as con:
        assert server._approved_price_sources_ready(con, today=today) is False
        con.execute(
            """CREATE TABLE zber_stav (
              tyzden TEXT, obchod TEXT, stav TEXT, pocet INTEGER,
              collector_kind TEXT, source_fingerprint TEXT,
              valid_from TEXT, valid_to TEXT,
              PRIMARY KEY (tyzden, obchod)
            )"""
        )
        con.executemany(
            """INSERT INTO zber_stav
               (tyzden,obchod,stav,pocet,collector_kind,source_fingerprint,
                valid_from,valid_to)
               VALUES ('2026-09-07',?,'ok',30,'manual-reviewed-facts',?,
                       '2026-09-07','2026-09-13')""",
            [(store, "a" * 64) for store in server.source_policy.REQUIRED_STORES],
        )
        for store in server.source_policy.REQUIRED_STORES:
            for index in range(server.source_policy.MIN_FACTS_PER_STORE):
                offer = {
                    "obchod": store,
                    "nazov": f"{store} potravina {index}",
                    "kategoria": "trvanlive",
                    "cena": 1.0 + index / 100,
                    "povodna": None,
                    "zlava": "",
                    "jednotka": "1 ks",
                    "source_url": f"https://example.test/{store.casefold()}/{index}",
                    "source_page": index + 1,
                    "valid_from": "2026-09-07",
                    "valid_to": "2026-09-13",
                    "cena_s_kartou": None,
                    "zlava_s_kartou": None,
                    "vernostny_program": None,
                    "minimalny_nakup": None,
                    "podmienka_s_kartou": None,
                }
                con.execute(
                    """INSERT INTO akcie
                       (tyzden,obchod,nazov,kategoria,cena,povodna,zlava,jednotka,
                        source_url,source_page,valid_from,valid_to,offer_key,
                        cena_s_kartou,zlava_s_kartou,vernostny_program,
                        minimalny_nakup,podmienka_s_kartou)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        "2026-09-07", offer["obchod"], offer["nazov"],
                        offer["kategoria"], offer["cena"], offer["povodna"],
                        offer["zlava"], offer["jednotka"], offer["source_url"],
                        offer["source_page"], offer["valid_from"], offer["valid_to"],
                        server.offer_key_for("2026-09-07", offer),
                        offer["cena_s_kartou"], offer["zlava_s_kartou"],
                        offer["vernostny_program"], offer["minimalny_nakup"],
                        offer["podmienka_s_kartou"],
                    ),
                )

        assert server._approved_price_sources_ready(con, today=today) is True

        con.execute("UPDATE akcie SET cena=-1 WHERE obchod='Tesco'")
        assert server._approved_price_sources_ready(con, today=today) is False
        con.execute(
            "UPDATE akcie SET cena=1.0 + (source_page - 1) / 100.0 "
            "WHERE obchod='Tesco'"
        )

        con.execute(
            "UPDATE zber_stav SET collector_kind=CASE obchod "
            "WHEN 'Kaufland' THEN 'official-kaufland-offers' "
            "WHEN 'Tesco' THEN 'official-tesco-viewer' "
            "WHEN 'Lidl' THEN 'official-lidl-viewer' END"
        )
        assert server._approved_price_sources_ready(con, today=today) is False

        con.execute(
            "UPDATE zber_stav SET collector_kind='manual-reviewed-facts'"
        )
        con.execute(
            "UPDATE zber_stav SET collector_kind='kupino-aggregator' WHERE obchod='Tesco'"
        )
        assert server._approved_price_sources_ready(con, today=today) is False


def test_runtime_readiness_requires_current_receipt_test_config_and_verified_phone(
        monkeypatch, tmp_path):
    server = load_server(
        monkeypatch,
        tmp_path,
        LEMON_CHECKOUT_URL=CHECKOUT,
        LEMON_WEBHOOK_SECRET="live-webhook",
        LEMON_STORE_ID="live-store",
        LEMON_VARIANT_ID="live-variant",
        LEMON_API_KEY="live-api",
        LEMON_TEST_CHECKOUT_URL="https://uvarsi.lemonsqueezy.com/checkout/test",
        LEMON_TEST_WEBHOOK_SECRET="test-webhook",
        LEMON_TEST_STORE_ID="test-store",
        LEMON_TEST_VARIANT_ID="test-variant",
        LEMON_TEST_API_KEY="test-api",
        UVARSI_VERIFIED_SUPPORT_PHONE="+421 900 123 456",
    )
    monkeypatch.setattr(
        server, "OPERATOR", replace(server.OPERATOR, support_phone="+421 900 123 456")
    )
    monkeypatch.setattr(server, "legal_version", lambda: server.LEGAL_VERSION)
    monkeypatch.setattr(server, "_approved_price_sources_ready", lambda *_a, **_k: True)
    monkeypatch.setattr(server, "_strict_current_receipt_ready", lambda **_k: True)
    monkeypatch.setattr(server.customer_requests, "workflow_ready", lambda _con: True)
    monkeypatch.setattr(server, "_payment_smoke_verified", lambda **_k: True)

    queue = {"worker_alive": True, "blocking_code": None}
    recipe = {
        "ready": False,
        "blockers": ["payments_enabled"],
        "release_gate": {
            "active_recipes": server.CURATED_RECIPE_COUNT,
            "curation_generation": 1,
            "provenance_complete": True,
            "library_errors": 0,
            "workflow_errors": 0,
        },
    }
    with closing(server.db()) as con:
        result = server._runtime_payment_readiness(
            con, queue_status=queue, recipe_status=recipe
        )

    assert result.ready is True

    monkeypatch.setattr(server, "_strict_current_receipt_ready", lambda **_k: False)
    with closing(server.db()) as con:
        result = server._runtime_payment_readiness(
            con, queue_status=queue, recipe_status=recipe
        )
    assert result.ready is False
    assert result.blockers == ("receipt_unhealthy",)


def test_empty_recipe_or_blocked_plan_worker_never_passes_runtime_readiness(
        monkeypatch, tmp_path):
    server = load_server(monkeypatch, tmp_path)

    assert server._recipe_gate_ready({}) is False
    assert server._recipe_gate_ready({
        "ready": False,
        "blockers": ["payments_enabled"],
        "release_gate": {"provenance_complete": True},
    }) is False
    assert server._recipe_gate_ready({
        "ready": False,
        "blockers": None,
        "release_gate": {},
    }) is False
    assert server._plan_worker_gate_ready(
        {"worker_alive": True, "blocking_code": "queue_oldest_exceeded"}
    ) is False


def test_strict_receipt_gate_rejects_historical_or_incomplete_payload(
        monkeypatch, tmp_path):
    server = load_server(monkeypatch, tmp_path)
    landing_path = tmp_path / "landing.json"
    monkeypatch.setattr(server, "LANDING_DATA", str(landing_path))

    landing_path.write_text("{}", encoding="utf-8")
    assert server._strict_current_receipt_ready(today=date(2026, 9, 11)) is False

    payload = {
        "schema_version": 1,
        "offer_data_version": server.CURRENT_COLLECTION_DATA_VERSION,
        "generated_at": "2026-09-11T08:00:00+02:00",
        "week": "2026-09-07",
        "week_label": "7.–13. 9. 2026",
        "sources": [
            {
                "store": store,
                "url": f"https://example.test/{store.casefold()}",
                "valid_from": "2026-09-07",
                "valid_to": "2026-09-13",
            }
            for store in ("Kaufland", "Tesco", "Lidl")
        ],
        "receipt": {
            "meals": [{
                "day": "PI",
                "name": "Testovacie jedlo",
                "instructions": ["Uvar suroviny domäkka."],
                "items": [
                    {
                        "offer_key": f"offer-{store.casefold()}",
                        "name": f"Surovina {store}",
                        "store": store,
                        "unit": "1 ks",
                        "quantity": 1,
                        "price": "1,00",
                        "original_price": "1,50" if store == "Kaufland" else None,
                        "savings": "0,50" if store == "Kaufland" else None,
                        "off": "-33 %" if store == "Kaufland" else "",
                    }
                    for store in ("Kaufland", "Tesco", "Lidl")
                ],
            }],
            "nakup_spolu": "3,00",
            "bezne": "3,50",
            "usetris": "0,50",
            "polozky": 3,
            "polozky_s_beznou_cenou": 1,
        },
    }
    landing_path.write_text(json.dumps(payload), encoding="utf-8")
    assert server._strict_current_receipt_ready(today=date(2026, 9, 11)) is True

    payload["week"] = "2026-08-31"
    landing_path.write_text(json.dumps(payload), encoding="utf-8")
    assert server._strict_current_receipt_ready(today=date(2026, 9, 11)) is False


@pytest.mark.parametrize("adresa", ["http://uvarsi.lemonsqueezy.com/buy/x", "javascript:alert(1)", "", "   "])
def test_checkout_url_odmietne_nedoveryhodnu_adresu(monkeypatch, tmp_path, adresa):
    server = load_server(monkeypatch, tmp_path)
    with pytest.raises(server.PlatbyNenastavene):
        server.checkout_url(
            adresa, user_id=1, attempt_id="x" * 43, email="a@uvar.si"
        )


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

    response = posli_webhook(client, objednavka(order_id="ord-42", total=3900))

    assert response.status_code == 200
    assert response.json()["akcia"] == "udelene"
    zaznam = naroky(server)
    assert len(zaznam) == 1
    assert zaznam[0]["user_id"] == 1
    assert zaznam[0]["produkt"] == "zakladajuci_clen"
    assert zaznam[0]["poskytovatel"] == "lemonsqueezy"
    assert zaznam[0]["objednavka_id"] == "ord-42"
    assert zaznam[0]["suma_centy"] == 3900
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
    assert stav["volne_miesta"] == 49
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
    with closing(server.db()) as con:
        attempt = server.create_checkout_attempt(
            con, user_id=1, legal_version=server.LEGAL_VERSION, now=server.AUTH_CLOCK()
        )
        con.commit()

    cudzia = posli_webhook(client, objednavka(order_id="ord-1", variant_id=999))
    spravna = posli_webhook(
        client,
        objednavka(order_id="ord-2", variant_id=555, attempt_id=attempt),
    )

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
    assert stav["volne_miesta"] == 50
    assert client.get("/api/me").json()["platiaci"] is False


def test_vratenie_uvolni_miesto_pre_dalsieho_clena(monkeypatch, tmp_path):
    server = zapnute_platby(monkeypatch, tmp_path)
    vytvor_pouzivatela(server)
    client = prihlaseny(server)
    posli_webhook(client, objednavka(order_id="ord-42"))
    naplnit_miesta(server, 49, od=100)
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
                   VALUES (?, 'zakladajuci_clen', 'lemonsqueezy', ?, 3900, 'EUR', 'aktivny', ?, ?)""",
                (user_id, f"seed-{user_id}", now, now),
            )
        con.commit()


def test_stav_hlasi_skutocny_pocet_volnych_miest(monkeypatch, tmp_path):
    server = zapnute_platby(monkeypatch, tmp_path)
    vytvor_pouzivatela(server)
    naplnit_miesta(server, 3)

    stav = prihlaseny(server).get("/api/platba/stav").json()

    assert stav["kapacita"] == 50
    assert stav["obsadene"] == 3
    assert stav["volne_miesta"] == 47


def test_51_platba_sa_zaznamena_ale_neudeli_narok(monkeypatch, tmp_path):
    server = zapnute_platby(monkeypatch, tmp_path)
    vytvor_pouzivatela(server)
    naplnit_miesta(server, 50)
    client = TestClient(server.app, raise_server_exceptions=False)

    response = posli_webhook(client, objednavka(order_id="ord-51"))

    assert response.status_code == 200
    assert response.json()["akcia"] == "nad_kapacitu"
    assert len(aktivne(server)) == 50
    nadbytocny = [row for row in naroky(server) if row["objednavka_id"] == "ord-51"]
    assert nadbytocny[0]["stav"] == "nad_kapacitu", "peniaze sa musia dať dohľadať a vrátiť"


def test_dve_sucasne_platby_neobsadia_miesto_51(monkeypatch, tmp_path):
    """Race: dvaja zaplatia naraz, keď je voľné posledné miesto. Vyhrať smie jeden."""
    server = zapnute_platby(monkeypatch, tmp_path)
    naplnit_miesta(server, 49)
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
    assert len(aktivne(server)) == 50


def test_databaza_nedovoli_dva_aktivne_naroky_pre_jedno_konto(monkeypatch, tmp_path):
    import sqlite3

    server = zapnute_platby(monkeypatch, tmp_path)
    vytvor_pouzivatela(server)
    now = server.AUTH_CLOCK()

    def vloz(con, objednavka_id):
        con.execute(
            """INSERT INTO naroky (user_id, produkt, poskytovatel, objednavka_id,
                                   suma_centy, mena, stav, ziskany_o, zmeneny_o)
               VALUES (1, 'zakladajuci_clen', 'lemonsqueezy', ?, 3900, 'EUR',
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


# ------------------------------------------------------------ ručný nárok
# Platby sú vypnuté, ale Premium (špajza, vyšší denný strop) treba vedieť
# vyskúšať. Odpoveď je jediná a tá istá ako pri platbe: riadok v `naroky`.
# Žiadna premenná prostredia, žiadny zoznam e-mailov, žiadna cesta z appky —
# nárok udelí majiteľ pri databáze a je poznať, že sa zaň neplatilo.
def platby_modul():
    return importlib.import_module("platby")


def test_rucny_narok_odomkne_premium_aj_ked_su_platby_vypnute(monkeypatch, tmp_path):
    server = load_server(monkeypatch, tmp_path)
    platby = platby_modul()
    vytvor_pouzivatela(server)

    with closing(server.db()) as con:
        vysledok = platby.udel_narok_rucne(con, user_id=1, now=server.AUTH_CLOCK())

    assert vysledok == {"akcia": platby.AKCIA_UDELENE}
    assert server.platby_su_zapnute() is False, "vypínač ostáva vypnutý"
    assert prihlaseny(server).get("/api/me").json()["premium"] is True


def test_rucny_narok_je_v_uctovnictve_poznat_ze_nebol_zaplateny(monkeypatch, tmp_path):
    server = load_server(monkeypatch, tmp_path)
    platby = platby_modul()
    vytvor_pouzivatela(server)

    with closing(server.db()) as con:
        platby.udel_narok_rucne(con, user_id=1, now=1234.0)

    riadok = aktivne(server)[0]
    assert riadok["poskytovatel"] == platby.POSKYTOVATEL_RUCNE == "rucne"
    assert riadok["suma_centy"] == 0 and riadok["mena"] is None
    assert riadok["produkt"] == platby.PRODUKT_ZAKLADAJUCI


def test_rucny_testovaci_narok_neznizi_pocet_platenych_miest(monkeypatch, tmp_path):
    server = zapnute_platby(monkeypatch, tmp_path)
    platby = platby_modul()
    vytvor_pouzivatela(server)
    vytvor_pouzivatela(server, user_id=2, email="kupujuci@uvar.si", session="session-2")

    with closing(server.db()) as con:
        platby.udel_narok_rucne(con, user_id=1, now=server.AUTH_CLOCK())

    stav_pred = prihlaseny(server, "session-2").get("/api/platba/stav").json()
    response = posli_webhook(
        TestClient(server.app, raise_server_exceptions=False),
        objednavka(order_id="paid-after-manual", user_id=2),
    )
    stav_po = prihlaseny(server, "session-2").get("/api/platba/stav").json()

    assert stav_pred["obsadene"] == 0
    assert stav_pred["volne_miesta"] == 50
    assert response.json()["akcia"] == "udelene"
    assert stav_po["obsadene"] == 1
    assert stav_po["volne_miesta"] == 49


def test_rucny_narok_neodomkne_nikoho_ineho(monkeypatch, tmp_path):
    server = load_server(monkeypatch, tmp_path)
    platby = platby_modul()
    vytvor_pouzivatela(server)
    vytvor_pouzivatela(server, user_id=2, email="druhy@uvar.si", session="session-2")

    with closing(server.db()) as con:
        platby.udel_narok_rucne(con, user_id=1, now=server.AUTH_CLOCK())
        assert platby.ma_narok(con, 2) is False

    assert prihlaseny(server, "session-2").get("/api/me").json()["premium"] is False


def test_rucny_narok_druhykrat_nevytvori_druhy_zaznam(monkeypatch, tmp_path):
    server = load_server(monkeypatch, tmp_path)
    platby = platby_modul()
    vytvor_pouzivatela(server)

    with closing(server.db()) as con:
        platby.udel_narok_rucne(con, user_id=1, now=1.0)
        opakovanie = platby.udel_narok_rucne(con, user_id=1, now=2.0)

    assert opakovanie == {"akcia": platby.AKCIA_UZ_UDELENE}
    assert len(naroky(server)) == 1


def test_rucny_narok_pre_neexistujuci_ucet_neudeli_nic(monkeypatch, tmp_path):
    server = load_server(monkeypatch, tmp_path)
    platby = platby_modul()

    with closing(server.db()) as con:
        with pytest.raises(platby.UdalostNepouzitelna):
            platby.udel_narok_rucne(con, user_id=77, now=server.AUTH_CLOCK())

    assert naroky(server) == []


@pytest.mark.parametrize("hodnota", [0, -1, True, "1", None, 1.0])
def test_rucny_narok_odmietne_nezmyselne_id(monkeypatch, tmp_path, hodnota):
    server = load_server(monkeypatch, tmp_path)
    platby = platby_modul()

    with closing(server.db()) as con:
        with pytest.raises(ValueError):
            platby.udel_narok_rucne(con, user_id=hodnota, now=server.AUTH_CLOCK())

    assert naroky(server) == []


def test_rucny_narok_nevyda_miesto_nad_kapacitu(monkeypatch, tmp_path):
    """Ani majiteľ si nevypýta 51. miesto — kapacita je kapacita."""
    server = load_server(monkeypatch, tmp_path)
    platby = platby_modul()
    vytvor_pouzivatela(server)
    naplnit_miesta(server, 50)

    with closing(server.db()) as con:
        vysledok = platby.udel_narok_rucne(con, user_id=1, now=server.AUTH_CLOCK())
        assert platby.ma_narok(con, 1) is False

    assert vysledok == {"akcia": platby.AKCIA_NAD_KAPACITU}


def test_rucny_narok_sa_da_zase_odobrat(monkeypatch, tmp_path):
    """Majiteľ si musí vedieť vyskúšať aj bezplatnú verziu, nielen Premium."""
    server = load_server(monkeypatch, tmp_path)
    platby = platby_modul()
    vytvor_pouzivatela(server)

    with closing(server.db()) as con:
        platby.udel_narok_rucne(con, user_id=1, now=1.0)
        vysledok = platby.zrus_narok_rucne(con, user_id=1, now=2.0)
        assert platby.ma_narok(con, 1) is False
        assert con.execute("SELECT platiaci FROM pouzivatelia WHERE id=1").fetchone()[0] == 0

    assert vysledok == {"akcia": platby.AKCIA_ZRUSENE}
    assert prihlaseny(server).get("/api/me").json()["premium"] is False


def test_odobratie_sa_nikdy_nedotkne_zaplateneho_naroku(monkeypatch, tmp_path):
    """Preklep v konzole nesmie zobrať Premium človeku, ktorý zaň zaplatil."""
    server = zapnute_platby(monkeypatch, tmp_path)
    platby = platby_modul()
    vytvor_pouzivatela(server)
    posli_webhook(prihlaseny(server), objednavka())

    with closing(server.db()) as con:
        vysledok = platby.zrus_narok_rucne(con, user_id=1, now=server.AUTH_CLOCK())
        assert platby.ma_narok(con, 1) is True

    assert vysledok == {"akcia": platby.AKCIA_IGNOROVANE}
    assert aktivne(server)[0]["poskytovatel"] == "lemonsqueezy"


def test_udeleny_a_odobraty_narok_sa_da_udelit_znova(monkeypatch, tmp_path):
    server = load_server(monkeypatch, tmp_path)
    platby = platby_modul()
    vytvor_pouzivatela(server)

    with closing(server.db()) as con:
        platby.udel_narok_rucne(con, user_id=1, now=1.0)
        platby.zrus_narok_rucne(con, user_id=1, now=2.0)
        znova = platby.udel_narok_rucne(con, user_id=1, now=3.0)
        assert platby.ma_narok(con, 1) is True

    assert znova == {"akcia": platby.AKCIA_UDELENE}
    assert len(naroky(server)) == 2, "história zostáva dohľadateľná"


def test_do_appky_nevedie_ziadna_cesta_k_rucnemu_naroku(monkeypatch, tmp_path):
    """Nárok udeľuje človek pri databáze, nie požiadavka z internetu."""
    server = load_server(monkeypatch, tmp_path)
    zdroj = (ROOT / "app" / "server.py").read_text(encoding="utf-8")

    assert "udel_narok_rucne" not in zdroj
    assert "zrus_narok_rucne" not in zdroj
    cesty = {getattr(route, "path", "") for route in server.app.routes}
    assert not any("narok" in cesta or "admin" in cesta or "premium" in cesta for cesta in cesty)
