"""Annual-subscription reconciliation, dunning, health, and privacy contracts."""

from __future__ import annotations

import importlib
import inspect
import json
import sqlite3
import sys
import urllib.error
from dataclasses import replace

import pytest

from app import platby, predplatne
from test_subscription_webhooks import (
    EXPECTED,
    P0_END,
    P0_START,
    P1_END,
    P2_END,
    activate_founder,
    event,
    process,
    seed_attempt,
)


def reconciliation_module():
    sys.modules.pop("rekonciliacia", None)
    return importlib.import_module("rekonciliacia")


@pytest.fixture
def db():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.executescript(
        """
        CREATE TABLE pouzivatelia (
          id INTEGER PRIMARY KEY,
          email TEXT,
          platiaci INTEGER NOT NULL DEFAULT 0
        );
        INSERT INTO pouzivatelia (id,email) VALUES (1,'one@example.sk');
        """
    )
    platby.migrate_platby_schema(connection)
    predplatne.migrate_subscription_schema(connection)
    seed_attempt(connection)
    activate_founder(connection)
    try:
        yield connection
    finally:
        connection.close()


def provider_subscription(
    *,
    status="active",
    period_start=P0_START,
    period_end=P0_END,
    renews_at=P0_END,
    ends_at=None,
    updated_at="2026-09-12T00:03:00Z",
    **extra,
):
    attributes = {
        "store_id": EXPECTED["store_id"],
        "variant_id": EXPECTED["variant_id"],
        "currency": EXPECTED["currency"],
        "test_mode": EXPECTED["test_mode"],
        "order_id": "ord_1",
        "customer_id": "cus_1",
        "status": status,
        "billing_period_start": period_start,
        "billing_period_end": period_end,
        "renews_at": renews_at,
        "ends_at": ends_at,
        "updated_at": updated_at,
    }
    attributes.update(extra)
    return {"type": "subscriptions", "id": "sub_1", "attributes": attributes}


def provider_invoice(
    *,
    invoice_id="inv_1",
    status="paid",
    subscription_status="active",
    period_start=P0_END,
    period_end=P1_END,
    renews_at=P2_END,
    updated_at="2027-09-12T00:00:02Z",
    billing_reason="renewal",
    total=4_900,
    **extra,
):
    attributes = {
        "store_id": EXPECTED["store_id"],
        "variant_id": EXPECTED["variant_id"],
        "currency": EXPECTED["currency"],
        "test_mode": EXPECTED["test_mode"],
        "subscription_id": "sub_1",
        "order_id": "ord_1",
        "customer_id": "cus_1",
        "status": status,
        "subscription_status": subscription_status,
        "billing_reason": billing_reason,
        "billing_period_start": period_start,
        "billing_period_end": period_end,
        "renews_at": renews_at,
        "updated_at": updated_at,
        "created_at": updated_at,
        "total": total,
        "discount_id": None,
        "refunded_amount": 0,
    }
    attributes.update(extra)
    return {
        "type": "subscription-invoices",
        "id": invoice_id,
        "attributes": attributes,
    }


def has_access(con, now=P0_END - 1.0):
    return predplatne.subscription_access(
        predplatne.subscription_for_user(con, 1), now=now
    )


def test_public_reconciliation_interface_is_exact():
    reconciliation = reconciliation_module()

    assert str(inspect.signature(reconciliation.reconcile_subscriptions)) == (
        "(con, *, provider_rows, invoice_rows, now, expected) -> dict"
    )


def test_reconciliation_recovers_missed_renewal_exactly_once(db):
    reconciliation = reconciliation_module()
    process(
        db,
        event(
            "subscription_payment_failed",
            invoice_id="inv_1",
            status="failed",
            subscription_status="past_due",
            updated_at="2027-09-12T00:00:01Z",
        ),
    )
    subscriptions = [
        provider_subscription(
            period_start=P0_END,
            period_end=P1_END,
            renews_at=P2_END,
            updated_at="2027-09-12T00:00:03Z",
        )
    ]
    invoices = [provider_invoice()]

    first = reconciliation.reconcile_subscriptions(
        db,
        provider_rows=subscriptions,
        invoice_rows=invoices,
        now=P0_END + 10.0,
        expected=EXPECTED,
    )
    second = reconciliation.reconcile_subscriptions(
        db,
        provider_rows=subscriptions,
        invoice_rows=invoices,
        now=P0_END + 20.0,
        expected=EXPECTED,
    )

    assert first["recovered_invoices"] == 1
    assert second["recovered_invoices"] == 0
    assert db.execute(
        "SELECT COUNT(*) FROM subscription_invoices "
        "WHERE provider_invoice_id='inv_1'"
    ).fetchone()[0] == 1
    current = predplatne.subscription_for_user(db, 1)
    assert (current.status, current.period_start, current.period_end) == (
        "active",
        P0_END,
        P1_END,
    )


def test_reconciliation_rolls_back_the_whole_batch_on_mid_batch_failure(
    db, monkeypatch
):
    reconciliation = reconciliation_module()
    before_snapshot = predplatne.subscription_for_user(db, 1)
    before_invoices = db.execute(
        "SELECT COUNT(*) FROM subscription_invoices"
    ).fetchone()[0]
    before_events = db.execute(
        "SELECT COUNT(*) FROM subscription_events"
    ).fetchone()[0]
    real_process = predplatne.process_subscription_event
    calls = 0

    def fail_on_second_event(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise sqlite3.OperationalError("private@example.sk token=secret")
        return real_process(*args, **kwargs)

    monkeypatch.setattr(
        reconciliation.predplatne,
        "process_subscription_event",
        fail_on_second_event,
    )

    result = reconciliation.reconcile_subscriptions(
        db,
        provider_rows=[
            provider_subscription(
                period_start=P0_END,
                period_end=P1_END,
                renews_at=P2_END,
                updated_at="2027-09-12T00:00:03Z",
            )
        ],
        invoice_rows=[provider_invoice(invoice_id="inv_atomic")],
        now=P0_END + 10.0,
        expected=EXPECTED,
    )

    assert predplatne.subscription_for_user(db, 1) == before_snapshot
    assert db.execute(
        "SELECT COUNT(*) FROM subscription_invoices"
    ).fetchone()[0] == before_invoices
    assert db.execute(
        "SELECT COUNT(*) FROM subscription_events"
    ).fetchone()[0] == before_events
    assert result["subscription_drift"] == 1
    assert result["error_codes"] == ["reconciliation_batch_failed"]
    public = json.dumps(
        platby.priprav_subscription_reconciliation_alert(
            result, den="2026-09-13"
        ),
        ensure_ascii=False,
    )
    assert "private@example.sk" not in public
    assert "token=secret" not in public


def test_official_api_shape_recovers_renewal_without_invented_invoice_fields(db):
    reconciliation = reconciliation_module()
    process(
        db,
        event(
            "subscription_payment_failed",
            invoice_id="inv_official",
            status="failed",
            subscription_status="past_due",
            updated_at="2027-09-12T00:00:01Z",
        ),
    )
    subscription = provider_subscription(
        period_start=P0_END,
        period_end=P1_END,
        renews_at="2028-09-12T00:00:00Z",
        updated_at="2027-09-12T00:00:03Z",
    )
    for name in ("currency", "billing_period_start", "billing_period_end"):
        subscription["attributes"].pop(name)
    invoice = provider_invoice(
        invoice_id="inv_official",
        updated_at="2027-09-12T00:00:02Z",
    )
    for name in (
        "variant_id",
        "order_id",
        "subscription_status",
        "billing_period_start",
        "billing_period_end",
        "renews_at",
        "discount_id",
    ):
        invoice["attributes"].pop(name)

    result = reconciliation.reconcile_subscriptions(
        db,
        provider_rows=[subscription],
        invoice_rows=[invoice],
        now=P0_END + 10.0,
        expected=EXPECTED,
    )

    current = predplatne.subscription_for_user(db, 1)
    assert result["recovered_invoices"] == 1
    assert (current.status, current.period_start, current.period_end) == (
        "active",
        P0_END,
        P1_END,
    )


def test_official_pending_invoice_and_past_due_snapshot_keep_access(db):
    reconciliation = reconciliation_module()
    subscription = provider_subscription(
        status="past_due",
        updated_at="2026-09-12T00:04:00Z",
    )
    subscription["attributes"].pop("currency")
    invoice = provider_invoice(
        invoice_id="inv_pending",
        status="pending",
        period_start=P0_START,
        period_end=P0_END,
        renews_at=P0_END,
        updated_at="2026-09-12T00:03:30Z",
    )
    for name in (
        "variant_id",
        "order_id",
        "subscription_status",
        "billing_period_start",
        "billing_period_end",
        "renews_at",
        "discount_id",
    ):
        invoice["attributes"].pop(name)

    result = reconciliation.reconcile_subscriptions(
        db,
        provider_rows=[subscription],
        invoice_rows=[invoice],
        now=P0_START + 500.0,
        expected=EXPECTED,
    )

    assert result["past_due"] == 1
    assert result["error_codes"] == []
    assert has_access(db) is True


def test_provider_unavailability_preserves_last_verified_access(db):
    reconciliation = reconciliation_module()
    before = predplatne.subscription_for_user(db, 1)

    with pytest.raises(reconciliation.ProviderUnavailable):
        reconciliation.reconcile_subscriptions(
            db,
            provider_rows=None,
            invoice_rows=[],
            now=P0_START + 500.0,
            expected=EXPECTED,
        )

    after = predplatne.subscription_for_user(db, 1)
    assert after == before
    assert has_access(db) is True


def test_api_fetch_is_all_or_nothing_before_any_subscription_mutation(db):
    reconciliation = reconciliation_module()
    before = predplatne.subscription_for_user(db, 1)
    calls = []

    def fetch(_api_key, *, store_id, api_url):
        calls.append((store_id, api_url))
        if api_url == reconciliation.SUBSCRIPTION_INVOICES_API_URL:
            raise urllib.error.URLError("offline")
        return [
            provider_subscription(
                status="unpaid", renews_at=None,
                updated_at="2026-09-12T00:04:00Z",
            )
        ]

    with pytest.raises(reconciliation.ProviderUnavailable) as error:
        reconciliation.reconcile_subscriptions_from_api(
            db,
            api_key="not-a-live-key",
            store_id=EXPECTED["store_id"],
            now=P0_START + 500.0,
            expected=EXPECTED,
            fetch=fetch,
        )

    assert str(error.value) == "provider_unavailable"
    assert [url for _, url in calls] == [
        reconciliation.SUBSCRIPTIONS_API_URL,
        reconciliation.SUBSCRIPTION_INVOICES_API_URL,
    ]
    assert predplatne.subscription_for_user(db, 1) == before
    assert has_access(db) is True


def test_confirmed_unpaid_suspends_and_confirmed_expired_removes_access(db):
    reconciliation = reconciliation_module()

    unpaid = reconciliation.reconcile_subscriptions(
        db,
        provider_rows=[
            provider_subscription(
                status="unpaid", renews_at=None,
                updated_at="2026-09-12T00:04:00Z",
            )
        ],
        invoice_rows=[],
        now=P0_START + 500.0,
        expected=EXPECTED,
    )
    assert has_access(db) is False
    assert unpaid["unpaid"] == 1

    expired = reconciliation.reconcile_subscriptions(
        db,
        provider_rows=[
            provider_subscription(
                status="expired", renews_at=None,
                updated_at="2026-09-12T00:05:00Z",
            )
        ],
        invoice_rows=[],
        now=P0_END + 1.0,
        expected=EXPECTED,
    )
    assert predplatne.subscription_for_user(db, 1).status == "expired"
    assert expired["expired"] == 1
    assert has_access(db, now=P0_END + 1.0) is False


def test_confirmed_recovered_payment_restores_access_without_duplicate_invoice(db):
    reconciliation = reconciliation_module()
    process(
        db,
        event(
            "subscription_updated",
            status="unpaid",
            updated_at="2026-09-12T00:04:00Z",
        ),
    )
    assert has_access(db) is False

    result = reconciliation.reconcile_subscriptions(
        db,
        provider_rows=[
            provider_subscription(
                period_start=P0_END,
                period_end=P1_END,
                renews_at=P2_END,
                updated_at="2027-09-12T00:06:00Z",
            )
        ],
        invoice_rows=[
            provider_invoice(
                invoice_id="inv_recovery",
                period_start=P0_END,
                period_end=P1_END,
                renews_at=P2_END,
                updated_at="2027-09-12T00:05:00Z",
            )
        ],
        now=P0_END + 600.0,
        expected=EXPECTED,
    )

    assert result["recovered_payments"] == 1
    assert has_access(db, now=P0_END + 600.0) is True
    assert db.execute(
        "SELECT COUNT(*) FROM subscription_invoices"
    ).fetchone()[0] == 2


def test_stale_provider_snapshot_cannot_degrade_newer_verified_state(db):
    reconciliation = reconciliation_module()
    before = predplatne.subscription_for_user(db, 1)

    result = reconciliation.reconcile_subscriptions(
        db,
        provider_rows=[
            provider_subscription(
                status="unpaid",
                renews_at=None,
                updated_at="2026-09-12T00:00:20Z",
            )
        ],
        invoice_rows=[],
        now=P0_START + 700.0,
        expected=EXPECTED,
    )

    after = predplatne.subscription_for_user(db, 1)
    assert result["subscription_drift"] == 1
    assert after.status == before.status == "active"
    assert after.provider_updated_at == before.provider_updated_at
    assert has_access(db) is True


def test_invalid_provider_row_is_reported_as_drift_not_healthy(db):
    reconciliation = reconciliation_module()

    result = reconciliation.reconcile_subscriptions(
        db,
        provider_rows=[{"type": "subscriptions", "id": "sub_1"}],
        invoice_rows=[],
        now=P0_START + 700.0,
        expected=EXPECTED,
    )

    assert result["subscription_drift"] == 1
    assert result["error_codes"] == ["invalid_subscription_row"]
    assert has_access(db) is True


def test_invoice_row_with_conflicting_resource_type_is_quarantined(db):
    reconciliation = reconciliation_module()
    before = db.execute(
        "SELECT COUNT(*) FROM subscription_invoices"
    ).fetchone()[0]
    row = provider_invoice(invoice_id="sub_1")
    row["type"] = "subscriptions"

    result = reconciliation.reconcile_subscriptions(
        db,
        provider_rows=[provider_subscription()],
        invoice_rows=[row],
        now=P0_END + 10.0,
        expected=EXPECTED,
    )

    assert db.execute(
        "SELECT COUNT(*) FROM subscription_invoices"
    ).fetchone()[0] == before
    assert result["subscription_drift"] == 1
    assert result["error_codes"] == ["invalid_invoice_row"]


def test_subscription_row_with_conflicting_resource_type_cannot_suspend(db):
    reconciliation = reconciliation_module()
    row = provider_subscription(
        status="unpaid",
        renews_at=None,
        updated_at="2026-09-12T00:04:00Z",
    )
    row["type"] = "subscription-invoices"
    row["attributes"]["subscription_id"] = "sub_1"

    result = reconciliation.reconcile_subscriptions(
        db,
        provider_rows=[row],
        invoice_rows=[],
        now=P0_START + 500.0,
        expected=EXPECTED,
    )

    assert predplatne.subscription_for_user(db, 1).status == "active"
    assert result["subscription_drift"] == 1
    assert result["error_codes"] == ["invalid_subscription_row"]


def test_dunning_past_due_keeps_access_and_health_reports_aggregate_counts(db):
    reconciliation = reconciliation_module()

    result = reconciliation.reconcile_subscriptions(
        db,
        provider_rows=[
            provider_subscription(
                status="past_due",
                updated_at="2026-09-12T00:04:00Z",
            )
        ],
        invoice_rows=[
            provider_invoice(
                invoice_id="inv_due",
                status="failed",
                subscription_status="past_due",
                period_start=P0_START,
                period_end=P0_END,
                renews_at=P0_END,
                updated_at="2026-09-12T00:03:30Z",
            )
        ],
        now=P0_START + 500.0,
        expected=EXPECTED,
    )

    assert has_access(db) is True
    assert result["past_due"] == 1
    health = platby.stav_dozoru(db)
    test_health = predplatne.subscription_health_counters(db, test_mode=True)
    assert set(("subscription_drift", "past_due", "unpaid", "expired", "queued_webhooks")) <= set(health)
    assert health["past_due"] == 0
    assert test_health["past_due"] == 1


def test_subscription_health_separates_live_and_test_mode(db):
    db.execute(
        "INSERT INTO pouzivatelia (id,email) VALUES (2,'test@example.sk')"
    )
    test_snapshot = predplatne.subscription_for_user(db, 1)
    live_snapshot = replace(
        test_snapshot,
        user_id=2,
        provider_customer_id="cus_live",
        provider_order_id="ord_live",
        provider_subscription_id="sub_live",
        test_mode=False,
        status="unpaid",
        renews_at=None,
        provider_updated_at=test_snapshot.provider_updated_at + 10.0,
    )
    assert predplatne.upsert_snapshot(
        db, live_snapshot, now=P0_START + 500.0
    ) is True
    db.execute(
        """INSERT INTO subscription_events
           (event_key,provider,test_mode,provider_subscription_id,event_type,
            source,payload_json,processing_status,needs_review,review_reason,
            received_at,processed_at,updated_at)
           VALUES ('live-queued','lemonsqueezy',0,'sub_live',
                   'subscription_updated','webhook','{}','requires_review',
                   1,'safe',?,NULL,?)""",
        (P0_START + 510.0, P0_START + 510.0),
    )

    live = predplatne.subscription_health_counters(db, test_mode=False)
    test = predplatne.subscription_health_counters(db, test_mode=True)
    result = reconciliation_module().reconcile_subscriptions(
        db,
        provider_rows=[],
        invoice_rows=[],
        now=P0_START + 520.0,
        expected=EXPECTED,
    )

    assert live["unpaid"] == 1
    assert live["queued_webhooks"] == 1
    assert test == {
        "subscription_drift": 0,
        "past_due": 0,
        "unpaid": 0,
        "expired": 0,
        "queued_webhooks": 0,
    }
    assert result["unpaid"] == 0
    assert result["queued_webhooks"] == 0


def test_quarantined_and_raw_webhooks_are_visible_only_as_aggregate(db):
    process(
        db,
        event(
            "subscription_updated",
            status="new-provider-state",
            updated_at="2026-09-12T00:04:00Z",
        ),
    )
    platby.odloz_webhook(
        db,
        telo=b'{"email":"private@example.sk","token":"secret"}',
        podpis="a" * 64,
        now=P0_START + 500.0,
    )

    health = platby.stav_dozoru(db)
    test_health = predplatne.subscription_health_counters(db, test_mode=True)
    encoded = json.dumps(health, ensure_ascii=False)

    assert health["queued_webhooks"] == 0
    assert health["cakajucich_tiel"] == 1
    assert test_health["queued_webhooks"] == 1
    for forbidden in ("private@example.sk", "secret", "sub_1", "cus_1", "ord_1"):
        assert forbidden not in encoded


def test_missing_subscription_tables_are_an_explicit_health_blocker():
    connection = sqlite3.connect(":memory:")
    connection.executescript(
        """
        CREATE TABLE pouzivatelia (
          id INTEGER PRIMARY KEY,
          email TEXT,
          platiaci INTEGER NOT NULL DEFAULT 0
        );
        """
    )
    platby.migrate_platby_schema(connection)
    try:
        health = platby.stav_dozoru(connection)
    finally:
        connection.close()

    assert health["subscription_health_available"] is False
    assert health["subscription_drift"] == 1
    assert health["subscription_health_error_codes"] == [
        "subscription_health_schema_unavailable"
    ]


def test_unexpected_subscription_health_db_error_is_safe_and_not_healthy(
    db, monkeypatch
):
    def fail_health(*args, **kwargs):
        raise sqlite3.OperationalError("private@example.sk token=secret")

    monkeypatch.setattr(predplatne, "subscription_health_counters", fail_health)

    health = platby.stav_dozoru(db)
    encoded = json.dumps(health, ensure_ascii=False)

    assert health["subscription_health_available"] is False
    assert health["subscription_drift"] == 1
    assert health["subscription_health_error_codes"] == [
        "subscription_health_db_error"
    ]
    assert "private@example.sk" not in encoded
    assert "token=secret" not in encoded


def test_reconciliation_result_event_storage_and_alerts_never_expose_provider_payload(db):
    reconciliation = reconciliation_module()
    row = provider_subscription(
        status="unpaid",
        renews_at=None,
        updated_at="2026-09-12T00:04:00Z",
        user_email="private@example.sk",
        signed_url="https://provider.example/private-token",
        api_token="provider-secret",
    )

    result = reconciliation.reconcile_subscriptions(
        db,
        provider_rows=[row],
        invoice_rows=[],
        now=P0_START + 500.0,
        expected=EXPECTED,
    )
    stored = " ".join(
        item[0] or "" for item in db.execute(
            "SELECT payload_json FROM subscription_events"
        ).fetchall()
    )
    alert = platby.priprav_subscription_reconciliation_alert(result, den="2026-09-12")
    public = json.dumps({"result": result, "alert": alert}, ensure_ascii=False)

    for forbidden in (
        "private@example.sk",
        "private-token",
        "provider-secret",
        "Bearer",
    ):
        assert forbidden not in stored
        assert forbidden not in public
    assert set(result) == {
        "seen_subscriptions",
        "seen_invoices",
        "recovered_invoices",
        "recovered_payments",
        "applied_transitions",
        "duplicates",
        "subscription_drift",
        "past_due",
        "unpaid",
        "expired",
        "queued_webhooks",
        "error_codes",
    }
    assert set(result["error_codes"]) <= {
        "invoice_requires_review",
        "subscription_requires_review",
        "invalid_invoice_row",
        "invalid_subscription_row",
    }


def test_public_alert_rejects_arbitrary_day_text_instead_of_echoing_it(db):
    alert = platby.priprav_subscription_reconciliation_alert(
        {
            "subscription_drift": 1,
            "past_due": 0,
            "unpaid": 0,
            "expired": 0,
            "queued_webhooks": 0,
            "error_codes": ["subscription_requires_review"],
        },
        den="private@example.sk token=secret",
    )

    encoded = json.dumps(alert, ensure_ascii=False)
    assert "private@example.sk" not in encoded
    assert "token=secret" not in encoded


def test_reconciliation_is_local_and_does_not_require_payment_rollout_flags(db, monkeypatch):
    reconciliation = reconciliation_module()
    monkeypatch.delenv("PLATBY_ZAPNUTE", raising=False)
    monkeypatch.delenv("UVARSI_SUBSCRIPTION_CHECKOUTS", raising=False)

    result = reconciliation.reconcile_subscriptions(
        db,
        provider_rows=[],
        invoice_rows=[],
        now=P0_START + 500.0,
        expected=EXPECTED,
    )

    assert result["seen_subscriptions"] == 0
    assert result["seen_invoices"] == 0
    assert has_access(db) is True
