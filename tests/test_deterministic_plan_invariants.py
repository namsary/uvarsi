from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
import math
import unicodedata

import pytest

from app.deterministic_plan import build_deterministic_plan
from app.ingredient_catalog import DietTag, load_ingredient_catalog
from app.offer_matcher import match_offers
from app.quantity_math import PantryEntry, Quantity
from app.recipe_catalog import (
    ALLOWED_MODES,
    IngredientSlot,
    InstructionTemplate,
    RecipeCatalog,
    RecipeTemplate,
)


WEEK = "2026-08-31"
STORES = ("Kaufland", "Lidl", "Tesco")
MODE_INGREDIENT = {
    "standard": "rice",
    "high_protein": "chicken_breast",
    "vegetarian": "tofu",
    "vegan": "tofu",
}
MODE_AMOUNT = {
    "standard": Decimal("75"),
    "high_protein": Decimal("200"),
    "vegetarian": Decimal("180"),
    "vegan": Decimal("180"),
}
MODE_ROLE = {
    "standard": "starch",
    "high_protein": "protein",
    "vegetarian": "protein",
    "vegan": "protein",
}
INGREDIENT_STEM = {
    "rice": "ryz",
    "tofu": "tofu",
    "chicken_breast": "kurac",
}

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
CHICKEN_STEPS = (
    "Osuš {main.amount} {main.name} čistou utierkou, kým povrch nebude suchý.",
    "Nakrájaj {main.amount} {main.name} {main.cut} na doske.",
    "Rozohrej v panvici 1 polievkovú lyžicu oleja 2 minúty na strednom ohni, "
    "kým sa olej začne ľahko lesknúť.",
    "Opekaj {main.amount} {main.name} v panvici 12 minút na strednom ohni, "
    "kým bude mäso zlatisté a v strede prepečené.",
    "Rozdeľ kuracie prsia na {portions} porcie a podávaj ich horúce.",
)


@dataclass(frozen=True)
class PlannerFixture:
    rows: tuple[dict, ...]
    recipes: RecipeCatalog
    offer_ingredient: dict[str, str]
    package_grams: dict[str, Decimal]


def _fold(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value.casefold())
    return "".join(
        character
        for character in normalized
        if not unicodedata.combining(character)
    )


def _steps_for(ingredient_id: str) -> tuple[str, ...]:
    if ingredient_id == "rice":
        return RICE_STEPS
    if ingredient_id == "tofu":
        return TOFU_STEPS
    return CHICKEN_STEPS


def _template(mode: str, index: int) -> RecipeTemplate:
    ingredient_id = MODE_INGREDIENT[mode]
    method = ("pot", "pan", "oven")[index % 3]
    slot = IngredientSlot(
        key="main",
        role=MODE_ROLE[mode],
        candidates=(ingredient_id,),
        amount_per_adult=MODE_AMOUNT[mode],
        unit="g",
        child_factor=Decimal("0.6"),
        required=True,
        use="main",
        cut=None if ingredient_id == "rice" else "na 2 cm kocky",
    )
    return RecipeTemplate(
        id=f"fixture-{mode}-{index:02d}",
        version=1,
        active=True,
        name_template="Jednoduché jedlo z {main.name}",
        family=f"fixture-{mode}-family-{index:02d}",
        method=method,
        minutes=30,
        modes=frozenset({mode}),
        equipment=("hrniec",) if method == "pot" else (method,),
        slots=(slot,),
        pantry_basics=("water", "salt") if ingredient_id == "rice" else ("oil",),
        instructions=tuple(InstructionTemplate(step) for step in _steps_for(ingredient_id)),
    )


def make_fixture(*, offer_count: int, template_count: int) -> PlannerFixture:
    if template_count < len(ALLOWED_MODES) * 3:
        raise ValueError("fixture needs at least three methods per mode")
    modes = tuple(sorted(ALLOWED_MODES))
    templates = tuple(
        _template(mode, index)
        for mode in modes
        for index in range(template_count // len(modes))
    )
    if len(templates) < template_count:
        templates += tuple(
            _template(modes[index % len(modes)], 100 + index)
            for index in range(template_count - len(templates))
        )

    ingredients = load_ingredient_catalog()
    # Spread the production-sized rows across the complete canonical catalog.
    # Repeating only the three recipe ingredients would model hundreds of
    # indistinguishable packages of the same product, not a grocery flyer.
    ingredient_ids = tuple(item.id for item in ingredients.all())
    rows = []
    offer_ingredient = {}
    package_grams = {}
    for index in range(offer_count):
        ingredient_id = ingredient_ids[index % len(ingredient_ids)]
        ingredient = ingredients.by_id(ingredient_id)
        offer_key = f"fixture-offer-{index:04d}"
        package = Decimal("500")
        rows.append(
            {
                "offer_key": offer_key,
                "obchod": STORES[index % len(STORES)],
                "nazov": ingredient.name,
                "jednotka": "500 g",
                "cena": "1.99",
                "povodna": "2.99",
                "zlava": "-33 %",
                "valid_from": WEEK,
                "valid_to": "2026-09-06",
                "source_url": f"https://example.test/{offer_key}",
                "source_page": index % 100 + 1,
            }
        )
        offer_ingredient[offer_key] = ingredient_id
        package_grams[offer_key] = package
    return PlannerFixture(
        tuple(rows),
        RecipeCatalog(6001, templates),
        offer_ingredient,
        package_grams,
    )


@pytest.fixture(scope="module")
def invariant_fixture() -> PlannerFixture:
    return make_fixture(offer_count=24, template_count=12)


def _pantry_state(mode: str, state: str) -> tuple[tuple[PantryEntry, ...], bool]:
    ingredient_id = MODE_INGREDIENT[mode]
    ingredient = load_ingredient_catalog().by_id(ingredient_id)
    if state == "empty":
        return (), False
    if state == "partial":
        return (
            PantryEntry(
                ingredient_id,
                ingredient.name,
                Quantity(Decimal("250"), "g"),
            ),
        ), False
    if state == "pantry_driven":
        return (
            PantryEntry(
                ingredient_id,
                ingredient.name,
                Quantity(Decimal("25000"), "g"),
            ),
        ), True
    raise AssertionError(f"unknown pantry state {state}")


def _build_case(
    fixture: PlannerFixture,
    *,
    household_size: int,
    frequency: int,
    mode: str,
    pantry_state: str,
) -> tuple[dict, tuple[PantryEntry, ...]]:
    adults = max(1, (household_size + 1) // 2)
    children = household_size - adults
    pantry, pantry_driven = _pantry_state(mode, pantry_state)
    plan = build_deterministic_plan(
        week=WEEK,
        rows=fixture.rows,
        stores=STORES,
        adults=adults,
        children=children,
        frequency=frequency,
        pantry=pantry,
        pantry_driven=pantry_driven,
        mode=mode,
        seed=f"invariant:{household_size}:{frequency}:{mode}:{pantry_state}",
        ingredient_catalog=load_ingredient_catalog(),
        recipe_catalog=fixture.recipes,
    )
    return plan, pantry


def _assert_instruction_ingredients_are_declared(plan: dict, fixture: PlannerFixture) -> None:
    template_by_id = {template.id: template for template in fixture.recipes.all()}
    all_fixture_stems = set(INGREDIENT_STEM.values())
    for meal in plan["jedla"]:
        template = template_by_id[meal["recept"]["template_id"]]
        expected_ids = {
            ingredient_id
            for slot in template.slots
            for ingredient_id in slot.candidates
            if slot.required
        }
        expected_stems = {INGREDIENT_STEM[item] for item in expected_ids}
        instructions = _fold(" ".join(meal["recept"]["kroky"]))
        mentioned_stems = {stem for stem in all_fixture_stems if stem in instructions}
        assert mentioned_stems == expected_stems


def _assert_required_ingredients_are_covered(
    plan: dict,
    pantry: tuple[PantryEntry, ...],
    fixture: PlannerFixture,
) -> None:
    template_by_id = {template.id: template for template in fixture.recipes.all()}
    required: dict[str, Decimal] = {}
    for meal in plan["jedla"]:
        template = template_by_id[meal["recept"]["template_id"]]
        household = meal["recept"]["domacnost"]
        for slot in template.slots:
            if not slot.required:
                continue
            ingredient_id = slot.candidates[0]
            equivalents = Decimal(household["dospeli"]) + (
                Decimal(household["deti"]) * slot.child_factor
            )
            amount = slot.amount_per_adult * equivalents * Decimal(meal["pokryva_dni"])
            required[ingredient_id] = required.get(ingredient_id, Decimal("0")) + amount

    supplied: dict[str, Decimal] = {}
    for entry in pantry:
        if entry.quantity is not None and entry.quantity.unit == "g":
            supplied[entry.ingredient_id] = supplied.get(
                entry.ingredient_id, Decimal("0")
            ) + entry.quantity.amount
    for group in plan["nakupny_zoznam"]:
        for row in group["polozky"]:
            offer_key = row["offer_key"]
            ingredient_id = fixture.offer_ingredient[offer_key]
            supplied[ingredient_id] = supplied.get(
                ingredient_id, Decimal("0")
            ) + fixture.package_grams[offer_key] * Decimal(row["mnozstvo"])

    assert supplied.keys() >= required.keys()
    assert all(supplied[item] >= amount for item, amount in required.items())


def _assert_nonnegative_whole_package_plan(plan: dict) -> None:
    assert Decimal(plan["nakup_spolu"].replace(",", ".")) >= 0
    assert Decimal(plan["bezna_cena"].replace(",", ".")) >= 0
    assert Decimal(plan["usetrene"].replace(",", ".")) >= 0
    for meal in plan["jedla"]:
        assert meal["pokryva_dni"] > 0
        assert meal["recept"]["porcie"] > 0
        for values in meal["recept"]["nutrition"].values():
            if isinstance(values, dict):
                assert all(Decimal(value) >= 0 for value in values.values())
    for group in plan["nakupny_zoznam"]:
        for row in group["polozky"]:
            assert type(row["mnozstvo"]) is int
            assert row["mnozstvo"] > 0
            required_number = row["potrebne"].replace(" ", "").replace(",", ".")
            assert Decimal(required_number) >= 0
            leftover_number = row["zostava"].replace(" ", "").split("g")[0].replace(",", ".")
            assert Decimal(leftover_number) >= 0


HOUSEHOLD_MATRIX = tuple(
    (size, frequency, mode)
    for size in range(1, 13)
    for frequency in (1, 2, 3)
    for mode in sorted(ALLOWED_MODES)
)


@pytest.mark.parametrize(("household_size", "frequency", "mode"), HOUSEHOLD_MATRIX)
def test_plan_invariants_hold_for_households_frequencies_modes_and_pantry_states(
    invariant_fixture,
    household_size,
    frequency,
    mode,
):
    states = ("empty", "partial", "pantry_driven")
    pantry_state = states[(household_size + frequency + sorted(ALLOWED_MODES).index(mode)) % 3]
    plan, pantry = _build_case(
        invariant_fixture,
        household_size=household_size,
        frequency=frequency,
        mode=mode,
        pantry_state=pantry_state,
    )

    assert sum(meal["pokryva_dni"] for meal in plan["jedla"]) == 7
    assert plan["meta"]["mode"] == mode
    _assert_nonnegative_whole_package_plan(plan)
    _assert_instruction_ingredients_are_declared(plan, invariant_fixture)
    _assert_required_ingredients_are_covered(plan, pantry, invariant_fixture)

    templates = {template.id: template for template in invariant_fixture.recipes.all()}
    catalog = load_ingredient_catalog()
    for meal in plan["jedla"]:
        template = templates[meal["recept"]["template_id"]]
        assert mode in template.modes
        selected_ids = {
            ingredient_id
            for slot in template.slots
            for ingredient_id in slot.candidates
        }
        if mode == "vegetarian":
            assert all(DietTag.VEGETARIAN in catalog.by_id(item).diet_tags for item in selected_ids)
        if mode == "vegan":
            assert all(DietTag.VEGAN in catalog.by_id(item).diet_tags for item in selected_ids)
        if mode == "high_protein":
            assert Decimal(meal["recept"]["nutrition"]["serving"]["protein_g"]) >= 30


@pytest.mark.parametrize("mode", sorted(ALLOWED_MODES))
@pytest.mark.parametrize("pantry_state", ("empty", "partial", "pantry_driven"))
def test_each_mode_preserves_invariants_in_every_relevant_pantry_state(
    invariant_fixture,
    mode,
    pantry_state,
):
    plan, pantry = _build_case(
        invariant_fixture,
        household_size=4,
        frequency=3,
        mode=mode,
        pantry_state=pantry_state,
    )

    assert sum(meal["pokryva_dni"] for meal in plan["jedla"]) == 7
    _assert_nonnegative_whole_package_plan(plan)
    _assert_instruction_ingredients_are_declared(plan, invariant_fixture)
    _assert_required_ingredients_are_covered(plan, pantry, invariant_fixture)


def test_fixture_matrix_really_covers_required_boundaries():
    assert {size for size, _, _ in HOUSEHOLD_MATRIX} == set(range(1, 13))
    assert {frequency for _, frequency, _ in HOUSEHOLD_MATRIX} == {1, 2, 3}
    assert {mode for _, _, mode in HOUSEHOLD_MATRIX} == set(ALLOWED_MODES)
    assert math.prod((12, 3, len(ALLOWED_MODES))) == len(HOUSEHOLD_MATRIX)


def test_offer_cache_key_tracks_changed_row_values(invariant_fixture):
    row = dict(invariant_fixture.rows[0])
    catalog = load_ingredient_catalog()

    first = match_offers((row,), catalog)
    row["cena"] = "1.49"
    second = match_offers((row,), catalog)

    assert first[0].sale_price == Decimal("1.99")
    assert second[0].sale_price == Decimal("1.49")
