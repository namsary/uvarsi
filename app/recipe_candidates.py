"""Offline validation and promotion of quarantined recipe drafts."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import date
import hashlib
import json
from json import JSONDecodeError
import os
from pathlib import Path
import shutil
import stat
import tempfile
import threading

from .ingredient_catalog import IngredientCatalog, load_ingredient_catalog
from .library_gate import audit_library
from .recipe_catalog import (
    _exact_keys,
    _json_object_without_duplicates,
    _load_strict_json,
    _object,
    _recipe_from_json,
    load_recipe_catalog,
)


CANDIDATE_ROOT = Path(__file__).with_name("catalog") / "candidates"
RECIPE_ROOT = Path(__file__).with_name("catalog") / "recipes"
_LOCAL_LOCKS: dict[str, threading.Lock] = {}
_LOCAL_LOCKS_GUARD = threading.Lock()


@dataclass(frozen=True)
class CandidateReport:
    path: Path
    recipe_ids: tuple[str, ...]
    errors: tuple[str, ...]

    @property
    def passed(self) -> bool:
        return not self.errors


class _CandidatePathError(ValueError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _candidate_path(path) -> tuple[Path, Path]:
    root = CANDIDATE_ROOT.resolve(strict=True)
    candidate = Path(os.path.abspath(Path(path)))
    if candidate.parent != root or candidate.suffix.casefold() != ".json":
        raise _CandidatePathError("unsafe_candidate_path")
    return root, candidate


def _read_candidate_secure(path) -> tuple[Path, bytes]:
    """Open one contained regular file and reject path/identity swaps."""
    try:
        _, candidate = _candidate_path(path)
    except FileNotFoundError as exc:
        raise _CandidatePathError("unsafe_candidate_path") from exc
    try:
        before = os.lstat(candidate)
    except FileNotFoundError as exc:
        raise _CandidatePathError("candidate_missing") from exc
    if stat.S_ISLNK(before.st_mode):
        raise _CandidatePathError("unsafe_candidate_path:symlink")
    if not stat.S_ISREG(before.st_mode):
        raise _CandidatePathError("unsafe_candidate_path:not_regular")

    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(candidate, flags)
    except FileNotFoundError as exc:
        raise _CandidatePathError("candidate_missing") from exc
    except OSError as exc:
        raise _CandidatePathError("unsafe_candidate_path:open") from exc
    try:
        opened = os.fstat(descriptor)
        identity = (before.st_dev, before.st_ino)
        if identity != (opened.st_dev, opened.st_ino) or not stat.S_ISREG(
            opened.st_mode
        ):
            raise _CandidatePathError("unsafe_candidate_path:identity_swap")
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = -1
            content = stream.read()
            after_read = os.fstat(stream.fileno())
        after_path = os.lstat(candidate)
        if (
            identity != (after_read.st_dev, after_read.st_ino)
            or identity != (after_path.st_dev, after_path.st_ino)
            or stat.S_ISLNK(after_path.st_mode)
            or before.st_size != after_read.st_size
            or before.st_mtime_ns != after_read.st_mtime_ns
        ):
            raise _CandidatePathError("unsafe_candidate_path:changed")
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    return candidate.resolve(strict=True), content


def _candidate_payload(content: bytes) -> dict:
    text = content.decode("utf-8")
    return _object(
        json.loads(text, object_pairs_hook=_json_object_without_duplicates),
        "kandidáta",
    )


def _validate_candidate_content(
    candidate: Path,
    content: bytes,
    ingredients: IngredientCatalog,
) -> tuple[CandidateReport, object | None, object | None]:
    try:
        payload = _candidate_payload(content)
    except (JSONDecodeError, UnicodeError, ValueError) as exc:
        return CandidateReport(candidate, (), (f"malformed_json:{exc}",)), None, None

    try:
        _exact_keys(payload, {"recipes"}, "kandidáta")
        values = payload["recipes"]
        if type(values) is not list or len(values) != 1:
            raise ValueError("kandidát musí obsahovať presne jeden recept")
        recipe = _recipe_from_json(values[0], ingredients)
    except (KeyError, TypeError, ValueError) as exc:
        return CandidateReport(candidate, (), (f"schema:{exc}",)), None, None

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
        return (
            CandidateReport(candidate, (recipe.id,), tuple(sorted(errors))),
            values[0],
            recipe,
        )

    active_ids = {item.id for item in active}
    audit_recipe = recipe
    if recipe.id in active_ids:
        errors.add(f"duplicate_id:{recipe.id}")
        suffix = hashlib.sha256(content).hexdigest()[:16]
        audit_id = f"candidate_audit_{suffix}"
        while audit_id in active_ids:
            audit_id += "_x"
        audit_recipe = replace(recipe, id=audit_id)
    errors.update(audit_library(ingredients, (*active, audit_recipe)).errors)
    return (
        CandidateReport(candidate, (recipe.id,), tuple(sorted(errors))),
        values[0],
        recipe,
    )


def validate_candidate(path, ingredients: IngredientCatalog) -> CandidateReport:
    """Validate one quarantined candidate and collect every reachable failure."""
    candidate = Path(os.path.abspath(Path(path)))
    try:
        candidate, content = _read_candidate_secure(path)
    except _CandidatePathError as exc:
        return CandidateReport(candidate, (), (exc.code,))
    return _validate_candidate_content(candidate, content, ingredients)[0]


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
    if parsed > date.today():
        raise ValueError("reviewed_on cannot be in the future")
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


def _json_bytes(payload) -> bytes:
    return (
        json.dumps(payload, ensure_ascii=False, indent=2, separators=(",", ": "))
        + "\n"
    ).encode("utf-8")


def _thread_lock(path: Path) -> threading.Lock:
    key = os.path.normcase(str(path.resolve()))
    with _LOCAL_LOCKS_GUARD:
        return _LOCAL_LOCKS.setdefault(key, threading.Lock())


@contextmanager
def _promotion_lock():
    """Serialize recipe promotion across threads and operating-system processes."""
    lock_path = RECIPE_ROOT.parent / ".recipe-promotion.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    local = _thread_lock(lock_path)
    with local:
        with lock_path.open("a+b") as stream:
            stream.seek(0, os.SEEK_END)
            if stream.tell() == 0:
                stream.write(b"\0")
                stream.flush()
            stream.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(stream.fileno(), msvcrt.LK_LOCK, 1)
                try:
                    yield
                finally:
                    stream.seek(0)
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _audit_root(ingredients: IngredientCatalog, root: Path) -> None:
    catalog = load_recipe_catalog(ingredients, root, include_inactive=True)
    audit = audit_library(ingredients, catalog)
    if audit.errors:
        raise ValueError("library gate failed: " + ", ".join(audit.errors))


def _manifest_state(path: Path) -> tuple[dict, int, int]:
    payload = _object(_load_strict_json(path), "manifestu")
    keys = set(payload)
    if keys not in (
        {"library_version"},
        {"library_version", "catalog_revision"},
    ):
        _exact_keys(payload, {"library_version", "catalog_revision"}, "manifestu")
    version = payload["library_version"]
    if type(version) is not int or version <= 0:
        raise ValueError("library_version must be a positive integer")
    revision = payload.get("catalog_revision", 0)
    if type(revision) is not int or revision < 0:
        raise ValueError("catalog_revision must be a non-negative integer")
    if revision % 2:
        raise ValueError("catalog manifest is mid-promotion")
    return payload, version, revision


def promote_candidate(path, reviewed_by, reviewed_on) -> Path:
    """Promote one reviewed draft without exposing a partial live catalog."""
    if not isinstance(reviewed_by, str) or not reviewed_by.strip():
        raise ValueError("reviewed_by must be non-empty")
    reviewer = reviewed_by.strip()
    review_day = _review_date(reviewed_on)
    ingredients = load_ingredient_catalog()
    with _promotion_lock():
        try:
            candidate_path, candidate_bytes = _read_candidate_secure(path)
        except _CandidatePathError as exc:
            raise ValueError(
                "candidate validation failed: " + exc.code
            ) from exc
        report, raw_recipe, recipe = _validate_candidate_content(
            candidate_path, candidate_bytes, ingredients
        )
        if not report.passed:
            raise ValueError(
                "candidate validation failed: " + ", ".join(report.errors)
            )
        if raw_recipe is None or recipe is None:
            raise ValueError("candidate validation failed")

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
        _, version, revision = _manifest_state(manifest)
        odd_manifest = {
            "library_version": version,
            "catalog_revision": revision + 1,
        }
        promoted_manifest = {
            "library_version": version + 1,
            "catalog_revision": revision + 2,
        }

        # Gate the exact target and final manifest before publishing either one.
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
        audit_payload = {
            "candidate_sha256": hashlib.sha256(candidate_bytes).hexdigest(),
            "promoted_to": target.name,
            "recipe_ids": list(report.recipe_ids),
            "reviewed_by": reviewer,
            "reviewed_on": review_day,
        }
        live_started = False
        final_published = False
        try:
            # Record review first; runtime remains unchanged if this write fails.
            _replace_json(audit_path, audit_payload)
            _replace_json(manifest, odd_manifest)
            live_started = True
            _replace_json(target, promoted_payload)
            final_published = True
            _replace_json(manifest, promoted_manifest)
            _audit_root(ingredients, RECIPE_ROOT)
        except Exception:
            if live_started:
                if final_published:
                    rollback_odd = {
                        "library_version": version + 1,
                        "catalog_revision": revision + 3,
                    }
                    _replace_bytes(manifest, _json_bytes(rollback_odd))
                _replace_bytes(target, target_before)
                _replace_bytes(manifest, manifest_before)
            if audit_path.exists():
                audit_path.unlink()
            raise
        return target.resolve()
