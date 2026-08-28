# Task 8 — Bratislava plan-calendar fix

## Root cause

`app/plan_worker.py` created a UTC timestamp and immediately removed its
timezone. The worker then passed that naive value into the server's plan
context check, which calls the existing `monday(today)` contract. Around a
Bratislava midnight, the UTC calendar date can still be Sunday, so a valid
Monday job was rejected as `stale_week`.

## Change

- Added `app/plan_calendar.py` as the authoritative conversion boundary.
  It normalizes aware instants to UTC and treats legacy naive timestamps as
  UTC before deriving a `Europe/Bratislava` calendar day or week.
- Kept worker queue operations (claim, lease renewal, dispatch, completion,
  heartbeat) on aware UTC instants.
- Passed only the Bratislava calendar `date` to the existing server context
  contract. `app/server.py` is unchanged.
- Pinned `TZ=Europe/Bratislava` in the worker unit as defense in depth; the
  Python calendar conversion remains correct on a UTC host without it.

## TDD and verification

- Red: the new focused tests initially failed because `app.plan_calendar`
  did not exist.
- Green: `python -m pytest -q tests/test_plan_calendar.py ...` passed 9
  focused tests.
- Final scoped run passed:

  ```text
  42 passed in 22.19s
  ```

The time tests cover `2026-08-31 00:30 +02:00`, both 2026 DST transitions,
a UTC host setting, and legacy naive UTC timestamps. Existing worker and
deployment tests continue to cover queue leases and UTC health-age reporting.
