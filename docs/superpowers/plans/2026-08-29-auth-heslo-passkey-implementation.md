# Heslo a Passkey Authentication Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Nahradiť každodenné prihlasovanie magic linkom bežným účtom s heslom, zachovať bezpečné potvrdenie e-mailu a pridať voliteľný Passkey/biometriu bez odhlasovania ostatných zariadení.

**Architecture:** Autentifikácia ostane v jednom FastAPI procese a jednej SQLite databáze. Nové tabuľky sa pridajú idempotentnou migráciou; heslá budú Argon2id, relácie viac-zariadenové s 90-dňovou posuvnou expiráciou a Passkey bude WebAuthn doplnok s heslom ako trvalým fallbackom. UI sa zapne až po serverových testoch a produkčnom smoke teste za feature flagom.

**Tech Stack:** Python 3.12, FastAPI, SQLite WAL, `argon2-cffi`, `webauthn`, vanilla JavaScript PWA, pytest, Caddy/systemd.

**Spec:** `docs/superpowers/specs/2026-08-29-auth-heslo-passkey-design.md`

## Global Constraints

- Platby ostávajú vypnuté.
- Migrácia je výhradne aditívna; nemaže používateľov, nároky, plány ani existujúce relácie.
- E-mail, heslo, raw session token, reset token, challenge ani WebAuthn credential sa nesmú dostať do logov.
- Prihlásenie na novom zariadení nesmie zrušiť relácie na ostatných zariadeniach.
- Passkey je voliteľný; heslo musí fungovať vždy.
- Produkčný UI sa zapne až po úspešnom zálohovaní DB, migrácii, health checku a smoke teste.

---

## Task 1: Pin authentication dependencies and deployment preflight

**Files:**
- Modify: `requirements-dev.txt`
- Create: `requirements-auth.txt`
- Modify: `hetzner/samopull.sh`
- Test: `tests/test_auth_deployment_contract.py`

- [ ] Write a failing deployment-contract test asserting that `requirements-auth.txt` pins `argon2-cffi` and `webauthn`, and that `samopull.sh` refuses a release when either module cannot import.
- [ ] Run `pytest -q tests/test_auth_deployment_contract.py`; expect RED because the file and preflight do not exist.
- [ ] Add exact pinned versions to both development and auth production requirement files. Add a pre-switch import probe:

```bash
if ! "$PY" -c "import argon2, webauthn" >/dev/null 2>&1; then
  log "auth závislosti chýbajú — vydanie NEPREPÍNAM"
  exit 1
fi
```

- [ ] Run the focused test; expect GREEN.
- [ ] Commit only these files: `build: pin password and passkey dependencies`.

## Task 2: Add the additive auth-v3 schema

**Files:**
- Modify: `app/auth_data.py`
- Test: `tests/test_auth.py`

- [ ] Add failing migration tests proving repeated migration is safe and existing `pouzivatelia`, `naroky`, `magic_tokens_v2` and `sessions_v2` rows survive.
- [ ] Add schema assertions for `auth_credentials`, `auth_action_tokens`, `auth_passkeys`, `auth_webauthn_challenges`, plus additive session columns `last_seen_at`, `device_name`, `revoked_at`.
- [ ] Run the migration tests; expect RED.
- [ ] Implement `migrate_auth_schema()` using `CREATE TABLE IF NOT EXISTS` and `PRAGMA table_info(sessions_v2)` guarded `ALTER TABLE` statements. Use these persistence fields:

```sql
CREATE TABLE IF NOT EXISTS auth_credentials (
  user_id INTEGER PRIMARY KEY,
  password_hash TEXT NOT NULL,
  changed_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS auth_action_tokens (
  token_hash TEXT PRIMARY KEY,
  email TEXT NOT NULL,
  purpose TEXT NOT NULL CHECK(purpose IN ('confirm','reset','setup')),
  pending_password_hash TEXT,
  expires_at REAL NOT NULL,
  created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS auth_passkeys (
  credential_id TEXT PRIMARY KEY,
  user_id INTEGER NOT NULL,
  public_key BLOB NOT NULL,
  sign_count INTEGER NOT NULL,
  transports TEXT NOT NULL DEFAULT '[]',
  name TEXT NOT NULL,
  created_at REAL NOT NULL,
  last_used_at REAL
);
CREATE TABLE IF NOT EXISTS auth_webauthn_challenges (
  challenge_hash TEXT PRIMARY KEY,
  user_id INTEGER,
  purpose TEXT NOT NULL CHECK(purpose IN ('register','login')),
  expires_at REAL NOT NULL,
  created_at REAL NOT NULL
);
```

- [ ] Run focused migration tests; expect GREEN.
- [ ] Commit: `feat(auth): add additive account schema`.

## Task 3: Implement password and one-time action-token primitives

**Files:**
- Modify: `app/auth_data.py`
- Test: `tests/test_auth.py`

- [ ] Add failing unit tests for 10–128 character Unicode passwords, no trimming, Argon2id hashes, generic failed verification, one-time 24-hour confirmation token and one-time 60-minute reset/setup token.
- [ ] Run only new primitive tests; expect RED.
- [ ] Implement typed boundaries with these signatures:

```python
def validate_password(value: object) -> str: ...
def hash_password(password: str) -> str: ...
def verify_password(encoded: str, password: str) -> bool: ...
def create_action_token(con, *, email: str, purpose: str, now: float,
                        pending_password_hash: str | None = None) -> str: ...
def consume_action_token(con, *, raw_token: str, purpose: str,
                         now: float) -> dict: ...
def set_password(con, *, user_id: int, password_hash: str, now: float) -> None: ...
def authenticate_password(con, *, email: str, password: str) -> int | None: ...
```

- [ ] Use `argon2.PasswordHasher` with parameters benchmarked in Task 10; never branch user-visible output on account existence.
- [ ] Run focused tests; expect GREEN.
- [ ] Commit: `feat(auth): add password credentials and action tokens`.

## Task 4: Replace single-device rotation with sliding multi-device sessions

**Files:**
- Modify: `app/auth_data.py`
- Modify: `app/server.py`
- Test: `tests/test_auth.py`

- [ ] Add failing tests that two logins create two valid sessions, logout removes only current session, password reset removes all other sessions, expired sessions fail, and an active session extends at most once per 24 hours to 90 days.
- [ ] Run focused session tests; expect RED and confirm the current `DELETE FROM sessions_v2 WHERE user_id=?` is caught.
- [ ] Add:

```python
SESSION_TTL_SECONDS = 90 * 24 * 60 * 60
SESSION_TOUCH_SECONDS = 24 * 60 * 60

def create_session(con, *, user_id: int, now: float, device_name: str) -> str: ...
def user_for_session(con, *, raw_session: str, now: float, touch: bool = True): ...
def list_sessions(con, *, user_id: int, current_token: str, now: float) -> list[dict]: ...
def revoke_session(con, *, user_id: int, session_hash: str) -> bool: ...
def revoke_other_sessions(con, *, user_id: int, current_token: str | None) -> None: ...
```

- [ ] Remove global session deletion from `consume_magic_token`; route all new sessions through `create_session()`.
- [ ] Set cookie max-age to 90 days, retaining `Secure`, `HttpOnly`, `SameSite=Lax`.
- [ ] Run focused tests; expect GREEN.
- [ ] Commit: `fix(auth): support safe multi-device sessions`.

## Task 5: Add registration, confirmation, login and reset APIs

**Files:**
- Modify: `app/server.py`
- Modify: `app/auth_data.py`
- Test: `tests/test_auth.py`

- [ ] Add failing route tests for all public password endpoints, scanner-safe confirmation, generic login/reset responses, malformed JSON, rate limits, origin enforcement and no secret leakage under provider/network failures.
- [ ] Run the new route tests; expect RED.
- [ ] Implement:

```text
POST /api/auth/register
POST /api/auth/confirm
POST /api/auth/login
POST /api/auth/password/request
POST /api/auth/password/reset
POST /api/auth/password/set
POST /api/auth/password/change
GET  /api/auth/sessions
DELETE /api/auth/sessions/{session_hash}
POST /api/auth/sessions/logout-others
```

- [ ] Keep `/api/auth/request` and `/api/auth/verify` only for existing-account setup during migration; do not expose them as the normal login path.
- [ ] Confirmation link must open a page and require an explicit POST button before creating the account.
- [ ] Apply per-IP and normalized-account limits to register, login, reset and challenge creation.
- [ ] Run focused tests; expect GREEN.
- [ ] Commit: `feat(auth): add password account lifecycle`.

## Task 6: Add WebAuthn persistence and verification

**Files:**
- Modify: `app/auth_data.py`
- Modify: `app/server.py`
- Test: `tests/test_auth_passkey.py`

- [ ] Write failing tests for RP ID `uvar.si`, origin `https://uvar.si`, `userVerification=required`, one-time five-minute challenges, replay rejection, sign-counter update, multiple credentials, ownership checks and unsupported/malformed assertions.
- [ ] Run `pytest -q tests/test_auth_passkey.py`; expect RED.
- [ ] Implement the four ceremony routes and management routes:

```text
POST   /api/auth/passkey/register/options
POST   /api/auth/passkey/register/verify
POST   /api/auth/passkey/login/options
POST   /api/auth/passkey/login/verify
GET    /api/auth/passkeys
DELETE /api/auth/passkeys/{credential_id}
```

- [ ] Store only public credential material and metadata; challenge lookup uses a SHA-256 hash and consumes the row transactionally.
- [ ] Run passkey tests; expect GREEN.
- [ ] Commit: `feat(auth): add optional passkey authentication`.

## Task 7: Replace the magic-link UI with account screens

**Files:**
- Modify: `app/static/app.html`
- Modify: `app/server.py`
- Test: `tests/test_auth.py`
- Test: `tests/test_app_frontend_contract.py`

- [ ] Add failing HTML/Node contract tests for registration, login, forgotten password, explicit confirmation, set-password migration and safe loading/error states.
- [ ] Assert password inputs use `autocomplete="new-password"` or `current-password`, buttons are guarded against double submission, errors are Slovak and no secret enters query parameters/localStorage.
- [ ] Run focused UI tests; expect RED.
- [ ] Implement a compact first screen with tabs `Prihlásiť sa` / `Vytvoriť účet`, password visibility toggle, reset flow and clear confirmation status.
- [ ] Preserve the current session on rollout; if `/api/me` says `password_configured:false`, show a non-blocking setup card under `Ja`.
- [ ] Run focused tests; expect GREEN.
- [ ] Commit: `feat(auth): replace magic-link login with accounts`.

## Task 8: Add optional Passkey and device-management UI

**Files:**
- Modify: `app/static/app.html`
- Test: `tests/test_app_frontend_contract.py`

- [ ] Add failing Node tests that hide Passkey when `window.PublicKeyCredential` is absent, preserve password login after ceremony errors, base64url-convert binary fields correctly and allow current/other device revocation.
- [ ] Run focused tests; expect RED.
- [ ] Add `Prihlásiť biometriou` only on supported clients and `Pridať biometriu/Passkey` plus device list under `Ja`.
- [ ] Never label stored server data as fingerprint/face; UI copy says the biometric check stays in the device.
- [ ] Run focused tests; expect GREEN.
- [ ] Commit: `feat(auth): add passkey and device controls`.

## Task 9: Feature flag, migration compatibility and release tests

**Files:**
- Modify: `app/server.py`
- Modify: `app/static/app.html`
- Modify: `tests/test_auth.py`
- Modify: `tests/test_server.py`
- Modify: `VERSION`

- [ ] Add failing tests for `UVARSI_AUTH_V3=0`: existing sessions and setup links still work, while new UI remains hidden. Add tests for `=1`: password and Passkey APIs are exposed and magic login is no longer the primary UI.
- [ ] Implement server-first flag handling and expose only boolean capability fields from `/api/me`.
- [ ] Run `pytest -q tests/test_auth.py tests/test_auth_passkey.py tests/test_app_frontend_contract.py tests/test_server.py`; expect GREEN.
- [ ] Run the full suite: `pytest -q`; expect zero failures.
- [ ] Bump `VERSION` and commit: `release: stage account authentication behind flag`.

## Task 10: Production benchmark and guarded activation

**Files:**
- Create: `nastroje/over_auth_v3.ps1`
- Modify: `docs/prevadzka.md`

- [ ] Add a read-only/local smoke helper that checks register response shape, login, two simultaneous sessions, `/api/me`, current-session logout and password fallback without printing credentials.
- [ ] Install `requirements-auth.txt` into `/opt/uvarsi/venv` before pushing the flagged release; benchmark Argon2 on the Hetzner host and select parameters targeting roughly 150–350 ms without memory pressure.
- [ ] Back up the SQLite DB and record row counts for users, entitlements, sessions and plans.
- [ ] Deploy backend with `UVARSI_AUTH_V3=0`; verify health, worker heartbeat, existing login and second hosted app.
- [ ] Set `UVARSI_AUTH_V3=1`, restart only `uvarsi`, run smoke tests on desktop and mobile, then verify old user session remains valid.
- [ ] If any check fails, switch the flag off; do not roll back or rewrite the migrated DB.
- [ ] Commit operational docs/script: `ops: add auth v3 rollout verification`.

## Final Verification

- [ ] Run `pytest -q` and record total passing tests.
- [ ] Run `rg -n "TODO|TBD|plaintext|localStorage.*password|DELETE FROM sessions_v2 WHERE user_id" app tests hetzner` and resolve every auth-related match.
- [ ] Verify password login on two devices, Passkey on one supported phone, reset flow, logout-current and logout-others.
- [ ] Verify `/api/health`, plan worker heartbeat, landing, PWA and `taktik-mapa` are unaffected.
- [ ] Request independent code review before pushing to `origin/main`.
