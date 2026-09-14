"""Isolated Test-mode evidence for future subscription webhook mechanics.

This module is deliberately not an entitlement implementation.  It owns two
private tables, accepts only a dedicated signed Test-mode daily probe, and
never calls the annual subscription domain or changes customer access.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import json
import math
import re
import secrets
from dataclasses import dataclass, field
from typing import Callable


PROBE_EVIDENCE_SOURCE = "test_mode_daily_probe"
PROBE_TOKEN_FIELD = "lifecycle_probe_token"
PROBE_MAX_BODY_BYTES = 64 * 1024
PROBE_MARKER_SCHEMA_VERSION = 5
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_PRICE_RE = re.compile(r"^[1-9][0-9]{0,8}$")

_SUPPORTED_EVENTS = frozenset({
    "subscription_created",
    "subscription_payment_success",
    "subscription_payment_failed",
    "subscription_payment_recovered",
    "subscription_cancelled",
    "subscription_resumed",
    "subscription_expired",
    "subscription_payment_refunded",
    "order_refunded",
})
_FUTURE_EVENTS = frozenset({
    "subscription_payment_failed",
    "subscription_payment_recovered",
    "subscription_cancelled",
    "subscription_resumed",
    "subscription_expired",
    "subscription_payment_refunded",
    "order_refunded",
})

PROBE_SCHEMA = """
CREATE TABLE IF NOT EXISTS subscription_lifecycle_probe_runs (
  token_digest TEXT PRIMARY KEY,
  config_fingerprint TEXT NOT NULL,
  state TEXT NOT NULL,
  provider_verified INTEGER NOT NULL DEFAULT 0,
  genuine_renewal_verified INTEGER NOT NULL DEFAULT 0,
  provider_subscription_id TEXT,
  provider_order_id TEXT,
  provider_renewal_invoice_id TEXT,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL,
  evidenced_at REAL,
  marker_attestation_id TEXT
);
CREATE TABLE IF NOT EXISTS subscription_lifecycle_probe_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  token_digest TEXT NOT NULL,
  event_key TEXT NOT NULL,
  body_digest TEXT NOT NULL,
  event_type TEXT NOT NULL,
  billing_reason TEXT,
  provider_subscription_id TEXT NOT NULL,
  provider_order_id TEXT,
  provider_invoice_id TEXT,
  status TEXT,
  amount_cents INTEGER,
  currency TEXT,
  period_start REAL,
  period_end REAL,
  source_queue_id INTEGER,
  received_at REAL NOT NULL,
  UNIQUE(token_digest, event_key),
  FOREIGN KEY(token_digest)
    REFERENCES subscription_lifecycle_probe_runs(token_digest) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS subscription_lifecycle_probe_events_run_idx
  ON subscription_lifecycle_probe_events(token_digest, id);
"""


class ProbeConfigError(ValueError):
    """The daily Test-mode probe identity is missing, malformed or colliding."""


class ProbeEventRejected(ValueError):
    """A signed body cannot safely count as evidence for the isolated probe."""


class ProbeCleanupRejected(ValueError):
    """Exact-run cleanup was not explicitly and safely authorized."""


@dataclass(frozen=True)
class LifecycleProbeConfig:
    api_key: str = field(repr=False)
    store_id: str
    variant_id: str
    webhook_secret: str = field(repr=False)
    price_cents: int
    test_mode: bool = True


@dataclass(frozen=True)
class ProbeRun:
    token: str = field(repr=False)
    token_digest: str


def _required_text(value, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ProbeConfigError(f"chýba {name}")
    return value.strip()


def _safe_id(value) -> str | None:
    if isinstance(value, int) and not isinstance(value, bool):
        value = str(value)
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value if _SAFE_ID_RE.fullmatch(value) else None


def _strict_price(value) -> int:
    if type(value) is int:
        price = value
    elif isinstance(value, str) and _PRICE_RE.fullmatch(value.strip()):
        price = int(value.strip())
    else:
        raise ProbeConfigError("cena denného probe musí byť celé číslo centov")
    if price <= 0 or price > 10**8:
        raise ProbeConfigError("cena denného probe je mimo povoleného rozsahu")
    return price


def validate_probe_config(
    config: LifecycleProbeConfig,
    *,
    annual_test_variant_id: str,
    live_webhook_secret: str,
    annual_test_webhook_secret: str,
) -> LifecycleProbeConfig:
    if not isinstance(config, LifecycleProbeConfig) or config.test_mode is not True:
        raise ProbeConfigError("probe musí byť výlučne v Test mode")
    api_key = _required_text(config.api_key, "testovací API kľúč")
    store_id = _safe_id(_required_text(config.store_id, "testovací obchod"))
    variant_id = _safe_id(_required_text(config.variant_id, "denný probe variant"))
    if store_id is None or variant_id is None:
        raise ProbeConfigError("probe obchod alebo variant má neplatný formát")
    webhook_secret = _required_text(
        config.webhook_secret, "samostatné probe webhook tajomstvo"
    )
    annual_variant = _required_text(
        annual_test_variant_id, "ročný testovací variant"
    )
    live_secret = _required_text(live_webhook_secret, "živé webhook tajomstvo")
    annual_test_secret = _required_text(
        annual_test_webhook_secret, "ročné testovacie webhook tajomstvo"
    )
    if hmac.compare_digest(variant_id, annual_variant):
        raise ProbeConfigError("denný probe nesmie použiť ročný variant")
    if hmac.compare_digest(webhook_secret, live_secret) or hmac.compare_digest(
        webhook_secret, annual_test_secret
    ):
        raise ProbeConfigError("probe webhook musí mať samostatné tajomstvo")
    return LifecycleProbeConfig(
        api_key=api_key,
        store_id=store_id,
        variant_id=variant_id,
        webhook_secret=webhook_secret,
        price_cents=_strict_price(config.price_cents),
    )


def read_probe_config(
    getenv: Callable[[str, str], str | None],
    *,
    annual_test_variant_id: str,
    live_webhook_secret: str,
    annual_test_webhook_secret: str,
) -> LifecycleProbeConfig:
    if not callable(getenv):
        raise ProbeConfigError("chýba čítačka konfigurácie")

    def read(name: str) -> str:
        try:
            value = getenv(name, "")
        except TypeError:
            value = getenv(name)
        return value if isinstance(value, str) else ""

    candidate = LifecycleProbeConfig(
        api_key=read("LEMON_TEST_API_KEY"),
        store_id=read("LEMON_TEST_STORE_ID"),
        variant_id=read("LEMON_TEST_LIFECYCLE_PROBE_VARIANT_ID"),
        webhook_secret=read("LEMON_TEST_LIFECYCLE_PROBE_WEBHOOK_SECRET"),
        price_cents=_strict_price(
            read("LEMON_TEST_LIFECYCLE_PROBE_PRICE_CENTS")
        ),
    )
    return validate_probe_config(
        candidate,
        annual_test_variant_id=annual_test_variant_id,
        live_webhook_secret=live_webhook_secret,
        annual_test_webhook_secret=annual_test_webhook_secret,
    )


def _basic_config(config: LifecycleProbeConfig) -> LifecycleProbeConfig:
    if not isinstance(config, LifecycleProbeConfig) or config.test_mode is not True:
        raise ProbeConfigError("probe musí byť výlučne v Test mode")
    store_id = _safe_id(_required_text(config.store_id, "testovací obchod"))
    variant_id = _safe_id(_required_text(config.variant_id, "denný probe variant"))
    if store_id is None or variant_id is None:
        raise ProbeConfigError("probe obchod alebo variant má neplatný formát")
    return LifecycleProbeConfig(
        api_key=_required_text(config.api_key, "testovací API kľúč"),
        store_id=store_id,
        variant_id=variant_id,
        webhook_secret=_required_text(
            config.webhook_secret, "samostatné probe webhook tajomstvo"
        ),
        price_cents=_strict_price(config.price_cents),
    )


def probe_config_fingerprint(
    config: LifecycleProbeConfig, *, signing_secret: str
) -> str:
    """Bind every probe input without exposing any of it in signed evidence."""
    config = _basic_config(config)
    key = _required_text(signing_secret, "podpisové tajomstvo").encode()
    payload = json.dumps(
        {
            "api_key": config.api_key,
            "store_id": config.store_id,
            "variant_id": config.variant_id,
            "webhook_secret": config.webhook_secret,
            "price_cents": config.price_cents,
            "currency": "EUR",
            "billing_interval": "day",
            "billing_interval_count": 1,
            "trial_days": 0,
            "discount_applied_cents": 0,
            "test_mode": True,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hmac.new(
        key, b"uvarsi-test-lifecycle-probe-v1\0" + payload, hashlib.sha256
    ).hexdigest()


def migrate_probe_schema(con) -> None:
    """Create only private probe tables; customer and entitlement tables stay untouched."""
    con.executescript(PROBE_SCHEMA)
    event_columns = {
        row[1] for row in con.execute(
            "PRAGMA table_info(subscription_lifecycle_probe_events)"
        ).fetchall()
    }
    if "source_queue_id" not in event_columns:
        con.execute(
            "ALTER TABLE subscription_lifecycle_probe_events "
            "ADD COLUMN source_queue_id INTEGER"
        )


def _token_digest(token: str) -> str:
    if not isinstance(token, str) or len(token) < 43 or len(token) > 256:
        raise ProbeEventRejected("neplatný párovací token probe")
    return hashlib.sha256(token.encode()).hexdigest()


def _validated_token_digest(value: str) -> str:
    if not isinstance(value, str) or not _DIGEST_RE.fullmatch(value):
        raise ProbeEventRejected("neplatný odtlačok probe behu")
    return value


def _valid_time(value) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("neplatný čas probe")
    result = float(value)
    if not math.isfinite(result) or result < 0:
        raise ValueError("neplatný čas probe")
    return result


def create_probe_run(
    con,
    *,
    config: LifecycleProbeConfig,
    signing_secret: str,
    now,
    token: str | None = None,
) -> ProbeRun:
    config = _basic_config(config)
    now = _valid_time(now)
    token = token or secrets.token_urlsafe(48)
    digest = _token_digest(token)
    fingerprint = probe_config_fingerprint(config, signing_secret=signing_secret)
    con.execute(
        """INSERT INTO subscription_lifecycle_probe_runs
           (token_digest,config_fingerprint,state,created_at,updated_at)
           VALUES (?,?,'collecting',?,?)""",
        (digest, fingerprint, now, now),
    )
    con.commit()
    return ProbeRun(token=token, token_digest=digest)


def _payload_parts(payload) -> tuple[dict, dict, dict]:
    if not isinstance(payload, dict):
        raise ProbeEventRejected("probe webhook nie je objekt")
    meta = payload.get("meta")
    data = payload.get("data")
    attributes = data.get("attributes") if isinstance(data, dict) else None
    if not isinstance(meta, dict) or not isinstance(data, dict) or not isinstance(
        attributes, dict
    ):
        raise ProbeEventRejected("probe webhook má neplatný tvar")
    return meta, data, attributes


def _event_name(meta: dict) -> str:
    value = meta.get("event_name")
    if not isinstance(value, str) or value not in _SUPPORTED_EVENTS:
        raise ProbeEventRejected("nepodporovaný typ probe udalosti")
    return value


def _id_from(attributes: dict, data: dict, name: str, resource: str) -> str | None:
    value = _safe_id(attributes.get(name))
    data_type = str(data.get("type") or "").replace("_", "-").casefold()
    if value is None and data_type == resource:
        value = _safe_id(data.get("id"))
    return value


def _amount(attributes: dict, name: str = "total") -> int | None:
    value = attributes.get(name)
    if type(value) is not int or value < 0 or value > 10**9:
        return None
    return value


def _currency(attributes: dict) -> str | None:
    value = attributes.get("currency")
    if not isinstance(value, str) or len(value.strip()) != 3:
        return None
    value = value.strip().upper()
    return value if value.isascii() and value.isalpha() else None


def _timestamp(value) -> float | None:
    if not isinstance(value, str) or not value.strip():
        return None
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    try:
        parsed = dt.datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.utcoffset() is None:
        return None
    return parsed.timestamp()


def _event_fingerprint(
    *, signing_secret: str, event_type: str, provider_identity: str
) -> str:
    key = _required_text(signing_secret, "podpisové tajomstvo").encode()
    return hmac.new(
        key,
        b"uvarsi-probe-event-v1\0"
        + event_type.encode()
        + b"\0"
        + provider_identity.encode(),
        hashlib.sha256,
    ).hexdigest()


def _event_exists(con, token_digest: str, event_type: str, *, reason=None) -> bool:
    query = (
        "SELECT 1 FROM subscription_lifecycle_probe_events "
        "WHERE token_digest=? AND event_type=?"
    )
    values: tuple[object, ...] = (token_digest, event_type)
    if reason is not None:
        query += " AND billing_reason=?"
        values += (reason,)
    return con.execute(query + " LIMIT 1", values).fetchone() is not None


def ingest_signed_probe_event(
    con,
    *,
    body: bytes,
    signature: str,
    config: LifecycleProbeConfig,
    signing_secret: str,
    now,
    source_queue_id: int | None = None,
) -> dict:
    """Validate and record one signed probe fact without retaining its raw body."""
    config = _basic_config(config)
    now = _valid_time(now)
    if source_queue_id is not None and (
        type(source_queue_id) is not int or source_queue_id < 1
    ):
        raise ProbeEventRejected("neplatný riadok probe fronty")
    if not isinstance(body, (bytes, bytearray)) or not body or len(body) > PROBE_MAX_BODY_BYTES:
        raise ProbeEventRejected("probe webhook má neplatnú veľkosť")
    body = bytes(body)
    if not isinstance(signature, str) or not _DIGEST_RE.fullmatch(signature.strip()):
        raise ProbeEventRejected("probe webhook nemá platný podpis")
    expected_signature = hmac.new(
        config.webhook_secret.encode(), body, hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(signature.strip(), expected_signature):
        raise ProbeEventRejected("podpis probe webhooku nesedí")
    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        raise ProbeEventRejected("probe webhook nie je platný JSON") from None
    meta, data, attributes = _payload_parts(payload)
    event_type = _event_name(meta)
    custom = meta.get("custom_data")
    token = custom.get(PROBE_TOKEN_FIELD) if isinstance(custom, dict) else None
    token_digest = _token_digest(token)
    row = con.execute(
        "SELECT * FROM subscription_lifecycle_probe_runs WHERE token_digest=?",
        (token_digest,),
    ).fetchone()
    if row is None or row["state"] not in {"collecting", "ready"}:
        raise ProbeEventRejected("probe udalosť patrí neznámemu behu")
    fingerprint = probe_config_fingerprint(config, signing_secret=signing_secret)
    if not hmac.compare_digest(row["config_fingerprint"], fingerprint):
        raise ProbeEventRejected("probe konfigurácia sa od vytvorenia zmenila")
    if attributes.get("test_mode") is not True:
        raise ProbeEventRejected("probe udalosť nie je v Test mode")
    if _safe_id(attributes.get("store_id")) != config.store_id:
        raise ProbeEventRejected("probe udalosť patrí inému obchodu")
    variant = _safe_id(attributes.get("variant_id"))
    if variant is None:
        first_item = attributes.get("first_order_item")
        variant = _safe_id(first_item.get("variant_id")) if isinstance(first_item, dict) else None
    if variant != config.variant_id:
        raise ProbeEventRejected("probe udalosť patrí inému variantu")
    if _currency(attributes) != "EUR":
        raise ProbeEventRejected("probe udalosť má inú menu")

    subscription_id = _id_from(
        attributes, data, "subscription_id", "subscriptions"
    )
    order_id = _id_from(attributes, data, "order_id", "orders")
    invoice_id = _id_from(
        attributes, data, "invoice_id", "subscription-invoices"
    )
    if subscription_id is None:
        raise ProbeEventRejected("probe udalosti chýba predplatné")
    if row["provider_subscription_id"] not in (None, subscription_id):
        raise ProbeEventRejected("probe udalosť patrí inému predplatnému")
    if row["provider_order_id"] is not None and order_id != row["provider_order_id"]:
        raise ProbeEventRejected("probe udalosť patrí inej objednávke")

    billing_reason = attributes.get("billing_reason")
    billing_reason = (
        billing_reason.strip().casefold() if isinstance(billing_reason, str) else None
    )
    amount = _amount(attributes)
    payment_event = event_type.startswith("subscription_payment_") or event_type == "order_refunded"
    if payment_event and amount != config.price_cents:
        raise ProbeEventRejected("suma probe udalosti nesedí")
    if event_type == "subscription_payment_success":
        if billing_reason not in {"initial", "subscription_created", "renewal"}:
            raise ProbeEventRejected("dôvod probe platby nie je dôveryhodný")
        if billing_reason in {"initial", "subscription_created"} and not _event_exists(
            con, token_digest, "subscription_created"
        ):
            raise ProbeEventRejected("prvá probe platba prišla pred vytvorením predplatného")
        if billing_reason == "renewal" and not _event_exists(
            con, token_digest, "subscription_payment_success", reason="initial"
        ) and not _event_exists(
            con, token_digest, "subscription_payment_success", reason="subscription_created"
        ):
            raise ProbeEventRejected("obnova prišla pred prvou probe platbou")
    if event_type in _FUTURE_EVENTS and row["genuine_renewal_verified"] != 1:
        raise ProbeEventRejected("budúca udalosť čaká na overenú dennú obnovu")
    if event_type == "subscription_payment_recovered" and not _event_exists(
        con, token_digest, "subscription_payment_failed"
    ):
        raise ProbeEventRejected("zotavenie prišlo pred zlyhaním platby")
    if event_type == "subscription_resumed" and not _event_exists(
        con, token_digest, "subscription_cancelled"
    ):
        raise ProbeEventRejected("obnovenie prišlo pred zrušením")
    if event_type == "subscription_expired" and not _event_exists(
        con, token_digest, "subscription_cancelled"
    ):
        raise ProbeEventRejected("ukončenie prišlo bez predchádzajúceho zrušenia")
    if event_type in {"subscription_payment_refunded", "order_refunded"} and _amount(
        attributes, "refunded_amount"
    ) != config.price_cents:
        raise ProbeEventRejected("probe refundácia nie je úplná")

    identity = invoice_id if payment_event else subscription_id
    if identity is None:
        raise ProbeEventRejected("probe udalosti chýba identifikátor")
    event_key = _event_fingerprint(
        signing_secret=signing_secret,
        event_type=event_type,
        provider_identity=identity,
    )
    if con.execute(
        "SELECT 1 FROM subscription_lifecycle_probe_events WHERE token_digest=? AND event_key=?",
        (token_digest, event_key),
    ).fetchone():
        return {"accepted": True, "duplicate": True, "event_type": event_type}

    period_start = _timestamp(attributes.get("billing_period_start"))
    period_end = _timestamp(attributes.get("billing_period_end"))
    status = _safe_id(attributes.get("status"))
    con.execute("SAVEPOINT lifecycle_probe_event")
    try:
        con.execute(
            """INSERT INTO subscription_lifecycle_probe_events
               (token_digest,event_key,body_digest,event_type,billing_reason,
                provider_subscription_id,provider_order_id,provider_invoice_id,
                status,amount_cents,currency,period_start,period_end,
                source_queue_id,received_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                token_digest,
                event_key,
                hashlib.sha256(body).hexdigest(),
                event_type,
                billing_reason,
                subscription_id,
                order_id,
                invoice_id,
                status,
                amount,
                _currency(attributes),
                period_start,
                period_end,
                source_queue_id,
                now,
            ),
        )
        con.execute(
            """UPDATE subscription_lifecycle_probe_runs
                  SET provider_subscription_id=COALESCE(provider_subscription_id,?),
                      provider_order_id=COALESCE(provider_order_id,?),updated_at=?
                WHERE token_digest=?""",
            (subscription_id, order_id, now, token_digest),
        )
        con.execute("RELEASE SAVEPOINT lifecycle_probe_event")
        con.commit()
    except Exception:
        con.execute("ROLLBACK TO SAVEPOINT lifecycle_probe_event")
        con.execute("RELEASE SAVEPOINT lifecycle_probe_event")
        raise
    return {"accepted": True, "duplicate": False, "event_type": event_type}


def _run_for_update(con, token: str, config, signing_secret):
    digest = _token_digest(token)
    row = con.execute(
        "SELECT * FROM subscription_lifecycle_probe_runs WHERE token_digest=?",
        (digest,),
    ).fetchone()
    if row is None:
        raise ProbeEventRejected("neznámy probe beh")
    current = probe_config_fingerprint(config, signing_secret=signing_secret)
    if not hmac.compare_digest(row["config_fingerprint"], current):
        raise ProbeEventRejected("probe konfigurácia sa od vytvorenia zmenila")
    return digest, row


def record_probe_provider_verified(
    con,
    *,
    token,
    config,
    signing_secret,
    test_mode,
    store_id,
    variant_id,
    price_cents,
    currency,
    interval,
    interval_count,
    trial_days,
    discount_applied_cents,
    variant_status,
    now,
) -> None:
    """Accept provider metadata facts only when they match the planned daily probe."""
    config = _basic_config(config)
    digest, _row = _run_for_update(con, token, config, signing_secret)
    if not (
        test_mode is True
        and store_id == config.store_id
        and variant_id == config.variant_id
        and type(price_cents) is int
        and price_cents == config.price_cents
        and currency == "EUR"
        and interval == "day"
        and type(interval_count) is int
        and interval_count == 1
        and type(trial_days) is int
        and trial_days == 0
        and type(discount_applied_cents) is int
        and discount_applied_cents == 0
        and variant_status == "published"
    ):
        raise ProbeEventRejected("provider nepotvrdil presnú dennú probe konfiguráciu")
    now = _valid_time(now)
    con.execute(
        "UPDATE subscription_lifecycle_probe_runs SET provider_verified=1,updated_at=? WHERE token_digest=?",
        (now, digest),
    )
    con.commit()


def record_genuine_daily_renewal(
    con,
    *,
    token,
    config,
    signing_secret,
    provider_subscription_id,
    provider_invoice_id,
    amount_cents,
    currency,
    interval,
    interval_count,
    period_start,
    period_end,
    now,
) -> None:
    """Pair provider-query facts with an already received signed renewal webhook."""
    config = _basic_config(config)
    digest, row = _run_for_update(con, token, config, signing_secret)
    subscription_id = _safe_id(provider_subscription_id)
    invoice_id = _safe_id(provider_invoice_id)
    start = _timestamp(period_start)
    end = _timestamp(period_end)
    if not (
        subscription_id is not None
        and subscription_id == row["provider_subscription_id"]
        and invoice_id is not None
        and type(amount_cents) is int
        and amount_cents == config.price_cents
        and currency == "EUR"
        and interval == "day"
        and type(interval_count) is int
        and interval_count == 1
        and start is not None
        and end is not None
        and 20 * 3600 <= end - start <= 28 * 3600
    ):
        raise ProbeEventRejected("provider nepotvrdil skutočnú dennú obnovu")
    renewal = con.execute(
        """SELECT 1 FROM subscription_lifecycle_probe_events
             WHERE token_digest=? AND event_type='subscription_payment_success'
               AND billing_reason='renewal' AND provider_subscription_id=?
               AND provider_invoice_id=? AND amount_cents=?
               AND period_start=? AND period_end=?""",
        (
            digest,
            subscription_id,
            invoice_id,
            config.price_cents,
            start,
            end,
        ),
    ).fetchone()
    if renewal is None:
        raise ProbeEventRejected("obnova nemá zhodný podpísaný webhook")
    now = _valid_time(now)
    con.execute(
        """UPDATE subscription_lifecycle_probe_runs
              SET genuine_renewal_verified=1,provider_renewal_invoice_id=?,
                  updated_at=? WHERE token_digest=?""",
        (invoice_id, now, digest),
    )
    con.commit()


def _safe_status(con, digest: str) -> dict:
    digest = _validated_token_digest(digest)
    row = con.execute(
        "SELECT state,provider_verified,genuine_renewal_verified FROM subscription_lifecycle_probe_runs WHERE token_digest=?",
        (digest,),
    ).fetchone()
    if row is None:
        raise ProbeEventRejected("neznámy probe beh")
    events = con.execute(
        """SELECT event_type,billing_reason,source_queue_id,received_at
             FROM subscription_lifecycle_probe_events WHERE token_digest=?
             ORDER BY received_at,id""",
        (digest,),
    ).fetchall()
    event_types = {item[0] for item in events}
    payment_reasons = {
        item[1] for item in events if item[0] == "subscription_payment_success"
    }
    refund_verified = bool(
        {"subscription_payment_refunded", "order_refunded"} & event_types
    )
    queue_bound = bool(events) and all(item[2] is not None for item in events)
    complete = bool(
        row["provider_verified"] == 1
        and row["genuine_renewal_verified"] == 1
        and queue_bound
        and "subscription_created" in event_types
        and bool({"initial", "subscription_created"} & payment_reasons)
        and "renewal" in payment_reasons
        and {
            "subscription_payment_failed",
            "subscription_payment_recovered",
            "subscription_cancelled",
            "subscription_resumed",
            "subscription_expired",
        } <= event_types
        and refund_verified
    )
    return {
        "state": "evidenced" if row["state"] == "evidenced" else (
            "ready" if complete else "collecting"
        ),
        "provider_verified": row["provider_verified"] == 1,
        "genuine_daily_renewal_verified": row["genuine_renewal_verified"] == 1,
        "signed_queue_only": queue_bound,
        "event_count": len(events),
        "events": [
            {"name": item[0], "received_at": float(item[3])}
            for item in events
        ],
        "complete": complete,
        "evidence_source": PROBE_EVIDENCE_SOURCE,
    }


def probe_status(con, *, token: str) -> dict:
    """Return aggregate facts only; provider identity and secrets never leave DB."""
    return _safe_status(con, _token_digest(token))


def probe_status_by_digest(con, *, token_digest: str) -> dict:
    """Server-CLI variant that never needs the raw opaque checkout token."""
    return _safe_status(con, _validated_token_digest(token_digest))


def matching_probe_run_digest(
    con,
    *,
    config: LifecycleProbeConfig,
    signing_secret: str,
    states=("collecting", "ready", "evidenced"),
) -> str:
    """Select exactly one run for this configuration without exposing its token."""
    config = _basic_config(config)
    if (
        not isinstance(states, tuple)
        or not states
        or any(state not in {"collecting", "ready", "evidenced"} for state in states)
    ):
        raise ProbeEventRejected("neplatný výber stavu probe")
    fingerprint = probe_config_fingerprint(config, signing_secret=signing_secret)
    placeholders = ",".join("?" for _ in states)
    rows = con.execute(
        "SELECT token_digest FROM subscription_lifecycle_probe_runs "
        f"WHERE config_fingerprint=? AND state IN ({placeholders}) "
        "ORDER BY created_at DESC",
        (fingerprint, *states),
    ).fetchall()
    if len(rows) != 1:
        raise ProbeEventRejected(
            "probe konfigurácia musí mať práve jeden izolovaný aktívny beh"
        )
    return _validated_token_digest(rows[0][0])


def _probe_marker_facts_for_digest(
    con, *, digest: str, config: LifecycleProbeConfig, signing_secret: str
) -> dict:
    config = _basic_config(config)
    digest = _validated_token_digest(digest)
    row = con.execute(
        "SELECT config_fingerprint FROM subscription_lifecycle_probe_runs "
        "WHERE token_digest=?",
        (digest,),
    ).fetchone()
    if row is None:
        raise ProbeEventRejected("neznámy probe beh")
    current = probe_config_fingerprint(config, signing_secret=signing_secret)
    if not hmac.compare_digest(str(row[0]), current):
        raise ProbeEventRejected("probe konfigurácia sa od vytvorenia zmenila")
    status = _safe_status(con, digest)
    if status["complete"] is not True:
        raise ProbeEventRejected("probe ešte nemá úplný lifecycle dôkaz")
    return {
        "provider": {
            "test_mode": True,
            "price_cents": config.price_cents,
            "currency": "EUR",
            "billing_interval": "day",
            "billing_interval_count": 1,
            "trial_days": 0,
            "discount_applied_cents": 0,
            "variant_status": "published",
        },
        "lifecycle": {
            "evidence_source": PROBE_EVIDENCE_SOURCE,
            "initial_payment_webhook_verified": True,
            "genuine_daily_renewal_verified": True,
            "failed_payment_webhook_verified": True,
            "recovered_payment_webhook_verified": True,
            "cancellation_webhook_verified": True,
            "resumed_webhook_verified": True,
            "expiration_webhook_verified": True,
            "refund_webhook_verified": True,
            "webhook_signature_verified": True,
            "identity_isolation_verified": True,
            "event_order_verified": True,
        },
    }


def probe_marker_facts(
    con, *, token: str, config: LifecycleProbeConfig, signing_secret: str
) -> dict:
    """Return only the exact, non-identifying facts consumed by marker B1."""
    config = _basic_config(config)
    digest, _row = _run_for_update(con, token, config, signing_secret)
    return _probe_marker_facts_for_digest(
        con, digest=digest, config=config, signing_secret=signing_secret
    )


def probe_marker_facts_by_digest(
    con, *, token_digest: str, config: LifecycleProbeConfig, signing_secret: str
) -> dict:
    """Server-only marker facts without recovering the discarded raw token."""
    return _probe_marker_facts_for_digest(
        con,
        digest=token_digest,
        config=config,
        signing_secret=signing_secret,
    )


def process_queued_probe_events(
    con,
    *,
    config: LifecycleProbeConfig,
    signing_secret: str,
    now,
    limit: int = 200,
) -> dict:
    """Consume only exact probe rows from the shared deferred-body queue.

    Bodies signed by another secret or carrying another mode, store, variant or
    token remain untouched for their proper consumer.  A successfully ingested
    probe body is immediately erased from the queue.
    """
    config = _basic_config(config)
    now = _valid_time(now)
    if type(limit) is not int or limit < 1 or limit > 200:
        raise ValueError("limit probe fronty musí byť od 1 do 200")
    rows = con.execute(
        """SELECT id,telo,podpis FROM platobne_odlozene
             WHERE spracovane_o IS NULL ORDER BY id LIMIT ?""",
        (limit,),
    ).fetchall()
    result = {"processed": 0, "left_unrelated": 0, "rejected": 0}
    for row in rows:
        body = bytes(row[1])
        signature = row[2]
        if not isinstance(signature, str) or not _DIGEST_RE.fullmatch(
            signature.strip()
        ):
            result["left_unrelated"] += 1
            continue
        expected = hmac.new(
            config.webhook_secret.encode(), body, hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(signature.strip(), expected):
            result["left_unrelated"] += 1
            continue
        try:
            ingested = ingest_signed_probe_event(
                con,
                body=body,
                signature=signature,
                config=config,
                signing_secret=signing_secret,
                now=now,
                source_queue_id=row[0],
            )
        except ProbeEventRejected:
            result["rejected"] += 1
            continue
        queue_result = "lifecycle_probe:" + ingested["event_type"]
        updated = con.execute(
            """UPDATE platobne_odlozene
                  SET telo=?,podpis=NULL,spracovane_o=?,vysledok=?
                WHERE id=? AND spracovane_o IS NULL""",
            (b"", now, queue_result, row[0]),
        ).rowcount
        con.commit()
        result["processed"] += int(updated == 1)
    return result


def mark_probe_evidenced(
    con,
    *,
    token,
    config,
    signing_secret,
    signed_marker,
    now,
) -> None:
    digest, _row = _run_for_update(con, token, config, signing_secret)
    if not isinstance(signed_marker, dict):
        raise ProbeEventRejected("chýba podpísaný marker probe")
    signature = signed_marker.get("signature")
    if not isinstance(signature, str) or not _DIGEST_RE.fullmatch(signature):
        raise ProbeEventRejected("marker probe nemá platný podpis")
    unsigned = {
        key: value for key, value in signed_marker.items() if key != "signature"
    }
    canonical = json.dumps(
        unsigned, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode()
    expected_signature = hmac.new(
        _required_text(signing_secret, "podpisové tajomstvo").encode(),
        canonical,
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(signature, expected_signature):
        raise ProbeEventRejected("podpis markeru probe nesedí")
    marker_attestation_id = signed_marker.get("attestation_id")
    daily = signed_marker.get("test_mode_daily_probe")
    lifecycle = daily.get("lifecycle") if isinstance(daily, dict) else None
    if not (
        signed_marker.get("schema_version") == PROBE_MARKER_SCHEMA_VERSION
        and isinstance(marker_attestation_id, str)
        and _DIGEST_RE.fullmatch(marker_attestation_id)
        and hmac.compare_digest(
            str(signed_marker.get("probe_config_fingerprint", "")),
            probe_config_fingerprint(config, signing_secret=signing_secret),
        )
        and isinstance(lifecycle, dict)
        and lifecycle.get("evidence_source") == PROBE_EVIDENCE_SOURCE
    ):
        raise ProbeEventRejected("marker nie je viazaný na tento denný probe")
    if _safe_status(con, digest)["complete"] is not True:
        raise ProbeEventRejected("probe ešte nemá úplný lifecycle dôkaz")
    now = _valid_time(now)
    con.execute(
        """UPDATE subscription_lifecycle_probe_runs
              SET state='evidenced',marker_attestation_id=?,evidenced_at=?,updated_at=?
            WHERE token_digest=?""",
        (marker_attestation_id, now, now, digest),
    )
    con.commit()


def mark_probe_digest_evidenced(
    con,
    *,
    token_digest: str,
    config,
    signing_secret,
    signed_marker,
    now,
) -> None:
    """Mark the one digest-selected run after full marker validation by the CLI."""
    digest = _validated_token_digest(token_digest)
    row = con.execute(
        "SELECT config_fingerprint FROM subscription_lifecycle_probe_runs "
        "WHERE token_digest=?",
        (digest,),
    ).fetchone()
    if row is None:
        raise ProbeEventRejected("neznámy probe beh")
    current = probe_config_fingerprint(config, signing_secret=signing_secret)
    if not hmac.compare_digest(row[0], current):
        raise ProbeEventRejected("probe konfigurácia sa od vytvorenia zmenila")
    if not isinstance(signed_marker, dict):
        raise ProbeEventRejected("chýba podpísaný marker probe")
    signature = signed_marker.get("signature")
    unsigned = {key: value for key, value in signed_marker.items() if key != "signature"}
    if not isinstance(signature, str) or not _DIGEST_RE.fullmatch(signature):
        raise ProbeEventRejected("marker probe nemá platný podpis")
    expected_signature = hmac.new(
        _required_text(signing_secret, "podpisové tajomstvo").encode(),
        json.dumps(
            unsigned, sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode(),
        hashlib.sha256,
    ).hexdigest()
    daily = signed_marker.get("test_mode_daily_probe")
    lifecycle = daily.get("lifecycle") if isinstance(daily, dict) else None
    if not (
        hmac.compare_digest(signature, expected_signature)
        and signed_marker.get("schema_version") == PROBE_MARKER_SCHEMA_VERSION
        and _DIGEST_RE.fullmatch(str(signed_marker.get("attestation_id", "")))
        and hmac.compare_digest(
            str(signed_marker.get("probe_config_fingerprint", "")), current
        )
        and isinstance(lifecycle, dict)
        and lifecycle.get("evidence_source") == PROBE_EVIDENCE_SOURCE
        and _safe_status(con, digest)["complete"] is True
    ):
        raise ProbeEventRejected("marker nie je viazaný na tento denný probe")
    now = _valid_time(now)
    con.execute(
        "UPDATE subscription_lifecycle_probe_runs SET state='evidenced',"
        "marker_attestation_id=?,evidenced_at=?,updated_at=? WHERE token_digest=?",
        (signed_marker["attestation_id"], now, now, digest),
    )
    con.commit()


def cleanup_confirmation(token: str | None = None, *, token_digest: str | None = None) -> str:
    if (token is None) == (token_digest is None):
        raise ProbeCleanupRejected("cleanup vyžaduje práve jeden probe beh")
    digest = (
        _token_digest(token) if token is not None
        else _validated_token_digest(token_digest)
    )
    return f"DELETE TEST PROBE {digest[:12]}"


def cleanup_probe_run(con, *, token: str, confirmation: str) -> dict:
    """Delete exactly one evidenced probe run and none of the app's other data."""
    digest = _token_digest(token)
    return cleanup_probe_run_by_digest(
        con, token_digest=digest, confirmation=confirmation
    )


def cleanup_probe_run_by_digest(
    con, *, token_digest: str, confirmation: str
) -> dict:
    """Digest-only server CLI entry point; scope is identical to B1 cleanup."""
    digest = _validated_token_digest(token_digest)
    if not isinstance(confirmation, str) or not hmac.compare_digest(
        confirmation, cleanup_confirmation(token_digest=digest)
    ):
        raise ProbeCleanupRejected("potvrdenie cleanupu nesedí")
    row = con.execute(
        "SELECT state FROM subscription_lifecycle_probe_runs WHERE token_digest=?",
        (digest,),
    ).fetchone()
    if row is None or row["state"] != "evidenced":
        raise ProbeCleanupRejected("vyčistiť sa dá iba probe s podpísaným dôkazom")
    con.execute("SAVEPOINT lifecycle_probe_cleanup")
    try:
        queue_ids = [
            row[0] for row in con.execute(
                """SELECT source_queue_id
                     FROM subscription_lifecycle_probe_events
                    WHERE token_digest=? AND source_queue_id IS NOT NULL""",
                (digest,),
            ).fetchall()
        ]
        events = con.execute(
            "DELETE FROM subscription_lifecycle_probe_events WHERE token_digest=?",
            (digest,),
        ).rowcount
        queue_rows = 0
        for queue_id in queue_ids:
            queue_rows += con.execute(
                """DELETE FROM platobne_odlozene
                    WHERE id=? AND spracovane_o IS NOT NULL AND telo=?
                      AND podpis IS NULL AND vysledok LIKE 'lifecycle_probe:%'""",
                (queue_id, b""),
            ).rowcount
        runs = con.execute(
            "DELETE FROM subscription_lifecycle_probe_runs WHERE token_digest=? AND state='evidenced'",
            (digest,),
        ).rowcount
        if runs != 1:
            raise ProbeCleanupRejected("probe beh sa počas cleanupu zmenil")
        con.execute("RELEASE SAVEPOINT lifecycle_probe_cleanup")
        con.commit()
    except Exception:
        con.execute("ROLLBACK TO SAVEPOINT lifecycle_probe_cleanup")
        con.execute("RELEASE SAVEPOINT lifecycle_probe_cleanup")
        raise
    return {
        "deleted_runs": runs,
        "deleted_events": events,
        "deleted_queue_rows": queue_rows,
    }
