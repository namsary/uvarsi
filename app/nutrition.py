"""Decimal-only nutrition estimates for deterministic recipes."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Context, Decimal, ROUND_HALF_EVEN
from fractions import Fraction
from typing import Sequence

if __package__:
    from .ingredient_catalog import Ingredient
else:
    from ingredient_catalog import Ingredient


@dataclass(frozen=True)
class MacroValues:
    kcal: Decimal
    protein_g: Decimal
    fat_g: Decimal
    carbs_g: Decimal


@dataclass(frozen=True)
class NutritionEstimate:
    total: MacroValues
    serving: MacroValues
    estimated: bool = True


def _coefficient_and_exponent(value: Decimal) -> tuple[int, int]:
    parts = value.as_tuple()
    coefficient = 0
    for digit in parts.digits:
        coefficient = coefficient * 10 + digit
    if parts.sign:
        coefficient = -coefficient
    return coefficient, parts.exponent


def _from_coefficient(coefficient: int, exponent: int) -> Decimal:
    parts = Decimal(coefficient).as_tuple()
    return Decimal((parts.sign, parts.digits, exponent))


def _shift_exponent(value: Decimal, places: int) -> Decimal:
    coefficient, exponent = _coefficient_and_exponent(value)
    return _from_coefficient(coefficient, exponent + places)


def _multiply_exact(left: Decimal, right: Decimal) -> Decimal:
    left_coefficient, left_exponent = _coefficient_and_exponent(left)
    right_coefficient, right_exponent = _coefficient_and_exponent(right)
    return _from_coefficient(
        left_coefficient * right_coefficient,
        left_exponent + right_exponent,
    )


def _add_exact(left: Decimal, right: Decimal) -> Decimal:
    left_coefficient, left_exponent = _coefficient_and_exponent(left)
    right_coefficient, right_exponent = _coefficient_and_exponent(right)
    exponent = min(left_exponent, right_exponent)
    left_coefficient *= 10 ** (left_exponent - exponent)
    right_coefficient *= 10 ** (right_exponent - exponent)
    return _from_coefficient(left_coefficient + right_coefficient, exponent)


def _fraction_to_decimal(value: Fraction) -> Decimal:
    denominator = value.denominator
    twos = 0
    fives = 0
    while denominator % 2 == 0:
        denominator //= 2
        twos += 1
    while denominator % 5 == 0:
        denominator //= 5
        fives += 1
    if denominator == 1:
        scale = max(twos, fives)
        coefficient = value.numerator
        coefficient *= 2 ** (scale - twos)
        coefficient *= 5 ** (scale - fives)
        return _from_coefficient(coefficient, -scale)

    integer_digits = max(
        1,
        len(str(abs(value.numerator))) - len(str(value.denominator)) + 1,
    )
    context = Context(prec=integer_digits + 64, rounding=ROUND_HALF_EVEN)
    return context.divide(Decimal(value.numerator), Decimal(value.denominator))


def _divide_deterministic(dividend: Decimal, divisor: Decimal) -> Decimal:
    return _fraction_to_decimal(Fraction(dividend) / Fraction(divisor))


def estimate_recipe_nutrition(
    lines: Sequence[tuple[Ingredient, Decimal]],
    adult_servings: Decimal,
) -> NutritionEstimate:
    """Estimate total and per-adult nutrition from edible gram quantities."""
    if not adult_servings.is_finite() or adult_servings <= Decimal("0"):
        raise ValueError("adult servings must be a positive finite value")

    totals = [Decimal("0"), Decimal("0"), Decimal("0"), Decimal("0")]

    for ingredient, edible_grams in lines:
        if not edible_grams.is_finite() or edible_grams < Decimal("0"):
            raise ValueError("edible grams must be a non-negative finite value")
        scale = _shift_exponent(edible_grams, -2)
        nutrition = ingredient.nutrition
        for index, value in enumerate(
            (
                nutrition.kcal,
                nutrition.protein_g,
                nutrition.fat_g,
                nutrition.carbs_g,
            )
        ):
            totals[index] = _add_exact(totals[index], _multiply_exact(value, scale))

    total = MacroValues(
        kcal=totals[0],
        protein_g=totals[1],
        fat_g=totals[2],
        carbs_g=totals[3],
    )
    serving = MacroValues(
        kcal=_divide_deterministic(total.kcal, adult_servings),
        protein_g=_divide_deterministic(total.protein_g, adult_servings),
        fat_g=_divide_deterministic(total.fat_g, adult_servings),
        carbs_g=_divide_deterministic(total.carbs_g, adult_servings),
    )
    return NutritionEstimate(total=total, serving=serving)


def qualifies_high_protein(value: NutritionEstimate) -> bool:
    macros = (
        value.serving.protein_g,
        value.serving.fat_g,
        value.serving.carbs_g,
    )
    if any(not item.is_finite() or item < 0 for item in macros):
        raise ValueError("serving macros must be non-negative finite values")

    protein_energy = Fraction(value.serving.protein_g) * 4
    legal_energy = (
        protein_energy
        + Fraction(value.serving.carbs_g) * 4
        + Fraction(value.serving.fat_g) * 9
    )
    return legal_energy > 0 and protein_energy * 5 >= legal_energy
