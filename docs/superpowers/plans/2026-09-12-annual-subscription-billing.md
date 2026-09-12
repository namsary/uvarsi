# Uvar.si Annual Premium Subscription Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the unlaunched one-time founder purchase with a safe annual Premium subscription: 39 € for the first year for the first 50 customers, then 49 € at every automatic renewal.

**Architecture:** Keep Lemon Squeezy as Merchant of Record and source of truth for billing. Add a focused subscription domain module and additive SQLite tables, then derive Premium access from signed webhook state plus periodic reconciliation. Create short-lived provider checkouts through the Lemon Squeezy API so the server can bind one exact variant, founder discount, consent record, user, and checkout attempt. The existing server owns public status and customer-facing routes; payments remain fail-closed until a complete test-mode lifecycle attestation passes.

**Tech Stack:** Python 3.12, FastAPI, SQLite, vanilla HTML/CSS/JavaScript, Lemon Squeezy API and signed webhooks, pytest.

**Spec:** `docs/superpowers/specs/2026-09-12-annual-subscription-billing-design.md`

## Global Constraints

- Founder offer: 39 € for the first 12 months, then 49 € at every annual renewal.
- Founder eligibility: at most the first 50 successful initial subscription payments.
- Regular offer: 49 € per year from the first period.
- Use a 49 € annual variant plus a provider-managed 10 € discount with `duration=once`; never use `custom_price=3900` for a subscription.
- Reserve founder eligibility atomically and expire abandoned reservations; provider redemption limits remain the final protection against a 51st discounted payment.
- No free trial and no monthly plan.
- Renewal is automatic until the customer cancels.
- Cancellation preserves Premium until the verified `ends_at`; only expiration removes access.
- Unknown or incomplete provider data never creates access and never silently removes previously verified paid access.
- Preserve the statutory withdrawal, defect, duplicate-payment, unauthorized-payment, and unavailable-service remedies.
- Keep production payments disabled through implementation and deployment.
- Checkout, webhook, portal, and reconciliation code must never log secrets or signed customer URLs.
- Database changes are additive and preserve accounts, sessions, passkeys, pantry data, plans, manual Premium, and historical payment records.
- Payment and regression tests make no live Anthropic calls.
- Use test-first development and commit after every task.

---

## File map

- Create `app/predplatne.py`: subscription schema, state model, Lemon Squeezy payload validation, idempotent lifecycle transitions, and access decisions.
- Modify `app/platby.py`: reusable checkout-attempt and secure-webhook infrastructure; remove one-time-product assumptions from shared paths.
- Modify `app/server.py`: checkout, payment state, Customer Portal, webhook dispatch, withdrawal, health, and Premium authorization.
- Modify `app/config.py`: named live/test subscription, discount ID/code, portal, and API settings.
- Modify `app/rekonciliacia.py`: subscription and invoice reconciliation.
- Modify `app/payment_readiness.py`: annual-subscription readiness facts and blockers.
- Modify `app/payment_smoke_marker.py`: test evidence for the full subscription lifecycle.
- Modify `app/customer_requests.py`: annual-period withdrawal and refund review metadata.
- Modify `app/legal_pages.py`, `app/operator_profile.py`, `index.html`, and `app/static/app.html`: one consistent offer and subscription-management copy.
- Modify `docs/legal/00_PRAVNY_AUDIT_UVARSI.md`, `docs/legal/01_VOP_NAVRH.md`, `docs/legal/04_CHECKLIST_PRED_PLATBAMI.md`, and `docs/prevadzka.md`: operational and legal runbooks.
- Add focused tests under `tests/test_predplatne.py`, `tests/test_subscription_checkout.py`, `tests/test_subscription_webhooks.py`, `tests/test_subscription_portal.py`, `tests/test_subscription_reconciliation.py`, and `tests/test_subscription_smoke.py`.

---

### Task 1: Subscription schema and access state machine

**Files:**
- Create: `app/predplatne.py`
- Create: `tests/test_predplatne.py`
- Create: `tests/test_db_schema.py`
- Modify: `app/server.py:430-620`

**Interfaces:**
- Produces: `migrate_subscription_schema(con) -> None`
- Produces: `SubscriptionSnapshot` dataclass
- Produces: `subscription_access(snapshot, now: float) -> bool`
- Produces: `subscription_for_user(con, user_id: int) -> SubscriptionSnapshot | None`
- Consumes: existing SQLite connection and epoch-second clock.

- [ ] **Step 1: Write failing schema and state tests**

```python
def test_cancelled_subscription_keeps_access_until_ends_at(db):
    predplatne.migrate_subscription_schema(db)
    predplatne.upsert_snapshot(db, sample(status="cancelled", ends_at=2_000.0), now=1_000.0)
    assert predplatne.subscription_access(
        predplatne.subscription_for_user(db, 1), now=1_999.0
    ) is True
    assert predplatne.subscription_access(
        predplatne.subscription_for_user(db, 1), now=2_001.0
    ) is False

def test_unpaid_suspends_and_expired_removes_access(db):
    for status in ("unpaid", "expired"):
        predplatne.upsert_snapshot(db, sample(status=status), now=1_000.0)
        assert predplatne.subscription_access(
            predplatne.subscription_for_user(db, 1), now=1_001.0
        ) is False

def test_paused_uses_verified_paid_through_date_and_requires_review(db):
    predplatne.upsert_snapshot(
        db, sample(status="paused", paid_through=2_000.0), now=1_000.0
    )
    snapshot = predplatne.subscription_for_user(db, 1)
    assert predplatne.subscription_access(snapshot, now=1_999.0) is True
    assert predplatne.subscription_access(snapshot, now=2_001.0) is False
    assert snapshot.needs_review is True
```

- [ ] **Step 2: Run the focused tests and confirm RED**

Run: `python -m pytest tests/test_predplatne.py -q`

Expected: import failure for missing `predplatne`.

- [ ] **Step 3: Implement the additive schema and state rules**

```python
ACCESS_STATUSES = frozenset({"active", "past_due"})
SUSPENDED_STATUSES = frozenset({"unpaid", "expired"})

def subscription_access(snapshot, *, now):
    if snapshot is None or snapshot.status in SUSPENDED_STATUSES:
        return False
    if snapshot.status == "cancelled":
        return snapshot.ends_at is not None and now < snapshot.ends_at
    if snapshot.status == "paused":
        return snapshot.paid_through is not None and now < snapshot.paid_through
    return snapshot.status in ACCESS_STATUSES
```

Create `subscriptions`, `subscription_invoices`, and
`subscription_events` with unique provider IDs, explicit test mode, period
dates, `paid_through`, first and renewal prices, discount ID, review state,
and update timestamps. Add indexes by user, status, and renewal date. Reject
unknown or incomplete transitions into a review queue without overwriting the
last verified access-bearing snapshot. Extend `migrate_db()` in `app/server.py`
to call the new migration.

- [ ] **Step 4: Run focused and migration tests**

Run: `python -m pytest tests/test_predplatne.py tests/test_db_schema.py -q`

Expected: PASS; running the migration twice leaves the same schema and data.

- [ ] **Step 5: Commit**

```bash
git add app/predplatne.py app/server.py tests/test_predplatne.py tests/test_db_schema.py
git commit -m "feat: add annual subscription state model"
```

---

### Task 2: Annual checkout contract and founder discount

**Files:**
- Create: `tests/test_subscription_checkout.py`
- Modify: `app/config.py`
- Modify: `app/platby.py:45-520`
- Modify: `app/server.py:4980-5060,6014-6207`
- Modify: `tests/test_checkout_consent.py`
- Modify: `tests/test_payment_readiness.py`

**Interfaces:**
- Produces: `create_subscription_checkout_attempt(con, *, user_id, legal_version, consent, now) -> CheckoutAttempt`
- Produces: `create_provider_subscription_checkout(*, attempt, email, provider, test_mode) -> str`
- Produces: `founder_places_used(con) -> int`
- Consumes: `LEMON_SUBSCRIPTION_VARIANT_ID`, `LEMON_FOUNDER_DISCOUNT_ID`, `LEMON_FOUNDER_DISCOUNT_CODE`, and their test equivalents.

- [ ] **Step 1: Write failing price and consent tests**

```python
def test_founder_attempt_records_39_then_49(db):
    attempt = platby.create_subscription_checkout_attempt(
        db, user_id=1, legal_version="2026-09-12-v5",
        consent=valid_annual_consent(), now=100.0
    )
    row = db.execute(
        "SELECT * FROM checkout_attempts WHERE public_id=?", (attempt.public_id,)
    ).fetchone()
    assert row["amount_cents"] == 3900
    assert row["renewal_amount_cents"] == 4900
    assert row["billing_interval"] == "year"
    assert row["auto_renews"] == 1

def test_subscription_checkout_never_sets_custom_price(provider, founder_attempt):
    platby.create_provider_subscription_checkout(
        attempt=founder_attempt, email="a@example.sk",
        provider=provider, test_mode=True
    )
    payload = provider.last_checkout_payload
    assert "custom_price" not in payload["data"]["attributes"]
    assert payload["data"]["attributes"]["checkout_data"]["discount_code"] == "FOUNDERS"
    assert payload["data"]["attributes"]["checkout_data"]["custom"]["attempt_id"] == founder_attempt.public_id

def test_founder_reservation_is_atomic_and_abandoned_slots_expire(db):
    create_49_live_founders_and_one_pending_reservation(db)
    regular = create_attempt_in_second_connection(db, now=100.0)
    assert regular.founder is False
    after_expiry = create_attempt_in_second_connection(db, now=100.0 + CHECKOUT_TTL + 1)
    assert after_expiry.founder is True
```

- [ ] **Step 2: Run and confirm RED**

Run: `python -m pytest tests/test_subscription_checkout.py tests/test_checkout_consent.py -q`

Expected: missing annual checkout fields and functions.

- [ ] **Step 3: Implement pricing constants and checkout attempt fields**

```python
CENA_PRVY_ROK_CENTY = 3900
CENA_OBNOVA_CENTY = 4900
INTERVAL_ROK = "year"

def founder_places_used(con):
    return con.execute(
        "SELECT COUNT(*) FROM subscriptions WHERE founder=1 AND initial_payment_verified=1"
    ).fetchone()[0]
```

Add `renewal_amount_cents`, `billing_interval`, `auto_renews`, `founder`, and
`discount_id`, `discount_code`, `provider_checkout_id`, and
`founder_reserved_until` to checkout attempts. Select founder eligibility in a
short `BEGIN IMMEDIATE` transaction by counting successful founders plus
unexpired pending reservations. Expire abandoned attempts. Create a short-lived
checkout with `POST /v1/checkouts`, bind the 49 € annual variant, pass the
one-use 10 € code through `checkout_data.discount_code`, include only the
opaque attempt ID in custom data, keep the subscription preview visible, and
never set `custom_price`. Store exact legal consent before the provider call.
Verify the returned store, variant, test mode, checkout ID, expiry, and preview
total before returning its signed URL; never persist or log the signed URL.

- [ ] **Step 4: Run checkout tests**

Run: `python -m pytest tests/test_subscription_checkout.py tests/test_checkout_consent.py tests/test_payment_readiness.py -q`

Expected: PASS, including the 50th founder attempt and a regular 51st checkout.

- [ ] **Step 5: Commit**

```bash
git add app/config.py app/platby.py app/server.py tests/test_subscription_checkout.py tests/test_checkout_consent.py tests/test_payment_readiness.py
git commit -m "feat: create annual founder subscription checkout"
```

---

### Task 3: Signed webhook lifecycle and renewal idempotency

**Files:**
- Create: `tests/test_subscription_webhooks.py`
- Modify: `app/predplatne.py`
- Modify: `app/platby.py:600-1005`
- Modify: `app/server.py:6208-6283`

**Interfaces:**
- Produces: `process_subscription_event(con, *, payload, now, expected, source, delivery_key) -> dict`
- Produces: `subscription_event_key(payload, *, verified_body_digest=None, reconciliation_key=None) -> str`
- Consumes: validated webhook body from `platby.over_podpis()` and expected store, variant, discount, currency, and test mode.

- [ ] **Step 1: Write failing lifecycle tests**

```python
def test_two_renewal_invoices_for_one_subscription_are_both_recorded(db):
    created = event("subscription_created", subscription_id="sub_1")
    initial = event("subscription_payment_success", subscription_id="sub_1", invoice_id="inv_0", total=3900)
    renewal1 = event("subscription_payment_success", subscription_id="sub_1", invoice_id="inv_1", total=4900)
    renewal2 = event("subscription_payment_success", subscription_id="sub_1", invoice_id="inv_2", total=4900)
    deliveries = (created, initial, renewal1, renewal2, renewal2)
    for index, payload in enumerate(deliveries):
        digest = signed_body_digest(payload)
        predplatne.process_subscription_event(
            db, payload=payload, now=100.0, expected=EXPECTED,
            source="webhook", delivery_key=digest
        )
    assert db.execute("SELECT COUNT(*) FROM subscription_invoices").fetchone()[0] == 3

def test_cancel_does_not_revoke_but_expiry_does(db):
    process(db, event("subscription_cancelled", status="cancelled", ends_at="2027-09-12T00:00:00Z"))
    assert has_access(db, user_id=1, now=1_800_000_000.0)
    process(db, event("subscription_expired", status="expired"))
    assert not has_access(db, user_id=1, now=1_800_000_001.0)

def test_order_created_pairs_attempt_but_does_not_create_second_entitlement(db):
    process(db, event("order_created", order_id="ord_1", attempt_id="attempt_1"))
    assert subscription_count(db) == 0
    assert premium_grant_count(db) == 0

def test_unknown_or_incomplete_update_preserves_previous_paid_access(db):
    seed_subscription(db, status="active", paid_through=2_000.0)
    result = process(db, event("subscription_updated", status="new-provider-state"))
    assert result["review_required"] is True
    assert has_access(db, user_id=1, now=1_500.0) is True

@pytest.mark.parametrize(
    "event_name",
    ["subscription_payment_failed", "subscription_payment_recovered",
     "subscription_resumed", "subscription_paused", "subscription_unpaused",
     "subscription_payment_refunded", "order_refunded"],
)
def test_every_supported_lifecycle_event_has_an_idempotent_transition(db, event_name):
    payload = event(event_name)
    delivery_key = signed_body_digest(payload)
    first = process(db, payload, delivery_key=delivery_key)
    replay = process(db, payload, delivery_key=delivery_key)
    assert first["duplicate"] is False
    assert replay["duplicate"] is True
```

- [ ] **Step 2: Run and confirm RED**

Run: `python -m pytest tests/test_subscription_webhooks.py -q`

Expected: current code collapses renewal events by original order ID and revokes on cancellation.

- [ ] **Step 3: Implement event dispatch and transition validation**

```python
def subscription_event_key(
    payload, *, verified_body_digest=None, reconciliation_key=None
):
    invoice_id = safe_invoice_id(payload)
    if invoice_id:
        return f"lemon:invoice:{event_name(payload)}:{invoice_id}"
    if verified_body_digest:
        return f"lemon:webhook:{event_name(payload)}:{verified_body_digest}"
    if reconciliation_key:
        return f"lemon:reconcile:{event_name(payload)}:{reconciliation_key}"
    raise SubscriptionEventRejected("chýba bezpečný kľúč doručenia")
```

Validate user mapping, checkout attempt, store, variant, currency, test mode,
subscription ID, period dates, initial/renewal amount, and founder discount.
The first discounted invoice must total 3900 cents and later renewal invoices
4900 cents; a regular first invoice must total 4900 cents. Route all supported
subscription events to `app/predplatne.py`; keep one-time historical events in
`app/platby.py`. `order_created` pairs the attempt but cannot create a second
entitlement. `subscription_cancelled` sets `cancelled` and `ends_at`.
`subscription_resumed` restores auto-renewal before expiry. Failed and
recovered payment events update dunning state without duplicate invoices.
`subscription_paused` marks the record for review and preserves access only
through the last verified paid-through date; `subscription_unpaused` restores
the verified provider state.
Full and partial refunds attach to the exact invoice and never revoke another
period. Only `subscription_expired` or verified `unpaid` suspends access.
Unknown/incomplete events are quarantined for review and leave the last
verified snapshot unchanged. Lemon webhook requests have no independent event
ID, so calculate `verified_body_digest = sha256(raw_body)` only after HMAC
verification. Use the provider invoice ID for payment events. For
reconciliation, derive a stable key from resource type, provider ID,
`updated_at`, and event type. Never use only the original order ID.

- [ ] **Step 4: Run webhook and legacy payment tests**

Run: `python -m pytest tests/test_subscription_webhooks.py tests/test_platby.py tests/test_webhook_security.py -q`

Expected: PASS; replaying any event changes no balance or entitlement.

- [ ] **Step 5: Commit**

```bash
git add app/predplatne.py app/platby.py app/server.py tests/test_subscription_webhooks.py
git commit -m "feat: process annual subscription lifecycle"
```

---

### Task 4: Premium authorization and payment status API

**Files:**
- Modify: `app/server.py:900-970,2794-2942,6014-6020`
- Modify: `app/platby.py:500-575`
- Create: `tests/test_subscription_access.py`
- Modify: `tests/test_premium_frontend_contract.py`

**Interfaces:**
- Produces: `has_premium(con, *, user_id, now) -> bool`
- Produces: `GET /api/platba/stav` fields `status`, `renews_at`, `ends_at`, `next_amount_cents`, `auto_renews`, `can_manage`.
- Consumes: manual entitlement from `platby.ma_narok()` and subscription snapshot from Task 1.

- [ ] **Step 1: Write failing authorization tests**

```python
def test_cancelled_paid_period_keeps_all_premium_features(client, db):
    seed_subscription(db, status="cancelled", ends_at=time.time() + 86400)
    assert client.get("/api/me").json()["premium"] is True
    assert client.get("/api/platba/stav").json()["auto_renews"] is False

def test_expired_subscription_cannot_use_premium(client, db):
    seed_subscription(db, status="expired", ends_at=time.time() - 1)
    assert client.get("/api/me").json()["premium"] is False

def test_authorization_uses_local_snapshot_without_provider_or_ai(client, provider, ai):
    provider.fail_if_called()
    ai.fail_if_called()
    seed_subscription(status="active")
    assert client.get("/api/me").json()["premium"] is True
    assert client.get("/api/platba/stav").status_code == 200
```

- [ ] **Step 2: Run and confirm RED**

Run: `python -m pytest tests/test_subscription_access.py -q`

Expected: API knows only the historical one-time entitlement.

- [ ] **Step 3: Implement one server-side authorization function**

```python
def has_premium(con, *, user_id, now):
    return platby.ma_narok(con, user_id) or predplatne.subscription_access(
        predplatne.subscription_for_user(con, user_id), now=now
    )
```

Replace every direct Premium check with this function. Return dates and price
from the verified local snapshot; never accept them from browser state. Normal
page loads, plan generation, pantry operations, and Premium authorization must
not call Lemon Squeezy or Anthropic. Only checkout creation, an explicit Portal
request, webhook processing, and the scheduled reconciliation may contact the
billing provider.

- [ ] **Step 4: Run authorization, pantry, plan, and auth tests**

Run: `python -m pytest tests/test_subscription_access.py tests/test_server.py tests/test_spajza_oddelena_od_planu.py tests/test_auth.py tests/test_auth_passkey.py tests/test_auth_rollout_contract.py tests/test_auth_deployment_contract.py -q`

Expected: PASS; manual Premium remains functional.

- [ ] **Step 5: Commit**

```bash
git add app/server.py app/platby.py tests/test_subscription_access.py tests/test_premium_frontend_contract.py
git commit -m "feat: authorize premium from subscription state"
```

---

### Task 5: Customer Portal and cancellation UX

**Files:**
- Create: `tests/test_subscription_portal.py`
- Modify: `app/server.py:6014-6207`
- Modify: `app/static/app.html:1650-2130`
- Modify: `app/config.py`

**Interfaces:**
- Produces: `POST /api/platba/portal` returning `{"url": "https://..."}` for the authenticated owner.
- Consumes: `LEMON_API_KEY`, provider customer/subscription ID, and Lemon Squeezy API response URLs.

- [ ] **Step 1: Write failing portal tests**

```python
def test_portal_requires_login_and_owned_subscription(client, provider):
    assert client.post("/api/platba/portal").status_code == 401
    login(client, user_id=1)
    seed_subscription(user_id=2)
    assert client.post("/api/platba/portal").status_code == 404

def test_portal_url_is_not_persisted_or_logged(client, db, provider, caplog):
    login(client, user_id=1)
    seed_subscription(user_id=1)
    provider.returns_portal("https://store.lemonsqueezy.com/billing/signed-secret")
    response = client.post("/api/platba/portal")
    assert response.status_code == 200
    assert "signed-secret" not in caplog.text
    assert "signed-secret" not in dump_database(db)
```

- [ ] **Step 2: Run and confirm RED**

Run: `python -m pytest tests/test_subscription_portal.py -q`

Expected: route does not exist.

- [ ] **Step 3: Implement portal route and profile card**

```python
@app.post("/api/platba/portal")
def payment_portal(req: Request):
    user = require_user(req)
    subscription = require_owned_subscription(user["id"])
    return {"url": fresh_customer_portal_url(subscription)}
```

Render status, next charge, renewal date, or access end date in Profile. Use
one button, “Spravovať predplatné”, for cancellation, resumption, payment
method, and invoices. If the provider is unavailable, preserve state and show
the support phone and email.

- [ ] **Step 4: Run portal and frontend contract tests**

Run: `python -m pytest tests/test_subscription_portal.py tests/test_app_frontend_contract.py tests/test_premium_frontend_contract.py -q`

Expected: PASS; no signed URL appears in storage or logs.

- [ ] **Step 5: Commit**

```bash
git add app/server.py app/static/app.html app/config.py tests/test_subscription_portal.py tests/test_app_frontend_contract.py tests/test_premium_frontend_contract.py
git commit -m "feat: add subscription management portal"
```

---

### Task 6: Withdrawal, refund, and subscription-period handling

**Files:**
- Modify: `app/customer_requests.py`
- Modify: `app/server.py:5605-6013`
- Modify: `app/predplatne.py`
- Create: `tests/test_subscription_withdrawal.py`
- Modify: `tests/test_customer_requests.py`

**Interfaces:**
- Produces: `create_subscription_withdrawal(con, *, user_id, invoice_id, message, now) -> ConsumerRequest`
- Produces: `pro_rata_refund_preview(*, amount_cents, period_start, period_end, withdrawn_at) -> int`
- Consumes: verified invoice and consent snapshot.

- [ ] **Step 1: Write failing withdrawal tests**

```python
def test_withdrawal_preview_is_proportional_and_bounded():
    assert predplatne.pro_rata_refund_preview(
        amount_cents=3900, period_start=0, period_end=365 * 86400,
        withdrawn_at=7 * 86400
    ) == 3825

def test_change_of_mind_after_14_days_cancels_without_refund(client, db):
    seed_paid_subscription(db, purchased_at=time.time() - 15 * 86400)
    response = client.post("/api/consumer/withdrawal", json={"message": "Končím"})
    assert response.status_code == 202
    assert response.json()["refund_scope"] == "cancel_at_period_end"
```

- [ ] **Step 2: Run and confirm RED**

Run: `python -m pytest tests/test_subscription_withdrawal.py tests/test_customer_requests.py -q`

Expected: current request model assumes a full refund of a one-time order.

- [ ] **Step 3: Implement request classification**

```python
def pro_rata_refund_preview(*, amount_cents, period_start, period_end, withdrawn_at):
    total = max(1, period_end - period_start)
    unused = max(0, period_end - min(max(withdrawn_at, period_start), period_end))
    return min(amount_cents, max(0, round(amount_cents * unused / total)))
```

Classify requests within 14 days as statutory review with a pro-rata preview
when immediate performance was requested. Classify later change-of-mind
requests as cancellation at period end without a refund. Keep complaints,
duplicate charges, unauthorized charges, and service defects separate. A
partial refund never revokes a different paid period.

- [ ] **Step 4: Run withdrawal and account tests**

Run: `python -m pytest tests/test_subscription_withdrawal.py tests/test_customer_requests.py tests/test_account_data.py -q`

Expected: PASS; account deletion remains separate from cancellation and refund.

- [ ] **Step 5: Commit**

```bash
git add app/customer_requests.py app/server.py app/predplatne.py tests/test_subscription_withdrawal.py tests/test_customer_requests.py
git commit -m "feat: handle annual cancellation and withdrawal"
```

---

### Task 7: Subscription reconciliation, dunning, and alerts

**Files:**
- Create: `tests/test_subscription_reconciliation.py`
- Modify: `app/rekonciliacia.py`
- Modify: `app/predplatne.py`
- Modify: `app/platby.py:1008-1450`
- Modify: `app/server.py:5415-5461`

**Interfaces:**
- Produces: `reconcile_subscriptions(con, *, provider_rows, invoice_rows, now, expected) -> dict`
- Produces: health counters `subscription_drift`, `past_due`, `unpaid`, `expired`, and `queued_webhooks`.
- Consumes: provider API rows and the same transition validation as webhooks.

- [ ] **Step 1: Write failing drift tests**

```python
def test_reconciliation_recovers_missed_renewal_once(db):
    seed_subscription(db, status="past_due", period_end=100.0)
    result = reconcile_subscriptions(
        db, provider_rows=[provider_subscription(status="active", renews_at=200.0)],
        invoice_rows=[provider_invoice(id="inv_new", status="paid", total=4900)],
        now=150.0, expected=EXPECTED
    )
    assert result["recovered_invoices"] == 1
    assert invoice_count(db, "inv_new") == 1

def test_reconciliation_never_expires_on_network_failure(db):
    seed_subscription(db, status="active")
    with pytest.raises(ProviderUnavailable):
        reconcile_from_api(db)
    assert has_access(db, 1)
```

- [ ] **Step 2: Run and confirm RED**

Run: `python -m pytest tests/test_subscription_reconciliation.py -q`

Expected: reconciliation currently handles orders, not subscription state and invoices.

- [ ] **Step 3: Implement fail-safe reconciliation**

Feed provider snapshots through `process_subscription_event()` equivalents;
never update tables through a weaker path. Network failure preserves the last
verified access. A confirmed `unpaid` suspends access; recovered payment
restores it; `expired` removes it. Alerts contain only aggregate counts and
safe error codes.

- [ ] **Step 4: Run reconciliation and privacy tests**

Run: `python -m pytest tests/test_subscription_reconciliation.py tests/test_platby_rekonciliacia.py tests/test_payment_notification_privacy.py -q`

Expected: PASS; repeated runs import no duplicate invoice.

- [ ] **Step 5: Commit**

```bash
git add app/rekonciliacia.py app/predplatne.py app/platby.py app/server.py tests/test_subscription_reconciliation.py
git commit -m "feat: reconcile subscription renewals and expiry"
```

---

### Task 8: Public offer, checkout, VOP, and service messages

**Files:**
- Modify: `app/operator_profile.py`
- Modify: `app/legal_pages.py`
- Modify: `app/server.py:5605-6207`
- Modify: `index.html`
- Modify: `app/static/app.html`
- Modify: `tests/test_operator_profile.py`
- Modify: `tests/test_legal_pages.py`
- Modify: `tests/test_app_html_contract.py`
- Modify: `tests/test_app_frontend_contract.py`
- Modify: `tests/test_landing_html_contract.py`
- Modify: `tests/test_landing_visual_contract.py`
- Modify: `tests/test_customer_requests.py`
- Modify: `tests/test_payment_readiness.py`

**Interfaces:**
- Produces: one immutable `LEGAL_VERSION` and one exact `ANNUAL_PREMIUM_PROMISE` used by every server-generated confirmation.
- Consumes: the commercial rules from the approved spec.

- [ ] **Step 1: Write failing cross-surface copy tests**

```python
PROMISE = (
    "Prvý rok za 39 €. Potom 49 € ročne. Predplatné sa automaticky "
    "obnovuje, kým ho nezrušíš."
)

def test_annual_offer_is_identical_everywhere(client):
    assert PROMISE in Path("index.html").read_text(encoding="utf-8")
    assert PROMISE in Path("app/static/app.html").read_text(encoding="utf-8")
    assert PROMISE in client.get("/vop").text

def test_old_lifetime_and_one_time_promises_are_absent():
    current = current_customer_surfaces()
    for stale in ("39 € raz", "24 mesiacov", "bez automatickej obnovy", "navždy"):
        assert stale not in current

def test_checkout_button_and_consent_make_payment_and_renewal_explicit(client):
    html = paid_offer(client)
    assert "Objednať Premium s povinnosťou platby" in html
    assert "okamžitú aktiváciu" in html
    assert "automaticky obnovuje" in html
```

- [ ] **Step 2: Run and confirm RED**

Run: `python -m pytest tests/test_operator_profile.py tests/test_legal_pages.py tests/test_app_html_contract.py tests/test_app_frontend_contract.py tests/test_landing_html_contract.py tests/test_landing_visual_contract.py tests/test_customer_requests.py tests/test_payment_readiness.py -q`

Expected: old one-time and 24-month language is still present.

- [ ] **Step 3: Publish one annual offer and legal version**

```python
ANNUAL_PREMIUM_PROMISE = (
    "Prvý rok za 39 €. Potom 49 € ročne. Predplatné sa automaticky "
    "obnovuje, kým ho nezrušíš. Zrušiť ho môžeš kedykoľvek; Premium "
    "zostane aktívne do konca zaplateného obdobia."
)
```

Update VOP, withdrawal instructions, FAQ, checkout, Profile, order receipt,
and server-generated service messages. Set `LEGAL_VERSION` to the new immutable
`2026-09-12-v5` revision. State the 14-day statutory process accurately;
state that ordinary change-of-mind cancellation after 14 days receives no
refund. Display the first price, renewal price/frequency, automatic renewal,
cancellation route, and immediate-service request before a button labelled
“Objednať Premium s povinnosťou platby”. Configure Lemon's checkout receipt
button and thank-you text through checkout `product_options`. Keep the PUMAR
contact data and statutory defect rights.

- [ ] **Step 4: Run copy and public-page tests**

Run: `python -m pytest tests/test_operator_profile.py tests/test_legal_pages.py tests/test_public_pages.py tests/test_app_html_contract.py tests/test_app_frontend_contract.py tests/test_landing_html_contract.py tests/test_landing_visual_contract.py tests/test_landing_static.py tests/test_customer_requests.py tests/test_payment_readiness.py -q`

Expected: PASS; every public legal page returns 200 and the stale phrases are absent from current customer surfaces.

- [ ] **Step 5: Commit**

```bash
git add app/operator_profile.py app/legal_pages.py app/server.py index.html app/static/app.html tests/test_operator_profile.py tests/test_legal_pages.py tests/test_app_html_contract.py tests/test_app_frontend_contract.py tests/test_landing_html_contract.py tests/test_landing_visual_contract.py tests/test_landing_static.py tests/test_customer_requests.py tests/test_payment_readiness.py
git commit -m "feat: publish annual premium terms"
```

---

### Task 9: Readiness gate and full lifecycle smoke evidence

**Files:**
- Create: `tests/test_subscription_smoke.py`
- Modify: `app/payment_readiness.py`
- Modify: `app/payment_smoke_marker.py`
- Modify: `app/server.py:4814-5060,5415-5461`
- Modify: `tests/test_payment_readiness.py`
- Modify: `tests/test_payment_smoke_contract.py`

**Interfaces:**
- Produces: subscription readiness blockers and signed test attestation for the current release.
- Consumes: live/test variant, discount, store, webhook, API, portal, source, receipt, worker, and recipe-smoke facts.

- [ ] **Step 1: Write failing readiness tests**

```python
def test_checkout_stays_closed_without_full_subscription_smoke():
    facts = ready_facts(subscription_smoke=None)
    result = payment_readiness.evaluate(facts)
    assert "subscription_smoke_missing" in result.blockers
    assert result.ready is False

def test_smoke_requires_every_lifecycle_transition():
    marker = marker_payload(expiry_verified=False)
    assert payment_smoke_marker.valid_subscription_marker(marker, expected()) is False
```

- [ ] **Step 2: Run and confirm RED**

Run: `python -m pytest tests/test_subscription_smoke.py tests/test_payment_readiness.py tests/test_payment_smoke_contract.py -q`

Expected: readiness recognizes only one-time purchase and refund evidence.

- [ ] **Step 3: Add annual readiness facts and attestation**

Require provider-API evidence that the annual variant is 49 €, has no trial,
and that the founder discount is fixed at 10 €, `duration=once`, published,
limited to the one variant and at most 50 redemptions. Require lifecycle
evidence for initial 39 € charge, displayed 49 € renewal, activation,
successful renewal invoice, failed payment, recovery, cancellation without
early revocation, expiration, refund, portal access, webhook signature, and
reconciliation. Bind the marker to release, variant, discount, store,
discount-code fingerprint, webhook-secret fingerprint, API-key fingerprint,
and test mode. Do not expose secret values or signed checkout/portal URLs in
health output.

- [ ] **Step 4: Run readiness and deployment tests**

Run: `python -m pytest tests/test_subscription_smoke.py tests/test_payment_readiness.py tests/test_payment_smoke_contract.py tests/test_deploy_covers_all_modules.py tests/test_deploy_runtime_env.py tests/test_deploy_shell_scripts.py tests/test_deploy_safety.py tests/test_deploy_manifest.py -q`

Expected: PASS; the flag cannot enable checkout without a current valid marker.

- [ ] **Step 5: Commit**

```bash
git add app/payment_readiness.py app/payment_smoke_marker.py app/server.py tests/test_subscription_smoke.py tests/test_payment_readiness.py tests/test_payment_smoke_contract.py
git commit -m "feat: gate annual subscription payments"
```

---

### Task 10: Operations, legal checklist, release, and full verification

**Files:**
- Modify: `docs/legal/00_PRAVNY_AUDIT_UVARSI.md`
- Modify: `docs/legal/01_VOP_NAVRH.md`
- Modify: `docs/legal/04_CHECKLIST_PRED_PLATBAMI.md`
- Modify: `docs/prevadzka.md`
- Modify: `RELEASE_LOG.md`
- Modify: `VERSION`
- Modify: `hetzner/samopull.sh`
- Modify: `nasad.ps1`
- Create: `tests/test_subscription_deployment_contract.py`
- Modify: `tests/test_deploy_covers_all_modules.py`
- Modify: `tests/test_deploy_runtime_env.py`
- Modify: `tests/test_deploy_shell_scripts.py`

**Interfaces:**
- Produces: exact Lemon Squeezy setup and test-mode runbook.
- Consumes: all interfaces from Tasks 1–9.

- [ ] **Step 1: Write failing deployment-contract tests**

```python
def test_release_requires_subscription_configuration_contract():
    text = Path("docs/prevadzka.md").read_text(encoding="utf-8")
    for key in (
        "LEMON_SUBSCRIPTION_VARIANT_ID", "LEMON_FOUNDER_DISCOUNT_ID",
        "LEMON_FOUNDER_DISCOUNT_CODE", "LEMON_TEST_SUBSCRIPTION_VARIANT_ID",
        "LEMON_TEST_FOUNDER_DISCOUNT_ID", "LEMON_TEST_FOUNDER_DISCOUNT_CODE",
    ):
        assert key in text
```

- [ ] **Step 2: Run and confirm RED**

Run: `python -m pytest tests/test_subscription_deployment_contract.py -q`

Expected: the annual configuration and lifecycle runbook are absent.

- [ ] **Step 3: Update runbooks and release metadata**

Document exact dashboard settings: 49 € yearly variant whose final tax display
is verified in Lemon checkout, no trial, 10 € fixed discount with
`duration=once`, limited to the annual variant and 50 redemptions, seven-day
renewal reminder, payment recovery, Customer Portal, live/test webhook events,
and fail-closed flags. Add the new module and environment keys to the deploy
allowlists without writing secret values into Git. Include test steps for
every lifecycle event. Bump `VERSION` once after all tests pass and add a
release-log entry.

- [ ] **Step 4: Run the complete local verification**

Run: `python -m pytest -q`

Expected: all tests PASS or only documented environment-dependent skips; no live AI request occurs.

Run: `git diff --check`

Expected: no output.

Run: `rg -n "39 € raz|24 mesiacov|bez automatickej obnovy" app index.html tests`

Expected: no matches in current customer behavior; historical release notes and archived specifications are excluded.

- [ ] **Step 5: Commit the release candidate**

```bash
git add docs/legal docs/prevadzka.md RELEASE_LOG.md VERSION hetzner/samopull.sh nasad.ps1 tests/test_subscription_deployment_contract.py tests/test_deploy_covers_all_modules.py tests/test_deploy_runtime_env.py tests/test_deploy_shell_scripts.py
git commit -m "docs: prepare annual subscription release"
```

- [ ] **Step 6: Deploy with payments disabled and verify production**

Push the reviewed fast-forward commit. Wait for the normal isolated Uvar.si
deployment. Verify `/api/health`, `/`, `/app`, `/vop`, `/odstupenie`, auth,
Free plan generation, current receipt, payment state, and Customer Portal
preflight. Expected: the new release is live, receipt and plans work, and
`payments_enabled` remains false.

- [ ] **Step 7: Complete Lemon Squeezy test-mode lifecycle**

Create one test subscription and simulate or wait for every required event.
Confirm the app state after each event, run reconciliation, complete a refund,
and generate the signed lifecycle marker for the same release. Expected:
readiness has no test-lifecycle blocker; production payments remain false.

- [ ] **Step 8: Independent review before payment activation**

Review the complete branch diff for auth, checkout, webhook trust, SQLite
transactions, entitlement timing, refund behavior, secrets, cache, plan
generation, pantry, receipt, and performance. Resolve every P0/P1 finding with
a failing regression test. Re-run the full suite and live preflight.

- [ ] **Step 9: Request separate owner approval for production activation**

Present the exact live price, renewal price, legal version, source approval,
test purchase evidence, lifecycle evidence, open payment cases, and health
blockers. Do not change the production payment flag until the owner explicitly
approves activation after seeing that evidence.
