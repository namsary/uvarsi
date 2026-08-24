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
import os, re, json, sqlite3, datetime, threading, time
from contextlib import asynccontextmanager, closing

import anyio.to_thread
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse, FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
import db_rezim
import naklady
from config import public_base_url, release_id
from landing_data import load_landing_data, validate_landing_data
from weekly_data import offers_for_current_week
from offer_data import migrate_akcie_schema
from plan_data import (
    build_personal_plan,
    cached_plan_is_current,
    personal_plan_messages,
    plan_signature,
    plan_variant_for,
)
from platby import (
    MAX_TELO_WEBHOOKU,
    PlatbyNenastavene,
    SPRAVA_NEPLATNY_PODPIS,
    SPRAVA_VELKE_TELO,
    SPRAVA_NENASTAVENE,
    SPRAVA_NEPRIRADITELNA,
    SPRAVA_POKAZENE_TELO,
    SPRAVA_UZ_MAS,
    SPRAVA_VYPNUTE,
    SPRAVA_VYPREDANE,
    UdalostNepouzitelna,
    checkout_url,
    ma_narok,
    migrate_platby_schema,
    overit_podpis,
    platby_zapnute,
    spracuj_udalost,
    stav_platieb,
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
ENV_FILE = "/opt/uvarsi/uvarsi.env"
COOKIE = "uvarsi_session"
SESSION_MAX_AGE = 30 * 24 * 60 * 60
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
# Nameraný najhorší prípad odpovede: 5 jedál × 6 podrobných krokov × 4 položky
# ≈ 1 950 tokenov. max_tokens ohraničuje uvažovanie AJ odpoveď dokopy, takže
# strop musí nechať odpovedi dvojnásobnú rezervu — orezaný JSON už raz appku
# zhodil a stálo to platené volanie navyše.
PLAN_ODPOVED_TOKENY = 2600
PLAN_TOKENS = 8000
# Koľko model nad výberom jedál uvažuje. None = doterajšie správanie, teda
# východisková námaha modelu. Je to posledná veľká páka na dĺžku volania, ale
# meria sa len naživo: nižšia námaha síce skracuje čakanie, no plán musí stále
# prejsť overením receptov, inak si používateľ počká na opakovanie. Zmeň až
# podľa čísel z LOG-u nižšie ("plán poskladaný, tokeny: ...").
PLAN_EFFORT = None
# Najhorší prípad čakania musí byť jedno číslo, nie súčin skrytých pokusov:
# samotný timeout bez max_retries nechá SDK opakovať volanie (default 2×), takže
# jedno zaseknuté spojenie drží používateľa aj vyše šesť minút pri točiacom sa
# koliesku. Preto: žiadne tiché opakovanie, jeden pokus a jasná hláška.
PLAN_TIMEOUT_SECONDS = 120.0
PLAN_MAX_RETRIES = 0
PLAN_WORST_CASE_SECONDS = PLAN_TIMEOUT_SECONDS * (PLAN_MAX_RETRIES + 1)
SPRAVA_PLAN_TRVA_PRIDLHO = (
    "Jedálniček sa nestihol poskladať do dvoch minút. Skús to prosím znova."
)
SPRAVA_PLAN_NEDOKONCENY = (
    "Jedálniček sa nestihol dopísať do konca. Skús to prosím znova."
)

# Koľko ponúk vidí model a ako sa medzi ne delí miesto. Zoradenie podľa
# kategórie a až potom ceny znamenalo, že do promptu sa zmestilo 89 mias a
# 51 zelenín — a ani jedna mliečna, trvanlivá či pekárenská ponuka. Z toho sa
# týždenný jedálniček zložiť nedá, tak si každá kategória drží svoj podiel.
PLAN_OFFER_LIMIT = 140
CATEGORY_QUOTA = (
    ("maso", 3), ("zelenina", 3), ("mliecne", 2),
    ("trvanlive", 2), ("ovocie", 1), ("pecivo", 1), ("ine", 1),
)
MIN_OFFERS_FOR_PLAN = 15
# Rovnaký profil dostane rovnaký plán, takže sa počíta raz pre všetkých. Aby
# však susedia s rovnakou domácnosťou nemali bajt na bajt to isté menu, podpis
# sa delí na túto malú sadu variantov. PLAN_VARIANTS=1 = maximálne zdieľanie.
PLAN_VARIANTS = 3


# ---------------------------------------------------------------- env / util
def env(key, default=None):
    if os.environ.get(key):
        return os.environ[key]
    try:
        for line in open(ENV_FILE, encoding="utf-8"):
            if line.strip().startswith(key):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
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


def migruj_schemu(con) -> None:
    """Celá schéma a všetky migrácie. Idempotentné, ale drahé — nie na požiadavku."""
    con.executescript(SCHEMA)
    migrate_auth_schema(con)
    migrate_akcie_schema(con)
    migrate_platby_schema(con)
    naklady.migrate_naklady_schema(con)
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


def user_from_request(req: Request):
    tok = req.cookies.get(COOKIE)
    if not tok:
        return None
    with closing(db()) as con:
        return user_for_session(con, raw_session=tok, now=AUTH_CLOCK())


def require_user(req: Request):
    u = user_from_request(req)
    if not u:
        raise HTTPException(status_code=401, detail="Neprihlásený")
    return u


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
    return (dnes or datetime.date.today()).isoformat()


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
let token=new URLSearchParams(location.hash.slice(1)).get('token')||'';
history.replaceState(null,'',location.pathname);
if(!token){statusNode.textContent='Odkaz chýba alebo má starý formát. Požiadaj o nový prihlasovací odkaz.';panel.classList.add('legacy');}
document.getElementById('confirm').onclick=async()=>{
  let submittedToken=token;token='';history.replaceState(null,'',location.pathname);
  try{
    const response=await fetch('/api/auth/verify',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({token:submittedToken})});
    submittedToken='';const data=await response.json().catch(()=>({}));
    if(response.ok){location.replace(data.redirect);return;}
    statusNode.textContent=data.detail||'Odkaz sa nepodarilo overiť. Požiadaj o nový.';
  }catch(error){submittedToken='';statusNode.textContent='Overenie sa nepodarilo pripojiť. Požiadaj o nový odkaz.';}
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
    response.set_cookie(
        COOKIE,
        session,
        max_age=SESSION_MAX_AGE,
        httponly=True,
        samesite="lax",
        secure=True,
    )
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
    return {"prihlaseny": True, "id": u["id"], "email": u["email"], "osoby": u["osoby"],
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
    osoby = max(1, min(12, int(d.get("osoby", 4))))
    frek = max(1, min(7, int(d.get("frekvencia", 2))))
    allowed_stores = ("Kaufland", "Tesco", "Lidl")
    obchody = [store for store in allowed_stores if store in d.get("obchody", [])]
    if not obchody:
        obchody = ["Kaufland", "Tesco", "Lidl"]
    with closing(db()) as con:
        old = con.execute("SELECT osoby, frekvencia, obchody FROM pouzivatelia WHERE id=?", (u["id"],)).fetchone()
        changed = old is None or (old["osoby"], old["frekvencia"], old["obchody"]) != (osoby, frek, ",".join(obchody))
        con.execute("UPDATE pouzivatelia SET osoby=?,frekvencia=?,obchody=?,onboarding=1"
                    " WHERE id=?", (osoby, frek, ",".join(obchody), u["id"]))
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
def akcie_pre(obchody, limit=PLAN_OFFER_LIMIT):
    if not obchody:
        return []

    today = datetime.date.today()
    with closing(db()) as con:
        rows = offers_for_current_week(con, obchody, today)

    return vyvazene_ponuky(rows, limit)


def _kategoria(row):
    known = {name for name, _ in CATEGORY_QUOTA}
    return row["kategoria"] if row["kategoria"] in known else "ine"


def vyvazene_ponuky(rows, limit=PLAN_OFFER_LIMIT):
    """Najlacnejšie ponuky, ale zo všetkých kategórií naraz.

    V každom kole si kategória vezme svoj podiel od najlacnejšej ďalej. Mäso
    stále vedie, no už nevytlačí mliečne a pečivo úplne z promptu. Keď obchod
    v niektorej kategórii nič nemá, miesto pripadne ostatným — chudobný týždeň
    sa tým nezmenší. Výber je deterministický, aby zdieľaný podpis sedel.
    """
    buckets = {name: [] for name, _ in CATEGORY_QUOTA}
    for row in rows:
        buckets[_kategoria(row)].append(row)
    for bucket in buckets.values():
        bucket.sort(key=lambda row: (row["cena"], row["offer_key"]))

    order = {name: index for index, (name, _) in enumerate(CATEGORY_QUOTA)}
    cursors = {name: 0 for name in buckets}
    taken = []
    while len(taken) < limit:
        before = len(taken)
        for name, quota in CATEGORY_QUOTA:
            bucket = buckets[name]
            for _ in range(quota):
                if len(taken) >= limit or cursors[name] >= len(bucket):
                    break
                taken.append(bucket[cursors[name]])
                cursors[name] += 1
        if len(taken) == before:
            break
    return sorted(taken, key=lambda row: (order[_kategoria(row)], row["cena"]))


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


def podpis_planu(tyzden, obchody, osoby, frekvencia, rows, spajza):
    return plan_signature(
        tyzden, obchody, osoby, frekvencia, [row["offer_key"] for row in rows], spajza
    )


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


def uloz_zdielany_plan(con, podpis, variant, tyzden, plan):
    """Ulož plán bez špajze — tú si každý čitateľ opečiatkuje vlastnú."""
    zdielany = {key: value for key, value in plan.items() if key != "spajza"}
    con.execute(
        "INSERT OR REPLACE INTO plany_zdielane (podpis,variant,tyzden,json) VALUES (?,?,?,?)",
        (podpis, variant, tyzden, json.dumps(zdielany, ensure_ascii=False)),
    )
    con.execute("DELETE FROM plany_zdielane WHERE tyzden<>?", (tyzden,))


def prevezmi_zdielany_plan(con, user_id, tyzden, zdielany, spajza):
    plan = dict(zdielany)
    plan["spajza"] = list(spajza)
    con.execute(
        "INSERT OR REPLACE INTO plany (user_id,tyzden,json) VALUES (?,?,?)",
        (user_id, tyzden, json.dumps(plan, ensure_ascii=False)),
    )
    return plan


def zahrej_plan_pre_pouzivatela(user_id):
    """Po zmene profilu prevezmi hotový plán, ak už pre ten profil existuje.

    Zahriatie zadarmo: model sa nevolá, takže onboarding nikdy nespustí platené
    volanie bez vyzvania. Keď plán pre daný podpis ešte nikto nevygeneroval,
    ticho sa nestane nič a používateľ si ho vyžiada sám.
    """
    tyzden = monday()
    with closing(db()) as con:
        profil = con.execute(
            "SELECT osoby, frekvencia, obchody FROM pouzivatelia WHERE id=?", (user_id,)
        ).fetchone()
        if profil is None:
            return None
        spajza = spajza_pouzivatela(con, user_id, je_premium(con, user_id))

    obchody = profil["obchody"].split(",")
    rows = akcie_pre(obchody)
    if len(rows) < MIN_OFFERS_FOR_PLAN:
        return None
    podpis = podpis_planu(
        tyzden, obchody, profil["osoby"], profil["frekvencia"], rows, spajza
    )
    with closing(db()) as con:
        zdielany = nacitaj_zdielany_plan(con, podpis, plan_variant_for(user_id, PLAN_VARIANTS))
        if not zdielany or not cached_plan_is_current(zdielany, rows):
            return None
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


@app.post("/api/plan/generuj")
def generuj_plan(req: Request, force: int = 0):
    u = require_user(req)
    tyz = monday()
    obchody = u["obchody"].split(",")
    rows = akcie_pre(obchody)
    if len(rows) < MIN_OFFERS_FOR_PLAN:
        raise HTTPException(503, sprava_o_chybajucich_akciach())

    with closing(db()) as con:
        premium = je_premium(con, u["id"])
        if not force:
            r = con.execute("SELECT json FROM plany WHERE user_id=? AND tyzden=?",
                            (u["id"], tyz)).fetchone()
            if r:
                try:
                    cached = json.loads(r["json"])
                except json.JSONDecodeError:
                    cached = None
                if cached and cached_plan_is_current(cached, rows):
                    return cached
                con.execute("DELETE FROM plany WHERE user_id=? AND tyzden=?", (u["id"], tyz))
                con.commit()
                raise HTTPException(503, "Aktuálny plán už obsahuje neplatnú ponuku. Skús to o chvíľu.")
        sp = spajza_pouzivatela(con, u["id"], premium)

    # Plán závisí len od profilu, špajze a ponúk. Keď ho pre presne tú istú
    # kombináciu už niekto vygeneroval, čaká sa milisekundy namiesto minút.
    # „Vygeneruj mi iný" (force) sa cache musí vyhnúť, inak by nič nezmenilo.
    podpis = podpis_planu(tyz, obchody, u["osoby"], u["frekvencia"], rows, sp)
    variant = plan_variant_for(u["id"], PLAN_VARIANTS)
    if not force:
        with closing(db()) as con:
            zdielany = nacitaj_zdielany_plan(con, podpis, variant)
            if zdielany is not None:
                if cached_plan_is_current(zdielany, rows):
                    plan = prevezmi_zdielany_plan(con, u["id"], tyz, zdielany, sp)
                    con.commit()
                    return plan
                con.execute(
                    "DELETE FROM plany_zdielane WHERE podpis=? AND variant=?", (podpis, variant)
                )
                con.commit()

    # Odtiaľto ďalej sa platí. Každé skladanie, ktoré sa naozaj dostane k modelu,
    # zaberie jedno miesto z dnešného stropu — zadarmo raz, s Premium päťkrát.
    # Plán podaný z cache (osobnej či zdieľanej) sa sem nikdy nedostane, takže
    # čítanie hotového jedálnička nie je ničím obmedzené.
    # Miesto v poole sa berie EŠTE PRED rezerváciou prepočtu. Kto sa nedostane
    # dnu, nesiahol na model ani na svoj denný strop — odchádza s hláškou a
    # s nedotknutým nárokom. Opačné poradie by ľuďom bralo prepočty za našu
    # záťaž. `blocking=False`: radšej úprimné „o minútu" než tiché visenie.
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
            return poskladaj_novy_plan(u, tyz, obchody, rows, sp, podpis, variant)
        except BaseException:
            vrat_prepocet(u["id"], den)
            raise
    finally:
        PLAN_MIESTA.release()


def poskladaj_novy_plan(u, tyz, obchody, rows, sp, podpis, variant):
    """Jediné platené volanie v celej appke — volá sa až po rezervácii miesta."""
    # Rozpočet sa overuje EŠTE PRED vyrobením klienta. Keď je vyčerpaný, appka
    # to povie rovno a pravdivo — nikdy nepodstrčí starý či vymyslený plán.
    with closing(db()) as ucty:
        try:
            naklady.skontroluj(ucty, "plan")
        except naklady.RozpocetVycerpany as odmietnutie:
            raise HTTPException(503, str(odmietnutie))

        import anthropic
        client = naklady.strazeny_klient(
            ucty,
            anthropic.Anthropic(
                api_key=env("ANTHROPIC_API_KEY"),
                timeout=PLAN_TIMEOUT_SECONDS,
                max_retries=PLAN_MAX_RETRIES,
            ),
            "plan",
        )
        # Blok ponúk je pre celý týždeň rovnaký a tvorí ~92 % promptu, tak ide
        # dopredu a s cache_control — inak sa ako predpona cachovať nedá.
        blocks = personal_plan_messages(
            rows, u["frekvencia"], sp, household_size=u["osoby"], variant=variant
        )
        plan_timeout = getattr(anthropic, "APITimeoutError", None)
        nastavenie = {"output_config": {"effort": PLAN_EFFORT}} if PLAN_EFFORT else {}

        def poskladaj(**navyse):
            return client.messages.create(model=MODEL_PLAN, max_tokens=PLAN_TOKENS,
                                          messages=[{"role": "user", "content": blocks}], **navyse)

        try:
            try:
                msg = poskladaj(**nastavenie)
            except TypeError:
                # Staršie SDK output_config nepozná; plán je dôležitejší než námaha.
                msg = poskladaj()
        except naklady.KreditVycerpany as odmietnutie:
            # Došiel kredit na API. Nie je to náš výpadok ani chyba používateľa,
            # tak sa to povie rovno a po slovensky: 503 s pravdivým dôvodom.
            # Nikdy nie 500 („server má krátkodobý problém") — to by človeka
            # posielalo skúšať znova do niečoho, čo samo od seba neprejde, a
            # nikdy nie starý či vymyslený jedálniček.
            LOG.warning("plán sa neposkladal: %s", naklady.KOD_KREDIT)
            raise HTTPException(503, str(odmietnutie))
        except naklady.RozpocetVycerpany as odmietnutie:
            raise HTTPException(503, str(odmietnutie))
        except Exception as error:
            if plan_timeout is not None and isinstance(error, plan_timeout):
                raise HTTPException(504, SPRAVA_PLAN_TRVA_PRIDLHO)
            raise
    LOG.info("plán poskladaný, tokeny: %s", pouzitie_modelu(getattr(msg, "usage", None)))
    # Orezanú odpoveď nemá zmysel skladať: JSON by nedával zmysel a používateľ
    # by dostal iba nezrozumiteľnú chybu z parsovania.
    if getattr(msg, "stop_reason", None) == "max_tokens":
        raise HTTPException(500, SPRAVA_PLAN_NEDOKONCENY)
    txt = "".join(b.text for b in msg.content
                  if getattr(b, "type", None) == "text").strip()
    txt = re.sub(r"^```(?:json)?|```$", "", txt, flags=re.M).strip()
    try:
        model_output = json.loads(txt)
    except json.JSONDecodeError:
        raise HTTPException(500, "Plán sa nepodarilo poskladať, skús to znova.")

    with closing(db()) as con:
        try:
            plan = build_personal_plan(
                con, model_output, obchody, u["frekvencia"], u["osoby"], pantry=sp
            )
        except ValueError:
            raise HTTPException(500, "Plán sa nepodarilo bezpečne overiť, skús to znova.")
        # Odtlačok špajze, z ktorej je plán poskladaný. Vďaka nemu appka vie
        # ticho upozorniť, že sa špajza medzitým zmenila — bez prepočtu.
        plan["spajza"] = sp
        con.execute("INSERT OR REPLACE INTO plany (user_id,tyzden,json) VALUES (?,?,?)",
                    (u["id"], tyz, json.dumps(plan, ensure_ascii=False)))
        uloz_zdielany_plan(con, podpis, variant, tyz, plan)
        con.commit()
    return plan


@app.get("/api/plan")
def daj_plan(req: Request):
    u = require_user(req)
    with closing(db()) as con:
        r = con.execute("SELECT json FROM plany WHERE user_id=? AND tyzden=?",
                        (u["id"], monday())).fetchone()
        if not r:
            return {"prazdny": True}
        try:
            cached = json.loads(r["json"])
        except json.JSONDecodeError:
            cached = None
        rows = offers_for_current_week(con, u["obchody"].split(","), datetime.date.today())
        if cached and cached_plan_is_current(cached, rows):
            return cached
        con.execute("DELETE FROM plany WHERE user_id=? AND tyzden=?", (u["id"], monday()))
        con.commit()
    raise HTTPException(503, "Aktuálny plán už obsahuje neplatnú ponuku. Skús to o chvíľu.")


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
    """
    today = datetime.date.today()
    with closing(db()) as con:
        rows = offers_for_current_week(con, ["Kaufland", "Tesco", "Lidl"], today)
        utrata = naklady.stav(con)
    return {"vydanie": release_id(), "tyzden": monday(today), "pocet": len(rows),
            "naklady": utrata}


@app.get("/api/naklady")
def prehlad_nakladov():
    """Podrobnejší pohľad na to, kam išli peniaze. Bez tajomstiev, bez SSH."""
    with closing(db()) as con:
        return naklady.stav(con, limit_poslednych=20)


@app.get("/api/public/landing")
def public_landing():
    try:
        return validate_landing_data(load_landing_data(LANDING_DATA), datetime.date.today())
    except (FileNotFoundError, ValueError):
        raise HTTPException(503, "Aktuálne letákové dáta sa obnovujú.")


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


@app.post("/api/platba/webhook")
async def platba_webhook(req: Request):
    """Jediný vstup, ktorý smie udeliť nárok — a to len s platným podpisom."""
    vyzaduj_zapnute_platby()
    telo = await req.body()
    if len(telo) > MAX_TELO_WEBHOOKU:
        raise HTTPException(413, SPRAVA_VELKE_TELO)
    if not overit_podpis(
        tajomstvo=env("LEMON_WEBHOOK_SECRET"),
        telo=telo,
        podpis=req.headers.get("X-Signature"),
    ):
        raise HTTPException(401, SPRAVA_NEPLATNY_PODPIS)
    try:
        payload = json.loads(telo)
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise HTTPException(400, SPRAVA_POKAZENE_TELO)
    try:
        with closing(db()) as con:
            vysledok = spracuj_udalost(
                con, payload=payload, now=AUTH_CLOCK(), variant_id=env("LEMON_VARIANT_ID")
            )
    except UdalostNepouzitelna:
        raise HTTPException(400, SPRAVA_NEPRIRADITELNA)
    return {"ok": True, **vysledok}


# ---------------------------------------------------------------- statické
@app.get("/app")
def app_index():
    return FileResponse(os.path.join(STATIC, "app.html"))


if os.path.isdir(STATIC):
    app.mount("/static", StaticFiles(directory=STATIC), name="static")
