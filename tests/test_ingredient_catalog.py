import json
from dataclasses import FrozenInstanceError
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from app.ingredient_catalog import DietTag, load_ingredient_catalog


CATALOG_PATH = Path(__file__).resolve().parents[1] / "app" / "catalog" / "ingredients.json"
REQUIRED_IDS = {
    "chicken_breast",
    "chicken_thigh",
    "pork_shoulder",
    "beef_mince",
    "salmon",
    "tofu",
    "red_lentils",
    "chickpeas",
    "egg",
    "cottage_cheese",
    "rice",
    "pasta",
    "potato",
    "bread",
    "zucchini",
    "tomato",
    "onion",
    "garlic",
    "carrot",
    "broccoli",
    "milk",
    "cream",
    "hard_cheese",
    "oil",
    "salt",
    "black_pepper",
}


def _ingredient(**overrides):
    value = {
        "id": "chicken_breast",
        "name": "kuracie prsia",
        "synonyms": ["kuracie prsné rezne"],
        "category": "mäso",
        "roles": ["protein"],
        "diet_tags": [],
        "allergens": [],
        "edible_ratio": "1",
        "grams_per_piece": None,
        "density_g_per_ml": None,
        "nutrition": {
            "kcal": "120",
            "protein_g": "22.5",
            "fat_g": "2.6",
            "carbs_g": "0",
            "source": (
                "USDA FoodData Central SR Legacy (April 2018), FDC ID 171077, "
                "Chicken, broilers or fryers, breast, meat only, raw; "
                "https://fdc.nal.usda.gov/fdc-app.html#/food-details/171077/nutrients"
            ),
            "verified_on": "2026-08-30",
        },
    }
    value.update(overrides)
    return value


def _write_catalog(tmp_path, ingredients):
    path = tmp_path / "ingredients.json"
    path.write_text(json.dumps({"ingredients": ingredients}), encoding="utf-8")
    return path


def test_catalog_resolves_slovak_synonym_without_guessing(tmp_path):
    fixture = _write_catalog(tmp_path, [_ingredient()])

    catalog = load_ingredient_catalog(fixture)

    assert catalog.resolve("  KURACIE   PRSIA ").id == "chicken_breast"
    assert catalog.resolve("kuracie prsne\u0301 rezne").id == "chicken_breast"
    assert catalog.resolve("kuracinka") is None
    assert catalog.resolve("kuracie prsia v akcii") is None


def test_catalog_rejects_duplicate_synonym(tmp_path):
    duplicate = _ingredient(
        id="chicken_thigh",
        name="kuracie stehná",
        synonyms=[" KURACIE   PRSNÉ REZNE "],
    )
    fixture = _write_catalog(tmp_path, [_ingredient(), duplicate])

    with pytest.raises(ValueError, match="duplicitné synonymum"):
        load_ingredient_catalog(fixture)


def test_catalog_rejects_duplicate_id_and_name(tmp_path):
    duplicate_id = _ingredient(name="kuracie stehná", synonyms=[])
    with pytest.raises(ValueError, match="duplicitné ID"):
        load_ingredient_catalog(_write_catalog(tmp_path, [_ingredient(), duplicate_id]))

    duplicate_name = _ingredient(id="chicken_thigh", synonyms=[])
    with pytest.raises(ValueError, match="duplicitný názov"):
        load_ingredient_catalog(_write_catalog(tmp_path, [_ingredient(), duplicate_name]))


def test_default_catalog_contains_verified_foundation_slice():
    catalog = load_ingredient_catalog()

    assert {item.id for item in catalog.all()} == REQUIRED_IDS
    assert catalog.by_id("rice").nutrition.protein_g > 0
    assert catalog.by_id("egg").grams_per_piece == Decimal("50")
    assert catalog.by_id("milk").density_g_per_ml == Decimal("1")
    assert catalog.by_id("oil").density_g_per_ml == Decimal("0.9")
    for item in catalog.all():
        assert "USDA FoodData Central" in item.nutrition.source
        assert "FDC ID" in item.nutrition.source
        assert "https://fdc.nal.usda.gov/" in item.nutrition.source
        assert item.nutrition.verified_on == date(2026, 8, 30)


def test_loaded_catalog_values_are_deeply_immutable(tmp_path):
    fixture = _write_catalog(tmp_path, [_ingredient()])
    ingredient = load_ingredient_catalog(fixture).by_id("chicken_breast")

    assert ingredient.synonyms == ("kuracie prsné rezne",)
    assert ingredient.roles == frozenset({"protein"})
    assert ingredient.diet_tags == frozenset()
    assert ingredient.allergens == ()
    assert ingredient.edible_ratio == Decimal("1")
    with pytest.raises(FrozenInstanceError):
        ingredient.name = "iné"


def test_vegan_catalog_item_is_also_vegetarian(tmp_path):
    fixture = _write_catalog(
        tmp_path,
        [_ingredient(id="tofu", name="tofu", synonyms=[], diet_tags=["vegan"])],
    )

    with pytest.raises(ValueError, match="vegan.*vegetarian"):
        load_ingredient_catalog(fixture)

    valid = _write_catalog(
        tmp_path,
        [
            _ingredient(
                id="tofu",
                name="tofu",
                synonyms=[],
                diet_tags=["vegan", "vegetarian"],
            )
        ],
    )
    assert load_ingredient_catalog(valid).by_id("tofu").diet_tags == frozenset(
        {DietTag.VEGAN, DietTag.VEGETARIAN}
    )


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("edible_ratio", "0", "jedlý podiel"),
        ("edible_ratio", "1.01", "jedlý podiel"),
        ("grams_per_piece", "0", "gramov na kus"),
        ("density_g_per_ml", "-0.1", "hustota"),
    ],
)
def test_catalog_rejects_invalid_physical_conversion(tmp_path, field, value, message):
    fixture = _write_catalog(tmp_path, [_ingredient(**{field: value})])

    with pytest.raises(ValueError, match=message):
        load_ingredient_catalog(fixture)


def test_catalog_rejects_unknown_role(tmp_path):
    fixture = _write_catalog(tmp_path, [_ingredient(roles=["main_character"])])

    with pytest.raises(ValueError, match="neznáma rola"):
        load_ingredient_catalog(fixture)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("kcal", "0"),
        ("protein_g", "-0.01"),
        ("fat_g", "NaN"),
        ("carbs_g", "Infinity"),
    ],
)
def test_catalog_rejects_invalid_nutrition(tmp_path, field, value):
    nutrition = dict(_ingredient()["nutrition"], **{field: value})
    fixture = _write_catalog(tmp_path, [_ingredient(nutrition=nutrition)])

    with pytest.raises(ValueError, match="výživ"):
        load_ingredient_catalog(fixture)


def test_catalog_rejects_energy_bearing_food_without_any_macro(tmp_path):
    nutrition = dict(
        _ingredient()["nutrition"], protein_g="0", fat_g="0", carbs_g="0"
    )
    fixture = _write_catalog(tmp_path, [_ingredient(nutrition=nutrition)])

    with pytest.raises(ValueError, match="výživové makrá"):
        load_ingredient_catalog(fixture)


def test_catalog_accepts_authoritative_zero_energy_for_mineral_seasoning(tmp_path):
    nutrition = dict(
        _ingredient()["nutrition"], kcal="0", protein_g="0", fat_g="0", carbs_g="0"
    )
    fixture = _write_catalog(
        tmp_path,
        [
            _ingredient(
                id="salt",
                name="soľ",
                synonyms=[],
                category="dochucovadlá",
                roles=["seasoning"],
                nutrition=nutrition,
            )
        ],
    )

    assert load_ingredient_catalog(fixture).by_id("salt").nutrition.kcal == 0


def test_catalog_requires_nutrition_source_metadata(tmp_path):
    nutrition = dict(_ingredient()["nutrition"], source="   ")
    fixture = _write_catalog(tmp_path, [_ingredient(nutrition=nutrition)])

    with pytest.raises(ValueError, match="zdroj"):
        load_ingredient_catalog(fixture)
