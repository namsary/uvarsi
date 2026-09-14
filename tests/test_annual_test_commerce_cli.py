"""Paired annual Test-mode operator flow; provider calls are fakes."""

from __future__ import annotations

import hashlib
import hmac
import importlib.util
import io
import json
import sqlite3
from pathlib import Path

import pytest

from app import payment_smoke_marker as marker
from app import platby, predplatne


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "hetzner" / "payment-lifecycle-probe.py"
NOW = 1_789_200_000.0


class TTY(io.StringIO):
    def isatty(self):
        return True


class Redirected(io.StringIO):
    def isatty(self):
        return False


def load_tool():
    spec = importlib.util.spec_from_file_location("annual_test_commerce", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def annual_config():
    return marker.SubscriptionConfig(
        store_id="test-store",
        variant_id="annual-variant",
        discount_id="founder-discount",
        discount_code="FOUNDER-CODE",
        webhook_secret="annual-test-hook",
        api_key="test-api",
        test_mode=True,
    )


@pytest.fixture
def db():
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.execute("CREATE TABLE pouzivatelia(id INTEGER PRIMARY KEY, email TEXT NOT NULL UNIQUE)")
    con.execute("INSERT INTO pouzivatelia(id,email) VALUES (7,'annual@example.test')")
    platby.migrate_platby_schema(con)
    predplatne.migrate_subscription_schema(con)
    yield con
    con.close()


def checkout_request(calls):
    def request(api_key, path, *, method="GET", payload=None):
        calls.append((api_key, path, method, payload))
        assert path == "/v1/checkouts"
        attrs = payload["data"]["attributes"]
        return {"data": {"type": "checkouts", "id": "annual-checkout-1", "attributes": {
            "test_mode": True,
            "store_id": "test-store",
            "variant_id": "annual-variant",
            "expires_at": attrs["expires_at"],
            "preview": {"currency": "EUR", "total": 3_900},
            "url": "https://store.lemonsqueezy.com/checkout/buy/annual?expires=1&signature=s",
        }}}
    return request


def test_annual_prepare_creates_exact_founder_attempt_and_prints_url_only_to_tty(db):
    tool = load_tool()
    calls = []
    output = TTY()

    result = tool.prepare_annual_checkout(
        db, user_id=7, email="annual@example.test", config=annual_config(),
        legal_version=platby.LEGAL_VERSION, now=NOW,
        stdin=TTY(), stdout=output, stderr=TTY(), request=checkout_request(calls),
        platby_module=platby,
    )

    assert result == {"prepared": True}
    row = dict(db.execute("SELECT * FROM checkout_attempts").fetchone())
    assert row["amount_cents"] == 3_900
    assert row["renewal_amount_cents"] == 4_900
    assert row["billing_interval"] == "year"
    assert row["auto_renews"] == 1
    assert row["test_mode"] == 1
    assert row["discount_id"] == "founder-discount"
    assert row["provider_checkout_id"] == "annual-checkout-1"
    payload = calls[0][3]
    custom = payload["data"]["attributes"]["checkout_data"]["custom"]
    assert set(custom) == {"attempt_id"}
    persisted = json.dumps(row, sort_keys=True)
    assert "lemonsqueezy.com" not in persisted
    assert "test-api" not in persisted
    assert "annual-test-hook" not in persisted
    assert "lemonsqueezy.com/checkout/" in output.getvalue()


def test_annual_prepare_rejects_redirected_output_before_attempt_or_provider_call(db):
    tool = load_tool()
    calls = []
    with pytest.raises(tool.ProbeToolFailed, match="terminál"):
        tool.prepare_annual_checkout(
            db, user_id=7, email="annual@example.test", config=annual_config(),
            legal_version=platby.LEGAL_VERSION, now=NOW,
            stdin=TTY(), stdout=Redirected(), stderr=TTY(),
            request=checkout_request(calls), platby_module=platby,
        )
    assert calls == []
    assert db.execute("SELECT COUNT(*) FROM checkout_attempts").fetchone()[0] == 0


def annual_payload(*, attempt_id, test_mode=True, variant="annual-variant"):
    return {
        "meta": {"event_name": "subscription_created", "custom_data": {
            "attempt_id": attempt_id,
        }},
        "data": {"type": "subscriptions", "id": "sub-annual", "attributes": {
            "test_mode": test_mode, "store_id": "test-store", "variant_id": variant,
            "currency": "EUR", "total": 3_900, "discount_id": "founder-discount",
            "subscription_id": "sub-annual", "order_id": "order-annual",
        }},
    }


def queue(db, payload, secret):
    body = json.dumps(payload, separators=(",", ":")).encode()
    signature = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    platby.odloz_webhook(db, telo=body, podpis=signature, now=NOW, dovod="payments_off")
    return body, signature


def test_annual_process_consumes_only_exact_signed_test_attempt(db):
    tool = load_tool()
    db.execute(
        "INSERT INTO checkout_attempts "
        "(public_id,user_id,product,amount_cents,renewal_amount_cents,currency,billing_interval,"
        "auto_renews,founder,discount_id,discount_code,legal_version,privacy_version,consent_json,"
        "accepted_at,expires_at,founder_reserved_until,test_mode,status) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("a" * 43, 7, "premium_annual", 3900, 4900, "EUR", "year", 1, 1,
         "founder-discount", "FOUNDER-CODE", platby.LEGAL_VERSION, platby.LEGAL_VERSION,
         "{}", NOW, NOW + 3600, NOW + 3600, 1, "pending"),
    )
    db.commit()
    own = queue(db, annual_payload(attempt_id="a" * 43), "annual-test-hook")
    other_attempt = queue(db, annual_payload(attempt_id="b" * 43), "annual-test-hook")
    live = queue(db, annual_payload(attempt_id="a" * 43, test_mode=False), "live-hook")
    probe_body = queue(db, {
        "meta": {"event_name": "subscription_created", "custom_data": {
            "lifecycle_probe_token": "x" * 48,
        }}, "data": {"attributes": {"test_mode": True}},
    }, "probe-hook")

    class Processor:
        calls = []

        @classmethod
        def process_subscription_event(cls, con, **kwargs):
            cls.calls.append(kwargs)
            return {"duplicate": False, "review_required": False,
                    "event_type": "subscription_created", "action": "activated"}

    result = tool.process_annual_queue(
        db, attempt_id="a" * 43, config=annual_config(), now=NOW + 1,
        predplatne_module=Processor,
    )

    assert result == {"processed": 1, "left_unrelated": 3, "rejected": 0}
    assert len(Processor.calls) == 1
    assert Processor.calls[0]["source"] == "odlozene"
    rows = db.execute("SELECT telo,podpis,spracovane_o FROM platobne_odlozene ORDER BY id").fetchall()
    assert bytes(rows[0][0]) == b"" and rows[0][1] is None
    assert bytes(rows[1][0]) == other_attempt[0] and rows[1][1] == other_attempt[1]
    assert bytes(rows[2][0]) == live[0] and rows[2][1] == live[1]
    assert bytes(rows[3][0]) == probe_body[0] and rows[3][1] == probe_body[1]


def test_annual_status_is_safe_and_requires_initial_39_not_fake_future_renewal(db):
    tool = load_tool()
    attempt = {
        "status": "paid", "amount_cents": 3900, "renewal_amount_cents": 4900,
        "currency": "EUR", "billing_interval": "year", "auto_renews": 1,
        "founder": 1, "test_mode": 1, "provider_checkout_id": "private-checkout",
    }
    subscription = {
        "test_mode": 1, "founder": 1, "initial_amount_cents": 3900,
        "renewal_amount_cents": 4900, "currency": "EUR",
        "provider_variant_id": "annual-variant", "initial_payment_verified": 1,
    }
    invoices = [{"invoice_kind": "initial", "amount_cents": 3900,
                 "currency": "EUR", "status": "refunded", "refunded_amount_cents": 3900}]
    events = [
        {"event_type": name, "processing_status": "processed", "needs_review": 0,
         "processed_at": NOW + index, "source": "webhook"}
        for index, name in enumerate((
            "subscription_created", "subscription_payment_success",
            "subscription_cancelled", "subscription_resumed",
            "subscription_payment_refunded",
        ))
    ]
    events.append({
        "event_type": "subscription_updated", "processing_status": "processed",
        "needs_review": 0, "processed_at": NOW + 10, "source": "reconciliation",
    })

    status = tool.annual_commercial_status(
        attempt=attempt, subscription=subscription, invoices=invoices, events=events,
        config=annual_config(), portal_verified=True, unresolved_cases=0,
        annual_domain_contract_verified=True,
    )

    assert status["complete"] is True
    assert status["initial_charge_cents"] == 3900
    assert status["renewal_display_cents"] == 4900
    assert status["reconciliation_verified"] is True
    assert "renewal_invoice" not in json.dumps(status)
    for forbidden in ("private-checkout", "annual-variant", "annual@example.test"):
        assert forbidden not in json.dumps(status)

    without_reconciliation = tool.annual_commercial_status(
        attempt=attempt, subscription=subscription, invoices=invoices,
        events=[event for event in events if event["source"] != "reconciliation"],
        config=annual_config(), portal_verified=True, unresolved_cases=0,
        annual_domain_contract_verified=True,
    )
    assert without_reconciliation["complete"] is False
    assert without_reconciliation["reconciliation_verified"] is False


def test_annual_reconcile_scopes_provider_snapshot_to_exact_test_subscription(db):
    tool = load_tool()
    db.execute(
        """INSERT INTO subscriptions
           (user_id,product,provider,provider_customer_id,provider_order_id,
            provider_subscription_id,provider_variant_id,currency,test_mode,status,
            period_start,period_end,renews_at,ends_at,paid_through,
            initial_amount_cents,renewal_amount_cents,discount_id,founder,
            initial_payment_verified,needs_review,review_reason,last_verified_event_at,
            provider_updated_at,created_at,updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            7, "premium_annual", "lemonsqueezy", "customer-annual", "order-annual",
            "sub-annual", "annual-variant", "EUR", 1, "active",
            NOW, NOW + 31_536_000, NOW + 31_536_000, None, NOW + 31_536_000,
            3_900, 4_900, "founder-discount", 1, 1, 0, None, NOW, NOW,
            NOW, NOW,
        ),
    )
    db.commit()

    exact_subscription = {"type": "subscriptions", "id": "sub-annual", "attributes": {
        "test_mode": True, "store_id": "test-store", "variant_id": "annual-variant",
        "status": "active",
    }}
    unrelated_subscription = {"type": "subscriptions", "id": "sub-other", "attributes": {
        "test_mode": True, "store_id": "test-store", "variant_id": "annual-variant",
        "status": "active",
    }}
    exact_invoice = {"type": "subscription-invoices", "id": "invoice-annual", "attributes": {
        "test_mode": True, "store_id": "test-store", "subscription_id": "sub-annual",
        "currency": "EUR", "status": "paid", "billing_reason": "initial", "total": 3_900,
    }}
    unrelated_invoice = {"type": "subscription-invoices", "id": "invoice-other", "attributes": {
        "test_mode": True, "store_id": "test-store", "subscription_id": "sub-other",
        "currency": "EUR", "status": "paid", "billing_reason": "initial", "total": 3_900,
    }}

    class Server:
        @staticmethod
        def normalize_email(value):
            return value.strip().casefold()

    class Reconciliation:
        SUBSCRIPTIONS_API_URL = "subscriptions"
        SUBSCRIPTION_INVOICES_API_URL = "invoices"
        calls = []
        applied = None

        @classmethod
        def stiahni_objednavky(cls, api_key, *, store_id, api_url):
            cls.calls.append((api_key, store_id, api_url))
            return (
                [exact_subscription, unrelated_subscription]
                if api_url == cls.SUBSCRIPTIONS_API_URL
                else [exact_invoice, unrelated_invoice]
            )

        @classmethod
        def reconcile_subscriptions(cls, con, *, provider_rows, invoice_rows, now, expected):
            cls.applied = (provider_rows, invoice_rows, expected)
            con.execute(
                """INSERT INTO subscription_events
                   (event_key,provider,test_mode,provider_subscription_id,event_type,
                    source,processing_status,needs_review,received_at,processed_at,updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    "reconcile-exact", "lemonsqueezy", 1, "sub-annual",
                    "subscription_updated", "reconciliation", "processed", 0,
                    now, now, now,
                ),
            )
            return {
                "seen_subscriptions": 1, "seen_invoices": 1,
                "subscription_drift": 0, "past_due": 0, "unpaid": 0,
                "expired": 0, "queued_webhooks": 0, "error_codes": [],
            }

    result = tool.reconcile_annual_account(
        db,
        server=Server,
        email="annual@example.test",
        config=annual_config(),
        release="release-current",
        signing_secret="marker-secret",
        now=NOW + 10,
        reconciliation_module=Reconciliation,
        marker_module=marker,
    )

    assert Reconciliation.calls == [
        ("test-api", "test-store", "subscriptions"),
        ("test-api", "test-store", "invoices"),
    ]
    assert Reconciliation.applied[0] == [exact_subscription]
    assert Reconciliation.applied[1] == [exact_invoice]
    assert Reconciliation.applied[2] == {
        "store_id": "test-store", "variant_id": "annual-variant",
        "founder_discount_id": "founder-discount", "currency": "EUR",
        "test_mode": True,
    }
    assert result == {
        "complete": True,
        "seen_subscriptions": 1,
        "seen_invoices": 1,
        "reconciliation_events": 1,
    }
    assert "sub-annual" not in json.dumps(result)
    assert "test-api" not in json.dumps(result)


def test_annual_reconcile_does_not_accept_only_a_historical_event(db):
    tool = load_tool()
    db.execute(
        """INSERT INTO subscriptions
           (user_id,product,provider,provider_customer_id,provider_order_id,
            provider_subscription_id,provider_variant_id,currency,test_mode,status,
            period_start,period_end,paid_through,initial_amount_cents,
            renewal_amount_cents,discount_id,founder,initial_payment_verified,
            needs_review,last_verified_event_at,created_at,updated_at)
           VALUES (7,'premium_annual','lemonsqueezy','customer-annual','order-annual',
                   'sub-annual','annual-variant','EUR',1,'active',?,?,?,3900,4900,
                   'founder-discount',1,1,0,?,?,?)""",
        (NOW, NOW + 31_536_000, NOW + 31_536_000, NOW, NOW, NOW),
    )
    db.execute(
        """INSERT INTO subscription_events
           (event_key,provider,test_mode,provider_subscription_id,event_type,
            source,processing_status,needs_review,received_at,processed_at,updated_at)
           VALUES ('old-reconcile','lemonsqueezy',1,'sub-annual',
                   'subscription_updated','reconciliation','processed',0,?,?,?)""",
        (NOW - 90_000, NOW - 90_000, NOW - 90_000),
    )
    db.commit()

    exact_subscription = {"type": "subscriptions", "id": "sub-annual", "attributes": {
        "test_mode": True, "store_id": "test-store", "variant_id": "annual-variant",
    }}
    exact_invoice = {"type": "subscription-invoices", "id": "invoice-annual", "attributes": {
        "test_mode": True, "store_id": "test-store", "subscription_id": "sub-annual",
        "currency": "EUR",
    }}

    class Server:
        normalize_email = staticmethod(lambda value: value.strip().casefold())

    class Reconciliation:
        SUBSCRIPTIONS_API_URL = "subscriptions"
        SUBSCRIPTION_INVOICES_API_URL = "invoices"

        @staticmethod
        def stiahni_objednavky(_api_key, *, store_id, api_url):
            return [exact_subscription] if api_url == "subscriptions" else [exact_invoice]

        @staticmethod
        def reconcile_subscriptions(_con, **_kwargs):
            return {
                "subscription_drift": 0, "past_due": 0, "unpaid": 0,
                "expired": 0, "queued_webhooks": 0, "error_codes": [],
            }

    result = tool.reconcile_annual_account(
        db, server=Server, email="annual@example.test", config=annual_config(),
        release="release-current", signing_secret="marker-secret", now=NOW + 10,
        reconciliation_module=Reconciliation, marker_module=marker,
    )

    event_key = marker.annual_reconciliation_event_key(
        signing_secret="marker-secret", release="release-current",
        config=annual_config(), provider_subscription_id="sub-annual",
    )
    row = db.execute(
        "SELECT event_type,source,processed_at FROM subscription_events WHERE event_key=?",
        (event_key,),
    ).fetchone()
    assert result["reconciliation_events"] == 1
    assert tuple(row) == ("annual_reconciliation_verified", "reconciliation_probe", NOW + 10)
