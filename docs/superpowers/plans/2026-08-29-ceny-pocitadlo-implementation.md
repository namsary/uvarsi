# Tretí Cenník a Pravdivé Počítadlo Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Doplniť na landing plnohodnotný tretí cenový plán a dôveryhodný ukazovateľ reálnej komunity bez falošných účtov alebo zavádzajúcej scarcity.

**Architecture:** Verejný landing endpoint spojí validované týždenné dáta s anonymným agregátom `COUNT(*)` používateľov. Frontend zobrazí počítadlo až od desiatich skutočných účtov; pri chybe ho skryje bez vplyvu na bloček a ceny. Cenník bude mať Free, zvýraznený Zakladajúci a Premium ročný anchor, pričom platobné CTA zostanú nezáväzné, kým sú platby vypnuté.

**Tech Stack:** FastAPI, SQLite, static HTML/CSS/JavaScript, pytest, existing landing JSON endpoint.

**Spec:** `docs/superpowers/specs/2026-08-29-diverzita-ceny-pocitadlo-design.md`

## Global Constraints

- Počítadlo používa výhradne reálny počet riadkov v `pouzivatelia`; žiadny seed, offset ani falošné účty.
- Hranica viditeľnosti je 10 skutočných účtov a cieľ je 250.
- Počet účtov nie je počet zaplatených Zakladajúcich nárokov; text ich nesmie zamieňať.
- Platby ostávajú vypnuté; žiadne tlačidlo nesmie predstierať nákup.
- Zlyhanie DB agregátu nesmie zhodiť platný bloček, FAQ ani cenník.

---

## Task 1: Extend public landing data with a safe community aggregate

**Files:**
- Modify: `app/server.py`
- Test: `tests/test_server.py`

- [ ] Add failing tests for 0, 9, 10 and 251 users. Expected payload shape:

```json
{"community":{"accounts":10,"goal":250,"visible":true}}
```

- [ ] Assert only integers leave the endpoint, no emails/IDs are returned, and 0–9 yields `visible:false`.
- [ ] Add a failing test where the user-count query raises `sqlite3.Error`; weekly landing data must still return HTTP 200 with `visible:false`.
- [ ] Run focused tests; expect RED.
- [ ] Implement:

```python
COMMUNITY_GOAL = 250
COMMUNITY_VISIBILITY_THRESHOLD = 10

def public_community(con) -> dict:
    accounts = int(con.execute("SELECT COUNT(*) FROM pouzivatelia").fetchone()[0])
    return {
        "accounts": accounts,
        "goal": COMMUNITY_GOAL,
        "visible": accounts >= COMMUNITY_VISIBILITY_THRESHOLD,
    }
```

- [ ] Wrap only the community query in its own failure boundary; do not weaken landing-data date validation.
- [ ] Run focused tests; expect GREEN.
- [ ] Commit: `feat(landing): expose anonymous community count`.

## Task 2: Render the truthful counter progressively

**Files:**
- Modify: `index.html`
- Test: `tests/test_landing_html_contract.py`
- Test: `tests/test_landing_visual_contract.py`

- [ ] Add failing HTML/Node tests for hidden 9, visible 10, visible 251, malformed payload and failed fetch.
- [ ] Assert the displayed copy is exactly `Testovacia komunita: X z cieľa 250 účtov` and the bar width is capped at 100%.
- [ ] Run focused tests; expect RED.
- [ ] Render the counter from the already fetched `/api/public/landing` payload; never add a second endpoint/fetch.
- [ ] Under threshold or on malformed/missing community data, render only `Prvých 250 získa zakladajúcu cenu`.
- [ ] Add `aria-valuemin`, `aria-valuemax`, `aria-valuenow` and screen-reader text to the progress bar.
- [ ] Run focused tests; expect GREEN.
- [ ] Commit: `feat(landing): show real community progress`.

## Task 3: Add the third Premium pricing tier

**Files:**
- Modify: `index.html`
- Test: `tests/test_landing_visual_contract.py`
- Test: `tests/test_landing_html_contract.py`

- [ ] Add failing copy/layout tests for exactly three plans and these prices:

```text
Free — 0 € navždy
Zakladajúci — 39 € jednorazovo, cena natrvalo, prvých 250
Premium — 49 € / rok po skončení zakladajúcej ponuky
```

- [ ] Assert Founding remains the only visually highlighted card and Premium is an anchor, not a live checkout.
- [ ] Run focused tests; expect RED.
- [ ] Change the pricing grid to three desktop columns, one mobile column and two-plus-one tablet layout without horizontal overflow.
- [ ] Give Founding the strongest CTA `Chcem zakladajúcu cenu`; Premium uses `Chcem vedieť o spustení` and submits the existing nonbinding email form with plan metadata.
- [ ] Keep core feature claims consistent: Premium and Founding offer the same core premium functionality; Founding differs by lifetime launch price, Premium by annual billing after launch.
- [ ] Run focused tests; expect GREEN.
- [ ] Commit: `feat(landing): add annual premium tier`.

## Task 4: Audit pricing copy and FAQ consistency

**Files:**
- Modify: `index.html`
- Modify: `docs/legal/01_VOP_NAVRH.md`
- Test: `tests/test_landing_visual_contract.py`

- [ ] Add failing tests that forbid contradictory prices, live-payment wording, fake popularity claims and wording equating accounts with purchasers.
- [ ] Run focused tests; expect RED if stale copy remains.
- [ ] Align FAQ and legal draft with: payments currently off, email interest is nonbinding, final purchase creates the entitlement, refund/withdrawal handling follows the final checkout terms, actual account count is informational.
- [ ] Preserve the motivating annual savings projection, but label it as a model/example rather than guaranteed savings.
- [ ] Run focused tests; expect GREEN.
- [ ] Commit: `docs(landing): align pricing and community claims`.

## Task 5: Release and production verification

**Files:**
- Modify: `VERSION`

- [ ] Run `pytest -q tests/test_server.py tests/test_landing_html_contract.py tests/test_landing_visual_contract.py`; expect zero failures.
- [ ] Run `pytest -q`; expect zero failures.
- [ ] Bump `VERSION` and commit: `release: add complete pricing and real community count`.
- [ ] Deploy through the existing samopull flow and verify desktop/mobile widths, landing endpoint, bloček freshness, all three CTAs and MailerLite metadata.
- [ ] Query production user count read-only and verify the public payload matches it exactly; with the current count below 10, confirm the counter remains hidden.
- [ ] Verify a failed/blocked landing API call leaves readable static pricing and never displays stale fabricated progress.

## Final Verification

- [ ] Run `rg -n "12/250|23|obsaden|zakladaj|39|49|community" index.html app tests docs/legal` and resolve every contradictory or deceptive match.
- [ ] Confirm no `INSERT INTO pouzivatelia` exists outside legitimate registration/tests/migrations.
- [ ] Confirm all three plan cards are keyboard-accessible and fit at 320 px width.
- [ ] Request independent copy/design/code review before pushing to `origin/main`.
