# Payment-ready Founder Launch Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Uvar.si ready to sell at most 50 one-time Founder Premium memberships for 39 €, while payments remain off until a test-mode purchase and refund pass.

**Architecture:** Keep LemonSqueezy as the payment provider and source of payment truth. Add one operator profile, immutable legal-document versions, a fail-closed readiness gate, audited checkout attempts, consumer-request workflows, and account export/deletion. Existing entitlements remain server-authoritative and the annual product remains disabled.

**Tech Stack:** Python 3.12, FastAPI, SQLite, vanilla HTML/CSS/JavaScript, pytest, LemonSqueezy signed webhooks.

**Spec:** `docs/superpowers/specs/2026-09-07-payment-ready-founder-launch-design.md`

## Global Constraints

- Sell only `zakladajuci_clen` for 39 € once; never create a subscription in this release.
- Enforce a hard capacity of 50 successful, paid, non-refunded Founder entitlements.
- Keep `PLATBY_ZAPNUTE=0` throughout implementation and deployment.
- Grant Premium only from a valid signed `order_created` webhook or existing owner-only manual tooling.
- Revoke Premium only after a valid full `order_refunded` event.
- Offer a full 14-day refund without pro-rating.
- Never send e-mail, user ID, order ID, provider ID, request content, or tokens to ntfy.
- Do not add analytics or advertising cookies.
- Do not call recipe AI from a customer request or any payment test.
- Preserve existing users, passwords, passkeys, sessions, plans, pantry rows, and manual Premium.
- Use `PUMAR s. r. o.`, IČO `57 370 591`, registered office `Alexandra Dubčeka 4318/33, 075 01 Trebišov`, Mestský súd Košice, section `Sro`, entry `64515/V`, and `pumaragency@gmail.com`.

---

### Task 1: Central operator profile and immutable legal version

**Files:**
- Create: `app/operator_profile.py`
- Create: `tests/test_operator_profile.py`
- Modify: `app/config.py`
- Modify: `tests/test_config.py`

**Interfaces:**
- Produces: `OperatorProfile`, `OPERATOR`, `LEGAL_VERSION`, `LEGAL_EFFECTIVE_DATE`, `validate_operator_profile(profile) -> tuple[str, ...]`, `public_operator_dict() -> dict`.
- Consumes: no runtime network service or environment secret.

- [ ] **Step 1: Write the failing profile tests**

```python
def test_operator_profile_has_verified_company_identity():
    assert OPERATOR.business_name == "PUMAR s. r. o."
    assert OPERATOR.company_id == "57370591"
    assert OPERATOR.register_entry == "64515/V"
    assert validate_operator_profile(OPERATOR) == ()

def test_public_profile_does_not_invent_tax_or_phone_data():
    data = public_operator_dict()
    assert "phone" not in data
    assert "vat_id" not in data
```

- [ ] **Step 2: Run the tests and verify the import fails**

Run: `python -m pytest -q tests/test_operator_profile.py`

Expected: FAIL because `app.operator_profile` does not exist.

- [ ] **Step 3: Implement the immutable profile**

```python
@dataclass(frozen=True)
class OperatorProfile:
    business_name: str
    company_id: str
    registered_office: str
    register_court: str
    register_section: str
    register_entry: str
    support_email: str

LEGAL_VERSION = "2026-09-07-v1"
LEGAL_EFFECTIVE_DATE = datetime.date(2026, 9, 7)
```

`validate_operator_profile` must reject blank fields, an IČO other than eight digits, malformed e-mail, and placeholders such as `DOPLNIŤ`, `TODO`, or square brackets.

- [ ] **Step 4: Run profile and configuration tests**

Run: `python -m pytest -q tests/test_operator_profile.py tests/test_config.py`

Expected: PASS.

- [ ] **Step 5: Commit the unit**

```bash
git add app/operator_profile.py app/config.py tests/test_operator_profile.py tests/test_config.py
git commit -m "feat: centralize verified operator identity"
```

### Task 2: Publish accurate legal pages

**Files:**
- Create: `app/legal_pages.py`
- Create: `tests/test_legal_pages.py`
- Modify: `app/server.py`
- Modify: `app/public_pages.py`
- Modify: `index.html`
- Modify: `app/static/app.html`
- Modify: `tests/test_public_pages.py`
- Modify: `tests/test_landing_html_contract.py`
- Modify: `tests/test_app_html_contract.py`
- Track and update: `docs/legal/01_VOP_NAVRH.md`
- Track and update: `docs/legal/02_OCHRANA_OSOBNYCH_UDAJOV_NAVRH.md`
- Track and update: `docs/legal/03_COOKIES_A_LOKALNE_ULOZISKA_NAVRH.md`

**Interfaces:**
- Consumes: `OPERATOR`, `LEGAL_VERSION`, `LEGAL_EFFECTIVE_DATE` from Task 1.
- Produces: `render_legal_page(slug) -> str`, `legal_text(slug) -> str`, public routes `/vop`, `/ochrana-osobnych-udajov`, `/cookies`, `/odstupenie`, `/reklamacie`, and `/pravne/{slug}.txt`.

- [ ] **Step 1: Write failing legal-content tests**

```python
@pytest.mark.parametrize("slug", ["vop", "ochrana-osobnych-udajov", "cookies", "odstupenie", "reklamacie"])
def test_legal_page_is_complete_and_versioned(slug):
    html = render_legal_page(slug)
    assert "PUMAR s. r. o." in html
    assert "57 370 591" in html
    assert LEGAL_VERSION in html
    assert "[DOPLNIŤ" not in html
    assert "magic link pri každom prihlásení" not in html
```

Add route tests for 200 responses, UTF-8, one H1, canonical URL, download link, and noindex only for unknown slugs.

- [ ] **Step 2: Run tests and verify missing renderer/routes fail**

Run: `python -m pytest -q tests/test_legal_pages.py tests/test_public_pages.py`

Expected: FAIL for missing module and routes.

- [ ] **Step 3: Implement structured legal content and routes**

Build pages from escaped headings and paragraphs, not raw user-controlled HTML. State:

- one-time 39 € Founder offer, no automatic renewal;
- full 14-day refund policy;
- password/passkey login and e-mail only for confirmation/reset;
- deterministic curated recipe engine and AI-assisted flyer processing;
- current data categories and processors;
- account export/deletion behavior;
- price, availability, allergy, and recipe limitations;
- complaint and alternative-dispute process.

Do not claim a VAT registration, phone support, a DPO, or a precise Hetzner datacenter without verified data.

- [ ] **Step 4: Add consistent footer links**

Landing and app footer must link to all five public pages and `mailto:pumaragency@gmail.com`. Remove copy saying legal terms will be added later.

- [ ] **Step 5: Run legal, landing, and app contract tests**

Run: `python -m pytest -q tests/test_legal_pages.py tests/test_public_pages.py tests/test_landing_html_contract.py tests/test_app_html_contract.py`

Expected: PASS.

- [ ] **Step 6: Commit the unit**

```bash
git add app/legal_pages.py app/server.py app/public_pages.py app/static/app.html index.html docs/legal tests/test_legal_pages.py tests/test_public_pages.py tests/test_landing_html_contract.py tests/test_app_html_contract.py
git commit -m "feat: publish complete Uvarsi legal pages"
```

### Task 3: Fail-closed payment readiness gate

**Files:**
- Create: `app/payment_readiness.py`
- Create: `tests/test_payment_readiness.py`
- Modify: `app/server.py`
- Modify: `tests/test_platby.py`
- Modify: `tests/test_server.py`

**Interfaces:**
- Consumes: operator validation, current release version, LemonSqueezy environment presence, approved source flag, private-notification policy, smoke-test marker, worker and recipe-engine health.
- Produces: `assess_payment_readiness(...) -> PaymentReadiness`, `public_readiness(readiness) -> dict`, and `require_checkout_ready(...)`.

- [ ] **Step 1: Write failing readiness tests**

```python
def test_checkout_fails_closed_when_any_required_gate_is_missing():
    result = assess_payment_readiness(valid_input(source_approved=False))
    assert result.ready is False
    assert "price_source_not_approved" in result.blockers

def test_public_status_never_names_secret_environment_variables():
    public = public_readiness(assess_payment_readiness(valid_input(webhook_secret="")))
    assert public["ready"] is False
    assert "LEMON_WEBHOOK_SECRET" not in json.dumps(public)
```

Test missing checkout URL, webhook secret, store, variant, API key, legal version, test-mode smoke marker, dead worker, failed recipe gate, and payments-on with an unready result.

- [ ] **Step 2: Run tests and verify the module is absent**

Run: `python -m pytest -q tests/test_payment_readiness.py`

Expected: FAIL.

- [ ] **Step 3: Implement readiness as pure data**

```python
@dataclass(frozen=True)
class PaymentReadiness:
    ready: bool
    blockers: tuple[str, ...]
    legal_version: str
    release: str
```

Use stable public blocker codes. Never include secret values. `PLATBY_ZAPNUTE=1` with blockers must disable `/api/platba/start`, return 503, and emit one generic owner alert.

- [ ] **Step 4: Expose readiness in `/api/health` and `/api/me`**

`/api/health` returns counts and public codes. `/api/me` returns only `platby_zapnute`, `platby_pripravene`, the legal version, price, and remaining real paid places.

- [ ] **Step 5: Run readiness and server tests**

Run: `python -m pytest -q tests/test_payment_readiness.py tests/test_platby.py tests/test_server.py`

Expected: PASS.

- [ ] **Step 6: Commit the unit**

```bash
git add app/payment_readiness.py app/server.py tests/test_payment_readiness.py tests/test_platby.py tests/test_server.py
git commit -m "feat: block incomplete payment launches"
```

### Task 4: Audited one-time checkout consent

**Files:**
- Modify: `app/platby.py`
- Modify: `app/server.py`
- Modify: `app/static/app.html`
- Create: `tests/test_checkout_consent.py`
- Modify: `tests/test_platby.py`
- Modify: `tests/test_premium_frontend_contract.py`

**Interfaces:**
- Produces: `checkout_attempts` table, `create_checkout_attempt(...) -> str`, `get_checkout_attempt(...)`, `mark_checkout_paid(...)`, and a stricter `checkout_url(..., attempt_id: str)`.
- API request: `POST /api/platba/start` with `{"accept_terms": true, "legal_version": "2026-09-07-v1"}`.

- [ ] **Step 1: Write failing consent and audit tests**

```python
def test_checkout_requires_current_explicit_terms_acceptance(client):
    response = client.post("/api/platba/start", json={"accept_terms": False})
    assert response.status_code == 422

def test_checkout_attempt_records_one_time_offer_and_legal_version(db):
    attempt = create_checkout_attempt(db, user_id=7, legal_version=LEGAL_VERSION, now=1000)
    row = db.execute("SELECT product, amount_cents, currency, legal_version FROM checkout_attempts WHERE public_id=?", (attempt,)).fetchone()
    assert tuple(row) == ("zakladajuci_clen", 3900, "EUR", LEGAL_VERSION)
```

Test that stale legal versions fail, attempts expire, custom data includes the attempt ID, and `order_created` must match the user and unused attempt.

- [ ] **Step 2: Run tests and verify they fail for missing schema/behavior**

Run: `python -m pytest -q tests/test_checkout_consent.py tests/test_platby.py`

Expected: FAIL.

- [ ] **Step 3: Add checkout-attempt schema and validation**

Add an idempotent table with `public_id`, `user_id`, `product`, `amount_cents`, `currency`, `legal_version`, `privacy_version`, `accepted_at`, `expires_at`, `status`, and `provider_order_id`. Generate 256-bit random public IDs; store no card data and no raw session token.

- [ ] **Step 4: Build the in-app order summary**

Show 39 €, one payment, no renewal, Founder features, 14-day refund, real remaining capacity, legal links, one required checkbox, and the exact button text `Prejsť k objednávke s povinnosťou platby`. Keep marketing opt-in absent from this transaction.

- [ ] **Step 5: Run checkout and frontend tests**

Run: `python -m pytest -q tests/test_checkout_consent.py tests/test_platby.py tests/test_premium_frontend_contract.py`

Expected: PASS.

- [ ] **Step 6: Commit the unit**

```bash
git add app/platby.py app/server.py app/static/app.html tests/test_checkout_consent.py tests/test_platby.py tests/test_premium_frontend_contract.py
git commit -m "feat: require audited founder checkout consent"
```

### Task 5: Remove customer identifiers from public alerts

**Files:**
- Modify: `app/platby.py`
- Modify: `app/rekonciliacia.py`
- Modify: `app/server.py`
- Create: `tests/test_payment_notification_privacy.py`
- Modify: `tests/test_platby_rekonciliacia.py`

**Interfaces:**
- Produces: `payment_cases` table and `create_payment_case(...) -> int`.
- Owner alerts contain only case type, unresolved count, and instruction to inspect protected storage.

- [ ] **Step 1: Write a failing privacy regression test**

```python
@pytest.mark.parametrize("forbidden", ["order-123", "user@example.com", "ls_987", "user_id=7"])
def test_public_payment_alert_contains_no_customer_identifier(forbidden):
    alert = priprav_upozornenie(DRUH_DUPLICITA, pocet=1, den="2026-09-07")
    assert forbidden not in json.dumps(alert)
```

Add a static test rejecting order interpolation in `priprav_upozornenie` and a runtime test confirming the protected database case retains the provider order ID.

- [ ] **Step 2: Run the tests and verify the current order-number alert fails**

Run: `python -m pytest -q tests/test_payment_notification_privacy.py tests/test_platby_rekonciliacia.py`

Expected: FAIL because current ntfy text contains the order number.

- [ ] **Step 3: Add protected cases and generic alerts**

Persist the actionable provider order ID only in SQLite. Replace ntfy text with, for example, `Uvar.si eviduje 1 nevyriešenú duplicitnú platbu. Otvor chránený prehľad platieb.`

- [ ] **Step 4: Run payment privacy and reconciliation tests**

Run: `python -m pytest -q tests/test_payment_notification_privacy.py tests/test_platby_rekonciliacia.py tests/test_platby.py`

Expected: PASS.

- [ ] **Step 5: Commit the unit**

```bash
git add app/platby.py app/rekonciliacia.py app/server.py tests/test_payment_notification_privacy.py tests/test_platby_rekonciliacia.py
git commit -m "fix: anonymize owner payment alerts"
```

### Task 6: Withdrawal, refund, and complaint workflow

**Files:**
- Create: `app/customer_requests.py`
- Create: `app/customer_requests_cli.py`
- Create: `tests/test_customer_requests.py`
- Modify: `app/server.py`
- Modify: `app/platby.py`
- Modify: `app/static/app.html`
- Modify: `tests/test_platby.py`

**Interfaces:**
- Produces: `consumer_requests` table, `create_withdrawal(...)`, `create_complaint(...)`, `requests_for_user(...)`, `close_requests_for_refund(...)`.
- API: `POST /api/consumer/withdrawal`, `POST /api/consumer/complaint`, `GET /api/consumer/requests`.
- CLI: list and mark a request as processing; never directly grant Premium.

- [ ] **Step 1: Write failing workflow tests**

```python
def test_withdrawal_within_14_days_requests_full_refund(db):
    request = create_withdrawal(db, user_id=7, order_id="order-1", purchased_at=1000, now=1000 + 13 * 86400)
    assert request.refund_scope == "full"
    assert request.status == "received"

def test_full_refund_webhook_closes_request_and_revokes_entitlement(db):
    result = process_refund(db, full_refund_payload("order-1"), now=2000)
    assert result["akcia"] == "vratene"
    assert request_status(db, "order-1") == "refunded"
```

Test duplicate submissions, generic public responses, ownership, rate limits, after-14-day handling, refund e-mail confirmation, and partial-refund quarantine.

- [ ] **Step 2: Run tests and verify missing module/routes fail**

Run: `python -m pytest -q tests/test_customer_requests.py tests/test_platby.py`

Expected: FAIL.

- [ ] **Step 3: Implement request storage and state transitions**

Store `public_id`, `user_id`, `order_id`, `type`, `message`, `status`, `refund_scope`, and timestamps. Accept plain text up to 4,000 characters; reject files and HTML. A user may only select their own order.

- [ ] **Step 4: Add forms and service e-mails**

Profile shows order, purchase date, refund deadline, request status, and links. Public forms return the same message whether an order exists or not. Send a receipt of the request without marketing content.

- [ ] **Step 5: Run workflow, payment, auth, and e-mail tests**

Run: `python -m pytest -q tests/test_customer_requests.py tests/test_platby.py tests/test_auth.py tests/test_server.py`

Expected: PASS.

- [ ] **Step 6: Commit the unit**

```bash
git add app/customer_requests.py app/customer_requests_cli.py app/server.py app/platby.py app/static/app.html tests/test_customer_requests.py tests/test_platby.py
git commit -m "feat: add withdrawal and complaint workflows"
```

### Task 7: Account export and safe deletion

**Files:**
- Create: `app/account_data.py`
- Create: `tests/test_account_data.py`
- Modify: `app/server.py`
- Modify: `app/static/app.html`
- Modify: `app/static/sw.js`
- Modify: `tests/test_auth.py`
- Modify: `tests/test_app_frontend_contract.py`

**Interfaces:**
- Produces: `export_user_data(con, user_id) -> dict`, `delete_user_account(con, user_id, now) -> dict`.
- API: `GET /api/account/export`, `POST /api/account/delete` with password/passkey reauthentication token.

- [ ] **Step 1: Write failing isolation and deletion tests**

```python
def test_export_contains_only_the_authenticated_users_data(db):
    exported = export_user_data(db, user_id=7)
    assert exported["account"]["id"] == 7
    assert "other@example.com" not in json.dumps(exported)

def test_delete_removes_personal_rows_but_retains_minimal_payment_record(db):
    result = delete_user_account(db, user_id=7, now=2000)
    assert result["deleted"] is True
    assert db.execute("SELECT COUNT(*) FROM spajza WHERE user_id=7").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM naroky WHERE user_id=7").fetchone()[0] == 1
```

Test credentials, passkeys, sessions, plans, jobs, precompute claims, pantry, local-state generation, active consumer requests, and idempotent retry.

- [ ] **Step 2: Run tests and verify the module is absent**

Run: `python -m pytest -q tests/test_account_data.py`

Expected: FAIL.

- [ ] **Step 3: Implement transaction-safe export and deletion**

Anonymize the retained account identity with a random non-routable address and remove links that are not required for payment records. Roll back the entire deletion if any table operation fails. Never delete `naroky`, payment events, checkout consent records, or refund cases required for accounting and claims.

- [ ] **Step 4: Add profile controls and local cleanup**

Export downloads UTF-8 JSON. Deletion requires a fresh reauthentication challenge, explains that deletion is not a refund, clears service-worker caches and Uvar.si local/session storage, and ends the current session.

- [ ] **Step 5: Run account, auth, pantry, plan, and frontend tests**

Run: `python -m pytest -q tests/test_account_data.py tests/test_auth.py tests/test_spajza_oddelena_od_planu.py tests/test_plan_data.py tests/test_app_frontend_contract.py`

Expected: PASS.

- [ ] **Step 6: Commit the unit**

```bash
git add app/account_data.py app/server.py app/static/app.html app/static/sw.js tests/test_account_data.py tests/test_auth.py tests/test_app_frontend_contract.py
git commit -m "feat: add account export and safe deletion"
```

### Task 8: Source-policy gate and operational proof

**Files:**
- Create: `app/source_policy.py`
- Create: `tests/test_source_policy.py`
- Modify: `app/zbierac_akcii.py`
- Modify: `app/payment_readiness.py`
- Modify: `app/server.py`
- Modify: `docs/legal/00_PRAVNY_AUDIT_UVARSI.md`
- Modify: `docs/legal/04_CHECKLIST_PRED_PLATBAMI.md`

**Interfaces:**
- Produces: `approved_source(store, collector_kind) -> bool`, `source_policy_status() -> dict`.
- Consumes: current collector metadata; never exposes technical source URLs to customers.

- [ ] **Step 1: Write failing source-policy tests**

```python
def test_unknown_or_unapproved_collector_blocks_payment_readiness():
    assert approved_source("Lidl", "unknown-proxy") is False

def test_public_status_contains_no_collector_url():
    assert "http" not in json.dumps(source_policy_status())
```

Add tests requiring all three stores to have current validity, internal provenance, no customer-facing flyer copy/link, and no foreign image/layout asset.

- [ ] **Step 2: Run tests and verify the policy module is absent**

Run: `python -m pytest -q tests/test_source_policy.py tests/test_zbierac_akcii.py`

Expected: FAIL.

- [ ] **Step 3: Implement an allowlist and payment gate integration**

The allowlist records store, data shape, commercial-review date, reviewer, and status. Only approved fact extraction may satisfy payment readiness. Keep the current collector disabled for payment readiness until the legal audit records a defensible source decision; this flag cannot be supplied by a browser.

- [ ] **Step 4: Update the legal checklist with machine-verifiable states**

Replace unchecked prose that code now enforces with links to tests and runtime blocker codes. Keep external counsel review explicitly marked as recommended, not falsely completed by software.

- [ ] **Step 5: Run collector, source, readiness, and SEO tests**

Run: `python -m pytest -q tests/test_source_policy.py tests/test_zbierac_akcii.py tests/test_payment_readiness.py tests/test_release_gate_seo.py tests/test_seo_contract.py`

Expected: PASS.

- [ ] **Step 6: Commit the unit**

```bash
git add app/source_policy.py app/zbierac_akcii.py app/payment_readiness.py app/server.py docs/legal tests/test_source_policy.py
git commit -m "feat: gate payments on approved price sources"
```

### Task 9: Full verification, guarded deployment, and test-mode payment

**Files:**
- Modify: `VERSION`
- Modify: `hetzner/samopull.sh`
- Modify: `hetzner/uvarsi-deploy-state.sh`
- Create: `hetzner/payment-smoke.py`
- Create: `tests/test_payment_smoke_contract.py`
- Modify: `docs/prevadzka.md`

**Interfaces:**
- Produces: owner-run test-mode smoke command and a signed local marker bound to release, store, and variant.
- Consumes: LemonSqueezy test-mode checkout and webhook; does not print keys.

- [ ] **Step 1: Write failing deployment/smoke contract tests**

Test that deployment always starts with payments off, migrates before health, refuses to activate without readiness, rolls back code without rolling back customer payment data, and never prints LemonSqueezy secrets.

- [ ] **Step 2: Run deployment contract tests and verify missing smoke tooling fails**

Run: `python -m pytest -q tests/test_payment_smoke_contract.py tests/test_deploy_safety.py tests/test_deploy_manifest.py`

Expected: FAIL.

- [ ] **Step 3: Implement the guarded smoke tooling**

The tool checks public readiness, guides one test checkout, polls only the authenticated payment status, verifies one entitlement, requests a test refund, verifies revocation, and writes a release-bound marker. It must not accept or store card data.

- [ ] **Step 4: Run the full local suite**

Run: `python -m pytest -q`

Expected: all tests pass; only environment-dependent tests may be skipped with an explicit reason.

- [ ] **Step 5: Run static verification**

Run: `git diff --check`

Run: `rg -n "\[DOPLNI|TODO|magic link pri každom|order-[0-9]|Objednávka \{" app index.html docs/legal`

Expected: no legal placeholders, stale login copy, or public alert interpolation.

- [ ] **Step 6: Bump the release and commit**

```bash
git add VERSION hetzner docs/prevadzka.md tests/test_payment_smoke_contract.py
git commit -m "chore: prepare guarded founder payment release"
```

- [ ] **Step 7: Deploy with payments off and verify production**

Verify `/api/health`, legal pages, registration, password/passkey login, existing plan, new plan, pantry, checkout summary, export, deletion on a disposable account, withdrawal, complaint, mobile layout, and service-worker update. Confirm `payments_enabled=false`.

- [ ] **Step 8: Execute one LemonSqueezy test-mode purchase and refund**

The evidence must show checkout success, signed webhook, one Founder entitlement, confirmation e-mail, full refund, refund webhook, entitlement removal, zero unresolved payment cases, and a release-matching smoke marker.

- [ ] **Step 9: Present the final owner gate**

Report the exact production release, test counts, readiness codes, provider test evidence, and remaining human/legal review. Ask for separate explicit approval before setting `PLATBY_ZAPNUTE=1`. Never enable payments as a side effect of this plan.
