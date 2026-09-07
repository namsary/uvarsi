"""Private withdrawal and complaint records for authenticated Uvar.si users."""

from __future__ import annotations

import datetime
import re
import secrets
import sqlite3
from dataclasses import dataclass


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
  CHECK(request_type IN ('withdrawal','complaint')),
  CHECK(status IN ('received','processing','refunded','requires_review','resolved')),
  CHECK(refund_scope IS NULL OR refund_scope IN ('full','review'))
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
    created: bool = False


def migrate_customer_requests_schema(con) -> None:
    con.executescript(SCHEMA)


def workflow_ready(con) -> bool:
    required = {
        "public_id", "user_id", "order_id", "request_type", "message",
        "status", "refund_scope", "purchased_at", "created_at", "updated_at",
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
                        refund_scope,purchased_at,created_at,updated_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (
                        public_id, user_id, order_id, request_type, message,
                        status, refund_scope, paid_at, now, now,
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
                  purchased_at,created_at,updated_at
             FROM consumer_requests WHERE user_id=? ORDER BY created_at DESC,id DESC""",
        (user_id,),
    ).fetchall()
    return [dict(row) for row in rows]


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
