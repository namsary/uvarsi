"""Fail-closed launch gate for the paid Founder checkout."""

import json
from dataclasses import replace

import pytest

from app.payment_readiness import (
    PaymentReadinessBlocked,
    PaymentReadinessInput,
    assess_payment_readiness,
    public_readiness,
    require_checkout_ready,
)


REQUIRED_LEGAL_VERSION = "2026-09-12-v4"
REQUIRED_FOUNDER_PROMISE = (
    "39 € raz. Premium garantované na 24 mesiacov, potom bez predplatného "
    "počas ďalšej prevádzky služby Uvar.si."
)


def valid_input(**changes):
    value = PaymentReadinessInput(
        operator_errors=(),
        support_phone_verified=True,
        legal_version=REQUIRED_LEGAL_VERSION,
        founder_promise=REQUIRED_FOUNDER_PROMISE,
        release="2026.09.11.28",
        checkout_url="https://uvarsi.lemonsqueezy.com/buy/test",
        webhook_secret="webhook-secret",
        store_id="12345",
        variant_id="67890",
        api_key="api-key",
        test_checkout_url="https://uvarsi.lemonsqueezy.com/checkout/test",
        test_webhook_secret="test-webhook-secret",
        test_store_id="test-store",
        test_variant_id="test-variant",
        test_api_key="test-api-key",
        source_approved=True,
        receipt_ready=True,
        private_alerts=True,
        consumer_workflows=True,
        smoke_verified=True,
        worker_alive=True,
        recipe_ready=True,
    )
    return replace(value, **changes)


def test_all_required_gates_make_checkout_ready():
    result = assess_payment_readiness(valid_input())

    assert result.ready is True
    assert result.blockers == ()
    assert result.legal_version == REQUIRED_LEGAL_VERSION
    assert result.release == "2026.09.11.28"


@pytest.mark.parametrize(
    ("change", "value", "blocker"),
    [
        ("operator_errors", ("company_id",), "operator_invalid"),
        ("support_phone_verified", False, "support_phone_not_verified"),
        ("legal_version", "", "legal_version_missing"),
        ("legal_version", "2026-09-07-v1", "legal_version_stale"),
        ("founder_promise", "cena natrvalo", "legal_promise_mismatch"),
        ("release", "", "release_missing"),
        ("checkout_url", "", "checkout_not_configured"),
        ("checkout_url", "http://example.test/buy", "checkout_not_configured"),
        ("webhook_secret", "", "webhook_not_configured"),
        ("store_id", "", "merchant_not_configured"),
        ("variant_id", "", "variant_not_configured"),
        ("api_key", "", "api_not_configured"),
        ("test_checkout_url", "", "test_checkout_not_configured"),
        ("test_checkout_url", "http://example.test", "test_checkout_not_configured"),
        ("test_webhook_secret", "", "test_webhook_not_configured"),
        ("test_store_id", "", "test_merchant_not_configured"),
        ("test_variant_id", "", "test_variant_not_configured"),
        ("test_api_key", "", "test_api_not_configured"),
        ("source_approved", False, "price_source_not_approved"),
        ("receipt_ready", False, "receipt_unhealthy"),
        ("private_alerts", False, "alerts_not_private"),
        ("consumer_workflows", False, "consumer_workflow_not_ready"),
        ("smoke_verified", False, "payment_smoke_missing"),
        ("worker_alive", False, "plan_worker_unhealthy"),
        ("recipe_ready", False, "recipe_gate_failed"),
    ],
)
def test_checkout_fails_closed_when_any_required_gate_is_missing(change, value, blocker):
    result = assess_payment_readiness(valid_input(**{change: value}))

    assert result.ready is False
    assert blocker in result.blockers


def test_public_status_contains_only_safe_stable_fields():
    result = assess_payment_readiness(
        valid_input(webhook_secret="", api_key="top-secret-api-key")
    )

    public = public_readiness(result)
    encoded = json.dumps(public)

    assert public == {
        "ready": False,
        "blockers": ["webhook_not_configured"],
        "legal_version": REQUIRED_LEGAL_VERSION,
        "release": "2026.09.11.28",
    }
    assert "LEMON_WEBHOOK_SECRET" not in encoded
    assert "top-secret-api-key" not in encoded


def test_checkout_guard_raises_with_public_codes_only():
    result = assess_payment_readiness(valid_input(source_approved=False))

    with pytest.raises(PaymentReadinessBlocked) as caught:
        require_checkout_ready(result)

    assert caught.value.blockers == ("price_source_not_approved",)
    assert "source_approved" not in str(caught.value)


def test_checkout_guard_accepts_ready_release():
    result = assess_payment_readiness(valid_input())

    assert require_checkout_ready(result) is result
