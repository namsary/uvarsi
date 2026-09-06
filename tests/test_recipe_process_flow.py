import json
from pathlib import Path

import pytest


RECIPE_ROOT = Path(__file__).parents[1] / "app" / "catalog" / "recipes"


def _recipe(recipe_id):
    for filename in ("03-one-pot.json", "05-vegan.json"):
        payload = json.loads((RECIPE_ROOT / filename).read_text(encoding="utf-8"))
        for recipe in payload["recipes"]:
            if recipe["id"] == recipe_id:
                return recipe
    raise AssertionError(f"Recipe {recipe_id!r} was not found")


def _step_index(steps, *required_fragments, after=-1):
    for index, step in enumerate(steps):
        if index > after and all(fragment in step for fragment in required_fragments):
            return index
    raise AssertionError(
        f"No step after {after} contains all fragments {required_fragments!r}: {steps!r}"
    )


@pytest.mark.parametrize(
    "recipe_id",
    ("pot_bean_chili_rice", "vegan_bean_chili_rice"),
)
def test_canned_beans_move_from_preparation_to_incorporation_to_cooking(recipe_id):
    steps = [step["text"] for step in _recipe(recipe_id)["instructions"]]

    preparation = _step_index(steps, "Sceď", "{protein.amount}", "{protein.name}")
    incorporation = _step_index(
        steps, "Pridaj", "{protein.name}", after=preparation
    )
    cooking = _step_index(steps, "Var", "{protein.name}", after=incorporation)

    assert preparation < incorporation < cooking


def test_lentil_curry_prepares_each_ingredient_before_incorporation_and_cooking():
    steps = [
        step["text"] for step in _recipe("pot_red_lentil_curry_rice")["instructions"]
    ]

    pulse_preparation = _step_index(
        steps, "Prepláchni", "{protein.amount}", "{protein.name}"
    )
    starch_preparation = _step_index(
        steps, "Prepláchni", "{starch.amount}", "{starch.name}"
    )
    dry_ingredients_incorporation = _step_index(
        steps, "Vlož", "{protein.name}", "{starch.name}"
    )
    dry_ingredients_cooking = _step_index(
        steps,
        "Var",
        "{protein.name}",
        "ryž",
        after=dry_ingredients_incorporation,
    )
    vegetable_preparation = _step_index(
        steps, "Nakrájaj", "{vegetable.amount}", "{vegetable.name}"
    )
    vegetable_incorporation = _step_index(
        steps, "Pridaj", "{vegetable.name}", after=vegetable_preparation
    )

    assert pulse_preparation < dry_ingredients_incorporation < dry_ingredients_cooking
    assert starch_preparation < dry_ingredients_incorporation < dry_ingredients_cooking
    assert all(
        "{vegetable.name}" not in step
        for step in steps[:vegetable_preparation]
    )
    assert vegetable_preparation < vegetable_incorporation
    assert "var" in steps[vegetable_incorporation].casefold()


def test_lentil_curry_hydrates_both_rice_and_dry_lentils():
    steps = [
        step["text"] for step in _recipe("pot_red_lentil_curry_rice")["instructions"]
    ]
    dry_ingredients_incorporation = _step_index(
        steps, "Vlož", "{protein.name}", "{starch.name}"
    )

    assert "{starch.water}" in steps[dry_ingredients_incorporation]
    assert "{protein.water}" in steps[dry_ingredients_incorporation]


def test_couscous_water_leaves_the_pot_before_vegetables_occupy_it():
    recipe = _recipe("pot_chickpea_tomato_couscous")
    steps = [step["text"] for step in recipe["instructions"]]

    couscous_hydration = _step_index(
        steps, "{starch.water}", "v hrnci", "do misky"
    )
    vegetable_pot_cooking = _step_index(
        steps, "Pridaj", "{vegetable.name}", "hrnca"
    )

    assert couscous_hydration < vegetable_pot_cooking
    assert {"hrniec", "miska", "sitko"} <= set(recipe["equipment"])
