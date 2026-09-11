# Task 4 report — cost, deployment and production gates

## Status

Task 4 is implemented and committed. The deployment paths now fail closed on
payments, Tesco bridge configuration/reachability, current three-store data,
receipt validity and runtime health. No production network, SSH, Cloudflare or
Anthropic call was made during this task.

Implementation commit: `500a04816e23b1ccb5d92d94c71987572f79c5d4`

## Implemented behavior

- Production bridge configuration is read from the protected Hetzner env file
  without sourcing or printing it. URL, environment and secret shape are
  validated before any authenticated request.
- The bearer header reaches curl through stdin config, not argv. Curl output and
  provider bodies are suppressed; cost diagnostics redact known secrets,
  bearer headers and structured provider error bodies before persistence or
  diagnostic return.
- Automatic and manual deploy paths prove payments OFF before bridge access and
  before live mutation. Post-deploy readiness proves payments remain OFF.
- Readiness requires active app and plan worker, fresh worker heartbeat, healthy
  supervisor state, reachable co-hosted Taktik, current validated landing JSON,
  exactly three meals, and current Kaufland/Tesco/Lidl data with at least 20
  offers per store, exact official collector kinds and matching active/staging
  source fingerprints.
- The supervisor entrypoint is bounded by GNU timeout to 14,400 seconds, with
  TERM followed by a five-minute KILL allowance. It does not restore or replace
  the production database or landing JSON on timeout.
- The existing collector behavior for an unchanged verified fingerprint is
  covered by a regression test proving no Anthropic construction/call and no
  additional collection budget unit or cost row.
- Operator documentation gives exact interactive Cloudflare secret commands,
  exact protected Hetzner env keys, bridge/readiness checks and the bounded cron
  entrypoint. Existing Caddy/Taktik configuration is not changed.

## TDD evidence

### Baseline

Before Task 4 production changes:

```text
python -m pytest -q tests/test_deploy_safety.py tests/test_stropy_pokryju_cely_zber.py
54 passed in 0.89s
```

### RED

The first valid RED run used a workspace-local pytest temp directory because
the host's default pytest temp root denied access:

```text
python -m pytest -q tests/test_tesco_bridge_deployment.py tests/test_deploy_safety.py tests/test_stropy_pokryju_cely_zber.py --basetemp .test-tmp/task4-red-evidence
10 failed, 69 passed in 3.34s
```

The failures showed the missing bridge preflight/readiness functions, missing
bounded supervisor entrypoint, missing four-hour ceiling and unsanitized cost
detail. The unchanged-fingerprint characterization already passed against the
concurrent Task 3 collector (`1 passed in 0.95s`), so Task 4 preserved that
behavior instead of duplicating collector logic.

Two later self-review regressions were also driven RED before their fixes:

```text
test_both_release_paths_prove_payments_off_before_bridge_access
1 failed in 0.43s

test_cost_details_redact_bridge_anthropic_and_bearer_secrets
1 failed in 0.40s
```

### GREEN

Final Task 4 focused verification, excluding one pre-existing deployment test
currently broken by the concurrently modified out-of-scope `dozorca.sh`:

```text
python -m pytest -q tests/test_tesco_bridge_deployment.py tests/test_stropy_pokryju_cely_zber.py tests/test_naklady.py tests/test_deploy_safety.py -k "not test_postdeploy_check_uses_the_dozorca_offer_threshold" --basetemp .test-tmp/task4-focused-final-1
129 passed, 1 deselected in 22.06s
```

Additional relevant verification before the concurrent Task 3 files changed
again:

```text
tests/test_naklady.py tests/test_naklady_kredit.py tests/test_naklady_integracia.py tests/test_tesco_bridge_deployment.py tests/test_deploy_safety.py tests/test_stropy_pokryju_cely_zber.py
179 passed, 3 skipped in 35.98s
```

Static verification:

- `bash -n hetzner/uvarsi-deploy-state.sh hetzner/samopull.sh` — PASS
- PowerShell parser for `nasad.ps1` — PASS
- `git diff --cached --check` for the implementation commit — PASS

All bridge/runtime calls in the tests use local fake curl, systemctl, timeout
and supervisor executables. The Anthropic regression installs a local fake
module and fails if a client is constructed.

## Full-suite blockers

The exact root-suite command could not collect the repository because eight
untracked directories owned by concurrent Task 5 work deny traversal:

```text
PermissionError: [WinError 5] Access is denied:
task5-fix2-expired-delivery-5n1st730
task5-fix2-repeat-legacy-kvk0yy1l
task5-fix2-retry-duplicates-xjckzyxo
task5-fix2-stale-lease-x__f3fy6
task5-fix2-upgrade-token-weju0n_c
task5-fix3-legacy-t889i57k
task5-fix3-legacy2-6dkiqtam
task5-fix3-null-1l8jhq17
```

Running the relevant set after concurrent changes produced `8 failed, 172
passed, 3 skipped`. Seven failures are in legacy cost/collector tests and trace
to the concurrently modified out-of-scope `app/zbierac_akcii.py`; one is
`tests/test_deploy_safety.py::test_postdeploy_check_uses_the_dozorca_offer_threshold`
and traces to the concurrently modified out-of-scope `hetzner/dozorca.sh`, which
no longer contains the numeric literal expected by that old contract. These
files were not reset, edited or staged by Task 4.

## Files changed

- `app/naklady.py`
- `hetzner/samopull.sh`
- `hetzner/uvarsi-deploy-state.sh`
- `nasad.ps1`
- `docs/prevadzka.md`
- `tests/test_tesco_bridge_deployment.py`
- `tests/test_deploy_safety.py`
- `tests/test_stropy_pokryju_cely_zber.py`

## Remaining risks

- Production bridge and Taktik reachability were intentionally not exercised;
  Task 5 must run the documented operator checks against real infrastructure.
- Official automated source kinds remain legally `pending_permission`; payment
  readiness must therefore remain blocked even though this deployment gate
  accepts the exact official kinds for the free, payments-OFF continuity path.
- Existing server cron needs the documented one-time operator replacement to
  use the bounded entrypoint. `nasad.ps1` deliberately preserves the shared
  crontab so it cannot disturb the co-hosted Taktik job.
- The shared repository emitted a permission warning while trying to clean
  unrelated stale worktree metadata for `uvarsi-protect-costs`; the Task 4
  implementation commit itself succeeded and contains only the eight files
  listed above.

---

## Review fix round 1 — 2026-09-11

Status: complete. Implementation commit:
`15493d631d6029e6787372d5373aa0aa77191c85`.

### Reviewer findings resolved

- `nasad.ps1` now sends and installs the replacement crontab. Both manual and
  automatic deployment paths install exactly one bounded Uvar.si supervisor
  row and preserve unrelated rows, including Taktik. Rollback restores the
  previous Uvar.si schedule.
- The timeout now sends TERM at 14,100 seconds and allows 300 seconds for
  shutdown before KILL, so the complete collector/receipt cycle has a hard
  14,400-second maximum rather than 4 h 05 min.
- A stale receipt or current aggregator provenance no longer creates a circular
  bootstrap gate. With payments OFF, the bounded cycle first refreshes official
  collection and the receipt, validates the strict state, and only then admits
  production readiness. The normal supervisor path remains the fast path when
  the strict state is already current.
- Bridge admission is pinned to the configured `workers.dev` Worker hostname
  and a reviewed 12–64-character lowercase hexadecimal release. The protected
  bearer secret is release-bound (`<release>.<random-suffix>`) and must be
  rotated with every Worker release. Tesco manifest and offer provenance must
  use the exact official hypermarket flyer URL and matching validity slug.
- Readiness now enforces the publishable receipt contract: current validity,
  real non-empty items, unique offer references, official source links,
  positive bounded quantities/prices/totals, exact arithmetic and exact
  agreement with active/staging offer facts.
- Readiness requires both the exact installed bounded schedule and a fresh
  success marker written atomically only after a successful bounded cycle.
- Inherited Bash xtrace is disabled before any protected env value is read or
  used. Tests prove the release-bound bridge secret does not appear in output.
- The bridge check in `samopull.sh` remains before the first live mutation but
  follows non-mutating release validation and snapshots. This keeps the
  release-content integration harness isolated without weakening the gate.

Payments remain fail-closed and OFF throughout. No real network, SSH,
Cloudflare or Anthropic call was made; all external commands in tests were
local fakes.

### TDD evidence

RED runs captured the individual defects before implementation:

```text
inherited-xtrace secret test: 1 failed, 22 deselected in 7.54s
samopull bounded-run ordering: 1 failed, 46 deselected in 1.10s
multiple official source pages: 1 failed in 3.96s
crontab read failure fail-closed: 1 failed in 2.01s
current aggregator bootstrap: 1 failed in 10.02s
release-content/deploy compatibility: 1 failed, 63 passed in 1.84s
```

Final focused GREEN verification:

```text
tests/test_tesco_bridge_deployment.py
40 passed in 63.07s

tests/test_recipe_catalog_deployment.py::{two samopull integration tests}
+ tests/test_deploy_safety.py + tests/test_stropy_pokryju_cely_zber.py
64 passed in 1.90s

tests/test_plan_worker_deployment_behavior.py
19 passed in 65.90s

tests/test_naklady.py
50 passed in 2.00s
```

Static verification also passed: Bash syntax for both deployment scripts,
PowerShell parsing for `nasad.ps1`, and Git whitespace checks.

Three broader application integration modules could not be collected with the
bundled Python because `fastapi` is unavailable. The existing workspace-local
dependency directory is owned by concurrent work and denies traversal, and no
network dependency installation was attempted. This does not affect the
focused Task 4/deployment runs above.

### Files changed in round 1

- `docs/prevadzka.md`
- `hetzner/samopull.sh`
- `hetzner/uvarsi-deploy-state.sh`
- `nasad.ps1`
- `tests/test_deploy_safety.py`
- `tests/test_tesco_bridge_deployment.py`

`app/naklady.py` and `tests/test_stropy_pokryju_cely_zber.py` required no code
change in this review round and were not included in the implementation commit.

### Remaining operational risk

The allowed Task 4 scope cannot add a signed release claim to the Worker
response itself. Release pinning therefore depends on the documented
deployment invariant that the protected secret is prefixed with, and rotated
for, the reviewed Worker commit SHA. Production reachability and cron execution
remain operator verification steps because real infrastructure calls were
explicitly prohibited for this task.

## Fix round 2 — complete schedule safety and bound Worker identity

Implementation commit: `bfb26378911779b77c88860e5d3d111c7cd77790`.

### Implemented behavior

- Deployment reads the current crontab before any change and fails closed on a
  transient read error. The snapshot contains the complete crontab with mode
  `0600`; installation submits one complete candidate, and rollback restores
  and byte-checks the complete snapshot. Taktik and unrelated rows remain
  unchanged.
- The Tesco Worker exposes its reviewed release and immutable Cloudflare
  version ID. It HMAC-signs those values, the request date and format, and the
  complete normalized leaflet. Hetzner accepts only the configured release,
  version, official Tesco provenance, and valid response-bound signature.
- Receipt readiness compares every public item with its exact active DB offer:
  name, unit, quantity, sale and original prices, discount, loyalty price and
  conditions, source reference, and validity. It recomputes line and receipt
  totals from those DB facts.
- `dozorca.sh` returns status 75 when its lock is busy. The bounded wrapper
  propagates that status and never writes a success marker for the skipped run.
- The existing 14,400-second absolute cap, xtrace shutdown before secret reads,
  payments-OFF gates, staged rollback behavior, and Taktik isolation remain in
  force.

### TDD evidence

The first Python RED run covered bridge identity, strict receipt fields,
complete crontab restoration, and production schedule installation:

```text
13 failed, 1 passed, 44 deselected in 49.52s
```

The Worker RED run proved that the response and config lacked release/version
attestation:

```text
3 failed
```

The separate deployment/liveness RED run proved that `nasad.ps1` lacked the
production schedule helper and version check, and that lock contention returned
false success:

```text
3 failed in 1.53s
```

Final bounded GREEN verification used workspace-local base directories and the
checked-in dependency directory. Each command ran under a hard process timeout:

```text
tests/test_tesco_bridge_deployment.py
55 passed in 178.50s (240s timeout)

tests/test_deploy_safety.py
tests/test_stropy_pokryju_cely_zber.py
tests/test_deploy_shell_scripts.py
65 passed in 1.09s (120s timeout)

tests/test_refresh_contract.py
tests/test_dozorca_contract.py
tests/test_dozorca_behavior.py
+ 10 staging/promotion cases from tests/test_zbierac_akcii.py
73 passed in 148.82s (330s timeout)

tests/test_plan_worker_deployment_behavior.py
19 passed in 97.53s (180s timeout)

tests/test_plan_worker_deployment_contract.py
4 passed, 1 dependency deprecation warning in 2.83s (120s timeout)

tests/test_recipe_catalog_deployment.py
35 passed in 45.17s (180s timeout)

cloudflare/tesco-bridge/test/worker.test.js
22 passed in 631.66ms (60s timeout)
```

Bash parsed `uvarsi-deploy-state.sh`, `samopull.sh`, and `dozorca.sh` under a
30-second limit. PowerShell parsed `nasad.ps1`, and `git diff --cached --check`
returned no errors. Tests used local fakes; they made no real network, SSH,
Cloudflare, or Anthropic calls.

### Hung test diagnosis

One long parallel regression stalled after test 162 while
`test_runtime_payment_gate_accepts_explicit_false_from_live_health` waited for
its Bash test harness. The root Python PID 27332 held child PIDs 57016 and
50280 with negligible CPU progress. The test-harness child chain, not the
production code, had stalled under the parallel Windows/MSYS run: the same test
passed alone in 0.91s, and its complete file passed serially (19/19) under the
180-second limit. Only that verified process tree was terminated. All final
runs were serial and bounded.

### Broader regression blockers outside this round

The first broad run produced `289 passed, 3 skipped, 10 failed`. The in-scope
deploy selector failure was fixed and passed. The remaining failures predate or
sit outside this round's allowed files:

- Seven `test_naklady_kredit.py` / `test_naklady_integracia.py` collector tests
  still mock the pre-staging collector path. They fail before their cost
  assertions under the current Task 3 collector. The previous Task 4 report
  already records this incompatibility.
- Two `test_dozorca_chybajuci_obchod.py` assertions expect the old active-row
  expressions replaced by Task 3C staging and fingerprint gates.
- Six cases in `test_receipt_data.py` create only the retired active-table
  fixture and therefore fail before receipt construction because
  `zber_staging_stav` is absent. The current Task 3C receipt contract and real
  staging/promotion cases pass in the 73-test bounded group above.

Changing these tests or `app/zbierac_akcii.py` would violate the approved scope,
so this round leaves them untouched.

### Files changed in the implementation commit

- `cloudflare/tesco-bridge/src/worker.js`
- `cloudflare/tesco-bridge/test/worker.test.js`
- `cloudflare/tesco-bridge/wrangler.jsonc`
- `docs/prevadzka.md`
- `hetzner/dozorca.sh`
- `hetzner/uvarsi-deploy-state.sh`
- `nasad.ps1`
- `tests/test_deploy_safety.py`
- `tests/test_dozorca_contract.py`
- `tests/test_tesco_bridge_deployment.py`

### Remaining operational risk

Production rollout still requires the operator to deploy the reviewed Worker,
record Cloudflare's immutable version ID, and set the matching Hetzner value.
This task did not contact production. Payments remain OFF.

## Fix round 3 — managed cron rollback and weighted receipt totals

Implementation commit: `dc60f39b48dbd4b1bcd0097101f61fd055f8bd3f`.

### Implemented behavior

- Rollback now reads the live crontab, removes only recognized Uvar.si jobs,
  and restores the original Uvar.si jobs from the protected snapshot. It
  preserves current Taktik and unrelated rows, including rows added after the
  snapshot, and does not resurrect an unrelated row removed during deployment.
  A live crontab read error prevents every write. The merged candidate is
  installed in one operation and checked byte for byte.
- The production schedule installer continues to derive its complete candidate
  from the live crontab, so it preserves Taktik and unrelated jobs.
- Receipt readiness accepts a one-line weighted purchase whose customer totals
  use the production generator's weight multiplier. It derives that multiplier
  from the sale subtotal and recomputes the original and loyalty totals from
  the same reviewed DB offer. The gate still rejects altered sale, original,
  loyalty, quantity, unit, validity, savings, and receipt totals.
- Worker attestation, lock-busy handling, the four-hour cap, secret-safe output,
  and payments-OFF behavior are unchanged.

### TDD and verification evidence

The first RED run proved both review findings:

```text
2 failed, 1 passed, 54 deselected in 10.90s
```

The second RED run isolated strict weighted original-price validation:

```text
1 failed, 4 passed, 57 deselected in 25.19s
```

Bounded GREEN runs used local fakes and workspace-local temporary directories:

```text
tests/test_tesco_bridge_deployment.py
63 passed in 225.38s (240s timeout)

tests/test_deploy_safety.py
tests/test_stropy_pokryju_cely_zber.py
tests/test_refresh_contract.py::test_receipt_selection_preserves_verified_totals_for_weighted_purchases
tests/test_offer_matcher.py::test_bare_kilogram_is_weight_pricing_not_a_fixed_package
64 passed in 1.30s (120s timeout)
```

Bash parsed `hetzner/uvarsi-deploy-state.sh` under a 30-second limit, and
`git diff --check` returned no errors. The tests made no real network, SSH,
Cloudflare, Anthropic, deployment, or payment calls. Taktik files and services
remained untouched.

### Files changed

- `docs/prevadzka.md`
- `hetzner/uvarsi-deploy-state.sh`
- `tests/test_tesco_bridge_deployment.py`
