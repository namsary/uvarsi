"""Release gate for every launch diet, household and cooking rhythm.

The fixture deliberately looks like validated flyer output, but is completely
local and immutable.  The gate exercises the production ingredient and recipe
catalogs; it must never construct a model client or use the network.
"""

from __future__ import annotations

from decimal import Decimal
import math
import re
import sys
import types

import pytest

from app.deterministic_plan import build_deterministic_plan
from app.ingredient_catalog import DietTag, load_ingredient_catalog
from app.nutrition import MacroValues, NutritionEstimate, qualifies_high_protein
from app.plan_data import cooking_days_for_frequency, days_covered_by_meal
from app.quantity_math import parse_quantity
from app.recipe_catalog import load_recipe_catalog


WEEK = "2026-08-31"
VALID_TO = "2026-09-06"
STORES = ("Lidl", "Kaufland", "Tesco")
MODES = ("standard", "high_protein", "vegetarian", "vegan")
HOUSEHOLDS = ((1, 0), (2, 2), (4, 0))
FREQUENCIES = (1, 2, 3)
EXPECTED_MEALS = {1: 7, 2: 4, 3: 3}

# ingredient_id, store, package, sale price, ordinary price
# These package shapes and price relationships mirror rows accepted from the
# verified weekly flyer pipeline.  Names still come from the production
# catalog so renaming an ingredient cannot silently weaken offer matching.
VERIFIED_WEEKLY_OFFERS = (
    ("barley", "Kaufland", "500 g", "1.19", "1.69"),
    ("beans", "Tesco", "500 g", "1.29", "1.79"),
    ("beans_canned", "Lidl", "400 g", "0.99", "1.49"),
    ("beef_mince", "Lidl", "500 g", "3.99", "5.49"),
    ("bell_pepper", "Kaufland", "500 g", "1.49", "2.19"),
    ("broccoli", "Tesco", "500 g", "1.29", "1.99"),
    ("carrot", "Lidl", "1 kg", "0.79", "1.19"),
    ("chicken_breast", "Kaufland", "500 g", "2.99", "4.49"),
    ("chicken_thigh", "Tesco", "500 g", "2.49", "3.49"),
    ("chicken_thigh_meat", "Kaufland", "500 g", "2.99", "4.19"),
    ("chickpeas", "Lidl", "500 g", "1.09", "1.59"),
    ("chickpeas_canned", "Tesco", "400 g", "0.99", "1.49"),
    ("coconut_milk", "Kaufland", "400 ml", "1.39", "1.99"),
    ("cottage_cheese", "Tesco", "200 g", "1.19", "1.69"),
    ("couscous", "Lidl", "500 g", "1.29", "1.89"),
    ("egg", "Kaufland", "10 ks", "2.19", "2.99"),
    ("egg_noodles", "Tesco", "250 g", "1.09", "1.59"),
    ("feta", "Lidl", "200 g", "1.49", "2.19"),
    ("garlic", "Kaufland", "200 g", "1.19", "1.69"),
    ("hard_cheese", "Tesco", "250 g", "2.49", "3.29"),
    ("mushrooms", "Lidl", "500 g", "1.69", "2.29"),
    ("onion", "Kaufland", "1 kg", "0.89", "1.39"),
    ("pasta", "Tesco", "500 g", "0.89", "1.39"),
    ("peas", "Lidl", "450 g", "1.29", "1.89"),
    ("plain_yogurt", "Kaufland", "500 g", "1.09", "1.59"),
    ("pork_shoulder", "Tesco", "500 g", "2.99", "4.29"),
    ("potato", "Lidl", "2 kg", "1.69", "2.49"),
    ("red_lentils", "Kaufland", "500 g", "1.29", "1.89"),
    ("rice", "Tesco", "1 kg", "1.49", "2.19"),
    ("salmon", "Lidl", "500 g", "5.99", "7.99"),
    ("spinach", "Kaufland", "450 g", "1.39", "1.99"),
    ("tofu", "Tesco", "200 g", "1.19", "1.69"),
    ("tomato", "Lidl", "500 g", "1.39", "1.99"),
    ("tuna", "Kaufland", "160 g", "1.39", "1.99"),
    ("turkey_breast", "Tesco", "500 g", "3.49", "4.99"),
    ("white_fish", "Lidl", "500 g", "3.99", "5.49"),
    ("zucchini", "Kaufland", "500 g", "1.19", "1.79"),
)

MATCHABLE_PRODUCT_NAMES = {
    "beans_canned": "červená fazuľa v konzerve",
    "chickpeas_canned": "cícer v konzerve",
}

MATRIX = tuple(
    (mode, household, frequency)
    for mode in MODES
    for household in HOUSEHOLDS
    for frequency in FREQUENCIES
)


class _ForbiddenModelClient:
    def __init__(self, *args, **kwargs):
        raise AssertionError("recipe mode matrix attempted to construct a model client")


@pytest.fixture(autouse=True)
def no_live_model_clients(monkeypatch):
    poison = types.SimpleNamespace(
        Anthropic=_ForbiddenModelClient,
        AsyncAnthropic=_ForbiddenModelClient,
        OpenAI=_ForbiddenModelClient,
        AsyncOpenAI=_ForbiddenModelClient,
        Client=_ForbiddenModelClient,
    )
    monkeypatch.setitem(sys.modules, "anthropic", poison)
    monkeypatch.setitem(sys.modules, "openai", poison)


@pytest.fixture(scope="module")
def production_catalogs():
    ingredients = load_ingredient_catalog()
    return ingredients, load_recipe_catalog(ingredients)


@pytest.fixture(scope="module")
def verified_offer_rows(production_catalogs):
    ingredients, _ = production_catalogs
    rows = []
    for index, (ingredient_id, store, package, sale, ordinary) in enumerate(
        VERIFIED_WEEKLY_OFFERS, start=1
    ):
        ingredient = ingredients.by_id(ingredient_id)
        rows.append(
            {
                "offer_key": f"verified-{ingredient_id}",
                "obchod": store,
                "nazov": MATCHABLE_PRODUCT_NAMES.get(
                    ingredient_id, ingredient.name
                ),
                "jednotka": package,
                "cena": sale,
                "povodna": ordinary,
                "zlava": f"-{round((1 - Decimal(sale) / Decimal(ordinary)) * 100)} %",
                "valid_from": WEEK,
                "valid_to": VALID_TO,
                "source_url": f"https://fixtures.uvar.si/flyer/{store.casefold()}",
                "source_page": index,
            }
        )
    return tuple(rows)


def _selected_ingredient_ids(plan):
    return {
        row["offer_key"].removeprefix("verified-")
        for meal in plan["jedla"]
        for row in meal["suroviny"]
        if "offer_key" in row
    }


def _nutrition_estimate(recipe):
    def macros(section):
        return MacroValues(
            kcal=Decimal(section["kcal"]),
            protein_g=Decimal(section["protein_g"]),
            fat_g=Decimal(section["fat_g"]),
            carbs_g=Decimal(section["carbs_g"]),
        )

    nutrition = recipe["nutrition"]
    return NutritionEstimate(
        total=macros(nutrition["total"]),
        serving=macros(nutrition["serving"]),
        estimated=nutrition["estimated"],
    )


def _display_quantity(value, unit=None):
    if unit is None:
        match = re.fullmatch(r"(.+?)\s+(g|kg|ml|l|ks)", value)
        assert match is not None
        value, unit = match.groups()
    canonical_unit = "piece" if unit == "ks" else unit
    number = value.replace(" ", "").replace(",", ".")
    return parse_quantity(f"{number} {canonical_unit}")


def _assert_valid_packages_and_amounts(plan):
    assert Decimal(plan["nakup_spolu"].replace(",", ".")) > 0
    assert Decimal(plan["bezna_cena"].replace(",", ".")) >= Decimal(
        plan["nakup_spolu"].replace(",", ".")
    )
    for group in plan["nakupny_zoznam"]:
        for row in group["polozky"]:
            assert type(row["mnozstvo"]) is int and row["mnozstvo"] > 0
            assert re.fullmatch(r"[1-9]\d*(?:[,.]\d+)? (?:g|kg|ml|ks)", row["jednotka"])
            assert Decimal(row["cena"].replace(",", ".")) > 0
            assert Decimal(row["povodna"].replace(",", ".")) >= Decimal(
                row["cena"].replace(",", ".")
            )
            required = _display_quantity(
                row["potrebne"], row["potrebna_jednotka"]
            )
            leftover = _display_quantity(row["zostava"])
            assert required.amount.is_finite() and required.amount > 0
            assert leftover.amount.is_finite() and leftover.amount >= 0

    for meal in plan["jedla"]:
        for row in meal["suroviny"]:
            if "offer_key" not in row:
                continue
            assert row["valid_from"] == WEEK
            assert row["valid_to"] == VALID_TO
            assert type(row["mnozstvo"]) is int and row["mnozstvo"] > 0
            assert re.fullmatch(r"[1-9]\d*(?:[,.]\d+)? (?:g|kg|ml|ks)", row["jednotka"])


@pytest.mark.parametrize(("mode", "household", "frequency"), MATRIX)
def test_recipe_modes_cover_every_launch_profile_without_live_ai(
    production_catalogs,
    verified_offer_rows,
    mode,
    household,
    frequency,
):
    ingredients, recipes = production_catalogs
    adults, children = household

    plan = build_deterministic_plan(
        week=WEEK,
        rows=verified_offer_rows,
        stores=STORES,
        adults=adults,
        children=children,
        frequency=frequency,
        pantry=(),
        pantry_driven=False,
        mode=mode,
        seed=f"release-matrix:{mode}:{adults}:{children}:{frequency}",
        ingredient_catalog=ingredients,
        recipe_catalog=recipes,
    )

    assert plan["meta"]["mode"] == mode
    assert len(plan["jedla"]) == EXPECTED_MEALS[frequency]
    assert tuple(meal["den"] for meal in plan["jedla"]) == cooking_days_for_frequency(
        frequency
    )
    assert tuple(meal["pokryva_dni"] for meal in plan["jedla"]) == tuple(
        days_covered_by_meal(frequency, day)
        for day in cooking_days_for_frequency(frequency)
    )
    assert sum(meal["pokryva_dni"] for meal in plan["jedla"]) == 7

    templates = {template.id: template for template in recipes.all()}
    assert all(
        mode in templates[meal["recept"]["template_id"]].modes
        for meal in plan["jedla"]
    )
    selected_ids = _selected_ingredient_ids(plan)
    if mode == "vegetarian":
        assert all(
            DietTag.VEGETARIAN in ingredients.by_id(item).diet_tags
            for item in selected_ids
        )
    if mode == "vegan":
        assert all(
            DietTag.VEGAN in ingredients.by_id(item).diet_tags
            for item in selected_ids
        )

    for meal in plan["jedla"]:
        recipe = meal["recept"]
        estimate = _nutrition_estimate(recipe)
        assert all(
            value.is_finite() and value >= 0
            for section in (estimate.total, estimate.serving)
            for value in (
                section.kcal,
                section.protein_g,
                section.fat_g,
                section.carbs_g,
            )
        )
        if mode == "high_protein":
            assert estimate.serving.protein_g >= Decimal("30")
            assert recipe.get("high_protein_claim") is qualifies_high_protein(estimate)
        else:
            assert "high_protein_claim" not in recipe

    _assert_valid_packages_and_amounts(plan)


def test_recipe_mode_matrix_has_exactly_the_required_36_combinations():
    assert len(MATRIX) == math.prod((len(MODES), len(HOUSEHOLDS), len(FREQUENCIES)))
    assert len(MATRIX) == 36
    assert {mode for mode, _, _ in MATRIX} == set(MODES)
    assert {household for _, household, _ in MATRIX} == set(HOUSEHOLDS)
    assert {frequency for _, _, frequency in MATRIX} == set(FREQUENCIES)


def test_verified_offer_fixture_covers_every_production_recipe_candidate(
    production_catalogs,
):
    _, recipes = production_catalogs
    fixture_ids = [ingredient_id for ingredient_id, *_ in VERIFIED_WEEKLY_OFFERS]
    recipe_ids = {
        ingredient_id
        for recipe in recipes.all()
        for slot in recipe.slots
        for ingredient_id in slot.candidates
    }

    assert len(fixture_ids) == len(set(fixture_ids))
    assert set(fixture_ids) == recipe_ids
