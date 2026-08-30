# Deterministic Planner Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Z overených akcií, profilu a receptových šablón vytvoriť kompletný sedemdňový plán bez LLM volania a v rovnakom JSON tvare, aký už zobrazuje aplikácia.

**Architecture:** Ponuky sa najprv striktne namapujú na kanonické suroviny. Matcher vyberie kompatibilnú kombináciu šablón deterministickým skórovaním a renderer vypočíta dávky, postup, špajzu, celé balenia, cenu a výživu. Modul ostane za interným rozhraním; produkčné endpointy sa prepnú až v rollout pláne.

**Tech Stack:** Python 3.12, SQLite, Decimal, pytest.

**Spec:** `docs/superpowers/specs/2026-08-30-vlastny-receptovy-engine-design.md`

**Prerequisite:** `docs/superpowers/plans/2026-08-30-recipe-engine-foundation.md`

## Global Constraints

- Rovnaký vstup, týždeň a verzia knižnice musia vytvoriť rovnaký plán.
- Engine nesmie vymyslieť ponuku, cenu, balenie ani surovinu.
- Kalendár musí pokryť presne sedem dní rytmom `7`, `4` alebo `3` varenia.
- Cena aj úspora sa počítajú z celých kupovaných balení.
- Neznáma jednotka alebo chýbajúca kompatibilná ponuka znamená vyradenie kandidáta, nie odhad.
- Cieľ p95 je pod 500 ms po načítaní akcií.
- Platby zostávajú vypnuté.

---

## File Map

- Create `app/offer_matcher.py`: mapovanie overených letákových názvov na kanonické suroviny.
- Create `app/recipe_matcher.py`: kompatibilita, skóre, pestrosť a stabilný tie-break.
- Create `app/recipe_renderer.py`: konkrétne množstvá, názvy a kuchárske kroky.
- Create `app/deterministic_plan.py`: verejné rozhranie enginu a výsledný plán.
- Modify `app/plan_data.py`: iba zdieľané verejné kalendárové a formátovacie utility; bez produkčného prepnutia.
- Test `tests/test_offer_matcher.py`, `tests/test_recipe_matcher.py`, `tests/test_recipe_renderer.py`, `tests/test_deterministic_plan.py`, `tests/test_deterministic_plan_performance.py`.

### Task 1: Fail-closed mapovanie ponúk

**Files:**
- Create: `app/offer_matcher.py`
- Test: `tests/test_offer_matcher.py`

**Interfaces:**
- Consumes: `IngredientCatalog.resolve()` and rows from `weekly_data.offers_for_current_week()`.
- Produces: `MatchedOffer` and `match_offers(rows, catalog) -> Sequence[MatchedOffer]`.

- [ ] **Step 1: Write exact matching tests**

```python
def test_maps_brand_product_to_canonical_ingredient():
    row = offer(nazov="Basmati ryža Golden Sun 1 kg")
    matched = match_offers([row], catalog)
    assert matched[0].ingredient.id == "rice"
    assert matched[0].offer_key == row["offer_key"]

def test_unmapped_product_is_not_guessed():
    assert match_offers([offer(nazov="Rodinná dobrota")], catalog) == ()
```

- [ ] **Step 2: Confirm RED**

Run: `pytest -q tests/test_offer_matcher.py`

- [ ] **Step 3: Implement explicit alias-token matching**

```python
@dataclass(frozen=True)
class MatchedOffer:
    offer_key: str
    store: str
    product_name: str
    ingredient: Ingredient
    package: PackageSize
    sale_price: Decimal
    original_price: Decimal | None
    valid_from: date
    valid_to: date
    source_url: str
```

Match only catalog aliases present as complete normalized token sequences. Prefer the longest alias; if two different ingredients tie, reject the row as ambiguous. Reuse validated `offer_key`, validity and prices; do not parse them again from display text. Reject offers whose package cannot be converted by `quantity_math`.

- [ ] **Step 4: Run tests and commit**

Run: `pytest -q tests/test_offer_matcher.py tests/test_offer_data.py tests/test_weekly_data.py`

```bash
git add app/offer_matcher.py tests/test_offer_matcher.py
git commit -m "feat: map flyer offers to ingredients"
```

### Task 2: Kompatibilita a deterministické skórovanie

**Files:**
- Create: `app/recipe_matcher.py`
- Test: `tests/test_recipe_matcher.py`

**Interfaces:**
- Consumes: `RecipeTemplate`, `MatchedOffer`, pantry quantities and mode.
- Produces: `SlotSelection`, `RecipeCandidate`, `rank_candidates(templates, offers, pantry, mode, seed) -> Sequence[RecipeCandidate]`.

- [ ] **Step 1: Write compatibility and ordering tests**

```python
def test_vegan_mode_never_selects_animal_ingredient():
    candidates = rank_candidates(templates, offers, pantry=(), mode="vegan", seed="w1")
    assert candidates
    assert all(sel.ingredient.diet_tags >= {DietTag.VEGAN}
               for c in candidates for sel in c.selections)

def test_same_seed_produces_same_order():
    first = [c.key for c in rank_candidates(templates, offers, (), "standard", "abc")]
    second = [c.key for c in rank_candidates(templates, offers, (), "standard", "abc")]
    assert first == second
```

- [ ] **Step 2: Confirm RED**

Run: `pytest -q tests/test_recipe_matcher.py`

- [ ] **Step 3: Implement compatibility before scoring**

```python
@dataclass(frozen=True)
class SlotSelection:
    slot: IngredientSlot
    ingredient: Ingredient
    offer: MatchedOffer | None
    pantry: Quantity | None

@dataclass(frozen=True)
class RecipeCandidate:
    template: RecipeTemplate
    selections: Sequence[SlotSelection]
    score: Decimal
    key: str
```

Required slots need a compatible offer or a compatible quantified pantry item. Optional slots may be omitted. Apply diet filtering before any score. Compute score from explicit integer weights:

```python
SCORE_SAVING = 30
SCORE_OFFER_COVERAGE = 20
SCORE_PANTRY_USE = 14
SCORE_STORE_PREFERENCE = 8
PENALTY_PACKAGE_LEFTOVER = 6
PENALTY_RECENT_FAMILY = 18
PENALTY_RECENT_METHOD = 12
```

Use normalized monetary and coverage ratios in `[0, 1]`. Final tie-break is `sha256(f"{seed}:{template.id}:{offer_keys}")`, never `random` or Python's salted `hash()`.

- [ ] **Step 4: Add high-protein eligibility test**

The matcher may prefilter by recipe metadata, but final acceptance requires rendered nutrition of at least 30 g protein per adult serving. A candidate below the target is discarded by the orchestration task, not relabeled.

- [ ] **Step 5: Run tests and commit**

Run: `pytest -q tests/test_recipe_matcher.py`

```bash
git add app/recipe_matcher.py tests/test_recipe_matcher.py
git commit -m "feat: rank compatible recipe candidates"
```

### Task 3: Renderer názvov, množstiev a krokov

**Files:**
- Create: `app/recipe_renderer.py`
- Test: `tests/test_recipe_renderer.py`

**Interfaces:**
- Consumes: `RecipeCandidate`, adults, children, covered days.
- Produces: `RenderedMeal` and `render_meal(candidate, *, adults, children, covered_days) -> RenderedMeal`.

- [ ] **Step 1: Write language and quantity tests**

```python
def test_renderer_uses_human_kitchen_amounts():
    meal = render_meal(candidate, adults=4, children=0, covered_days=3)
    assert "1 980 g" not in " ".join(meal.instructions)
    assert "2 kg" in " ".join(meal.instructions)

def test_every_instruction_placeholder_is_resolved():
    meal = render_meal(candidate, adults=2, children=1, covered_days=2)
    assert all("{" not in step and "}" not in step for step in meal.instructions)
```

- [ ] **Step 2: Confirm RED**

Run: `pytest -q tests/test_recipe_renderer.py`

- [ ] **Step 3: Implement exact serving math**

```python
adult_equivalents = Decimal(adults) + Decimal(children) * slot.child_factor
batch_equivalents = adult_equivalents * Decimal(covered_days)
recipe_amount = Quantity(slot.amount_per_adult * batch_equivalents, slot.unit)
```

The displayed `porcie` remains the count of people-meals (`(adults + children) * covered_days`), while ingredient quantities use adult equivalents. Format g/ml as whole practical values: under 1 kg round to the nearest sensible 5 or 10 g; at or above 1 kg use kg with at most one decimal when exact enough. Never show `.5 g`.

Before nutrition calculation, convert `g` directly, `piece` through `Ingredient.grams_per_piece` and `ml` through `Ingredient.density_g_per_ml`, then apply `edible_ratio`. Missing conversion data rejects the candidate instead of fabricating nutrition.

- [ ] **Step 4: Render instructions from controlled placeholders**

Supported values are `slot.name`, `slot.amount`, `slot.cut` and `portions`. Teploty a časy sú overené literály šablóny, nie dynamické vstupy. Each template step must resolve to a complete Slovak imperative sentence. Reuse `plan_data.validate_recipe_language()` as a release gate, not as a generator. Add snapshot cases for rice, pasta, roasted vegetables, tofu and chicken thighs.

- [ ] **Step 5: Run tests and commit**

Run: `pytest -q tests/test_recipe_renderer.py tests/test_recepty.py`

```bash
git add app/recipe_renderer.py tests/test_recipe_renderer.py
git commit -m "feat: render deterministic Slovak recipes"
```

### Task 4: Shopping list, pantry and whole-package pricing

**Files:**
- Modify: `app/recipe_renderer.py`
- Create: `tests/test_deterministic_shopping_list.py`

**Interfaces:**
- Produces: `build_shopping_list(rendered_meals, pantry) -> list[dict]` compatible with current frontend keys.

- [ ] **Step 1: Write compatibility regression**

```python
def test_shopping_list_uses_frontend_contract_and_whole_pack_price():
    result = build_shopping_list(meals_needing_300g_rice, pantry=[])
    rice = result[0]["polozky"][0]
    assert rice["potrebne"] == "300"
    assert rice["potrebna_jednotka"] == "g"
    assert rice["mnozstvo"] == 1
    assert rice["zostava"] == "700 g"
    assert rice["cena"] == rice["cena_za_balenie"]
```

- [ ] **Step 2: Confirm RED**

Run: `pytest -q tests/test_deterministic_shopping_list.py`

- [ ] **Step 3: Aggregate before package rounding**

Group by exact `offer_key` and package identity, sum recipe consumption across the week, subtract compatible pantry quantities once, then call `purchase_requirement`. Preserve frontend keys `offer_key`, `nazov`, `obchod`, `mnozstvo`, `cena`, `povodna`, `potrebne`, `potrebna_jednotka`, `cena_za_balenie`, `povodna_za_balenie`, `zostava`, `source_url`.

If pantry quantity is unknown, mark the row `mnozstvo_nezname: true` and ask the user to confirm; do not assume full coverage and do not remove the purchase automatically.

- [ ] **Step 4: Test pantry non-duplication and totals**

Run: `pytest -q tests/test_deterministic_shopping_list.py tests/test_spajza_nakupny_zoznam.py`

- [ ] **Step 5: Commit**

```bash
git add app/recipe_renderer.py tests/test_deterministic_shopping_list.py
git commit -m "feat: price deterministic shopping lists"
```

### Task 5: Complete deterministic plan orchestration

**Files:**
- Create: `app/deterministic_plan.py`
- Test: `tests/test_deterministic_plan.py`

**Interfaces:**
- Produces:

```python
build_deterministic_plan(
    *, week: str, rows: Sequence[Mapping[str, object]], stores: Sequence[str],
    adults: int, children: int, frequency: Literal[1, 2, 3],
    pantry: Sequence[PantryEntry], pantry_driven: bool, mode: str, seed: str,
    ingredient_catalog: IngredientCatalog | None = None,
    recipe_catalog: RecipeCatalog | None = None,
) -> dict
```

- [ ] **Step 1: Write end-to-end schedule tests**

```python
@pytest.mark.parametrize("frequency,days,coverage", [
    (1, ["PO","UT","ST","ŠT","PI","SO","NE"], [1,1,1,1,1,1,1]),
    (2, ["PO","ST","PI","NE"], [2,2,2,1]),
    (3, ["PO","ŠT","NE"], [3,3,1]),
])
def test_plan_covers_exactly_seven_days(frequency, days, coverage):
    plan = build_fixture_plan(frequency=frequency)
    assert [meal["den"] for meal in plan["jedla"]] == days
    assert [meal["pokryva_dni"] for meal in plan["jedla"]] == coverage
    assert sum(coverage) == 7
```

- [ ] **Step 2: Confirm RED**

Run: `pytest -q tests/test_deterministic_plan.py`

- [ ] **Step 3: Implement orchestration and diversity selection**

Use `cooking_days_for_frequency()` and `days_covered_by_meal()` from `plan_data.py`. With `pantry_driven=false`, select recipes independently of personal pantry so the base plan remains shareable, then subtract pantry only from the personal shopping list. With `pantry_driven=true`, pantry may satisfy slots and affects scoring; the plan is personal. Select candidates greedily with backtracking bounded to the top 12 candidates per day. Enforce no adjacent same family+method and at least three methods where the catalog allows it. If no complete plan exists, raise `NoCompatiblePlan(code, suggestions)` with codes `insufficient_offers`, `diet_too_strict`, `unmeasurable_packages`.

Return the existing public shape:

```python
{
  "tyzden": week,
  "jedla": [{"den": "PO", "nazov": "Kuracie s ryžou a brokolicou", "recept": {"porcie": 8}}],
  "nakupny_zoznam": [{"obchod": "Lidl", "polozky": []}],
  "nakup_spolu": "17,49",
  "bezna_cena": "27,19",
  "usetrene": "9,70",
  "meta": {"engine": "deterministic", "library_version": 1, "mode": mode}
}
```

- [ ] **Step 4: Add high-protein final gate**

In `high_protein` mode every rendered meal must have `nutrition.serving.protein_g >= 30`. Add `high_protein_claim: true` only when `qualifies_high_protein()` also passes the legal 20%-energy threshold. Otherwise show the estimate without that claim.

- [ ] **Step 5: Run tests and commit**

Run: `pytest -q tests/test_deterministic_plan.py tests/test_plan_data.py tests/test_plan_calendar.py`

```bash
git add app/deterministic_plan.py tests/test_deterministic_plan.py
git commit -m "feat: build deterministic weekly plans"
```

### Task 6: Performance and invariant gate

**Files:**
- Create: `tests/test_deterministic_plan_performance.py`
- Create: `tests/test_deterministic_plan_invariants.py`

**Interfaces:**
- Verifies the public `build_deterministic_plan()` contract.

- [ ] **Step 1: Add invariant tests over a matrix**

Generate fixed cases for 1–12 household members, all three frequencies, all modes and pantry states. Assert no negative quantities, whole package counts, seven covered days, every instruction ingredient appears in the meal and every required ingredient is covered by pantry or purchase.

- [ ] **Step 2: Add a deterministic performance benchmark**

```python
def test_p95_plan_build_is_below_500ms(production_sized_fixture):
    samples = [timed_build(production_sized_fixture, seed=str(i)) for i in range(40)]
    assert statistics.quantiles(samples, n=20)[18] < 0.500
```

Warm catalog loading before timing. The fixture must include at least 850 offers and 60 templates.

- [ ] **Step 3: Run gate**

Run: `pytest -q tests/test_deterministic_plan_invariants.py tests/test_deterministic_plan_performance.py`

Expected: all invariant tests pass and local p95 < 500 ms.

- [ ] **Step 4: Run full suite and commit**

Run: `pytest -q`

```bash
git add tests/test_deterministic_plan_invariants.py tests/test_deterministic_plan_performance.py
git commit -m "test: gate deterministic plan quality and speed"
```
