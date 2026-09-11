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
