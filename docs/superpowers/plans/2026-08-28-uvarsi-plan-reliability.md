# Uvar.si Plan Reliability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Plán sa po chybnom výstupe modelu obnoví bez slepej uličky, server bude jediným zdrojom kuchárskych jednotiek a majiteľ bude môcť otestovať Premium bez zapnutia platieb.

**Architecture:** Model vyberá jedlo, ponuky a postup, ale porciovú rolu, jednotku a bezpečnú dávku určuje Python z názvu, kategórie a balenia ponuky. Platný zdieľaný plán má prednosť pred starým zlyhaným jobom. Premium sa majiteľovi udelí existujúcou serverovou CLI ako nulový ručný nárok, oddelený od platieb.

**Tech Stack:** Python 3.12, FastAPI, SQLite, pytest, vanilla JavaScript, systemd worker.

**Spec:** `00_HANDOFF_projekt_uvarsi.md`

## Global Constraints

- Platby zostávajú vypnuté.
- Ceny a ponuky sa vždy čítajú iba z overenej SQLite databázy.
- Žiadne mazanie účtovníctva, limitov ani produkčných dát.
- Nedotýkať sa Caddy ani aplikácie taktik-mapa.
- Každá zmena správania musí mať najprv zlyhávajúci regresný test.

---

### Task 1: Server-owned portion contract

**Files:**
- Modify: `app/plan_data.py`
- Test: `tests/test_plan_data.py`

**Interfaces:**
- Consumes: overený riadok ponuky a modelový item s `offer_key`.
- Produces: `_amount_per_adult(item, row) -> (role, base, Decimal)` odvodené serverom; modelové polia nesmú meniť výsledok.

- [ ] **Step 1: Write the failing test**

Pridaj test, v ktorom model označí zeleninu ako `1 ks`, ale server z názvu a kategórie vyberie bezpečnú gramovú dávku a plán zostaví.

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest -q tests/test_plan_data.py -k server_owns_portion`
Expected: FAIL s dnešnou chybou o jednotke porciovej triedy.

- [ ] **Step 3: Write minimal implementation**

Pridaj serverovú mapu predvolených dávok podľa `ingredient_role_for()` a zmeň `_amount_per_adult()`, aby neveril `amount_per_adult`, `unit` ani `ingredient_role` z modelu. Zvýš `PLAN_ALGO_VERSION`.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest -q tests/test_plan_data.py`
Expected: PASS.

### Task 2: Recovery from a failed job

**Files:**
- Modify: `app/server.py`
- Test: `tests/test_plan_async_api.py`

**Interfaces:**
- Consumes: failed job a platný zdieľaný plán pre rovnaký podpis/variant.
- Produces: `GET /api/plan` vráti platný plán, nie starú chybovú stenu.

- [ ] **Step 1: Write the failing test**

Vytvor failed job, potom publikuj platný zdieľaný plán a over, že ďalší GET vráti `jedla` bez `status=failed`.

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest -q tests/test_plan_async_api.py -k shared_plan_recovers_failed_job`
Expected: FAIL, odpoveď má `status=failed`.

- [ ] **Step 3: Write minimal implementation**

V `daj_plan()` skontroluj a adoptuj platný zdieľaný plán skôr, než sa vráti terminálny failed payload.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest -q tests/test_plan_async_api.py`
Expected: PASS.

### Task 3: Release and owner beta entitlement

**Files:**
- Modify: `VERSION`
- Verify: all tests and production health

**Interfaces:**
- Consumes: zelený commit na `origin/main`.
- Produces: nasadená verzia, aktívny worker, platný plán a ručný Premium nárok pre `martinnn@centrum.sk`.

- [ ] **Step 1: Run focused and full verification**

Run: `pytest -q`
Expected: všetky testy PASS.

- [ ] **Step 2: Commit and push**

Commit iba súbory tejto opravy a pushni branch na `origin/main`.

- [ ] **Step 3: Wait for autonomous deployment**

Over `/api/health`, systemd služby a produkčnú verziu bez výpisu tajomstiev.

- [ ] **Step 4: Grant test Premium**

Run on server: `cd /opt/uvarsi/app && ../venv/bin/python premium_cli.py martinnn@centrum.sk`
Expected: aktívny ručný nárok za 0 EUR; platby zostávajú vypnuté.

- [ ] **Step 5: Production smoke**

Over prihlásený tok: `/api/me` hlási Premium, plán je pripravený alebo sa pripravuje, worker ho dokončí a špajza sa dá upraviť.
