"""Fail-closed launch gate for the paid Founder checkout."""

import json
from dataclasses import replace

import pytest

from app import config as app_config
from app.payment_readiness import (
    PaymentReadinessBlocked,
    PaymentReadinessInput,
    assess_payment_readiness,
    public_readiness,
    require_checkout_ready,
)


REQUIRED_LEGAL_VERSION = "2026-09-12-v5"
REQUIRED_FOUNDER_PROMISE = (
    "Prvý rok za 39 €. Potom 49 € ročne. Predplatné sa automaticky "
    "obnovuje, kým ho nezrušíš. Zrušiť ho môžeš kedykoľvek; Premium "
    "zostane aktívne do konca zaplateného obdobia."
)


def valid_input(**changes):
    value = PaymentReadinessInput(
        operator_errors=(),
        support_phone_verified=True,
        legal_version=REQUIRED_LEGAL_VERSION,
        founder_promise=REQUIRED_FOUNDER_PROMISE,
        release="2026.09.11.28",
        webhook_secret="webhook-secret",
        store_id="12345",
        variant_id="67890",
        discount_id="founder-discount",
        discount_code="FOUNDERS",
        api_key="api-key",
        test_webhook_secret="test-webhook-secret",
        test_store_id="test-store",
        test_variant_id="test-variant",
        test_discount_id="test-founder-discount",
        test_discount_code="TEST-FOUNDERS",
        test_api_key="test-api-key",
        source_approved=True,
        receipt_ready=True,
        private_alerts=True,
        consumer_workflows=True,
        subscription_smoke="verified",
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
        ("webhook_secret", "", "webhook_not_configured"),
        ("store_id", "", "merchant_not_configured"),
        ("variant_id", "", "variant_not_configured"),
        ("discount_id", "", "discount_not_configured"),
        ("discount_code", "", "discount_code_not_configured"),
        ("api_key", "", "api_not_configured"),
        ("test_webhook_secret", "", "test_webhook_not_configured"),
        ("test_store_id", "", "test_merchant_not_configured"),
        ("test_variant_id", "", "test_variant_not_configured"),
        ("test_discount_id", "", "test_discount_not_configured"),
        ("test_discount_code", "", "test_discount_code_not_configured"),
        ("test_api_key", "", "test_api_not_configured"),
        ("source_approved", False, "price_source_not_approved"),
        ("receipt_ready", False, "receipt_unhealthy"),
        ("private_alerts", False, "alerts_not_private"),
        ("consumer_workflows", False, "consumer_workflow_not_ready"),
        ("subscription_smoke", None, "subscription_smoke_missing"),
        ("subscription_smoke", "subscription_smoke_stale", "subscription_smoke_stale"),
        ("subscription_smoke", "subscription_smoke_incomplete", "subscription_smoke_incomplete"),
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


def test_readiness_facts_repr_does_not_expose_provider_secrets():
    facts = valid_input(
        webhook_secret="live-secret-value",
        discount_code="LIVE-PRIVATE-CODE",
        api_key="live-private-api",
        test_webhook_secret="test-secret-value",
        test_discount_code="TEST-PRIVATE-CODE",
        test_api_key="test-private-api",
    )

    rendered = repr(facts)
    for secret in (
        "live-secret-value", "LIVE-PRIVATE-CODE", "live-private-api",
        "test-secret-value", "TEST-PRIVATE-CODE", "test-private-api",
    ):
        assert secret not in rendered


def test_checkout_guard_raises_with_public_codes_only():
    result = assess_payment_readiness(valid_input(source_approved=False))

    with pytest.raises(PaymentReadinessBlocked) as caught:
        require_checkout_ready(result)

    assert caught.value.blockers == ("price_source_not_approved",)
    assert "source_approved" not in str(caught.value)


def test_checkout_guard_accepts_ready_release():
    result = assess_payment_readiness(valid_input())

    assert require_checkout_ready(result) is result


def test_unknown_subscription_smoke_state_fails_closed_without_echoing_it():
    result = assess_payment_readiness(valid_input(subscription_smoke="secret payload"))

    assert result.ready is False
    assert result.blockers == ("subscription_smoke_invalid",)
    assert "secret payload" not in json.dumps(public_readiness(result))


def test_subscription_checkout_configuration_keeps_live_and_test_ids_separate(
    monkeypatch,
):
    values = {
        "LEMON_API_KEY": "live-api",
        "LEMON_STORE_ID": "live-store",
        "LEMON_SUBSCRIPTION_VARIANT_ID": "live-variant",
        "LEMON_FOUNDER_DISCOUNT_ID": "live-discount",
        "LEMON_FOUNDER_DISCOUNT_CODE": "LIVE-FOUNDERS",
        "LEMON_TEST_API_KEY": "test-api",
        "LEMON_TEST_STORE_ID": "test-store",
        "LEMON_TEST_SUBSCRIPTION_VARIANT_ID": "test-variant",
        "LEMON_TEST_FOUNDER_DISCOUNT_ID": "test-discount",
        "LEMON_TEST_FOUNDER_DISCOUNT_CODE": "TEST-FOUNDERS",
    }
    monkeypatch.delenv("PLATBY_ZAPNUTE", raising=False)
    for name, value in values.items():
        monkeypatch.setenv(name, value)

    live = app_config.lemon_subscription_checkout_config(test_mode=False)
    test = app_config.lemon_subscription_checkout_config(test_mode=True)

    assert (
        live.store_id,
        live.variant_id,
        live.founder_discount_id,
        live.founder_discount_code,
    ) == ("live-store", "live-variant", "live-discount", "LIVE-FOUNDERS")
    assert (
        test.store_id,
        test.variant_id,
        test.founder_discount_id,
        test.founder_discount_code,
    ) == ("test-store", "test-variant", "test-discount", "TEST-FOUNDERS")
    assert live.api_key == "live-api"
    assert test.api_key == "test-api"
    assert app_config.os.environ.get("PLATBY_ZAPNUTE", "") == ""
