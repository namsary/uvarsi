# Rozmanitejšie Recepty Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Zabezpečiť, aby týždenný plán nebol monotónny, ale stále používal iba aktuálne overené akcie, sedel na režim 7/4/3 varení a nezvyšoval počet platených AI volaní.

**Architecture:** Prompt dostane explicitné pravidlá rozmanitosti a server každý AI výstup deterministicky klasifikuje podľa hlavnej suroviny, prílohy a spôsobu prípravy. Validátor odmietne iba objektívne porušenie dosiahnuteľného pravidla; pri úzkom letákovom katalógu uvoľní len nedosiahnuteľný limit. Zmena zvýši algoritmickú verziu a vytvorí novú zdieľanú cache bez mazania starej.

**Tech Stack:** Python 3.12, SQLite, Anthropic SDK, deterministic Python validators, pytest, background plan worker.

**Spec:** `docs/superpowers/specs/2026-08-29-diverzita-ceny-pocitadlo-design.md`

## Global Constraints

- Jedna úloha smie vykonať najviac jedno platené Anthropic volanie.
- Server naďalej overuje ponuky, množstvá, kalendár, špajzu a receptový jazyk.
- Rozmanitosť nesmie vynútiť potravinu, ktorá nie je v aktuálnych letákoch.
- Pravidlá musia fungovať pre 7, 4 aj 3 varenia a nesmú meniť počet porcií.
- Existujúce plány sa nemažú; nový `PLAN_ALGO_VERSION` ich iba prestane vydávať ako aktuálne.

---

## Task 1: Add deterministic meal signatures

**Files:**
- Modify: `app/plan_data.py`
- Test: `tests/test_plan_data.py`

- [ ] Add failing table-driven tests for protein families (chicken/pork/beef/fish/legume/egg/cheese/vegetable), side families (rice/pasta/potato/dumpling/bread/legume/none) and methods (oven/pan/pot/soup/no-cook).
- [ ] Include Slovak inflections such as `kuracieho`, `bravčové`, `šošovicový`, `zapeč`, `opeč`, `uvar`, `dusené`.
- [ ] Run the new tests; expect RED.
- [ ] Add immutable signature and helpers:

```python
@dataclass(frozen=True)
class MealDiversitySignature:
    protein: str
    side: str
    method: str

def meal_diversity_signature(name: str, steps: list[str], selected_items: list[tuple]) -> MealDiversitySignature: ...
def _primary_protein(selected_items: list[tuple]) -> str: ...
def _dominant_side(selected_items: list[tuple]) -> str: ...
def _preparation_method(name: str, steps: list[str]) -> str: ...
```

- [ ] Prefer verified row role/category/name over recipe prose; use folded Slovak text only as fallback.
- [ ] Run focused tests; expect GREEN.
- [ ] Commit: `feat(plans): classify recipe diversity`.

## Task 2: Compute availability-aware diversity limits

**Files:**
- Modify: `app/plan_data.py`
- Test: `tests/test_plan_data.py`

- [ ] Add failing tests for rich and narrow offer catalogs. Rich catalogs require max two identical proteins, max two identical sides and `min(3, meal_count)` methods. A catalog with only one feasible protein relaxes only the protein limit.
- [ ] Run focused tests; expect RED.
- [ ] Implement:

```python
@dataclass(frozen=True)
class DiversityLimits:
    max_same_protein: int
    max_same_side: int
    required_methods: int

def diversity_limits(rows: list[dict], meal_count: int) -> DiversityLimits: ...
def validate_weekly_diversity(parsed_meals: list[tuple], rows: list[dict]) -> None: ...
```

- [ ] Define similar consecutive meals as matching both primary protein and dominant side; reject that pair on adjacent cooking days.
- [ ] Do not infer feasible cooking methods from flyers. Require `min(3, meal_count)` methods because every verified ingredient can be prepared by multiple ordinary methods; only method count can drop when meal count is below three.
- [ ] Run focused tests; expect GREEN.
- [ ] Commit: `feat(plans): validate attainable weekly variety`.

## Task 3: Wire validation into model parsing

**Files:**
- Modify: `app/plan_data.py`
- Test: `tests/test_plan_data.py`
- Test: `tests/test_server.py`

- [ ] Add failing tests proving `_model_meals()` rejects a repetitive 7-meal output, accepts a diverse 7/4/3 output and retains all existing amount/pantry/calendar checks.
- [ ] Run focused tests; expect RED.
- [ ] Call `validate_weekly_diversity(parsed, list(offers_by_key.values()))` after day completeness and before returning sorted meals.
- [ ] Ensure validation errors are stable internal errors and are never shown verbatim to end users.
- [ ] Run focused parser/server tests; expect GREEN.
- [ ] Commit: `feat(plans): enforce weekly variety server-side`.

## Task 4: Align the Anthropic prompt with the validator

**Files:**
- Modify: `app/plan_data.py`
- Test: `tests/test_plan_data.py`

- [ ] Add failing prompt tests asserting the exact limits, non-consecutive rule and requested method count are present for frequencies 1/2/3.
- [ ] Run focused tests; expect RED.
- [ ] Add one generated prompt block from the same `DiversityLimits` object used by validation:

```text
ROZMANITOSŤ TÝŽDŇA
- Rovnaký hlavný druh bielkoviny použi najviac 2×, ak katalóg ponúka alternatívy.
- Rovnakú dominantnú prílohu použi najviac 2×, ak katalóg ponúka alternatívy.
- Použi aspoň N odlišné spôsoby prípravy.
- Dve po sebe idúce varenia nesmú mať súčasne rovnakú bielkovinu aj prílohu.
```

- [ ] Keep `PLAN_VARIANT_HINTS` as broad cuisine direction, not a substitute for within-week diversity.
- [ ] Run focused tests; expect GREEN.
- [ ] Commit: `feat(plans): prompt for validated weekly variety`.

## Task 5: Version cache and test worker cost behavior

**Files:**
- Modify: `app/plan_data.py`
- Modify: `tests/test_plan_cache_versioning.py`
- Modify: `tests/test_plan_worker.py`
- Modify: `VERSION`

- [ ] Add failing tests expecting the next algorithm version and proving one job still dispatches at most one model call.
- [ ] Bump `PLAN_ALGO_VERSION` from 15 to 16 and update its changelog comment.
- [ ] Run `pytest -q tests/test_plan_data.py tests/test_plan_cache_versioning.py tests/test_plan_worker.py tests/test_server.py`; expect GREEN.
- [ ] Run `pytest -q`; expect zero failures.
- [ ] Bump `VERSION` and commit: `release: diversify weekly recipes`.

## Task 6: Controlled production rollout

**Files:**
- Modify: `docs/prevadzka.md`

- [ ] Record current queue depth, AI ledger total and worker heartbeat before deploy.
- [ ] Deploy without deleting old cache and verify `/api/health` plus worker heartbeat.
- [ ] Generate one controlled plan for each frequency 1/2/3 and inspect protein, side, method, quantities, pantry subtraction and shopping packages.
- [ ] Confirm the ledger increased by no more than three calls and repeated identical requests hit shared cache.
- [ ] Verify no repetitive pair appears consecutively and no plan lost Sunday coverage.
- [ ] Commit rollout notes: `docs: record recipe diversity verification`.

## Final Verification

- [ ] Run `pytest -q` and record total passing tests.
- [ ] Run `rg -n "PLAN_ALGO_VERSION|MealDiversitySignature|DiversityLimits|validate_weekly_diversity" app tests` and confirm names/signatures agree.
- [ ] Check three real current-week plans manually for culinary sense; this is release evidence, not a replacement for tests.
- [ ] Request independent code review before pushing to `origin/main`.
