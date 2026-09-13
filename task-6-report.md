# Task 6 report — Withdrawal and exact subscription-period handling

Date: 2026-09-13
Branch: `codex/uvarsi-hybrid-plan-loading`
Base: `07502041ec12690859595443429fcf51c5e5561a`
Planned commit: `feat: handle annual cancellation and withdrawal`

## Delivered scope

- Added `create_subscription_withdrawal(...)`, bound to the authenticated user
  and an exact paid Lemon Squeezy invoice belonging to a verified
  `premium_annual` subscription. The invoice identity, order, subscription,
  currency, expected initial/renewal amount, paid timestamp, and paid period
  must all agree with stored records.
- Added deterministic `pro_rata_refund_preview(...)` calculation using decimal
  half-up cent rounding, actual paid-period length, and bounds before the period
  start and at/after the period end. Coverage includes 39,00 € and 49,00 €,
  leap-year periods, half-cent ties, and invalid periods.
- At the exact 14-day boundary, withdrawal is stored as `statutory_review`.
  A proportional preview is produced only when the immutable checkout evidence
  proves the express request for immediate performance and the required
  information. Missing, malformed, stale, or incomplete consent produces a
  full remaining-amount preview with no consumed-service deduction.
- Ordinary change of mind after 14 days is stored as
  `cancel_at_period_end` with no refund preview. The response and durable
  confirmation state that cancellation has not happened and must still be
  completed through the Customer Portal or support.
- Defect, nonconformity, unavailable service, duplicate charge, and
  unauthorized charge are stored as separate remedy reviews, including when
  submitted after 14 days. They are never converted into late change of mind.
- Snapshotted the minimum durable invoice, period, consent-proof, legal-version,
  classification, and preview facts needed for review. Email, provider customer
  identity, and unrelated consent payload fields are not copied into the
  request record or API response.
- Repeat and concurrent submissions for the same user, invoice, request type,
  and remedy are idempotent. The first immutable request and confirmation key
  are reused.
- Request creation performs no provider refund, cancellation, subscription
  mutation, invoice mutation, network request, or AI call. Existing exact-
  invoice refund processing remains the only mutation path, so another paid
  period cannot be altered by this preview workflow.
- Preserved the legacy one-time-payment request flow, confirmation retry
  behavior, and account deletion as a separate operation.

## Strict TDD evidence

- Pro-rata RED: 13 focused failures because the planned function did not exist.
  GREEN: all 13 arithmetic, rounding, bound, 39/49 amount, and period tests
  passed.
- Request-model RED: 19 focused failures because annual invoice binding,
  consent proof, classification, immutable replay, and concurrency handling did
  not exist. GREEN: all model and arithmetic tests passed.
- Route RED: 12 expected failures because the annual route outcomes were not
  exposed or persisted. GREEN: authenticated ownership, exact-current-renewal,
  14-day, late-cancellation, separate-remedy, generic foreign-invoice,
  replay, no-provider, and no-AI cases passed.
- Confirmation coverage then proved the durable late-cancellation and
  missing-consent wording.
- Final hardening RED: two tests demonstrated that an invoice attached to an
  unverified or non-annual subscription was accepted. The query-level binding
  was tightened; the full focused Task 6 file finished at **48 passed**.

## Verification

No full suite was run, as requested. Both rollout flags were forced OFF. All
provider/AI boundaries were local fakes or explicit forbidden-call assertions;
no live provider, network, or Anthropic call was made.

- Exact Task 6 plan slice (`subscription_withdrawal`, `customer_requests`,
  `account_data`): **79 passed**.
- Relevant subscription/payment slice (checkout consent, access, checkout,
  webhooks, portal, legacy payments, reconciliation, readiness, notification
  privacy): **378 passed**.
- Relevant authentication slice: **244 passed**.
- Relevant deployment and payment-smoke contracts: **87 passed**.
- Legal-page slice: **19 passed**.
- Python compilation and `git diff --check`: passed.

Pytest reports the existing Starlette `BlockingPortal` deprecation warning;
it does not affect the results.

One broader diagnostic included `test_app_html_contract.py` and produced
**163 passes plus one unrelated pre-existing failure**: that test expects
`<form id="withdrawal-form">`, which is already absent from
`app/static/app.html` at the requested base commit. Task 6 does not modify that
file, so the stale/out-of-scope contract was left untouched.

## Files in the Task 6 commit

- `app/customer_requests.py`
- `app/predplatne.py`
- `app/server.py`
- `tests/test_subscription_withdrawal.py`
- `task-6-report.md`

Pre-existing untracked worktree artifacts were left untouched. No push,
deployment, agent, automatic provider refund, or full-suite run was performed.
