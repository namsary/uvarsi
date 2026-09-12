"""Local annual-subscription snapshots and access rules."""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import datetime
from collections.abc import Mapping
from dataclasses import asdict, dataclass, replace


ACCESS_STATUSES = frozenset({"active", "past_due"})
SUSPENDED_STATUSES = frozenset({"unpaid", "expired"})
KNOWN_STATUSES = ACCESS_STATUSES | SUSPENDED_STATUSES | {"cancelled", "paused"}
ACCESS_BEARING_STATUSES = ACCESS_STATUSES | {"cancelled", "paused"}
ANNUAL_PREMIUM_PRODUCT = "premium_annual"
ANNUAL_PREMIUM_CURRENCY = "EUR"
ANNUAL_RENEWAL_AMOUNT_CENTS = 4_900
FOUNDER_DISCOUNT_AMOUNT_CENTS = 1_000
FOUNDER_INITIAL_AMOUNT_CENTS = (
    ANNUAL_RENEWAL_AMOUNT_CENTS - FOUNDER_DISCOUNT_AMOUNT_CENTS
)
PAYMENT_EVENT_TYPES = frozenset(
    {
        "subscription_payment_success",
        "subscription_payment_failed",
        "subscription_payment_recovered",
        "subscription_payment_refunded",
        "order_refunded",
    }
)
SUBSCRIPTION_EVENT_TYPES = frozenset(
    {
        "order_created",
        "subscription_created",
        "subscription_updated",
        "subscription_cancelled",
        "subscription_resumed",
        "subscription_expired",
        "subscription_paused",
        "subscription_unpaused",
    }
) | PAYMENT_EVENT_TYPES
_SAFE_ID_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"
)
_HEX_CHARS = frozenset("0123456789abcdefABCDEF")


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
  provider_updated_at REAL,
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
    provider_updated_at: float | None = None
    created_at: float | None = None
    updated_at: float | None = None


def migrate_subscription_schema(con) -> None:
    """Create subscription storage without changing existing application data."""
    con.executescript(SUBSCRIPTION_SCHEMA)
    columns = {
        row[1] for row in con.execute("PRAGMA table_info(subscriptions)")
    }
    if "provider_updated_at" not in columns:
        con.execute(
            "ALTER TABLE subscriptions ADD COLUMN provider_updated_at REAL"
        )


def _nonempty_text(value) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _approved_annual_offer(snapshot: SubscriptionSnapshot) -> bool:
    if (
        snapshot.product != ANNUAL_PREMIUM_PRODUCT
        or snapshot.currency != ANNUAL_PREMIUM_CURRENCY
    ):
        return False
    if snapshot.founder is True:
        return (
            snapshot.initial_amount_cents == FOUNDER_INITIAL_AMOUNT_CENTS
            and snapshot.renewal_amount_cents == ANNUAL_RENEWAL_AMOUNT_CENTS
            and _nonempty_text(snapshot.discount_id)
        )
    if snapshot.founder is False:
        return (
            snapshot.initial_amount_cents == ANNUAL_RENEWAL_AMOUNT_CENTS
            and snapshot.renewal_amount_cents == ANNUAL_RENEWAL_AMOUNT_CENTS
            and snapshot.discount_id is None
        )
    return False


def subscription_access(snapshot, *, now: float) -> bool:
    if (
        not isinstance(snapshot, SubscriptionSnapshot)
        or not _approved_annual_offer(snapshot)
        or (
            snapshot.status in ACCESS_BEARING_STATUSES
            and snapshot.initial_payment_verified is not True
        )
        or snapshot.status in SUSPENDED_STATUSES
    ):
        return False
    if snapshot.status == "cancelled":
        return snapshot.ends_at is not None and now < snapshot.ends_at
    if snapshot.status == "paused":
        return snapshot.paid_through is not None and now < snapshot.paid_through
    return snapshot.status in ACCESS_STATUSES


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
    if snapshot.product != ANNUAL_PREMIUM_PRODUCT:
        return "unsupported product"
    if snapshot.currency != ANNUAL_PREMIUM_CURRENCY:
        return "invalid annual Premium currency"
    if not _approved_annual_offer(snapshot):
        return "invalid annual Premium price"
    if (
        snapshot.status in ACCESS_BEARING_STATUSES
        and not snapshot.initial_payment_verified
    ):
        return "initial payment not verified"
    for name in ("period_start", "period_end", "paid_through"):
        if not _valid_time(getattr(snapshot, name)):
            return f"missing {name}"
    if float(snapshot.period_start) >= float(snapshot.period_end):
        return "invalid period dates"
    for name in ("renews_at", "ends_at"):
        if not _valid_optional_time(getattr(snapshot, name)):
            return f"invalid {name}"
    if not _valid_optional_time(snapshot.provider_updated_at):
        return "invalid provider_updated_at"
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

    existing = con.execute(
        "SELECT provider,provider_customer_id,provider_order_id,"
        "provider_subscription_id,provider_variant_id,currency,test_mode,"
        "provider_updated_at FROM subscriptions WHERE user_id=? AND product=?",
        (snapshot.user_id, snapshot.product),
    ).fetchone()
    if existing is not None:
        incoming_identity = (
            snapshot.provider,
            snapshot.provider_customer_id,
            snapshot.provider_order_id,
            snapshot.provider_subscription_id,
            snapshot.provider_variant_id,
            snapshot.currency,
            int(snapshot.test_mode),
        )
        if tuple(existing[:7]) != incoming_identity:
            _queue_review(
                con,
                snapshot,
                reason="provider identity conflicts with verified subscription",
                now=now,
            )
            return False
        existing_revision = existing[7]
        if (
            snapshot.provider_updated_at is not None
            and existing_revision is not None
            and snapshot.provider_updated_at < existing_revision
        ):
            _queue_review(
                con,
                snapshot,
                reason="provider update is older than verified subscription",
                now=now,
            )
            return False
        if snapshot.provider_updated_at is None and existing_revision is not None:
            snapshot = replace(
                snapshot, provider_updated_at=float(existing_revision)
            )

    needs_review = snapshot.needs_review or snapshot.status == "paused"
    review_reason = snapshot.review_reason
    if snapshot.status == "paused" and not review_reason:
        review_reason = "paused subscription requires reconciliation"
    if snapshot.status == "paused" and con.execute(
        "SELECT 1 FROM subscriptions "
        "WHERE user_id=? AND product=? AND initial_payment_verified=1",
        (snapshot.user_id, snapshot.product),
    ).fetchone() is not None:
        _queue_review(
            con,
            snapshot,
            reason=review_reason,
            now=now,
        )
        return False
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
        snapshot.provider_updated_at,
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
                last_verified_event_at,provider_updated_at,created_at,updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
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
                 provider_updated_at=excluded.provider_updated_at,
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
    "provider_updated_at",
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


class SubscriptionEventRejected(RuntimeError):
    """A delivery lacks the trusted identity required for safe processing."""


class _ReviewRequired(RuntimeError):
    pass


def _payload_meta(payload) -> dict:
    value = payload.get("meta") if isinstance(payload, dict) else None
    return value if isinstance(value, dict) else {}


def _payload_data(payload) -> dict:
    value = payload.get("data") if isinstance(payload, dict) else None
    return value if isinstance(value, dict) else {}


def _payload_attributes(payload) -> dict:
    value = _payload_data(payload).get("attributes")
    return value if isinstance(value, dict) else {}


def _safe_id(value) -> str | None:
    if isinstance(value, int) and not isinstance(value, bool):
        value = str(value)
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value or len(value) > 128 or not set(value) <= _SAFE_ID_CHARS:
        return None
    return value


def _event_name(payload) -> str:
    value = _payload_meta(payload).get("event_name")
    if not isinstance(value, str):
        return ""
    value = value.strip()
    if (
        not value
        or len(value) > 64
        or not value.replace("_", "").isalnum()
    ):
        return ""
    return value


def _provider_invoice_id(payload) -> str | None:
    attributes = _payload_attributes(payload)
    invoice_id = _safe_id(attributes.get("invoice_id"))
    data = _payload_data(payload)
    data_type = str(data.get("type") or "").replace("_", "-").casefold()
    if invoice_id is None and data_type == "subscription-invoices":
        invoice_id = _safe_id(data.get("id"))
    return invoice_id


def subscription_event_key(
    payload, *, verified_body_digest=None, reconciliation_key=None
) -> str:
    """Return a delivery key without ever treating an original order as an invoice."""
    event_name = _event_name(payload)
    if event_name not in SUBSCRIPTION_EVENT_TYPES:
        raise SubscriptionEventRejected("nepodporovaný typ udalosti")
    if reconciliation_key is not None:
        if (
            not isinstance(reconciliation_key, str)
            or not reconciliation_key.strip()
            or len(reconciliation_key) > 512
            or any(character in reconciliation_key for character in "\r\n\0")
        ):
            raise SubscriptionEventRejected("neplatný kľúč rekonciliácie")
        return f"lemon:reconcile:{event_name}:{reconciliation_key.strip()}"
    invoice_id = _provider_invoice_id(payload)
    if event_name in PAYMENT_EVENT_TYPES:
        if invoice_id is None:
            raise SubscriptionEventRejected("platobnej udalosti chýba id faktúry")
        return f"lemon:invoice:{event_name}:{invoice_id}"
    if verified_body_digest is not None:
        if (
            not isinstance(verified_body_digest, str)
            or len(verified_body_digest) != 64
            or not set(verified_body_digest) <= _HEX_CHARS
        ):
            raise SubscriptionEventRejected("neplatný digest overeného tela")
        return f"lemon:webhook:{event_name}:{verified_body_digest.lower()}"
    raise SubscriptionEventRejected("chýba bezpečný kľúč doručenia")


def _attempt_id(payload) -> str | None:
    custom = _payload_meta(payload).get("custom_data")
    if not isinstance(custom, dict):
        return None
    for name in ("attempt_id", "checkout_attempt"):
        value = _safe_id(custom.get(name))
        if value is not None and len(value) >= 43:
            return value
    return None


def _custom_user_id(payload) -> int | None:
    custom = _payload_meta(payload).get("custom_data")
    if not isinstance(custom, dict):
        return None
    value = custom.get("user_id")
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        value = value.strip()
        if not value.isdigit() or len(value) > 18:
            return None
        value = int(value)
    return value if isinstance(value, int) and value > 0 else None


def _resource_type(payload) -> str:
    value = _payload_data(payload).get("type")
    return value.strip().replace("_", "-").casefold() if isinstance(value, str) else ""


def _provider_order_id(payload) -> str | None:
    attributes = _payload_attributes(payload)
    value = _safe_id(attributes.get("order_id"))
    if value is None and _resource_type(payload) == "orders":
        value = _safe_id(_payload_data(payload).get("id"))
    return value


def _provider_subscription_id(payload) -> str | None:
    attributes = _payload_attributes(payload)
    value = _safe_id(attributes.get("subscription_id"))
    if value is None and _resource_type(payload) == "subscriptions":
        value = _safe_id(_payload_data(payload).get("id"))
    return value


def _relationship_id(payload, name: str) -> str | None:
    relationships = _payload_data(payload).get("relationships")
    if not isinstance(relationships, dict):
        return None
    relationship = relationships.get(name)
    data = relationship.get("data") if isinstance(relationship, dict) else None
    return _safe_id(data.get("id")) if isinstance(data, dict) else None


def _provider_store_id(payload) -> str | None:
    return _safe_id(_payload_attributes(payload).get("store_id")) or _relationship_id(
        payload, "store"
    )


def _provider_variant_id(payload) -> str | None:
    attributes = _payload_attributes(payload)
    value = _safe_id(attributes.get("variant_id"))
    first_item = attributes.get("first_order_item")
    if value is None and isinstance(first_item, dict):
        value = _safe_id(first_item.get("variant_id"))
    return value or _relationship_id(payload, "variant")


def _currency(payload) -> str | None:
    value = _payload_attributes(payload).get("currency")
    if (
        not isinstance(value, str)
        or len(value.strip()) != 3
        or not value.strip().isascii()
        or not value.strip().isalpha()
    ):
        return None
    return value.strip().upper()


def _amount(payload, name="total") -> int | None:
    value = _payload_attributes(payload).get(name)
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or value < 0
        or value > 10**9
    ):
        return None
    return value


def _discount_id(payload) -> str | None:
    value = _payload_attributes(payload).get("discount_id")
    return None if value is None else _safe_id(value)


def _timestamp(value) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        result = float(value)
        return result if math.isfinite(result) and result >= 0 else None
    if not isinstance(value, str) or not value.strip():
        return None
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    try:
        parsed = datetime.datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.utcoffset() is None:
        return None
    result = parsed.timestamp()
    return result if math.isfinite(result) and result >= 0 else None


def _provider_revision(payload) -> float:
    revision = _timestamp(_payload_attributes(payload).get("updated_at"))
    if revision is None:
        raise _ReviewRequired("udalosti chýba dôveryhodný updated_at")
    return revision


def _fresh_provider_revision(
    snapshot: SubscriptionSnapshot, payload
) -> float:
    revision = _provider_revision(payload)
    if (
        snapshot.provider_updated_at is not None
        and revision < snapshot.provider_updated_at
    ):
        raise _ReviewRequired("udalosť je staršia než overený stav predplatného")
    return revision


def _period(payload) -> tuple[float, float]:
    attributes = _payload_attributes(payload)
    start = _timestamp(
        attributes.get("billing_period_start", attributes.get("period_start"))
    )
    end = _timestamp(
        attributes.get("billing_period_end", attributes.get("period_end"))
    )
    if start is None or end is None or start >= end:
        raise _ReviewRequired("neplatné obdobie predplatného")
    return start, end


def _row_dict(con, query: str, parameters=()) -> dict | None:
    cursor = con.execute(query, parameters)
    row = cursor.fetchone()
    if row is None:
        return None
    names = [column[0] for column in cursor.description]
    return dict(zip(names, tuple(row)))


def _attempt(con, payload) -> dict:
    public_id = _attempt_id(payload)
    if public_id is None:
        raise _ReviewRequired("chýba bezpečný pokus objednávky")
    row = _row_dict(
        con, "SELECT * FROM checkout_attempts WHERE public_id=?", (public_id,)
    )
    if row is None:
        raise _ReviewRequired("neznámy pokus objednávky")
    return row


def _snapshot_from_values(values: dict) -> SubscriptionSnapshot:
    for name in ("test_mode", "founder", "initial_payment_verified", "needs_review"):
        values[name] = bool(values[name])
    return SubscriptionSnapshot(**values)


def _snapshot_for_subscription(
    con, provider_subscription_id: str, *, test_mode: bool
) -> SubscriptionSnapshot | None:
    cursor = con.execute(
        f"SELECT {','.join(_SNAPSHOT_COLUMNS)} FROM subscriptions "
        "WHERE provider='lemonsqueezy' AND provider_subscription_id=? "
        "AND test_mode=? ORDER BY updated_at DESC,id DESC LIMIT 1",
        (provider_subscription_id, int(test_mode)),
    )
    row = cursor.fetchone()
    if row is None:
        return None
    return _snapshot_from_values(dict(zip(_SNAPSHOT_COLUMNS, tuple(row))))


def _expected_value(expected, name: str):
    if isinstance(expected, Mapping):
        return expected.get(name)
    return getattr(expected, name, None)


def _validated_expected(expected) -> dict:
    values = {
        "store_id": _safe_id(_expected_value(expected, "store_id")),
        "variant_id": _safe_id(_expected_value(expected, "variant_id")),
        "founder_discount_id": _safe_id(
            _expected_value(expected, "founder_discount_id")
        ),
        "currency": _expected_value(expected, "currency"),
        "test_mode": _expected_value(expected, "test_mode"),
    }
    if (
        values["store_id"] is None
        or values["variant_id"] is None
        or values["founder_discount_id"] is None
        or values["currency"] != ANNUAL_PREMIUM_CURRENCY
        or type(values["test_mode"]) is not bool
    ):
        raise SubscriptionEventRejected("neúplná očakávaná konfigurácia predplatného")
    return values


def _validate_common(payload, expected: dict) -> None:
    facts = {
        "store_id": _provider_store_id(payload),
        "variant_id": _provider_variant_id(payload),
        "currency": _currency(payload),
        "test_mode": _payload_attributes(payload).get("test_mode"),
    }
    for name in ("store_id", "variant_id", "currency"):
        if facts[name] != expected[name]:
            raise _ReviewRequired(f"udalosť má neplatné {name}")
    if facts["test_mode"] is not expected["test_mode"]:
        raise _ReviewRequired("udalosť má nesprávny test_mode")


def _validate_attempt(attempt: dict, payload, expected: dict) -> None:
    founder = attempt.get("founder") == 1
    expected_initial = (
        FOUNDER_INITIAL_AMOUNT_CENTS if founder else ANNUAL_RENEWAL_AMOUNT_CENTS
    )
    expected_discount = expected["founder_discount_id"] if founder else None
    if (
        attempt.get("product") != ANNUAL_PREMIUM_PRODUCT
        or attempt.get("currency") != ANNUAL_PREMIUM_CURRENCY
        or attempt.get("billing_interval") != "year"
        or attempt.get("auto_renews") != 1
        or attempt.get("amount_cents") != expected_initial
        or attempt.get("renewal_amount_cents") != ANNUAL_RENEWAL_AMOUNT_CENTS
        or attempt.get("discount_id") != expected_discount
        or attempt.get("test_mode") != int(expected["test_mode"])
        or attempt.get("status") not in {"pending", "paid"}
    ):
        raise _ReviewRequired("pokus nezodpovedá ročnej ponuke")
    user_id = attempt.get("user_id")
    if (
        not isinstance(user_id, int)
        or isinstance(user_id, bool)
        or user_id <= 0
    ):
        raise _ReviewRequired("pokus nemá platného používateľa")
    custom_user_id = _custom_user_id(payload)
    if custom_user_id is not None and custom_user_id != user_id:
        raise _ReviewRequired("udalosť patrí inému používateľovi")


def _validate_user_exists(con, attempt: dict) -> None:
    user_id = attempt.get("user_id")
    if (
        not isinstance(user_id, int)
        or isinstance(user_id, bool)
        or user_id <= 0
        or con.execute(
            "SELECT 1 FROM pouzivatelia WHERE id=?", (user_id,)
        ).fetchone()
        is None
    ):
        raise _ReviewRequired("pokus nemá známeho používateľa")


def _validate_offer(payload, attempt: dict, expected: dict, *, renewal: bool) -> None:
    founder = attempt["founder"] == 1
    wanted_amount = (
        ANNUAL_RENEWAL_AMOUNT_CENTS
        if renewal or not founder
        else FOUNDER_INITIAL_AMOUNT_CENTS
    )
    wanted_discount = expected["founder_discount_id"] if founder and not renewal else None
    if _amount(payload) != wanted_amount:
        raise _ReviewRequired("fakturovaná suma nezodpovedá obdobiu")
    if _discount_id(payload) != wanted_discount:
        raise _ReviewRequired("zľava nezodpovedá obdobiu")


def _pair_attempt(con, attempt: dict, payload, *, require_paid_status: bool) -> None:
    order_id = _provider_order_id(payload)
    if order_id is None:
        raise _ReviewRequired("chýba id objednávky")
    if require_paid_status:
        status = _payload_attributes(payload).get("status")
        if not isinstance(status, str) or status.strip().casefold() != "paid":
            raise _ReviewRequired("objednávka nie je zaplatená")
    stored_order = _safe_id(attempt.get("provider_order_id"))
    if stored_order is not None and stored_order != order_id:
        raise _ReviewRequired("pokus je spárovaný s inou objednávkou")
    if attempt.get("status") == "pending":
        cursor = con.execute(
            "UPDATE checkout_attempts SET status='paid',provider_order_id=? "
            "WHERE public_id=? AND status='pending'",
            (order_id, attempt["public_id"]),
        )
        if cursor.rowcount != 1:
            raise _ReviewRequired("pokus objednávky už nemožno spárovať")
    elif attempt.get("status") != "paid":
        raise _ReviewRequired("pokus objednávky už nemožno spárovať")


def _validate_snapshot_identity(
    snapshot: SubscriptionSnapshot, payload, attempt: dict | None = None
) -> None:
    if snapshot.provider_order_id != _provider_order_id(payload):
        raise _ReviewRequired("udalosť má inú pôvodnú objednávku")
    if snapshot.provider_customer_id != _safe_id(
        _payload_attributes(payload).get("customer_id")
    ):
        raise _ReviewRequired("udalosť má iného zákazníka")
    if attempt is not None and snapshot.user_id != attempt.get("user_id"):
        raise _ReviewRequired("pokus patrí inému predplatnému")


def _validate_existing_provider_identity(
    snapshot: SubscriptionSnapshot,
    *,
    customer_id: str,
    order_id: str,
    subscription_id: str,
    expected: dict,
) -> None:
    identity = (
        snapshot.provider,
        snapshot.provider_customer_id,
        snapshot.provider_order_id,
        snapshot.provider_subscription_id,
        snapshot.provider_variant_id,
        snapshot.currency,
        snapshot.test_mode,
    )
    incoming = (
        "lemonsqueezy",
        customer_id,
        order_id,
        subscription_id,
        expected["variant_id"],
        expected["currency"],
        expected["test_mode"],
    )
    if identity != incoming:
        raise _ReviewRequired(
            "nové predplatné nesmie prepísať overenú identitu poskytovateľa"
        )
    if snapshot.status != "active":
        raise _ReviewRequired(
            "opakované vytvorenie nesmie obnoviť ukončené predplatné"
        )


def _replace_verified_snapshot(
    con, snapshot: SubscriptionSnapshot, *, now: float, **changes
) -> None:
    candidate = replace(
        snapshot,
        needs_review=False,
        review_reason=None,
        **changes,
    )
    if not upsert_snapshot(con, candidate, now=now):
        raise _ReviewRequired("prechod predplatného sa nedá bezpečne uložiť")


def _process_order_created(con, payload, expected: dict, *, now: float) -> dict:
    _validate_common(payload, expected)
    attempt = _attempt(con, payload)
    _validate_attempt(attempt, payload, expected)
    _validate_user_exists(con, attempt)
    _validate_offer(payload, attempt, expected, renewal=False)
    _pair_attempt(con, attempt, payload, require_paid_status=True)
    return {"action": "paired", "user_id": attempt["user_id"]}


def _process_subscription_created(con, payload, expected: dict, *, now: float) -> dict:
    _validate_common(payload, expected)
    attempt = _attempt(con, payload)
    _validate_attempt(attempt, payload, expected)
    _validate_user_exists(con, attempt)
    _validate_offer(payload, attempt, expected, renewal=False)
    _pair_attempt(con, attempt, payload, require_paid_status=False)
    subscription_id = _provider_subscription_id(payload)
    customer_id = _safe_id(_payload_attributes(payload).get("customer_id"))
    order_id = _provider_order_id(payload)
    if subscription_id is None or customer_id is None or order_id is None:
        raise _ReviewRequired("chýba identita predplatného")
    revision = _provider_revision(payload)
    existing = subscription_for_user(con, attempt["user_id"])
    if existing is not None:
        _validate_existing_provider_identity(
            existing,
            customer_id=customer_id,
            order_id=order_id,
            subscription_id=subscription_id,
            expected=expected,
        )
        if (
            existing.provider_updated_at is not None
            and revision < existing.provider_updated_at
        ):
            raise _ReviewRequired(
                "udalosť je staršia než overený stav predplatného"
            )
    status = _payload_attributes(payload).get("status")
    status = status.strip().casefold() if isinstance(status, str) else ""
    if status != "active":
        raise _ReviewRequired("nové predplatné nie je aktívne")
    period_start, period_end = _period(payload)
    renews_at = _timestamp(_payload_attributes(payload).get("renews_at"))
    if renews_at is None or renews_at < period_end:
        raise _ReviewRequired("chýba overený dátum obnovy")
    founder = attempt["founder"] == 1
    snapshot = SubscriptionSnapshot(
        user_id=attempt["user_id"],
        product=ANNUAL_PREMIUM_PRODUCT,
        provider="lemonsqueezy",
        provider_customer_id=customer_id,
        provider_order_id=order_id,
        provider_subscription_id=subscription_id,
        provider_variant_id=expected["variant_id"],
        currency=expected["currency"],
        test_mode=expected["test_mode"],
        status="active",
        period_start=period_start,
        period_end=period_end,
        renews_at=renews_at,
        ends_at=None,
        paid_through=period_end,
        initial_amount_cents=attempt["amount_cents"],
        renewal_amount_cents=attempt["renewal_amount_cents"],
        discount_id=attempt["discount_id"],
        founder=founder,
        initial_payment_verified=True,
        provider_updated_at=revision,
    )
    if not upsert_snapshot(con, snapshot, now=now):
        raise _ReviewRequired("predplatné sa nedá bezpečne aktivovať")
    return {"action": "activated", "user_id": attempt["user_id"]}


def _existing_context(con, payload, expected: dict) -> tuple[SubscriptionSnapshot, dict | None]:
    _validate_common(payload, expected)
    subscription_id = _provider_subscription_id(payload)
    if subscription_id is None:
        raise _ReviewRequired("chýba id predplatného")
    snapshot = _snapshot_for_subscription(
        con, subscription_id, test_mode=expected["test_mode"]
    )
    if snapshot is None:
        raise _ReviewRequired("udalosť patrí neznámemu predplatnému")
    attempt = None
    if _attempt_id(payload) is not None:
        attempt = _attempt(con, payload)
        _validate_attempt(attempt, payload, expected)
        _validate_user_exists(con, attempt)
    _validate_snapshot_identity(snapshot, payload, attempt)
    return snapshot, attempt


def _validate_current_period(snapshot: SubscriptionSnapshot, payload) -> tuple[float, float]:
    period_start, period_end = _period(payload)
    if period_start != snapshot.period_start or period_end != snapshot.period_end:
        raise _ReviewRequired("udalosť mení obdobie bez overenej faktúry")
    return period_start, period_end


def _process_payment_success(con, payload, expected: dict, *, now: float) -> dict:
    snapshot, attempt = _existing_context(con, payload, expected)
    revision = _fresh_provider_revision(snapshot, payload)
    invoice_id = _provider_invoice_id(payload)
    if invoice_id is None:
        raise SubscriptionEventRejected("platobnej udalosti chýba id faktúry")
    count = con.execute(
        "SELECT COUNT(*) FROM subscription_invoices WHERE provider='lemonsqueezy' "
        "AND test_mode=? AND provider_subscription_id=?",
        (int(expected["test_mode"]), snapshot.provider_subscription_id),
    ).fetchone()[0]
    billing_reason = _payload_attributes(payload).get("billing_reason")
    billing_reason = (
        billing_reason.strip().casefold() if isinstance(billing_reason, str) else ""
    )
    invoice_kind = "initial" if count == 0 else "renewal"
    if billing_reason not in {
        invoice_kind,
        "subscription_created" if invoice_kind == "initial" else "renewal",
    }:
        raise _ReviewRequired("dôvod faktúry nezodpovedá poradiu obdobia")
    if attempt is None:
        attempt = _row_dict(
            con,
            "SELECT * FROM checkout_attempts WHERE provider_order_id=?",
            (snapshot.provider_order_id,),
        )
        if attempt is None:
            raise _ReviewRequired("predplatnému chýba pokus objednávky")
        _validate_attempt(attempt, payload, expected)
    renewal = invoice_kind == "renewal"
    _validate_offer(payload, attempt, expected, renewal=renewal)
    period_start, period_end = _period(payload)
    if renewal:
        if period_start != snapshot.paid_through or period_end <= period_start:
            raise _ReviewRequired("obnovovacia faktúra nenadväzuje na zaplatené obdobie")
    elif period_start != snapshot.period_start or period_end != snapshot.period_end:
        raise _ReviewRequired("prvá faktúra nemá prvé obdobie predplatného")
    status = _payload_attributes(payload).get("status")
    if not isinstance(status, str) or status.strip().casefold() != "paid":
        raise _ReviewRequired("faktúra nie je zaplatená")
    renews_at = None
    if renewal:
        renews_at = _timestamp(_payload_attributes(payload).get("renews_at"))
        if renews_at is None or renews_at < period_end:
            raise _ReviewRequired("obnove chýba ďalší dátum obnovy")
    con.execute(
        """INSERT INTO subscription_invoices
           (provider,test_mode,provider_invoice_id,provider_subscription_id,
            provider_order_id,invoice_kind,status,amount_cents,currency,
            period_start,period_end,paid_at,refunded_amount_cents,created_at,updated_at)
           VALUES ('lemonsqueezy',?,?,?,?,?,'paid',?,?,?,?,?,0,?,?)""",
        (
            int(expected["test_mode"]),
            invoice_id,
            snapshot.provider_subscription_id,
            snapshot.provider_order_id,
            invoice_kind,
            _amount(payload),
            expected["currency"],
            period_start,
            period_end,
            now,
            now,
            now,
        ),
    )
    if renewal:
        _replace_verified_snapshot(
            con,
            snapshot,
            now=now,
            status="active",
            period_start=period_start,
            period_end=period_end,
            renews_at=renews_at,
            ends_at=None,
            paid_through=period_end,
            provider_updated_at=revision,
        )
    else:
        _replace_verified_snapshot(
            con,
            snapshot,
            now=now,
            provider_updated_at=revision,
        )
    return {"action": "invoice_recorded", "user_id": snapshot.user_id}


def _process_dunning(con, payload, expected: dict, *, now: float, recovered: bool) -> dict:
    snapshot, _attempt_row = _existing_context(con, payload, expected)
    revision = _fresh_provider_revision(snapshot, payload)
    _validate_current_period(snapshot, payload)
    attributes = _payload_attributes(payload)
    invoice_status = attributes.get("status")
    invoice_status = (
        invoice_status.strip().casefold() if isinstance(invoice_status, str) else ""
    )
    provider_status = attributes.get("subscription_status")
    provider_status = (
        provider_status.strip().casefold() if isinstance(provider_status, str) else ""
    )
    wanted_invoice = "paid" if recovered else "failed"
    wanted_subscription = "active" if recovered else "past_due"
    if invoice_status != wanted_invoice or provider_status != wanted_subscription:
        raise _ReviewRequired("stav záchrany platby nie je overený")
    if recovered:
        if snapshot.status not in {"active", "past_due", "unpaid", "expired"}:
            raise _ReviewRequired("stav predplatného neumožňuje obnovu platby")
    elif snapshot.status not in {"active", "past_due"}:
        raise _ReviewRequired("stav predplatného neumožňuje dunning")
    _replace_verified_snapshot(
        con,
        snapshot,
        now=now,
        status=wanted_subscription,
        provider_updated_at=revision,
    )
    return {
        "action": "payment_recovered" if recovered else "payment_failed",
        "user_id": snapshot.user_id,
    }


def _process_refund(con, payload, expected: dict, *, now: float) -> dict:
    snapshot, _attempt_row = _existing_context(con, payload, expected)
    invoice_id = _provider_invoice_id(payload)
    invoice = _row_dict(
        con,
        "SELECT * FROM subscription_invoices WHERE provider='lemonsqueezy' "
        "AND test_mode=? AND provider_invoice_id=?",
        (int(expected["test_mode"]), invoice_id),
    )
    if invoice is None:
        raise _ReviewRequired("refundácia nemá známu faktúru")
    period_start, period_end = _period(payload)
    if (
        invoice["provider_subscription_id"] != snapshot.provider_subscription_id
        or invoice["provider_order_id"] != snapshot.provider_order_id
        or invoice["amount_cents"] != _amount(payload)
        or invoice["currency"] != _currency(payload)
        or invoice["period_start"] != period_start
        or invoice["period_end"] != period_end
    ):
        raise _ReviewRequired("refundácia nezodpovedá presnej faktúre")
    refunded = _amount(payload, "refunded_amount")
    if (
        refunded is None
        or refunded <= 0
        or refunded > invoice["amount_cents"]
        or refunded < invoice["refunded_amount_cents"]
    ):
        raise _ReviewRequired("refundovaná suma nie je platná")
    status = _payload_attributes(payload).get("status")
    status = status.strip().casefold() if isinstance(status, str) else ""
    wanted_status = "refunded" if refunded == invoice["amount_cents"] else "partial_refund"
    if status != wanted_status:
        raise _ReviewRequired("stav refundácie nezodpovedá sume")
    con.execute(
        "UPDATE subscription_invoices SET status=?,refunded_amount_cents=?,updated_at=? "
        "WHERE id=?",
        (wanted_status, refunded, now, invoice["id"]),
    )
    return {"action": wanted_status, "user_id": snapshot.user_id}


def _process_subscription_transition(
    con, payload, expected: dict, *, now: float, event_name: str
) -> dict:
    snapshot, _attempt_row = _existing_context(con, payload, expected)
    revision = _fresh_provider_revision(snapshot, payload)
    _validate_current_period(snapshot, payload)
    attributes = _payload_attributes(payload)
    status = attributes.get("status")
    status = status.strip().casefold() if isinstance(status, str) else ""
    if event_name == "subscription_paused" or status == "paused":
        raise _ReviewRequired("pozastavené predplatné vyžaduje kontrolu")
    if status not in KNOWN_STATUSES:
        raise _ReviewRequired("neznámy alebo neúplný stav predplatného")

    changes = {}
    if event_name == "subscription_cancelled" or status == "cancelled":
        if snapshot.status not in {"active", "past_due", "cancelled"}:
            raise _ReviewRequired("stav predplatného neumožňuje zrušenie")
        ends_at = _timestamp(attributes.get("ends_at"))
        if status != "cancelled" or ends_at != snapshot.paid_through:
            raise _ReviewRequired("zrušenie nemá overený koniec prístupu")
        changes = {"status": "cancelled", "renews_at": None, "ends_at": ends_at}
    elif event_name == "subscription_resumed":
        renews_at = _timestamp(attributes.get("renews_at"))
        if (
            status != "active"
            or snapshot.status != "cancelled"
            or snapshot.ends_at is None
            or now >= snapshot.ends_at
            or renews_at is None
        ):
            raise _ReviewRequired("predplatné nemožno bezpečne obnoviť")
        changes = {"status": "active", "renews_at": renews_at, "ends_at": None}
    elif event_name == "subscription_expired" or status == "expired":
        if snapshot.status not in {
            "active",
            "past_due",
            "unpaid",
            "cancelled",
            "expired",
        }:
            raise _ReviewRequired("stav predplatného neumožňuje expiráciu")
        if status != "expired":
            raise _ReviewRequired("expirácia nie je overená")
        changes = {"status": "expired", "renews_at": None, "ends_at": snapshot.ends_at}
    elif event_name == "subscription_unpaused":
        if status != "active" or snapshot.status not in {"active", "past_due"}:
            raise _ReviewRequired("obnovený stav po pauze nie je aktívny")
        renews_at = _timestamp(attributes.get("renews_at"))
        if renews_at is None:
            raise _ReviewRequired("po pauze chýba dátum obnovy")
        changes = {"status": "active", "renews_at": renews_at, "ends_at": None}
    elif event_name in {"subscription_updated"}:
        if status in {"active", "past_due"}:
            allowed_from = {"active"} if status == "active" else {"active", "past_due"}
            if snapshot.status not in allowed_from:
                raise _ReviewRequired(
                    "všeobecná aktualizácia nesmie obnoviť ukončené predplatné"
                )
            renews_at = _timestamp(attributes.get("renews_at"))
            if renews_at is None:
                raise _ReviewRequired("aktívnemu stavu chýba dátum obnovy")
            changes = {"status": status, "renews_at": renews_at}
        elif status == "unpaid":
            if snapshot.status not in {"active", "past_due", "unpaid"}:
                raise _ReviewRequired("stav predplatného neumožňuje unpaid prechod")
            changes = {"status": "unpaid"}
        else:
            raise _ReviewRequired("nepodporovaný aktualizačný prechod")
    else:
        raise _ReviewRequired("nepodporovaný prechod predplatného")
    _replace_verified_snapshot(
        con, snapshot, now=now, provider_updated_at=revision, **changes
    )
    return {"action": status, "user_id": snapshot.user_id}


def _event_payload_json(payload) -> str:
    if not isinstance(payload, dict):
        raise SubscriptionEventRejected("udalosť nie je platný JSON objekt")
    attributes = _payload_attributes(payload)
    resource_type = _resource_type(payload)
    projection = {
        "event_name": _event_name(payload),
        "resource_type": (
            resource_type
            if resource_type in {"orders", "subscriptions", "subscription-invoices"}
            else None
        ),
        "resource_id": _safe_id(_payload_data(payload).get("id")),
        "provider_order_id": _provider_order_id(payload),
        "provider_subscription_id": _provider_subscription_id(payload),
        "provider_invoice_id": _provider_invoice_id(payload),
        "store_id": _provider_store_id(payload),
        "variant_id": _provider_variant_id(payload),
        "currency": _currency(payload),
        "test_mode": (
            attributes.get("test_mode")
            if type(attributes.get("test_mode")) is bool
            else None
        ),
        "status": _safe_id(attributes.get("status")),
        "subscription_status": _safe_id(attributes.get("subscription_status")),
        "billing_reason": _safe_id(attributes.get("billing_reason")),
        "amount_cents": _amount(payload),
        "refunded_amount_cents": _amount(payload, "refunded_amount"),
        "discount_present": attributes.get("discount_id") is not None,
        "period_start": _timestamp(
            attributes.get("billing_period_start", attributes.get("period_start"))
        ),
        "period_end": _timestamp(
            attributes.get("billing_period_end", attributes.get("period_end"))
        ),
        "provider_updated_at": _timestamp(attributes.get("updated_at")),
    }
    return json.dumps(
        {name: value for name, value in projection.items() if value is not None},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _insert_delivery_event(
    con,
    *,
    event_key: str,
    payload,
    event_name: str,
    source: str,
    now: float,
) -> bool:
    attributes = _payload_attributes(payload)
    test_mode = attributes.get("test_mode")
    test_mode = int(test_mode) if type(test_mode) is bool else None
    cursor = con.execute(
        """INSERT INTO subscription_events
           (event_key,provider,test_mode,provider_event_id,
            provider_subscription_id,provider_invoice_id,event_type,source,
            payload_json,processing_status,needs_review,review_reason,
            received_at,processed_at,updated_at)
           VALUES (?,'lemonsqueezy',?,NULL,?,?,?, ?,?,'processing',0,NULL,?,NULL,?)
           ON CONFLICT(event_key) DO NOTHING""",
        (
            event_key,
            test_mode,
            _provider_subscription_id(payload),
            _provider_invoice_id(payload),
            event_name,
            source,
            _event_payload_json(payload),
            now,
            now,
        ),
    )
    return cursor.rowcount == 1


def _retry_reviewed_delivery(
    con,
    *,
    event_key: str,
    payload,
    event_name: str,
    source: str,
    now: float,
) -> None:
    attributes = _payload_attributes(payload)
    test_mode = attributes.get("test_mode")
    test_mode = int(test_mode) if type(test_mode) is bool else None
    con.execute(
        """UPDATE subscription_events
              SET test_mode=?,provider_subscription_id=?,provider_invoice_id=?,
                  event_type=?,source=?,payload_json=?,processing_status='processing',
                  needs_review=0,review_reason=NULL,processed_at=NULL,updated_at=?
            WHERE event_key=? AND processing_status!='processed'""",
        (
            test_mode,
            _provider_subscription_id(payload),
            _provider_invoice_id(payload),
            event_name,
            source,
            _event_payload_json(payload),
            now,
            event_key,
        ),
    )


def _mark_delivery(
    con,
    event_key: str,
    *,
    now: float,
    status: str,
    review_reason: str | None = None,
) -> None:
    con.execute(
        "UPDATE subscription_events SET processing_status=?,needs_review=?,"
        "review_reason=?,processed_at=?,updated_at=? WHERE event_key=?",
        (
            status,
            int(review_reason is not None),
            review_reason,
            now,
            now,
            event_key,
        ),
    )


def process_subscription_event(
    con, *, payload, now, expected, source, delivery_key
) -> dict:
    """Validate and apply one annual lifecycle event in one idempotent transaction."""
    if not isinstance(payload, dict):
        raise SubscriptionEventRejected("telo udalosti nie je objekt")
    if not _valid_time(now):
        raise ValueError("neplatný čas udalosti")
    now = float(now)
    expected = _validated_expected(expected)
    if source in {"webhook", "odlozene"}:
        event_key = subscription_event_key(
            payload, verified_body_digest=delivery_key
        )
    elif source in {"reconciliation", "rekonciliacia"}:
        event_key = subscription_event_key(
            payload, reconciliation_key=delivery_key
        )
    else:
        raise SubscriptionEventRejected("neznámy zdroj udalosti")
    event_name = _event_name(payload)

    # SAVEPOINT owns only this delivery.  On a clean connection RELEASE commits
    # it; inside a caller transaction RELEASE never commits the caller's work.
    outer_savepoint = "annual_subscription_delivery"
    domain_savepoint = "annual_subscription_domain"
    con.execute(f"SAVEPOINT {outer_savepoint}")
    try:
        inserted = _insert_delivery_event(
            con,
            event_key=event_key,
            payload=payload,
            event_name=event_name,
            source=source,
            now=now,
        )
        if not inserted:
            previous = con.execute(
                "SELECT processing_status FROM subscription_events WHERE event_key=?",
                (event_key,),
            ).fetchone()
            if previous is not None and previous[0] == "processed":
                con.execute(f"RELEASE SAVEPOINT {outer_savepoint}")
                return {
                    "duplicate": True,
                    "review_required": False,
                    "event_type": event_name,
                    "action": "duplicate",
                    "user_id": None,
                }
            _retry_reviewed_delivery(
                con,
                event_key=event_key,
                payload=payload,
                event_name=event_name,
                source=source,
                now=now,
            )
        con.execute(f"SAVEPOINT {domain_savepoint}")
        try:
            if event_name == "order_created":
                result = _process_order_created(con, payload, expected, now=now)
            elif event_name == "subscription_created":
                result = _process_subscription_created(con, payload, expected, now=now)
            elif event_name == "subscription_payment_success":
                result = _process_payment_success(con, payload, expected, now=now)
            elif event_name == "subscription_payment_failed":
                result = _process_dunning(
                    con, payload, expected, now=now, recovered=False
                )
            elif event_name == "subscription_payment_recovered":
                result = _process_dunning(
                    con, payload, expected, now=now, recovered=True
                )
            elif event_name in {"subscription_payment_refunded", "order_refunded"}:
                result = _process_refund(con, payload, expected, now=now)
            else:
                result = _process_subscription_transition(
                    con, payload, expected, now=now, event_name=event_name
                )
        except _ReviewRequired as error:
            con.execute(f"ROLLBACK TO SAVEPOINT {domain_savepoint}")
            con.execute(f"RELEASE SAVEPOINT {domain_savepoint}")
            _mark_delivery(
                con,
                event_key,
                now=now,
                status="requires_review",
                review_reason=str(error),
            )
            con.execute(f"RELEASE SAVEPOINT {outer_savepoint}")
            return {
                "duplicate": False,
                "review_required": True,
                "event_type": event_name,
                "action": "requires_review",
                "user_id": None,
            }
        con.execute(f"RELEASE SAVEPOINT {domain_savepoint}")
        _mark_delivery(con, event_key, now=now, status="processed")
        con.execute(f"RELEASE SAVEPOINT {outer_savepoint}")
        return {
            "duplicate": False,
            "review_required": False,
            "event_type": event_name,
            **result,
        }
    except Exception:
        try:
            con.execute(f"ROLLBACK TO SAVEPOINT {outer_savepoint}")
            con.execute(f"RELEASE SAVEPOINT {outer_savepoint}")
        except sqlite3.OperationalError:
            pass
        raise


def is_subscription_event(con, payload) -> bool:
    """Identify annual events while preserving historical one-time cancellation."""
    event_name = _event_name(payload)
    if event_name not in SUBSCRIPTION_EVENT_TYPES:
        return False
    custom = _payload_meta(payload).get("custom_data")
    if isinstance(custom, dict) and (
        "attempt_id" in custom or "checkout_attempt" in custom
    ):
        public_id = _attempt_id(payload)
        if public_id is not None:
            row = con.execute(
                "SELECT product FROM checkout_attempts WHERE public_id=?",
                (public_id,),
            ).fetchone()
            if row is not None:
                return row[0] == ANNUAL_PREMIUM_PRODUCT
        # Task 2's provider contract owns this new key.  The old
        # ``checkout_attempt`` key remains available to historical one-time
        # purchases and must not make an unknown delivery look annual.
        return "attempt_id" in custom
    resource_type = _resource_type(payload)
    if resource_type == "subscription-invoices":
        return True
    subscription_id = _provider_subscription_id(payload)
    test_mode = _payload_attributes(payload).get("test_mode")
    if subscription_id is not None and type(test_mode) is bool:
        if con.execute(
            "SELECT 1 FROM subscriptions WHERE provider='lemonsqueezy' "
            "AND provider_subscription_id=? AND test_mode=?",
            (subscription_id, int(test_mode)),
        ).fetchone() is not None:
            return True
    if event_name == "subscription_created" and resource_type == "subscriptions":
        return True
    if event_name == "order_refunded" and _provider_invoice_id(payload) is not None:
        return True
    return False
