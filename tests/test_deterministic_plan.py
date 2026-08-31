import json
import os
from pathlib import Path
import subprocess
import sys
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
        "zlava": "-28 %",
        "valid_from": WEEK,
        "valid_to": "2026-09-06",
        "source_url": f"https://example.test/{offer_key}",
        "source_page": 4,
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


def _rice_recipes(version=7, *, child_factor="0.5"):
    return RecipeCatalog(
        version,
        (
            _template("rice-pot", method="pot", child_factor=child_factor),
            _template("rice-pan", method="pan", child_factor=child_factor),
            _template("rice-oven", method="oven", child_factor=child_factor),
        ),
    )


def _rice_with_optional_recipes(
    *,
    optional_ingredient_id="tofu",
    optional_amount="20",
    optional_child_factor="0.25",
):
    templates = []
    for method in ("pot", "pan", "oven"):
        steps = RICE_STEPS
        if optional_ingredient_id == "tofu":
            steps = (
                *RICE_STEPS[:-1],
                "Pridaj tofu do hrnca a premiešaj.",
                RICE_STEPS[-1],
            )
        base = _template(
            f"rice-{method}-optional",
            method=method,
            steps=steps,
        )
        optional = IngredientSlot(
            key="extra",
            role="addition",
            candidates=(optional_ingredient_id,),
            amount_per_adult=Decimal(optional_amount),
            unit="g",
            child_factor=Decimal(optional_child_factor),
            required=False,
            use="addition",
            cut=None,
        )
        templates.append(replace(base, slots=(*base.slots, optional)))
    return RecipeCatalog(12, tuple(templates))


def _heterogeneous_reserve_recipes():
    small = _template(
        "small-optional",
        amount="10",
        child_factor="0.25",
        family="small-family",
        method="pot",
    )
    optional = IngredientSlot(
        key="extra",
        role="addition",
        candidates=("rice",),
        amount_per_adult=Decimal("10"),
        unit="g",
        child_factor=Decimal("0.25"),
        required=False,
        use="addition",
        cut=None,
    )
    large = _template(
        "large-future",
        amount="100",
        child_factor="1",
        family="large-family",
        method="pan",
    )
    return RecipeCatalog(13, (replace(small, slots=(*small.slots, optional)), large))


def _rank_boundary_reserve_recipes():
    generic_steps = (
        "Osuš {main.amount} {main.name} utierkou, kým povrch nebude suchý.",
        "Nakrájaj {main.amount} {main.name} {main.cut} na doske.",
        "Opekaj {main.amount} {main.name} na panvici 8 minút na strednom "
        "ohni, kým bude povrch zlatistý.",
        "Rozdeľ {main.name} na {portions} porcie a podávaj ho teplé.",
    )
    decoys = []
    for index in range(11):
        decoy = _template(
            f"decoy-{index}",
            ingredient_id="chicken_breast",
            role="protein",
            amount="10",
            child_factor="0",
            family="decoy-family",
            method="pot",
            cut="na kocky",
            name="Opečené {main.name}",
            steps=generic_steps,
        )
        absent = tuple(
            IngredientSlot(
                key=f"decoy-{index}-absent-{slot_index}",
                role="vegetable",
                candidates=("broccoli",),
                amount_per_adult=Decimal("10"),
                unit="g",
                child_factor=Decimal("0"),
                required=False,
                use="addition",
                cut=None,
            )
            for slot_index in range(17)
        )
        decoys.append(replace(decoy, slots=(*decoy.slots, *absent)))
    safe = _template(
        "safe-alternate",
        ingredient_id="chicken_breast",
        role="protein",
        amount="10",
        child_factor="0",
        family="decoy-family",
        method="pot",
        cut="na kocky",
        name="Pečené {main.name}",
        steps=generic_steps,
    )
    absent_optional = tuple(
        IngredientSlot(
            key=f"absent-{index}",
            role="vegetable",
            candidates=("broccoli",),
            amount_per_adult=Decimal("10"),
            unit="g",
            child_factor=Decimal("0"),
            required=False,
            use="addition",
            cut=None,
        )
        for index in range(26)
    )
    safe = replace(safe, slots=(*safe.slots, *absent_optional))

    mover = _template(
        "optional-rank-mover",
        amount="100",
        child_factor="0",
        family="mover-family",
        method="pan",
    )
    mover_optional = tuple(
        IngredientSlot(
            key=f"extra-{index}",
            role="addition",
            candidates=("rice",),
            amount_per_adult=Decimal("10"),
            unit="g",
            child_factor=Decimal("0"),
            required=False,
            use="addition",
            cut=None,
        )
        for index in range(39)
    )
    mover = replace(mover, slots=(*mover.slots, *mover_optional))
    return RecipeCatalog(15, (*decoys, safe, mover))


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


def test_serialized_plan_is_identical_across_python_processes():
    script = (
        "import json, runpy; "
        "namespace = runpy.run_path('tests/test_deterministic_plan.py'); "
        "print(json.dumps(namespace['_build'](), ensure_ascii=True, "
        "sort_keys=True, separators=(',', ':')))"
    )
    outputs = []
    for hash_seed in ("1", "987654"):
        environment = dict(os.environ)
        environment["PYTHONHASHSEED"] = hash_seed
        completed = subprocess.run(
            [sys.executable, "-c", script],
            cwd=Path(__file__).resolve().parents[1],
            env=environment,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        outputs.append(completed.stdout)

    assert outputs[0] == outputs[1]


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


@pytest.mark.parametrize(
    ("adults", "children", "child_factor", "exact_weekly_rice"),
    (
        (1, 1, "0.5", "787.5"),
        (2, 1, "0.25", "1181.25"),
    ),
)
def test_pantry_driven_sizing_uses_each_slots_exact_child_factor(
    adults, children, child_factor, exact_weekly_rice
):
    plan = _build(
        rows=(),
        adults=adults,
        children=children,
        pantry=(
            PantryEntry(
                "rice",
                "ryža",
                Quantity(Decimal(exact_weekly_rice), "g"),
            ),
        ),
        pantry_driven=True,
        recipe_catalog=_rice_recipes(child_factor=child_factor),
    )

    assert sum(meal["pokryva_dni"] for meal in plan["jedla"]) == 7
    assert plan["nakupny_zoznam"] == []


def test_pantry_driven_exposes_optional_only_pantry_to_candidate_selection():
    plan = _build(
        adults=1,
        children=1,
        pantry=(
            PantryEntry("tofu", "tofu", Quantity(Decimal("175"), "g")),
        ),
        pantry_driven=True,
        recipe_catalog=_rice_with_optional_recipes(),
    )

    assert all(
        {"spajza": "tofu"} in meal["suroviny"] for meal in plan["jedla"]
    )


def test_optional_personal_pantry_does_not_affect_shareable_ranking():
    recipes = _rice_with_optional_recipes()
    rows = (
        _offer(),
        _offer("Pevné tofu", offer_key="offer_tofu"),
    )
    baseline = _build(rows=rows, recipe_catalog=recipes)
    with_personal_pantry = _build(
        rows=rows,
        pantry=(
            PantryEntry("tofu", "tofu", Quantity(Decimal("175"), "g")),
        ),
        pantry_driven=False,
        recipe_catalog=recipes,
    )

    assert with_personal_pantry["jedla"] == baseline["jedla"]
    assert all(
        any(
            ingredient.get("offer_key") == "offer_tofu"
            for ingredient in meal["suroviny"]
        )
        for meal in with_personal_pantry["jedla"]
    )


def test_optional_pantry_uses_exact_factor_without_spending_required_reserve():
    recipes = _rice_with_optional_recipes(optional_ingredient_id="rice")
    required_only = _build(
        rows=(),
        adults=1,
        children=1,
        pantry=(
            PantryEntry("rice", "ryža", Quantity(Decimal("787.5"), "g")),
        ),
        pantry_driven=True,
        recipe_catalog=recipes,
    )
    exactly_enriched = _build(
        rows=(),
        adults=1,
        children=1,
        pantry=(
            PantryEntry("rice", "ryža", Quantity(Decimal("962.5"), "g")),
        ),
        pantry_driven=True,
        recipe_catalog=recipes,
    )

    assert all(
        meal["suroviny"].count({"spajza": "ryža"}) == 1
        for meal in required_only["jedla"]
    )
    assert all(
        meal["suroviny"].count({"spajza": "ryža"}) == 2
        for meal in exactly_enriched["jedla"]
    )
    assert required_only["nakupny_zoznam"] == []
    assert exactly_enriched["nakupny_zoznam"] == []


def test_future_candidate_maximum_reserve_withholds_current_optional_pantry():
    plan = _build(
        rows=(),
        adults=1,
        children=1,
        pantry=(
            PantryEntry("rice", "ryža", Quantity(Decimal("650"), "g")),
        ),
        pantry_driven=True,
        recipe_catalog=_heterogeneous_reserve_recipes(),
    )

    assert [
        meal["recept"]["template_id"] for meal in plan["jedla"]
    ] == ["small-optional", "large-future", "small-optional"]
    assert plan["jedla"][0]["suroviny"].count({"spajza": "ryža"}) == 1
    assert plan["nakupny_zoznam"] == []
    assert sum(meal["pokryva_dni"] for meal in plan["jedla"]) == 7


def test_future_reserve_ignores_unrenderable_and_out_of_mode_candidates():
    safe = _rice_with_optional_recipes(optional_ingredient_id="rice")
    unrenderable = _template(
        "huge-unrenderable",
        amount="1000",
        child_factor="1",
        family="huge-family",
        method="pressure",
        steps=(
            "Uvar {main.amount} {main.name}.",
            "Premiešaj {main.amount} {main.name}.",
            "Podávaj {main.amount} {main.name}.",
        ),
    )
    out_of_mode = replace(
        unrenderable,
        id="huge-out-of-mode",
        modes=frozenset({"vegan"}),
    )
    recipes = RecipeCatalog(14, (*safe.all(), unrenderable, out_of_mode))

    plan = _build(
        adults=1,
        children=1,
        pantry=(
            PantryEntry("rice", "ryža", Quantity(Decimal("962.5"), "g")),
        ),
        pantry_driven=True,
        recipe_catalog=recipes,
    )

    assert all(
        meal["suroviny"].count({"spajza": "ryža"}) == 2
        for meal in plan["jedla"]
    )


def test_stabilized_reserve_aligns_rank_boundary_and_prevents_future_starvation():
    from app import deterministic_plan

    ingredients = load_ingredient_catalog()
    recipes = _rank_boundary_reserve_recipes()
    rows = ()
    pantry = (
        PantryEntry("rice", "ryža", Quantity(Decimal("760"), "g")),
        PantryEntry(
            "chicken_breast",
            "kuracie prsia",
            Quantity(Decimal("30"), "g"),
        ),
    )
    offers = tuple(deterministic_plan.match_offers(rows, ingredients))
    balances = deterministic_plan._pantry_balances(pantry, ingredients)
    rank_arguments = {
        "templates": recipes.all(),
        "offers": offers,
        "balances": balances,
        "pantry_driven": True,
        "mode": "standard",
        "seed": f"fixture-seed:{WEEK}:PO",
        "ingredient_catalog": ingredients,
        "adults": 1,
        "children": 0,
        "covered_days": 2,
        "recent_families": (),
        "recent_methods": (),
    }
    discovery = deterministic_plan._rank_for_day(
        **rank_arguments,
        required_reserve=balances,
    )
    exposed = deterministic_plan._rank_for_day(
        **rank_arguments,
        required_reserve={},
    )

    # Eleven pantry-backed decoys score exactly 1 / 18 * 14. The safe filler
    # scores 1 / 27 * 14. The mover scores 1 / 40 * 14 = 0.35 when optional
    # pantry is suppressed. With no rice reserve, 38 optional slots fit beside
    # the required slot, so its exposed score is 39 / 40 * 14 = 13.65.
    assert all(
        candidate.score == Decimal("0.7777777777777777777777777778")
        for candidate in discovery[:11]
    )
    assert (discovery[11].template.id, discovery[11].score) == (
        "safe-alternate",
        Decimal("0.5185185185185185185185185185"),
    )
    assert (discovery[12].template.id, discovery[12].score) == (
        "optional-rank-mover",
        Decimal("0.35"),
    )
    assert (exposed[0].template.id, exposed[0].score) == (
        "optional-rank-mover",
        Decimal("13.650"),
    )
    assert exposed[12].template.id == "safe-alternate"
    assert len(
        [
            deterministic_plan.render_meal(
                candidate,
                adults=1,
                children=0,
                covered_days=2,
            )
            for candidate in discovery
        ]
    ) == 13

    plan = _build(
        rows=rows,
        adults=1,
        children=0,
        frequency=2,
        pantry=pantry,
        pantry_driven=True,
        recipe_catalog=recipes,
    )

    template_ids = [
        meal["recept"]["template_id"] for meal in plan["jedla"]
    ]
    assert template_ids[::2] == ["optional-rank-mover"] * 2
    assert all(
        template_id.startswith("decoy-")
        for template_id in template_ids[1::2]
    )
    assert "safe-alternate" not in template_ids
    assert sum(meal["pokryva_dni"] for meal in plan["jedla"]) == 7
    assert plan["nakupny_zoznam"] == []

    stabilized = deterministic_plan._stabilized_pantry_state(
        days=("PO", "ST", "PI", "NE"),
        frequency=2,
        templates=recipes.all(),
        offers=offers,
        balances=balances,
        mode="standard",
        seed="fixture-seed",
        week=WEEK,
        adults=1,
        children=0,
        ingredient_catalog=ingredients,
        recent_families=(),
        recent_methods=(),
    )
    reranked = tuple(
        deterministic_plan._ranked_renderable_for_day(
            day=day,
            coverage=2 if day != "NE" else 1,
            templates=recipes.all(),
            offers=offers,
            balances=balances,
            mode="standard",
            seed="fixture-seed",
            week=WEEK,
            adults=1,
            children=0,
            ingredient_catalog=ingredients,
            recent_families=(),
            recent_methods=(),
            required_reserve=stabilized.reserve,
        )
        for day in ("PO", "ST", "PI", "NE")
    )
    assert tuple(
        ranking.bounded_identity for ranking in stabilized.rankings
    ) == tuple(ranking.bounded_identity for ranking in reranked)


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
        "zlava": "-28 %",
        "source_url": "https://example.test/offer_rice",
        "source_page": 4,
        "valid_from": WEEK,
        "valid_to": "2026-09-06",
    }

    pantry_plan = _build(
        rows=(),
        pantry=(PantryEntry("rice", "ryža", Quantity(Decimal("600"), "g")),),
        pantry_driven=True,
    )

    assert pantry_plan["jedla"][0]["suroviny"][0] == {"spajza": "ryža"}


def test_meal_serialization_preserves_complete_established_recipe_contract():
    plan = _build(
        adults=1,
        children=1,
        recipe_catalog=_rice_recipes(child_factor="0.5"),
    )

    meal = plan["jedla"][0]
    recipe = meal["recept"]

    assert meal["suroviny"] == [
        {
            "offer_key": "offer_rice",
            "nazov": "Basmati ryža Golden Sun",
            "obchod": "Lidl",
            "jednotka": "500 g",
            "mnozstvo": 1,
            "davka": "340 g",
            "cena": "1,79",
            "povodna": "2,49",
            "zlava": "-28 %",
            "source_url": "https://example.test/offer_rice",
            "source_page": 4,
            "valid_from": WEEK,
            "valid_to": "2026-09-06",
        },
        {"spajza": "voda"},
        {"spajza": "soľ"},
    ]
    assert recipe.keys() >= {
        "template_id",
        "family",
        "method",
        "min",
        "porcie",
        "pre",
        "davky",
        "skontroluj_doma",
        "kroky",
        "uchovanie",
        "domacnost",
        "dni",
        "dospely_ekvivalent",
        "poznamka",
        "nutrition",
    }
    assert recipe["porcie"] == 6
    assert recipe["pre"] == "1 dospelý + 1 dieťa × 3 dni"
    assert recipe["davky"] == [
        "Basmati ryža Golden Sun – 340 g",
        "voda zo špajze",
        "soľ zo špajze",
    ]
    assert recipe["skontroluj_doma"] == ["voda"]
    assert recipe["uchovanie"] == (
        "Porcie na ďalšie dni do 1 hodiny schlaď. Porciu na tretí deň "
        "hneď zamraz a po rozmrazení ju dôkladne zohrej iba raz."
    )
    assert recipe["domacnost"] == {"dospeli": 1, "deti": 1}
    assert recipe["dni"] == 3
    assert recipe["dospely_ekvivalent"] == "4,5"
    assert recipe["poznamka"] == "Kuchársky odhad na plánovanie nákupu."


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


def _protein_setup(
    *, protein="17.27", fat="8.72", child_factor="0.5", amount="180"
):
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
                amount=amount,
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


def test_recipe_payload_exposes_catalog_allergens_for_honest_ui_warning():
    ingredients, recipes = _protein_setup()

    plan = _build(
        rows=(_offer("Pevné tofu", offer_key="offer_tofu"),),
        mode="high_protein",
        ingredient_catalog=ingredients,
        recipe_catalog=recipes,
    )

    assert all(meal["recept"]["allergens"] == ["soy"] for meal in plan["jedla"])


def test_high_protein_gate_uses_true_adult_serving_without_child_dilution():
    ingredients, recipes = _protein_setup(child_factor="0.1")

    plan = _build(
        rows=(_offer("Pevné tofu", offer_key="offer_tofu"),),
        adults=1,
        children=1,
        mode="high_protein",
        ingredient_catalog=ingredients,
        recipe_catalog=recipes,
    )

    assert all(
        Decimal(meal["recept"]["nutrition"]["serving"]["protein_g"])
        == Decimal("31.086")
        for meal in plan["jedla"]
    )


def test_high_protein_gate_still_rejects_an_adult_serving_below_thirty_grams():
    ingredients, recipes = _protein_setup(
        child_factor="0.1", amount="170"
    )

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
