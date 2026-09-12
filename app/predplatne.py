"""Local annual-subscription snapshots and access rules."""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from dataclasses import asdict, dataclass


ACCESS_STATUSES = frozenset({"active", "past_due"})
SUSPENDED_STATUSES = frozenset({"unpaid", "expired"})
KNOWN_STATUSES = ACCESS_STATUSES | SUSPENDED_STATUSES | {"cancelled", "paused"}


SUBSCRIPTION_SCHEMA = """
CREATE TABLE IF NOT EXISTS subscriptions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL,
  product TEXT NOT NULL,
  provider TEXT NOT NULL,
  provider_customer_id TEXT NOT NULL,
  provider_order_id TEXT NOT NULL,
  provider_subscription_id TEXT NOT NULL,
  provider_variant_id TEXT NOT NULL,
  currency TEXT NOT NULL,
  test_mode INTEGER NOT NULL CHECK(test_mode IN (0, 1)),
  status TEXT NOT NULL CHECK(
    status IN ('active','cancelled','past_due','unpaid','expired','paused')
  ),
  period_start REAL NOT NULL,
  period_end REAL NOT NULL,
  renews_at REAL,
  ends_at REAL,
  paid_through REAL NOT NULL,
  initial_amount_cents INTEGER NOT NULL CHECK(initial_amount_cents >= 0),
  renewal_amount_cents INTEGER NOT NULL CHECK(renewal_amount_cents >= 0),
  discount_id TEXT,
  founder INTEGER NOT NULL DEFAULT 0 CHECK(founder IN (0, 1)),
  initial_payment_verified INTEGER NOT NULL DEFAULT 0
    CHECK(initial_payment_verified IN (0, 1)),
  needs_review INTEGER NOT NULL DEFAULT 0 CHECK(needs_review IN (0, 1)),
  review_reason TEXT,
  last_verified_event_at REAL NOT NULL,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL,
  UNIQUE(user_id, product)
);
CREATE INDEX IF NOT EXISTS subscriptions_user_idx
  ON subscriptions(user_id);
CREATE INDEX IF NOT EXISTS subscriptions_status_idx
  ON subscriptions(status);
CREATE INDEX IF NOT EXISTS subscriptions_renewal_idx
  ON subscriptions(renews_at) WHERE renews_at IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS subscriptions_provider_order_idx
  ON subscriptions(provider, test_mode, provider_order_id);
CREATE UNIQUE INDEX IF NOT EXISTS subscriptions_provider_subscription_idx
  ON subscriptions(provider, test_mode, provider_subscription_id);

CREATE TABLE IF NOT EXISTS subscription_invoices (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  provider TEXT NOT NULL,
  test_mode INTEGER NOT NULL CHECK(test_mode IN (0, 1)),
  provider_invoice_id TEXT NOT NULL,
  provider_subscription_id TEXT NOT NULL,
  provider_order_id TEXT,
  invoice_kind TEXT NOT NULL,
  status TEXT NOT NULL,
  amount_cents INTEGER NOT NULL CHECK(amount_cents >= 0),
  currency TEXT NOT NULL,
  period_start REAL NOT NULL,
  period_end REAL NOT NULL,
  paid_at REAL,
  refunded_amount_cents INTEGER NOT NULL DEFAULT 0
    CHECK(refunded_amount_cents >= 0),
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS subscription_invoices_provider_idx
  ON subscription_invoices(provider, test_mode, provider_invoice_id);
CREATE INDEX IF NOT EXISTS subscription_invoices_subscription_idx
  ON subscription_invoices(provider, test_mode, provider_subscription_id, period_end);

CREATE TABLE IF NOT EXISTS subscription_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  event_key TEXT NOT NULL UNIQUE,
  provider TEXT NOT NULL,
  test_mode INTEGER CHECK(test_mode IS NULL OR test_mode IN (0, 1)),
  provider_event_id TEXT,
  provider_subscription_id TEXT,
  provider_invoice_id TEXT,
  event_type TEXT NOT NULL,
  source TEXT NOT NULL,
  payload_json TEXT,
  processing_status TEXT NOT NULL,
  needs_review INTEGER NOT NULL DEFAULT 0 CHECK(needs_review IN (0, 1)),
  review_reason TEXT,
  received_at REAL NOT NULL,
  processed_at REAL,
  updated_at REAL NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS subscription_events_provider_idx
  ON subscription_events(provider, test_mode, provider_event_id)
  WHERE provider_event_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS subscription_events_review_idx
  ON subscription_events(needs_review, processing_status, received_at);
CREATE INDEX IF NOT EXISTS subscription_events_subscription_idx
  ON subscription_events(provider, test_mode, provider_subscription_id, received_at);
"""


@dataclass(frozen=True)
class SubscriptionSnapshot:
    user_id: int
    product: str
    provider: str
    provider_customer_id: str | None
    provider_order_id: str | None
    provider_subscription_id: str | None
    provider_variant_id: str | None
    currency: str | None
    test_mode: bool | None
    status: str | None
    period_start: float | None
    period_end: float | None
    renews_at: float | None
    ends_at: float | None
    paid_through: float | None
    initial_amount_cents: int | None
    renewal_amount_cents: int | None
    discount_id: str | None
    founder: bool
    initial_payment_verified: bool
    needs_review: bool = False
    review_reason: str | None = None
    last_verified_event_at: float | None = None
    created_at: float | None = None
    updated_at: float | None = None


def migrate_subscription_schema(con) -> None:
    """Create subscription storage without changing existing application data."""
    con.executescript(SUBSCRIPTION_SCHEMA)


def subscription_access(snapshot, *, now):
    if snapshot is None or snapshot.status in SUSPENDED_STATUSES:
        return False
    if snapshot.status == "cancelled":
        return snapshot.ends_at is not None and now < snapshot.ends_at
    if snapshot.status == "paused":
        return snapshot.paid_through is not None and now < snapshot.paid_through
    return snapshot.status in ACCESS_STATUSES


def _nonempty_text(value) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _valid_time(value) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(float(value))
        and float(value) >= 0
    )


def _valid_optional_time(value) -> bool:
    return value is None or _valid_time(value)


def _validation_error(snapshot: SubscriptionSnapshot) -> str | None:
    if (
        not isinstance(snapshot.user_id, int)
        or isinstance(snapshot.user_id, bool)
        or snapshot.user_id <= 0
    ):
        return "invalid user_id"
    for name in (
        "product",
        "provider",
        "provider_customer_id",
        "provider_order_id",
        "provider_subscription_id",
        "provider_variant_id",
        "currency",
    ):
        if not _nonempty_text(getattr(snapshot, name)):
            return f"missing {name}"
    if type(snapshot.test_mode) is not bool:
        return "missing test_mode"
    if snapshot.status not in KNOWN_STATUSES:
        return "unknown subscription status"
    for name in ("initial_amount_cents", "renewal_amount_cents"):
        value = getattr(snapshot, name)
        if (
            not isinstance(value, int)
            or isinstance(value, bool)
            or value < 0
        ):
            return f"invalid {name}"
    for name in ("founder", "initial_payment_verified", "needs_review"):
        if type(getattr(snapshot, name)) is not bool:
            return f"invalid {name}"
    for name in ("period_start", "period_end", "paid_through"):
        if not _valid_time(getattr(snapshot, name)):
            return f"missing {name}"
    if float(snapshot.period_start) >= float(snapshot.period_end):
        return "invalid period dates"
    for name in ("renews_at", "ends_at"):
        if not _valid_optional_time(getattr(snapshot, name)):
            return f"invalid {name}"
    if snapshot.status in ACCESS_STATUSES and snapshot.renews_at is None:
        return "missing renews_at"
    if snapshot.status == "cancelled" and snapshot.ends_at is None:
        return "missing ends_at"
    return None


def _queue_review(con, snapshot: SubscriptionSnapshot, *, reason: str, now: float) -> None:
    payload_json = json.dumps(
        asdict(snapshot), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    digest = hashlib.sha256(
        f"{reason}\0{payload_json}".encode("utf-8")
    ).hexdigest()
    provider = snapshot.provider if _nonempty_text(snapshot.provider) else "unknown"
    test_mode = int(snapshot.test_mode) if type(snapshot.test_mode) is bool else None
    con.execute(
        """INSERT INTO subscription_events
           (event_key,provider,test_mode,provider_event_id,
            provider_subscription_id,provider_invoice_id,event_type,source,
            payload_json,processing_status,needs_review,review_reason,
            received_at,processed_at,updated_at)
           VALUES (?,?,?,NULL,?,NULL,'snapshot_transition','local',
                   ?,'requires_review',1,?,?,NULL,?)
           ON CONFLICT(event_key) DO UPDATE SET updated_at=excluded.updated_at""",
        (
            f"snapshot-review:{digest}",
            provider,
            test_mode,
            snapshot.provider_subscription_id,
            payload_json,
            reason,
            now,
            now,
        ),
    )


def upsert_snapshot(con, snapshot: SubscriptionSnapshot, *, now: float) -> bool:
    """Store one verified snapshot, or quarantine an unsafe transition."""
    if not isinstance(snapshot, SubscriptionSnapshot):
        raise TypeError("snapshot must be a SubscriptionSnapshot")
    if not _valid_time(now):
        raise ValueError("invalid update time")
    now = float(now)
    reason = _validation_error(snapshot)
    if reason is not None:
        _queue_review(con, snapshot, reason=reason, now=now)
        return False

    needs_review = snapshot.needs_review or snapshot.status == "paused"
    review_reason = snapshot.review_reason
    if snapshot.status == "paused" and not review_reason:
        review_reason = "paused subscription requires reconciliation"
    if needs_review:
        _queue_review(
            con,
            snapshot,
            reason=review_reason or "subscription requires review",
            now=now,
        )

    values = (
        snapshot.user_id,
        snapshot.product,
        snapshot.provider,
        snapshot.provider_customer_id,
        snapshot.provider_order_id,
        snapshot.provider_subscription_id,
        snapshot.provider_variant_id,
        snapshot.currency,
        int(snapshot.test_mode),
        snapshot.status,
        float(snapshot.period_start),
        float(snapshot.period_end),
        None if snapshot.renews_at is None else float(snapshot.renews_at),
        None if snapshot.ends_at is None else float(snapshot.ends_at),
        float(snapshot.paid_through),
        snapshot.initial_amount_cents,
        snapshot.renewal_amount_cents,
        snapshot.discount_id,
        int(snapshot.founder),
        int(snapshot.initial_payment_verified),
        int(needs_review),
        review_reason,
        now,
        now,
        now,
    )
    try:
        con.execute(
            """INSERT INTO subscriptions
               (user_id,product,provider,provider_customer_id,provider_order_id,
                provider_subscription_id,provider_variant_id,currency,test_mode,
                status,period_start,period_end,renews_at,ends_at,paid_through,
                initial_amount_cents,renewal_amount_cents,discount_id,founder,
                initial_payment_verified,needs_review,review_reason,
                last_verified_event_at,created_at,updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(user_id,product) DO UPDATE SET
                 provider=excluded.provider,
                 provider_customer_id=excluded.provider_customer_id,
                 provider_order_id=excluded.provider_order_id,
                 provider_subscription_id=excluded.provider_subscription_id,
                 provider_variant_id=excluded.provider_variant_id,
                 currency=excluded.currency,
                 test_mode=excluded.test_mode,
                 status=excluded.status,
                 period_start=excluded.period_start,
                 period_end=excluded.period_end,
                 renews_at=excluded.renews_at,
                 ends_at=excluded.ends_at,
                 paid_through=excluded.paid_through,
                 initial_amount_cents=excluded.initial_amount_cents,
                 renewal_amount_cents=excluded.renewal_amount_cents,
                 discount_id=excluded.discount_id,
                 founder=excluded.founder,
                 initial_payment_verified=excluded.initial_payment_verified,
                 needs_review=excluded.needs_review,
                 review_reason=excluded.review_reason,
                 last_verified_event_at=excluded.last_verified_event_at,
                 updated_at=excluded.updated_at""",
            values,
        )
    except sqlite3.IntegrityError:
        _queue_review(
            con,
            snapshot,
            reason="provider identity conflicts with verified subscription",
            now=now,
        )
        return False
    return True


_SNAPSHOT_COLUMNS = (
    "user_id",
    "product",
    "provider",
    "provider_customer_id",
    "provider_order_id",
    "provider_subscription_id",
    "provider_variant_id",
    "currency",
    "test_mode",
    "status",
    "period_start",
    "period_end",
    "renews_at",
    "ends_at",
    "paid_through",
    "initial_amount_cents",
    "renewal_amount_cents",
    "discount_id",
    "founder",
    "initial_payment_verified",
    "needs_review",
    "review_reason",
    "last_verified_event_at",
    "created_at",
    "updated_at",
)


def subscription_for_user(con, user_id: int) -> SubscriptionSnapshot | None:
    if not isinstance(user_id, int) or isinstance(user_id, bool) or user_id <= 0:
        raise ValueError("invalid user id")
    cursor = con.execute(
        f"SELECT {','.join(_SNAPSHOT_COLUMNS)} FROM subscriptions "
        "WHERE user_id=? ORDER BY updated_at DESC,id DESC LIMIT 1",
        (user_id,),
    )
    row = cursor.fetchone()
    if row is None:
        return None
    values = dict(zip(_SNAPSHOT_COLUMNS, tuple(row)))
    for name in ("test_mode", "founder", "initial_payment_verified", "needs_review"):
        values[name] = bool(values[name])
    return SubscriptionSnapshot(**values)
