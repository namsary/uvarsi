# Uvar.si SEO/GEO Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Vybudovať technický SEO základ a tri slovenské verejné obsahové stránky, ktoré sú rýchle, indexovateľné, citovateľné a nikdy nevydávajú staré ceny za aktuálne.

**Architecture:** FastAPI bude server-side vykresľovať verejné obsahové stránky a technické SEO endpointy. Dynamická týždenná stránka spotrebuje iba už validovaný `landing_data.json`; evergreen stránky budú čisté HTML bez externého JavaScriptu. Caddy oddelí kanonický host od presmerovaní, nastaví cache a zachová existujúcu izoláciu druhej aplikácie na serveri.

**Tech Stack:** Python 3.12, FastAPI, pytest, HTML/CSS, JSON-LD, Caddy, PowerShell deploy

**Spec:** `docs/superpowers/specs/2026-08-25-uvarsi-seo-geo-design.md`

## Global Constraints

- Cieľ je iba Slovensko a slovenčina počas najbližších 80 dní.
- Platby ostávajú vypnuté a `app/platby.py` sa nemení.
- Aktuálne ceny sa smú zobraziť iba po úspešnom `validate_landing_data(..., today)`.
- Žiadne vymyslené ratingy, recenzie, úspory, ceny alebo počty používateľov.
- Žiadne masové programatické SEO stránky ani nová JavaScriptová závislosť.
- Caddy zmena musí zachovať blok `mapa.89.167.72.159.sslip.io`, prejsť validáciou pred výmenou a mať rollback.

---

### Task 1: Server-rendered public page module

**Files:**
- Create: `app/public_pages.py`
- Create: `tests/test_public_pages.py`

**Interfaces:**
- Consumes: `landing_data.validate_landing_data(payload, today)` and a loaded landing payload.
- Produces: `render_weekly_page(payload, today) -> RenderedPage`, `render_evergreen_page(slug) -> RenderedPage`, `render_sitemap(today, weekly_modified) -> str`, `ROBOTS_TXT`.

- [ ] **Step 1: Write failing tests for metadata, one H1 and safe escaping**

Create tests asserting every page has a unique `<title>`, description, canonical, one `<h1>`, Slovak `lang`, Open Graph metadata, valid JSON-LD and escaped offer/source text.

- [ ] **Step 2: Run the focused tests and confirm failure**

Run: `python -m pytest tests/test_public_pages.py -q`

Expected: import failure because `app.public_pages` does not exist.

- [ ] **Step 3: Implement focused renderers**

Create an immutable result type:

```python
@dataclass(frozen=True)
class RenderedPage:
    html: str
    indexable: bool
    last_modified: date | None = None
```

Build one shared HTML shell with local typography, canonical, description, Open Graph/Twitter tags, JSON-LD and internal navigation. Use `html.escape` for every string originating in data. Use `json.dumps(..., ensure_ascii=False).replace("</", "<\\/")` for JSON-LD.

For `/co-varit-tento-tyzden`, call `validate_landing_data(payload, today)` before rendering prices. Render range, meals, sources and explicit methodology. On validation failure, render a price-free recovery state and set `indexable=False`.

Implement evergreen bodies for `lacny-jedalnicek` and `ako-varime-z-akcii` as text constants with direct-answer introductions, practical subsections, internal links and no unstable price claims.

- [ ] **Step 4: Run focused tests**

Run: `python -m pytest tests/test_public_pages.py -q`

Expected: all tests pass.

- [ ] **Step 5: Commit**

```powershell
git add app/public_pages.py tests/test_public_pages.py
git commit -m "feat: add trustworthy public SEO pages"
```

### Task 2: Public SEO routes and private-route noindex

**Files:**
- Modify: `app/server.py`
- Modify: `tests/test_server.py`
- Modify: `tests/test_auth.py`

**Interfaces:**
- Consumes: renderers and constants from `app.public_pages`.
- Produces: GET routes `/co-varit-tento-tyzden`, `/lacny-jedalnicek`, `/ako-varime-z-akcii`, `/robots.txt`, `/sitemap.xml`; noindex headers for `/app` and `/prihlasenie`.

- [ ] **Step 1: Write failing route tests**

Assert:

```python
assert client.get("/robots.txt").headers["content-type"].startswith("text/plain")
assert "OAI-SearchBot" in client.get("/robots.txt").text
assert "Disallow: /api/" in client.get("/robots.txt").text
assert "https://uvar.si/co-varit-tento-tyzden" in client.get("/sitemap.xml").text
assert client.get("/app").headers["x-robots-tag"] == "noindex, nofollow, noarchive"
assert client.get("/prihlasenie").headers["x-robots-tag"] == "noindex, nofollow, noarchive"
```

Add a current-payload route test and an expired-payload route test. The expired case must contain no old price, return `X-Robots-Tag: noindex, follow`, and remain human-readable.

- [ ] **Step 2: Run focused tests and confirm failure**

Run: `python -m pytest tests/test_server.py tests/test_auth.py -q`

Expected: missing routes and missing headers.

- [ ] **Step 3: Add routes with explicit response headers**

Use `HTMLResponse`, `PlainTextResponse` and `Response(media_type="application/xml")`. Load `LANDING_DATA` inside the weekly handler; catch file/JSON/validation errors and call the renderer with no trusted payload. Add `Cache-Control: public, max-age=300, must-revalidate` to public HTML/XML/text. Add noindex headers and `Cache-Control: no-store` to app/login responses.

- [ ] **Step 4: Add meta robots fallback to login and app HTML**

Insert:

```html
<meta name="robots" content="noindex,nofollow,noarchive">
```

into both server login templates and `app/static/app.html`.

- [ ] **Step 5: Run focused tests**

Run: `python -m pytest tests/test_server.py tests/test_auth.py tests/test_app_html_contract.py -q`

Expected: all pass.

- [ ] **Step 6: Commit**

```powershell
git add app/server.py app/static/app.html tests/test_server.py tests/test_auth.py tests/test_app_html_contract.py
git commit -m "feat: expose crawlable pages and noindex private UI"
```

### Task 3: Homepage metadata, schema and internal linking

**Files:**
- Modify: `index.html`
- Modify: `tests/test_landing_html_contract.py`
- Create: `tests/test_seo_contract.py`

**Interfaces:**
- Consumes: canonical public routes from Task 2.
- Produces: homepage discoverability and crawl paths into the content cluster.

- [ ] **Step 1: Write failing static SEO contract tests**

Tests parse the homepage and assert exactly one canonical, one H1, title/description, `og:title`, `og:description`, `og:url`, `og:type`, `twitter:card`, valid JSON-LD with `WebSite` and `SoftwareApplication`, plus internal links to all three content pages. Assert JSON-LD contains no `aggregateRating`, `review`, `offers` or unsupported savings claim.

- [ ] **Step 2: Run tests and confirm failure**

Run: `python -m pytest tests/test_seo_contract.py -q`

Expected: Open Graph, JSON-LD and internal links missing.

- [ ] **Step 3: Add truthful metadata and navigation**

Add social metadata and JSON-LD to `<head>`. Add a compact public navigation/footer cluster. Keep the existing hero and conversion flow unchanged. Use the exact canonical URLs from the sitemap.

- [ ] **Step 4: Run landing and size tests**

Run: `python -m pytest tests/test_seo_contract.py tests/test_landing_html_contract.py tests/test_frontend_speed_contract.py -q`

Expected: all pass and compressed landing remains inside the existing size budget.

- [ ] **Step 5: Commit**

```powershell
git add index.html tests/test_seo_contract.py tests/test_landing_html_contract.py
git commit -m "feat: connect homepage to SEO content cluster"
```

### Task 4: Canonical host, cache and deployment coverage

**Files:**
- Modify: `nasad.ps1`
- Modify: `tests/test_deploy_safety.py`
- Modify: `tests/test_deploy_covers_all_modules.py`
- Modify: `tests/test_deploy_manifest.py`

**Interfaces:**
- Consumes: `app/public_pages.py` and public routes.
- Produces: deployed module, canonical redirects, immutable static cache and short public-page cache.

- [ ] **Step 1: Write failing deployment contract tests**

Assert `public_pages.py` is copied to `/opt/uvarsi/app/public_pages.py`. Assert generated Caddy has one canonical `uvar.si` block and a separate redirect block for `www.uvar.si`, `uvarsi.sk`, `www.uvarsi.sk` and the sslip hostname. Assert path-preserving `redir https://uvar.si{uri} permanent`. Assert immutable cache for `/static/fonts/*` and `/static/*.png` while `/api/*` is not publicly cached.

- [ ] **Step 2: Run deployment tests and confirm failure**

Run: `python -m pytest tests/test_deploy_safety.py tests/test_deploy_covers_all_modules.py tests/test_deploy_manifest.py -q`

Expected: new module and redirect contract missing.

- [ ] **Step 3: Update deploy manifest and Caddy template**

Upload `app/public_pages.py`. Split host blocks, retain pre-write validation, backup and rollback. Route the three content pages, robots and sitemap to FastAPI. Keep root landing static, `/api/*`, `/app*`, `/prihlasenie*` and `/static/*` behavior explicit.

- [ ] **Step 4: Extend post-deploy checks**

Verify:

```sh
curl -fsS https://uvar.si/robots.txt
curl -fsS https://uvar.si/sitemap.xml
curl -fsSI https://uvar.si/app | grep -qi 'x-robots-tag: noindex'
curl -fsSI https://uvar.si/static/fonts/manrope-400-800.7101939e.woff2 | grep -qi 'immutable'
curl -fsSI https://www.uvar.si/co-varit-tento-tyzden | grep -qi 'location: https://uvar.si/co-varit-tento-tyzden'
```

- [ ] **Step 5: Run deployment tests**

Run: `python -m pytest tests/test_deploy_safety.py tests/test_deploy_covers_all_modules.py tests/test_deploy_manifest.py tests/test_deploy_runtime_env.py -q`

Expected: all pass.

- [ ] **Step 6: Commit**

```powershell
git add nasad.ps1 tests/test_deploy_safety.py tests/test_deploy_covers_all_modules.py tests/test_deploy_manifest.py
git commit -m "feat: enforce canonical host and SEO cache policy"
```

### Task 5: Release gate and end-to-end verification

**Files:**
- Modify: `nastroje/release_gate.py`
- Create: `tests/test_release_gate_seo.py`
- Modify: `VERSION`
- Modify: `RELEASE_LOG.md`

**Interfaces:**
- Consumes: all production endpoints and headers from Tasks 1–4.
- Produces: a blocking automated verdict preventing future SEO regressions.

- [ ] **Step 1: Write failing release-gate unit tests**

Mock `_ziskaj` responses and assert the gate blocks on: robots/sitemap failure, missing weekly page, stale week, missing noindex, missing immutable cache, wrong www redirect, invalid canonical or invalid JSON-LD.

- [ ] **Step 2: Run focused test and confirm failure**

Run: `python -m pytest tests/test_release_gate_seo.py -q`

Expected: SEO checks do not yet exist.

- [ ] **Step 3: Add production SEO checks**

Extend the gate with status, header and body-aware HTTP fetching. Report each requirement separately so a failure identifies the exact regression. Do not print secrets, cookies or response bodies.

- [ ] **Step 4: Run the complete local suite**

Run: `python -m pytest -q`

Expected: zero failures.

- [ ] **Step 5: Bump version and document release**

Increment `VERSION` once after all code is green. Record scope, tests and rollback notes in `RELEASE_LOG.md`.

- [ ] **Step 6: Run local release gate**

Run: `python nastroje/release_gate.py --len-testy`

Expected: `LOCAL PASS`.

- [ ] **Step 7: Review, deploy and verify production**

Run the existing guarded deployment only after code review. Then run `python nastroje/release_gate.py` and browser-check desktop/mobile rendering of all four public pages. Payments remain off.

- [ ] **Step 8: Commit release metadata**

```powershell
git add VERSION RELEASE_LOG.md nastroje/release_gate.py tests/test_release_gate_seo.py
git commit -m "chore: gate SEO GEO release"
```

## Self-review

- Spec coverage: crawl/indexation, three pages, stale-data fail-safe, schema, host redirects, cache, release gate and payment isolation are each mapped to a task.
- Placeholder scan: no `TBD`, `TODO`, deferred implementation placeholder or unspecified error-handling step remains.
- Interface consistency: `RenderedPage` is created in Task 1, consumed in Task 2; canonical routes are created in Task 2 and linked by Tasks 3–5.
