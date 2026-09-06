from decimal import Decimal

import pytest

from app.ingredient_catalog import load_ingredient_catalog
from app.offer_matcher import match_offers
from app.recipe_catalog import load_recipe_catalog


def _offer(product_name: str, unit: str = "400 g") -> dict[str, object]:
    return {
        "offer_key": "catalog-consistency-offer",
        "obchod": "Lidl",
        "nazov": product_name,
        "jednotka": unit,
        "cena": 1.49,
        "povodna": 1.99,
        "valid_from": "2026-09-03",
        "valid_to": "2026-09-09",
        "source_url": "https://example.test/letak",
    }


@pytest.mark.parametrize(
    "product_name",
    (
        "Cícer 400 g",
        "Červená fazuľa 400 g",
        "Fazuľa 400 g",
    ),
)
def test_generic_legume_offer_without_product_form_fails_closed(product_name):
    catalog = load_ingredient_catalog()

    assert match_offers([_offer(product_name)], catalog) == ()


@pytest.mark.parametrize(
    ("product_name", "expected_id"),
    (
        ("Cícer v konzerve 400 g", "chickpeas_canned"),
        ("Cícer suchý 500 g", "chickpeas"),
        ("Červená fazuľa v konzerve 400 g", "beans_canned"),
        ("Fazuľa v konzerve 400 g", "beans_canned"),
        ("Konzervovaná fazuľa 400 g", "beans_canned"),
        ("Sterilizovaná fazuľa 400 g", "beans_canned"),
        ("Červená fazuľa suchá 500 g", "beans"),
        ("Suchá fazuľa 500 g", "beans"),
        ("Sušená fazuľa 500 g", "beans"),
    ),
)
def test_explicit_offer_product_form_distinguishes_dry_and_canned_legumes(
    product_name, expected_id
):
    catalog = load_ingredient_catalog()

    matched = match_offers([_offer(product_name)], catalog)

    assert len(matched) == 1
    assert matched[0].ingredient.id == expected_id


def test_dry_and_canned_legumes_have_distinct_yield_and_nutrition():
    catalog = load_ingredient_catalog()

    chickpeas_dry = catalog.by_id("chickpeas")
    chickpeas_canned = catalog.by_id("chickpeas_canned")
    beans_dry = catalog.by_id("beans")
    beans_canned = catalog.by_id("beans_canned")

    assert chickpeas_dry.edible_ratio == Decimal("1")
    assert beans_dry.edible_ratio == Decimal("1")
    assert chickpeas_canned.edible_ratio == Decimal("0.6")
    assert beans_canned.edible_ratio == Decimal("0.6")
    assert chickpeas_dry.nutrition.kcal == Decimal("378")
    assert chickpeas_canned.nutrition.kcal == Decimal("137.22")
    assert beans_dry.nutrition.kcal == Decimal("333")
    assert beans_canned.nutrition.kcal == Decimal("127")


def _recipes_using(ingredient_id: str):
    ingredients = load_ingredient_catalog()
    return tuple(
        recipe
        for recipe in load_recipe_catalog(ingredients, include_inactive=True).all()
        if any(ingredient_id in slot.candidates for slot in recipe.slots)
    )


@pytest.mark.parametrize("ingredient_id", ("chickpeas", "beans"))
def test_dry_legume_recipes_include_advance_prep_cooking_and_final_draining(
    ingredient_id,
):
    recipes = _recipes_using(ingredient_id)

    assert recipes
    for recipe in recipes:
        steps = " ".join(step.text for step in recipe.instructions).casefold()
        assert "12 hodín" in steps, recipe.id
        assert "60 minút" in steps, recipe.id
        assert steps.count("sceď") >= 2, recipe.id


@pytest.mark.parametrize(
    "ingredient_id", ("chickpeas_canned", "beans_canned")
)
def test_canned_legume_recipes_only_drain_rinse_and_heat(ingredient_id):
    recipes = _recipes_using(ingredient_id)

    assert recipes
    for recipe in recipes:
        steps = " ".join(step.text for step in recipe.instructions).casefold()
        assert "sceď" in steps, recipe.id
        assert "prepláchni" in steps, recipe.id
        assert "namoč" not in steps, recipe.id
        assert "12 hodín" not in steps, recipe.id
        assert "60 minút" not in steps, recipe.id


def test_one_recipe_slot_never_mixes_dry_and_canned_versions():
    ingredient_pairs = (
        frozenset(("chickpeas", "chickpeas_canned")),
        frozenset(("beans", "beans_canned")),
    )

    ingredients = load_ingredient_catalog()
    recipes = load_recipe_catalog(ingredients, include_inactive=True).all()
    for recipe in recipes:
        for slot in recipe.slots:
            candidates = frozenset(slot.candidates)
            for pair in ingredient_pairs:
                assert not pair.issubset(candidates), f"{recipe.id}:{slot.key}"


def test_canned_legume_alone_is_not_marked_as_high_protein():
    recipes = load_recipe_catalog(
        load_ingredient_catalog(), include_inactive=True
    ).all()

    offenders = []
    canned_legumes = {"chickpeas_canned", "beans_canned"}
    for recipe in recipes:
        protein_slots = tuple(slot for slot in recipe.slots if slot.role == "protein")
        if (
            "high_protein" in recipe.modes
            and len(protein_slots) == 1
            and canned_legumes.intersection(protein_slots[0].candidates)
        ):
            offenders.append(recipe.id)

    assert offenders == []


@pytest.mark.parametrize(
    ("product_name", "expected_id"),
    (
        ("Kuracie stehná 1 kg", "chicken_thigh"),
        ("Kuracie stehná s kosťou 1 kg", "chicken_thigh"),
        ("Kuracie stehná bez kostí 600 g", "chicken_thigh_meat"),
        ("Kuracie stehenné rezne 600 g", "chicken_thigh_meat"),
    ),
)
def test_offer_matching_distinguishes_bone_in_and_boneless_thighs(
    product_name, expected_id
):
    catalog = load_ingredient_catalog()

    matched = match_offers([_offer(product_name, "1 kg")], catalog)

    assert len(matched) == 1
    assert matched[0].ingredient.id == expected_id


def test_bone_in_pork_shoulder_offer_fails_closed():
    catalog = load_ingredient_catalog()

    matched = match_offers(
        [_offer("Bravčové pliecko s kosťou 1 kg", "1 kg")],
        catalog,
    )

    assert matched == ()


@pytest.mark.parametrize(
    "offer_name", ("Bravčové pliecko 1 kg", "Bravčové plece bez kosti 1 kg")
)
def test_pork_shoulder_still_matches_generic_or_explicit_boneless_offer(offer_name):
    catalog = load_ingredient_catalog()

    matched = match_offers([_offer(offer_name, "1 kg")], catalog)

    assert len(matched) == 1
    assert matched[0].ingredient.id == "pork_shoulder"


def test_chicken_thigh_yield_and_nutrition_match_the_purchased_cut():
    catalog = load_ingredient_catalog()
    bone_in = catalog.by_id("chicken_thigh")
    boneless = catalog.by_id("chicken_thigh_meat")

    assert bone_in.edible_ratio == Decimal("0.79")
    assert boneless.edible_ratio == Decimal("1")
    assert bone_in.nutrition.kcal == Decimal("188")
    assert boneless.nutrition.kcal == Decimal("144")

    bone_in_kcal = Decimal("245") * bone_in.edible_ratio * bone_in.nutrition.kcal / 100
    boneless_kcal = Decimal("190") * boneless.edible_ratio * boneless.nutrition.kcal / 100
    assert bone_in_kcal == Decimal("363.8740")
    assert boneless_kcal == Decimal("273.6")


def test_bone_in_thigh_recipe_buys_gross_weight_and_never_cuts_the_bone():
    ingredients = load_ingredient_catalog()
    recipes = load_recipe_catalog(ingredients, include_inactive=True).all()
    bone_in_slots = tuple(
        (recipe, slot)
        for recipe in recipes
        for slot in recipe.slots
        if "chicken_thigh" in slot.candidates
    )

    assert bone_in_slots
    for recipe, slot in bone_in_slots:
        assert slot.amount_per_adult == Decimal("245"), recipe.id
        assert slot.cut is None, recipe.id


def test_boneless_thigh_meat_is_available_in_cuttable_recipes():
    recipes = _recipes_using("chicken_thigh_meat")

    assert len(recipes) >= 3
    for recipe in recipes:
        matching_slots = tuple(
            slot for slot in recipe.slots if "chicken_thigh_meat" in slot.candidates
        )
        assert all(slot.cut for slot in matching_slots), recipe.id
        assert all("chicken_thigh" not in slot.candidates for slot in matching_slots)


def test_every_recipe_that_uses_a_pan_lists_it_as_equipment():
    ingredients = load_ingredient_catalog()
    recipes = load_recipe_catalog(ingredients, include_inactive=True).all()

    offenders = tuple(
        recipe.id
        for recipe in recipes
        if "panvic" in " ".join(step.text for step in recipe.instructions).casefold()
        and not any("panvic" in item.casefold() for item in recipe.equipment)
    )

    assert offenders == ()


def test_modern_family_signature_ingredients_exist():
    catalog = load_ingredient_catalog()
    required = {
        "basil_pesto",
        "chili_powder",
        "cumin",
        "gnocchi",
        "grilling_cheese",
        "lemon",
        "lettuce",
        "mozzarella",
        "soy_sauce",
        "tortilla",
    }

    for ingredient_id in required:
        ingredient = catalog.by_id(ingredient_id)
        assert ingredient.name
        assert ingredient.synonyms


def test_high_protein_signature_ingredients_exist():
    catalog = load_ingredient_catalog()
    required = {
        "bulgur",
        "cucumber",
        "skyr",
        "sweet_potato",
        "turkey_mince",
    }

    for ingredient_id in required:
        ingredient = catalog.by_id(ingredient_id)
        assert ingredient.name
        assert ingredient.synonyms
