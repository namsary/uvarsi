"""Pure fail-closed readiness gate for the Uvar.si paid checkout.

This module deliberately knows no environment-variable names and performs no
network or database work.  Runtime code supplies facts; this module turns them
into stable, safe blocker codes suitable for a public health response.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit


@dataclass(frozen=True)
class PaymentReadinessInput:
    operator_errors: tuple[str, ...]
    legal_version: str
    release: str
    checkout_url: str
    webhook_secret: str
    store_id: str
    variant_id: str
    api_key: str
    source_approved: bool
    private_alerts: bool
    smoke_verified: bool
    worker_alive: bool
    recipe_ready: bool


@dataclass(frozen=True)
class PaymentReadiness:
    ready: bool
    blockers: tuple[str, ...]
    legal_version: str
    release: str


class PaymentReadinessBlocked(RuntimeError):
    """Checkout was requested while one or more launch gates were closed."""

    def __init__(self, blockers: tuple[str, ...]):
        self.blockers = blockers
        super().__init__(", ".join(blockers))


def _present(value: str) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _secure_checkout_url(value: str) -> bool:
    if not _present(value):
        return False
    try:
        parsed = urlsplit(value.strip())
    except ValueError:
        return False
    return parsed.scheme == "https" and bool(parsed.netloc)


def assess_payment_readiness(facts: PaymentReadinessInput) -> PaymentReadiness:
    """Evaluate every launch prerequisite; omission always blocks checkout."""
    blockers: list[str] = []
    if facts.operator_errors:
        blockers.append("operator_invalid")
    if not _present(facts.legal_version):
        blockers.append("legal_version_missing")
    if not _present(facts.release):
        blockers.append("release_missing")
    if not _secure_checkout_url(facts.checkout_url):
        blockers.append("checkout_not_configured")
    if not _present(facts.webhook_secret):
        blockers.append("webhook_not_configured")
    if not _present(facts.store_id):
        blockers.append("merchant_not_configured")
    if not _present(facts.variant_id):
        blockers.append("variant_not_configured")
    if not _present(facts.api_key):
        blockers.append("api_not_configured")
    if facts.source_approved is not True:
        blockers.append("price_source_not_approved")
    if facts.private_alerts is not True:
        blockers.append("alerts_not_private")
    if facts.smoke_verified is not True:
        blockers.append("payment_smoke_missing")
    if facts.worker_alive is not True:
        blockers.append("plan_worker_unhealthy")
    if facts.recipe_ready is not True:
        blockers.append("recipe_gate_failed")

    return PaymentReadiness(
        ready=not blockers,
        blockers=tuple(blockers),
        legal_version=facts.legal_version.strip() if isinstance(facts.legal_version, str) else "",
        release=facts.release.strip() if isinstance(facts.release, str) else "",
    )


def public_readiness(readiness: PaymentReadiness) -> dict:
    """Return only non-secret status fields safe for a public endpoint."""
    return {
        "ready": readiness.ready,
        "blockers": list(readiness.blockers),
        "legal_version": readiness.legal_version,
        "release": readiness.release,
    }


def require_checkout_ready(readiness: PaymentReadiness) -> PaymentReadiness:
    """Return a ready result or fail closed with public blocker codes."""
    if not readiness.ready:
        raise PaymentReadinessBlocked(readiness.blockers)
    return readiness
