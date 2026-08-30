#!/usr/bin/env python3
"""
Uvar.si — backend (FastAPI + SQLite).

Účty bez hesiel (magic link e-mailom), profil, špajza, generovanie osobného
týždenného plánu z databázy akcií (naplní ju zbierac_akcii.py raz týždenne).

Beh:  /opt/uvarsi/venv/bin/python -m uvicorn server:app --host 127.0.0.1 --port 8090
Závislosti: fastapi uvicorn itsdangerous
"""
import asyncio
import logging
import os, re, json, sqlite3, datetime, threading, time, hashlib
from contextlib import asynccontextmanager, closing
from urllib.parse import urlsplit

import anyio.to_thread
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse, FileResponse, HTMLResponse, Response, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from webauthn import (
    generate_authentication_options,
    generate_registration_options,
    options_to_json,
    verify_authentication_response,
    verify_registration_response,
)
from webauthn.helpers import base64url_to_bytes, bytes_to_base64url
from webauthn.helpers.exceptions import (
    InvalidAuthenticationResponse,
    InvalidRegistrationResponse,
)
from webauthn.helpers.structs import (
    AuthenticatorSelectionCriteria,
    AuthenticatorTransport,
    PublicKeyCredentialDescriptor,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)
import db_rezim
import naklady
import plan_jobs
import predpocet
from config import public_base_url, admin_emails, release_id
from landing_data import load_landing_data, validate_landing_data
from public_pages import ROBOTS_TXT, render_evergreen_page, render_sitemap, render_weekly_page
from weekly_data import offers_for_current_week, stores_missing_this_week
from offer_data import OfferKeyCollision, migrate_akcie_schema
from plan_shortlist import select_offers
from plan_jobs import JobRequest
from plan_calendar import bratislava_day
from plan_data import (
    PLAN_ALGO_VERSION,
    PlanDiversityError,
    PORTION_STANDARD_VERSION,
    apply_pantry_to_shopping_list,
    build_personal_plan,
    cached_plan_is_current,
    measurable_offers,
    personal_plan_messages,
    plan_output_config,
    plan_signature,
    plan_variant_for,
    plan_without_pantry,
)
from platby import (
    AKCIA_IGNOROVANE,
    AKCIA_NAD_KAPACITU,
    AKCIA_ODLOZENE,
    DRUH_DUPLICITA,
    DRUH_IGNOROVANE,
    DRUH_NAD_KAPACITU,
    DRUH_NEPOUZITELNA,
    DRUH_ODLOZENE,
    MAIL_PREDMET_DUPLICITA,
    MAIL_PREDMET_NAD_KAPACITU,
    MAX_TELO_WEBHOOKU,
    PlatbyNenastavene,
    SPRAVA_DUPLICITA_ZAKAZNIK,
    SPRAVA_NAD_KAPACITU_ZAKAZNIK,
    SPRAVA_NEPLATNY_PODPIS,
    SPRAVA_VELKE_TELO,
    SPRAVA_NENASTAVENE,
    SPRAVA_NEPRIRADITELNA,
    SPRAVA_POKAZENE_TELO,
    SPRAVA_UZ_MAS,
    SPRAVA_VYPNUTE,
    SPRAVA_VYPREDANE,
    STAV_DUPLICITNY,
    UdalostNepouzitelna,
    checkout_url,
    email_uctu,
    hodnoverny_podpis,
    ma_narok,
    migrate_platby_schema,
    odloz_webhook,
    overit_podpis,
    platby_zapnute,
    pocet_cakajucich,
    spracuj_udalost,
    stav_dozoru,
    stav_platieb,
    upozornenie_raz,
    volne_miesta,
)
from auth_data import (
    ActionTokenExpired,
    ActionTokenInvalid,
    ClientIpRateLimiter,
    DeliveryError,
    EmailCooldown,
    EmailRequestInProgress,
    MagicTokenExpired,
    MagicTokenInvalid,
    PasskeyCloneDetected,
    ReservationInvalid,
    WebAuthnChallengeExpired,
    WebAuthnChallengeInvalid,
    PASSWORD_ACTION_TOKEN_TTL_SECONDS,
    PASSWORDLESS_CREDENTIAL_VERSION,
    SESSION_TTL_SECONDS,
    cancel_magic_token_reservation,
    consume_action_token,
    consume_magic_token,
    consume_webauthn_challenge,
    create_action_token,
    create_session,
    create_webauthn_challenge,
    claim_password_reset_job,
    cleanup_auth_records,
    delete_passkey,
    delete_session,
    delete_setup_session,
    enqueue_password_reset_job,
    finish_password_reset_job,
    hash_password,
    list_passkeys,
    list_sessions,
    migrate_auth_schema,
    normalize_email,
    password_authentication_material,
    password_reset_outbox_next_wake,
    passkey_for_credential,
    promote_magic_token,
    reserve_magic_token,
    revoke_other_sessions,
    revoke_session,
    send_resend_message,
    set_password,
    store_passkey,
    token_hash,
    user_for_session,
    user_for_setup_session,
    update_passkey_use,
    validate_password,
    verify_password_and_rehash,
)

DB = os.environ.get("UVARSI_DB", "/opt/uvarsi/uvarsi.db")
STATIC = os.environ.get("UVARSI_STATIC", "/opt/uvarsi/app/static")
LANDING_DATA = os.environ.get("UVARSI_LANDING_DATA", "/var/lib/uvarsi/landing_data.json")
BASE_URL = public_base_url()
PUBLIC_CACHE_CONTROL = "public, max-age=300, must-revalidate"
PRIVATE_CACHE_CONTROL = "private, no-store"
NOINDEX_HEADER = "noindex, nofollow, noarchive"
RETRY_AFTER_PUBLIC_DATA = "900"
COMMUNITY_GOAL = 250
COMMUNITY_VISIBILITY_THRESHOLD = 10
ENV_FILE = "/opt/uvarsi/uvarsi.env"
COOKIE = "uvarsi_session"
SETUP_COOKIE = "uvarsi_setup"
SESSION_MAX_AGE = SESSION_TTL_SECONDS
SETUP_MAX_AGE = PASSWORD_ACTION_TOKEN_TTL_SECONDS
AUTH_CLOCK = time.time
# Single-worker beta guard only; the deployed edge still needs a shared limiter.
IP_REQUEST_LIMITER = ClientIpRateLimiter(
    max_requests=5, window_seconds=10 * 60, max_clients=10_000
)
AUTH_V3_IP_LIMITER = ClientIpRateLimiter(
    max_requests=5, window_seconds=10 * 60, max_clients=10_000
)
AUTH_V3_ACCOUNT_LIMITER = ClientIpRateLimiter(
    max_requests=5, window_seconds=10 * 60, max_clients=50_000
)
AUTH_KDF_CONCURRENCY = 2
AUTH_KDF_GATE = asyncio.BoundedSemaphore(AUTH_KDF_CONCURRENCY)
MAX_AUTH_JSON_BYTES = 16 * 1024
AUTH_BACKGROUND_TASKS: set[asyncio.Task] = set()
AUTH_OUTBOX_BATCH_SIZE = 1
AUTH_OUTBOX_WORKER_ID = f"auth-reset-{os.getpid()}-{id(AUTH_BACKGROUND_TASKS)}"
AUTH_OUTBOX_SHUTTING_DOWN = False
AUTH_OUTBOX_SHUTDOWN_DEADLINE = 1.0
AUTH_OUTBOX_PROVIDER_RETRY_SECONDS = 60.0
AUTH_OUTBOX_WAKE_HANDLES = {}
AUTH_OUTBOX_CALL_LATER = lambda loop, delay, callback: loop.call_later(
    delay, callback
)

AUTH_SUCCESS_MESSAGE = (
    "Poskytovateľ prijal žiadosť o prihlasovací e-mail. "
    "Odkaz bude platný 60 minút."
)
AUTH_PROVIDER_FAILURE_MESSAGE = (
    "Prihlasovací e-mail sa teraz nepodarilo odovzdať poskytovateľovi. "
    "Skús to znova o chvíľu."
)
ACCOUNT_REQUEST_MESSAGE = (
    "Ak je možné túto adresu použiť, poslali sme na ňu ďalší bezpečný krok."
)
PASSWORD_REQUEST_MESSAGE = (
    "Ak účet s touto adresou existuje, poslali sme pokyny na nastavenie hesla."
)
PASSWORD_LOGIN_FAILURE_MESSAGE = "E-mail alebo heslo nie sú správne."
PASSKEY_FAILURE_MESSAGE = (
    "Passkey sa nepodarilo overiť. Použi heslo alebo skús znova."
)
PASSKEY_RP_ID = "uvar.si"
PASSKEY_ORIGIN = "https://uvar.si"
PASSKEY_OPTIONS_TIMEOUT_MS = 5 * 60 * 1000
ACCOUNT_PROVIDER_FAILURE_MESSAGE = (
    "E-mail sa teraz nepodarilo odovzdať poskytovateľovi. Skús to znova o chvíľu."
)

LOG = logging.getLogger("uvarsi.plan")

MODEL_PLAN = "claude-sonnet-5"     # skladanie plánu = text, lacné
# Nameraný najhorší prípad odpovede: 7 jedál × 6 podrobných krokov × 4 položky
# je o 40 % dlhší než pôvodných päť, preto 3 640 tokenov. max_tokens ohraničuje
# uvažovanie AJ odpoveď dokopy, takže
# strop musí nechať odpovedi dvojnásobnú rezervu — orezaný JSON už raz appku
# zhodil a stálo to platené volanie navyše.
PLAN_ODPOVED_TOKENY = 3640
PLAN_TOKENS = 10_000
# Produkčné meranie 27. 8. 2026: predvolená námaha päťkrát minula celý strop
# a odrezala JSON pred koncom. Výber je už ohraničený katalógom a výsledok
# následne overuje server, preto je low správny kompromis: kratšie uvažovanie,
# nižšia cena a hlavne dokončená odpoveď. max_tokens ostáva iba bezpečnostný
# strop; neznamená, že sa všetkých 10 000 tokenov pri každom pláne minie.
PLAN_EFFORT = "low"
# Najhorší prípad čakania musí byť jedno číslo, nie súčin skrytých pokusov:
# samotný timeout bez max_retries nechá SDK opakovať volanie (default 2×), takže
# jedno zaseknuté spojenie drží používateľa aj vyše šesť minút pri točiacom sa
# koliesku. Preto: žiadne tiché opakovanie, jeden pokus a jasná hláška.
PLAN_TIMEOUT_SECONDS = 120.0
PLAN_MAX_RETRIES = 0
PLAN_WORST_CASE_SECONDS = PLAN_TIMEOUT_SECONDS * (PLAN_MAX_RETRIES + 1)
MODEL_VALIDATION_ATTEMPTS = 2
SPRAVA_PLAN_TRVA_PRIDLHO = (
    "Jedálniček sa nestihol poskladať do dvoch minút. Skús to prosím znova."
)
SPRAVA_PLAN_NEDOKONCENY = (
    "Jedálniček sa nestihol dopísať do konca. Skús to prosím znova."
)

MIN_OFFERS_FOR_PLAN = 15
# Rovnaký profil dostane rovnaký plán, takže sa počíta raz pre všetkých. Aby
# však susedia s rovnakou domácnosťou nemali bajt na bajt to isté menu, podpis
# sa delí na túto malú sadu variantov. PLAN_VARIANTS=1 = maximálne zdieľanie.
PLAN_VARIANTS = 3
PLAN_JOB_PRIORITY_LIVE = 100
PLAN_JOB_RETRY_AFTER = 4
SPRAVA_PLAN_PRIPRAVUJEME = "Plán pripravujeme. Pokojne pokračuj inde."
SPRAVA_PLAN_ZLYHAL = "Plán sa nepodarilo pripraviť. Skús to znova."
KOD_PLAN_ZLYHAL = "plan_failed"
PLAN_JOB_NON_RETRYABLE_CODES = {
    naklady.KOD_KREDIT,
    "incomplete_stores",
    "invalid_profile",
    "plan_result_missing",
    "stale_algorithm",
    "stale_context",
    "stale_pantry",
    "stale_signature",
    "stale_week",
}


# ---------------------------------------------------------------- env / util
def env(key, default=None):
    if os.environ.get(key):
        return os.environ[key]
    try:
        for line in open(ENV_FILE, encoding="utf-8"):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export") and len(line) > len("export") and line[len("export")].isspace():
                line = line[len("export"):].lstrip()
            if "=" not in line:
                continue
            name, value = line.split("=", 1)
            if name.strip() == key:
                return value.strip().strip('"').strip("'")
    except FileNotFoundError:
        pass
    return default


def monday(d=None):
    d = d or datetime.date.today()
    return (d - datetime.timedelta(days=d.weekday())).isoformat()


SCHEMA = """
CREATE TABLE IF NOT EXISTS pouzivatelia (
  id      INTEGER PRIMARY KEY AUTOINCREMENT,
  email   TEXT UNIQUE NOT NULL,
  vytvoreny TEXT DEFAULT CURRENT_TIMESTAMP,
  platiaci INTEGER DEFAULT 0,
  osoby   INTEGER DEFAULT 4,
  dospeli INTEGER DEFAULT 4,
  deti    INTEGER NOT NULL DEFAULT 0,
  frekvencia INTEGER DEFAULT 2,          -- variť raz za N dní
  obchody TEXT DEFAULT 'Kaufland,Tesco,Lidl',
  onboarding INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS spajza (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL,
  nazov TEXT NOT NULL,
  mnozstvo REAL NULL,
  jednotka TEXT NULL,
  CHECK (
    (mnozstvo IS NULL AND jednotka IS NULL)
    OR (
      mnozstvo IS NOT NULL
      AND jednotka IS NOT NULL
      AND typeof(mnozstvo) IN ('integer', 'real')
      AND mnozstvo > 0
      AND jednotka IN ('g', 'ml', 'piece')
    )
  )
);
CREATE TABLE IF NOT EXISTS plany (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL,
  tyzden TEXT NOT NULL,
  json TEXT NOT NULL,
  vytvoreny TEXT DEFAULT CURRENT_TIMESTAMP,
  UNIQUE(user_id, tyzden)
);
-- Plán závisí len od (týždeň, obchody, osoby, frekvencia, špajza, ponuky).
-- Dvaja ľudia s rovnakým podpisom dostanú ten istý plán, tak sa skladá raz.
-- Kľúč JE pravidlom neplatnosti: iný týždeň či iná ponuková sada = iný podpis.
CREATE TABLE IF NOT EXISTS plany_zdielane (
  podpis  TEXT NOT NULL,
  variant INTEGER NOT NULL,
  tyzden  TEXT NOT NULL,
  json    TEXT NOT NULL,
  vytvoreny TEXT DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (podpis, variant)
);
-- Denný strop skladania plánov. Jeden riadok = jeden účet a jeden deň; drží sa
-- len dnešok, staršie riadky sa pri prvej rezervácii zmažú. Bez neho je každé
-- kliknutie na „Chcem iný plán" neohraničené platené volanie modelu.
CREATE TABLE IF NOT EXISTS prepocty (
  user_id INTEGER NOT NULL,
  den     TEXT NOT NULL,
  pocet   INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (user_id, den)
);
CREATE TABLE IF NOT EXISTS tokeny (
  token TEXT PRIMARY KEY,
  email TEXT NOT NULL,
  platny_do TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sedenia (
  token TEXT PRIMARY KEY,
  user_id INTEGER NOT NULL,
  vytvorene TEXT DEFAULT CURRENT_TIMESTAMP
);
-- Zhodné so SCHEMA v zbierac_akcii.py. db() je jediný vstupný bod appky a volá
-- migrate_akcie_schema(); na chýbajúcej tabuľke vráti PRAGMA table_info(akcie)
-- nula riadkov (bez chyby) a ALTER TABLE potom padne na `no such table` —
-- na čerstvej databáze by 500-kovalo úplne všetko vrátane prihlásenia.
CREATE TABLE IF NOT EXISTS akcie (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  tyzden     TEXT NOT NULL,       -- ISO pondelok, napr. 2026-08-10
  obchod     TEXT NOT NULL,
  nazov      TEXT NOT NULL,
  kategoria  TEXT,                -- maso|zelenina|ovocie|mliecne|trvanlive|pecivo|ine
  cena       REAL,                -- akciová cena
  povodna    REAL,                -- bežná cena
  zlava      TEXT,                -- "−52 %" alebo "1+1"
  jednotka   TEXT,                -- ks|kg|l|balenie
  source_url TEXT,
  source_page INTEGER,
  valid_from TEXT,
  valid_to TEXT,
  offer_key TEXT,
  created    TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_akcie_tyzden ON akcie(tyzden);
CREATE INDEX IF NOT EXISTS idx_akcie_kat ON akcie(tyzden, kategoria);
"""


# Databázy, ktoré už v tomto procese prešli migráciami. `db()` sa volá 5–6× za
# jedno generovanie plánu a predtým zakaždým prehnalo celú schému plus štyri
# migračné funkcie — teda desiatky príkazov navyše na KAŽDEJ požiadavke.
_SCHEMA_HOTOVA: set[str] = set()


def migrate_household_schema(con) -> None:
    """Rozdeľ staré ``osoby`` na dospelých a deti bez straty kompatibility."""
    columns = {row[1] for row in con.execute("PRAGMA table_info(pouzivatelia)")}
    if "dospeli" not in columns:
        # DEFAULT ostáva dôležitý aj po migrácii: auth_data vytvára nový účet
        # iba z e-mailu a profil si človek doplní až následne.
        legacy = con.execute("SELECT id, osoby FROM pouzivatelia").fetchall()
        con.execute("ALTER TABLE pouzivatelia ADD COLUMN dospeli INTEGER DEFAULT 4")
        con.executemany(
            "UPDATE pouzivatelia SET dospeli=? WHERE id=?",
            [(int(osoby) if osoby is not None else 4, user_id) for user_id, osoby in legacy],
        )
    if "deti" not in columns:
        con.execute(
            "ALTER TABLE pouzivatelia ADD COLUMN deti INTEGER NOT NULL DEFAULT 0"
        )
    con.execute("UPDATE pouzivatelia SET dospeli=osoby WHERE dospeli IS NULL")
    con.execute("UPDATE pouzivatelia SET deti=0 WHERE deti IS NULL")


def migrate_pantry_schema(con) -> None:
    """Add optional pantry quantities and enforce their invariant in SQLite."""
    columns = {row[1] for row in con.execute("PRAGMA table_info(spajza)")}
    if "mnozstvo" not in columns:
        con.execute("ALTER TABLE spajza ADD COLUMN mnozstvo REAL NULL")
    if "jednotka" not in columns:
        con.execute("ALTER TABLE spajza ADD COLUMN jednotka TEXT NULL")
    con.executescript(
        """
        CREATE TRIGGER IF NOT EXISTS spajza_quantity_insert
        BEFORE INSERT ON spajza
        WHEN NOT (
          (NEW.mnozstvo IS NULL AND NEW.jednotka IS NULL)
          OR (
            NEW.mnozstvo IS NOT NULL
            AND NEW.jednotka IS NOT NULL
            AND typeof(NEW.mnozstvo) IN ('integer', 'real')
            AND NEW.mnozstvo > 0
            AND NEW.jednotka IN ('g', 'ml', 'piece')
          )
        )
        BEGIN
          SELECT RAISE(ABORT, 'invalid pantry quantity');
        END;

        CREATE TRIGGER IF NOT EXISTS spajza_quantity_update
        BEFORE UPDATE ON spajza
        WHEN NOT (
          (NEW.mnozstvo IS NULL AND NEW.jednotka IS NULL)
          OR (
            NEW.mnozstvo IS NOT NULL
            AND NEW.jednotka IS NOT NULL
            AND typeof(NEW.mnozstvo) IN ('integer', 'real')
            AND NEW.mnozstvo > 0
            AND NEW.jednotka IN ('g', 'ml', 'piece')
          )
        )
        BEGIN
          SELECT RAISE(ABORT, 'invalid pantry quantity');
        END;
        """
    )


def migruj_schemu(con) -> None:
    """Celá schéma a všetky migrácie. Idempotentné, ale drahé — nie na požiadavku."""
    import plan_jobs

    con.executescript(SCHEMA)
    migrate_household_schema(con)
    migrate_pantry_schema(con)
    migrate_auth_schema(con)
    migrate_akcie_schema(con)
    migrate_platby_schema(con)
    naklady.migrate_naklady_schema(con)
    plan_jobs.migrate_plan_jobs_schema(con)
    predpocet.migrate_predpocet_schema(con)
    con.commit()


def priprav_databazu(cesta=None) -> None:
    """Zmigruj databázu práve raz za proces.

    Appka to spraví pri štarte (lifespan). Poistka tu ostáva zámerne: CLI
    (`premium_cli.py`), zberač aj testy volajú `db()` bez toho, aby ktokoľvek
    lifespan spustil — a na čerstvej databáze by inak spadli na chýbajúcich
    tabuľkách. Po prvom behu je to obyčajný pohľad do množiny.
    """
    cesta = cesta or DB
    if cesta in _SCHEMA_HOTOVA:
        return
    with closing(db_rezim.otvor(cesta)) as con:
        migruj_schemu(con)
    _SCHEMA_HOTOVA.add(cesta)


def db():
    """Lacné pripojenie. Žiadne migrácie — tie prebehli raz pri štarte."""
    priprav_databazu()
    return db_rezim.otvor(DB)


# anyio má vo východiskovom stave 40 vlákien a `generuj_plan` je synchronné
# `def`, takže jedno skladanie drží vlákno až PLAN_TIMEOUT_SECONDS. Pri 40
# súbežných plánoch nemal pool voľné vlákno ani pre `/api/me` či prihlásenie —
# zamrzla celá appka. Vlákien je preto viac a plány si z nich smú vziať len časť.
PLAN_VLAKNA_STROP = 160
PLAN_SUBEZNE_MAX = 12
PLAN_MIESTA = threading.BoundedSemaphore(PLAN_SUBEZNE_MAX)
SPRAVA_PLAN_ZANEPRAZDNENY = (
    "Momentálne skladáme veľa jedálničkov naraz. Skús to prosím o minútu — "
    "dnešný prepočet ti zostáva."
)
KOD_PLAN_ZANEPRAZDNENY = "plan_zaneprazdneny"


def zvys_strop_vlakien() -> None:
    """Zdvihni anyio threadpool. Volá sa zo štartu appky, kde beží event loop."""
    try:
        anyio.to_thread.current_default_thread_limiter().total_tokens = PLAN_VLAKNA_STROP
    except Exception:                      # strop nesmie zabrániť štartu appky
        LOG.warning("strop vlákien sa nepodarilo zdvihnúť", exc_info=True)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global AUTH_OUTBOX_SHUTTING_DOWN
    priprav_databazu()
    with closing(db()) as con:
        cleanup_auth_records(con, now=AUTH_CLOCK())
    zvys_strop_vlakien()
    AUTH_OUTBOX_SHUTTING_DOWN = False
    ensure_password_reset_worker()
    try:
        yield
    finally:
        AUTH_OUTBOX_SHUTTING_DOWN = True
        for handle, _wake_at in tuple(AUTH_OUTBOX_WAKE_HANDLES.values()):
            handle.cancel()
        AUTH_OUTBOX_WAKE_HANDLES.clear()
        await drain_password_reset_workers(deadline=AUTH_OUTBOX_SHUTDOWN_DEADLINE)


app = FastAPI(title="Uvar.si", lifespan=lifespan)

AUTH_V3_PRIMARY_PATHS = frozenset(
    {
        "/api/auth/register",
        "/api/auth/confirm",
        "/api/auth/login",
        "/api/auth/password/request",
        "/api/auth/password/reset",
        "/api/auth/password/change",
        "/api/auth/pages/potvrdenie",
        "/potvrdenie",
    }
)
AUTH_V3_PRIMARY_PREFIXES = (
    "/api/auth/sessions",
    "/api/auth/passkey",
    "/api/auth/passkeys",
)


def auth_v3_enabled() -> bool:
    return env("UVARSI_AUTH_V3", "0") == "1"


def auth_v3_primary_path(path: str) -> bool:
    normalized = path.rstrip("/") or "/"
    return normalized in AUTH_V3_PRIMARY_PATHS or any(
        normalized == prefix or normalized.startswith(prefix + "/")
        for prefix in AUTH_V3_PRIMARY_PREFIXES
    )


@app.middleware("http")
async def stage_auth_v3(request: Request, call_next):
    if not auth_v3_enabled() and auth_v3_primary_path(request.url.path):
        return JSONResponse({"detail": "Nenájdené"}, status_code=404)
    return await call_next(request)


def set_session_cookie(response: Response, raw_session: str) -> None:
    response.set_cookie(
        COOKIE,
        raw_session,
        max_age=SESSION_MAX_AGE,
        httponly=True,
        samesite="lax",
        secure=True,
    )


def set_setup_cookie(response: Response, raw_session: str) -> None:
    response.set_cookie(
        SETUP_COOKIE,
        raw_session,
        max_age=SETUP_MAX_AGE,
        httponly=True,
        samesite="lax",
        secure=True,
    )


@app.middleware("http")
async def private_ui_noindex(request: Request, call_next):
    response = await call_next(request)
    renewal = getattr(request.state, "renew_session_cookie", None)
    if renewal:
        set_session_cookie(response, renewal)
    if request.url.path == "/app" or request.url.path.startswith(
        ("/prihlasenie", "/potvrdenie", "/heslo", "/api/auth/pages/")
    ):
        response.headers["Cache-Control"] = PRIVATE_CACHE_CONTROL
        response.headers["X-Robots-Tag"] = NOINDEX_HEADER
    return response


# Skrátený `offer_key` je zrážka rizika s cenou promptu. Zrážku sme vyhrali,
# ale zvyškové riziko sa nesmie prehltnúť: keby dva rôzne výrobky predsa len
# vyšli na jeden kľúč, appka by ukázala reálnu cenu pri cudzom výrobku — presne
# to, čo si tento produkt nemôže dovoliť. Radšej úprimné odmietnutie.
SPRAVA_KOLIZIA_KLUCOV = (
    "Ceny z letákov sa práve nedajú spoľahlivo priradiť k výrobkom, tak ti "
    "radšej nič neukážeme, než by sme ukázali nesprávnu cenu. Pracujeme na tom."
)


@app.exception_handler(OfferKeyCollision)
def kolizia_offer_key(req: Request, chyba: OfferKeyCollision):
    LOG.error("kolízia offer_key: %s", chyba)
    return JSONResponse({"detail": SPRAVA_KOLIZIA_KLUCOV}, status_code=503)


def user_from_request(req: Request):
    tok = req.cookies.get(COOKIE)
    if not tok:
        return None
    with closing(db()) as con:
        changes_before = con.total_changes
        user = user_for_session(con, raw_session=tok, now=AUTH_CLOCK())
        if user is not None and con.total_changes > changes_before:
            req.state.renew_session_cookie = tok
        return user


def require_user(req: Request):
    u = user_from_request(req)
    if not u:
        raise HTTPException(status_code=401, detail="Neprihlásený")
    return u


def require_owner(req: Request):
    u = require_user(req)
    email = u.get("email")
    if not isinstance(email, str) or email.strip().casefold() not in admin_emails(
        env("UVARSI_ADMIN_EMAILS", "")
    ):
        raise HTTPException(status_code=403, detail="Prístup zamietnutý")
    return u


def zlozenie_domacnosti(row):
    """Vráť kanonické (dospelí, deti) aj pre riadok zo starého klienta.

    Priamy starý zápis mohol zmeniť iba ``osoby``. Ak sú nové stĺpce ešte
    prázdne alebo s týmto súčtom nesedia, berieme pôvodný údaj ako počet
    dospelých. API pri najbližšom uložení zapíše všetky tri polia naraz.
    """
    def value(key, default=None):
        try:
            return row[key]
        except (KeyError, IndexError, TypeError):
            return default

    total = int(value("osoby", 4) or 4)
    adults = value("dospeli")
    children = value("deti")
    if adults is None or children is None:
        return total, 0
    adults, children = int(adults), int(children)
    if adults + children != total:
        return total, 0
    return adults, children


def validuj_zlozenie_domacnosti(data):
    """Nový payload používa dve polia; staré ``osoby`` znamená dospelých."""
    if "adults" in data or "children" in data:
        if "adults" not in data or "children" not in data:
            raise HTTPException(422, "Zadaj počet dospelých aj detí.")
        adults, children = data["adults"], data["children"]
    else:
        adults, children = data.get("osoby", 4), 0
    if (not isinstance(adults, int) or isinstance(adults, bool)
            or not isinstance(children, int) or isinstance(children, bool)
            or adults < 0 or children < 0 or not 1 <= adults + children <= 12):
        raise HTTPException(422, "Domácnosť musí mať spolu 1 až 12 ľudí.")
    return adults, children


# ---------------------------------------------------------------- Premium
# Špajza a vyšší denný strop prepočtov sú platené. Rozhoduje o nich výhradne
# server, a to z tabuľky `naroky` (platby.py) — teda z podpísanej udalosti
# poskytovateľa. Ani cookie, ani telo požiadavky, ani stĺpec `platiaci` nie sú
# dôkazom o platbe. Kým sú platby vypnuté, nárok nemá nikto a všetci sú zadarmo.
LIMIT_PREPOCTOV_ZDARMA = 1
LIMIT_PREPOCTOV_PREMIUM = 5

SPRAVA_SPAJZA_PREMIUM = (
    "Špajza je súčasťou Premium. V bezplatnej verzii skladáme jedálniček "
    "z akcií v tvojich obchodoch — bez toho, čo máš doma."
)
# Veta je pre človeka, kód pre appku. Bez kódu by sa obrazovka rozhodovala podľa
# reťazca, ktorý raz preformulujeme — a zámok by prestal fungovať potichu.
KOD_SPAJZA_PREMIUM = "spajza_premium"
KOD_LIMIT_PREPOCTOV = "limit_prepoctov"


def je_premium(con, user_id) -> bool:
    """Odvodené na každej požiadavke nanovo: vrátená platba platí okamžite."""
    return ma_narok(con, user_id)


def odmietni(status: int, sprava: str, kod: str, **navyse) -> JSONResponse:
    """Odmietnutie, ktoré rozumie človek aj appka.

    `detail` ostáva obyčajná veta na tom istom mieste, kde ju appka čaká pri
    každej inej chybe; `kod` je stále rovnaký identifikátor, podľa ktorého sa dá
    vykresliť zamknutý stav bez hádania z textu.
    """
    return JSONResponse({"detail": sprava, "kod": kod, **navyse}, status_code=status)


def limit_prepoctov(premium: bool) -> int:
    return LIMIT_PREPOCTOV_PREMIUM if premium else LIMIT_PREPOCTOV_ZDARMA


def spajza_pouzivatela(con, user_id, premium: bool):
    """Špajza vstupuje do plánu len platiacim.

    Zadarmo sa vracia prázdno, aj keď v tabuľke riadky sú (napr. po skončenom
    Premium). Vďaka tomu ostáva podpis plánu bez špajze — a teda zdieľaný.
    """
    if not premium:
        return []
    return [row["nazov"] for row in con.execute(
        "SELECT nazov FROM spajza WHERE user_id=? ORDER BY id", (user_id,))]


def pocet_ulozenej_spajze(con, user_id) -> int:
    """Koľko riadkov v špajzi naozaj leží — aj keď do plánu práve nevstupujú."""
    row = con.execute("SELECT COUNT(*) FROM spajza WHERE user_id=?", (user_id,)).fetchone()
    return int(row[0]) if row else 0


def sprava_o_uspanej_spajze(pocet: int):
    """Čo sa stalo so špajzou, ktorú si človek napísal ešte pred Premium.

    Nemažeme ju a netvárime sa, že tam nie je. Iba povieme pravdu: leží
    nedotknutá a do jedálnička dočasne nevstupuje. Po Premium sa vráti presne
    taká, aká bola.
    """
    if pocet <= 0:
        return None
    if pocet == 1:
        polozky = "1 položku"
    elif pocet < 5:
        polozky = f"{pocet} položky"
    else:
        polozky = f"{pocet} položiek"
    return (
        f"V špajzi máš uložených {polozky} z minulosti. Nemažeme ich — len do "
        "jedálnička teraz nevstupujú. S Premium sa zapoja späť presne tak, ako si ich napísal."
    )


def dnesok(dnes=None) -> str:
    if isinstance(dnes, datetime.datetime):
        return bratislava_day(dnes).isoformat()
    if isinstance(dnes, datetime.date):
        return dnes.isoformat()
    return bratislava_day().isoformat()


def zajtrajsok(den=None) -> str:
    """Kedy sa denný strop obnoví. ISO dátum, aby si to appka nemusela rátať."""
    zaklad = datetime.date.fromisoformat(den) if isinstance(den, str) else (den or datetime.date.today())
    return (zaklad + datetime.timedelta(days=1)).isoformat()


def pouzite_prepocty(con, user_id, den) -> int:
    row = con.execute(
        "SELECT pocet FROM prepocty WHERE user_id=? AND den=?", (user_id, den)
    ).fetchone()
    return int(row[0]) if row else 0


def rezervuj_prepocet(user_id, limit, den):
    """Zaber jedno miesto z dnešného stropu. Vráti zostatok, alebo None.

    Rezervuje sa tesne pred volaním modelu, lebo práve to volanie stojí peniaze.
    BEGIN IMMEDIATE serializuje súbežné kliknutia, takže dve otvorené záložky
    nevedia strop obísť. Plán podaný z cache sa sem vôbec nedostane.
    """
    with closing(db()) as con:
        if con.in_transaction:
            con.commit()
        con.execute("BEGIN IMMEDIATE")
        try:
            pouzite = pouzite_prepocty(con, user_id, den)
            if pouzite >= limit:
                con.rollback()
                return None
            con.execute(
                "INSERT INTO prepocty (user_id, den, pocet) VALUES (?,?,1)"
                " ON CONFLICT(user_id, den) DO UPDATE SET pocet=pocet+1",
                (user_id, den),
            )
            con.execute("DELETE FROM prepocty WHERE den<>?", (den,))
            con.commit()
        except Exception:
            if con.in_transaction:
                con.rollback()
            raise
    return limit - pouzite - 1


def vrat_prepocet(user_id, den):
    """Neúspešné skladanie sa neúčtuje — kto nedostal plán, o pokus neprišiel."""
    with closing(db()) as con:
        con.execute(
            "UPDATE prepocty SET pocet=pocet-1 WHERE user_id=? AND den=? AND pocet>0",
            (user_id, den),
        )
        con.commit()


def sprava_o_limite(limit: int, premium: bool, dnes=None) -> str:
    """Nikdy holá chyba: povie koľko, prečo a odkedy to ide znova."""
    zajtra = (dnes or datetime.date.today()) + datetime.timedelta(days=1)
    kolko = "raz za deň" if limit == 1 else f"{limit}× za deň"
    text = (
        f"Nový jedálniček si môžeš dať poskladať {kolko} a dnešok už máš vyčerpaný."
        f" Ďalší si vyžiadaj zajtra {zajtra.day}. {zajtra.month}. po polnoci —"
        " jedálniček, ktorý máš teraz, ti zostáva."
    )
    if not premium:
        text += f" S Premium je to {LIMIT_PREPOCTOV_PREMIUM}× denne."
    return text


# ---------------------------------------------------------------- e-mail
def posli_mail(
    komu: str,
    predmet: str,
    telo: str,
    html: str,
    *,
    idempotency_key: str | None = None,
):
    return send_resend_message(
        api_key=env("RESEND_API_KEY"),
        sender=env("MAIL_FROM", "Uvar.si <info@uvar.si>"),
        recipient=komu,
        subject=predmet,
        text=telo,
        html=html,
        idempotency_key=idempotency_key,
    )


# ---------------------------------------------------------------- auth
def require_auth_origin(req: Request) -> None:
    configured = urlsplit(BASE_URL)
    allowed = f"{configured.scheme}://{configured.netloc}"
    if req.headers.get("origin") != allowed:
        raise HTTPException(403, "Neplatný pôvod požiadavky.")


async def auth_json(req: Request) -> dict:
    content_length = req.headers.get("content-length")
    if content_length is not None:
        try:
            if int(content_length) > MAX_AUTH_JSON_BYTES:
                raise HTTPException(413, "Požiadavka je príliš veľká.")
        except ValueError:
            raise HTTPException(400, "Požiadavka musí obsahovať platný JSON objekt.")
    chunks = []
    size = 0
    try:
        async for chunk in req.stream():
            size += len(chunk)
            if size > MAX_AUTH_JSON_BYTES:
                raise HTTPException(413, "Požiadavka je príliš veľká.")
            chunks.append(chunk)
        data = json.loads(b"".join(chunks))
    except (ValueError, UnicodeDecodeError):
        raise HTTPException(400, "Požiadavka musí obsahovať platný JSON objekt.")
    if not isinstance(data, dict):
        raise HTTPException(400, "Požiadavka musí obsahovať platný JSON objekt.")
    return data


async def run_password_kdf(function, *args):
    """Offload one Argon2 operation under the process-local capacity bound."""
    async with AUTH_KDF_GATE:
        return await asyncio.to_thread(function, *args)


async def authenticate_password_async(
    *, email: str, password: str, now: float, rehash: bool = True
) -> tuple[int, str, str | None] | None:
    with closing(db()) as con:
        user_id, encoded = password_authentication_material(con, email=email)
    verified, replacement = await run_password_kdf(
        verify_password_and_rehash, encoded, password
    )
    if user_id is None or not verified:
        return None
    return int(user_id), encoded, replacement if rehash else None


def auth_ip_rate_limit(req: Request, *, operation: str, now: float) -> None:
    client_ip = req.client.host if req.client else "unknown"
    if not AUTH_V3_IP_LIMITER.allow(f"{operation}:{client_ip}", now):
        raise HTTPException(429, "Priveľa pokusov. Skús to znova o 10 minút.")


def auth_account_rate_limit(*, account: str, operation: str, now: float) -> None:
    if not AUTH_V3_ACCOUNT_LIMITER.allow(f"{operation}:{account}", now):
        raise HTTPException(429, "Priveľa pokusov. Skús to znova o 10 minút.")


def auth_device_name(req: Request, data: dict, fallback: str) -> str:
    supplied = data.get("device_name")
    if isinstance(supplied, str):
        if len(supplied) > 80:
            raise HTTPException(400, "Názov zariadenia môže mať najviac 80 znakov.")
        supplied = " ".join(supplied.split())
        if supplied:
            return supplied[:80]
    user_agent = " ".join(req.headers.get("user-agent", "").split())
    return user_agent[:80] or fallback


def require_passkey_feature() -> None:
    if not auth_v3_enabled():
        raise HTTPException(404, "Nenájdené")


def passkey_credential_id(credential: object) -> str:
    if not isinstance(credential, dict):
        raise HTTPException(400, PASSKEY_FAILURE_MESSAGE)
    credential_id = credential.get("id")
    if (
        not isinstance(credential_id, str)
        or re.fullmatch(r"[A-Za-z0-9_-]{1,2048}", credential_id) is None
    ):
        raise HTTPException(400, PASSKEY_FAILURE_MESSAGE)
    return credential_id


def passkey_transports(data: dict) -> list[str]:
    supplied = data.get("transports", [])
    if not isinstance(supplied, list) or len(supplied) > 8:
        raise HTTPException(400, PASSKEY_FAILURE_MESSAGE)
    allowed = {transport.value for transport in AuthenticatorTransport}
    transports = []
    for value in supplied:
        if not isinstance(value, str) or value not in allowed:
            raise HTTPException(400, PASSKEY_FAILURE_MESSAGE)
        if value not in transports:
            transports.append(value)
    return transports


def passkey_descriptor(passkey: dict) -> PublicKeyCredentialDescriptor:
    transports = []
    for value in passkey["transports"]:
        try:
            transports.append(AuthenticatorTransport(value))
        except ValueError:
            continue
    return PublicKeyCredentialDescriptor(
        id=base64url_to_bytes(passkey["credential_id"]),
        transports=transports,
    )


def action_email(
    *, email: str, subject: str, heading: str, link: str, idempotency_key=None
):
    text = (
        f"Ahoj!\n\n{heading}:\n{link}\n\n"
        "Ak si o túto zmenu nežiadal, e-mail ignoruj.\n\nUvar.si\nhttps://uvar.si"
    )
    html = (
        "<!doctype html><html lang='sk'><body><h1>Uvar.si</h1>"
        f"<p>{heading}</p><p><a href='{link}'>Pokračovať</a></p>"
        "<p>Ak si o túto zmenu nežiadal, e-mail ignoruj.</p></body></html>"
    )
    return posli_mail(
        email, subject, text, html, idempotency_key=idempotency_key
    )


def process_password_reset_outbox_batch(
    worker_id: str, *, limit: int = AUTH_OUTBOX_BATCH_SIZE
) -> int:
    """Process at most one durable delivery per executor invocation."""
    if limit <= 0 or AUTH_OUTBOX_SHUTTING_DOWN:
        return 0
    token_secret = env("RESEND_API_KEY")
    if not token_secret:
        if AUTH_OUTBOX_SHUTTING_DOWN:
            return 0
        with closing(db()) as con:
            claim_password_reset_job(
                con,
                worker_id=worker_id,
                now=AUTH_CLOCK(),
                token_secret=None,
            )
        return -1
    if AUTH_OUTBOX_SHUTTING_DOWN:
        return 0
    with closing(db()) as con:
        delivery = claim_password_reset_job(
            con,
            worker_id=worker_id,
            now=AUTH_CLOCK(),
            token_secret=token_secret,
        )
    if delivery is None:
        return 0
    if delivery.raw_token is None:
        return 1
    if AUTH_OUTBOX_SHUTTING_DOWN:
        with closing(db()) as con:
            finish_password_reset_job(
                con, delivery, accepted=False, now=AUTH_CLOCK()
            )
        return 1
    link = (
        f"{BASE_URL}/api/auth/pages/heslo"
        f"#token={delivery.raw_token}&purpose=reset"
    )
    accepted = False
    try:
        action_email(
            email=delivery.email,
            subject="Obnova hesla Uvar.si",
            heading="Nastav si nové heslo; odkaz platí 60 minút",
            link=link,
            idempotency_key=delivery.idempotency_key,
        )
        accepted = True
    except Exception:
        pass
    with closing(db()) as con:
        finish_password_reset_job(
            con, delivery, accepted=accepted, now=AUTH_CLOCK()
        )
    return 1


def schedule_password_reset_wake(wake_at: float) -> None:
    if AUTH_OUTBOX_SHUTTING_DOWN:
        return
    loop = asyncio.get_running_loop()
    current = AUTH_OUTBOX_WAKE_HANDLES.get(loop)
    if current is not None:
        handle, current_wake = current
        if not handle.cancelled() and current_wake <= wake_at:
            return
        handle.cancel()

    def wake() -> None:
        AUTH_OUTBOX_WAKE_HANDLES.pop(loop, None)
        if not AUTH_OUTBOX_SHUTTING_DOWN:
            ensure_password_reset_worker()

    delay = max(0.0, wake_at - AUTH_CLOCK())
    AUTH_OUTBOX_WAKE_HANDLES[loop] = (
        AUTH_OUTBOX_CALL_LATER(loop, delay, wake),
        wake_at,
    )


def ensure_password_reset_worker() -> asyncio.Task:
    loop = asyncio.get_running_loop()
    for task in tuple(AUTH_BACKGROUND_TASKS):
        if task.done():
            AUTH_BACKGROUND_TASKS.discard(task)
        elif task.get_loop() is loop:
            return task
    task = asyncio.create_task(
        asyncio.to_thread(
            process_password_reset_outbox_batch,
            AUTH_OUTBOX_WORKER_ID,
            limit=AUTH_OUTBOX_BATCH_SIZE,
        )
    )
    AUTH_BACKGROUND_TASKS.add(task)

    def discard_outcome(completed: asyncio.Task) -> None:
        AUTH_BACKGROUND_TASKS.discard(completed)
        if completed.cancelled():
            return
        try:
            processed = completed.result()
        except BaseException:
            return
        if AUTH_OUTBOX_SHUTTING_DOWN:
            return
        if processed < 0:
            now = AUTH_CLOCK()
            with closing(db()) as con:
                durable_wake = password_reset_outbox_next_wake(con, now=now)
            provider_wake = now + AUTH_OUTBOX_PROVIDER_RETRY_SECONDS
            if durable_wake is not None and durable_wake > now:
                provider_wake = min(provider_wake, durable_wake)
            schedule_password_reset_wake(provider_wake)
            return
        with closing(db()) as con:
            wake_at = password_reset_outbox_next_wake(
                con, now=AUTH_CLOCK()
            )
        if wake_at is None:
            return
        if wake_at <= AUTH_CLOCK():
            ensure_password_reset_worker()
        else:
            schedule_password_reset_wake(wake_at)

    task.add_done_callback(discard_outcome)
    return task


async def drain_password_reset_workers(
    *, deadline: float = AUTH_OUTBOX_SHUTDOWN_DEADLINE
) -> bool:
    loop = asyncio.get_running_loop()
    stop_at = loop.time() + max(0.0, deadline)
    while True:
        active = [task for task in AUTH_BACKGROUND_TASKS if not task.done()]
        if not active:
            if AUTH_OUTBOX_SHUTTING_DOWN:
                return True
            if not env("RESEND_API_KEY"):
                return True
            with closing(db()) as con:
                wake_at = password_reset_outbox_next_wake(
                    con, now=AUTH_CLOCK()
                )
            if wake_at is None or wake_at > AUTH_CLOCK():
                return True
            active = [ensure_password_reset_worker()]
        remaining = stop_at - loop.time()
        if remaining <= 0:
            return False
        _done, pending = await asyncio.wait(active, timeout=remaining)
        if pending:
            return False


def enqueue_password_reset_delivery(*, email: str, requested_at: float) -> None:
    with closing(db()) as con:
        enqueue_password_reset_job(
            con, email=email, requested_at=requested_at
        )
    ensure_password_reset_worker()


@app.post("/api/auth/request")
async def auth_request(req: Request):
    require_auth_origin(req)
    now = AUTH_CLOCK()
    client_ip = req.client.host if req.client else "unknown"
    if not IP_REQUEST_LIMITER.allow(client_ip, now):
        raise HTTPException(429, "Priveľa pokusov. Skús to znova o 10 minút.")
    data = await auth_json(req)
    try:
        email = normalize_email(data.get("email"))
    except ValueError:
        raise HTTPException(400, "Zadaj platnú e-mailovú adresu.")

    # The old magic route is now a one-time migration bridge. Unknown addresses
    # and accounts that already have a password get the identical public
    # response but never receive a login credential.
    with closing(db()) as con:
        existing_account = con.execute(
            """SELECT 1 FROM pouzivatelia p
               LEFT JOIN auth_credentials c ON c.user_id=p.id
               LEFT JOIN auth_legacy_setup_claims l ON l.user_id=p.id
               WHERE p.email=? AND c.user_id IS NULL AND l.user_id IS NULL""",
            (email,),
        ).fetchone() is not None
    if not existing_account:
        return {"ok": True, "message": AUTH_SUCCESS_MESSAGE}

    def deliver(tok):
        link = f"{BASE_URL}/prihlasenie#token={tok}"
        text = (f"Ahoj!\n\nKlikni sem a pokračuj v nastavení hesla (odkaz platí 60 minút):\n{link}\n\n"
                f"Ak si o nastavenie hesla nežiadal, tento e-mail pokojne ignoruj.\n\n"
                f"Uvar.si — z letáka rovno na tanier\nhttps://uvar.si")
        html = f"""<!DOCTYPE html><html lang="sk"><body style="margin:0;padding:28px;
background:#FFFCF5;font-family:-apple-system,Segoe UI,Roboto,sans-serif;color:#14231C">
<div style="max-width:460px;margin:0 auto;background:#fff;border:2px solid #14231C;padding:30px">
  <div style="font-size:22px;font-weight:800;letter-spacing:.04em;text-transform:uppercase;
    margin-bottom:22px">UVAR<span style="color:#E23A26">.SI</span></div>
  <h1 style="font-size:21px;margin:0 0 12px">Nastavenie hesla</h1>
  <p style="color:#5C6B62;line-height:1.6;margin:0 0 24px">Klikni na tlačidlo a pokračuj
     v bezpečnom nastavení hesla. Odkaz platí 60 minút.</p>
  <a href="{link}" style="display:inline-block;background:#FFD400;color:#14231C;
     border:2px solid #14231C;padding:14px 24px;text-decoration:none;font-weight:700;
     letter-spacing:.04em;text-transform:uppercase;font-size:14px">Pokračovať →</a>
  <p style="color:#5C6B62;font-size:13px;line-height:1.6;margin:26px 0 0">
     Ak si o nastavenie hesla nežiadal, tento e-mail pokojne ignoruj — nič sa nestane.</p>
  <hr style="border:0;border-top:1px dashed #C9C2B4;margin:24px 0">
  <p style="color:#5C6B62;font-size:12px;margin:0">Uvar.si — jedálniček a recepty z toho,
     čo je práve v akcii. <a href="https://uvar.si" style="color:#14231C">uvar.si</a></p>
</div></body></html>"""
        return posli_mail(email, "Nastavenie hesla Uvar.si", text, html)

    try:
        with closing(db()) as con:
            reservation = reserve_magic_token(con, email=email, now=now)
    except (EmailCooldown, EmailRequestInProgress):
        raise HTTPException(429, "Nový odkaz môžeš vyžiadať po 60 sekundách.")

    def deliver_and_finalize():
        """Own the reservation until delivery is decided, independent of the client."""
        try:
            accepted = deliver(reservation.raw_token)
        except BaseException:
            # Nothing was handed over, so the pending reservation is worthless.
            with closing(db()) as con:
                cancel_magic_token_reservation(con, reservation)
            raise
        try:
            with closing(db()) as con:
                promote_magic_token(
                    con,
                    reservation=reservation,
                    now=AUTH_CLOCK(),
                    accepted=accepted,
                )
        except ReservationInvalid:
            with closing(db()) as con:
                cancel_magic_token_reservation(con, reservation)
            raise
        # Any other finalize failure keeps the accepted reservation: the provider
        # already carries that link, so it stays exclusive until recovery.

    def drop_unobserved_outcome(task):
        """Delivery outcome is already persisted; retrieve it so nothing is logged."""
        if not task.cancelled():
            task.exception()

    delivery = asyncio.ensure_future(asyncio.to_thread(deliver_and_finalize))
    delivery.add_done_callback(drop_unobserved_outcome)
    try:
        # Shielded: a disconnected client must neither abort delivery nor free the
        # reservation early, otherwise a retry would slip past the per-email limit.
        await asyncio.shield(delivery)
    except (DeliveryError, ReservationInvalid):
        raise HTTPException(503, AUTH_PROVIDER_FAILURE_MESSAGE)
    return {"ok": True, "message": AUTH_SUCCESS_MESSAGE}


@app.post("/api/auth/register")
async def auth_register(req: Request):
    require_auth_origin(req)
    data = await auth_json(req)
    now = AUTH_CLOCK()
    auth_ip_rate_limit(req, operation="register", now=now)
    try:
        email = normalize_email(data.get("email"))
    except ValueError:
        raise HTTPException(400, "Zadaj platný e-mail a heslo s 10 až 128 znakmi.")
    auth_account_rate_limit(account=email, operation="register", now=now)
    try:
        password = validate_password(data.get("password"))
    except ValueError:
        raise HTTPException(400, "Zadaj platný e-mail a heslo s 10 až 128 znakmi.")
    pending_hash = await run_password_kdf(hash_password, password)

    with closing(db()) as con:
        account = con.execute(
            """SELECT p.id, c.user_id, c.changed_at
               FROM pouzivatelia p
               LEFT JOIN auth_credentials c ON c.user_id=p.id
               WHERE p.email=?""",
            (email,),
        ).fetchone()
        if account is None:
            purpose = "confirm"
            raw_token = create_action_token(
                con,
                email=email,
                purpose=purpose,
                now=now,
                pending_password_hash=pending_hash,
            )
            link = f"{BASE_URL}/api/auth/pages/potvrdenie#token={raw_token}"
            subject = "Potvrď účet Uvar.si"
            heading = "Potvrď vytvorenie účtu; odkaz platí 24 hodín"
        else:
            purpose = "reset" if account[1] is not None else "setup"
            raw_token = create_action_token(
                con,
                email=email,
                purpose=purpose,
                now=now,
                credential_changed_at=(
                    account[2]
                    if purpose == "reset"
                    else PASSWORDLESS_CREDENTIAL_VERSION
                ),
            )
            link = (
                f"{BASE_URL}/api/auth/pages/heslo"
                f"#token={raw_token}&purpose={purpose}"
            )
            subject = "Účet Uvar.si už existuje"
            heading = "Pokračuj prihlásením alebo bezpečným nastavením hesla"
    try:
        await asyncio.to_thread(
            action_email,
            email=email,
            subject=subject,
            heading=heading,
            link=link,
        )
    except DeliveryError:
        raise HTTPException(503, ACCOUNT_PROVIDER_FAILURE_MESSAGE)
    return {"ok": True, "message": ACCOUNT_REQUEST_MESSAGE}


@app.post("/api/auth/confirm")
async def auth_confirm(req: Request):
    require_auth_origin(req)
    data = await auth_json(req)
    now = AUTH_CLOCK()
    try:
        with closing(db()) as con:
            try:
                action = consume_action_token(
                    con,
                    raw_token=data.get("token"),
                    purpose="confirm",
                    now=now,
                )
                pending_hash = action.get("pending_password_hash")
                if not isinstance(pending_hash, str):
                    raise ActionTokenInvalid("confirmation has no password")
                existing = con.execute(
                    "SELECT id FROM pouzivatelia WHERE email=?", (action["email"],)
                ).fetchone()
                if existing is not None:
                    con.commit()
                    raise ActionTokenInvalid("account already exists")
                user_id = con.execute(
                    "INSERT INTO pouzivatelia (email) VALUES (?)", (action["email"],)
                ).lastrowid
                set_password(
                    con, user_id=user_id, password_hash=pending_hash, now=now
                )
                session = create_session(
                    con,
                    user_id=user_id,
                    now=now,
                    device_name=auth_device_name(req, data, "Nové zariadenie"),
                )
                con.commit()
            except Exception:
                if con.in_transaction:
                    con.rollback()
                raise
    except ActionTokenExpired:
        raise HTTPException(410, "Potvrdzovací odkaz vypršal. Vyžiadaj si nový.")
    except ActionTokenInvalid:
        raise HTTPException(400, "Potvrdzovací odkaz je neplatný alebo už použitý.")
    response = JSONResponse({"ok": True, "redirect": "/app"})
    set_session_cookie(response, session)
    return response


@app.post("/api/auth/login")
async def auth_password_login(req: Request):
    require_auth_origin(req)
    data = await auth_json(req)
    now = AUTH_CLOCK()
    auth_ip_rate_limit(req, operation="login", now=now)
    try:
        email = normalize_email(data.get("email"))
    except ValueError:
        raise HTTPException(401, PASSWORD_LOGIN_FAILURE_MESSAGE)
    try:
        password = validate_password(data.get("password"))
    except ValueError:
        raise HTTPException(401, PASSWORD_LOGIN_FAILURE_MESSAGE)
    device_name = auth_device_name(req, data, "Prihlásené zariadenie")
    auth_account_rate_limit(account=email, operation="login", now=now)
    authentication = await authenticate_password_async(
        email=email, password=password, now=now
    )
    if authentication is None:
        raise HTTPException(401, PASSWORD_LOGIN_FAILURE_MESSAGE)
    user_id, verified_hash, replacement_hash = authentication
    with closing(db()) as con:
        con.execute("BEGIN IMMEDIATE")
        try:
            if replacement_hash is None:
                current = con.execute(
                    """SELECT 1 FROM auth_credentials
                       WHERE user_id=? AND password_hash=?""",
                    (user_id, verified_hash),
                ).fetchone()
                if current is None:
                    raise CredentialStateChanged()
            else:
                updated = con.execute(
                    """UPDATE auth_credentials SET password_hash=?, changed_at=?
                       WHERE user_id=? AND password_hash=?""",
                    (replacement_hash, now, user_id, verified_hash),
                )
                if updated.rowcount != 1:
                    raise CredentialStateChanged()
            session = create_session(
                con,
                user_id=user_id,
                now=now,
                device_name=device_name,
            )
            con.commit()
        except CredentialStateChanged:
            con.rollback()
            raise HTTPException(401, PASSWORD_LOGIN_FAILURE_MESSAGE)
        except Exception:
            con.rollback()
            raise
    response = JSONResponse({"ok": True, "redirect": "/app"})
    set_session_cookie(response, session)
    return response


@app.post("/api/auth/password/request")
async def auth_password_request(req: Request):
    require_auth_origin(req)
    data = await auth_json(req)
    now = AUTH_CLOCK()
    auth_ip_rate_limit(req, operation="password-request", now=now)
    try:
        email = normalize_email(data.get("email"))
    except ValueError:
        raise HTTPException(400, "Zadaj platnú e-mailovú adresu.")
    auth_account_rate_limit(account=email, operation="password-request", now=now)
    enqueue_password_reset_delivery(email=email, requested_at=now)
    return {"ok": True, "message": PASSWORD_REQUEST_MESSAGE}


@app.post("/api/auth/password/reset")
async def auth_password_reset(req: Request):
    require_auth_origin(req)
    data = await auth_json(req)
    now = AUTH_CLOCK()
    auth_ip_rate_limit(req, operation="password-reset", now=now)
    purpose = data.get("purpose", "reset")
    if purpose not in {"reset", "setup"}:
        raise HTTPException(400, "Odkaz je neplatný alebo už použitý.")
    try:
        password = validate_password(data.get("password"))
    except ValueError:
        raise HTTPException(400, "Heslo musí mať 10 až 128 znakov.")
    raw_token = data.get("token")
    if isinstance(raw_token, str) and raw_token:
        with closing(db()) as con:
            token_account = con.execute(
                """SELECT email FROM auth_action_tokens
                   WHERE token_hash=? AND purpose=?""",
                (token_hash(raw_token), purpose),
            ).fetchone()
        if token_account is not None:
            auth_account_rate_limit(
                account=token_account[0], operation="password-reset", now=now
            )
    device_name = auth_device_name(req, data, "Obnovené zariadenie")
    password_hash = await run_password_kdf(hash_password, password)
    try:
        with closing(db()) as con:
            try:
                action = consume_action_token(
                    con,
                    raw_token=raw_token,
                    purpose=purpose,
                    now=now,
                )
                user = con.execute(
                    "SELECT id FROM pouzivatelia WHERE email=?", (action["email"],)
                ).fetchone()
                if user is None:
                    con.commit()
                    raise ActionTokenInvalid("account missing")
                user_id = int(user[0])
                set_password(
                    con, user_id=user_id, password_hash=password_hash, now=now
                )
                con.execute(
                    "DELETE FROM auth_setup_sessions WHERE user_id=?", (user_id,)
                )
                if purpose == "setup":
                    con.execute(
                        """INSERT OR REPLACE INTO auth_legacy_setup_claims
                           (user_id, claimed_at) VALUES (?, ?)""",
                        (user_id, now),
                    )
                    con.execute(
                        "DELETE FROM magic_tokens_v2 WHERE email=?",
                        (action["email"],),
                    )
                session = create_session(
                    con,
                    user_id=user_id,
                    now=now,
                    device_name=device_name,
                )
                revoke_other_sessions(
                    con, user_id=user_id, current_token=session
                )
                con.commit()
            except Exception:
                if con.in_transaction:
                    con.rollback()
                raise
    except ActionTokenExpired:
        raise HTTPException(410, "Odkaz na heslo vypršal. Vyžiadaj si nový.")
    except ActionTokenInvalid:
        raise HTTPException(400, "Odkaz je neplatný alebo už použitý.")
    response = JSONResponse({"ok": True, "redirect": "/app"})
    set_session_cookie(response, session)
    return response


class PasswordAlreadyConfigured(RuntimeError):
    pass


class CredentialStateChanged(RuntimeError):
    pass


def update_authenticated_password(
    req: Request,
    *,
    user: dict,
    password_hash: str,
    now: float,
    finalize_legacy_setup: bool = False,
    expected_password_hash: str | None = None,
    require_current_session: bool = False,
) -> None:
    current = req.cookies.get(COOKIE)
    with closing(db()) as con:
        con.execute("BEGIN IMMEDIATE")
        try:
            if finalize_legacy_setup and con.execute(
                "SELECT 1 FROM auth_credentials WHERE user_id=?", (user["id"],)
            ).fetchone():
                raise PasswordAlreadyConfigured()
            if require_current_session:
                if not current or con.execute(
                    """SELECT 1 FROM sessions_v2
                       WHERE token_hash=? AND user_id=? AND revoked_at IS NULL
                         AND expires_at>?""",
                    (token_hash(current), user["id"], now),
                ).fetchone() is None:
                    raise CredentialStateChanged()
                updated = con.execute(
                    """UPDATE auth_credentials SET password_hash=?, changed_at=?
                       WHERE user_id=? AND password_hash=?""",
                    (password_hash, now, user["id"], expected_password_hash),
                )
                if updated.rowcount != 1:
                    raise CredentialStateChanged()
            else:
                set_password(
                    con, user_id=user["id"], password_hash=password_hash, now=now
                )
            con.execute(
                """DELETE FROM auth_action_tokens
                   WHERE email=? AND purpose IN ('reset', 'setup')""",
                (user["email"],),
            )
            con.execute(
                "DELETE FROM auth_setup_sessions WHERE user_id=?",
                (user["id"],),
            )
            if finalize_legacy_setup:
                con.execute(
                    """INSERT OR REPLACE INTO auth_legacy_setup_claims
                       (user_id, claimed_at) VALUES (?, ?)""",
                    (user["id"], now),
                )
                con.execute(
                    "DELETE FROM magic_tokens_v2 WHERE email=?",
                    (user["email"],),
                )
            revoke_other_sessions(
                con, user_id=user["id"], current_token=current
            )
            con.commit()
        except Exception:
            con.rollback()
            raise


def complete_restricted_setup_password(
    req: Request,
    *,
    raw_setup_session: str,
    password_hash: str,
    now: float,
) -> str:
    """Atomically turn one setup capability into a password and full session."""
    with closing(db()) as con:
        con.execute("BEGIN IMMEDIATE")
        try:
            user = user_for_setup_session(
                con, raw_session=raw_setup_session, now=now
            )
            if user is None:
                raise PasswordAlreadyConfigured()
            set_password(
                con, user_id=user["id"], password_hash=password_hash, now=now
            )
            con.execute(
                """INSERT OR REPLACE INTO auth_legacy_setup_claims
                   (user_id, claimed_at) VALUES (?, ?)""",
                (user["id"], now),
            )
            con.execute(
                "DELETE FROM magic_tokens_v2 WHERE email=?", (user["email"],)
            )
            con.execute(
                """DELETE FROM auth_action_tokens
                   WHERE email=? AND purpose IN ('reset', 'setup')""",
                (user["email"],),
            )
            con.execute(
                "DELETE FROM auth_setup_sessions WHERE user_id=?", (user["id"],)
            )
            session = create_session(
                con,
                user_id=user["id"],
                now=now,
                device_name=auth_device_name(req, {}, "Nastavené zariadenie"),
            )
            revoke_other_sessions(
                con, user_id=user["id"], current_token=session
            )
            con.commit()
            return session
        except Exception:
            con.rollback()
            raise


@app.post("/api/auth/password/set")
async def auth_password_set(req: Request):
    require_auth_origin(req)
    data = await auth_json(req)
    user = None
    raw_setup_session = None
    candidate = req.cookies.get(SETUP_COOKIE)
    if candidate:
        with closing(db()) as con:
            setup_user = user_for_setup_session(
                con, raw_session=candidate, now=AUTH_CLOCK()
            )
        if setup_user is not None:
            user = setup_user
            raw_setup_session = candidate
    if user is None:
        user = user_from_request(req)
    if user is None:
        raise HTTPException(status_code=401, detail="Neprihlásený")
    try:
        password = validate_password(data.get("password"))
    except ValueError:
        raise HTTPException(400, "Heslo musí mať 10 až 128 znakov.")
    with closing(db()) as con:
        if con.execute(
            "SELECT 1 FROM auth_credentials WHERE user_id=?", (user["id"],)
        ).fetchone():
            raise HTTPException(409, "Heslo už je nastavené. Použi zmenu hesla.")
    now = AUTH_CLOCK()
    password_hash = await run_password_kdf(hash_password, password)
    try:
        if raw_setup_session is not None:
            session = complete_restricted_setup_password(
                req,
                raw_setup_session=raw_setup_session,
                password_hash=password_hash,
                now=now,
            )
        else:
            update_authenticated_password(
                req,
                user=user,
                password_hash=password_hash,
                now=now,
                finalize_legacy_setup=True,
            )
            session = None
    except PasswordAlreadyConfigured:
        raise HTTPException(409, "Heslo už je nastavené. Použi zmenu hesla.")
    response = JSONResponse({"ok": True})
    if session is not None:
        set_session_cookie(response, session)
        response.delete_cookie(
            SETUP_COOKIE, httponly=True, samesite="lax", secure=True
        )
    return response


@app.post("/api/auth/password/change")
async def auth_password_change(req: Request):
    require_auth_origin(req)
    user = require_user(req)
    data = await auth_json(req)
    try:
        current_password = validate_password(data.get("current_password"))
    except ValueError:
        raise HTTPException(401, PASSWORD_LOGIN_FAILURE_MESSAGE)
    try:
        password = validate_password(data.get("password"))
    except ValueError:
        raise HTTPException(400, "Heslo musí mať 10 až 128 znakov.")
    now = AUTH_CLOCK()
    authentication = await authenticate_password_async(
        email=user["email"], password=current_password, now=now, rehash=False
    )
    if authentication is None or authentication[0] != user["id"]:
        raise HTTPException(401, PASSWORD_LOGIN_FAILURE_MESSAGE)
    password_hash = await run_password_kdf(hash_password, password)
    try:
        update_authenticated_password(
            req,
            user=user,
            password_hash=password_hash,
            now=now,
            expected_password_hash=authentication[1],
            require_current_session=True,
        )
    except CredentialStateChanged:
        raise HTTPException(401, PASSWORD_LOGIN_FAILURE_MESSAGE)
    return {"ok": True}


@app.get("/api/auth/sessions")
def auth_sessions(req: Request):
    user = require_user(req)
    current = req.cookies.get(COOKIE) or ""
    with closing(db()) as con:
        active = list_sessions(
            con, user_id=user["id"], current_token=current, now=AUTH_CLOCK()
        )
    return {"sessions": active}


@app.delete("/api/auth/sessions/{management_id}")
def auth_session_delete(management_id: str, req: Request):
    require_auth_origin(req)
    user = require_user(req)
    if re.fullmatch(r"[A-Za-z0-9_-]{20,128}", management_id) is None:
        raise HTTPException(404, "Relácia sa nenašla.")
    current = req.cookies.get(COOKIE)
    with closing(db()) as con:
        current_management_id = next(
            (
                item["management_id"]
                for item in list_sessions(
                    con,
                    user_id=user["id"],
                    current_token=current or "",
                    now=AUTH_CLOCK(),
                )
                if item["current"]
            ),
            None,
        )
        if not revoke_session(
            con, user_id=user["id"], management_id=management_id
        ):
            raise HTTPException(404, "Relácia sa nenašla.")
    response = JSONResponse({"ok": True})
    if current_management_id == management_id:
        req.state.renew_session_cookie = None
        response.delete_cookie(COOKIE, httponly=True, samesite="lax", secure=True)
    return response


@app.post("/api/auth/sessions/logout-others")
def auth_sessions_logout_others(req: Request):
    require_auth_origin(req)
    user = require_user(req)
    with closing(db()) as con:
        revoke_other_sessions(
            con,
            user_id=user["id"],
            current_token=req.cookies.get(COOKIE),
        )
    return {"ok": True}


@app.post("/api/auth/passkey/register/options")
async def auth_passkey_register_options(req: Request):
    require_passkey_feature()
    require_auth_origin(req)
    user = require_user(req)
    await auth_json(req)
    now = AUTH_CLOCK()
    auth_ip_rate_limit(req, operation="passkey-register-options", now=now)
    auth_account_rate_limit(
        account=user["email"], operation="passkey-register-options", now=now
    )
    with closing(db()) as con:
        existing = list_passkeys(con, user_id=user["id"])
        challenge = create_webauthn_challenge(
            con, purpose="register", now=now, user_id=user["id"]
        )
    options = generate_registration_options(
        rp_id=PASSKEY_RP_ID,
        rp_name="Uvar.si",
        user_name=user["email"],
        user_display_name=user["email"],
        user_id=str(user["id"]).encode("ascii"),
        challenge=base64url_to_bytes(challenge),
        timeout=PASSKEY_OPTIONS_TIMEOUT_MS,
        authenticator_selection=AuthenticatorSelectionCriteria(
            resident_key=ResidentKeyRequirement.REQUIRED,
            require_resident_key=True,
            user_verification=UserVerificationRequirement.REQUIRED
        ),
        exclude_credentials=[passkey_descriptor(item) for item in existing],
    )
    return JSONResponse(json.loads(options_to_json(options)))


@app.post("/api/auth/passkey/register/verify")
async def auth_passkey_register_verify(req: Request):
    require_passkey_feature()
    require_auth_origin(req)
    user = require_user(req)
    data = await auth_json(req)
    now = AUTH_CLOCK()
    auth_ip_rate_limit(req, operation="passkey-register-verify", now=now)
    auth_account_rate_limit(
        account=user["email"], operation="passkey-register-verify", now=now
    )
    raw_challenge = data.get("challenge")
    try:
        expected_challenge = base64url_to_bytes(raw_challenge)
    except (TypeError, ValueError):
        raise HTTPException(400, PASSKEY_FAILURE_MESSAGE)
    with closing(db()) as con:
        try:
            consume_webauthn_challenge(
                con,
                raw_challenge=raw_challenge,
                purpose="register",
                now=now,
                expected_user_id=user["id"],
            )
        except WebAuthnChallengeExpired:
            raise HTTPException(410, "Passkey výzva vypršala. Začni znova.")
        except WebAuthnChallengeInvalid:
            raise HTTPException(400, PASSKEY_FAILURE_MESSAGE)
        try:
            credential = data.get("credential")
            requested_credential_id = passkey_credential_id(credential)
            transports = passkey_transports(data)
            name = auth_device_name(
                req, {"device_name": data.get("name")}, "Passkey"
            )
            try:
                verified = verify_registration_response(
                    credential=credential,
                    expected_challenge=expected_challenge,
                    expected_rp_id=PASSKEY_RP_ID,
                    expected_origin=PASSKEY_ORIGIN,
                    require_user_verification=True,
                )
                credential_id = bytes_to_base64url(verified.credential_id)
                if (
                    not verified.user_verified
                    or credential_id != requested_credential_id
                ):
                    raise InvalidRegistrationResponse("credential mismatch")
            except Exception:
                con.commit()
                raise HTTPException(400, PASSKEY_FAILURE_MESSAGE)
            store_passkey(
                con,
                credential_id=credential_id,
                user_id=user["id"],
                public_key=bytes(verified.credential_public_key),
                sign_count=int(verified.sign_count),
                transports=transports,
                name=name,
                now=now,
            )
            con.commit()
        except sqlite3.IntegrityError:
            if con.in_transaction:
                con.rollback()
            raise HTTPException(409, "Tento Passkey už je priradený.")
        except HTTPException:
            if con.in_transaction:
                con.commit()
            raise
        except Exception:
            if con.in_transaction:
                con.rollback()
            raise
    return {"ok": True}


@app.post("/api/auth/passkey/login/options")
async def auth_passkey_login_options(req: Request):
    require_passkey_feature()
    require_auth_origin(req)
    await auth_json(req)
    now = AUTH_CLOCK()
    auth_ip_rate_limit(req, operation="passkey-login-options", now=now)
    with closing(db()) as con:
        challenge = create_webauthn_challenge(
            con, purpose="login", now=now, user_id=None
        )
    options = generate_authentication_options(
        rp_id=PASSKEY_RP_ID,
        challenge=base64url_to_bytes(challenge),
        timeout=PASSKEY_OPTIONS_TIMEOUT_MS,
        user_verification=UserVerificationRequirement.REQUIRED,
    )
    return JSONResponse(json.loads(options_to_json(options)))


@app.post("/api/auth/passkey/login/verify")
async def auth_passkey_login_verify(req: Request):
    require_passkey_feature()
    require_auth_origin(req)
    data = await auth_json(req)
    now = AUTH_CLOCK()
    auth_ip_rate_limit(req, operation="passkey-login-verify", now=now)
    raw_challenge = data.get("challenge")
    try:
        expected_challenge = base64url_to_bytes(raw_challenge)
    except (TypeError, ValueError):
        raise HTTPException(400, PASSKEY_FAILURE_MESSAGE)
    with closing(db()) as con:
        try:
            consume_webauthn_challenge(
                con,
                raw_challenge=raw_challenge,
                purpose="login",
                now=now,
            )
        except WebAuthnChallengeExpired:
            raise HTTPException(410, "Passkey výzva vypršala. Začni znova.")
        except WebAuthnChallengeInvalid:
            raise HTTPException(400, PASSKEY_FAILURE_MESSAGE)
        try:
            credential = data.get("credential")
            credential_id = passkey_credential_id(credential)
            passkey = passkey_for_credential(
                con, credential_id=credential_id
            )
            if passkey is None:
                con.commit()
                raise HTTPException(401, PASSKEY_FAILURE_MESSAGE)
            auth_account_rate_limit(
                account=f"user:{passkey['user_id']}",
                operation="passkey-login-verify",
                now=now,
            )
            try:
                verified = verify_authentication_response(
                    credential=credential,
                    expected_challenge=expected_challenge,
                    expected_rp_id=PASSKEY_RP_ID,
                    expected_origin=PASSKEY_ORIGIN,
                    credential_public_key=passkey["public_key"],
                    credential_current_sign_count=passkey["sign_count"],
                    require_user_verification=True,
                )
                if (
                    not verified.user_verified
                    or bytes_to_base64url(verified.credential_id) != credential_id
                ):
                    raise InvalidAuthenticationResponse("credential mismatch")
            except Exception:
                con.commit()
                raise HTTPException(400, PASSKEY_FAILURE_MESSAGE)
            try:
                update_passkey_use(
                    con,
                    credential_id=credential_id,
                    user_id=passkey["user_id"],
                    new_sign_count=int(verified.new_sign_count),
                    now=now,
                )
            except PasskeyCloneDetected:
                con.commit()
                raise HTTPException(401, PASSKEY_FAILURE_MESSAGE)
            session = create_session(
                con,
                user_id=passkey["user_id"],
                now=now,
                device_name=auth_device_name(
                    req, data, "Zariadenie s Passkey"
                ),
            )
            con.commit()
        except HTTPException:
            if con.in_transaction:
                con.commit()
            raise
        except Exception:
            if con.in_transaction:
                con.rollback()
            raise
    response = JSONResponse({"ok": True, "redirect": "/app"})
    set_session_cookie(response, session)
    return response


@app.get("/api/auth/passkeys")
def auth_passkeys(req: Request):
    require_passkey_feature()
    user = require_user(req)
    with closing(db()) as con:
        credentials = list_passkeys(con, user_id=user["id"])
    return {"passkeys": credentials}


@app.delete("/api/auth/passkeys/{credential_id}")
def auth_passkey_delete(credential_id: str, req: Request):
    require_passkey_feature()
    require_auth_origin(req)
    user = require_user(req)
    with closing(db()) as con:
        if not delete_passkey(
            con, user_id=user["id"], credential_id=credential_id
        ):
            raise HTTPException(404, "Passkey sa nenašiel.")
    return {"ok": True}


ACCOUNT_PAGE_STYLE = """
:root{--paper:#F7F5EF;--surface:#fff;--ink:#183229;--muted:#66746E;--red:#C93427;
  --yellow:#F3C928;--border:#DCD7CA;--success:#237A50}
*{box-sizing:border-box}body{margin:0;background:var(--paper);color:var(--ink);
  font-family:Manrope,system-ui,sans-serif;line-height:1.55}
main{max-width:480px;margin:0 auto;padding:8vh 18px 30px}.brand{font-size:22px;font-weight:900;
  letter-spacing:.035em;text-transform:uppercase}.brand em{color:var(--red);font-style:normal}
.card{margin-top:24px;padding:24px;background:var(--surface);border:1px solid var(--border);
  border-radius:14px;box-shadow:0 8px 24px rgba(24,50,41,.07)}
.eyebrow{font-size:11px;font-weight:800;letter-spacing:.1em;text-transform:uppercase;color:var(--red)}
h1{font-size:30px;line-height:1.08;margin:8px 0 12px}p{color:var(--muted)}
label{display:block;font-size:13px;font-weight:800;color:var(--muted);margin:18px 0 7px}
input{width:100%;min-height:48px;padding:13px 14px;border:1px solid var(--border);border-radius:10px;
  background:var(--surface);font:inherit;color:var(--ink)}
.password{display:grid;grid-template-columns:1fr auto;gap:8px}.toggle{padding:0 13px;border:1px solid var(--border);
  border-radius:10px;background:#F0EEE7;color:var(--ink);font-weight:800;cursor:pointer}
.action{width:100%;min-height:48px;margin-top:18px;padding:13px 18px;border:0;border-radius:10px;
  background:var(--yellow);color:var(--ink);font:800 15px Manrope,system-ui,sans-serif;cursor:pointer;
  box-shadow:0 3px 0 #C9A816}.action:disabled{background:#F0EEE7;color:var(--muted);box-shadow:none}
.hint{font-size:14px;margin:7px 0 0}.status{min-height:24px;margin:13px 0 0;font-size:14px}
a{color:var(--red);font-weight:800;text-underline-offset:3px}
"""


ACCOUNT_CONFIRMATION_PAGE = f"""<!doctype html>
<html lang="sk"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Potvrdenie účtu · Uvar.si</title><meta name="robots" content="noindex,nofollow,noarchive">
<style>{ACCOUNT_PAGE_STYLE}</style></head><body><main><div class="brand">Uvar<em>.si</em></div>
<section class="card"><span class="eyebrow">Posledný krok</span><h1>Potvrď účet</h1>
<p>Účet vznikne až po stlačení tlačidla. Samotné otvorenie odkazu nič nepotvrdí.</p>
<button class="action" id="confirm" type="button">Potvrdiť účet</button>
<p class="status" id="status" role="status" aria-live="polite"></p></section></main><script>
let token=new URLSearchParams(location.hash.slice(1)).get('token')||'';
history.replaceState(null,'',location.pathname);
const button=document.getElementById('confirm');const statusNode=document.getElementById('status');let busy=false;
button.textContent='Potvrdiť účet';
button.onclick=async()=>{{
  if(busy)return;busy=true;button.disabled=true;button.textContent='Pracujem…';statusNode.textContent='';
  try{{
    const submittedToken=token;
    const response=await fetch('/api/auth/confirm',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{token:submittedToken}})}});
    const data=await response.json().catch(()=>({{}}));
    if(!response.ok)throw new Error(data.detail||'Potvrdenie sa nepodarilo. Vyžiadaj si nový odkaz.');
    token='';statusNode.textContent='Účet je potvrdený. Presmerúvame ťa…';location.replace(data.redirect||'/app');return;
  }}catch(error){{statusNode.textContent=error&&error.message?error.message:'Nepodarilo sa pripojiť. Skontroluj pripojenie a skús to znova.';}}
  busy=false;button.disabled=false;button.textContent='Potvrdiť účet';
}};
</script></body></html>"""


PASSWORD_RESET_PAGE = f"""<!doctype html>
<html lang="sk"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Nové heslo · Uvar.si</title><meta name="robots" content="noindex,nofollow,noarchive">
<style>{ACCOUNT_PAGE_STYLE}</style></head><body><main><div class="brand">Uvar<em>.si</em></div>
<section class="card"><span class="eyebrow">Zabezpečenie účtu</span><h1>Nastav nové heslo</h1>
<p>Heslo sa odošle až po potvrdení. Použi 10 až 128 znakov.</p>
<label for="password">Nové heslo</label><div class="password">
<input id="password" type="password" autocomplete="new-password" minlength="10" maxlength="128">
<button class="toggle" id="toggle" type="button" aria-pressed="false">Zobraziť heslo</button></div>
<button class="action" id="submit" type="button">Uložiť heslo</button>
<p class="status" id="status" role="status" aria-live="polite"></p></section></main><script>
const fragment=new URLSearchParams(location.hash.slice(1));let token=fragment.get('token')||'';let purpose=fragment.get('purpose')||'reset';
history.replaceState(null,'',location.pathname);
const passwordNode=document.getElementById('password');const button=document.getElementById('submit');
const toggle=document.getElementById('toggle');const statusNode=document.getElementById('status');let busy=false;
button.textContent='Uložiť heslo';toggle.textContent='Zobraziť heslo';
toggle.onclick=()=>{{const visible=passwordNode.type==='password';passwordNode.type=visible?'text':'password';
  toggle.textContent=visible?'Skryť heslo':'Zobraziť heslo';toggle.setAttribute('aria-pressed',visible?'true':'false');}};
button.onclick=async()=>{{
  if(busy)return;const password=passwordNode.value;
  if(password.length<10||password.length>128){{statusNode.textContent='Heslo musí mať 10 až 128 znakov.';return;}}
  busy=true;button.disabled=true;button.textContent='Pracujem…';statusNode.textContent='';
  try{{
    const endpoint=token?'/api/auth/password/reset':'/api/auth/password/set';
    const payload=token?{{token,purpose,password}}:{{password}};
    const response=await fetch(endpoint,{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify(payload)}});
    const data=await response.json().catch(()=>({{}}));
    if(!response.ok)throw new Error(data.detail||'Heslo sa nepodarilo uložiť.');
    token='';passwordNode.value='';statusNode.textContent='Heslo je uložené. Presmerúvame ťa…';location.replace(data.redirect||'/app');return;
  }}catch(error){{
    statusNode.textContent=error&&error.message&&error.message!=='offline'?error.message:'Nepodarilo sa pripojiť. Skontroluj pripojenie a skús to znova.';
  }}
  busy=false;button.disabled=false;button.textContent='Uložiť heslo';
}};
</script></body></html>"""


@app.get("/potvrdenie")
@app.get("/api/auth/pages/potvrdenie")
def account_confirmation_page():
    return HTMLResponse(ACCOUNT_CONFIRMATION_PAGE)


@app.get("/heslo")
@app.get("/api/auth/pages/heslo")
def password_reset_page():
    return HTMLResponse(PASSWORD_RESET_PAGE)


LOGIN_CONFIRMATION_PAGE = """<!doctype html>
<html lang="sk"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>Potvrdenie nastavenia hesla · Uvar.si</title>
<meta name="robots" content="noindex,nofollow,noarchive">
<style>
:root{--paper:#fffcf5;--ink:#14231c;--soft:#5c6b62;--yellow:#ffd400;--red:#e23a26}
*{box-sizing:border-box}body{margin:0;background:var(--paper);color:var(--ink);font-family:Arial,sans-serif}
main{max-width:520px;margin:9vh auto;padding:24px}.brand{font-size:24px;font-weight:900;text-transform:uppercase}
.brand em{color:var(--red);font-style:normal}.card{background:#fff;border:2px solid var(--ink);padding:28px;margin-top:24px}
h1{font-size:28px;margin:0 0 12px}p{color:var(--soft);line-height:1.6}.action{display:inline-block;border:2px solid var(--ink);background:var(--yellow);color:var(--ink);padding:14px 18px;font-weight:800;text-decoration:none;cursor:pointer}
#resend{display:none;margin-top:16px}.legacy #confirm{display:none}.legacy #resend{display:inline-block}.legacy #status{color:var(--red)}
</style></head><body><main><div class="brand">Uvar<em>.si</em></div>
<section class="card" id="panel"><h1>Potvrď nastavenie hesla</h1>
<p id="status">Odkaz sa použije až po tvojom potvrdení. Platí 60 minút.</p>
<button class="action" id="confirm" type="button">Pokračovať k heslu</button>
<a class="action" id="resend" href="/app">Požiadať o nový odkaz</a></section></main>
<script>
(()=>{
const panel=document.getElementById('panel');const statusNode=document.getElementById('status');
const freshToken=new URLSearchParams(location.hash.slice(1)).get('token')||'';
let token=freshToken;
history.replaceState(null,'',location.pathname);
if(!token){statusNode.textContent='Odkaz už nie je v tejto karte. Otvor ho znova z e-mailu alebo požiadaj o nový odkaz na nastavenie hesla.';panel.classList.add('legacy');}
document.getElementById('confirm').onclick=async()=>{
  const submittedToken=token;history.replaceState(null,'',location.pathname);
  try{
    const response=await fetch('/api/auth/verify',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({token:submittedToken})});
    const data=await response.json().catch(()=>({}));
    if(response.ok){token='';location.replace(data.redirect);return;}
    if(response.status===400||response.status===410)token='';
    statusNode.textContent=data.detail||'Odkaz sa nepodarilo overiť. Požiadaj o nový.';
  }catch(error){statusNode.textContent='Overenie sa nepodarilo pripojiť. Požiadaj o nový odkaz.';}
  document.getElementById('confirm').style.display='none';document.getElementById('resend').style.display='inline-block';
};
})();
</script></body></html>"""


LEGACY_LOGIN_PAGE = LOGIN_CONFIRMATION_PAGE.replace(
    '<section class="card" id="panel">', '<section class="card legacy" id="panel">'
).replace(
    "Odkaz sa použije až po tvojom potvrdení. Platí 60 minút.",
    "Tento odkaz má starý formát a z bezpečnostných dôvodov ho nemožno použiť.",
)


@app.get("/prihlasenie")
def auth_confirmation(req: Request):
    if "token" in req.query_params:
        return HTMLResponse(LEGACY_LOGIN_PAGE, status_code=400)
    return HTMLResponse(LOGIN_CONFIRMATION_PAGE)


@app.post("/api/auth/verify")
async def auth_verify(req: Request):
    require_auth_origin(req)
    data = await auth_json(req)
    try:
        with closing(db()) as con:
            setup_session = consume_magic_token(
                con, raw_token=data.get("token"), now=AUTH_CLOCK()
            )
    except MagicTokenExpired:
        raise HTTPException(410, "Odkaz vypršal. Požiadaj o nový prihlasovací odkaz.")
    except MagicTokenInvalid:
        raise HTTPException(400, "Odkaz je neplatný alebo už bol použitý. Požiadaj o nový.")
    response = JSONResponse({"ok": True, "redirect": "/api/auth/pages/heslo"})
    set_setup_cookie(response, setup_session)
    return response


@app.post("/api/auth/logout")
def auth_logout(req: Request):
    require_auth_origin(req)
    tok = req.cookies.get(COOKIE)
    setup_tok = req.cookies.get(SETUP_COOKIE)
    if tok or setup_tok:
        with closing(db()) as con:
            if tok:
                delete_session(con, tok)
            if setup_tok:
                delete_setup_session(con, setup_tok)
    r = JSONResponse({"ok": True})
    r.delete_cookie(COOKIE, httponly=True, samesite="lax", secure=True)
    r.delete_cookie(SETUP_COOKIE, httponly=True, samesite="lax", secure=True)
    return r


# ---------------------------------------------------------------- profil
@app.get("/api/me")
def me(req: Request):
    auth_v3 = auth_v3_enabled()
    u = user_from_request(req)
    if not u:
        public = {"prihlaseny": False}
        if auth_v3:
            public["auth_v3"] = True
        return public
    den = dnesok()
    with closing(db()) as con:
        password_configured = False
        if auth_v3:
            password_configured = con.execute(
                "SELECT 1 FROM auth_credentials WHERE user_id=?", (u["id"],)
            ).fetchone() is not None
        premium = je_premium(con, u["id"])
        sp = spajza_pouzivatela(con, u["id"], premium)
        ulozenych = pocet_ulozenej_spajze(con, u["id"])
        limit = limit_prepoctov(premium)
        zostava = max(0, limit - pouzite_prepocty(con, u["id"], den))
    # Špajza uspatá koncom Premium sa nezamlčí: appka vie, koľko riadkov leží
    # a prečo do plánu nevstupujú. Ich názvy sem nepatria — obrazovka o platbe
    # nemá zobrazovať údaje, ktoré práve nič neovplyvňujú.
    uspana = bool(not premium and ulozenych)
    adults, children = zlozenie_domacnosti(u)
    result = {"prihlaseny": True, "id": u["id"], "email": u["email"],
              "adults": adults, "children": children, "osoby": adults + children,
              "frekvencia": u["frekvencia"], "obchody": u["obchody"].split(","),
              "onboarding": bool(u["onboarding"]),
              # `platiaci` je len stĺpec; pravdu o platbe drží tabuľka nárokov.
              "platiaci": premium, "premium": premium,
              "platby_zapnute": platby_su_zapnute(),
              "spajza": sp, "spajza_premium": premium, "spajza_dostupna": premium,
              "spajza_ulozenych": ulozenych, "spajza_uspana": uspana,
              "spajza_sprava": sprava_o_uspanej_spajze(ulozenych) if uspana else None,
              "limit_prepoctov": limit, "zostava_prepoctov": zostava,
              "prepocty_obnova": zajtrajsok(den)}
    if auth_v3:
        result.update(auth_v3=True, password_configured=password_configured)
    return result


@app.post("/api/profil")
async def uloz_profil(req: Request):
    u = require_user(req)
    d = await req.json()
    adults, children = validuj_zlozenie_domacnosti(d)
    osoby = adults + children
    frek = max(1, min(7, int(d.get("frekvencia", 2))))
    allowed_stores = ("Kaufland", "Tesco", "Lidl")
    obchody = [store for store in allowed_stores if store in d.get("obchody", [])]
    if not obchody:
        obchody = ["Kaufland", "Tesco", "Lidl"]
    with closing(db()) as con:
        old = con.execute(
            "SELECT osoby, dospeli, deti, frekvencia, obchody "
            "FROM pouzivatelia WHERE id=?", (u["id"],)
        ).fetchone()
        old_adults, old_children = zlozenie_domacnosti(old) if old is not None else (None, None)
        changed = old is None or (
            old_adults, old_children, old["frekvencia"], old["obchody"]
        ) != (adults, children, frek, ",".join(obchody))
        con.execute(
            "UPDATE pouzivatelia SET osoby=?,dospeli=?,deti=?,frekvencia=?,obchody=?,onboarding=1"
            " WHERE id=?",
            (osoby, adults, children, frek, ",".join(obchody), u["id"]),
        )
        if changed:
            con.execute("DELETE FROM plany WHERE user_id=? AND tyzden=?", (u["id"], monday()))
        con.commit()
    # Až po commite, na vlastnom spojení: hotový plán pre nový profil sa
    # prevezme hneď, takže na obrazovke s jedálničkom sa už nečaká. Zlyhanie
    # zahriatia nesmie zhodiť uloženie profilu — je to len bonus navyše.
    if changed:
        try:
            zahrej_plan_pre_pouzivatela(u["id"])
        except Exception:
            LOG.info("zahriatie plánu preskočené", exc_info=True)
    return {"ok": True}


@app.post("/api/spajza")
async def uloz_spajzu(req: Request):
    u = require_user(req)
    # Špajza je platená vlastnosť, tak sa zadarmo neuloží ani sa nezahodí ticho:
    # odmietnutie je jasné a appka ju bezplatnému účtu ani neponúkne. Kontrola je
    # tu vždy nanovo — nárok, ktorý medzitým zanikol, platí okamžite.
    with closing(db()) as con:
        if not je_premium(con, u["id"]):
            # Uložené riadky ostávajú ležať — odmietnutie nie je mazanie.
            return odmietni(
                403, SPRAVA_SPAJZA_PREMIUM, KOD_SPAJZA_PREMIUM,
                premium=False, spajza_dostupna=False,
                spajza_ulozenych=pocet_ulozenej_spajze(con, u["id"]),
            )
    d = await req.json()
    polozky = [str(x).strip()[:40] for x in d.get("polozky", []) if str(x).strip()][:60]
    # Špajza je oddelený systém: uloží sa okamžite a plán sa jej NIKDY nedotkne.
    # Predtým tu bolo DELETE FROM plany — jedno pridané vajíčko tak zahodilo
    # jedálniček, ktorý si používateľ práve čítal, a vynútilo platené volanie
    # modelu bez vyzvania. Rozdiel oproti plánu ukáže appka ako tichý návrh.
    with closing(db()) as con:
        old = [row["nazov"] for row in con.execute(
            "SELECT nazov FROM spajza WHERE user_id=? ORDER BY id", (u["id"],))]
        if old != polozky:
            con.execute("DELETE FROM spajza WHERE user_id=?", (u["id"],))
            con.executemany("INSERT INTO spajza (user_id,nazov) VALUES (?,?)",
                            [(u["id"], p) for p in polozky])
        con.commit()
    return {"ok": True, "pocet": len(polozky)}


# ---------------------------------------------------------------- plán
def akcie_pre(obchody):
    """Complete current priceable rows for signatures, caches, and validation.

    Prompt shortening happens only at the model-call boundary through
    ``plan_shortlist.select_offers``. Offers without a measurable package are
    excluded here, before a user's generation allowance can be reserved.
    """
    if not obchody:
        return []

    today = datetime.date.today()
    with closing(db()) as con:
        rows = measurable_offers(offers_for_current_week(con, obchody, today))

    return rows


def pouzitie_modelu(usage):
    """Spotreba tokenov z odpovede — dôkaz, že prompt caching naozaj chytá.

    Bez tohto sa dá cachovanie iba predpokladať: `cache_read` väčší ako nula
    znamená, že blok ponúk sa nečítal nanovo.
    """
    if usage is None:
        return {}
    return {
        "input": getattr(usage, "input_tokens", None),
        "output": getattr(usage, "output_tokens", None),
        "cache_write": getattr(usage, "cache_creation_input_tokens", None),
        "cache_read": getattr(usage, "cache_read_input_tokens", None),
    }


def podpis_planu(tyzden, obchody, frekvencia, rows, spajza, *, adults, children,
                 zo_spajze=False):
    """Podpis zdieľaného plánu. Špajza doň vstupuje len pri výslovnom vyžiadaní.

    Bežný plán sa skladá bez špajze, takže ho zdieľajú aj platiace účty a
    nečakajú 60–120 sekúnd na to isté menu. `zo_spajze=True` je jediná cesta,
    ktorá špajzu do kľúča (a do promptu) pustí — a taký plán sa neukladá
    zdieľane vôbec.
    """
    return plan_signature(
        tyzden, obchody, None, frekvencia, [row["offer_key"] for row in rows], spajza,
        pantry_driven=zo_spajze,
        adults=adults, children=children,
    )


def so_spajzou(plan, spajza):
    """Plán tak, ako ho má vidieť TENTO čitateľ: s vlastnou špajzou nad zoznamom.

    Počíta sa pri každej odpovedi nanovo a nikam sa neukladá. Vďaka tomu sa
    zmena špajze prejaví okamžite a do zdieľaného riadku sa nemá ako dostať.
    """
    upraveny = apply_pantry_to_shopping_list(plan, spajza)
    upraveny.pop("_uvarsi_meta", None)
    upraveny["spajza"] = list(spajza)
    return upraveny


PLAN_META_KEY = "_uvarsi_meta"


def podpis_spajze(spajza):
    """Stabilný odtlačok obsahu špajze bez ukladania ďalšej čitateľnej kópie."""
    normalizovane = sorted({str(item).strip().casefold() for item in spajza if str(item).strip()})
    payload = json.dumps(normalizovane, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def osobny_plan_na_ulozenie(plan, spajza=(), zo_spajze=False):
    ulozeny = dict(plan)
    meta = {
        "algo_version": PLAN_ALGO_VERSION,
        "portion_standard_version": PORTION_STANDARD_VERSION,
        "pantry_driven": bool(zo_spajze),
    }
    if zo_spajze:
        meta["pantry_signature"] = podpis_spajze(spajza)
    ulozeny[PLAN_META_KEY] = meta
    return ulozeny


def osobna_cache_plati(plan, spajza):
    if not isinstance(plan, dict) or not isinstance(plan.get(PLAN_META_KEY), dict):
        return False
    meta = plan[PLAN_META_KEY]
    if meta.get("algo_version") != PLAN_ALGO_VERSION:
        return False
    if meta.get("portion_standard_version") != PORTION_STANDARD_VERSION:
        return False
    if meta.get("pantry_driven") is True:
        return meta.get("pantry_signature") == podpis_spajze(spajza)
    return meta.get("pantry_driven") is False


def obnova_neplatnej_osobnej_cache(plan, spajza):
    """Povie klientovi, akú výslovnú akciu má ponúknuť — nikdy ju nespúšťa."""
    meta = plan.get(PLAN_META_KEY) if isinstance(plan, dict) else None
    if (isinstance(meta, dict)
            and meta.get("algo_version") == PLAN_ALGO_VERSION
            and meta.get("portion_standard_version") == PORTION_STANDARD_VERSION
            and meta.get("pantry_driven") is True
            and meta.get("pantry_signature") != podpis_spajze(spajza)):
        return {"dovod": "spajza_zmenena", "obnovit_cez": "/api/plan/zo-spajze"}
    return {"dovod": "plan_zastaral", "obnovit_cez": "/api/plan/generuj"}


def nacitaj_zdielany_plan(con, podpis, variant):
    row = con.execute(
        "SELECT json FROM plany_zdielane WHERE podpis=? AND variant=?", (podpis, variant)
    ).fetchone()
    if not row:
        return None
    try:
        return json.loads(row["json"])
    except json.JSONDecodeError:
        return None


def uloz_zdielany_plan(con, podpis, variant, tyzden, plan, predpocitany=False):
    """Ulož plán bez špajze — tú si každý čitateľ dopočíta vlastnú.

    `predpocitany=True` označí riadok, ktorý vznikol v noci (predpocet.py).
    Bez tej značky by sa nedalo povedať, koľko čakania nočné zahrievanie naozaj
    ušetrilo — a to je jediné číslo, podľa ktorého sa dá rozhodnúť, či ho
    rozšíriť alebo zúžiť.

    Kým bola špajza v podpise, zdieľaný riadok sa nikdy nedostal k človeku
    s inou špajzou a stačilo odstrániť vrchný kľúč. Odkedy v podpise nie je,
    je toto orezanie jediná ochrana proti tomu, aby špajza jedného človeka
    pretiekla druhému — preto ide cez `plan_without_pantry`, ktorý odstráni
    aj suroviny a dávky zo špajze vnútri jedál.
    """
    zdielany = plan_without_pantry(plan)
    zdielany.pop(PLAN_META_KEY, None)
    con.execute(
        "INSERT OR REPLACE INTO plany_zdielane (podpis,variant,tyzden,json,predpocitany)"
        " VALUES (?,?,?,?,?)",
        (podpis, variant, tyzden, json.dumps(zdielany, ensure_ascii=False),
         1 if predpocitany else 0),
    )
    con.execute("DELETE FROM plany_zdielane WHERE tyzden<>?", (tyzden,))


def prevezmi_zdielany_plan(con, user_id, tyzden, zdielany, spajza):
    """Prevezmi zdieľaný plán do vlastného riadku a podaj ho so svojou špajzou.

    Do `plany` sa ukladá plán BEZ špajze: pohľad so špajzou sa dopočíta pri
    každom čítaní, takže sa nikdy nestane zastaraným a nezaklincuje sa do
    databázy niečo, čo pri ďalšej zmene špajze prestane platiť.
    """
    plan = osobny_plan_na_ulozenie(plan_without_pantry(zdielany))
    con.execute(
        "INSERT OR REPLACE INTO plany (user_id,tyzden,json) VALUES (?,?,?)",
        (user_id, tyzden, json.dumps(plan, ensure_ascii=False)),
    )
    return so_spajzou(plan, spajza)


def zahrej_plan_pre_pouzivatela(user_id):
    """Po zmene profilu prevezmi hotový plán, ak už pre ten profil existuje.

    Zahriatie zadarmo: model sa nevolá, takže onboarding nikdy nespustí platené
    volanie bez vyzvania. Keď plán pre daný podpis ešte nikto nevygeneroval,
    ticho sa nestane nič a používateľ si ho vyžiada sám.
    """
    tyzden = monday()
    with closing(db()) as con:
        profil = con.execute(
            "SELECT osoby, dospeli, deti, frekvencia, obchody "
            "FROM pouzivatelia WHERE id=?", (user_id,)
        ).fetchone()
        if profil is None:
            return None
        spajza = spajza_pouzivatela(con, user_id, je_premium(con, user_id))

    obchody = profil["obchody"].split(",")
    adults, children = zlozenie_domacnosti(profil)
    rows = akcie_pre(obchody)
    if len(rows) < MIN_OFFERS_FOR_PLAN:
        return None
    podpis = podpis_planu(
        tyzden, obchody, profil["frekvencia"], rows, spajza,
        adults=adults, children=children,
    )
    with closing(db()) as con:
        variant = plan_variant_for(user_id, PLAN_VARIANTS)
        zdielany = nacitaj_zdielany_plan(con, podpis, variant)
        if not zdielany or not cached_plan_is_current(zdielany, rows):
            return None
        predpocet.zapocitaj_zasah(con, podpis, variant, tyzden)
        plan = prevezmi_zdielany_plan(con, user_id, tyzden, zdielany, spajza)
        con.commit()
    return plan


def sprava_o_chybajucich_akciach() -> str:
    """Prečo appka nemá z čoho skladať — a to pravdivo.

    „Obnovujú sa, skús to o chvíľu“ platí len vtedy, keď sa naozaj obnovujú.
    Keď API odmieta pre nulový kredit, zber letákov nemá ako dobehnúť a ten
    sľub je klamstvo — človek by čakal na niečo, čo samo od seba nepríde.
    """
    try:
        with closing(db()) as con:
            if naklady.kredit_stav(con)["vycerpany"]:
                return naklady.SPRAVA_KREDIT_AKCIE
    except Exception:                      # diagnostika nesmie zhodiť odpoveď
        pass
    return "Aktuálne letákové dáta sa obnovujú. Skús to o chvíľu."


def pending_payload(job):
    return {
        "prazdny": True,
        "status": "preparing",
        "job_id": job.id,
        "retry_after": PLAN_JOB_RETRY_AFTER,
        "message": SPRAVA_PLAN_PRIPRAVUJEME,
    }


def failed_payload(status, retry_allowed):
    return {
        "prazdny": True,
        "status": "failed",
        "job_id": status.id,
        "code": status.error_code or KOD_PLAN_ZLYHAL,
        "message": (
            naklady.SPRAVA_KREDIT
            if status.error_code == naklady.KOD_KREDIT
            else SPRAVA_PLAN_ZLYHAL
        ),
        "retry_allowed": retry_allowed,
    }


def _job_status_response(status, retry_allowed):
    if status is None:
        return None
    if status.state in ("queued", "running"):
        return JSONResponse(status_code=202, content=pending_payload(status))
    if status.state == "failed":
        return failed_payload(status, retry_allowed)
    if status.state == "ready":
        payload = failed_payload(status, False)
        payload["code"] = "plan_result_missing"
        return payload
    return None


def _job_payload(obchody, frekvencia, adults, children, *, spajza=(), zo_spajze=False):
    payload = {
        "stores": list(obchody),
        "frequency": frekvencia,
        "adults": adults,
        "children": children,
        "algo_version": PLAN_ALGO_VERSION,
    }
    if zo_spajze:
        payload["pantry_signature"] = podpis_spajze(spajza)
    return payload


def _enqueue_live_plan(u, tyz, obchody, podpis, variant, premium, *,
                       spajza=(), zo_spajze=False, job_key=None,
                       is_force=False):
    adults, children = zlozenie_domacnosti(u)
    den = dnesok()
    strop = limit_prepoctov(premium)
    kind = "pantry" if zo_spajze else "regular"
    key = job_key or f"{kind}:{podpis}:{variant}"
    request = JobRequest(
        job_key=key,
        signature=podpis,
        variant=variant,
        kind=kind,
        user_id=u["id"],
        week=tyz,
        priority=PLAN_JOB_PRIORITY_LIVE,
        payload=_job_payload(
            obchody, u["frekvencia"], adults, children,
            spajza=spajza, zo_spajze=zo_spajze,
        ),
        regeneration_limit=strop,
        regeneration_day=den,
        is_force=is_force,
    )
    try:
        with closing(db()) as con:
            result = plan_jobs.enqueue(
                con, request, now=datetime.datetime.now().astimezone()
            )
    except plan_jobs.RegenerationLimitReached:
        return odmietni(
            429, sprava_o_limite(strop, premium), KOD_LIMIT_PREPOCTOV,
            premium=premium, limit_prepoctov=strop, zostava_prepoctov=0,
            obnova=zajtrajsok(den),
        )
    except naklady.RozpocetVycerpany as refusal:
        return odmietni(503, str(refusal), refusal.kod)
    if result.created:
        with closing(db()) as con:
            con.execute("DELETE FROM prepocty WHERE den<>?", (den,))
            con.commit()
    return JSONResponse(status_code=202, content=pending_payload(result.job))


def _current_job_status(con, user_id, tyz, podpis, pantry_podpis, variant):
    statuses = [
        plan_jobs.latest_user_request(
            con,
            user_id=user_id,
            signature=podpis,
            variant=variant,
            kind="regular",
            week=tyz,
            is_force=True,
        ),
        plan_jobs.latest_user_request(
            con,
            user_id=user_id,
            signature=pantry_podpis,
            variant=variant,
            kind="pantry",
            week=tyz,
            is_force=False,
        ),
        plan_jobs.latest_shared_regular_request(
            con,
            signature=podpis,
            variant=variant,
            week=tyz,
        ),
    ]
    return max((status for status in statuses if status is not None), key=lambda item: item.id,
               default=None)


def _job_is_at_least_as_new_as_cache(status, cached_created):
    if status is None or not cached_created:
        return status is not None
    try:
        job_created = datetime.datetime.fromisoformat(status.created)
        if job_created.tzinfo is None:
            job_created = job_created.astimezone()
        cache_created = datetime.datetime.fromisoformat(cached_created)
        if cache_created.tzinfo is None:
            cache_created = cache_created.replace(tzinfo=datetime.timezone.utc)
        return job_created.astimezone(datetime.timezone.utc) >= cache_created.astimezone(
            datetime.timezone.utc
        )
    except (TypeError, ValueError):
        return True


def _retry_allowed(con, u, status, premium, sp):
    if status is None or status.error_code in PLAN_JOB_NON_RETRYABLE_CODES:
        return False
    if status.kind == "pantry" and (not premium or not sp):
        return False
    if pouzite_prepocty(con, u["id"], dnesok()) >= limit_prepoctov(premium):
        return False
    try:
        naklady.skontroluj(
            con,
            "plan",
            odhad_eur=status.reserved_eur,
            teraz=datetime.datetime.now().astimezone(),
            rezervovane_eur=plan_jobs.active_reservations_eur(con),
        )
    except naklady.RozpocetVycerpany:
        return False
    return True


@app.post("/api/plan/generuj")
def generuj_plan(req: Request, force: int = 0):
    u = require_user(req)
    adults, children = zlozenie_domacnosti(u)
    tyz = monday()
    obchody = u["obchody"].split(",")
    rows = akcie_pre(obchody)
    if len(rows) < MIN_OFFERS_FOR_PLAN:
        raise HTTPException(503, sprava_o_chybajucich_akciach())

    with closing(db()) as con:
        premium = je_premium(con, u["id"])
        sp = spajza_pouzivatela(con, u["id"], premium)
        if not force:
            r = con.execute("SELECT json FROM plany WHERE user_id=? AND tyzden=?",
                            (u["id"], tyz)).fetchone()
            if r:
                try:
                    cached = json.loads(r["json"])
                except json.JSONDecodeError:
                    cached = None
                if osobna_cache_plati(cached, sp) and cached_plan_is_current(cached, rows):
                    return so_spajzou(cached, sp)
                con.execute("DELETE FROM plany WHERE user_id=? AND tyzden=?", (u["id"], tyz))
                con.commit()
                if cached and osobna_cache_plati(cached, sp):
                    raise HTTPException(503, "Aktuálny plán už obsahuje neplatnú ponuku. Skús to o chvíľu.")

    # Plán závisí len od profilu a ponúk — špajza doň nevstupuje, takže sa
    # rovnaká domácnosť trafí do zdieľanej cache aj s plnou špajzou a čaká
    # milisekundy namiesto minút. Špajza sa dopočíta až nad nákupným zoznamom.
    # „Vygeneruj mi iný" (force) sa cache musí vyhnúť, inak by nič nezmenilo.
    podpis = podpis_planu(
        tyz, obchody, u["frekvencia"], rows, sp,
        adults=adults, children=children,
    )
    variant = plan_variant_for(u["id"], PLAN_VARIANTS)
    with closing(db()) as con:
        # Agregovaná evidencia dopytu: KOĽKO ráz taký profil niekto chcel.
        # Bez user_id a bez e-mailu — je to podklad pre nočné zahrievanie
        # (predpocet.py), nie záznam o človeku. Nikdy nesmie zhodiť požiadavku.
        predpocet.zaznamenaj_dopyt(
            con, tyz, obchody, dospeli=adults, deti=children,
            frekvencia=u["frekvencia"], variant=variant,
        )
        if not force:
            zdielany = nacitaj_zdielany_plan(con, podpis, variant)
            if zdielany is not None:
                if cached_plan_is_current(zdielany, rows):
                    predpocet.zapocitaj_zasah(con, podpis, variant, tyz)
                    plan = prevezmi_zdielany_plan(con, u["id"], tyz, zdielany, sp)
                    con.commit()
                    return plan
                con.execute(
                    "DELETE FROM plany_zdielane WHERE podpis=? AND variant=?", (podpis, variant)
                )
        con.commit()

    return _enqueue_live_plan(
        u, tyz, obchody, podpis, variant, premium, spajza=sp,
        is_force=bool(force),
    )


def zaplat_a_poskladaj(u, tyz, obchody, rows, sp, podpis, variant, premium, zo_spajze=False):
    """Odtiaľto ďalej sa platí — spoločná brána pre obe cesty ku skladaniu.

    Každé skladanie, ktoré sa naozaj dostane k modelu, zaberie jedno miesto
    z dnešného stropu — zadarmo raz, s Premium päťkrát. Platí to aj pre
    výslovné „navrhni jedlá z toho, čo mám doma": je to rovnako drahé volanie.
    Plán podaný z cache (osobnej či zdieľanej) sa sem nikdy nedostane, takže
    čítanie hotového jedálnička nie je ničím obmedzené.

    Miesto v poole sa berie EŠTE PRED rezerváciou prepočtu. Kto sa nedostane
    dnu, nesiahol na model ani na svoj denný strop — odchádza s hláškou a
    s nedotknutým nárokom. Opačné poradie by ľuďom bralo prepočty za našu
    záťaž. `blocking=False`: radšej úprimné „o minútu" než tiché visenie.
    """
    if not PLAN_MIESTA.acquire(blocking=False):
        LOG.warning("plán odmietnutý — plno (%d súbežných)", PLAN_SUBEZNE_MAX)
        return odmietni(503, SPRAVA_PLAN_ZANEPRAZDNENY, KOD_PLAN_ZANEPRAZDNENY)
    try:
        den = dnesok()
        strop = limit_prepoctov(premium)
        if rezervuj_prepocet(u["id"], strop, den) is None:
            return odmietni(
                429, sprava_o_limite(strop, premium), KOD_LIMIT_PREPOCTOV,
                premium=premium, limit_prepoctov=strop, zostava_prepoctov=0,
                obnova=zajtrajsok(den),
            )
        try:
            return poskladaj_novy_plan(u, tyz, obchody, rows, sp, podpis, variant, zo_spajze)
        except BaseException:
            vrat_prepocet(u["id"], den)
            raise
    finally:
        PLAN_MIESTA.release()


SPRAVA_SPAJZA_PRAZDNA = (
    "Špajza je prázdna, takže z čoho variť? Napíš do nej, čo máš doma, "
    "a skús to znova."
)
KOD_SPAJZA_PRAZDNA = "spajza_prazdna"


@app.post("/api/plan/zo-spajze")
def plan_zo_spajze(req: Request):
    """Výslovné „Navrhni jedlá z toho, čo mám doma".

    Toto je JEDINÁ cesta, ktorou sa špajza dostane do promptu a do podpisu.
    Bežný plán ju ignoruje práve preto, aby sa dal zdieľať a aby pridané
    vajíčko nikomu nepreskladalo týždeň bez vyzvania. Tento plán je z podstaty
    osobný, takže sa do zdieľanej tabuľky neukladá vôbec a stojí jeden prepočet
    z denného stropu.
    """
    u = require_user(req)
    adults, children = zlozenie_domacnosti(u)
    tyz = monday()
    obchody = u["obchody"].split(",")

    with closing(db()) as con:
        premium = je_premium(con, u["id"])
        if not premium:
            return odmietni(
                403, SPRAVA_SPAJZA_PREMIUM, KOD_SPAJZA_PREMIUM,
                premium=False, spajza_dostupna=False,
                spajza_ulozenych=pocet_ulozenej_spajze(con, u["id"]),
            )
        sp = spajza_pouzivatela(con, u["id"], premium)
    if not sp:
        return odmietni(400, SPRAVA_SPAJZA_PRAZDNA, KOD_SPAJZA_PRAZDNA)

    rows = akcie_pre(obchody)
    if len(rows) < MIN_OFFERS_FOR_PLAN:
        raise HTTPException(503, sprava_o_chybajucich_akciach())

    podpis = podpis_planu(
        tyz, obchody, u["frekvencia"], rows, sp,
        adults=adults, children=children, zo_spajze=True,
    )
    variant = plan_variant_for(u["id"], PLAN_VARIANTS)
    return _enqueue_live_plan(
        u, tyz, obchody, podpis, variant, premium,
        spajza=sp, zo_spajze=True,
        job_key=f"pantry:{u['id']}:{podpis}:{variant}",
    )


class StalePlanJob(ValueError):
    """A queued job no longer describes inputs that are safe to dispatch."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


class WorkerLeaseLostAfterDispatch(RuntimeError):
    pass


def _new_plan_model_client():
    import anthropic

    return anthropic.Anthropic(
        api_key=env("ANTHROPIC_API_KEY"),
        timeout=PLAN_TIMEOUT_SECONDS,
        max_retries=PLAN_MAX_RETRIES,
    )


def _job_profile(job):
    payload = job.payload
    stores = payload.get("stores")
    if not isinstance(stores, list) or not stores:
        raise StalePlanJob("invalid_profile")
    frequency = payload.get("frequency")
    adults = payload.get("adults")
    children = payload.get("children")
    if (any(isinstance(value, bool) or not isinstance(value, int)
            for value in (frequency, adults, children))
            or frequency not in (1, 2, 3)
            or adults < 0 or children < 0 or not 1 <= adults + children <= 12):
        raise StalePlanJob("invalid_profile")
    return stores, frequency, adults, children


def _current_job_context(job, stores, frequency, adults, children, *, con, now):
    payload = job.payload
    today = now.date() if isinstance(now, datetime.datetime) else now
    if job.week != monday(today):
        raise StalePlanJob("stale_week")
    if payload.get("algo_version") != PLAN_ALGO_VERSION:
        raise StalePlanJob("stale_algorithm")

    missing_stores = stores_missing_this_week(con, stores, today)
    if missing_stores:
        raise StalePlanJob("incomplete_stores")
    rows = measurable_offers(offers_for_current_week(con, stores, today))
    # `stores_missing_this_week` vyššie stráži, že zber každého vybraného
    # obchodu naozaj dobehol. Ak však jeden leták nemá ani jednu položku s
    # kúpiteľnou jednotkou alebo balením, plán smie použiť ostatné obchody;
    # inak by chybná jednotka zablokovala celý nákup.
    if len(rows) < MIN_OFFERS_FOR_PLAN:
        raise StalePlanJob("incomplete_stores")

    pantry = []
    if job.user_id is not None:
        pantry = spajza_pouzivatela(con, job.user_id, je_premium(con, job.user_id))
    pantry_driven = job.kind == "pantry"
    pantry_signature = podpis_spajze(pantry) if pantry_driven else None
    if pantry_driven and payload.get("pantry_signature") != pantry_signature:
        raise StalePlanJob("stale_pantry")

    current_signature = podpis_planu(
        job.week, stores, frequency, rows, pantry,
        adults=adults, children=children, zo_spajze=pantry_driven,
    )
    if current_signature != job.signature:
        raise StalePlanJob("stale_signature")

    identity_facts = {
        "week": monday(today),
        "stores": sorted(stores),
        "complete_stores": sorted(set(stores) - set(missing_stores)),
        "offer_keys": sorted(row["offer_key"] for row in rows),
        "signature": current_signature,
        "algo_version": PLAN_ALGO_VERSION,
        "portion_standard_version": PORTION_STANDARD_VERSION,
        "pantry_signature": pantry_signature,
    }
    canonical = json.dumps(
        identity_facts, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )
    identity = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return rows, pantry, identity


def _job_context(job, client):
    stores, frequency, adults, children = _job_profile(job)
    payload = job.payload

    if payload.get("_compat_rows") is not None:
        return (
            stores, frequency, adults, children,
            payload["_compat_rows"], payload.get("_pantry", []), None,
        )

    now = getattr(client, "job_now", None)
    with closing(db()) as con:
        rows, pantry, identity = _current_job_context(
            job, stores, frequency, adults, children,
            con=con, now=now or datetime.datetime.now(),
        )
    return stores, frequency, adults, children, rows, pantry, identity


def revalidate_job_context(job, expected_identity, *, con, now):
    """Require the mutable job inputs to match one validated snapshot exactly."""
    stores, frequency, adults, children = _job_profile(job)
    _rows, _pantry, current_identity = _current_job_context(
        job, stores, frequency, adults, children, con=con, now=now,
    )
    if current_identity != expected_identity:
        raise StalePlanJob("stale_context")


def build_and_store_job(job, *, client=None) -> dict:
    """Build one plan; one semantic correction is allowed after a complete response."""
    stores, frequency, adults, children, rows, pantry, context_identity = _job_context(job, client)
    bind_context = getattr(client, "bind_job_context", None)
    if bind_context is not None:
        bind_context(context_identity)
    pantry_driven = job.kind == "pantry"
    purpose = "predpocet" if job.kind == "precompute" else "plan"
    prompt_source = measurable_offers(rows)
    if not prompt_source:
        raise HTTPException(503, "Z aktuálnych akcií sa nedajú spoľahlivo vypočítať množstvá.")
    prompt_rows = select_offers(prompt_source, stores, limit=120)
    blocks = personal_plan_messages(
        rows, frequency, pantry if pantry_driven else (), household_size=None,
        variant=job.variant, pantry_driven=pantry_driven,
        prompt_rows=prompt_rows, adults=adults, children=children,
    )
    settings = {"output_config": plan_output_config(PLAN_EFFORT)}

    with closing(db()) as accounts:
        own_reservation = getattr(job, "reserved_eur", None)
        queued_reservations = lambda: plan_jobs.active_reservations_eur(
            accounts, exclude_job_id=getattr(job, "id", None),
        )
        try:
            naklady.skontroluj(
                accounts,
                purpose,
                odhad_eur=own_reservation,
                rezervovane_eur=queued_reservations(),
            )
        except naklady.RozpocetVycerpany as refusal:
            error = HTTPException(503, str(refusal))
            error.kod = refusal.kod
            raise error
        raw_client = client or _new_plan_model_client()
        prepare = getattr(raw_client, "prepare", None)
        if prepare is not None:
            prepare(_new_plan_model_client)
        guarded = naklady.strazeny_klient(
            accounts,
            raw_client,
            purpose,
            odhad_eur=own_reservation,
            rezervovane_eur=queued_reservations,
        )
        messages = [{"role": "user", "content": blocks}]
        plan = None
        for attempt in range(MODEL_VALIDATION_ATTEMPTS):
            try:
                msg = guarded.messages.create(
                    model=MODEL_PLAN,
                    max_tokens=PLAN_TOKENS,
                    messages=messages,
                    **settings,
                )
            except naklady.KreditVycerpany as refusal:
                LOG.warning("plán sa neposkladal: %s", naklady.KOD_KREDIT)
                error = HTTPException(503, str(refusal))
                error.kod = refusal.kod
                raise error
            except naklady.RozpocetVycerpany as refusal:
                error = HTTPException(503, str(refusal))
                error.kod = refusal.kod
                raise error
            except Exception as error:
                try:
                    import anthropic
                    plan_timeout = getattr(anthropic, "APITimeoutError", None)
                except ImportError:
                    plan_timeout = None
                if plan_timeout is not None and isinstance(error, plan_timeout):
                    raise HTTPException(504, SPRAVA_PLAN_TRVA_PRIDLHO)
                raise

            LOG.info("plán poskladaný, tokeny: %s", pouzitie_modelu(getattr(msg, "usage", None)))
            if getattr(msg, "stop_reason", None) == "max_tokens":
                raise HTTPException(500, SPRAVA_PLAN_NEDOKONCENY)
            text = "".join(
                block.text for block in msg.content if getattr(block, "type", None) == "text"
            ).strip()
            text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.M).strip()
            try:
                model_output = json.loads(text)
            except json.JSONDecodeError as invalid:
                validation_error = ValueError("Výstup nie je platný JSON.")
            else:
                try:
                    with closing(db()) as validation_con:
                        plan = build_personal_plan(
                            validation_con, model_output, stores, frequency, None,
                            pantry=pantry if pantry_driven else (),
                            adults=adults, children=children,
                        )
                except ValueError as invalid:
                    validation_error = invalid
                else:
                    break

            LOG.warning("modelový plán neprešiel bezpečnostnou kontrolou: %s", validation_error)
            if attempt + 1 >= MODEL_VALIDATION_ATTEMPTS:
                if isinstance(validation_error, PlanDiversityError):
                    with closing(db()) as validation_con:
                        plan = build_personal_plan(
                            validation_con, model_output, stores, frequency, None,
                            pantry=pantry if pantry_driven else (),
                            adults=adults, children=children, enforce_diversity=False,
                        )
                    LOG.warning(
                        "model po oprave nesplnil iba rozmanitosť; "
                        "rozmanitosť ostala odporúčaním a bezpečný plán sa použil"
                    )
                    break
                raise HTTPException(500, "Plán sa nepodarilo bezpečne overiť, skús to znova.")
            messages = [
                {"role": "user", "content": blocks},
                {"role": "assistant", "content": text},
                {"role": "user", "content": (
                    "Predchádzajúci návrh neprešiel bezpečnostnou kontrolou. "
                    f"Dôvod: {validation_error} Oprav túto chybu a vráť celý plán "
                    "znova iba ako JSON podľa pôvodnej schémy."
                )},
            ]

    if plan is None:  # obrana pri budúcej zmene slučky
        raise HTTPException(500, "Plán sa nepodarilo bezpečne overiť, skús to znova.")

    complete_job = getattr(client, "complete_job", None)
    revalidate_context = getattr(client, "revalidate_job_context", None)
    with closing(db()) as con:
        if complete_job is not None:
            con.execute("BEGIN IMMEDIATE")
        try:
            if revalidate_context is not None:
                revalidate_context(con)
            plan = osobny_plan_na_ulozenie(plan, pantry, pantry_driven)
            compatibility_call = job.payload.get("_compat_rows") is not None
            if job.user_id is not None and (pantry_driven or compatibility_call):
                con.execute(
                    "INSERT OR REPLACE INTO plany (user_id,tyzden,json) VALUES (?,?,?)",
                    (job.user_id, job.week, json.dumps(plan, ensure_ascii=False)),
                )
            if not pantry_driven:
                uloz_zdielany_plan(
                    con, job.signature, job.variant, job.week, plan,
                    predpocitany=job.kind == "precompute",
                )
            if complete_job is not None and not complete_job(con):
                raise WorkerLeaseLostAfterDispatch()
            con.commit()
        except BaseException:
            if con.in_transaction:
                con.rollback()
            raise
    return so_spajzou(plan, pantry)


def poskladaj_novy_plan(u, tyz, obchody, rows, sp, podpis, variant, zo_spajze=False):
    """Temporary synchronous compatibility wrapper until the API enqueues jobs."""
    from types import SimpleNamespace

    adults, children = zlozenie_domacnosti(u)
    job = SimpleNamespace(
        id=None,
        signature=podpis,
        variant=variant,
        kind="pantry" if zo_spajze else "regular",
        user_id=u["id"],
        week=tyz,
        lease_owner=None,
        payload={
            "stores": obchody,
            "frequency": u["frekvencia"],
            "adults": adults,
            "children": children,
            "algo_version": PLAN_ALGO_VERSION,
            "_compat_rows": rows,
            "_pantry": sp,
        },
    )
    return build_and_store_job(job)


@app.get("/api/plan")
def daj_plan(req: Request):
    u = require_user(req)
    tyz = monday()
    obchody = u["obchody"].split(",")
    adults, children = zlozenie_domacnosti(u)
    with closing(db()) as con:
        r = con.execute("SELECT json, vytvoreny FROM plany WHERE user_id=? AND tyzden=?",
                        (u["id"], tyz)).fetchone()
        cached = None
        if r:
            try:
                cached = json.loads(r["json"])
            except json.JSONDecodeError:
                pass
        rows = measurable_offers(
            offers_for_current_week(con, u["obchody"].split(","), datetime.date.today())
        )
        premium = je_premium(con, u["id"])
        sp = spajza_pouzivatela(con, u["id"], premium)
        podpis = podpis_planu(
            tyz, obchody, u["frekvencia"], rows, sp,
            adults=adults, children=children,
        )
        variant = plan_variant_for(u["id"], PLAN_VARIANTS)
        pantry_podpis = podpis_planu(
            tyz, obchody, u["frekvencia"], rows, sp,
            adults=adults, children=children, zo_spajze=True,
        )
        status = _current_job_status(
            con, u["id"], tyz, podpis, pantry_podpis, variant
        )
        if status is not None and status.state in ("queued", "running"):
            return JSONResponse(status_code=202, content=pending_payload(status))

        valid_cached = bool(
            cached and osobna_cache_plati(cached, sp) and cached_plan_is_current(cached, rows)
        )
        retry_allowed = _retry_allowed(con, u, status, premium, sp)
        if (
            status is not None
            and status.state == "failed"
            and _job_is_at_least_as_new_as_cache(status, r["vytvoreny"] if r else None)
        ):
            # Bežný plán je zdieľaný. Ak ho po našom zlyhaní úspešne vytvoril
            # iný rovnaký profil alebo nočný predpočet, platný výsledok má
            # prednosť pred starou chybovou stenou. Force a špajzový job sú
            # osobné požiadavky, preto pri nich starý výsledok nikdy nemaskuj.
            if status.kind == "regular" and not status.is_force:
                recovered = nacitaj_zdielany_plan(con, podpis, variant)
                if recovered is not None and cached_plan_is_current(recovered, rows):
                    predpocet.zapocitaj_zasah(con, podpis, variant, tyz)
                    plan = prevezmi_zdielany_plan(con, u["id"], tyz, recovered, sp)
                    con.commit()
                    return plan
            return failed_payload(status, retry_allowed)
        if valid_cached and not (
            status is not None
            and status.state == "ready"
            and status.kind != "pantry"
        ):
            # Špajza sa dopočíta až tu, pri každom čítaní nanovo — preto sa
            # zmena v špajzi prejaví okamžite a bez plateného prepočtu.
            return so_spajzou(cached, sp)

        invalidation = None
        stale_current_cache = False
        if r and not valid_cached:
            invalidation = obnova_neplatnej_osobnej_cache(cached, sp)
            stale_current_cache = bool(cached and osobna_cache_plati(cached, sp))
            con.execute("DELETE FROM plany WHERE user_id=? AND tyzden=?", (u["id"], tyz))
            con.commit()

        # A ready force/regular job published a new shared row in the same
        # transaction as its ready state. Adopt it before returning an older
        # personal cache; otherwise GET polling would stop on the old plan.
        zdielany = nacitaj_zdielany_plan(con, podpis, variant)
        if zdielany is not None:
            if cached_plan_is_current(zdielany, rows):
                predpocet.zapocitaj_zasah(con, podpis, variant, tyz)
                plan = prevezmi_zdielany_plan(con, u["id"], tyz, zdielany, sp)
                con.commit()
                return plan
            con.execute(
                "DELETE FROM plany_zdielane WHERE podpis=? AND variant=?", (podpis, variant)
            )
            con.commit()

        if valid_cached:
            return so_spajzou(cached, sp)
        response = _job_status_response(status, retry_allowed)
        if response is not None:
            return response
        if invalidation and not osobna_cache_plati(cached, sp):
            return {"prazdny": True, "vyzaduje_akciu": True, **invalidation}
        if stale_current_cache:
            raise HTTPException(
                503, "Aktuálny plán už obsahuje neplatnú ponuku. Skús to o chvíľu."
            )
        return {"prazdny": True}


@app.get("/api/akcie/pocet")
def pocet_akcii():
    today = datetime.date.today()
    with closing(db()) as con:
        rows = offers_for_current_week(con, ["Kaufland", "Tesco", "Lidl"], today)
    return {"tyzden": monday(today), "pocet": len(rows)}


@app.get("/api/health")
def health():
    """Čo naozaj beží: vydanie, týždeň a počet akcií.

    Nasadenie porovná `vydanie` s lokálnym súborom VERSION — tým odhalí
    čiastočne prenesený scp (trieda chyby, ktorá zhodila auth_data.py).
    Žiadne tajomstvá sa sem nedostanú.

    `naklady` ukazuje, koľko appka dnes a tento mesiac minula na AI a koľko
    z rozpočtu zostáva. Majiteľ tak vidí míňanie bez SSH — presne to mu
    chýbalo, keď mu kredit ticho zmizol za dva dni.

    `predpocet` hovorí, či nočné zahrievanie plánov beží: koľko profilov sa
    zahrialo, koľko to stálo a koľko živých generovaní sa vďaka tomu vôbec
    nekonalo. Bez toho čísla sa nedá rozhodnúť, či zahrievať viac alebo menej.

    `platby` sú dve čísla, ktoré musia byť nula: koľko surových tiel webhookov
    čaká na spracovanie a za koľko platieb dlhujeme zákazníkom vrátenie peňazí.
    Kým sú nenulové, niekto zaplatil a nedostal nič — a to sa nesmie dať
    prehliadnuť len preto, že sa majiteľ nemá ako prihlásiť na server.
    """
    today = datetime.date.today()
    with closing(db()) as con:
        rows = offers_for_current_week(con, ["Kaufland", "Tesco", "Lidl"], today)
        utrata = naklady.stav(con)
        zahrievanie = predpocet.stav(con)
        platby_stav = stav_dozoru(con)
        fronta_planov = plan_jobs.health(
            con, now=datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
        )
    return {"vydanie": release_id(), "tyzden": monday(today), "pocet": len(rows),
            "naklady": utrata, "predpocet": zahrievanie, "platby": platby_stav,
            "plan_queue": fronta_planov}


@app.get("/api/naklady")
def prehlad_nakladov(req: Request):
    """Podrobnejší pohľad na to, kam išli peniaze. Bez tajomstiev, bez SSH."""
    require_owner(req)
    with closing(db()) as con:
        return {
            **naklady.stav(con, limit_poslednych=20),
            "predpocet": predpocet.stav(con),
            "plan_queue": plan_jobs.health(
                con, now=datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
            ),
        }


def public_community(con) -> dict:
    accounts = int(con.execute("SELECT COUNT(*) FROM pouzivatelia").fetchone()[0])
    return {
        "accounts": accounts,
        "goal": COMMUNITY_GOAL,
        "visible": accounts >= COMMUNITY_VISIBILITY_THRESHOLD,
    }


@app.get("/api/public/landing")
def public_landing():
    try:
        payload = validate_landing_data(
            load_landing_data(LANDING_DATA), datetime.date.today()
        )
    except (FileNotFoundError, ValueError):
        raise HTTPException(503, "Aktuálne letákové dáta sa obnovujú.")

    try:
        with closing(db()) as con:
            community = public_community(con)
    except sqlite3.Error:
        community = {"visible": False}
    return {**payload, "community": community}


def _weekly_public_page(today: datetime.date | None = None):
    today = today or datetime.date.today()
    try:
        payload = load_landing_data(LANDING_DATA)
    except (FileNotFoundError, json.JSONDecodeError, UnicodeDecodeError):
        payload = None
    return render_weekly_page(payload, today=today)


@app.get("/co-varit-tento-tyzden")
def seo_weekly_page():
    page = _weekly_public_page()
    headers = {"Cache-Control": PUBLIC_CACHE_CONTROL}
    status_code = 200
    if not page.indexable:
        status_code = 503
        headers = {
            "Cache-Control": "no-store",
            "Retry-After": RETRY_AFTER_PUBLIC_DATA,
        }
    return HTMLResponse(page.html, status_code=status_code, headers=headers)


@app.get("/lacny-jedalnicek")
def seo_budget_page():
    page = render_evergreen_page("lacny-jedalnicek")
    return HTMLResponse(page.html, headers={"Cache-Control": PUBLIC_CACHE_CONTROL})


@app.get("/ako-varime-z-akcii")
def seo_flyer_method_page():
    page = render_evergreen_page("ako-varime-z-akcii")
    return HTMLResponse(page.html, headers={"Cache-Control": PUBLIC_CACHE_CONTROL})


@app.get("/robots.txt")
def robots_txt():
    return PlainTextResponse(ROBOTS_TXT, headers={"Cache-Control": PUBLIC_CACHE_CONTROL})


@app.get("/sitemap.xml")
def sitemap_xml():
    today = datetime.date.today()
    weekly_page = _weekly_public_page(today)
    weekly_modified = weekly_page.last_modified if weekly_page.indexable else None
    xml = render_sitemap(today, weekly_modified)
    return Response(xml, media_type="application/xml", headers={"Cache-Control": PUBLIC_CACHE_CONTROL})


# ---------------------------------------------------------------- platby
# Vypnuté, kým majiteľ nenastaví PLATBY_ZAPNUTE=1. Dovtedy sa nikomu nič
# neúčtuje a adresa poskytovateľa sa ani nezostaví.
def platby_su_zapnute() -> bool:
    return platby_zapnute(env("PLATBY_ZAPNUTE"))


def vyzaduj_zapnute_platby():
    if not platby_su_zapnute():
        raise HTTPException(503, SPRAVA_VYPNUTE)


@app.get("/api/platba/stav")
def platba_stav(req: Request):
    u = require_user(req)
    with closing(db()) as con:
        return stav_platieb(con, user_id=u["id"], zapnute=platby_su_zapnute())


@app.post("/api/platba/start")
def platba_start(req: Request):
    u = require_user(req)
    vyzaduj_zapnute_platby()
    with closing(db()) as con:
        if ma_narok(con, u["id"]):
            raise HTTPException(409, SPRAVA_UZ_MAS)
        volne = volne_miesta(con)
    if volne <= 0:
        raise HTTPException(409, SPRAVA_VYPREDANE)
    try:
        url = checkout_url(env("LEMON_CHECKOUT_URL"), user_id=u["id"], email=u["email"])
    except (PlatbyNenastavene, ValueError):
        raise HTTPException(503, SPRAVA_NENASTAVENE)
    return {"ok": True, "url": url, "volne_miesta": volne}


# Upozornenia majiteľovi idú tým istým ntfy kanálom, ktorý už sleduje (naklady.py
# a dozorca.sh). Text skladá platby.py a zámerne v ňom nie je e-mail, token ani
# úryvok logu: kanál je natvrdo v repozitári, teda verejne čitateľný.
def posli_upozornenie_majitelovi(sprava: dict) -> None:
    """Najlepšia snaha — upozornenie nikdy nesmie zhodiť spracovanie platby."""
    try:
        naklady.posli_ntfy(sprava)
    except Exception:
        pass


def _ohlas_majitelovi(con, druh, *, now, **kw) -> None:
    try:
        sprava = upozornenie_raz(con, druh, now=now, **kw)
    except (sqlite3.Error, OSError, ValueError):
        return
    if sprava is not None:
        posli_upozornenie_majitelovi(sprava)


def _napis_zakaznikovi(con, user_id, predmet: str, text: str) -> None:
    """Zákazník, ktorý zaplatil a nič nedostal, sa to musí dozvedieť od nás.

    Mlčať a peniaze si nechať nie je možnosť ani ľudsky, ani podľa európskych
    pravidiel. E-mail je najlepšia snaha: keď mailer nefunguje, správa ostáva
    v appke (`/api/platba/stav`) a majiteľ o prípade aj tak vie z ntfy.
    """
    if user_id is None:
        return
    try:
        komu = email_uctu(con, user_id)
    except (sqlite3.Error, OSError):
        return
    if not komu:
        return
    telo = f"Ahoj!\n\n{text}\n\nUvar.si — z letáka rovno na tanier\nhttps://uvar.si"
    html = (
        '<!DOCTYPE html><html lang="sk"><body style="margin:0;padding:28px;'
        'background:#FFFCF5;font-family:-apple-system,Segoe UI,Roboto,sans-serif;'
        'color:#14231C"><div style="max-width:460px;margin:0 auto;background:#fff;'
        'border:2px solid #14231C;padding:30px"><p>Ahoj!</p><p>'
        + text.replace("\n", "<br>")
        + '</p><p style="margin-top:24px">Uvar.si — z letáka rovno na tanier</p>'
        "</div></body></html>"
    )
    try:
        posli_mail(komu, predmet, telo, html)
    except Exception:
        pass


def _doriesit_udalost(con, vysledok: dict, now: float) -> None:
    """Čo sa musí stať navyše: upozorniť majiteľa a povedať pravdu zákazníkovi."""
    akcia = vysledok.get("akcia")
    objednavka = vysledok.get("objednavka")
    if akcia == AKCIA_NAD_KAPACITU:
        _ohlas_majitelovi(con, DRUH_NAD_KAPACITU, now=now, objednavka=objednavka)
        _napis_zakaznikovi(con, vysledok.get("user_id"), MAIL_PREDMET_NAD_KAPACITU,
                           SPRAVA_NAD_KAPACITU_ZAKAZNIK)
    elif vysledok.get("stav") == STAV_DUPLICITNY:
        _ohlas_majitelovi(con, DRUH_DUPLICITA, now=now, objednavka=objednavka)
        _napis_zakaznikovi(con, vysledok.get("user_id"), MAIL_PREDMET_DUPLICITA,
                           SPRAVA_DUPLICITA_ZAKAZNIK)
    elif akcia == AKCIA_IGNOROVANE:
        # Podpis sedel, ale appka s udalosťou nič neurobila. Buď je to cudzí
        # produkt v tom istom obchode, alebo nesedí LEMON_VARIANT_ID — a to
        # druhé znamená, že sa práve zahadzujú skutočné objednávky.
        _ohlas_majitelovi(con, DRUH_IGNOROVANE, now=now, typ=vysledok.get("typ"))


@app.post("/api/platba/webhook")
async def platba_webhook(req: Request):
    """Jediný vstup, ktorý smie udeliť nárok — a to len s platným podpisom.

    Vypnuté platby už NEZNAMENAJÚ 503. Poskytovateľ pri 503 doručenie pár ráz
    zopakuje a potom ho zahodí; jeden preklep v PLATBY_ZAPNUTE tak stál celú
    platbu. Telo sa preto odloží tak, ako prišlo, a spracuje sa neskôr —
    aj s overením podpisu, ktoré sa neobchádza ani o kúsok.
    """
    telo = await req.body()
    if len(telo) > MAX_TELO_WEBHOOKU:
        raise HTTPException(413, SPRAVA_VELKE_TELO)
    podpis = req.headers.get("X-Signature")
    if not platby_su_zapnute():
        # Tajomstvo sa tu zámerne NEČÍTA — vypnuté platby nesiahajú na LEMON_*.
        # Podpis sa overí až pri spracovaní (platby.spracuj_odlozene).
        if not hodnoverny_podpis(podpis):
            raise HTTPException(503, SPRAVA_VYPNUTE)
        return await anyio.to_thread.run_sync(_odloz_na_neskor, bytes(telo), podpis)
    if not overit_podpis(
        tajomstvo=env("LEMON_WEBHOOK_SECRET"),
        telo=telo,
        podpis=podpis,
    ):
        raise HTTPException(401, SPRAVA_NEPLATNY_PODPIS)
    try:
        payload = json.loads(telo)
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise HTTPException(400, SPRAVA_POKAZENE_TELO)
    now = AUTH_CLOCK()
    variant = env("LEMON_VARIANT_ID")

    def spracuj():
        with closing(db()) as con:
            try:
                vysledok = spracuj_udalost(
                    con, payload=payload, now=now, variant_id=variant
                )
            except UdalostNepouzitelna:
                # Podpis sedel, teda peniaze sú skutočné — len ich nemáme komu
                # priradiť. Telo si odložíme, aby sa dalo dohľadať, a majiteľ
                # sa to musí dozvedieť; inak tá platba mlčky zmizne.
                odloz_webhook(con, telo=bytes(telo), podpis=podpis, now=now,
                              dovod="nepouzitelna")
                _ohlas_majitelovi(con, DRUH_NEPOUZITELNA, now=now)
                raise
            _doriesit_udalost(con, vysledok, now)
            return {"ok": True, "akcia": vysledok["akcia"]}

    try:
        return await anyio.to_thread.run_sync(spracuj)
    except UdalostNepouzitelna:
        raise HTTPException(400, SPRAVA_NEPRIRADITELNA)


def _odloz_na_neskor(telo: bytes, podpis) -> dict:
    now = AUTH_CLOCK()
    with closing(db()) as con:
        stav = odloz_webhook(con, telo=telo, podpis=podpis, now=now)
        if stav["nove"]:
            _ohlas_majitelovi(con, DRUH_ODLOZENE, now=now,
                              pocet=pocet_cakajucich(con))
    return {"ok": True, "akcia": AKCIA_ODLOZENE}


# ---------------------------------------------------------------- statické
@app.get("/app")
def app_index():
    return FileResponse(os.path.join(STATIC, "app.html"))


if os.path.isdir(STATIC):
    app.mount("/static", StaticFiles(directory=STATIC), name="static")
