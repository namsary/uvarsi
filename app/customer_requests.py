"""Private withdrawal and complaint records for authenticated Uvar.si users."""

from __future__ import annotations

import datetime
import json
import math
import re
import secrets
import sqlite3
from dataclasses import dataclass
from zoneinfo import ZoneInfo

try:
    from . import predplatne
    from .legal_pages import legal_text
    from .operator_profile import LEGAL_VERSION
except ImportError:
    import predplatne
    from legal_pages import legal_text
    from operator_profile import LEGAL_VERSION


TYPE_WITHDRAWAL = "withdrawal"
TYPE_COMPLAINT = "complaint"
STATUS_RECEIVED = "received"
STATUS_PROCESSING = "processing"
STATUS_REFUNDED = "refunded"
STATUS_REQUIRES_REVIEW = "requires_review"
STATUS_RESOLVED = "resolved"
REFUND_FULL = "full"
REFUND_REVIEW = "review"
REFUND_STATUTORY_REVIEW = "statutory_review"
REFUND_MANUAL_LEGAL_REVIEW = "manual_legal_review"
REFUND_CANCEL_AT_PERIOD_END = "cancel_at_period_end"
CLASSIFICATION_REMEDY_REVIEW = "remedy_review"
REMEDY_DEFECT = "defect"
REMEDY_NONCONFORMITY = "nonconformity"
REMEDY_UNAVAILABLE_SERVICE = "unavailable_service"
REMEDY_DUPLICATE_CHARGE = "duplicate_charge"
REMEDY_UNAUTHORIZED_CHARGE = "unauthorized_charge"
SUBSCRIPTION_REMEDIES = frozenset(
    {
        REMEDY_DEFECT,
        REMEDY_NONCONFORMITY,
        REMEDY_UNAVAILABLE_SERVICE,
        REMEDY_DUPLICATE_CHARGE,
        REMEDY_UNAUTHORIZED_CHARGE,
    }
)
PROVIDER = "lemonsqueezy"
PRODUCT = "zakladajuci_clen"
SUBSCRIPTION_PRODUCT = "premium_annual"
WITHDRAWAL_SECONDS = 14 * 24 * 60 * 60
MAX_MESSAGE_LENGTH = 4_000
CONFIRMATION_PENDING = "pending"
CONFIRMATION_SENDING = "sending"
CONFIRMATION_SENT = "sent"
CONFIRMATION_FAILED = "failed"
CONFIRMATION_MAX_ATTEMPTS = 5
CONFIRMATION_RETRY_BASE_SECONDS = 60
CONFIRMATION_RETRY_MAX_SECONDS = 60 * 60
CONFIRMATION_LEASE_SECONDS = 60
LEGACY_LEGAL_VERSION = "legacy-unknown"
LEGACY_LEGAL_SNAPSHOT = (
    "Historické podanie: presná verzia zmluvných dokumentov platná pri prijatí "
    "nebola v pôvodnej evidencii uložená. Prevádzkovateľ ju preto spätne "
    "nenahrádza aktuálnym znením."
)
_SAFE_FAILURE_CODES = {
    "provider_unavailable",
    "provider_rejected",
    "provider_malformed",
    "delivery_error",
}
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_HTML_RE = re.compile(r"<\s*/?\s*[A-Za-z!][^>]*>")
_OPEN_STATUSES = (STATUS_RECEIVED, STATUS_PROCESSING, STATUS_REQUIRES_REVIEW)
_BRATISLAVA = ZoneInfo("Europe/Bratislava")
_SLOVAK_FIXED_REST_DAYS = frozenset(
    {
        (1, 1),
        (1, 6),
        (5, 1),
        (5, 8),
        (7, 5),
        (8, 29),
        (9, 15),
        (11, 1),
        (12, 24),
        (12, 25),
        (12, 26),
    }
)
_SLOVAK_YEARLY_REST_DAY_REMOVALS = {
    2026: frozenset({(5, 8), (9, 15)}),
}


SCHEMA = """
CREATE TABLE IF NOT EXISTS consumer_requests (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  public_id TEXT NOT NULL UNIQUE,
  user_id INTEGER NOT NULL,
  order_id TEXT NOT NULL,
  request_type TEXT NOT NULL,
  message TEXT NOT NULL,
  status TEXT NOT NULL,
  refund_scope TEXT,
  purchased_at REAL NOT NULL,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL,
  legal_version TEXT NOT NULL,
  legal_snapshot TEXT NOT NULL,
  confirmation_state TEXT NOT NULL DEFAULT 'pending',
  confirmation_attempts INTEGER NOT NULL DEFAULT 0,
  confirmation_last_attempt_at REAL,
  confirmation_next_attempt_at REAL,
  confirmation_sent_at REAL,
  confirmation_failure_code TEXT,
  confirmation_lease_owner TEXT,
  confirmation_lease_expires_at REAL,
  confirmation_idempotency_key TEXT NOT NULL,
  invoice_id TEXT,
  invoice_kind TEXT,
  contract_concluded_at REAL,
  statutory_deadline_at REAL,
  subscription_id TEXT,
  subscription_status TEXT,
  subscription_ends_at REAL,
  invoice_amount_cents INTEGER,
  invoice_refunded_amount_cents INTEGER,
  invoice_currency TEXT,
  period_start REAL,
  period_end REAL,
  consent_accepted_at REAL,
  consent_snapshot TEXT,
  consent_valid_for_proration INTEGER,
  refund_preview_cents INTEGER,
  consumed_charge_preview_cents INTEGER,
  request_classification TEXT,
  remedy_type TEXT,
  CHECK(request_type IN ('withdrawal','complaint')),
  CHECK(status IN ('received','processing','refunded','requires_review','resolved')),
  CHECK(refund_scope IS NULL OR refund_scope IN ('full','review')),
  CHECK(confirmation_state IN ('pending','sending','sent','failed')),
  CHECK(confirmation_attempts >= 0)
);
CREATE INDEX IF NOT EXISTS consumer_requests_user_idx
  ON consumer_requests(user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS consumer_requests_open_idx
  ON consumer_requests(status, created_at);
CREATE UNIQUE INDEX IF NOT EXISTS consumer_requests_one_open_idx
  ON consumer_requests(user_id, order_id, request_type)
  WHERE status IN ('received','processing','requires_review');
"""


class RequestNotAllowed(ValueError):
    """The selected order is absent or belongs to another account."""


@dataclass(frozen=True)
class ConsumerRequest:
    public_id: str
    user_id: int
    order_id: str
    request_type: str
    message: str
    status: str
    refund_scope: str | None
    purchased_at: float
    created_at: float
    updated_at: float
    legal_version: str
    legal_snapshot: str
    confirmation_state: str
    confirmation_attempts: int
    confirmation_last_attempt_at: float | None
    confirmation_next_attempt_at: float | None
    confirmation_sent_at: float | None
    confirmation_failure_code: str | None
    confirmation_idempotency_key: str
    invoice_id: str | None
    invoice_kind: str | None
    contract_concluded_at: float | None
    statutory_deadline_at: float | None
    subscription_id: str | None
    subscription_status: str | None
    subscription_ends_at: float | None
    invoice_amount_cents: int | None
    invoice_refunded_amount_cents: int | None
    invoice_currency: str | None
    period_start: float | None
    period_end: float | None
    consent_accepted_at: float | None
    consent_valid_for_proration: bool | None
    refund_preview_cents: int | None
    consumed_charge_preview_cents: int | None
    request_classification: str | None
    remedy_type: str | None
    created: bool = False


@dataclass(frozen=True)
class ConfirmationDelivery:
    public_id: str
    user_id: int
    order_id: str
    request_type: str
    message: str
    status: str
    refund_scope: str | None
    purchased_at: float
    created_at: float
    legal_version: str
    legal_snapshot: str
    email: str
    worker_id: str
    attempt: int
    idempotency_key: str
    invoice_id: str | None
    invoice_kind: str | None
    subscription_status: str | None
    subscription_ends_at: float | None
    invoice_amount_cents: int | None
    invoice_currency: str | None
    period_start: float | None
    period_end: float | None
    consent_valid_for_proration: bool | None
    refund_preview_cents: int | None
    consumed_charge_preview_cents: int | None
    request_classification: str | None
    remedy_type: str | None


def migrate_customer_requests_schema(con) -> None:
    con.executescript(SCHEMA)
    columns = {row[1] for row in con.execute("PRAGMA table_info(consumer_requests)")}
    additions = (
        ("legal_version", "TEXT"),
        ("legal_snapshot", "TEXT"),
        ("confirmation_state", "TEXT"),
        ("confirmation_attempts", "INTEGER"),
        ("confirmation_last_attempt_at", "REAL"),
        ("confirmation_next_attempt_at", "REAL"),
        ("confirmation_sent_at", "REAL"),
        ("confirmation_failure_code", "TEXT"),
        ("confirmation_lease_owner", "TEXT"),
        ("confirmation_lease_expires_at", "REAL"),
        ("confirmation_idempotency_key", "TEXT"),
        ("invoice_id", "TEXT"),
        ("invoice_kind", "TEXT"),
        ("contract_concluded_at", "REAL"),
        ("statutory_deadline_at", "REAL"),
        ("subscription_id", "TEXT"),
        ("subscription_status", "TEXT"),
        ("subscription_ends_at", "REAL"),
        ("invoice_amount_cents", "INTEGER"),
        ("invoice_refunded_amount_cents", "INTEGER"),
        ("invoice_currency", "TEXT"),
        ("period_start", "REAL"),
        ("period_end", "REAL"),
        ("consent_accepted_at", "REAL"),
        ("consent_snapshot", "TEXT"),
        ("consent_valid_for_proration", "INTEGER"),
        ("refund_preview_cents", "INTEGER"),
        ("consumed_charge_preview_cents", "INTEGER"),
        ("request_classification", "TEXT"),
        ("remedy_type", "TEXT"),
    )
    for name, kind in additions:
        if name not in columns:
            con.execute(f"ALTER TABLE consumer_requests ADD COLUMN {name} {kind}")
    con.execute(
        """UPDATE consumer_requests
              SET legal_version=?, legal_snapshot=?
            WHERE legal_snapshot IS NULL OR legal_snapshot=''""",
        (LEGACY_LEGAL_VERSION, LEGACY_LEGAL_SNAPSHOT),
    )
    con.execute(
        """UPDATE consumer_requests SET legal_version=?
            WHERE legal_version IS NULL OR legal_version=''""",
        (LEGACY_LEGAL_VERSION,),
    )
    con.execute(
        """UPDATE consumer_requests
              SET confirmation_state=COALESCE(confirmation_state, ?),
                  confirmation_attempts=COALESCE(confirmation_attempts, 0),
                  confirmation_next_attempt_at=COALESCE(confirmation_next_attempt_at, created_at)
            WHERE confirmation_state IS NULL
               OR confirmation_attempts IS NULL
               OR confirmation_next_attempt_at IS NULL""",
        (CONFIRMATION_PENDING,),
    )
    rows = con.execute(
        """SELECT public_id FROM consumer_requests
            WHERE confirmation_idempotency_key IS NULL
               OR confirmation_idempotency_key=''"""
    ).fetchall()
    con.executemany(
        "UPDATE consumer_requests SET confirmation_idempotency_key=? WHERE public_id=?",
        ((f"consumer-request/{row[0]}", row[0]) for row in rows),
    )
    con.execute(
        """CREATE INDEX IF NOT EXISTS consumer_requests_confirmation_idx
             ON consumer_requests(confirmation_state, confirmation_next_attempt_at, created_at)"""
    )
    con.execute("DROP INDEX IF EXISTS consumer_requests_one_open_idx")
    con.execute(
        """CREATE UNIQUE INDEX IF NOT EXISTS consumer_requests_one_open_legacy_idx
             ON consumer_requests(user_id, order_id, request_type)
          WHERE invoice_id IS NULL
            AND status IN ('received','processing','requires_review')"""
    )
    con.execute(
        """CREATE UNIQUE INDEX IF NOT EXISTS consumer_requests_one_annual_idx
             ON consumer_requests(
               user_id, invoice_id, request_type, COALESCE(remedy_type, '')
             )
          WHERE invoice_id IS NOT NULL"""
    )


def workflow_ready(con) -> bool:
    required = {
        "public_id", "user_id", "order_id", "request_type", "message",
        "status", "refund_scope", "purchased_at", "created_at", "updated_at",
        "legal_version", "legal_snapshot", "confirmation_state",
        "confirmation_attempts",
        "confirmation_last_attempt_at", "confirmation_next_attempt_at",
        "confirmation_sent_at", "confirmation_failure_code",
        "confirmation_lease_owner", "confirmation_lease_expires_at",
        "confirmation_idempotency_key",
        "invoice_id", "invoice_kind", "contract_concluded_at",
        "statutory_deadline_at", "subscription_id", "subscription_status",
        "subscription_ends_at", "invoice_amount_cents",
        "invoice_refunded_amount_cents", "invoice_currency", "period_start",
        "period_end", "consent_accepted_at", "consent_snapshot",
        "consent_valid_for_proration", "refund_preview_cents",
        "consumed_charge_preview_cents", "request_classification",
        "remedy_type",
    }
    columns = {row[1] for row in con.execute("PRAGMA table_info(consumer_requests)")}
    return required <= columns


def _user_id(value) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValueError("neplatný používateľ")
    return value


def _order_id(value) -> str:
    if not isinstance(value, str):
        raise RequestNotAllowed("objednávka sa nedá použiť")
    value = value.strip()
    if not _ID_RE.fullmatch(value):
        raise RequestNotAllowed("objednávka sa nedá použiť")
    return value


def _time(value) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("neplatný čas")
    value = float(value)
    if value < 0 or not math.isfinite(value):
        raise ValueError("neplatný čas")
    return value


def _gregorian_easter_sunday(year: int) -> datetime.date:
    """Return Gregorian Easter Sunday without locale or network data."""
    a = year % 19
    b = year // 100
    c = year % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = (h + l - 7 * m + 114) % 31 + 1
    return datetime.date(year, month, day)


def _slovak_day_of_rest(day: datetime.date) -> bool:
    if day.weekday() >= 5:
        return True
    fixed = set(_SLOVAK_FIXED_REST_DAYS)
    # These state holidays ceased to be days of rest under the time-effective
    # Act No. 241/1993 Coll.; they remain here only for earlier contracts.
    if day.year <= 2023:
        fixed.add((9, 1))
    if day.year <= 2024:
        fixed.add((11, 17))
    fixed.difference_update(_SLOVAK_YEARLY_REST_DAY_REMOVALS.get(day.year, ()))
    if (day.month, day.day) in fixed:
        return True
    easter = _gregorian_easter_sunday(day.year)
    return day in (
        easter - datetime.timedelta(days=2),
        easter + datetime.timedelta(days=1),
    )


def statutory_withdrawal_deadline(contract_concluded_at) -> float:
    """Return the inclusive statutory deadline in Europe/Bratislava."""
    concluded_at = _time(contract_concluded_at)
    concluded_day = datetime.datetime.fromtimestamp(
        concluded_at, datetime.timezone.utc
    ).astimezone(_BRATISLAVA).date()
    deadline_day = concluded_day + datetime.timedelta(days=14)
    while _slovak_day_of_rest(deadline_day):
        deadline_day += datetime.timedelta(days=1)
    return datetime.datetime.combine(
        deadline_day, datetime.time.max, tzinfo=_BRATISLAVA
    ).timestamp()


def _message(value, *, required: bool) -> str:
    if value is None and not required:
        return ""
    if not isinstance(value, str):
        raise ValueError("správa musí byť text")
    value = "\n".join(line.strip() for line in value.strip().splitlines()).strip()
    if required and not value:
        raise ValueError("napíš stručne, čo nie je v poriadku")
    if len(value) > MAX_MESSAGE_LENGTH:
        raise ValueError("správa je príliš dlhá")
    if "\x00" in value or _HTML_RE.search(value):
        raise ValueError("správa nesmie obsahovať HTML")
    return value


def _owned_order(con, *, user_id: int, order_id: str):
    return con.execute(
        """SELECT objednavka_id, ziskany_o, suma_centy, mena, stav
             FROM naroky
            WHERE user_id=? AND poskytovatel=? AND produkt=? AND objednavka_id=?
            LIMIT 1""",
        (user_id, PROVIDER, PRODUCT, order_id),
    ).fetchone()


def _current_legal_snapshot(request_type: str) -> str:
    if request_type == TYPE_WITHDRAWAL:
        request_slug = "odstupenie"
        request_title = "PODMIENKY A POUČENIE K ODSTÚPENIU"
    elif request_type == TYPE_COMPLAINT:
        request_slug = "reklamacie"
        request_title = "REKLAMAČNÉ PODMIENKY"
    else:
        raise ValueError("neplatný typ žiadosti")
    return (
        "NEMENNÁ KÓPIA ZMLUVNÝCH INFORMÁCIÍ PLATNÝCH PRI PODANÍ\n"
        f"Právna verzia: {LEGAL_VERSION}\n\n"
        "VŠEOBECNÉ OBCHODNÉ PODMIENKY\n"
        "--------------------------------\n"
        f"{legal_text('vop').rstrip()}\n\n"
        f"{request_title}\n"
        f"{'-' * len(request_title)}\n"
        f"{legal_text(request_slug).rstrip()}\n"
    )


def _from_row(row, *, created=False) -> ConsumerRequest:
    classification = row["request_classification"]
    refund_scope = row["refund_scope"]
    if row["request_type"] == TYPE_WITHDRAWAL and classification is not None:
        refund_scope = classification
    return ConsumerRequest(
        public_id=str(row["public_id"]),
        user_id=int(row["user_id"]),
        order_id=str(row["order_id"]),
        request_type=str(row["request_type"]),
        message=str(row["message"]),
        status=str(row["status"]),
        refund_scope=refund_scope,
        purchased_at=float(row["purchased_at"]),
        created_at=float(row["created_at"]),
        updated_at=float(row["updated_at"]),
        legal_version=str(row["legal_version"]),
        legal_snapshot=str(row["legal_snapshot"]),
        confirmation_state=str(row["confirmation_state"]),
        confirmation_attempts=int(row["confirmation_attempts"]),
        confirmation_last_attempt_at=(
            None if row["confirmation_last_attempt_at"] is None
            else float(row["confirmation_last_attempt_at"])
        ),
        confirmation_next_attempt_at=(
            None if row["confirmation_next_attempt_at"] is None
            else float(row["confirmation_next_attempt_at"])
        ),
        confirmation_sent_at=(
            None if row["confirmation_sent_at"] is None
            else float(row["confirmation_sent_at"])
        ),
        confirmation_failure_code=row["confirmation_failure_code"],
        confirmation_idempotency_key=str(row["confirmation_idempotency_key"]),
        invoice_id=row["invoice_id"],
        invoice_kind=row["invoice_kind"],
        contract_concluded_at=(
            None if row["contract_concluded_at"] is None
            else float(row["contract_concluded_at"])
        ),
        statutory_deadline_at=(
            None if row["statutory_deadline_at"] is None
            else float(row["statutory_deadline_at"])
        ),
        subscription_id=row["subscription_id"],
        subscription_status=row["subscription_status"],
        subscription_ends_at=(
            None if row["subscription_ends_at"] is None
            else float(row["subscription_ends_at"])
        ),
        invoice_amount_cents=(
            None if row["invoice_amount_cents"] is None
            else int(row["invoice_amount_cents"])
        ),
        invoice_refunded_amount_cents=(
            None if row["invoice_refunded_amount_cents"] is None
            else int(row["invoice_refunded_amount_cents"])
        ),
        invoice_currency=row["invoice_currency"],
        period_start=(
            None if row["period_start"] is None else float(row["period_start"])
        ),
        period_end=(
            None if row["period_end"] is None else float(row["period_end"])
        ),
        consent_accepted_at=(
            None if row["consent_accepted_at"] is None
            else float(row["consent_accepted_at"])
        ),
        consent_valid_for_proration=(
            None if row["consent_valid_for_proration"] is None
            else bool(row["consent_valid_for_proration"])
        ),
        refund_preview_cents=(
            None if row["refund_preview_cents"] is None
            else int(row["refund_preview_cents"])
        ),
        consumed_charge_preview_cents=(
            None if row["consumed_charge_preview_cents"] is None
            else int(row["consumed_charge_preview_cents"])
        ),
        request_classification=classification,
        remedy_type=row["remedy_type"],
        created=created,
    )


def _create(
    con, *, user_id, order_id, request_type, message, now, purchased_at=None
) -> ConsumerRequest:
    user_id = _user_id(user_id)
    order_id = _order_id(order_id)
    now = _time(now)
    message = _message(message, required=request_type == TYPE_COMPLAINT)
    legal_snapshot = _current_legal_snapshot(request_type)
    if con.in_transaction:
        con.commit()
    con.execute("BEGIN IMMEDIATE")
    try:
        order = _owned_order(con, user_id=user_id, order_id=order_id)
        if order is None:
            raise RequestNotAllowed("objednávka sa nedá použiť")
        paid_at = _time(order["ziskany_o"])
        if purchased_at is not None and abs(_time(purchased_at) - paid_at) > 1:
            raise RequestNotAllowed("čas objednávky nesedí")
        if request_type == TYPE_WITHDRAWAL:
            existing = con.execute(
                """SELECT * FROM consumer_requests
                    WHERE user_id=? AND order_id=? AND request_type=?
                    ORDER BY id DESC LIMIT 1""",
                (user_id, order_id, request_type),
            ).fetchone()
        else:
            existing = con.execute(
                """SELECT * FROM consumer_requests
                    WHERE user_id=? AND order_id=? AND request_type=?
                      AND status IN ('received','processing','requires_review')
                    ORDER BY id DESC LIMIT 1""",
                (user_id, order_id, request_type),
            ).fetchone()
        if existing is not None:
            con.commit()
            return _from_row(existing, created=False)
        if request_type == TYPE_WITHDRAWAL:
            in_time = now <= paid_at + WITHDRAWAL_SECONDS
            refund_scope = REFUND_FULL if in_time else REFUND_REVIEW
            status = STATUS_RECEIVED if in_time else STATUS_REQUIRES_REVIEW
        elif request_type == TYPE_COMPLAINT:
            refund_scope = None
            status = STATUS_RECEIVED
        else:
            raise ValueError("neplatný typ žiadosti")
        for _ in range(3):
            public_id = secrets.token_urlsafe(32)
            try:
                con.execute(
                    """INSERT INTO consumer_requests
                       (public_id,user_id,order_id,request_type,message,status,
                         refund_scope,purchased_at,created_at,updated_at,legal_version,
                         legal_snapshot,
                         confirmation_state,confirmation_attempts,
                         confirmation_next_attempt_at,confirmation_idempotency_key)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        public_id, user_id, order_id, request_type, message,
                        status, refund_scope, paid_at, now, now, LEGAL_VERSION,
                        legal_snapshot,
                        CONFIRMATION_PENDING, 0, now,
                        f"consumer-request/{public_id}",
                    ),
                )
                break
            except sqlite3.IntegrityError as error:
                if "consumer_requests.public_id" not in str(error):
                    raise
        else:
            raise RuntimeError("žiadosť sa nepodarilo uložiť")
        row = con.execute(
            "SELECT * FROM consumer_requests WHERE public_id=?", (public_id,)
        ).fetchone()
        con.commit()
        return _from_row(row, created=True)
    except Exception:
        if con.in_transaction:
            con.rollback()
        raise


def create_withdrawal(
    con, *, user_id, order_id, now, message="", purchased_at=None
) -> ConsumerRequest:
    return _create(
        con,
        user_id=user_id,
        order_id=order_id,
        request_type=TYPE_WITHDRAWAL,
        message=message,
        now=now,
        purchased_at=purchased_at,
    )


def create_complaint(con, *, user_id, order_id, message, now) -> ConsumerRequest:
    return _create(
        con,
        user_id=user_id,
        order_id=order_id,
        request_type=TYPE_COMPLAINT,
        message=message,
        now=now,
    )


def _subscription_invoice_context(con, *, user_id: int, invoice_id: str) -> dict:
    cursor = con.execute(
        """SELECT i.provider_invoice_id AS invoice_id,
                  i.provider_subscription_id AS subscription_id,
                  i.provider_order_id AS provider_order_id,
                  i.invoice_kind,i.status AS invoice_status,
                  i.amount_cents,i.refunded_amount_cents,
                  i.currency,i.period_start,i.period_end,i.paid_at,
                  i.contract_concluded_at,
                  s.status AS subscription_status,
                  s.ends_at AS subscription_ends_at,
                  s.initial_amount_cents,s.renewal_amount_cents,
                  a.public_id AS consent_attempt_id,
                  a.status AS consent_attempt_status,
                  a.legal_version AS consent_legal_version,
                  a.privacy_version AS consent_privacy_version,
                  a.consent_json,a.accepted_at AS consent_accepted_at
             FROM subscription_invoices i
             JOIN subscriptions s
               ON s.provider=i.provider
              AND s.test_mode=i.test_mode
              AND s.provider_subscription_id=i.provider_subscription_id
              AND s.provider_order_id=i.provider_order_id
              AND s.currency=i.currency
               AND s.user_id=?
               AND s.product=?
               AND s.initial_payment_verified=1
             LEFT JOIN checkout_attempts a
               ON a.user_id=s.user_id
              AND a.product=s.product
              AND a.test_mode=s.test_mode
              AND a.provider_order_id=s.provider_order_id
            WHERE i.provider=? AND i.provider_invoice_id=?
            LIMIT 1""",
        (user_id, SUBSCRIPTION_PRODUCT, PROVIDER, invoice_id),
    )
    row = cursor.fetchone()
    if row is None:
        raise RequestNotAllowed("faktúra sa nedá použiť")
    context = dict(zip((column[0] for column in cursor.description), tuple(row)))
    try:
        amount = int(context["amount_cents"])
        refunded = int(context["refunded_amount_cents"])
        paid_at = _time(context["paid_at"])
        period_start = _time(context["period_start"])
        period_end = _time(context["period_end"])
        subscription_ends_at = (
            None
            if context["subscription_ends_at"] is None
            else _time(context["subscription_ends_at"])
        )
    except (TypeError, ValueError, OverflowError):
        raise RequestNotAllowed("faktúra sa nedá použiť") from None
    invoice_kind = context["invoice_kind"]
    wanted_amount = (
        context["initial_amount_cents"]
        if invoice_kind == "initial"
        else context["renewal_amount_cents"]
        if invoice_kind == "renewal"
        else None
    )
    if (
        context["invoice_status"] not in {"paid", "partial_refund", "refunded"}
        or not isinstance(wanted_amount, int)
        or amount != wanted_amount
        or amount < 0
        or refunded < 0
        or refunded > amount
        or context["currency"] != "EUR"
        or period_start >= period_end
        or not _ID_RE.fullmatch(str(context["provider_order_id"] or ""))
        or not _ID_RE.fullmatch(str(context["subscription_id"] or ""))
        or context["subscription_status"] not in predplatne.KNOWN_STATUSES
    ):
        raise RequestNotAllowed("faktúra sa nedá použiť")
    context.update(
        amount_cents=amount,
        refunded_amount_cents=refunded,
        paid_at=paid_at,
        period_start=period_start,
        period_end=period_end,
        subscription_ends_at=subscription_ends_at,
    )
    return context


def current_subscription_invoice_id(con, *, user_id, now) -> str:
    user_id = _user_id(user_id)
    now = _time(now)
    rows = con.execute(
        """SELECT i.provider_invoice_id
             FROM subscriptions s
             JOIN subscription_invoices i
               ON i.provider=s.provider
              AND i.test_mode=s.test_mode
              AND i.provider_subscription_id=s.provider_subscription_id
              AND i.provider_order_id=s.provider_order_id
              AND i.currency=s.currency
              AND i.period_start=s.period_start
              AND i.period_end=s.period_end
            WHERE s.user_id=? AND s.product=?
              AND i.status IN ('paid','partial_refund','refunded')
              AND i.period_start<=? AND ?<i.period_end
            ORDER BY i.paid_at DESC,i.id DESC LIMIT 2""",
        (user_id, SUBSCRIPTION_PRODUCT, now, now),
    ).fetchall()
    if len(rows) != 1:
        raise RequestNotAllowed("faktúra sa nedá použiť")
    return str(rows[0][0])


def _safe_snapshot_text(value) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value or len(value) > 128 or any(char in value for char in "\r\n\0"):
        return None
    return value


def _consent_proof(context: dict) -> tuple[str, float | None, bool]:
    try:
        decoded = json.loads(context.get("consent_json"))
    except (TypeError, json.JSONDecodeError):
        decoded = None
    decoded = decoded if isinstance(decoded, dict) else {}
    facts = {
        name: decoded.get(name) is True
        for name in (
            "accept_terms",
            "accept_automatic_renewal",
            "request_immediate_activation",
            "acknowledge_withdrawal_proration",
        )
    }
    accepted = context.get("consent_accepted_at")
    accepted_at = (
        float(accepted)
        if not isinstance(accepted, bool)
        and isinstance(accepted, (int, float))
        and math.isfinite(float(accepted))
        and float(accepted) >= 0
        else None
    )
    legal_version = _safe_snapshot_text(context.get("consent_legal_version"))
    privacy_version = _safe_snapshot_text(context.get("consent_privacy_version"))
    attempt_id = _safe_snapshot_text(context.get("consent_attempt_id"))
    valid = (
        context.get("consent_attempt_status") == "paid"
        and attempt_id is not None
        and legal_version is not None
        and privacy_version is not None
        and decoded.get("legal_version") == legal_version
        and accepted_at is not None
        and accepted_at <= context["paid_at"]
        and all(facts.values())
    )
    snapshot = json.dumps(
        {
            "attempt_id": attempt_id,
            "accepted_at": accepted_at,
            "legal_version": legal_version,
            "privacy_version": privacy_version,
            "facts": facts,
            "valid_for_proration": valid,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return snapshot, accepted_at, valid


def _trusted_contract_conclusion(
    context: dict, *, consent_accepted_at: float | None
) -> float | None:
    value = context.get("contract_concluded_at")
    if (
        context.get("invoice_kind") != "initial"
        or consent_accepted_at is None
        or isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
    ):
        return None
    concluded_at = float(value)
    if not (consent_accepted_at <= concluded_at <= context["paid_at"]):
        return None
    return concluded_at


def _existing_subscription_request(
    con, *, user_id: int, invoice_id: str, request_type: str, remedy_type: str | None
):
    parameters = [user_id, invoice_id, request_type, remedy_type or ""]
    return con.execute(
        """SELECT * FROM consumer_requests
            WHERE user_id=? AND invoice_id=? AND request_type=?
              AND COALESCE(remedy_type,'')=?"""
        + " ORDER BY id DESC LIMIT 1",
        parameters,
    ).fetchone()


def _create_subscription_request(
    con,
    *,
    user_id,
    invoice_id,
    message,
    now,
    request_type,
    remedy_type=None,
) -> ConsumerRequest:
    user_id = _user_id(user_id)
    invoice_id = _order_id(invoice_id)
    now = _time(now)
    if request_type == TYPE_WITHDRAWAL:
        message = _message(message, required=False)
        remedy_type = None
    elif request_type == TYPE_COMPLAINT:
        message = _message(message, required=True)
        if remedy_type not in SUBSCRIPTION_REMEDIES:
            raise ValueError("neplatný dôvod nápravy")
    else:
        raise ValueError("neplatný typ žiadosti")
    legal_snapshot = _current_legal_snapshot(request_type)
    if con.in_transaction:
        con.commit()
    con.execute("BEGIN IMMEDIATE")
    try:
        invoice = _subscription_invoice_context(
            con, user_id=user_id, invoice_id=invoice_id
        )
        if now < invoice["paid_at"]:
            raise ValueError("žiadosť nemôže predchádzať platbe")
        existing = _existing_subscription_request(
            con,
            user_id=user_id,
            invoice_id=invoice_id,
            request_type=request_type,
            remedy_type=remedy_type,
        )
        if existing is not None:
            con.commit()
            return _from_row(existing, created=False)

        consent_snapshot, consent_accepted_at, consent_valid = _consent_proof(
            invoice
        )
        contract_concluded_at = _trusted_contract_conclusion(
            invoice, consent_accepted_at=consent_accepted_at
        )
        statutory_deadline_at = (
            statutory_withdrawal_deadline(contract_concluded_at)
            if contract_concluded_at is not None
            else None
        )
        refund_preview = None
        consumed_preview = None
        if request_type == TYPE_WITHDRAWAL:
            if (
                invoice["invoice_kind"] == "initial"
                and contract_concluded_at is None
            ):
                classification = REFUND_MANUAL_LEGAL_REVIEW
            elif statutory_deadline_at is not None and now <= statutory_deadline_at:
                classification = REFUND_STATUTORY_REVIEW
                target_refund = (
                    predplatne.pro_rata_refund_preview(
                        amount_cents=invoice["amount_cents"],
                        period_start=invoice["period_start"],
                        period_end=invoice["period_end"],
                        withdrawn_at=now,
                    )
                    if consent_valid
                    else invoice["amount_cents"]
                )
                refund_preview = min(
                    invoice["amount_cents"] - invoice["refunded_amount_cents"],
                    max(0, target_refund - invoice["refunded_amount_cents"]),
                )
                consumed_preview = invoice["amount_cents"] - target_refund
            else:
                classification = REFUND_CANCEL_AT_PERIOD_END
                refund_preview = 0
        else:
            classification = CLASSIFICATION_REMEDY_REVIEW

        public_id = None
        for _ in range(3):
            candidate = secrets.token_urlsafe(32)
            cursor = con.execute(
                """INSERT OR IGNORE INTO consumer_requests
                   (public_id,user_id,order_id,request_type,message,status,
                    refund_scope,purchased_at,created_at,updated_at,legal_version,
                     legal_snapshot,confirmation_state,confirmation_attempts,
                     confirmation_next_attempt_at,confirmation_idempotency_key,
                     invoice_id,invoice_kind,contract_concluded_at,
                     statutory_deadline_at,subscription_id,subscription_status,
                     subscription_ends_at,invoice_amount_cents,
                     invoice_refunded_amount_cents,invoice_currency,
                     period_start,period_end,consent_accepted_at,consent_snapshot,
                     consent_valid_for_proration,refund_preview_cents,
                     consumed_charge_preview_cents,request_classification,remedy_type)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    candidate,
                    user_id,
                    invoice["provider_order_id"],
                    request_type,
                    message,
                    STATUS_REQUIRES_REVIEW,
                    None,
                    invoice["paid_at"],
                    now,
                    now,
                    LEGAL_VERSION,
                    legal_snapshot,
                    CONFIRMATION_PENDING,
                    0,
                    now,
                    f"consumer-request/{candidate}",
                    invoice_id,
                    invoice["invoice_kind"],
                    contract_concluded_at,
                    statutory_deadline_at,
                    invoice["subscription_id"],
                    invoice["subscription_status"],
                    invoice["subscription_ends_at"],
                    invoice["amount_cents"],
                    invoice["refunded_amount_cents"],
                    invoice["currency"],
                    invoice["period_start"],
                    invoice["period_end"],
                    consent_accepted_at,
                    consent_snapshot,
                    int(consent_valid),
                    refund_preview,
                    consumed_preview,
                    classification,
                    remedy_type,
                ),
            )
            if cursor.rowcount == 1:
                public_id = candidate
                break
            existing = _existing_subscription_request(
                con,
                user_id=user_id,
                invoice_id=invoice_id,
                request_type=request_type,
                remedy_type=remedy_type,
            )
            if existing is not None:
                con.commit()
                return _from_row(existing, created=False)
        if public_id is None:
            raise RuntimeError("žiadosť sa nepodarilo uložiť")
        row = con.execute(
            "SELECT * FROM consumer_requests WHERE public_id=?", (public_id,)
        ).fetchone()
        con.commit()
        return _from_row(row, created=True)
    except Exception:
        if con.in_transaction:
            con.rollback()
        raise


def create_subscription_withdrawal(
    con, *, user_id, invoice_id, message, now
) -> ConsumerRequest:
    return _create_subscription_request(
        con,
        user_id=user_id,
        invoice_id=invoice_id,
        message=message,
        now=now,
        request_type=TYPE_WITHDRAWAL,
    )


def create_subscription_remedy(
    con, *, user_id, invoice_id, remedy_type, message, now
) -> ConsumerRequest:
    return _create_subscription_request(
        con,
        user_id=user_id,
        invoice_id=invoice_id,
        message=message,
        now=now,
        request_type=TYPE_COMPLAINT,
        remedy_type=remedy_type,
    )


def requests_for_user(con, *, user_id) -> list[dict]:
    user_id = _user_id(user_id)
    rows = con.execute(
        """SELECT public_id,order_id,request_type,message,status,
                  CASE WHEN request_type='withdrawal'
                            AND request_classification IS NOT NULL
                       THEN request_classification ELSE refund_scope END AS refund_scope,
                  purchased_at,created_at,updated_at,legal_version,
                  confirmation_state,confirmation_attempts,
                  confirmation_last_attempt_at,confirmation_next_attempt_at,
                  confirmation_sent_at,confirmation_failure_code,
                  invoice_id,invoice_kind,contract_concluded_at,
                  statutory_deadline_at,subscription_id,invoice_amount_cents,
                  subscription_status,subscription_ends_at,
                  invoice_refunded_amount_cents,invoice_currency,
                  period_start,period_end,consent_accepted_at,
                  consent_valid_for_proration,refund_preview_cents,
                  consumed_charge_preview_cents,request_classification,remedy_type
             FROM consumer_requests WHERE user_id=? ORDER BY created_at DESC,id DESC""",
        (user_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def _worker_id(value) -> str:
    if not isinstance(value, str):
        raise ValueError("neplatný worker")
    value = value.strip()
    if not _ID_RE.fullmatch(value):
        raise ValueError("neplatný worker")
    return value


def _safe_failure_code(value) -> str:
    return value if value in _SAFE_FAILURE_CODES else "delivery_error"


def _recover_expired_confirmation_leases(con, *, now: float) -> None:
    con.execute(
        """UPDATE consumer_requests
              SET confirmation_state=CASE
                    WHEN confirmation_attempts < ? THEN ? ELSE ? END,
                  confirmation_next_attempt_at=CASE
                    WHEN confirmation_attempts < ? THEN ? ELSE NULL END,
                  confirmation_failure_code=COALESCE(
                    confirmation_failure_code, 'delivery_error'),
                  confirmation_lease_owner=NULL,
                  confirmation_lease_expires_at=NULL,
                  updated_at=?
            WHERE confirmation_state=? AND confirmation_lease_expires_at<=?""",
        (
            CONFIRMATION_MAX_ATTEMPTS,
            CONFIRMATION_PENDING,
            CONFIRMATION_FAILED,
            CONFIRMATION_MAX_ATTEMPTS,
            now,
            now,
            CONFIRMATION_SENDING,
            now,
        ),
    )


def claim_confirmation_delivery(
    con,
    *,
    worker_id,
    now,
    public_id=None,
    lease_seconds=CONFIRMATION_LEASE_SECONDS,
) -> ConfirmationDelivery | None:
    """Lease one due confirmation; at most one worker can own it."""
    worker_id = _worker_id(worker_id)
    now = _time(now)
    lease_seconds = _time(lease_seconds)
    if lease_seconds <= 0:
        raise ValueError("neplatný lease")
    if public_id is not None:
        public_id = _order_id(public_id)
    if con.in_transaction:
        con.commit()
    con.execute("BEGIN IMMEDIATE")
    try:
        _recover_expired_confirmation_leases(con, now=now)
        where_public = " AND r.public_id=?" if public_id is not None else ""
        parameters = [
            CONFIRMATION_PENDING,
            CONFIRMATION_MAX_ATTEMPTS,
            now,
        ]
        if public_id is not None:
            parameters.append(public_id)
        row = con.execute(
            f"""SELECT r.*, p.email
                  FROM consumer_requests r
                  JOIN pouzivatelia p ON p.id=r.user_id
                 WHERE r.confirmation_state=?
                   AND r.confirmation_attempts<?
                   AND r.confirmation_next_attempt_at<=?
                   {where_public}
                 ORDER BY r.created_at,r.id LIMIT 1""",
            parameters,
        ).fetchone()
        if row is None:
            con.commit()
            return None
        attempt = int(row["confirmation_attempts"]) + 1
        changed = con.execute(
            """UPDATE consumer_requests
                  SET confirmation_state=?,confirmation_attempts=?,
                      confirmation_last_attempt_at=?,confirmation_lease_owner=?,
                      confirmation_lease_expires_at=?,updated_at=?
                WHERE id=? AND confirmation_state=?
                  AND confirmation_attempts=?""",
            (
                CONFIRMATION_SENDING,
                attempt,
                now,
                worker_id,
                now + lease_seconds,
                now,
                row["id"],
                CONFIRMATION_PENDING,
                attempt - 1,
            ),
        )
        if changed.rowcount != 1:
            con.rollback()
            return None
        con.commit()
        classification = row["request_classification"]
        refund_scope = row["refund_scope"]
        if row["request_type"] == TYPE_WITHDRAWAL and classification is not None:
            refund_scope = classification
        return ConfirmationDelivery(
            public_id=str(row["public_id"]),
            user_id=int(row["user_id"]),
            order_id=str(row["order_id"]),
            request_type=str(row["request_type"]),
            message=str(row["message"]),
            status=str(row["status"]),
            refund_scope=refund_scope,
            purchased_at=float(row["purchased_at"]),
            created_at=float(row["created_at"]),
            legal_version=str(row["legal_version"]),
            legal_snapshot=str(row["legal_snapshot"]),
            email=str(row["email"]),
            worker_id=worker_id,
            attempt=attempt,
            idempotency_key=str(row["confirmation_idempotency_key"]),
            invoice_id=row["invoice_id"],
            invoice_kind=row["invoice_kind"],
            subscription_status=row["subscription_status"],
            subscription_ends_at=(
                None if row["subscription_ends_at"] is None
                else float(row["subscription_ends_at"])
            ),
            invoice_amount_cents=(
                None if row["invoice_amount_cents"] is None
                else int(row["invoice_amount_cents"])
            ),
            invoice_currency=row["invoice_currency"],
            period_start=(
                None if row["period_start"] is None
                else float(row["period_start"])
            ),
            period_end=(
                None if row["period_end"] is None else float(row["period_end"])
            ),
            consent_valid_for_proration=(
                None if row["consent_valid_for_proration"] is None
                else bool(row["consent_valid_for_proration"])
            ),
            refund_preview_cents=(
                None if row["refund_preview_cents"] is None
                else int(row["refund_preview_cents"])
            ),
            consumed_charge_preview_cents=(
                None if row["consumed_charge_preview_cents"] is None
                else int(row["consumed_charge_preview_cents"])
            ),
            request_classification=classification,
            remedy_type=row["remedy_type"],
        )
    except Exception:
        if con.in_transaction:
            con.rollback()
        raise


def finish_confirmation_delivery(
    con, delivery: ConfirmationDelivery, *, sent: bool, now, failure_code=None
) -> bool:
    """Finalize only the caller's lease and retain no provider response body."""
    now = _time(now)
    con.execute("BEGIN IMMEDIATE")
    try:
        row = con.execute(
            """SELECT confirmation_attempts FROM consumer_requests
                WHERE public_id=? AND confirmation_state=?
                  AND confirmation_lease_owner=? AND confirmation_attempts=?""",
            (
                delivery.public_id,
                CONFIRMATION_SENDING,
                delivery.worker_id,
                delivery.attempt,
            ),
        ).fetchone()
        if row is None:
            con.rollback()
            return False
        attempts = int(row[0])
        if sent:
            con.execute(
                """UPDATE consumer_requests
                      SET confirmation_state=?,confirmation_sent_at=?,
                          confirmation_next_attempt_at=NULL,
                          confirmation_failure_code=NULL,
                          confirmation_lease_owner=NULL,
                          confirmation_lease_expires_at=NULL,updated_at=?
                    WHERE public_id=?""",
                (CONFIRMATION_SENT, now, now, delivery.public_id),
            )
        else:
            retry = attempts < CONFIRMATION_MAX_ATTEMPTS
            delay = min(
                CONFIRMATION_RETRY_BASE_SECONDS * (2 ** (attempts - 1)),
                CONFIRMATION_RETRY_MAX_SECONDS,
            )
            con.execute(
                """UPDATE consumer_requests
                      SET confirmation_state=?,confirmation_next_attempt_at=?,
                          confirmation_failure_code=?,
                          confirmation_lease_owner=NULL,
                          confirmation_lease_expires_at=NULL,updated_at=?
                    WHERE public_id=?""",
                (
                    CONFIRMATION_PENDING if retry else CONFIRMATION_FAILED,
                    now + delay if retry else None,
                    _safe_failure_code(failure_code),
                    now,
                    delivery.public_id,
                ),
            )
        con.commit()
        return True
    except Exception:
        if con.in_transaction:
            con.rollback()
        raise


def confirmation_next_wake(con, *, now) -> float | None:
    """Return the next retry or abandoned-lease recovery time."""
    now = _time(now)
    row = con.execute(
        """SELECT MIN(wake_at) FROM (
             SELECT confirmation_next_attempt_at AS wake_at
               FROM consumer_requests
              WHERE confirmation_state=? AND confirmation_attempts<?
             UNION ALL
             SELECT confirmation_lease_expires_at AS wake_at
               FROM consumer_requests WHERE confirmation_state=?
           ) WHERE wake_at IS NOT NULL""",
        (
            CONFIRMATION_PENDING,
            CONFIRMATION_MAX_ATTEMPTS,
            CONFIRMATION_SENDING,
        ),
    ).fetchone()
    if row is None or row[0] is None:
        return None
    return max(now, float(row[0]))


def public_confirmation_state(con, *, public_id) -> dict:
    public_id = _order_id(public_id)
    row = con.execute(
        "SELECT confirmation_state FROM consumer_requests WHERE public_id=?",
        (public_id,),
    ).fetchone()
    if row is None:
        raise RequestNotAllowed("žiadosť sa nenašla")
    state = str(row[0])
    sent = state == CONFIRMATION_SENT
    pending = state in (CONFIRMATION_PENDING, CONFIRMATION_SENDING)
    public_state = "sent" if sent else "pending_retry" if pending else "failed"
    return {"state": public_state, "sent": sent, "pending": pending}


def orders_for_user(con, *, user_id) -> list[dict]:
    user_id = _user_id(user_id)
    rows = con.execute(
        """SELECT objednavka_id AS order_id,ziskany_o AS purchased_at,
                  suma_centy AS amount_cents,mena AS currency,stav AS status
             FROM naroky
            WHERE user_id=? AND poskytovatel=? AND produkt=? AND suma_centy>0
            ORDER BY ziskany_o DESC,id DESC""",
        (user_id, PROVIDER, PRODUCT),
    ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item["refund_deadline"] = float(item["purchased_at"]) + WITHDRAWAL_SECONDS
        result.append(item)
    return result


def subscription_invoices_for_user(con, *, user_id) -> list[dict]:
    """Return only the safe annual-invoice facts owned by the authenticated user."""
    user_id = _user_id(user_id)
    rows = con.execute(
        """SELECT i.provider_invoice_id AS invoice_id,
                  i.invoice_kind,i.status,i.amount_cents,
                  i.refunded_amount_cents,i.currency,
                  i.period_start,i.period_end,i.paid_at
             FROM subscriptions s
             JOIN subscription_invoices i
               ON i.provider=s.provider
              AND i.test_mode=s.test_mode
              AND i.provider_subscription_id=s.provider_subscription_id
              AND i.provider_order_id=s.provider_order_id
              AND i.currency=s.currency
            WHERE s.user_id=? AND s.product=?
              AND s.initial_payment_verified=1
              AND i.status IN ('paid','partial_refund','refunded')
            ORDER BY i.paid_at DESC,i.id DESC""",
        (user_id, SUBSCRIPTION_PRODUCT),
    ).fetchall()
    return [dict(row) for row in rows]


def close_requests_for_refund(con, *, order_id, now) -> int:
    order_id = _order_id(order_id)
    now = _time(now)
    cursor = con.execute(
        """UPDATE consumer_requests SET status=?,updated_at=?
            WHERE order_id=? AND request_type=?
              AND status IN ('received','processing','requires_review')""",
        (STATUS_REFUNDED, now, order_id, TYPE_WITHDRAWAL),
    )
    return int(cursor.rowcount)


def mark_processing(con, *, public_id, now) -> bool:
    public_id = _order_id(public_id)
    now = _time(now)
    cursor = con.execute(
        """UPDATE consumer_requests SET status=?,updated_at=?
            WHERE public_id=? AND status IN ('received','requires_review')""",
        (STATUS_PROCESSING, now, public_id),
    )
    con.commit()
    return cursor.rowcount == 1


def count_unresolved_requests(con) -> int:
    marks = ",".join("?" for _ in _OPEN_STATUSES)
    row = con.execute(
        f"SELECT COUNT(*) FROM consumer_requests WHERE status IN ({marks})",
        _OPEN_STATUSES,
    ).fetchone()
    return int(row[0]) if row else 0


def format_timestamp(value) -> str:
    return datetime.datetime.fromtimestamp(
        float(value), datetime.timezone.utc
    ).isoformat(timespec="seconds")
