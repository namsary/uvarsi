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

## Fix round 1/5 — independent review blockers

Date: 2026-08-28

The full-suite timestamp failure and all independent deployment-supervision
findings from review were addressed together. No production command, push,
Caddy edit, other-app edit, or secret-value read/copy occurred.

### Changes

- Queue ages now normalize aware timestamps (including persisted `+02:00`
  values) and legacy naive timestamps to UTC before subtraction. Future clock
  skew is clamped to zero, so health never reports a negative age.
- Queue health now exposes the persisted heartbeat instant as canonical
  UTC-aware `heartbeat_at`; both `/api/health` and `/api/naklady` continue to
  expose the same truthful `plan_queue` object.
- Added `hetzner/uvarsi-deploy-state.sh`, shared by manual and autonomous
  deployment. Its executable tests cover backup failure, rollback after a
  partial live mutation, restore failure propagation, and exact restoration of
  prior absent/present, disabled/enabled, and inactive/active worker state.
- Deployment snapshots read the persisted pre-restart heartbeat marker. A
  restarted worker passes only when health is fully valid, `worker_alive` is
  true, and `heartbeat_at` is a strictly later instant. An otherwise fresh but
  unchanged heartbeat is rejected.
- `nasad.ps1` uploads every file into a complete staging tree before taking the
  live snapshot or changing the live app. After live mutation starts, native
  command failures and unexpected PowerShell errors enter the tested rollback
  path. The helper and worker unit are both in its manifest.
- `samopull.sh` requires the helper in the release manifest, aborts every backup
  failure before live mutation, checks every app/unit/web/script restore, and
  cannot report a successful rollback after any restore failure.
- Dozorca parses JSON with the bundled environment Python and validates all
  required queue fields, scalar types, and null relationships. Missing,
  malformed, or inconsistent health is UNKNOWN: it neither alerts nor clears
  the existing one-shot marker. A complete healthy response still clears it.
- The complete-three-store gate and Task 6 Dozorca ordering were not changed.

### TDD evidence

1. Timestamp RED: the aware `+02:00` endpoint case and both direct queue-health
   regressions failed at `app/plan_jobs.py:560` with `TypeError: can't subtract
   offset-naive and offset-aware datetimes` (`3 failed`).
2. Timestamp GREEN: aware and legacy-naive instants produced the correct UTC
   ages, and future timestamps produced zero (`3 passed`).
3. Deployment RED: the executable state harness failed all six cases because
   safe snapshot/restore/install/fresh-heartbeat behavior did not exist
   (`6 failed`).
4. Dozorca RED: an incomplete `plan_queue` response deleted an existing alert
   marker instead of preserving UNKNOWN state (`1 failed`).
5. GREEN: exact worker-state restoration, failure propagation, strict fresh
   heartbeat comparison, staged deployment contracts, and UNKNOWN marker
   behavior all passed.

### Fresh verification

Bundled Python and `--basetemp=.superpowers\pytest-task7` were used for the
requested focused suites (with the pytest cache provider disabled):

- Queue/worker/health/náklady/server and deployment behavior:
  `197 passed in 71.16s`.
- Deployment manifests, safety contracts, shell handling, and executable
  rollback behavior: `64 passed in 14.20s`.
- Dozorca contract, complete-store gate, and precomputation ordering:
  `73 passed in 49.16s`.
- Final executable helper rerun after self-review adjustment:
  `7 passed in 14.84s`.
- Final full deployment regression rerun after that adjustment:
  `65 passed in 15.59s`.
- `bash -n` passed for `dozorca.sh`, `samopull.sh`, and
  `uvarsi-deploy-state.sh`.
- PowerShell parser passed for `nasad.ps1` with zero errors.
- `git diff --check` passed (only Windows LF/CRLF notices were emitted).

### Self-review

- All rollback targets are limited to Uvar.si app/version, Uvar.si worker unit
  and state, and Uvar.si-owned web/script files. No Caddy or Jarvis/taktik-mapa
  service operation was added.
- The worker unit still loads secrets only through the existing application env
  path; no key value is logged, embedded, or copied.
- Backup happens before live mutation. Restore operations are checked and exact
  worker state is verified after restoration.
- Alert thresholds remain strict (`>180s` queue age, `>60s` heartbeat age), use
  one marker, suppress repeats, and clear only on a complete healthy payload.

## Fix round 2/5 — Windows behavior-test path transport

Date: 2026-08-28

### Root cause and scope

The six full-suite failures were reproduced with a deliberately long Windows
pytest base-temp path containing spaces. Four snapshot-state cases, the
backup/restore failure case, and the partial-mutation rollback case failed at
the first `mkdir`; the heartbeat-only case passed. The failing process showed
the dynamically interpolated absolute snapshot argument truncated at the first
space (`C:/Users/Ucet`).

The unchanged production helper passed all seven cases under the identical
long path when the shell execution boundary did not rewrite the command. This
isolated the defect to test-only Windows subprocess argument transport, not
`uvarsi-deploy-state.sh` quoting or deployment semantics.

The initial path-transport correction changed only
`tests/test_plan_worker_deployment_behavior.py`:

- snapshot and release locations are passed as `UVARSI_TEST_SNAPSHOT` and
  `UVARSI_TEST_RELEASE` environment variables;
- their values are stable relative names (`snapshot`, `release`), anchored by
  running Bash with the pytest case directory as `cwd`;
- Bash invokes `uvarsi_snapshot "$UVARSI_TEST_SNAPSHOT"`,
  `uvarsi_restore "$UVARSI_TEST_SNAPSHOT"`, and `uvarsi_install_core` with the
  quoted environment variables;
- no dynamic absolute pytest path is interpolated into the `bash -c` command
  string.

That correction did not require a production semantic change.

### TDD and verification evidence

1. RED under a long spaced base-temp path: `6 failed, 1 passed in 4.48s`.
   Every path-consuming case failed with the snapshot path truncated at
   `C:/Users/Ucet`; the path-free heartbeat case passed.
2. GREEN under a deliberately long spaced base-temp path without elevated
   execution: `7 passed in 16.50s`.
3. Full deployment suite under a spaced base-temp path:
   `65 passed in 16.70s`.

Assertions remain unchanged: the harness still executes the real shell helper
and verifies exact worker state, backup/restore failures, partial-mutation
rollback, and strict fresh-heartbeat behavior.

### P1 re-review — complete manual rollback surface

The subsequent re-review found that manual deployment staged and mutated more
Uvar.si-owned live targets than its snapshot restored. In addition to app,
VERSION, and the worker unit, it changed landing `index.html`, `sw.js`, five
operational scripts, and `/etc/systemd/system/uvarsi.service`. A later service,
heartbeat, Caddy-validation, cron, payment-check, backup, or final-health
failure could therefore restore the app while leaving a mixed deployment.

The shared deployment-state helper now:

- snapshots and restores `index.html`, `sw.js`, `refresh_blocek.py`,
  `recepty.py`, `dozorca.sh`, `zaloha.sh`, and `uvarsi-deploy-state.sh`, with an
  explicit absence marker for every file;
- snapshots and restores the prior `uvarsi.service` bytes or absence, plus its
  enabled/disabled and active/inactive state;
- verifies each file, unit, daemon-reload, enable/disable, and start/stop
  restore operation;
- provides `uvarsi_install_manual_release`, which installs core app/worker and
  all manual-only targets as one rollback domain.

`nasad.ps1` now creates `uvarsi.service` in the upload staging tree before the
snapshot, invokes the tested full manual-release installer, and no longer
writes the live app unit later as an unsnapshotted mutation. Any later failure
continues to call the same complete `uvarsi_restore` path. No Caddy target or
other application was added to snapshot, install, or rollback handling.

Executable RED/GREEN evidence:

1. RED: the new failure-after-mutation test failed in all three prior app-unit
   states because the full manual installer/rollback did not exist
   (`3 failed in 3.95s`).
2. GREEN: the injected failure occurred only after app, worker, landing files,
   scripts, and app unit had been copied; rollback restored every asserted file
   byte-for-byte, removed a previously absent script, and restored prior app
   unit existence/enabled/active state (`3 passed in 21.04s`).
3. Entire behavior harness under a deliberately long spaced path:
   `10 passed in 43.18s`.
4. Full deployment suite under a deliberately long spaced path:
   `68 passed in 41.81s`.
5. Bash syntax passed for `uvarsi-deploy-state.sh` and `samopull.sh`; the
   PowerShell parser passed for `nasad.ps1`.

Self-review also narrowed the post-install CRLF/mode command from the broad
`/opt/uvarsi/*.sh` glob to the three shell files this manual release actually
installs and snapshots. It therefore cannot mutate an unrelated Uvar.si script
outside the rollback manifest. After that adjustment, the fresh final
deployment suite passed `68 passed in 59.62s` under a long spaced base-temp
path, with Bash and PowerShell syntax checks in the same command.

No secret value, Caddy file, or other application was touched. No push or
production deployment was performed.
