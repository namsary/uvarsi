"""Privacy-safe proof that one release passed the test-payment lifecycle."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from datetime import datetime, timezone
from urllib.parse import urlsplit


SCHEMA_VERSION = 3
ACTIVATION_SCHEMA_VERSION = 1
SMOKE_MAX_AGE_SECONDS = 24 * 60 * 60
SMOKE_FUTURE_SKEW_SECONDS = 5 * 60
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")


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
    *, secret: str, checkout_url: str, store_id: str, variant_id: str
) -> str:
    """Bind a smoke to the exact planned live checkout without exposing its URL."""
    key = _text(secret, name="podpisové tajomstvo").encode("utf-8")
    values = {
        "checkout_url": _text(checkout_url, name="živá pokladňa"),
        "store_id": _text(store_id, name="živý obchod"),
        "variant_id": _text(variant_id, name="živý variant"),
    }
    payload = json.dumps(
        values, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    return hmac.new(key, b"uvarsi-live-payment-config-v1\0" + payload, hashlib.sha256).hexdigest()


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
    store_id: str, variant_id: str
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
            store_id=store_id,
            variant_id=variant_id,
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
    store_id: str,
    variant_id: str,
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
        store_id=store_id,
        variant_id=variant_id,
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
    store_id: str,
    variant_id: str,
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
            store_id=store_id,
            variant_id=variant_id,
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
