#!/usr/bin/env python3
"""
Uvar.si — backend (FastAPI + SQLite).

Účty bez hesiel (magic link e-mailom), profil, špajza, generovanie osobného
týždenného plánu z databázy akcií (naplní ju zbierac_akcii.py raz týždenne).

Beh:  /opt/uvarsi/venv/bin/python -m uvicorn server:app --host 127.0.0.1 --port 8090
Závislosti: fastapi uvicorn itsdangerous
"""
import asyncio
import os, re, json, sqlite3, datetime, time
from contextlib import closing

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse, FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from config import public_base_url, release_id
from landing_data import load_landing_data, validate_landing_data
from weekly_data import offers_for_current_week
from offer_data import migrate_akcie_schema
from plan_data import build_personal_plan, cached_plan_is_current, personal_plan_prompt
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

MODEL_PLAN = "claude-sonnet-5"     # skladanie plánu = text, lacné
PLAN_TOKENS = 8000


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


def db():
    con = sqlite3.connect(DB, timeout=20)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    migrate_auth_schema(con)
    migrate_akcie_schema(con)
    migrate_platby_schema(con)
    return con


app = FastAPI(title="Uvar.si")


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
    with closing(db()) as con:
        sp = [r["nazov"] for r in con.execute(
            "SELECT nazov FROM spajza WHERE user_id=? ORDER BY id", (u["id"],))]
    return {"prihlaseny": True, "id": u["id"], "email": u["email"], "osoby": u["osoby"],
            "frekvencia": u["frekvencia"], "obchody": u["obchody"].split(","),
            "onboarding": bool(u["onboarding"]), "platiaci": bool(u["platiaci"]),
            "spajza": sp}


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
    return {"ok": True}


@app.post("/api/spajza")
async def uloz_spajzu(req: Request):
    u = require_user(req)
    d = await req.json()
    polozky = [str(x).strip()[:40] for x in d.get("polozky", []) if str(x).strip()][:60]
    with closing(db()) as con:
        old = [row["nazov"] for row in con.execute(
            "SELECT nazov FROM spajza WHERE user_id=? ORDER BY id", (u["id"],))]
        if old != polozky:
            con.execute("DELETE FROM spajza WHERE user_id=?", (u["id"],))
            con.executemany("INSERT INTO spajza (user_id,nazov) VALUES (?,?)",
                            [(u["id"], p) for p in polozky])
            con.execute("DELETE FROM plany WHERE user_id=? AND tyzden=?", (u["id"], monday()))
        con.commit()
    return {"ok": True, "pocet": len(polozky)}


# ---------------------------------------------------------------- plán
def akcie_pre(obchody, limit=140):
    if not obchody:
        return []

    today = datetime.date.today()
    with closing(db()) as con:
        rows = offers_for_current_week(con, obchody, today)

    category_order = {"maso": 1, "zelenina": 2, "mliecne": 3, "trvanlive": 4}
    return sorted(rows, key=lambda row: (category_order.get(row["kategoria"], 5), row["cena"]))[:limit]


@app.post("/api/plan/generuj")
def generuj_plan(req: Request, force: int = 0):
    u = require_user(req)
    tyz = monday()
    obchody = u["obchody"].split(",")
    rows = akcie_pre(obchody)
    if len(rows) < 15:
        raise HTTPException(503, "Aktuálne letákové dáta sa obnovujú. Skús to o chvíľu.")

    with closing(db()) as con:
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
        sp = [x["nazov"] for x in con.execute(
            "SELECT nazov FROM spajza WHERE user_id=?", (u["id"],))]

    import anthropic
    client = anthropic.Anthropic(api_key=env("ANTHROPIC_API_KEY"), timeout=120.0)
    prompt = personal_plan_prompt(rows, u["frekvencia"], sp, household_size=u["osoby"])
    msg = client.messages.create(model=MODEL_PLAN, max_tokens=PLAN_TOKENS,
                                 messages=[{"role": "user", "content": prompt}])
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
        con.execute("INSERT OR REPLACE INTO plany (user_id,tyzden,json) VALUES (?,?,?)",
                    (u["id"], tyz, json.dumps(plan, ensure_ascii=False)))
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
    """
    today = datetime.date.today()
    with closing(db()) as con:
        rows = offers_for_current_week(con, ["Kaufland", "Tesco", "Lidl"], today)
    return {"vydanie": release_id(), "tyzden": monday(today), "pocet": len(rows)}


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
