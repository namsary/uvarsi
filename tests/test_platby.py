"""Platby: infraštruktúra pre Zakladajúceho člena — vypnutá, ale celá otestovaná.

Peniaze sa nesmú hýbať skôr, než majiteľ vedome zapne PLATBY_ZAPNUTE. Tieto testy
držia tri veci: vypínač naozaj vypína, podpis webhooku sa overuje konštantne
a nárok sa odvodzuje výhradne z uložených udalostí poskytovateľa — nikdy z klienta.
"""
import hashlib
import hmac
import importlib
import json
import os
import sys
import threading
from contextlib import closing
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.payment_smoke_marker import (
    create_activation_attestation,
    create_marker,
    live_config_fingerprint,
    sign_marker,
    test_config_fingerprint as payment_test_config_fingerprint,
)


ROOT = Path(__file__).resolve().parents[1]

TAJOMSTVO = "tajny-webhook-podpisovy-kluc"
CHECKOUT = "https://uvarsi.lemonsqueezy.com/buy/11111111-2222-3333-4444-555555555555"
LIVE_WEBHOOK_SECRET = "live-webhook-secret"
LIVE_API_KEY = "live-api-key"
TEST_CHECKOUT = "https://uvarsi.lemonsqueezy.com/checkout/test-verified"
TEST_WEBHOOK_SECRET = "test-webhook-secret"
TEST_STORE_ID = "test-store"
TEST_VARIANT_ID = "test-variant"
TEST_API_KEY = "test-api-key"
TEST_CONFIG_DIGEST = "c92c6b55bd48b997ddb73fbc7abbaf44074f989d5bedb0ee0f590a9c9e464a7e"
CURRENT_LEGAL_VERSION = "2026-09-12-v5"
CONSENT = {
    "accept_terms": True,
    "accept_automatic_renewal": True,
    "request_immediate_activation": True,
    "acknowledge_withdrawal_proration": True,
    "legal_version": CURRENT_LEGAL_VERSION,
    "expected_offer_id": "premium-annual-founder-first-year-v1",
    "expected_amount_cents": 3900,
}
STANDARD_CONSENT = {
    **CONSENT,
    "expected_offer_id": "premium-annual-standard-v1",
    "expected_amount_cents": 4900,
}
SMOKE_NOW = datetime(2026, 9, 11, 12, 0, tzinfo=timezone.utc)

PLATBY_ENV = (
    "PLATBY_ZAPNUTE",
    "LEMON_WEBHOOK_SECRET",
    "LEMON_CHECKOUT_URL",
    "LEMON_STORE_ID",
    "LEMON_VARIANT_ID",
    "LEMON_API_KEY",
    "LEMON_SUBSCRIPTION_VARIANT_ID",
    "LEMON_FOUNDER_DISCOUNT_ID",
    "LEMON_FOUNDER_DISCOUNT_CODE",
    "LEMON_TEST_CHECKOUT_URL",
    "LEMON_TEST_WEBHOOK_SECRET",
    "LEMON_TEST_STORE_ID",
    "LEMON_TEST_VARIANT_ID",
    "LEMON_TEST_API_KEY",
    "LEMON_TEST_SUBSCRIPTION_VARIANT_ID",
    "LEMON_TEST_FOUNDER_DISCOUNT_ID",
    "LEMON_TEST_FOUNDER_DISCOUNT_CODE",
    "UVARSI_VERIFIED_SUPPORT_PHONE",
    "UVARSI_PAYMENT_SMOKE_SIGNING_SECRET",
    "UVARSI_PAYMENT_ACTIVATION_MARKER",
)


class FakeSubscriptionCheckoutProvider:
    api_key = "live-api-key"
    store_id = "live-store"
    variant_id = "live-subscription-variant"
    founder_discount_id = "live-founder-discount"
    founder_discount_code = "FOUNDERS"

    def __init__(self):
        self.last_checkout_payload = None
        self.call_count = 0

    def create_checkout(self, payload):
        self.call_count += 1
        self.last_checkout_payload = payload
        attributes = payload["data"]["attributes"]
        total = 3900 if "discount_code" in attributes["checkout_data"] else 4900
        checkout_id = f"checkout-api-{self.call_count}"
        return {
            "data": {
                "type": "checkouts",
                "id": checkout_id,
                "attributes": {
                    "store_id": self.store_id,
                    "variant_id": self.variant_id,
                    "test_mode": attributes["test_mode"],
                    "expires_at": attributes["expires_at"],
                    "preview": {"currency": "EUR", "total": total},
                    "url": (
                        "https://uvarsi.lemonsqueezy.com/checkout/custom/"
                        f"{checkout_id}"
                        "?expires=1&signature=fake-signature"
                    ),
                },
            }
        }


class PausingSubscriptionCheckoutProvider(FakeSubscriptionCheckoutProvider):
    """Pause the first completed fake checkout before local ID binding."""

    def __init__(self):
        super().__init__()
        self.checkout_created = threading.Event()
        self.allow_binding = threading.Event()

    def create_checkout(self, payload):
        response = super().create_checkout(payload)
        if self.call_count == 1:
            self.checkout_created.set()
            if not self.allow_binding.wait(5):
                raise RuntimeError("test checkout binding was not released")
        return response


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


def _annual_payment_environment(**changes):
    values = {
        "LEMON_WEBHOOK_SECRET": "live-webhook",
        "LEMON_STORE_ID": "live-store",
        "LEMON_SUBSCRIPTION_VARIANT_ID": "live-annual",
        "LEMON_FOUNDER_DISCOUNT_ID": "live-founder",
        "LEMON_FOUNDER_DISCOUNT_CODE": "LIVE-FOUNDERS",
        "LEMON_API_KEY": "live-api",
        "LEMON_TEST_WEBHOOK_SECRET": "test-webhook",
        "LEMON_TEST_STORE_ID": "test-store",
        "LEMON_TEST_SUBSCRIPTION_VARIANT_ID": "test-annual",
        "LEMON_TEST_FOUNDER_DISCOUNT_ID": "test-founder",
        "LEMON_TEST_FOUNDER_DISCOUNT_CODE": "TEST-FOUNDERS",
        "LEMON_TEST_API_KEY": "test-api",
        "UVARSI_PAYMENT_SMOKE_SIGNING_SECRET": TAJOMSTVO,
    }
    values.update(changes)
    return values


def _full_annual_subscription_smoke(server, *, completed_at=None):
    marker_module = importlib.import_module("payment_smoke_marker")
    completed_at = completed_at or datetime.now(timezone.utc).replace(
        microsecond=0
    )
    expectation = server._subscription_marker_expectation(server.release_id())

    def provider(config):
        return marker_module.AnnualProviderEvidence(
            store_id=config.store_id,
            variant_id=config.variant_id,
            discount_id=config.discount_id,
            test_mode=config.test_mode,
            annual_price_cents=4_900,
            currency="EUR",
            billing_interval="year",
            billing_interval_count=1,
            trial_days=0,
            variant_status="published",
            discount_kind="fixed",
            discount_amount_cents=1_000,
            discount_duration="once",
            discount_status="published",
            discount_variant_ids=(config.variant_id,),
            discount_redemption_limit=50,
            discount_code_fingerprint=marker_module.discount_code_fingerprint(
                signing_secret=expectation.signing_secret,
                discount_code=config.discount_code,
            ),
        )

    lifecycle = marker_module.SubscriptionLifecycleEvidence(
        initial_charge_cents=3_900,
        renewal_displayed_cents=4_900,
        activation_verified=True,
        renewal_invoice_cents=4_900,
        failed_payment_verified=True,
        recovery_verified=True,
        cancellation_verified=True,
        access_retained_until_period_end=True,
        expiration_verified=True,
        refund_verified=True,
        portal_access_verified=True,
        webhook_signature_verified=True,
        reconciliation_verified=True,
    )
    unsigned = marker_module.create_subscription_marker(
        expectation=expectation,
        live_provider=provider(expectation.live),
        test_provider=provider(expectation.test),
        lifecycle=lifecycle,
        completed_at=completed_at.isoformat(),
        expires_at=(completed_at + timedelta(hours=24)).isoformat(),
        attestation_id="a" * 64,
    )
    signed = marker_module.sign_marker(
        unsigned, secret=expectation.signing_secret
    )
    return marker_module, expectation, signed, completed_at


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
    provider = FakeSubscriptionCheckoutProvider()
    monkeypatch.setattr(
        server,
        "_subscription_checkout_provider",
        lambda *, test_mode: provider,
        raising=False,
    )
    server._fake_subscription_checkout_provider = provider
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
    arguments = {
        "release": "release-1",
        "checkout_url": CHECKOUT,
        "webhook_secret": LIVE_WEBHOOK_SECRET,
        "store_id": "store-1",
        "variant_id": "variant-1",
        "api_key": LIVE_API_KEY,
        "test_checkout_url": TEST_CHECKOUT,
        "test_webhook_secret": TEST_WEBHOOK_SECRET,
        "test_store_id": TEST_STORE_ID,
        "test_variant_id": TEST_VARIANT_ID,
        "test_api_key": TEST_API_KEY,
        "now": SMOKE_NOW,
    }

    assert server._payment_smoke_verified(**arguments) is False

    marker = create_marker(
        release="release-1",
        live_config_digest=live_config_fingerprint(
            secret=TAJOMSTVO, checkout_url=CHECKOUT,
            webhook_secret=LIVE_WEBHOOK_SECRET,
            store_id="store-1", variant_id="variant-1",
            api_key=LIVE_API_KEY,
        ),
        test_config_digest=TEST_CONFIG_DIGEST,
        test_store_id=TEST_STORE_ID,
        test_variant_id=TEST_VARIANT_ID,
        completed_at="2026-09-11T11:30:00+00:00",
        receipt_email_verified=True,
        test_mode_verified=True,
    )
    incomplete_marker = dict(marker)
    incomplete_marker.pop("test_config_digest")
    marker_path.write_text(
        json.dumps(sign_marker(incomplete_marker, secret=TAJOMSTVO)),
        encoding="utf-8",
    )
    assert server._payment_smoke_verified(**arguments) is False

    signed_marker = sign_marker(marker, secret=TAJOMSTVO)
    marker_path.write_text(
        json.dumps(signed_marker), encoding="utf-8"
    )

    assert server._payment_smoke_verified(**arguments) is True
    stored = json.dumps(signed_marker)
    assert TEST_CHECKOUT not in stored
    assert TEST_WEBHOOK_SECRET not in stored
    assert TEST_API_KEY not in stored
    assert LIVE_WEBHOOK_SECRET not in stored
    assert LIVE_API_KEY not in stored

    for field, wrong in (
        ("release", "release-2"),
        ("webhook_secret", "other-live-webhook-secret"),
        ("api_key", "other-live-api-key"),
        ("test_checkout_url", "https://attacker.example/checkout"),
        ("test_webhook_secret", "other-test-webhook-secret"),
        ("test_store_id", "other-test-store"),
        ("test_variant_id", "other-test-variant"),
        ("test_api_key", "other-test-api-key"),
    ):
        assert server._payment_smoke_verified(
            **{**arguments, field: wrong}
        ) is False


def test_smoke_dokaz_musi_byt_vzdy_cerstvy_pred_aktivaciou(
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
            webhook_secret=LIVE_WEBHOOK_SECRET,
            store_id="store-1", variant_id="variant-1",
            api_key=LIVE_API_KEY,
        ),
        test_config_digest=TEST_CONFIG_DIGEST,
        test_store_id=TEST_STORE_ID,
        test_variant_id=TEST_VARIANT_ID,
        completed_at="2026-09-10T11:59:59+00:00",
        receipt_email_verified=True,
        test_mode_verified=True,
    )
    marker_path.write_text(
        json.dumps(sign_marker(marker, secret=TAJOMSTVO)), encoding="utf-8"
    )

    assert server._payment_smoke_verified(
        release="release-1", checkout_url=CHECKOUT,
        webhook_secret=LIVE_WEBHOOK_SECRET,
        store_id="store-1", variant_id="variant-1",
        api_key=LIVE_API_KEY,
        test_checkout_url=TEST_CHECKOUT,
        test_webhook_secret=TEST_WEBHOOK_SECRET,
        test_store_id=TEST_STORE_ID, test_variant_id=TEST_VARIANT_ID,
        test_api_key=TEST_API_KEY,
        now=SMOKE_NOW,
    ) is False

def test_atomic_replace_same_size_and_mtime_zneplatni_payment_marker_cache(
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
            webhook_secret=LIVE_WEBHOOK_SECRET,
            store_id="store-1", variant_id="variant-1",
            api_key=LIVE_API_KEY,
        ),
        test_config_digest=TEST_CONFIG_DIGEST,
        test_store_id=TEST_STORE_ID,
        test_variant_id=TEST_VARIANT_ID,
        completed_at="2026-09-11T11:30:00+00:00",
        receipt_email_verified=True,
        test_mode_verified=True,
    )
    marker_path.write_text(
        json.dumps(sign_marker(marker, secret=TAJOMSTVO)), encoding="utf-8"
    )
    arguments = {
        "release": "release-1",
        "checkout_url": CHECKOUT,
        "webhook_secret": LIVE_WEBHOOK_SECRET,
        "store_id": "store-1",
        "variant_id": "variant-1",
        "api_key": LIVE_API_KEY,
        "test_checkout_url": TEST_CHECKOUT,
        "test_webhook_secret": TEST_WEBHOOK_SECRET,
        "test_store_id": TEST_STORE_ID,
        "test_variant_id": TEST_VARIANT_ID,
        "test_api_key": TEST_API_KEY,
        "now": SMOKE_NOW,
    }

    assert server._payment_smoke_verified(**arguments) is True
    original_stat = marker_path.stat()
    replacement = sign_marker(
        create_marker(
            release="release-2",
            live_config_digest=marker["live_config_digest"],
            test_config_digest=marker["test_config_digest"],
            test_store_id=TEST_STORE_ID,
            test_variant_id=TEST_VARIANT_ID,
            completed_at=marker["completed_at"],
            receipt_email_verified=True,
            test_mode_verified=True,
        ),
        secret=TAJOMSTVO,
    )
    replacement_path = tmp_path / "payment-smoke.replacement"
    replacement_path.write_text(json.dumps(replacement), encoding="utf-8")
    assert replacement_path.stat().st_size == original_stat.st_size
    os.replace(replacement_path, marker_path)
    os.utime(
        marker_path,
        ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns),
    )
    replaced_stat = marker_path.stat()
    assert replaced_stat.st_size == original_stat.st_size
    assert replaced_stat.st_mtime_ns == original_stat.st_mtime_ns

    assert server._payment_smoke_verified(**arguments) is False


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
def test_start_vytvori_overeny_rocny_checkout_s_nepriehladnym_attempt_id(
    monkeypatch, tmp_path
):
    server = zapnute_platby(monkeypatch, tmp_path)
    vytvor_pouzivatela(server, user_id=7, email="clen@uvar.si")

    response = prihlaseny(server).post("/api/platba/start", json=CONSENT)

    assert response.status_code == 200
    data = response.json()
    assert data["url"].endswith("signature=fake-signature")
    assert data["founder"] is True
    assert data["amount_cents"] == 3900
    assert data["renewal_amount_cents"] == 4900
    assert data["volne_miesta"] == 49
    assert naroky(server) == [], "start nesmie sám nič udeliť"
    payload = server._fake_subscription_checkout_provider.last_checkout_payload
    custom = payload["data"]["attributes"]["checkout_data"]["custom"]
    assert set(custom) == {"attempt_id"}
    assert len(custom["attempt_id"]) >= 43
    assert "custom_price" not in payload["data"]["attributes"]
    with closing(server.db()) as con:
        attempt = con.execute(
            "SELECT user_id,product,amount_cents,renewal_amount_cents,currency,"
            "billing_interval,auto_renews,founder,legal_version,status,"
            "provider_checkout_id "
            "FROM checkout_attempts"
        ).fetchone()
    assert tuple(attempt) == (
        7,
        "premium_annual",
        3900,
        4900,
        "EUR",
        "year",
        1,
        1,
        server.LEGAL_VERSION,
        "pending",
        "checkout-api-1",
    )


def test_start_retry_with_active_checkout_fails_closed_without_second_provider_call(
    monkeypatch, tmp_path
):
    server = zapnute_platby(monkeypatch, tmp_path)
    vytvor_pouzivatela(server, user_id=7, email="clen@uvar.si")
    client = prihlaseny(server)

    first = client.post("/api/platba/start", json=CONSENT)
    assert first.status_code == 200
    with closing(server.db()) as con:
        before = tuple(
            con.execute(
                "SELECT status,provider_checkout_id,expires_at,"
                "founder_reserved_until FROM checkout_attempts"
            ).fetchone()
        )

    retry = client.post("/api/platba/start", json=CONSENT)

    assert retry.status_code == 409
    assert retry.json()["detail"] == (
        "Platobná pokladňa je už aktívna. Dokonči ju alebo počkaj do jej expirácie."
    )
    assert server._fake_subscription_checkout_provider.call_count == 1
    with closing(server.db()) as con:
        assert con.execute("SELECT COUNT(*) FROM checkout_attempts").fetchone()[0] == 1
        after_retry = tuple(
            con.execute(
                "SELECT status,provider_checkout_id,expires_at,"
                "founder_reserved_until FROM checkout_attempts"
            ).fetchone()
        )
    assert after_retry == before

    monkeypatch.setattr(server, "AUTH_CLOCK", lambda: before[2] + 1)
    after_expiry = client.post("/api/platba/start", json=CONSENT)

    assert after_expiry.status_code == 200
    assert server._fake_subscription_checkout_provider.call_count == 2
    with closing(server.db()) as con:
        rows = con.execute(
            "SELECT status,provider_checkout_id,expires_at "
            "FROM checkout_attempts ORDER BY accepted_at"
        ).fetchall()
    assert len(rows) == 2
    assert tuple(rows[0]) == ("expired", "checkout-api-1", before[2])
    assert tuple(rows[1]) == (
        "pending",
        "checkout-api-2",
        before[2] + 1 + 60 * 60,
    )


def test_concurrent_retry_is_rejected_while_first_checkout_waits_for_local_binding(
    monkeypatch, tmp_path
):
    server = zapnute_platby(monkeypatch, tmp_path)
    vytvor_pouzivatela(server, user_id=7, email="clen@uvar.si")
    provider = PausingSubscriptionCheckoutProvider()
    monkeypatch.setattr(
        server,
        "_subscription_checkout_provider",
        lambda *, test_mode: provider,
    )
    result = {}

    def start_first_checkout():
        try:
            result["response"] = prihlaseny(server).post(
                "/api/platba/start", json=CONSENT
            )
        except BaseException as error:  # surfaced in the main test thread
            result["error"] = error

    worker = threading.Thread(target=start_first_checkout)
    worker.start()
    assert provider.checkout_created.wait(3)
    with closing(server.db()) as con:
        before_retry = tuple(
            con.execute(
                "SELECT public_id,status,provider_checkout_id,expires_at,"
                "founder_reserved_until FROM checkout_attempts"
            ).fetchone()
        )

    try:
        retry = prihlaseny(server).post("/api/platba/start", json=CONSENT)

        assert retry.status_code == 409
        assert retry.json()["detail"] == (
            "Platobná pokladňa je už aktívna. "
            "Dokonči ju alebo počkaj do jej expirácie."
        )
        assert provider.call_count == 1
        with closing(server.db()) as con:
            assert con.execute(
                "SELECT COUNT(*) FROM checkout_attempts"
            ).fetchone()[0] == 1
            after_retry = tuple(
                con.execute(
                    "SELECT public_id,status,provider_checkout_id,expires_at,"
                    "founder_reserved_until FROM checkout_attempts"
                ).fetchone()
            )
        assert after_retry == before_retry
        assert after_retry[1] == "pending"
        assert after_retry[2] is None
    finally:
        provider.allow_binding.set()
        worker.join(timeout=5)

    assert worker.is_alive() is False
    assert "error" not in result
    assert result["response"].status_code == 200
    assert provider.call_count == 1
    with closing(server.db()) as con:
        bound = tuple(
            con.execute(
                "SELECT public_id,status,provider_checkout_id,expires_at,"
                "founder_reserved_until FROM checkout_attempts"
            ).fetchone()
        )
        assert con.execute("SELECT COUNT(*) FROM checkout_attempts").fetchone()[0] == 1
    assert bound == (
        before_retry[0],
        "pending",
        "checkout-api-1",
        before_retry[3],
        before_retry[4],
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
    assert response.json()["detail"] == (
        "Pred platbou potvrď podmienky, ročnú obnovu a okamžitú aktiváciu."
    )
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


def test_start_po_50_zakladateloch_vytvori_bezny_checkout_za_49(
    monkeypatch, tmp_path
):
    server = zapnute_platby(monkeypatch, tmp_path)
    vytvor_pouzivatela(server, user_id=999, email="neskoro@uvar.si")
    naplnit_predplatene_zakladajuce_miesta(server, 50)

    response = prihlaseny(server).post(
        "/api/platba/start", json=STANDARD_CONSENT
    )

    assert response.status_code == 200
    assert response.json()["founder"] is False
    assert response.json()["amount_cents"] == 4900
    checkout_data = server._fake_subscription_checkout_provider.last_checkout_payload[
        "data"
    ]["attributes"]["checkout_data"]
    assert "discount_code" not in checkout_data


def test_stale_founder_price_never_returns_a_payable_standard_checkout(
    monkeypatch, tmp_path
):
    server = zapnute_platby(monkeypatch, tmp_path)
    vytvor_pouzivatela(server, user_id=999, email="neskoro@uvar.si")
    naplnit_predplatene_zakladajuce_miesta(server, 50)
    client = prihlaseny(server)

    changed = client.post("/api/platba/start", json=CONSENT)

    assert changed.status_code == 409
    body = changed.json()
    assert body["kod"] == "price_changed"
    assert "url" not in body
    assert body["offer"] == {
        "offer_id": "premium-annual-standard-v1",
        "founder": False,
        "amount_cents": 4900,
        "renewal_amount_cents": 4900,
        "currency": "EUR",
        "billing_interval": "year",
        "auto_renews": True,
        "title": "Premium",
        "price_note": "ročne",
        "summary": (
            "49 € ročne. Predplatné sa automaticky obnovuje každý rok, "
            "kým ho nezrušíš."
        ),
    }
    assert server._fake_subscription_checkout_provider.call_count == 0
    with closing(server.db()) as con:
        assert con.execute("SELECT COUNT(*) FROM checkout_attempts").fetchone()[0] == 0

    retried = client.post("/api/platba/start", json=STANDARD_CONSENT)

    assert retried.status_code == 200
    assert retried.json()["amount_cents"] == 4900
    assert server._fake_subscription_checkout_provider.call_count == 1


def test_tampered_expected_price_gets_current_offer_without_checkout(
    monkeypatch, tmp_path
):
    server = zapnute_platby(monkeypatch, tmp_path)
    vytvor_pouzivatela(server, user_id=7, email="clen@uvar.si")

    response = prihlaseny(server).post(
        "/api/platba/start",
        json={**CONSENT, "expected_amount_cents": 1},
    )

    assert response.status_code == 409
    assert response.json()["kod"] == "price_changed"
    assert response.json()["offer"]["amount_cents"] == 3900
    assert "url" not in response.json()
    assert server._fake_subscription_checkout_provider.call_count == 0
    with closing(server.db()) as con:
        assert con.execute("SELECT COUNT(*) FROM checkout_attempts").fetchone()[0] == 0


def test_me_exposes_standard_offer_when_founder_capacity_is_zero(monkeypatch, tmp_path):
    server = zapnute_platby(monkeypatch, tmp_path)
    vytvor_pouzivatela(server, user_id=999, email="neskoro@uvar.si")
    naplnit_predplatene_zakladajuce_miesta(server, 50)

    offer = prihlaseny(server).get("/api/me").json()["checkout_offer"]

    assert offer["offer_id"] == "premium-annual-standard-v1"
    assert offer["amount_cents"] == 4900
    assert "39 €" not in offer["summary"]


def test_start_je_503_ked_chyba_konfiguracia_rocnej_pokladne(
    monkeypatch, tmp_path
):
    server = zapnute_platby(monkeypatch, tmp_path)
    vytvor_pouzivatela(server)
    monkeypatch.setattr(
        server,
        "_subscription_checkout_provider",
        lambda *, test_mode: (_ for _ in ()).throw(
            server.PlatbyNenastavene("missing")
        ),
    )

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


def test_runtime_readiness_accepts_the_code_owned_verified_support_phone(
        monkeypatch, tmp_path):
    server = load_server(
        monkeypatch,
        tmp_path,
        **_annual_payment_environment(),
    )
    assert server.OPERATOR.support_phone == "+421 917 347 009"
    monkeypatch.setattr(server, "legal_version", lambda: server.LEGAL_VERSION)
    monkeypatch.setattr(server, "_approved_price_sources_ready", lambda *_a, **_k: True)
    monkeypatch.setattr(server, "_strict_current_receipt_ready", lambda *_a, **_k: True)
    monkeypatch.setattr(server.customer_requests, "workflow_ready", lambda _con: True)
    smoke_path = tmp_path / "annual-payment-smoke.json"
    activation_path = tmp_path / "annual-payment-activation.json"
    monkeypatch.setattr(server, "PAYMENT_SMOKE_MARKER", str(smoke_path))
    monkeypatch.setattr(server, "PAYMENT_ACTIVATION_MARKER", str(activation_path))
    marker_module, expectation, smoke, completed_at = (
        _full_annual_subscription_smoke(server)
    )
    smoke_path.write_text(json.dumps(smoke), encoding="utf-8")

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
    assert result.blockers == ()

    monkeypatch.setattr(marker_module, "_trusted_utcnow", lambda: completed_at)
    activation = marker_module.create_subscription_activation_attestation(
        smoke, expectation
    )
    activation_path.write_text(json.dumps(activation), encoding="utf-8")
    monkeypatch.setenv("PLATBY_ZAPNUTE", "1")
    with closing(server.db()) as con:
        result = server._runtime_payment_readiness(
            con, queue_status=queue, recipe_status=recipe
        )
    assert result.ready is True
    assert result.blockers == ()

    monkeypatch.setattr(server, "_strict_current_receipt_ready", lambda *_a, **_k: False)
    with closing(server.db()) as con:
        result = server._runtime_payment_readiness(
            con, queue_status=queue, recipe_status=recipe
        )
    assert result.ready is False
    assert result.blockers == ("receipt_unhealthy",)


def test_zapnuty_flag_bez_podpisanej_aktivacie_neodomkne_checkout(
        monkeypatch, tmp_path):
    server = load_server(
        monkeypatch,
        tmp_path,
        **_annual_payment_environment(
            PLATBY_ZAPNUTE="1",
            UVARSI_VERIFIED_SUPPORT_PHONE="+421 900 123 456",
        ),
    )
    monkeypatch.setattr(
        server, "OPERATOR", replace(server.OPERATOR, support_phone="+421 900 123 456")
    )
    monkeypatch.setattr(server, "legal_version", lambda: server.LEGAL_VERSION)
    monkeypatch.setattr(server, "_approved_price_sources_ready", lambda *_a, **_k: True)
    monkeypatch.setattr(server, "_strict_current_receipt_ready", lambda *_a, **_k: True)
    monkeypatch.setattr(server, "_private_payment_alerts_ready", lambda: True)
    monkeypatch.setattr(server.customer_requests, "workflow_ready", lambda _con: True)
    activation_path = tmp_path / "missing-annual-payment-activation.json"
    monkeypatch.setattr(server, "PAYMENT_ACTIVATION_MARKER", str(activation_path))
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

    assert result.ready is False
    assert result.blockers == ("subscription_smoke_missing",)


def test_zapnuty_flag_vyzaduje_cerstvy_provider_marker_a_ekonomiku(
        monkeypatch, tmp_path):
    server = load_server(
        monkeypatch,
        tmp_path,
        **_annual_payment_environment(PLATBY_ZAPNUTE="1"),
    )
    monkeypatch.setattr(server, "legal_version", lambda: server.LEGAL_VERSION)
    monkeypatch.setattr(server, "_approved_price_sources_ready", lambda *_a, **_k: True)
    monkeypatch.setattr(server, "_strict_current_receipt_ready", lambda *_a, **_k: True)
    monkeypatch.setattr(server.customer_requests, "workflow_ready", lambda _con: True)
    smoke_path = tmp_path / "annual-payment-smoke.json"
    activation_path = tmp_path / "annual-payment-activation.json"
    monkeypatch.setattr(server, "PAYMENT_SMOKE_MARKER", str(smoke_path))
    monkeypatch.setattr(server, "PAYMENT_ACTIVATION_MARKER", str(activation_path))
    marker_module, expectation, smoke, completed_at = (
        _full_annual_subscription_smoke(server)
    )
    monkeypatch.setattr(
        marker_module, "_trusted_utcnow", lambda: completed_at
    )
    activation = marker_module.create_subscription_activation_attestation(
        smoke, expectation
    )
    smoke_path.write_text(json.dumps(smoke), encoding="utf-8")
    activation_path.write_text(json.dumps(activation), encoding="utf-8")
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
        assert server._runtime_payment_readiness(
            con, queue_status=queue, recipe_status=recipe
        ).ready is True

    changed_economics = json.loads(json.dumps(smoke))
    changed_economics["live_provider"]["annual_price_cents"] = 3_900
    changed_economics = marker_module.sign_marker(
        changed_economics, secret=expectation.signing_secret
    )
    smoke_path.write_text(json.dumps(changed_economics), encoding="utf-8")

    with closing(server.db()) as con:
        readiness = server._runtime_payment_readiness(
            con, queue_status=queue, recipe_status=recipe
        )
    assert readiness.ready is False
    assert readiness.blockers == ("subscription_smoke_incomplete",)


def test_podpisana_aktivacia_neexpiruje_ale_zmena_configu_ju_zablokuje(
        monkeypatch, tmp_path):
    server = load_server(
        monkeypatch,
        tmp_path,
        UVARSI_PAYMENT_SMOKE_SIGNING_SECRET=TAJOMSTVO,
    )
    activation_path = tmp_path / "payment-activation.json"
    monkeypatch.setattr(server, "PAYMENT_ACTIVATION_MARKER", str(activation_path))
    smoke = create_marker(
        release="release-1",
        live_config_digest=live_config_fingerprint(
            secret=TAJOMSTVO,
            checkout_url=CHECKOUT,
            webhook_secret=LIVE_WEBHOOK_SECRET,
            store_id="store-1",
            variant_id="variant-1",
            api_key=LIVE_API_KEY,
        ),
        test_config_digest=payment_test_config_fingerprint(
            secret=TAJOMSTVO,
            checkout_url=TEST_CHECKOUT,
            webhook_secret=TEST_WEBHOOK_SECRET,
            store_id=TEST_STORE_ID,
            variant_id=TEST_VARIANT_ID,
            api_key=TEST_API_KEY,
        ),
        test_store_id=TEST_STORE_ID,
        test_variant_id=TEST_VARIANT_ID,
        completed_at="2026-09-11T11:30:00+00:00",
        receipt_email_verified=True,
        test_mode_verified=True,
    )
    signed_smoke = sign_marker(smoke, secret=TAJOMSTVO)
    activation = create_activation_attestation(
        signed_smoke,
        secret=TAJOMSTVO,
        release="release-1",
        checkout_url=CHECKOUT,
        webhook_secret=LIVE_WEBHOOK_SECRET,
        store_id="store-1",
        variant_id="variant-1",
        api_key=LIVE_API_KEY,
        test_checkout_url=TEST_CHECKOUT,
        test_webhook_secret=TEST_WEBHOOK_SECRET,
        test_store_id=TEST_STORE_ID,
        test_variant_id=TEST_VARIANT_ID,
        test_api_key=TEST_API_KEY,
        activated_at="2026-09-11T12:00:00+00:00",
    )
    activation_path.write_text(json.dumps(activation), encoding="utf-8")
    arguments = {
        "release": "release-1",
        "checkout_url": CHECKOUT,
        "webhook_secret": LIVE_WEBHOOK_SECRET,
        "store_id": "store-1",
        "variant_id": "variant-1",
        "api_key": LIVE_API_KEY,
        "test_checkout_url": TEST_CHECKOUT,
        "test_webhook_secret": TEST_WEBHOOK_SECRET,
        "test_store_id": TEST_STORE_ID,
        "test_variant_id": TEST_VARIANT_ID,
        "test_api_key": TEST_API_KEY,
    }

    assert server._payment_activation_verified(**arguments) is True
    assert server._payment_activation_verified(
        **{**arguments, "test_api_key": "rotated-test-api-key"}
    ) is False
    assert server._payment_activation_verified(
        **{**arguments, "checkout_url": CHECKOUT + "-changed"}
    ) is False
    assert server._payment_activation_verified(
        **{**arguments, "webhook_secret": "rotated-live-webhook"}
    ) is False
    assert server._payment_activation_verified(
        **{**arguments, "api_key": "rotated-live-api"}
    ) is False

    tampered = dict(activation, activated_at="2026-09-11T12:01:00+00:00")
    activation_path.write_text(json.dumps(tampered), encoding="utf-8")
    assert server._payment_activation_verified(**arguments) is False


@pytest.mark.parametrize(
    "secret_name,rotated_value",
    [
        ("LEMON_WEBHOOK_SECRET", "rotated-live-webhook-secret"),
        ("LEMON_API_KEY", "rotated-live-api-key"),
    ],
)
def test_zmena_ktorehokolvek_live_secretu_zneplatni_aktivaciu_a_checkout(
        monkeypatch, tmp_path, secret_name, rotated_value):
    live_webhook_secret = "verified-live-webhook-secret"
    live_api_key = "verified-live-api-key"
    server = load_server(
        monkeypatch,
        tmp_path,
        **_annual_payment_environment(
            PLATBY_ZAPNUTE="1",
            LEMON_WEBHOOK_SECRET=live_webhook_secret,
            LEMON_API_KEY=live_api_key,
            UVARSI_VERIFIED_SUPPORT_PHONE="+421 900 123 456",
        ),
    )
    monkeypatch.setattr(
        server, "OPERATOR", replace(server.OPERATOR, support_phone="+421 900 123 456")
    )
    monkeypatch.setattr(server, "legal_version", lambda: server.LEGAL_VERSION)
    monkeypatch.setattr(server, "_approved_price_sources_ready", lambda *_a, **_k: True)
    monkeypatch.setattr(server, "_strict_current_receipt_ready", lambda *_a, **_k: True)
    monkeypatch.setattr(server, "_private_payment_alerts_ready", lambda: True)
    monkeypatch.setattr(server.customer_requests, "workflow_ready", lambda _con: True)
    smoke_path = tmp_path / "payment-smoke.json"
    activation_path = tmp_path / "payment-activation.json"
    monkeypatch.setattr(server, "PAYMENT_SMOKE_MARKER", str(smoke_path))
    monkeypatch.setattr(server, "PAYMENT_ACTIVATION_MARKER", str(activation_path))
    marker_module, expectation, smoke, completed_at = (
        _full_annual_subscription_smoke(server)
    )
    monkeypatch.setattr(marker_module, "_trusted_utcnow", lambda: completed_at)
    activation = marker_module.create_subscription_activation_attestation(
        smoke, expectation
    )
    smoke_path.write_text(json.dumps(smoke), encoding="utf-8")
    activation_path.write_text(json.dumps(activation), encoding="utf-8")
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
        assert server._runtime_payment_readiness(
            con, queue_status=queue, recipe_status=recipe
        ).ready is True
    monkeypatch.setenv(secret_name, rotated_value)
    with closing(server.db()) as con:
        readiness = server._runtime_payment_readiness(
            con, queue_status=queue, recipe_status=recipe
        )

    assert readiness.ready is False
    assert readiness.blockers == ("subscription_smoke_mismatch",)


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
    with closing(server.db()) as con:
        assert server._strict_current_receipt_ready(con, today=date(2026, 9, 11)) is False

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
    monkeypatch.setattr(server, "public_receipt_matches_verified_offers", lambda *_args: True)
    with closing(server.db()) as con:
        assert server._strict_current_receipt_ready(con, today=date(2026, 9, 11)) is True

    payload["week"] = "2026-08-31"
    landing_path.write_text(json.dumps(payload), encoding="utf-8")
    with closing(server.db()) as con:
        assert server._strict_current_receipt_ready(con, today=date(2026, 9, 11)) is False


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


def naplnit_predplatene_zakladajuce_miesta(server, pocet, od=1000):
    """Create verified founder subscriptions for annual checkout capacity tests."""
    now = server.AUTH_CLOCK()
    with closing(server.db()) as con:
        for index in range(pocet):
            user_id = od + index
            con.execute(
                """INSERT INTO subscriptions
                   (user_id,product,provider,provider_customer_id,
                    provider_order_id,provider_subscription_id,
                    provider_variant_id,currency,test_mode,status,period_start,
                    period_end,renews_at,ends_at,paid_through,
                    initial_amount_cents,renewal_amount_cents,discount_id,
                    founder,initial_payment_verified,needs_review,review_reason,
                    last_verified_event_at,created_at,updated_at)
                   VALUES (?,'premium_annual','lemonsqueezy',?,?,?,?,
                           'EUR',0,'active',?,?,?,?,?,3900,4900,?,1,1,0,NULL,?,?,?)""",
                (
                    user_id,
                    f"customer-{user_id}",
                    f"order-{user_id}",
                    f"subscription-{user_id}",
                    "live-subscription-variant",
                    now,
                    now + 365 * 24 * 60 * 60,
                    now + 365 * 24 * 60 * 60,
                    None,
                    now + 365 * 24 * 60 * 60,
                    "live-founder-discount",
                    now,
                    now,
                    now,
                ),
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
