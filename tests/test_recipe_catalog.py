import json
from dataclasses import FrozenInstanceError
from decimal import Decimal
from pathlib import Path
import subprocess
import sys

import pytest

from app.ingredient_catalog import load_ingredient_catalog
from app.recipe_catalog import load_recipe_catalog


DEFAULT_RECIPE_ROOT = (
    Path(__file__).resolve().parents[1] / "app" / "catalog" / "recipes"
)
APP_DIR = Path(__file__).resolve().parents[1] / "app"


@pytest.fixture
def ingredients():
    return load_ingredient_catalog()


def _slot(**overrides):
    value = {
        "key": "protein",
        "role": "protein",
        "candidates": ["chicken_breast"],
        "amount_per_adult": "150",
        "unit": "g",
        "child_factor": "0.6",
        "required": True,
        "use": "main",
        "cut": "na kocky",
    }
    value.update(overrides)
    return value


def _recipe(**overrides):
    value = {
        "id": "chicken_rice_pan",
        "version": 1,
        "active": True,
        "name_template": "Panvica z {protein.name}",
        "family": "rice_pan",
        "method": "pan",
        "minutes": 30,
        "modes": ["standard", "high_protein"],
        "equipment": ["panvica"],
        "slots": [_slot()],
        "pantry_basics": ["oil", "salt", "black_pepper"],
        "instructions": [
            {"text": "Nakrájaj {protein.name} {protein.cut}."},
            {"text": "Rozohrej panvicu a opekaj {protein.amount} 8 minút."},
            {"text": "Rozdeľ jedlo na {portions} porcií."},
        ],
    }
    value.update(overrides)
    return value


def _write_library(tmp_path, recipes, manifest=None):
    root = tmp_path / "recipes"
    root.mkdir()
    if manifest is not False:
        (root / "manifest.json").write_text(
            json.dumps(
                {"library_version": 1} if manifest is None else manifest,
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
    (root / "fixtures.json").write_text(
        json.dumps({"recipes": recipes}, ensure_ascii=False),
        encoding="utf-8",
    )
    return root


def _write_raw_library(tmp_path, manifest_text, recipes_text):
    root = tmp_path / "recipes"
    root.mkdir()
    (root / "manifest.json").write_text(manifest_text, encoding="utf-8")
    (root / "fixtures.json").write_text(recipes_text, encoding="utf-8")
    return root


def test_loads_only_active_templates_by_default(ingredients, tmp_path):
    inactive = _recipe(id="inactive_recipe", active=False)
    root = _write_library(tmp_path, [_recipe(), inactive])

    catalog = load_recipe_catalog(ingredients, root)

    assert catalog.version == 1
    assert [recipe.id for recipe in catalog.all()] == ["chicken_rice_pan"]
    assert [
        recipe.id
        for recipe in load_recipe_catalog(
            ingredients, root, include_inactive=True
        ).all()
    ] == ["chicken_rice_pan", "inactive_recipe"]


def test_default_smoke_templates_stay_inactive(ingredients):
    assert load_recipe_catalog(ingredients, DEFAULT_RECIPE_ROOT).all() == ()
    assert [
        recipe.id
        for recipe in load_recipe_catalog(
            ingredients, DEFAULT_RECIPE_ROOT, include_inactive=True
        ).all()
    ] == ["chicken_rice_pan", "tofu_vegetable_pan", "lentil_tomato_pot"]


def test_recipe_catalog_imports_from_production_app_working_directory():
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            "import sys; sys.path.insert(0, '.'); import recipe_catalog",
        ],
        cwd=APP_DIR,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr


def test_loaded_template_values_are_deeply_immutable(ingredients, tmp_path):
    recipe = load_recipe_catalog(
        ingredients,
        _write_library(tmp_path, [_recipe()]),
    ).all()[0]

    assert recipe.modes == frozenset({"standard", "high_protein"})
    assert recipe.equipment == ("panvica",)
    assert recipe.slots[0].candidates == ("chicken_breast",)
    assert recipe.slots[0].amount_per_adult == Decimal("150")
    assert recipe.instructions[0].text.startswith("Nakrájaj")
    with pytest.raises(FrozenInstanceError):
        recipe.active = False


@pytest.mark.parametrize(
    "modes",
    [["vegan"], ["standard", "vegan"]],
)
def test_rejects_vegan_recipe_with_nonvegan_candidate(
    ingredients, tmp_path, modes
):
    root = _write_library(tmp_path, [_recipe(modes=modes)])

    with pytest.raises(ValueError, match="vegan"):
        load_recipe_catalog(ingredients, root)


def test_rejects_vegetarian_recipe_with_nonvegetarian_pantry_basic(
    ingredients, tmp_path
):
    root = _write_library(
        tmp_path,
        [
            _recipe(
                modes=["vegetarian"],
                slots=[_slot(candidates=["egg"])],
                pantry_basics=["chicken_breast"],
            )
        ],
    )

    with pytest.raises(ValueError, match="vegetarian"):
        load_recipe_catalog(ingredients, root)


def test_rejects_instruction_unknown_slot(ingredients, tmp_path):
    instructions = list(_recipe()["instructions"])
    instructions[1] = {"text": "Pridaj {mystery.name} a premiešaj."}
    root = _write_library(tmp_path, [_recipe(instructions=instructions)])

    with pytest.raises(ValueError, match="neznáma pozícia"):
        load_recipe_catalog(ingredients, root)


def test_rejects_missing_required_slot_placeholder(ingredients, tmp_path):
    root = _write_library(
        tmp_path,
        [
            _recipe(
                name_template="Rýchla panvica",
                instructions=[
                    {"text": "Rozohrej panvicu."},
                    {"text": "Opekaj 8 minút."},
                    {"text": "Rozdeľ na {portions} porcií."},
                ],
            )
        ],
    )

    with pytest.raises(ValueError, match="povinná pozícia.*protein"):
        load_recipe_catalog(ingredients, root)


@pytest.mark.parametrize(
    "text",
    ["Pridaj {protein.price}.", "Pridaj {protein}.", "Pridaj {portions.count}."],
)
def test_rejects_placeholder_outside_closed_vocabulary(
    ingredients, tmp_path, text
):
    instructions = list(_recipe()["instructions"])
    instructions[1] = {"text": text}
    root = _write_library(tmp_path, [_recipe(instructions=instructions)])

    with pytest.raises(ValueError, match="placeholder"):
        load_recipe_catalog(ingredients, root)


def test_rejects_duplicate_slot_keys(ingredients, tmp_path):
    root = _write_library(
        tmp_path,
        [_recipe(slots=[_slot(), _slot(candidates=["tofu"])])],
    )

    with pytest.raises(ValueError, match="duplicitná pozícia"):
        load_recipe_catalog(ingredients, root)


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"candidates": ["not_in_catalog"]}, "neznáma surovina"),
        ({"role": "dessert"}, "neznáma rola"),
        ({"role": "vegetable"}, "rola.*chicken_breast"),
    ],
)
def test_rejects_unknown_or_incompatible_ingredient_slot(
    ingredients, tmp_path, overrides, message
):
    root = _write_library(tmp_path, [_recipe(slots=[_slot(**overrides)])])

    with pytest.raises(ValueError, match=message):
        load_recipe_catalog(ingredients, root)


def test_rejects_fewer_than_three_instructions(ingredients, tmp_path):
    root = _write_library(
        tmp_path,
        [_recipe(instructions=_recipe()["instructions"][:2])],
    )

    with pytest.raises(ValueError, match="najmenej tri"):
        load_recipe_catalog(ingredients, root)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("amount_per_adult", "0"),
        ("amount_per_adult", "NaN"),
        ("child_factor", "0"),
    ],
)
def test_rejects_nonpositive_slot_quantities(
    ingredients, tmp_path, field, value
):
    root = _write_library(
        tmp_path,
        [_recipe(slots=[_slot(**{field: value})])],
    )

    with pytest.raises(ValueError, match="kladná"):
        load_recipe_catalog(ingredients, root)


def test_rejects_unknown_unit(ingredients, tmp_path):
    root = _write_library(
        tmp_path,
        [_recipe(slots=[_slot(unit="tablespoon")])],
    )

    with pytest.raises(ValueError, match="jednotka"):
        load_recipe_catalog(ingredients, root)


@pytest.mark.parametrize(
    ("slot", "message"),
    [
        (_slot(candidates=["rice"], role="starch", unit="piece"), "gramov na kus"),
        (_slot(candidates=["rice"], role="starch", unit="ml"), "hustota"),
    ],
)
def test_rejects_unit_without_nutrition_conversion(
    ingredients, tmp_path, slot, message
):
    root = _write_library(tmp_path, [_recipe(slots=[slot])])

    with pytest.raises(ValueError, match=message):
        load_recipe_catalog(ingredients, root)


def test_accepts_piece_and_ml_with_catalog_conversion(ingredients, tmp_path):
    slots = [
        _slot(candidates=["egg"], unit="piece", amount_per_adult="2"),
        _slot(
            key="liquid",
            role="dairy",
            candidates=["milk"],
            unit="ml",
            amount_per_adult="100",
            use="addition",
        ),
    ]
    root = _write_library(
        tmp_path,
        [
            _recipe(
                name_template="Vajcia {protein.name} s {liquid.name}",
                slots=slots,
                instructions=[
                    {"text": "Rozmiešaj {protein.amount} {protein.name}."},
                    {"text": "Pridaj {liquid.amount} {liquid.name}."},
                    {"text": "Uvar a rozdeľ na {portions} porcií."},
                ],
            )
        ],
    )

    assert len(load_recipe_catalog(ingredients, root).all()) == 1


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("modes", ["keto"], "režim"),
        ("method", "grill", "spôsob prípravy"),
        ("version", 0, "verzia receptu"),
        ("minutes", 0, "čas prípravy"),
    ],
)
def test_rejects_invalid_recipe_enums_and_positive_integers(
    ingredients, tmp_path, field, value, message
):
    root = _write_library(tmp_path, [_recipe(**{field: value})])

    with pytest.raises(ValueError, match=message):
        load_recipe_catalog(ingredients, root)


@pytest.mark.parametrize(
    ("manifest", "message"),
    [
        (False, "manifest"),
        ({}, "library_version"),
        ({"library_version": "1"}, "celé číslo"),
        ({"library_version": True}, "celé číslo"),
        ({"library_version": 0}, "kladná"),
        ({"library_version": 1, "extra": 2}, "manifest"),
    ],
)
def test_manifest_is_the_only_source_of_positive_integer_library_version(
    ingredients, tmp_path, manifest, message
):
    root = _write_library(tmp_path, [_recipe(version=99)], manifest=manifest)

    with pytest.raises(ValueError, match=message):
        load_recipe_catalog(ingredients, root)


def test_rejects_duplicate_json_key_in_manifest(ingredients, tmp_path):
    root = _write_raw_library(
        tmp_path,
        '{"library_version":1,"library_version":1}',
        json.dumps({"recipes": [_recipe()]}, ensure_ascii=False),
    )

    with pytest.raises(ValueError, match="duplicitný JSON kľúč: library_version"):
        load_recipe_catalog(ingredients, root)


@pytest.mark.parametrize(
    ("needle", "replacement", "duplicate_key"),
    [
        ('"unit": "g"', '"unit": "g", "unit": "g"', "unit"),
        ('"text": ', '"text": "duplicitný krok", "text": ', "text"),
    ],
)
def test_rejects_duplicate_json_key_recursively_in_recipe_data(
    ingredients, tmp_path, needle, replacement, duplicate_key
):
    recipes_text = json.dumps({"recipes": [_recipe()]}, ensure_ascii=False)
    assert needle in recipes_text
    recipes_text = recipes_text.replace(needle, replacement, 1)
    root = _write_raw_library(
        tmp_path,
        '{"library_version":1}',
        recipes_text,
    )

    with pytest.raises(ValueError, match=f"duplicitný JSON kľúč: {duplicate_key}"):
        load_recipe_catalog(ingredients, root)


def test_catalog_rejects_duplicate_recipe_ids(ingredients, tmp_path):
    root = _write_library(tmp_path, [_recipe(), _recipe()])

    with pytest.raises(ValueError, match="duplicitné ID receptu"):
        load_recipe_catalog(ingredients, root)
