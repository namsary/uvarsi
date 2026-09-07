"""Verified, code-owned identity of the Uvar.si service operator."""

from __future__ import annotations

import datetime
import re
from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class OperatorProfile:
    business_name: str
    company_id: str
    registered_office: str
    register_court: str
    register_section: str
    register_entry: str
    support_email: str


OPERATOR = OperatorProfile(
    business_name="PUMAR s. r. o.",
    company_id="57370591",
    registered_office="Alexandra Dubčeka 4318/33, 075 01 Trebišov",
    register_court="Mestský súd Košice",
    register_section="Sro",
    register_entry="64515/V",
    support_email="pumaragency@gmail.com",
)

LEGAL_VERSION = "2026-09-07-v1"
LEGAL_EFFECTIVE_DATE = datetime.date(2026, 9, 7)

_EMAIL = re.compile(
    r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@"
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+"
)
_PLACEHOLDERS = ("doplniť", "doplnit", "todo", "[", "]")


def validate_operator_profile(profile: OperatorProfile) -> tuple[str, ...]:
    """Return stable field names whose public company value is unsafe."""
    invalid: list[str] = []
    values = asdict(profile)
    for field, value in values.items():
        if not isinstance(value, str) or not value.strip():
            invalid.append(field)
            continue
        normalized = value.strip().casefold()
        if any(marker in normalized for marker in _PLACEHOLDERS):
            invalid.append(field)

    if not re.fullmatch(r"\d{8}", profile.company_id):
        invalid.append("company_id")
    if not _EMAIL.fullmatch(profile.support_email.strip()):
        invalid.append("support_email")
    return tuple(dict.fromkeys(invalid))


def _formatted_company_id(company_id: str) -> str:
    return f"{company_id[:2]} {company_id[2:5]} {company_id[5:]}"


def public_operator_dict() -> dict[str, str]:
    """Public identity only; absent tax, VAT and phone data stay absent."""
    return {
        "business_name": OPERATOR.business_name,
        "company_id": _formatted_company_id(OPERATOR.company_id),
        "registered_office": OPERATOR.registered_office,
        "register": (
            f"{OPERATOR.register_court}, oddiel {OPERATOR.register_section}, "
            f"vložka č. {OPERATOR.register_entry}"
        ),
        "support_email": OPERATOR.support_email,
        "legal_version": LEGAL_VERSION,
        "legal_effective_date": LEGAL_EFFECTIVE_DATE.isoformat(),
    }
