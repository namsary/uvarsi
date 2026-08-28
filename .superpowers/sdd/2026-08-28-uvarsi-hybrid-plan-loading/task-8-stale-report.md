# Task 8: stale async test migration

Worktree: `C:\Users\Ucet\OneDrive\Online produkt\.worktrees\uvarsi-hybrid-plan-loading`

## Scope

Updated only the four stale async/cost tests in:

- `tests/test_naklady_integracia.py`
- `tests/test_naklady_kredit.py`

No backend, deployment, push, or deploy changes were made for Task 8. The
test fixtures now select Lidl explicitly because `current_plan_rows()` contains
only Lidl offers; this lets the real worker pass its current-store validation.

## Contract covered

- Successful plan generation asserts POST `202`, observes the queued `0.12 €`
  reservation and one force regeneration slot, runs
  `app.plan_worker.process_one()` through `build_and_store_job`, then verifies
  the shared plan and actual Anthropic usage ledger row.
- Credit exhaustion asserts POST `202`, a terminal worker failure with the
  persisted `generation_failed` code, and GET `/api/plan` returning the stable
  failed payload without a fabricated plan.
- Repeated active force requests join one job without duplicate reservations.
  After the worker terminal failure, the outstanding euro reservation is
  released, the usage ledger remains empty, and the consumed free daily force
  slot is retained; another force request returns the current `429` limit
  response.
- Health and owner cost views are queried only after the worker failure and
  assert the persisted credit marker, zero spend/ledger entries, zero queued
  jobs, one failed job, and a live worker heartbeat.

## Verification

Bundled Python:

`C:\Users\Ucet\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe`

Baseline focused run before migration: 4 failed stale tests, all failing on
the old synchronous expectations (`200`/`503` POST responses, missing worker
mutation, or zero force-slot usage).

Focused migrated run:

`4 passed in 2.14s`

Required full run, using `--basetemp=.superpowers\pytest-task8-stale`:

`tests/test_naklady_integracia.py tests/test_naklady_kredit.py tests/test_plan_async_api.py tests/test_plan_worker.py tests/test_naklady.py`

Result: `145 passed in 23.42s`.

`git diff --check` passed for the Task 8 diff. Existing unrelated worktree
changes were not staged.
