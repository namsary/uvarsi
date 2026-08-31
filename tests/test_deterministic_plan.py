from dataclasses import replace
from decimal import Decimal

import pytest

from app.deterministic_plan import NoCompatiblePlan, build_deterministic_plan
from app.ingredient_catalog import IngredientCatalog, load_ingredient_catalog
from app.quantity_math import PantryEntry, Quantity
from app.recipe_catalog import (
    IngredientSlot,
    InstructionTemplate,
    RecipeCatalog,
    RecipeTemplate,
)


WEEK = "2026-08-31"
RICE_STEPS = (
    "Prepláchni {main.amount} {main.name} v jemnom sitku pod studenou vodou, "
    "kým odtekajúca voda nebude takmer číra.",
    "Vlož {main.amount} {main.name} do hrnca, prilej 450 ml vody, pridaj "
    "štipku soli a obsah hrnca priveď na silnom ohni za 5 minút do varu, "
    "kým voda nezačne súvislo bublať.",
    "Var {main.amount} {main.name} v prikrytom hrnci 12 minút na miernom "
    "ohni, kým sa voda vsiakne.",
    "Odstav hrniec a nechaj ryžu prikrytú 5 minút dôjsť, kým budú zrná "
    "mäkké a oddelené.",
    "Rozdeľ ryžu na {portions} porcie a podávaj ju horúcu.",
)
TOFU_STEPS = (
    "Osuš {main.amount} {main.name} čistou utierkou, kým povrch nebude suchý.",
    "Nakrájaj {main.amount} {main.name} {main.cut} na doske.",
    "Rozohrej v panvici 1 polievkovú lyžicu oleja 2 minúty na strednom ohni, "
    "kým sa olej začne ľahko lesknúť.",
    "Opekaj {main.amount} {main.name} v panvici 8 minút na strednom ohni, "
    "kým budú všetky strany zlatisté a chrumkavé.",
    "Rozdeľ tofu na {portions} porcie a podávaj ho ihneď.",
)


def _offer(
    ingredient_name="Basmati ryža Golden Sun",
    *,
    offer_key="offer_rice",
    package="500 g",
    sale="1.79",
    original="2.49",
    store="Lidl",
):
    return {
        "offer_key": offer_key,
        "obchod": store,
        "nazov": ingredient_name,
        "jednotka": package,
        "cena": sale,
        "povodna": original,
        "valid_from": WEEK,
        "valid_to": "2026-09-06",
        "source_url": f"https://example.test/{offer_key}",
    }


def _template(
    recipe_id,
    *,
    ingredient_id="rice",
    role="starch",
    amount="75",
    child_factor="0.5",
    family=None,
    method="pot",
    mode="standard",
    cut=None,
    name="Absorpčne varená {main.name}",
    steps=RICE_STEPS,
):
    slot = IngredientSlot(
        key="main",
        role=role,
        candidates=(ingredient_id,),
        amount_per_adult=Decimal(amount),
        unit="g",
        child_factor=Decimal(child_factor),
        required=True,
        use="main",
        cut=cut,
    )
    return RecipeTemplate(
        id=recipe_id,
        version=1,
        active=True,
        name_template=name,
        family=family or f"family-{recipe_id}",
        method=method,
        minutes=30,
        modes=frozenset({mode}),
        equipment=("hrniec",),
        slots=(slot,),
        pantry_basics=("water", "salt") if ingredient_id == "rice" else ("oil",),
        instructions=tuple(InstructionTemplate(step) for step in steps),
    )


def _rice_recipes(version=7):
    return RecipeCatalog(
        version,
        (
            _template("rice-pot", method="pot"),
            _template("rice-pan", method="pan"),
            _template("rice-oven", method="oven"),
        ),
    )


def _build(*, frequency=3, pantry=(), pantry_driven=False, **overrides):
    values = {
        "week": WEEK,
        "rows": (_offer(),),
        "stores": ("Lidl",),
        "adults": 1,
        "children": 0,
        "frequency": frequency,
        "pantry": pantry,
        "pantry_driven": pantry_driven,
        "mode": "standard",
        "seed": "fixture-seed",
        "ingredient_catalog": load_ingredient_catalog(),
        "recipe_catalog": _rice_recipes(),
    }
    values.update(overrides)
    return build_deterministic_plan(**values)


@pytest.mark.parametrize(
    ("frequency", "days", "coverage"),
    (
        (1, ["PO", "UT", "ST", "ŠT", "PI", "SO", "NE"], [1, 1, 1, 1, 1, 1, 1]),
        (2, ["PO", "ST", "PI", "NE"], [2, 2, 2, 1]),
        (3, ["PO", "ŠT", "NE"], [3, 3, 1]),
    ),
)
def test_plan_covers_exactly_seven_days(frequency, days, coverage):
    plan = _build(frequency=frequency)

    assert [meal["den"] for meal in plan["jedla"]] == days
    assert [meal["pokryva_dni"] for meal in plan["jedla"]] == coverage
    assert sum(meal["pokryva_dni"] for meal in plan["jedla"]) == 7


def test_public_plan_is_deterministic_and_prices_whole_packages():
    first = _build()
    second = _build()

    assert first == second
    assert first["tyzden"] == WEEK
    assert first["nakup_spolu"] == "3,58"
    assert first["bezna_cena"] == "4,98"
    assert first["usetrene"] == "1,40"
    assert first["meta"] == {
        "engine": "deterministic",
        "library_version": 7,
        "mode": "standard",
    }
    assert len(first["nakupny_zoznam"]) == 1
    assert first["nakupny_zoznam"][0]["polozky"][0]["mnozstvo"] == 2


def test_week_uses_three_methods_and_never_repeats_adjacent_family_and_method():
    plan = _build(frequency=1)
    recipes = [meal["recept"] for meal in plan["jedla"]]

    assert len({recipe["method"] for recipe in recipes}) >= 3
    assert all(
        (left["family"], left["method"])
        != (right["family"], right["method"])
        for left, right in zip(recipes, recipes[1:])
    )


def test_regular_selection_ignores_pantry_but_personal_shopping_subtracts_it():
    baseline = _build()
    with_pantry = _build(
        pantry=(PantryEntry("rice", "ryža", Quantity(Decimal("300"), "g")),),
    )

    assert [meal["recept"]["template_id"] for meal in with_pantry["jedla"]] == [
        meal["recept"]["template_id"] for meal in baseline["jedla"]
    ]
    assert baseline["nakup_spolu"] == "3,58"
    assert with_pantry["nakup_spolu"] == "1,79"
    assert with_pantry["nakupny_zoznam"][0]["polozky"][0]["mnozstvo"] == 1


def test_pantry_driven_plan_can_fill_slots_without_an_offer_and_buys_nothing():
    plan = _build(
        rows=(),
        pantry=(PantryEntry("rice", "ryža", Quantity(Decimal("600"), "g")),),
        pantry_driven=True,
    )

    assert sum(meal["pokryva_dni"] for meal in plan["jedla"]) == 7
    assert plan["nakupny_zoznam"] == []
    assert (plan["nakup_spolu"], plan["bezna_cena"], plan["usetrene"]) == (
        "0,00",
        "0,00",
        "0,00",
    )


def test_meal_ingredients_preserve_established_offer_and_pantry_shapes():
    offer_plan = _build()
    bought = offer_plan["jedla"][0]["suroviny"][0]

    assert bought == {
        "offer_key": "offer_rice",
        "nazov": "Basmati ryža Golden Sun",
        "obchod": "Lidl",
        "jednotka": "500 g",
        "mnozstvo": 1,
        "davka": "230 g",
        "cena": "1,79",
        "povodna": "2,49",
        "source_url": "https://example.test/offer_rice",
        "valid_from": WEEK,
        "valid_to": "2026-09-06",
    }

    pantry_plan = _build(
        rows=(),
        pantry=(PantryEntry("rice", "ryža", Quantity(Decimal("600"), "g")),),
        pantry_driven=True,
    )

    assert pantry_plan["jedla"][0]["suroviny"][0] == {"spajza": "ryža"}


def test_search_never_considers_candidate_thirteen(monkeypatch):
    from app import deterministic_plan, recipe_matcher

    ingredients = load_ingredient_catalog()
    offers = deterministic_plan.match_offers((_offer(),), ingredients)
    first_twelve = tuple(
        _template(f"same-{index}", family="same", method="pot")
        for index in range(12)
    )
    thirteenth = _template("escape", family="escape", method="oven")
    ranked = recipe_matcher.rank_candidates(
        (*first_twelve, thirteenth),
        offers,
        (),
        "standard",
        "fixed",
        ingredient_catalog=ingredients,
    )
    by_id = {candidate.template.id: candidate for candidate in ranked}
    forced_order = tuple(by_id[item.id] for item in first_twelve) + (by_id["escape"],)

    monkeypatch.setattr(
        deterministic_plan,
        "rank_candidates",
        lambda *args, **kwargs: forced_order,
    )

    with pytest.raises(NoCompatiblePlan):
        _build(
            frequency=3,
            ingredient_catalog=ingredients,
            recipe_catalog=RecipeCatalog(1, (*first_twelve, thirteenth)),
        )


@pytest.mark.parametrize(
    ("overrides", "code"),
    (
        ({"rows": (), "pantry": (), "pantry_driven": False}, "insufficient_offers"),
        (
            {"rows": (_offer(package="rodinné balenie"),)},
            "unmeasurable_packages",
        ),
        (
            {
                "mode": "vegan",
                "recipe_catalog": _rice_recipes(),
            },
            "diet_too_strict",
        ),
    ),
)
def test_impossible_plans_raise_typed_honest_errors(overrides, code):
    with pytest.raises(NoCompatiblePlan) as captured:
        _build(**overrides)

    assert captured.value.code == code
    assert captured.value.suggestions


def _protein_setup(*, protein="17.27", fat="8.72", child_factor="0.5"):
    default = load_ingredient_catalog().by_id("tofu")
    tofu = replace(
        default,
        nutrition=replace(
            default.nutrition,
            protein_g=Decimal(protein),
            fat_g=Decimal(fat),
        ),
    )
    ingredients = IngredientCatalog((tofu,))
    recipes = RecipeCatalog(
        11,
        tuple(
            _template(
                f"tofu-{method}",
                ingredient_id="tofu",
                role="protein",
                amount="180",
                child_factor=child_factor,
                method=method,
                mode="high_protein",
                cut="na 2 cm kocky",
                name="Chrumkavé {main.name} z panvice",
                steps=TOFU_STEPS,
            )
            for method in ("pot", "pan", "oven")
        ),
    )
    return ingredients, recipes


def test_high_protein_claim_is_added_only_after_the_legal_energy_gate():
    ingredients, recipes = _protein_setup()
    claimed = _build(
        rows=(_offer("Pevné tofu", offer_key="offer_tofu"),),
        mode="high_protein",
        ingredient_catalog=ingredients,
        recipe_catalog=recipes,
    )
    high_fat_ingredients, high_fat_recipes = _protein_setup(fat="100")
    estimated_only = _build(
        rows=(_offer("Pevné tofu", offer_key="offer_tofu"),),
        mode="high_protein",
        ingredient_catalog=high_fat_ingredients,
        recipe_catalog=high_fat_recipes,
    )

    assert all(
        Decimal(meal["recept"]["nutrition"]["serving"]["protein_g"])
        >= Decimal("30")
        for meal in claimed["jedla"]
    )
    assert all(meal["recept"]["high_protein_claim"] is True for meal in claimed["jedla"])
    assert all(
        "high_protein_claim" not in meal["recept"]
        for meal in estimated_only["jedla"]
    )


def test_high_protein_final_gate_runs_after_household_rendering():
    ingredients, recipes = _protein_setup(child_factor="0.1")

    with pytest.raises(NoCompatiblePlan) as captured:
        _build(
            rows=(_offer("Pevné tofu", offer_key="offer_tofu"),),
            adults=1,
            children=1,
            mode="high_protein",
            ingredient_catalog=ingredients,
            recipe_catalog=recipes,
        )

    assert captured.value.code == "diet_too_strict"
