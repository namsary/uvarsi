"""Generic quality gate for wave-2 curated recipes.

Every target in docs/research/recipe-targets-wave2.json must have a promoted
candidate that validates, matches its research record, renders readable
Slovak steps for one person and for a three-day family batch, and fits the
nutrition bounds of every mode it claims.
"""
from decimal import Decimal, ROUND_CEILING
import json
import re

import pytest

from app.nutrition import estimate_recipe_nutrition, qualifies_high_protein
from app.recipe_renderer import render_meal
from tests.recipe_waves import WAVE1_COUNT, wave2_targets
from tests.test_curated_recipe_snapshots import (
    CANDIDATE_ROOT,
    _assert_legume_workflow,
    _assert_readable_render,
    _assert_released_candidate_validation,
    _declared_ids,
    _load_recipe_with_public_catalog,
    _render_candidate,
    _rendered_edible_grams,
)
from app.ingredient_catalog import load_ingredient_catalog

TARGETS = wave2_targets()
IDS = tuple(row["id"] for row in TARGETS)
ALLOWED_LANES = {"slovak_classic", "modern_family", "high_protein", "plant_based"}
ALLOWED_MODES = {"standard", "high_protein", "vegetarian", "vegan"}
ANIMAL_IDS = {
    "egg", "milk", "cream", "plain_yogurt", "cottage_cheese", "hard_cheese",
    "feta", "bryndza", "butter", "mozzarella", "grilling_cheese", "sour_cream",
    "quark", "honey",
}


@pytest.fixture(scope="module")
def ingredient_catalog():
    return load_ingredient_catalog()


def test_wave2_targets_are_unique_and_well_formed():
    assert len(IDS) == len(set(IDS))
    for row in TARGETS:
        assert re.fullmatch(r"[a-z0-9_]+", row["id"]), row["id"]
        assert row["editorial_lane"] in ALLOWED_LANES, row["id"]
        assert set(row["expected_modes"]) <= ALLOWED_MODES, row["id"]
        assert "standard" in row["expected_modes"], row["id"]
        assert row["display_name"].strip(), row["id"]
        assert row["selection_reason"].strip(), row["id"]
        assert len(row["references"]) >= 2, row["id"]
        for reference in row["references"]:
            assert reference["url"].startswith("https://"), row["id"]
            assert reference["title"].strip(), row["id"]
            assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", reference["accessed_on"])


def test_wave2_does_not_reuse_wave1_ids():
    wave1 = json.loads(
        (CANDIDATE_ROOT.parents[2] / "docs" / "research" / "recipe-targets.json")
        .read_text(encoding="utf-8")
    )["targets"]
    assert len(wave1) == WAVE1_COUNT
    assert {row["id"] for row in wave1}.isdisjoint(IDS)


@pytest.mark.parametrize("recipe_id", IDS, ids=IDS)
def test_wave2_candidate(recipe_id, ingredient_catalog, tmp_path):
    target = next(row for row in TARGETS if row["id"] == recipe_id)
    path = CANDIDATE_ROOT / f"{recipe_id}.json"
    assert path.is_file(), f"{recipe_id}: candidate_missing"
    assert path.with_suffix(".review.json").is_file(), f"{recipe_id}: not promoted"

    _assert_released_candidate_validation(path, recipe_id, ingredient_catalog)
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["source_record"] == {
        "recipe_id": recipe_id,
        "editorial_lane": target["editorial_lane"],
        "core": target["core"],
        "references": target["references"],
    }

    recipe = _load_recipe_with_public_catalog(
        payload, ingredient_catalog, tmp_path / "catalog"
    )
    assert recipe.id == recipe_id
    assert recipe.version == 2 and recipe.active is True
    assert recipe.modes == frozenset(target["expected_modes"])
    assert recipe.storage is not None
    assert all(
        Decimal("0") < slot.child_factor <= Decimal("0.8") for slot in recipe.slots
    )

    declared = _declared_ids(recipe)
    if declared & ANIMAL_IDS:
        assert "vegan" not in recipe.modes, recipe_id
    if "vegan" in recipe.modes:
        assert "vegetarian" in recipe.modes, recipe_id
    _assert_legume_workflow(recipe)

    source_steps = " ".join(step.text for step in recipe.instructions)
    for slot in recipe.slots:
        assert source_steps.count(f"{{{slot.key}.amount}}") <= 1, (
            f"{recipe_id}:{slot.key} repeats its exact amount"
        )
        if "oil" in slot.candidates:
            assert f"{{{slot.key}.amount}}" in source_steps or (
                f"{{{slot.key}.name}}" in source_steps
            ), f"{recipe_id}: oil is declared but never used"

    if recipe.method == "soup":
        assert "water" in recipe.pantry_basics

    candidate = _render_candidate(recipe, ingredient_catalog)
    one = render_meal(candidate, adults=1, children=0, covered_days=1)
    _assert_readable_render(one, portions=1, days=1)
    edible = tuple(
        (item.ingredient, _rendered_edible_grams(item)) for item in one.ingredients
    )
    nutrition = estimate_recipe_nutrition(edible, adult_servings=Decimal("1"))
    assert Decimal("250") <= nutrition.serving.kcal <= Decimal("950"), (
        recipe_id, nutrition.serving.kcal
    )
    assert Decimal("200") <= sum(grams for _, grams in edible) <= Decimal("1100")
    if "high_protein" in recipe.modes:
        assert nutrition.serving.protein_g >= Decimal("30"), recipe_id
        assert qualifies_high_protein(nutrition), recipe_id
    elif recipe.method not in {"soup", "salad"}:
        assert nutrition.serving.protein_g >= Decimal("12"), recipe_id

    family = render_meal(candidate, adults=2, children=1, covered_days=1)
    _assert_readable_render(family, portions=3, days=1)
    for item in family.ingredients:
        if item.ingredient.id == "egg":
            assert item.quantity.amount == item.quantity.amount.to_integral_value()

    if recipe.storage.refrigerated_days >= 3:
        batch = render_meal(candidate, adults=4, children=0, covered_days=3)
        _assert_readable_render(batch, portions=12, days=3)
        for slot, one_item, batch_item in zip(
            recipe.slots, one.ingredients, batch.ingredients, strict=True
        ):
            if batch_item.quantity.unit == "piece":
                expected = (slot.amount_per_adult * 12).to_integral_value(
                    rounding=ROUND_CEILING
                )
            else:
                expected = one_item.quantity.amount * 12
            assert batch_item.quantity.amount == expected
