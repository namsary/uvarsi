# Recipe Library and Pro Modes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Dodať kulinársky kontrolovanú knižnicu 60 flexibilných šablón, režimy Viac bielkovín/Vegetariánsky/Vegánsky a množstevnú špajzu v profile a mobilnom rozhraní.

**Architecture:** Recepty zostanú dátami, nie vetvami v Pythone. Knižničná kontrola bude počítať skutočné pokrytie režimov a pestrosť. Profil uloží režim ako serverom autorizovanú Pro voľbu; špajza bude prijímať štruktúrované položky, no zachová starý textový formát na čítanie existujúcich účtov.

**Tech Stack:** Python 3.12, JSON, SQLite, vanilla HTML/CSS/JS, pytest.

**Spec:** `docs/superpowers/specs/2026-08-30-vlastny-receptovy-engine-design.md`

**Prerequisites:** foundation and deterministic planner plans.

## Global Constraints

- Minimálne 60 aktívnych flexibilných šablón.
- Po filtrovaní musí zostať aspoň 50 štandardných, 24 bielkovinových, 20 vegetariánskych a 12 vegánskych šablón; prekryv je povolený.
- Každý režim musí mať aspoň tri rodiny a tri spôsoby prípravy.
- Viac bielkovín znamená najmenej 30 g bielkovín na dospelú porciu.
- „Vysoký obsah bielkovín“ sa zobrazí iba pri najmenej 20 % energie z bielkovín.
- Bezplatný účet môže uložiť iba režim `standard`; server nesmie veriť samotnému UI zámku.
- Existujúca špajza sa pri migrácii ani strate Premium nemaže.
- Platby zostávajú vypnuté.

---

## File Map

- Create `app/catalog/recipes/01-pan.json` through `06-soup-salad.json`: 60 aktívnych šablón.
- Modify `app/catalog/ingredients.json`: suroviny a synonymá potrebné pre všetky šablóny.
- Create `app/library_gate.py`: pokrytie, pestrosť, jazyk a výživové brány.
- Modify `app/server.py`: `stravovanie`, Pro autorizácia a štruktúrovaná špajza.
- Modify `app/static/app.html`: voľba režimu, množstvá v špajzi a výživový odhad.
- Test `tests/test_recipe_library_gate.py`, `tests/test_diet_profile.py`, `tests/test_pantry_quantity_api.py`, `tests/test_diet_frontend_contract.py`, `tests/test_recipe_language_snapshots.py`.

### Task 1: First 30 omnivore and high-protein templates

**Files:**
- Create: `app/catalog/recipes/01-pan.json`
- Create: `app/catalog/recipes/02-oven.json`
- Create: `app/catalog/recipes/03-one-pot.json`
- Modify: `app/catalog/ingredients.json`
- Test: `tests/test_recipe_library_gate.py`

**Interfaces:**
- Consumes the recipe schema and ingredient catalog.
- Produces 30 active templates with unique IDs and real substitution slots.

- [ ] **Step 1: Write the initial count and uniqueness gate**

```python
def test_first_library_slice_has_thirty_unique_active_templates():
    recipes = load_recipe_catalog(ingredients).all()
    first_slice = [r for r in recipes if r.id.startswith(("pan_", "oven_", "pot_"))]
    assert len(first_slice) == 30
    assert len({r.id for r in first_slice}) == 30
```

- [ ] **Step 2: Confirm RED**

Run: `pytest -q tests/test_recipe_library_gate.py`

- [ ] **Step 3: Add exactly these pan IDs**

```text
pan_chicken_rice_vegetables
pan_chicken_pasta_tomato
pan_pork_potato_onion
pan_beef_rice_pepper
pan_fish_potato_spinach
pan_turkey_couscous_zucchini
pan_egg_potato_spinach
pan_tofu_rice_broccoli
pan_chickpea_tomato_spinach
pan_cottage_pasta_zucchini
```

Each template must contain one main protein slot, one starch slot where appropriate, at least one vegetable slot, 3–7 complete Slovak steps, explicit heat/time and practical seasoning.

- [ ] **Step 4: Add exactly these oven IDs**

```text
oven_chicken_thigh_potato_carrot
oven_chicken_breast_zucchini_rice
oven_pork_shoulder_root_vegetables
oven_meatballs_tomato_potato
oven_salmon_potato_broccoli
oven_white_fish_tomato_rice
oven_tofu_vegetables_potato
oven_feta_tomato_pasta
oven_egg_vegetable_frittata
oven_lentil_vegetable_loaf
```

- [ ] **Step 5: Add exactly these one-pot IDs**

```text
pot_chicken_rice_peas
pot_chicken_paprika_pasta
pot_pork_barley_vegetables
pot_beef_tomato_pasta
pot_fish_tomato_potato
pot_turkey_lentil_tomato
pot_red_lentil_curry_rice
pot_chickpea_tomato_couscous
pot_tofu_coconut_vegetables
pot_bean_chili_rice
```

Do not create cosmetic duplicates. Candidate substitutions may vary the real offer, but family, method and result must remain culinarily valid.

- [ ] **Step 6: Run schema, language and nutrition tests**

Run: `pytest -q tests/test_recipe_catalog.py tests/test_recipe_library_gate.py tests/test_recipe_renderer.py`

- [ ] **Step 7: Commit**

```bash
git add app/catalog/ingredients.json app/catalog/recipes/01-pan.json app/catalog/recipes/02-oven.json app/catalog/recipes/03-one-pot.json tests/test_recipe_library_gate.py
git commit -m "content: add core recipe templates"
```

### Task 2: 30 vegetarian, vegan, soup and salad templates

**Files:**
- Create: `app/catalog/recipes/04-vegetarian.json`
- Create: `app/catalog/recipes/05-vegan.json`
- Create: `app/catalog/recipes/06-soup-salad.json`
- Modify: `app/catalog/ingredients.json`
- Modify: `tests/test_recipe_library_gate.py`

**Interfaces:**
- Produces the complete 60-template launch library.

- [ ] **Step 1: Extend the gate to exact launch floors**

```python
def test_launch_library_meets_mode_floors():
    coverage = library_coverage(load_recipe_catalog(ingredients))
    assert coverage.total_active >= 60
    assert coverage.eligible["standard"] >= 50
    assert coverage.eligible["high_protein"] >= 24
    assert coverage.eligible["vegetarian"] >= 20
    assert coverage.eligible["vegan"] >= 12
    assert all(value >= 3 for value in coverage.method_count.values())
    assert all(value >= 3 for value in coverage.family_count.values())
```

- [ ] **Step 2: Add exactly these vegetarian IDs**

```text
veg_egg_rice_vegetables
veg_egg_tomato_pasta
veg_cottage_potato_spinach
veg_cottage_rice_zucchini
veg_feta_couscous_vegetables
veg_cheese_broccoli_pasta
veg_mushroom_barley_pan
veg_lentil_tomato_pasta
veg_chickpea_spinach_rice
veg_bean_potato_stew
```

- [ ] **Step 3: Add exactly these vegan IDs**

```text
vegan_tofu_rice_vegetables
vegan_tofu_pasta_tomato
vegan_lentil_rice_curry
vegan_lentil_bolognese_pasta
vegan_chickpea_couscous_salad
vegan_chickpea_tomato_stew
vegan_bean_chili_rice
vegan_bean_potato_goulash
vegan_pea_potato_pan
vegan_mushroom_barley_pot
```

- [ ] **Step 4: Add exactly these soup and complete-salad IDs**

```text
soup_chicken_vegetable_noodle
soup_beef_vegetable_barley
soup_fish_tomato_potato
soup_red_lentil_tomato
soup_chickpea_vegetable
salad_chicken_potato_yogurt
salad_tuna_bean_tomato
salad_egg_pasta_vegetable
salad_tofu_rice_vegetable
salad_chickpea_couscous_vegetable
```

Salads must be complete main meals, not side salads. Soups must specify safe cooking time for meat/fish and storage instructions when a batch covers several days.

- [ ] **Step 5: Run full library gate and commit**

Run: `pytest -q tests/test_recipe_library_gate.py tests/test_recipe_language_snapshots.py tests/test_nutrition.py`

```bash
git add app/catalog/ingredients.json app/catalog/recipes/04-vegetarian.json app/catalog/recipes/05-vegan.json app/catalog/recipes/06-soup-salad.json tests/test_recipe_library_gate.py tests/test_recipe_language_snapshots.py
git commit -m "content: complete launch recipe library"
```

### Task 3: Automated release gate for library quality

**Files:**
- Create: `app/library_gate.py`
- Modify: `tests/test_recipe_library_gate.py`
- Create: `tests/test_recipe_language_snapshots.py`

**Interfaces:**
- Produces `audit_library(ingredients, recipes) -> LibraryAudit` and CLI exit code 0/1.

- [ ] **Step 1: Write fail-closed audit tests**

```python
def test_audit_rejects_duplicate_family_disguised_as_new_recipe():
    audit = audit_library(ingredients, duplicate_fingerprint_catalog)
    assert "duplicate_fingerprint" in audit.errors

def test_audit_rejects_unverified_high_protein_template():
    audit = audit_library(ingredients, under_30g_catalog)
    assert "high_protein_below_30g" in audit.errors
```

- [ ] **Step 2: Implement stable culinary fingerprint**

Fingerprint = `(family, method, sorted(required roles), sorted(primary candidate categories))`. More than two active templates with the same fingerprint require different named sauces/processes and distinct instruction snapshots; otherwise fail.

- [ ] **Step 3: Add forbidden-language assertions**

The snapshot gate rejects case-insensitive stems for known defects: `sceďok`, `reziek`, `rezky`, unresolved opening or closing template braces, decimal grams, missing serving action and generic-only steps. It also confirms every selected seasoning appears in ingredients or `Skontroluj doma`.

- [ ] **Step 4: Add CLI and tests**

Run: `python -m app.library_gate`

Expected: prints counts by mode/method/family and exits 0.

Run: `pytest -q tests/test_recipe_library_gate.py tests/test_recipe_language_snapshots.py`

- [ ] **Step 5: Commit**

```bash
git add app/library_gate.py tests/test_recipe_library_gate.py tests/test_recipe_language_snapshots.py
git commit -m "test: gate recipe library quality"
```

### Task 4: Server-authorized diet mode in profile

**Files:**
- Modify: `app/server.py` at user schema, migration, `/api/me`, `/api/profil`, signatures.
- Modify: `app/plan_data.py` at `plan_signature` and `PLAN_ALGO_VERSION`.
- Test: `tests/test_diet_profile.py`
- Modify: `tests/test_plan_cache_versioning.py`

**Interfaces:**
- Adds `pouzivatelia.stravovanie TEXT NOT NULL DEFAULT 'standard'`.
- `/api/me` returns `stravovanie` and `stravovanie_moznosti`.
- `/api/profil` accepts `standard`, `high_protein`, `vegetarian`, `vegan`.

- [ ] **Step 1: Write entitlement and cache tests**

```python
def test_free_user_cannot_persist_pro_diet_mode(client):
    response = client.post("/api/profil", json=profile(stravovanie="vegan"))
    assert response.status_code == 403
    assert response.json()["code"] == "stravovanie_premium"

def test_diet_mode_changes_plan_signature():
    assert signature(mode="standard") != signature(mode="vegan")
```

- [ ] **Step 2: Confirm RED**

Run: `pytest -q tests/test_diet_profile.py tests/test_plan_cache_versioning.py`

- [ ] **Step 3: Implement additive migration and authorization**

Allowed values are a module constant tuple. If `je_premium()` is false, only `standard` is accepted. Losing Premium does not erase the stored preference; `/api/me` reports effective `standard` plus `stravovanie_ulozene` so it can be restored later. Add effective mode and recipe library version to plan signatures.

- [ ] **Step 4: Increment algorithm version once**

Set `PLAN_ALGO_VERSION` to the next integer required by the current branch and update the exact assertion in `tests/test_plan_cache_versioning.py`. Do not increment it in multiple commits for the same release.

- [ ] **Step 5: Run tests and commit**

Run: `pytest -q tests/test_diet_profile.py tests/test_plan_cache_versioning.py tests/test_server.py`

```bash
git add app/server.py app/plan_data.py tests/test_diet_profile.py tests/test_plan_cache_versioning.py
git commit -m "feat: persist authorized diet modes"
```

### Task 5: Structured pantry quantity API

**Files:**
- Modify: `app/server.py` at `spajza_pouzivatela` and `/api/spajza`.
- Modify: `app/plan_data.py` pantry signatures only if still used by legacy flow.
- Test: `tests/test_pantry_quantity_api.py`

**Interfaces:**
- New payload: `{"polozky":[{"nazov":"ryža","mnozstvo":450,"jednotka":"g"}]}`.
- `/api/me.spajza` returns structured objects.
- Legacy database rows become `{"nazov":"ryža","mnozstvo":null,"jednotka":null}`.

- [ ] **Step 1: Write validation and preservation tests**

```python
def test_pantry_accepts_quantified_rice(premium_client):
    response = premium_client.post("/api/spajza", json={"polozky":[
        {"nazov":"ryža", "mnozstvo":450, "jednotka":"g"}
    ]})
    assert response.status_code == 200
    assert premium_client.get("/api/me").json()["spajza"][0]["mnozstvo"] == 450

@pytest.mark.parametrize("amount", [0, -1, float("nan")])
def test_pantry_rejects_invalid_amount(premium_client, amount):
    response = premium_client.post("/api/spajza", json={"polozky":[
        {"nazov":"ryža", "mnozstvo":amount, "jednotka":"g"}
    ]})
    assert response.status_code == 422
    assert premium_client.get("/api/me").json()["spajza"] == []
```

- [ ] **Step 2: Confirm RED**

Run: `pytest -q tests/test_pantry_quantity_api.py`

- [ ] **Step 3: Implement one normalization boundary**

Normalize units to `g`, `ml`, `piece`; cap name at 80 characters, entries at 60 and finite amount at a practical maximum. Write all rows in one transaction. On invalid input write nothing. Keep Premium gate and never delete rows merely because entitlement changed.

- [ ] **Step 4: Run tests and commit**

Run: `pytest -q tests/test_pantry_quantity_api.py tests/test_spajza_oddelena_od_planu.py tests/test_spajza_nakupny_zoznam.py`

```bash
git add app/server.py app/plan_data.py tests/test_pantry_quantity_api.py
git commit -m "feat: store quantified pantry items"
```

### Task 6: Mobile profile, pantry and nutrition UI

**Files:**
- Modify: `app/static/app.html`
- Test: `tests/test_diet_frontend_contract.py`
- Modify: `tests/test_spajza_frontend_contract.py`

**Interfaces:**
- Profile sends `stravovanie`.
- Pantry sends structured items.
- Recipe card displays estimated protein per adult serving when present.

- [ ] **Step 1: Write frontend contract tests**

Assert exact labels `Bez obmedzenia`, `Viac bielkovín`, `Vegetariánsky`, `Vegánsky`; Premium modes render locked for free users; payload includes the selected mode; pantry input includes numeric amount and unit; UI never prints `vysoký obsah bielkovín` unless `high_protein_claim === true`.

- [ ] **Step 2: Confirm RED**

Run: `pytest -q tests/test_diet_frontend_contract.py tests/test_spajza_frontend_contract.py`

- [ ] **Step 3: Implement accessible mode chips**

Use real `<button type="button">` controls with `aria-pressed`, explanatory copy and server error handling. Free users can see the value proposition but cannot silently persist a Pro mode. Existing profile remains usable without JavaScript migration.

- [ ] **Step 4: Implement pantry rows with amount/unit**

Each row contains ingredient name, positive numeric input and `g/ml/ks` selector. Legacy unknown quantities display `Doplň množstvo`; they are not assumed to cover a purchase.

Shopping rows with a calculated remainder show `Pridať zvyšok do špajze`. Rendering the row performs no write. Only an explicit click, followed by server confirmation, merges that quantity into the matching pantry item. Add a contract test that rendering and checking off a purchase make no `/api/spajza` request, while the explicit button makes exactly one.

- [ ] **Step 5: Display nutrition and allergens honestly**

Recipe card copy: `Odhad: 34 g bielkovín na dospelú porciu`. Add `Vysoký obsah bielkovín` badge only from the server boolean. Show catalog allergens on the expanded recipe and the warning `Pri alergii skontroluj zloženie konkrétneho výrobku na obale.` Include the existing non-medical disclaimer in Profile.

- [ ] **Step 6: Test mobile contracts and commit**

Run: `pytest -q tests/test_diet_frontend_contract.py tests/test_spajza_frontend_contract.py tests/test_app_html_contract.py tests/test_frontend_speed_contract.py`

```bash
git add app/static/app.html tests/test_diet_frontend_contract.py tests/test_spajza_frontend_contract.py
git commit -m "feat: add diet and pantry quantity controls"
```

### Task 7: Offline candidate quarantine workflow

**Files:**
- Create: `app/recipe_candidates.py`
- Create: `app/catalog/candidates/.gitkeep`
- Create: `tests/test_recipe_candidate_workflow.py`

**Interfaces:**
- Produces `validate_candidate(path, ingredients) -> CandidateReport` and `promote_candidate(path, reviewed_by, reviewed_on) -> Path`.
- Candidate files are never loaded by `load_recipe_catalog()` and are excluded from production deploy.

- [ ] **Step 1: Write quarantine tests**

```python
def test_candidate_is_not_visible_to_runtime_catalog(candidate_root):
    write_valid_candidate(candidate_root / "draft.json")
    assert "draft_recipe" not in {r.id for r in load_recipe_catalog(ingredients).all()}

def test_promotion_requires_human_review_metadata(valid_candidate):
    with pytest.raises(ValueError, match="reviewed_by"):
        promote_candidate(valid_candidate, reviewed_by="", reviewed_on=date.today())
```

- [ ] **Step 2: Confirm RED**

Run: `pytest -q tests/test_recipe_candidate_workflow.py`

- [ ] **Step 3: Implement validation without automatic publishing**

Validation runs schema, ingredient, nutrition, dietary, language, package and duplicate-fingerprint checks and returns all failures. Promotion copies a passing candidate into the appropriate recipe JSON only after non-empty reviewer metadata, increments `manifest.json.library_version`, reruns `python -m app.library_gate` and leaves the original candidate as an audit record. The command never calls an LLM itself; an externally generated draft is only input data.

- [ ] **Step 4: Exclude candidates from deployment**

Add deploy contract assertions that `app/catalog/candidates` is not copied to the staged release, while active recipes and manifest are required.

- [ ] **Step 5: Run tests and commit**

Run: `pytest -q tests/test_recipe_candidate_workflow.py tests/test_recipe_catalog_deployment.py tests/test_recipe_library_gate.py`

```bash
git add app/recipe_candidates.py app/catalog/candidates/.gitkeep tests/test_recipe_candidate_workflow.py tests/test_recipe_catalog_deployment.py
git commit -m "feat: quarantine recipe candidates before review"
```

### Task 8: Complete content and entitlement matrix gate

**Files:**
- Create: `tests/test_recipe_mode_matrix.py`
- Modify: `app/library_gate.py`

**Interfaces:**
- Validates 60 templates against all household/frequency/mode combinations.

- [ ] **Step 1: Add matrix tests**

For each mode, run fixed real-offer fixtures with `(adults, children)` of `(1,0)`, `(2,2)`, `(4,0)` and frequencies 1/2/3. Assert complete seven-day plan, correct entitlement, no dietary violation and high-protein target where selected.

- [ ] **Step 2: Add a no-live-model assertion**

Monkeypatch Anthropic/OpenAI constructors to raise immediately and call the deterministic engine for every matrix row. Every build must still pass.

- [ ] **Step 3: Run complete content gate**

Run: `python -m app.library_gate`

Run: `pytest -q tests/test_recipe_mode_matrix.py tests/test_recipe_library_gate.py tests/test_recipe_language_snapshots.py`

- [ ] **Step 4: Run full suite and commit**

Run: `pytest -q`

```bash
git add app/library_gate.py tests/test_recipe_mode_matrix.py
git commit -m "test: verify recipe modes across profiles"
```
