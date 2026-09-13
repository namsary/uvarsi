# Task 9 — annual subscription payment gate

## Stale regression expectations identified before test edits

| Regression case | Obsolete expectation | Authoritative Task 9 invariant |
| --- | --- | --- |
| `test_runtime_readiness_accepts_the_code_owned_verified_support_phone` | Runtime readiness was unlocked by legacy `LEMON_VARIANT_ID`, static checkout URLs and a boolean result from the old one-time purchase/refund smoke helper. | Runtime must use `LEMON_SUBSCRIPTION_VARIANT_ID`, founder discount ID/code and a complete signed annual lifecycle marker bound to the current release and both live/test configurations. |
| `test_zapnuty_flag_bez_podpisanej_aktivacie_neodomkne_checkout` | The test configured only legacy variant/checkout variables and expected the generic `payment_smoke_missing` blocker. | Even with the payment flag enabled and complete annual configuration, a missing current annual activation attestation must fail closed with `subscription_smoke_missing`. |
| `test_zmena_ktorehokolvek_live_secretu_zneplatni_aktivaciu_a_checkout[LEMON_WEBHOOK_SECRET]` | A legacy activation derived from a one-time purchase/refund marker was treated as sufficient until the live webhook secret changed. | Readiness starts from a full annual lifecycle activation attestation; rotating the live webhook secret must change its fingerprint and block checkout with `subscription_smoke_mismatch`. |
| `test_zmena_ktorehokolvek_live_secretu_zneplatni_aktivaciu_a_checkout[LEMON_API_KEY]` | A legacy activation derived from a one-time purchase/refund marker was treated as sufficient until the live API key changed. | Readiness starts from a full annual lifecycle activation attestation; rotating the live API key must change its fingerprint and block checkout with `subscription_smoke_mismatch`. |

## Verification evidence

Base: `a35536a263b35bfed9f49a852cd6007e4df22526`.

### RED

- Initial focused Task 9 run: `61 failed, 18 passed, 1 error`. The failures showed that readiness still accepted only the legacy purchase/refund proof and that the annual marker/readiness interfaces did not exist. The single error was an unrelated locked pytest temporary directory; subsequent runs used isolated local `--basetemp` directories.
- Server integration contract: `1 failed`. `app/server.py` had not yet consumed the annual marker status or annual subscription configuration.
- Existing payment regression before its approved update: `4 failed, 110 passed`. The four cases listed above still supplied legacy variant/marker facts.

### GREEN

- Focused Task 9 plus deploy contract slice: `155 passed in 1.57s`.
- Full `tests/test_platby.py` payment regression: `114 passed in 50.35s`.
- Annual checkout, access, webhook, reconciliation, withdrawal, portal, payment reconciliation and notification-privacy slice: `334 passed in 92.52s`.
- Production modules compile successfully and `git diff --check` reports no whitespace errors.

The tests use local fakes and files only. No Lemon Squeezy, Anthropic or other network call was made. No smoke marker was fabricated for production, and payment flags remain off.

## Residual risks

- Production checkout remains intentionally fail-closed until the real test-mode lifecycle is completed and its current-release marker and activation attestation are installed.
- The existing Starlette test client emits one upstream deprecation warning about `anyio.abc.BlockingPortal`; it does not affect test outcomes or payment behavior.
- Task 9 does not push, deploy, turn on payment flags or perform the live provider lifecycle. Those actions remain separate release operations.

## Fix round 1 — hardened annual provider evidence

### Review findings converted to RED tests

| Finding | RED evidence | New invariant |
| --- | --- | --- |
| Provider discount code was not independently bound to the runtime code. | Marker/provider slice: `3 failed, 32 passed`. | The provider-returned code is compared by keyed HMAC with the configured code; only the fingerprint enters the signed marker. |
| `unresolved_cases` accepted truthy numeric lookalikes such as `False` and `0.0`. | Same slice above. | Only `type(value) is int and value == 0` is accepted by both producer and verifier. |
| Activation time was caller-controlled and had no explicit lifetime. | Activation slice: `4 failed, 35 passed`. | Activation uses the server clock, rejects a caller timestamp and expires after seven days; old, future and expired signed attestations fail closed. |
| Runtime trusted marker path, size and mtime as a cache identity. | Server slice: `2 failed, 113 deselected`. | Readiness re-reads and verifies marker contents every time; an atomic same-size replacement with restored mtime closes the gate. |
| Payments-on readiness accepted a durable activation without current provider economics. | Same server slice above. | Payments ON requires both a valid activation and a fresh current provider marker. Changed price/trial/discount facts close checkout even when IDs are unchanged. |
| `hetzner/payment-smoke.py` still executed the one-time order/refund flow. | Annual tool helper slice: `16 failed, 19 deselected`; annual main slice: `2 failed, 33 deselected`. | The command now reads annual live/test configuration, verifies 49 € EUR/no trial and the founder discount, validates the complete local test lifecycle, verifies portal evidence and emits the current signed annual marker. |
| Currency, cancellation order and refund evidence could be inferred too loosely. | Currency slice: `1 failed, 9 passed`; cancellation and refund slices each failed once before GREEN. | Store currency must be EUR; cancellation must precede paid-through expiry; recovery must follow failure; a full refund must exist on a persisted invoice. |

### Final GREEN evidence

- `tests/test_payment_smoke_contract.py`: `38 passed` after the full annual tool, portal and main-path contracts.
- Focused Task 9 plus deploy-contract slice: `183 passed in 1.87s`.
- `tests/test_platby.py`: `115 passed, 1 upstream warning in 49.79s`.
- Annual checkout/consent/webhook/access/portal/reconciliation, payment privacy and auth slices: `456 passed, 1 upstream warning in 196.46s`.
- Production modules compile successfully. `git diff --check` reports no whitespace errors.

All fix-round tests use local fakes, temporary files and local databases. No Lemon Squeezy, Anthropic or other network call was made. No production marker was created. `PLATBY_ZAPNUTE` remains unchanged and OFF.

### Fix-round residual risks

- The real provider sandbox lifecycle is intentionally not fabricated by tests. Before activation, an operator must complete the actual test-mode lifecycle so the tool can verify its persisted events, invoices, portal and provider configuration.
- While payments are ON, a provider marker older than 24 hours or an activation older than seven days closes checkout. Operations must refresh the explicit provider smoke and renew activation on schedule.
- Provider API compatibility is covered with strict local contract fakes but was not exercised against Lemon Squeezy in this no-network fix round.
- Legacy one-time helper functions remain for Task 1–8 regression compatibility, but the executable `main()` no longer invokes them.
