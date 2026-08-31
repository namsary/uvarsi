import re
from dataclasses import replace
from decimal import Decimal
from itertools import product

import pytest

import app.library_gate as library_gate
from app.ingredient_catalog import load_ingredient_catalog
from app.library_gate import audit_library, main
from app.recipe_catalog import InstructionTemplate, load_recipe_catalog
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

EXPECTED_VEGETARIAN_IDS = {
    "veg_egg_rice_vegetables",
    "veg_egg_tomato_pasta",
    "veg_cottage_potato_spinach",
    "veg_cottage_rice_zucchini",
    "veg_feta_couscous_vegetables",
    "veg_cheese_broccoli_pasta",
    "veg_mushroom_barley_pan",
    "veg_lentil_tomato_pasta",
    "veg_chickpea_spinach_rice",
    "veg_bean_potato_stew",
}
EXPECTED_VEGAN_IDS = {
    "vegan_tofu_rice_vegetables",
    "vegan_tofu_pasta_tomato",
    "vegan_lentil_rice_curry",
    "vegan_lentil_bolognese_pasta",
    "vegan_chickpea_couscous_salad",
    "vegan_chickpea_tomato_stew",
    "vegan_bean_chili_rice",
    "vegan_bean_potato_goulash",
    "vegan_pea_potato_pan",
    "vegan_mushroom_barley_pot",
}
EXPECTED_SOUP_SALAD_IDS = {
    "soup_chicken_vegetable_noodle",
    "soup_beef_vegetable_barley",
    "soup_fish_tomato_potato",
    "soup_red_lentil_tomato",
    "soup_chickpea_vegetable",
    "salad_chicken_potato_yogurt",
    "salad_tuna_bean_tomato",
    "salad_egg_pasta_vegetable",
    "salad_tofu_rice_vegetable",
    "salad_chickpea_couscous_vegetable",
}
EXPECTED_SECOND_SLICE_IDS = (
    EXPECTED_VEGETARIAN_IDS | EXPECTED_VEGAN_IDS | EXPECTED_SOUP_SALAD_IDS
)


def _first_slice():
    ingredients = load_ingredient_catalog()
    return tuple(
        recipe
        for recipe in load_recipe_catalog(ingredients).all()
        if recipe.id.startswith(("pan_", "oven_", "pot_"))
    )


def _second_slice():
    ingredients = load_ingredient_catalog()
    return tuple(
        recipe
        for recipe in load_recipe_catalog(ingredients).all()
        if recipe.id in EXPECTED_SECOND_SLICE_IDS
    )


def _active_library():
    ingredients = load_ingredient_catalog()
    return load_recipe_catalog(ingredients).all()


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


def test_second_library_slice_has_exactly_thirty_expected_active_templates():
    recipes = _second_slice()

    assert len(recipes) == 30
    assert {recipe.id for recipe in recipes} == EXPECTED_SECOND_SLICE_IDS
    assert len({recipe.id for recipe in recipes}) == 30
    assert all(recipe.active for recipe in recipes)


def test_launch_library_meets_mode_method_and_family_floors():
    recipes = _active_library()
    floors = {
        "standard": 50,
        "high_protein": 24,
        "vegetarian": 20,
        "vegan": 12,
    }

    assert len(recipes) >= 60
    for mode, floor in floors.items():
        eligible = tuple(recipe for recipe in recipes if mode in recipe.modes)
        assert len(eligible) >= floor, mode
        assert len({recipe.method for recipe in eligible}) >= 3, mode
        assert len({recipe.family for recipe in eligible}) >= 3, mode


def test_second_slice_is_distinct_beginner_complete_and_correctly_classified():
    recipes = _second_slice()

    assert len({recipe.family for recipe in recipes}) == 30
    for recipe in recipes:
        instructions = " ".join(step.text for step in recipe.instructions)
        assert len(recipe.instructions) in range(3, 8), recipe.id
        assert re.search(
            r"(?:\d+\s*°C|(?:miernom|strednom|silnom) ohni)", instructions
        ), recipe.id
        assert re.search(r"\d+ (?:až \d+ )?minút", instructions), recipe.id
        assert "soľ" in instructions and "čiernym korením" in instructions, recipe.id
        assert "{portions}" in instructions, recipe.id
        assert all(slot.candidates for slot in recipe.slots), recipe.id

        if recipe.id in EXPECTED_VEGETARIAN_IDS:
            assert "vegetarian" in recipe.modes, recipe.id
        if recipe.id in EXPECTED_VEGAN_IDS:
            assert {"vegetarian", "vegan"} <= recipe.modes, recipe.id


def test_second_slice_has_no_cosmetic_method_and_ingredient_duplicates():
    signatures = [
        (
            recipe.method,
            tuple(
                sorted(
                    (slot.role, tuple(sorted(slot.candidates)))
                    for slot in recipe.slots
                )
            ),
        )
        for recipe in _second_slice()
    ]

    assert len(signatures) == len(set(signatures))


def test_soups_are_batch_safe_and_salads_are_complete_main_meals():
    recipes = {recipe.id: recipe for recipe in _second_slice()}

    for recipe_id in EXPECTED_SOUP_SALAD_IDS:
        recipe = recipes[recipe_id]
        roles = {slot.role for slot in recipe.slots if slot.required}
        instructions = " ".join(step.text for step in recipe.instructions).lower()
        if recipe_id.startswith("soup_"):
            assert recipe.method == "soup"
            assert "chladničke" in instructions and re.search(r"\b(?:dni|dní)\b", instructions)
        else:
            assert recipe.method == "salad"
            assert {"protein", "starch", "vegetable"} <= roles


def _second_variant_cases():
    return tuple(
        (recipe.id, candidate_ids)
        for recipe in _second_slice()
        for candidate_ids in product(*(slot.candidates for slot in recipe.slots))
    )


@pytest.mark.parametrize(
    ("recipe_id", "candidate_ids"),
    _second_variant_cases(),
    ids=lambda value: "+".join(value) if isinstance(value, tuple) else value,
)
def test_second_library_slice_renders_every_real_variant(recipe_id, candidate_ids):
    ingredients = load_ingredient_catalog()
    recipe = next(recipe for recipe in _second_slice() if recipe.id == recipe_id)
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
            key=f"launch-gate:{recipe.id}:{'+'.join(candidate_ids)}",
        ),
        adults=2,
        children=1,
        covered_days=3,
    )

    assert meal.template_id == recipe.id
    assert len(meal.instructions) == len(recipe.instructions)
    assert all("{" not in step and "}" not in step for step in meal.instructions)


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


def test_audit_rejects_duplicate_family_disguised_as_new_recipe():
    ingredients = load_ingredient_catalog()
    base = _active_library()[0]
    recipes = tuple(
        replace(base, id=f"{base.id}_copy_{index}")
        for index in range(3)
    )

    audit = audit_library(ingredients, recipes)

    assert "duplicate_fingerprint" in audit.errors


def test_audit_rejects_unverified_high_protein_template():
    ingredients = load_ingredient_catalog()
    base = next(recipe for recipe in _active_library() if "high_protein" in recipe.modes)
    slots = tuple(
        replace(slot, amount_per_adult=Decimal("1"))
        if slot.role == "protein"
        else slot
        for slot in base.slots
    )

    audit = audit_library(ingredients, (replace(base, slots=slots),))

    assert "high_protein_below_30g" in audit.errors


def test_audit_reports_stable_coverage_for_the_same_library():
    ingredients = load_ingredient_catalog()
    recipes = _active_library()

    first = audit_library(ingredients, recipes)
    second = audit_library(ingredients, tuple(reversed(recipes)))

    assert first.coverage_lines() == second.coverage_lines()
    assert first.coverage_lines()[0] == "recipes.active=60"
    assert first.coverage_lines()[-1] == "errors=0"


def test_cli_prints_stable_coverage_and_returns_success(capsys):
    exit_code = main()

    assert exit_code == 0
    output = capsys.readouterr().out.splitlines()
    assert output[0] == "recipes.active=60"
    assert "modes.high_protein=35" in output
    assert "methods.pan=15" in output
    assert output[-1] == "errors=0"


def test_cli_fails_closed_when_catalog_loading_fails(monkeypatch, capsys):
    def fail_to_load(*args, **kwargs):
        raise ValueError("poškodený katalóg")

    monkeypatch.setattr(library_gate, "load_recipe_catalog", fail_to_load)

    assert main() == 1
    assert capsys.readouterr().out.splitlines() == [
        "recipes.active=0",
        "error.catalog_load=1",
        "errors=1",
    ]


MODE_FLOORS = {
    "standard": 50,
    "high_protein": 24,
    "vegetarian": 20,
    "vegan": 12,
}


@pytest.mark.parametrize(("mode", "floor"), MODE_FLOORS.items())
def test_audit_enforces_hard_recipe_floor_for_every_mode(mode, floor):
    ingredients = load_ingredient_catalog()
    recipes = list(_active_library())
    eligible_indexes = [
        index for index, recipe in enumerate(recipes) if mode in recipe.modes
    ]
    replacement_mode = "vegetarian" if mode == "standard" else "standard"
    for index in eligible_indexes[floor - 1 :]:
        remaining = recipes[index].modes - {mode}
        recipes[index] = replace(
            recipes[index],
            modes=remaining or frozenset({replacement_mode}),
        )

    audit = audit_library(ingredients, recipes)

    assert f"mode_{mode}_below_{floor}" in audit.errors


def test_audit_enforces_hard_total_recipe_floor():
    ingredients = load_ingredient_catalog()

    audit = audit_library(ingredients, _active_library()[:59])

    assert "total_below_60" in audit.errors


@pytest.mark.parametrize("mode", MODE_FLOORS)
def test_audit_requires_three_families_and_methods_per_mode(mode):
    ingredients = load_ingredient_catalog()
    recipes = _active_library()
    one_family = tuple(
        replace(recipe, family="single_family")
        if mode in recipe.modes
        else recipe
        for recipe in recipes
    )
    one_method = tuple(
        replace(recipe, method="pan") if mode in recipe.modes else recipe
        for recipe in recipes
    )

    family_audit = audit_library(ingredients, one_family)
    method_audit = audit_library(ingredients, one_method)

    assert f"mode_{mode}_families_below_3" in family_audit.errors
    assert f"mode_{mode}_methods_below_3" in method_audit.errors


def test_public_audit_excludes_each_partially_malformed_recipe_without_crashing():
    ingredients = load_ingredient_catalog()
    recipes = _active_library()
    base = recipes[0]
    malformed = (
        object(),
        replace(base, id=None),
        replace(base, modes=None),
        replace(base, slots=(object(),)),
        replace(
            base,
            id="duplicate_slot_shape",
            slots=(*base.slots, base.slots[0]),
        ),
        replace(
            base,
            id="duplicate_candidate_shape",
            slots=(
                replace(
                    base.slots[0],
                    candidates=(
                        base.slots[0].candidates[0],
                        base.slots[0].candidates[0],
                    ),
                ),
                *base.slots[1:],
            ),
        ),
        replace(
            base,
            id="invalid_unit_shape",
            slots=(replace(base.slots[0], unit="bucket"), *base.slots[1:]),
        ),
        replace(
            base,
            id="invalid_slot_key_shape",
            slots=(replace(base.slots[0], key=None), *base.slots[1:]),
        ),
        replace(
            base,
            id="unhashable_candidate_shape",
            slots=(
                replace(base.slots[0], candidates=(["chicken_breast"],)),
                *base.slots[1:],
            ),
        ),
    )
    baseline = audit_library(ingredients, recipes)

    audit = audit_library(ingredients, (*recipes, *malformed))

    assert audit.active_recipes == baseline.active_recipes == 60
    assert audit.mode_counts == baseline.mode_counts
    assert audit.method_counts == baseline.method_counts
    assert audit.family_counts == baseline.family_counts
    assert audit.errors == ("invalid_recipe",)


def test_public_audit_fails_closed_when_recipe_iterable_cannot_start():
    ingredients = load_ingredient_catalog()

    class ExplodingRecipes:
        def __iter__(self):
            raise RuntimeError("broken recipe source")

    audit = audit_library(ingredients, ExplodingRecipes())

    assert audit.active_recipes == 0
    assert "invalid_recipe" in audit.errors


def test_duplicate_fingerprint_cannot_be_bypassed_with_seasoning_keywords():
    ingredients = load_ingredient_catalog()
    base = next(
        recipe
        for recipe in _active_library()
        if recipe.id == "pan_chicken_rice_vegetables"
    )
    cosmetic_variants = (
        ("cesnakom", "garlic"),
        ("oreganom", "oregano"),
        ("mletou paprikou", "paprika_powder"),
    )
    recipes = tuple(
        replace(
            base,
            id=f"cosmetic_{index}",
            name_template=f"{base.name_template} s {wording}",
            pantry_basics=tuple(dict.fromkeys((*base.pantry_basics, seasoning_id))),
            instructions=(
                *base.instructions,
                InstructionTemplate(f"Posyp jedlo {wording}."),
            ),
        )
        for index, (wording, seasoning_id) in enumerate(cosmetic_variants)
    )

    audit = audit_library(ingredients, recipes)

    assert "duplicate_fingerprint" in audit.errors


def test_duplicate_fingerprint_accepts_distinct_processes_with_same_action_sequence():
    ingredients = load_ingredient_catalog()
    base = next(
        recipe
        for recipe in _active_library()
        if recipe.id == "pan_chicken_rice_vegetables"
    )
    recipes = tuple(
        replace(
            base,
            id=f"same_sequence_{index}",
            family="shared_pan_process",
            name_template=name,
            instructions=(
                InstructionTemplate(
                    "Pridaj {vegetable.name} ({vegetable.amount}) a 120 ml vody."
                ),
                InstructionTemplate(process_step),
                InstructionTemplate(
                    "Rozdeľ jedlo na {portions} porcií a podávaj ho teplé."
                ),
            ),
        )
        for index, (name, process_step) in enumerate(
            (
                (
                    "Kuracie v redukovanej zeleninovej šťave",
                    "Dus 18 minút odkryté, kým sa zelenina rozvarí "
                    "na hustú lesklú šťavu.",
                ),
                (
                    "Kuracie v zamatovej krémovej omáčke",
                    "Dus 18 minút na miernom ohni, kým vznikne "
                    "jemná krémová omáčka.",
                ),
                (
                    "Kuracie na pomaly dusenom základe",
                    "Dus 18 minút pod pokrievkou, kým kúsky zmäknú "
                    "a vytvoria šťavnatý základ.",
                ),
            )
        )
    )

    audit = audit_library(ingredients, recipes)

    assert "duplicate_fingerprint" not in audit.errors


def test_duplicate_fingerprint_rejects_cosmetic_names_and_minor_wording():
    ingredients = load_ingredient_catalog()
    base = next(
        recipe
        for recipe in _active_library()
        if recipe.id == "pan_chicken_rice_vegetables"
    )
    recipes = tuple(
        replace(
            base,
            id=f"minor_wording_{index}",
            name_template=f"{label} {base.name_template}",
            instructions=(
                InstructionTemplate(
                    f"{base.instructions[0].text.rstrip('.')} {adverb}."
                ),
                *base.instructions[1:],
            ),
        )
        for index, (label, adverb) in enumerate(
            (
                ("Kuracie ragú", "opatrne"),
                ("Kuracie soté", "jemne"),
                ("Kurací pilaf", "dôkladne"),
            )
        )
    )

    audit = audit_library(ingredients, recipes)

    assert "duplicate_fingerprint" in audit.errors


def test_duplicate_fingerprint_normalizes_substitutions_quantities_and_seasoning():
    ingredients = load_ingredient_catalog()
    base = next(
        recipe
        for recipe in _active_library()
        if recipe.id == "pan_chicken_rice_vegetables"
    )
    variants = (
        (
            "Kuracie ragú",
            "{vegetable.name} ({vegetable.amount})",
            "cesnak",
            "garlic",
            "12 minút",
        ),
        ("Kuracie soté", "mrkvu (250 g)", "oregano", "oregano", "18 minút"),
        (
            "Kurací pilaf",
            "cuketu (400 g)",
            "mletú papriku",
            "paprika_powder",
            "24 minút",
        ),
    )
    recipes = tuple(
        replace(
            base,
            id=f"normalized_cosmetic_{index}",
            name_template=name,
            pantry_basics=tuple(
                dict.fromkeys((*base.pantry_basics, seasoning_id))
            ),
            instructions=(
                InstructionTemplate(
                    f"Pridaj {ingredient_text} a {seasoning_text}."
                ),
                InstructionTemplate(
                    f"Dus {duration}, kým zmes zhustne na omáčku."
                ),
                InstructionTemplate(
                    "Rozdeľ jedlo na {portions} porcií a podávaj ho teplé."
                ),
            ),
        )
        for index, (
            name,
            ingredient_text,
            seasoning_text,
            seasoning_id,
            duration,
        ) in enumerate(variants)
    )

    audit = audit_library(ingredients, recipes)

    assert "duplicate_fingerprint" in audit.errors


def _recipe_with_candidate_counts(candidate_counts):
    base = next(
        recipe
        for recipe in _active_library()
        if recipe.id == "pan_chicken_rice_vegetables"
    )
    slots = tuple(
        replace(
            base.slots[0],
            key=f"slot_{slot_index}",
            candidates=tuple(
                f"candidate_{slot_index}_{candidate_index}"
                for candidate_index in range(candidate_count)
            ),
        )
        for slot_index, candidate_count in enumerate(candidate_counts)
    )
    return replace(base, slots=slots)


@pytest.mark.parametrize(
    ("candidate_counts", "expected"),
    (
        ((1, 1, 1, 1, 1), 1),
        ((4,), 4),
        ((3, 3, 3, 3), 81),
    ),
)
def test_variant_bound_accepts_any_shape_at_or_below_81(
    candidate_counts,
    expected,
    monkeypatch,
):
    recipe = _recipe_with_candidate_counts(candidate_counts)
    product_calls = []

    def bounded_product(*candidate_groups):
        product_calls.append(tuple(len(group) for group in candidate_groups))
        return ()

    monkeypatch.setattr(library_gate, "product", bounded_product)
    errors = set()

    assert library_gate._bounded_variant_count(recipe) == expected
    library_gate._audit_recipe(load_ingredient_catalog(), recipe, errors)
    assert product_calls == [candidate_counts]
    assert "variant_limit_exceeded" not in errors


def test_variant_bound_rejects_82_or_more_combinations(monkeypatch):
    recipe = _recipe_with_candidate_counts((82,))

    def unbounded_product_must_not_run(*candidate_groups):
        raise AssertionError("Cartesian product above 81 was reached")

    monkeypatch.setattr(library_gate, "product", unbounded_product_must_not_run)
    errors = set()

    assert library_gate._bounded_variant_count(recipe) is None
    library_gate._audit_recipe(load_ingredient_catalog(), recipe, errors)
    assert errors == {"variant_limit_exceeded"}
