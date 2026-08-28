# Task 8 — frontend polling race fix

## Scope

Worktree: `C:\Users\Ucet\OneDrive\Online produkt\.worktrees\uvarsi-hybrid-plan-loading`

Changed only:

- `app/static/app.html`
- `tests/test_app_html_contract.py`
- this report

No push or deploy was performed.

## Root cause

Polling used a global boolean for the in-flight GET. After a context invalidation, the new context's timer correctly refused to start a duplicate GET while the old request was pending. When the old request settled, its `finally` block only scheduled another timer if its captured context version was still current. Because that version was stale, the new context was left without polling.

## Fix

The in-flight state is now a request token containing the preparation and context version. The settling request clears the token only if it still owns it, then schedules the currently active visible preparation. Stale responses remain blocked by the existing preparation/version guard. Terminal and ready responses clear the preparation before settlement, so polling stops normally.

## TDD evidence

- RED: the new deterministic contract failed with `new context was left without a follow-up timer` on the old implementation.
- GREEN: the same contract passed after the token-scoped settlement fix.
- The contract reproduces: old GET in flight → invalidate → new timer fires → old GET settles. It verifies no duplicate GET, no stale render, and continued polling in the new context.

## Verification

Focused polling contracts:

```text
4 passed
```

Full frontend contract selection (`test_app_html_contract.py`, `test_frontend_speed_contract.py`, `test_premium_frontend_contract.py`, `test_spajza_frontend_contract.py`, `test_task_a_resume.py`):

```text
102 passed, 1 skipped, 5 failed
```

The five failures are unrelated to this fix: four legacy `cscript.exe` contracts are blocked by sandbox `Access is denied`, and the existing pantry-refresh contract extracts a function that calls `invalidatePlanState()` without providing that helper in its isolated harness.
