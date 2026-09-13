"""Fail-closed evidence for the complete annual subscription lifecycle."""

from dataclasses import replace
from datetime import datetime, timezone
import json

import pytest

from app import payment_smoke_marker as marker


NOW = datetime(2026, 9, 13, 10, 0, tzinfo=timezone.utc)


def expected():
    return marker.SubscriptionMarkerExpectation(
        release="2026.09.13.1",
        live=marker.SubscriptionConfig(
            store_id="live-store",
            variant_id="live-annual",
            discount_id="live-founder",
            discount_code="LIVE-FOUNDERS",
            webhook_secret="live-webhook-secret",
            api_key="live-api-key",
            test_mode=False,
        ),
        test=marker.SubscriptionConfig(
            store_id="test-store",
            variant_id="test-annual",
            discount_id="test-founder",
            discount_code="TEST-FOUNDERS",
            webhook_secret="test-webhook-secret",
            api_key="test-api-key",
            test_mode=True,
        ),
        signing_secret="local-marker-secret",
    )


def provider_evidence(*, test_mode: bool):
    config = expected().test if test_mode else expected().live
    return marker.AnnualProviderEvidence(
        store_id=config.store_id,
        variant_id=config.variant_id,
        discount_id=config.discount_id,
        test_mode=test_mode,
        annual_price_cents=4_900,
        currency="EUR",
        billing_interval="year",
        billing_interval_count=1,
        trial_days=0,
        variant_status="published",
        discount_kind="fixed",
        discount_amount_cents=1_000,
        discount_duration="once",
        discount_status="published",
        discount_variant_ids=(config.variant_id,),
        discount_redemption_limit=50,
        discount_code_fingerprint=marker.discount_code_fingerprint(
            signing_secret=expected().signing_secret,
            discount_code=config.discount_code,
        ),
    )


def lifecycle():
    return marker.SubscriptionLifecycleEvidence(
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


def signed_marker(**changes):
    values = {
        "expectation": expected(),
        "live_provider": provider_evidence(test_mode=False),
        "test_provider": provider_evidence(test_mode=True),
        "lifecycle": lifecycle(),
        "completed_at": "2026-09-13T09:30:00+00:00",
        "expires_at": "2026-09-14T09:30:00+00:00",
        "attestation_id": "a" * 64,
    }
    values.update(changes)
    unsigned = marker.create_subscription_marker(**values)
    return marker.sign_marker(unsigned, secret=expected().signing_secret)


def test_complete_current_subscription_marker_is_valid():
    proof = signed_marker()

    assert marker.valid_subscription_marker(proof, expected(), now=NOW) is True
    assert marker.subscription_marker_status(proof, expected(), now=NOW) == "verified"


@pytest.mark.parametrize(
    "field",
    [
        "activation_verified",
        "failed_payment_verified",
        "recovery_verified",
        "cancellation_verified",
        "access_retained_until_period_end",
        "expiration_verified",
        "refund_verified",
        "portal_access_verified",
        "webhook_signature_verified",
        "reconciliation_verified",
    ],
)
def test_smoke_requires_every_lifecycle_transition(field):
    incomplete = replace(lifecycle(), **{field: False})
    proof = signed_marker(lifecycle=incomplete)

    assert marker.valid_subscription_marker(proof, expected(), now=NOW) is False
    assert marker.subscription_marker_status(proof, expected(), now=NOW) == (
        "subscription_smoke_incomplete"
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("annual_price_cents", 3_900),
        ("currency", "USD"),
        ("billing_interval", "month"),
        ("billing_interval_count", 12),
        ("trial_days", 14),
        ("variant_status", "draft"),
        ("discount_kind", "percentage"),
        ("discount_amount_cents", 999),
        ("discount_duration", "repeating"),
        ("discount_status", "draft"),
        ("discount_variant_ids", ("other-variant",)),
        ("discount_redemption_limit", 51),
    ],
)
def test_provider_evidence_must_match_the_approved_annual_offer(field, value):
    invalid = replace(provider_evidence(test_mode=True), **{field: value})
    proof = signed_marker(test_provider=invalid)

    assert marker.valid_subscription_marker(proof, expected(), now=NOW) is False


def test_lifecycle_prices_are_exact_and_not_merely_boolean_claims():
    assert marker.valid_subscription_marker(
        signed_marker(lifecycle=replace(lifecycle(), initial_charge_cents=4_900)),
        expected(),
        now=NOW,
    ) is False
    assert marker.valid_subscription_marker(
        signed_marker(lifecycle=replace(lifecycle(), renewal_invoice_cents=3_900)),
        expected(),
        now=NOW,
    ) is False


def test_marker_is_bound_to_release_mode_and_every_provider_identity():
    proof = signed_marker()
    changed = replace(expected(), release="2026.09.13.2")
    assert marker.subscription_marker_status(proof, changed, now=NOW) == (
        "subscription_smoke_mismatch"
    )

    for field in (
        "store_id", "variant_id", "discount_id", "discount_code",
        "webhook_secret", "api_key",
    ):
        changed_test = replace(expected().test, **{field: f"changed-{field}"})
        changed = replace(expected(), test=changed_test)
        assert marker.valid_subscription_marker(proof, changed, now=NOW) is False

    wrong_mode = replace(expected(), test=replace(expected().test, test_mode=False))
    assert marker.valid_subscription_marker(proof, wrong_mode, now=NOW) is False


def test_provider_discount_code_is_bound_by_hmac_without_entering_marker():
    proof = signed_marker()

    assert proof["test_provider"]["discount_code_fingerprint"] == (
        marker.discount_code_fingerprint(
            signing_secret=expected().signing_secret,
            discount_code=expected().test.discount_code,
        )
    )
    assert expected().test.discount_code not in json.dumps(proof)

    wrong_provider_code = json.loads(json.dumps(proof))
    wrong_provider_code["test_provider"]["discount_code_fingerprint"] = (
        marker.discount_code_fingerprint(
            signing_secret=expected().signing_secret,
            discount_code="WRONG-PROVIDER-CODE",
        )
    )
    wrong_provider_code = marker.sign_marker(
        wrong_provider_code, secret=expected().signing_secret
    )
    assert marker.subscription_marker_status(
        wrong_provider_code, expected(), now=NOW
    ) == "subscription_smoke_mismatch"


def test_marker_has_explicit_freshness_and_rejects_old_future_or_partial_proof():
    assert marker.subscription_marker_status(None, expected(), now=NOW) == (
        "subscription_smoke_missing"
    )
    assert marker.subscription_marker_status(
        signed_marker(expires_at="2026-09-13T09:59:59+00:00"), expected(), now=NOW
    ) == "subscription_smoke_stale"
    assert marker.subscription_marker_status(
        signed_marker(completed_at="2026-09-13T10:05:01+00:00"), expected(), now=NOW
    ) == "subscription_smoke_stale"

    partial = signed_marker()
    partial.pop("lifecycle")
    partial = marker.sign_marker(partial, secret=expected().signing_secret)
    assert marker.subscription_marker_status(partial, expected(), now=NOW) == (
        "subscription_smoke_incomplete"
    )


def test_marker_never_contains_secrets_pii_or_signed_provider_urls():
    encoded = json.dumps(signed_marker(), ensure_ascii=False)

    for forbidden in (
        "LIVE-FOUNDERS", "TEST-FOUNDERS", "live-webhook-secret",
        "test-webhook-secret", "live-api-key", "test-api-key",
        "martin@example.test", "checkout.lemonsqueezy.com", "customer_portal",
    ):
        assert forbidden not in encoded


def test_activation_attestation_requires_a_fresh_full_subscription_marker(
        monkeypatch):
    proof = signed_marker()
    monkeypatch.setattr(marker, "_trusted_utcnow", lambda: NOW)
    activation = marker.create_subscription_activation_attestation(
        proof, expected()
    )

    assert marker.valid_subscription_activation_attestation(
        activation, expected(), now=NOW
    ) is True
    assert marker.valid_subscription_activation_attestation(
        activation, replace(expected(), release="another-release")
    ) is False
    assert marker.subscription_activation_status(
        activation, replace(expected(), release="another-release")
    ) == "subscription_smoke_mismatch"
    with pytest.raises(ValueError, match="čerstv|platn"):
        marker.create_subscription_activation_attestation(
            signed_marker(expires_at="2026-09-13T09:59:59+00:00"),
            expected(),
        )


def test_activation_attestation_reports_missing_corrupt_and_partial_evidence(
        monkeypatch):
    assert marker.subscription_activation_status(None, expected()) == (
        "subscription_smoke_missing"
    )
    assert marker.subscription_activation_status({}, expected()) == (
        "subscription_smoke_invalid"
    )

    monkeypatch.setattr(marker, "_trusted_utcnow", lambda: NOW)
    activation = marker.create_subscription_activation_attestation(
        signed_marker(), expected()
    )
    activation.pop("smoke_evidence_digest")
    partial = marker.sign_marker(
        activation, secret=expected().signing_secret
    )
    assert marker.subscription_activation_status(partial, expected()) == (
        "subscription_smoke_incomplete"
    )


def test_activation_uses_trusted_server_clock_and_rejects_caller_timestamp(
        monkeypatch):
    monkeypatch.setattr(marker, "_trusted_utcnow", lambda: NOW, raising=False)
    try:
        activation = marker.create_subscription_activation_attestation(
            signed_marker(), expected()
        )
    except TypeError as error:
        pytest.fail(f"aktivácia stále vyžaduje čas od volajúceho: {error}")

    assert activation["activated_at"] == "2026-09-13T10:00:00+00:00"
    assert activation["activation_expires_at"] == "2026-09-20T10:00:00+00:00"
    assert marker.subscription_activation_status(
        activation, expected(), now=NOW
    ) == "verified"
    with pytest.raises(TypeError):
        marker.create_subscription_activation_attestation(
            signed_marker(), expected(),
            activated_at="2020-01-01T00:00:00+00:00",
        )


@pytest.mark.parametrize(
    ("activated_at", "expires_at"),
    [
        ("2020-01-01T00:00:00+00:00", "2020-01-08T00:00:00+00:00"),
        ("2026-09-13T10:05:01+00:00", "2026-09-20T10:05:01+00:00"),
        ("2026-09-06T09:59:59+00:00", "2026-09-13T09:59:59+00:00"),
    ],
)
def test_activation_rejects_old_future_and_expired_signed_times(
        monkeypatch, activated_at, expires_at):
    monkeypatch.setattr(marker, "_trusted_utcnow", lambda: NOW, raising=False)
    activation = marker.create_subscription_activation_attestation(
        signed_marker(), expected()
    )
    activation["activated_at"] = activated_at
    activation["activation_expires_at"] = expires_at
    activation = marker.sign_marker(
        activation, secret=expected().signing_secret
    )

    assert marker.subscription_activation_status(
        activation, expected(), now=NOW
    ) == "subscription_smoke_stale"


def test_boolean_lifecycle_claims_and_mode_are_not_truthy_shortcuts():
    proof = signed_marker(
        lifecycle=replace(lifecycle(), portal_access_verified=1)
    )
    assert marker.valid_subscription_marker(proof, expected(), now=NOW) is False

    wrong_mode = replace(provider_evidence(test_mode=True), test_mode=1)
    proof = signed_marker(test_provider=wrong_mode)
    assert marker.valid_subscription_marker(proof, expected(), now=NOW) is False


@pytest.mark.parametrize("unresolved", [False, 0.0, "0", None])
def test_unresolved_cases_requires_exact_integer_zero(unresolved):
    proof = signed_marker()
    proof["unresolved_cases"] = unresolved
    proof = marker.sign_marker(proof, secret=expected().signing_secret)

    assert marker.subscription_marker_status(
        proof, expected(), now=NOW
    ) == "subscription_smoke_incomplete"
