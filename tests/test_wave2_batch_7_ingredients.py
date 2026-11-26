"""Ingredients added for wave 2, batch 7 (fish and turkey II).

Olives for the cod in tomato sauce, a whole hot-smoked mackerel for the potato
salad and a bone-in turkey thigh for roasting. Each one has an exact USDA SR
Legacy record, Slovak renderer forms, a practical purchase rule where it is
not sold by weight, and fail-closed flyer matching.
"""
from datetime import date
from decimal import Decimal

import pytest

from app.ingredient_catalog import (
    load_ingredient_catalog,
    product_family,
    product_family_shared_forms,
)
from app.offer_matcher import match_offers
from app.quantity_math import Quantity
from app.recipe_renderer import (
    _EXTRA_INGREDIENT_FORMS,
    _QUANTITY_NAMES,
    _REFERENCE_NAMES,
    _slovak_title_forms,
)
from app.regular_purchase import regular_purchase_rule


WAVE2_BATCH7_FDC_IDS = {
    "olives": "169094",
    "smoked_mackerel": "175120",
    "turkey_thigh": "171533",
}

WAVE2_BATCH7_RENDER_FORMS = {
    "olives": ("olív bez kôstky", "olivy", "olív", "olivami"),
    "smoked_mackerel": (
        "údenej makrely",
        "údenú makrelu",
        "údenej makrely",
        "údenou makrelou",
    ),
    "turkey_thigh": (
        "morčacieho stehna",
        "morčacie stehno",
        "morčacieho stehna",
        "morčacím stehnom",
    ),
}


@pytest.fixture
def catalog():
    return load_ingredient_catalog()


def offer(**overrides):
    row = {
        "offer_key": "offer_123456789abc",
        "obchod": "Kaufland",
        "nazov": "Olivy čierne bez kôstky 340 g",
        "jednotka": "340 g",
        "cena": 1.49,
        "povodna": 1.99,
        "valid_from": "2026-11-26",
        "valid_to": "2026-12-02",
        "source_url": "https://example.test/kaufland-olivy.jpg",
    }
    row.update(overrides)
    return row


def test_batch7_ingredients_use_exact_sr_legacy_records(catalog):
    for ingredient_id, fdc_id in WAVE2_BATCH7_FDC_IDS.items():
        ingredient = catalog.by_id(ingredient_id)
        source = ingredient.nutrition.source
        assert "USDA FoodData Central SR Legacy (April 2018)" in source
        assert f"FDC ID {fdc_id}," in source
        assert "https://fdc.nal.usda.gov/download-datasets/" in source
        assert ingredient.nutrition.verified_on == date(2026, 11, 26)


def test_batch7_nutrition_matches_the_cited_records(catalog):
    olives = catalog.by_id("olives").nutrition
    mackerel = catalog.by_id("smoked_mackerel").nutrition
    thigh = catalog.by_id("turkey_thigh").nutrition

    assert (olives.kcal, olives.protein_g, olives.fat_g, olives.carbs_g) == (
        Decimal("116"), Decimal("0.84"), Decimal("10.9"), Decimal("6.04")
    )
    assert (mackerel.kcal, mackerel.protein_g, mackerel.fat_g) == (
        Decimal("262"), Decimal("23.85"), Decimal("17.81")
    )
    assert (thigh.kcal, thigh.protein_g, thigh.fat_g) == (
        Decimal("161"), Decimal("19.54"), Decimal("9.16")
    )


def test_batch7_ingredients_render_slovak_forms():
    title_forms = _slovak_title_forms()

    for ingredient_id, expected in WAVE2_BATCH7_RENDER_FORMS.items():
        quantity, reference, genitive, instrumental = expected
        assert _QUANTITY_NAMES[ingredient_id] == quantity
        assert _REFERENCE_NAMES[ingredient_id] == reference
        assert title_forms[ingredient_id] == {
            "genitive": genitive,
            "instrumental": instrumental,
        }
        assert reference in _EXTRA_INGREDIENT_FORMS[ingredient_id]


def test_batch7_edible_yields_discount_brine_and_bones(catalog):
    assert catalog.by_id("olives").edible_ratio == Decimal("0.45")
    assert catalog.by_id("smoked_mackerel").edible_ratio == Decimal("0.6")
    assert catalog.by_id("turkey_thigh").edible_ratio == Decimal("0.8")
    assert {tag.value for tag in catalog.by_id("olives").diet_tags} == {
        "vegetarian",
        "vegan",
    }
    assert "fish" in catalog.by_id("smoked_mackerel").allergens


def test_smoked_and_raw_mackerel_are_distinct_family_members(catalog):
    assert catalog.resolve("makrela").id == "mackerel"
    assert catalog.resolve("údená makrela").id == "smoked_mackerel"
    assert product_family("smoked_mackerel") == frozenset(
        {"mackerel", "smoked_mackerel"}
    )
    assert "makrelu" in product_family_shared_forms("mackerel")


def test_turkey_thigh_is_not_turkey_breast(catalog):
    assert catalog.resolve("morčacie stehno").id == "turkey_thigh"
    assert catalog.resolve("morčacie horné stehno").id == "turkey_thigh"
    assert catalog.resolve("morčacie prsia").id == "turkey_breast"


def test_olives_have_a_jar_purchase_rule_and_meat_fish_sell_by_weight(catalog):
    olives = regular_purchase_rule(catalog.by_id("olives"), "g")
    mackerel = regular_purchase_rule(catalog.by_id("smoked_mackerel"), "g")
    thigh = regular_purchase_rule(catalog.by_id("turkey_thigh"), "g")

    assert olives.package == Quantity(Decimal("340"), "g")
    assert olives.pricing_basis == "package"
    assert mackerel.pricing_basis == "weight"
    assert thigh.pricing_basis == "weight"


@pytest.mark.parametrize(
    ("product_name", "unit", "expected_id"),
    (
        ("Olivy čierne bez kôstky 340 g", "340 g", "olives"),
        ("Zelené olivy bez kôstky 340 g", "340 g", "olives"),
        ("Čierne olivy 300 g", "300 g", "olives"),
        ("Údená makrela 1 kg", "kg", "smoked_mackerel"),
        ("Údená makrela chladená 1 kg", "kg", "smoked_mackerel"),
        ("Morčacie stehno 1 kg", "kg", "turkey_thigh"),
        ("Morčacie horné stehno chladené 1 kg", "kg", "turkey_thigh"),
    ),
)
def test_batch7_labels_match(catalog, product_name, unit, expected_id):
    matched = match_offers([offer(nazov=product_name, jednotka=unit)], catalog)

    assert [item.ingredient.id for item in matched] == [expected_id]


@pytest.mark.parametrize(
    ("product_name", "unit"),
    (
        ("Olivy zelené plnené paprikou 340 g", "340 g"),
        ("Olivy čierne bez kôstky v oleji 200 g", "200 g"),
        ("Olivy s cesnakom 150 g", "150 g"),
        ("Makrela údená 1 kg", "1 kg"),
        ("Údená makrela filety 200 g", "200 g"),
        ("Morčacie stehno bez kosti 1 kg", "kg"),
        ("Morčacie stehno údené 500 g", "500 g"),
        ("Morčacie spodné stehno 1 kg", "kg"),
    ),
)
def test_batch7_lookalikes_fail_closed(catalog, product_name, unit):
    assert match_offers([offer(nazov=product_name, jednotka=unit)], catalog) == ()


def test_olive_oil_stays_oil_and_raw_mackerel_stays_raw(catalog):
    oil = match_offers(
        [offer(nazov="Olivový olej extra panenský 500 ml", jednotka="500 ml")],
        catalog,
    )
    mackerel = match_offers(
        [offer(nazov="Makrela chladená 1 kg", jednotka="1 kg")], catalog
    )

    assert [item.ingredient.id for item in oil] == ["oil"]
    assert [item.ingredient.id for item in mackerel] == ["mackerel"]
