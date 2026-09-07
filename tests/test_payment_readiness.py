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


def valid_input(**changes):
    value = PaymentReadinessInput(
        operator_errors=(),
        legal_version="2026-09-07-v1",
        release="2026.09.07.28",
        checkout_url="https://uvarsi.lemonsqueezy.com/buy/test",
        webhook_secret="webhook-secret",
        store_id="12345",
        variant_id="67890",
        api_key="api-key",
        source_approved=True,
        private_alerts=True,
        smoke_verified=True,
        worker_alive=True,
        recipe_ready=True,
    )
    return replace(value, **changes)


def test_all_required_gates_make_checkout_ready():
    result = assess_payment_readiness(valid_input())

    assert result.ready is True
    assert result.blockers == ()
    assert result.legal_version == "2026-09-07-v1"
    assert result.release == "2026.09.07.28"


@pytest.mark.parametrize(
    ("change", "value", "blocker"),
    [
        ("operator_errors", ("company_id",), "operator_invalid"),
        ("legal_version", "", "legal_version_missing"),
        ("release", "", "release_missing"),
        ("checkout_url", "", "checkout_not_configured"),
        ("checkout_url", "http://example.test/buy", "checkout_not_configured"),
        ("webhook_secret", "", "webhook_not_configured"),
        ("store_id", "", "store_not_configured"),
        ("variant_id", "", "variant_not_configured"),
        ("api_key", "", "api_not_configured"),
        ("source_approved", False, "price_source_not_approved"),
        ("private_alerts", False, "alerts_not_private"),
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
        "legal_version": "2026-09-07-v1",
        "release": "2026.09.07.28",
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
