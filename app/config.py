import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Callable, Literal, cast

try:
    from .operator_profile import LEGAL_VERSION
except ImportError:  # server.py imports config as a top-level module in production
    from operator_profile import LEGAL_VERSION


RecipeEngineMode = Literal["off", "shadow", "on"]
_RECIPE_ENGINE_MODES = frozenset({"off", "shadow", "on"})


@dataclass(frozen=True)
class LemonSubscriptionCheckoutConfig:
    """One mode's server-only identity for annual checkout creation."""

    api_key: str = field(repr=False)
    store_id: str
    variant_id: str
    founder_discount_id: str
    founder_discount_code: str = field(repr=False)
    test_mode: bool


@dataclass(frozen=True)
class LemonCustomerPortalConfig:
    """One mode's server-only identity for existing subscription management."""

    api_key: str = field(repr=False)
    store_id: str
    variant_id: str
    test_mode: bool


def lemon_subscription_checkout_config(
    *,
    test_mode: bool,
    getenv: Callable[[str, str], str | None] | None = None,
) -> LemonSubscriptionCheckoutConfig:
    """Read live or test annual checkout settings without caching secrets."""
    read = getenv or os.environ.get
    prefix = "LEMON_TEST_" if test_mode else "LEMON_"

    def value(name: str) -> str:
        raw = read(f"{prefix}{name}", "")
        return raw.strip() if isinstance(raw, str) else ""

    return LemonSubscriptionCheckoutConfig(
        api_key=value("API_KEY"),
        store_id=value("STORE_ID"),
        variant_id=value("SUBSCRIPTION_VARIANT_ID"),
        founder_discount_id=value("FOUNDER_DISCOUNT_ID"),
        founder_discount_code=value("FOUNDER_DISCOUNT_CODE"),
        test_mode=test_mode,
    )


def lemon_customer_portal_config(
    *,
    test_mode: bool,
    getenv: Callable[[str, str], str | None] | None = None,
) -> LemonCustomerPortalConfig:
    """Read only the matching provider identity needed by Customer Portal."""
    read = getenv or os.environ.get
    prefix = "LEMON_TEST_" if test_mode else "LEMON_"

    def value(name: str) -> str:
        raw = read(f"{prefix}{name}", "")
        return raw.strip() if isinstance(raw, str) else ""

    return LemonCustomerPortalConfig(
        api_key=value("API_KEY"),
        store_id=value("STORE_ID"),
        variant_id=value("SUBSCRIPTION_VARIANT_ID"),
        test_mode=test_mode,
    )


def admin_emails(raw: str) -> frozenset[str]:
    """Normalizovaný allowlist majiteľov; prázdna konfigurácia nič nepovolí."""
    return frozenset(
        email.strip().casefold()
        for email in raw.split(",")
        if email.strip()
    )


def public_base_url() -> str:
    value = os.environ.get("UVARSI_URL", "").strip().rstrip("/")
    if not value:
        raise RuntimeError("Chýba UVARSI_URL.")
    if value != "https://uvar.si":
        raise RuntimeError("UVARSI_URL musí byť presne https://uvar.si.")
    return value


def release_id() -> str:
    path = Path(os.environ.get("UVARSI_VERSION_FILE", "VERSION"))
    return path.read_text(encoding="utf-8").strip()


def legal_version() -> str:
    """Return the reviewed legal revision bundled with this release."""
    return LEGAL_VERSION


@lru_cache(maxsize=1)
def recipe_engine_mode() -> RecipeEngineMode:
    value = os.environ.get("UVARSI_RECIPE_ENGINE", "off")
    if value not in _RECIPE_ENGINE_MODES:
        raise RuntimeError(
            "UVARSI_RECIPE_ENGINE musí byť presne off, shadow alebo on."
        )
    return cast(RecipeEngineMode, value)


def reset_config_cache_for_tests() -> None:
    recipe_engine_mode.cache_clear()
