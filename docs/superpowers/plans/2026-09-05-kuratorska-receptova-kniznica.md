# Curated Recipe Library Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace generic recipe combinations with 104 source-backed, kitchen-valid recipe archetypes that Uvar.si can adapt to current offers without a live model call.

**Architecture:** Research records remain separate from runtime recipe data. Version-2 recipes carry explicit workflow state for ingredients and equipment; the release gate validates this state before activation. New recipes enter quarantine as one-recipe candidate files, then one atomic batch promotion publishes the curated generation and retires the generic generation.

**Tech Stack:** Python 3.12, standard-library JSON/dataclasses/URL parsing, pytest, existing deterministic matcher/renderer, systemd and the existing Hetzner samopull release path.

**Spec:** `docs/superpowers/specs/2026-09-05-kuratorska-receptova-kniznica-design.md`

## Global Constraints

- Production contains 100–120 active, materially distinct curated archetypes; this plan targets 104.
- User plan creation performs no Anthropic, OpenAI, or other generative-model call.
- Every active curated recipe has at least one source reference and original Slovak wording; core Slovak recipes have at least two independent references.
- Research starts from at least 160 multi-source candidates; exactly 104 are selected for curated generation 1.
- Do not copy source prose, photographs, videos, or a substantial part of one recipe database.
- Recipe steps use natural modern Slovak and repeat quantities only when a later split depends on the amount.
- Allergens and nutrition are calculated from the selected canonical ingredients; each version-2 recipe carries its own refrigerated-storage rule.
- Dry/canned legumes and bone-in/boneless meat remain distinct.
- The active library contains at least 24 high-protein, 24 vegetarian, and 16 vegan recipes, including cross-tagged recipes outside the primary editorial lane.
- Plan creation p95 remains below 500 ms on the existing Hetzner server.
- Payments remain off throughout this work.
- Do not change Caddy or the co-hosted `taktik-mapa` service.
- Every behavior change follows RED → GREEN → REFACTOR and receives an independent review before integration.

## File Map

- Create `app/recipe_provenance.py`: strict loader and validator for source records.
- Create `app/recipe_workflow.py`: deterministic ingredient/equipment state validation.
- Create `app/catalog/recipe_sources.json`: production provenance keyed by recipe ID.
- Create `docs/research/recipe-candidates.json`: at least 160 researched candidate archetypes before selection.
- Create `docs/research/recipe-targets.json`: the fixed 104-recipe editorial inventory.
- Create `app/catalog/recipes/10-classic-meat.json`: 22 curated classics with meat.
- Create `app/catalog/recipes/11-classic-meatless.json`: 20 meatless classics and soups.
- Create `app/catalog/recipes/12-modern-family.json`: 20 modern family meals.
- Create `app/catalog/recipes/13-modern-quick.json`: 16 quick modern meals.
- Create `app/catalog/recipes/14-high-protein.json`: 16 high-protein meals.
- Create `app/catalog/recipes/15-plant-based.json`: 10 plant-based meals.
- Modify `app/recipe_catalog.py`: version-2 instruction workflow fields and manifest generation.
- Modify `app/recipe_renderer.py`: recipe-specific storage output while keeping calculated nutrition/allergens.
- Modify `app/recipe_candidates.py`: source-aware validation and atomic batch promotion.
- Modify `app/library_gate.py`: provenance, workflow, diversity, and generation gates.
- Modify `app/recipe_matcher.py`: curated-generation preference during shadow comparison.
- Modify `app/catalog/recipes/manifest.json`: activate curated generation only at final cutover.
- Modify the six legacy recipe files: retire generic recipes only after the curated batch passes.
- Modify `hetzner/recipe-engine-rollout.sh`: add curated generation and provenance checks.
- Create `tests/test_recipe_provenance.py`.
- Create `tests/test_recipe_workflow.py`.
- Modify `tests/test_recipe_candidate_workflow.py`.
- Modify `tests/test_recipe_library_gate.py`.
- Modify `tests/test_recipe_matcher.py`.
- Create `tests/test_curated_recipe_inventory.py`.
- Create `tests/test_curated_recipe_snapshots.py`.

---

### Task 1: Stabilize and release the repaired 60-recipe base

**Files:**
- Modify only the currently changed Uvar.si application, catalog, test, `VERSION`, and rollout files already present in the worktree.
- Test: existing `tests/` suite.

**Interfaces:**
- Consumes: current repaired deterministic engine and agent-reviewed bug fixes.
- Produces: a green production baseline with recipe engine enabled and payments disabled.

- [ ] **Step 1: Record the exact intended change set**

Run:

```powershell
git status --short --untracked-files=no
git diff --name-only
git diff --check
```

Expected: only Uvar.si files listed in the prior repair scope; no Caddy or `taktik-mapa` file.

- [ ] **Step 2: Run focused regressions**

Run:

```powershell
python -m pytest -q tests/test_recipe_process_flow.py tests/test_catalog_product_consistency.py tests/test_ingredient_catalog.py tests/test_offer_matcher.py tests/test_recipe_renderer.py tests/test_diet_profile.py -p no:cacheprovider
```

Expected: all tests pass.

- [ ] **Step 3: Run the complete suite from a clean start**

Run:

```powershell
python -m pytest -q -p no:cacheprovider
```

Expected: all tests pass; no collection error or interrupted run.

- [ ] **Step 4: Commit the repaired base**

Stage the exact paths shown by Step 1, excluding this plan and unrelated untracked files. Commit:

```powershell
git commit -m "fix: stabilize deterministic meal plans"
```

- [ ] **Step 5: Deploy and verify the base**

Push fast-forward to `origin/main`, run `/opt/uvarsi/samopull.sh`, then verify `/api/health`, `/app`, one real plan, worker health, `payments=off`, and all three services. Expected: Uvar.si and `taktik-mapa` stay active; a plan loads without a model call.

### Task 2: Add strict recipe provenance

**Files:**
- Create: `app/recipe_provenance.py`
- Create: `app/catalog/recipe_sources.json`
- Test: `tests/test_recipe_provenance.py`

**Interfaces:**
- Produces: `SourceReference`, `RecipeProvenance`, and `load_recipe_provenance(active_ids, path=None) -> Mapping[str, RecipeProvenance]`.
- Consumes: active recipe IDs from `RecipeCatalog.all()`.

- [ ] **Step 1: Write failing provenance tests**

```python
def test_provenance_requires_every_active_recipe(tmp_path):
    path = write_sources(tmp_path, records=[source("one")])
    with pytest.raises(ValueError, match="missing provenance: two"):
        load_recipe_provenance({"one", "two"}, path)


def test_core_recipe_requires_two_independent_hosts(tmp_path):
    record = source("classic_chicken_paprikash", lane="slovak_classic",
                    core=True, urls=["https://example.sk/a", "https://example.sk/b"])
    with pytest.raises(ValueError, match="independent source"):
        load_recipe_provenance({record["recipe_id"]}, write_sources(tmp_path, [record]))


def test_provenance_rejects_non_https_and_duplicate_urls(tmp_path):
    record = source("one", urls=["http://example.sk/a", "http://example.sk/a"])
    with pytest.raises(ValueError):
        load_recipe_provenance({"one"}, write_sources(tmp_path, [record]))
```

- [ ] **Step 2: Verify RED**

Run `python -m pytest -q tests/test_recipe_provenance.py -p no:cacheprovider`.

Expected: import failure because `app.recipe_provenance` does not exist.

- [ ] **Step 3: Implement the strict data types and loader**

```python
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


def load_recipe_provenance(active_ids, path=None) -> Mapping[str, RecipeProvenance]:
    """Load exact JSON, reject missing/extra IDs, unsafe URLs, and weak core sourcing."""
```

Use `urllib.parse.urlsplit`; require `https`, a non-empty host, unique URLs, and two distinct hosts when `core` is true. Accept these lanes only: `slovak_classic`, `modern_family`, `high_protein`, `plant_based`.

- [ ] **Step 4: Add an empty production provenance file without activating the gate**

```json
{
  "schema_version": 1,
  "recipes": []
}
```

The library gate will begin enforcing this file only when `curation_generation` becomes `1` in Task 11.

- [ ] **Step 5: Verify GREEN and commit**

Run the focused test, then:

```powershell
git add app/recipe_provenance.py app/catalog/recipe_sources.json tests/test_recipe_provenance.py
git commit -m "feat: validate recipe source provenance"
```

### Task 3: Add explicit workflow state to version-2 recipes

**Files:**
- Create: `app/recipe_workflow.py`
- Modify: `app/recipe_catalog.py`
- Modify: `app/recipe_renderer.py`
- Modify: `app/library_gate.py`
- Test: `tests/test_recipe_workflow.py`
- Test: `tests/test_recipe_catalog.py`
- Test: `tests/test_recipe_renderer.py`

**Interfaces:**
- Produces: `InstructionTemplate(text, requires=(), produces=())`, `StorageRule(refrigerated_days, instruction)`, and `workflow_errors(recipe) -> tuple[str, ...]`.
- Consumes: slot keys and equipment names from `RecipeTemplate`.

- [ ] **Step 1: Write failing state-machine tests**

```python
def test_workflow_rejects_cutting_bone_in_thigh():
    recipe = v2_recipe(slot=("protein", "chicken_thigh"),
                       transitions=[step("protein:raw", "protein:cut")])
    assert "incompatible_ingredient_transition" in workflow_errors(recipe)


def test_workflow_rejects_prepared_ingredient_never_served():
    recipe = v2_recipe(transitions=[step("protein:raw", "protein:prepared")])
    assert "workflow_unserved_ingredient:protein" in workflow_errors(recipe)


def test_workflow_rejects_second_use_of_occupied_pot():
    recipe = v2_recipe(transitions=[
        step("pot:free", "pot:occupied"),
        step("pot:free", "starch:cooked"),
    ])
    assert "workflow_unmet_requirement:pot:free" in workflow_errors(recipe)


def test_v2_recipe_requires_recipe_specific_storage_rule():
    payload = v2_recipe_payload()
    payload.pop("storage")
    with pytest.raises(ValueError, match="storage"):
        load_single_recipe(payload)


def test_renderer_uses_recipe_specific_storage_rule():
    meal = render(v2_recipe(storage={
        "refrigerated_days": 2,
        "instruction": "Po vychladnutí odlož do chladničky a zjedz do 2 dní.",
    }))
    assert meal.storage == "Po vychladnutí odlož do chladničky a zjedz do 2 dní."
```

- [ ] **Step 2: Verify RED**

Run `python -m pytest -q tests/test_recipe_workflow.py -p no:cacheprovider`.

Expected: import failure because `app.recipe_workflow` does not exist.

- [ ] **Step 3: Extend version-2 instruction parsing**

```python
@dataclass(frozen=True)
class InstructionTemplate:
    text: str
    requires: tuple[str, ...] = ()
    produces: tuple[str, ...] = ()


@dataclass(frozen=True)
class StorageRule:
    refrigerated_days: int
    instruction: str
```

Add `storage: StorageRule | None` to `RecipeTemplate`. Version-1 recipes continue to accept `{"text": "..."}` and have `storage=None`. Version-2 recipes require exact instruction keys `text`, `requires`, and `produces`, plus an exact top-level `storage` object with `refrigerated_days` from 1 through 4 and a non-empty original Slovak `instruction`. Tokens use `resource:state`; valid initial resources are each slot at `raw` and each equipment item at `free`.

- [ ] **Step 4: Implement deterministic workflow validation**

```python
def workflow_errors(recipe: RecipeTemplate) -> tuple[str, ...]:
    state = {slot.key: "raw" for slot in recipe.slots}
    state.update({_equipment_key(item): "free" for item in recipe.equipment})
    errors: set[str] = set()
    for instruction in recipe.instructions:
        for token in instruction.requires:
            resource, expected = _token(token)
            if state.get(resource) != expected:
                errors.add(f"workflow_unmet_requirement:{token}")
        for token in instruction.produces:
            resource, produced = _token(token)
            state[resource] = produced
    for slot in recipe.slots:
        if slot.required and state.get(slot.key) != "served":
            errors.add(f"workflow_unserved_ingredient:{slot.key}")
    return tuple(sorted(errors))
```

Add the explicit rule that `chicken_thigh` cannot produce `cut`, `diced`, or `cubed`; `chicken_thigh_meat` can.

- [ ] **Step 5: Integrate the workflow into the library gate**

For every active recipe with `version >= 2`, merge `workflow_errors(recipe)` into `LibraryAudit.errors`. When curated generation is active, reject any active version-1 recipe with `legacy_recipe_active`.

- [ ] **Step 6: Render storage while preserving calculated food facts**

Use `recipe.storage.instruction` for version-2 meals that cover more than one day. Keep allergens and nutrition calculated from the actual selected ingredient records; do not duplicate them as manually maintained recipe metadata. Reject a plan when `covered_days > recipe.storage.refrigerated_days`.

- [ ] **Step 7: Verify GREEN and commit**

Run:

```powershell
python -m pytest -q tests/test_recipe_workflow.py tests/test_recipe_catalog.py tests/test_recipe_renderer.py tests/test_recipe_library_gate.py -p no:cacheprovider
git add app/recipe_workflow.py app/recipe_catalog.py app/recipe_renderer.py app/library_gate.py tests/test_recipe_workflow.py tests/test_recipe_catalog.py tests/test_recipe_renderer.py tests/test_recipe_library_gate.py
git commit -m "feat: validate recipe preparation workflows"
```

### Task 4: Make candidate promotion source-aware and atomic in batches

**Files:**
- Modify: `app/recipe_candidates.py`
- Modify: `app/catalog/recipes/manifest.json`
- Test: `tests/test_recipe_candidate_workflow.py`

**Interfaces:**
- Produces: `promote_candidates(paths, reviewed_by, reviewed_on) -> tuple[Path, ...]`.
- Consumes: one-recipe candidate payloads containing exact keys `recipes` and `source_record`.

- [ ] **Step 1: Write failing batch-promotion tests**

```python
def test_batch_promotion_publishes_recipes_sources_and_one_version(tmp_catalog):
    first = write_candidate("first", source_record("first"))
    second = write_candidate("second", source_record("second"))
    before = manifest_version(tmp_catalog)
    paths = promote_candidates([first, second], "Martin", date(2026, 9, 5))
    assert len(paths) == 2
    assert manifest_version(tmp_catalog) == before + 1
    assert source_ids(tmp_catalog) >= {"first", "second"}


def test_batch_promotion_rolls_back_every_file_when_final_audit_fails(tmp_catalog):
    before = snapshot_catalog(tmp_catalog)
    with pytest.raises(ValueError):
        promote_candidates([valid_candidate(), invalid_candidate()], "Martin", date(2026, 9, 5))
    assert snapshot_catalog(tmp_catalog) == before
```

- [ ] **Step 2: Verify RED**

Run the two tests. Expected: `promote_candidates` is missing and candidate schema accepts no source record.

- [ ] **Step 3: Parse source records during candidate validation**

Change the top-level exact schema to:

```json
{
  "recipes": [{"id": "candidate_id"}],
  "source_record": {
    "recipe_id": "candidate_id",
    "editorial_lane": "modern_family",
    "core": false,
    "references": [
      {"url": "https://example.sk/recept", "title": "Názov", "accessed_on": "2026-09-05"}
    ]
  }
}
```

Validate that both IDs match. Use the same provenance parser as Task 2.

- [ ] **Step 4: Implement one-lock batch promotion**

Within `_promotion_lock()`, validate every candidate first, reject duplicate IDs, stage all destination recipe files plus `recipe_sources.json`, run the exact staged library gate, then publish under one odd/even manifest transition. Preserve `curation_generation` when rebuilding the manifest. On any exception restore every changed file byte-for-byte and remove new review records.

Keep `promote_candidate()` as a wrapper:

```python
def promote_candidate(path, reviewed_by, reviewed_on) -> Path:
    return promote_candidates((path,), reviewed_by, reviewed_on)[0]
```

- [ ] **Step 5: Verify GREEN and commit**

Run the complete candidate workflow suite and commit the exact files.

### Task 5: Research 160 candidates and freeze the 104-recipe editorial inventory

**Files:**
- Create: `docs/research/recipe-candidates.json`
- Create: `docs/research/recipe-targets.json`
- Test: `tests/test_curated_recipe_inventory.py`

**Interfaces:**
- Produces: a research pool of at least 160 unique archetypes and fixed target records with `id`, `display_name`, `editorial_lane`, `expected_modes`, `core`, and `references`.
- Consumes: public recipe pages used only for factual research.

- [ ] **Step 1: Write the failing inventory contract**

```python
def test_target_inventory_has_fixed_size_mix_and_unique_ids():
    candidates = load_candidates()
    rows = load_targets()
    assert len(candidates) >= 160
    assert len({row["id"] for row in candidates}) == len(candidates)
    assert len(rows) == 104
    assert len({row["id"] for row in rows}) == 104
    assert {row["id"] for row in rows} <= {row["id"] for row in candidates}
    assert Counter(row["editorial_lane"] for row in rows) == {
        "slovak_classic": 42,
        "modern_family": 36,
        "high_protein": 16,
        "plant_based": 10,
    }
```

Add assertions that every candidate has an HTTPS reference, every selected target records `selection_reason`, and every selected `core: true` row has two distinct source hosts.

- [ ] **Step 2: Verify RED**

Expected: both research files are missing.

- [ ] **Step 3: Build the research pool before choosing the release set**

Create `recipe-candidates.json` with exact top-level keys `schema_version` and `candidates`. Each candidate has exact keys `id`, `display_name`, `editorial_lane`, `expected_modes`, `core`, `references`, `familiarity_evidence`, and `selection_notes`. Research at least 160 materially different dishes across Varecha, Naničmama, Tesco, Kuchyňa Lidla, Aktin, Cvičte, BBC Good Food, and additional editorial sources where needed. Use discussion sites only for familiarity evidence. Store factual summaries and URLs, never copied instructions or images.

- [ ] **Step 4: Score and select the fixed release set**

Score each candidate from 0 through 5 for Slovak familiarity, flyer-ingredient availability, family practicality, safe substitutability, diet contribution, and distinctiveness. Put the selected 104 into `recipe-targets.json`; each selected row includes `selection_reason` explaining why it survived. Reject near-duplicates that differ only by a seasoning or one interchangeable vegetable.

- [ ] **Step 5: Add these exact IDs to the selected inventory**

`slovak_classic` (42):

```text
classic_chicken_paprikash, classic_chicken_perkelt, classic_slovak_chicken_risotto,
classic_roast_chicken_thighs_potatoes, classic_chicken_noodle_soup,
classic_chicken_sote_rice, classic_pork_natural_rice, classic_pork_shoulder_onion,
classic_segedin_goulash, classic_pork_perkelt, classic_french_potatoes,
classic_meatballs_mash, classic_stuffed_peppers, classic_beef_goulash,
classic_tomato_meatballs, classic_bolognese_spaghetti, classic_baked_pasta_ham,
classic_pork_cabbage_bake, classic_meatloaf_potatoes, classic_roast_pork_root_veg,
classic_beef_barley_soup, classic_goulash_soup, classic_potato_stew_egg,
classic_lentil_stew_egg, classic_bean_stew_egg, classic_pumpkin_stew_egg,
classic_pea_stew_egg, classic_lecho_egg, classic_granadir, classic_cabbage_noodles,
classic_bryndza_dumplings, classic_cabbage_dumplings, classic_potato_pancakes,
classic_sauerkraut_soup, classic_bean_soup, classic_sour_lentil_soup,
classic_potato_soup, classic_garlic_soup, classic_tomato_soup,
classic_cauliflower_soup, classic_rice_pudding, classic_apple_bread_pudding
```

`modern_family` (36):

```text
modern_chicken_curry_rice, modern_chicken_mushroom_pasta,
modern_one_pot_chicken_rice, modern_teriyaki_chicken_broccoli,
modern_chicken_fajita_tortilla, modern_chicken_caesar_salad,
modern_baked_chicken_zucchini, modern_pork_noodle_stir_fry,
modern_meatballs_tomato_pasta, modern_chili_con_carne,
modern_cottage_shepherd_pie, modern_family_lasagne, modern_tuna_tomato_pasta,
modern_tuna_pasta_salad, modern_salmon_potato_broccoli,
modern_white_fish_tomato_rice, modern_fish_tacos_yogurt,
modern_feta_tomato_pasta, modern_mushroom_risotto, modern_pumpkin_risotto,
quick_broccoli_cheese_pasta, quick_pesto_chicken_pasta, quick_gnocchi_spinach,
quick_couscous_grilled_cheese, quick_egg_fried_rice, quick_shakshuka,
quick_vegetable_frittata, quick_tuna_cheese_tortilla,
quick_baked_potato_cottage, quick_zucchini_fritters, quick_cauliflower_curry,
quick_orzo_chicken_one_pan, quick_tomato_mozzarella_pasta,
quick_pea_ham_risotto, quick_egg_potato_spinach_pan,
quick_chickpea_tomato_couscous
```

`high_protein` (16):

```text
protein_turkey_couscous, protein_chicken_bulgur, protein_chicken_yogurt_potato,
protein_beef_rice_bowl, protein_cottage_tomato_pasta,
protein_cottage_potato_spinach, protein_tuna_bean_salad,
protein_egg_cottage_salad, protein_salmon_couscous_salad,
protein_chicken_lentil_stew, protein_turkey_meatballs,
protein_tofu_broccoli_rice, protein_cottage_lentil_lasagne,
protein_beef_bean_chili, protein_chicken_yogurt_traybake,
protein_skyr_chicken_wrap
```

`plant_based` (10):

```text
plant_red_lentil_dal, plant_chickpea_curry, plant_bean_chili,
plant_lentil_bolognese, plant_tofu_coconut_curry, plant_tofu_tomato_pasta,
plant_chickpea_couscous_salad, plant_bean_potato_goulash,
plant_mushroom_barley, plant_lentil_loaf
```

For each ID record the familiar Slovak display name, required mode tags, and real source URLs. Do not store copied directions or source photos.

- [ ] **Step 6: Verify GREEN and commit**

Run the inventory test and commit both research files plus its test.

### Task 6: Author the 42 Slovak classics as quarantined version-2 candidates

**Files:**
- Create: 42 exact `app/catalog/candidates/<recipe-id>.json` files for the `slovak_classic` IDs.
- Create: `tests/test_curated_recipe_snapshots.py`

**Interfaces:**
- Consumes: Task 5 target records and Task 3 version-2 workflow schema.
- Produces: 42 individually valid candidate recipes; no runtime activation.

- [ ] **Step 1: Write failing classic snapshot tests**

```python
@pytest.mark.parametrize("recipe_id", CLASSIC_IDS)
def test_classic_candidate_is_source_backed_and_workflow_valid(recipe_id):
    report = validate_candidate(candidate_path(recipe_id), load_ingredient_catalog())
    assert report.errors == (), (recipe_id, report.errors)


def test_bone_in_roast_thigh_is_never_cut_into_cubes():
    recipe = candidate("classic_roast_chicken_thighs_potatoes")
    assert "chicken_thigh" in slot(recipe, "protein")["candidates"]
    assert all("kock" not in step["text"].casefold() for step in recipe["instructions"])
```

Add golden assertions for paprikáš, slovenské rizoto, segedín, francúzske zemiaky, prívarky, and soups: characteristic ingredients must appear, and unrelated curry/oregano seasoning must not.

- [ ] **Step 2: Verify RED**

Expected: all 42 candidate files are missing.

- [ ] **Step 3: Write the 22 meat classics**

Use original Slovak wording, exact product states, natural quantities, recipe-specific seasonings, and explicit workflow tokens. Restrict substitutions to the same culinary cut. Render each recipe for one adult/one day and four adults/three days before moving to the next candidate.

- [ ] **Step 4: Write the 20 meatless classics and soups**

Model eggs and dairy explicitly where used. Mark only genuinely animal-free variants vegan. Dry legumes require soaking/cooking workflow; canned legumes require draining and no soaking.

- [ ] **Step 5: Verify all variants and commit**

Run:

```powershell
python -m pytest -q tests/test_curated_recipe_snapshots.py tests/test_recipe_candidate_workflow.py tests/test_recipe_language_snapshots.py -p no:cacheprovider
```

Commit all 42 exact candidate paths and the snapshot tests with `feat: curate Slovak recipe classics`.

### Task 7: Author the 36 modern family candidates

**Files:**
- Create: 36 exact candidate files for the `modern_family` IDs.
- Modify: `tests/test_curated_recipe_snapshots.py`

**Interfaces:**
- Consumes: source inventory, ingredient catalog, workflow schema.
- Produces: 36 individually valid modern candidates; no runtime activation.

- [ ] **Step 1: Add failing modern-family contracts**

Assert all 36 reports pass, every recipe uses no more than three vessels, quick-prefixed recipes take at most 35 minutes, and each recipe contains a recognizable completion cue.

- [ ] **Step 2: Verify RED**

Expected: 36 missing candidates.

- [ ] **Step 3: Author 20 family meals**

Cover curry, pasta, one-pot rice, tortillas, meatballs, chili, fish, and risotto. Preserve defining flavour profiles; do not allow a universal seasoning set.

- [ ] **Step 4: Author 16 quick meals**

Use ordinary supermarket ingredients and short practical methods. A quick recipe must not hide overnight soaking, long marinating, or multi-hour cooking.

- [ ] **Step 5: Verify and commit**

Run the snapshot, candidate, renderer, and language suites. Commit with `feat: curate modern family recipes`.

### Task 8: Author the 16 high-protein candidates

**Files:**
- Create: 16 exact candidate files for the `high_protein` IDs.
- Modify: `tests/test_curated_recipe_snapshots.py`

**Interfaces:**
- Produces: 16 candidates whose rendered adult portion has at least 30 g protein.

- [ ] **Step 1: Add failing nutritional contracts**

```python
@pytest.mark.parametrize("recipe_id", HIGH_PROTEIN_IDS)
def test_high_protein_candidate_reaches_30g_per_adult(recipe_id):
    meal = render_candidate(recipe_id, adults=1, children=0, covered_days=1)
    assert meal.nutrition.serving.protein_g >= Decimal("30")
```

Also assert that no child factor is raised merely to satisfy the adult target.

- [ ] **Step 2: Verify RED**

Expected: candidate files are missing.

- [ ] **Step 3: Author and render all 16 candidates**

Use meat, fish, eggs, cottage cheese, skyr, tofu, beans, and lentils across the set. Keep adult energy and portion size plausible; use the neutral product label „Viac bielkovín“ unless the legal 20%-of-energy threshold also passes.

- [ ] **Step 4: Verify and commit**

Run nutrition, candidate, renderer, and language suites. Commit with `feat: curate high protein recipes`.

### Task 9: Author the 10 plant-based candidates and verify cross-lane diets

**Files:**
- Create: 10 exact candidate files for the `plant_based` IDs.
- Modify: `tests/test_curated_recipe_snapshots.py`

**Interfaces:**
- Produces: 10 plant-based candidates plus enough vegan/vegetarian cross-tags to meet 16/24 after promotion.

- [ ] **Step 1: Add failing diet coverage tests**

```python
def test_curated_candidates_meet_diet_floors():
    recipes = all_curated_candidate_templates()
    assert sum("vegetarian" in item.modes for item in recipes) >= 24
    assert sum("vegan" in item.modes for item in recipes) >= 16
```

- [ ] **Step 2: Verify RED**

Expected: plant-based files are missing and vegan coverage is below 16.

- [ ] **Step 3: Author the 10 candidates**

Use explicit dry/canned states and suitable hydration. Every main meal combines a meaningful protein source with starch or sufficient vegetables; no recipe is merely a side dish relabelled as dinner.

- [ ] **Step 4: Tag eligible classics and modern meals**

Apply vegan or vegetarian modes only where every candidate ingredient and pantry basic passes the existing ingredient-level diet validator. Do not create a separate weaker diet check.

- [ ] **Step 5: Verify and commit**

Run all curated snapshots, diet tests, candidate workflow, and full library gate against the quarantined set. Commit with `feat: curate plant based recipes`.

### Task 10: Prefer curated recipes and prove menu diversity

**Files:**
- Modify: `app/recipe_matcher.py`
- Modify: `tests/test_recipe_matcher.py`
- Modify: `tests/test_deterministic_plan_invariants.py`

**Interfaces:**
- Consumes: recipe IDs and provenance lane map.
- Produces: deterministic tie-breaking that favours curated recipes only during mixed-generation shadow runs.

- [ ] **Step 1: Write failing mixed-generation tests**

```python
def test_curated_candidate_wins_equal_score_against_legacy_candidate():
    ranked = rank_candidates([legacy_candidate(score="80"), curated_candidate(score="80")],
                             curated_ids={"curated"})
    assert ranked[0].template.id == "curated"
```

Add a seven-day invariant requiring at least three families, three methods, and no repeated primary protein on consecutive cooking days when enough candidates exist.

- [ ] **Step 2: Verify RED**

Expected: equal-score ordering ignores curated status.

- [ ] **Step 3: Add an explicit curated tie-break**

Pass `curated_ids: frozenset[str] = frozenset()` into ranking and place the curated bit after real saving/coverage score but before stable hash ordering. Never let curated status make an incompatible or more expensive recipe eligible.

- [ ] **Step 4: Verify and commit**

Run matcher and deterministic invariant suites. Commit with `feat: prefer curated recipe archetypes`.

### Task 11: Atomically activate curated generation 1

**Files:**
- Modify: `app/catalog/recipes/manifest.json`
- Modify: six legacy recipe JSON files.
- Create: six curated destination files through `promote_candidates`.
- Populate: `app/catalog/recipe_sources.json` through promotion.
- Modify: `app/library_gate.py`
- Modify: `tests/test_recipe_library_gate.py`

**Interfaces:**
- Produces: exactly 104 active version-2 curated recipes and no active version-1 generic recipe.

- [ ] **Step 1: Add failing generation-1 release gates**

```python
def test_curated_generation_one_has_exact_active_inventory():
    recipes = load_recipe_catalog(load_ingredient_catalog()).all()
    assert len(recipes) == 104
    assert all(recipe.version >= 2 for recipe in recipes)
    assert {recipe.id for recipe in recipes} == target_ids()
```

Add assertions for provenance coverage, editorial mix, diet floors, family/method diversity, workflow success, and zero duplicate fingerprints.

- [ ] **Step 2: Verify RED**

Expected: manifest generation is inactive and the runtime still contains legacy recipes.

- [ ] **Step 3: Promote all 104 candidates in one batch**

Call `promote_candidates(sorted(candidate_paths), reviewed_by="Martin + Uvar.si QA", reviewed_on=date.today())`. Confirm the staged audit passes before publication.

- [ ] **Step 4: Retire the generic generation and activate generation 1**

Set `active` to false for the 60 legacy recipe records. Set manifest fields to an even `catalog_revision`, increment `library_version` once for the cutover, and set `curation_generation` to `1`.

- [ ] **Step 5: Verify and commit**

Run the catalog, provenance, workflow, snapshots, matcher, renderer, deterministic-plan, and full library-gate suites. Commit with `feat: activate curated recipe library`.

### Task 12: Release gates, production rollout, and rollback proof

**Files:**
- Modify: `hetzner/recipe-engine-rollout.sh`
- Modify: `tests/test_recipe_engine_release_controller.py`
- Modify: `tests/test_recipe_engine_health.py`
- Modify: `VERSION`

**Interfaces:**
- Produces: a guarded production release with curated generation 1 and payments off.

- [ ] **Step 1: Write failing rollout assertions**

Require the rollout to stop when active count differs from 104, provenance is incomplete, `curation_generation != 1`, workflow errors exist, p95 exceeds 500 ms, or payments are enabled.

- [ ] **Step 2: Verify RED**

Run the two rollout/health suites. Expected: current controller does not inspect curated generation or provenance.

- [ ] **Step 3: Add fail-closed release checks**

Extend the existing Uvar.si-only controller; preserve its rollback and service-scope protections. Do not add commands that restart Caddy or `taktik-mapa`.

- [ ] **Step 4: Run the complete verification matrix**

Run:

```powershell
python -m pytest -q -p no:cacheprovider
python -m app.library_gate
git diff --check
```

Expected: complete pytest pass, `recipes.active=104`, `errors=0`, and a clean diff check.

- [ ] **Step 5: Obtain independent code and content review**

The code reviewer checks security, rollback, cache/versioning, and service scope. The content reviewer checks every rendered recipe for practical cooking order, natural Slovak, correct product state, seasoning, quantities, and recognisable identity. Resolve every blocking finding and repeat Step 4.

- [ ] **Step 6: Commit, push, and deploy**

Commit the rollout files and version, push fast-forward to `origin/main`, then run the existing samopull path. Payments remain off.

- [ ] **Step 7: Verify production and rollback readiness**

Check `/api/health`, `/app`, all four diet modes, all three cooking frequencies, one adult/child household, pantry subtraction, whole-package shopping, p50/p95 plan time, worker health, and service status. Confirm `taktik-mapa` stayed active. Keep the prior release symlink available until the production smoke matrix passes.
