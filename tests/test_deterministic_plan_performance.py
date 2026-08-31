from __future__ import annotations

import math
from time import perf_counter_ns

from app.deterministic_plan import build_deterministic_plan
from app.ingredient_catalog import load_ingredient_catalog
from app.offer_matcher import match_offers
from tests.test_deterministic_plan_invariants import STORES, WEEK, make_fixture


BUILD_COUNT = 40
P95_LIMIT_MS = 500.0
BUILD_SEEDS = tuple(f"production-sized-performance-{index:02d}" for index in range(BUILD_COUNT))


def test_production_sized_fixture_builds_under_p95_budget():
    fixture = make_fixture(offer_count=850, template_count=60)
    ingredients = load_ingredient_catalog()
    matched_offers = tuple(match_offers(fixture.rows, ingredients))
    kwargs = {
        "week": WEEK,
        "rows": fixture.rows,
        "stores": STORES,
        "adults": 2,
        "children": 2,
        "frequency": 2,
        "pantry": (),
        "pantry_driven": False,
        "mode": "standard",
        "ingredient_catalog": ingredients,
        "recipe_catalog": fixture.recipes,
    }

    # Warm catalog-backed caches and renderer validation before the measured builds.
    warm_plan = build_deterministic_plan(**kwargs, seed="performance-warmup")
    assert sum(meal["pokryva_dni"] for meal in warm_plan["jedla"]) == 7

    samples_ms = []
    for seed in BUILD_SEEDS:
        started = perf_counter_ns()
        plan = build_deterministic_plan(**kwargs, seed=seed)
        samples_ms.append((perf_counter_ns() - started) / 1_000_000)
        assert sum(meal["pokryva_dni"] for meal in plan["jedla"]) == 7

    ordered = sorted(samples_ms)
    p95_index = math.ceil(0.95 * len(ordered)) - 1
    p95_ms = ordered[p95_index]
    print(
        "deterministic_plan_benchmark "
        f"offers={len(fixture.rows)} templates={len(fixture.recipes.all())} "
        f"builds={len(samples_ms)} p95_ms={p95_ms:.3f} "
        f"min_ms={ordered[0]:.3f} max_ms={ordered[-1]:.3f}"
    )

    assert len(fixture.rows) >= 850
    assert len(matched_offers) >= 850
    assert len(fixture.recipes.all()) >= 60
    assert len(samples_ms) == 40
    assert len(set(BUILD_SEEDS)) == 40
    assert p95_ms < P95_LIMIT_MS
