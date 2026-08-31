from datetime import date
import hashlib
import json
from pathlib import Path
import shutil

import pytest

from app.ingredient_catalog import load_ingredient_catalog
from app import recipe_candidates
from app.recipe_catalog import load_recipe_catalog


PROJECT_ROOT = Path(__file__).resolve().parents[1]
ACTIVE_RECIPES = PROJECT_ROOT / "app" / "catalog" / "recipes"


def _candidate_recipe(**overrides):
    recipe = {
        "id": "candidate_chicken_rice_zucchini",
        "version": 1,
        "active": True,
        "name_template": "Kuracia ryžová panvica s {vegetable.name}",
        "family": "candidate_zucchini_rice_pan",
        "method": "pan",
        "minutes": 35,
        "modes": ["standard"],
        "equipment": ["panvica", "hrniec"],
        "slots": [
            {
                "key": "protein",
                "role": "protein",
                "candidates": ["chicken_breast"],
                "amount_per_adult": "180",
                "unit": "g",
                "child_factor": "0.6",
                "required": True,
                "use": "main",
                "cut": "na kocky",
            },
            {
                "key": "starch",
                "role": "starch",
                "candidates": ["rice"],
                "amount_per_adult": "75",
                "unit": "g",
                "child_factor": "0.55",
                "required": True,
                "use": "main",
                "cut": None,
            },
            {
                "key": "vegetable",
                "role": "vegetable",
                "candidates": ["zucchini"],
                "amount_per_adult": "150",
                "unit": "g",
                "child_factor": "0.65",
                "required": True,
                "use": "main",
                "cut": "na malé kocky",
            },
        ],
        "pantry_basics": ["oil", "salt", "black_pepper", "garlic"],
        "instructions": [
            {"text": "Prepláchni {starch.amount} {starch.name} studenou vodou."},
            {"text": "Uvar {starch.amount} {starch.name} v hrnci na miernom ohni 15 minút, kým voda vsiakne."},
            {"text": "Nakrájaj {protein.amount} {protein.name} {protein.cut} a {vegetable.amount} {vegetable.name} {vegetable.cut}."},
            {"text": "Opekaj {protein.amount} {protein.name} v panvici na strednom ohni 8 minút, kým mäso dosiahne 74 °C."},
            {"text": "Pridaj {vegetable.amount} {vegetable.name} do panvice a opekaj na strednom ohni 6 minút, kým zelenina zmäkne."},
            {"text": "Premiešaj jedlo s uvarenou ryžou."},
            {"text": "Dochuť jedlo cesnakom, soľou a čiernym korením a rozdeľ na {portions} porcií."},
        ],
    }
    recipe.update(overrides)
    return recipe


def _write_candidate(path: Path, recipe=None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"recipes": [recipe or _candidate_recipe()]}, ensure_ascii=False),
        encoding="utf-8",
    )
    return path


def _candidate_from_active_file(filename: str):
    payload = json.loads((ACTIVE_RECIPES / filename).read_text(encoding="utf-8"))
    recipe = payload["recipes"][0]
    recipe["id"] = f"candidate_{filename.removesuffix('.json').replace('-', '_')}"
    recipe["family"] = f"candidate_family_{filename.removesuffix('.json')}"
    return recipe


@pytest.fixture
def quarantined_catalog(tmp_path, monkeypatch):
    catalog = tmp_path / "catalog"
    recipes = catalog / "recipes"
    candidates = catalog / "candidates"
    shutil.copytree(ACTIVE_RECIPES, recipes)
    candidates.mkdir()
    monkeypatch.setattr(recipe_candidates, "RECIPE_ROOT", recipes)
    monkeypatch.setattr(recipe_candidates, "CANDIDATE_ROOT", candidates)
    return candidates, recipes


def test_validate_candidate_returns_passing_report_for_valid_quarantined_recipe(
    quarantined_catalog,
):
    candidates, _ = quarantined_catalog
    path = _write_candidate(candidates / "draft.json")

    report = recipe_candidates.validate_candidate(path, load_ingredient_catalog())

    assert isinstance(report, recipe_candidates.CandidateReport)
    assert report.path == path.resolve()
    assert report.recipe_ids == ("candidate_chicken_rice_zucchini",)
    assert report.errors == ()
    assert report.passed is True


def test_candidate_file_is_not_visible_to_runtime_catalog(quarantined_catalog):
    candidates, recipes = quarantined_catalog
    _write_candidate(candidates / "draft.json")

    runtime_ids = {
        recipe.id
        for recipe in load_recipe_catalog(
            load_ingredient_catalog(), recipes, include_inactive=True
        ).all()
    }

    assert "candidate_chicken_rice_zucchini" not in runtime_ids


def test_validate_candidate_rejects_path_outside_quarantine(
    quarantined_catalog, tmp_path
):
    _write_candidate(tmp_path / "escaped.json")

    report = recipe_candidates.validate_candidate(
        tmp_path / "escaped.json", load_ingredient_catalog()
    )

    assert report.recipe_ids == ()
    assert "unsafe_candidate_path" in report.errors
    assert report.passed is False


def test_validate_candidate_reports_malformed_json_without_publishing(
    quarantined_catalog,
):
    candidates, recipes = quarantined_catalog
    path = candidates / "broken.json"
    path.write_text('{"recipes":[', encoding="utf-8")
    before = (recipes / "manifest.json").read_bytes()

    report = recipe_candidates.validate_candidate(path, load_ingredient_catalog())

    assert report.recipe_ids == ()
    assert any(error.startswith("malformed_json:") for error in report.errors)
    assert (recipes / "manifest.json").read_bytes() == before


def test_validate_candidate_reports_schema_failure_instead_of_raising(
    quarantined_catalog,
):
    candidates, _ = quarantined_catalog
    recipe = _candidate_recipe()
    recipe["slots"][0]["candidates"] = ["ingredient_not_in_catalog"]
    path = _write_candidate(candidates / "unknown-ingredient.json", recipe)

    report = recipe_candidates.validate_candidate(path, load_ingredient_catalog())

    assert report.recipe_ids == ()
    assert any(
        error.startswith("schema:") and "neznáma surovina" in error
        for error in report.errors
    )


def test_validate_candidate_returns_all_independent_gate_failures(
    quarantined_catalog,
):
    candidates, _ = quarantined_catalog
    recipe = _candidate_recipe()
    recipe["instructions"][-1] = {
        "text": "Sceďok odváž na 1,5 g a jedlo nechaj na stole."
    }
    path = _write_candidate(candidates / "several-errors.json", recipe)

    report = recipe_candidates.validate_candidate(path, load_ingredient_catalog())

    assert {"forbidden_language", "decimal_grams", "missing_serving_action"} <= set(
        report.errors
    )


def test_validate_candidate_rejects_id_already_present_in_active_library(
    quarantined_catalog,
):
    candidates, _ = quarantined_catalog
    path = _write_candidate(
        candidates / "duplicate-id.json",
        _candidate_recipe(id="pan_chicken_rice_vegetables"),
    )

    report = recipe_candidates.validate_candidate(path, load_ingredient_catalog())

    assert "duplicate_id:pan_chicken_rice_vegetables" in report.errors
    assert report.passed is False


@pytest.mark.parametrize("reviewed_by", ["", "   ", None])
def test_promotion_requires_nonempty_human_reviewer(
    quarantined_catalog, reviewed_by
):
    candidates, recipes = quarantined_catalog
    path = _write_candidate(candidates / "draft.json")
    before = {item.name: item.read_bytes() for item in recipes.glob("*.json")}

    with pytest.raises(ValueError, match="reviewed_by"):
        recipe_candidates.promote_candidate(
            path, reviewed_by=reviewed_by, reviewed_on=date(2026, 8, 31)
        )

    assert {item.name: item.read_bytes() for item in recipes.glob("*.json")} == before


@pytest.mark.parametrize("reviewed_on", ["", "31-08-2026", None, object()])
def test_promotion_requires_valid_review_date(quarantined_catalog, reviewed_on):
    candidates, recipes = quarantined_catalog
    path = _write_candidate(candidates / "draft.json")
    before = {item.name: item.read_bytes() for item in recipes.glob("*.json")}

    with pytest.raises(ValueError, match="reviewed_on"):
        recipe_candidates.promote_candidate(
            path, reviewed_by="Mária Kontrolórka", reviewed_on=reviewed_on
        )

    assert {item.name: item.read_bytes() for item in recipes.glob("*.json")} == before


@pytest.mark.parametrize(
    "source_file",
    [
        "01-pan.json",
        "02-oven.json",
        "03-one-pot.json",
        "04-vegetarian.json",
        "05-vegan.json",
        "06-soup-salad.json",
    ],
)
def test_promotion_adds_passing_recipe_to_its_active_collection_atomically(
    quarantined_catalog, source_file
):
    candidates, recipes = quarantined_catalog
    path = _write_candidate(
        candidates / f"draft-{source_file}", _candidate_from_active_file(source_file)
    )
    original = path.read_bytes()
    target = recipes / source_file
    target_before = json.loads(target.read_text(encoding="utf-8"))
    version_before = json.loads(
        (recipes / "manifest.json").read_text(encoding="utf-8")
    )["library_version"]

    promoted_to = recipe_candidates.promote_candidate(
        path,
        reviewed_by="Mária Kontrolórka",
        reviewed_on=date(2026, 8, 31),
    )

    target_after = json.loads(target.read_text(encoding="utf-8"))
    manifest_after = json.loads(
        (recipes / "manifest.json").read_text(encoding="utf-8")
    )
    audit_path = path.with_suffix(".review.json")
    audit = json.loads(audit_path.read_text(encoding="utf-8"))
    assert promoted_to == target.resolve()
    assert len(target_after["recipes"]) == len(target_before["recipes"]) + 1
    assert target_after["recipes"][-1]["id"].startswith("candidate_")
    assert manifest_after == {"library_version": version_before + 1}
    assert path.read_bytes() == original
    assert audit == {
        "candidate_sha256": hashlib.sha256(original).hexdigest(),
        "promoted_to": source_file,
        "recipe_ids": [target_after["recipes"][-1]["id"]],
        "reviewed_by": "Mária Kontrolórka",
        "reviewed_on": "2026-08-31",
    }


def test_promotion_rejects_failing_candidate_without_changing_catalog(
    quarantined_catalog,
):
    candidates, recipes = quarantined_catalog
    path = _write_candidate(
        candidates / "duplicate.json",
        _candidate_recipe(id="pan_chicken_rice_vegetables"),
    )
    before = {item.name: item.read_bytes() for item in recipes.glob("*.json")}

    with pytest.raises(ValueError, match="candidate validation failed"):
        recipe_candidates.promote_candidate(
            path,
            reviewed_by="Mária Kontrolórka",
            reviewed_on=date(2026, 8, 31),
        )

    assert {item.name: item.read_bytes() for item in recipes.glob("*.json")} == before
    assert not path.with_suffix(".review.json").exists()


@pytest.mark.parametrize("failure_call", [4, 5])
def test_promotion_rolls_back_target_manifest_and_audit_after_live_write_failure(
    quarantined_catalog, monkeypatch, failure_call
):
    candidates, recipes = quarantined_catalog
    path = _write_candidate(candidates / "rollback.json")
    before = {item.name: item.read_bytes() for item in recipes.glob("*.json")}
    real_replace = recipe_candidates._replace_json
    calls = 0

    def fail_once(target, payload):
        nonlocal calls
        calls += 1
        if calls == failure_call:
            raise OSError("simulated live write failure")
        return real_replace(target, payload)

    monkeypatch.setattr(recipe_candidates, "_replace_json", fail_once)

    with pytest.raises(OSError, match="simulated live write failure"):
        recipe_candidates.promote_candidate(
            path,
            reviewed_by="Mária Kontrolórka",
            reviewed_on=date(2026, 8, 31),
        )

    assert {item.name: item.read_bytes() for item in recipes.glob("*.json")} == before
    assert not path.with_suffix(".review.json").exists()


def test_promotion_rolls_back_if_final_live_library_gate_fails(
    quarantined_catalog, monkeypatch
):
    candidates, recipes = quarantined_catalog
    path = _write_candidate(candidates / "gate-rollback.json")
    before = {item.name: item.read_bytes() for item in recipes.glob("*.json")}
    real_audit_root = recipe_candidates._audit_root
    calls = 0

    def fail_live_gate(ingredients, root):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise ValueError("simulated final library gate failure")
        return real_audit_root(ingredients, root)

    monkeypatch.setattr(recipe_candidates, "_audit_root", fail_live_gate)

    with pytest.raises(ValueError, match="simulated final library gate failure"):
        recipe_candidates.promote_candidate(
            path,
            reviewed_by="Mária Kontrolórka",
            reviewed_on=date(2026, 8, 31),
        )

    assert {item.name: item.read_bytes() for item in recipes.glob("*.json")} == before
    assert not path.with_suffix(".review.json").exists()


def test_promotion_rejects_path_traversal_before_any_write(
    quarantined_catalog, tmp_path
):
    _, recipes = quarantined_catalog
    escaped = _write_candidate(tmp_path / "escaped-promotion.json")
    before = {item.name: item.read_bytes() for item in recipes.glob("*.json")}

    with pytest.raises(ValueError, match="candidate validation failed"):
        recipe_candidates.promote_candidate(
            escaped,
            reviewed_by="Mária Kontrolórka",
            reviewed_on=date(2026, 8, 31),
        )

    assert {item.name: item.read_bytes() for item in recipes.glob("*.json")} == before
