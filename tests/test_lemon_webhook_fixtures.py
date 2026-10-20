"""Ročné predplatné nad webhookmi v tvare, aký Lemon Squeezy naozaj posiela.

POZOR — PRÍPRAVKY PODĽA DOKUMENTÁCIE, NIE ZACHYTENÉ TELÁ.
Telá nižšie sú poskladané podľa verejnej dokumentácie Lemon Squeezy
(JSON:API objekty Subscription, Order a Subscription invoice a príklady
webhookov: data.type, data.id, data.attributes{store_id, customer_id,
order_id, product_id, variant_id, status, renews_at, ends_at, user_email,
test_mode, ...}, meta.event_name, meta.custom_data). Pred spustením živých
platieb ich treba NAHRADIŤ skutočnými telami zachytenými z test-mode nákupu
(Settings → Webhooks → doručenia) a test znova spustiť. Dôležité rozdiely
oproti starším syntetickým telám v test_subscription_webhooks.py:

* objekt predplatného nemá currency, total, discount_id ani billing_period_*,
* faktúra (subscription-invoices) nemá order_id, variant_id ani obdobie,
* objednávka má discount_total namiesto discount_id a order_refunded nemá
  id faktúry,
* ID v atribútoch sú čísla, data.id je reťazec.
"""

from __future__ import annotations

import copy
import hashlib
import json
import sqlite3
from contextlib import closing

import pytest

from app import platby, predplatne


ATTEMPT_ID = "attempt_" + "c" * 43
STORE = 12345
VARIANT = 67890
PRODUCT = 2468
FOUNDER_DISCOUNT_ID = "111"
CUSTOMER = 555
ORDER = 9001
SUBSCRIPTION = "4321"
INVOICE_INITIAL = "7001"
INVOICE_RENEWAL = "7002"
EXPECTED = {
    "store_id": str(STORE),
    "variant_id": str(VARIANT),
    "founder_discount_id": FOUNDER_DISCOUNT_ID,
    "currency": "EUR",
    "test_mode": True,
}
# 2026-09-12T00:00:00Z
P0_START = 1_789_171_200.0
NOW = P0_START + 100.0
CREATED = "2026-09-12T00:01:40.000000Z"
RENEWS_1 = "2027-09-12T00:00:00.000000Z"
RENEWS_2 = "2028-09-12T00:00:00.000000Z"


def _ts(value):
    return predplatne._timestamp(value)


def _meta(event_name, *, custom=True):
    meta = {
        "test_mode": True,
        "event_name": event_name,
        "webhook_id": "a2b9c3d4-0000-4000-8000-000000000001",
    }
    if custom:
        meta["custom_data"] = {"attempt_id": ATTEMPT_ID, "user_id": "1"}
    return meta


def lemon_order(
    event_name="order_created", *, status="paid", refunded_amount=0,
    subtotal=4_900, discount_total=1_000, tax=729, tax_inclusive=True,
    total=3_900, updated_at="2026-09-12T00:01:38.000000Z",
):
    return {
        "meta": _meta(event_name),
        "data": {
            "type": "orders",
            "id": str(ORDER),
            "attributes": {
                "store_id": STORE,
                "customer_id": CUSTOMER,
                "identifier": "104e18a2-d755-4d4b-80c4-a6c1dcbe1c10",
                "order_number": 1,
                "user_name": "Jana Testovacia",
                "user_email": "jana@example.test",
                "currency": "EUR",
                "currency_rate": "1.0000",
                "subtotal": subtotal,
                "setup_fee": 0,
                "discount_total": discount_total,
                "tax": tax,
                "total": total,
                "subtotal_usd": subtotal,
                "discount_total_usd": discount_total,
                "tax_usd": tax,
                "total_usd": total,
                "tax_name": "VAT",
                "tax_rate": "23.00",
                "tax_inclusive": tax_inclusive,
                "status": status,
                "status_formatted": status.replace("_", " ").title(),
                "refunded": status in {"refunded", "partial_refund"},
                "refunded_at": (
                    "2026-10-01T10:00:00.000000Z"
                    if status in {"refunded", "partial_refund"} else None
                ),
                "refunded_amount": refunded_amount,
                "first_order_item": {
                    "id": 1,
                    "order_id": ORDER,
                    "product_id": PRODUCT,
                    "variant_id": VARIANT,
                    "price_id": 1,
                    "product_name": "Uvar.si Premium",
                    "variant_name": "Ročne",
                    "price": 4_900,
                    "quantity": 1,
                    "created_at": "2026-09-12T00:01:35.000000Z",
                    "updated_at": "2026-09-12T00:01:35.000000Z",
                    "test_mode": True,
                },
                "urls": {"receipt": "https://app.lemonsqueezy.com/my-orders/x"},
                "created_at": "2026-09-12T00:01:35.000000Z",
                "updated_at": updated_at,
                "test_mode": True,
            },
            "relationships": {
                "store": {"links": {}},
                "customer": {"links": {}},
            },
            "links": {"self": f"https://api.lemonsqueezy.com/v1/orders/{ORDER}"},
        },
    }


def lemon_subscription(
    event_name="subscription_created", *, status="active",
    renews_at=RENEWS_1, ends_at=None, cancelled=False,
    updated_at="2026-09-12T00:01:41.000000Z",
):
    return {
        "meta": _meta(event_name),
        "data": {
            "type": "subscriptions",
            "id": SUBSCRIPTION,
            "attributes": {
                "store_id": STORE,
                "customer_id": CUSTOMER,
                "order_id": ORDER,
                "order_item_id": 1,
                "product_id": PRODUCT,
                "variant_id": VARIANT,
                "product_name": "Uvar.si Premium",
                "variant_name": "Ročne",
                "user_name": "Jana Testovacia",
                "user_email": "jana@example.test",
                "status": status,
                "status_formatted": status.replace("_", " ").title(),
                "card_brand": "visa",
                "card_last_four": "4242",
                "payment_processor": "stripe",
                "pause": None,
                "cancelled": cancelled,
                "trial_ends_at": None,
                "billing_anchor": 12,
                "first_subscription_item": {
                    "id": 1,
                    "subscription_id": int(SUBSCRIPTION),
                    "price_id": 1,
                    "quantity": 1,
                    "is_usage_based": False,
                    "created_at": CREATED,
                    "updated_at": CREATED,
                },
                "urls": {
                    "update_payment_method": "https://example.lemonsqueezy.com/u",
                    "customer_portal": "https://example.lemonsqueezy.com/billing",
                    "customer_portal_update_subscription": (
                        "https://example.lemonsqueezy.com/billing/u"
                    ),
                },
                "renews_at": renews_at,
                "ends_at": ends_at,
                "created_at": CREATED,
                "updated_at": updated_at,
                "test_mode": True,
            },
            "relationships": {},
            "links": {
                "self": f"https://api.lemonsqueezy.com/v1/subscriptions/{SUBSCRIPTION}"
            },
        },
    }


def lemon_invoice(
    event_name="subscription_payment_success", *, invoice_id=INVOICE_INITIAL,
    billing_reason="initial", status="paid", subtotal=4_900,
    discount_total=1_000, tax=729, total=3_900, refunded_amount=0,
    created_at="2026-09-12T00:01:42.000000Z",
    updated_at="2026-09-12T00:01:43.000000Z",
):
    return {
        "meta": _meta(event_name),
        "data": {
            "type": "subscription-invoices",
            "id": invoice_id,
            "attributes": {
                "store_id": STORE,
                "subscription_id": int(SUBSCRIPTION),
                "customer_id": CUSTOMER,
                "user_name": "Jana Testovacia",
                "user_email": "jana@example.test",
                "billing_reason": billing_reason,
                "card_brand": "visa",
                "card_last_four": "4242",
                "currency": "EUR",
                "currency_rate": "1.0000",
                "status": status,
                "status_formatted": status.title(),
                "refunded": status in {"refunded", "partial_refund"},
                "refunded_at": None,
                "subtotal": subtotal,
                "discount_total": discount_total,
                "tax": tax,
                "tax_inclusive": True,
                "total": total,
                "refunded_amount": refunded_amount,
                "subtotal_usd": subtotal,
                "discount_total_usd": discount_total,
                "tax_usd": tax,
                "total_usd": total,
                "refunded_amount_usd": refunded_amount,
                "urls": {"invoice_url": "https://app.lemonsqueezy.com/my-orders/i"},
                "created_at": created_at,
                "updated_at": updated_at,
                "test_mode": True,
            },
            "relationships": {},
        },
    }


class FakeLookup:
    """Náhrada klienta API: GET /v1/subscriptions/{id} a /v1/orders/{id}."""

    def __init__(self):
        self.subscription_row = lemon_subscription()["data"]
        self.order_row = lemon_order()["data"]
        self.calls = []

    def subscription(self, identifier):
        self.calls.append(("subscription", str(identifier)))
        return {"data": copy.deepcopy(self.subscription_row)}

    def order(self, identifier):
        self.calls.append(("order", str(identifier)))
        return {"data": copy.deepcopy(self.order_row)}


@pytest.fixture
def con():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.executescript(
        """
        CREATE TABLE pouzivatelia (
          id INTEGER PRIMARY KEY, email TEXT,
          platiaci INTEGER NOT NULL DEFAULT 0
        );
        INSERT INTO pouzivatelia (id,email) VALUES (1,'jana@example.test');
        """
    )
    platby.migrate_platby_schema(connection)
    predplatne.migrate_subscription_schema(connection)
    connection.execute(
        """INSERT INTO checkout_attempts
           (public_id,user_id,product,amount_cents,renewal_amount_cents,
            currency,billing_interval,auto_renews,founder,discount_id,
            discount_code,legal_version,privacy_version,consent_json,
            accepted_at,expires_at,founder_reserved_until,test_mode,status,
            provider_checkout_id,provider_order_id)
           VALUES (?,1,'premium_annual',3900,4900,'EUR','year',1,1,?,
                   'FOUNDERS',?,?,'{}',?,?,?,1,'pending','checkout_1',NULL)""",
        (
            ATTEMPT_ID,
            FOUNDER_DISCOUNT_ID,
            platby.LEGAL_VERSION,
            platby.LEGAL_VERSION,
            P0_START + 90.0,
            P0_START + 3_600.0,
            P0_START + 3_600.0,
        ),
    )
    connection.commit()
    try:
        yield connection
    finally:
        connection.close()


def deliver(con, payload, *, lookup=None, now=NOW):
    body = json.dumps(payload, sort_keys=True).encode("utf-8")
    return predplatne.process_subscription_event(
        con,
        payload=payload,
        now=now,
        expected=EXPECTED,
        source="webhook",
        delivery_key=hashlib.sha256(body).hexdigest(),
        lookup=lookup,
    )


def assert_processed(result, action):
    assert result["review_required"] is False, result
    assert result["action"] == action


def activate(con, lookup):
    for payload in (lemon_order(), lemon_subscription(), lemon_invoice()):
        assert predplatne.is_subscription_event(con, payload) is True
    assert_processed(deliver(con, lemon_order(), lookup=lookup), "paired")
    assert_processed(
        deliver(con, lemon_subscription(), lookup=lookup), "activated"
    )
    assert_processed(
        deliver(con, lemon_invoice(), lookup=lookup), "invoice_recorded"
    )


def test_realny_tvar_webhookov_prejde_celym_zivotnym_cyklom(con):
    lookup = FakeLookup()
    activate(con, lookup)
    snapshot = predplatne.subscription_for_user(con, 1)
    assert snapshot.status == "active"
    assert snapshot.provider_order_id == str(ORDER)
    assert snapshot.period_start == _ts(CREATED)
    assert snapshot.period_end == _ts(RENEWS_1)
    assert predplatne.subscription_access(snapshot, now=NOW + 60) is True

    assert_processed(
        deliver(
            con,
            lemon_subscription(
                "subscription_updated",
                updated_at="2026-09-12T00:02:00.000000Z",
            ),
            lookup=lookup,
        ),
        "active",
    )

    # Obnova: faktúra nemá obdobie ani nový renews_at; doplní ich
    # predplatné načítané podľa ID.
    lookup.subscription_row = lemon_subscription(
        "subscription_updated", renews_at=RENEWS_2,
        updated_at="2027-09-12T00:00:05.000000Z",
    )["data"]
    assert_processed(
        deliver(
            con,
            lemon_invoice(
                invoice_id=INVOICE_RENEWAL,
                billing_reason="renewal",
                subtotal=4_900,
                discount_total=0,
                tax=916,
                total=4_900,
                created_at="2027-09-12T00:00:03.000000Z",
                updated_at="2027-09-12T00:00:04.000000Z",
            ),
            lookup=lookup,
        ),
        "invoice_recorded",
    )
    snapshot = predplatne.subscription_for_user(con, 1)
    assert snapshot.paid_through == _ts(RENEWS_2)

    assert_processed(
        deliver(
            con,
            lemon_subscription(
                "subscription_cancelled", status="cancelled",
                renews_at=RENEWS_2, ends_at=RENEWS_2, cancelled=True,
                updated_at="2027-10-01T00:00:00.000000Z",
            ),
            lookup=lookup,
        ),
        "cancelled",
    )
    assert_processed(
        deliver(
            con,
            lemon_subscription(
                "subscription_expired", status="expired",
                renews_at=RENEWS_2, ends_at=RENEWS_2, cancelled=True,
                updated_at="2028-09-12T00:00:05.000000Z",
            ),
            lookup=lookup,
        ),
        "expired",
    )
    assert predplatne.subscription_for_user(con, 1).status == "expired"


def test_bez_api_sa_subscription_created_doplni_z_overenej_objednavky(con):
    # order_created overil sumu aj zľavu a spároval pokus; z neho sa
    # doplní subscription_created, ktorý tieto polia nenesie.
    assert_processed(deliver(con, lemon_order()), "paired")
    assert_processed(deliver(con, lemon_subscription()), "activated")
    snapshot = predplatne.subscription_for_user(con, 1)
    assert snapshot.founder is True
    assert snapshot.initial_amount_cents == 3_900
    assert snapshot.currency == "EUR"


def test_subscription_created_pred_objednavkou_bez_api_ide_na_kontrolu(con):
    result = deliver(con, lemon_subscription())
    assert result["review_required"] is True
    assert predplatne.subscription_for_user(con, 1) is None


def test_order_refunded_bez_id_faktury_sa_sparuje_s_prvou_faktorou(con):
    lookup = FakeLookup()
    activate(con, lookup)
    refund = lemon_order(
        "order_refunded", status="refunded", refunded_amount=3_900,
        updated_at="2026-09-20T10:00:00.000000Z",
    )
    refund["meta"].pop("custom_data")
    assert predplatne.is_subscription_event(con, refund) is True
    result = deliver(con, refund, lookup=lookup)
    assert result["review_required"] is False, result
    assert result["action"] == "refunded"
    status = con.execute(
        "SELECT status,refunded_amount_cents FROM subscription_invoices "
        "WHERE provider_invoice_id=?",
        (INVOICE_INITIAL,),
    ).fetchone()
    assert tuple(status) == ("refunded", 3_900)


def test_ina_zlava_ako_zakladajuca_neprejde(con):
    lookup = FakeLookup()
    result = deliver(
        con, lemon_order(discount_total=2_000, total=2_900), lookup=lookup
    )
    assert result["review_required"] is True


def test_doplnenie_neprepise_hodnotu_z_podpisaneho_tela(con):
    lookup = FakeLookup()
    activate(con, lookup)
    payload = lemon_subscription(
        "subscription_updated", updated_at="2026-09-12T00:03:00.000000Z"
    )
    payload["data"]["attributes"]["currency"] = "USD"
    enriched = predplatne.enrich_provider_payload(
        con, payload, expected=EXPECTED, lookup=lookup
    )
    assert enriched["data"]["attributes"]["currency"] == "USD"
    assert payload["data"]["attributes"].get("billing_period_start") is None
    assert deliver(con, payload, lookup=lookup)["review_required"] is True


# ---------------------------------------------------------------- refundácia
def test_plna_refundacia_konci_pristup_a_zrusenie_ho_iba_potvrdi(con):
    lookup = FakeLookup()
    activate(con, lookup)
    refund_at = NOW + 86_400
    result = deliver(
        con,
        lemon_invoice(
            "subscription_payment_refunded", status="refunded",
            refunded_amount=3_900, updated_at="2026-09-13T00:01:43.000000Z",
        ),
        lookup=lookup,
        now=refund_at,
    )
    assert result["review_required"] is False, result
    assert result["action"] == "refunded"
    assert result["access_ended"] is True
    assert result["provider_subscription_id"] == SUBSCRIPTION
    assert result["provider_order_id"] == str(ORDER)
    snapshot = predplatne.subscription_for_user(con, 1)
    assert snapshot.status == "expired"
    assert snapshot.renews_at is None
    assert predplatne.subscription_access(snapshot, now=refund_at + 1) is False

    # Lemon po našom DELETE pošle subscription_cancelled/updated so stavom
    # cancelled; to nesmie vrátiť prístup ani založiť falošnú kontrolu.
    for name in ("subscription_updated", "subscription_cancelled"):
        confirmation = deliver(
            con,
            lemon_subscription(
                name, status="cancelled", ends_at=RENEWS_1, cancelled=True,
                updated_at=f"2026-09-13T00:0{2 if name.endswith('d') else 3}:00.000000Z",
            ),
            lookup=lookup,
            now=refund_at + 60,
        )
        assert confirmation["review_required"] is False, confirmation
    assert predplatne.subscription_for_user(con, 1).status == "expired"


def test_ciastocna_refundacia_pristup_nekonci(con):
    lookup = FakeLookup()
    activate(con, lookup)
    result = deliver(
        con,
        lemon_invoice(
            "subscription_payment_refunded", status="partial_refund",
            refunded_amount=1_000, updated_at="2026-09-13T00:01:43.000000Z",
        ),
        lookup=lookup,
    )
    assert result["action"] == "partial_refund"
    assert result["access_ended"] is False
    assert predplatne.subscription_for_user(con, 1).status == "active"


@pytest.mark.parametrize("zrus", [None, "zlyha"])
def test_bez_zrusenia_u_poskytovatela_vznikne_pozadovana_akcia(con, zrus):
    def zlyha(_subscription_id):
        raise OSError("api down")

    vysledok = {
        "access_ended": True,
        "user_id": 1,
        "provider_order_id": str(ORDER),
        "provider_subscription_id": SUBSCRIPTION,
    }
    assert platby.dokonci_refundaciu_predplatneho(
        con, vysledok, now=NOW, zrus=zlyha if zrus else None
    ) is False
    row = con.execute(
        "SELECT case_type,provider_order_id,status FROM payment_cases"
    ).fetchone()
    assert tuple(row) == (platby.DRUH_ZRUSIT_PREDPLATNE, str(ORDER), "open")
    alert = platby.priprav_upozornenie(
        platby.DRUH_ZRUSIT_PREDPLATNE, den="2026-09-12", pocet=1
    )
    assert str(ORDER) not in json.dumps(alert, ensure_ascii=False)


def test_uspesne_zrusenie_nezaklada_pripad(con):
    zrusene = []
    assert platby.dokonci_refundaciu_predplatneho(
        con,
        {
            "access_ended": True,
            "user_id": 1,
            "provider_order_id": str(ORDER),
            "provider_subscription_id": SUBSCRIPTION,
        },
        now=NOW,
        zrus=zrusene.append,
    ) is True
    assert zrusene == [SUBSCRIPTION]
    assert con.execute("SELECT COUNT(*) FROM payment_cases").fetchone()[0] == 0


def _live(payload):
    payload = copy.deepcopy(payload)
    payload["meta"]["test_mode"] = False
    attributes = payload["data"]["attributes"]
    attributes["test_mode"] = False
    if isinstance(attributes.get("first_order_item"), dict):
        attributes["first_order_item"]["test_mode"] = False
    return payload


def test_webhook_plnej_refundacie_zrusi_predplatne_uzavrie_ziadost_a_napise(
        monkeypatch, tmp_path):
    from test_platby import load_server, posli_webhook, vytvor_pouzivatela
    from fastapi.testclient import TestClient

    secret = "fixture-live-webhook-secret"
    server = load_server(
        monkeypatch,
        tmp_path,
        PLATBY_ZAPNUTE="1",
        UVARSI_PAYMENTS_ENABLED="1",
        LEMON_WEBHOOK_SECRET=secret,
        LEMON_API_KEY="live-api-key",
        LEMON_STORE_ID=str(STORE),
        LEMON_SUBSCRIPTION_VARIANT_ID=str(VARIANT),
        LEMON_FOUNDER_DISCOUNT_ID=FOUNDER_DISCOUNT_ID,
        LEMON_FOUNDER_DISCOUNT_CODE="FOUNDERS",
    )
    monkeypatch.setattr(server, "AUTH_CLOCK", lambda: NOW + 600)
    vytvor_pouzivatela(server, email="jana@example.test", session=None)
    with closing(server.db()) as db:
        db.execute(
            """INSERT INTO checkout_attempts
               (public_id,user_id,product,amount_cents,renewal_amount_cents,
                currency,billing_interval,auto_renews,founder,discount_id,
                discount_code,legal_version,privacy_version,consent_json,
                accepted_at,expires_at,founder_reserved_until,test_mode,status,
                provider_checkout_id,provider_order_id)
               VALUES (?,1,'premium_annual',3900,4900,'EUR','year',1,1,?,
                       'FOUNDERS',?,?,'{}',?,?,?,0,'pending','checkout_1',NULL)""",
            (
                ATTEMPT_ID, FOUNDER_DISCOUNT_ID, platby.LEGAL_VERSION,
                platby.LEGAL_VERSION, P0_START + 90.0, P0_START + 3_600.0,
                P0_START + 3_600.0,
            ),
        )
        db.commit()

    api_calls = []
    lookup = FakeLookup()

    def fake_lemon(api_key, resource, identifier, *, method="GET"):
        api_calls.append((method, resource, identifier))
        if method == "DELETE":
            return {"data": {"type": resource, "id": identifier}}
        row = lookup.subscription_row if resource == "subscriptions" else lookup.order_row
        return {"data": _live({"meta": {}, "data": row})["data"]}

    mails = []
    alerts = []
    monkeypatch.setattr(server, "_lemon_resource_request", fake_lemon)
    monkeypatch.setattr(
        server, "posli_mail", lambda komu, predmet, *_a, **_k: mails.append((komu, predmet))
    )
    monkeypatch.setattr(server, "posli_upozornenie_majitelovi", alerts.append)
    client = TestClient(server.app, raise_server_exceptions=False)
    for payload in (lemon_order(), lemon_subscription(), lemon_invoice()):
        response = posli_webhook(client, _live(payload), tajomstvo=secret)
        assert response.status_code == 200, response.text

    with closing(server.db()) as db:
        request = server.customer_requests.create_subscription_withdrawal(
            db, user_id=1, invoice_id=INVOICE_INITIAL, message="", now=NOW + 700,
        )
    refund = _live(lemon_order(
        "order_refunded", status="refunded", refunded_amount=3_900,
        updated_at="2026-09-12T02:00:00.000000Z",
    ))
    response = posli_webhook(client, refund, tajomstvo=secret)
    assert response.status_code == 200, response.text
    assert response.json()["akcia"] == "refunded"

    assert ("DELETE", "subscriptions", SUBSCRIPTION) in api_calls
    with closing(server.db()) as db:
        status = db.execute(
            "SELECT status FROM consumer_requests WHERE public_id=?",
            (request.public_id,),
        ).fetchone()[0]
        subscription = db.execute(
            "SELECT status FROM subscriptions WHERE user_id=1"
        ).fetchone()[0]
        cases = db.execute("SELECT COUNT(*) FROM payment_cases").fetchone()[0]
    assert status == "refunded"
    assert subscription == "expired"
    assert cases == 0
    assert mails == [
        ("jana@example.test", platby.MAIL_PREDMET_REFUNDACIA_PREDPLATNEHO)
    ]
    assert alerts == []


# ------------------------------------- stratený subscription_created
def _reconciliation():
    import importlib
    import sys

    sys.modules.pop("rekonciliacia", None)
    return importlib.import_module("rekonciliacia")


def _reconcile(con, *, provider_rows, invoice_rows, now=NOW + 3_600):
    return _reconciliation().reconcile_subscriptions(
        con,
        provider_rows=provider_rows,
        invoice_rows=invoice_rows,
        now=now,
        expected=EXPECTED,
    )


def test_rekonciliacia_aktivuje_predplatne_po_stratenom_subscription_created(con):
    # order_created prišiel a spároval pokus, subscription_created sa stratil.
    assert_processed(deliver(con, lemon_order()), "paired")
    assert predplatne.subscription_for_user(con, 1) is None

    summary = _reconcile(
        con,
        provider_rows=[lemon_subscription()["data"]],
        invoice_rows=[lemon_invoice()["data"]],
    )

    snapshot = predplatne.subscription_for_user(con, 1)
    assert snapshot is not None, summary
    assert snapshot.status == "active"
    assert snapshot.provider_subscription_id == SUBSCRIPTION
    assert snapshot.founder is True
    assert snapshot.initial_amount_cents == 3_900
    assert predplatne.subscription_access(snapshot, now=NOW + 7_200) is True
    assert con.execute(
        "SELECT invoice_kind FROM subscription_invoices WHERE provider_invoice_id=?",
        (INVOICE_INITIAL,),
    ).fetchone()[0] == "initial"
    assert summary["error_codes"] == [], summary

    # Druhý beh nič nezdvojí.
    again = _reconcile(
        con,
        provider_rows=[lemon_subscription()["data"]],
        invoice_rows=[lemon_invoice()["data"]],
    )
    assert again["error_codes"] == []
    assert con.execute("SELECT COUNT(*) FROM subscriptions").fetchone()[0] == 1


def test_rekonciliacia_sparuje_aj_bez_order_created_podla_emailu_a_okna(con):
    _reconcile(
        con,
        provider_rows=[lemon_subscription()["data"]],
        invoice_rows=[lemon_invoice()["data"]],
    )
    snapshot = predplatne.subscription_for_user(con, 1)
    assert snapshot is not None
    assert snapshot.provider_order_id == str(ORDER)
    attempt = con.execute(
        "SELECT status,provider_order_id FROM checkout_attempts WHERE public_id=?",
        (ATTEMPT_ID,),
    ).fetchone()
    assert tuple(attempt) == ("paid", str(ORDER))


def test_bez_zaplatenej_prvej_faktury_sa_nic_neaktivuje(con):
    assert_processed(deliver(con, lemon_order()), "paired")
    summary = _reconcile(
        con,
        provider_rows=[lemon_subscription()["data"]],
        invoice_rows=[],
    )
    assert predplatne.subscription_for_user(con, 1) is None
    assert summary["subscription_drift"] >= 1


def test_nejednoznacny_pokus_sa_neháda(con):
    con.execute(
        """INSERT INTO checkout_attempts
           (public_id,user_id,product,amount_cents,renewal_amount_cents,
            currency,billing_interval,auto_renews,founder,discount_id,
            discount_code,legal_version,privacy_version,consent_json,
            accepted_at,expires_at,founder_reserved_until,test_mode,status)
           VALUES (?,1,'premium_annual',3900,4900,'EUR','year',1,1,?,
                   'FOUNDERS',?,?,'{}',?,?,?,1,'pending')""",
        (
            "attempt_" + "d" * 43, FOUNDER_DISCOUNT_ID, platby.LEGAL_VERSION,
            platby.LEGAL_VERSION, P0_START + 95.0, P0_START + 3_600.0,
            P0_START + 3_600.0,
        ),
    )
    con.commit()
    _reconcile(
        con,
        provider_rows=[lemon_subscription()["data"]],
        invoice_rows=[lemon_invoice()["data"]],
    )
    assert predplatne.subscription_for_user(con, 1) is None


# ------------------------------------- webhook po vypršaní pokusu
def test_lehota_po_vyprsani_je_rovnaka_v_oboch_moduloch():
    assert predplatne.CHECKOUT_PAYMENT_GRACE_SECONDS == (
        platby.CHECKOUT_PAYMENT_GRACE_SECONDS
    )


def test_oneskoreny_webhook_sa_sparuje_s_vyprsanym_pokusom(con):
    # Iný checkout medzitým upratal pokus ako vypršaný.
    con.execute(
        "UPDATE checkout_attempts SET status='expired',founder_reserved_until=NULL "
        "WHERE public_id=?",
        (ATTEMPT_ID,),
    )
    con.commit()
    late = NOW + 6 * 3_600
    assert_processed(deliver(con, lemon_order(), now=late), "paired")
    assert_processed(deliver(con, lemon_subscription(), now=late), "activated")
    snapshot = predplatne.subscription_for_user(con, 1)
    assert snapshot.founder is True
    assert con.execute(
        "SELECT status FROM checkout_attempts WHERE public_id=?", (ATTEMPT_ID,)
    ).fetchone()[0] == "paid"


def test_objednavka_po_lehote_sa_nesparuje(con):
    con.execute(
        "UPDATE checkout_attempts SET status='expired' WHERE public_id=?",
        (ATTEMPT_ID,),
    )
    con.commit()
    order = lemon_order()
    # expires_at = P0_START + 1 h; objednávka vznikla o 1 h + 24 h + 1 s.
    order["data"]["attributes"]["created_at"] = "2026-09-13T01:00:01.000000Z"
    result = deliver(con, order, now=NOW + 2 * 86_400)
    assert result["review_required"] is True
    assert con.execute(
        "SELECT status FROM checkout_attempts WHERE public_id=?", (ATTEMPT_ID,)
    ).fetchone()[0] == "expired"


def test_zaplateny_pokus_drzi_zakladajuce_miesto_kym_ho_nezapocita_predplatne(con):
    after_ttl = P0_START + 2 * 3_600
    assert platby.volne_miesta(con, test_mode=True, now=after_ttl) == 50
    assert_processed(deliver(con, lemon_order()), "paired")
    # Pokus je zaplatený, predplatné ešte nevzniklo: miesto sa neuvoľní ani
    # po uplynutí pôvodnej rezervácie.
    assert platby.volne_miesta(con, test_mode=True, now=after_ttl) == 49
    assert_processed(deliver(con, lemon_subscription()), "activated")
    assert platby.founder_places_used(con, test_mode=True) == 1
    assert platby.volne_miesta(con, test_mode=True, now=after_ttl) == 49
