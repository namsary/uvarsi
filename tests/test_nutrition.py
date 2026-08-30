from decimal import Decimal, localcontext

import pytest

from app.ingredient_catalog import load_ingredient_catalog
from app.nutrition import (
    MacroValues,
    NutritionEstimate,
    estimate_recipe_nutrition,
    qualifies_high_protein,
)


@pytest.fixture
def chicken_and_rice():
    catalog = load_ingredient_catalog()
    return catalog.by_id("chicken_breast"), catalog.by_id("rice")


def test_nutrition_totals_use_edible_grams_and_per_100_values(chicken_and_rice):
    chicken, rice = chicken_and_rice

    estimate = estimate_recipe_nutrition(
        [(chicken, Decimal("600")), (rice, Decimal("300"))],
        adult_servings=Decimal("4"),
    )

    assert estimate.total == MacroValues(
        kcal=Decimal("1815"),
        protein_g=Decimal("156.39"),
        fat_g=Decimal("17.70"),
        carbs_g=Decimal("239.85"),
    )
    assert estimate.estimated is True


def test_nutrition_is_divided_by_real_adult_equivalents(chicken_and_rice):
    chicken, rice = chicken_and_rice

    estimate = estimate_recipe_nutrition(
        [(chicken, Decimal("600")), (rice, Decimal("300"))],
        adult_servings=Decimal("4"),
    )

    assert estimate.serving.protein_g == estimate.total.protein_g / 4
    assert estimate.serving == MacroValues(
        kcal=Decimal("453.75"),
        protein_g=Decimal("39.0975"),
        fat_g=Decimal("4.425"),
        carbs_g=Decimal("59.9625"),
    )


def test_high_protein_requires_twenty_percent_of_energy():
    serving = MacroValues(
        kcal=Decimal("370"),
        protein_g=Decimal("30"),
        fat_g=Decimal("10"),
        carbs_g=Decimal("40"),
    )

    assert qualifies_high_protein(
        NutritionEstimate(total=serving, serving=serving)
    ) is True


@pytest.mark.parametrize(
    ("kcal", "protein_g", "expected"),
    [
        ("400", "20", True),
        ("400", "19.99", False),
        ("0", "20", False),
    ],
)
def test_high_protein_uses_inclusive_twenty_percent_boundary(
    kcal, protein_g, expected
):
    serving = MacroValues(
        kcal=Decimal(kcal),
        protein_g=Decimal(protein_g),
        fat_g=Decimal("0"),
        carbs_g=Decimal("0"),
    )
    estimate = NutritionEstimate(total=serving, serving=serving)

    assert qualifies_high_protein(estimate) is expected


def test_high_protein_rejects_extreme_value_just_below_twenty_percent():
    serving = MacroValues(
        kcal=Decimal("400"),
        protein_g=Decimal("19.9999999999999999999999999999"),
        fat_g=Decimal("0"),
        carbs_g=Decimal("0"),
    )

    assert qualifies_high_protein(
        NutritionEstimate(total=serving, serving=serving)
    ) is False


def test_high_protein_preserves_boundary_under_changed_ambient_precision():
    below = MacroValues(
        kcal=Decimal("400"),
        protein_g=Decimal("19.9999999999999999999999999999"),
        fat_g=Decimal("0"),
        carbs_g=Decimal("0"),
    )
    exact = MacroValues(
        kcal=Decimal("400"),
        protein_g=Decimal("20"),
        fat_g=Decimal("0"),
        carbs_g=Decimal("0"),
    )

    with localcontext() as context:
        context.prec = 2
        assert qualifies_high_protein(
            NutritionEstimate(total=below, serving=below)
        ) is False
        assert qualifies_high_protein(
            NutritionEstimate(total=exact, serving=exact)
        ) is True


@pytest.mark.parametrize("adult_servings", [Decimal("0"), Decimal("-1")])
def test_nutrition_rejects_non_positive_adult_servings(
    chicken_and_rice, adult_servings
):
    chicken, _ = chicken_and_rice

    with pytest.raises(ValueError, match="adult servings"):
        estimate_recipe_nutrition(
            [(chicken, Decimal("100"))],
            adult_servings=adult_servings,
        )


def test_nutrition_rejects_negative_edible_grams(chicken_and_rice):
    chicken, _ = chicken_and_rice

    with pytest.raises(ValueError, match="edible grams"):
        estimate_recipe_nutrition(
            [(chicken, Decimal("-0.01"))],
            adult_servings=Decimal("1"),
        )
