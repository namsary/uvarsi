from decimal import Decimal

import pytest

from app.recipe_workflow import workflow_errors
from app.recipe_catalog import (
    IngredientSlot,
    InstructionTemplate,
    RecipeTemplate,
    StorageRule,
)


def _step(*, requires=(), produces=()):
    return InstructionTemplate(
        "Kuchynský krok.",
        requires=tuple(requires),
        produces=tuple(produces),
    )


def _v2_recipe(*, candidate="chicken_breast", transitions=(), required=True):
    slot = IngredientSlot(
        key="protein",
        role="protein",
        candidates=(candidate,),
        amount_per_adult=Decimal("150"),
        unit="g",
        child_factor=Decimal("0.6"),
        required=required,
        use="main",
        cut=None,
    )
    return RecipeTemplate(
        id="workflow-fixture",
        version=2,
        active=True,
        name_template="Jedlo",
        family="workflow_fixture",
        method="pot",
        minutes=30,
        modes=frozenset({"standard"}),
        equipment=("pot",),
        slots=(slot,),
        pantry_basics=(),
        instructions=tuple(
            _step(requires=requires, produces=produces)
            for requires, produces in transitions
        ),
        storage=StorageRule(
            refrigerated_days=2,
            instruction="Po vychladnutí odlož do chladničky a zjedz do 2 dní.",
        ),
    )


def test_workflow_rejects_cutting_bone_in_thigh():
    recipe = _v2_recipe(
        candidate="chicken_thigh",
        transitions=[(("protein:raw",), ("protein:cut",))],
    )

    assert "incompatible_ingredient_transition" in workflow_errors(recipe)


def test_workflow_allows_cutting_boneless_thigh_meat():
    recipe = _v2_recipe(
        candidate="chicken_thigh_meat",
        transitions=[
            (("protein:raw",), ("protein:cut",)),
            (("protein:cut",), ("protein:served",)),
        ],
    )

    assert workflow_errors(recipe) == ()


def test_workflow_rejects_prepared_ingredient_never_served():
    recipe = _v2_recipe(
        transitions=[(("protein:raw",), ("protein:prepared",))],
    )

    assert "workflow_unserved_ingredient:protein" in workflow_errors(recipe)


def test_workflow_rejects_second_use_of_occupied_pot():
    recipe = _v2_recipe(
        transitions=[
            (("pot:free",), ("pot:occupied",)),
            (("pot:free",), ("protein:cooked",)),
            (("protein:cooked",), ("protein:served",)),
        ],
    )

    assert "workflow_unmet_requirement:pot:free" in workflow_errors(recipe)


@pytest.mark.parametrize("token", ["protein", "protein:", ":raw", "protein:raw:extra"])
def test_workflow_strictly_rejects_malformed_tokens(token):
    recipe = _v2_recipe(transitions=[((token,), ("protein:served",))])

    with pytest.raises(ValueError, match="token"):
        workflow_errors(recipe)


def test_workflow_strictly_rejects_unknown_resources():
    recipe = _v2_recipe(
        transitions=[(("mystery:raw",), ("protein:served",))],
    )

    with pytest.raises(ValueError, match="mystery"):
        workflow_errors(recipe)
