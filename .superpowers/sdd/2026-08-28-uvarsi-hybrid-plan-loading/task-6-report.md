# Task 6 evidence — Queue-based targeted precomputation

Date: 2026-08-28
Worktree: `C:\Users\Ucet\OneDrive\Online produkt\.worktrees\uvarsi-hybrid-plan-loading`

## Scope

Implemented Task 6 only in:

- `app/predpocet.py`
- `tests/test_predpocet.py`
- `hetzner/dozorca.sh`
- `tests/test_dozorca_contract.py`

No backend server, frontend, deployment, push, or subagent work was performed.

## Implementation evidence

- Added `enqueue_popular_profiles(*, count=None, now=None)`.
- Candidate ordering is active exact user profiles, recent aggregated demand, then defaults.
- Queue jobs use `kind='precompute'`, priority `20`, stable `precompute:<signature>:<variant>` keys, and the Task 1 payload contract.
- Existing current shared plans and any active matching job (including live jobs) are skipped.
- The producer never imports or calls Anthropic on the `--zahrej` path; generation remains worker-owned.
- Historical spend plus active queue reservations are included before preserving the configured live-user reserve.
- CLI output reports queued, skipped, and blocked counts and returns quickly after enqueue decisions.
- Dozorca still refreshes the landing before warming, requires the complete three-store gate, keeps the process lock, and invokes the queue handoff on every eligible hourly run. Handoff failure remains non-fatal so hourly recovery continues.

## TDD evidence

1. RED: five new Task 6 tests failed because `enqueue_popular_profiles` was absent.
2. GREEN: those five tests passed after the initial implementation.
3. RED: the matching active live-job regression failed because the first query only filtered precompute jobs.
4. GREEN: removing that kind filter made the regression pass and preserved live priority ordering.

## Verification

Bundled Python: `C:\Users\Ucet\AppData\Local\Programs\Python\Python312\python.exe`

Command:

```text
python -m pytest -p no:cacheprovider tests/test_predpocet.py tests/test_dozorca_contract.py tests/test_dozorca_chybajuci_obchod.py -q --basetemp=.superpowers\pytest-task6
```

Result: `69 passed in 24.04s`.

Additional checks:

- `python -m py_compile app/predpocet.py` — passed.
- `git diff --check` — passed; only Git’s LF/CRLF normalization warnings were emitted.

The full repository suite completed with `1129 passed, 47 skipped, 6 failed`. The
six failures are outside this task’s permitted change set: two deployment
manifest checks require unrelated queue-worker modules to be added to the
deployment script, and four older synchronous API/credit tests still expect
the pre-existing HTTP 200/503 Anthropic behavior instead of the already-landed
HTTP 202 queue handoff. No changes were made to those out-of-scope files.

The pre-change baseline for the same focused suite was `60 passed, 2 failed`; both failures were existing Task 4 async expectations asserting HTTP 200/model execution where the already-landed async API returns HTTP 202. The dozorca contracts passed in that baseline.

## Fix round 1/5 — complete required-store collection gate

Root cause: `enqueue_popular_profiles` checked only the offer rows needed by
each selected profile. A Lidl-only profile could therefore enter the queue
while Kaufland or Tesco had no successful current collection.

The producer now fails closed before reserving a precompute run or inserting
any queue row. It uses the server's authoritative
`stores_missing_this_week` metadata and current verified offer catalogue,
requiring successful current collection status, enough verified offers, and
representation of Lidl, Kaufland, and Tesco. An incomplete gate returns all
target profiles as blocked, queues zero jobs, and never imports or calls
Anthropic. The direct `predpocet.py --zahrej` path uses the same producer gate.

TDD evidence:

1. RED: the missing-row unit test and failed-store CLI test both queued jobs;
   the complete-three-store control passed (`2 failed, 1 passed`).
2. GREEN: all three direct unit/CLI cases passed after the gate (`3 passed`).
3. Full `tests/test_predpocet.py`: `55 passed`.

Focused verification with bundled Python and
`--basetemp=.superpowers\pytest-task6-fix1`:

```text
python -m pytest -p no:cacheprovider tests/test_predpocet.py tests/test_dozorca_contract.py tests/test_dozorca_chybajuci_obchod.py -q --basetemp=.superpowers\pytest-task6-fix1 -k "not test_dozorca_alerts_once_for_a_stalled_plan_queue_and_clears_after_recovery"
```

Result: `72 passed, 1 deselected in 45.80s`. The one deselected case is an
uncommitted Task 7 watchdog-alert test that the user explicitly required this
fix to preserve. The combined run without deselection produced `72 passed,
1 failed`; that sole failure was the same Task 7 test. No Task 7 file was
edited for this fix.
