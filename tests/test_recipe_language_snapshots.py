from dataclasses import replace

import pytest

from app.ingredient_catalog import load_ingredient_catalog
from app.library_gate import audit_library
from app.recipe_catalog import InstructionTemplate, load_recipe_catalog


def _base_recipe():
    ingredients = load_ingredient_catalog()
    recipe = next(
        recipe
        for recipe in load_recipe_catalog(ingredients).all()
        if recipe.id == "pan_chicken_rice_vegetables"
    )
    return ingredients, recipe


def _replace_step(recipe, index, text):
    instructions = list(recipe.instructions)
    instructions[index] = InstructionTemplate(text)
    return replace(recipe, instructions=tuple(instructions))


@pytest.mark.parametrize("defect", ["SCEĎOK", "reziek", "rezky"])
def test_language_snapshot_rejects_known_czech_or_garbled_terms(defect):
    ingredients, recipe = _base_recipe()
    broken = _replace_step(
        recipe,
        -1,
        recipe.instructions[-1].text + f" {defect}.",
    )

    assert "forbidden_language" in audit_library(ingredients, (broken,)).errors


@pytest.mark.parametrize(
    "defect",
    [
        "Počkaj, kým kontrolka signalizuje nahriatie.",
        "Peč mäso, kým teplomer ukáže 74 °C.",
        "Peč mäso, kým dosiahne 74 °C.",
        "Peč mäso, kým bude zlatisté a bude mať 74 °C.",
    ],
)
def test_language_snapshot_rejects_impractical_kitchen_instructions(defect):
    ingredients, recipe = _base_recipe()
    broken = _replace_step(
        recipe,
        -1,
        recipe.instructions[-1].text + f" {defect}",
    )

    assert "impractical_kitchen_instruction" in audit_library(
        ingredients,
        (broken,),
    ).errors


@pytest.mark.parametrize("cut", ["na kocky", "do kociek"])
def test_library_gate_rejects_cutting_bone_in_chicken_thighs_into_cubes(cut):
    ingredients, recipe = _base_recipe()
    broken_slot = replace(
        recipe.slots[0],
        candidates=("chicken_breast", "chicken_thigh"),
        cut=cut,
    )
    broken = replace(recipe, slots=(broken_slot, *recipe.slots[1:]))

    assert "incompatible_ingredient_cut" in audit_library(
        ingredients,
        (broken,),
    ).errors


@pytest.mark.parametrize("brace", ["{", "}", "{mystery}"])
def test_language_snapshot_rejects_each_unresolved_template_brace(brace):
    ingredients, recipe = _base_recipe()
    broken = _replace_step(
        recipe,
        -1,
        recipe.instructions[-1].text + f" {brace}",
    )

    assert "unresolved_braces" in audit_library(ingredients, (broken,)).errors


@pytest.mark.parametrize("amount", ["12,5 g", "12.5 g"])
def test_language_snapshot_rejects_decimal_grams(amount):
    ingredients, recipe = _base_recipe()
    broken = _replace_step(
        recipe,
        -1,
        recipe.instructions[-1].text + f" Pridaj {amount} soli.",
    )

    assert "decimal_grams" in audit_library(ingredients, (broken,)).errors


def test_language_snapshot_requires_an_explicit_serving_action():
    ingredients, recipe = _base_recipe()
    instructions = tuple(
        InstructionTemplate(
            step.text.replace("a rozdeľ na {portions} porcií", "")
        )
        for step in recipe.instructions
    )

    assert "missing_serving_action" in audit_library(
        ingredients,
        (replace(recipe, instructions=instructions),),
    ).errors


@pytest.mark.parametrize(
    "generic_step",
    ["Priprav suroviny.", "Všetko spolu premiešaj.", "Dochuť podľa chuti."],
)
def test_language_snapshot_rejects_generic_only_step(generic_step):
    ingredients, recipe = _base_recipe()
    instructions = recipe.instructions + (InstructionTemplate(generic_step),)

    assert "generic_only_step" in audit_library(
        ingredients,
        (replace(recipe, instructions=instructions),),
    ).errors


def test_language_snapshot_rejects_undeclared_seasoning():
    ingredients, recipe = _base_recipe()
    assert "oregano" not in recipe.pantry_basics
    broken = _replace_step(
        recipe,
        -1,
        recipe.instructions[-1].text + " Posyp oreganom.",
    )

    assert "undeclared_seasoning" in audit_library(ingredients, (broken,)).errors


def test_current_library_language_snapshots_are_release_safe():
    ingredients = load_ingredient_catalog()
    recipes = load_recipe_catalog(ingredients).all()

    audit = audit_library(ingredients, recipes)

    assert audit.errors == ()
