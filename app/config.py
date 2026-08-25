import os
from pathlib import Path


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
