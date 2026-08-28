# Task 5 report: navigation-safe pending UI

## Scope

Implemented only the Task 5 frontend change in `app/static/app.html` and the
frontend contract tests in:

- `tests/test_app_html_contract.py`
- `tests/test_frontend_speed_contract.py`
- `tests/test_premium_frontend_contract.py`

No backend files were modified.

## Implementation evidence

- Regular, force, and pantry plan POSTs use the shared in-flight guard and
  accept the server's `status: "preparing"` response without a browser timeout.
- The pending card displays exactly: `Plán pripravujeme. Pokojne pokračuj inde.`
- Navigation remains enabled while preparation is pending.
- `pollPlanStatus()` uses only `GET /api/plan`, schedules checks every 4000 ms,
  and pauses while the document is hidden.
- A normal startup `GET /api/plan` restores a pending job after reopening.
- Terminal failures preserve the server message and show an explicit retry
  button.
- Pending acknowledgements prevent a second POST after the first 202 response.
- Context-version checks prevent stale plan/profile responses from replacing a
  newer profile or pantry state.
- Elapsed progress, percentages, estimated completion times, and the old
  150-second browser wait were removed.

## Test evidence

Runtime used:

`C:\Users\Ucet\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe`

Basetemp used:

`.superpowers\pytest-task5`

Focused red run before implementation: the new pending, polling, and failure
contracts failed because the state-machine functions were absent.

Passing regression run:

```text
tests/test_plan_async_api.py: 24 passed in 6.79s
```

Passing frontend contract run with four pre-existing Windows cscript checks
excluded:

```text
71 passed, 27 skipped, 5 deselected
```

The complete frontend command reached `71 passed, 28 skipped`, but four
unrelated legacy tests that invoke `C:\Windows\System32\cscript.exe` were
blocked by the environment with `Access is denied`. An escalated retry was
also blocked during pytest basetemp cleanup. The Task 5 tests themselves pass.

`git diff --check` completed without whitespace errors.

## Commit

Pending commit: `feat: show background meal plan preparation`
