# Task 6 report — Withdrawal and exact subscription-period handling

Date: 2026-09-13

Branch: `codex/uvarsi-hybrid-plan-loading`

Base: `07502041ec12690859595443429fcf51c5e5561a`

Initial Task 6 commit: `dd8103860be93b33c543ac25896eeeeba765e907`

Fix round 1 commit message: `fix: harden subscription withdrawal handling`

Fix round 1 commit: `5771c57e2bc660dc1abf9450751ee88d8ac74b8b`

Fix round 2 commit message: `fix: require legal review for unverifiable withdrawals`

## Fix round 2 outcome

- An initial invoice without a trustworthy `contract_concluded_at` now uses
  `manual_legal_review`, regardless of whether the submission appears early or
  late relative to payment. It has no automatic refund preview, consumed-
  service charge, late-cancellation classification, provider action, or claim
  that the statutory period expired.
- The API and durable confirmation explain that the contract date cannot be
  verified automatically and that support will review the request. Focused
  tests forbid provider calls in both possible-age cases.
- A replay of a resolved complaint returns HTTP 200 with `created=false`,
  `request_received=false`, the original request ID, its current `resolved`
  status, and text identifying the already closed request. It creates no new
  record and preserves the existing ownership and privacy boundaries.

Strict TDD evidence: the new focused cases first produced **7 expected
failures** against fix round 1. After the minimal implementation, the same
cases passed **7/7**.

Final fix-round-2 verification, with both flags OFF:

- Focused Task 6: **76 passed**.
- Subscription and payment: **383 passed**.
- Authentication: **244 passed**.
- Deployment, payment-smoke, and legal: **111 passed**.
- Total across these disjoint slices: **814 passed**.

No full suite, push, deployment, live provider call, network call, or AI call
ran during fix round 2.

## Fix round 1 outcome

- Renewal invoices no longer start a statutory withdrawal period. Ordinary
  change of mind on a renewal produces `cancel_at_period_end` and a zero refund
  preview. Defect, nonconformity, unavailable service, duplicate charge, and
  unauthorized charge remain separate remedy reviews.
- Initial invoices store a verified `contract_concluded_at`. The provider time
  must be valid, follow checkout acceptance, and not exceed the authenticated
  provider revision. Missing or inconsistent evidence creates no invoice and
  goes to review. Migrated historical invoices receive no inferred date and
  fail closed.
- The deadline uses the Europe/Bratislava calendar. Counting starts the day
  after contract conclusion, includes the whole fourteenth local day, and
  moves a Saturday, Sunday, or Slovak day of rest to the next working day. The
  calendar handles DST and movable holidays. It treats 8 May and 15 September
  as working days only in 2026 under that year's exception to Act No. 241/1993
  Coll.
- A repeated or concurrent submission after resolution returns the original
  closed request and truthful status instead of raising a server error.
- Authenticated users can list safe facts for their verified annual invoices,
  including expired periods. The list omits provider customer, subscription,
  and order identifiers. A foreign or absent invoice receives a generic 404;
  an empty selection receives 422. Neither path claims receipt or stores a
  request. Explicit legacy-order masking remains unchanged.
- Annual requests snapshot verified subscription status and `ends_at`. API and
  durable confirmation output distinguishes pending cancellation from
  subscriptions already `cancelled` or `expired`. Terminal states do not claim
  that Portal or provider action remains necessary.
- The workflow remains a classification and preview. It performs no provider
  refund, cancellation, subscription mutation, network request, or AI call.
  Account deletion remains separate.

## Preserved Task 6 behavior

- `create_subscription_withdrawal(...)` binds the authenticated user to one
  exact paid invoice, verified annual subscription, consent attempt, amount,
  currency, and paid period.
- `pro_rata_refund_preview(...)` uses decimal half-up cent rounding, exact
  period lengths, and bounded edges. Tests cover 39,00 €, 49,00 €, leap years,
  half-cent ties, and invalid input.
- A timely initial-contract request produces `statutory_review`. It deducts a
  consumed-service amount only when immutable consent proves the immediate-
  performance request and required information. Invalid or absent consent
  produces no consumed-service charge in the preview.
- Partial and full refund state remains bound to the selected invoice and
  period. The preview cannot alter another paid period.
- Durable records retain the minimum legal, invoice, consent-proof,
  classification, subscription-state, and confirmation facts needed for
  review. They omit provider customer identity and unrelated consent data.

## Strict TDD evidence for fix round 1

- Calendar RED: 11 failures; GREEN: 11 passed. Cases cover DST, whole-day
  boundaries, weekends, Christmas, Easter, 2026 exceptions, and invalid time.
- Contract timestamp RED: 5 failures; GREEN: 5 passed. Cases cover storage and
  missing, malformed, premature, and post-revision timestamps.
- Classification RED: 7 expected domain failures; GREEN: all 9 focused
  classification and remedy tests passed.
- Resolved replay RED: 2 failures; GREEN: both sequential and concurrent cases
  passed with one immutable record.
- Historical invoice RED: 3 failures; GREEN: all 3 passed, plus the legacy
  foreign-order regression.
- Subscription-state RED: 5 failures; GREEN: all 6 focused status, `ends_at`,
  confirmation, and immutable replay cases passed.
- Empty-selection RED: 1 failure; GREEN: the corrected 422 case and legacy-
  order regression passed.

## Verification

No full suite ran, as requested. Both rollout flags stayed OFF. Tests used
local fakes and forbidden-call assertions; no live provider, network,
Anthropic, or other AI call ran.

- Task 6, customer-request, and account slice: **104 passed**.
- Subscription and payment slice: **383 passed**.
- Authentication slice: **244 passed**.
- Deployment and payment-smoke contracts: **92 passed**.
- Legal-page slice: **19 passed**.
- Total across the five disjoint final slices: **842 passed**.
- Python compilation and `git diff --check`: passed.

Pytest emitted the existing Starlette `BlockingPortal` deprecation warning.
The sandbox also prevented pytest from writing its optional `.pytest_cache`;
isolated test execution succeeded.

## Remaining risks

- Future amendments to Act No. 241/1993 Coll. require calendar updates and
  year-specific tests. The code covers the rules required through the explicit
  2026 exceptions in this review.
- Provider `created_at` semantics remain an external contract. New initial
  invoices go to event review if the signed webhook omits or contradicts that
  timestamp. Migrated historical invoices receive no inferred statutory date;
  withdrawal requests for them enter `manual_legal_review`.
- The workflow never executes provider cancellation or refund. An active late
  cancellation still requires Portal or support action; statutory and remedy
  outcomes require human review.
- Immutable uniqueness returns the original resolved request for the same
  invoice, request type, and remedy. A materially different later incident
  under the same remedy needs a separate support workflow unless the model
  gains an explicit incident identity.

## Files changed in fix round 1

- `app/customer_requests.py`
- `app/predplatne.py`
- `app/server.py`
- `tests/test_subscription_webhooks.py`
- `tests/test_subscription_withdrawal.py`
- `task-6-report.md`

Pre-existing untracked worktree artifacts remain untouched. No push,
deployment, agent, provider mutation, network call, or full-suite run occurred.
