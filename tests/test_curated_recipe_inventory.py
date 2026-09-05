from __future__ import annotations

from collections import Counter
from datetime import date
import json
from pathlib import Path
import re
import unicodedata
from urllib.parse import urlsplit

import pytest


ROOT = Path(__file__).resolve().parents[1]
CANDIDATES_PATH = ROOT / "docs" / "research" / "recipe-candidates.json"
TARGETS_PATH = ROOT / "docs" / "research" / "recipe-targets.json"

ALLOWED_EDITORIAL_LANES = {
    "slovak_classic",
    "modern_family",
    "high_protein",
    "plant_based",
}
ALLOWED_MODES = {"standard", "high_protein", "vegetarian", "vegan"}
CANDIDATE_KEYS = {
    "id",
    "display_name",
    "editorial_lane",
    "expected_modes",
    "core",
    "references",
    "familiarity_evidence",
    "selection_notes",
}
TARGET_KEYS = {
    "id",
    "display_name",
    "editorial_lane",
    "expected_modes",
    "core",
    "references",
    "selection_reason",
}
REFERENCE_KEYS = {"url", "title", "accessed_on"}
SELECTION_NOTE_KEYS = {"summary", "scores"}
SCORE_KEYS = {
    "slovak_familiarity",
    "flyer_availability",
    "family_practicality",
    "safe_substitutability",
    "diet_contribution",
    "distinctiveness",
}
EXPECTED_TARGET_COUNTS = {
    "slovak_classic": 42,
    "modern_family": 36,
    "high_protein": 16,
    "plant_based": 10,
}
SLOVAK_MARKERS = set("áäčďéíĺľňóôŕšťúýž")
FORBIDDEN_CONTENT_KEYS = {
    "directions",
    "image",
    "images",
    "instruction",
    "instructions",
    "photo",
    "photos",
}


def _frozen_ids(raw: str) -> frozenset[str]:
    return frozenset(part for part in re.split(r"[\s,]+", raw.strip()) if part)


FROZEN_TARGET_IDS_BY_LANE = {
    "slovak_classic": _frozen_ids(
        """
        classic_chicken_paprikash, classic_chicken_perkelt,
        classic_slovak_chicken_risotto, classic_roast_chicken_thighs_potatoes,
        classic_chicken_noodle_soup, classic_chicken_sote_rice,
        classic_pork_natural_rice, classic_pork_shoulder_onion,
        classic_segedin_goulash, classic_pork_perkelt,
        classic_french_potatoes, classic_meatballs_mash,
        classic_stuffed_peppers, classic_beef_goulash,
        classic_tomato_meatballs, classic_bolognese_spaghetti,
        classic_baked_pasta_ham, classic_pork_cabbage_bake,
        classic_meatloaf_potatoes, classic_roast_pork_root_veg,
        classic_beef_barley_soup, classic_goulash_soup,
        classic_potato_stew_egg, classic_lentil_stew_egg,
        classic_bean_stew_egg, classic_pumpkin_stew_egg,
        classic_pea_stew_egg, classic_lecho_egg, classic_granadir,
        classic_cabbage_noodles, classic_bryndza_dumplings,
        classic_cabbage_dumplings, classic_potato_pancakes,
        classic_sauerkraut_soup, classic_bean_soup,
        classic_sour_lentil_soup, classic_potato_soup,
        classic_garlic_soup, classic_tomato_soup,
        classic_cauliflower_soup, classic_rice_pudding,
        classic_apple_bread_pudding
        """
    ),
    "modern_family": _frozen_ids(
        """
        modern_chicken_curry_rice, modern_chicken_mushroom_pasta,
        modern_one_pot_chicken_rice, modern_teriyaki_chicken_broccoli,
        modern_chicken_fajita_tortilla, modern_chicken_caesar_salad,
        modern_baked_chicken_zucchini, modern_pork_noodle_stir_fry,
        modern_meatballs_tomato_pasta, modern_chili_con_carne,
        modern_cottage_shepherd_pie, modern_family_lasagne,
        modern_tuna_tomato_pasta, modern_tuna_pasta_salad,
        modern_salmon_potato_broccoli, modern_white_fish_tomato_rice,
        modern_fish_tacos_yogurt, modern_feta_tomato_pasta,
        modern_mushroom_risotto, modern_pumpkin_risotto,
        quick_broccoli_cheese_pasta, quick_pesto_chicken_pasta,
        quick_gnocchi_spinach, quick_couscous_grilled_cheese,
        quick_egg_fried_rice, quick_shakshuka, quick_vegetable_frittata,
        quick_tuna_cheese_tortilla, quick_baked_potato_cottage,
        quick_zucchini_fritters, quick_cauliflower_curry,
        quick_orzo_chicken_one_pan, quick_tomato_mozzarella_pasta,
        quick_pea_ham_risotto, quick_egg_potato_spinach_pan,
        quick_chickpea_tomato_couscous
        """
    ),
    "high_protein": _frozen_ids(
        """
        protein_turkey_couscous, protein_chicken_bulgur,
        protein_chicken_yogurt_potato, protein_beef_rice_bowl,
        protein_cottage_tomato_pasta, protein_cottage_potato_spinach,
        protein_tuna_bean_salad, protein_egg_cottage_salad,
        protein_salmon_couscous_salad, protein_chicken_lentil_stew,
        protein_turkey_meatballs, protein_tofu_broccoli_rice,
        protein_cottage_lentil_lasagne, protein_beef_bean_chili,
        protein_chicken_yogurt_traybake, protein_skyr_chicken_wrap
        """
    ),
    "plant_based": _frozen_ids(
        """
        plant_red_lentil_dal, plant_chickpea_curry, plant_bean_chili,
        plant_lentil_bolognese, plant_tofu_coconut_curry,
        plant_tofu_tomato_pasta, plant_chickpea_couscous_salad,
        plant_bean_potato_goulash, plant_mushroom_barley,
        plant_lentil_loaf
        """
    ),
}
FROZEN_TARGET_IDS = frozenset().union(*FROZEN_TARGET_IDS_BY_LANE.values())


def _reject_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        assert key not in result, f"duplicate JSON key: {key}"
        result[key] = value
    return result


def _load_json(path: Path):
    assert path.is_file(), f"missing research inventory: {path.relative_to(ROOT)}"
    raw = path.read_bytes()
    text = raw.decode("utf-8")
    return json.loads(text, object_pairs_hook=_reject_duplicate_keys)


def _load_inventories():
    candidates_payload = _load_json(CANDIDATES_PATH)
    targets_payload = _load_json(TARGETS_PATH)
    return candidates_payload["candidates"], targets_payload["targets"]


def _assert_nonempty_text(value, label: str) -> None:
    assert isinstance(value, str), f"{label} must be text"
    assert value.strip(), f"{label} must not be blank"


def _assert_slovak_text(value, label: str) -> None:
    _assert_nonempty_text(value, label)
    assert SLOVAK_MARKERS & set(value.casefold()), f"{label} must be Slovak text"


def _reference_hosts(row: dict) -> set[str]:
    return {
        urlsplit(reference["url"]).hostname.casefold()
        for reference in row["references"]
    }


def _looks_like_recipe_page(url: str) -> bool:
    path = urlsplit(url).path.casefold().rstrip("/")
    return (
        "/collection/" not in path
        and not path.endswith(".pdf")
        and not path.endswith("/recipes")
        and not path.endswith("/recepty")
    )


def _normalized_display_name(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value.casefold())
    without_marks = "".join(
        character
        for character in decomposed
        if not unicodedata.combining(character)
    )
    return " ".join(re.findall(r"[a-z0-9]+", without_marks))


def _walk_keys(value):
    if isinstance(value, dict):
        for key, child in value.items():
            yield key
            yield from _walk_keys(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_keys(child)


@pytest.mark.parametrize("path", [CANDIDATES_PATH, TARGETS_PATH])
def test_inventory_files_exist(path):
    assert path.is_file(), f"missing research inventory: {path.relative_to(ROOT)}"


def test_inventory_roots_are_valid_utf8_json_with_exact_schemas():
    candidates_payload = _load_json(CANDIDATES_PATH)
    targets_payload = _load_json(TARGETS_PATH)

    assert set(candidates_payload) == {"schema_version", "candidates"}
    assert set(targets_payload) == {"schema_version", "targets"}
    assert type(candidates_payload["schema_version"]) is int
    assert type(targets_payload["schema_version"]) is int
    assert candidates_payload["schema_version"] == 1
    assert targets_payload["schema_version"] == 1
    assert type(candidates_payload["candidates"]) is list
    assert type(targets_payload["targets"]) is list


def test_candidate_inventory_has_exact_count_ids_and_row_schema():
    candidates, _ = _load_inventories()

    assert len(candidates) == 160
    candidate_ids = [row["id"] for row in candidates]
    assert len(set(candidate_ids)) == len(candidate_ids)

    for row in candidates:
        assert set(row) == CANDIDATE_KEYS
        assert re.fullmatch(r"[a-z0-9]+(?:_[a-z0-9]+)*", row["id"])
        _assert_nonempty_text(row["display_name"], f"{row['id']} display_name")
        assert row["editorial_lane"] in ALLOWED_EDITORIAL_LANES
        assert type(row["core"]) is bool
        assert type(row["expected_modes"]) is list
        assert row["expected_modes"]
        assert len(row["expected_modes"]) == len(set(row["expected_modes"]))
        assert set(row["expected_modes"]) <= ALLOWED_MODES
        assert "standard" in row["expected_modes"]


def test_target_inventory_is_the_exact_frozen_release_set_and_lane_mix():
    candidates, targets = _load_inventories()

    assert len(FROZEN_TARGET_IDS) == 104
    assert {
        lane: len(ids) for lane, ids in FROZEN_TARGET_IDS_BY_LANE.items()
    } == EXPECTED_TARGET_COUNTS
    assert len(targets) == 104

    target_ids = [row["id"] for row in targets]
    assert len(set(target_ids)) == len(target_ids)
    assert set(target_ids) == FROZEN_TARGET_IDS
    assert set(target_ids) <= {row["id"] for row in candidates}
    assert Counter(row["editorial_lane"] for row in targets) == (
        EXPECTED_TARGET_COUNTS
    )

    for row in targets:
        assert set(row) == TARGET_KEYS
        assert row["id"] in FROZEN_TARGET_IDS_BY_LANE[row["editorial_lane"]]
        assert row["core"] is True


def test_target_values_are_derived_from_their_candidate_records():
    candidates, targets = _load_inventories()
    candidate_by_id = {row["id"]: row for row in candidates}
    shared_keys = {
        "id",
        "display_name",
        "editorial_lane",
        "expected_modes",
        "core",
        "references",
    }

    for target in targets:
        candidate = candidate_by_id[target["id"]]
        assert {key: target[key] for key in shared_keys} == {
            key: candidate[key] for key in shared_keys
        }
        assert target["selection_reason"] == candidate["selection_notes"]["summary"]


def test_core_mapping_matches_the_frozen_selection_exactly():
    candidates, targets = _load_inventories()

    assert {row["id"] for row in candidates if row["core"]} == FROZEN_TARGET_IDS
    assert all(row["core"] for row in targets)
    for row in candidates:
        assert row["core"] is (row["id"] in FROZEN_TARGET_IDS)


def test_references_have_exact_schema_and_strong_selected_provenance():
    candidates, targets = _load_inventories()

    for collection_name, rows in (("candidate", candidates), ("target", targets)):
        for row in rows:
            references = row["references"]
            assert type(references) is list
            assert references, f"{collection_name} {row['id']} needs a reference"
            urls = []
            for reference in references:
                assert set(reference) == REFERENCE_KEYS
                _assert_nonempty_text(
                    reference["title"],
                    f"{collection_name} {row['id']} reference title",
                )
                _assert_nonempty_text(
                    reference["accessed_on"],
                    f"{collection_name} {row['id']} reference date",
                )
                assert date.fromisoformat(reference["accessed_on"])

                url = reference["url"]
                _assert_nonempty_text(
                    url, f"{collection_name} {row['id']} reference URL"
                )
                parsed = urlsplit(url)
                assert parsed.scheme == "https"
                assert parsed.hostname
                urls.append(url)

            assert len(urls) == len(set(urls))
            assert any(_looks_like_recipe_page(url) for url in urls), (
                f"{collection_name} {row['id']} needs a direct recipe-page reference"
            )
            if row["core"] or collection_name == "target":
                assert len(_reference_hosts(row)) >= 2


def test_quick_orzo_has_two_direct_sources_and_no_tesco_collection():
    candidates, targets = _load_inventories()
    rows = (
        {row["id"]: row for row in candidates}["quick_orzo_chicken_one_pan"],
        {row["id"]: row for row in targets}["quick_orzo_chicken_one_pan"],
    )
    food_network_reference = {
        "url": (
            "https://www.foodnetwork.com/recipes/valerie-bertinelli/"
            "one-pot-chicken-and-orzo-12667478"
        ),
        "title": "One Pot Chicken and Orzo Recipe | Valerie Bertinelli | Food Network",
        "accessed_on": "2026-09-05",
    }
    expected_urls = {
        "https://www.bbcgoodfood.com/recipes/marry-me-chicken-orzo",
        food_network_reference["url"],
    }
    tesco_collection_url = "https://realfood.tesco.com/chicken-recipes.html"

    for row in rows:
        urls = {reference["url"] for reference in row["references"]}
        assert urls == expected_urls
        assert tesco_collection_url not in urls
        assert food_network_reference in row["references"]
        assert all(_looks_like_recipe_page(url) for url in urls)
        assert len(_reference_hosts(row)) == 2


def test_protein_beef_chili_is_a_distinct_baked_stuffed_sweet_potato():
    candidates, targets = _load_inventories()
    candidate_by_id = {row["id"]: row for row in candidates}
    target_by_id = {row["id"]: row for row in targets}
    protein_candidate = candidate_by_id["protein_beef_bean_chili"]
    protein_target = target_by_id["protein_beef_bean_chili"]
    family_chili = candidate_by_id["modern_chili_con_carne"]
    expected_display_name = "Proteínový batat plnený hovädzím chilli"
    old_food_network_url = (
        "https://www.foodnetwork.com/recipes/food-network-kitchen/"
        "chili-stuffed-sweet-potatoes-recipe-2120999"
    )
    expected_references = [
        {
            "url": "https://terianncarty.com/high-protein-stuffed-sweet-potatoes/",
            "title": "High Protein Stuffed Sweet Potatoes | Teri-Ann Carty Recipes",
            "accessed_on": "2026-09-05",
        },
        {
            "url": (
                "https://www.hellofresh.co.uk/recipes/"
                "easy-cheesy-beef-chilli-loaded-sweet-potato-67bdff0a8e41cf2506379267"
            ),
            "title": "Easy Cheesy Beef Chilli Loaded Sweet Potato Recipe | HelloFresh",
            "accessed_on": "2026-09-05",
        },
    ]

    assert family_chili["display_name"] == "Chilli con carne"
    assert protein_candidate["display_name"] != family_chili["display_name"]
    for row in (protein_candidate, protein_target):
        assert row["display_name"] == expected_display_name
        assert row["editorial_lane"] == "high_protein"
        assert row["expected_modes"] == ["standard", "high_protein"]
        assert row["references"] == expected_references
        assert old_food_network_url not in {
            reference["url"] for reference in row["references"]
        }
        assert all(
            _looks_like_recipe_page(reference["url"])
            for reference in row["references"]
        )
        assert len(_reference_hosts(row)) == 2

    editorial_texts = (
        protein_candidate["familiarity_evidence"],
        protein_candidate["selection_notes"]["summary"],
        protein_target["selection_reason"],
    )
    for text in editorial_texts:
        normalized = text.casefold()
        assert "batat" in normalized
        assert re.search(r"\b(?:peč|upeč)\w*", normalized)
        assert re.search(r"\bpln\w*", normalized)
        assert "chilli con carne" in normalized
        assert "odliš" in normalized or "iný" in normalized


def test_candidate_selection_notes_have_exact_scores_and_slovak_text():
    candidates, _ = _load_inventories()

    for row in candidates:
        notes = row["selection_notes"]
        assert type(notes) is dict
        assert set(notes) == SELECTION_NOTE_KEYS
        _assert_slovak_text(notes["summary"], f"{row['id']} selection summary")
        _assert_slovak_text(
            row["familiarity_evidence"], f"{row['id']} familiarity evidence"
        )

        scores = notes["scores"]
        assert type(scores) is dict
        assert set(scores) == SCORE_KEYS
        for score_name, score in scores.items():
            assert type(score) is int, f"{row['id']} {score_name} must be an integer"
            assert 0 <= score <= 5, f"{row['id']} {score_name} must be from 0 to 5"


def test_target_selection_reasons_are_concise_natural_slovak():
    _, targets = _load_inventories()

    for row in targets:
        reason = row["selection_reason"]
        _assert_slovak_text(reason, f"{row['id']} selection reason")
        assert len(reason) <= 360


def test_display_names_have_no_normalized_near_duplicates():
    candidates, targets = _load_inventories()

    for collection_name, rows in (("candidate", candidates), ("target", targets)):
        normalized = [_normalized_display_name(row["display_name"]) for row in rows]
        assert all(normalized)
        duplicates = [
            name for name, count in Counter(normalized).items() if count > 1
        ]
        assert not duplicates, (
            f"{collection_name} display names collide after normalization: "
            f"{duplicates}"
        )


def test_inventories_do_not_contain_copied_instructions_or_photo_fields():
    payloads = (_load_json(CANDIDATES_PATH), _load_json(TARGETS_PATH))
    present_keys = {key.casefold() for payload in payloads for key in _walk_keys(payload)}
    assert not (present_keys & FORBIDDEN_CONTENT_KEYS)
