"""Decimal-only nutrition estimates for deterministic recipes."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
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


def estimate_recipe_nutrition(
    lines: Sequence[tuple[Ingredient, Decimal]],
    adult_servings: Decimal,
) -> NutritionEstimate:
    """Estimate total and per-adult nutrition from edible gram quantities."""
    if not adult_servings.is_finite() or adult_servings <= Decimal("0"):
        raise ValueError("adult servings must be a positive finite value")

    kcal = Decimal("0")
    protein_g = Decimal("0")
    fat_g = Decimal("0")
    carbs_g = Decimal("0")

    for ingredient, edible_grams in lines:
        if not edible_grams.is_finite() or edible_grams < Decimal("0"):
            raise ValueError("edible grams must be a non-negative finite value")
        scale = edible_grams / Decimal("100")
        nutrition = ingredient.nutrition
        kcal += nutrition.kcal * scale
        protein_g += nutrition.protein_g * scale
        fat_g += nutrition.fat_g * scale
        carbs_g += nutrition.carbs_g * scale

    total = MacroValues(
        kcal=kcal,
        protein_g=protein_g,
        fat_g=fat_g,
        carbs_g=carbs_g,
    )
    serving = MacroValues(
        kcal=total.kcal / adult_servings,
        protein_g=total.protein_g / adult_servings,
        fat_g=total.fat_g / adult_servings,
        carbs_g=total.carbs_g / adult_servings,
    )
    return NutritionEstimate(total=total, serving=serving)


def qualifies_high_protein(value: NutritionEstimate) -> bool:
    protein_kcal = value.serving.protein_g * Decimal("4")
    return (
        value.serving.kcal > Decimal("0")
        and protein_kcal / value.serving.kcal >= Decimal("0.20")
    )
