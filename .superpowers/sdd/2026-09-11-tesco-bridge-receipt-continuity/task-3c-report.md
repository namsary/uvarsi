# Task 3C report — atomic receipt publication and same-day recovery

## Status

Completed. Implementation commit: `30b127ad926942a59466237af7d31af88a41ce07`.

## Implemented behavior

- `refresh_blocek` reads the current week from the validated three-store staging layer.
- It composes the receipt, validates the public contract, JSON-serializes and round-trips the complete candidate, then validates the round-tripped value again.
- Only a fully valid candidate can call `promote_staged_week(con, week, today=...)`.
- Landing JSON publication happens after successful promotion and still uses the existing fsync plus atomic replace writer.
- Candidate, validation, missing-store, serialization, and pre-replace failures preserve the existing landing JSON byte-for-byte. Candidate failure also leaves active offers untouched because promotion has not started.
- The supervisor evaluates staging completeness before deciding whether to run the collector, so a complete verified stage goes directly to receipt construction.
- Receipt and collection structural suppression is bound to the staged source-fingerprint identity and deployed release. A changed fingerprint or release unlocks a new attempt; an unknown identity is retried only within the existing daily bound.
- The existing process lock, credit-exhaustion handling, same-day transient retry, historical LKG behavior, and Taktik isolation remain unchanged.
- No additional staged-offer accessor was needed. This task uses the existing read-only `staged_week_readiness` gate and existing `promote_staged_week` API; it does not edit `app/zbierac_akcii.py` or `app/offer_data.py`.

## TDD evidence

All Python commands used the required bundled interpreter and local dependency directory:

```powershell
$env:PYTHONDONTWRITEBYTECODE='1'
$env:PYTHONPATH=(Resolve-Path -LiteralPath '.superpowers\sdd\2026-09-11-tesco-bridge-receipt-continuity\python-deps').Path
& 'C:\Users\Ucet\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m pytest -p no:cacheprovider ...
```

### RED

Receipt preservation tests were added before the receipt implementation. The focused run for missing-store preservation, malformed-candidate preservation, and non-JSON rejection failed as expected (`3 failed`) because the old flow read active data and did not validate a JSON-round-tripped candidate before publication.

The staged source identity test then demonstrated that receipt suppression was still tied to active input:

```text
tests/test_dozorca_behavior.py::test_changed_source_fingerprint_invalidates_structural_suppression
FAILED: expected 2 refresh calls, observed 1
1 failed, 1 passed
```

The complete-stage reuse test demonstrated that the supervisor still invoked the collector from active-state checks:

```text
tests/test_dozorca_behavior.py::test_complete_verified_stage_skips_collector_and_goes_directly_to_receipt
FAILED: "zbierac_akcii.py" was present in recorded calls
1 failed
```

The staged collection fingerprint test also failed before its suppression key was moved to staging (`expected 2 collector calls, observed 1`).

### GREEN

Final receipt, preservation, and real staging/promotion API checks:

```text
tests/test_refresh_contract.py plus focused tests/test_zbierac_akcii.py staging/promotion cases
33 passed in 6.79s
```

Final supervisor contract and behavior checks:

```text
tests/test_dozorca_contract.py tests/test_dozorca_behavior.py
34 passed in 118.52s
```

Shell syntax and staged diff checks:

```text
bash -n hetzner/dozorca.sh
exit 0

git diff --cached --check
no output
```

No test called the real network or Anthropic API.

## Files changed in the implementation commit

- `hetzner/refresh_blocek.py`
- `hetzner/dozorca.sh`
- `tests/test_refresh_contract.py`
- `tests/test_dozorca_contract.py`
- `tests/test_dozorca_behavior.py`

## Remaining risk

- This commit depends on the parallel stage-only collector change retaining `staged_week_readiness` and `promote_staged_week` with their current contracts; both APIs were present and their focused tests passed in this worktree.
- SQLite promotion and filesystem replacement cannot form one cross-resource transaction. If the process dies after successful DB promotion but before `os.replace`, the old landing JSON remains intact and the next bounded supervisor run rebuilds and republishes from the retained complete stage.

## Fix round 1 — shared deployment threshold

Implementation commit: `4b4fe45c051f8d22b9ba58e82cd1eda1444637ce`.

### Finding and fix

The staging conversion had moved the supervisor's inline total-offer check from `POCET` to `STAGED_POCET`. The post-deploy contract still discovered the authoritative numeric threshold through the removed active-offer expression, so it could no longer prove that deployment readiness and the supervisor used the same value.

`dozorca.sh` now declares one mechanically discoverable `MIN_TOTAL_OFFERS=30`. The staged collection gate and both active-data plan-warming gates consume that constant. The deployment contract reads the named constant, verifies that the staged gate uses it, and still verifies that `nasad.ps1` assigns the identical numeric value to `PRAH`. The test was strengthened for the staged architecture rather than removed or weakened.

### RED

```text
tests/test_deploy_safety.py::test_postdeploy_check_uses_the_dozorca_offer_threshold
FAILED: dozorca.sh must have one named numeric offer threshold
1 failed in 0.41s
```

### GREEN and regression verification

All Python runs used the required bundled interpreter, local dependency directory, disabled bytecode writes, and a worktree-local `--basetemp`.

```text
tests/test_deploy_safety.py::test_postdeploy_check_uses_the_dozorca_offer_threshold
1 passed in 0.50s

tests/test_refresh_contract.py
tests/test_dozorca_contract.py
tests/test_dozorca_behavior.py
tests/test_deploy_safety.py::test_postdeploy_check_uses_the_dozorca_offer_threshold
60 passed in 132.19s

bash -n hetzner/dozorca.sh
exit 0

git diff --cached --check
no output
```

No real network or Anthropic call was made.

### Files changed

- `hetzner/dozorca.sh`
- `tests/test_deploy_safety.py`

### Remaining risk

`nasad.ps1` still carries the runtime `PRAH=30` literal because it is outside this fix round's write scope. The strengthened integration contract now fails if that literal drifts from `MIN_TOTAL_OFFERS` or if the staged supervisor gate stops consuming the named threshold.
