"""Offline validation and promotion of quarantined recipe drafts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import hashlib
import json
from json import JSONDecodeError
import os
from pathlib import Path
import shutil
import tempfile

from .ingredient_catalog import IngredientCatalog, load_ingredient_catalog
from .library_gate import audit_library
from .recipe_catalog import (
    _exact_keys,
    _load_strict_json,
    _object,
    _recipe_from_json,
    load_recipe_catalog,
)


CANDIDATE_ROOT = Path(__file__).with_name("catalog") / "candidates"
RECIPE_ROOT = Path(__file__).with_name("catalog") / "recipes"


@dataclass(frozen=True)
class CandidateReport:
    path: Path
    recipe_ids: tuple[str, ...]
    errors: tuple[str, ...]

    @property
    def passed(self) -> bool:
        return not self.errors


def validate_candidate(path, ingredients: IngredientCatalog) -> CandidateReport:
    """Validate one quarantined candidate and collect every reachable failure."""
    candidate = Path(path)
    resolved = candidate.resolve()
    root = CANDIDATE_ROOT.resolve()
    if (
        resolved.parent != root
        or resolved.suffix.casefold() != ".json"
        or not resolved.is_file()
    ):
        error = (
            "candidate_missing"
            if resolved.parent == root and not resolved.is_file()
            else "unsafe_candidate_path"
        )
        return CandidateReport(resolved, (), (error,))

    try:
        payload = _object(_load_strict_json(resolved), "kandidáta")
    except (JSONDecodeError, OSError, UnicodeError, ValueError) as exc:
        return CandidateReport(resolved, (), (f"malformed_json:{exc}",))

    try:
        _exact_keys(payload, {"recipes"}, "kandidáta")
        values = payload["recipes"]
        if type(values) is not list or len(values) != 1:
            raise ValueError("kandidát musí obsahovať presne jeden recept")
        recipe = _recipe_from_json(values[0], ingredients)
    except (KeyError, TypeError, ValueError) as exc:
        return CandidateReport(resolved, (), (f"schema:{exc}",))

    errors: set[str] = set()
    if not recipe.active:
        errors.add("candidate_must_be_active")
    try:
        active = load_recipe_catalog(
            ingredients,
            RECIPE_ROOT,
            include_inactive=True,
        ).all()
    except (OSError, TypeError, ValueError) as exc:
        errors.add(f"active_catalog:{exc}")
        return CandidateReport(resolved, (recipe.id,), tuple(sorted(errors)))

    if recipe.id in {item.id for item in active}:
        errors.add(f"duplicate_id:{recipe.id}")
    else:
        errors.update(audit_library(ingredients, (*active, recipe)).errors)
    return CandidateReport(resolved, (recipe.id,), tuple(sorted(errors)))


def _review_date(value) -> str:
    if isinstance(value, str):
        try:
            parsed = date.fromisoformat(value)
        except ValueError as exc:
            raise ValueError("reviewed_on must be a valid ISO date") from exc
    elif type(value) is date:
        parsed = value
    else:
        raise ValueError("reviewed_on must be a valid date")
    return parsed.isoformat()


def _destination_name(recipe) -> str:
    if recipe.method in {"soup", "salad"}:
        return "06-soup-salad.json"
    if "vegan" in recipe.modes:
        return "05-vegan.json"
    if "vegetarian" in recipe.modes:
        return "04-vegetarian.json"
    if recipe.method == "oven":
        return "02-oven.json"
    if recipe.method in {"pot", "one_pot"}:
        return "03-one-pot.json"
    if recipe.method == "pan":
        return "01-pan.json"
    raise ValueError(f"candidate has no active collection: {recipe.id}")


def _replace_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _replace_json(path: Path, payload) -> None:
    content = (
        json.dumps(payload, ensure_ascii=False, indent=2, separators=(",", ": "))
        + "\n"
    ).encode("utf-8")
    _replace_bytes(path, content)


def _audit_root(ingredients: IngredientCatalog, root: Path) -> None:
    catalog = load_recipe_catalog(ingredients, root, include_inactive=True)
    audit = audit_library(ingredients, catalog)
    if audit.errors:
        raise ValueError("library gate failed: " + ", ".join(audit.errors))


def _candidate_value(path: Path, ingredients: IngredientCatalog):
    payload = _object(_load_strict_json(path), "kandidáta")
    _exact_keys(payload, {"recipes"}, "kandidáta")
    values = payload["recipes"]
    if type(values) is not list or len(values) != 1:
        raise ValueError("kandidát musí obsahovať presne jeden recept")
    return values[0], _recipe_from_json(values[0], ingredients)


def promote_candidate(path, reviewed_by, reviewed_on) -> Path:
    """Promote one reviewed draft without exposing a partial live catalog."""
    if not isinstance(reviewed_by, str) or not reviewed_by.strip():
        raise ValueError("reviewed_by must be non-empty")
    reviewer = reviewed_by.strip()
    review_day = _review_date(reviewed_on)
    ingredients = load_ingredient_catalog()
    report = validate_candidate(path, ingredients)
    if not report.passed:
        raise ValueError(
            "candidate validation failed: " + ", ".join(report.errors)
        )

    candidate_path = report.path
    raw_recipe, recipe = _candidate_value(candidate_path, ingredients)
    target = RECIPE_ROOT / _destination_name(recipe)
    manifest = RECIPE_ROOT / "manifest.json"
    audit_path = candidate_path.with_suffix(".review.json")
    if audit_path.exists():
        raise ValueError("candidate already has a review audit record")

    target_payload = _object(_load_strict_json(target), target.name)
    _exact_keys(target_payload, {"recipes"}, target.name)
    if type(target_payload["recipes"]) is not list:
        raise ValueError(f"recipes in {target.name} must be a list")
    promoted_payload = {
        "recipes": [*target_payload["recipes"], raw_recipe],
    }
    manifest_payload = _object(_load_strict_json(manifest), "manifestu")
    _exact_keys(manifest_payload, {"library_version"}, "manifestu")
    version = manifest_payload["library_version"]
    if type(version) is not int or version <= 0:
        raise ValueError("library_version must be a positive integer")
    promoted_manifest = {"library_version": version + 1}

    # Prove the exact candidate files pass before touching the live catalog.
    with tempfile.TemporaryDirectory(
        prefix="recipe-promotion-", dir=RECIPE_ROOT.parent
    ) as temporary_directory:
        staged = Path(temporary_directory) / "recipes"
        shutil.copytree(RECIPE_ROOT, staged)
        _replace_json(staged / target.name, promoted_payload)
        _replace_json(staged / manifest.name, promoted_manifest)
        _audit_root(ingredients, staged)

    target_before = target.read_bytes()
    manifest_before = manifest.read_bytes()
    candidate_before = candidate_path.read_bytes()
    audit_written = False
    try:
        _replace_json(target, promoted_payload)
        _replace_json(manifest, promoted_manifest)
        _audit_root(ingredients, RECIPE_ROOT)
        _replace_json(
            audit_path,
            {
                "candidate_sha256": hashlib.sha256(candidate_before).hexdigest(),
                "promoted_to": target.name,
                "recipe_ids": list(report.recipe_ids),
                "reviewed_by": reviewer,
                "reviewed_on": review_day,
            },
        )
        audit_written = True
    except Exception:
        _replace_bytes(target, target_before)
        _replace_bytes(manifest, manifest_before)
        if audit_written or audit_path.exists():
            audit_path.unlink()
        raise
    return target.resolve()
