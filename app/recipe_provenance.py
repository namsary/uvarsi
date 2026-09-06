"""Strict source-provenance records for curated recipes."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import json
from pathlib import Path
from types import MappingProxyType
from typing import Iterable, Mapping
from urllib.parse import urlsplit


DEFAULT_RECIPE_PROVENANCE_PATH = (
    Path(__file__).with_name("catalog") / "recipe_sources.json"
)
ALLOWED_EDITORIAL_LANES = frozenset(
    {"slovak_classic", "modern_family", "high_protein", "plant_based"}
)


@dataclass(frozen=True)
class SourceReference:
    url: str
    title: str
    accessed_on: date


@dataclass(frozen=True)
class RecipeProvenance:
    recipe_id: str
    editorial_lane: str
    core: bool
    references: tuple[SourceReference, ...]


def _json_object_without_duplicates(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _object(value, label: str) -> dict:
    if type(value) is not dict:
        raise ValueError(f"{label} must be an object")
    return value


def _exact_keys(value: dict, expected: set[str], label: str) -> None:
    actual = set(value)
    if actual == expected:
        return
    missing = sorted(expected - actual)
    extra = sorted(actual - expected)
    details = []
    if missing:
        details.append("missing " + ", ".join(missing))
    if extra:
        details.append("extra " + ", ".join(extra))
    raise ValueError(f"invalid {label} schema: {'; '.join(details)}")


def _text(value, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be non-empty text")
    return value


def _source_reference(value, label: str) -> tuple[SourceReference, str]:
    raw = _object(value, label)
    _exact_keys(raw, {"url", "title", "accessed_on"}, label)

    url = _text(raw["url"], f"{label} URL")
    try:
        parsed_url = urlsplit(url)
        host = parsed_url.hostname
    except ValueError as exc:
        raise ValueError(f"{label} URL has an invalid host") from exc
    if parsed_url.scheme != "https":
        raise ValueError(f"{label} URL must use HTTPS")
    if not host:
        raise ValueError(f"{label} URL must include a host")

    title = _text(raw["title"], f"{label} title")
    accessed_on = raw["accessed_on"]
    if type(accessed_on) is not str:
        raise ValueError(f"{label} accessed_on must be an ISO date")
    try:
        accessed_date = date.fromisoformat(accessed_on)
    except ValueError as exc:
        raise ValueError(f"{label} accessed_on must be an ISO date") from exc

    return SourceReference(url, title, accessed_date), host.casefold()


def _recipe_provenance(value, label: str) -> RecipeProvenance:
    raw = _object(value, label)
    _exact_keys(
        raw,
        {"recipe_id", "editorial_lane", "core", "references"},
        label,
    )

    recipe_id = _text(raw["recipe_id"], f"{label} recipe_id")
    editorial_lane = _text(
        raw["editorial_lane"], f"{label} editorial lane"
    )
    if editorial_lane not in ALLOWED_EDITORIAL_LANES:
        raise ValueError(f"{label} has an invalid editorial lane")
    core = raw["core"]
    if type(core) is not bool:
        raise ValueError(f"{label} core must be a boolean")

    raw_references = raw["references"]
    if type(raw_references) is not list or not raw_references:
        raise ValueError(f"{label} must have at least one source reference")
    parsed_references = tuple(
        _source_reference(reference, f"{label} reference {index}")
        for index, reference in enumerate(raw_references, start=1)
    )
    references = tuple(reference for reference, _ in parsed_references)
    urls = [reference.url for reference in references]
    if len(urls) != len(set(urls)):
        raise ValueError(f"{label} contains a duplicate source URL")
    hosts = {host for _, host in parsed_references}
    if core and len(hosts) < 2:
        raise ValueError(f"{label} needs two independent source hosts")

    return RecipeProvenance(recipe_id, editorial_lane, core, references)


def load_recipe_provenance(
    active_ids: Iterable[str],
    path=None,
) -> Mapping[str, RecipeProvenance]:
    """Load exact JSON and reject missing/extra IDs and weak sourcing."""
    source_path = (
        DEFAULT_RECIPE_PROVENANCE_PATH if path is None else Path(path)
    )
    with source_path.open(encoding="utf-8") as stream:
        payload = json.load(
            stream,
            object_pairs_hook=_json_object_without_duplicates,
        )
    root = _object(payload, "provenance file")
    _exact_keys(root, {"schema_version", "recipes"}, "provenance file")
    if type(root["schema_version"]) is not int or root["schema_version"] != 1:
        raise ValueError("schema_version must be 1")
    if type(root["recipes"]) is not list:
        raise ValueError("recipes must be a list")

    records = {}
    for index, value in enumerate(root["recipes"], start=1):
        record = _recipe_provenance(value, f"recipe provenance {index}")
        if record.recipe_id in records:
            raise ValueError(f"duplicate recipe_id: {record.recipe_id}")
        records[record.recipe_id] = record

    expected_ids = set(active_ids)
    if any(
        not isinstance(recipe_id, str) or not recipe_id.strip()
        for recipe_id in expected_ids
    ):
        raise ValueError("active recipe IDs must be non-empty text")
    actual_ids = set(records)
    missing = sorted(expected_ids - actual_ids)
    if missing:
        raise ValueError("missing provenance: " + ", ".join(missing))
    extra = sorted(actual_ids - expected_ids)
    if extra:
        raise ValueError("extra provenance: " + ", ".join(extra))
    return MappingProxyType(records)
