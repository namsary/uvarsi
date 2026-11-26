"""Ingredients added for wave 2, batch 6 (quick weeknight dinners II)."""
from datetime import date
from decimal import Decimal

import pytest

from app.ingredient_catalog import load_ingredient_catalog
from app.offer_matcher import match_offers
from app.recipe_renderer import (
    _EXTRA_INGREDIENT_FORMS,
    _QUANTITY_NAMES,
    _REFERENCE_NAMES,
    _slovak_title_forms,
)
from app.quantity_math import Quantity
from app.regular_purchase import regular_purchase_rule
from tests.test_offer_matcher import offer


@pytest.fixture
def catalog():
    return load_ingredient_catalog()


def test_rice_noodles_use_exact_sr_legacy_record(catalog):
    ingredient = catalog.by_id("rice_noodles")
    nutrition = ingredient.nutrition

    assert "USDA FoodData Central SR Legacy (April 2018)" in nutrition.source
    assert "FDC ID 169742, Rice noodles, dry;" in nutrition.source
    assert "https://fdc.nal.usda.gov/download-datasets/" in nutrition.source
    assert nutrition.verified_on == date(2026, 11, 26)
    assert (nutrition.kcal, nutrition.protein_g, nutrition.fat_g, nutrition.carbs_g) == (
        Decimal("364"),
        Decimal("5.95"),
        Decimal("0.56"),
        Decimal("80.18"),
    )
    assert ingredient.edible_ratio == Decimal("1")
    assert {tag.value for tag in ingredient.diet_tags} == {"vegetarian", "vegan"}
    assert "starch" in ingredient.roles


def test_rice_noodles_render_slovak_forms():
    assert _QUANTITY_NAMES["rice_noodles"] == "ryžových rezancov"
    assert _REFERENCE_NAMES["rice_noodles"] == "ryžové rezance"
    assert "ryžové rezance" in _EXTRA_INGREDIENT_FORMS["rice_noodles"]
    assert _slovak_title_forms()["rice_noodles"] == {
        "genitive": "ryžových rezancov",
        "instrumental": "ryžovými rezancami",
    }


def test_rice_noodles_stay_distinct_from_rice_and_egg_noodles(catalog):
    assert catalog.resolve("ryžové rezance").id == "rice_noodles"
    assert catalog.resolve("ryža").id == "rice"
    assert catalog.resolve("vaječné rezance").id == "egg_noodles"


def test_rice_noodles_have_a_regular_package(catalog):
    rule = regular_purchase_rule(catalog.by_id("rice_noodles"), "g")

    assert rule.package == Quantity(Decimal("200"), "g")


@pytest.mark.parametrize(
    ("product_name", "unit", "expected_id"),
    (
        ("Ryžové rezance 250 g", "250 g", "rice_noodles"),
        ("Rezance ryžové široké 200 g", "200 g", "rice_noodles"),
        ("Ryžové nudle 400 g", "400 g", "rice_noodles"),
        ("Ryža dlhozrnná 1 kg", "1 kg", "rice"),
        ("Vaječné rezance 500 g", "500 g", "egg_noodles"),
    ),
)
def test_wave2_batch6_labels_match(catalog, product_name, unit, expected_id):
    matched = match_offers([offer(nazov=product_name, jednotka=unit)], catalog)

    assert [item.ingredient.id for item in matched] == [expected_id]


@pytest.mark.parametrize(
    ("product_name", "unit"),
    (
        ("Instantné ryžové rezance 60 g", "60 g"),
        ("Ryžové rezance s príchuťou kuracou 60 g", "60 g"),
        ("Ryžové rezance so zeleninou 300 g", "300 g"),
    ),
)
def test_wave2_batch6_lookalikes_fail_closed(catalog, product_name, unit):
    assert match_offers([offer(nazov=product_name, jednotka=unit)], catalog) == ()
