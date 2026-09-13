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
