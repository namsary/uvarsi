# Recipe Engine Production Rollout Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Bezpečne prepnúť Uvar.si z plateného asynchrónneho generovania receptov na okamžitý deterministický engine, overiť produkciu a ponechať krátke rollback okno.

**Architecture:** Trojstavový serverový flag `off|shadow|on` oddelí nasadenie kódu od aktivácie. V `shadow` sa nový engine vyhodnocuje mimo odpovede používateľa a výsledok sa iba anonymne meria. V `on` vytvoria plan endpointy výsledok synchronne, uložia ho do existujúcej cache a nevolajú model ani frontu.

**Tech Stack:** FastAPI, SQLite, systemd, Caddy, samopull, pytest, shell/PowerShell deploy gates.

**Spec:** `docs/superpowers/specs/2026-08-30-vlastny-receptovy-engine-design.md`

**Prerequisites:** foundation, deterministic planner, recipe library and Pro modes plans.

## Global Constraints

- Produkčné platby zostávajú vypnuté.
- Aktivácia nesmie vyžadovať používateľský zásah ani zapnutý notebook.
- `on` cesta nesmie importovať ani volať Anthropic/OpenAI pri používateľskom pláne.
- OCR/AI zber letákov zostáva nezmenený.
- Rollback flagu nesmie mazať používateľov, špajzu, plány, ponuky ani účtovné dáta.
- Release musí prejsť existujúcim samopull, dozorcom, health a rollback gate.
- Verejná chyba nesmie prezradiť interný stack trace ani osobné údaje.

---

## File Map

- Modify `app/config.py`: validovaný `UVARSI_RECIPE_ENGINE=off|shadow|on`.
- Modify `app/server.py`: synchronná deterministic path, cache a chybové odpovede.
- Modify `app/plan_data.py`: verzia/signature compatibility.
- Modify `app/predpocet.py`: deterministický predvýpočet bez modelových nákladov.
- Modify `app/plan_worker.py`: v `on` nespracúva nové recipe jobs; staré dokončí alebo bezpečne zneplatní.
- Modify `app/static/app.html`: okamžitá odpoveď a kompatibilita so starým `preparing` stavom počas rollback okna.
- Modify `app/naklady.py`: oddeliť nulové náklady plánov od AI nákladov letákov.
- Modify `app/server.py` health: engine mode/version/library coverage/timing.
- Modify `hetzner/dozorca.sh`, `nasad.ps1`, `hetzner/samopull.sh`: activation and smoke gates.
- Test `tests/test_recipe_engine_flag.py`, `tests/test_deterministic_plan_api.py`, `tests/test_no_live_recipe_ai.py`, `tests/test_recipe_engine_health.py`, `tests/test_recipe_engine_rollout_contract.py`.

### Task 1: Validated three-state feature flag

**Files:**
- Modify: `app/config.py`
- Create: `tests/test_recipe_engine_flag.py`

**Interfaces:**
- Produces `recipe_engine_mode() -> Literal["off", "shadow", "on"]`.

- [ ] **Step 1: Write flag tests**

```python
@pytest.mark.parametrize("value", ["off", "shadow", "on"])
def test_accepts_recipe_engine_modes(monkeypatch, value):
    monkeypatch.setenv("UVARSI_RECIPE_ENGINE", value)
    assert recipe_engine_mode() == value

def test_invalid_mode_fails_closed(monkeypatch):
    monkeypatch.setenv("UVARSI_RECIPE_ENGINE", "yes")
    with pytest.raises(RuntimeError, match="UVARSI_RECIPE_ENGINE"):
        recipe_engine_mode()
```

- [ ] **Step 2: Confirm RED**

Run: `pytest -q tests/test_recipe_engine_flag.py`

- [ ] **Step 3: Implement cached validated config**

Default is `off`. Trim/casefold is not accepted silently: deployment values must be exact to avoid accidental activation. Add `reset_config_cache_for_tests()` if needed rather than mutating a module global from tests.

- [ ] **Step 4: Run tests and commit**

Run: `pytest -q tests/test_recipe_engine_flag.py tests/test_config.py`

```bash
git add app/config.py tests/test_recipe_engine_flag.py
git commit -m "feat: gate deterministic recipe engine"
```

### Task 2: Synchronous plan API behind `on`

**Files:**
- Modify: `app/server.py` around `/api/plan/generuj`, `/api/plan/zo-spajze`, cache storage.
- Create: `tests/test_deterministic_plan_api.py`

**Interfaces:**
- `off`: current queue/model behavior unchanged.
- `shadow`: current response behavior unchanged; no user-visible deterministic result.
- `on`: POST returns HTTP 200 with plan or a typed 4xx/503 error; no plan job is enqueued.

- [ ] **Step 1: Write endpoint contract tests**

```python
def test_on_returns_ready_plan_without_queue(on_server, authenticated_client):
    response = authenticated_client.post("/api/plan/generuj")
    assert response.status_code == 200
    assert response.json()["meta"]["engine"] == "deterministic"
    assert count_rows(on_server.db(), "plan_jobs") == 0

def test_on_pantry_plan_uses_quantities(on_server, premium_client):
    save_pantry(premium_client, "ryža", 450, "g")
    plan = premium_client.post("/api/plan/zo-spajze").json()
    assert plan["meta"]["pantry_applied"] is True
```

- [ ] **Step 2: Confirm RED**

Run: `pytest -q tests/test_deterministic_plan_api.py`

- [ ] **Step 3: Add one server helper for deterministic creation**

```python
def poskladaj_deterministicky_plan(u, tyz, obchody, rows, spajza, *, mode, zo_spajze):
    adults, children = zlozenie_domacnosti(u)
    podpis = podpis_planu(tyz, obchody, u["frekvencia"], rows, spajza,
                          adults=adults, children=children, zo_spajze=zo_spajze)
    variant = plan_variant_for(u["id"], PLAN_VARIANTS)
    return build_deterministic_plan(
        week=tyz, rows=rows, stores=obchody,
        adults=adults, children=children, frequency=u["frekvencia"],
        pantry=spajza, pantry_driven=zo_spajze,
        mode=mode, seed=f"{tyz}:{podpis}:{variant}",
    )
```

Both regular and pantry plans subtract known pantry quantities from the personal shopping list. Only `zo_spajze=True` lets pantry change recipe selection and therefore creates a personal signature/cache. Store through existing personal/shared cache functions in one transaction. Preserve forced-plan entitlement by advancing among a bounded set of deterministic variants, not paid model calls.

- [ ] **Step 4: Map honest failure states**

Map `NoCompatiblePlan` codes to Slovak responses:

```python
{
  "insufficient_offers": "V zvolených obchodoch nie je dosť vhodných akcií.",
  "diet_too_strict": "Pre tento režim a obchody nevieme zostaviť celý týždeň.",
  "unmeasurable_packages": "Pri niektorých akciách chýba veľkosť balenia."
}
```

Include `suggestions` with allowed actions only: add store, use standard mode, wait for complete flyer refresh. Never suggest retrying the same deterministic input as if it could randomly succeed.

- [ ] **Step 5: Run API tests and commit**

Run: `pytest -q tests/test_deterministic_plan_api.py tests/test_plan_async_api.py tests/test_server.py`

```bash
git add app/server.py tests/test_deterministic_plan_api.py
git commit -m "feat: serve deterministic plans synchronously"
```

### Task 3: Prove no live recipe AI or cost reservation

**Files:**
- Create: `tests/test_no_live_recipe_ai.py`
- Modify: `app/naklady.py`
- Modify: `tests/test_naklady_integracia.py`

**Interfaces:**
- `on` path creates no Anthropic client, no LLM message, no `plan`/`predpocet` cost reservation and no `plan_jobs` row.
- Flyer collector cost accounting remains unchanged.

- [ ] **Step 1: Write constructor-bomb tests**

```python
def test_all_plan_endpoints_work_when_anthropic_constructor_explodes(on_server, monkeypatch):
    monkeypatch.setitem(sys.modules, "anthropic", BombModule())
    assert client.post("/api/plan/generuj").status_code == 200
    assert premium_client.post("/api/plan/zo-spajze").status_code == 200
```

- [ ] **Step 2: Assert unchanged cost ledger**

Snapshot relevant `naklady` rows, generate regular/pantry/forced plans, and assert exact equality afterward. Separately run collector tests to prove flyer model calls are still charged.

- [ ] **Step 3: Remove recipe-cost UI assumptions**

Daily regeneration limits may remain as abuse protection, but their user copy must not imply each plan costs model credit. Keep a reasonable request limit and HTTP 429 behavior independent of Premium payment status.

- [ ] **Step 4: Run tests and commit**

Run: `pytest -q tests/test_no_live_recipe_ai.py tests/test_naklady_integracia.py tests/test_naklady.py tests/test_naklady_kredit.py`

```bash
git add app/naklady.py tests/test_no_live_recipe_ai.py tests/test_naklady_integracia.py
git commit -m "fix: remove live recipe AI costs"
```

### Task 4: Shadow comparison without delaying users

**Files:**
- Modify: `app/predpocet.py`
- Modify: `app/server.py` observability helpers.
- Create: `tests/test_recipe_engine_shadow.py`

**Interfaces:**
- `shadow` runs deterministic builds in scheduled precompute, not inline with a user response.
- Stores aggregate quality metrics only; no recipe content or PII in logs.

- [ ] **Step 1: Write non-blocking shadow test**

```python
def test_shadow_user_request_does_not_run_deterministic_builder(monkeypatch, shadow_client):
    monkeypatch.setattr(server, "build_deterministic_plan", bomb)
    response = shadow_client.get("/api/plan")
    assert response.status_code == 200
```

- [ ] **Step 2: Add scheduled shadow sample**

After successful flyer refresh/predpocet, build a fixed anonymous matrix of modes, household sizes and frequencies. Record only counts, duration, error code, family/method diversity and price delta against an existing plan when available.

- [ ] **Step 3: Add activation floors**

Shadow is eligible for `on` only when the latest complete flyer week has:

```text
success_rate >= 0.98
p95_ms < 500
dietary_violations == 0
negative_quantities == 0
invalid_package_counts == 0
library_gate == pass
```

- [ ] **Step 4: Run tests and commit**

Run: `pytest -q tests/test_recipe_engine_shadow.py tests/test_predpocet.py`

```bash
git add app/predpocet.py app/server.py tests/test_recipe_engine_shadow.py
git commit -m "feat: shadow deterministic plan quality"
```

### Task 5: Worker and frontend transition

**Files:**
- Modify: `app/plan_worker.py`
- Modify: `app/static/app.html`
- Create: `tests/test_recipe_engine_transition.py`

**Interfaces:**
- In `on`, no new regular/pantry/precompute AI job is created.
- Old queued jobs are marked failed with `engine_replaced` before dispatch, without a model call.
- Frontend accepts immediate 200 and legacy `preparing` during rollback window.

- [ ] **Step 1: Write stale queue tests**

```python
def test_on_worker_does_not_dispatch_legacy_recipe_job(on_worker, queued_job):
    result = on_worker.process_one(client=BombClient())
    assert result.state == "failed"
    assert job(queued_job).error_code == "engine_replaced"
```

- [ ] **Step 2: Implement mode-aware worker behavior**

Check flag after claiming and before dispatch. Mark only recipe plan job kinds; do not touch flyer collector processes. Keep heartbeats healthy even when no recipe work remains.

- [ ] **Step 3: Simplify visible waiting behavior**

On immediate 200, render the plan directly. Preserve polling code only for `off/shadow` rollback compatibility. Remove two-minute promise text from normal `on` experience. A synchronous failure shows exact actionable suggestion.

- [ ] **Step 4: Run tests and commit**

Run: `pytest -q tests/test_recipe_engine_transition.py tests/test_plan_worker.py tests/test_app_html_contract.py tests/test_frontend_speed_contract.py`

```bash
git add app/plan_worker.py app/static/app.html tests/test_recipe_engine_transition.py
git commit -m "feat: transition plan worker and client"
```

### Task 6: Health, dozorca and autonomous activation gate

**Files:**
- Modify: `app/server.py` `/api/health`.
- Modify: `hetzner/dozorca.sh`
- Create: `tests/test_recipe_engine_health.py`
- Create: `tests/test_recipe_engine_rollout_contract.py`

**Interfaces:**
- Health returns `recipe_engine.mode`, `library_version`, `active_templates`, coverage by mode, `last_shadow`, `p95_ms` and `ready`.

- [ ] **Step 1: Write health contract tests**

```python
def test_on_health_requires_launch_library_and_current_offers(client):
    health = client.get("/api/health").json()["recipe_engine"]
    assert set(health) >= {"mode", "library_version", "active_templates", "coverage", "ready"}
    assert health["ready"] is True
```

- [ ] **Step 2: Fail health when guarantees are broken**

`ready=false` if catalog load fails, coverage floors fail, current complete offers are unavailable or latest production smoke failed. Do not fail the whole web health endpoint solely because old recipe worker has no jobs in `on` mode.

- [ ] **Step 3: Teach dozorca the new mode**

In `on`, dozorca checks engine readiness and executes a rate-limited authenticated synthetic smoke using a dedicated server-side test profile with no real e-mail. It must not create fake public users or affect signup counts. On failure notify immediately and leave the last cached public plan available.

- [ ] **Step 4: Run tests and commit**

Run: `pytest -q tests/test_recipe_engine_health.py tests/test_recipe_engine_rollout_contract.py tests/test_deploy_safety.py`

```bash
git add app/server.py hetzner/dozorca.sh tests/test_recipe_engine_health.py tests/test_recipe_engine_rollout_contract.py
git commit -m "ops: monitor deterministic recipe engine"
```

### Task 7: Release packaging and staged production activation

**Files:**
- Modify: `nasad.ps1`
- Modify: `hetzner/samopull.sh`
- Modify: `VERSION`
- Modify: relevant deploy contract tests.

**Interfaces:**
- Deployment preserves current flag unless an explicit activation step changes it.
- `off → shadow → on` each has a fail-closed gate and flag-only rollback.

- [ ] **Step 1: Add release preflight tests**

Require import of all new modules, `python -m app.library_gate`, full catalog presence and deterministic smoke before switching the release directory. Require `UVARSI_PAYMENTS_ENABLED=0` throughout.

- [ ] **Step 2: Run complete local release gate**

```powershell
python -m app.library_gate
python -m pytest -q
```

Expected: library gate 0; full suite green; no modified files outside intended scope.

- [ ] **Step 3: Deploy code with flag `off`**

Increment `VERSION`, commit and push through the existing non-force release path. Verify public health, login, cached plan, flyer freshness, worker heartbeat and payments off. Do not activate the engine in the same mutation.

- [ ] **Step 4: Activate `shadow` autonomously**

Set only `UVARSI_RECIPE_ENGINE=shadow`, restart Uvar.si services, verify health and wait for one complete scheduled shadow matrix. If activation floors fail, return to `off` and keep the release code deployed for diagnosis.

- [ ] **Step 5: Activate `on` after the shadow gate**

Set only `UVARSI_RECIPE_ENGINE=on`; restart; verify:

```text
GET /api/health = 200, recipe_engine.ready=true
real login remains valid
POST /api/plan/generuj returns 200 in < 2 s externally
plan.meta.engine=deterministic
no new plan_jobs row
no plan/predpocet AI cost entry
payments_enabled=false
```

- [ ] **Step 6: Execute real-account smoke**

Through one existing authorized test account, verify standard plan, all 7/4/3 schedules, quantified pantry, Pro diet mode, shopping packages, recipe Slovak and mobile rendering. Do not alter another user or create fabricated accounts.

- [ ] **Step 7: Commit release wiring**

```bash
git add nasad.ps1 hetzner/samopull.sh VERSION tests
git commit -m "release: activate deterministic recipe engine"
```

### Task 8: Remove live recipe generation after rollback window

**Files:**
- Modify: `app/server.py`
- Modify: `app/plan_data.py`
- Modify: `app/plan_jobs.py`
- Modify: `app/plan_worker.py`
- Modify: `app/predpocet.py`
- Modify: tests that mock recipe Anthropic calls.

**Interfaces:**
- Flyer OCR keeps Anthropic support.
- Recipe endpoints have only deterministic implementation.

- [ ] **Step 1: Define evidence for closing rollback window**

Require at least two complete flyer cycles in `on`, success rate ≥98%, zero dietary violations, p95 <500 ms server-side, no critical user-reported recipe defect and verified rollback backup.

- [ ] **Step 2: Write import/call prohibition test**

```python
def test_recipe_runtime_has_no_model_prompt_or_client():
    source = Path("app/server.py").read_text("utf-8") + Path("app/plan_data.py").read_text("utf-8")
    assert "personal_plan_messages(" not in source
    assert "_new_plan_model_client(" not in source
```

Scope the assertion to recipe runtime only; `zbierac_akcii.py` may still use Anthropic.

- [ ] **Step 3: Remove dead recipe prompt and queue paths**

Delete live recipe prompt generation, model correction loop and recipe-specific cost reservation. Retain plan cache utilities, calendar, whole-package compatibility and any worker infrastructure still required elsewhere. Migrate or expire old queued rows without deleting audit history.

- [ ] **Step 4: Run full regression and release**

Run: `python -m app.library_gate`

Run: `pytest -q`

Deploy as a separate version and verify the same production smoke. Payments stay off until their own release.

- [ ] **Step 5: Commit**

```bash
git add app tests VERSION
git commit -m "refactor: remove live AI recipe generation"
```
