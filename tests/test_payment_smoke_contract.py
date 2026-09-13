import hashlib
import hmac
import importlib.util
import inspect
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys

import pytest

from app.payment_readiness import PaymentReadinessInput, assess_payment_readiness


ROOT = Path(__file__).resolve().parents[1]
SMOKE_SCRIPT = ROOT / "hetzner" / "payment-smoke.py"
MARKER_MODULE = ROOT / "app" / "payment_smoke_marker.py"
SAMOPULL = ROOT / "hetzner" / "samopull.sh"
DEPLOY_STATE = ROOT / "hetzner" / "uvarsi-deploy-state.sh"
MANUAL_DEPLOY = ROOT / "nasad.ps1"


def _load_marker_module():
    spec = importlib.util.spec_from_file_location("payment_smoke_marker", MARKER_MODULE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _load_smoke_module():
    app_dir = str(ROOT / "app")
    if app_dir not in sys.path:
        sys.path.insert(0, app_dir)
    spec = importlib.util.spec_from_file_location("payment_smoke_tool", SMOKE_SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _annual_expectation(marker_module):
    return marker_module.SubscriptionMarkerExpectation(
        release="release-annual-1",
        live=marker_module.SubscriptionConfig(
            "live-store", "live-annual", "live-founder", "LIVE-CODE",
            "live-webhook", "live-api", False,
        ),
        test=marker_module.SubscriptionConfig(
            "test-store", "test-annual", "test-founder", "TEST-CODE",
            "test-webhook", "test-api", True,
        ),
        signing_secret="marker-secret",
    )


def _schema5_expectation(marker_module, *, release="release-annual-1"):
    annual = _annual_expectation(marker_module)
    return marker_module.SubscriptionMarkerExpectation(
        release=release,
        live=annual.live,
        test=annual.test,
        signing_secret=annual.signing_secret,
        probe=marker_module.LifecycleProbeConfig(
            api_key=annual.test.api_key,
            store_id=annual.test.store_id,
            variant_id="test-daily-probe",
            webhook_secret="probe-webhook",
            price_cents=100,
        ),
    )


def _annual_provider_request(*, mode, code, changes=None):
    store_id = f"{mode}-store"
    variant_id = f"{mode}-annual"
    discount_id = f"{mode}-founder"
    test_mode = mode == "test"
    values = {
        "store_currency": "EUR",
        "variant_price": 4_900,
        "variant_interval": "year",
        "variant_interval_count": 1,
        "variant_has_free_trial": False,
        "discount_amount": 1_000,
        "discount_amount_type": "fixed",
        "discount_duration": "once",
        "discount_status": "published",
        "discount_max_redemptions": 50,
        "discount_code": code,
    }
    values.update(changes or {})

    def request(_api_key, path, **_kwargs):
        if path == f"/v1/stores/{store_id}":
            return {"data": {"type": "stores", "id": store_id, "attributes": {
                "currency": values["store_currency"],
            }}}
        if path == f"/v1/variants/{variant_id}":
            return {"data": {"type": "variants", "id": variant_id, "attributes": {
                "product_id": f"{mode}-product",
                "test_mode": test_mode,
                "status": "published",
                "price": values["variant_price"],
                "is_subscription": True,
                "interval": values["variant_interval"],
                "interval_count": values["variant_interval_count"],
                "has_free_trial": values["variant_has_free_trial"],
            }}}
        if path == f"/v1/products/{mode}-product":
            return {"data": {"type": "products", "id": f"{mode}-product", "attributes": {
                "store_id": store_id,
                "test_mode": test_mode,
                "status": "published",
            }}}
        if path == f"/v1/variants?filter%5Bproduct_id%5D={mode}-product":
            return {"data": [{
                "type": "variants", "id": variant_id,
                "attributes": {
                    "product_id": f"{mode}-product",
                    "test_mode": test_mode,
                    "status": "published",
                },
            }]}
        if path == f"/v1/discounts/{discount_id}":
            return {"data": {
                "type": "discounts",
                "id": discount_id,
                "attributes": {
                    "store_id": store_id,
                    "test_mode": test_mode,
                    "code": values["discount_code"],
                    "amount": values["discount_amount"],
                    "amount_type": values["discount_amount_type"],
                    "duration": values["discount_duration"],
                    "status": values["discount_status"],
                    "is_limited_redemptions": True,
                    "max_redemptions": values["discount_max_redemptions"],
                },
            }}
        if path == f"/v1/discounts/{discount_id}/variants":
            return {"data": [{"type": "variants", "id": variant_id}]}
        raise AssertionError(path)

    return request


def _complete_local_lifecycle_records():
    subscription = {
        "test_mode": 1,
        "status": "expired",
        "initial_amount_cents": 3_900,
        "renewal_amount_cents": 4_900,
        "founder": 1,
        "initial_payment_verified": 1,
        "needs_review": 0,
        "paid_through": 3_000.0,
    }
    invoices = [
        {
            "invoice_kind": "initial", "status": "refunded",
            "amount_cents": 3_900, "refunded_amount_cents": 3_900,
            "period_end": 2_000.0,
        },
        {
            "invoice_kind": "renewal", "status": "paid",
            "amount_cents": 4_900, "refunded_amount_cents": 0,
            "period_end": 3_000.0,
        },
    ]
    event_types = (
        "subscription_created",
        "subscription_payment_success",
        "subscription_payment_failed",
        "subscription_payment_recovered",
        "subscription_cancelled",
        "subscription_expired",
        "subscription_payment_refunded",
    )
    processed_times = {
        "subscription_created": 1_000.0,
        "subscription_payment_success": 1_100.0,
        "subscription_payment_failed": 2_000.0,
        "subscription_payment_recovered": 2_100.0,
        "subscription_cancelled": 2_500.0,
        "subscription_expired": 3_000.0,
        "subscription_payment_refunded": 3_100.0,
    }
    events = [
        {
            "event_type": event_type,
            "source": "webhook",
            "processing_status": "processed",
            "needs_review": 0,
            "processed_at": processed_times[event_type],
        }
        for event_type in event_types
    ]
    events.append({
        "event_type": "subscription_updated",
        "source": "reconciliation",
        "processing_status": "processed",
        "needs_review": 0,
        "processed_at": 3_200.0,
    })
    return subscription, invoices, events


def _public_health(*, blockers, ready=False):
    return {
        "vydanie": "release-1",
        "recipe_engine": {"payments_enabled": False},
        "payment_readiness": {
            "ready": ready,
            "blockers": blockers,
            "legal_version": "2026-09-12-v5",
            "release": "release-1",
        },
    }


def _run_public_preflight(monkeypatch, health):
    smoke = _load_smoke_module()
    monkeypatch.setattr(smoke, "_json_request", lambda *_a, **_k: health)
    return smoke, smoke._public_preflight("https://uvar.si", "release-1")


def test_public_preflight_allows_first_annual_marker_bootstrap(monkeypatch):
    health = _public_health(blockers=["subscription_smoke_missing"])

    _smoke, result = _run_public_preflight(monkeypatch, health)

    assert result is health


def test_public_preflight_allows_expired_annual_marker_refresh(monkeypatch):
    health = _public_health(blockers=["subscription_smoke_stale"])

    _smoke, result = _run_public_preflight(monkeypatch, health)

    assert result is health


@pytest.mark.parametrize(
    "blocker",
    [
        "subscription_smoke_invalid",
        "subscription_smoke_incomplete",
        "subscription_smoke_mismatch",
    ],
)
def test_public_preflight_allows_only_other_repairable_marker_states(
        monkeypatch, blocker):
    health = _public_health(blockers=[blocker])

    _smoke, result = _run_public_preflight(monkeypatch, health)

    assert result is health


def test_public_preflight_keeps_unrelated_readiness_blockers_fatal(monkeypatch):
    health = _public_health(blockers=["receipt_unhealthy"])
    smoke = _load_smoke_module()
    monkeypatch.setattr(smoke, "_json_request", lambda *_a, **_k: health)

    with pytest.raises(smoke.SmokeFailed, match="iné blokátory"):
        smoke._public_preflight("https://uvar.si", "release-1")


@pytest.mark.parametrize(
    "readiness",
    [
        None,
        [],
        {},
        {"ready": False, "blockers": None,
         "legal_version": "2026-09-12-v5", "release": "release-1"},
        {"ready": False, "blockers": ("subscription_smoke_missing",),
         "legal_version": "2026-09-12-v5", "release": "release-1"},
        {"ready": False, "blockers": [1],
         "legal_version": "2026-09-12-v5", "release": "release-1"},
        {"ready": False, "blockers": [""],
         "legal_version": "2026-09-12-v5", "release": "release-1"},
        {"ready": True, "blockers": ["subscription_smoke_missing"],
         "legal_version": "2026-09-12-v5", "release": "release-1"},
        {"ready": False, "blockers": [],
         "legal_version": "2026-09-12-v5", "release": "release-1"},
        {"ready": False, "blockers": ["subscription_smoke_missing"],
         "legal_version": 5, "release": "release-1"},
        {"ready": False, "blockers": ["subscription_smoke_missing"],
         "legal_version": "2026-09-12-v5", "release": "other-release"},
    ],
)
def test_public_preflight_rejects_missing_or_malformed_readiness(
        monkeypatch, readiness):
    health = _public_health(blockers=[])
    health["payment_readiness"] = readiness
    smoke = _load_smoke_module()
    monkeypatch.setattr(smoke, "_json_request", lambda *_a, **_k: health)

    with pytest.raises(smoke.SmokeFailed, match="tvar pripravenosti"):
        smoke._public_preflight("https://uvar.si", "release-1")


def test_deployment_starts_with_payments_off_and_migrates_before_health():
    samopull = SAMOPULL.read_text(encoding="utf-8")

    off_gate = samopull.index("uvarsi_require_payments_off")
    live_mutation = samopull.index("# --- 3. záloha aktuálneho stavu a prepnutie ---")
    migration = samopull.index("uvarsi_migrate_release")
    health = samopull.index("&& zdravie")

    assert off_gate < live_mutation
    assert live_mutation < migration < health


def test_manual_release_prepares_dependencies_and_checks_running_payment_flag_first():
    source = MANUAL_DEPLOY.read_text(encoding="utf-8")

    dependencies = source.index('Krok "4/8  Python venv a zavislosti"')
    preflight = source.index("$releasePreflight = @'")
    runtime_gate = source.index("uvarsi_require_runtime_payments_off")
    mutation = source.index("$script:LiveMutationStarted = $true")

    assert dependencies < preflight < runtime_gate < mutation
    assert source.count("uvarsi_require_runtime_payments_off") >= 3
    assert "requirements-auth.txt" in source


def test_automatic_rollback_never_restores_the_live_database():
    deploy = DEPLOY_STATE.read_text(encoding="utf-8")
    restore = deploy.split("uvarsi_restore()", 1)[1].split(
        "_uvarsi_apply_core()", 1
    )[0]

    assert "_uvarsi_restore_database" not in restore
    assert "uvarsi.db" not in restore
    assert "_uvarsi_snapshot_database" in deploy


def test_smoke_tool_has_no_card_input_and_never_logs_secrets():
    source = SMOKE_SCRIPT.read_text(encoding="utf-8")
    lower = source.casefold()

    assert "card_number" not in lower
    assert "cvv" not in lower
    assert "cvc" not in lower
    assert "authorization: bearer" not in lower
    assert "print(api_key" not in lower
    assert "print(webhook_secret" not in lower
    assert "/api/platba/stav" in source
    assert "/v1/orders/" in source and "/refund" in source


def test_signed_marker_is_bound_to_release_store_variant_and_full_lifecycle():
    marker_module = _load_marker_module()
    secret = "test-only-signing-secret"
    raw_order_id = "provider-test-order-do-not-persist-4f891a"
    live_fingerprint = marker_module.live_config_fingerprint(
        secret=secret,
        checkout_url="https://uvarsi.lemonsqueezy.com/checkout/buy/live-product",
        webhook_secret="live-webhook-secret",
        store_id="123",
        variant_id="456",
        api_key="live-api-key",
    )
    test_fingerprint = marker_module.test_config_fingerprint(
        secret=secret,
        checkout_url="https://uvarsi.lemonsqueezy.com/checkout/test-product",
        webhook_secret="test-webhook-secret",
        store_id="test-123",
        variant_id="test-456",
        api_key="test-api-key",
    )
    marker = marker_module.create_marker(
        release="2026.09.07.29",
        live_config_digest=live_fingerprint,
        test_config_digest=test_fingerprint,
        test_store_id="test-123",
        test_variant_id="test-456",
        completed_at="2026-09-07T20:15:00+00:00",
        receipt_email_verified=True,
        test_mode_verified=True,
    )

    assert marker_module.verify_marker(
        marker,
        secret=secret,
        release="2026.09.07.29",
        checkout_url="https://uvarsi.lemonsqueezy.com/checkout/buy/live-product",
        webhook_secret="live-webhook-secret",
        store_id="123",
        variant_id="456",
        api_key="live-api-key",
    ) is False

    marker = marker_module.sign_marker(marker, secret=secret)
    assert marker_module.verify_marker(
        marker,
        secret=secret,
        release="2026.09.07.29",
        checkout_url="https://uvarsi.lemonsqueezy.com/checkout/buy/live-product",
        webhook_secret="live-webhook-secret",
        store_id="123",
        variant_id="456",
        api_key="live-api-key",
    ) is True
    assert marker["purchase_webhook_verified"] is True
    assert marker["entitlement_verified"] is True
    assert marker["receipt_email_verified"] is True
    assert marker["refund_webhook_verified"] is True
    assert marker["entitlement_revoked"] is True
    assert marker["test_mode_verified"] is True
    assert marker["unresolved_cases"] == 0
    assert "order_digest" not in marker
    assert raw_order_id not in json.dumps(marker)
    assert "test-webhook-secret" not in json.dumps(marker)
    assert "test-api-key" not in json.dumps(marker)
    assert "test-product" not in json.dumps(marker)
    assert "live-webhook-secret" not in json.dumps(marker)
    assert "live-api-key" not in json.dumps(marker)

    for field, wrong in (
        ("release", "2026.09.07.30"),
        ("checkout_url", "https://uvarsi.lemonsqueezy.com/checkout/buy/other"),
        ("webhook_secret", "other-live-webhook-secret"),
        ("store_id", "999"),
        ("variant_id", "999"),
        ("api_key", "other-live-api-key"),
    ):
        values = {
            "release": "2026.09.07.29",
            "checkout_url": "https://uvarsi.lemonsqueezy.com/checkout/buy/live-product",
            "webhook_secret": "live-webhook-secret",
            "store_id": "123",
            "variant_id": "456",
            "api_key": "live-api-key",
        }
        values[field] = wrong
        assert marker_module.verify_marker(marker, secret=secret, **values) is False

    tampered = dict(marker, unresolved_cases=1)
    assert marker_module.verify_marker(
        tampered,
        secret=secret,
        release="2026.09.07.29",
        checkout_url="https://uvarsi.lemonsqueezy.com/checkout/buy/live-product",
        webhook_secret="live-webhook-secret",
        store_id="123",
        variant_id="456",
        api_key="live-api-key",
    ) is False

    for bad_mode in (False, None):
        without_test_proof = dict(marker)
        if bad_mode is None:
            without_test_proof.pop("test_mode_verified")
        else:
            without_test_proof["test_mode_verified"] = bad_mode
        without_test_proof = marker_module.sign_marker(
            without_test_proof, secret=secret
        )
        assert marker_module.verify_marker(
            without_test_proof,
            secret=secret,
            release="2026.09.07.29",
            checkout_url="https://uvarsi.lemonsqueezy.com/checkout/buy/live-product",
            webhook_secret="live-webhook-secret",
            store_id="123",
            variant_id="456",
            api_key="live-api-key",
        ) is False


def test_marker_signature_uses_canonical_hmac_sha256():
    marker_module = _load_marker_module()
    secret = "secret"
    unsigned = marker_module.create_marker(
        release="r1",
        live_config_digest=marker_module.live_config_fingerprint(
            secret=secret,
            checkout_url="https://uvarsi.lemonsqueezy.com/checkout/buy/live",
            webhook_secret="live-webhook",
            store_id="s1",
            variant_id="v1",
            api_key="live-api",
        ),
        test_config_digest=marker_module.test_config_fingerprint(
            secret=secret,
            checkout_url="https://uvarsi.lemonsqueezy.com/checkout/test",
            webhook_secret="test-webhook",
            store_id="ts1",
            variant_id="tv1",
            api_key="test-api",
        ),
        test_store_id="ts1",
        test_variant_id="tv1",
        completed_at="2026-09-07T20:15:00+00:00",
        receipt_email_verified=True,
        test_mode_verified=True,
    )
    signed = marker_module.sign_marker(unsigned, secret=secret)
    payload = json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode()

    assert hmac.compare_digest(
        signed["signature"], hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
    )


def test_server_reads_only_a_local_release_bound_smoke_marker():
    source = (ROOT / "app" / "server.py").read_text(encoding="utf-8")

    assert "UVARSI_PAYMENT_SMOKE_MARKER" in source
    assert "subscription_marker_status" in source
    assert "subscription_activation_status" in source
    assert "SubscriptionMarkerExpectation" in source
    assert "UVARSI_PAYMENT_SMOKE_SIGNING_SECRET" in source
    runtime = source.split("def _runtime_payment_readiness", 1)[1].split(
        "def _smoke_counts", 1
    )[0]
    assert 'env("LEMON_VARIANT_ID"' not in runtime
    assert 'env("LEMON_TEST_VARIANT_ID"' not in runtime
    assert "_subscription_marker_expectation" in runtime
    expectation_builder = source.split(
        "def _subscription_marker_expectation", 1
    )[1].split("def _subscription_evidence_status", 1)[0]
    annual_builder = source.split(
        "def _annual_subscription_marker_configs", 1
    )[1].split("def _subscription_marker_expectation", 1)[0]
    assert "lemon_subscription_checkout_config" in annual_builder
    assert "read_probe_config" in expectation_builder
    assert "probe=probe" in expectation_builder


def test_legacy_purchase_refund_marker_is_not_annual_subscription_evidence():
    marker_module = _load_marker_module()
    legacy = marker_module.sign_marker(
        marker_module.create_marker(
            release="release-1",
            live_config_digest="a" * 64,
            test_config_digest="b" * 64,
            test_store_id="test-store",
            test_variant_id="test-variant",
            completed_at="2026-09-13T09:00:00+00:00",
            receipt_email_verified=True,
            test_mode_verified=True,
        ),
        secret="marker-secret",
    )

    expectation = marker_module.SubscriptionMarkerExpectation(
        release="release-1",
        live=marker_module.SubscriptionConfig(
            "live-store", "live-variant", "live-discount", "LIVE-CODE",
            "live-webhook", "live-api", False,
        ),
        test=marker_module.SubscriptionConfig(
            "test-store", "test-variant", "test-discount", "TEST-CODE",
            "test-webhook", "test-api", True,
        ),
        signing_secret="marker-secret",
    )

    assert marker_module.valid_subscription_marker(
        legacy,
        expectation,
        now=datetime(2026, 9, 13, 10, 0, tzinfo=timezone.utc),
    ) is False


def test_test_checkout_is_rejected_before_url_is_returned_when_variant_is_live():
    smoke = _load_smoke_module()
    calls = []

    def provider_request(_api_key, path, **kwargs):
        calls.append((path, kwargs.get("method", "GET")))
        return {
            "data": {
                "type": "variants",
                "id": "456",
                "attributes": {
                    "test_mode": False,
                    "status": "published",
                    "price": 3900,
                    "is_subscription": False,
                    "product_id": 77,
                },
            }
        }

    with pytest.raises(smoke.SmokeFailed, match="testovacom režime"):
        smoke._verified_test_variant(
            "test-api-key", store_id="123", variant_id="456",
            request=provider_request,
        )

    assert calls == [("/v1/variants/456", "GET")]


def test_variant_store_is_verified_through_its_product_not_a_fake_variant_field():
    smoke = _load_smoke_module()
    calls = []

    def provider_request(_api_key, path, **kwargs):
        calls.append((path, kwargs.get("method", "GET")))
        if path == "/v1/variants/456":
            return {
                "data": {
                    "type": "variants",
                    "id": "456",
                    "attributes": {
                        "product_id": 77,
                        "test_mode": True,
                        "status": "published",
                        "price": 3900,
                        "is_subscription": False,
                    },
                }
            }
        if path == "/v1/products/77":
            return {
                "data": {
                    "type": "products",
                    "id": "77",
                    "attributes": {
                        "store_id": 999,
                        "test_mode": True,
                        "status": "published",
                        "buy_now_url": "https://uvarsi.lemonsqueezy.com/buy/founder",
                    },
                }
            }
        raise AssertionError(path)

    with pytest.raises(smoke.SmokeFailed, match="inému obchodu"):
        smoke._verified_test_variant(
            "test-api-key",
            store_id="123",
            variant_id="456",
            request=provider_request,
        )

    assert calls == [
        ("/v1/variants/456", "GET"),
        ("/v1/products/77", "GET"),
    ]


def test_live_checkout_is_bound_to_provider_product_and_its_only_variant():
    smoke = _load_smoke_module()
    calls = []
    checkout_url = "https://uvarsi.lemonsqueezy.com/buy/founder"

    def provider_request(_api_key, path, **kwargs):
        calls.append(path)
        if path == "/v1/variants/456":
            return {
                "data": {
                    "type": "variants",
                    "id": "456",
                    "attributes": {
                        "product_id": 77,
                        "test_mode": False,
                        "status": "published",
                        "price": 3900,
                        "is_subscription": False,
                    },
                }
            }
        if path == "/v1/products/77":
            return {
                "data": {
                    "type": "products",
                    "id": "77",
                    "attributes": {
                        "store_id": 123,
                        "test_mode": False,
                        "status": "published",
                        "buy_now_url": checkout_url,
                    },
                }
            }
        if path == "/v1/variants?filter%5Bproduct_id%5D=77":
            return {
                "data": [{
                    "type": "variants",
                    "id": "456",
                    "attributes": {
                        "product_id": 77,
                        "test_mode": False,
                        "status": "published",
                    },
                }]
            }
        raise AssertionError(path)

    result = smoke._verified_live_configuration(
        "live-api-key",
        store_id="123",
        variant_id="456",
        checkout_url=checkout_url,
        request=provider_request,
    )

    assert result["product"]["id"] == "77"
    assert calls == [
        "/v1/variants/456",
        "/v1/products/77",
        "/v1/variants?filter%5Bproduct_id%5D=77",
    ]


@pytest.mark.parametrize(
    "configured_url, extra_variant",
    [
        ("https://uvarsi.lemonsqueezy.com/buy/iny", False),
        ("https://uvarsi.lemonsqueezy.com/buy/founder", True),
    ],
)
def test_live_checkout_rejects_wrong_url_or_an_ambiguous_multi_variant_product(
    configured_url, extra_variant
):
    smoke = _load_smoke_module()
    provider_url = "https://uvarsi.lemonsqueezy.com/buy/founder"

    def provider_request(_api_key, path, **_kwargs):
        if path == "/v1/variants/456":
            return {"data": {"type": "variants", "id": "456", "attributes": {
                "product_id": 77, "test_mode": False, "status": "published",
                "price": 3900, "is_subscription": False,
            }}}
        if path == "/v1/products/77":
            return {"data": {"type": "products", "id": "77", "attributes": {
                "store_id": 123, "test_mode": False, "status": "published",
                "buy_now_url": provider_url,
            }}}
        if path == "/v1/variants?filter%5Bproduct_id%5D=77":
            variants = [{"type": "variants", "id": "456", "attributes": {
                "product_id": 77, "test_mode": False, "status": "published",
            }}]
            if extra_variant:
                variants.append({"type": "variants", "id": "789", "attributes": {
                    "product_id": 77, "test_mode": False, "status": "published",
                }})
            return {"data": variants}
        raise AssertionError(path)

    with pytest.raises(smoke.SmokeFailed, match="poklad|variant"):
        smoke._verified_live_configuration(
            "live-api-key",
            store_id="123",
            variant_id="456",
            checkout_url=configured_url,
            request=provider_request,
        )


def test_test_checkout_url_comes_from_provider_confirmed_test_checkout():
    smoke = _load_smoke_module()
    payloads = []

    def provider_request(_api_key, path, **kwargs):
        payloads.append((path, kwargs))
        return {
            "data": {
                "type": "checkouts",
                "id": "checkout-1",
                "attributes": {
                    "store_id": 123,
                    "variant_id": 456,
                    "test_mode": True,
                    "url": "https://example.lemonsqueezy.com/checkout/custom/test",
                },
            }
        }

    url = smoke._create_verified_test_checkout(
        "test-api-key",
        store_id="123",
        variant_id="456",
        user_id=7,
        attempt_id="attempt-token",
        email="test@example.com",
        request=provider_request,
    )

    assert url == "https://example.lemonsqueezy.com/checkout/custom/test"
    path, request_args = payloads[0]
    assert path == "/v1/checkouts"
    assert request_args["method"] == "POST"
    body = request_args["payload"]["data"]
    assert body["attributes"]["test_mode"] is True
    assert body["attributes"]["checkout_data"]["custom"] == {
        "user_id": "7",
        "checkout_attempt": "attempt-token",
    }


def test_smoke_tool_main_is_annual_and_never_falls_back_to_one_time_flow():
    source = SMOKE_SCRIPT.read_text(encoding="utf-8")
    main_source = source[source.index("def main("):]

    assert 'prefix = "LEMON_TEST_" if test_mode else "LEMON_"' in source
    assert 'f"{prefix}SUBSCRIPTION_VARIANT_ID"' in source
    assert 'f"{prefix}FOUNDER_DISCOUNT_ID"' in source
    assert 'f"{prefix}FOUNDER_DISCOUNT_CODE"' in source
    assert "_annual_expectation_from_env" in main_source
    assert "_verified_annual_provider_evidence" in main_source
    assert "_lifecycle_evidence_from_records" in main_source
    assert "_build_annual_subscription_marker" in main_source
    assert "LEMON_CHECKOUT_URL" not in main_source
    assert '"LEMON_VARIANT_ID"' not in main_source
    assert "_refund_test_order(" not in main_source
    assert "spracuj_odlozene(" not in source
    assert "rekonciluj(" not in source


def test_invalid_exact_smoke_signature_fails_cleanly_without_traceback():
    smoke = _load_smoke_module()

    class FakeConnection:
        def close(self):
            pass

    class FakeServer:
        @staticmethod
        def db():
            return FakeConnection()

    class FakePayments:
        class UdalostNepouzitelna(ValueError):
            pass

        @staticmethod
        def spracuj_odlozene_pre_smoke(*_args, **_kwargs):
            raise FakePayments.UdalostNepouzitelna("citlivý interný detail")

    with pytest.raises(smoke.SmokeFailed, match="[Pp]odpis") as error:
        smoke._wait_for_signed_webhook(
            FakeServer,
            FakePayments,
            secret="test-secret",
            variant_id="variant-1",
            order_id="order-1",
            event_type="order_created",
            timeout_seconds=1,
        )

    assert "citlivý interný detail" not in str(error.value)


def test_marker_can_only_be_built_from_exact_signed_webhook_lifecycle():
    smoke = _load_smoke_module()
    marker_module = _load_marker_module()
    common = {
        "release": "release-1",
        "live_checkout_url": "https://uvarsi.lemonsqueezy.com/checkout/buy/live",
        "live_webhook_secret": "live-webhook-secret",
        "live_store_id": "live-store",
        "live_variant_id": "live-variant",
        "live_api_key": "live-api-key",
        "test_store_id": "test-store",
        "test_variant_id": "test-variant",
        "test_checkout_url": "https://uvarsi.lemonsqueezy.com/checkout/test",
        "test_webhook_secret": "test-webhook-secret",
        "test_api_key": "test-api-key",
        "order_id": "test-order",
        "receipt_email_verified": True,
        "unresolved_cases": 0,
        "completed_at": "2026-09-08T10:00:00+00:00",
        "signing_secret": "local-marker-secret",
        "create_marker": marker_module.create_marker,
        "live_config_fingerprint": marker_module.live_config_fingerprint,
        "test_config_fingerprint": marker_module.test_config_fingerprint,
        "sign_marker": marker_module.sign_marker,
    }
    purchase = {
        "typ": "order_created",
        "objednavka": "test-order",
        "akcia": "udelene",
        "zdroj": "odlozene",
        "test_mode": True,
    }
    refund = {
        "typ": "order_refunded",
        "objednavka": "test-order",
        "akcia": "vratene",
        "zdroj": "odlozene",
        "test_mode": True,
    }

    marker = smoke._build_completed_marker(
        purchase_event=purchase, refund_event=refund, **common
    )
    assert marker_module.verify_marker(
        marker,
        secret=common["signing_secret"],
        release=common["release"],
        checkout_url=common["live_checkout_url"],
        webhook_secret=common["live_webhook_secret"],
        store_id=common["live_store_id"],
        variant_id=common["live_variant_id"],
        api_key=common["live_api_key"],
    ) is True
    assert marker["test_config_digest"] == marker_module.test_config_fingerprint(
        secret=common["signing_secret"],
        checkout_url=common["test_checkout_url"],
        webhook_secret=common["test_webhook_secret"],
        store_id=common["test_store_id"],
        variant_id=common["test_variant_id"],
        api_key=common["test_api_key"],
    )
    serialized = json.dumps(marker)
    assert common["test_checkout_url"] not in serialized
    assert common["test_webhook_secret"] not in serialized
    assert common["test_api_key"] not in serialized
    assert common["live_webhook_secret"] not in serialized
    assert common["live_api_key"] not in serialized

    with pytest.raises(smoke.SmokeFailed, match="webhook"):
        smoke._build_completed_marker(
            purchase_event=dict(purchase, zdroj="rekonciliacia"),
            refund_event=refund,
            **common,
        )

    with pytest.raises(smoke.SmokeFailed, match="objednávk"):
        smoke._build_completed_marker(
            purchase_event=purchase,
            refund_event=dict(refund, objednavka="other-order"),
            **common,
        )

    with pytest.raises(smoke.SmokeFailed, match="testovacom režime"):
        smoke._build_completed_marker(
            purchase_event=dict(purchase, test_mode=False),
            refund_event=refund,
            **common,
        )


def test_production_activation_builder_requires_fresh_signed_smoke_and_stays_valid():
    smoke = _load_smoke_module()
    marker_module = _load_marker_module()
    config = {
        "release": "release-1",
        "live_checkout_url": "https://uvarsi.lemonsqueezy.com/checkout/buy/live",
        "live_webhook_secret": "live-webhook-secret",
        "live_store_id": "live-store",
        "live_variant_id": "live-variant",
        "live_api_key": "live-api-key",
        "test_checkout_url": "https://uvarsi.lemonsqueezy.com/checkout/test",
        "test_webhook_secret": "test-webhook-secret",
        "test_store_id": "test-store",
        "test_variant_id": "test-variant",
        "test_api_key": "test-api-key",
        "signing_secret": "local-marker-secret",
    }
    test_digest = marker_module.test_config_fingerprint(
        secret=config["signing_secret"],
        checkout_url=config["test_checkout_url"],
        webhook_secret=config["test_webhook_secret"],
        store_id=config["test_store_id"],
        variant_id=config["test_variant_id"],
        api_key=config["test_api_key"],
    )
    marker = marker_module.sign_marker(
        marker_module.create_marker(
            release=config["release"],
            live_config_digest=marker_module.live_config_fingerprint(
                secret=config["signing_secret"],
                checkout_url=config["live_checkout_url"],
                webhook_secret=config["live_webhook_secret"],
                store_id=config["live_store_id"],
                variant_id=config["live_variant_id"],
                api_key=config["live_api_key"],
            ),
            test_config_digest=test_digest,
            test_store_id=config["test_store_id"],
            test_variant_id=config["test_variant_id"],
            completed_at="2026-09-12T10:00:00+00:00",
            receipt_email_verified=True,
            test_mode_verified=True,
        ),
        secret=config["signing_secret"],
    )

    activation = smoke._build_activation_attestation(
        smoke_marker=marker,
        activated_at="2026-09-12T10:30:00+00:00",
        create_activation_attestation=marker_module.create_activation_attestation,
        **config,
    )

    assert marker_module.verify_activation_attestation(
        activation,
        secret=config["signing_secret"],
        release=config["release"],
        checkout_url=config["live_checkout_url"],
        webhook_secret=config["live_webhook_secret"],
        store_id=config["live_store_id"],
        variant_id=config["live_variant_id"],
        api_key=config["live_api_key"],
        test_checkout_url=config["test_checkout_url"],
        test_webhook_secret=config["test_webhook_secret"],
        test_store_id=config["test_store_id"],
        test_variant_id=config["test_variant_id"],
        test_api_key=config["test_api_key"],
    ) is True
    serialized = json.dumps(activation)
    assert config["test_checkout_url"] not in serialized
    assert config["test_webhook_secret"] not in serialized
    assert config["test_api_key"] not in serialized
    assert config["live_webhook_secret"] not in serialized
    assert config["live_api_key"] not in serialized

    with pytest.raises(smoke.SmokeFailed, match="čerstv"):
        smoke._build_activation_attestation(
            smoke_marker=marker,
            activated_at="2026-09-13T10:00:01+00:00",
            create_activation_attestation=marker_module.create_activation_attestation,
            **config,
        )


def test_old_schema4_annual_marker_cannot_authorize_schema5_activation(
        monkeypatch, tmp_path):
    smoke = _load_smoke_module()
    marker_module = _load_marker_module()
    now = datetime.now(timezone.utc).replace(microsecond=0)
    expectation = _schema5_expectation(marker_module, release="release-1")
    signed_smoke = marker_module.sign_marker({
        "schema_version": 4,
        "attestation_id": "a" * 64,
        "release": expectation.release,
        "lifecycle": {
            "renewal_invoice_cents": 4_900,
            "webhook_signature_verified": True,
        },
        "completed_at": now.isoformat(),
        "expires_at": (now + timedelta(hours=24)).isoformat(),
        "unresolved_cases": 0,
    }, secret=expectation.signing_secret)
    smoke_path = tmp_path / "payment-smoke.json"
    activation_path = tmp_path / "payment-activation.json"
    smoke_path.write_text(json.dumps(signed_smoke), encoding="utf-8")

    class Server:
        @staticmethod
        def release_id():
            return "release-1"

    monkeypatch.setattr(
        smoke,
        "_load_runtime",
        lambda _app_dir: (
            Server,
            marker_module.AnnualProviderEvidence,
            marker_module.SubscriptionConfig,
            marker_module.SubscriptionLifecycleEvidence,
            marker_module.SubscriptionMarkerExpectation,
            marker_module.create_subscription_activation_attestation,
            marker_module.create_subscription_marker,
            marker_module.discount_code_fingerprint,
            marker_module.sign_marker,
        ),
    )
    monkeypatch.setattr(
        smoke, "_annual_expectation_from_env", lambda **_kwargs: expectation
    )
    monkeypatch.setattr(
        smoke,
        "_public_preflight",
        lambda *_args, **_kwargs: pytest.fail("aktivácia nesmie volať sieť"),
    )

    with pytest.raises(smoke.SmokeFailed, match="čerstvý annual smoke dôkaz"):
        smoke.main([
            "--authorize-activation",
            "--marker", str(smoke_path),
            "--activation-marker", str(activation_path),
        ])

    assert marker_module.subscription_marker_status(
        signed_smoke, expectation, now=now
    ) == "subscription_smoke_incomplete"
    assert activation_path.exists() is False


@pytest.mark.parametrize(
    "changes",
    [
        {"variant_price": 3_900},
        {"store_currency": "USD"},
        {"variant_interval": "month"},
        {"variant_interval_count": 12},
        {"variant_has_free_trial": True},
        {"discount_amount": 999},
        {"discount_amount_type": "percent"},
        {"discount_duration": "forever"},
        {"discount_status": "draft"},
        {"discount_max_redemptions": 51},
    ],
)
def test_annual_provider_tool_rejects_wrong_variant_or_discount_economics(changes):
    smoke = _load_smoke_module()
    marker_module = _load_marker_module()
    expectation = _annual_expectation(marker_module)

    with pytest.raises(smoke.SmokeFailed):
        smoke._verified_annual_provider_evidence(
            "test-api",
            config=expectation.test,
            signing_secret=expectation.signing_secret,
            request=_annual_provider_request(
                mode="test", code="TEST-CODE", changes=changes
            ),
            evidence_type=marker_module.AnnualProviderEvidence,
            fingerprint=marker_module.discount_code_fingerprint,
        )


def test_provider_returned_discount_code_fingerprint_must_match_runtime_code():
    smoke = _load_smoke_module()
    marker_module = _load_marker_module()
    expectation = _annual_expectation(marker_module)

    with pytest.raises(smoke.SmokeFailed, match="zľav"):
        smoke._verified_annual_provider_evidence(
            "test-api",
            config=expectation.test,
            signing_secret=expectation.signing_secret,
            request=_annual_provider_request(mode="test", code="WRONG-CODE"),
            evidence_type=marker_module.AnnualProviderEvidence,
            fingerprint=marker_module.discount_code_fingerprint,
        )


def test_test_portal_proof_is_provider_bound_and_never_returns_signed_url():
    smoke = _load_smoke_module()
    signed_url = "https://app.lemonsqueezy.com/my-orders/abc?signature=secret"

    def request(_api_key, path, **_kwargs):
        assert path == "/v1/subscriptions/sub-1"
        return {"data": {
            "type": "subscriptions",
            "id": "sub-1",
            "attributes": {
                "test_mode": True,
                "variant_id": "test-annual",
                "urls": {"customer_portal": signed_url},
            },
        }}

    assert smoke._verified_test_portal_access(
        "test-api", subscription_id="sub-1", variant_id="test-annual",
        request=request,
    ) is True

    def hostile_request(*_args, **_kwargs):
        payload = request(None, "/v1/subscriptions/sub-1")
        payload["data"]["attributes"]["urls"]["customer_portal"] = (
            "https://attacker.invalid/portal"
        )
        return payload

    with pytest.raises(smoke.SmokeFailed, match="portál"):
        smoke._verified_test_portal_access(
            "test-api", subscription_id="sub-1", variant_id="test-annual",
            request=hostile_request,
        )


def test_local_records_must_prove_the_complete_annual_lifecycle():
    smoke = _load_smoke_module()
    marker_module = _load_marker_module()
    subscription, invoices, events = _complete_local_lifecycle_records()

    lifecycle = smoke._lifecycle_evidence_from_records(
        subscription=subscription,
        invoices=invoices,
        events=events,
        portal_access_verified=True,
        unresolved_cases=0,
        evidence_type=marker_module.SubscriptionLifecycleEvidence,
    )

    assert lifecycle == marker_module.SubscriptionLifecycleEvidence(
        initial_charge_cents=3_900,
        renewal_displayed_cents=4_900,
        activation_verified=True,
        renewal_invoice_cents=4_900,
        failed_payment_verified=True,
        recovery_verified=True,
        cancellation_verified=True,
        access_retained_until_period_end=True,
        expiration_verified=True,
        refund_verified=True,
        portal_access_verified=True,
        webhook_signature_verified=True,
        reconciliation_verified=True,
    )

    incomplete = [
        event for event in events
        if event["event_type"] != "subscription_payment_recovered"
    ]
    with pytest.raises(smoke.SmokeFailed, match="lifecycle"):
        smoke._lifecycle_evidence_from_records(
            subscription=subscription,
            invoices=invoices,
            events=incomplete,
            portal_access_verified=True,
            unresolved_cases=0,
            evidence_type=marker_module.SubscriptionLifecycleEvidence,
        )

    refund_without_refunded_invoice = [dict(invoice) for invoice in invoices]
    refund_without_refunded_invoice[0].update(
        status="paid", refunded_amount_cents=0
    )
    with pytest.raises(smoke.SmokeFailed, match="lifecycle"):
        smoke._lifecycle_evidence_from_records(
            subscription=subscription,
            invoices=refund_without_refunded_invoice,
            events=events,
            portal_access_verified=True,
            unresolved_cases=0,
            evidence_type=marker_module.SubscriptionLifecycleEvidence,
        )

    late_cancellation = [dict(event) for event in events]
    for event in late_cancellation:
        if event["event_type"] == "subscription_cancelled":
            event["processed_at"] = 3_001.0
    with pytest.raises(smoke.SmokeFailed, match="lifecycle"):
        smoke._lifecycle_evidence_from_records(
            subscription=subscription,
            invoices=invoices,
            events=late_cancellation,
            portal_access_verified=True,
            unresolved_cases=0,
            evidence_type=marker_module.SubscriptionLifecycleEvidence,
        )


@pytest.mark.parametrize("unresolved", [False, 0.0, "0", None])
def test_annual_tool_rejects_non_integer_unresolved_count(unresolved):
    smoke = _load_smoke_module()
    marker_module = _load_marker_module()
    subscription, invoices, events = _complete_local_lifecycle_records()

    with pytest.raises(smoke.SmokeFailed, match="nevyriešen"):
        smoke._lifecycle_evidence_from_records(
            subscription=subscription,
            invoices=invoices,
            events=events,
            portal_access_verified=True,
            unresolved_cases=unresolved,
            evidence_type=marker_module.SubscriptionLifecycleEvidence,
        )


def test_b2_contract_rejects_old_annual_as_daily_marker_path():
    smoke = _load_smoke_module()
    marker_module = _load_marker_module()
    expectation = _schema5_expectation(marker_module)
    live_provider = smoke._verified_annual_provider_evidence(
        "live-api",
        config=expectation.live,
        signing_secret=expectation.signing_secret,
        request=_annual_provider_request(mode="live", code="LIVE-CODE"),
        evidence_type=marker_module.AnnualProviderEvidence,
        fingerprint=marker_module.discount_code_fingerprint,
    )
    test_provider = smoke._verified_annual_provider_evidence(
        "test-api",
        config=expectation.test,
        signing_secret=expectation.signing_secret,
        request=_annual_provider_request(mode="test", code="TEST-CODE"),
        evidence_type=marker_module.AnnualProviderEvidence,
        fingerprint=marker_module.discount_code_fingerprint,
    )
    subscription, invoices, events = _complete_local_lifecycle_records()
    lifecycle = smoke._lifecycle_evidence_from_records(
        subscription=subscription,
        invoices=invoices,
        events=events,
        portal_access_verified=True,
        unresolved_cases=0,
        evidence_type=marker_module.SubscriptionLifecycleEvidence,
    )
    completed = datetime(2026, 9, 13, 10, 0, tzinfo=timezone.utc)

    with pytest.raises(smoke.SmokeFailed, match="nedá bezpečne podpísať"):
        smoke._build_annual_subscription_marker(
            expectation=expectation,
            live_provider=live_provider,
            test_provider=test_provider,
            lifecycle=lifecycle,
            completed_at=completed,
            create_marker=marker_module.create_subscription_marker,
            sign_marker=marker_module.sign_marker,
        )

    marker_parameters = set(
        inspect.signature(marker_module.create_subscription_marker).parameters
    )
    assert "lifecycle" not in marker_parameters
    assert {
        "annual_commercial", "probe_provider", "probe_lifecycle"
    } <= marker_parameters
    assert marker_module.PROBE_EVIDENCE_SOURCE == "test_mode_daily_probe"


def test_old_annual_main_cannot_write_schema5_marker_without_probe_evidence(
        monkeypatch, tmp_path):
    smoke = _load_smoke_module()
    marker_module = _load_marker_module()
    expectation = _schema5_expectation(marker_module)
    values = {
        "LEMON_API_KEY": expectation.live.api_key,
        "LEMON_STORE_ID": expectation.live.store_id,
        "LEMON_SUBSCRIPTION_VARIANT_ID": expectation.live.variant_id,
        "LEMON_FOUNDER_DISCOUNT_ID": expectation.live.discount_id,
        "LEMON_FOUNDER_DISCOUNT_CODE": expectation.live.discount_code,
        "LEMON_WEBHOOK_SECRET": expectation.live.webhook_secret,
        "LEMON_TEST_API_KEY": expectation.test.api_key,
        "LEMON_TEST_STORE_ID": expectation.test.store_id,
        "LEMON_TEST_SUBSCRIPTION_VARIANT_ID": expectation.test.variant_id,
        "LEMON_TEST_FOUNDER_DISCOUNT_ID": expectation.test.discount_id,
        "LEMON_TEST_FOUNDER_DISCOUNT_CODE": expectation.test.discount_code,
        "LEMON_TEST_WEBHOOK_SECRET": expectation.test.webhook_secret,
        "UVARSI_PAYMENT_SMOKE_SIGNING_SECRET": expectation.signing_secret,
    }

    class Server:
        @staticmethod
        def release_id():
            return expectation.release

    monkeypatch.setattr(smoke, "_load_runtime", lambda _app_dir: (
        Server,
        marker_module.AnnualProviderEvidence,
        marker_module.SubscriptionConfig,
        marker_module.SubscriptionLifecycleEvidence,
        marker_module.SubscriptionMarkerExpectation,
        marker_module.create_subscription_activation_attestation,
        marker_module.create_subscription_marker,
        marker_module.discount_code_fingerprint,
        marker_module.sign_marker,
    ))
    monkeypatch.setattr(
        smoke, "_env_value", lambda name, **_kwargs: values.get(name, "")
    )
    monkeypatch.setattr(
        smoke, "_annual_expectation_from_env", lambda **_kwargs: expectation
    )
    monkeypatch.setattr(smoke, "_public_preflight", lambda *_a, **_k: {})
    verify_provider = smoke._verified_annual_provider_evidence

    def provider(api_key, *, config, signing_secret, evidence_type,
                 fingerprint):
        mode = "test" if config.test_mode else "live"
        return verify_provider(
            api_key, config=config, signing_secret=signing_secret,
            request=_annual_provider_request(
                mode=mode, code=config.discount_code
            ),
            evidence_type=evidence_type, fingerprint=fingerprint,
        )

    monkeypatch.setattr(smoke, "_verified_annual_provider_evidence", provider)
    monkeypatch.setattr("builtins.input", lambda _prompt: "test@example.test")
    monkeypatch.setattr(smoke.getpass, "getpass", lambda _prompt: "password")
    monkeypatch.setattr(smoke, "_authenticated_opener", lambda *_a: object())
    monkeypatch.setattr(smoke, "_payment_status", lambda *_a: {"ma_narok": False})
    subscription, invoices, events = _complete_local_lifecycle_records()
    monkeypatch.setattr(
        smoke, "_load_annual_lifecycle_records",
        lambda *_a, **_k: (subscription, invoices, events, 0, "sub-1"),
    )
    monkeypatch.setattr(smoke, "_verified_test_portal_access", lambda *_a, **_k: True)
    marker_path = tmp_path / "annual-smoke.json"

    with pytest.raises(smoke.SmokeFailed, match="nedá bezpečne podpísať"):
        smoke.main(["--marker", str(marker_path)])

    assert marker_path.exists() is False
