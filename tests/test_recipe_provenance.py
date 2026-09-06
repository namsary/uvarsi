import json
from dataclasses import FrozenInstanceError
from datetime import date
from pathlib import Path

import pytest

import app.recipe_provenance as recipe_provenance
from app.recipe_provenance import (
    RecipeProvenance,
    SourceReference,
    load_recipe_provenance,
)


def source(
    recipe_id,
    *,
    lane="modern_family",
    core=False,
    urls=None,
):
    if urls is None:
        urls = ["https://example.sk/recept"]
    return {
        "recipe_id": recipe_id,
        "editorial_lane": lane,
        "core": core,
        "references": [
            {
                "url": url,
                "title": f"Pôvodný názov {index}",
                "accessed_on": "2026-09-05",
            }
            for index, url in enumerate(urls, start=1)
        ],
    }


def write_sources(tmp_path, records):
    path = tmp_path / "recipe_sources.json"
    path.write_text(
        json.dumps({"schema_version": 1, "recipes": records}, ensure_ascii=False),
        encoding="utf-8",
    )
    return path


def test_default_provenance_uses_bootstrap_copy_when_legacy_packager_omits_catalog_file(
    monkeypatch, tmp_path
):
    """A server still running the pre-provenance packager can ship one upgrade."""
    missing_canonical = tmp_path / "catalog" / "recipe_sources.json"
    bootstrap = write_sources(tmp_path, [source("one")])
    monkeypatch.setattr(
        recipe_provenance,
        "DEFAULT_RECIPE_PROVENANCE_PATH",
        missing_canonical,
    )
    monkeypatch.setattr(
        recipe_provenance,
        "BOOTSTRAP_RECIPE_PROVENANCE_PATH",
        bootstrap,
        raising=False,
    )

    loaded = load_recipe_provenance({"one"})

    assert set(loaded) == {"one"}


def test_bootstrap_provenance_copy_matches_the_canonical_runtime_asset():
    root = Path(__file__).resolve().parents[1]

    assert (root / "app/recipe_sources.json").read_bytes() == (
        root / "app/catalog/recipe_sources.json"
    ).read_bytes()


def test_provenance_requires_every_active_recipe(tmp_path):
    path = write_sources(tmp_path, records=[source("one")])

    with pytest.raises(ValueError, match="missing provenance: two"):
        load_recipe_provenance({"one", "two"}, path)


def test_core_recipe_requires_two_independent_hosts(tmp_path):
    record = source(
        "classic_chicken_paprikash",
        lane="slovak_classic",
        core=True,
        urls=["https://example.sk/a", "https://example.sk/b"],
    )

    with pytest.raises(ValueError, match="independent source"):
        load_recipe_provenance(
            {record["recipe_id"]}, write_sources(tmp_path, [record])
        )


@pytest.mark.parametrize(
    "urls",
    [
        ["http://example.sk/a"],
        ["https://example.sk/a", "https://example.sk/a"],
    ],
)
def test_provenance_rejects_non_https_and_duplicate_urls(tmp_path, urls):
    record = source("one", urls=urls)

    with pytest.raises(ValueError):
        load_recipe_provenance({"one"}, write_sources(tmp_path, [record]))


def test_provenance_rejects_extra_recipe(tmp_path):
    path = write_sources(tmp_path, [source("one"), source("retired")])

    with pytest.raises(ValueError, match="extra provenance: retired"):
        load_recipe_provenance({"one"}, path)


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (
            lambda record: record.update(editorial_lane="trend_driven"),
            "editorial lane",
        ),
        (lambda record: record.update(references=[]), "source reference"),
        (
            lambda record: record["references"][0].update(title=" "),
            "title",
        ),
        (
            lambda record: record["references"][0].update(
                accessed_on="05-09-2026"
            ),
            "accessed_on",
        ),
        (
            lambda record: record["references"][0].update(
                url="https:///missing-host"
            ),
            "host",
        ),
    ],
)
def test_provenance_rejects_invalid_record_values(tmp_path, change, message):
    record = source("one")
    change(record)

    with pytest.raises(ValueError, match=message):
        load_recipe_provenance({"one"}, write_sources(tmp_path, [record]))


@pytest.mark.parametrize(
    "payload",
    [
        {"schema_version": 2, "recipes": []},
        {"schema_version": 1, "recipes": [], "notes": "unexpected"},
        {"schema_version": 1, "recipes": [source("one", core="false")]},
    ],
)
def test_provenance_rejects_non_exact_schema(tmp_path, payload):
    path = tmp_path / "recipe_sources.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError):
        load_recipe_provenance(set(), path)


def test_provenance_rejects_duplicate_recipe_ids(tmp_path):
    duplicate_ids = write_sources(tmp_path, [source("one"), source("one")])

    with pytest.raises(ValueError, match="duplicate recipe_id: one"):
        load_recipe_provenance({"one"}, duplicate_ids)


def test_provenance_rejects_duplicate_json_keys(tmp_path):
    duplicate_keys = tmp_path / "duplicate-keys.json"
    duplicate_keys.write_text(
        '{"schema_version":1,"schema_version":1,"recipes":[]}',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="duplicate JSON key: schema_version"):
        load_recipe_provenance(set(), duplicate_keys)


def test_provenance_loads_typed_immutable_records(tmp_path):
    record = source(
        "classic_chicken_paprikash",
        lane="slovak_classic",
        core=True,
        urls=["https://varecha.pravda.sk/a", "https://kuchynalidla.sk/b"],
    )

    loaded = load_recipe_provenance(
        {record["recipe_id"]}, write_sources(tmp_path, [record])
    )

    assert loaded == {
        "classic_chicken_paprikash": RecipeProvenance(
            recipe_id="classic_chicken_paprikash",
            editorial_lane="slovak_classic",
            core=True,
            references=(
                SourceReference(
                    url="https://varecha.pravda.sk/a",
                    title="Pôvodný názov 1",
                    accessed_on=date(2026, 9, 5),
                ),
                SourceReference(
                    url="https://kuchynalidla.sk/b",
                    title="Pôvodný názov 2",
                    accessed_on=date(2026, 9, 5),
                ),
            ),
        )
    }
    with pytest.raises(TypeError):
        loaded["another"] = loaded["classic_chicken_paprikash"]
    with pytest.raises(FrozenInstanceError):
        loaded["classic_chicken_paprikash"].core = False
