"""Truthful annual-commercial plus Test-mode daily-probe marker semantics."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import json

from app import payment_smoke_marker as marker
from app import subscription_lifecycle_probe as probe


NOW = datetime(2026, 9, 13, 10, 0, tzinfo=timezone.utc)


def expectation():
    return marker.SubscriptionMarkerExpectation(
        release="2026.09.13.2",
        live=marker.SubscriptionConfig(
            "live-store", "live-annual", "live-founder", "LIVE-CODE",
            "live-hook", "live-api", False,
        ),
        test=marker.SubscriptionConfig(
            "test-store", "test-annual", "test-founder", "TEST-CODE",
            "test-hook", "test-api", True,
        ),
        probe=probe.LifecycleProbeConfig(
            api_key="test-api",
            store_id="test-store",
            variant_id="test-daily-probe",
            webhook_secret="probe-hook",
            price_cents=100,
        ),
        signing_secret="marker-secret",
    )


def annual_provider(*, test_mode):
    config = expectation().test if test_mode else expectation().live
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
            signing_secret=expectation().signing_secret,
            discount_code=config.discount_code,
        ),
    )


def annual_commercial():
    return marker.AnnualCommercialEvidence(
        founder_initial_cents=3_900,
        standard_and_renewal_cents=4_900,
        currency="EUR",
        billing_interval="year",
        billing_interval_count=1,
        annual_test_checkout_verified=True,
        annual_test_initial_payment_verified=True,
        annual_renewal_terms_verified=True,
        annual_domain_cancellation_verified=True,
        annual_domain_refund_verified=True,
        portal_access_verified=True,
        reconciliation_verified=True,
    )


def probe_provider():
    return marker.DailyProbeProviderEvidence(
        test_mode=True,
        price_cents=100,
        currency="EUR",
        billing_interval="day",
        billing_interval_count=1,
        trial_days=0,
        discount_applied_cents=0,
        variant_status="published",
    )


def probe_lifecycle():
    return marker.DailyProbeLifecycleEvidence(
        evidence_source="test_mode_daily_probe",
        initial_payment_webhook_verified=True,
        genuine_daily_renewal_verified=True,
        failed_payment_webhook_verified=True,
        recovered_payment_webhook_verified=True,
        cancellation_webhook_verified=True,
        resumed_webhook_verified=True,
        expiration_webhook_verified=True,
        refund_webhook_verified=True,
        webhook_signature_verified=True,
        identity_isolation_verified=True,
        event_order_verified=True,
    )


def signed_marker(**changes):
    values = {
        "expectation": expectation(),
        "live_provider": annual_provider(test_mode=False),
        "test_provider": annual_provider(test_mode=True),
        "annual_commercial": annual_commercial(),
        "probe_provider": probe_provider(),
        "probe_lifecycle": probe_lifecycle(),
        "completed_at": "2026-09-13T09:30:00+00:00",
        "expires_at": "2026-09-14T09:30:00+00:00",
        "attestation_id": "a" * 64,
    }
    values.update(changes)
    unsigned = marker.create_subscription_marker(**values)
    return marker.sign_marker(unsigned, secret=expectation().signing_secret)


def test_marker_keeps_annual_economics_separate_from_daily_lifecycle():
    proof = signed_marker()

    assert marker.subscription_marker_status(proof, expectation(), now=NOW) == "verified"
    assert proof["annual_commercial"]["standard_and_renewal_cents"] == 4_900
    assert proof["test_mode_daily_probe"]["provider"]["price_cents"] == 100
    assert proof["test_mode_daily_probe"]["provider"]["billing_interval"] == "day"
    assert proof["test_mode_daily_probe"]["lifecycle"]["evidence_source"] == (
        "test_mode_daily_probe"
    )
    assert "annual_renewal_invoice_cents" not in json.dumps(proof)


def test_marker_and_activation_bind_probe_configuration_drift(monkeypatch):
    proof = signed_marker()
    changed_configs = (
        replace(expectation().probe, price_cents=101),
        replace(expectation().probe, variant_id="another-daily-probe"),
        replace(expectation().probe, webhook_secret="rotated-probe-secret"),
    )
    for changed_probe in changed_configs:
        changed = replace(expectation(), probe=changed_probe)
        assert marker.subscription_marker_status(proof, changed, now=NOW) == (
            "subscription_smoke_mismatch"
        )

    monkeypatch.setattr(marker, "_trusted_utcnow", lambda: NOW)
    activation = marker.create_subscription_activation_attestation(
        proof, expectation()
    )
    changed = replace(expectation(), probe=changed_configs[0])
    assert marker.subscription_activation_status(
        activation, changed, now=NOW
    ) == "subscription_smoke_mismatch"


def test_marker_contains_no_provider_ids_secrets_or_pii():
    rendered = json.dumps(signed_marker(), sort_keys=True)

    for forbidden in (
        "live-store", "live-annual", "live-founder", "LIVE-CODE",
        "live-hook", "live-api", "test-store", "test-annual",
        "test-founder", "TEST-CODE", "test-hook", "test-api",
        "test-daily-probe", "probe-hook", "@",
    ):
        assert forbidden not in rendered


def test_old_subscription_marker_schema_is_rejected_fail_closed():
    proof = signed_marker()
    proof["schema_version"] -= 1
    proof = marker.sign_marker(proof, secret=expectation().signing_secret)

    assert marker.subscription_marker_status(
        proof, expectation(), now=NOW
    ) == "subscription_smoke_incomplete"


def test_daily_probe_cannot_be_renamed_as_annual_evidence():
    invalid = replace(
        probe_lifecycle(), evidence_source="annual_subscription_renewal"
    )

    proof = signed_marker(probe_lifecycle=invalid)

    assert marker.subscription_marker_status(
        proof, expectation(), now=NOW
    ) == "subscription_smoke_incomplete"


def test_probe_provider_evidence_requires_exact_planned_price_and_no_discount():
    for invalid in (
        replace(probe_provider(), price_cents=99),
        replace(probe_provider(), billing_interval="year"),
        replace(probe_provider(), discount_applied_cents=10),
    ):
        proof = signed_marker(probe_provider=invalid)
        assert marker.subscription_marker_status(
            proof, expectation(), now=NOW
        ) == "subscription_smoke_incomplete"


def test_marker_rejects_probe_identity_collisions_fail_closed():
    for invalid_probe in (
        replace(expectation().probe, variant_id=expectation().test.variant_id),
        replace(expectation().probe, webhook_secret=expectation().live.webhook_secret),
        replace(expectation().probe, webhook_secret=expectation().test.webhook_secret),
        replace(expectation().probe, store_id="another-test-store"),
        replace(expectation().probe, api_key="another-test-api"),
    ):
        changed = replace(expectation(), probe=invalid_probe)
        try:
            proof = signed_marker(expectation=changed)
        except ValueError:
            continue
        assert marker.subscription_marker_status(proof, changed, now=NOW) != "verified"
