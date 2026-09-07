from dataclasses import FrozenInstanceError, replace

import pytest

from app.operator_profile import (
    LEGAL_EFFECTIVE_DATE,
    LEGAL_VERSION,
    OPERATOR,
    public_operator_dict,
    validate_operator_profile,
)


def test_operator_profile_has_verified_company_identity():
    assert OPERATOR.business_name == "PUMAR s. r. o."
    assert OPERATOR.company_id == "57370591"
    assert OPERATOR.register_court == "Mestský súd Košice"
    assert OPERATOR.register_section == "Sro"
    assert OPERATOR.register_entry == "64515/V"
    assert validate_operator_profile(OPERATOR) == ()


def test_legal_version_is_explicit_and_immutable():
    assert LEGAL_VERSION == "2026-09-07-v1"
    assert LEGAL_EFFECTIVE_DATE.isoformat() == "2026-09-07"
    with pytest.raises(FrozenInstanceError):
        OPERATOR.business_name = "Iná firma"


def test_public_profile_formats_ico_without_inventing_tax_or_phone_data():
    data = public_operator_dict()

    assert data["company_id"] == "57 370 591"
    assert data["registered_office"] == (
        "Alexandra Dubčeka 4318/33, 075 01 Trebišov"
    )
    assert data["register"] == (
        "Mestský súd Košice, oddiel Sro, vložka č. 64515/V"
    )
    assert "phone" not in data
    assert "tax_id" not in data
    assert "vat_id" not in data


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("business_name", ""),
        ("registered_office", "  "),
        ("company_id", "57 370 59"),
        ("company_id", "SK57370591"),
        ("support_email", "nie-je-email"),
        ("register_entry", "[DOPLNIŤ]"),
        ("register_court", "TODO"),
    ],
)
def test_validation_rejects_incomplete_or_malformed_identity(field, value):
    invalid = replace(OPERATOR, **{field: value})

    assert field in validate_operator_profile(invalid)
