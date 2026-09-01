from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
import math
import re
from string import Formatter
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
    PANTRY_BASIC_NAMES,
    RecipeCatalog,
    RecipeTemplate,
)
from app.recipe_renderer import _ingredient_forms


WEEK = "2026-08-31"
STORES = ("Kaufland", "Lidl", "Tesco")
MODE_INGREDIENT = {
    "standard": "rice",
    "high_protein": "chicken_breast",
    "vegetarian": "tofu",
    "vegan": "tofu",
}
MODE_CANDIDATES = {
    "standard": ("rice", "pasta"),
    "high_protein": ("chicken_breast", "chicken_thigh"),
    "vegetarian": ("tofu", "chickpeas"),
    "vegan": ("tofu", "chickpeas"),
}
OPTIONAL_CANDIDATES = ("zucchini", "broccoli")
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
RICE_STEPS = (
    "Prepláchni {main.amount} {main.name} v jemnom sitku pod studenou vodou, "
    "kým odtekajúca voda nebude takmer číra.",
    "Vlož {main.amount} {main.name} do hrnca, prilej 450 ml vody, pridaj "
    "štipku soli a obsah hrnca priveď na silnom ohni za 5 minút do varu, "
    "kým voda nezačne súvislo bublať.",
    "Var {main.amount} {main.name} v prikrytom hrnci 12 minút na miernom "
    "ohni, kým sa voda vsiakne.",
    "Nakrájaj {vegetable.amount} {vegetable.name} {vegetable.cut}.",
    "Pridaj {vegetable.amount} {vegetable.name} do hrnca a var na miernom "
    "ohni 6 minút, kým zelenina zmäkne.",
    "Odstav hrniec a nechaj obsah prikrytý 5 minút dôjsť, kým bude príloha "
    "mäkká.",
    "Rozdeľ uvarenú prílohu na {portions} porcie a podávaj ju horúcu.",
)
TOFU_STEPS = (
    "Osuš {main.amount} {main.name} čistou utierkou, kým povrch nebude suchý.",
    "Nakrájaj {main.amount} {main.name} {main.cut} na doske.",
    "Rozohrej v panvici 1 polievkovú lyžicu oleja 2 minúty na strednom ohni, "
    "kým sa olej začne ľahko lesknúť.",
    "Opekaj {main.amount} {main.name} v panvici 8 minút na strednom ohni, "
    "kým budú všetky strany zlatisté a chrumkavé.",
    "Nakrájaj {vegetable.amount} {vegetable.name} {vegetable.cut}.",
    "Pridaj {vegetable.amount} {vegetable.name} do panvice a opekaj na "
    "strednom ohni 6 minút, kým zelenina zmäkne.",
    "Rozdeľ opečenú hlavnú surovinu na {portions} porcie a podávaj ju ihneď.",
)
CHICKEN_STEPS = (
    "Osuš {main.amount} {main.name} čistou utierkou, kým povrch nebude suchý.",
    "Nakrájaj {main.amount} {main.name} {main.cut} na doske.",
    "Rozohrej v panvici 1 polievkovú lyžicu oleja 2 minúty na strednom ohni, "
    "kým sa olej začne ľahko lesknúť.",
    "Opekaj {main.amount} {main.name} v panvici 12 minút na strednom ohni, "
    "kým bude mäso zlatisté a v strede prepečené.",
    "Nakrájaj {vegetable.amount} {vegetable.name} {vegetable.cut}.",
    "Pridaj {vegetable.amount} {vegetable.name} do panvice a opekaj na "
    "strednom ohni 6 minút, kým zelenina zmäkne.",
    "Rozdeľ upečené mäso na {portions} porcie a podávaj ho horúce.",
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
    main_slot = IngredientSlot(
        key="main",
        role=MODE_ROLE[mode],
        candidates=MODE_CANDIDATES[mode],
        amount_per_adult=MODE_AMOUNT[mode],
        unit="g",
        child_factor=Decimal("0.6"),
        required=True,
        use="main",
        cut=None if ingredient_id == "rice" else "na 2 cm kocky",
    )
    vegetable_slot = IngredientSlot(
        key="vegetable",
        role="vegetable",
        candidates=OPTIONAL_CANDIDATES,
        amount_per_adult=Decimal("70"),
        unit="g",
        child_factor=Decimal("0.6"),
        required=False,
        use="addition",
        cut="na malé kúsky",
    )
    return RecipeTemplate(
        id=f"fixture-{mode}-{index:02d}",
        version=1,
        active=True,
        name_template="Jednoduché jedlo z {main.name} a {vegetable.name}",
        family=f"fixture-{mode}-family-{index:02d}",
        method=method,
        minutes=30,
        modes=frozenset({mode}),
        equipment=("hrniec",) if method == "pot" else (method,),
        slots=(main_slot, vegetable_slot),
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
                "cena": (
                    "0.99"
                    if ingredient_id in {*MODE_INGREDIENT.values(), "zucchini"}
                    else "1.99"
                ),
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
    ingredients = load_ingredient_catalog()
    if state == "empty":
        return (), False
    if state == "partial":
        ingredient_ids = (MODE_INGREDIENT[mode], "zucchini")
        return tuple(
            PantryEntry(
                ingredient_id,
                ingredients.by_id(ingredient_id).name,
                Quantity(Decimal("20"), "g"),
            )
            for ingredient_id in ingredient_ids
        ), False
    if state == "pantry_driven":
        ingredient_ids = tuple(
            dict.fromkeys((*MODE_CANDIDATES[mode], *OPTIONAL_CANDIDATES))
        )
        return tuple(
            PantryEntry(
                ingredient_id,
                ingredients.by_id(ingredient_id).name,
                Quantity(Decimal("25000"), "g"),
            )
            for ingredient_id in ingredient_ids
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


def _meal_ingredient_ids(meal: dict, fixture: PlannerFixture) -> set[str]:
    catalog = load_ingredient_catalog()
    result = set()
    for row in meal["suroviny"]:
        offer_key = row.get("offer_key")
        if offer_key is not None:
            result.add(fixture.offer_ingredient[offer_key])
            continue
        pantry_name = row.get("spajza")
        ingredient = catalog.resolve(pantry_name) if pantry_name else None
        if ingredient is not None:
            result.add(ingredient.id)
    return result


def _ingredient_is_mentioned(ingredient, folded_instructions: str) -> bool:
    for form in _ingredient_forms(ingredient):
        words = re.findall(r"[a-z0-9]+", _fold(form))
        if words and re.search(
            r"\b" + r"\s+".join(map(re.escape, words)) + r"\b",
            folded_instructions,
        ):
            return True
    return False


def _instruction_slot_keys(template: RecipeTemplate) -> set[str]:
    return {
        field_name.split(".", 1)[0]
        for instruction in template.instructions
        for _, field_name, _, _ in Formatter().parse(instruction.text)
        if field_name is not None and field_name != "portions"
    }


def _assert_instruction_ingredients_are_declared(plan: dict, fixture: PlannerFixture) -> None:
    template_by_id = {template.id: template for template in fixture.recipes.all()}
    catalog = load_ingredient_catalog()
    for meal in plan["jedla"]:
        template = template_by_id[meal["recept"]["template_id"]]
        selected_ids = _meal_ingredient_ids(meal, fixture)
        folded_instructions = _fold(" ".join(meal["recept"]["kroky"]))
        mentioned_ids = {
            ingredient.id
            for ingredient in catalog.all()
            if _ingredient_is_mentioned(ingredient, folded_instructions)
        }

        catalog_basic_ids = {
            ingredient_id
            for ingredient_id in template.pantry_basics
            if ingredient_id not in PANTRY_BASIC_NAMES
        }
        assert mentioned_ids == selected_ids | catalog_basic_ids
        referenced_slots = _instruction_slot_keys(template)
        assert referenced_slots == {slot.key for slot in template.slots}
        for slot in template.slots:
            assert selected_ids.intersection(slot.candidates)

        declared_pantry_names = {
            _fold(row["spajza"])
            for row in meal["suroviny"]
            if "spajza" in row
        }
        declared_home_names = {
            _fold(name) for name in meal["recept"]["skontroluj_doma"]
        }
        for basic_id, basic_name in PANTRY_BASIC_NAMES.items():
            if basic_id in template.pantry_basics:
                assert _fold(basic_name) in declared_home_names
                assert _fold(basic_name) not in declared_pantry_names
        for basic_id in catalog_basic_ids:
            basic_name = catalog.by_id(basic_id).name
            assert _fold(basic_name) in declared_home_names
            assert _fold(basic_name) not in declared_pantry_names


def _assert_required_ingredients_are_covered(
    plan: dict,
    pantry: tuple[PantryEntry, ...],
    fixture: PlannerFixture,
) -> None:
    template_by_id = {template.id: template for template in fixture.recipes.all()}
    required: dict[str, Decimal] = {}
    for meal in plan["jedla"]:
        template = template_by_id[meal["recept"]["template_id"]]
        selected_ids = _meal_ingredient_ids(meal, fixture)
        household = meal["recept"]["domacnost"]
        for slot in template.slots:
            if not slot.required:
                continue
            selected_candidates = selected_ids.intersection(slot.candidates)
            assert len(selected_candidates) == 1
            ingredient_id = next(iter(selected_candidates))
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


PANTRY_STATES = ("empty", "partial", "pantry_driven")
HOUSEHOLD_MATRIX = tuple(
    (size, frequency, mode, pantry_state)
    for size in range(1, 13)
    for frequency in (1, 2, 3)
    for mode in sorted(ALLOWED_MODES)
    for pantry_state in PANTRY_STATES
)


@pytest.mark.parametrize(
    ("household_size", "frequency", "mode", "pantry_state"),
    HOUSEHOLD_MATRIX,
)
def test_plan_invariants_hold_for_households_frequencies_modes_and_pantry_states(
    invariant_fixture,
    household_size,
    frequency,
    mode,
    pantry_state,
):
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

def test_fixture_matrix_really_covers_required_boundaries():
    assert all(len(case) == 4 for case in HOUSEHOLD_MATRIX)
    assert len(HOUSEHOLD_MATRIX) == math.prod((12, 3, len(ALLOWED_MODES), 3))
    assert {size for size, _, _, _ in HOUSEHOLD_MATRIX} == set(range(1, 13))
    assert {frequency for _, frequency, _, _ in HOUSEHOLD_MATRIX} == {1, 2, 3}
    assert {mode for _, _, mode, _ in HOUSEHOLD_MATRIX} == set(ALLOWED_MODES)
    assert {state for _, _, _, state in HOUSEHOLD_MATRIX} == set(PANTRY_STATES)


def test_fixture_exercises_required_optional_and_alternative_slots(
    invariant_fixture,
):
    slots = tuple(
        slot
        for template in invariant_fixture.recipes.all()
        for slot in template.slots
    )

    assert any(slot.required for slot in slots)
    assert any(not slot.required for slot in slots)
    assert any(len(slot.candidates) > 1 for slot in slots)


def test_offer_cache_key_tracks_changed_row_values(invariant_fixture):
    row = dict(invariant_fixture.rows[0])
    catalog = load_ingredient_catalog()
    initial_price = Decimal(str(row["cena"]))

    first = match_offers((row,), catalog)
    row["cena"] = "1.49"
    second = match_offers((row,), catalog)

    assert first[0].sale_price == initial_price
    assert second[0].sale_price == Decimal("1.49")
