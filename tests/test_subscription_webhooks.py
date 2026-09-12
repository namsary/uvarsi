"""Signed annual-subscription webhook lifecycle and idempotency contracts."""

from __future__ import annotations

import hashlib
import hmac
import importlib
import inspect
import json
import sqlite3
import sys
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from typing import get_type_hints

import pytest
from fastapi.testclient import TestClient

from app import platby, predplatne


ROOT = Path(__file__).resolve().parents[1]
WEBHOOK_SECRET = "task-3-webhook-secret"
ATTEMPT_ID = "attempt_" + "a" * 43
OTHER_ATTEMPT_ID = "attempt_" + "b" * 43
TEST_WEBHOOK_SECRET = "task-3-test-webhook-secret"
STORE_ID = "store_1"
VARIANT_ID = "variant_annual"
FOUNDER_DISCOUNT_ID = "discount_founders"
P0_START = 1_789_171_200.0
P0_END = 1_820_707_200.0
P1_END = 1_852_329_600.0
P2_END = 1_883_865_600.0
NOW = P0_START + 100.0
EXPECTED = {
    "store_id": STORE_ID,
    "variant_id": VARIANT_ID,
    "founder_discount_id": FOUNDER_DISCOUNT_ID,
    "currency": "EUR",
    "test_mode": True,
}


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
        INSERT INTO pouzivatelia (id,email) VALUES (2,'two@example.sk');
        """
    )
    platby.migrate_platby_schema(connection)
    predplatne.migrate_subscription_schema(connection)
    seed_attempt(connection)
    try:
        yield connection
    finally:
        connection.close()


def seed_attempt(
    con,
    *,
    public_id=ATTEMPT_ID,
    user_id=1,
    founder=True,
    test_mode=True,
    status="pending",
    provider_order_id=None,
    provider_checkout_id="checkout_1",
):
    amount = 3_900 if founder else 4_900
    discount_id = FOUNDER_DISCOUNT_ID if founder else None
    discount_code = "FOUNDERS" if founder else None
    con.execute(
        """INSERT INTO checkout_attempts
           (public_id,user_id,product,amount_cents,renewal_amount_cents,
            currency,billing_interval,auto_renews,founder,discount_id,
            discount_code,legal_version,privacy_version,consent_json,
            accepted_at,expires_at,founder_reserved_until,test_mode,status,
            provider_checkout_id,provider_order_id)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            public_id,
            user_id,
            "premium_annual",
            amount,
            4_900,
            "EUR",
            "year",
            1,
            int(founder),
            discount_id,
            discount_code,
            platby.LEGAL_VERSION,
            platby.LEGAL_VERSION,
            json.dumps(
                {
                    "accept_terms": True,
                    "accept_automatic_renewal": True,
                    "request_immediate_activation": True,
                    "acknowledge_withdrawal_proration": True,
                    "legal_version": platby.LEGAL_VERSION,
                },
                sort_keys=True,
            ),
            NOW - 10.0,
            P0_END,
            P0_END if founder else None,
            int(test_mode),
            status,
            provider_checkout_id,
            provider_order_id,
        ),
    )
    con.commit()


def _iso(timestamp):
    values = {
        P0_START: "2026-09-12T00:00:00Z",
        P0_END: "2027-09-12T00:00:00Z",
        P1_END: "2028-09-12T00:00:00Z",
        P2_END: "2029-09-12T00:00:00Z",
    }
    return values[timestamp]


def event(
    event_name,
    *,
    subscription_id="sub_1",
    order_id="ord_1",
    invoice_id="inv_0",
    attempt_id=ATTEMPT_ID,
    total=3_900,
    status=None,
    subscription_status=None,
    period_start=P0_START,
    period_end=P0_END,
    discount_id=FOUNDER_DISCOUNT_ID,
    refunded_amount=0,
    store_id=STORE_ID,
    variant_id=VARIANT_ID,
    currency="EUR",
    test_mode=True,
    user_id=None,
    billing_reason=None,
    customer_id="cus_1",
    updated_at="2026-09-12T00:01:40Z",
):
    custom = {"attempt_id": attempt_id} if attempt_id is not None else {}
    if user_id is not None:
        custom["user_id"] = str(user_id)
    meta = {"event_name": event_name, "custom_data": custom}
    common = {
        "store_id": store_id,
        "variant_id": variant_id,
        "currency": currency,
        "test_mode": test_mode,
        "order_id": order_id,
        "customer_id": customer_id,
        "subscription_id": subscription_id,
        "billing_period_start": _iso(period_start),
        "billing_period_end": _iso(period_end),
        "renews_at": _iso(period_end),
        "updated_at": updated_at,
        "total": total,
        "discount_id": discount_id,
    }
    if event_name == "order_created":
        attributes = {
            **common,
            "status": status or "paid",
            "first_order_item": {"variant_id": variant_id},
        }
        return {"meta": meta, "data": {"type": "orders", "id": order_id, "attributes": attributes}}
    if event_name.startswith("subscription_payment_") or event_name == "order_refunded":
        attributes = {
            **common,
            "invoice_id": invoice_id,
            "status": status or "paid",
            "subscription_status": subscription_status or "active",
            "billing_reason": billing_reason or "initial",
            "refunded_amount": refunded_amount,
        }
        data_type = "orders" if event_name == "order_refunded" else "subscription-invoices"
        data_id = order_id if event_name == "order_refunded" else invoice_id
        return {"meta": meta, "data": {"type": data_type, "id": data_id, "attributes": attributes}}
    attributes = {
        **common,
        "status": status or "active",
        "ends_at": None,
    }
    return {"meta": meta, "data": {"type": "subscriptions", "id": subscription_id, "attributes": attributes}}


def delivery_digest(payload):
    body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(body).hexdigest()


def process(
    con,
    payload,
    *,
    now=NOW,
    source="webhook",
    delivery_key=None,
    expected=EXPECTED,
):
    return predplatne.process_subscription_event(
        con,
        payload=payload,
        now=now,
        expected=expected,
        source=source,
        delivery_key=delivery_key or delivery_digest(payload),
    )


def activate_founder(con):
    process(con, event("order_created"))
    process(con, event("subscription_created"))
    process(con, event("subscription_payment_success", billing_reason="initial"))


def access(con, *, now):
    return predplatne.subscription_access(
        predplatne.subscription_for_user(con, 1), now=now
    )


def test_task_3_public_interfaces_have_the_approved_signatures():
    process_hints = get_type_hints(predplatne.process_subscription_event)
    key_hints = get_type_hints(predplatne.subscription_event_key)

    assert str(inspect.signature(predplatne.process_subscription_event)) == (
        "(con, *, payload, now, expected, source, delivery_key) -> 'dict'"
    )
    assert str(inspect.signature(predplatne.subscription_event_key)) == (
        "(payload, *, verified_body_digest=None, reconciliation_key=None) -> 'str'"
    )
    assert process_hints["return"] is dict
    assert key_hints["return"] is str


def test_nonpayment_webhook_key_uses_verified_body_digest_not_original_order():
    payload = event("subscription_created")

    first = predplatne.subscription_event_key(payload, verified_body_digest="a" * 64)
    second = predplatne.subscription_event_key(payload, verified_body_digest="b" * 64)

    assert first == "lemon:webhook:subscription_created:" + "a" * 64
    assert second == "lemon:webhook:subscription_created:" + "b" * 64


def test_payment_event_key_uses_provider_invoice_id_not_original_order_id():
    first = event("subscription_payment_success", invoice_id="inv_1", order_id="ord_same")
    second = event("subscription_payment_success", invoice_id="inv_2", order_id="ord_same")

    assert predplatne.subscription_event_key(first) == (
        "lemon:invoice:subscription_payment_success:inv_1"
    )
    assert predplatne.subscription_event_key(second) == (
        "lemon:invoice:subscription_payment_success:inv_2"
    )


def test_payment_event_without_provider_invoice_id_is_rejected_even_with_order_and_digest():
    payload = event("subscription_payment_success", invoice_id=None)
    payload["data"]["id"] = ""
    payload["data"]["attributes"].pop("invoice_id")

    with pytest.raises(predplatne.SubscriptionEventRejected):
        predplatne.subscription_event_key(payload, verified_body_digest="a" * 64)


def test_reconciliation_key_takes_precedence_and_stays_distinct_from_webhook_key():
    payload = event("subscription_updated")
    reconciliation_key = "subscriptions:sub_1:2026-09-12T00_01_40Z"

    assert predplatne.subscription_event_key(
        payload, reconciliation_key=reconciliation_key
    ) == f"lemon:reconcile:subscription_updated:{reconciliation_key}"
    assert predplatne.subscription_event_key(
        payload,
        verified_body_digest="c" * 64,
        reconciliation_key=reconciliation_key,
    ) == f"lemon:reconcile:subscription_updated:{reconciliation_key}"
    assert predplatne.subscription_event_key(
        payload,
        verified_body_digest="c" * 64,
    ) == "lemon:webhook:subscription_updated:" + "c" * 64


def test_task_2_attempt_id_is_the_annual_webhook_pairing_token():
    assert platby.custom_checkout_attempt(event("order_created")) == ATTEMPT_ID


def test_order_created_pairs_attempt_but_grants_no_entitlement(db):
    result = process(db, event("order_created"))

    attempt = db.execute(
        "SELECT status,provider_order_id FROM checkout_attempts WHERE public_id=?",
        (ATTEMPT_ID,),
    ).fetchone()
    assert result["duplicate"] is False
    assert tuple(attempt) == ("paid", "ord_1")
    assert db.execute("SELECT COUNT(*) FROM subscriptions").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM subscription_invoices").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM naroky").fetchone()[0] == 0
    assert db.execute("SELECT platiaci FROM pouzivatelia WHERE id=1").fetchone()[0] == 0


def test_subscription_created_activates_only_the_matching_verified_attempt(db):
    process(db, event("order_created"))

    result = process(db, event("subscription_created"))
    snapshot = predplatne.subscription_for_user(db, 1)

    assert result["review_required"] is False
    assert snapshot.provider_subscription_id == "sub_1"
    assert snapshot.founder is True
    assert snapshot.initial_amount_cents == 3_900
    assert snapshot.renewal_amount_cents == 4_900
    assert snapshot.initial_payment_verified is True
    assert access(db, now=P0_END - 1.0) is True


def test_initial_payment_advances_revision_past_delayed_unpaid_update(db):
    process(db, event("order_created", updated_at="2026-09-12T00:01:00Z"))
    process(
        db,
        event("subscription_created", updated_at="2026-09-12T00:02:00Z"),
    )
    initial = process(
        db,
        event(
            "subscription_payment_success",
            billing_reason="initial",
            updated_at="2026-09-12T00:04:00Z",
        ),
    )
    after_payment = predplatne.subscription_for_user(db, 1)

    delayed = process(
        db,
        event(
            "subscription_updated",
            status="unpaid",
            updated_at="2026-09-12T00:03:00Z",
        ),
    )
    current = predplatne.subscription_for_user(db, 1)

    assert initial["review_required"] is False
    assert after_payment.provider_updated_at == P0_START + 240.0
    assert delayed["review_required"] is True
    assert current.status == "active"
    assert current.period_start == P0_START
    assert current.period_end == P0_END
    assert current.paid_through == P0_END
    assert current.provider_updated_at == P0_START + 240.0
    assert access(db, now=P0_END - 1.0) is True
    assert db.execute(
        "SELECT COUNT(*) FROM subscription_invoices"
    ).fetchone()[0] == 1


def test_second_valid_attempt_cannot_replace_verified_provider_identity(db):
    activate_founder(db)
    before = predplatne.subscription_for_user(db, 1)
    invoices_before = [
        tuple(row)
        for row in db.execute(
            "SELECT provider_invoice_id,provider_subscription_id,provider_order_id "
            "FROM subscription_invoices ORDER BY id"
        )
    ]
    seed_attempt(
        db,
        public_id=OTHER_ATTEMPT_ID,
        user_id=1,
        provider_checkout_id="checkout_2",
    )
    second_order = event(
        "order_created",
        attempt_id=OTHER_ATTEMPT_ID,
        order_id="ord_2",
        subscription_id="sub_2",
        customer_id="cus_2",
        updated_at="2026-09-12T00:03:00Z",
    )
    second_created = event(
        "subscription_created",
        attempt_id=OTHER_ATTEMPT_ID,
        order_id="ord_2",
        subscription_id="sub_2",
        customer_id="cus_2",
        updated_at="2026-09-12T00:03:01Z",
    )

    assert process(db, second_order)["review_required"] is False
    result = process(db, second_created)

    after = predplatne.subscription_for_user(db, 1)
    assert result["review_required"] is True
    assert (
        after.provider_customer_id,
        after.provider_order_id,
        after.provider_subscription_id,
    ) == (
        before.provider_customer_id,
        before.provider_order_id,
        before.provider_subscription_id,
    )
    assert [
        tuple(row)
        for row in db.execute(
            "SELECT provider_invoice_id,provider_subscription_id,provider_order_id "
            "FROM subscription_invoices ORDER BY id"
        )
    ] == invoices_before


def test_repeated_subscription_created_with_same_provider_identity_is_allowed(db):
    process(db, event("order_created"))
    process(db, event("subscription_created"))

    repeated = event(
        "subscription_created", updated_at="2026-09-12T00:02:00Z"
    )
    result = process(db, repeated)

    assert result["review_required"] is False
    assert predplatne.subscription_for_user(db, 1).provider_subscription_id == "sub_1"


def test_two_renewal_invoices_for_one_subscription_are_both_recorded(db):
    activate_founder(db)
    renewal_1 = event(
        "subscription_payment_success",
        invoice_id="inv_1",
        total=4_900,
        discount_id=None,
        period_start=P0_END,
        period_end=P1_END,
        billing_reason="renewal",
    )
    renewal_2 = event(
        "subscription_payment_success",
        invoice_id="inv_2",
        total=4_900,
        discount_id=None,
        period_start=P1_END,
        period_end=P2_END,
        billing_reason="renewal",
    )

    process(db, renewal_1)
    process(db, renewal_2)
    replay = process(db, renewal_2)

    invoices = db.execute(
        "SELECT provider_invoice_id,invoice_kind,amount_cents FROM subscription_invoices ORDER BY id"
    ).fetchall()
    assert [tuple(row) for row in invoices] == [
        ("inv_0", "initial", 3_900),
        ("inv_1", "renewal", 4_900),
        ("inv_2", "renewal", 4_900),
    ]
    assert replay["duplicate"] is True
    assert predplatne.subscription_for_user(db, 1).paid_through == P2_END


def test_regular_initial_and_renewal_invoices_are_both_4900(db):
    db.execute("DELETE FROM checkout_attempts")
    seed_attempt(db, founder=False)
    expected = {**EXPECTED, "founder_discount_id": FOUNDER_DISCOUNT_ID}
    created = event("subscription_created", total=4_900, discount_id=None)
    initial = event(
        "subscription_payment_success", total=4_900, discount_id=None,
        billing_reason="initial",
    )
    renewal = event(
        "subscription_payment_success", invoice_id="inv_1", total=4_900,
        discount_id=None, period_start=P0_END, period_end=P1_END,
        billing_reason="renewal",
    )

    for payload in (event("order_created", total=4_900, discount_id=None), created, initial, renewal):
        predplatne.process_subscription_event(
            db, payload=payload, now=NOW, expected=expected,
            source="webhook", delivery_key=delivery_digest(payload),
        )

    assert [
        tuple(row)
        for row in db.execute(
            "SELECT invoice_kind,amount_cents FROM subscription_invoices ORDER BY id"
        )
    ] == [("initial", 4_900), ("renewal", 4_900)]


@pytest.mark.parametrize(
    "field,value",
    (
        ("store_id", "other_store"),
        ("variant_id", "other_variant"),
        ("currency", "USD"),
        ("test_mode", False),
        ("total", 4_900),
        ("discount_id", None),
    ),
)
def test_subscription_created_quarantines_mismatched_commercial_facts(db, field, value):
    process(db, event("order_created"))
    payload = event("subscription_created")
    payload["data"]["attributes"][field] = value

    result = process(db, payload)

    assert result["review_required"] is True
    assert predplatne.subscription_for_user(db, 1) is None
    assert db.execute(
        "SELECT processing_status FROM subscription_events ORDER BY id DESC LIMIT 1"
    ).fetchone()[0] == "requires_review"


def test_subscription_created_quarantines_conflicting_user_mapping(db):
    process(db, event("order_created"))

    result = process(db, event("subscription_created", user_id=2))

    assert result["review_required"] is True
    assert predplatne.subscription_for_user(db, 1) is None
    assert predplatne.subscription_for_user(db, 2) is None


def test_subscription_created_quarantines_invalid_period_dates(db):
    process(db, event("order_created"))
    payload = event("subscription_created")
    payload["data"]["attributes"]["billing_period_end"] = "2026-09-11T00:00:00Z"

    result = process(db, payload)

    assert result["review_required"] is True
    assert predplatne.subscription_for_user(db, 1) is None


def test_payment_quarantines_wrong_subscription_and_regressive_period(db):
    activate_founder(db)
    wrong_subscription = event(
        "subscription_payment_success",
        subscription_id="sub_other",
        invoice_id="inv_wrong",
        total=4_900,
        discount_id=None,
        period_start=P0_END,
        period_end=P1_END,
        billing_reason="renewal",
    )
    regressive = event(
        "subscription_payment_success",
        invoice_id="inv_regressive",
        total=4_900,
        discount_id=None,
        period_start=P0_START,
        period_end=P0_END,
        billing_reason="renewal",
    )

    assert process(db, wrong_subscription)["review_required"] is True
    assert process(db, regressive)["review_required"] is True
    assert db.execute("SELECT COUNT(*) FROM subscription_invoices").fetchone()[0] == 1
    assert predplatne.subscription_for_user(db, 1).paid_through == P0_END


def test_invoice_before_subscription_is_reviewed_without_partial_write_then_repairable(db):
    initial_invoice = event(
        "subscription_payment_success", billing_reason="initial"
    )

    reviewed = process(db, initial_invoice)

    assert reviewed["review_required"] is True
    assert db.execute("SELECT COUNT(*) FROM subscription_invoices").fetchone()[0] == 0
    process(db, event("order_created"))
    process(db, event("subscription_created"))

    repaired = process(db, initial_invoice)

    assert repaired["duplicate"] is False
    assert repaired["review_required"] is False
    assert db.execute("SELECT COUNT(*) FROM subscription_invoices").fetchone()[0] == 1
    assert db.execute(
        "SELECT processing_status FROM subscription_events "
        "WHERE event_key='lemon:invoice:subscription_payment_success:inv_0'"
    ).fetchone()[0] == "processed"


def test_incomplete_renewal_rolls_back_invoice_and_corrected_webhook_repairs_it(db):
    activate_founder(db)
    incomplete = event(
        "subscription_payment_success",
        invoice_id="inv_1",
        total=4_900,
        discount_id=None,
        period_start=P0_END,
        period_end=P1_END,
        billing_reason="renewal",
        updated_at="2027-09-12T00:00:01Z",
    )
    incomplete["data"]["attributes"]["renews_at"] = None

    reviewed = process(db, incomplete)

    assert reviewed["review_required"] is True
    assert db.execute("SELECT COUNT(*) FROM subscription_invoices").fetchone()[0] == 1
    assert predplatne.subscription_for_user(db, 1).paid_through == P0_END

    corrected = event(
        "subscription_payment_success",
        invoice_id="inv_1",
        total=4_900,
        discount_id=None,
        period_start=P0_END,
        period_end=P1_END,
        billing_reason="renewal",
        updated_at="2027-09-12T00:00:02Z",
    )
    repaired = process(db, corrected)

    assert repaired["duplicate"] is False
    assert repaired["review_required"] is False
    assert db.execute("SELECT COUNT(*) FROM subscription_invoices").fetchone()[0] == 2
    assert predplatne.subscription_for_user(db, 1).paid_through == P1_END


def test_reconciliation_can_repair_reviewed_invoice_with_separate_namespace(db):
    activate_founder(db)
    incomplete = event(
        "subscription_payment_success",
        invoice_id="inv_reconcile",
        total=4_900,
        discount_id=None,
        period_start=P0_END,
        period_end=P1_END,
        billing_reason="renewal",
        updated_at="2027-09-12T00:00:01Z",
    )
    incomplete["data"]["attributes"]["renews_at"] = None
    assert process(db, incomplete)["review_required"] is True
    assert db.execute("SELECT COUNT(*) FROM subscription_invoices").fetchone()[0] == 1

    corrected = event(
        "subscription_payment_success",
        invoice_id="inv_reconcile",
        total=4_900,
        discount_id=None,
        period_start=P0_END,
        period_end=P1_END,
        billing_reason="renewal",
        updated_at="2027-09-12T00:00:02Z",
    )
    repaired = process(
        db,
        corrected,
        source="reconciliation",
        delivery_key="invoice:inv_reconcile:corrected",
    )

    assert repaired["duplicate"] is False
    assert repaired["review_required"] is False
    assert db.execute("SELECT COUNT(*) FROM subscription_invoices").fetchone()[0] == 2
    keys = {
        row[0]
        for row in db.execute(
            "SELECT event_key FROM subscription_events "
            "WHERE provider_invoice_id='inv_reconcile'"
        )
    }
    assert keys == {
        "lemon:invoice:subscription_payment_success:inv_reconcile",
        "lemon:reconcile:subscription_payment_success:invoice:inv_reconcile:corrected",
    }


def test_cancel_keeps_access_until_verified_end_but_expiry_removes_it(db):
    activate_founder(db)
    cancelled = event("subscription_cancelled", status="cancelled")
    cancelled["data"]["attributes"]["ends_at"] = _iso(P0_END)

    process(db, cancelled)

    assert access(db, now=P0_END - 1.0) is True
    assert access(db, now=P0_END + 1.0) is False
    process(db, event("subscription_expired", status="expired"), now=P0_END + 1.0)
    assert predplatne.subscription_for_user(db, 1).status == "expired"
    assert access(db, now=P0_END + 1.0) is False


def test_active_and_past_due_access_never_exceeds_verified_paid_through(db):
    activate_founder(db)

    assert access(db, now=P0_END - 1.0) is True
    assert access(db, now=P0_END + 1.0) is False
    process(
        db,
        event(
            "subscription_payment_failed",
            invoice_id="inv_late",
            status="failed",
            subscription_status="past_due",
            updated_at="2026-09-12T00:02:00Z",
        ),
    )
    assert access(db, now=P0_END + 1.0) is False


def test_delayed_active_update_cannot_revive_verified_expiry(db):
    activate_founder(db)
    expired = event(
        "subscription_expired",
        status="expired",
        updated_at="2027-09-12T00:00:01Z",
    )
    process(db, expired, now=P0_END + 1.0)
    verified = predplatne.subscription_for_user(db, 1)

    delayed = event(
        "subscription_updated",
        status="active",
        updated_at="2026-09-12T00:02:00Z",
    )
    result = process(db, delayed, now=P0_END + 2.0)
    current = predplatne.subscription_for_user(db, 1)

    assert result["review_required"] is True
    assert current.status == "expired"
    assert current.paid_through == verified.paid_through
    assert current.provider_updated_at == verified.provider_updated_at
    assert access(db, now=P0_END + 2.0) is False


def test_generic_active_update_cannot_recover_unpaid_but_verified_payment_can(db):
    activate_founder(db)
    process(
        db,
        event(
            "subscription_updated",
            status="unpaid",
            updated_at="2026-09-12T00:03:00Z",
        ),
    )

    generic = process(
        db,
        event(
            "subscription_updated",
            status="active",
            updated_at="2026-09-12T00:04:00Z",
        ),
    )
    assert generic["review_required"] is True
    assert predplatne.subscription_for_user(db, 1).status == "unpaid"

    recovered = process(
        db,
        event(
            "subscription_payment_recovered",
            invoice_id="inv_recovery_strict",
            status="paid",
            subscription_status="active",
            updated_at="2026-09-12T00:05:00Z",
        ),
    )
    assert recovered["review_required"] is False
    assert predplatne.subscription_for_user(db, 1).status == "active"


def test_verified_unpaid_suspends_and_payment_recovery_restores_access(db):
    activate_founder(db)

    process(db, event("subscription_updated", status="unpaid"))
    assert access(db, now=P0_END - 1.0) is False

    recovered = event(
        "subscription_payment_recovered",
        invoice_id="inv_recovery",
        status="paid",
        subscription_status="active",
    )
    process(db, recovered)
    assert access(db, now=P0_END - 1.0) is True
    assert db.execute("SELECT COUNT(*) FROM subscription_invoices").fetchone()[0] == 1


@pytest.mark.parametrize(
    "payload_mutation",
    ("paused", "unknown", "incomplete"),
)
def test_paused_unknown_or_incomplete_updates_preserve_paid_through_access(db, payload_mutation):
    activate_founder(db)
    before = predplatne.subscription_for_user(db, 1)
    payload = event("subscription_updated", status="active")
    if payload_mutation == "paused":
        payload = event("subscription_paused", status="paused")
    elif payload_mutation == "unknown":
        payload["data"]["attributes"]["status"] = "new-provider-state"
    else:
        payload["data"]["attributes"].pop("variant_id")

    result = process(db, payload)
    after = predplatne.subscription_for_user(db, 1)

    assert result["review_required"] is True
    assert after.status == before.status
    assert after.paid_through == before.paid_through
    assert after.last_verified_event_at == before.last_verified_event_at
    assert access(db, now=P0_END - 1.0) is True


def test_failed_and_recovered_payment_change_dunning_without_adding_invoice(db):
    activate_founder(db)
    failed = event(
        "subscription_payment_failed",
        invoice_id="inv_due",
        status="failed",
        subscription_status="past_due",
    )
    recovered = event(
        "subscription_payment_recovered",
        invoice_id="inv_due",
        status="paid",
        subscription_status="active",
    )

    process(db, failed)
    assert predplatne.subscription_for_user(db, 1).status == "past_due"
    process(db, recovered)

    assert predplatne.subscription_for_user(db, 1).status == "active"
    assert db.execute("SELECT COUNT(*) FROM subscription_invoices").fetchone()[0] == 1


def test_full_and_partial_refunds_update_only_the_exact_invoice_period(db):
    activate_founder(db)
    renewal = event(
        "subscription_payment_success",
        invoice_id="inv_1",
        total=4_900,
        discount_id=None,
        period_start=P0_END,
        period_end=P1_END,
        billing_reason="renewal",
    )
    process(db, renewal)
    process(
        db,
        event(
            "order_refunded",
            invoice_id="inv_0",
            total=3_900,
            refunded_amount=3_900,
            status="refunded",
            billing_reason="initial",
        ),
    )
    process(
        db,
        event(
            "subscription_payment_refunded",
            invoice_id="inv_1",
            total=4_900,
            discount_id=None,
            refunded_amount=1_000,
            status="partial_refund",
            period_start=P0_END,
            period_end=P1_END,
            billing_reason="renewal",
        ),
    )

    rows = db.execute(
        "SELECT provider_invoice_id,status,refunded_amount_cents FROM subscription_invoices ORDER BY id"
    ).fetchall()
    assert [tuple(row) for row in rows] == [
        ("inv_0", "refunded", 3_900),
        ("inv_1", "partial_refund", 1_000),
    ]
    assert predplatne.subscription_for_user(db, 1).paid_through == P1_END
    assert access(db, now=P1_END - 1.0) is True


@pytest.mark.parametrize(
    "event_name",
    (
        "subscription_payment_failed",
        "subscription_payment_recovered",
        "subscription_resumed",
        "subscription_paused",
        "subscription_unpaused",
        "subscription_payment_refunded",
        "order_refunded",
    ),
)
def test_every_supported_lifecycle_event_has_an_idempotent_transition(db, event_name):
    activate_founder(db)
    if event_name == "subscription_payment_failed":
        payload = event(event_name, invoice_id="inv_transition", status="failed", subscription_status="past_due")
    elif event_name == "subscription_payment_recovered":
        payload = event(event_name, invoice_id="inv_transition", status="paid", subscription_status="active")
    elif event_name == "subscription_resumed":
        cancelled = event("subscription_cancelled", status="cancelled")
        cancelled["data"]["attributes"]["ends_at"] = _iso(P0_END)
        process(db, cancelled)
        payload = event(event_name, status="active")
    elif event_name == "subscription_paused":
        payload = event(event_name, status="paused")
    elif event_name == "subscription_unpaused":
        payload = event(event_name, status="active")
    else:
        payload = event(
            event_name,
            invoice_id="inv_0",
            refunded_amount=1_000,
            status="partial_refund",
            billing_reason="initial",
        )
    key = delivery_digest(payload)

    first = process(db, payload, delivery_key=key)
    state_after_first = (
        db.execute("SELECT COUNT(*) FROM subscription_invoices").fetchone()[0],
        tuple(predplatne.subscription_for_user(db, 1).__dict__.values()),
    )
    replay = process(db, payload, delivery_key=key)
    state_after_replay = (
        db.execute("SELECT COUNT(*) FROM subscription_invoices").fetchone()[0],
        tuple(predplatne.subscription_for_user(db, 1).__dict__.values()),
    )

    assert first["duplicate"] is False
    assert replay["duplicate"] is (event_name != "subscription_paused")
    assert replay["review_required"] is (event_name == "subscription_paused")
    assert state_after_replay == state_after_first


def test_durable_event_payload_is_a_strict_redacted_projection(db):
    payload = event("order_created")
    payload["meta"].update(
        {
            "webhook_secret": "never-store-secret",
            "signed_url": "https://signed.example/secret-token",
            "customer_email": "private@example.sk",
        }
    )
    payload["data"]["attributes"].update(
        {
            "urls": {"receipt": "https://receipt.example/private"},
            "user_name": "Private Person",
            "api_token": "provider-api-token",
        }
    )
    payload["data"]["relationships"] = {
        "customer": {
            "data": {
                "id": "cus_1",
                "email": "nested@example.sk",
                "url": "https://customer.example/private",
            }
        }
    }

    process(db, payload)

    stored = db.execute(
        "SELECT payload_json FROM subscription_events ORDER BY id DESC LIMIT 1"
    ).fetchone()[0]
    projection = json.loads(stored)
    assert set(projection) <= {
        "amount_cents",
        "billing_reason",
        "currency",
        "discount_present",
        "event_name",
        "period_end",
        "period_start",
        "provider_invoice_id",
        "provider_order_id",
        "provider_subscription_id",
        "provider_updated_at",
        "refunded_amount_cents",
        "resource_id",
        "resource_type",
        "status",
        "store_id",
        "subscription_status",
        "test_mode",
        "variant_id",
    }
    assert projection["event_name"] == "order_created"
    for forbidden in (
        "never-store-secret",
        "secret-token",
        "private@example.sk",
        "nested@example.sk",
        "provider-api-token",
        "Private Person",
        "https://",
        ATTEMPT_ID,
    ):
        assert forbidden not in stored


@pytest.mark.parametrize("outcome", ("review", "replay"))
def test_processor_uses_savepoint_without_committing_caller_transaction(db, outcome):
    original = event("order_created")
    process(db, original)
    db.execute(
        "INSERT INTO pouzivatelia (id,email,platiaci) VALUES (3,'pending@example.sk',0)"
    )

    if outcome == "review":
        payload = event("subscription_created")
        payload["data"]["attributes"].pop("variant_id")
        result = process(db, payload)
        assert result["review_required"] is True
    else:
        result = process(db, original)
        assert result["duplicate"] is True

    assert db.in_transaction is True
    db.rollback()
    assert db.execute(
        "SELECT 1 FROM pouzivatelia WHERE id=3"
    ).fetchone() is None


def _load_server(monkeypatch, tmp_path, *, payments_enabled=True):
    monkeypatch.setenv("UVARSI_DB", str(tmp_path / "task3.db"))
    monkeypatch.setenv("UVARSI_URL", "https://uvar.si")
    monkeypatch.setenv("UVARSI_VERSION_FILE", str(ROOT / "VERSION"))
    monkeypatch.setenv("UVARSI_STATIC", str(tmp_path / "static"))
    monkeypatch.setenv("PLATBY_ZAPNUTE", "1" if payments_enabled else "0")
    monkeypatch.setenv("LEMON_WEBHOOK_SECRET", WEBHOOK_SECRET)
    monkeypatch.setenv("LEMON_STORE_ID", STORE_ID)
    monkeypatch.setenv("LEMON_SUBSCRIPTION_VARIANT_ID", VARIANT_ID)
    monkeypatch.setenv("LEMON_FOUNDER_DISCOUNT_ID", FOUNDER_DISCOUNT_ID)
    monkeypatch.setenv("LEMON_FOUNDER_DISCOUNT_CODE", "FOUNDERS")
    monkeypatch.syspath_prepend(str(ROOT / "app"))
    sys.modules.pop("server", None)
    server = importlib.import_module("server")
    monkeypatch.setattr(server, "ENV_FILE", str(tmp_path / "missing.env"))
    with closing(server.db()) as con:
        con.execute(
            "INSERT INTO pouzivatelia (id,email,platiaci) VALUES (1,'one@example.sk',0)"
        )
        seed_attempt(con, test_mode=False)
    return server


def _signed_post(client, payload, *, secret=WEBHOOK_SECRET, indent=None):
    body = json.dumps(
        payload,
        ensure_ascii=False,
        indent=indent,
        separators=None if indent is not None else (",", ":"),
    ).encode("utf-8")
    response = client.post(
        "/api/platba/webhook",
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-Signature": hmac.new(
                secret.encode("utf-8"), body, hashlib.sha256
            ).hexdigest(),
        },
    )
    return response, body


def test_real_live_endpoint_verifies_hmac_then_digest_and_processes_annual_lifecycle(
    monkeypatch, tmp_path
):
    server = _load_server(monkeypatch, tmp_path)
    calls = []
    real_verify = server.overit_podpis
    real_sha256 = hashlib.sha256

    def verify(*, tajomstvo, telo, podpis):
        calls.append(("hmac", bytes(telo)))
        return real_verify(tajomstvo=tajomstvo, telo=telo, podpis=podpis)

    def sha256(value):
        calls.append(("sha256", bytes(value)))
        return real_sha256(value)

    monkeypatch.setattr(server, "overit_podpis", verify)
    monkeypatch.setattr(server, "hashlib", SimpleNamespace(sha256=sha256))
    client = TestClient(server.app, raise_server_exceptions=False)
    payloads = [
        event("order_created", test_mode=False),
        event("subscription_created", test_mode=False),
        event(
            "subscription_payment_success",
            test_mode=False,
            billing_reason="initial",
        ),
    ]
    bodies = []
    actions = []
    for payload in payloads:
        response, body = _signed_post(client, payload, indent=2)
        assert response.status_code == 200
        actions.append(response.json()["akcia"])
        bodies.append(body)

    assert actions == ["paired", "activated", "invoice_recorded"]
    assert calls == [
        item
        for body in bodies
        for item in (("hmac", body), ("sha256", body))
    ]
    with closing(server.db()) as con:
        snapshot = predplatne.subscription_for_user(con, 1)
        invoice = con.execute(
            "SELECT provider_invoice_id,amount_cents FROM subscription_invoices"
        ).fetchone()
    assert snapshot.test_mode is False
    assert snapshot.provider_subscription_id == "sub_1"
    assert tuple(invoice) == ("inv_0", 3_900)


def test_deferred_live_annual_order_subscription_and_payment_use_real_processor(
    monkeypatch, tmp_path
):
    server = _load_server(monkeypatch, tmp_path, payments_enabled=False)
    client = TestClient(server.app, raise_server_exceptions=False)
    payloads = [
        event("order_created", test_mode=False),
        event("subscription_created", test_mode=False),
        event(
            "subscription_payment_success",
            test_mode=False,
            billing_reason="initial",
        ),
    ]
    for payload in payloads:
        response, _body = _signed_post(client, payload)
        assert response.status_code == 200
        assert response.json()["akcia"] == "odlozene"

    with closing(server.db()) as con:
        result = platby.spracuj_odlozene(
            con,
            tajomstvo=WEBHOOK_SECRET,
            now=NOW,
            expected_test_mode=False,
            subscription_expected={**EXPECTED, "test_mode": False},
        )
        snapshot = predplatne.subscription_for_user(con, 1)
        invoices = con.execute(
            "SELECT provider_invoice_id,amount_cents FROM subscription_invoices"
        ).fetchall()
        queued = con.execute(
            "SELECT telo,podpis,spracovane_o FROM platobne_odlozene ORDER BY id"
        ).fetchall()

    assert result["spracovane"] == 3
    assert [item["akcia"] for item in result["udalosti"]] == [
        "paired",
        "activated",
        "invoice_recorded",
    ]
    assert snapshot.provider_subscription_id == "sub_1"
    assert [tuple(row) for row in invoices] == [("inv_0", 3_900)]
    assert all(bytes(row["telo"]) == b"" for row in queued)
    assert all(row["podpis"] is None for row in queued)
    assert all(row["spracovane_o"] is not None for row in queued)


def test_deferred_replay_authenticates_before_digest_and_json_and_keeps_bad_body_pending(
    db, monkeypatch
):
    malformed = b'{"meta":'
    signature = hmac.new(
        WEBHOOK_SECRET.encode("utf-8"), malformed, hashlib.sha256
    ).hexdigest()
    platby.odloz_webhook(db, telo=malformed, podpis=signature, now=NOW)
    calls = []
    real_sha256 = hashlib.sha256
    real_loads = json.loads

    def verify(*, tajomstvo, telo, podpis):
        calls.append("hmac")
        expected_signature = hmac.new(
            tajomstvo.encode("utf-8"), bytes(telo), real_sha256
        ).hexdigest()
        return hmac.compare_digest(expected_signature, podpis)

    def sha256(value):
        calls.append("sha256")
        return real_sha256(value)

    def loads(value):
        calls.append("json")
        return real_loads(value)

    monkeypatch.setattr(platby, "overit_podpis", verify)
    monkeypatch.setattr(platby, "hashlib", SimpleNamespace(sha256=sha256))
    monkeypatch.setattr(platby, "json", SimpleNamespace(loads=loads))

    result = platby.spracuj_odlozene(
        db,
        tajomstvo=WEBHOOK_SECRET,
        now=NOW,
        expected_test_mode=False,
        subscription_expected={**EXPECTED, "test_mode": False},
    )

    assert calls == ["hmac", "sha256", "json"]
    assert result["spracovane"] == 0
    assert result["pokazene"] == 1
    row = db.execute(
        "SELECT telo,spracovane_o FROM platobne_odlozene"
    ).fetchone()
    assert bytes(row["telo"]) == malformed
    assert row["spracovane_o"] is None


def test_deferred_replay_rejects_signature_before_hash_or_json_and_keeps_row_pending(
    db, monkeypatch
):
    payload = event("order_created", test_mode=False)
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    platby.odloz_webhook(db, telo=body, podpis="a" * 64, now=NOW)
    calls = []
    real_sha256 = hashlib.sha256

    def verify(*, tajomstvo, telo, podpis):
        calls.append("hmac")
        expected_signature = hmac.new(
            tajomstvo.encode("utf-8"), bytes(telo), real_sha256
        ).hexdigest()
        return hmac.compare_digest(expected_signature, podpis)

    def forbidden(*_args, **_kwargs):
        calls.append("forbidden")
        raise AssertionError("digest/JSON ran before a valid signature")

    monkeypatch.setattr(platby, "overit_podpis", verify)
    monkeypatch.setattr(platby, "hashlib", SimpleNamespace(sha256=forbidden))
    monkeypatch.setattr(platby, "json", SimpleNamespace(loads=forbidden))

    result = platby.spracuj_odlozene(
        db,
        tajomstvo=WEBHOOK_SECRET,
        now=NOW,
        expected_test_mode=False,
        subscription_expected={**EXPECTED, "test_mode": False},
    )

    assert calls == ["hmac"]
    assert result["spracovane"] == 0
    assert result["neplatny_podpis"] == 1
    assert db.execute(
        "SELECT spracovane_o FROM platobne_odlozene"
    ).fetchone()[0] is None


def test_deferred_replay_binds_test_secret_mode_and_annual_config(db):
    test_variant = "variant_test_annual"
    valid = event(
        "order_created", test_mode=True, variant_id=test_variant
    )
    valid_body = json.dumps(valid, separators=(",", ":")).encode("utf-8")
    valid_signature = hmac.new(
        TEST_WEBHOOK_SECRET.encode("utf-8"), valid_body, hashlib.sha256
    ).hexdigest()
    platby.odloz_webhook(
        db, telo=valid_body, podpis=valid_signature, now=NOW
    )
    forged_mode = event(
        "order_created",
        order_id="ord_wrong_mode",
        attempt_id=OTHER_ATTEMPT_ID,
        test_mode=False,
        variant_id=test_variant,
    )
    forged_body = json.dumps(forged_mode, separators=(",", ":")).encode("utf-8")
    forged_signature = hmac.new(
        TEST_WEBHOOK_SECRET.encode("utf-8"), forged_body, hashlib.sha256
    ).hexdigest()
    platby.odloz_webhook(
        db, telo=forged_body, podpis=forged_signature, now=NOW
    )

    result = platby.spracuj_odlozene(
        db,
        tajomstvo=WEBHOOK_SECRET,
        test_tajomstvo=TEST_WEBHOOK_SECRET,
        now=NOW,
        subscription_expected={**EXPECTED, "test_mode": False},
        test_subscription_expected={
            **EXPECTED,
            "variant_id": test_variant,
            "test_mode": True,
        },
    )

    assert result["spracovane"] == 1
    assert result["udalosti"][0]["akcia"] == "paired"
    rows = db.execute(
        "SELECT telo,podpis,spracovane_o FROM platobne_odlozene ORDER BY id"
    ).fetchall()
    assert bytes(rows[0]["telo"]) == b""
    assert rows[0]["podpis"] is None
    assert rows[0]["spracovane_o"] is not None
    assert bytes(rows[1]["telo"]) == forged_body
    assert rows[1]["spracovane_o"] is None


def test_deferred_annual_quarantine_is_durable_before_raw_queue_body_is_removed(db):
    invalid = event("order_created", variant_id="wrong_variant")
    body = json.dumps(invalid, separators=(",", ":")).encode("utf-8")
    signature = hmac.new(
        TEST_WEBHOOK_SECRET.encode("utf-8"), body, hashlib.sha256
    ).hexdigest()
    platby.odloz_webhook(db, telo=body, podpis=signature, now=NOW)

    result = platby.spracuj_odlozene(
        db,
        tajomstvo=TEST_WEBHOOK_SECRET,
        now=NOW,
        expected_test_mode=True,
        test_subscription_expected=EXPECTED,
    )

    assert result["spracovane"] == 1
    assert result["udalosti"][0]["akcia"] == "requires_review"
    audit = db.execute(
        "SELECT processing_status,needs_review FROM subscription_events"
    ).fetchone()
    queued = db.execute(
        "SELECT telo,podpis,spracovane_o FROM platobne_odlozene"
    ).fetchone()
    assert tuple(audit) == ("requires_review", 1)
    assert bytes(queued["telo"]) == b""
    assert queued["podpis"] is None
    assert queued["spracovane_o"] is not None
