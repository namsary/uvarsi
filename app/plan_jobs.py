"""Durable repository for asynchronous meal-plan jobs.

This module owns queue persistence and state transitions only.  It deliberately
does not start a worker or make model/HTTP calls.
"""
import datetime
import json
import sqlite3
from dataclasses import dataclass, replace
from typing import Literal

try:  # `server.py` runs as a script; tests also import the package form.
    from . import naklady
except ImportError:  # pragma: no cover - exercised by the production entrypoint
    import naklady


JobKind = Literal["regular", "pantry", "precompute"]
JobState = Literal["queued", "running", "ready", "failed"]
LEASE_SECONDS = 150
MAX_ATTEMPTS = 2
QUEUE_OLDEST_ALERT_SECONDS = 180
WORKER_HEARTBEAT_ALERT_SECONDS = 60


SCHEMA = """
CREATE TABLE IF NOT EXISTS plan_jobs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  job_key TEXT NOT NULL,
  signature TEXT NOT NULL,
  variant INTEGER NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('regular', 'pantry', 'precompute')),
  is_force INTEGER NOT NULL DEFAULT 0 CHECK (is_force IN (0, 1)),
  user_id INTEGER,
  week TEXT NOT NULL,
  priority INTEGER NOT NULL,
  payload_json TEXT NOT NULL,
  state TEXT NOT NULL CHECK (state IN ('queued', 'running', 'ready', 'failed')),
  attempts INTEGER NOT NULL DEFAULT 0,
  dispatched_at TEXT,
  reserved_eur REAL NOT NULL CHECK (reserved_eur >= 0),
  regeneration_limit INTEGER,
  regeneration_day TEXT,
  created TEXT NOT NULL,
  started_at TEXT,
  finished_at TEXT,
  updated_at TEXT NOT NULL,
  lease_owner TEXT,
  lease_expires_at TEXT,
  error_code TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS plan_jobs_one_active_key
  ON plan_jobs(job_key) WHERE state IN ('queued', 'running');
CREATE INDEX IF NOT EXISTS plan_jobs_next
  ON plan_jobs(state, priority DESC, created ASC, id ASC);
CREATE TABLE IF NOT EXISTS plan_worker_state (
  singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
  worker_id TEXT,
  job_id INTEGER,
  heartbeat_at TEXT
);
"""


@dataclass(frozen=True)
class JobRequest:
    job_key: str
    signature: str
    variant: int
    kind: JobKind
    user_id: int | None
    week: str
    priority: int
    payload: dict
    reserved_eur: float = 0.12
    regeneration_limit: int | None = None
    regeneration_day: str | None = None
    is_force: bool = False

    def __post_init__(self):
        if self.kind not in ("regular", "pantry", "precompute"):
            raise ValueError("kind must be regular, pantry, or precompute")
        if self.is_force and self.kind != "regular":
            raise ValueError("only regular jobs can be force requests")
        has_regeneration = self.regeneration_limit is not None or self.regeneration_day is not None
        if self.kind == "precompute":
            if self.user_id is not None or has_regeneration:
                raise ValueError("precompute jobs cannot reserve a user regeneration")
        else:
            if self.user_id is None:
                raise ValueError("regular and pantry jobs require user_id")
            if self.regeneration_limit is None or self.regeneration_day is None:
                raise ValueError("regular and pantry jobs require both regeneration fields")
            if self.regeneration_limit < 0:
                raise ValueError("regeneration_limit must not be negative")
        if self.reserved_eur < 0:
            raise ValueError("reserved_eur must not be negative")


@dataclass(frozen=True)
class Job:
    id: int
    job_key: str
    signature: str
    variant: int
    kind: JobKind
    is_force: bool
    user_id: int | None
    week: str
    priority: int
    payload: dict
    state: JobState
    attempts: int
    dispatched_at: str | None
    reserved_eur: float
    regeneration_limit: int | None
    regeneration_day: str | None
    created: str
    started_at: str | None
    finished_at: str | None
    lease_owner: str | None
    lease_expires_at: str | None
    error_code: str | None


@dataclass(frozen=True)
class EnqueueResult:
    job: Job
    created: bool


@dataclass(frozen=True)
class JobStatus:
    id: int
    job_key: str
    signature: str
    variant: int
    kind: JobKind
    is_force: bool
    user_id: int | None
    week: str
    state: JobState
    reserved_eur: float
    regeneration_limit: int | None
    regeneration_day: str | None
    created: str
    error_code: str | None


class RegenerationLimitReached(Exception):
    """A new job would exceed the user's daily forced-regeneration limit."""


class DirtyConnectionError(RuntimeError):
    """Queue operations refuse caller-owned transactions."""


def _require_clean_connection(con) -> None:
    if con.in_transaction:
        raise DirtyConnectionError("plan_jobs requires a clean connection")


def migrate_plan_jobs_schema(con) -> None:
    con.executescript(SCHEMA)
    columns = {row[1] for row in con.execute("PRAGMA table_info(plan_jobs)")}
    for name, definition in (
        ("regeneration_limit", "INTEGER"),
        ("regeneration_day", "TEXT"),
    ):
        if name not in columns:
            con.execute(f"ALTER TABLE plan_jobs ADD COLUMN {name} {definition}")
    if "is_force" not in columns:
        con.execute(
            "ALTER TABLE plan_jobs ADD COLUMN is_force INTEGER NOT NULL DEFAULT 0 "
            "CHECK (is_force IN (0, 1))"
        )
        # One-time compatibility backfill. Runtime identity never parses keys.
        con.execute("UPDATE plan_jobs SET is_force=1 WHERE job_key LIKE 'force:%'")
    con.execute(
        "CREATE INDEX IF NOT EXISTS plan_jobs_request_lookup "
        "ON plan_jobs(signature, variant, week, kind, is_force, state, created, id)"
    )


def _stamp(now: datetime.datetime) -> str:
    return now.isoformat(timespec="seconds")


def _job(row: sqlite3.Row) -> Job:
    return Job(
        id=int(row["id"]),
        job_key=row["job_key"],
        signature=row["signature"],
        variant=int(row["variant"]),
        kind=row["kind"],
        is_force=bool(row["is_force"]),
        user_id=row["user_id"],
        week=row["week"],
        priority=int(row["priority"]),
        payload=json.loads(row["payload_json"]),
        state=row["state"],
        attempts=int(row["attempts"]),
        dispatched_at=row["dispatched_at"],
        reserved_eur=float(row["reserved_eur"]),
        regeneration_limit=row["regeneration_limit"],
        regeneration_day=row["regeneration_day"],
        created=row["created"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        lease_owner=row["lease_owner"],
        lease_expires_at=row["lease_expires_at"],
        error_code=row["error_code"],
    )


def active_reservations_eur(con) -> float:
    row = con.execute(
        "SELECT COALESCE(SUM(reserved_eur), 0) FROM plan_jobs "
        "WHERE state IN ('queued', 'running')"
    ).fetchone()
    return float(row[0] or 0.0)


def _status(row: sqlite3.Row) -> JobStatus:
    return JobStatus(
        id=int(row["id"]),
        job_key=row["job_key"],
        signature=row["signature"],
        variant=int(row["variant"]),
        kind=row["kind"],
        is_force=bool(row["is_force"]),
        user_id=row["user_id"],
        week=row["week"],
        state=row["state"],
        reserved_eur=float(row["reserved_eur"]),
        regeneration_limit=row["regeneration_limit"],
        regeneration_day=row["regeneration_day"],
        created=row["created"],
        error_code=row["error_code"],
    )


def latest_user_request(
    con,
    *,
    user_id: int,
    signature: str,
    variant: int,
    kind: JobKind,
    week: str,
    is_force: bool | None = None,
    states: tuple[JobState, ...] | None = None,
) -> JobStatus | None:
    """Return one user's latest matching request using durable typed columns."""
    values = [user_id, signature, variant, kind, week]
    force_clause = ""
    if is_force is not None:
        force_clause = " AND is_force=?"
        values.append(int(is_force))
    state_clause = ""
    if states:
        state_clause = f" AND state IN ({','.join('?' for _ in states)})"
        values.extend(states)
    row = con.execute(
        """SELECT * FROM plan_jobs
           WHERE user_id=? AND signature=? AND variant=? AND kind=? AND week=?"""
        + force_clause
        + state_clause
        + " ORDER BY created DESC, id DESC LIMIT 1",
        values,
    ).fetchone()
    return _status(row) if row is not None else None


def latest_shared_regular_request(
    con,
    *,
    signature: str,
    variant: int,
    week: str,
) -> JobStatus | None:
    """Return status that is safe to share for one identical regular request."""
    row = con.execute(
        """SELECT * FROM plan_jobs
           WHERE signature=? AND variant=? AND week=?
             AND kind='regular' AND is_force=0
           ORDER BY created DESC, id DESC LIMIT 1""",
        (signature, variant, week),
    ).fetchone()
    return _status(row) if row is not None else None


def _reserve_user_regeneration(con, request: JobRequest) -> None:
    if request.regeneration_limit is None:
        return
    row = con.execute(
        "SELECT pocet FROM prepocty WHERE user_id=? AND den=?",
        (request.user_id, request.regeneration_day),
    ).fetchone()
    used = int(row[0]) if row else 0
    if used >= request.regeneration_limit:
        raise RegenerationLimitReached()
    con.execute(
        "INSERT INTO prepocty (user_id, den, pocet) VALUES (?, ?, 1) "
        "ON CONFLICT(user_id, den) DO UPDATE SET pocet=pocet+1",
        (request.user_id, request.regeneration_day),
    )


def enqueue(con, request: JobRequest, *, now: datetime.datetime) -> EnqueueResult:
    """Create one active job, reserving budget and a user retry atomically."""
    _require_clean_connection(con)
    con.execute("BEGIN IMMEDIATE")
    try:
        if request.is_force:
            active_request = latest_user_request(
                con,
                user_id=request.user_id,
                signature=request.signature,
                variant=request.variant,
                kind=request.kind,
                week=request.week,
                is_force=True,
                states=("queued", "running"),
            )
            if active_request is not None:
                active = con.execute(
                    "SELECT * FROM plan_jobs WHERE id=?", (active_request.id,)
                ).fetchone()
                con.commit()
                return EnqueueResult(job=_job(active), created=False)
            row = con.execute(
                "SELECT pocet FROM prepocty WHERE user_id=? AND den=?",
                (request.user_id, request.regeneration_day),
            ).fetchone()
            next_sequence = (int(row[0]) if row else 0) + 1
            request = replace(
                request,
                job_key=(
                    f"force:{request.user_id}:{request.signature}:{request.variant}:"
                    f"{request.regeneration_day}:{next_sequence}"
                ),
            )
        active = con.execute(
            "SELECT * FROM plan_jobs WHERE job_key=? AND state IN ('queued', 'running')",
            (request.job_key,),
        ).fetchone()
        if active is not None:
            con.commit()
            return EnqueueResult(job=_job(active), created=False)

        outstanding = active_reservations_eur(con)
        purpose = "predpocet" if request.kind == "precompute" else "plan"
        naklady.skontroluj(
            con,
            purpose,
            odhad_eur=request.reserved_eur,
            teraz=now,
            rezervovane_eur=outstanding,
        )
        _reserve_user_regeneration(con, request)
        stamp = _stamp(now)
        cursor = con.execute(
            """INSERT INTO plan_jobs (
                job_key, signature, variant, kind, is_force, user_id, week, priority,
                payload_json, state, reserved_eur, regeneration_limit, regeneration_day,
                created, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?, ?, ?, ?)""",
            (
                request.job_key,
                request.signature,
                request.variant,
                request.kind,
                int(request.is_force),
                request.user_id,
                request.week,
                request.priority,
                json.dumps(request.payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                request.reserved_eur,
                request.regeneration_limit,
                request.regeneration_day,
                stamp,
                stamp,
            ),
        )
        row = con.execute("SELECT * FROM plan_jobs WHERE id=?", (cursor.lastrowid,)).fetchone()
        con.commit()
        return EnqueueResult(job=_job(row), created=True)
    except Exception:
        if con.in_transaction:
            con.rollback()
        raise


def _recover_expired_leases(con, stamp: str) -> None:
    con.execute(
        """UPDATE plan_jobs
           SET state='queued', lease_owner=NULL, lease_expires_at=NULL, updated_at=?
           WHERE state='running' AND lease_expires_at <= ? AND dispatched_at IS NULL""",
        (stamp, stamp),
    )
    con.execute(
        """UPDATE plan_jobs
           SET state='failed', finished_at=?, updated_at=?,
               lease_owner=NULL, lease_expires_at=NULL,
               error_code='worker_lost_before_dispatch'
           WHERE state='queued' AND attempts >= ? AND dispatched_at IS NULL""",
        (stamp, stamp, MAX_ATTEMPTS),
    )
    con.execute(
        """UPDATE plan_jobs
           SET state='failed', finished_at=?, lease_owner=NULL, lease_expires_at=NULL,
               updated_at=?, error_code='worker_lost_after_dispatch'
           WHERE state='running' AND lease_expires_at <= ? AND dispatched_at IS NOT NULL""",
        (stamp, stamp, stamp),
    )


def claim_next(con, worker_id: str, *, now: datetime.datetime,
               lease_seconds: int = LEASE_SECONDS) -> Job | None:
    """Atomically claim the highest-priority queued job after lease recovery."""
    _require_clean_connection(con)
    con.execute("BEGIN IMMEDIATE")
    try:
        stamp = _stamp(now)
        expiry = _stamp(now + datetime.timedelta(seconds=lease_seconds))
        _recover_expired_leases(con, stamp)
        row = con.execute(
            """UPDATE plan_jobs
               SET state='running', attempts=attempts+1, started_at=COALESCE(started_at, ?),
                   updated_at=?, lease_owner=?, lease_expires_at=?
               WHERE id=(
                   SELECT id FROM plan_jobs WHERE state='queued' AND attempts < ?
                   ORDER BY priority DESC, created ASC, id ASC LIMIT 1
               )
               AND NOT EXISTS (
                   SELECT 1 FROM plan_jobs AS running
                   WHERE running.state='running' AND running.lease_expires_at > ?
               )
               RETURNING *""",
            (stamp, stamp, worker_id, expiry, MAX_ATTEMPTS, stamp),
        ).fetchone()
        con.commit()
        return _job(row) if row is not None else None
    except Exception:
        if con.in_transaction:
            con.rollback()
        raise


def heartbeat(con, worker_id: str, job_id: int | None, *, now: datetime.datetime) -> None:
    """Record worker liveness and renew the current worker's lease."""
    stamp = _stamp(now)
    expiry = _stamp(now + datetime.timedelta(seconds=LEASE_SECONDS))
    with con:
        con.execute(
            """INSERT INTO plan_worker_state (singleton, worker_id, job_id, heartbeat_at)
               VALUES (1, ?, ?, ?)
               ON CONFLICT(singleton) DO UPDATE SET worker_id=excluded.worker_id,
                   job_id=excluded.job_id, heartbeat_at=excluded.heartbeat_at""",
            (worker_id, job_id, stamp),
        )
        if job_id is not None:
            con.execute(
                """UPDATE plan_jobs SET lease_expires_at=?, updated_at=?
                   WHERE id=? AND state='running' AND lease_owner=?""",
                (expiry, stamp, job_id, worker_id),
            )


def mark_dispatched(con, job_id: int, *, worker_id: str, now: datetime.datetime) -> bool:
    stamp = _stamp(now)
    statement = """UPDATE plan_jobs SET dispatched_at=?, updated_at=?
                   WHERE id=? AND state='running' AND lease_owner=? AND lease_expires_at > ?
                     AND dispatched_at IS NULL"""
    values = (stamp, stamp, job_id, worker_id, stamp)
    if con.in_transaction:
        cursor = con.execute(statement, values)
    else:
        with con:
            cursor = con.execute(statement, values)
    return cursor.rowcount == 1


def mark_ready(con, job_id: int, *, worker_id: str, now: datetime.datetime) -> bool:
    stamp = _stamp(now)
    statement = """UPDATE plan_jobs
                   SET state='ready', finished_at=?, updated_at=?, error_code=NULL,
                       lease_owner=NULL, lease_expires_at=NULL
                   WHERE id=? AND state='running' AND lease_owner=? AND lease_expires_at > ?"""
    values = (stamp, stamp, job_id, worker_id, stamp)
    if con.in_transaction:
        cursor = con.execute(statement, values)
    else:
        with con:
            cursor = con.execute(statement, values)
    return cursor.rowcount == 1


def mark_failed(
    con,
    job_id: int,
    code: str,
    retryable_before_dispatch: bool,
    *,
    worker_id: str,
    now: datetime.datetime,
) -> bool:
    stamp = _stamp(now)
    with con:
        if retryable_before_dispatch:
            cursor = con.execute(
                """UPDATE plan_jobs SET state='queued', updated_at=?, error_code=?,
                       lease_owner=NULL, lease_expires_at=NULL
                   WHERE id=? AND state='running' AND lease_owner=? AND lease_expires_at > ?
                     AND dispatched_at IS NULL""",
                (stamp, code, job_id, worker_id, stamp),
            )
            if cursor.rowcount:
                return True
        cursor = con.execute(
            """UPDATE plan_jobs SET state='failed', finished_at=?, updated_at=?, error_code=?,
                   lease_owner=NULL, lease_expires_at=NULL
               WHERE id=? AND state='running' AND lease_owner=? AND lease_expires_at > ?""",
            (stamp, stamp, code, job_id, worker_id, stamp),
        )
    return cursor.rowcount == 1


def status_for_key(con, job_key: str) -> JobStatus | None:
    row = con.execute(
        """SELECT * FROM plan_jobs WHERE job_key=?
           ORDER BY created DESC, id DESC LIMIT 1""",
        (job_key,),
    ).fetchone()
    if row is None:
        return None
    return _status(row)


def health(con, *, now: datetime.datetime) -> dict:
    """Return persisted queue state without pretending an absent worker is alive."""
    queued, oldest = con.execute(
        "SELECT COUNT(*), MIN(created) FROM plan_jobs WHERE state='queued'"
    ).fetchone()
    last_ready = con.execute(
        "SELECT MAX(finished_at) FROM plan_jobs WHERE state='ready'"
    ).fetchone()[0]
    failed = con.execute(
        "SELECT COUNT(*) FROM plan_jobs WHERE state='failed'"
    ).fetchone()[0]
    heartbeat_at = con.execute(
        "SELECT heartbeat_at FROM plan_worker_state WHERE singleton=1"
    ).fetchone()

    def as_utc(value: datetime.datetime) -> datetime.datetime:
        if value.utcoffset() is None:
            return value.replace(tzinfo=datetime.timezone.utc)
        return value.astimezone(datetime.timezone.utc)

    now_utc = as_utc(now)

    def age(stamp: str | None) -> int | None:
        if stamp is None:
            return None
        recorded_utc = as_utc(datetime.datetime.fromisoformat(stamp))
        return max(0, int((now_utc - recorded_utc).total_seconds()))

    oldest_seconds = age(oldest)
    heartbeat_seconds = age(heartbeat_at[0]) if heartbeat_at is not None else None
    heartbeat_stamp = (
        as_utc(datetime.datetime.fromisoformat(heartbeat_at[0])).isoformat()
        if heartbeat_at is not None
        else None
    )
    worker_alive = (
        heartbeat_seconds is not None
        and heartbeat_seconds <= WORKER_HEARTBEAT_ALERT_SECONDS
    )
    if not worker_alive:
        blocking_code = "worker_heartbeat_stale"
    elif oldest_seconds is not None and oldest_seconds > QUEUE_OLDEST_ALERT_SECONDS:
        blocking_code = "queue_oldest_exceeded"
    else:
        blocking_code = None
    return {
        "queued": int(queued),
        "oldest_seconds": oldest_seconds,
        "worker_alive": worker_alive,
        "heartbeat_seconds": heartbeat_seconds,
        "heartbeat_at": heartbeat_stamp,
        "last_ready": last_ready,
        "failed": int(failed),
        "blocking_code": blocking_code,
    }
