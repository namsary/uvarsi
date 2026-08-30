# Recipe Engine Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Vytvoriť verziované, validované dátové jadro surovín, výživy, receptových šablón, množstiev v špajzi a celých nákupných balení bez zmeny produkčného toku plánov.

**Architecture:** Nové malé moduly budú načítavať JSON katalógy do nemenných dataclasses a poskytovať čisté výpočtové funkcie. Súčasný `plan_data.py` ostane kompatibilný; v tejto fáze sa žiadny používateľský endpoint neprepína na nový engine.

**Tech Stack:** Python 3.12, standard library (`dataclasses`, `decimal`, `json`, `pathlib`), SQLite, pytest.

**Spec:** `docs/superpowers/specs/2026-08-30-vlastny-receptovy-engine-design.md`

## Global Constraints

- Používateľská požiadavka nesmie volať Anthropic, OpenAI ani iný generatívny model po finálnom prepnutí.
- Cena nákupu sa počíta z celých kupovaných balení, nie zo spotrebovaného podielu.
- Existujúce riadky špajze bez množstva musia po migrácii zostať zachované.
- Výživové hodnoty sa zobrazujú ako odhad, ak nejde o overený značkový údaj.
- Platby zostávajú vypnuté.
- Každý nový runtime modul a dátový súbor musí byť súčasťou manuálneho aj samopull deployu.

---

## File Map

- Create `app/ingredient_catalog.py`: kanonické suroviny, synonymá, stravovacie značky a načítanie katalógu.
- Create `app/recipe_catalog.py`: schéma receptových šablón a fail-closed validácia.
- Create `app/nutrition.py`: deterministický výpočet energie a makier.
- Create `app/quantity_math.py`: jednotky, celé balenia, spotreba a zvyšky.
- Create `app/catalog/ingredients.json`: zdrojové údaje surovín.
- Create `app/catalog/recipes/manifest.json`: explicitná verzia knižnice.
- Create `app/catalog/recipes/smoke.json`: tri neaktívne vývojové šablóny na overenie schémy.
- Modify `app/server.py`: iba bezpečná migrácia množstva v špajzi; bez zmeny endpointov.
- Modify `nasad.ps1`, `hetzner/samopull.sh`: prenos nových modulov a katalógov.
- Test `tests/test_ingredient_catalog.py`, `tests/test_recipe_catalog.py`, `tests/test_nutrition.py`, `tests/test_quantity_math.py`, `tests/test_pantry_quantity_migration.py`.

### Task 0: Close the already-written concise-step regression

**Files:**
- Modify: `app/plan_data.py` at `_cookable_steps()` and `PLAN_ALGO_VERSION`.
- Preserve and commit: `tests/test_plan_data.py`, `tests/test_plan_cache_versioning.py` changes already present in the worktree.

**Interfaces:**
- The legacy engine may accept at most two allow-listed concise actions in an otherwise complete 5–7 step recipe.
- All existing quantity, time, heat, doneness and serving checks remain mandatory.

- [ ] **Step 1: Run the two existing failing tests**

Run: `pytest -q tests/test_plan_data.py::test_accepts_two_self_explanatory_concise_steps_in_an_otherwise_complete_recipe tests/test_plan_cache_versioning.py::test_algo_version_is_a_positive_integer`

Expected: concise-step test fails and version test expects 18 while code is 17.

- [ ] **Step 2: Make the minimal legacy fix**

Change only:

```python
allowed_concise = 2 if len(steps) >= 5 else 0
```

Keep `_SAFE_CONCISE_ACTION`, majority-numeric, duration, heat, doneness and final-serving checks intact. Add the version comment for 18 and set `PLAN_ALGO_VERSION = 18` so failed v17 cache/jobs cannot be reused.

- [ ] **Step 3: Run focused and neighboring tests**

Run: `pytest -q tests/test_plan_data.py tests/test_plan_cache_versioning.py`

Expected: PASS.

- [ ] **Step 4: Commit only the legacy regression**

```bash
git add app/plan_data.py tests/test_plan_data.py tests/test_plan_cache_versioning.py
git commit -m "fix: accept safe concise recipe actions"
```

### Task 1: Kanonický katalóg surovín

**Files:**
- Create: `app/ingredient_catalog.py`
- Create: `app/catalog/ingredients.json`
- Test: `tests/test_ingredient_catalog.py`

**Interfaces:**
- Produces: `DietTag`, `NutritionPer100`, `Ingredient`, `IngredientCatalog`, `load_ingredient_catalog(path=None)`.
- `IngredientCatalog.resolve(text: str) -> Ingredient | None` musí používať presný normalizovaný názov alebo synonymum; fuzzy matching nie je v tejto fáze povolený.

- [ ] **Step 1: Write failing loader and resolution tests**

```python
def test_catalog_resolves_slovak_synonym_without_guessing():
    catalog = load_ingredient_catalog(FIXTURE)
    assert catalog.resolve("kuracie prsia").id == "chicken_breast"
    assert catalog.resolve("kuracinka") is None

def test_catalog_rejects_duplicate_synonym():
    with pytest.raises(ValueError, match="duplicitné synonymum"):
        load_ingredient_catalog(duplicate_fixture)
```

- [ ] **Step 2: Run the focused tests and confirm RED**

Run: `pytest -q tests/test_ingredient_catalog.py`

Expected: import failure for `app.ingredient_catalog`.

- [ ] **Step 3: Implement immutable catalog types and strict validation**

```python
class DietTag(str, Enum):
    VEGETARIAN = "vegetarian"
    VEGAN = "vegan"

@dataclass(frozen=True)
class NutritionPer100:
    kcal: Decimal
    protein_g: Decimal
    fat_g: Decimal
    carbs_g: Decimal
    source: str
    verified_on: date

@dataclass(frozen=True)
class Ingredient:
    id: str
    name: str
    synonyms: Sequence[str]
    category: str
    roles: frozenset[str]
    diet_tags: frozenset[DietTag]
    allergens: Sequence[str]
    edible_ratio: Decimal
    grams_per_piece: Decimal | None
    density_g_per_ml: Decimal | None
    nutrition: NutritionPer100

class IngredientCatalog:
    def __init__(self, ingredients: Iterable[Ingredient]):
        values = tuple(ingredients)
        self._by_id = {item.id: item for item in values}
        self._by_alias = build_alias_index(values)

    def by_id(self, ingredient_id: str) -> Ingredient:
        return self._by_id[ingredient_id]

    def resolve(self, text: str) -> Ingredient | None:
        return self._by_alias.get(normalize_name(text))
```

Normalize with Unicode NFKC, trim, collapse whitespace and `casefold()`. Reject duplicate IDs, names, synonyms, non-positive energy/macros, edible ratios outside `(0, 1]`, non-positive piece/density conversions, unknown roles and inconsistent diet tags (vegan implies vegetarian).

- [ ] **Step 4: Add the first verified catalog slice**

`ingredients.json` must contain at least these IDs so later plans have stable fixtures: `chicken_breast`, `chicken_thigh`, `pork_shoulder`, `beef_mince`, `salmon`, `tofu`, `red_lentils`, `chickpeas`, `egg`, `cottage_cheese`, `rice`, `pasta`, `potato`, `bread`, `zucchini`, `tomato`, `onion`, `garlic`, `carrot`, `broccoli`, `milk`, `cream`, `hard_cheese`, `oil`, `salt`, `black_pepper`.

Each entry must include a named source and verification date. Test data values against explicit fixture assertions, for example `catalog.by_id("rice").nutrition.protein_g > 0`, rather than copying unverified internet numbers into the test.

- [ ] **Step 5: Run tests and commit**

Run: `pytest -q tests/test_ingredient_catalog.py`

Expected: PASS.

```bash
git add app/ingredient_catalog.py app/catalog/ingredients.json tests/test_ingredient_catalog.py
git commit -m "feat: add canonical ingredient catalog"
```

### Task 2: Fail-closed schéma receptových šablón

**Files:**
- Create: `app/recipe_catalog.py`
- Create: `app/catalog/recipes/manifest.json`
- Create: `app/catalog/recipes/smoke.json`
- Test: `tests/test_recipe_catalog.py`

**Interfaces:**
- Consumes: `IngredientCatalog.by_id()` from Task 1.
- Produces: `IngredientSlot`, `InstructionTemplate`, `RecipeTemplate`, `RecipeCatalog.version`, `load_recipe_catalog(ingredient_catalog, root=None, include_inactive=False)`.

- [ ] **Step 1: Write schema tests**

```python
def test_loads_only_active_templates_by_default():
    catalog = load_recipe_catalog(ingredients, FIXTURES)
    assert [recipe.id for recipe in catalog.all()] == ["chicken_rice_pan"]

def test_rejects_vegan_recipe_with_nonvegan_candidate():
    with pytest.raises(ValueError, match="vegan"):
        load_recipe_catalog(ingredients, invalid_vegan_root)

def test_rejects_instruction_unknown_slot():
    with pytest.raises(ValueError, match="neznáma pozícia"):
        load_recipe_catalog(ingredients, unknown_slot_root)
```

- [ ] **Step 2: Confirm RED**

Run: `pytest -q tests/test_recipe_catalog.py`

Expected: import failure.

- [ ] **Step 3: Implement exact template types**

```python
@dataclass(frozen=True)
class IngredientSlot:
    key: str
    role: str
    candidates: Sequence[str]
    amount_per_adult: Decimal
    unit: Literal["g", "ml", "piece"]
    child_factor: Decimal
    required: bool
    use: Literal["main", "addition"]
    cut: str | None

@dataclass(frozen=True)
class InstructionTemplate:
    text: str

@dataclass(frozen=True)
class RecipeTemplate:
    id: str
    version: int
    active: bool
    name_template: str
    family: str
    method: str
    minutes: int
    modes: frozenset[str]
    equipment: Sequence[str]
    slots: Sequence[IngredientSlot]
    pantry_basics: Sequence[str]
    instructions: Sequence[InstructionTemplate]
```

Allowed modes are exactly `standard`, `high_protein`, `vegetarian`, `vegan`. Allowed methods are exactly `pan`, `oven`, `pot`, `one_pot`, `salad`, `soup`. Templates use `{slot.name}`, `{slot.amount}`, `{slot.cut}` and `{portions}` placeholders only. Reject missing placeholders for required slots, duplicate slot keys, unknown ingredients, fewer than three instructions, non-positive quantities, unknown units and diet violations. For nutrition, `piece` requires `grams_per_piece` and `ml` requires `density_g_per_ml` in the ingredient catalog.

`manifest.json` contains exactly `{"library_version":1}`. `RecipeCatalog.version` comes only from this file; missing, non-integer or non-positive versions fail loading.

- [ ] **Step 4: Add three inactive smoke templates**

Create `smoke.json` with IDs `chicken_rice_pan`, `tofu_vegetable_pan`, `lentil_tomato_pot`. Set `active: false`; this task validates structure without affecting production.

- [ ] **Step 5: Run tests and commit**

Run: `pytest -q tests/test_recipe_catalog.py`

Expected: PASS.

```bash
git add app/recipe_catalog.py app/catalog/recipes/manifest.json app/catalog/recipes/smoke.json tests/test_recipe_catalog.py
git commit -m "feat: validate versioned recipe templates"
```

### Task 3: Výživový výpočet a právna hranica bielkovín

**Files:**
- Create: `app/nutrition.py`
- Test: `tests/test_nutrition.py`

**Interfaces:**
- Consumes: `Ingredient`, `NutritionPer100`.
- Produces: `NutritionEstimate`, `estimate_recipe_nutrition(lines: Sequence[tuple[Ingredient, Decimal]], adult_servings: Decimal)`, `qualifies_high_protein(estimate)`. The Decimal value is edible grams after unit conversion.

- [ ] **Step 1: Write arithmetic and threshold tests**

```python
def test_nutrition_is_divided_by_real_adult_equivalents():
    estimate = estimate_recipe_nutrition(
        [(chicken, Decimal("600")), (rice, Decimal("300"))],
        adult_servings=Decimal("4"),
    )
    assert estimate.serving.protein_g == estimate.total.protein_g / 4

def test_high_protein_requires_twenty_percent_of_energy():
    serving = MacroValues(
        kcal=Decimal("370"), protein_g=Decimal("30"),
        fat_g=Decimal("10"), carbs_g=Decimal("40"),
    )
    assert qualifies_high_protein(
        NutritionEstimate(total=serving, serving=serving)
    ) is True
```

- [ ] **Step 2: Confirm RED**

Run: `pytest -q tests/test_nutrition.py`

- [ ] **Step 3: Implement Decimal-only calculations**

```python
@dataclass(frozen=True)
class MacroValues:
    kcal: Decimal
    protein_g: Decimal
    fat_g: Decimal
    carbs_g: Decimal

@dataclass(frozen=True)
class NutritionEstimate:
    total: MacroValues
    serving: MacroValues
    estimated: bool = True

def qualifies_high_protein(value: NutritionEstimate) -> bool:
    protein_kcal = value.serving.protein_g * Decimal("4")
    return value.serving.kcal > 0 and protein_kcal / value.serving.kcal >= Decimal("0.20")
```

Reject zero servings and negative edible grams. The renderer converts `piece` with `Ingredient.grams_per_piece` and `ml` with `Ingredient.density_g_per_ml` before calling this function; missing conversion data is an error. Quantize display values only at the presentation boundary; keep calculations unrounded.

- [ ] **Step 4: Run tests and commit**

Run: `pytest -q tests/test_nutrition.py`

```bash
git add app/nutrition.py tests/test_nutrition.py
git commit -m "feat: calculate recipe nutrition estimates"
```

### Task 4: Množstvá, celé balenia a zvyšky

**Files:**
- Create: `app/quantity_math.py`
- Test: `tests/test_quantity_math.py`

**Interfaces:**
- Produces: `Quantity`, `PackageSize`, `PurchaseRequirement`, `parse_quantity(text)`, `purchase_requirement(required, pantry, package)`.

- [ ] **Step 1: Write the rice and partial-pantry regressions**

```python
def test_buys_whole_rice_package_and_keeps_remainder():
    q = parse_quantity
    result = purchase_requirement(q("300 g"), q("0 g"), PackageSize(q("1 kg")))
    assert result.packages == 1
    assert result.to_buy == q("1 kg")
    assert result.used_from_purchase == q("300 g")
    assert result.leftover == q("700 g")

def test_partial_pantry_reduces_missing_amount_before_rounding_packages():
    q = parse_quantity
    result = purchase_requirement(q("700 g"), q("450 g"), PackageSize(q("500 g")))
    assert result.used_from_pantry == q("450 g")
    assert result.packages == 1
    assert result.leftover == q("250 g")
```

- [ ] **Step 2: Confirm RED**

Run: `pytest -q tests/test_quantity_math.py`

- [ ] **Step 3: Implement compatible-unit arithmetic**

```python
@dataclass(frozen=True)
class Quantity:
    amount: Decimal
    unit: Literal["g", "ml", "piece"]

@dataclass(frozen=True)
class PackageSize:
    content: Quantity

@dataclass(frozen=True)
class PantryEntry:
    ingredient_id: str
    name: str
    quantity: Quantity | None

@dataclass(frozen=True)
class PurchaseRequirement:
    required: Quantity
    used_from_pantry: Quantity
    missing: Quantity
    packages: int
    to_buy: Quantity
    used_from_purchase: Quantity
    leftover: Quantity
```

Normalize kg→g and l→ml. Use `ROUND_CEILING` only for package count. Reject incompatible units rather than guessing. Keep existing `plan_data.py` calculations unchanged until the deterministic engine consumes this module.

- [ ] **Step 4: Run tests and commit**

Run: `pytest -q tests/test_quantity_math.py`

```bash
git add app/quantity_math.py tests/test_quantity_math.py
git commit -m "feat: calculate whole-package purchases"
```

### Task 5: Additive migration for pantry quantities

**Files:**
- Modify: `app/server.py` near `SCHEMA` and database migration.
- Test: `tests/test_pantry_quantity_migration.py`

**Interfaces:**
- Produces SQLite columns `spajza.mnozstvo REAL NULL`, `spajza.jednotka TEXT NULL`.
- Existing `nazov` rows remain valid and mean “unknown quantity”.

- [ ] **Step 1: Write migration preservation tests**

```python
def test_migration_preserves_legacy_pantry_rows(connection):
    connection.execute("INSERT INTO spajza(user_id,nazov) VALUES(1,'ryža')")
    migrate(connection)
    row = connection.execute(
        "SELECT nazov,mnozstvo,jednotka FROM spajza WHERE user_id=1"
    ).fetchone()
    assert tuple(row) == ("ryža", None, None)
```

- [ ] **Step 2: Confirm RED**

Run: `pytest -q tests/test_pantry_quantity_migration.py`

- [ ] **Step 3: Add idempotent migration**

Use `PRAGMA table_info(spajza)` and add only missing columns. Add a database check at write time: amount must be positive when present; unit must be one of `g`, `ml`, `piece`; both are null or both are populated. Do not alter `/api/spajza` payload yet.

- [ ] **Step 4: Run database tests and commit**

Run: `pytest -q tests/test_pantry_quantity_migration.py tests/test_fresh_database.py tests/test_server.py -x`

```bash
git add app/server.py tests/test_pantry_quantity_migration.py
git commit -m "feat: preserve pantry quantities in schema"
```

### Task 6: Deploy completeness and foundation gate

**Files:**
- Modify: `nasad.ps1`
- Modify: `hetzner/samopull.sh`
- Modify: `tests/test_deploy_covers_all_modules.py`
- Create: `tests/test_recipe_catalog_deployment.py`

**Interfaces:**
- Deploys `app/*.py`, `app/catalog/ingredients.json`, `app/catalog/recipes/manifest.json` and active recipe JSON files atomically. Development candidate directories are never runtime assets.

- [ ] **Step 1: Write failing deploy tests**

```python
def test_manual_and_samopull_deploy_recipe_catalog():
    assert 'app\\catalog' in Path("nasad.ps1").read_text("utf-8")
    assert "app/catalog/ingredients.json" in Path("hetzner/samopull.sh").read_text("utf-8")
```

- [ ] **Step 2: Confirm RED**

Run: `pytest -q tests/test_deploy_covers_all_modules.py tests/test_recipe_catalog_deployment.py`

- [ ] **Step 3: Extend both deploy paths**

Manual deploy must copy `app/catalog/ingredients.json` and `app/catalog/recipes/` into the staged release, not live; it must not copy `app/catalog/candidates/`. Samopull required-files gate must require `app/catalog/ingredients.json`, `app/catalog/recipes/manifest.json` and at least one recipe JSON before switching. Rollback remains directory-based and therefore restores the previous catalog with the previous code.

- [ ] **Step 4: Run foundation and full regression tests**

Run: `pytest -q tests/test_ingredient_catalog.py tests/test_recipe_catalog.py tests/test_nutrition.py tests/test_quantity_math.py tests/test_pantry_quantity_migration.py tests/test_recipe_catalog_deployment.py tests/test_deploy_covers_all_modules.py`

Then: `pytest -q`

Expected: all tests pass; no production flag or endpoint behavior changed.

- [ ] **Step 5: Commit**

```bash
git add nasad.ps1 hetzner/samopull.sh tests/test_deploy_covers_all_modules.py tests/test_recipe_catalog_deployment.py
git commit -m "build: deploy recipe engine catalogs"
```
