# Task 1 report — business promise and public legal contract

## Scope completed

- Replaced the unqualified founding lifetime claim with the approved promise: `39 € raz. Premium bez predplatného počas prevádzky služby Uvar.si.`
- Added the missing verified-phone field. Its production value remains empty, it is omitted from public output and `validate_operator_profile(OPERATOR)` returns `support_phone`, so payment readiness remains fail-closed.
- Updated the legal version to `2026-09-11-v2`, effective `2026-09-11`.
- Aligned the landing, pre-checkout screen and VOP on the one-time price, service duration, no renewal and maximum 50 successful non-refunded memberships.
- Defined Lemon Squeezy as Merchant of Record/seller for the checkout transaction and PUMAR s. r. o. as Uvar.si operator and product-support contact.
- Defined order acceptance, Premium activation and manual activation/full-refund remedy when a successful payment is not activated.
- Added service termination notice, preservation of statutory remedies and the statutory rights for material adverse service changes.
- Corrected the withdrawal deadline explanation, completed the optional withdrawal template and expanded the signed-in online form so its stored message contains the complete notice fields.
- Added service-period defect responsibility and written reasons for rejected complaints.
- Added required-data consequences, SCC-backed transfer wording for Anthropic/Resend/MailerLite, waitlist scope/retention/unsubscribe rules, and deterministic browser-storage lifetimes.
- Limited the public waitlist form to requested launch/founding-offer information with a required scoped consent and privacy link.

## TDD evidence

RED 1: seven focused behavior tests failed because `support_phone`, the exact promise and service-duration clauses were absent.

RED 2: the expanded focused run failed 19 tests covering MoR/operator roles, activation remedy, material changes, withdrawal, complaints, GDPR, storage and waitlist behavior.

RED 3: the retention test failed until a maximum `12 mesiacov od prihlásenia` criterion was added.

GREEN:

```text
122 passed, 4 skipped, 1 warning in 17.71s
PYTEST_EXIT=0
```

Focused files:

- `tests/test_operator_profile.py`
- `tests/test_legal_pages.py`
- `tests/test_landing_html_contract.py`
- `tests/test_landing_visual_contract.py`
- `tests/test_app_html_contract.py`

Additional checks:

```text
Python compile: OK
Approved promise occurrences: 3
Forbidden customer claims: 0
PHONE_GATE=BLOCKED
git diff --check: OK
```

## External blocker retained

The verified public support phone is still unknown. No placeholder was published. Live payment readiness must remain blocked until the owner supplies and verifies the real public number.

Payments were not enabled. No source approval, deployment or push was performed.
