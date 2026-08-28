# Task 7 evidence — Worker supervision, health, and safe deployment

Date: 2026-08-28
Worktree: `C:\Users\Ucet\OneDrive\Online produkt\.worktrees\uvarsi-hybrid-plan-loading`
Parent commit preserved: `6da1be8` (`fix: require complete flyers for precomputation`)

## Scope

Implemented Task 7 only. No push, production deployment, Caddy change, other-app
change, payment change, or subagent work was performed.

Files changed:

- `app/plan_jobs.py`
- `app/server.py`
- `hetzner/uvarsi-plan-worker.service`
- `hetzner/dozorca.sh`
- `hetzner/samopull.sh`
- `nasad.ps1`
- `tests/test_dozorca_contract.py`
- `tests/test_plan_worker_deployment_contract.py`

## Implementation evidence

- Added `plan_jobs.health(con, *, now)` with queued count, oldest queued age,
  worker heartbeat age/aliveness, last ready completion, failed count, and a
  stable blocking code.
- Added truthful `plan_queue` data to both `/api/health` and owner-only
  `/api/naklady`.
- Added `uvarsi-plan-worker.service`. It starts `plan_worker.py` from the
  existing application directory and contains no secret or key values. Worker
  secrets continue to load through the existing `server.env()` path.
- Dozorca alerts when the oldest queued job exceeds 180 seconds or the worker
  heartbeat exceeds 60 seconds. One `.plan_queue_alert_state` marker suppresses
  repeated hourly alerts and is removed after a readable healthy response.
- Dozorca treats missing/incompatible queue-health data as unknown instead of
  sending a false alert. Curl is injectable for deterministic shell tests.
- `samopull.sh` requires all queue/shortlist/worker release files, installs and
  enables the unit, verifies app health before restarting the worker, verifies
  a fresh worker heartbeat, and restores the prior app/version/unit on failure.
- `nasad.ps1` uploads all new modules and the unit, enables/restarts/checks both
  services, verifies the heartbeat, and restores its pre-deploy app/version/unit
  backup if service startup or final health validation fails.
- Existing Caddy and taktik-mapa/Jarvis configuration is outside every changed
  block. The Task 6 complete-three-store gate and refresh-before-precompute
  ordering are unchanged.

## TDD evidence

1. RED: deployment/health contracts failed because `plan_queue`, the worker
   unit, and the Dozorca alert marker behavior were absent (`3 failed,
   13 passed`).
2. GREEN: queue health, the worker unit, deployment supervision, and one-marker
   alert/recovery behavior were implemented.
3. RED: the manual deployment safety contract failed because `nasad.ps1` did
   not verify heartbeat or restore the prior app/unit (`1 failed, 2 passed`).
4. GREEN: manual backup, heartbeat validation, and rollback were added
   (`3 passed`).
5. RED: the release-manifest contract caught missing `app/plan_shortlist.py`
   validation in `samopull.sh` (`1 failed, 2 passed`).
6. GREEN: the complete worker release manifest passed (`3 passed`).

## Fresh verification

Bundled Python:
`C:\Users\Ucet\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe`

All pytest commands used `--basetemp=.superpowers\pytest-task7` and disabled
the cache provider.

- `tests/test_dozorca_contract.py`: `14 passed in 26.62s`.
- `tests/test_plan_worker_deployment_contract.py tests/test_server.py`:
  `99 passed in 24.78s`.
- `tests/test_plan_jobs.py tests/test_plan_worker.py tests/test_naklady.py`:
  `90 passed in 14.57s`.
- Final deployment contract after the manifest fix: `3 passed in 0.96s`.

The fresh unique focused coverage totals 203 tests: 3 deployment, 14 Dozorca,
96 server, and 90 queue/worker/cost tests.

Additional checks:

- `bash -n hetzner/dozorca.sh hetzner/samopull.sh` — passed.
- PowerShell parser check for `nasad.ps1` — passed with zero parse errors.
- `git diff --check` — passed; Git emitted only existing LF/CRLF normalization
  warnings on Windows.

## Self-review

- Secrets: no API key value is copied, printed, or embedded in the unit or
  deployment changes.
- Health truthfulness: timestamps come from persisted queue/worker state and
  ages use UTC; a missing heartbeat is not reported alive.
- Alerting: both required thresholds are strict (`>180`, `>60`), one marker
  prevents spam, and recovery clears it.
- Deployment: both app and worker are installed, enabled, restarted, and
  checked. Worker heartbeat failure enters rollback.
- Rollback: prior Uvar.si app/version/unit are restored; a previously absent
  worker unit is removed and disabled. No Caddy or other-app service is touched.
- Task 6: commit `6da1be8`, complete-three-store gating, and Dozorca ordering
  remain preserved.
