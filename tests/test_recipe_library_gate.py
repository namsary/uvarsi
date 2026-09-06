import json
from collections import Counter
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

import app.library_gate as library_gate
from app.ingredient_catalog import load_ingredient_catalog
from app.library_gate import audit_library, main
from app.recipe_catalog import (
    InstructionTemplate,
    RecipeCatalog,
    StorageRule,
    load_recipe_catalog,
)
from app.recipe_provenance import load_recipe_provenance
from app.recipe_workflow import workflow_errors


PROJECT_ROOT = Path(__file__).resolve().parents[1]
TARGETS_PATH = PROJECT_ROOT / "docs" / "research" / "recipe-targets.json"
PROVENANCE_PATH = PROJECT_ROOT / "app" / "catalog" / "recipe_sources.json"
EXPECTED_ACTIVE_RECIPES = 104
EXPECTED_EDITORIAL_LANES = {
    "slovak_classic": 42,
    "modern_family": 36,
    "high_protein": 16,
    "plant_based": 10,
}
MODE_FLOORS = {
    "standard": 104,
    "high_protein": 24,
    "vegetarian": 24,
    "vegan": 16,
}
MINIMUM_MODE_FAMILIES = 3
MINIMUM_MODE_METHODS = 3


def _target_rows():
    payload = json.loads(TARGETS_PATH.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 1
    return tuple(payload["targets"])


def _target_map():
    return {row["id"]: row for row in _target_rows()}


def _target_ids():
    return frozenset(_target_map())


def _active_library():
    ingredients = load_ingredient_catalog()
    return load_recipe_catalog(ingredients).all()


def _representative_recipe():
    return next(recipe for recipe in _active_library() if recipe.slots)


def _curated_catalog(recipes):
    return RecipeCatalog(5, tuple(recipes), curation_generation=1)


def _with_valid_v2_workflow(recipe):
    required_keys = tuple(slot.key for slot in recipe.slots if slot.required)
    instructions = list(recipe.instructions)
    instructions[0] = replace(
        instructions[0],
        requires=tuple(f"{key}:raw" for key in required_keys),
        produces=tuple(f"{key}:prepared" for key in required_keys),
    )
    instructions[-1] = replace(
        instructions[-1],
        requires=tuple(f"{key}:prepared" for key in required_keys),
        produces=tuple(f"{key}:served" for key in required_keys),
    )
    return replace(
        recipe,
        version=2,
        instructions=tuple(instructions),
        storage=StorageRule(
            refrigerated_days=3,
            instruction=(
                "Po vychladnutí odlož do chladničky a zjedz do 3 dní."
            ),
        ),
    )


def test_audit_merges_workflow_errors_for_active_version_2_recipe():
    ingredients = load_ingredient_catalog()
    recipes = list(_active_library())
    recipes[0] = replace(
        _with_valid_v2_workflow(recipes[0]),
        instructions=tuple(
            replace(step, produces=())
            if index == len(recipes[0].instructions) - 1
            else step
            for index, step in enumerate(
                _with_valid_v2_workflow(recipes[0]).instructions
            )
        ),
    )

    audit = audit_library(ingredients, _curated_catalog(recipes))

    assert any(
        error.startswith("workflow_unserved_ingredient:")
        for error in audit.errors
    )


def test_audit_rejects_active_v1_recipe_only_in_curated_generation_one():
    ingredients = load_ingredient_catalog()
    recipes = list(_active_library())
    recipes[0] = replace(recipes[0], version=1, storage=None)

    dormant = audit_library(
        ingredients,
        RecipeCatalog(3, tuple(recipes), curation_generation=0),
    )
    curated = audit_library(
        ingredients,
        RecipeCatalog(4, tuple(recipes), curation_generation=1),
    )

    assert "legacy_recipe_active" not in dormant.errors
    assert "legacy_recipe_active" in curated.errors


def test_curated_generation_one_has_exact_active_inventory():
    ingredients = load_ingredient_catalog()
    catalog = load_recipe_catalog(ingredients)
    recipes = catalog.all()

    assert catalog.curation_generation == 1
    assert len(recipes) == EXPECTED_ACTIVE_RECIPES
    assert all(recipe.active and recipe.version >= 2 for recipe in recipes)
    assert {recipe.id for recipe in recipes} == _target_ids()


def test_curated_generation_one_modes_match_the_frozen_editorial_inventory():
    targets = _target_map()
    recipes = {recipe.id: recipe for recipe in _active_library()}

    assert set(recipes) == set(targets)
    assert {
        recipe_id: recipe.modes for recipe_id, recipe in recipes.items()
    } == {
        recipe_id: frozenset(row["expected_modes"])
        for recipe_id, row in targets.items()
    }


def test_curated_generation_one_has_complete_exact_provenance():
    targets = _target_map()
    payload = json.loads(PROVENANCE_PATH.read_text(encoding="utf-8"))
    source_rows = payload["recipes"]

    assert payload["schema_version"] == 1
    assert {row["recipe_id"] for row in source_rows} == set(targets)

    provenance = load_recipe_provenance(targets, PROVENANCE_PATH)
    for recipe_id, target in targets.items():
        record = provenance[recipe_id]
        assert record.editorial_lane == target["editorial_lane"], recipe_id
        assert record.core is target["core"], recipe_id
        assert [
            (reference.url, reference.title, reference.accessed_on.isoformat())
            for reference in record.references
        ] == [
            (reference["url"], reference["title"], reference["accessed_on"])
            for reference in target["references"]
        ], recipe_id


def test_curated_generation_one_meets_editorial_diet_family_and_method_floors():
    targets = _target_map()
    recipes = tuple(_active_library())
    recipes_by_id = {recipe.id: recipe for recipe in recipes}

    assert set(recipes_by_id) == set(targets)
    assert Counter(
        target["editorial_lane"] for target in targets.values()
    ) == Counter(EXPECTED_EDITORIAL_LANES)

    for mode, floor in MODE_FLOORS.items():
        eligible = tuple(recipe for recipe in recipes if mode in recipe.modes)
        assert len(eligible) >= floor, mode
        assert len({recipe.family for recipe in eligible}) >= MINIMUM_MODE_FAMILIES, mode
        assert len({recipe.method for recipe in eligible}) >= MINIMUM_MODE_METHODS, mode


def test_curated_generation_one_has_no_workflow_errors():
    recipes = tuple(_active_library())

    assert len(recipes) == EXPECTED_ACTIVE_RECIPES
    assert all(recipe.version >= 2 for recipe in recipes)
    failures = {
        recipe.id: workflow_errors(recipe)
        for recipe in recipes
        if workflow_errors(recipe)
    }

    assert failures == {}


def test_curated_generation_one_audit_has_no_errors_or_duplicate_fingerprints():
    ingredients = load_ingredient_catalog()
    catalog = load_recipe_catalog(ingredients)

    assert catalog.curation_generation == 1
    audit = audit_library(ingredients, catalog)
    assert "duplicate_fingerprint" not in audit.errors
    assert audit.errors == ()


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
    assert first.coverage_lines()[0] == "recipes.active=104"
    assert first.coverage_lines()[-1] == "errors=0"


def test_cli_prints_stable_coverage_and_returns_success(capsys):
    exit_code = main()

    assert exit_code == 0
    output = capsys.readouterr().out.splitlines()
    assert output[0] == "recipes.active=104"
    assert "modes.standard=104" in output
    assert "modes.high_protein=58" in output
    assert "modes.vegetarian=46" in output
    assert "modes.vegan=16" in output
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

    audit = audit_library(ingredients, _curated_catalog(recipes))

    assert f"mode_{mode}_below_{floor}" in audit.errors


def test_audit_enforces_hard_total_recipe_floor():
    ingredients = load_ingredient_catalog()

    audit = audit_library(
        ingredients,
        _curated_catalog(_active_library()[: EXPECTED_ACTIVE_RECIPES - 1]),
    )

    assert "total_below_104" in audit.errors


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

    assert audit.active_recipes == baseline.active_recipes == EXPECTED_ACTIVE_RECIPES
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
    base = _representative_recipe()
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
    base = _representative_recipe()
    slot_key = base.slots[0].key
    recipes = tuple(
        replace(
            base,
            id=f"same_sequence_{index}",
            family="shared_pan_process",
            name_template=name,
            instructions=(
                InstructionTemplate(
                    f"Pridaj {{{slot_key}.name}} ({{{slot_key}.amount}}) a 120 ml vody."
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
    base = _representative_recipe()
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
    base = _representative_recipe()
    slot_key = base.slots[0].key
    variants = (
        (
            "Kuracie ragú",
            f"{{{slot_key}.name}} ({{{slot_key}.amount}})",
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
    base = _representative_recipe()
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
    return replace(base, version=1, storage=None, slots=slots)


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
