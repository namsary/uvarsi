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

import anyio.to_thread
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse, FileResponse, HTMLResponse, Response, PlainTextResponse
from fastapi.staticfiles import StaticFiles
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
    ClientIpRateLimiter,
    DeliveryError,
    EmailCooldown,
    EmailRequestInProgress,
    MagicTokenExpired,
    MagicTokenInvalid,
    ReservationInvalid,
    SESSION_TTL_SECONDS,
    cancel_magic_token_reservation,
    consume_magic_token,
    delete_session,
    migrate_auth_schema,
    normalize_email,
    promote_magic_token,
    reserve_magic_token,
    send_resend_message,
    user_for_session,
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
SESSION_MAX_AGE = SESSION_TTL_SECONDS
AUTH_CLOCK = time.time
# Single-worker beta guard only; the deployed edge still needs a shared limiter.
IP_REQUEST_LIMITER = ClientIpRateLimiter(
    max_requests=5, window_seconds=10 * 60, max_clients=10_000
)

AUTH_SUCCESS_MESSAGE = (
    "Poskytovateľ prijal žiadosť o prihlasovací e-mail. "
    "Odkaz bude platný 60 minút."
)
AUTH_PROVIDER_FAILURE_MESSAGE = (
    "Prihlasovací e-mail sa teraz nepodarilo odovzdať poskytovateľovi. "
    "Skús to znova o chvíľu."
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
  nazov TEXT NOT NULL
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


def migruj_schemu(con) -> None:
    """Celá schéma a všetky migrácie. Idempotentné, ale drahé — nie na požiadavku."""
    import plan_jobs

    con.executescript(SCHEMA)
    migrate_household_schema(con)
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
    priprav_databazu()
    zvys_strop_vlakien()
    yield


app = FastAPI(title="Uvar.si", lifespan=lifespan)


def set_session_cookie(response: Response, raw_session: str) -> None:
    response.set_cookie(
        COOKIE,
        raw_session,
        max_age=SESSION_MAX_AGE,
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
    if request.url.path == "/app" or request.url.path.startswith("/prihlasenie"):
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
def posli_mail(komu: str, predmet: str, telo: str, html: str):
    return send_resend_message(
        api_key=env("RESEND_API_KEY"),
        sender=env("MAIL_FROM", "Uvar.si <info@uvar.si>"),
        recipient=komu,
        subject=predmet,
        text=telo,
        html=html,
    )


# ---------------------------------------------------------------- auth
@app.post("/api/auth/request")
async def auth_request(req: Request):
    now = AUTH_CLOCK()
    client_ip = req.client.host if req.client else "unknown"
    if not IP_REQUEST_LIMITER.allow(client_ip, now):
        raise HTTPException(429, "Priveľa pokusov. Skús to znova o 10 minút.")
    try:
        data = await req.json()
        if not isinstance(data, dict):
            raise ValueError("JSON body must be an object")
        email = normalize_email(data.get("email"))
    except (ValueError, UnicodeDecodeError):
        raise HTTPException(400, "Zadaj platnú e-mailovú adresu.")

    def deliver(tok):
        link = f"{BASE_URL}/prihlasenie#token={tok}"
        text = (f"Ahoj!\n\nKlikni sem a potvrď prihlásenie (odkaz platí 60 minút):\n{link}\n\n"
                f"Ak si o prihlásenie nežiadal, tento e-mail pokojne ignoruj.\n\n"
                f"Uvar.si — z letáka rovno na tanier\nhttps://uvar.si")
        html = f"""<!DOCTYPE html><html lang="sk"><body style="margin:0;padding:28px;
background:#FFFCF5;font-family:-apple-system,Segoe UI,Roboto,sans-serif;color:#14231C">
<div style="max-width:460px;margin:0 auto;background:#fff;border:2px solid #14231C;padding:30px">
  <div style="font-size:22px;font-weight:800;letter-spacing:.04em;text-transform:uppercase;
    margin-bottom:22px">UVAR<span style="color:#E23A26">.SI</span></div>
  <h1 style="font-size:21px;margin:0 0 12px">Tvoje prihlásenie</h1>
  <p style="color:#5C6B62;line-height:1.6;margin:0 0 24px">Klikni na tlačidlo a potvrď prihlásenie.
     Žiadne heslo netreba. Odkaz platí 60 minút.</p>
  <a href="{link}" style="display:inline-block;background:#FFD400;color:#14231C;
     border:2px solid #14231C;padding:14px 24px;text-decoration:none;font-weight:700;
     letter-spacing:.04em;text-transform:uppercase;font-size:14px">Prihlásiť sa →</a>
  <p style="color:#5C6B62;font-size:13px;line-height:1.6;margin:26px 0 0">
     Ak si o prihlásenie nežiadal, tento e-mail pokojne ignoruj — nič sa nestane.</p>
  <hr style="border:0;border-top:1px dashed #C9C2B4;margin:24px 0">
  <p style="color:#5C6B62;font-size:12px;margin:0">Uvar.si — jedálniček a recepty z toho,
     čo je práve v akcii. <a href="https://uvar.si" style="color:#14231C">uvar.si</a></p>
</div></body></html>"""
        return posli_mail(email, "Prihlásenie do Uvar.si", text, html)

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


LOGIN_CONFIRMATION_PAGE = """<!doctype html>
<html lang="sk"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>Potvrdenie prihlásenia · Uvar.si</title>
<meta name="robots" content="noindex,nofollow,noarchive">
<style>
:root{--paper:#fffcf5;--ink:#14231c;--soft:#5c6b62;--yellow:#ffd400;--red:#e23a26}
*{box-sizing:border-box}body{margin:0;background:var(--paper);color:var(--ink);font-family:Arial,sans-serif}
main{max-width:520px;margin:9vh auto;padding:24px}.brand{font-size:24px;font-weight:900;text-transform:uppercase}
.brand em{color:var(--red);font-style:normal}.card{background:#fff;border:2px solid var(--ink);padding:28px;margin-top:24px}
h1{font-size:28px;margin:0 0 12px}p{color:var(--soft);line-height:1.6}.action{display:inline-block;border:2px solid var(--ink);background:var(--yellow);color:var(--ink);padding:14px 18px;font-weight:800;text-decoration:none;cursor:pointer}
#resend{display:none;margin-top:16px}.legacy #confirm{display:none}.legacy #resend{display:inline-block}.legacy #status{color:var(--red)}
</style></head><body><main><div class="brand">Uvar<em>.si</em></div>
<section class="card" id="panel"><h1>Potvrď prihlásenie</h1>
<p id="status">Odkaz sa použije až po tvojom potvrdení. Platí 60 minút.</p>
<button class="action" id="confirm" type="button">Potvrdiť prihlásenie</button>
<a class="action" id="resend" href="/app">Požiadať o nový odkaz</a></section></main>
<script>
const panel=document.getElementById('panel');const statusNode=document.getElementById('status');
const MAGIC_TOKEN_SESSION_KEY='uvarsi.auth.magic-token.v1';
const freshToken=new URLSearchParams(location.hash.slice(1)).get('token')||'';
function storedToken(){try{return sessionStorage.getItem(MAGIC_TOKEN_SESSION_KEY)||'';}catch(error){return '';}}
function rememberToken(value){try{sessionStorage.setItem(MAGIC_TOKEN_SESSION_KEY,value);}catch(error){}}
function forgetToken(){try{sessionStorage.removeItem(MAGIC_TOKEN_SESSION_KEY);}catch(error){}}
let token=freshToken||storedToken();
if(freshToken)rememberToken(freshToken);
history.replaceState(null,'',location.pathname);
if(!token){statusNode.textContent='Odkaz chýba alebo má starý formát. Požiadaj o nový prihlasovací odkaz.';panel.classList.add('legacy');}
document.getElementById('confirm').onclick=async()=>{
  const submittedToken=token;history.replaceState(null,'',location.pathname);
  try{
    const response=await fetch('/api/auth/verify',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({token:submittedToken})});
    const data=await response.json().catch(()=>({}));
    if(response.ok){token='';forgetToken();location.replace(data.redirect);return;}
    if(response.status===400||response.status===410){token='';forgetToken();}
    statusNode.textContent=data.detail||'Odkaz sa nepodarilo overiť. Požiadaj o nový.';
  }catch(error){statusNode.textContent='Overenie sa nepodarilo pripojiť. Požiadaj o nový odkaz.';}
  document.getElementById('confirm').style.display='none';document.getElementById('resend').style.display='inline-block';
};
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
    data = await req.json()
    try:
        with closing(db()) as con:
            session = consume_magic_token(
                con, raw_token=data.get("token"), now=AUTH_CLOCK()
            )
    except MagicTokenExpired:
        raise HTTPException(410, "Odkaz vypršal. Požiadaj o nový prihlasovací odkaz.")
    except MagicTokenInvalid:
        raise HTTPException(400, "Odkaz je neplatný alebo už bol použitý. Požiadaj o nový.")
    response = JSONResponse({"ok": True, "redirect": "/app"})
    set_session_cookie(response, session)
    return response


@app.post("/api/auth/logout")
def auth_logout(req: Request):
    tok = req.cookies.get(COOKIE)
    if tok:
        with closing(db()) as con:
            delete_session(con, tok)
    r = JSONResponse({"ok": True})
    r.delete_cookie(COOKIE, httponly=True, samesite="lax", secure=True)
    return r


# ---------------------------------------------------------------- profil
@app.get("/api/me")
def me(req: Request):
    u = user_from_request(req)
    if not u:
        return {"prihlaseny": False}
    den = dnesok()
    with closing(db()) as con:
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
    return {"prihlaseny": True, "id": u["id"], "email": u["email"],
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
