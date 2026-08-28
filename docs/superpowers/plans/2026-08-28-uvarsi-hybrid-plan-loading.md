# Uvar.si Hybrid Meal Plan Loading Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Return a cached meal plan or an honest background-preparation state within one second, then finish cold plans autonomously on the existing Hetzner server without duplicate AI calls.

**Architecture:** Keep finished regular plans in `plany_zdielane`, but replace synchronous cold generation with a durable SQLite queue. The web process enqueues idempotently and returns HTTP 202; a separate single-concurrency systemd worker generates plans, while targeted precomputation submits low-priority jobs through the same queue. A deterministic offer shortlist reduces model input, but final validation still uses the complete current offer set.

**Tech Stack:** Python 3.12, FastAPI, SQLite WAL, Anthropic Python SDK, vanilla JavaScript PWA, systemd, Bash, PowerShell, pytest.

**Spec:** `docs/superpowers/specs/2026-08-28-uvarsi-hybrid-plan-loading-design.md`

## Global Constraints

- Use the existing Hetzner server; do not add Redis, Celery, or another paid service.
- Keep payments disabled.
- Do not reset or rewrite existing daily, weekly, or monthly cost ledgers.
- Deploy commit `ace8ef7` with this release; it separates regular plans from pantry-driven plans.
- A cache hit or queue acknowledgement must return within one second at p95.
- A cold plan targets p95 completion within 60 seconds; one model attempt has a hard 120-second limit.
- One job may cause at most one dispatched paid Anthropic request.
- Process one AI job at a time in the first release.
- Never generate from incomplete Lidl, Kaufland, or Tesco data.
- Keep regular and pantry-driven signatures, storage, and invalidation separate.
- Preserve the other application running on the same server.
- Use TDD for every task and commit only the files named by that task.

## File Map

- Create `app/plan_jobs.py`: queue schema, status model, idempotent enqueue, lease, heartbeat, and job repository.
- Create `app/plan_shortlist.py`: deterministic 100–150-offer selection with store and category coverage.
- Create `app/plan_worker.py`: one-job worker loop and generation orchestration; imports `server` late to avoid a cycle.
- Modify `app/server.py`: migrations, async plan API, cache adoption, and health output.
- Modify `app/naklady.py`: account for outstanding queue reservations without changing historical ledgers.
- Modify `app/predpocet.py`: submit low-priority jobs instead of calling Anthropic directly.
- Modify `app/static/app.html`: pending-state UI, polling, navigation-safe resume, and removal of the 150-second browser wait.
- Create `hetzner/uvarsi-plan-worker.service`: persistent worker unit.
- Modify `hetzner/samopull.sh`, `hetzner/dozorca.sh`, and `nasad.ps1`: install, supervise, validate, and roll back the worker safely.
- Modify `VERSION`: identify the release.
- Create `tests/test_plan_jobs.py`, `tests/test_plan_shortlist.py`, `tests/test_plan_worker.py`, and `tests/test_plan_async_api.py`.
- Modify `tests/test_predpocet.py`, `tests/test_app_html_contract.py`, `tests/test_frontend_speed_contract.py`, `tests/test_dozorca_contract.py`, and relevant server/cost tests.

---

### Task 1: Durable queue and atomic reservations

**Files:**
- Create: `app/plan_jobs.py`
- Modify: `app/server.py` (`migruj_schemu` only)
- Modify: `app/naklady.py` (budget check accepts outstanding reservations)
- Create: `tests/test_plan_jobs.py`
- Modify: `tests/test_naklady.py`

**Interfaces:**
- Produces: `plan_jobs.migrate_plan_jobs_schema(con) -> None`
- Produces: `plan_jobs.enqueue(con, request: JobRequest, *, now: datetime) -> EnqueueResult`
- Produces: `plan_jobs.claim_next(con, worker_id: str, *, now: datetime, lease_seconds: int = 150) -> Job | None`
- Produces: `plan_jobs.heartbeat(con, worker_id: str, job_id: int | None, *, now: datetime) -> None`
- Produces: `plan_jobs.mark_dispatched(con, job_id: int, *, now: datetime) -> None`
- Produces: `plan_jobs.mark_ready(con, job_id: int, *, now: datetime) -> None`
- Produces: `plan_jobs.mark_failed(con, job_id: int, code: str, retryable_before_dispatch: bool, *, now: datetime) -> None`
- Produces: `plan_jobs.status_for_key(con, job_key: str) -> JobStatus | None`
- Modifies: `naklady.skontroluj(con, ucel, odhad_eur=None, teraz=None, rezervovane_eur=0.0)`

- [ ] **Step 1: Write queue migration and uniqueness tests**

```python
def test_enqueue_is_idempotent_for_one_active_key(con):
    req = JobRequest(job_key="regular:abc:0", signature="abc", variant=0,
                     kind="regular", user_id=1, week="2026-08-24",
                     priority=100, payload={})
    first = enqueue(con, req, now=NOW)
    second = enqueue(con, req, now=NOW)
    assert first.created is True
    assert second.created is False
    assert second.job.id == first.job.id

def test_regular_and_pantry_jobs_never_collide(con):
    regular = request(job_key="regular:abc:0", kind="regular")
    pantry = request(job_key="pantry:1:abc:0", kind="pantry")
    assert enqueue(con, regular, now=NOW).job.id != enqueue(con, pantry, now=NOW).job.id
```

- [ ] **Step 2: Run the focused tests and confirm they fail**

Run: `python -m pytest tests/test_plan_jobs.py -q`

Expected: collection fails because `app.plan_jobs` does not exist.

- [ ] **Step 3: Implement schema and immutable request/status types**

Create `plan_jobs` with states `queued`, `running`, `ready`, and `failed`; include `job_key`, signature, variant, kind, user ID, week, priority, payload JSON, attempts, dispatch marker, reserved euros, timestamps, lease owner/expiry, and error code. Add a partial unique index for one `queued` or `running` row per `job_key`, plus `plan_worker_state` with a singleton heartbeat row.

```python
@dataclass(frozen=True)
class JobRequest:
    job_key: str
    signature: str
    variant: int
    kind: Literal["regular", "pantry", "precompute"]
    user_id: int | None
    week: str
    priority: int
    payload: dict
    reserved_eur: float = 0.12

@dataclass(frozen=True)
class EnqueueResult:
    job: Job
    created: bool
```

Use `BEGIN IMMEDIATE` around budget check, user-limit reservation, and insert. Count `reserved_eur` only from `queued` and `running` rows. Do not alter rows in `naklady`.

- [ ] **Step 4: Add lease and crash-recovery tests**

```python
def test_claim_prefers_live_priority_and_recovers_expired_lease(con):
    enqueue(con, request(job_key="pre", priority=20), now=NOW)
    live = enqueue(con, request(job_key="live", priority=100), now=NOW).job
    claimed = claim_next(con, "worker-a", now=NOW, lease_seconds=150)
    assert claimed.id == live.id
    recovered = claim_next(con, "worker-b", now=NOW + timedelta(seconds=151))
    assert recovered.id == live.id

def test_dispatched_job_is_not_automatically_requeued(con):
    job = claim_one(con)
    mark_dispatched(con, job.id, now=NOW)
    mark_failed(con, job.id, "provider_timeout", retryable_before_dispatch=False, now=NOW)
    assert status_for_key(con, job.job_key).state == "failed"
```

- [ ] **Step 5: Implement leasing, dispatch marker, terminal states, and heartbeat**

Use one `UPDATE ... RETURNING` claim transaction ordered by `priority DESC, created ASC`. An expired lease may return to `queued` only when `dispatched_at IS NULL`; otherwise mark it `failed` with `worker_lost_after_dispatch` to prevent a second paid call.

- [ ] **Step 6: Run queue and cost tests**

Run: `python -m pytest tests/test_plan_jobs.py tests/test_naklady.py -q`

Expected: all pass; historical `naklady` rows remain unchanged.

- [ ] **Step 7: Commit**

```bash
git add app/plan_jobs.py app/server.py app/naklady.py tests/test_plan_jobs.py tests/test_naklady.py
git commit -m "feat: add durable meal plan queue"
```

---

### Task 2: Deterministic offer shortlist

**Files:**
- Create: `app/plan_shortlist.py`
- Create: `tests/test_plan_shortlist.py`
- Modify: `app/plan_data.py` (message builder receives shortlisted rows while validation keeps full rows)
- Modify: `tests/test_plan_data.py`

**Interfaces:**
- Consumes: offer rows returned by `server.akcie_pre(obchody)`.
- Produces: `plan_shortlist.select_offers(rows, stores: Sequence[str], limit: int = 120) -> list`
- Produces: `plan_data.personal_plan_messages(..., prompt_rows=None)` where `rows` remains the full validation set and `prompt_rows` is the shortlist.

- [ ] **Step 1: Write deterministic coverage tests**

```python
def test_shortlist_is_bounded_deterministic_and_covers_each_store_and_core_category():
    rows = fixture_with_582_offers()
    first = select_offers(rows, ["Lidl", "Kaufland", "Tesco"], limit=120)
    second = select_offers(list(reversed(rows)), ["Tesco", "Lidl", "Kaufland"], limit=120)
    assert offer_keys(first) == offer_keys(second)
    assert len(first) <= 120
    assert stores(first) == {"Lidl", "Kaufland", "Tesco"}
    assert {"maso", "zelenina", "mliecne", "trvanlive"} <= categories(first)

def test_shortlist_excludes_invalid_or_expired_offers():
    assert expired_offer not in select_offers(rows, STORES, limit=120)
```

- [ ] **Step 2: Run tests and confirm failure**

Run: `python -m pytest tests/test_plan_shortlist.py -q`

Expected: module import fails.

- [ ] **Step 3: Implement a stable quota selector**

Normalize store/category names, remove duplicate `offer_key` values, reject rows outside the requested week/validity, then rank by usable category, discount ratio, presence of numeric price, and stable `offer_key`. Reserve minimum slots per selected store and core category; fill remaining slots by score. Never select more than `limit`.

- [ ] **Step 4: Prove validation still uses the full offer database**

```python
def test_plan_may_only_use_offer_keys_from_full_rows_even_with_short_prompt():
    messages = personal_plan_messages(full_rows, profile, prompt_rows=short_rows)
    assert offer_payload(messages) == offer_keys(short_rows)
    with pytest.raises(ValueError, match="neznáma ponuka"):
        validate_plan(model_plan_using_unknown_key, full_rows, profile)
```

- [ ] **Step 5: Run focused tests**

Run: `python -m pytest tests/test_plan_shortlist.py tests/test_plan_data.py -q`

Expected: all pass and existing portion/pantry validation remains strict.

- [ ] **Step 6: Commit**

```bash
git add app/plan_shortlist.py app/plan_data.py tests/test_plan_shortlist.py tests/test_plan_data.py
git commit -m "perf: shortlist offers before meal plan generation"
```

---

### Task 3: Single-concurrency background worker

**Files:**
- Create: `app/plan_worker.py`
- Create: `tests/test_plan_worker.py`
- Modify: `app/server.py` (extract reusable generation function without changing API yet)

**Interfaces:**
- Consumes: Task 1 queue functions and Task 2 `select_offers`.
- Produces: `plan_worker.process_one(*, now=None, client=None) -> ProcessResult`
- Produces: `plan_worker.run_forever(poll_seconds: float = 1.0) -> None`
- Produces: `server.build_and_store_job(job, *, client=None) -> dict`

- [ ] **Step 1: Write worker success and one-dispatch tests**

```python
def test_worker_builds_regular_plan_and_marks_job_ready(app_db, fake_model):
    job = queued_regular_job(app_db)
    result = process_one(client=fake_model, now=NOW)
    assert result.job_id == job.id
    assert job_state(app_db, job.id) == "ready"
    assert shared_plan_exists(app_db, job.signature, job.variant)
    assert fake_model.calls == 1

def test_worker_never_retries_after_dispatch_timeout(app_db, timeout_model):
    queued_regular_job(app_db)
    process_one(client=timeout_model, now=NOW)
    process_one(client=timeout_model, now=NOW + timedelta(minutes=3))
    assert timeout_model.calls == 1
    assert only_job(app_db).state == "failed"
```

- [ ] **Step 2: Run tests and confirm failure**

Run: `python -m pytest tests/test_plan_worker.py -q`

Expected: module import fails.

- [ ] **Step 3: Extract generation orchestration from the HTTP request**

Move the model call, shortlist construction, validation, cost accounting, and cache write behind `server.build_and_store_job`. Keep `poskladaj_novy_plan` temporarily as a compatibility wrapper so existing tests remain green until Task 4 switches the endpoints.

- [ ] **Step 4: Implement worker processing**

The worker must claim one job, verify complete current offers, mark `dispatched_at` immediately before the Anthropic SDK call, save the result transactionally, and release the reservation by entering `ready` or `failed`. A small heartbeat thread updates the worker and lease every 15 seconds while the blocking SDK call runs, then stops in `finally`; this prevents a valid 60–120 second generation from looking dead. A pantry job re-reads the current pantry and compares its SHA-256 signature before dispatch.

```python
def process_one(*, now=None, client=None):
    with server.db() as con:
        job = plan_jobs.claim_next(con, WORKER_ID, now=now or utcnow())
    if job is None:
        return ProcessResult.empty()
    return server.build_and_store_job(job, client=client)
```

- [ ] **Step 5: Add interruption, stale-input, and incomplete-store tests**

Cover worker death before dispatch, worker death after dispatch, pantry changed while queued, week changed while queued, algorithm version changed, and one missing store. Each case must finish without publishing an invalid plan or dispatching a duplicate model call.

- [ ] **Step 6: Run worker and existing generation tests**

Run: `python -m pytest tests/test_plan_worker.py tests/test_server.py tests/test_spajza_oddelena_od_planu.py tests/test_priepustnost.py -q`

Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add app/plan_worker.py app/server.py tests/test_plan_worker.py
git commit -m "feat: generate meal plans in background worker"
```

---

### Task 4: Async plan API and concurrency contract

**Files:**
- Modify: `app/server.py`
- Create: `tests/test_plan_async_api.py`
- Modify: `tests/test_server.py`
- Modify: `tests/test_priepustnost.py`
- Modify: `tests/test_spajza_oddelena_od_planu.py`

**Interfaces:**
- Consumes: Tasks 1 and 3.
- Produces: backward-compatible ready responses and pending payloads:

```json
{
  "prazdny": true,
  "status": "preparing",
  "job_id": 123,
  "retry_after": 4,
  "message": "Plán pripravujeme. Pokojne pokračuj inde."
}
```

- Produces: failed payload with `status`, stable `code`, `message`, and `retry_allowed`.

- [ ] **Step 1: Write API contract tests**

```python
def test_cold_post_returns_202_without_calling_model(client, model_spy):
    response = client.post("/api/plan/generuj")
    assert response.status_code == 202
    assert response.json()["status"] == "preparing"
    assert model_spy.calls == 0

def test_same_signature_from_two_users_creates_one_job(first, second):
    a = first.post("/api/plan/generuj")
    b = second.post("/api/plan/generuj")
    assert a.json()["job_id"] == b.json()["job_id"]
    assert active_job_count() == 1

def test_cache_hit_still_returns_plan_directly(client, cached_plan):
    response = client.get("/api/plan")
    assert response.status_code == 200
    assert response.json()["jedla"]
```

- [ ] **Step 2: Run tests and confirm synchronous behavior fails them**

Run: `python -m pytest tests/test_plan_async_api.py -q`

Expected: cold POST calls the model or returns 200 instead of 202.

- [ ] **Step 3: Replace cold synchronous calls with atomic enqueue**

Keep personal and shared cache lookup first. On a miss, create or join a queue job and return `JSONResponse(status_code=202, content=pending_payload(job))`. Use priority 100 for live requests. A forced regeneration uses a user-scoped key containing the reserved daily sequence so it cannot join an old ready job; repeated clicks while active still join the same job.

- [ ] **Step 4: Make GET adopt completed work and report active/failed work**

For regular plans, recompute the current signature, adopt `plany_zdielane` when ready, then inspect the active job key. For pantry plans, check the user-scoped pantry job and its expected pantry signature. Preserve existing `prazdny/vyzaduje_akciu/obnovit_cez` invalidation semantics when no job exists.

- [ ] **Step 5: Prove limits and costs are atomic**

Add tests for double-click, 20 concurrent identical requests, two different signatures, force limits, failure before dispatch, timeout after dispatch, monthly cap, and an unreadable ledger. Assert that cache hits and job joins do not consume another user regeneration or reservation.

- [ ] **Step 6: Run backend suites**

Run: `python -m pytest tests/test_plan_async_api.py tests/test_server.py tests/test_priepustnost.py tests/test_spajza_oddelena_od_planu.py tests/test_naklady.py -q`

Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add app/server.py tests/test_plan_async_api.py tests/test_server.py tests/test_priepustnost.py tests/test_spajza_oddelena_od_planu.py
git commit -m "feat: return meal plan preparation immediately"
```

---

### Task 5: Navigation-safe pending UI

**Files:**
- Modify: `app/static/app.html`
- Modify: `tests/test_app_html_contract.py`
- Modify: `tests/test_frontend_speed_contract.py`
- Modify: `tests/test_premium_frontend_contract.py`

**Interfaces:**
- Consumes: Task 4 pending/failed API payloads.
- Produces: `setPlanPreparation(response)`, `startPlanPolling()`, `stopPlanPolling()`, and `pollPlanStatus()`.

- [ ] **Step 1: Write frontend contract tests**

```python
def test_generation_does_not_use_the_150_second_browser_timeout():
    generate = declaration(HTML, "function generujPlan() ")
    assert "PLAN_TIMEOUT_MS" not in generate
    assert "method:'POST'" in generate

def test_pending_plan_keeps_navigation_available_and_polls_get_only():
    assert "Plán pripravujeme. Pokojne pokračuj inde." in HTML
    polling = declaration(HTML, "async function pollPlanStatus() ")
    assert "api('/api/plan')" in polling
    assert "/api/plan/generuj" not in polling
```

- [ ] **Step 2: Run tests and confirm failure**

Run: `python -m pytest tests/test_app_html_contract.py tests/test_frontend_speed_contract.py -q`

Expected: current UI waits on `PLAN_TIMEOUT_MS = 150000`.

- [ ] **Step 3: Implement the truthful pending card**

On HTTP 202, stop the elapsed-seconds spinner, store `job_id`, render a compact pending card, leave bottom navigation enabled, and poll `GET /api/plan` every four seconds only while the document is visible. Do not display a percentage or estimated completion time.

- [ ] **Step 4: Handle completion, navigation, reload, and failure**

When GET returns a plan, call `setPlan`, stop polling, refresh `/api/me`, and render. When the tab becomes visible or the app reopens, the normal startup GET resumes the state. On terminal failure, show the server message and one explicit retry button; polling must never POST.

- [ ] **Step 5: Cover regular, force, and pantry flows**

Test that all three POST actions accept 202, a second click cannot create a second browser request, pantry navigation remains usable, and an old pending job cannot overwrite a newer profile/signature response.

- [ ] **Step 6: Run frontend suites**

Run: `python -m pytest tests/test_app_html_contract.py tests/test_frontend_speed_contract.py tests/test_premium_frontend_contract.py tests/test_spajza_frontend_contract.py -q`

Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add app/static/app.html tests/test_app_html_contract.py tests/test_frontend_speed_contract.py tests/test_premium_frontend_contract.py
git commit -m "feat: show background meal plan preparation"
```

---

### Task 6: Queue-based targeted precomputation

**Files:**
- Modify: `app/predpocet.py`
- Modify: `tests/test_predpocet.py`
- Modify: `hetzner/dozorca.sh`
- Modify: `tests/test_dozorca_contract.py`

**Interfaces:**
- Consumes: `plan_jobs.enqueue` from Task 1.
- Produces: `predpocet.enqueue_popular_profiles(*, count=None, now=None) -> dict`.
- Keeps CLI: `python predpocet.py --zahrej`, but it now reports queued, skipped, and blocked counts rather than synchronously generated plans.

- [ ] **Step 1: Write priority and active-profile tests**

```python
def test_precompute_queues_active_exact_profiles_before_defaults(db):
    create_active_user(db, stores="Lidl,Tesco", adults=2, children=2, frequency=3)
    result = enqueue_popular_profiles(count=3, now=NOW)
    jobs = queued_jobs(db)
    assert jobs[0].payload["stores"] == ["Lidl", "Tesco"]
    assert all(job.priority == 20 for job in jobs)
    assert result["queued"] <= 3

def test_live_job_runs_before_precompute_job(db):
    enqueue_precompute(db)
    live = enqueue_live(db)
    assert claim_next(db, "worker", now=NOW).id == live.id
```

- [ ] **Step 2: Run focused tests and confirm current synchronous behavior fails**

Run: `python -m pytest tests/test_predpocet.py -q`

Expected: precompute calls the model directly instead of enqueuing.

- [ ] **Step 3: Replace synchronous precompute generation with low-priority enqueue**

Use complete-store checks, current active users, aggregate recent demand, then default profiles. Deduplicate by signature and variant. Stop before the configured reserve using outstanding reservations plus historical spend. Do not queue a profile that already has a current shared plan or active job.

- [ ] **Step 4: Keep dozorca ordering and idempotence**

After verified flyer refresh, `dozorca.sh` invokes `predpocet.py --zahrej`; that command should finish quickly because it only queues work. The persistent worker performs generation. Preserve the existing hourly recovery and complete-three-store gate.

- [ ] **Step 5: Run precompute and dozorca suites**

Run: `python -m pytest tests/test_predpocet.py tests/test_dozorca_contract.py tests/test_dozorca_chybajuci_obchod.py -q`

Expected: all pass; no test invokes Anthropic from precompute.

- [ ] **Step 6: Commit**

```bash
git add app/predpocet.py hetzner/dozorca.sh tests/test_predpocet.py tests/test_dozorca_contract.py
git commit -m "feat: queue targeted meal plan precomputation"
```

---

### Task 7: Worker supervision, health, and safe deployment

**Files:**
- Create: `hetzner/uvarsi-plan-worker.service`
- Modify: `app/server.py`
- Modify: `hetzner/dozorca.sh`
- Modify: `hetzner/samopull.sh`
- Modify: `nasad.ps1`
- Modify: `tests/test_dozorca_contract.py`
- Create: `tests/test_plan_worker_deployment_contract.py`

**Interfaces:**
- Consumes: Task 1 worker heartbeat and queue metrics.
- Produces: `plan_jobs.health(con, *, now) -> dict` included under `plan_queue` in `/api/health` and `/api/naklady`.

- [ ] **Step 1: Write health and deployment contract tests**

```python
def test_health_reports_queue_and_worker(client):
    health = client.get("/api/health").json()["plan_queue"]
    assert {"queued", "oldest_seconds", "worker_alive", "last_ready", "failed"} <= health.keys()

def test_release_installs_and_restarts_worker_without_touching_other_app():
    assert "uvarsi-plan-worker.service" in SAMOPULL
    assert "systemctl restart uvarsi-plan-worker" in SAMOPULL
    assert "taktik-mapa" not in worker_unit_text()
```

- [ ] **Step 2: Run tests and confirm failure**

Run: `python -m pytest tests/test_plan_worker_deployment_contract.py tests/test_dozorca_contract.py -q`

Expected: worker unit and queue health are missing.

- [ ] **Step 3: Add the systemd unit**

```ini
[Unit]
Description=Uvar.si meal plan worker
After=network.target uvarsi.service

[Service]
WorkingDirectory=/opt/uvarsi/app
Environment=UVARSI_VERSION_FILE=/opt/uvarsi/VERSION
ExecStart=/opt/uvarsi/venv/bin/python -u plan_worker.py
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
```

The worker loads secrets through the existing `server.env()` path from `/opt/uvarsi/uvarsi.env`; the unit must not print or copy key values.

- [ ] **Step 4: Extend health and alerting**

Report queue count, oldest age, worker heartbeat age, last completion, failures, and blocking code. Dozorca sends one ntfy alert when queued work is older than 180 seconds or the worker heartbeat is older than 60 seconds; use a state marker so hourly checks do not spam. Clear the marker after recovery.

- [ ] **Step 5: Make both deployment paths install and roll back the worker**

`nasad.ps1` uploads the unit, runs `systemctl daemon-reload`, enables/restarts both Uvar.si services, and checks both active states. `samopull.sh` copies the unit, restarts the worker only after app health passes, verifies a fresh heartbeat, and restores the previous app/unit on failure. Keep Caddy and the other app untouched.

- [ ] **Step 6: Run deployment contracts**

Run: `python -m pytest tests/test_plan_worker_deployment_contract.py tests/test_dozorca_contract.py tests/test_server.py -q`

Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add hetzner/uvarsi-plan-worker.service app/server.py hetzner/dozorca.sh hetzner/samopull.sh nasad.ps1 tests/test_plan_worker_deployment_contract.py tests/test_dozorca_contract.py
git commit -m "ops: supervise background meal plan worker"
```

---

### Task 8: Release gate, production verification, and timing measurement

**Files:**
- Modify: `VERSION`
- Modify: `RELEASE_LOG.md` only if the existing project release process requires a new factual entry; preserve unrelated user edits.
- Test: entire `tests/` suite.

**Interfaces:**
- Consumes: all earlier tasks.
- Produces: one deployable release containing `ace8ef7` and the hybrid architecture.

- [ ] **Step 1: Run static checks and targeted regression suites**

Run:

```powershell
git -c "safe.directory=C:/Users/Ucet/OneDrive/Online produkt" diff --check
python -m pytest tests/test_plan_jobs.py tests/test_plan_shortlist.py tests/test_plan_worker.py tests/test_plan_async_api.py -q
```

Expected: no whitespace errors; all focused tests pass.

- [ ] **Step 2: Run the complete portable suite**

Run: `python -m pytest -q`

Expected: zero failures. Record the exact passed/skipped counts; do not reuse the previous `1037 passed, 1 skipped` result.

- [ ] **Step 3: Request independent code review**

Review queue atomicity, cost reservations, post-dispatch timeout behavior, pantry isolation, worker rollback, and frontend polling. Resolve every correctness or security finding, then rerun the focused and full suites.

- [ ] **Step 4: Bump the version and commit the release**

Set `VERSION` to the next unused release identifier, then:

```bash
git add VERSION
git commit -m "release: prepare hybrid plan loading"
```

- [ ] **Step 5: Push only after explicit approval of the exact release commit**

Run: `git push origin main`

Expected: origin contains the reviewed release commit. Do not force-push.

- [ ] **Step 6: Let samopull deploy and verify both services**

Run read-only checks over SSH:

```bash
systemctl is-active uvarsi
systemctl is-active uvarsi-plan-worker
curl -fsS http://127.0.0.1:8090/api/health
```

Expected: both services are `active`; health reports the new version, `worker_alive: true`, and no stale queue.

- [ ] **Step 7: Run a reversible production smoke test**

Use a temporary internal session, request one cold regular plan, and assert POST returns 202 in under one second. Poll GET until ready or 120 seconds, record completion time and Anthropic cost, then request the identical profile from a second temporary user and assert a ready response in under one second with no additional AI ledger entry. Delete temporary sessions and smoke-test user rows afterward; do not delete shared production plans or reset any ledger.

- [ ] **Step 8: Verify pantry and navigation behavior manually**

With the authorized account, request a pantry plan, navigate to another tab during preparation, close/reopen the PWA, and confirm the same job finishes. Change pantry contents while another pantry job is queued and confirm the old result is not published as current.

- [ ] **Step 9: Decide against the acceptance targets using measured data**

Record queue acknowledgement, cold completion, cache-hit latency, model input/output tokens, and cost. Release passes when acknowledgement/cache p95 are below one second, the cold sample completes within 120 seconds, no duplicate cost appears, and all data-validity checks pass. If measured cold p95 later exceeds 60 seconds, keep the honest background UX and open a separate optimization task; do not lengthen the browser wait.

- [ ] **Step 10: Final release report**

Report the exact deployed commit/version, full test counts, service state, smoke timings, actual AI cost, remaining known risks, and confirmation that payments are still disabled.
