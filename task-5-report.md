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

---

## Round 1 fixes — 2026-09-13

Planned commit: `fix: harden subscription portal management`

### Findings addressed

- Authenticated Lemon API reads now use a redirect-rejecting opener shared by
  checkout and subscription retrieval. Every HTTP 300–399 response is rejected;
  no same-host or cross-host redirect is followed manually or automatically.
  A deterministic two-server loopback harness proves that each redirect causes
  exactly one first-hop request, zero target requests, and no forwarded
  `Authorization` header. A normal non-redirected request still succeeds.
- Server and browser portal URL validation now reject every C0 character
  U+0000–U+001F and DEL U+007F before parsing, in addition to the existing
  whitespace, user-info, explicit-port, fragment, scheme and Lemon-host rules.
- `/api/platba/stav` now exposes server-owned `subscription_has_access`,
  `access_until`, and `needs_review`. `access_until` uses `ends_at` for a
  cancelled subscription and verified `paid_through` for paused, review and
  expired states. The browser does not compare clocks or infer entitlement.
- Profile wording now distinguishes a cancelled subscription before and at its
  exact cutoff, treats `past_due` as a payment being resolved without claiming
  a retry date, and renders paused/review/expired states from the authoritative
  access result and cutoff. A separate legacy/manual Premium grant cannot make
  an expired subscription look active.
- Billing dates are rendered with the explicit `Europe/Bratislava` time zone.
- The management capability flag now requires the same complete local provider
  identity shape as the portal route, including variant and boolean test/live
  mode. Active, cancelled, `past_due`, paused, unpaid and expired subscriptions
  remain manageable when that identity is valid.
- The enforceable initial app-shell ceiling is restored to 37,600 bytes. Profile
  payment/support code is loaded only when Profile is rendered from the
  content-addressed asset `subscription-profile.50f7b3f42981.js`. Its SHA-384
  integrity metadata is verified by tests, it has a separate 4,500-byte gzip
  ceiling, the service worker does not pre-cache it, and deployment requires
  the file and serves its exact hashed path with immutable caching.

The final measurements at deployed gzip level 5 are:

- app shell: **36,728 bytes** (872 bytes below the 37,600-byte ceiling);
- lazy Profile asset: **4,287 bytes** (213 bytes below its 4,500-byte ceiling).

### Strict TDD evidence

- Redirect RED: the transport test failed because no guarded opener existed.
  GREEN: direct requests passed and all 100 redirect codes plus a cross-origin
  redirect were rejected with zero second request.
- URL RED: server validation accepted NUL and DEL, and browser validation
  accepted all three sampled C0/DEL inputs. GREEN: all focused server and
  browser cases passed.
- Authoritative-state RED: seven route cases failed because the API omitted the
  subscription access/cutoff fields. GREEN: cancelled boundary, paused,
  expired, review and manual-entitlement combinations passed. The test was
  corrected to preserve the existing domain invariant that paused snapshots
  are review-marked.
- Capability RED: a snapshot with a missing provider variant was still
  advertised as manageable. GREEN: all five corrupted-identity cases are now
  non-manageable, while 12 live/test and status combinations remain manageable.
- Frontend/performance RED: the profile asset, SRI contract and lazy loading did
  not exist, and the shell exceeded 37,600 bytes. GREEN: all eight focused
  profile, URL, timezone, lazy-loading, deployment and shell-budget checks
  passed.
- Legacy-network RED: the first redirect patch removed the unrelated
  unauthenticated preflight-notification opener. GREEN: its focused regression
  test and the authenticated redirect test both pass; only Lemon requests use
  the no-redirect transport.

### Verification

No full suite was run. Both rollout flags were forced OFF in the final payment,
authentication and frontend commands. Provider behavior used fakes; the only
network traffic was the deterministic local redirect harness. No live Lemon,
Anthropic or other external request was made.

- Focused payment slice (portal, subscription access, checkout, legacy payment
  and readiness): **254 passed**.
- Focused authentication/session slice: **7 passed**.
- Focused frontend/performance slice: **65 passed**.
- Deployment-safety slice: **54 passed**.
- `git diff --check`: passed.

The broader performance diagnostic still has the same two unrelated baseline
conditions recorded above: the optional Brotli decoder is unavailable for the
font-inspection test, and unchanged `index.html` is 12,230 bytes against its
12,100-byte practical-headroom check while remaining under the 12,400-byte hard
limit. Neither file or limit was changed in Round 1.

Pre-existing untracked worktree artifacts were left untouched. No push,
deployment, live provider call or full-suite run was performed.
