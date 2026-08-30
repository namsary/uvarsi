from decimal import Decimal

import pytest

from app.quantity_math import (
    PackageSize,
    Quantity,
    parse_quantity,
    purchase_requirement,
)


@pytest.mark.parametrize(
    ("text", "amount", "unit"),
    [
        ("1 kg", Decimal("1000"), "g"),
        ("1.25 l", Decimal("1250.00"), "ml"),
        ("2 piece", Decimal("2"), "piece"),
    ],
)
def test_parse_quantity_normalizes_to_base_units(text, amount, unit):
    assert parse_quantity(text) == Quantity(amount, unit)


@pytest.mark.parametrize(
    "text",
    [
        "",
        "1kg",
        "1e3 g",
        "1,5 kg",
        "1 kg extra",
        "NaN g",
        "Infinity g",
        "-1 g",
        "1 oz",
        "kg 1",
    ],
)
def test_parse_quantity_rejects_ambiguous_or_unsupported_syntax(text):
    with pytest.raises(ValueError):
        parse_quantity(text)


def test_quantity_requires_decimal_amounts():
    with pytest.raises(TypeError):
        Quantity(1, "g")


@pytest.mark.parametrize(
    "amount",
    [Decimal("-0.01"), Decimal("NaN"), Decimal("Infinity")],
)
def test_quantity_rejects_negative_and_non_finite_amounts(amount):
    with pytest.raises(ValueError):
        Quantity(amount, "g")


def test_quantity_rejects_non_canonical_units():
    with pytest.raises(ValueError):
        Quantity(Decimal("1"), "kg")


def test_package_size_rejects_zero_content():
    with pytest.raises(ValueError):
        PackageSize(Quantity(Decimal("0"), "g"))


@pytest.mark.parametrize(
    ("required", "pantry", "package"),
    [
        ("100 g", "25 ml", "500 g"),
        ("100 g", "25 g", "500 ml"),
    ],
)
def test_purchase_requirement_rejects_incompatible_dimensions(
    required, pantry, package
):
    q = parse_quantity
    with pytest.raises(ValueError):
        purchase_requirement(q(required), q(pantry), PackageSize(q(package)))


def test_rounds_only_package_count_up_for_multiple_packages():
    q = parse_quantity
    result = purchase_requirement(
        q("1000.25 g"), q("0.10 g"), PackageSize(q("400 g"))
    )
    assert result.missing == q("1000.15 g")
    assert result.packages == 3
    assert result.to_buy == q("1200 g")
    assert result.used_from_purchase == q("1000.15 g")
    assert result.leftover == q("199.85 g")


def test_buys_whole_rice_package_and_keeps_remainder():
    q = parse_quantity
    result = purchase_requirement(q("300 g"), q("0 g"), PackageSize(q("1 kg")))
    assert result.required == q("300 g")
    assert result.used_from_pantry == q("0 g")
    assert result.missing == q("300 g")
    assert result.packages == 1
    assert result.to_buy == q("1 kg")
    assert result.used_from_purchase == q("300 g")
    assert result.leftover == q("700 g")


def test_partial_pantry_reduces_missing_amount_before_rounding_packages():
    q = parse_quantity
    result = purchase_requirement(q("700 g"), q("450 g"), PackageSize(q("500 g")))
    assert result.required == q("700 g")
    assert result.used_from_pantry == q("450 g")
    assert result.missing == q("250 g")
    assert result.packages == 1
    assert result.to_buy == q("500 g")
    assert result.used_from_purchase == q("250 g")
    assert result.leftover == q("250 g")
