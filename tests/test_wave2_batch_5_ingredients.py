"""Ingredients added for wave 2, batch 5 (more Slovak classics)."""

from datetime import date
from decimal import Decimal

import pytest

from app.ingredient_catalog import load_ingredient_catalog
from app.offer_matcher import match_offers
from app.quantity_math import Quantity
from app.recipe_renderer import (
    _EXTRA_INGREDIENT_FORMS,
    _QUANTITY_NAMES,
    _REFERENCE_NAMES,
    _slovak_title_forms,
)
from app.regular_purchase import regular_purchase_rule
from tests.test_offer_matcher import offer


BATCH5_FDC_IDS = {
    "chicken_liver": "171060",
    "smoked_pork": "168288",
    "pork_ribs": "167853",
    "chicken_drumstick": "172373",
    "duck_leg": "172408",
    "quark": "172179",
}

BATCH5_RENDER_FORMS = {
    "chicken_liver": (
        "kuracej pečene",
        "kuraciu pečeň",
        "kuracej pečene",
        "kuracou pečeňou",
    ),
    "smoked_pork": (
        "údeného bravčového mäsa",
        "údené bravčové mäso",
        "údeného bravčového mäsa",
        "údeným bravčovým mäsom",
    ),
    "pork_ribs": (
        "bravčových rebierok",
        "bravčové rebierka",
        "bravčových rebierok",
        "bravčovými rebierkami",
    ),
    "chicken_drumstick": (
        "kuracích paličiek",
        "kuracie paličky",
        "kuracích paličiek",
        "kuracími paličkami",
    ),
    "duck_leg": (
        "kačacích stehien",
        "kačacie stehná",
        "kačacích stehien",
        "kačacími stehnami",
    ),
    "quark": ("tvarohu", "tvaroh", "tvarohu", "tvarohom"),
}

BATCH5_PACKAGES = {
    "chicken_liver": Quantity(Decimal("500"), "g"),
    "smoked_pork": Quantity(Decimal("300"), "g"),
    "pork_ribs": Quantity(Decimal("1000"), "g"),
    "chicken_drumstick": Quantity(Decimal("1000"), "g"),
    "duck_leg": Quantity(Decimal("600"), "g"),
    "quark": Quantity(Decimal("250"), "g"),
}


@pytest.fixture(scope="module")
def catalog():
    return load_ingredient_catalog()


def test_batch5_ingredients_use_exact_sr_legacy_records(catalog):
    for ingredient_id, fdc_id in BATCH5_FDC_IDS.items():
        ingredient = catalog.by_id(ingredient_id)
        source = ingredient.nutrition.source
        assert "USDA FoodData Central SR Legacy (April 2018)" in source
        assert f"FDC ID {fdc_id}," in source
        assert "https://fdc.nal.usda.gov/download-datasets/" in source
        assert ingredient.nutrition.verified_on == date(2026, 11, 26)


def test_batch5_nutrition_matches_the_cited_records(catalog):
    expected = {
        "pork_ribs": ("277", "15.47", "23.4", "0"),
        "chicken_drumstick": ("161", "18.08", "9.2", "0.11"),
        "duck_leg": ("404", "11.49", "39.34", "0"),
        "quark": ("98", "11.12", "4.3", "3.38"),
    }
    for ingredient_id, (kcal, protein, fat, carbs) in expected.items():
        nutrition = catalog.by_id(ingredient_id).nutrition
        assert nutrition.kcal == Decimal(kcal)
        assert nutrition.protein_g == Decimal(protein)
        assert nutrition.fat_g == Decimal(fat)
        assert nutrition.carbs_g == Decimal(carbs)


def test_batch5_bone_in_meat_uses_edible_yields(catalog):
    assert catalog.by_id("pork_ribs").edible_ratio == Decimal("0.7")
    assert catalog.by_id("chicken_drumstick").edible_ratio == Decimal("0.7")
    assert catalog.by_id("duck_leg").edible_ratio == Decimal("0.55")
    assert catalog.by_id("quark").edible_ratio == Decimal("1")


def test_batch5_quark_is_vegetarian_dairy(catalog):
    quark = catalog.by_id("quark")
    assert quark.roles == frozenset({"protein", "dairy"})
    assert quark.allergens == ("milk",)
    assert "vegetarian" in {tag.value for tag in quark.diet_tags}
    assert catalog.resolve("tvaroh").id == "quark"
    assert catalog.resolve("cottage cheese").id == "cottage_cheese"


def test_batch5_ingredients_render_slovak_forms():
    title_forms = _slovak_title_forms()
    for ingredient_id, expected in BATCH5_RENDER_FORMS.items():
        quantity, reference, genitive, instrumental = expected
        assert _QUANTITY_NAMES[ingredient_id] == quantity
        assert _REFERENCE_NAMES[ingredient_id] == reference
        assert title_forms[ingredient_id] == {
            "genitive": genitive,
            "instrumental": instrumental,
        }


def test_batch5_extra_forms_stay_specific():
    # Generic words such as „stehná“ would flag other recipes that mention a
    # chicken thigh, so duck forms must keep the „kačacie“ qualifier.
    assert all("kačac" in form for form in _EXTRA_INGREDIENT_FORMS["duck_leg"])
    assert "paličky" in _EXTRA_INGREDIENT_FORMS["chicken_drumstick"]
    assert "rebierka" in _EXTRA_INGREDIENT_FORMS["pork_ribs"]


def test_batch5_regular_purchase_packages(catalog):
    for ingredient_id, package in BATCH5_PACKAGES.items():
        rule = regular_purchase_rule(catalog.by_id(ingredient_id), "g")
        assert rule.package == package


@pytest.mark.parametrize(
    ("product_name", "unit", "expected_id"),
    (
        ("Bravčové rebierka 1 kg", "1 kg", "pork_ribs"),
        ("Bravčové rebrá chladené 1 kg", "1 kg", "pork_ribs"),
        ("Kuracie paličky 1 kg", "1 kg", "chicken_drumstick"),
        ("Kuracie spodné stehná chladené 1 kg", "1 kg", "chicken_drumstick"),
        ("Kačacie stehná 600 g", "600 g", "duck_leg"),
        ("Kačacie stehno chladené 300 g", "300 g", "duck_leg"),
        ("Tvaroh polotučný 250 g", "250 g", "quark"),
        ("Hrudkovitý tvaroh 250 g", "250 g", "quark"),
        ("Jemný tvaroh 250 g", "250 g", "quark"),
        ("Kuracia pečeň 500 g", "500 g", "chicken_liver"),
        ("Kuracie pečienky 500 g", "500 g", "chicken_liver"),
        ("Údené bravčové karé 300 g", "300 g", "smoked_pork"),
        ("Údené mäso 300 g", "300 g", "smoked_pork"),
    ),
)
def test_batch5_offer_labels_match(catalog, product_name, unit, expected_id):
    matched = match_offers([offer(nazov=product_name, jednotka=unit)], catalog)

    assert [item.ingredient.id for item in matched] == [expected_id]


@pytest.mark.parametrize(
    ("product_name", "unit"),
    (
        ("Bravčové rebierka údené 500 g", "500 g"),
        ("Bravčové rebierka marinované 600 g", "600 g"),
        ("Bravčové rebierka v BBQ marináde 600 g", "600 g"),
        ("Kuracie paličky marinované 500 g", "500 g"),
        ("Kuracie paličky obaľované 400 g", "400 g"),
        ("Kačacie stehná konfitované 500 g", "500 g"),
        ("Tvarohový dezert vanilkový 140 g", "140 g"),
        ("Tvaroh s jahodami 140 g", "140 g"),
        ("Kuracie srdiečka a žalúdky 500 g", "500 g"),
        ("Údené rebierka 500 g", "500 g"),
    ),
)
def test_batch5_processed_or_flavoured_products_fail_closed(
    catalog, product_name, unit
):
    assert match_offers([offer(nazov=product_name, jednotka=unit)], catalog) == ()


def test_chicken_thighs_do_not_match_drumsticks(catalog):
    matched = match_offers(
        [offer(nazov="Kuracie stehná 1 kg", jednotka="1 kg")], catalog
    )
    assert [item.ingredient.id for item in matched] == ["chicken_thigh"]
