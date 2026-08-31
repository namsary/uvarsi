"""Single-concurrency durable meal-plan worker."""
import datetime
import importlib
import logging
import os
import socket
import threading
import time
from dataclasses import dataclass

try:
    from . import plan_jobs
    from .plan_calendar import bratislava_day, utc_instant
except ImportError:  # pragma: no cover - production runs this file directly
    import plan_jobs
    from plan_calendar import bratislava_day, utc_instant


LOG = logging.getLogger("uvarsi.plan_worker")
WORKER_ID = f"{socket.gethostname()}:{os.getpid()}"
HEARTBEAT_SECONDS = 15.0
MAX_ATTEMPTS = plan_jobs.MAX_ATTEMPTS


def utcnow():
    return utc_instant()


def _server():
    """Import late so the web process never needs to import the worker."""
    try:
        return importlib.import_module("server")
    except ImportError:
        return importlib.import_module("app.server")


@dataclass(frozen=True)
class ProcessResult:
    job_id: int | None
    status: str
    error_code: str | None = None

    @classmethod
    def empty(cls):
        return cls(job_id=None, status="empty")


class LeaseLostBeforeDispatch(RuntimeError):
    _uvarsi_request_not_dispatched = True


class LeaseLostAfterDispatch(RuntimeError):
    pass


class InputChangedBeforeDispatch(RuntimeError):
    _uvarsi_request_not_dispatched = True

    def __init__(self, code):
        super().__init__(code)
        self.code = code


class EngineReplaced(RuntimeError):
    """A legacy recipe job must not reach AI after deterministic activation."""

    kod = "engine_replaced"
    _uvarsi_request_not_dispatched = True


def _require_legacy_recipe_engine(server):
    if server.recipe_engine_mode() == "on":
        raise EngineReplaced("deterministic recipe engine is active")


class _LeaseAwareClient:
    def __init__(self, server, job, client, clock, calendar_clock):
        self._server = server
        self._job = job
        self._client = client
        self._clock = clock
        self._calendar_clock = calendar_clock
        self.messages = self
        self.dispatched = False
        self.job_now = calendar_clock()
        self._context_identity = None

    def bind_job_context(self, identity):
        self._context_identity = identity

    def prepare(self, factory):
        _require_legacy_recipe_engine(self._server)
        if self._client is None:
            self._client = factory()

    def _heartbeat_loop(self, stop):
        while not stop.wait(HEARTBEAT_SECONDS):
            try:
                with self._server.db() as con:
                    plan_jobs.heartbeat(
                        con, WORKER_ID, self._job.id, now=self._clock(),
                    )
            except Exception:
                LOG.exception("worker heartbeat failed for job %s", self._job.id)

    def _lease_is_current(self):
        stamp = self._clock().isoformat(timespec="seconds")
        with self._server.db() as con:
            row = con.execute(
                """SELECT 1 FROM plan_jobs
                   WHERE id=? AND state='running' AND lease_owner=?
                     AND lease_expires_at > ? AND dispatched_at IS NOT NULL""",
                (self._job.id, WORKER_ID, stamp),
            ).fetchone()
        return row is not None

    def complete_job(self, con):
        return plan_jobs.mark_ready(
            con,
            self._job.id,
            worker_id=WORKER_ID,
            now=self._clock(),
        )

    def revalidate_job_context(self, con):
        self._server.revalidate_job_context(
            self._job,
            self._context_identity,
            con=con,
            now=self._calendar_clock(),
        )

    def create(self, **kwargs):
        _require_legacy_recipe_engine(self._server)
        with self._server.db() as con:
            con.execute("BEGIN IMMEDIATE")
            try:
                try:
                    self.revalidate_job_context(con)
                except self._server.StalePlanJob as error:
                    raise InputChangedBeforeDispatch(error.code) from error
                if not self.dispatched:
                    marked = plan_jobs.mark_dispatched(
                        con, self._job.id, worker_id=WORKER_ID, now=self._clock(),
                    )
                    if not marked:
                        raise LeaseLostBeforeDispatch()
                con.commit()
            except BaseException:
                if con.in_transaction:
                    con.rollback()
                raise
        if self.dispatched and not self._lease_is_current():
            raise LeaseLostAfterDispatch()
        self.dispatched = True

        stop = threading.Event()
        heartbeat = threading.Thread(
            target=self._heartbeat_loop,
            args=(stop,),
            name=f"plan-heartbeat-{self._job.id}",
            daemon=True,
        )
        heartbeat.start()
        try:
            response = self._client.messages.create(**kwargs)
        finally:
            stop.set()
            heartbeat.join()
        if not self._lease_is_current():
            raise LeaseLostAfterDispatch()
        return response


def _error_code(server, error):
    if isinstance(error, server.StalePlanJob):
        return error.code
    if isinstance(error, LeaseLostBeforeDispatch):
        return "lease_lost_before_dispatch"
    if isinstance(error, InputChangedBeforeDispatch):
        return error.code
    if isinstance(error, LeaseLostAfterDispatch):
        return "worker_lost_after_dispatch"
    if isinstance(error, server.WorkerLeaseLostAfterDispatch):
        return "worker_lost_after_dispatch"
    code = getattr(error, "kod", None)
    if isinstance(code, str):
        return code
    status = getattr(error, "status_code", None)
    if status == 504:
        return "provider_timeout"
    if isinstance(error, TimeoutError):
        return "provider_timeout"
    if status == 500:
        return "invalid_model_output"
    return "generation_failed"


def _record_idle(server, now):
    try:
        with server.db() as con:
            plan_jobs.heartbeat(con, WORKER_ID, None, now=now)
    except Exception:
        LOG.exception("worker could not record its idle heartbeat")


def process_one(*, now=None, client=None) -> ProcessResult:
    server = _server()
    claim_time = utc_instant(now) if now is not None else utcnow()
    clock = (lambda: claim_time) if now is not None else utcnow
    calendar_clock = lambda: bratislava_day(clock())
    with server.db() as con:
        job = plan_jobs.claim_next(con, WORKER_ID, now=claim_time)
    if job is None:
        with server.db() as con:
            plan_jobs.heartbeat(con, WORKER_ID, None, now=claim_time)
        return ProcessResult.empty()

    guarded_client = _LeaseAwareClient(server, job, client, clock, calendar_clock)
    try:
        _require_legacy_recipe_engine(server)
        server.build_and_store_job(job, client=guarded_client)
    except BaseException as error:
        code = _error_code(server, error)
        retryable = (
            not guarded_client.dispatched
            and not isinstance(
                error, (
                    server.StalePlanJob,
                    EngineReplaced,
                    LeaseLostBeforeDispatch,
                    InputChangedBeforeDispatch,
                ),
            )
            and job.attempts < MAX_ATTEMPTS
            and code != server.naklady.KOD_KREDIT
        )
        with server.db() as con:
            changed = plan_jobs.mark_failed(
                con,
                job.id,
                code,
                retryable,
                worker_id=WORKER_ID,
                now=clock(),
            )
        if not changed and guarded_client.dispatched:
            code = "worker_lost_after_dispatch"
        if changed:
            _record_idle(server, clock())
        status = "queued" if retryable and changed else "failed"
        return ProcessResult(job_id=job.id, status=status, error_code=code)

    _record_idle(server, clock())
    return ProcessResult(job_id=job.id, status="ready")


def run_forever(poll_seconds: float = 1.0) -> None:
    if poll_seconds < 0:
        raise ValueError("poll_seconds must not be negative")
    while True:
        result = process_one()
        if result.job_id is None or result.status == "queued":
            time.sleep(poll_seconds)


if __name__ == "__main__":
    run_forever()
