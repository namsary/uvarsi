from decimal import Decimal, localcontext
from fractions import Fraction

import pytest

from app.quantity_math import (
    PackageSize,
    Quantity,
    parse_quantity,
    purchase_requirement,
)


def _exact_amount(quantity):
    return Fraction(*quantity.amount.as_integer_ratio())


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


def test_kg_normalization_preserves_high_precision_under_low_ambient_context():
    with localcontext() as context:
        context.prec = 3
        result = parse_quantity("1234567890123456789012345678.9 kg")

    assert result == Quantity(Decimal("1234567890123456789012345678900"), "g")


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


@pytest.mark.parametrize("precision", [1, 3, 28, 50])
def test_infinitesimal_package_overage_uses_exact_ceiling_and_balances(precision):
    required = Quantity(Decimal("1.0000000000000000000000000001"), "g")
    pantry = Quantity(Decimal("0"), "g")
    package = PackageSize(Quantity(Decimal("1"), "g"))

    with localcontext() as context:
        context.prec = precision
        result = purchase_requirement(required, pantry, package)

    assert result.required == required
    assert result.used_from_pantry == pantry
    assert result.missing == required
    assert result.packages == 2
    assert result.to_buy == Quantity(Decimal("2"), "g")
    assert result.used_from_purchase == required
    assert result.leftover == Quantity(
        Decimal("0.9999999999999999999999999999"), "g"
    )
    assert _exact_amount(result.used_from_pantry) + _exact_amount(
        result.missing
    ) == _exact_amount(result.required)
    assert _exact_amount(package.content) * result.packages == _exact_amount(
        result.to_buy
    )
    assert _exact_amount(result.used_from_purchase) + _exact_amount(
        result.leftover
    ) == _exact_amount(result.to_buy)
    assert result.leftover.amount >= 0


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
