from collections import Counter
from decimal import Decimal, ROUND_CEILING
import json
from pathlib import Path
import re
import unicodedata

import pytest

from app.ingredient_catalog import load_ingredient_catalog
from app.recipe_candidates import validate_candidate
from app.recipe_catalog import load_recipe_catalog
from app.recipe_matcher import RecipeCandidate, SlotSelection
from app.recipe_renderer import render_meal


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CANDIDATE_ROOT = PROJECT_ROOT / "app" / "catalog" / "candidates"
TARGETS_PATH = PROJECT_ROOT / "docs" / "research" / "recipe-targets.json"

CLASSIC_IDS = (
    "classic_chicken_paprikash",
    "classic_chicken_perkelt",
    "classic_slovak_chicken_risotto",
    "classic_roast_chicken_thighs_potatoes",
    "classic_chicken_noodle_soup",
    "classic_chicken_sote_rice",
    "classic_pork_natural_rice",
    "classic_pork_shoulder_onion",
    "classic_segedin_goulash",
    "classic_pork_perkelt",
    "classic_french_potatoes",
    "classic_meatballs_mash",
    "classic_stuffed_peppers",
    "classic_beef_goulash",
    "classic_tomato_meatballs",
    "classic_bolognese_spaghetti",
    "classic_baked_pasta_ham",
    "classic_pork_cabbage_bake",
    "classic_meatloaf_potatoes",
    "classic_roast_pork_root_veg",
    "classic_beef_barley_soup",
    "classic_goulash_soup",
    "classic_potato_stew_egg",
    "classic_lentil_stew_egg",
    "classic_bean_stew_egg",
    "classic_pumpkin_stew_egg",
    "classic_pea_stew_egg",
    "classic_lecho_egg",
    "classic_granadir",
    "classic_cabbage_noodles",
    "classic_bryndza_dumplings",
    "classic_cabbage_dumplings",
    "classic_potato_pancakes",
    "classic_sauerkraut_soup",
    "classic_bean_soup",
    "classic_sour_lentil_soup",
    "classic_potato_soup",
    "classic_garlic_soup",
    "classic_tomato_soup",
    "classic_cauliflower_soup",
    "classic_rice_pudding",
    "classic_apple_bread_pudding",
)

# Each inner set is one defining ingredient choice. The recipe must declare at
# least one member of every set; wording and quantities remain free to evolve.
CHARACTERISTIC_INGREDIENTS = {
    "classic_chicken_paprikash": (
        frozenset({"chicken_thigh"}),
        frozenset({"onion"}),
        frozenset({"paprika_powder"}),
        frozenset({"cream", "plain_yogurt"}),
    ),
    "classic_chicken_perkelt": (
        frozenset({"chicken_thigh"}),
        frozenset({"onion"}),
        frozenset({"paprika_powder"}),
    ),
    "classic_pork_perkelt": (
        frozenset({"pork_shoulder"}),
        frozenset({"onion"}),
        frozenset({"paprika_powder"}),
    ),
    "classic_slovak_chicken_risotto": (
        frozenset({"chicken_breast", "chicken_thigh_meat"}),
        frozenset({"rice"}),
        frozenset({"peas", "carrot", "bell_pepper"}),
    ),
    "classic_segedin_goulash": (
        frozenset({"pork_shoulder"}),
        frozenset({"sauerkraut"}),
        frozenset({"paprika_powder"}),
        frozenset({"cream", "plain_yogurt"}),
    ),
    "classic_french_potatoes": (
        frozenset({"potato"}),
        frozenset({"egg"}),
        frozenset({"smoked_sausage", "ham"}),
        frozenset({"milk", "cream", "plain_yogurt"}),
    ),
    "classic_pork_natural_rice": (
        frozenset({"pork_loin"}),
        frozenset({"rice"}),
    ),
    "classic_beef_goulash": (
        frozenset({"beef_chuck"}),
        frozenset({"onion"}),
        frozenset({"paprika_powder"}),
    ),
    "classic_potato_stew_egg": (
        frozenset({"potato"}),
        frozenset({"egg"}),
        frozenset({"milk", "cream", "plain_yogurt"}),
        frozenset({"wheat_flour"}),
        frozenset({"apple_cider_vinegar"}),
    ),
    "classic_lentil_stew_egg": (
        frozenset({"lentils"}),
        frozenset({"egg"}),
        frozenset({"milk", "cream", "plain_yogurt"}),
        frozenset({"garlic"}),
        frozenset({"apple_cider_vinegar"}),
    ),
    "classic_bean_stew_egg": (
        frozenset({"beans"}),
        frozenset({"egg"}),
        frozenset({"milk", "cream", "plain_yogurt"}),
        frozenset({"garlic"}),
        frozenset({"paprika_powder"}),
        frozenset({"apple_cider_vinegar"}),
    ),
    "classic_pumpkin_stew_egg": (
        frozenset({"pumpkin"}),
        frozenset({"egg"}),
        frozenset({"milk", "cream", "plain_yogurt"}),
        frozenset({"apple_cider_vinegar"}),
    ),
    "classic_pea_stew_egg": (
        frozenset({"dry_peas"}),
        frozenset({"egg"}),
        frozenset({"milk", "cream", "plain_yogurt"}),
        frozenset({"garlic"}),
        frozenset({"marjoram"}),
    ),
    "classic_chicken_noodle_soup": (
        frozenset({"chicken_thigh"}),
        frozenset({"egg_noodles"}),
        frozenset({"carrot"}),
    ),
    "classic_beef_barley_soup": (
        frozenset({"beef_chuck"}),
        frozenset({"barley"}),
        frozenset({"carrot"}),
    ),
    "classic_goulash_soup": (
        frozenset({"beef_chuck"}),
        frozenset({"potato"}),
        frozenset({"paprika_powder"}),
    ),
    "classic_sauerkraut_soup": (
        frozenset({"sauerkraut"}),
        frozenset({"paprika_powder"}),
    ),
    "classic_bean_soup": (
        frozenset({"beans"}),
        frozenset({"carrot"}),
    ),
    "classic_sour_lentil_soup": (frozenset({"lentils"}),),
    "classic_potato_soup": (frozenset({"potato"}),),
    "classic_garlic_soup": (
        frozenset({"garlic"}),
        frozenset({"potato"}),
    ),
    "classic_tomato_soup": (frozenset({"tomato"}),),
    "classic_baked_pasta_ham": (
        frozenset({"pasta"}),
        frozenset({"ham"}),
    ),
    "classic_pork_cabbage_bake": (
        frozenset({"pork_mince"}),
        frozenset({"sauerkraut"}),
        frozenset({"rice"}),
        frozenset({"paprika_powder"}),
        frozenset({"cream", "plain_yogurt"}),
    ),
    "classic_cabbage_noodles": (
        frozenset({"white_cabbage"}),
        frozenset({"pasta", "egg_noodles"}),
    ),
    "classic_bryndza_dumplings": (
        frozenset({"potato"}),
        frozenset({"wheat_flour"}),
        frozenset({"bryndza"}),
    ),
    "classic_cabbage_dumplings": (
        frozenset({"potato"}),
        frozenset({"wheat_flour"}),
        frozenset({"sauerkraut"}),
    ),
    "classic_potato_pancakes": (
        frozenset({"potato"}),
        frozenset({"wheat_flour"}),
        frozenset({"marjoram"}),
    ),
    "classic_cauliflower_soup": (frozenset({"cauliflower"}),),
    "classic_rice_pudding": (
        frozenset({"rice"}),
        frozenset({"milk"}),
        frozenset({"sugar"}),
    ),
    "classic_apple_bread_pudding": (
        frozenset({"apple"}),
        frozenset({"bread"}),
        frozenset({"milk"}),
    ),
}

CHARACTERISTIC_DECLARED_TERMS = {}

PRIVAROK_IDS = (
    "classic_potato_stew_egg",
    "classic_lentil_stew_egg",
    "classic_bean_stew_egg",
    "classic_pumpkin_stew_egg",
    "classic_pea_stew_egg",
)

GARLIC_COOKING_IDS = (
    "classic_chicken_perkelt",
    "classic_chicken_sote_rice",
    "classic_pork_shoulder_onion",
    "classic_pork_perkelt",
)

RICE_IDS = (
    "classic_chicken_sote_rice",
    "classic_pork_cabbage_bake",
    "classic_pork_natural_rice",
    "classic_pork_shoulder_onion",
    "classic_rice_pudding",
    "classic_slovak_chicken_risotto",
    "classic_stuffed_peppers",
)

EXPLICIT_WATER_IDS = (
    "classic_bean_soup",
    "classic_bean_stew_egg",
    "classic_beef_barley_soup",
    "classic_cauliflower_soup",
    "classic_chicken_noodle_soup",
    "classic_garlic_soup",
    "classic_goulash_soup",
    "classic_lentil_stew_egg",
    "classic_pea_stew_egg",
    "classic_potato_soup",
    "classic_sauerkraut_soup",
    "classic_sour_lentil_soup",
    "classic_tomato_soup",
)

LARGE_OVEN_BATCH_IDS = (
    "classic_baked_pasta_ham",
    "classic_french_potatoes",
)

LARGE_BROWNING_BATCH_IDS = (
    "classic_cabbage_noodles",
    "classic_beef_goulash",
    "classic_bolognese_spaghetti",
    "classic_chicken_sote_rice",
)

REQUIRED_RECIPE_EQUIPMENT = {
    "classic_baked_pasta_ham": ("nôž", "doska", "strúhadlo", "metlička"),
    "classic_beef_goulash": ("nôž", "doska"),
    "classic_french_potatoes": ("nôž", "doska"),
    "classic_chicken_noodle_soup": ("nôž", "doska"),
    "classic_chicken_paprikash": ("nôž", "doska"),
    "classic_chicken_perkelt": ("nôž", "doska"),
    "classic_chicken_sote_rice": ("nôž", "doska"),
    "classic_bolognese_spaghetti": ("strúhadlo",),
}

NAMED_PASTA_TERMS = {
    "classic_bolognese_spaghetti": "spaget",
    "classic_cabbage_noodles": "fliac",
}

MAIN_MEAL_LIMITS = {
    "classic_pork_cabbage_bake": (Decimal("550"), Decimal("850")),
    "classic_bryndza_dumplings": (Decimal("430"), Decimal("800")),
    "classic_baked_pasta_ham": (Decimal("320"), Decimal("800")),
    "classic_apple_bread_pudding": (Decimal("550"), Decimal("700")),
}

FLAVOUR_GUARD_IDS = frozenset(
    {
        "classic_chicken_paprikash",
        "classic_chicken_perkelt",
        "classic_pork_perkelt",
        "classic_slovak_chicken_risotto",
        "classic_segedin_goulash",
        "classic_french_potatoes",
        "classic_potato_stew_egg",
        "classic_lentil_stew_egg",
        "classic_bean_stew_egg",
        "classic_pumpkin_stew_egg",
        "classic_pea_stew_egg",
        "classic_chicken_noodle_soup",
        "classic_beef_barley_soup",
        "classic_goulash_soup",
        "classic_sauerkraut_soup",
        "classic_bean_soup",
        "classic_sour_lentil_soup",
        "classic_potato_soup",
        "classic_garlic_soup",
        "classic_tomato_soup",
        "classic_cauliflower_soup",
    }
)

EGG_NAMED_IDS = frozenset(
    {
        "classic_potato_stew_egg",
        "classic_lentil_stew_egg",
        "classic_bean_stew_egg",
        "classic_pumpkin_stew_egg",
        "classic_pea_stew_egg",
        "classic_lecho_egg",
    }
)

SOUP_IDS = frozenset(recipe_id for recipe_id in CLASSIC_IDS if recipe_id.endswith("_soup"))
DRY_SOAK_LEGUMES = frozenset({"beans", "chickpeas"})
DRY_COOK_LEGUMES = frozenset(
    {"beans", "chickpeas", "red_lentils", "lentils", "dry_peas"}
)
CANNED_LEGUMES = frozenset({"beans_canned", "chickpeas_canned"})
COMPLETION_CUE = re.compile(
    r"\b(?:kym|makk\w*|zmak\w*|zlat\w*|chrumk\w*|stuhn\w*|zhust\w*|"
    r"prepec\w*|cira\s+stava|bubl\w*|vsiakn\w*)\b"
)
IMPRactical_LANGUAGE = re.compile(
    r"teplomer|kontrolk|\b74\s*°?\s*c\b|vnutorna\s+teplota"
)
ACCUSATIVE_AFTER_WITH = re.compile(r"\b(?:s|so)\s+\w+ú\b", re.IGNORECASE)
MECHANICAL_LANGUAGE = re.compile(
    r"\bv podobe\b|premiesaj vajce so smotanou|"
    r"premiesaj smotanovu zmes s polievkou|"
    r"premiesaj vychladnutu ryzu s vajcovou zmesou"
)


@pytest.fixture(scope="module")
def ingredient_catalog():
    return load_ingredient_catalog()


@pytest.fixture(scope="module")
def target_rows():
    payload = json.loads(TARGETS_PATH.read_text(encoding="utf-8"))
    return tuple(
        row for row in payload["targets"] if row["editorial_lane"] == "slovak_classic"
    )


def _fold(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value.casefold())
    return "".join(
        character for character in normalized if not unicodedata.combining(character)
    )


def _candidate_path(recipe_id: str) -> Path:
    return CANDIDATE_ROOT / f"{recipe_id}.json"


def _declared_ids(recipe) -> frozenset[str]:
    return frozenset(
        (*recipe.pantry_basics, *(item for slot in recipe.slots for item in slot.candidates))
    )


def _declared_ingredient_text(recipe, ingredients) -> str:
    terms = []
    for ingredient_id in sorted(_declared_ids(recipe) - {"water"}):
        ingredient = ingredients.by_id(ingredient_id)
        terms.extend((ingredient.name, *ingredient.synonyms))
    return _fold(" ".join(terms))


def _load_recipe_with_public_catalog(payload: dict, ingredients, root: Path):
    root.mkdir()
    (root / "manifest.json").write_text(
        json.dumps({"library_version": 1}), encoding="utf-8"
    )
    (root / "candidate.json").write_text(
        json.dumps({"recipes": payload["recipes"]}, ensure_ascii=False),
        encoding="utf-8",
    )
    recipes = load_recipe_catalog(ingredients, root, include_inactive=True).all()
    assert len(recipes) == 1
    return recipes[0]


def _render_candidate(recipe, ingredients) -> RecipeCandidate:
    selections = tuple(
        SlotSelection(
            slot=slot,
            ingredient=ingredients.by_id(slot.candidates[0]),
            offer=None,
            pantry=None,
        )
        for slot in recipe.slots
    )
    return RecipeCandidate(
        template=recipe,
        selections=selections,
        score=Decimal("0"),
        key=f"curated-snapshot:{recipe.id}",
    )


def _load_candidate_recipe(recipe_id: str, ingredients, root: Path):
    payload = json.loads(_candidate_path(recipe_id).read_text(encoding="utf-8"))
    return _load_recipe_with_public_catalog(payload, ingredients, root / recipe_id)


def _rendered_edible_grams(item) -> Decimal:
    if item.quantity.unit == "g":
        grams = item.quantity.amount
    elif item.quantity.unit == "ml":
        grams = item.quantity.amount * item.ingredient.density_g_per_ml
    else:
        grams = item.quantity.amount * item.ingredient.grams_per_piece
    return grams * item.ingredient.edible_ratio


def _assert_legume_workflow(recipe) -> None:
    declared = _declared_ids(recipe)
    text = _fold(" ".join(step.text for step in recipe.instructions))
    for slot in recipe.slots:
        candidates = frozenset(slot.candidates)
        assert not (
            candidates & DRY_COOK_LEGUMES and candidates & CANNED_LEGUMES
        ), f"{recipe.id}:{slot.key} mixes dry and canned legume workflows"
    if declared & DRY_SOAK_LEGUMES:
        assert "namoc" in text, f"{recipe.id}: dry beans/chickpeas must be soaked"
    if declared & DRY_COOK_LEGUMES:
        assert re.search(r"\b(?:uvar|var)\w*\b", text), (
            f"{recipe.id}: dry legumes must be cooked"
        )
    if declared & CANNED_LEGUMES:
        assert "namoc" not in text, f"{recipe.id}: canned legumes must not be soaked"
        assert re.search(r"\b(?:sced|zlej|odkvapk)\w*\b", text), (
            f"{recipe.id}: canned legumes must be drained"
        )


def _assert_readable_render(meal, *, portions: int, days: int) -> None:
    assert meal.portions == portions
    assert meal.covered_days == days
    assert meal.name.strip() and "{" not in meal.name and "}" not in meal.name
    assert len(meal.instructions) >= 3
    assert all(step.strip() and step.rstrip()[-1] in ".!?" for step in meal.instructions)

    text = " ".join((meal.name, *meal.instructions))
    folded = _fold(text)
    assert IMPRactical_LANGUAGE.search(folded) is None
    assert COMPLETION_CUE.search(folded), f"{meal.template_id}: missing completion cue"

    amount_counts = Counter(item.display_amount for item in meal.ingredients)
    for amount, allowed_mentions in amount_counts.items():
        assert text.count(amount) <= allowed_mentions, (
            f"{meal.template_id}: repeated rendered amount {amount}"
        )

    upper_bounds = {
        "g": Decimal("1000") * portions,
        "ml": Decimal("1000") * portions,
        "piece": Decimal("12") * portions,
    }
    for item in meal.ingredients:
        assert Decimal("0") < item.quantity.amount <= upper_bounds[item.quantity.unit], (
            meal.template_id,
            item.ingredient.id,
            item.quantity,
        )


@pytest.mark.parametrize("recipe_id", CLASSIC_IDS, ids=CLASSIC_IDS)
def test_curated_slovak_classic_candidate_snapshot(
    recipe_id,
    ingredient_catalog,
    target_rows,
    tmp_path,
):
    path = _candidate_path(recipe_id)
    assert path.is_file(), f"{recipe_id}: candidate_missing: {path}"

    assert len(CLASSIC_IDS) == 42
    assert len(set(CLASSIC_IDS)) == 42
    assert tuple(row["id"] for row in target_rows) == CLASSIC_IDS
    target = next(row for row in target_rows if row["id"] == recipe_id)

    report = validate_candidate(path, ingredient_catalog)
    assert report.errors == (), f"{recipe_id}: {report.errors}"
    assert report.recipe_ids == (recipe_id,)

    payload = json.loads(path.read_text(encoding="utf-8"))
    expected_source = {
        "recipe_id": recipe_id,
        "editorial_lane": "slovak_classic",
        "core": True,
        "references": target["references"],
    }
    assert payload["source_record"] == expected_source

    recipe = _load_recipe_with_public_catalog(
        payload, ingredient_catalog, tmp_path / "catalog"
    )
    assert recipe.id == recipe_id == path.stem == payload["source_record"]["recipe_id"]
    assert recipe.version == 2
    assert recipe.active is True
    assert recipe.modes == frozenset(target["expected_modes"])
    assert payload["source_record"]["editorial_lane"] == target["editorial_lane"]
    assert payload["source_record"]["core"] is target["core"] is True

    declared = _declared_ids(recipe)
    for alternatives in CHARACTERISTIC_INGREDIENTS.get(recipe_id, ()):
        assert declared & alternatives, (
            f"{recipe_id}: missing characteristic ingredient; expected one of "
            f"{sorted(alternatives)}"
        )

    declared_text = _declared_ingredient_text(recipe, ingredient_catalog)
    for alternatives in CHARACTERISTIC_DECLARED_TERMS.get(recipe_id, ()):
        assert any(_fold(term) in declared_text for term in alternatives), (
            f"{recipe_id}: declared ingredients must express one of {alternatives}"
        )

    recipe_text = _fold(
        " ".join(
            (
                recipe.name_template,
                *(step.text for step in recipe.instructions),
                recipe.storage.instruction,
            )
        )
    )
    if recipe_id in FLAVOUR_GUARD_IDS:
        assert {"curry_powder", "oregano"}.isdisjoint(declared)
        assert re.search(r"\b(?:kari|oregano)\b", recipe_text) is None
    if recipe_id in SOUP_IDS:
        assert recipe.method == "soup"
        assert "water" in recipe.pantry_basics
    if recipe_id in EGG_NAMED_IDS:
        assert "egg" in declared

    if "egg" in declared or declared & {
        "milk",
        "cream",
        "plain_yogurt",
        "cottage_cheese",
        "hard_cheese",
        "feta",
        "bryndza",
        "butter",
    }:
        assert "vegan" not in recipe.modes

    if recipe_id == "classic_roast_chicken_thighs_potatoes":
        thigh_slots = tuple(
            slot for slot in recipe.slots if "chicken_thigh" in slot.candidates
        )
        assert thigh_slots, "bone-in roast must use chicken_thigh"
        assert all(slot.cut is None for slot in thigh_slots)
        assert all(
            f"{{{slot.key}.cut}}" not in step.text
            for slot in thigh_slots
            for step in recipe.instructions
        )
        assert re.search(r"\bkock\w*|\bkociek\b", recipe_text) is None
        assert "pri kosti" in recipe_text
        assert "nie je ruzov" in recipe_text
        assert "cira stava" in recipe_text

    _assert_legume_workflow(recipe)
    source_instructions = " ".join(step.text for step in recipe.instructions)
    for slot in recipe.slots:
        assert source_instructions.count(f"{{{slot.key}.amount}}") <= 1, (
            f"{recipe_id}:{slot.key} repeats its exact amount"
        )

    candidate = _render_candidate(recipe, ingredient_catalog)
    one_day = render_meal(candidate, adults=1, children=0, covered_days=1)
    _assert_readable_render(one_day, portions=1, days=1)

    if recipe.storage.refrigerated_days >= 3:
        three_days = render_meal(candidate, adults=4, children=0, covered_days=3)
        _assert_readable_render(three_days, portions=12, days=3)
        assert three_days.storage == recipe.storage.instruction
        for slot, one_item, batch_item in zip(
            recipe.slots, one_day.ingredients, three_days.ingredients, strict=True
        ):
            assert batch_item.quantity.unit == one_item.quantity.unit
            if batch_item.quantity.unit == "piece":
                expected = (slot.amount_per_adult * 12).to_integral_value(
                    rounding=ROUND_CEILING
                )
            else:
                expected = one_item.quantity.amount * 12
            assert batch_item.quantity.amount == expected


@pytest.mark.parametrize("recipe_id", CLASSIC_IDS, ids=CLASSIC_IDS)
def test_classic_recipe_measures_cooking_oil_when_used(
    recipe_id, ingredient_catalog, tmp_path
):
    recipe = _load_candidate_recipe(recipe_id, ingredient_catalog, tmp_path)
    instruction_text = _fold(" ".join(step.text for step in recipe.instructions))
    if "olej" not in instruction_text:
        return

    oil_slots = tuple(slot for slot in recipe.slots if "oil" in slot.candidates)
    assert oil_slots, f"{recipe_id}: cooking oil must be a measured slot"
    assert "oil" not in recipe.pantry_basics


@pytest.mark.parametrize("recipe_id", CLASSIC_IDS, ids=CLASSIC_IDS)
def test_classic_recipe_never_renders_a_fractional_egg(
    recipe_id, ingredient_catalog, tmp_path
):
    recipe = _load_candidate_recipe(recipe_id, ingredient_catalog, tmp_path)
    if not any("egg" in slot.candidates for slot in recipe.slots):
        return
    covered_days = min(3, recipe.storage.refrigerated_days)
    meal = render_meal(
        _render_candidate(recipe, ingredient_catalog),
        adults=2,
        children=2,
        covered_days=covered_days,
    )
    egg = next(item for item in meal.ingredients if item.ingredient.id == "egg")

    assert egg.quantity.amount == egg.quantity.amount.to_integral_value()


@pytest.mark.parametrize("recipe_id", PRIVAROK_IDS, ids=PRIVAROK_IDS)
def test_privarok_batch_uses_the_same_whole_egg_count_everywhere(
    recipe_id, ingredient_catalog, tmp_path
):
    recipe = _load_candidate_recipe(recipe_id, ingredient_catalog, tmp_path)
    meal = render_meal(
        _render_candidate(recipe, ingredient_catalog),
        adults=2,
        children=2,
        covered_days=3,
    )
    egg = next(item for item in meal.ingredients if item.ingredient.id == "egg")

    assert egg.quantity.amount == egg.quantity.amount.to_integral_value()
    assert egg.display_amount == f"{int(egg.quantity.amount)} ks"


def test_child_only_spice_amount_never_displays_as_zero(
    ingredient_catalog, tmp_path
):
    recipe = _load_candidate_recipe(
        "classic_pea_stew_egg", ingredient_catalog, tmp_path
    )
    meal = render_meal(
        _render_candidate(recipe, ingredient_catalog),
        adults=0,
        children=1,
        covered_days=1,
    )
    marjoram = next(
        item for item in meal.ingredients if item.ingredient.id == "marjoram"
    )

    assert marjoram.quantity.amount > 0
    assert marjoram.display_amount != "0 g"


@pytest.mark.parametrize("recipe_id", PRIVAROK_IDS, ids=PRIVAROK_IDS)
def test_privarok_egg_step_uses_the_declared_small_pot(
    recipe_id, ingredient_catalog, tmp_path
):
    recipe = _load_candidate_recipe(recipe_id, ingredient_catalog, tmp_path)
    egg_step = next(step.text for step in recipe.instructions if "egg:raw" in step.requires)

    assert "v malom hrnci" in egg_step


def test_pumpkin_stew_removes_skin_and_seeds_before_grating(
    ingredient_catalog, tmp_path
):
    recipe = _load_candidate_recipe(
        "classic_pumpkin_stew_egg", ingredient_catalog, tmp_path
    )
    preparation = _fold(recipe.instructions[0].text)

    assert "osup" in preparation
    assert "semen" in preparation


@pytest.mark.parametrize(
    "recipe_id",
    (
        "classic_meatballs_mash",
        "classic_meatloaf_potatoes",
        "classic_stuffed_peppers",
        "classic_tomato_meatballs",
    ),
)
def test_minced_meat_mixture_has_an_egg_binder(
    recipe_id, ingredient_catalog, tmp_path
):
    recipe = _load_candidate_recipe(recipe_id, ingredient_catalog, tmp_path)
    egg_slots = tuple(slot for slot in recipe.slots if "egg" in slot.candidates)
    workflow = tuple(recipe.instructions)

    assert len(egg_slots) == 1
    assert any("egg:raw" in step.requires for step in workflow)
    assert any("egg:served" in step.produces for step in workflow)


def test_meatballs_mash_uses_butter_in_the_mash(
    ingredient_catalog, tmp_path
):
    recipe = _load_candidate_recipe(
        "classic_meatballs_mash", ingredient_catalog, tmp_path
    )
    butter = next(slot for slot in recipe.slots if slot.key == "butter")
    mash_step = next(
        step for step in recipe.instructions if "potato:mashed" in step.produces
    )
    serve_step = recipe.instructions[-1]

    assert butter.candidates == ("butter",)
    assert "{butter.amount}" in mash_step.text
    assert "butter:raw" in mash_step.requires
    assert "butter:combined" in mash_step.produces
    assert "butter:combined" in serve_step.requires


def test_apple_bread_pudding_uses_declared_cinnamon(
    ingredient_catalog, tmp_path
):
    recipe = _load_candidate_recipe(
        "classic_apple_bread_pudding", ingredient_catalog, tmp_path
    )
    cinnamon = next(slot for slot in recipe.slots if slot.key == "cinnamon")
    text = " ".join(step.text for step in recipe.instructions)
    serve_step = recipe.instructions[-1]

    assert cinnamon.candidates == ("cinnamon",)
    assert "{cinnamon.amount}" in text
    assert any("cinnamon:raw" in step.requires for step in recipe.instructions)
    assert "cinnamon:served" in serve_step.produces


def test_stuffed_peppers_are_counted_as_whole_peppers(
    ingredient_catalog, tmp_path
):
    recipe = _load_candidate_recipe(
        "classic_stuffed_peppers", ingredient_catalog, tmp_path
    )
    pepper_slot = next(slot for slot in recipe.slots if slot.key == "pepper")
    pepper = ingredient_catalog.by_id("bell_pepper")

    assert pepper_slot.unit == "piece"
    assert pepper_slot.amount_per_adult == Decimal("2")
    assert pepper.grams_per_piece is not None


def test_stuffed_peppers_have_a_classic_thickened_seasoned_sauce(
    ingredient_catalog, tmp_path
):
    recipe = _load_candidate_recipe(
        "classic_stuffed_peppers", ingredient_catalog, tmp_path
    )
    declared = _declared_ids(recipe)
    recipe_text = _fold(" ".join(step.text for step in recipe.instructions))

    assert {"wheat_flour", "sugar", "marjoram"} <= declared
    assert "zapraz" in recipe_text
    assert "omack" in recipe_text


def test_stuffed_pepper_filling_fits_the_declared_whole_peppers(
    ingredient_catalog, tmp_path
):
    recipe = _load_candidate_recipe(
        "classic_stuffed_peppers", ingredient_catalog, tmp_path
    )
    slots = {slot.key: slot for slot in recipe.slots}
    filling_grams = (
        slots["beef"].amount_per_adult
        + slots["rice"].amount_per_adult * 3
        + slots["egg"].amount_per_adult * 50
        + slots["onion"].amount_per_adult
    )

    assert filling_grams <= slots["pepper"].amount_per_adult * 130


def test_pork_shoulder_is_explicitly_boneless(ingredient_catalog):
    ingredient = ingredient_catalog.by_id("pork_shoulder")

    assert "bez kosti" in _fold(ingredient.name)


def test_roast_pork_uses_even_chunks_so_time_is_not_tied_to_one_roast_size(
    ingredient_catalog, tmp_path
):
    recipe = _load_candidate_recipe(
        "classic_roast_pork_root_veg", ingredient_catalog, tmp_path
    )
    meat = next(slot for slot in recipe.slots if slot.key == "meat")
    text = _fold(" ".join(step.text for step in recipe.instructions))

    assert meat.cut is not None
    assert "5 cm" in _fold(meat.cut)
    assert "potom ho nakrajaj" not in text


def test_chicken_perkelt_prepares_vegetables_before_paprika_touches_the_heat(
    ingredient_catalog, tmp_path
):
    recipe = _load_candidate_recipe(
        "classic_chicken_perkelt", ingredient_catalog, tmp_path
    )
    source_texts = tuple(step.text for step in recipe.instructions)
    texts = tuple(_fold(text) for text in source_texts)
    vegetable_prep = next(
        index
        for index, text in enumerate(source_texts)
        if "{pepper.amount}" in text and "Nakrájaj" in text
    )
    ground_paprika = next(
        index
        for index, step in enumerate(recipe.instructions)
        if "paprika:raw" in step.requires
    )

    assert vegetable_prep < ground_paprika
    assert "odstav" in texts[ground_paprika]


@pytest.mark.parametrize(
    "recipe_id",
    (
        "classic_chicken_sote_rice",
        "classic_chicken_paprikash",
        "classic_goulash_soup",
        "classic_pork_shoulder_onion",
    ),
)
def test_ground_paprika_is_added_only_after_the_hot_vessel_is_removed(
    recipe_id, ingredient_catalog, tmp_path
):
    recipe = _load_candidate_recipe(recipe_id, ingredient_catalog, tmp_path)
    paprika_step = next(
        step
        for step in recipe.instructions
        if "prisyp" in _fold(step.text) and "paprik" in _fold(step.text)
    )

    assert "odstav" in _fold(paprika_step.text)


@pytest.mark.parametrize(
    ("recipe_id", "expected_guidance"),
    (
        ("classic_tomato_meatballs", "dva alebo viac velkych hrncov"),
        ("classic_beef_goulash", "dva alebo viac velkych hrncov"),
    ),
)
def test_large_family_batches_warn_for_every_capacity_limited_pot_phase(
    recipe_id, expected_guidance, ingredient_catalog, tmp_path
):
    recipe = _load_candidate_recipe(recipe_id, ingredient_catalog, tmp_path)
    meal = render_meal(
        _render_candidate(recipe, ingredient_catalog),
        adults=4,
        children=0,
        covered_days=3,
    )

    assert expected_guidance in _fold(" ".join(meal.instructions))


def test_large_french_potatoes_batch_has_one_baking_dish_warning(
    ingredient_catalog, tmp_path
):
    recipe = _load_candidate_recipe(
        "classic_french_potatoes", ingredient_catalog, tmp_path
    )
    meal = render_meal(
        _render_candidate(recipe, ingredient_catalog),
        adults=4,
        children=0,
        covered_days=3,
    )
    instructions = _fold(" ".join(meal.instructions))

    assert instructions.count("dva pekace") == 1


def test_large_meatloaf_batch_is_split_before_shaping(
    ingredient_catalog, tmp_path
):
    recipe = _load_candidate_recipe(
        "classic_meatloaf_potatoes", ingredient_catalog, tmp_path
    )
    meal = render_meal(
        _render_candidate(recipe, ingredient_catalog),
        adults=4,
        children=0,
        covered_days=3,
    )
    steps = tuple(_fold(step) for step in meal.instructions)
    split_index = next(index for index, step in enumerate(steps) if "dva pekace" in step)
    shape_index = next(index for index, step in enumerate(steps) if "vytvaruj" in step)

    assert split_index <= shape_index
    assert "dva rovnake bochniky" in " ".join(steps)


@pytest.mark.parametrize("recipe_id", CLASSIC_IDS, ids=CLASSIC_IDS)
def test_classics_do_not_hide_measured_aromatics_as_pantry_basics(
    recipe_id, ingredient_catalog, tmp_path
):
    recipe = _load_candidate_recipe(recipe_id, ingredient_catalog, tmp_path)

    assert set(recipe.pantry_basics) <= {"water", "salt", "black_pepper"}


@pytest.mark.parametrize("recipe_id", CLASSIC_IDS, ids=CLASSIC_IDS)
def test_single_egg_copy_stays_singular(
    recipe_id, ingredient_catalog, tmp_path
):
    recipe = _load_candidate_recipe(recipe_id, ingredient_catalog, tmp_path)
    if not any("egg" in slot.candidates for slot in recipe.slots):
        return
    meal = render_meal(
        _render_candidate(recipe, ingredient_catalog),
        adults=1,
        children=0,
        covered_days=1,
    )
    egg = next(item for item in meal.ingredients if item.ingredient.id == "egg")
    if egg.quantity.amount != 1:
        return

    instructions = _fold(" ".join(meal.instructions))
    assert re.search(r"\b(?:vajcia|zltky|bielky)\b", instructions) is None


@pytest.mark.parametrize("recipe_id", PRIVAROK_IDS, ids=PRIVAROK_IDS)
def test_privarok_boiled_egg_has_water_timing_and_correct_number(
    recipe_id, ingredient_catalog, tmp_path
):
    recipe = _load_candidate_recipe(recipe_id, ingredient_catalog, tmp_path)
    meal = render_meal(
        _render_candidate(recipe, ingredient_catalog),
        adults=1,
        children=0,
        covered_days=1,
    )
    egg_steps = " ".join(step for step in meal.instructions if "vaj" in _fold(step))
    folded = _fold(egg_steps)

    assert "vod" in folded
    assert "varu" in folded
    assert "9 minut" in folded
    assert "vajce" in egg_steps
    assert "Schlaď ho" not in egg_steps


def test_dry_red_bean_stew_starts_with_a_safe_brisk_boil(
    ingredient_catalog, tmp_path
):
    recipe = _load_candidate_recipe(
        "classic_bean_stew_egg", ingredient_catalog, tmp_path
    )
    text = _fold(" ".join(step.text for step in recipe.instructions))

    assert "cerstv" in text
    assert "prudk" in text
    assert re.search(r"\b(?:var|uvar)\w*\b[^.]*\b30 minut", text)


@pytest.mark.parametrize(
    ("recipe_id", "dairy_key"),
    (
        ("classic_potato_stew_egg", "cream"),
        ("classic_pumpkin_stew_egg", "yogurt"),
    ),
)
def test_prepared_slurry_is_added_once_without_readding_its_parts(
    recipe_id, dairy_key, ingredient_catalog, tmp_path
):
    recipe = _load_candidate_recipe(recipe_id, ingredient_catalog, tmp_path)
    steps = tuple(step.text for step in recipe.instructions)
    preparation_index = next(
        index for index, step in enumerate(steps) if "zálievku" in step
    )
    following = steps[preparation_index + 1]

    assert "zálievku" in following
    assert f"{{{dairy_key}.name}}" not in following
    assert "{flour.name}" not in following


@pytest.mark.parametrize("recipe_id", PRIVAROK_IDS, ids=PRIVAROK_IDS)
def test_privarok_water_is_precalculated_and_three_day_storage_is_supported(
    recipe_id, ingredient_catalog, tmp_path
):
    recipe = _load_candidate_recipe(recipe_id, ingredient_catalog, tmp_path)
    candidate = _render_candidate(recipe, ingredient_catalog)
    meal = render_meal(candidate, adults=4, children=0, covered_days=3)
    text = _fold(" ".join(meal.instructions))

    assert "na kazdu porciu" not in text
    assert re.search(r"\b\d+(?:[,.]\d+)?\s*(?:ml|l) vody\b", text)
    assert meal.storage == recipe.storage.instruction


def test_kolozvarska_uses_ground_meat_and_scales_its_added_water(
    ingredient_catalog, tmp_path
):
    recipe = _load_candidate_recipe(
        "classic_pork_cabbage_bake", ingredient_catalog, tmp_path
    )
    declared = _declared_ids(recipe)
    meal = render_meal(
        _render_candidate(recipe, ingredient_catalog),
        adults=4,
        children=0,
        covered_days=1,
    )
    text = _fold(" ".join(meal.instructions))

    assert "pork_mince" in declared
    assert "pork_shoulder" not in declared
    assert "na kazdu porciu" not in text
    assert "160 ml vody" in text
    assert "polej povrch smotanu" not in text
    assert "rovnomerne nou polej" in text


def test_pumpkin_stew_is_a_complete_main_meal(ingredient_catalog, tmp_path):
    recipe = _load_candidate_recipe(
        "classic_pumpkin_stew_egg", ingredient_catalog, tmp_path
    )
    meal = render_meal(
        _render_candidate(recipe, ingredient_catalog),
        adults=1,
        children=0,
        covered_days=1,
    )

    assert "potato" in _declared_ids(recipe)
    assert meal.nutrition.serving.kcal >= Decimal("400")


@pytest.mark.parametrize(
    ("recipe_id", "required_ingredient"),
    (
        ("classic_potato_soup", "bread"),
        ("classic_tomato_soup", "pasta"),
        ("classic_cauliflower_soup", "pasta"),
    ),
)
def test_main_meal_soups_are_filling_enough_for_an_adult(
    recipe_id, required_ingredient, ingredient_catalog, tmp_path
):
    recipe = _load_candidate_recipe(recipe_id, ingredient_catalog, tmp_path)
    meal = render_meal(
        _render_candidate(recipe, ingredient_catalog),
        adults=1,
        children=0,
        covered_days=1,
    )

    assert required_ingredient in _declared_ids(recipe)
    assert meal.nutrition.serving.kcal >= Decimal("400")
    assert meal.nutrition.serving.protein_g >= Decimal("12")


@pytest.mark.parametrize(
    "recipe_id",
    ("classic_pork_cabbage_bake", *PRIVAROK_IDS),
)
def test_reviewed_classics_declare_basic_cutting_equipment(
    recipe_id, ingredient_catalog, tmp_path
):
    recipe = _load_candidate_recipe(recipe_id, ingredient_catalog, tmp_path)

    assert "nôž" in recipe.equipment
    assert "doska" in recipe.equipment


@pytest.mark.parametrize("recipe_id", GARLIC_COOKING_IDS, ids=GARLIC_COOKING_IDS)
def test_garlic_is_added_while_the_meal_is_still_cooking(
    recipe_id, ingredient_catalog, tmp_path
):
    recipe = _load_candidate_recipe(recipe_id, ingredient_catalog, tmp_path)
    assert "garlic" in _declared_ids(recipe)
    cooked_garlic_steps = tuple(
        step.text
        for step in recipe.instructions
        if ("{garlic.name}" in step.text or "cesnak" in _fold(step.text))
        and re.search(r"\b(?:opekaj|var|dus|povar)\w*\b", _fold(step.text))
    )
    assert cooked_garlic_steps, f"{recipe_id}: garlic is only added after cooking"
    assert "dochut" not in _fold(" ".join(cooked_garlic_steps))


@pytest.mark.parametrize("recipe_id", RICE_IDS, ids=RICE_IDS)
def test_classic_rice_has_scaled_water_cover_cue_and_safe_storage(
    recipe_id, ingredient_catalog, tmp_path
):
    recipe = _load_candidate_recipe(recipe_id, ingredient_catalog, tmp_path)
    assert "rice" in _declared_ids(recipe)
    instructions = " ".join(step.text for step in recipe.instructions)
    folded = _fold(instructions)
    assert "{rice.water}" in instructions
    assert "prikry" in folded
    assert re.search(r"\b(?:vsiakn|zmak|sypk|pev)\w*\b", folded)
    assert recipe.storage.refrigerated_days == 1
    storage = _fold(recipe.storage.instruction)
    assert "plytk" in storage
    assert "jednej hodiny" in storage or "1 hodiny" in storage
    assert "24 hodin" in storage


@pytest.mark.parametrize("recipe_id", EXPLICIT_WATER_IDS, ids=EXPLICIT_WATER_IDS)
def test_soups_and_legumes_state_water_per_portion(
    recipe_id, ingredient_catalog, tmp_path
):
    recipe = _load_candidate_recipe(recipe_id, ingredient_catalog, tmp_path)
    source_instructions = " ".join(step.text for step in recipe.instructions)
    scaled_water_slots = tuple(
        slot for slot in recipe.slots if slot.water_ml_per_adult is not None
    )
    if scaled_water_slots:
        assert any(
            f"{{{slot.key}.water}}" in source_instructions
            for slot in scaled_water_slots
        ), f"{recipe_id}: scaled water must be rendered from its slot"
    else:
        instructions = _fold(source_instructions)
        assert re.search(r"\b\d{3,4}\s*ml (?:\w+\s+)?vody\b", instructions), (
            f"{recipe_id}: water amount affects the result and must be explicit"
        )
        assert "porci" in instructions


@pytest.mark.parametrize("recipe_id", CLASSIC_IDS, ids=CLASSIC_IDS)
def test_classic_recipes_scale_added_water_instead_of_leaving_per_portion_math(
    recipe_id, ingredient_catalog, tmp_path
):
    recipe = _load_candidate_recipe(recipe_id, ingredient_catalog, tmp_path)
    source_instructions = " ".join(step.text for step in recipe.instructions)
    folded = _fold(source_instructions)

    assert "na kazdu porciu" not in folded
    assert "na jednu porciu" not in folded
    assert re.search(r"\b\d+(?:[,.]\d+)?\s*ml vody\b", folded) is None


@pytest.mark.parametrize("recipe_id", LARGE_OVEN_BATCH_IDS, ids=LARGE_OVEN_BATCH_IDS)
def test_large_three_day_oven_meals_tell_the_cook_to_split_the_batch(
    recipe_id, ingredient_catalog, tmp_path
):
    recipe = _load_candidate_recipe(recipe_id, ingredient_catalog, tmp_path)
    instructions = _fold(" ".join(step.text for step in recipe.instructions))

    assert "vacsej davke" in instructions
    assert "dvoch pekac" in instructions


@pytest.mark.parametrize(
    "recipe_id", LARGE_BROWNING_BATCH_IDS, ids=LARGE_BROWNING_BATCH_IDS
)
def test_large_pan_and_browning_recipes_tell_the_cook_to_work_in_batches(
    recipe_id, ingredient_catalog, tmp_path
):
    recipe = _load_candidate_recipe(recipe_id, ingredient_catalog, tmp_path)
    instructions = _fold(" ".join(step.text for step in recipe.instructions))

    assert "po castiach" in instructions


@pytest.mark.parametrize(
    ("recipe_id", "vessel_term", "split_guidance"),
    (
        ("classic_meatloaf_potatoes", "pekac", "dva rovnake bochniky"),
        ("classic_roast_pork_root_veg", "pekac", "rozdel tuto velku davku"),
        ("classic_tomato_meatballs", "plech", "rozdel tuto velku davku"),
        ("classic_segedin_goulash", "hrnc", "rozdel tuto velku davku"),
        ("classic_bean_stew_egg", "hrnc", "rozdel tuto velku davku"),
    ),
)
def test_large_family_batch_is_split_between_realistic_vessels(
    recipe_id, vessel_term, split_guidance, ingredient_catalog, tmp_path
):
    recipe = _load_candidate_recipe(recipe_id, ingredient_catalog, tmp_path)
    meal = render_meal(
        _render_candidate(recipe, ingredient_catalog),
        adults=4,
        children=0,
        covered_days=3,
    )
    instructions = _fold(" ".join(meal.instructions))

    assert split_guidance in instructions
    assert vessel_term in instructions


def test_single_day_batch_does_not_get_large_batch_warning(
    ingredient_catalog, tmp_path
):
    recipe = _load_candidate_recipe(
        "classic_segedin_goulash", ingredient_catalog, tmp_path
    )
    meal = render_meal(
        _render_candidate(recipe, ingredient_catalog),
        adults=2,
        children=0,
        covered_days=1,
    )

    assert "rozdel tuto velku davku" not in _fold(" ".join(meal.instructions))


@pytest.mark.parametrize("recipe_id", CLASSIC_IDS, ids=CLASSIC_IDS)
def test_three_day_batches_are_portioned_and_refrigerated_promptly(
    recipe_id, ingredient_catalog, tmp_path
):
    recipe = _load_candidate_recipe(recipe_id, ingredient_catalog, tmp_path)
    if recipe.storage.refrigerated_days < 3:
        pytest.skip("recipe is not offered as a three-day batch")

    storage = _fold(recipe.storage.instruction)
    assert "plytk" in storage
    assert re.search(r"\b(?:1|2|jednej|dvoch) hodin", storage)


@pytest.mark.parametrize(
    ("recipe_id", "required"),
    REQUIRED_RECIPE_EQUIPMENT.items(),
    ids=REQUIRED_RECIPE_EQUIPMENT,
)
def test_classic_recipes_declare_the_tools_used_by_their_method(
    recipe_id, required, ingredient_catalog, tmp_path
):
    recipe = _load_candidate_recipe(recipe_id, ingredient_catalog, tmp_path)
    equipment = _fold(" ".join(recipe.equipment))

    for item in required:
        assert _fold(item) in equipment


def test_baked_pasta_uses_natural_egg_and_cream_grammar(
    ingredient_catalog, tmp_path
):
    recipe = _load_candidate_recipe(
        "classic_baked_pasta_ham", ingredient_catalog, tmp_path
    )
    meal = render_meal(
        _render_candidate(recipe, ingredient_catalog),
        adults=1,
        children=0,
        covered_days=1,
    )
    instructions = _fold(" ".join(meal.instructions))

    assert "rozslahaj vajce v mise" in instructions
    assert "prilej smotanu na slahanie" in instructions
    assert "vajce s smotanu" not in instructions


def test_bolognese_grates_cheese_and_serves_it_over_the_finished_pasta(
    ingredient_catalog, tmp_path
):
    recipe = _load_candidate_recipe(
        "classic_bolognese_spaghetti", ingredient_catalog, tmp_path
    )
    source = _fold(" ".join(step.text for step in recipe.instructions))

    assert "nastruhaj" in source
    assert "syrom" in source
    assert "cheese:grated" in {
        token for step in recipe.instructions for token in (*step.requires, *step.produces)
    }


def test_garlic_soup_keeps_later_croutons_separate(
    ingredient_catalog, tmp_path
):
    recipe = _load_candidate_recipe(
        "classic_garlic_soup", ingredient_catalog, tmp_path
    )
    source = _fold(" ".join(step.text for step in recipe.instructions))

    assert "kruton" in source
    assert "odloz bokom" in source


@pytest.mark.parametrize(
    ("recipe_id", "term"), NAMED_PASTA_TERMS.items(), ids=NAMED_PASTA_TERMS
)
def test_named_pasta_identity_is_preserved_in_the_method(
    recipe_id, term, ingredient_catalog, tmp_path
):
    recipe = _load_candidate_recipe(recipe_id, ingredient_catalog, tmp_path)
    instructions = _fold(" ".join(step.text for step in recipe.instructions))
    assert term in instructions


@pytest.mark.parametrize("recipe_id", CLASSIC_IDS, ids=CLASSIC_IDS)
def test_classic_method_avoids_mechanical_slovak(
    recipe_id, ingredient_catalog, tmp_path
):
    recipe = _load_candidate_recipe(recipe_id, ingredient_catalog, tmp_path)
    source_instructions = " ".join(step.text for step in recipe.instructions)
    instructions = _fold(source_instructions)
    assert "60 sekund" not in instructions
    assert MECHANICAL_LANGUAGE.search(instructions) is None
    assert ACCUSATIVE_AFTER_WITH.search(source_instructions) is None


@pytest.mark.parametrize("recipe_id", CLASSIC_IDS, ids=CLASSIC_IDS)
def test_every_classic_recipe_has_unambiguous_safe_storage(
    recipe_id, ingredient_catalog, tmp_path
):
    recipe = _load_candidate_recipe(recipe_id, ingredient_catalog, tmp_path)
    storage = _fold(recipe.storage.instruction)

    assert "plytk" in storage
    assert "chladnick" in storage
    assert re.search(r"\b(?:1|2|jednej|dvoch) hodin", storage)


def test_tomato_meatballs_declares_the_oven(ingredient_catalog, tmp_path):
    recipe = _load_candidate_recipe(
        "classic_tomato_meatballs", ingredient_catalog, tmp_path
    )
    assert "rúra" in recipe.equipment


@pytest.mark.parametrize(
    ("recipe_id", "max_grams", "max_kcal"),
    tuple(
        (recipe_id, *limits)
        for recipe_id, limits in MAIN_MEAL_LIMITS.items()
    ),
    ids=MAIN_MEAL_LIMITS,
)
def test_large_classic_main_meals_have_reasonable_adult_servings(
    recipe_id, max_grams, max_kcal, ingredient_catalog, tmp_path
):
    recipe = _load_candidate_recipe(recipe_id, ingredient_catalog, tmp_path)
    meal = render_meal(
        _render_candidate(recipe, ingredient_catalog),
        adults=1,
        children=0,
        covered_days=1,
    )
    edible_grams = sum(
        (_rendered_edible_grams(item) for item in meal.ingredients), Decimal("0")
    )
    assert edible_grams <= max_grams, (recipe_id, edible_grams, max_grams)
    assert meal.nutrition.serving.kcal <= max_kcal, (
        recipe_id,
        meal.nutrition.serving.kcal,
        max_kcal,
    )
