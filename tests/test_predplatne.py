import sqlite3
from dataclasses import replace

import pytest

from app import predplatne


@pytest.fixture
def db():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    predplatne.migrate_subscription_schema(connection)
    try:
        yield connection
    finally:
        connection.close()


def sample(**overrides):
    values = {
        "user_id": 1,
        "product": "premium_annual",
        "provider": "lemonsqueezy",
        "provider_customer_id": "cus_1",
        "provider_order_id": "ord_1",
        "provider_subscription_id": "sub_1",
        "provider_variant_id": "var_annual",
        "currency": "EUR",
        "test_mode": True,
        "status": "active",
        "period_start": 900.0,
        "period_end": 2_000.0,
        "renews_at": 2_000.0,
        "ends_at": None,
        "paid_through": 2_000.0,
        "initial_amount_cents": 3_900,
        "renewal_amount_cents": 4_900,
        "discount_id": "discount_founders",
        "founder": True,
        "initial_payment_verified": True,
    }
    values.update(overrides)
    return predplatne.SubscriptionSnapshot(**values)


@pytest.mark.parametrize("status", ("active", "past_due"))
def test_verified_access_statuses_grant_access(db, status):
    predplatne.upsert_snapshot(db, sample(status=status), now=1_000.0)

    assert predplatne.subscription_access(
        predplatne.subscription_for_user(db, 1), now=1_999.0
    ) is True


def test_cancelled_subscription_keeps_access_until_ends_at(db):
    predplatne.upsert_snapshot(
        db,
        sample(status="cancelled", renews_at=None, ends_at=2_000.0),
        now=1_000.0,
    )

    assert predplatne.subscription_access(
        predplatne.subscription_for_user(db, 1), now=1_999.0
    ) is True
    assert predplatne.subscription_access(
        predplatne.subscription_for_user(db, 1), now=2_001.0
    ) is False


@pytest.mark.parametrize("status", ("unpaid", "expired"))
def test_unpaid_suspends_and_expired_removes_access(db, status):
    predplatne.upsert_snapshot(
        db, sample(status=status, renews_at=None), now=1_000.0
    )

    assert predplatne.subscription_access(
        predplatne.subscription_for_user(db, 1), now=1_001.0
    ) is False


def test_paused_uses_verified_paid_through_date_and_requires_review(db):
    predplatne.upsert_snapshot(
        db,
        sample(status="paused", renews_at=None, paid_through=2_000.0),
        now=1_000.0,
    )

    snapshot = predplatne.subscription_for_user(db, 1)
    assert predplatne.subscription_access(snapshot, now=1_999.0) is True
    assert predplatne.subscription_access(snapshot, now=2_001.0) is False
    assert snapshot.needs_review is True
    review = db.execute(
        "SELECT processing_status,needs_review,review_reason "
        "FROM subscription_events"
    ).fetchone()
    assert dict(review) == {
        "processing_status": "requires_review",
        "needs_review": 1,
        "review_reason": "paused subscription requires reconciliation",
    }


def test_unknown_transition_is_queued_without_replacing_verified_access(db):
    original = sample(status="active", paid_through=2_000.0)
    predplatne.upsert_snapshot(db, original, now=1_000.0)

    accepted = predplatne.upsert_snapshot(
        db, replace(original, status="new-provider-state"), now=1_100.0
    )

    current = predplatne.subscription_for_user(db, 1)
    assert accepted is False
    assert current.status == "active"
    assert current.updated_at == 1_000.0
    assert predplatne.subscription_access(current, now=1_500.0) is True
    review = db.execute(
        "SELECT processing_status,review_reason,payload_json "
        "FROM subscription_events"
    ).fetchone()
    assert review["processing_status"] == "requires_review"
    assert review["review_reason"] == "unknown subscription status"
    assert '"status":"new-provider-state"' in review["payload_json"]


def test_incomplete_suspension_is_queued_without_revoking_verified_access(db):
    original = sample(status="active", paid_through=2_000.0)
    predplatne.upsert_snapshot(db, original, now=1_000.0)

    accepted = predplatne.upsert_snapshot(
        db,
        replace(original, status="expired", provider_variant_id=""),
        now=1_100.0,
    )

    current = predplatne.subscription_for_user(db, 1)
    assert accepted is False
    assert current.status == "active"
    assert predplatne.subscription_access(current, now=1_500.0) is True
    assert db.execute(
        "SELECT review_reason FROM subscription_events"
    ).fetchone()[0] == "missing provider_variant_id"


def test_unknown_transition_without_verified_snapshot_grants_no_access(db):
    accepted = predplatne.upsert_snapshot(
        db, sample(status="new-provider-state"), now=1_000.0
    )

    assert accepted is False
    assert predplatne.subscription_for_user(db, 1) is None
    assert predplatne.subscription_access(None, now=1_001.0) is False
