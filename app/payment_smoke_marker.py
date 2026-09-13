"""Privacy-safe proof that one release passed the test-payment lifecycle."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit


SCHEMA_VERSION = 3
ACTIVATION_SCHEMA_VERSION = 1
SMOKE_MAX_AGE_SECONDS = 24 * 60 * 60
SMOKE_FUTURE_SKEW_SECONDS = 5 * 60
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")

SUBSCRIPTION_SCHEMA_VERSION = 4
SUBSCRIPTION_ACTIVATION_SCHEMA_VERSION = 2
SUBSCRIPTION_SMOKE_MAX_AGE_SECONDS = 24 * 60 * 60
SUBSCRIPTION_SMOKE_FUTURE_SKEW_SECONDS = 5 * 60
SUBSCRIPTION_ACTIVATION_MAX_AGE_SECONDS = 7 * 24 * 60 * 60


@dataclass(frozen=True)
class SubscriptionConfig:
    """One provider mode's identity; secret values never enter a marker."""

    store_id: str
    variant_id: str
    discount_id: str
    discount_code: str = field(repr=False)
    webhook_secret: str = field(repr=False)
    api_key: str = field(repr=False)
    test_mode: bool


@dataclass(frozen=True)
class SubscriptionMarkerExpectation:
    release: str
    live: SubscriptionConfig
    test: SubscriptionConfig
    signing_secret: str = field(repr=False)


@dataclass(frozen=True)
class AnnualProviderEvidence:
    store_id: str
    variant_id: str
    discount_id: str
    test_mode: bool
    annual_price_cents: int
    currency: str
    billing_interval: str
    billing_interval_count: int
    trial_days: int
    variant_status: str
    discount_kind: str
    discount_amount_cents: int
    discount_duration: str
    discount_status: str
    discount_variant_ids: tuple[str, ...]
    discount_redemption_limit: int
    discount_code_fingerprint: str


@dataclass(frozen=True)
class SubscriptionLifecycleEvidence:
    initial_charge_cents: int
    renewal_displayed_cents: int
    activation_verified: bool
    renewal_invoice_cents: int
    failed_payment_verified: bool
    recovery_verified: bool
    cancellation_verified: bool
    access_retained_until_period_end: bool
    expiration_verified: bool
    refund_verified: bool
    portal_access_verified: bool
    webhook_signature_verified: bool
    reconciliation_verified: bool


def _text(value, *, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"chýba {name}")
    return value.strip()


def _unsigned(marker: dict) -> dict:
    return {key: value for key, value in marker.items() if key != "signature"}


def _canonical(marker: dict) -> bytes:
    return json.dumps(
        _unsigned(marker), sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")


def live_config_fingerprint(
    *, secret: str, checkout_url: str, webhook_secret: str,
    store_id: str, variant_id: str, api_key: str,
) -> str:
    """Bind every live-provider input without exposing its plaintext."""
    key = _text(secret, name="podpisové tajomstvo").encode("utf-8")
    values = {
        "checkout_url": _text(checkout_url, name="živá pokladňa"),
        "webhook_secret": _text(webhook_secret, name="živý webhook"),
        "store_id": _text(store_id, name="živý obchod"),
        "variant_id": _text(variant_id, name="živý variant"),
        "api_key": _text(api_key, name="živý API kľúč"),
    }
    payload = json.dumps(
        values, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    return hmac.new(
        key, b"uvarsi-live-payment-config-v2\0" + payload, hashlib.sha256
    ).hexdigest()


def test_config_fingerprint(
    *, secret: str, checkout_url: str, webhook_secret: str,
    store_id: str, variant_id: str, api_key: str,
) -> str:
    """Bind every test-provider input without exposing its plaintext."""
    key = _text(secret, name="podpisové tajomstvo").encode("utf-8")
    values = {
        "checkout_url": _text(checkout_url, name="testovacia pokladňa"),
        "webhook_secret": _text(webhook_secret, name="testovací webhook"),
        "store_id": _text(store_id, name="testovací obchod"),
        "variant_id": _text(variant_id, name="testovací variant"),
        "api_key": _text(api_key, name="testovací API kľúč"),
    }
    parsed = urlsplit(values["checkout_url"])
    if parsed.scheme != "https" or not parsed.netloc:
        raise ValueError("neplatná testovacia pokladňa")
    payload = json.dumps(
        values, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    return hmac.new(
        key, b"uvarsi-test-payment-config-v1\0" + payload, hashlib.sha256
    ).hexdigest()


def create_marker(
    *, release: str, live_config_digest: str, test_config_digest: str,
    test_store_id: str,
    test_variant_id: str, completed_at: str, receipt_email_verified: bool,
    test_mode_verified: bool,
) -> dict:
    """Build unsigned evidence only after both signed webhooks were observed."""
    if test_mode_verified is not True:
        raise ValueError("testovací režim nebol overený")
    digest = _text(live_config_digest, name="odtlačok živej konfigurácie")
    if not _DIGEST_RE.fullmatch(digest):
        raise ValueError("neplatný odtlačok živej konfigurácie")
    test_digest = _text(
        test_config_digest, name="odtlačok testovacej konfigurácie"
    )
    if not _DIGEST_RE.fullmatch(test_digest):
        raise ValueError("neplatný odtlačok testovacej konfigurácie")
    return {
        "schema_version": SCHEMA_VERSION,
        "release": _text(release, name="vydanie"),
        "live_config_digest": digest,
        "test_config_digest": test_digest,
        "test_store_id": _text(test_store_id, name="testovací obchod"),
        "test_variant_id": _text(test_variant_id, name="testovací variant"),
        "completed_at": _text(completed_at, name="čas dokončenia"),
        "purchase_webhook_verified": True,
        "entitlement_verified": True,
        "receipt_email_verified": receipt_email_verified is True,
        "test_mode_verified": True,
        "refund_webhook_verified": True,
        "entitlement_revoked": True,
        "unresolved_cases": 0,
    }


def sign_marker(marker: dict, *, secret: str) -> dict:
    """Return a signed copy; never place the signing secret in the marker."""
    secret = _text(secret, name="podpisové tajomstvo")
    unsigned = _unsigned(dict(marker))
    signature = hmac.new(
        secret.encode("utf-8"), _canonical(unsigned), hashlib.sha256
    ).hexdigest()
    return {**unsigned, "signature": signature}


def verify_marker(
    marker, *, secret: str, release: str, checkout_url: str,
    webhook_secret: str, store_id: str, variant_id: str, api_key: str,
) -> bool:
    """Fail closed unless signature, release binding and lifecycle all match."""
    if not isinstance(marker, dict) or not isinstance(secret, str) or not secret:
        return False
    signature = marker.get("signature")
    if not isinstance(signature, str) or not _DIGEST_RE.fullmatch(signature):
        return False
    expected = hmac.new(
        secret.encode("utf-8"), _canonical(marker), hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(signature, expected):
        return False
    if marker.get("schema_version") != SCHEMA_VERSION:
        return False
    if marker.get("release") != release:
        return False
    try:
        live_digest = live_config_fingerprint(
            secret=secret,
            checkout_url=checkout_url,
            webhook_secret=webhook_secret,
            store_id=store_id,
            variant_id=variant_id,
            api_key=api_key,
        )
    except ValueError:
        return False
    if not hmac.compare_digest(str(marker.get("live_config_digest", "")), live_digest):
        return False
    if not _DIGEST_RE.fullmatch(str(marker.get("test_config_digest", ""))):
        return False
    if not isinstance(marker.get("completed_at"), str) or not marker["completed_at"]:
        return False
    if not all(
        isinstance(marker.get(key), str) and bool(marker[key].strip())
        for key in ("test_store_id", "test_variant_id")
    ):
        return False
    return (
        marker.get("purchase_webhook_verified") is True
        and marker.get("entitlement_verified") is True
        and marker.get("receipt_email_verified") is True
        and marker.get("test_mode_verified") is True
        and marker.get("refund_webhook_verified") is True
        and marker.get("entitlement_revoked") is True
        and marker.get("unresolved_cases") == 0
    )


def _secret_fingerprint(secret: str, *, label: str, value: str) -> str:
    key = _text(secret, name="podpisové tajomstvo").encode("utf-8")
    raw = _text(value, name=label).encode("utf-8")
    return hmac.new(
        key,
        b"uvarsi-subscription-secret-v1\0" + label.encode("ascii") + b"\0" + raw,
        hashlib.sha256,
    ).hexdigest()


def discount_code_fingerprint(*, signing_secret: str, discount_code: str) -> str:
    """Bind a provider-returned discount code without persisting the code."""
    return _secret_fingerprint(
        signing_secret, label="discount-code", value=discount_code
    )


def _subscription_config_identity(
    config: SubscriptionConfig, *, signing_secret: str
) -> dict:
    if not isinstance(config, SubscriptionConfig) or type(config.test_mode) is not bool:
        raise ValueError("neplatná konfigurácia predplatného")
    return {
        "store_id": _text(config.store_id, name="obchod"),
        "variant_id": _text(config.variant_id, name="ročný variant"),
        "discount_id": _text(config.discount_id, name="zakladajúca zľava"),
        "discount_code_fingerprint": _secret_fingerprint(
            signing_secret, label="discount-code", value=config.discount_code
        ),
        "webhook_secret_fingerprint": _secret_fingerprint(
            signing_secret, label="webhook-secret", value=config.webhook_secret
        ),
        "api_key_fingerprint": _secret_fingerprint(
            signing_secret, label="api-key", value=config.api_key
        ),
        "test_mode": config.test_mode,
    }


def _provider_payload(evidence: AnnualProviderEvidence) -> dict:
    if not isinstance(evidence, AnnualProviderEvidence):
        raise ValueError("chýba dôkaz nastavenia ročného variantu")
    payload = asdict(evidence)
    payload["discount_variant_ids"] = list(evidence.discount_variant_ids)
    return payload


def _lifecycle_payload(evidence: SubscriptionLifecycleEvidence) -> dict:
    if not isinstance(evidence, SubscriptionLifecycleEvidence):
        raise ValueError("chýba dôkaz životného cyklu predplatného")
    return asdict(evidence)


def create_subscription_marker(
    *, expectation: SubscriptionMarkerExpectation,
    live_provider: AnnualProviderEvidence,
    test_provider: AnnualProviderEvidence,
    lifecycle: SubscriptionLifecycleEvidence,
    completed_at: str,
    expires_at: str,
    attestation_id: str | None = None,
) -> dict:
    """Record observed annual-subscription facts without inventing evidence."""
    if not isinstance(expectation, SubscriptionMarkerExpectation):
        raise ValueError("chýba očakávaná konfigurácia predplatného")
    if expectation.live.test_mode is not False or expectation.test.test_mode is not True:
        raise ValueError("živý a testovací režim nie sú oddelené")
    identifier = attestation_id or secrets.token_hex(32)
    if not isinstance(identifier, str) or not _DIGEST_RE.fullmatch(identifier):
        raise ValueError("neplatný identifikátor testu")
    return {
        "schema_version": SUBSCRIPTION_SCHEMA_VERSION,
        "attestation_id": identifier,
        "release": _text(expectation.release, name="vydanie"),
        "live_config": _subscription_config_identity(
            expectation.live, signing_secret=expectation.signing_secret
        ),
        "test_config": _subscription_config_identity(
            expectation.test, signing_secret=expectation.signing_secret
        ),
        "live_provider": _provider_payload(live_provider),
        "test_provider": _provider_payload(test_provider),
        "lifecycle": _lifecycle_payload(lifecycle),
        "completed_at": _text(completed_at, name="čas dokončenia"),
        "expires_at": _text(expires_at, name="koniec platnosti"),
        "unresolved_cases": 0,
    }


def _valid_provider_evidence(
    value, config: SubscriptionConfig, *, signing_secret: str
) -> bool:
    if not isinstance(value, dict):
        return False
    if type(value.get("test_mode")) is not bool:
        return False
    if not all(
        type(value.get(key)) is int
        for key in (
            "annual_price_cents",
            "billing_interval_count",
            "trial_days",
            "discount_amount_cents",
            "discount_redemption_limit",
        )
    ):
        return False
    return value == {
        "store_id": config.store_id,
        "variant_id": config.variant_id,
        "discount_id": config.discount_id,
        "test_mode": config.test_mode,
        "annual_price_cents": 4_900,
        "currency": "EUR",
        "billing_interval": "year",
        "billing_interval_count": 1,
        "trial_days": 0,
        "variant_status": "published",
        "discount_kind": "fixed",
        "discount_amount_cents": 1_000,
        "discount_duration": "once",
        "discount_status": "published",
        "discount_variant_ids": [config.variant_id],
        "discount_redemption_limit": 50,
        "discount_code_fingerprint": discount_code_fingerprint(
            signing_secret=signing_secret,
            discount_code=config.discount_code,
        ),
    }


def _valid_lifecycle_evidence(value) -> bool:
    if not isinstance(value, dict):
        return False
    boolean_fields = (
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
    )
    if not all(type(value.get(key)) is bool for key in boolean_fields):
        return False
    if not all(
        type(value.get(key)) is int
        for key in (
            "initial_charge_cents",
            "renewal_displayed_cents",
            "renewal_invoice_cents",
        )
    ):
        return False
    return value == {
        "initial_charge_cents": 3_900,
        "renewal_displayed_cents": 4_900,
        "activation_verified": True,
        "renewal_invoice_cents": 4_900,
        "failed_payment_verified": True,
        "recovery_verified": True,
        "cancellation_verified": True,
        "access_retained_until_period_end": True,
        "expiration_verified": True,
        "refund_verified": True,
        "portal_access_verified": True,
        "webhook_signature_verified": True,
        "reconciliation_verified": True,
    }


def subscription_marker_status(
    marker,
    expectation: SubscriptionMarkerExpectation,
    *,
    now: datetime | None = None,
) -> str:
    """Return one stable, non-sensitive fail-closed lifecycle status."""
    if marker is None:
        return "subscription_smoke_missing"
    if not isinstance(marker, dict) or not isinstance(
        expectation, SubscriptionMarkerExpectation
    ):
        return "subscription_smoke_invalid"
    signature = marker.get("signature")
    secret = expectation.signing_secret
    if (
        not isinstance(secret, str)
        or not secret
        or not isinstance(signature, str)
        or not _DIGEST_RE.fullmatch(signature)
    ):
        return "subscription_smoke_invalid"
    expected_signature = hmac.new(
        secret.encode("utf-8"), _canonical(marker), hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(signature, expected_signature):
        return "subscription_smoke_invalid"
    required = {
        "schema_version", "attestation_id", "release", "live_config",
        "test_config", "live_provider", "test_provider", "lifecycle",
        "completed_at", "expires_at", "unresolved_cases", "signature",
    }
    if set(marker) != required or marker.get("schema_version") != SUBSCRIPTION_SCHEMA_VERSION:
        return "subscription_smoke_incomplete"
    if not _DIGEST_RE.fullmatch(str(marker.get("attestation_id", ""))):
        return "subscription_smoke_invalid"
    try:
        expected_live = _subscription_config_identity(
            expectation.live, signing_secret=secret
        )
        expected_test = _subscription_config_identity(
            expectation.test, signing_secret=secret
        )
    except ValueError:
        return "subscription_smoke_mismatch"
    if expectation.live.test_mode is not False or expectation.test.test_mode is not True:
        return "subscription_smoke_mismatch"
    if (
        marker.get("release") != expectation.release
        or marker.get("live_config") != expected_live
        or marker.get("test_config") != expected_test
    ):
        return "subscription_smoke_mismatch"
    live_provider = marker.get("live_provider")
    test_provider = marker.get("test_provider")
    expected_live_code = discount_code_fingerprint(
        signing_secret=secret, discount_code=expectation.live.discount_code
    )
    expected_test_code = discount_code_fingerprint(
        signing_secret=secret, discount_code=expectation.test.discount_code
    )
    if (
        isinstance(live_provider, dict)
        and live_provider.get("discount_code_fingerprint") != expected_live_code
    ) or (
        isinstance(test_provider, dict)
        and test_provider.get("discount_code_fingerprint") != expected_test_code
    ):
        return "subscription_smoke_mismatch"
    if not _valid_provider_evidence(
        live_provider, expectation.live, signing_secret=secret
    ):
        return "subscription_smoke_incomplete"
    if not _valid_provider_evidence(
        test_provider, expectation.test, signing_secret=secret
    ):
        return "subscription_smoke_incomplete"
    if not _valid_lifecycle_evidence(marker.get("lifecycle")):
        return "subscription_smoke_incomplete"
    if (
        type(marker.get("unresolved_cases")) is not int
        or marker.get("unresolved_cases") != 0
    ):
        return "subscription_smoke_incomplete"
    try:
        completed = _parse_aware_timestamp(marker.get("completed_at"))
        expires = _parse_aware_timestamp(marker.get("expires_at"))
    except (TypeError, ValueError):
        return "subscription_smoke_invalid"
    validity = (expires - completed).total_seconds()
    checked = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    age = (checked - completed).total_seconds()
    if (
        validity <= 0
        or validity > SUBSCRIPTION_SMOKE_MAX_AGE_SECONDS
        or age < -SUBSCRIPTION_SMOKE_FUTURE_SKEW_SECONDS
        or checked > expires
    ):
        return "subscription_smoke_stale"
    return "verified"


def valid_subscription_marker(
    marker,
    expectation: SubscriptionMarkerExpectation,
    *,
    now: datetime | None = None,
) -> bool:
    return subscription_marker_status(marker, expectation, now=now) == "verified"


def create_subscription_activation_attestation(
    smoke_marker: dict,
    expectation: SubscriptionMarkerExpectation,
) -> dict:
    """Authorize this exact release/config only from a fresh full test proof."""
    activation_time = _trusted_utcnow()
    status = subscription_marker_status(
        smoke_marker, expectation, now=activation_time
    )
    if status != "verified":
        raise ValueError("smoke dôkaz nie je čerstvý a platný pre aktiváciu")
    unsigned = {
        "schema_version": SUBSCRIPTION_ACTIVATION_SCHEMA_VERSION,
        "release": expectation.release,
        "attestation_id": smoke_marker["attestation_id"],
        "live_config": smoke_marker["live_config"],
        "test_config": smoke_marker["test_config"],
        "smoke_evidence_digest": _smoke_evidence_digest(smoke_marker),
        "smoke_completed_at": smoke_marker["completed_at"],
        "smoke_expires_at": smoke_marker["expires_at"],
        "activated_at": activation_time.isoformat(),
        "activation_expires_at": (
            activation_time
            + timedelta(seconds=SUBSCRIPTION_ACTIVATION_MAX_AGE_SECONDS)
        ).isoformat(),
    }
    return sign_marker(unsigned, secret=expectation.signing_secret)


def subscription_activation_status(
    attestation, expectation: SubscriptionMarkerExpectation,
    *,
    now: datetime | None = None,
) -> str:
    """Return a safe status for the durable, release-bound activation proof."""
    if attestation is None:
        return "subscription_smoke_missing"
    if not isinstance(attestation, dict) or not isinstance(
        expectation, SubscriptionMarkerExpectation
    ):
        return "subscription_smoke_invalid"
    signature = attestation.get("signature")
    if not isinstance(signature, str) or not _DIGEST_RE.fullmatch(signature):
        return "subscription_smoke_invalid"
    secret = expectation.signing_secret
    if not isinstance(secret, str) or not secret:
        return "subscription_smoke_invalid"
    expected_signature = hmac.new(
        secret.encode("utf-8"), _canonical(attestation), hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(signature, expected_signature):
        return "subscription_smoke_invalid"
    required = {
        "schema_version", "release", "attestation_id", "live_config",
        "test_config", "smoke_evidence_digest", "smoke_completed_at",
        "smoke_expires_at", "activated_at", "activation_expires_at",
        "signature",
    }
    if set(attestation) != required:
        return "subscription_smoke_incomplete"
    try:
        live = _subscription_config_identity(
            expectation.live, signing_secret=secret
        )
        test = _subscription_config_identity(
            expectation.test, signing_secret=secret
        )
        completed = _parse_aware_timestamp(attestation.get("smoke_completed_at"))
        expires = _parse_aware_timestamp(attestation.get("smoke_expires_at"))
        activated = _parse_aware_timestamp(attestation.get("activated_at"))
        activation_expires = _parse_aware_timestamp(
            attestation.get("activation_expires_at")
        )
    except (TypeError, ValueError):
        return "subscription_smoke_invalid"
    if (
        attestation.get("schema_version") != SUBSCRIPTION_ACTIVATION_SCHEMA_VERSION
        or not _DIGEST_RE.fullmatch(str(attestation.get("attestation_id", "")))
        or not _DIGEST_RE.fullmatch(
            str(attestation.get("smoke_evidence_digest", ""))
        )
    ):
        return "subscription_smoke_incomplete"
    if (
        attestation.get("release") != expectation.release
        or attestation.get("live_config") != live
        or attestation.get("test_config") != test
        or expectation.live.test_mode is not False
        or expectation.test.test_mode is not True
    ):
        return "subscription_smoke_mismatch"
    validity = (expires - completed).total_seconds()
    activation_age = (activated - completed).total_seconds()
    activation_validity = (activation_expires - activated).total_seconds()
    checked = (now or _trusted_utcnow()).astimezone(timezone.utc)
    current_activation_age = (checked - activated).total_seconds()
    if (
        validity <= 0
        or validity > SUBSCRIPTION_SMOKE_MAX_AGE_SECONDS
        or activation_age < -SUBSCRIPTION_SMOKE_FUTURE_SKEW_SECONDS
        or activated > expires
        or activation_validity <= 0
        or activation_validity > SUBSCRIPTION_ACTIVATION_MAX_AGE_SECONDS
        or current_activation_age < -SUBSCRIPTION_SMOKE_FUTURE_SKEW_SECONDS
        or checked > activation_expires
    ):
        return "subscription_smoke_stale"
    return "verified"


def valid_subscription_activation_attestation(
    attestation, expectation: SubscriptionMarkerExpectation,
    *,
    now: datetime | None = None,
) -> bool:
    """Validate a durable activation bound to one release and both modes."""
    return subscription_activation_status(
        attestation, expectation, now=now
    ) == "verified"


def _trusted_utcnow() -> datetime:
    """Return the server clock used for signed activation timestamps."""
    return datetime.now(timezone.utc)


def _parse_aware_timestamp(value) -> datetime:
    if not isinstance(value, str) or not value:
        raise ValueError("chýba čas dôkazu")
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    parsed = datetime.fromisoformat(normalized)
    if parsed.utcoffset() is None:
        raise ValueError("čas dôkazu nemá časové pásmo")
    return parsed.astimezone(timezone.utc)


def _activation_unsigned(attestation: dict) -> dict:
    return {key: value for key, value in attestation.items() if key != "signature"}


def _activation_canonical(attestation: dict) -> bytes:
    return json.dumps(
        _activation_unsigned(attestation),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")


def _smoke_evidence_digest(marker: dict) -> str:
    payload = json.dumps(
        marker, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def create_activation_attestation(
    smoke_marker: dict,
    *,
    secret: str,
    release: str,
    checkout_url: str,
    webhook_secret: str,
    store_id: str,
    variant_id: str,
    api_key: str,
    test_checkout_url: str,
    test_webhook_secret: str,
    test_store_id: str,
    test_variant_id: str,
    test_api_key: str,
    activated_at: str,
) -> dict:
    """Authorize activation only from a fresh, exact, signed smoke result."""
    secret = _text(secret, name="podpisové tajomstvo")
    if not verify_marker(
        smoke_marker,
        secret=secret,
        release=release,
        checkout_url=checkout_url,
        webhook_secret=webhook_secret,
        store_id=store_id,
        variant_id=variant_id,
        api_key=api_key,
    ):
        raise ValueError("neplatný podpísaný smoke dôkaz")
    expected_test_digest = test_config_fingerprint(
        secret=secret,
        checkout_url=test_checkout_url,
        webhook_secret=test_webhook_secret,
        store_id=test_store_id,
        variant_id=test_variant_id,
        api_key=test_api_key,
    )
    recorded_test_digest = str(smoke_marker.get("test_config_digest", ""))
    if not hmac.compare_digest(recorded_test_digest, expected_test_digest):
        raise ValueError("smoke dôkaz patrí inej testovacej konfigurácii")
    smoke_at = _parse_aware_timestamp(smoke_marker.get("completed_at"))
    activation_at = _parse_aware_timestamp(activated_at)
    age = (activation_at - smoke_at).total_seconds()
    if age < -SMOKE_FUTURE_SKEW_SECONDS or age > SMOKE_MAX_AGE_SECONDS:
        raise ValueError("smoke dôkaz nie je čerstvý pre aktiváciu")

    unsigned = {
        "schema_version": ACTIVATION_SCHEMA_VERSION,
        "release": _text(release, name="vydanie"),
        "live_config_digest": str(smoke_marker["live_config_digest"]),
        "test_config_digest": expected_test_digest,
        "smoke_evidence_digest": _smoke_evidence_digest(smoke_marker),
        "smoke_completed_at": smoke_marker["completed_at"],
        "activated_at": _text(activated_at, name="čas aktivácie"),
    }
    signature = hmac.new(
        secret.encode("utf-8"),
        b"uvarsi-payment-activation-v1\0" + _activation_canonical(unsigned),
        hashlib.sha256,
    ).hexdigest()
    return {**unsigned, "signature": signature}


def verify_activation_attestation(
    attestation,
    *,
    secret: str,
    release: str,
    checkout_url: str,
    webhook_secret: str,
    store_id: str,
    variant_id: str,
    api_key: str,
    test_checkout_url: str,
    test_webhook_secret: str,
    test_store_id: str,
    test_variant_id: str,
    test_api_key: str,
) -> bool:
    """Verify activation identity without expiring it merely as time passes."""
    if not isinstance(attestation, dict) or not isinstance(secret, str) or not secret:
        return False
    signature = attestation.get("signature")
    if not isinstance(signature, str) or not _DIGEST_RE.fullmatch(signature):
        return False
    expected_signature = hmac.new(
        secret.encode("utf-8"),
        b"uvarsi-payment-activation-v1\0" + _activation_canonical(attestation),
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(signature, expected_signature):
        return False
    if (
        attestation.get("schema_version") != ACTIVATION_SCHEMA_VERSION
        or attestation.get("release") != release
        or not _DIGEST_RE.fullmatch(
            str(attestation.get("smoke_evidence_digest", ""))
        )
    ):
        return False
    try:
        expected_live_digest = live_config_fingerprint(
            secret=secret,
            checkout_url=checkout_url,
            webhook_secret=webhook_secret,
            store_id=store_id,
            variant_id=variant_id,
            api_key=api_key,
        )
        expected_test_digest = test_config_fingerprint(
            secret=secret,
            checkout_url=test_checkout_url,
            webhook_secret=test_webhook_secret,
            store_id=test_store_id,
            variant_id=test_variant_id,
            api_key=test_api_key,
        )
        smoke_at = _parse_aware_timestamp(attestation.get("smoke_completed_at"))
        activation_at = _parse_aware_timestamp(attestation.get("activated_at"))
    except (TypeError, ValueError):
        return False
    if not hmac.compare_digest(
        str(attestation.get("live_config_digest", "")), expected_live_digest
    ):
        return False
    if not hmac.compare_digest(
        str(attestation.get("test_config_digest", "")), expected_test_digest
    ):
        return False
    activation_age = (activation_at - smoke_at).total_seconds()
    return (
        activation_age >= -SMOKE_FUTURE_SKEW_SECONDS
        and activation_age <= SMOKE_MAX_AGE_SECONDS
    )
