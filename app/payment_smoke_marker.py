"""Privacy-safe proof that one release passed the test-payment lifecycle."""

from __future__ import annotations

import hashlib
import hmac
import json
import re


SCHEMA_VERSION = 3
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


def create_marker(
    *, release: str, live_config_digest: str, test_store_id: str,
    test_variant_id: str, completed_at: str, receipt_email_verified: bool,
    test_mode_verified: bool,
) -> dict:
    """Build unsigned evidence only after both signed webhooks were observed."""
    if test_mode_verified is not True:
        raise ValueError("testovací režim nebol overený")
    digest = _text(live_config_digest, name="odtlačok živej konfigurácie")
    if not _DIGEST_RE.fullmatch(digest):
        raise ValueError("neplatný odtlačok živej konfigurácie")
    return {
        "schema_version": SCHEMA_VERSION,
        "release": _text(release, name="vydanie"),
        "live_config_digest": digest,
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
