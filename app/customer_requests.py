"""Private withdrawal and complaint records for authenticated Uvar.si users."""

from __future__ import annotations

import datetime
import re
import secrets
import sqlite3
from dataclasses import dataclass

try:
    from .operator_profile import LEGAL_VERSION
except ImportError:
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
PROVIDER = "lemonsqueezy"
PRODUCT = "zakladajuci_clen"
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
_SAFE_FAILURE_CODES = {
    "provider_unavailable",
    "provider_rejected",
    "provider_malformed",
    "delivery_error",
}
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_HTML_RE = re.compile(r"<\s*/?\s*[A-Za-z!][^>]*>")
_OPEN_STATUSES = (STATUS_RECEIVED, STATUS_PROCESSING, STATUS_REQUIRES_REVIEW)


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
  confirmation_state TEXT NOT NULL DEFAULT 'pending',
  confirmation_attempts INTEGER NOT NULL DEFAULT 0,
  confirmation_last_attempt_at REAL,
  confirmation_next_attempt_at REAL,
  confirmation_sent_at REAL,
  confirmation_failure_code TEXT,
  confirmation_lease_owner TEXT,
  confirmation_lease_expires_at REAL,
  confirmation_idempotency_key TEXT NOT NULL,
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
    confirmation_state: str
    confirmation_attempts: int
    confirmation_last_attempt_at: float | None
    confirmation_next_attempt_at: float | None
    confirmation_sent_at: float | None
    confirmation_failure_code: str | None
    confirmation_idempotency_key: str
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
    email: str
    worker_id: str
    attempt: int
    idempotency_key: str


def migrate_customer_requests_schema(con) -> None:
    con.executescript(SCHEMA)
    columns = {row[1] for row in con.execute("PRAGMA table_info(consumer_requests)")}
    additions = (
        ("legal_version", "TEXT"),
        ("confirmation_state", "TEXT"),
        ("confirmation_attempts", "INTEGER"),
        ("confirmation_last_attempt_at", "REAL"),
        ("confirmation_next_attempt_at", "REAL"),
        ("confirmation_sent_at", "REAL"),
        ("confirmation_failure_code", "TEXT"),
        ("confirmation_lease_owner", "TEXT"),
        ("confirmation_lease_expires_at", "REAL"),
        ("confirmation_idempotency_key", "TEXT"),
    )
    for name, kind in additions:
        if name not in columns:
            con.execute(f"ALTER TABLE consumer_requests ADD COLUMN {name} {kind}")
    con.execute(
        "UPDATE consumer_requests SET legal_version=? WHERE legal_version IS NULL OR legal_version=''",
        (LEGAL_VERSION,),
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


def workflow_ready(con) -> bool:
    required = {
        "public_id", "user_id", "order_id", "request_type", "message",
        "status", "refund_scope", "purchased_at", "created_at", "updated_at",
        "legal_version", "confirmation_state", "confirmation_attempts",
        "confirmation_last_attempt_at", "confirmation_next_attempt_at",
        "confirmation_sent_at", "confirmation_failure_code",
        "confirmation_lease_owner", "confirmation_lease_expires_at",
        "confirmation_idempotency_key",
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
    if value < 0:
        raise ValueError("neplatný čas")
    return value


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


def _from_row(row, *, created=False) -> ConsumerRequest:
    return ConsumerRequest(
        public_id=str(row["public_id"]),
        user_id=int(row["user_id"]),
        order_id=str(row["order_id"]),
        request_type=str(row["request_type"]),
        message=str(row["message"]),
        status=str(row["status"]),
        refund_scope=row["refund_scope"],
        purchased_at=float(row["purchased_at"]),
        created_at=float(row["created_at"]),
        updated_at=float(row["updated_at"]),
        legal_version=str(row["legal_version"]),
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
        created=created,
    )


def _create(
    con, *, user_id, order_id, request_type, message, now, purchased_at=None
) -> ConsumerRequest:
    user_id = _user_id(user_id)
    order_id = _order_id(order_id)
    now = _time(now)
    message = _message(message, required=request_type == TYPE_COMPLAINT)
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
                        confirmation_state,confirmation_attempts,
                        confirmation_next_attempt_at,confirmation_idempotency_key)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        public_id, user_id, order_id, request_type, message,
                        status, refund_scope, paid_at, now, now, LEGAL_VERSION,
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


def requests_for_user(con, *, user_id) -> list[dict]:
    user_id = _user_id(user_id)
    rows = con.execute(
        """SELECT public_id,order_id,request_type,message,status,refund_scope,
                  purchased_at,created_at,updated_at,legal_version,
                  confirmation_state,confirmation_attempts,
                  confirmation_last_attempt_at,confirmation_next_attempt_at,
                  confirmation_sent_at,confirmation_failure_code
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
        return ConfirmationDelivery(
            public_id=str(row["public_id"]),
            user_id=int(row["user_id"]),
            order_id=str(row["order_id"]),
            request_type=str(row["request_type"]),
            message=str(row["message"]),
            status=str(row["status"]),
            refund_scope=row["refund_scope"],
            purchased_at=float(row["purchased_at"]),
            created_at=float(row["created_at"]),
            legal_version=str(row["legal_version"]),
            email=str(row["email"]),
            worker_id=worker_id,
            attempt=attempt,
            idempotency_key=str(row["confirmation_idempotency_key"]),
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
