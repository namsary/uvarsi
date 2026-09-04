from dataclasses import replace
from decimal import Decimal, localcontext
from itertools import product
import re

import pytest

from app.ingredient_catalog import load_ingredient_catalog
from app.quantity_math import Quantity
from app.recipe_catalog import (
    IngredientSlot,
    InstructionTemplate,
    RecipeTemplate,
    load_recipe_catalog,
)
from app.recipe_matcher import RecipeCandidate, SlotSelection
from app.recipe_renderer import _QUANTITY_NAMES, _quantity_name, render_meal


@pytest.fixture(scope="module")
def ingredients():
    return load_ingredient_catalog()


def _candidate(
    ingredient,
    *,
    amount="75",
    unit="g",
    child_factor="0.5",
    cut=None,
    name_template="Jedlo z {main.name}",
    method="pot",
    equipment=("hrniec",),
    pantry_basics=(),
    instructions=(),
):
    slot = IngredientSlot(
        key="main",
        role=next(iter(ingredient.roles)),
        candidates=(ingredient.id,),
        amount_per_adult=Decimal(amount),
        unit=unit,
        child_factor=Decimal(child_factor),
        required=True,
        use="main",
        cut=cut,
    )
    template = RecipeTemplate(
        id=f"render-{ingredient.id}",
        version=1,
        active=True,
        name_template=name_template,
        family="renderer_snapshot",
        method=method,
        minutes=30,
        modes=frozenset({"standard"}),
        equipment=equipment,
        slots=(slot,),
        pantry_basics=pantry_basics,
        instructions=tuple(InstructionTemplate(text) for text in instructions),
    )
    return RecipeCandidate(
        template=template,
        selections=(
            SlotSelection(
                slot=slot,
                ingredient=ingredient,
                offer=None,
                pantry=None,
            ),
        ),
        score=Decimal("0"),
        key=f"candidate-{ingredient.id}",
    )


def _catalog_candidate(ingredients, recipe_id, candidate_ids):
    recipe = next(
        recipe
        for recipe in load_recipe_catalog(ingredients).all()
        if recipe.id == recipe_id
    )
    selections = tuple(
        SlotSelection(
            slot=slot,
            ingredient=ingredients.by_id(candidate_id),
            offer=None,
            pantry=None,
        )
        for slot, candidate_id in zip(recipe.slots, candidate_ids, strict=True)
    )
    return RecipeCandidate(
        template=recipe,
        selections=selections,
        score=Decimal("0"),
        key=f"capacity-audit:{recipe.id}:{'+'.join(candidate_ids)}",
    )


def _rendered_grams(item):
    quantity = item.quantity
    if quantity.unit == "g":
        grams = quantity.amount
    elif quantity.unit == "piece":
        grams = quantity.amount * item.ingredient.grams_per_piece
    else:
        grams = quantity.amount * item.ingredient.density_g_per_ml
    return grams * item.ingredient.edible_ratio


def test_large_multi_day_pan_batch_uses_capacity_safe_deterministic_guidance(
    ingredients,
):
    candidate = _candidate(
        ingredients.by_id("chicken_breast"),
        amount="180",
        cut="na kocky",
        name_template="Kuracie kúsky z panvice",
        method="pan",
        equipment=("panvica", "doska"),
        pantry_basics=("oil", "salt", "black_pepper"),
        instructions=(
            "Osuš {main.amount} {main.name} papierovou utierkou.",
            "Nakrájaj {main.amount} {main.name} {main.cut}.",
            "Opekaj {main.amount} {main.name} v panvici na strednom ohni "
            "8 minút, kým bude mäso zlatisté a v strede prepečené.",
            "Rozdeľ mäso na {portions} porcií a podávaj ho horúce.",
        ),
    )

    meal = render_meal(candidate, adults=4, children=0, covered_days=3)

    cooking_step = next(step for step in meal.instructions if "Každú dávku" in step)
    assert "kuracie prsia" in cooking_step
    assert "2,2 kg" not in cooking_step
    assert "jednej vrstve" in cooking_step
    assert "ďalšiu panvicu" in cooking_step
    assert "kým bude mäso zlatisté a v strede prepečené" in cooking_step
    assert "8 minút" not in cooking_step


def test_large_tomato_pan_step_does_not_depend_on_opekaj_keyword(ingredients):
    candidate = _catalog_candidate(
        ingredients,
        "pan_chicken_pasta_tomato",
        ("chicken_breast", "pasta", "tomato"),
    )

    meal = render_meal(candidate, adults=4, children=0, covered_days=3)

    tomato_step = next(
        step
        for step in meal.instructions
        if "paradajky" in step and "panvic" in step
    )
    assert "ďalšiu panvicu" in tomato_step
    assert "Každú dávku tepelne uprav v panvici na miernom ohni" in tomato_step
    assert "kým zelenina zmäkne" in tomato_step
    assert "7 minút" not in tomato_step


def test_ordinary_steps_keep_tomato_weight_in_the_ingredient_list(
    ingredients,
):
    candidate = _catalog_candidate(
        ingredients,
        "pot_chickpea_tomato_couscous",
        ("chickpeas", "couscous", "tomato"),
    )

    meal = render_meal(candidate, adults=4, children=0, covered_days=2)

    assert meal.ingredients[2].display_amount == "1,6 kg"
    assert all("1,6 kg" not in step for step in meal.instructions)
    assert "Nakrájaj paradajky na malé kúsky." in meal.instructions
    assert any(
        "Pridaj paradajky do hrnca" in step for step in meal.instructions
    )


def test_large_egg_pan_step_supports_vlej_and_preserves_doneness(ingredients):
    candidate = _candidate(
        ingredients.by_id("egg"),
        amount="4",
        unit="piece",
        name_template="Vajcia z panvice",
        method="pan",
        equipment=("panvica", "misa"),
        pantry_basics=("oil", "salt"),
        instructions=(
            "Rozšľahaj {main.amount} {main.name} v mise.",
            "Vlej {main.amount} {main.name} do panvice a opekaj ich 5 minút "
            "na miernom ohni, kým úplne stuhnú.",
            "Rozdeľ vajcia na {portions} porcií a podávaj ich teplé.",
        ),
    )

    meal = render_meal(candidate, adults=4, children=0, covered_days=3)

    egg_step = next(
        step
        for step in meal.instructions
        if "vajcia" in step and "panvic" in step
    )
    assert "ďalšiu panvicu" in egg_step
    assert "Každú dávku tepelne uprav v panvici na miernom ohni" in egg_step
    assert "kým úplne stuhnú" in egg_step
    assert "5 minút" not in egg_step


def test_all_150_catalog_variants_are_capacity_safe_for_four_adults_three_days(
    ingredients,
):
    recipes = load_recipe_catalog(ingredients).all()
    rendered_count = 0
    audited_capacity_steps = 0

    for recipe in recipes:
        for candidate_ids in product(*(slot.candidates for slot in recipe.slots)):
            meal = render_meal(
                _catalog_candidate(ingredients, recipe.id, candidate_ids),
                adults=4,
                children=0,
                covered_days=3,
            )
            rendered_count += 1
            for source, output in zip(
                recipe.instructions, meal.instructions, strict=True
            ):
                source_text = source.text.casefold()
                if not all(
                    marker in source_text for marker in ("panvic", "minút", "kým")
                ):
                    continue
                step_items = tuple(
                    item
                    for item in meal.ingredients
                    if f"{{{item.slot.key}.amount}}" in source.text
                )
                step_grams = sum(
                    (_rendered_grams(item) for item in step_items), Decimal("0")
                )
                if step_grams <= Decimal("800"):
                    continue
                audited_capacity_steps += 1
                assert "ďalšiu panvicu" in output, (recipe.id, output)
                assert "Každú dávku tepelne uprav v panvici" in output
                assert re.search(r"\d+(?:[,.]\d+)?\s*minút", output) is None
                assert "kým" in output

    assert rendered_count == 150
    assert audited_capacity_steps > 0


def test_all_catalog_variants_keep_weights_in_ingredient_list_not_steps(
    ingredients,
):
    recipes = load_recipe_catalog(ingredients).all()
    rendered_count = 0

    for recipe in recipes:
        for candidate_ids in product(*(slot.candidates for slot in recipe.slots)):
            meal = render_meal(
                _catalog_candidate(ingredients, recipe.id, candidate_ids),
                adults=4,
                children=0,
                covered_days=2,
            )
            rendered_count += 1
            for item in meal.ingredients:
                measured_phrase = f"{item.display_amount} {_quantity_name(item)}"
                assert all(
                    measured_phrase not in step for step in meal.instructions
                ), (recipe.id, item.slot.key, measured_phrase, meal.instructions)

    assert rendered_count == 150


@pytest.mark.parametrize(
    ("recipe_id", "candidate_ids", "expected", "forbidden"),
    [
        (
            "pan_turkey_couscous_zucchini",
            ("turkey_breast", "couscous", "zucchini"),
            "Priprav kuskus v miske s 280 ml vody.",
            "Prilej vodu k kuskus.",
        ),
        (
            "soup_chicken_vegetable_noodle",
            ("chicken_breast", "egg_noodles", "carrot"),
            "Pridaj vaječné rezance do hrnca",
            "Pridaj vaječných rezancov do hrnca",
        ),
        (
            "veg_mushroom_barley_pan",
            ("chickpeas", "barley", "mushrooms"),
            "Opekaj biele šampiňóny v panvici",
            "Opekaj bielych šampiňónov v panvici",
        ),
        (
            "salad_chicken_potato_yogurt",
            ("chicken_breast", "potato", "bell_pepper", "plain_yogurt"),
            "Premiešaj biely plnotučný jogurt so zemiakmi",
            "Premiešaj bieleho plnotučného jogurtu so zemiakmi",
        ),
    ],
)
def test_amountless_catalog_steps_keep_natural_slovak_cases(
    ingredients, recipe_id, candidate_ids, expected, forbidden
):
    meal = render_meal(
        _catalog_candidate(ingredients, recipe_id, candidate_ids),
        adults=4,
        children=0,
        covered_days=1,
    )

    instructions = " ".join(meal.instructions)
    assert expected in instructions
    assert forbidden not in instructions


@pytest.mark.parametrize(
    ("name_template", "expected"),
    [
        ("Jedlo s {main.name}", "Jedlo s cuketou"),
        ("Jedlo z {main.name}", "Jedlo z cukety"),
    ],
)
def test_recipe_title_uses_catalogued_slovak_prepositional_form(
    ingredients, name_template, expected
):
    candidate = _candidate(
        ingredients.by_id("zucchini"),
        name_template=name_template,
        instructions=ROASTED_VEGETABLE_STEPS,
        equipment=("rúra", "plech", "doska"),
        pantry_basics=("oil", "salt"),
        cut="na polkolieska",
        amount="150",
    )

    meal = render_meal(candidate, adults=4, children=0, covered_days=1)

    assert meal.name == expected


def _with_instructions(candidate, instructions):
    return replace(
        candidate,
        template=replace(
            candidate.template,
            instructions=tuple(InstructionTemplate(text) for text in instructions),
        ),
    )


RICE_STEPS = (
    "Prepláchni {main.amount} {main.name} v jemnom sitku pod studenou vodou, "
    "kým odtekajúca voda nebude takmer číra.",
    "Vlož {main.amount} {main.name} do hrnca, prilej {main.water} vody, pridaj "
    "štipku soli a obsah hrnca priveď na silnom ohni za 5 minút do varu, "
    "kým voda nezačne súvislo bublať.",
    "Var {main.amount} {main.name} v prikrytom hrnci 12 minút na miernom "
    "ohni, kým sa voda vsiakne.",
    "Odstav hrniec a nechaj ryžu prikrytú 5 minút dôjsť, kým budú zrná "
    "mäkké a oddelené.",
    "Rozdeľ ryžu na {portions} porcie a podávaj ju horúcu.",
)

PASTA_STEPS = (
    "Priveď v hrnci 2 l vody so štipkou soli na silnom ohni za 8 minút "
    "do prudkého varu, kým voda nezačne súvislo bublať.",
    "Vsyp {main.amount} {main.name} do hrnca a var ich 9 minút na strednom "
    "ohni, kým budú mäkké, ale pri zahryznutí ešte pevné.",
    "Odober z hrnca 100 ml vody z varenia a odlož ju na zriedenie omáčky.",
    "Sceď cestoviny v sitku a nechaj ich 30 sekúnd odkvapkať, kým z nich "
    "prestane tiecť voda.",
    "Rozdeľ cestoviny na {portions} porcie a podávaj ich ešte teplé.",
)

ROASTED_VEGETABLE_STEPS = (
    "Predhrej rúru na 200 °C.",
    "Nakrájaj {main.amount} {main.name} {main.cut} na doske na približne "
    "1 cm hrubé kúsky.",
    "Rozlož {main.amount} {main.name} v jednej vrstve na plech, pridaj "
    "1 polievkovú lyžicu oleja a štipku soli.",
    "Peč {main.amount} {main.name} na plechu 25 minút pri 200 °C, kým "
    "zmäkne a okraje nezozlatnú.",
    "Rozdeľ pečenú cuketu na {portions} porcie a podávaj ju horúcu.",
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
    "Osuš {main.amount} {main.name} papierovou utierkou a rovnomerne ich "
    "osoľ a okoreň.",
    "Predhrej rúru na 200 °C.",
    "Rozlož {main.amount} {main.name} na pekáč kožou nahor tak, aby sa "
    "jednotlivé kúsky nedotýkali.",
    "Peč {main.amount} {main.name} v pekáči 40 minút pri 200 °C, kým "
    "bude mäso prepečené až ku kosti a po narezaní z neho potečie číra "
    "šťava.",
    "Nechaj kuracie stehná na pekáči 5 minút odpočívať, kým sa šťava "
    "prestane uvoľňovať.",
    "Rozdeľ kuracie stehná na {portions} porcie a podávaj ich horúce.",
)


@pytest.mark.parametrize(
    ("ingredient_id", "amount", "cut", "name_template", "equipment", "basics", "steps", "expected"),
    [
        (
            "rice",
            "75",
            None,
            "Absorpčne varená {main.name}",
            ("hrniec", "sitko"),
            ("water", "salt"),
            RICE_STEPS,
            (
                "Absorpčne varená ryža",
                "300 g · ryža",
                (
                    "Prepláchni ryžu v jemnom sitku pod studenou vodou, kým odtekajúca voda nebude takmer číra.",
                    "Vlož ryžu do hrnca, prilej 450 ml vody, pridaj štipku soli a obsah hrnca priveď na silnom ohni za 5 minút do varu, kým voda nezačne súvislo bublať.",
                    "Var ryžu v prikrytom hrnci 12 minút na miernom ohni, kým sa voda vsiakne.",
                    "Odstav hrniec a nechaj ryžu prikrytú 5 minút dôjsť, kým budú zrná mäkké a oddelené.",
                    "Rozdeľ ryžu na 4 porcie a podávaj ju horúcu.",
                ),
            ),
        ),
        (
            "pasta",
            "75",
            None,
            "{main.name} al dente",
            ("hrniec", "sitko"),
            ("water", "salt"),
            PASTA_STEPS,
            (
                "Cestoviny al dente",
                "300 g · cestoviny",
                (
                    "Priveď v hrnci 2 l vody so štipkou soli na silnom ohni za 8 minút do prudkého varu, kým voda nezačne súvislo bublať.",
                    "Vsyp cestoviny do hrnca a var ich 9 minút na strednom ohni, kým budú mäkké, ale pri zahryznutí ešte pevné.",
                    "Odober z hrnca 100 ml vody z varenia a odlož ju na zriedenie omáčky.",
                    "Sceď cestoviny v sitku a nechaj ich 30 sekúnd odkvapkať, kým z nich prestane tiecť voda.",
                    "Rozdeľ cestoviny na 4 porcie a podávaj ich ešte teplé.",
                ),
            ),
        ),
        (
            "zucchini",
            "150",
            "na polkolieska",
            "Pečená {main.name}",
            ("rúra", "plech", "doska"),
            ("oil", "salt"),
            ROASTED_VEGETABLE_STEPS,
            (
                "Pečená cuketa",
                "600 g · cuketa",
                (
                    "Predhrej rúru na 200 °C.",
                    "Nakrájaj cuketu na polkolieska na doske na približne 1 cm hrubé kúsky.",
                    "Rozlož cuketu v jednej vrstve na plech, pridaj 1 polievkovú lyžicu oleja a štipku soli.",
                    "Peč cuketu na plechu 25 minút pri 200 °C, kým zmäkne a okraje nezozlatnú.",
                    "Rozdeľ pečenú cuketu na 4 porcie a podávaj ju horúcu.",
                ),
            ),
        ),
        (
            "tofu",
            "160",
            "na 2 cm kocky",
            "Chrumkavé {main.name} z panvice",
            ("panvica", "doska"),
            ("oil",),
            TOFU_STEPS,
            (
                "Chrumkavé tofu z panvice",
                "640 g · tofu",
                (
                    "Osuš tofu čistou utierkou, kým povrch nebude suchý.",
                    "Nakrájaj tofu na 2 cm kocky na doske.",
                    "Rozohrej v panvici 1 polievkovú lyžicu oleja 2 minúty na strednom ohni, kým sa olej začne ľahko lesknúť.",
                    "Opekaj tofu v panvici 8 minút na strednom ohni, kým budú všetky strany zlatisté a chrumkavé.",
                    "Rozdeľ tofu na 4 porcie a podávaj ho ihneď.",
                ),
            ),
        ),
        (
            "chicken_thigh",
            "225",
            None,
            "Pečené {main.name}",
            ("rúra", "pekáč"),
            ("salt", "black_pepper"),
            CHICKEN_STEPS,
            (
                "Pečené kuracie stehná",
                "900 g · kuracie stehná",
                (
                    "Osuš kuracie stehná papierovou utierkou a rovnomerne ich osoľ a okoreň.",
                    "Predhrej rúru na 200 °C.",
                    "Rozlož kuracie stehná na pekáč kožou nahor tak, aby sa jednotlivé kúsky nedotýkali.",
                    "Peč kuracie stehná v pekáči 40 minút pri 200 °C, kým bude mäso prepečené až ku kosti a po narezaní z neho potečie číra šťava.",
                    "Nechaj kuracie stehná na pekáči 5 minút odpočívať, kým sa šťava prestane uvoľňovať.",
                    "Rozdeľ kuracie stehná na 4 porcie a podávaj ich horúce.",
                ),
            ),
        ),
    ],
)
def test_beginner_friendly_slovak_language_snapshots(
    ingredients,
    ingredient_id,
    amount,
    cut,
    name_template,
    equipment,
    basics,
    steps,
    expected,
):
    candidate = _candidate(
        ingredients.by_id(ingredient_id),
        amount=amount,
        cut=cut,
        name_template=name_template,
        equipment=equipment,
        pantry_basics=basics,
        instructions=steps,
    )

    meal = render_meal(candidate, adults=4, children=0, covered_days=1)

    assert (meal.name, meal.ingredients[0].label, meal.instructions) == expected
    assert meal.portions == 4


def test_batch_math_stays_exact_and_displayed_portions_are_people_meals(ingredients):
    candidate = _candidate(
        ingredients.by_id("rice"),
        amount="75.125",
        child_factor="0.625",
        name_template="Absorpčne varená {main.name}",
        equipment=("hrniec", "sitko"),
        pantry_basics=("water", "salt"),
        instructions=RICE_STEPS,
    )

    with localcontext() as context:
        context.prec = 4
        meal = render_meal(candidate, adults=2, children=1, covered_days=3)

    assert meal.ingredients[0].quantity == Quantity(Decimal("591.609375"), "g")
    assert meal.ingredients[0].display_amount == "590 g"
    assert meal.portions == 9
    assert meal.covered_days == 3


@pytest.mark.parametrize(
    ("adults", "expected"),
    [(1, "1 porciu"), (2, "2 porcie"), (4, "4 porcie"), (5, "5 porcií"), (12, "12 porcií")],
)
def test_serving_instruction_uses_the_correct_slovak_portion_form(
    ingredients, adults, expected
):
    candidate = _candidate(
        ingredients.by_id("tofu"),
        amount="160",
        name_template="Tofu z panvice",
        equipment=("panvica",),
        pantry_basics=("oil",),
        instructions=(
            "Nakrájaj {main.name} na rovnaké kocky.",
            "Opekaj {main.name} v panvici 8 minút na strednom ohni, kým bude zlatisté.",
            "Rozdeľ tofu na {portions} porcií a podávaj ho teplé.",
        ),
    )

    meal = render_meal(candidate, adults=adults, children=0, covered_days=1)

    assert expected in meal.instructions[-1]


@pytest.mark.parametrize(
    ("ingredient_id", "quantity_form"),
    [
        ("turkey_breast", "morčacích pŕs"),
        ("white_fish", "filetu z bielej ryby"),
        ("bell_pepper", "papriky"),
        ("spinach", "špenátu"),
        ("peas", "zeleného hrášku"),
        ("couscous", "kuskusu"),
        ("barley", "jačmenných krúp"),
        ("feta", "syra feta"),
        ("beans", "červenej fazule"),
        ("coconut_milk", "kokosovej smotany"),
        ("paprika_powder", "mletej papriky"),
        ("curry_powder", "karí korenia"),
        ("oregano", "sušeného oregana"),
        ("egg_noodles", "vaječných rezancov"),
        ("mushrooms", "bielych šampiňónov"),
        ("plain_yogurt", "bieleho plnotučného jogurtu"),
        ("tuna", "tuniaka vo vlastnej šťave"),
    ],
)
def test_launch_catalog_has_natural_slovak_forms_after_quantities(
    ingredient_id, quantity_form
):
    assert _QUANTITY_NAMES[ingredient_id] == quantity_form


def test_display_rounds_1980_grams_to_two_kilos_without_changing_quantity(ingredients):
    candidate = _candidate(
        ingredients.by_id("chicken_thigh"),
        amount="165",
        name_template="Pečené {main.name}",
        equipment=("rúra", "pekáč"),
        pantry_basics=("salt", "black_pepper"),
        instructions=CHICKEN_STEPS,
    )

    meal = render_meal(candidate, adults=4, children=0, covered_days=3)

    assert meal.ingredients[0].quantity == Quantity(Decimal("1980"), "g")
    assert meal.ingredients[0].display_amount == "2 kg"
    assert meal.ingredients[0].label == "2 kg · kuracie stehná"
    assert "1 980 g" not in " ".join(meal.instructions)
    assert "2 kg" not in " ".join(meal.instructions)
    assert "kuracie stehná" in " ".join(meal.instructions)


def test_millilitres_use_litres_with_one_useful_decimal(ingredients):
    milk_steps = (
        "Nalej {main.amount} {main.name} do hrnca.",
        "Zohrievaj {main.amount} {main.name} v hrnci 5 minút na miernom ohni, "
        "kým sa z mlieka začne pariť.",
        "Odstav hrniec a nechaj mlieko 2 minúty chladnúť, kým prestane bublať.",
        "Rozdeľ nápoj na {portions} porcie a podávaj ho teplý.",
    )
    candidate = _candidate(
        ingredients.by_id("milk"),
        amount="312.5",
        unit="ml",
        name_template="Teplé {main.name}",
        instructions=milk_steps,
    )

    meal = render_meal(candidate, adults=4, children=0, covered_days=1)

    assert meal.ingredients[0].quantity == Quantity(Decimal("1250.0"), "ml")
    assert meal.ingredients[0].display_amount == "1,3 l"


def test_nutrition_converts_pieces_before_using_edible_grams(ingredients):
    egg_steps = (
        "Rozšľahaj {main.amount} {main.name} v mise, kým sa bielky a žĺtky spoja.",
        "Rozohrej panvicu 2 minúty na strednom ohni, kým je povrch horúci.",
        "Vlej {main.amount} {main.name} do panvice a opekaj ich 4 minúty na "
        "miernom ohni, kým úplne stuhnú.",
        "Rozdeľ vajcia na {portions} porcie a podávaj ich horúce.",
    )
    candidate = _candidate(
        ingredients.by_id("egg"),
        amount="1",
        unit="piece",
        name_template="Pražené {main.name}",
        equipment=("misa", "panvica"),
        instructions=egg_steps,
    )

    meal = render_meal(candidate, adults=4, children=0, covered_days=1)

    assert meal.ingredients[0].quantity == Quantity(Decimal("4"), "piece")
    assert meal.nutrition.total.protein_g == Decimal("25.12")


def test_nutrition_converts_millilitres_and_applies_edible_ratio(ingredients):
    milk = replace(ingredients.by_id("milk"), edible_ratio=Decimal("0.8"))
    milk_steps = (
        "Nalej {main.amount} {main.name} do hrnca.",
        "Zohrievaj {main.amount} {main.name} v hrnci 5 minút na miernom ohni, "
        "kým sa z mlieka začne pariť.",
        "Odstav hrniec a nechaj mlieko 2 minúty chladnúť, kým prestane bublať.",
        "Rozdeľ nápoj na {portions} porcie a podávaj ho teplý.",
    )
    candidate = _candidate(
        milk,
        amount="100",
        unit="ml",
        name_template="Teplé {main.name}",
        instructions=milk_steps,
    )

    meal = render_meal(candidate, adults=4, children=0, covered_days=1)

    assert meal.ingredients[0].quantity == Quantity(Decimal("400"), "ml")
    assert meal.nutrition.total.protein_g == Decimal("10.080")


@pytest.mark.parametrize(
    ("ingredient_id", "unit", "missing_field"),
    [
        ("egg", "piece", "grams_per_piece"),
        ("milk", "ml", "density_g_per_ml"),
    ],
)
def test_missing_nutrition_conversion_rejects_candidate(
    ingredients, ingredient_id, unit, missing_field
):
    ingredient = replace(ingredients.by_id(ingredient_id), **{missing_field: None})
    candidate = _candidate(
        ingredient,
        amount="1",
        unit=unit,
        instructions=(
            "Priprav {main.amount} {main.name} v mise.",
            "Zohrievaj {main.amount} {main.name} v hrnci 5 minút na miernom "
            "ohni, kým sa začne pariť.",
            "Rozdeľ jedlo na {portions} porcie a podávaj ho teplé.",
        ),
    )

    with pytest.raises(ValueError, match="prepočet|conversion"):
        render_meal(candidate, adults=1, children=0, covered_days=1)


@pytest.mark.parametrize(
    ("broken_steps", "message"),
    [
        (("Priprav suroviny.", "Uvar jedlo.", "Podávaj."), "všeobecn|neurčit"),
        (("Nakrájaj {main.amount} {main.name.", *RICE_STEPS[1:]), "placeholder"),
        (("Nakrájaj {main.amount} {main.temperature}.", *RICE_STEPS[1:]), "placeholder"),
        (("Prepláchni {main.amount} {main.name} a potom ju sceďok.", *RICE_STEPS[1:]), "ryž|sceď"),
    ],
)
def test_renderer_rejects_generic_steps_bad_placeholders_and_czechisms(
    ingredients, broken_steps, message
):
    candidate = _candidate(
        ingredients.by_id("rice"),
        amount="75",
        name_template="Absorpčne varená {main.name}",
        equipment=("hrniec", "sitko"),
        pantry_basics=("water", "salt"),
        instructions=broken_steps,
    )

    with pytest.raises(ValueError, match=message):
        render_meal(candidate, adults=4, children=0, covered_days=1)


def test_renderer_rejects_malformed_chicken_thigh_inflection(ingredients):
    candidate = _candidate(
        ingredients.by_id("chicken_thigh"),
        amount="225",
        name_template="Pečené {main.name}",
        equipment=("rúra", "pekáč"),
        pantry_basics=("salt", "black_pepper"),
        instructions=(
            "Osuš 900 g stehenných rezíkov papierovou utierkou.",
            *CHICKEN_STEPS[1:],
        ),
    )

    with pytest.raises(ValueError, match="rezíkov|slovenč"):
        render_meal(candidate, adults=4, children=0, covered_days=1)


def test_renderer_rejects_ingredient_used_in_steps_but_missing_from_list(ingredients):
    candidate = _candidate(
        ingredients.by_id("rice"),
        amount="75",
        name_template="Absorpčne varená {main.name}",
        equipment=("hrniec", "sitko"),
        pantry_basics=("water", "salt"),
        instructions=(*RICE_STEPS[:-1], "Rozdeľ ryžu a mrkvu na {portions} porcie."),
    )

    with pytest.raises(ValueError, match="mrkva|zozname surovín"):
        render_meal(candidate, adults=4, children=0, covered_days=1)


def test_renderer_rejects_unknown_measured_ingredient_missing_from_list(ingredients):
    candidate = _candidate(
        ingredients.by_id("tofu"),
        amount="160",
        cut="na 2 cm kocky",
        name_template="Chrumkavé {main.name} z panvice",
        equipment=("panvica", "doska"),
        pantry_basics=("oil",),
        instructions=(
            *TOFU_STEPS[:-1],
            "Pridaj do panvice 5 g šafranu.",
            TOFU_STEPS[-1],
        ),
    )

    with pytest.raises(ValueError, match="šafran|zozname surovín"):
        render_meal(candidate, adults=4, children=0, covered_days=1)


def test_renderer_rejects_unknown_added_ingredient_without_amount(ingredients):
    candidate = _candidate(
        ingredients.by_id("tofu"),
        amount="160",
        cut="na 2 cm kocky",
        name_template="Chrumkavé {main.name} z panvice",
        equipment=("panvica", "doska"),
        pantry_basics=("oil",),
        instructions=(
            *TOFU_STEPS[:-1],
            "Pridaj šafran do panvice.",
            TOFU_STEPS[-1],
        ),
    )

    with pytest.raises(ValueError, match="šafran|zozname surovín"):
        render_meal(candidate, adults=4, children=0, covered_days=1)


def test_renderer_rejects_unknown_ingredient_mixed_with_allowed_ingredient(
    ingredients,
):
    candidate = _candidate(
        ingredients.by_id("tofu"),
        amount="160",
        cut="na 2 cm kocky",
        name_template="Chrumkavé {main.name} z panvice",
        equipment=("panvica", "doska"),
        pantry_basics=("oil",),
        instructions=(
            *TOFU_STEPS[:-1],
            "Pridaj šafran k tofu.",
            TOFU_STEPS[-1],
        ),
    )

    with pytest.raises(ValueError, match="šafran|zozname surovín"):
        render_meal(candidate, adults=4, children=0, covered_days=1)


@pytest.mark.parametrize(
    "addition",
    [
        "Pridaj šafran spolu s tofu.",
        "Pridaj šafran s tofu.",
        "Pridaj šafran so 640 g tofu.",
        "Pridaj šafran do hrnca s tofu.",
    ],
)
def test_controlled_addition_grammar_rejects_unknown_source_before_destination(
    ingredients, addition
):
    candidate = _candidate(
        ingredients.by_id("tofu"),
        amount="160",
        cut="na 2 cm kocky",
        name_template="Chrumkavé {main.name} z panvice",
        equipment=("panvica", "doska"),
        pantry_basics=("oil",),
        instructions=(*TOFU_STEPS[:-1], addition, TOFU_STEPS[-1]),
    )

    with pytest.raises(ValueError, match="šafran|zozname surovín"):
        render_meal(candidate, adults=4, children=0, covered_days=1)


@pytest.mark.parametrize(
    "addition",
    [
        "Pridaj štipku soli a premiešaj šafran.",
        "Pridaj štipku soli a potom premiešaj šafran.",
        "Pridaj štipku soli do panvice a premiešaj šafran.",
        "Pridaj štipku soli spolu s tofu a premiešaj šafran.",
    ],
)
def test_controlled_addition_grammar_rejects_undeclared_follow_up_ingredient(
    ingredients, addition
):
    candidate = _candidate(
        ingredients.by_id("tofu"),
        amount="160",
        cut="na 2 cm kocky",
        name_template="Chrumkavé {main.name} z panvice",
        equipment=("panvica", "doska"),
        pantry_basics=("oil", "salt"),
        instructions=(*TOFU_STEPS[:-1], addition, TOFU_STEPS[-1]),
    )

    with pytest.raises(ValueError, match="šafran|zozname surovín"):
        render_meal(candidate, adults=4, children=0, covered_days=1)


@pytest.mark.parametrize(
    "addition",
    [
        "Pridaj štipku soli a premiešaj na miernom ohni so šafranom.",
        "Pridaj štipku soli a nechaj ho 5 minút so šafranom.",
    ],
)
def test_controlled_follow_up_rejects_payload_after_consumed_context(
    ingredients, addition
):
    candidate = _candidate(
        ingredients.by_id("tofu"),
        amount="160",
        cut="na 2 cm kocky",
        name_template="Chrumkavé {main.name} z panvice",
        equipment=("panvica", "doska"),
        pantry_basics=("oil", "salt"),
        instructions=(*TOFU_STEPS[:-1], addition, TOFU_STEPS[-1]),
    )

    with pytest.raises(ValueError, match="šafran|zozname surovín"):
        render_meal(candidate, adults=4, children=0, covered_days=1)


@pytest.mark.parametrize(
    "addition",
    [
        "Pridaj štipku soli a premiešaj.",
        "Pridaj štipku soli a potom premiešaj.",
        "Pridaj štipku soli do panvice a premiešaj.",
        "Pridaj štipku soli spolu s tofu a premiešaj.",
        "Pridaj štipku soli s tofu a premiešaj.",
        "Pridaj štipku soli so 640 g tofu a premiešaj.",
        "Pridaj štipku soli a premiešaj tofu.",
        "Pridaj štipku soli a premiešaj na miernom ohni.",
        "Pridaj štipku soli a nechaj ho 5 minút.",
    ],
)
def test_controlled_addition_grammar_accepts_follow_up_imperative(
    ingredients, addition
):
    candidate = _candidate(
        ingredients.by_id("tofu"),
        amount="160",
        cut="na 2 cm kocky",
        name_template="Chrumkavé {main.name} z panvice",
        equipment=("panvica", "doska"),
        pantry_basics=("oil", "salt"),
        instructions=(*TOFU_STEPS[:-1], addition, TOFU_STEPS[-1]),
    )

    meal = render_meal(candidate, adults=4, children=0, covered_days=1)

    assert meal.pantry_basics == ("olej", "soľ")


def test_renderer_rejects_unknown_prepared_ingredient_without_amount(ingredients):
    candidate = _candidate(
        ingredients.by_id("tofu"),
        amount="160",
        cut="na 2 cm kocky",
        name_template="Chrumkavé {main.name} z panvice",
        equipment=("panvica", "doska"),
        pantry_basics=("oil",),
        instructions=(
            *TOFU_STEPS[:-1],
            "Nakrájaj šafran na doske.",
            TOFU_STEPS[-1],
        ),
    )

    with pytest.raises(ValueError, match="šafran|zozname surovín"):
        render_meal(candidate, adults=4, children=0, covered_days=1)


def test_water_is_a_visible_declared_pantry_basic(ingredients):
    candidate = _candidate(
        ingredients.by_id("rice"),
        amount="75",
        name_template="Absorpčne varená {main.name}",
        equipment=("hrniec", "sitko"),
        pantry_basics=("water", "salt"),
        instructions=RICE_STEPS,
    )

    meal = render_meal(candidate, adults=4, children=0, covered_days=1)

    assert meal.pantry_basics == ("voda", "soľ")
    assert meal.ingredients[0].quantity == Quantity(Decimal("300"), "g")
    assert "450 ml vody" in meal.instructions[1]


def test_absorption_water_scales_with_household_and_covered_days(ingredients):
    candidate = _candidate(
        ingredients.by_id("rice"),
        amount="75",
        name_template="Absorpčne varená {main.name}",
        equipment=("hrniec", "sitko"),
        pantry_basics=("water", "salt"),
        instructions=RICE_STEPS,
    )

    meal = render_meal(candidate, adults=4, children=0, covered_days=3)

    assert meal.ingredients[0].quantity == Quantity(Decimal("900"), "g")
    assert "1,4 l vody" in meal.instructions[1]
    assert "450 ml vody" not in meal.instructions[1]


def test_renderer_rejects_inconsistent_selected_ingredient_name(ingredients):
    candidate = _candidate(
        ingredients.by_id("chicken_thigh"),
        amount="225",
        name_template="Pečené {main.name}",
        equipment=("rúra", "pekáč"),
        pantry_basics=("salt", "black_pepper"),
        instructions=(
            "Osuš 900 g kuracích pŕs papierovou utierkou a osoľ ich.",
            *CHICKEN_STEPS[1:],
        ),
    )

    with pytest.raises(ValueError, match="kuracie prsia|zozname surovín"):
        render_meal(candidate, adults=4, children=0, covered_days=1)


@pytest.mark.parametrize(
    ("cooking_step", "message"),
    [
        (
            "Opekaj {main.amount} {main.name} 8 minút na strednom ohni, "
            "kým budú všetky strany zlatisté.",
            "nádob|panvic",
        ),
        (
            "Opekaj {main.amount} {main.name} v panvici 8 minút, kým budú "
            "všetky strany zlatisté.",
            "ohrev|teplot",
        ),
        (
            "Opekaj {main.amount} {main.name} v panvici na strednom ohni, "
            "kým budú všetky strany zlatisté.",
            "čas|minút",
        ),
        (
            "Opekaj {main.amount} {main.name} v panvici 8 minút na strednom ohni.",
            "hotov|výsled",
        ),
    ],
)
def test_each_cooking_step_requires_vessel_heat_time_and_doneness_cue(
    ingredients, cooking_step, message
):
    candidate = _candidate(
        ingredients.by_id("tofu"),
        amount="160",
        cut="na 2 cm kocky",
        name_template="Chrumkavé {main.name} z panvice",
        equipment=("panvica", "doska"),
        pantry_basics=("oil",),
        instructions=(*TOFU_STEPS[:3], cooking_step, TOFU_STEPS[-1]),
    )

    with pytest.raises(ValueError, match=message):
        render_meal(candidate, adults=4, children=0, covered_days=1)


def test_cooking_step_accepts_a_natural_ingredient_name_without_repeating_amount(
    ingredients,
):
    candidate = _candidate(
        ingredients.by_id("tofu"),
        amount="160",
        cut="na 2 cm kocky",
        name_template="Chrumkavé {main.name} z panvice",
        equipment=("panvica", "doska"),
        pantry_basics=("oil",),
        instructions=(
            "Osuš {main.amount} {main.name} a nakrájaj ho {main.cut}.",
            "Opekaj {main.name} v panvici 8 minút na strednom ohni, kým budú "
            "všetky strany zlatisté.",
            "Rozdeľ tofu na {portions} porcie a podávaj ho teplé.",
        ),
    )

    meal = render_meal(candidate, adults=4, children=0, covered_days=1)

    assert meal.instructions[1] == (
        "Opekaj tofu v panvici 8 minút na strednom ohni, kým budú všetky "
        "strany zlatisté."
    )


@pytest.mark.parametrize(
    ("ingredient_id", "incomplete_step", "other_steps"),
    [
        (
            "rice",
            "Uvar {main.amount} {main.name}.",
            (
                "Prepláchni {main.amount} {main.name} v sitku pod studenou "
                "vodou, kým odtekajúca voda nebude takmer číra.",
                "Rozdeľ ryžu na {portions} porcie a podávaj ju horúcu.",
            ),
        ),
        (
            "zucchini",
            "Upeč {main.amount} {main.name}.",
            (
                "Nakrájaj {main.amount} {main.name} na doske na 1 cm kúsky.",
                "Rozdeľ cuketu na {portions} porcie a podávaj ju horúcu.",
            ),
        ),
        (
            "milk",
            "Priveď {main.amount} {main.name} do varu.",
            (
                "Nalej {main.amount} {main.name} do hrnca.",
                "Rozdeľ nápoj na {portions} porcie a podávaj ho teplý.",
            ),
        ),
    ],
)
def test_prefixed_cooking_imperatives_cannot_bypass_step_detail_validation(
    ingredients, ingredient_id, incomplete_step, other_steps
):
    candidate = _candidate(
        ingredients.by_id(ingredient_id),
        instructions=(other_steps[0], incomplete_step, other_steps[1]),
    )

    with pytest.raises(ValueError, match="nádob|ohrev|teplot|čas|minút|hotov|výsled"):
        render_meal(candidate, adults=4, children=0, covered_days=1)


def test_boiling_result_does_not_replace_an_explicit_heat_level(ingredients):
    candidate = _candidate(
        ingredients.by_id("milk"),
        amount="500",
        unit="ml",
        instructions=(
            "Nalej {main.amount} {main.name} do hrnca.",
            "Priveď {main.amount} {main.name} v hrnci za 5 minút do varu, "
            "kým začne súvislo bublať.",
            "Rozdeľ nápoj na {portions} porcie a podávaj ho teplý.",
        ),
    )

    with pytest.raises(ValueError, match="ohrev|teplot"):
        render_meal(candidate, adults=4, children=0, covered_days=1)


def test_bare_kym_without_observable_result_is_not_a_doneness_cue(ingredients):
    candidate = _candidate(
        ingredients.by_id("tofu"),
        amount="160",
        cut="na 2 cm kocky",
        name_template="Chrumkavé {main.name} z panvice",
        equipment=("panvica", "doska"),
        pantry_basics=("oil",),
        instructions=(
            *TOFU_STEPS[:3],
            "Opekaj {main.amount} {main.name} v panvici 8 minút na strednom "
            "ohni, kým môžeš pokračovať.",
            TOFU_STEPS[-1],
        ),
    )

    with pytest.raises(ValueError, match="hotov|výsled"):
        render_meal(candidate, adults=4, children=0, covered_days=1)


@pytest.mark.parametrize(
    ("ingredient_id", "amount", "instructions"),
    [
        (
            "tofu",
            "160",
            (
                "Osuš {main.amount} {main.name} čistou utierkou.",
                "Opekaj {main.amount} {main.name} v horúcej panvici 8 minút "
                "na strednom ohni.",
                "Rozdeľ tofu na {portions} porcie a podávaj ho ihneď.",
            ),
        ),
        (
            "tomato",
            "150",
            (
                "Opláchni {main.amount} {main.name} v sitku pod studenou vodou.",
                "Var {main.amount} {main.name} v hrnci 8 minút na strednom ohni.",
                "Rozdeľ paradajky na {portions} porcie a podávaj ich teplé.",
            ),
        ),
    ],
)
def test_context_words_are_not_observable_doneness_cues(
    ingredients, ingredient_id, amount, instructions
):
    candidate = _candidate(
        ingredients.by_id(ingredient_id),
        amount=amount,
        instructions=instructions,
    )

    with pytest.raises(ValueError, match="hotov|výsled"):
        render_meal(candidate, adults=4, children=0, covered_days=1)


def test_preheating_does_not_require_an_appliance_specific_readiness_cue(
    ingredients,
):
    candidate = _candidate(
        ingredients.by_id("zucchini"),
        amount="150",
        cut="na polkolieska",
        name_template="Pečená {main.name}",
        equipment=("rúra", "plech", "doska"),
        pantry_basics=("oil", "salt"),
        instructions=(
            "Predhrej rúru na 200 °C.",
            *ROASTED_VEGETABLE_STEPS[1:],
        ),
    )

    meal = render_meal(candidate, adults=4, children=0, covered_days=1)

    assert meal.instructions[0] == "Predhrej rúru na 200 °C."


@pytest.mark.parametrize(
    ("adults", "children", "covered_days"),
    [
        (0, 0, 1),
        (-1, 1, 1),
        (1, -1, 1),
        (True, 0, 1),
        (1, 0, 0),
        (1, 0, 4),
    ],
)
def test_renderer_rejects_invalid_household_or_coverage(
    ingredients, adults, children, covered_days
):
    candidate = _candidate(
        ingredients.by_id("rice"),
        name_template="Absorpčne varená {main.name}",
        equipment=("hrniec", "sitko"),
        pantry_basics=("water", "salt"),
        instructions=RICE_STEPS,
    )

    with pytest.raises(ValueError):
        render_meal(
            candidate,
            adults=adults,
            children=children,
            covered_days=covered_days,
        )
