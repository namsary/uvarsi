# Uvar.si Release Integrity Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Create a release process that proves production matches source, never presents last week's prices as current, and keeps dynamic flyer content separate from the static landing.

**Architecture:** Static HTML is deployed separately from validated flyer data. The receipt and model example become one JSON document written atomically to `/var/lib/uvarsi/landing_data.json`; FastAPI exposes it only when it matches the current Monday. A release manifest records hashes and a health endpoint proves the server, DB and landing data agree.

**Tech Stack:** Python 3.12, FastAPI, SQLite, pytest, Bash cron, PowerShell, Caddy.

**Spec:** `docs/superpowers/specs/2026-08-18-uvarsi-release-captain-design.md`

## Global Constraints

- Never read, log or copy `/opt/uvarsi/uvarsi.env`; only test whether required non-secret keys exist.
- Never modify `taktik-mapa`, its Caddy block, cron entries or backups.
- Do not silently fall back to a previous week of prices.
- Deploy must not write dynamic data into `/var/www/uvarsi/index.html`.
- Every deploy compares remote hashes and fails loudly on a mismatch.
- Remove claims for allergies, city-level availability, best shopping day and cheapest cross-store item until implemented and tested.
- Every production behavior starts with a failing test.

## File map

| File | Responsibility |
|---|---|
| `VERSION` | Release ID, changed per release. |
| `app/config.py` | Required public URL and release ID. |
| `app/weekly_data.py` | Current-week DB helpers. |
| `app/landing_data.py` | JSON validation and atomic storage. |
| `app/server.py` | Customer API, health and public landing-data endpoints. |
| `hetzner/refresh_blocek.py` | Creates JSON, never edits landing HTML. |
| `hetzner/dozorca.sh` | Checks structured freshness and retries. |
| `index.html` | Static shell plus JSON renderer. |
| `scripts/release_check.py` | Safe HTTP production check. |
| `nasad.ps1` | Fail-fast Uvar-only release. |
| `tests/` | Regression suite, no API key or network required. |

## Contracts

### Landing JSON

~~~json
{
  "schema_version": 1,
  "generated_at": "2026-08-18T05:02:20+02:00",
  "week": "2026-08-17",
  "week_label": "17.–23. 8. 2026",
  "sources": [{"store":"Lidl","url":"https://source","valid_from":"2026-08-17","valid_to":"2026-08-23"}],
  "receipt": {"meals":[],"nakup_spolu":"12,81","bezne":"20,97","usetris":"8,16"}
}
~~~

### Health endpoint

`GET /api/health` returns 200 only if the current Monday has at least 30 DB offers and landing JSON has the same Monday. Otherwise it returns 503:

~~~json
{"status":"degraded","release_id":"2026.08.18.1","expected_week":"2026-08-17","deals_count":0,"landing_week":"2026-08-10"}
~~~

## Task 1: Establish versioned, testable baseline

**Files:**
- Create: `VERSION`, `requirements-dev.txt`, `tests/conftest.py`, `tests/test_config.py`
- Modify: `.gitignore`

**Interfaces:** Produces `release_id() -> str` for all later health and release checks.

- [ ] **Step 1: Create Git baseline and ignore data/secrets.**

~~~powershell
git init
git branch -M main
@'
__pycache__/
.pytest_cache/
*.pyc
*.db
*.env
uvarsi.env
'@ | Set-Content .gitignore -Encoding utf8
'2026.08.18.1' | Set-Content VERSION -Encoding ascii
@'
pytest==8.3.5
httpx==0.28.1
'@ | Set-Content requirements-dev.txt -Encoding ascii
~~~

- [ ] **Step 2: Write the failing configuration test.**

~~~python
# tests/test_config.py
import pytest
from app.config import public_base_url, release_id

def test_public_url_requires_explicit_value(monkeypatch):
    monkeypatch.delenv("UVARSI_URL", raising=False)
    with pytest.raises(RuntimeError, match="UVARSI_URL"):
        public_base_url()

def test_public_url_is_exact_canonical_https(monkeypatch):
    monkeypatch.setenv("UVARSI_URL", "https://uvar.si/")
    assert public_base_url() == "https://uvar.si"

def test_release_id_reads_version_file(tmp_path, monkeypatch):
    path = tmp_path / "VERSION"
    path.write_text("2026.08.18.1\n", encoding="utf-8")
    monkeypatch.setenv("UVARSI_VERSION_FILE", str(path))
    assert release_id() == "2026.08.18.1"
~~~

- [ ] **Step 3: Verify RED.**

~~~powershell
py -m pytest tests/test_config.py -v
~~~

Expected: import error because `app.config` does not exist.

- [ ] **Step 4: Implement the minimal config module.**

~~~python
# app/config.py
import os
from pathlib import Path

def public_base_url():
    value = os.environ.get("UVARSI_URL", "").strip().rstrip("/")
    if not value:
        raise RuntimeError("Chýba UVARSI_URL.")
    if value != "https://uvar.si":
        raise RuntimeError("UVARSI_URL musí byť presne https://uvar.si.")
    return value

def release_id():
    return Path(os.environ.get("UVARSI_VERSION_FILE", "VERSION")).read_text(encoding="utf-8").strip()
~~~

- [ ] **Step 5: Verify GREEN and commit.**

~~~powershell
py -m pytest tests/test_config.py -v
git add .gitignore VERSION requirements-dev.txt app/config.py tests/test_config.py
git commit -m "chore: establish Uvar release baseline"
~~~

## Task 2: Stop serving stale prices as current

**Files:**
- Create: `app/weekly_data.py`, `tests/test_weekly_data.py`
- Create: `tests/test_server.py`
- Modify: `app/server.py:19-26,283-316,357-362`

**Interfaces:** `current_monday(today=None) -> str`, `offers_for_current_week(con, stores, today=None) -> list`, `is_current_data_ready(con, today=None, minimum=30) -> tuple[bool,int]`.

- [ ] **Step 1: Write failing stale-data test.**

~~~python
# tests/test_weekly_data.py
import sqlite3
from datetime import date
from app.weekly_data import offers_for_current_week

def test_never_falls_back_to_last_weeks_prices():
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE akcie (tyzden TEXT, obchod TEXT, nazov TEXT, cena REAL)")
    con.execute("INSERT INTO akcie VALUES ('2026-08-10','Lidl','Mlieko',1.0)")
    assert offers_for_current_week(con, ["Lidl"], date(2026, 8, 18)) == []
~~~

- [ ] **Step 2: Verify RED.**

~~~powershell
py -m pytest tests/test_weekly_data.py::test_never_falls_back_to_last_weeks_prices -v
~~~

Expected: import error because `app.weekly_data` does not exist.

- [ ] **Step 3: Implement current-week-only query.**

~~~python
# app/weekly_data.py
from datetime import date, timedelta

def current_monday(today=None):
    today = today or date.today()
    return (today - timedelta(days=today.weekday())).isoformat()

def offers_for_current_week(con, stores, today=None):
    week = current_monday(today)
    marks = ",".join("?" for _ in stores)
    return con.execute(
        f"SELECT * FROM akcie WHERE tyzden=? AND obchod IN ({marks}) ORDER BY cena",
        (week, *stores),
    ).fetchall()
~~~

- [ ] **Step 4: Replace `akcie_pre` fallback in `app/server.py`.**

Delete the query for `SELECT tyzden FROM akcie ORDER BY tyzden DESC LIMIT 1`. When fewer than 15 current offers exist, return HTTP 503 with exactly:

~~~text
Aktuálne letákové dáta sa obnovujú. Skús to o chvíľu.
~~~

- [ ] **Step 5: Add route regression test, verify and commit.**

Use FastAPI TestClient with a temporary DB containing only 2026-08-10 offers. In `tests/test_server.py`, set `UVARSI_DB` before importing `server`, create a signed-in test session in SQLite, then assert:

~~~python
def test_plan_is_503_when_only_previous_week_exists(client):
    response = client.post("/api/plan/generuj")
    assert response.status_code == 503
    assert response.json()["detail"] == "Aktuálne letákové dáta sa obnovujú. Skús to o chvíľu."
~~~

The test is correct only if the mocked Anthropic constructor is never called.

~~~powershell
py -m pytest tests/test_weekly_data.py tests/test_server.py -v
git add app/weekly_data.py app/server.py tests/test_weekly_data.py tests/test_server.py
git commit -m "fix: never serve stale flyer prices as current"
~~~

## Task 3: Move flyer output out of index.html

**Files:**
- Create: `app/landing_data.py`, `tests/test_landing_data.py`
- Modify: `hetzner/refresh_blocek.py:310-467`, `app/server.py`, `index.html`

**Interfaces:** `validate_landing_data(payload, today=None) -> dict`, `landing_data_is_current(path, today=None) -> bool`, `write_landing_data_atomic(path, payload) -> None`, `load_landing_data(path) -> dict`, `GET /api/public/landing`.

- [ ] **Step 1: Write failing JSON validation tests.**

~~~python
# tests/test_landing_data.py
from datetime import date
import pytest
from app.landing_data import validate_landing_data

def payload():
    return {
        "schema_version":1, "generated_at":"2026-08-18T05:02:20+02:00",
        "week":"2026-08-17", "week_label":"17.–23. 8. 2026", "sources":[],
        "receipt":{"meals":[{"day":"PO","name":"Test","items":[]}],
                   "nakup_spolu":"1,00","bezne":"2,00","usetris":"1,00"}
    }

def test_rejects_previous_week():
    data = payload(); data["week"] = "2026-08-10"
    with pytest.raises(ValueError, match="aktuálny týždeň"):
        validate_landing_data(data, date(2026,8,18))

def test_rejects_wrong_savings_math():
    data = payload(); data["receipt"]["usetris"] = "0,50"
    with pytest.raises(ValueError, match="úspora"):
        validate_landing_data(data, date(2026,8,18))
~~~

- [ ] **Step 2: Verify RED.**

~~~powershell
py -m pytest tests/test_landing_data.py -v
~~~

Expected: import error because `app.landing_data` does not exist.

- [ ] **Step 3: Implement validation and atomic write.**

`app/landing_data.py` must require the exact current Monday, non-empty meals and cents math `bezne - nakup_spolu == usetris`. Write only to `path.with_suffix('.tmp')`, flush, fsync and `os.replace`. It must never produce HTML.

- [ ] **Step 4: Refactor producer and API.**

In `refresh_blocek.py`, keep flyer collection and composition but replace HTML `re.sub` with validated JSON output to `/var/lib/uvarsi/landing_data.json`. Leave the last valid JSON untouched on model or validation failure.

Add in `server.py`:

~~~python
@app.get("/api/public/landing")
def public_landing():
    try:
        return validate_landing_data(load_landing_data(), datetime.date.today())
    except (FileNotFoundError, ValueError):
        raise HTTPException(503, "Aktuálne letákové dáta sa obnovujú.")
~~~

- [ ] **Step 5: Change the landing.**

Replace hardcoded receipt/model example with an empty region marked `aria-live="polite"`. Fetch `/api/public/landing`; render only after a 200 response. On 503 show `Aktuálne letákové dáta práve obnovujeme.` and hide money-saving claims.

- [ ] **Step 6: Verify and commit.**

~~~powershell
py -m pytest tests/test_landing_data.py tests/test_server.py -v
git add app/landing_data.py app/server.py hetzner/refresh_blocek.py index.html tests/test_landing_data.py tests/test_server.py
git commit -m "feat: serve validated dynamic landing data"
~~~

## Task 4: Make the dozorca and healthcheck prove the same week

**Files:**
- Create: `tests/test_dozorca_contract.py`, `scripts/release_check.py`, `tests/test_release_check.py`
- Modify: `hetzner/dozorca.sh`, `app/server.py`

**Interfaces:** `landing_data_is_current(path, today=None) -> bool`; `GET /api/health`; `release_check.py --url --release-id --week`.

- [ ] **Step 1: Write failing dozorca contract test.**

First make the shell script testable by allowing these environment overrides while keeping the existing values as defaults:

~~~bash
DIR="${UVARSI_DIR:-/opt/uvarsi}"
LANDING_DATA="${UVARSI_LANDING_DATA:-/var/lib/uvarsi/landing_data.json}"
PY="${UVARSI_PY:-$DIR/venv/bin/python}"
TODAY="${UVARSI_TODAY:-$(date +%F)}"
~~~

Then create `tests/test_dozorca_contract.py`. It uses temporary executable files: fake `sqlite3` prints `30`; fake Python records its arguments in `called.txt`. Run the real shell script twice with `UVARSI_TODAY=2026-08-18`:

~~~python
from datetime import date
from pathlib import Path
from app.landing_data import landing_data_is_current

def test_stale_landing_data_runs_refresh_once(tmp_path):
    path = tmp_path / "landing_data.json"
    path.write_text('{"week":"2026-08-10"}', encoding="utf-8")
    assert landing_data_is_current(path, date(2026, 8, 18)) is False

def test_current_landing_data_skips_refresh(tmp_path):
    path = tmp_path / "landing_data.json"
    path.write_text('{"week":"2026-08-17"}', encoding="utf-8")
    assert landing_data_is_current(path, date(2026, 8, 18)) is True

def test_dozorca_delegates_to_tested_freshness_helper():
    script = Path("hetzner/dozorca.sh").read_text(encoding="utf-8")
    assert "landing_data_is_current" in script
    assert "grep -qF" not in script
~~~

- [ ] **Step 2: Verify RED.**

~~~powershell
py -m pytest tests/test_dozorca_contract.py -v
~~~

Expected: current dozorca checks text in `index.html`, not structured JSON; the helper import also does not yet exist.

- [ ] **Step 3: Refactor only freshness check.**

Replace `PAGE=/var/www/uvarsi/index.html` and `grep -qF` with `LANDING_DATA=/var/lib/uvarsi/landing_data.json`. Check the JSON week only through the tested helper with:

~~~bash
"$PY" -c 'from app.landing_data import landing_data_is_current; import sys; raise SystemExit(0 if landing_data_is_current(sys.argv[1]) else 1)' "$LANDING_DATA"
~~~

Pass `$LANDING_DATA` to refresh. Preserve the six-attempt and ntfy policy.

- [ ] **Step 4: Add failing health tests.**

A current DB with stale landing JSON returns 503. Current DB plus current landing JSON returns 200 with `status=ok`, `release_id`, `expected_week`, `deals_count`, `landing_week`.

- [ ] **Step 5: Implement health and release checker.**

`/api/health` reads only non-sensitive facts. `scripts/release_check.py` uses standard-library `urllib.request`, fetches `<url>/api/health`, compares release ID and week and exits 0 only on exact match.

- [ ] **Step 6: Verify and commit.**

~~~powershell
py -m pytest tests/test_dozorca_contract.py tests/test_release_check.py tests/test_server.py -v
git add hetzner/dozorca.sh app/server.py scripts/release_check.py tests
git commit -m "feat: add production freshness health gate"
~~~

## Task 5: Replace unsafe deploy with verified Uvar-only deploy

**Files:**
- Modify: `nasad.ps1`
- Create: `tests/test_deploy_contract.ps1`

**Interfaces:** Deployment prints release ID; exits non-zero on transfer, hash, health or cron failure; it never rewrites Caddy or Taktik data.

- [ ] **Step 1: Write failing PowerShell contract checks.**

The test reads `nasad.ps1` and asserts it contains:

~~~text
$ErrorActionPreference = 'Stop'
/var/lib/uvarsi
crontab -l
scripts/release_check.py
sha256sum
caddy validate
~~~

It also asserts it does not contain `caddyfix.py`, `taktik-mapa` or `$ErrorActionPreference = "Continue"`.

- [ ] **Step 2: Verify RED.**

~~~powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\tests\test_deploy_contract.ps1
~~~

Expected: current deploy is permissive and rewrites Caddy.

- [ ] **Step 3: Refactor deployment.**

Required behavior:

1. Set `$ErrorActionPreference = 'Stop'`.
2. Upload `VERSION`, code, static assets and scripts, but never `landing_data.json`.
3. Create `/var/lib/uvarsi` with restrictive permissions.
4. Ensure exactly one Uvar cron line exists; preserve all other lines.
5. Set `UVARSI_URL=https://uvar.si` only if absent and never print the env file.
6. Do not rewrite Caddy. Run only `caddy validate`; fail if invalid.
7. Upload a release manifest with SHA-256 for `server.py`, `dozorca.sh`, `refresh_blocek.py`, `index.html`.
8. Restart only `uvarsi`, compare remote hashes and run `scripts/release_check.py`.
9. On failure restore only a timestamped backup of the Uvar files created by this deploy, print `RELEASE FAILED` and exit non-zero.

- [ ] **Step 4: Verify syntax, contract and commit.**

~~~powershell
powershell -NoProfile -Command "[ScriptBlock]::Create((Get-Content .\nasad.ps1 -Raw)) | Out-Null"
powershell -NoProfile -ExecutionPolicy Bypass -File .\tests\test_deploy_contract.ps1
git add nasad.ps1 tests/test_deploy_contract.ps1
git commit -m "feat: verify Uvar deployment before success"
~~~

## Task 6: Align the public promise and add legal minimum

**Files:**
- Modify: `index.html:387-398,461-475`
- Create: `privacy.html`, `terms.html`, `tests/test_public_claims.py`

**Interfaces:** Footer has legal links. Landing contains no unimplemented functionality claim.

- [ ] **Step 1: Write failing claim tests.**

~~~python
from pathlib import Path

def test_landing_has_no_unimplemented_claims():
    html = Path("index.html").read_text(encoding="utf-8").lower()
    assert "alergie" not in html
    assert "povie ti aj kedy nakúpiť" not in html

def test_landing_links_legal_pages():
    html = Path("index.html").read_text(encoding="utf-8")
    assert 'href="/privacy.html"' in html
    assert 'href="/terms.html"' in html
~~~

- [ ] **Step 2: Verify RED.**

~~~powershell
py -m pytest tests/test_public_claims.py -v
~~~

Expected: unsupported claims remain and legal links do not exist.

- [ ] **Step 3: Write truthful copy and legal pages.**

Use only working benefits: current-flyer plan, pantry, recipes and grouped list. Add Slovak privacy/terms pages with controller, contact, purpose, processors (Hetzner, Resend, MailerLite, Anthropic), retention, rights, deletion request and a clear warning that individual store terms and validity apply.

- [ ] **Step 4: Verify and commit.**

~~~powershell
py -m pytest tests/test_public_claims.py -v
git add index.html privacy.html terms.html tests/test_public_claims.py
git commit -m "fix: align public promise with live capabilities"
~~~

## Task 7: Release rehearsal and live acceptance

**Files:**
- Create: `docs/releases/2026.08.18.1.md`

- [ ] **Step 1: Run all local tests.**

~~~powershell
py -m pytest -v
~~~

Expected: all pass without a network call or API key.

- [ ] **Step 2: Deploy once.**

~~~powershell
powershell -ExecutionPolicy Bypass -File "$env:USERPROFILE\OneDrive\Online produkt\nasad.ps1"
~~~

Expected: matching remote hashes, exactly one Uvar cron line, `https://uvar.si/api/health` returns 200 and both DB and landing JSON use current Monday.

- [ ] **Step 3: Smoke test on phone and desktop.**

1. Open `https://uvar.si`: current week only, no stale price or unsupported claim.
2. Open `https://uvar.si/app`: login request works.
3. Request a test magic link: it begins `https://uvar.si/prihlasenie?token=`.
4. Complete onboarding and generate a current-week plan.
5. Do not enable payments in this release.

- [ ] **Step 4: Record evidence and commit.**

Document release ID, commit, test output summary, remote hash result, health result and exact rollback command. Do not record token, e-mail, secret or full server log.

~~~powershell
git add docs/releases
git commit -m "docs: record verified Uvar release 2026.08.18.1"
~~~

## Plan self-review

- **Spec coverage:** Tasks 1–7 cover versioned source, proof gates, dynamic/static separation, current-week enforcement, branded URL, cron verification, health checks, public truthfulness and safe rollback.
- **Deliberate scope limit:** Product-level validity windows, unit-price comparison, payments and full entitlement logic require a second plan after this release passes.
- **Consistency:** Every later consumer uses contracts introduced earlier: `current_monday`, `validate_landing_data`, `/api/health` and the release ID.
