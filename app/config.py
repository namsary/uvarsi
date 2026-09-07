import os
from functools import lru_cache
from pathlib import Path
from typing import Literal, cast

try:
    from .operator_profile import LEGAL_VERSION
except ImportError:  # server.py imports config as a top-level module in production
    from operator_profile import LEGAL_VERSION


RecipeEngineMode = Literal["off", "shadow", "on"]
_RECIPE_ENGINE_MODES = frozenset({"off", "shadow", "on"})


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
