# Task 5 report — Customer Portal and cancellation UX

Date: 2026-09-13
Branch: `codex/uvarsi-hybrid-plan-loading`
Base: `83280744b956595f427b8a5eaf9cf0c5d86807cc`
Planned commit: `feat: add subscription management portal`

## Delivered scope

- Added authenticated `POST /api/platba/portal` for the session user's own
  locally stored annual Lemon Squeezy subscription.
- Kept portal management available while `PLATBY_ZAPNUTE=0`; the flag still
  controls only new checkout behavior.
- Required non-empty local provider customer, subscription, and variant
  identity, then cross-checked the fresh provider response against the local
  subscription, configured store/variant, and test/live mode.
- Accepted only fresh HTTPS portal URLs on `lemonsqueezy.com` or its
  subdomains, without credentials, explicit ports, fragments, whitespace, or a
  root-only path.
- Kept the signed URL request-scoped: it is returned only by the explicit POST
  with `Cache-Control: no-store`; it is not persisted, placed in HTML, sent to
  analytics, printed, or logged. Error responses never echo provider details
  or signed URLs.
- Added a server-owned subscription card to Profile for active, cancelled,
  `past_due`, unpaid, expired, paused, manual-Premium, and free states. It shows
  the next annual charge and renewal date or the access end when applicable.
- Added one accessible action, **Spravovať predplatné**, whose copy accurately
  says Lemon Squeezy handles cancellation, resumption, payment methods, and
  invoices. The app does not claim to cancel locally.
- Added accessible loading and error states, a repeat-click guard, defensive
  client-side URL validation, and the canonical PUMAR support contact:
  `+421 917 347 009` and `pumaragency@gmail.com`.
- Ordinary app, profile, payment-status, and customer-service loads do not
  create or fetch a Customer Portal URL.

## TDD evidence

- Portal RED: 39 focused failures, all caused by the missing Task 5 route and
  configuration boundary.
- Portal GREEN: the original 39 passed; review coverage then expanded the file
  to 46 passing tests for flags-off behavior, forged client identity, the fake
  provider adapter, malformed responses, and privacy checks.
- Frontend RED: 4 new contract tests failed while the existing 20 passed.
- Frontend GREEN: all 24 premium frontend contract tests passed.
- Accuracy edge RED/GREEN: a focused test first proved that absent charge data
  rendered a false `0 €`; after the fix, the same test passed and now renders no
  invented amount.
- Cache edge RED/GREEN: a focused test first proved the signed-URL response had
  no explicit cache prohibition; after the fix it passed with `no-store`.

All provider behavior in tests uses fakes or a patched local adapter. No live
Lemon Squeezy, network, or Anthropic call was made.

## Verification

No full suite was run, as requested.

- Exact Task 5 brief slice (`subscription_portal`, `app_frontend_contract`,
  `premium_frontend_contract`): **95 passed**.
- Relevant payment slice (subscription access/checkout/webhooks/portal,
  payment core/reconciliation/readiness/privacy): **351 passed**.
- Relevant authentication slice (auth, passkeys, rollout and deployment
  contracts): **244 passed**.
- Relevant frontend slice plus the app gzip budget: **80 passed**.
- Remaining applicable frontend speed checks: **37 passed, 2 deselected**.
- Relevant deployment-safety slice: **89 passed**.

The app shell is 39,282 bytes at deployed gzip level 5. Its reviewed Task 5
budget is 39,600 bytes, leaving 318 bytes of headroom with no new blocking
asset or extra initial network request.

The broader frontend diagnostic also exposed two unrelated conditions that
were not changed for Task 5:

- the optional Python Brotli decoder is absent, so the font glyph inspection
  cannot run in this test environment;
- unchanged `index.html` is 12,230 bytes gzip against its older 12,100-byte
  practical-headroom check (it remains below the separate 12,400-byte hard
  limit).

Pytest also reports the existing Starlette `BlockingPortal` deprecation and
cannot write the legacy `.pytest_cache` path; neither affects test results.

## Files in the Task 5 commit

- `app/config.py`
- `app/server.py`
- `app/static/app.html`
- `tests/test_subscription_portal.py`
- `tests/test_premium_frontend_contract.py`
- `tests/test_frontend_speed_contract.py`
- `task-5-report.md`

Pre-existing untracked worktree artifacts were left untouched. No push or
deployment was performed.
