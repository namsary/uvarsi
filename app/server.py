#!/usr/bin/env python3
"""
Uvar.si — backend (FastAPI + SQLite).

Účty bez hesiel (magic link e-mailom), profil, špajza, generovanie osobného
týždenného plánu z databázy akcií (naplní ju zbierac_akcii.py raz týždenne).

Beh:  /opt/uvarsi/venv/bin/python -m uvicorn server:app --host 127.0.0.1 --port 8090
Závislosti: fastapi uvicorn itsdangerous
"""
import os, re, json, secrets, sqlite3, smtplib, datetime, hashlib
from email.message import EmailMessage
from contextlib import closing

from fastapi import FastAPI, Request, HTTPException, Response
from fastapi.responses import JSONResponse, FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from config import public_base_url
from landing_data import load_landing_data, validate_landing_data
from weekly_data import offers_for_current_week
from offer_data import migrate_akcie_schema
from plan_data import build_personal_plan, cached_plan_is_current, personal_plan_prompt

DB = os.environ.get("UVARSI_DB", "/opt/uvarsi/uvarsi.db")
STATIC = os.environ.get("UVARSI_STATIC", "/opt/uvarsi/app/static")
LANDING_DATA = os.environ.get("UVARSI_LANDING_DATA", "/var/lib/uvarsi/landing_data.json")
BASE_URL = public_base_url()
ENV_FILE = "/opt/uvarsi/uvarsi.env"
COOKIE = "uvarsi_session"

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
"""


def db():
    con = sqlite3.connect(DB, timeout=20)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    migrate_akcie_schema(con)
    return con


app = FastAPI(title="Uvar.si")


def user_from_request(req: Request):
    tok = req.cookies.get(COOKIE)
    if not tok:
        return None
    with closing(db()) as con:
        r = con.execute(
            "SELECT p.* FROM sedenia s JOIN pouzivatelia p ON p.id=s.user_id "
            "WHERE s.token=?", (tok,)).fetchone()
        return dict(r) if r else None


def require_user(req: Request):
    u = user_from_request(req)
    if not u:
        raise HTTPException(status_code=401, detail="Neprihlásený")
    return u


# ---------------------------------------------------------------- e-mail
def posli_mail(komu: str, predmet: str, telo: str, html: str = None):
    """Odosiela cez Resend API (RESEND_API_KEY v uvarsi.env).
    Bez kľúča beží dev režim — odkaz sa vypíše do logu."""
    key = env("RESEND_API_KEY")
    odosielatel = env("MAIL_FROM", "Uvar.si <info@uvar.si>")
    if not key:
        print(f"[MAIL:DEV] pre {komu}: {telo}", flush=True)
        return
    try:
        import requests
        r = requests.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {key}",
                     "Content-Type": "application/json"},
            json={"from": odosielatel, "to": [komu], "subject": predmet,
                  "text": telo, **({"html": html} if html else {})},
            timeout=20)
        if r.status_code >= 300:
            print(f"[MAIL:CHYBA] {r.status_code} {r.text[:200]}", flush=True)
    except Exception as e:
        print(f"[MAIL:CHYBA] {e}", flush=True)


# ---------------------------------------------------------------- auth
@app.post("/api/auth/request")
async def auth_request(req: Request):
    data = await req.json()
    email = (data.get("email") or "").strip().lower()
    if not re.match(r"^[^@\s]+@[^@\s]+\.[a-z]{2,}$", email):
        raise HTTPException(400, "Neplatný e-mail")
    tok = secrets.token_urlsafe(32)
    do = (datetime.datetime.now() + datetime.timedelta(minutes=30)).isoformat()
    with closing(db()) as con:
        con.execute("INSERT INTO tokeny (token,email,platny_do) VALUES (?,?,?)",
                    (tok, email, do))
        con.commit()
    link = f"{BASE_URL}/prihlasenie?token={tok}"
    text = (f"Ahoj!\n\nKlikni sem a si dnu (odkaz platí 30 minút):\n{link}\n\n"
            f"Ak si o prihlásenie nežiadal, tento e-mail pokojne ignoruj.\n\n"
            f"Uvar.si — z letáka rovno na tanier\nhttps://uvar.si")
    html = f"""<!DOCTYPE html><html lang="sk"><body style="margin:0;padding:28px;
background:#FFFCF5;font-family:-apple-system,Segoe UI,Roboto,sans-serif;color:#14231C">
<div style="max-width:460px;margin:0 auto;background:#fff;border:2px solid #14231C;padding:30px">
  <div style="font-size:22px;font-weight:800;letter-spacing:.04em;text-transform:uppercase;
    margin-bottom:22px">UVAR<span style="color:#E23A26">.SI</span></div>
  <h1 style="font-size:21px;margin:0 0 12px">Tvoje prihlásenie</h1>
  <p style="color:#5C6B62;line-height:1.6;margin:0 0 24px">Klikni na tlačidlo a si dnu.
     Žiadne heslo netreba. Odkaz platí 30 minút.</p>
  <a href="{link}" style="display:inline-block;background:#FFD400;color:#14231C;
     border:2px solid #14231C;padding:14px 24px;text-decoration:none;font-weight:700;
     letter-spacing:.04em;text-transform:uppercase;font-size:14px">Prihlásiť sa →</a>
  <p style="color:#5C6B62;font-size:13px;line-height:1.6;margin:26px 0 0">
     Ak si o prihlásenie nežiadal, tento e-mail pokojne ignoruj — nič sa nestane.</p>
  <hr style="border:0;border-top:1px dashed #C9C2B4;margin:24px 0">
  <p style="color:#5C6B62;font-size:12px;margin:0">Uvar.si — jedálniček a recepty z toho,
     čo je práve v akcii. <a href="https://uvar.si" style="color:#14231C">uvar.si</a></p>
</div></body></html>"""
    posli_mail(email, "Prihlásenie do Uvar.si", text, html)
    return {"ok": True}


@app.get("/prihlasenie")
def auth_verify(token: str):
    with closing(db()) as con:
        r = con.execute("SELECT * FROM tokeny WHERE token=?", (token,)).fetchone()
        if not r or r["platny_do"] < datetime.datetime.now().isoformat():
            return RedirectResponse("/?chyba=link", status_code=302)
        email = r["email"]
        con.execute("DELETE FROM tokeny WHERE token=?", (token,))
        u = con.execute("SELECT * FROM pouzivatelia WHERE email=?", (email,)).fetchone()
        if not u:
            cur = con.execute("INSERT INTO pouzivatelia (email) VALUES (?)", (email,))
            uid = cur.lastrowid
        else:
            uid = u["id"]
        ses = secrets.token_urlsafe(32)
        con.execute("INSERT INTO sedenia (token,user_id) VALUES (?,?)", (ses, uid))
        con.commit()
    resp = RedirectResponse("/app", status_code=302)
    resp.set_cookie(COOKIE, ses, max_age=60 * 60 * 24 * 365,
                    httponly=True, samesite="lax", secure=True)
    return resp


@app.post("/api/auth/logout")
def auth_logout(req: Request):
    tok = req.cookies.get(COOKIE)
    if tok:
        with closing(db()) as con:
            con.execute("DELETE FROM sedenia WHERE token=?", (tok,))
            con.commit()
    r = JSONResponse({"ok": True})
    r.delete_cookie(COOKIE)
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
    prompt = personal_plan_prompt(rows, u["frekvencia"], sp)
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
            plan = build_personal_plan(con, model_output, obchody, u["frekvencia"], pantry=sp)
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


@app.get("/api/public/landing")
def public_landing():
    try:
        return validate_landing_data(load_landing_data(LANDING_DATA), datetime.date.today())
    except (FileNotFoundError, ValueError):
        raise HTTPException(503, "Aktuálne letákové dáta sa obnovujú.")


# ---------------------------------------------------------------- statické
@app.get("/app")
def app_index():
    return FileResponse(os.path.join(STATIC, "app.html"))


if os.path.isdir(STATIC):
    app.mount("/static", StaticFiles(directory=STATIC), name="static")
