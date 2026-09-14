"""Isolated Test-mode daily lifecycle probe contracts.

Synthetic payloads in this module exercise local validation only.  They are
never release evidence and never call Lemon Squeezy.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3

import pytest

from app import platby
from app import predplatne
from app import subscription_lifecycle_probe as probe


SECRET = "probe-marker-secret"
TOKEN = "probe-token-" + "a" * 48
NOW = 1_789_200_000.0


def config(**changes):
    values = {
        "api_key": "test-api-key",
        "store_id": "test-store",
        "variant_id": "daily-probe-variant",
        "webhook_secret": "dedicated-probe-webhook-secret",
        "price_cents": 100,
    }
    values.update(changes)
    return probe.LifecycleProbeConfig(**values)


@pytest.fixture
def db():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    platby.migrate_platby_schema(connection)
    predplatne.migrate_subscription_schema(connection)
    probe.migrate_probe_schema(connection)
    yield connection
    connection.close()


def payload(
    event_type,
    *,
    token=TOKEN,
    test_mode=True,
    store_id="test-store",
    variant_id="daily-probe-variant",
    total=100,
    subscription_id="sub-probe-1",
    order_id="order-probe-1",
    invoice_id="invoice-probe-1",
    billing_reason=None,
    period_start="2026-09-13T00:00:00Z",
    period_end="2026-09-14T00:00:00Z",
    refunded_amount=0,
):
    custom = {"lifecycle_probe_token": token}
    attributes = {
        "test_mode": test_mode,
        "store_id": store_id,
        "variant_id": variant_id,
        "currency": "EUR",
        "total": total,
        "order_id": order_id,
        "subscription_id": subscription_id,
        "invoice_id": invoice_id,
        "billing_reason": billing_reason,
        "billing_period_start": period_start,
        "billing_period_end": period_end,
        "updated_at": "2026-09-13T12:00:00Z",
        "refunded_amount": refunded_amount,
        "status": "paid",
    }
    data_type = (
        "subscription-invoices"
        if event_type.startswith("subscription_payment_")
        else "subscriptions"
    )
    data_id = invoice_id if data_type == "subscription-invoices" else subscription_id
    return {
        "meta": {"event_name": event_type, "custom_data": custom},
        "data": {"type": data_type, "id": data_id, "attributes": attributes},
    }


def signed_body(event, *, secret="dedicated-probe-webhook-secret"):
    body = json.dumps(event, sort_keys=True, separators=(",", ":")).encode()
    signature = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return body, signature


def signed_probe_marker(*, config_override=None):
    active_config = config_override or config()
    unsigned = {
        "schema_version": 5,
        "attestation_id": "c" * 64,
        "probe_config_fingerprint": probe.probe_config_fingerprint(
            active_config, signing_secret=SECRET
        ),
        "test_mode_daily_probe": {
            "lifecycle": {"evidence_source": "test_mode_daily_probe"}
        },
    }
    body = json.dumps(
        unsigned, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode()
    return {
        **unsigned,
        "signature": hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest(),
    }


def create_run(db):
    run = probe.create_probe_run(
        db,
        config=config(),
        signing_secret=SECRET,
        now=NOW,
        token=TOKEN,
    )
    assert run.token == TOKEN
    return run


def ingest(db, event, *, signature_secret="dedicated-probe-webhook-secret"):
    body, signature = signed_body(event, secret=signature_secret)
    return probe.ingest_signed_probe_event(
        db,
        body=body,
        signature=signature,
        config=config(),
        signing_secret=SECRET,
        now=NOW + 10,
    )


def queue_ingest(db, event):
    body, signature = signed_body(event)
    queued = platby.odloz_webhook(
        db, telo=body, podpis=signature, now=NOW, dovod="payments_off"
    )
    assert queued["ulozene"] is True
    result = probe.process_queued_probe_events(
        db, config=config(), signing_secret=SECRET, now=NOW + 10,
    )
    assert result["processed"] == 1


def record_renewal(db, *, queued=False):
    event = payload(
            "subscription_payment_success",
            billing_reason="renewal",
            invoice_id="invoice-renewal-1",
            period_start="2026-09-14T00:00:00Z",
            period_end="2026-09-15T00:00:00Z",
    )
    (queue_ingest if queued else ingest)(db, event)
    probe.record_genuine_daily_renewal(
        db,
        token=TOKEN,
        config=config(),
        signing_secret=SECRET,
        provider_subscription_id="sub-probe-1",
        provider_invoice_id="invoice-renewal-1",
        amount_cents=100,
        currency="EUR",
        interval="day",
        interval_count=1,
        period_start="2026-09-14T00:00:00Z",
        period_end="2026-09-15T00:00:00Z",
        now=NOW + 20,
    )


def complete_run(db, *, queued=True):
    create_run(db)
    probe.record_probe_provider_verified(
        db,
        token=TOKEN,
        config=config(),
        signing_secret=SECRET,
        test_mode=True,
        store_id="test-store",
        variant_id="daily-probe-variant",
        price_cents=100,
        currency="EUR",
        interval="day",
        interval_count=1,
        trial_days=0,
        discount_applied_cents=0,
        variant_status="published",
        now=NOW + 1,
    )
    send = queue_ingest if queued else ingest
    send(db, payload("subscription_created"))
    send(db, payload("subscription_payment_success", billing_reason="initial"))
    record_renewal(db, queued=queued)
    send(db, payload("subscription_payment_failed", invoice_id="inv-failed"))
    send(db, payload("subscription_payment_recovered", invoice_id="inv-recovered"))
    send(db, payload("subscription_cancelled", invoice_id="inv-cancel"))
    send(db, payload("subscription_resumed", invoice_id="inv-resume"))
    send(db, payload("subscription_expired", invoice_id="inv-expired"))
    send(
        db,
        payload(
            "subscription_payment_refunded",
            invoice_id="inv-refund",
            refunded_amount=100,
        ),
    )


def test_reader_reuses_test_store_and_api_but_requires_isolated_identity():
    values = {
        "LEMON_TEST_API_KEY": "test-api-key",
        "LEMON_TEST_STORE_ID": "test-store",
        "LEMON_TEST_LIFECYCLE_PROBE_VARIANT_ID": "daily-probe-variant",
        "LEMON_TEST_LIFECYCLE_PROBE_WEBHOOK_SECRET": "probe-hook",
        "LEMON_TEST_LIFECYCLE_PROBE_PRICE_CENTS": "100",
    }

    result = probe.read_probe_config(
        values.get,
        annual_test_variant_id="annual-test-variant",
        live_webhook_secret="live-hook",
        annual_test_webhook_secret="annual-test-hook",
    )

    assert result.store_id == "test-store"
    assert result.api_key == "test-api-key"
    assert result.variant_id == "daily-probe-variant"
    assert result.price_cents == 100
    assert "test-api-key" not in repr(result)
    assert "probe-hook" not in repr(result)


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("LEMON_TEST_LIFECYCLE_PROBE_VARIANT_ID", "annual-test-variant"),
        ("LEMON_TEST_LIFECYCLE_PROBE_WEBHOOK_SECRET", "live-hook"),
        ("LEMON_TEST_LIFECYCLE_PROBE_WEBHOOK_SECRET", "annual-test-hook"),
        ("LEMON_TEST_LIFECYCLE_PROBE_PRICE_CENTS", "1.00"),
        ("LEMON_TEST_LIFECYCLE_PROBE_PRICE_CENTS", ""),
        ("LEMON_TEST_LIFECYCLE_PROBE_PRICE_CENTS", "0"),
    ],
)
def test_reader_rejects_collisions_and_non_strict_price(key, value):
    values = {
        "LEMON_TEST_API_KEY": "test-api-key",
        "LEMON_TEST_STORE_ID": "test-store",
        "LEMON_TEST_LIFECYCLE_PROBE_VARIANT_ID": "daily-probe-variant",
        "LEMON_TEST_LIFECYCLE_PROBE_WEBHOOK_SECRET": "probe-hook",
        "LEMON_TEST_LIFECYCLE_PROBE_PRICE_CENTS": "100",
    }
    values[key] = value

    with pytest.raises(probe.ProbeConfigError):
        probe.read_probe_config(
            values.get,
            annual_test_variant_id="annual-test-variant",
            live_webhook_secret="live-hook",
            annual_test_webhook_secret="annual-test-hook",
        )


def test_schema_and_run_are_isolated_and_store_no_raw_token_or_secrets(db):
    create_run(db)

    assert db.execute("SELECT COUNT(*) FROM naroky").fetchone()[0] == 0
    row = dict(db.execute("SELECT * FROM subscription_lifecycle_probe_runs").fetchone())
    serialized = json.dumps(row, sort_keys=True)
    assert TOKEN not in serialized
    assert config().api_key not in serialized
    assert config().webhook_secret not in serialized
    assert row["token_digest"] == hashlib.sha256(TOKEN.encode()).hexdigest()


def test_probe_migration_is_additive_idempotent_and_rollback_safe(db):
    db.execute(
        "INSERT INTO naroky(user_id,produkt,poskytovatel,objednavka_id,stav,ziskany_o,zmeneny_o) "
        "VALUES (7,'premium_annual','lemonsqueezy','live-order','aktivny',1,1)"
    )
    db.commit()

    probe.migrate_probe_schema(db)
    probe.migrate_probe_schema(db)
    db.rollback()

    assert db.execute("SELECT COUNT(*) FROM naroky").fetchone()[0] == 1
    assert db.execute(
        "SELECT COUNT(*) FROM subscription_lifecycle_probe_runs"
    ).fetchone()[0] == 0


def test_valid_signed_events_are_idempotent_and_never_grant_entitlement(db):
    create_run(db)
    event = payload("subscription_created")

    first = ingest(db, event)
    second = ingest(db, event)

    assert first == {"accepted": True, "duplicate": False,
                     "event_type": "subscription_created"}
    assert second == {"accepted": True, "duplicate": True,
                      "event_type": "subscription_created"}
    assert db.execute(
        "SELECT COUNT(*) FROM subscription_lifecycle_probe_events"
    ).fetchone()[0] == 1
    assert db.execute("SELECT COUNT(*) FROM naroky").fetchone()[0] == 0
    assert db.execute(
        "SELECT COUNT(*) FROM subscriptions"
    ).fetchone()[0] == 0


@pytest.mark.parametrize(
    "change",
    [
        {"signature_secret": "wrong-secret"},
        {"test_mode": False},
        {"store_id": "live-store"},
        {"variant_id": "annual-test-variant"},
        {"token": "other-token-" + "b" * 48},
        {"total": 101},
    ],
)
def test_invalid_signature_mode_identity_token_or_amount_is_rejected(db, change):
    create_run(db)
    signature_secret = change.pop("signature_secret", "dedicated-probe-webhook-secret")
    event = payload("subscription_payment_success", billing_reason="initial", **change)

    with pytest.raises(probe.ProbeEventRejected):
        ingest(db, event, signature_secret=signature_secret)

    assert db.execute(
        "SELECT COUNT(*) FROM subscription_lifecycle_probe_events"
    ).fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM naroky").fetchone()[0] == 0


def test_future_payment_events_require_initial_and_verified_genuine_renewal(db):
    create_run(db)
    ingest(db, payload("subscription_created"))
    ingest(db, payload("subscription_payment_success", billing_reason="initial"))

    with pytest.raises(probe.ProbeEventRejected, match="obnov"):
        ingest(db, payload("subscription_payment_failed", invoice_id="inv-failed"))

    record_renewal(db)
    assert ingest(
        db, payload("subscription_payment_failed", invoice_id="inv-failed")
    )["accepted"] is True
    assert ingest(
        db, payload("subscription_payment_recovered", invoice_id="inv-recovered")
    )["accepted"] is True


def test_event_order_rejects_recovery_before_failure(db):
    create_run(db)
    ingest(db, payload("subscription_created"))
    ingest(db, payload("subscription_payment_success", billing_reason="initial"))
    record_renewal(db)

    with pytest.raises(probe.ProbeEventRejected, match="zlyhan"):
        ingest(db, payload("subscription_payment_recovered", invoice_id="inv-rec"))


def test_status_is_aggregate_safe_and_contains_no_provider_identity(db):
    create_run(db)
    ingest(db, payload("subscription_created"))

    status = probe.probe_status(db, token=TOKEN)
    rendered = json.dumps(status, sort_keys=True)

    assert status["state"] == "collecting"
    assert status["event_count"] == 1
    for forbidden in (
        TOKEN, "test-store", "daily-probe-variant", "sub-probe-1",
        "order-probe-1", "invoice-probe-1", "test-api-key",
        "dedicated-probe-webhook-secret",
    ):
        assert forbidden not in rendered


def test_successful_ingest_retains_no_raw_body_email_url_or_secret(db):
    create_run(db)
    event = payload("subscription_created")
    event["data"]["attributes"]["user_email"] = "probe@example.test"
    event["data"]["attributes"]["checkout_url"] = "https://example.test/secret"
    body, signature = signed_body(event)

    probe.ingest_signed_probe_event(
        db, body=body, signature=signature, config=config(),
        signing_secret=SECRET, now=NOW + 10,
    )

    rows = [dict(row) for row in db.execute(
        "SELECT * FROM subscription_lifecycle_probe_events"
    ).fetchall()]
    rendered = json.dumps(rows, sort_keys=True)
    for forbidden in (
        body.decode(), "probe@example.test", "https://example.test/secret",
        "dedicated-probe-webhook-secret", TOKEN,
    ):
        assert forbidden not in rendered


def test_queue_processor_consumes_only_exact_signed_probe_rows(db):
    create_run(db)
    probe_body, probe_signature = signed_body(payload("subscription_created"))
    other_body, other_signature = signed_body(
        payload("subscription_created", variant_id="annual-test-variant"),
        secret="annual-test-secret",
    )
    wrong_probe_body, wrong_probe_signature = signed_body(
        payload("subscription_created", variant_id="wrong-probe-variant")
    )
    platby.odloz_webhook(
        db, telo=probe_body, podpis=probe_signature, now=NOW,
        dovod="payments_off",
    )
    platby.odloz_webhook(
        db, telo=other_body, podpis=other_signature, now=NOW,
        dovod="payments_off",
    )
    platby.odloz_webhook(
        db, telo=wrong_probe_body, podpis=wrong_probe_signature, now=NOW,
        dovod="payments_off",
    )

    result = probe.process_queued_probe_events(
        db, config=config(), signing_secret=SECRET, now=NOW + 10,
    )

    assert result == {"processed": 1, "left_unrelated": 1, "rejected": 1}
    rows = db.execute(
        "SELECT telo,podpis,spracovane_o FROM platobne_odlozene ORDER BY id"
    ).fetchall()
    assert bytes(rows[0][0]) == b""
    assert rows[0][1] is None
    assert rows[0][2] == NOW + 10
    assert bytes(rows[1][0]) == other_body
    assert rows[1][1] == other_signature
    assert rows[1][2] is None
    assert bytes(rows[2][0]) == wrong_probe_body
    assert rows[2][1] == wrong_probe_signature
    assert rows[2][2] is None
    assert db.execute("SELECT COUNT(*) FROM naroky").fetchone()[0] == 0


def test_complete_probe_exports_only_truthful_safe_marker_contract(db):
    complete_run(db)

    facts = probe.probe_marker_facts(
        db, token=TOKEN, config=config(), signing_secret=SECRET,
    )
    digest_facts = probe.probe_marker_facts_by_digest(
        db,
        token_digest=hashlib.sha256(TOKEN.encode()).hexdigest(),
        config=config(),
        signing_secret=SECRET,
    )
    rendered = json.dumps(facts, sort_keys=True)

    assert digest_facts == facts
    assert facts["provider"]["price_cents"] == 100
    assert facts["provider"]["billing_interval"] == "day"
    assert facts["lifecycle"]["evidence_source"] == "test_mode_daily_probe"
    for forbidden in (
        TOKEN, "test-store", "daily-probe-variant", "sub-probe-1",
        "order-probe-1", "invoice-probe-1", "test-api-key",
        "dedicated-probe-webhook-secret", "@",
    ):
        assert forbidden not in rendered


def test_direct_synthetic_events_cannot_become_release_evidence(db):
    complete_run(db, queued=False)

    status = probe.probe_status(db, token=TOKEN)

    assert status["signed_queue_only"] is False
    assert status["complete"] is False
    with pytest.raises(probe.ProbeEventRejected, match="úplný"):
        probe.probe_marker_facts(
            db, token=TOKEN, config=config(), signing_secret=SECRET,
        )


def test_exact_run_cleanup_requires_evidence_and_never_touches_customer_tables(db):
    complete_run(db)
    db.execute(
        "INSERT INTO naroky(user_id,produkt,poskytovatel,objednavka_id,stav,ziskany_o,zmeneny_o) "
        "VALUES (1,'premium_annual','lemonsqueezy','live-order','aktivny',1,1)"
    )
    phrase = probe.cleanup_confirmation(TOKEN)
    with pytest.raises(probe.ProbeCleanupRejected):
        probe.cleanup_probe_run(db, token=TOKEN, confirmation=phrase)

    probe.mark_probe_evidenced(
        db,
        token=TOKEN,
        config=config(),
        signing_secret=SECRET,
        signed_marker=signed_probe_marker(),
        now=NOW + 30,
    )
    with pytest.raises(probe.ProbeCleanupRejected):
        probe.cleanup_probe_run(db, token=TOKEN, confirmation="DELETE")

    result = probe.cleanup_probe_run(db, token=TOKEN, confirmation=phrase)

    assert result == {
        "deleted_runs": 1, "deleted_events": 9, "deleted_queue_rows": 9,
    }
    assert db.execute("SELECT COUNT(*) FROM naroky").fetchone()[0] == 1
    assert db.execute(
        "SELECT COUNT(*) FROM platobne_odlozene"
    ).fetchone()[0] == 0
