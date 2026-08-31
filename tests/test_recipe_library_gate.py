import re
from decimal import Decimal
from itertools import product

import pytest

from app.ingredient_catalog import load_ingredient_catalog
from app.recipe_catalog import load_recipe_catalog
from app.recipe_matcher import RecipeCandidate, SlotSelection
from app.recipe_renderer import render_meal


EXPECTED_FIRST_SLICE_IDS = {
    "pan_chicken_rice_vegetables",
    "pan_chicken_pasta_tomato",
    "pan_pork_potato_onion",
    "pan_beef_rice_pepper",
    "pan_fish_potato_spinach",
    "pan_turkey_couscous_zucchini",
    "pan_egg_potato_spinach",
    "pan_tofu_rice_broccoli",
    "pan_chickpea_tomato_spinach",
    "pan_cottage_pasta_zucchini",
    "oven_chicken_thigh_potato_carrot",
    "oven_chicken_breast_zucchini_rice",
    "oven_pork_shoulder_root_vegetables",
    "oven_meatballs_tomato_potato",
    "oven_salmon_potato_broccoli",
    "oven_white_fish_tomato_rice",
    "oven_tofu_vegetables_potato",
    "oven_feta_tomato_pasta",
    "oven_egg_vegetable_frittata",
    "oven_lentil_vegetable_loaf",
    "pot_chicken_rice_peas",
    "pot_chicken_paprika_pasta",
    "pot_pork_barley_vegetables",
    "pot_beef_tomato_pasta",
    "pot_fish_tomato_potato",
    "pot_turkey_lentil_tomato",
    "pot_red_lentil_curry_rice",
    "pot_chickpea_tomato_couscous",
    "pot_tofu_coconut_vegetables",
    "pot_bean_chili_rice",
}


def _first_slice():
    ingredients = load_ingredient_catalog()
    return tuple(
        recipe
        for recipe in load_recipe_catalog(ingredients).all()
        if recipe.id.startswith(("pan_", "oven_", "pot_"))
    )


def test_first_library_slice_has_thirty_unique_active_templates():
    recipes = _first_slice()

    assert len(recipes) == 30
    assert {recipe.id for recipe in recipes} == EXPECTED_FIRST_SLICE_IDS
    assert all(recipe.active for recipe in recipes)


def test_first_library_slice_has_real_slots_and_beginner_complete_steps():
    recipes = _first_slice()

    for recipe in recipes:
        roles = [slot.role for slot in recipe.slots if slot.required]
        assert roles.count("protein") == 1, recipe.id
        assert "vegetable" in roles, recipe.id
        assert len(recipe.instructions) in range(3, 8), recipe.id
        assert all(slot.candidates for slot in recipe.slots), recipe.id
        assert any(len(slot.candidates) > 1 for slot in recipe.slots), recipe.id

        instructions = " ".join(step.text for step in recipe.instructions)
        assert re.search(r"(?:\d+\s*°C|(?:miernom|strednom|silnom) ohni)", instructions), recipe.id
        assert re.search(r"\d+ (?:až \d+ )?minút", instructions), recipe.id
        assert "soľ" in instructions and "čiernym korením" in instructions, recipe.id
        assert "{portions}" in instructions, recipe.id


def test_first_library_slice_has_meaningful_variety_and_high_protein_depth():
    recipes = _first_slice()

    assert {recipe.method for recipe in recipes} == {"pan", "oven", "one_pot"}
    assert len({recipe.family for recipe in recipes}) >= 24
    assert sum("high_protein" in recipe.modes for recipe in recipes) >= 24


def _variant_cases():
    return tuple(
        (recipe.id, candidate_ids)
        for recipe in _first_slice()
        for candidate_ids in product(*(slot.candidates for slot in recipe.slots))
    )


@pytest.mark.parametrize(
    ("recipe_id", "candidate_ids"),
    _variant_cases(),
    ids=lambda value: "+".join(value) if isinstance(value, tuple) else value,
)
def test_first_library_slice_renders_every_variant_safely(
    recipe_id, candidate_ids
):
    ingredients = load_ingredient_catalog()
    recipe = next(recipe for recipe in _first_slice() if recipe.id == recipe_id)

    selections = tuple(
        SlotSelection(
            slot=slot,
            ingredient=ingredients.by_id(candidate_id),
            offer=None,
            pantry=None,
        )
        for slot, candidate_id in zip(recipe.slots, candidate_ids, strict=True)
    )
    meal = render_meal(
        RecipeCandidate(
            template=recipe,
            selections=selections,
            score=Decimal("0"),
            key=f"gate:{recipe.id}:{'+'.join(candidate_ids)}",
        ),
        adults=2,
        children=1,
        covered_days=2,
    )

    assert meal.template_id == recipe.id
    assert len(meal.instructions) == len(recipe.instructions)
    assert all("{" not in step and "}" not in step for step in meal.instructions)
    if "high_protein" in recipe.modes:
        adult_meal = render_meal(
            RecipeCandidate(
                template=recipe,
                selections=selections,
                score=Decimal("0"),
                key=f"gate-adult:{recipe.id}:{'+'.join(candidate_ids)}",
            ),
            adults=1,
            children=0,
            covered_days=1,
        )
        assert adult_meal.nutrition.serving.protein_g >= Decimal("30"), recipe.id
