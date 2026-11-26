"""Curated recipe library size across editorial waves.

Wave 1 is the frozen launch library (104 recipes, pinned by its own snapshot
tests). Wave 2 recipes are listed in docs/research/recipe-targets-wave2.json
and checked generically by tests/test_curated_recipe_wave2.py.
"""
import json
from pathlib import Path

WAVE1_COUNT = 104
WAVE2_TARGETS_PATH = (
    Path(__file__).resolve().parents[1]
    / "docs" / "research" / "recipe-targets-wave2.json"
)


def wave2_targets() -> tuple[dict, ...]:
    payload = json.loads(WAVE2_TARGETS_PATH.read_text(encoding="utf-8"))
    return tuple(payload["targets"])


def wave2_ids() -> tuple[str, ...]:
    return tuple(row["id"] for row in wave2_targets())


def curated_count() -> int:
    return WAVE1_COUNT + len(wave2_targets())
