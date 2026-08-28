# Task 8 fix A — credit exhaustion and reservation integrity

Date: 2026-08-28
Worktree: `C:\Users\Ucet\OneDrive\Online produkt\.worktrees\uvarsi-hybrid-plan-loading`

## Scope

Implemented the final-review credit/reservation fix only. No subagents, push,
deployment, production command, payment change, or unrelated concurrent edit
was performed.

Changed files:

- `app/naklady.py`
- `app/plan_jobs.py`
- `app/server.py`
- `app/predpocet.py`
- `app/zbierac_akcii.py`
- `tests/test_naklady_kredit.py`
- `tests/test_plan_jobs.py`
- `tests/test_zbierac_akcii.py`

## Root cause and fix

- `naklady_kredit` was persisted and displayed by health endpoints, but the
  pre-call budget gate did not consult it. `plan_jobs.enqueue` could therefore
  create a job and reserve a daily regeneration after credit exhaustion was
  already known.
- Queue reservations were checked while enqueueing, but were omitted from the
  guarded model gate used by the plan worker and collector. A non-queue model
  call could consume capacity already promised to queued jobs.
- The worker converted the typed credit failure to a generic failure payload,
  which made the retry status and user-facing message untruthful.

`naklady.skontroluj` now fail-closes on a persisted credit marker before any
slot/reservation mutation. Because `plan_jobs.enqueue` runs this check inside
its existing `BEGIN IMMEDIATE` transaction and before `_reserve_user_regeneration`,
the rejected request creates neither a job nor a daily slot.

`StrazenyKlient` now carries either a reservation total or a late-bound
reservation reader into each actual `s_rozpoctom` gate. The plan worker reads
all active reservations except its claimed job; its own estimate supplies that
job's one permitted call, while every other queued/running reservation remains
protected. The collector and precompute paths read all active plan reservations
at their guarded call boundaries. The collector also migrates the queue schema on its
standalone database connection. This introduces no circular import:
`plan_jobs` depends on `naklady`, while the collector only reads
`plan_jobs`.

Credit failures are terminal (`kredit_vycerpany`), `retry_allowed` is false,
and failed-plan payloads use the credit-exhaustion message. Existing explicit
marker clearing (`zabudni_kredit`) remains the recovery action after a credit
top-up; a successful call cannot prove recovery while the fail-closed marker
correctly prevents it from being dispatched.

## TDD evidence

1. RED: added regressions for marker-before-enqueue, current-job reservation
   exclusion with a second reservation, collector/queue interleaving, and
   truthful credit failure. The focused run failed exactly because the marker
   was ignored, `StrazenyKlient` accepted no reservation reader, and the
   collector had no guarded reservation path (`3 failed, 1 passed`).
2. GREEN: added marker enforcement, exclusion-aware reservation totals,
   guarded-call propagation, collector wiring/schema migration, and truthful
   terminal credit error propagation (`5 passed`).
3. RED: the precompute guarded call ignored a live reservation and dispatched
   its fake model (`1 failed`).
4. GREEN: the precompute path now forwards the active reservation reader
   (`1 passed`).
5. Regression adjustment: existing tests correctly exposed the new fail-closed
   contract. They now verify that a known marker emits no further collector
   request, and that recovery requires explicit marker clearing before a
   successful ledger write.

## Fresh verification

Bundled Python:
`C:\Users\Ucet\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe`

Fresh isolated pytest directories were used because the default shared Windows
pytest temp root was inaccessible.

- `tests/test_naklady_kredit.py`: `35 passed in 9.92s`.
- `tests/test_plan_jobs.py`: `26 passed in 7.85s`.
- `tests/test_plan_worker.py`: `26 passed in 20.05s`.
- `tests/test_plan_async_api.py`: `28 passed in 14.97s`.
- `tests/test_zbierac_akcii.py`: `26 passed in 1.31s`.
- `tests/test_predpocet.py::test_predpocet_model_gate_preserves_capacity_reserved_by_live_plan_jobs`:
  `1 passed in 5.38s`.
- `git diff --check`: passed; Git emitted only Windows LF/CRLF normalization
  warnings.

Focused coverage includes queue/collector interleaving, multiple reservations,
the current-job exclusion, persisted marker enqueue/retry/daily-slot behavior,
and zero-ledger/returned-weekly-run behavior for credit rejection.

## Review notes

- The enqueue budget check and regeneration reservation remain one SQLite
  immediate transaction; a budget or marker rejection rolls back both.
- The current claimed job is excluded only from reservation accounting, not
  from the actual estimate supplied to its guarded model call, so capacity is
  neither double-counted nor released early.
- Successful and ordinary failed calls retain the existing ledger behavior;
  only confirmed pre-dispatch credit rejection produces no ledger entry and no
  weekly-run consumption.
- Existing unrelated concurrent changes in deployment/worker test files and
  shell scripts were left unstaged and unmodified.
