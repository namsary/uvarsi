from dataclasses import FrozenInstanceError, replace
from datetime import date
from decimal import Decimal
from hashlib import sha256

import pytest

from app.ingredient_catalog import DietTag, IngredientCatalog, load_ingredient_catalog
from app.offer_matcher import MatchedOffer
from app.quantity_math import PackageSize, PantryEntry, Quantity
from app.recipe_catalog import IngredientSlot, InstructionTemplate, RecipeTemplate
from app.recipe_matcher import RecipeCandidate, SlotSelection, rank_candidates


@pytest.fixture
def ingredients():
    return load_ingredient_catalog()


def slot(ingredient_ids, **overrides):
    values = {
        "key": "protein",
        "role": "protein",
        "candidates": tuple(ingredient_ids),
        "amount_per_adult": Decimal("100"),
        "unit": "g",
        "child_factor": Decimal("0.7"),
        "required": True,
        "use": "main",
        "cut": None,
    }
    values.update(overrides)
    return IngredientSlot(**values)


def template(recipe_id, slots, **overrides):
    values = {
        "id": recipe_id,
        "version": 1,
        "active": True,
        "name_template": recipe_id,
        "family": "bowl",
        "method": "pot",
        "minutes": 20,
        "modes": frozenset({"standard"}),
        "equipment": ("hrniec",),
        "slots": tuple(slots),
        "pantry_basics": (),
        "instructions": (
            InstructionTemplate("Priprav suroviny."),
            InstructionTemplate("Uvar jedlo."),
            InstructionTemplate("Podávaj."),
        ),
    }
    values.update(overrides)
    return RecipeTemplate(**values)


def offer(ingredient, **overrides):
    values = {
        "offer_key": f"offer_{ingredient.id}",
        "store": "Lidl",
        "product_name": ingredient.name,
        "ingredient": ingredient,
        "package": PackageSize(Quantity(Decimal("100"), "g")),
        "sale_price": Decimal("2"),
        "original_price": Decimal("4"),
        "valid_from": date(2026, 8, 27),
        "valid_to": date(2026, 9, 2),
        "source_url": "https://example.test/offer",
    }
    values.update(overrides)
    return MatchedOffer(**values)


def test_required_slot_needs_an_offer_or_quantified_pantry(ingredients):
    rice = ingredients.by_id("rice")
    recipe = template(
        "rice-bowl",
        [slot([rice.id], role="starch")],
    )

    assert rank_candidates([recipe], (), (), "standard", "week-1") == ()
    assert len(
        rank_candidates(
            [recipe],
            (),
            [PantryEntry(rice.id, rice.name, Quantity(Decimal("100"), "g"))],
            "standard",
            "week-1",
            ingredient_catalog=ingredients,
        )
    ) == 1
    assert (
        rank_candidates(
            [recipe],
            (),
            [PantryEntry(rice.id, rice.name, None)],
            "standard",
            "week-1",
            ingredient_catalog=ingredients,
        )
        == ()
    )


def test_partial_pantry_without_offer_cannot_satisfy_required_slot(ingredients):
    rice = ingredients.by_id("rice")
    recipe = template(
        "rice-bowl",
        [slot([rice.id], role="starch")],
    )

    candidates = rank_candidates(
        [recipe],
        (),
        [PantryEntry(rice.id, rice.name, Quantity(Decimal("50"), "g"))],
        "standard",
        "week-1",
        ingredient_catalog=ingredients,
    )

    assert candidates == ()


def test_partial_pantry_with_offer_satisfies_required_slot(ingredients):
    rice = ingredients.by_id("rice")
    recipe = template(
        "rice-bowl",
        [slot([rice.id], role="starch")],
    )

    candidate = rank_candidates(
        [recipe],
        [offer(rice)],
        [PantryEntry(rice.id, rice.name, Quantity(Decimal("50"), "g"))],
        "standard",
        "week-1",
    )[0]

    assert candidate.selections[0].pantry == Quantity(Decimal("50"), "g")
    assert candidate.selections[0].offer is not None


def test_pantry_balance_is_allocated_once_across_slots(ingredients):
    rice = ingredients.by_id("rice")
    recipe = template(
        "double-rice",
        [
            slot([rice.id], key="first", role="starch"),
            slot([rice.id], key="second", role="starch"),
        ],
    )

    candidate = rank_candidates(
        [recipe],
        [offer(rice)],
        [PantryEntry(rice.id, rice.name, Quantity(Decimal("150"), "g"))],
        "standard",
        "week-1",
    )[0]

    assert [selection.pantry for selection in candidate.selections] == [
        Quantity(Decimal("100"), "g"),
        Quantity(Decimal("50"), "g"),
    ]
    assert [selection.offer is not None for selection in candidate.selections] == [
        False,
        True,
    ]


def test_optional_slot_before_required_cannot_consume_reserved_pantry(ingredients):
    rice = ingredients.by_id("rice")
    recipe = template(
        "optional-before-required",
        [
            slot(
                [rice.id],
                key="optional",
                role="starch",
                required=False,
                use="addition",
            ),
            slot([rice.id], key="required", role="starch"),
        ],
    )

    candidate = rank_candidates(
        [recipe],
        (),
        [PantryEntry(rice.id, rice.name, Quantity(Decimal("100"), "g"))],
        "standard",
        "week-1",
        ingredient_catalog=ingredients,
    )[0]

    assert [selection.slot.key for selection in candidate.selections] == [
        "required"
    ]
    assert candidate.selections[0].pantry == Quantity(Decimal("100"), "g")


def test_offered_alternative_preserves_pantry_for_required_slot(ingredients):
    rice = ingredients.by_id("rice")
    pasta = ingredients.by_id("pasta")
    recipe = template(
        "offered-alternative",
        [
            slot([rice.id, pasta.id], key="flexible", role="starch"),
            slot([rice.id], key="rice_only", role="starch"),
        ],
    )

    candidate = rank_candidates(
        [recipe],
        [offer(pasta)],
        [PantryEntry(rice.id, rice.name, Quantity(Decimal("100"), "g"))],
        "standard",
        "week-1",
        ingredient_catalog=ingredients,
    )[0]

    assert [selection.ingredient.id for selection in candidate.selections] == [
        pasta.id,
        rice.id,
    ]
    assert candidate.selections[0].offer is not None
    assert candidate.selections[1].pantry == Quantity(Decimal("100"), "g")


def test_required_reservations_do_not_double_use_or_reorder_pantry(ingredients):
    rice = ingredients.by_id("rice")
    recipe = template(
        "required-around-optional",
        [
            slot([rice.id], key="first", role="starch"),
            slot(
                [rice.id],
                key="optional",
                role="starch",
                required=False,
                use="addition",
            ),
            slot([rice.id], key="last", role="starch"),
        ],
    )

    candidate = rank_candidates(
        [recipe],
        [offer(rice)],
        [PantryEntry(rice.id, rice.name, Quantity(Decimal("150"), "g"))],
        "standard",
        "week-1",
    )[0]

    assert [selection.slot.key for selection in candidate.selections] == [
        "first",
        "optional",
        "last",
    ]
    pantry_by_slot = {
        selection.slot.key: selection.pantry
        for selection in candidate.selections
    }
    assert pantry_by_slot == {
        "first": Quantity(Decimal("100"), "g"),
        "optional": None,
        "last": Quantity(Decimal("50"), "g"),
    }
    assert sum(
        (
            selection.pantry.amount
            for selection in candidate.selections
            if selection.pantry is not None
        ),
        Decimal("0"),
    ) == Decimal("150")


def test_optional_slot_is_omitted_when_it_has_no_source(ingredients):
    rice = ingredients.by_id("rice")
    tofu = ingredients.by_id("tofu")
    recipe = template(
        "optional-tofu",
        [
            slot([rice.id], role="starch"),
            slot(
                [tofu.id],
                key="extra",
                required=False,
                use="addition",
            ),
        ],
    )

    candidates = rank_candidates(
        [recipe], [offer(rice)], (), "standard", "week-1"
    )

    assert [selection.slot.key for selection in candidates[0].selections] == [
        "protein"
    ]


def test_vegan_mode_never_selects_animal_ingredient(ingredients):
    chicken = ingredients.by_id("chicken_breast")
    tofu = ingredients.by_id("tofu")
    recipe = template(
        "vegan-bowl",
        [slot([chicken.id, tofu.id])],
        modes=frozenset({"vegan"}),
    )

    candidates = rank_candidates(
        [recipe],
        [
            offer(chicken, sale_price=Decimal("1")),
            offer(tofu, sale_price=Decimal("3")),
        ],
        (),
        "vegan",
        "week-1",
    )

    assert candidates
    assert all(
        DietTag.VEGAN in selection.ingredient.diet_tags
        for candidate in candidates
        for selection in candidate.selections
    )


def test_vegetarian_mode_rejects_meat_only_template(ingredients):
    chicken = ingredients.by_id("chicken_breast")
    recipe = template(
        "chicken-bowl",
        [slot([chicken.id])],
        modes=frozenset({"vegetarian"}),
    )

    assert (
        rank_candidates(
            [recipe], [offer(chicken)], (), "vegetarian", "week-1"
        )
        == ()
    )


def test_score_uses_normalized_saving_coverage_store_and_leftover(ingredients):
    rice = ingredients.by_id("rice")
    exact = template("exact", [slot([rice.id], role="starch")])
    leftover = template("leftover", [slot([rice.id], role="starch")])

    exact_candidate = rank_candidates(
        [exact], [offer(rice)], (), "standard", "week-1"
    )[0]
    leftover_candidate = rank_candidates(
        [
            leftover,
        ],
        [
            offer(
                rice,
                package=PackageSize(Quantity(Decimal("200"), "g")),
            )
        ],
        (),
        "standard",
        "week-1",
    )[0]

    assert exact_candidate.score == Decimal("43")
    assert leftover_candidate.score == Decimal("40")


def test_weight_priced_offer_is_not_penalized_as_a_whole_package(ingredients):
    rice = ingredients.by_id("rice")
    recipe = template(
        "weighted-rice",
        [slot([rice.id], role="starch", amount_per_adult=Decimal("180"))],
    )
    weighted = offer(
        rice,
        offer_key="weighted",
        package=PackageSize(Quantity(Decimal("1000"), "g")),
        sale_price=Decimal("5"),
        original_price=Decimal("10"),
        pricing_basis="weight",
    )
    fixed = offer(
        rice,
        offer_key="fixed",
        package=PackageSize(Quantity(Decimal("500"), "g")),
        sale_price=Decimal("2.50"),
        original_price=Decimal("5"),
    )

    candidate = rank_candidates(
        [recipe], [fixed, weighted], (), "standard", "week-1"
    )[0]

    assert candidate.selections[0].offer.offer_key == "weighted"


def test_equal_score_offer_tie_uses_actual_required_cost(ingredients):
    rice = ingredients.by_id("rice")
    recipe = template(
        "weighted-rice-tie",
        [slot([rice.id], role="starch", amount_per_adult=Decimal("180"))],
    )
    weighted = offer(
        rice,
        offer_key="weighted",
        package=PackageSize(Quantity(Decimal("1000"), "g")),
        sale_price=Decimal("5"),
        original_price=Decimal("10"),
        pricing_basis="weight",
    )
    fixed = offer(
        rice,
        offer_key="fixed",
        package=PackageSize(Quantity(Decimal("180"), "g")),
        sale_price=Decimal("1.50"),
        original_price=Decimal("3"),
    )

    candidate = rank_candidates(
        [recipe], [fixed, weighted], (), "standard", "week-1"
    )[0]

    assert candidate.selections[0].offer.offer_key == "weighted"


def test_recent_family_and_method_apply_exact_score_penalties(ingredients):
    rice = ingredients.by_id("rice")
    recipe = template("recent", [slot([rice.id], role="starch")])
    offers = [offer(rice)]

    baseline = rank_candidates([recipe], offers, (), "standard", "week-1")[0]
    recent_family = rank_candidates(
        [recipe],
        offers,
        (),
        "standard",
        "week-1",
        recent_families=("bowl",),
    )[0]
    recent_method = rank_candidates(
        [recipe],
        offers,
        (),
        "standard",
        "week-1",
        recent_methods=("pot",),
    )[0]
    both = rank_candidates(
        [recipe],
        offers,
        (),
        "standard",
        "week-1",
        recent_families=("bowl",),
        recent_methods=("pot",),
    )[0]

    assert baseline.score == Decimal("43")
    assert recent_family.score == Decimal("25")
    assert recent_method.score == Decimal("31")
    assert both.score == Decimal("13")


def test_same_seed_and_input_produce_same_hash_order(ingredients):
    rice = ingredients.by_id("rice")
    recipes = [
        template("alpha", [slot([rice.id], role="starch")]),
        template("beta", [slot([rice.id], role="starch")]),
    ]
    offers = [offer(rice)]

    first = rank_candidates(recipes, offers, (), "standard", "abc")
    second = rank_candidates(recipes, offers, (), "standard", "abc")
    expected_keys = sorted(
        sha256(f"abc:{item.id}:offer_rice".encode()).hexdigest()
        for item in recipes
    )

    assert [candidate.key for candidate in first] == expected_keys
    assert [candidate.key for candidate in second] == expected_keys


def test_high_protein_mode_discards_selection_below_thirty_grams(ingredients):
    rice = ingredients.by_id("rice")
    chicken = ingredients.by_id("chicken_breast")
    low = template(
        "low-protein",
        [slot([rice.id], role="starch")],
        modes=frozenset({"high_protein"}),
    )
    enough = template(
        "enough-protein",
        [slot([chicken.id], amount_per_adult=Decimal("150"))],
        modes=frozenset({"high_protein"}),
    )

    candidates = rank_candidates(
        [low, enough],
        [offer(rice), offer(chicken)],
        (),
        "high_protein",
        "week-1",
    )

    assert [candidate.template.id for candidate in candidates] == [
        "enough-protein"
    ]


def test_package_conversion_fails_closed_without_ingredient_metadata(ingredients):
    rice = ingredients.by_id("rice")
    recipe = template(
        "rice-by-volume",
        [slot([rice.id], role="starch")],
    )
    incompatible = offer(
        rice,
        package=PackageSize(Quantity(Decimal("250"), "ml")),
    )

    assert (
        rank_candidates([recipe], [incompatible], (), "standard", "week-1")
        == ()
    )


def test_pantry_only_resolution_is_isolated_to_caller_catalog(ingredients):
    caller_rice = replace(
        ingredients.by_id("rice"),
        name="ryža volajúceho",
        synonyms=(),
    )
    caller_catalog = IngredientCatalog((caller_rice,))
    recipe = template(
        "caller-rice",
        [slot([caller_rice.id], role="starch")],
    )
    pantry = [
        PantryEntry(
            caller_rice.id,
            caller_rice.name,
            Quantity(Decimal("100"), "g"),
        )
    ]

    assert rank_candidates([recipe], (), pantry, "standard", "week-1") == ()
    candidate = rank_candidates(
        [recipe],
        (),
        pantry,
        "standard",
        "week-1",
        ingredient_catalog=caller_catalog,
    )[0]

    assert candidate.selections[0].ingredient is caller_rice


def test_public_results_are_immutable(ingredients):
    rice = ingredients.by_id("rice")
    recipe = template("rice", [slot([rice.id], role="starch")])
    candidate = rank_candidates(
        [recipe], [offer(rice)], (), "standard", "week-1"
    )[0]

    assert isinstance(candidate, RecipeCandidate)
    assert isinstance(candidate.selections[0], SlotSelection)
    with pytest.raises(FrozenInstanceError):
        candidate.score = Decimal("0")


def test_cached_offer_choice_tracks_changed_price_values(ingredients):
    rice = ingredients.by_id("rice")
    recipe = template("price-cache-key", [slot([rice.id], role="starch")])
    original = offer(rice, sale_price=Decimal("2"))

    first = rank_candidates(
        [recipe], [original], (), "standard", "price-cache-key"
    )[0]
    second = rank_candidates(
        [recipe],
        [replace(original, sale_price=Decimal("1"))],
        (),
        "standard",
        "price-cache-key",
    )[0]

    assert second.score > first.score
