import json
from dataclasses import FrozenInstanceError, replace
from decimal import Decimal
from pathlib import Path
import re
import subprocess
import sys

import pytest

from app import recipe_catalog
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


def _v2_recipe_payload(**overrides):
    value = _recipe(
        version=2,
        instructions=[
            {
                "text": "Nakrájaj {protein.amount} {protein.name} {protein.cut}.",
                "requires": ["protein:raw"],
                "produces": ["protein:prepared"],
            },
            {
                "text": "Opekaj mäso v panvici 8 minút.",
                "requires": ["protein:prepared", "panvica:free"],
                "produces": ["protein:cooked", "panvica:occupied"],
            },
            {
                "text": "Rozdeľ jedlo na {portions} porcií.",
                "requires": ["protein:cooked"],
                "produces": ["protein:served", "panvica:free"],
            },
        ],
        storage={
            "refrigerated_days": 2,
            "instruction": (
                "Po vychladnutí odlož do chladničky a zjedz do 2 dní."
            ),
        },
    )
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


@pytest.mark.parametrize(
    ("filename", "recipe_id"),
    [
        ("02-oven.json", "oven_chicken_breast_zucchini_rice"),
        ("03-one-pot.json", "pot_chicken_rice_peas"),
        ("03-one-pot.json", "pot_chicken_paprika_pasta"),
        ("03-one-pot.json", "pot_pork_barley_vegetables"),
        ("03-one-pot.json", "pot_beef_tomato_pasta"),
        ("03-one-pot.json", "pot_turkey_lentil_tomato"),
    ],
)
def test_absorption_recipes_use_scaled_water_instead_of_fixed_batch_amount(
    filename, recipe_id
):
    payload = json.loads((DEFAULT_RECIPE_ROOT / filename).read_text(encoding="utf-8"))
    recipe = next(item for item in payload["recipes"] if item["id"] == recipe_id)
    instructions = " ".join(step["text"] for step in recipe["instructions"])

    assert "{starch.water}" in instructions
    assert "500 ml vody" not in instructions


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


def test_generation_one_activates_curated_v2_library_and_retires_legacy(
    ingredients,
):
    catalog = load_recipe_catalog(ingredients, DEFAULT_RECIPE_ROOT)
    active = catalog.all()
    all_recipes = load_recipe_catalog(
        ingredients, DEFAULT_RECIPE_ROOT, include_inactive=True
    ).all()
    launch_groups = {
        "pan": ("pan_",),
        "oven": ("oven_",),
        "one_pot": ("pot_",),
        "vegetarian": ("veg_",),
        "vegan": ("vegan_",),
        "soup_salad": ("soup_", "salad_"),
    }
    legacy = tuple(
        recipe
        for recipe in all_recipes
        if any(
            recipe.id.startswith(prefixes)
            for prefixes in launch_groups.values()
        )
    )

    assert catalog.curation_generation == 1
    assert len(active) == 104
    assert all(recipe.active and recipe.version == 2 for recipe in active)
    assert len(legacy) == 60
    assert {
        group: sum(recipe.id.startswith(prefixes) for recipe in legacy)
        for group, prefixes in launch_groups.items()
    } == {group: 10 for group in launch_groups}
    assert all(
        sum(recipe.id.startswith(prefixes) for prefixes in launch_groups.values()) == 1
        for recipe in legacy
    )
    assert all(not recipe.active and recipe.version == 1 for recipe in legacy)
    assert [
        recipe.id
        for recipe in all_recipes
        if not recipe.active
        and recipe.id in {
            "chicken_rice_pan",
            "tofu_vegetable_pan",
            "lentil_tomato_pot",
        }
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
    assert recipe.instructions[0].requires == ()
    assert recipe.instructions[0].produces == ()
    assert recipe.storage is None
    with pytest.raises(FrozenInstanceError):
        recipe.active = False


def test_v1_recipe_preserves_legacy_equipment_text_and_workflow_defaults(
    ingredients, tmp_path
):
    root = _write_library(
        tmp_path,
        [_recipe(equipment=["2 l hrniec"])],
    )

    recipe = load_recipe_catalog(ingredients, root).all()[0]

    assert recipe.equipment == ("2 l hrniec",)
    assert all(step.requires == () for step in recipe.instructions)
    assert all(step.produces == () for step in recipe.instructions)
    assert recipe.storage is None


def test_loads_version_2_workflow_and_recipe_specific_storage(ingredients, tmp_path):
    root = _write_library(tmp_path, [_v2_recipe_payload()])

    recipe = load_recipe_catalog(ingredients, root).all()[0]

    assert recipe.instructions[0].requires == ("protein:raw",)
    assert recipe.instructions[0].produces == ("protein:prepared",)
    assert recipe.storage.refrigerated_days == 2
    assert recipe.storage.instruction.endswith("do 2 dní.")


def test_slot_can_declare_scaled_recipe_specific_water(ingredients, tmp_path):
    payload = _v2_recipe_payload(
        slots=[_slot(water_ml_per_adult="250")],
    )
    root = _write_library(tmp_path, [payload])

    recipe = load_recipe_catalog(ingredients, root).all()[0]

    assert recipe.slots[0].water_ml_per_adult == Decimal("250")


def test_v2_recipe_requires_recipe_specific_storage_rule(ingredients, tmp_path):
    payload = _v2_recipe_payload()
    payload.pop("storage")
    root = _write_library(tmp_path, [payload])

    with pytest.raises(ValueError, match="storage"):
        load_recipe_catalog(ingredients, root)


@pytest.mark.parametrize(
    "storage",
    [
        {"refrigerated_days": 0, "instruction": "Platný text."},
        {"refrigerated_days": 5, "instruction": "Platný text."},
        {"refrigerated_days": 2, "instruction": ""},
        {"refrigerated_days": 2, "instruction": "Platný text.", "extra": True},
    ],
)
def test_v2_recipe_rejects_invalid_storage_rule(ingredients, tmp_path, storage):
    root = _write_library(tmp_path, [_v2_recipe_payload(storage=storage)])

    with pytest.raises(ValueError, match="storage|chladničke"):
        load_recipe_catalog(ingredients, root)


@pytest.mark.parametrize(
    "instructions",
    [
        [
            {"text": step["text"], "produces": step["produces"]}
            for step in _v2_recipe_payload()["instructions"]
        ],
        [
            {**step, "extra": []}
            for step in _v2_recipe_payload()["instructions"]
        ],
    ],
)
def test_v2_recipe_requires_exact_instruction_workflow_keys(
    ingredients, tmp_path, instructions
):
    root = _write_library(
        tmp_path,
        [_v2_recipe_payload(instructions=instructions)],
    )

    with pytest.raises(ValueError, match="schéma.*kroku"):
        load_recipe_catalog(ingredients, root)


@pytest.mark.parametrize("token", ["protein", "mystery:raw"])
def test_v2_recipe_rejects_malformed_or_unknown_workflow_tokens(
    ingredients, tmp_path, token
):
    payload = _v2_recipe_payload()
    payload["instructions"][0]["requires"] = [token]
    root = _write_library(tmp_path, [payload])

    with pytest.raises(ValueError, match="token|mystery"):
        load_recipe_catalog(ingredients, root)


def test_manifest_exposes_dormant_curated_generation(ingredients, tmp_path):
    root = _write_library(
        tmp_path,
        [_recipe()],
        manifest={
            "library_version": 7,
            "catalog_revision": 2,
            "curation_generation": 1,
        },
    )

    catalog = load_recipe_catalog(ingredients, root)

    assert catalog.curation_generation == 1


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


def test_accepts_water_as_a_culinary_pantry_basic(ingredients, tmp_path):
    root = _write_library(tmp_path, [_recipe(pantry_basics=["water", "salt"])])

    recipe = load_recipe_catalog(ingredients, root).all()[0]

    assert recipe.pantry_basics == ("water", "salt")


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
    ("instructions", "missing"),
    [
        (
            [
                {"text": "Rozohrej panvicu."},
                {"text": "Opekaj {protein.amount} 8 minút."},
                {"text": "Rozdeľ na {portions} porcií."},
            ],
            "názvu",
        ),
        (
            [
                {"text": "Nakrájaj {protein.name}."},
                {"text": "Opekaj 8 minút."},
                {"text": "Rozdeľ na {portions} porcií."},
            ],
            "množstva",
        ),
    ],
)
def test_required_slot_needs_name_and_amount_in_instructions(
    ingredients, tmp_path, instructions, missing
):
    root = _write_library(
        tmp_path,
        [
            _recipe(
                name_template="Panvica z {protein.name}",
                instructions=instructions,
            )
        ],
    )

    with pytest.raises(ValueError, match=rf"povinná pozícia.*{missing}.*protein"):
        load_recipe_catalog(ingredients, root)


def test_active_catalog_measures_every_slot_exactly_once(ingredients):
    recipes = tuple(
        recipe for recipe in load_recipe_catalog(ingredients).all() if recipe.active
    )

    assert len(recipes) == 104
    assert all(recipe.version == 2 for recipe in recipes)
    for recipe in recipes:
        for slot in recipe.slots:
            amount_placeholder = f"{{{slot.key}.amount}}"
            occurrences = sum(
                instruction.text.count(amount_placeholder)
                for instruction in recipe.instructions
            )
            assert occurrences == 1, (
                recipe.id,
                slot.key,
                occurrences,
            )


def test_unmeasured_instruction_names_do_not_follow_case_changing_prepositions(
    ingredients,
):
    recipes = load_recipe_catalog(ingredients).all()

    for recipe in recipes:
        for instruction in recipe.instructions:
            for slot in recipe.slots:
                for preposition in ("s", "so", "k", "ku", "z", "zo"):
                    unsafe = re.compile(
                        rf"(?<!\w){preposition} \{{{slot.key}\.name\}}"
                    )
                    assert unsafe.search(instruction.text) is None, (
                        recipe.id,
                        instruction.text,
                        unsafe.pattern,
                    )


def test_malformed_inactive_template_cannot_hide_missing_instruction_contract(
    ingredients, tmp_path
):
    root = _write_library(
        tmp_path,
        [
            _recipe(
                active=False,
                name_template="Panvica z {protein.name}",
                instructions=[
                    {"text": "Rozohrej panvicu."},
                    {"text": "Opekaj 8 minút."},
                    {"text": "Rozdeľ na {portions} porcií."},
                ],
            )
        ],
    )

    with pytest.raises(ValueError, match=r"povinná pozícia.*protein"):
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
        ("water_ml_per_adult", "0"),
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
        ({"library_version": 1, "catalog_revision": "2"}, "catalog_revision"),
        ({"library_version": 1, "catalog_revision": True}, "catalog_revision"),
        ({"library_version": 1, "catalog_revision": None}, "catalog_revision"),
        ({"library_version": 1, "catalog_revision": -1}, "catalog_revision"),
    ],
)
def test_manifest_is_the_only_source_of_positive_integer_library_version(
    ingredients, tmp_path, manifest, message
):
    root = _write_library(tmp_path, [_recipe(version=99)], manifest=manifest)

    with pytest.raises(ValueError, match=message):
        load_recipe_catalog(ingredients, root)


def test_loader_accepts_legacy_manifest_without_catalog_revision(
    ingredients, tmp_path
):
    root = _write_library(
        tmp_path,
        [_recipe()],
        manifest={"library_version": 7},
    )

    assert load_recipe_catalog(ingredients, root).version == 7


def test_production_manifest_uses_even_catalog_revision():
    manifest = json.loads(
        (DEFAULT_RECIPE_ROOT / "manifest.json").read_text(encoding="utf-8")
    )

    assert type(manifest["catalog_revision"]) is int
    assert manifest["catalog_revision"] >= 0
    assert manifest["catalog_revision"] % 2 == 0


def test_production_manifest_invalidates_plans_from_old_recipe_wording():
    manifest = json.loads(
        (DEFAULT_RECIPE_ROOT / "manifest.json").read_text(encoding="utf-8")
    )

    assert manifest["library_version"] >= 2
    assert manifest["catalog_revision"] >= 4


def test_loader_retries_when_manifest_changes_and_returns_only_stable_snapshot(
    ingredients, tmp_path, monkeypatch
):
    root = _write_library(
        tmp_path,
        [_recipe()],
        manifest={"library_version": 1, "catalog_revision": 2},
    )
    real_load = recipe_catalog._load_strict_json
    real_load_recipes = recipe_catalog._load_recipe_values
    recipe_loads = 0
    snapshots = iter(
        [
            {"library_version": 1, "catalog_revision": 2},
            {"library_version": 2, "catalog_revision": 4},
            {"library_version": 2, "catalog_revision": 4},
            {"library_version": 2, "catalog_revision": 4},
        ]
    )

    def changing_manifest(path):
        if Path(path).name == "manifest.json":
            return next(snapshots)
        return real_load(path)

    def changing_recipes(ingredient_catalog, source_root):
        nonlocal recipe_loads
        recipe_loads += 1
        values = real_load_recipes(ingredient_catalog, source_root)
        if recipe_loads == 1:
            return values
        return [replace(values[0], id="new_snapshot_recipe")]

    monkeypatch.setattr(recipe_catalog, "_load_strict_json", changing_manifest)
    monkeypatch.setattr(recipe_catalog, "_load_recipe_values", changing_recipes)

    catalog = load_recipe_catalog(ingredients, root)

    assert catalog.version == 2
    assert [item.id for item in catalog.all()] == ["new_snapshot_recipe"]


def test_loader_retries_odd_revision_then_reads_even_snapshot(
    ingredients, tmp_path, monkeypatch
):
    root = _write_library(
        tmp_path,
        [_recipe()],
        manifest={"library_version": 1, "catalog_revision": 2},
    )
    real_load = recipe_catalog._load_strict_json
    snapshots = iter(
        [
            {"library_version": 1, "catalog_revision": 3},
            {"library_version": 2, "catalog_revision": 4},
            {"library_version": 2, "catalog_revision": 4},
        ]
    )

    def odd_then_even(path):
        if Path(path).name == "manifest.json":
            return next(snapshots)
        return real_load(path)

    monkeypatch.setattr(recipe_catalog, "_load_strict_json", odd_then_even)

    assert load_recipe_catalog(ingredients, root).version == 2


def test_loader_fails_closed_after_bounded_odd_revision_retries(
    ingredients, tmp_path
):
    root = _write_library(
        tmp_path,
        [_recipe()],
        manifest={"library_version": 1, "catalog_revision": 3},
    )

    with pytest.raises(ValueError, match="stabilný snapshot"):
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
