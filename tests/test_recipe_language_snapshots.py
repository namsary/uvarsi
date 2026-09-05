from dataclasses import replace
import json
from pathlib import Path
import re

import pytest

from app.ingredient_catalog import load_ingredient_catalog
from app.library_gate import audit_library
from app.recipe_catalog import InstructionTemplate, load_recipe_catalog


RECIPE_ROOT = Path(__file__).parents[1] / "app" / "catalog" / "recipes"


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
    recipes = load_recipe_catalog(ingredients, include_inactive=True).all()

    audit = audit_library(ingredients, recipes)

    assert audit.errors == ()


def _all_recipes():
    ingredients = load_ingredient_catalog()
    return load_recipe_catalog(ingredients, include_inactive=True).all()


def _instruction_text(recipe):
    return " ".join(step.text for step in recipe.instructions).lower()


def _recipes_using(ingredient_id):
    return tuple(
        recipe
        for recipe in _all_recipes()
        if any(
            ingredient_id in slot.candidates
            for slot in recipe.slots
        )
    )


@pytest.mark.parametrize(
    ("ingredient_id", "required_phrases"),
    (
        ("chickpeas", ("12 hodín", "60 minút", "kým cícer zmäkne")),
        (
            "beans",
            (
                "12 hodín",
                "silnom ohni 10 minút",
                "kým fazuľa zmäkne",
            ),
        ),
    ),
)
def test_dry_legumes_have_truthful_preparation(ingredient_id, required_phrases):
    recipes = _recipes_using(ingredient_id)

    assert recipes, ingredient_id
    for recipe in recipes:
        text = _instruction_text(recipe)
        for phrase in required_phrases:
            assert phrase in text, f"{recipe.id}: chýba {phrase!r}"


def test_couscous_is_steeped_in_boiling_water_instead_of_boiled():
    recipes = _recipes_using("couscous")

    assert recipes
    for recipe in recipes:
        text = _instruction_text(recipe)
        assert any(
            phrase in text
            for phrase in (
                "vlož {starch.amount} {starch.name}",
                "priprav {starch.amount} {starch.name}",
            )
        ), recipe.id
        assert "{starch.water}" in text, recipe.id
        assert "prikry" in text, recipe.id
        assert "uvar {starch" not in text, recipe.id


def test_every_soup_explicitly_adds_water():
    soups = tuple(recipe for recipe in _all_recipes() if recipe.id.startswith("soup_"))

    assert soups
    for recipe in soups:
        assert "water" in recipe.pantry_basics, recipe.id
        assert any(
            form in _instruction_text(recipe)
            for form in ("voda", "vodu", "vody")
        ), recipe.id


def test_static_recipe_slots_do_not_mix_incompatible_cooking_methods():
    incompatible_groups = (
        frozenset(("rice", "couscous")),
        frozenset(("rice", "barley")),
        frozenset(("red_lentils", "chickpeas")),
        frozenset(("beans", "chickpeas")),
    )

    for recipe in _all_recipes():
        for slot in recipe.slots:
            candidates = frozenset(slot.candidates)
            for group in incompatible_groups:
                assert not group.issubset(candidates), (
                    f"{recipe.id}:{slot.key} mieša suroviny s odlišnou prípravou"
                )


def test_non_cuttable_ingredients_never_inherit_a_cut_instruction():
    non_cuttable = {
        "beans",
        "chickpeas",
        "coconut_milk",
        "cottage_cheese",
        "couscous",
        "egg_noodles",
        "barley",
        "pasta",
        "peas",
        "plain_yogurt",
        "red_lentils",
        "rice",
    }

    for recipe in _all_recipes():
        for slot in recipe.slots:
            if non_cuttable.intersection(slot.candidates):
                assert slot.cut is None, f"{recipe.id}:{slot.key} -> {slot.cut}"


def test_each_ingredient_amount_appears_in_at_most_one_source_step():
    for recipe in _all_recipes():
        text = _instruction_text(recipe)
        for slot in recipe.slots:
            placeholder = "{" + slot.key + ".amount}"
            assert text.count(placeholder) <= 1, (
                f"{recipe.id}: množstvo {slot.key} sa v postupe opakuje"
            )


@pytest.mark.parametrize(
    "bad_phrase",
    ("rybací", "cheesom", "peč {protein.name} so zeleninou a syrom v panvici"),
)
def test_current_library_avoids_known_unnatural_slovak_phrases(bad_phrase):
    offenders = tuple(
        recipe.id
        for recipe in _all_recipes()
        if bad_phrase in (
            recipe.name_template + " " + _instruction_text(recipe)
        ).lower()
    )

    assert offenders == ()


def test_absorption_starches_have_measured_water_outside_soups():
    absorption_starches = {"rice", "barley", "couscous"}

    for recipe in _all_recipes():
        text = _instruction_text(recipe)
        for slot in recipe.slots:
            if not absorption_starches.intersection(slot.candidates):
                continue
            assert "water" in recipe.pantry_basics, recipe.id
            assert any(form in text for form in ("voda", "vodu", "vody")), recipe.id
            if recipe.method != "soup":
                assert f"{{{slot.key}.water}}" in text, recipe.id


def test_every_json_recipe_including_inactive_smoke_avoids_systemic_bad_patterns():
    for path in sorted(RECIPE_ROOT.glob("*.json")):
        if path.name == "manifest.json":
            continue
        payload = json.loads(path.read_text(encoding="utf-8"))
        for recipe in payload["recipes"]:
            text = " ".join(
                (recipe["name_template"], *(step["text"] for step in recipe["instructions"]))
            ).casefold()
            assert "kontrolka" not in text, recipe["id"]
            assert "teplomer" not in text, recipe["id"]
            assert re.search(r"\b74\s*°?\s*c\b", text) is None, recipe["id"]
            for slot in recipe["slots"]:
                if "chicken_thigh" not in slot["candidates"]:
                    continue
                cut = (slot.get("cut") or "").casefold()
                assert re.search(r"\b(?:kock\w*|kociek)\b", cut) is None, recipe["id"]
