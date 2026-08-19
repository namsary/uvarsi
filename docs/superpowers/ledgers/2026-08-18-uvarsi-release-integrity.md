# Uvar.si Release Integrity — execution ledger

Plan: `docs/superpowers/plans/2026-08-18-uvarsi-release-integrity.md`

## Preflight

- Ruling: Create a source-only initial Git snapshot before Task 1 — the plan's staged file list would not version the existing deployable source, so it cannot prove source/production alignment. Cost if wrong: the snapshot contains existing unsafe behavior, but later commits identify the fixes precisely.
- Ruling: Exclude `kluc.ps1` and `odkaz.ps1` from version control until a separate security review — they handle credentials or privileged server access. Cost if wrong: they are not preserved in the release history.
- Ruling: Use the explicitly installed Python 3.12 executable rather than `py` — the launcher is absent from the active shell path. Cost if wrong: future contributors must use the documented executable or add it to PATH.
- Ruling: Fix the Task 4 test clock injection before implementing it — `UVARSI_TODAY` alone cannot make the shell's independent `date` calls deterministic. Cost if wrong: the watcher tests could give false confidence.
- Ruling: Add FastAPI and Anthropic test dependencies only when server-route tests begin, not to the configuration-only Task 1 environment. Cost if wrong: Task 2 setup will require an additional dependency step.

## Completed tasks

- Task 1: fix round 1/5 — strict production URL wiring and non-canonical URL coverage added; scoped re-review clean (commit `65e0376`).
- Task 1: complete (commits `9912c76..65e0376`, review clean).
- Task 2: fix round 1/5 — reálna HTTP regresia používa SQLite reláciu, preukazuje 503 bez konštrukcie Anthropic a produkčný výber je napojený na helper aktuálneho týždňa; scoped re-review clean (commit `a8f8eca`).
- Task 2: complete (commits `ad0e30e..a8f8eca`, review clean).

## Production evidence and active release gate

- Evidence (2026-08-19): `uvarsi.service` was active after the branded-link hotfix. The hotfix’s first health probe raced Uvicorn startup; journal evidence showed startup completed one second later. The local hotfix contract now requires a conditional 30-second API readiness retry. Status: LOCAL PASS only; the revised helper has not been deployed because production is already running.
- Evidence (2026-08-19): public `/api/akcie/pocet` reported zero offers for current Monday `2026-08-17`. The resulting 503 on plan generation is correct current-data protection, not a Pro-plan entitlement failure.
- Ruling: Legacy `dozorca.sh` HTML-text freshness detection and its last-week fallback wording are release blockers. The file must be replaced by the structured current-week landing-data health path before any paid-beta claim. Cost if wrong: the release takes longer, but customers cannot be shown an obsolete receipt as current.
- Ruling: Payments and paid entitlements remain out of scope until login, pantry, recipes, shopping list and three real weekly beta cycles are production verified. Cost if wrong: revenue is delayed; the alternative risks charging for an unverified product.
- Evidence (2026-08-19 independent QA audit): The partial dynamic-data refactor is unsafe to deploy. Legacy `dozorca.sh` passes `index.html` as the output destination while the new refresher writes JSON; this can overwrite the landing. Static landing still shows expired July/August receipt data and does not consume the public landing endpoint.
- Evidence (2026-08-19 independent QA audit): Flyer validity is not proven, source metadata can be assigned independently of an extracted item, and fixed collection caps omit later pages/offers. Model-generated plan prices/totals are not reconciled to the database.
- Ruling: Treat the dynamic-data worktree changes as incomplete until an independent task review proves the landing JSON contract, current source validity, offer provenance and safe dozorca destination together. Cost if wrong: added implementation time; it avoids corrupting the public landing or displaying unverifiable prices.
- Ruling: Customer-flow defects (pantry invalidation, magic-link provider errors and shopping-list namespacing) are P1 beta gates after the price/data path. Cost if wrong: beta is delayed; it avoids broken personalised plans and misleading login outcomes.

## Task D fix round 1 — LOCAL PASS (2026-08-19)

- Stable opaque `offer_key` references now bind plans and public receipts to verified commercial and provenance facts. Legacy null keys, reused rowids, stale caches and delayed model responses are rejected.
- Personal-plan generation now carries validated household size, validates positive recipe duration, reconstructs non-negative evidence totals correctly when original price is absent, and displays shopping quantity with the verified unit.
- Strict TDD evidence: each review regression failed first for the expected missing behavior; the final affected suite passed 113 tests and the full suite passed 134 tests with the absolute system Python 3.12 executable.
- Release status: LOCAL PASS only. No deployment, production access, live API call or secret access was performed.

- Task D fix round 1: complete (commit f47b9c6, scoped re-review clean; 134 tests passed).

## Authentication and e-mail hardening — LOCAL PASS (2026-08-19)

- RED — delivery, validation and hashed issuance: `C:\Users\Ucet\AppData\Local\Programs\Python\Python312\python.exe -m pytest tests/test_auth.py -q` → `13 failed`. Legacy behavior returned 200 for missing-key/timeout/non-2xx/malformed provider outcomes, printed message data, accepted malformed full addresses, used a 30-minute plaintext query token and returned no provider-acceptance message.
- RED — resend ordering and abuse controls: the same command → `2 failed, 16 passed`. The missing behaviors were the normalized-email 60-second database cooldown and five-requests-per-10-minute client-IP window.
- RED — interstitial, atomic redemption and sessions: the same command → `10 failed, 18 passed`. GET required/consumed a query token, POST verification did not exist, concurrent redemption had no atomic path, sessions were plaintext/year-long and legacy plaintext sessions remained valid.
- RED — app auth UX: `C:\Users\Ucet\AppData\Local\Programs\Python\Python312\python.exe -m pytest tests/test_app_html_contract.py -q` → `3 failed, 2 passed`. The shared API wrapper did not handle later 401s, resend had no deterministic cooldown, and the screen claimed an e-mail was sent.
- RED — explicit verification-network UX: `C:\Users\Ucet\AppData\Local\Programs\Python\Python312\python.exe -m pytest tests/test_auth.py::test_confirmation_page_turns_verification_network_failure_into_resend_ux -q` → `1 failed`; the interstitial had no network-failure/resend state.
- RED — malformed provider boundary: `C:\Users\Ucet\AppData\Local\Programs\Python\Python312\python.exe -m pytest tests/test_auth.py::test_resend_response_missing_http_status_returns_truthful_503 -q` → `1 failed` with `500 != 503`.
- Mutation RED checks for initially satisfied transaction/privacy invariants: successful resend without prior invalidation → `1 failed` with two outstanding rows; failed resend deleting before rollback → `1 failed` with no preserved row; account-specific response mutation → `1 failed` because `account_exists` exposed the known account.
- GREEN — targeted auth/server/UI suite: `C:\Users\Ucet\AppData\Local\Programs\Python\Python312\python.exe -m pytest tests/test_auth.py tests/test_server.py tests/test_app_html_contract.py -q` → `53 passed in 5.54s`.
- GREEN — full regression suite: `C:\Users\Ucet\AppData\Local\Programs\Python\Python312\python.exe -m pytest -q` → `169 passed in 7.15s` (the original 134-test meal/offer/plan/landing baseline remains green).
- Review: additive/idempotent v2 schema only; SHA-256 magic/session hashes; provider-accepted 2xx required before generic success; failed-send rollback preserves older valid links; fragment plus explicit-click POST; single-use atomic redemption; 60-minute/30-day absolute expiry; secure host-only cookie; deterministic DB/IP abuse controls; later 401 returns to login. No deployment, production access, live provider call, secret access, payment enablement, MailerLite change, or excluded-file edit was performed. MailerLite/waitlist remains for the later public-flow task.
- Intentional compatibility break: all legacy plaintext magic links and plaintext sessions are invalid after this release and require a fresh login request.

## Authentication fix round 1 — LOCAL PASS (2026-08-19)

- RED — reservation isolation/concurrency: `C:\Users\Ucet\AppData\Local\Programs\Python\Python312\python.exe -m pytest tests/test_auth.py::test_failed_resend_preserves_the_older_unexpired_token tests/test_auth.py::test_provider_pause_keeps_reservation_short_and_does_not_block_unrelated_write tests/test_auth.py::test_paused_provider_does_not_delay_async_event_loop_heartbeat tests/test_auth.py::test_concurrent_same_email_request_gets_in_progress_response_without_second_provider_call tests/test_auth.py::test_cancelled_request_removes_only_its_pending_reservation -q` → `5 failed in 4.70s`: the reservation table was absent, an unrelated insert raised `sqlite3.OperationalError: database is locked`, the heartbeat showed event-loop blocking, a concurrent same-email request could not complete while delivery was paused, and cancellation had no reservation cleanup.
- RED — redirect refusal: `C:\Users\Ucet\AppData\Local\Programs\Python\Python312\python.exe -m pytest tests/test_auth.py::test_resend_never_follows_redirect_with_token_bearing_body -q` → `4 failed in 1.16s` (`301`, `302`, `307`, `308` each returned `200 != 503` because the client could follow the redirect).
- RED — JSON/e-mail boundaries: `C:\Users\Ucet\AppData\Local\Programs\Python\Python312\python.exe -m pytest tests/test_auth.py::test_malformed_json_body_returns_the_same_safe_400 tests/test_auth.py::test_non_object_json_body_returns_the_same_safe_400 tests/test_auth.py::test_ascii_email_length_and_label_boundaries -q` → `7 failed, 8 passed in 1.58s`: malformed JSON and five non-object bodies returned 500, and a 65-character local part was accepted.
- RED — bounded IP limiter: `C:\Users\Ucet\AppData\Local\Programs\Python\Python312\python.exe -m pytest tests/test_auth.py::test_ip_limiter_globally_prunes_expired_clients_before_applying_cardinality_bound tests/test_auth.py::test_ip_limiter_rolls_each_client_window_at_exact_boundary -q` → `2 failed in 0.76s`; `ClientIpRateLimiter` had no `max_clients` bound.
- RED — stale visible identity: `C:\Users\Ucet\AppData\Local\Programs\Python\Python312\python.exe -m pytest tests/test_app_html_contract.py::test_later_api_401_clears_user_state_and_returns_to_login -q` → `1 failed in 0.10s`; no shared authenticated-state clearing helper existed, leaving the alias header stale.
- RED — stale reservation pruning: `C:\Users\Ucet\AppData\Local\Programs\Python\Python312\python.exe -m pytest tests/test_auth.py::test_stale_reservations_are_pruned_even_when_current_email_is_on_cooldown -q` → `1 failed in 0.76s` with `stale_count == 1`; cooldown rollback restored the stale row.
- GREEN — reservation isolation/concurrency selection: `5 passed in 1.89s`; redirect refusal: `4 passed in 0.89s`; JSON/e-mail boundaries: `15 passed in 1.34s`; bounded IP limiter plus existing route limit: `3 passed in 0.91s`; app contract: `5 passed in 0.40s`; stale-pruning/concurrent selection: `2 passed in 0.75s`.
- GREEN — final targeted auth/server/UI suite: `C:\Users\Ucet\AppData\Local\Programs\Python\Python312\python.exe -m pytest tests/test_auth.py tests/test_server.py tests/test_app_html_contract.py -q` → `79 passed in 8.60s`.
- GREEN — final pre-commit full regression suite: `C:\Users\Ucet\AppData\Local\Programs\Python\Python312\python.exe -m pytest -q` → `195 passed in 14.88s` (all prior 169 tests remain green; an earlier full run also passed all 195 in 10.67s).
- Review: delivery now uses a short durable hashed reservation, `asyncio.to_thread`, and separate short promotion/cancellation transactions; paused provider I/O holds neither SQLite nor the event loop. Same-email overlap permits one provider call, failed/cancelled sends preserve the predecessor token, redirects are never followed, body/e-mail boundaries are explicit, and the deterministic one-worker IP map globally prunes and remains bounded. Shared proxy-edge limiting remains required. Every unauthenticated UI transition clears the visible alias and user state. Raw tokens remain memory-only and are neither persisted nor logged.
- Release status: LOCAL PASS only. No deployment, production connection, secret access, provider call, payment/MailerLite change, or excluded-file edit was performed.
