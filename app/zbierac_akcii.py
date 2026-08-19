#!/usr/bin/env python3
"""
Uvar.si — ZBIERAČ AKCIÍ (beží raz týždenne pre VŠETKÝCH používateľov).

Prečíta letáky (Kaufland, Tesco, Lidl) cez vision a uloží VŠETKY nájdené
potravinové akcie do SQLite. Osobné plány sa potom skladajú z tejto databázy
lacnými textovými volaniami — takže jeden drahý beh týždenne obslúži
neobmedzený počet používateľov.

Zdroje strán letákov: kupino.sk (primárne), mletaky.sk (záložné).
Beh:  /opt/uvarsi/venv/bin/python -u zbierac_akcii.py
"""
import os, re, json, base64, datetime, sqlite3, requests
from io import BytesIO

try:
    from offer_data import migrate_akcie_schema, replace_store_week
except ImportError:
    from app.offer_data import migrate_akcie_schema, replace_store_week

DB = os.environ.get("UVARSI_DB", "/opt/uvarsi/uvarsi.db")
ENV_FILE = "/opt/uvarsi/uvarsi.env"

MODEL_READ = "claude-opus-5"                 # najsilnejšia vision → presné ceny
READ_EFFORT = "high"
READ_TOKENS = 16000
MODEL_SCAN = "claude-haiku-4-5-20251001"     # lacné triedenie strán

STORES = ["kaufland", "tesco", "lidl"]
MAX_PAGES = 60
FOOD_CAP = 14            # koľko potravinových strán na obchod čítať
READ_PX = 1500
SCAN_PX = 320
SKIP_SLUG = ("nova-predajna", "brozura", "back-to-school", "special",
             "shop", "nabytok", "zahrada")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
H = {"User-Agent": UA}


def log(*a):
    print(*a, flush=True)


def load_key():
    if os.environ.get("ANTHROPIC_API_KEY"):
        return os.environ["ANTHROPIC_API_KEY"]
    for line in open(ENV_FILE, encoding="utf-8"):
        if line.strip().startswith("ANTHROPIC_API_KEY"):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise SystemExit("Chýba ANTHROPIC_API_KEY.")


# ---------------------------------------------------------------- databáza
SCHEMA = """
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
  created    TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_akcie_tyzden ON akcie(tyzden);
CREATE INDEX IF NOT EXISTS idx_akcie_kat ON akcie(tyzden, kategoria);
"""


def db():
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    migrate_akcie_schema(con)
    return con


def monday():
    t = datetime.date.today()
    return (t - datetime.timedelta(days=t.weekday())).isoformat()


# ---------------------------------------------------------------- zdroje strán
def kupino_meta(store):
    base = "https://www.kupino.sk"
    idx = requests.get(f"{base}/letaky/{store}", headers=H, timeout=20).text
    cands = re.findall(r'href="(/letak/' + store + r'-letak[a-z0-9-]*)"', idx)
    cands += re.findall(r'href="(/letak/[a-z0-9-]*' + store + r'[a-z0-9-]*)"', idx)
    slug = next((c for c in cands if not any(b in c for b in SKIP_SLUG)), None)
    if not slug:
        return None
    pg = requests.get(f"{base}{slug}/strana-2", headers=H, timeout=20).text
    om = re.search(r'img\.kupino\.sk/letaky/(\d+)/thumbs/([a-z0-9-]+)-1_320\.jpg', pg)
    return (om.group(1), om.group(2), slug) if om else None


def mletaky_base(store):
    html_ = requests.get(f"https://mletaky.sk/obchody/{store}", headers=H, timeout=20).text
    cands = set(re.findall(r'https?://app\.mletaky\.sk/(\d{6})_(\d{6})_'
                           + store + r'_([a-z0-9]+)', html_))
    today, best = datetime.date.today(), None
    for vto, vfrom, h in cands:
        try:
            d_from = datetime.datetime.strptime(vfrom, "%y%m%d").date()
        except ValueError:
            continue
        if d_from <= today and (best is None or d_from > best[0]):
            best = (d_from, f"https://app.mletaky.sk/{vto}_{vfrom}_{store}_{h}")
    return best[1] if best else None


def page_exists(url):
    try:
        r = requests.get(url, headers=H, timeout=25, stream=True, allow_redirects=False)
        ok = r.status_code == 200 and r.headers.get("content-type", "").startswith("image")
        r.close()
        return ok
    except Exception:
        return False


def store_pages(store):
    """(list[(thumb|None, full)], popis_zdroja)"""
    try:
        meta = kupino_meta(store)
    except Exception as e:
        log(f"[WARN] {store}: kupino zlyhalo ({e})")
        meta = None
    if meta:
        lid, name, slug = meta
        pages = []
        for n in range(1, MAX_PAGES + 1):
            t = f"https://img.kupino.sk/letaky/{lid}/thumbs/{name}-{n}_320.jpg"
            if page_exists(t):
                pages.append((t, f"https://img.kupino.sk/letaky/{lid}/{name}-{n}.jpg"))
            else:
                break
        if pages:
            return pages, f"kupino{slug}"
    try:
        base = mletaky_base(store)
    except Exception as e:
        log(f"[WARN] {store}: mletaky zlyhalo ({e})")
        base = None
    if base:
        pages, misses = [], 0
        for n in range(0, MAX_PAGES + 1):
            u = f"{base}/image{n:02d}.webp"
            if page_exists(u):
                pages.append((None, u))
                misses = 0
            else:
                misses += 1
                if misses >= 2 and pages:
                    break
        if pages:
            return pages, f"mletaky/{base.rsplit('/', 1)[-1]}"
    return [], None


def get_b64(url, max_px):
    from PIL import Image
    r = requests.get(url, headers=H, timeout=45, allow_redirects=True)
    if r.status_code != 200:
        return None
    im = Image.open(BytesIO(r.content)).convert("RGB")
    w, h = im.size
    s = min(1.0, max_px / max(w, h))
    if s < 1.0:
        im = im.resize((int(w * s), int(h * s)), Image.LANCZOS)
    buf = BytesIO()
    im.save(buf, format="JPEG", quality=82)
    return base64.standard_b64encode(buf.getvalue()).decode()


def img_block(b):
    return {"type": "image", "source": {
        "type": "base64", "media_type": "image/jpeg", "data": b}}


# ---------------------------------------------------------------- Claude
def claude_json(client, model, content, max_tokens, effort=None):
    kw = {"output_config": {"effort": effort}} if effort else {}
    try:
        msg = client.messages.create(model=model, max_tokens=max_tokens,
                                     messages=[{"role": "user", "content": content}], **kw)
    except TypeError:
        msg = client.messages.create(model=model, max_tokens=max_tokens,
                                     messages=[{"role": "user", "content": content}])
    if getattr(msg, "stop_reason", None) == "max_tokens":
        raise ValueError("odseknuté na max_tokens")
    txt = "".join(b.text for b in msg.content
                  if getattr(b, "type", None) == "text").strip()
    txt = re.sub(r"^```(?:json)?|```$", "", txt, flags=re.M).strip()
    return json.loads(txt)


SCAN_PROMPT = """Toto sú náhľady strán letáku. Pri každej je číslo. Vráť IBA JSON zoznam \
čísel strán, ktoré obsahujú POTRAVINY (mäso, hydina, ryby, zelenina, ovocie, mliečne, \
syry, vajcia, pečivo, ryža, cestoviny, múka, oleje, strukoviny, konzervy).
PRIORITA: strany s ČERSTVOU ZELENINOU, OVOCÍM a MÄSOM zaraď VŽDY — sú najdôležitejšie \
(spoznáš ich podľa fotiek surovín: paprika, paradajky, zemiaky, cibuľa, jablká, banány, \
kuracie, bravčové). Tieto strany často nemajú percentá, len veľkú cenu — aj tak ich zaraď.
Vynechaj drogériu, kozmetiku, textil, hračky, elektroniku, domácnosť, záhradu, nábytok. \
Formát: [1,2,5,6]"""

EXTRACT_PROMPT = """Toto sú potravinové strany letáku obchodu {store}. Vypíš VŠETKY \
potraviny s uvedenou cenou, ktoré na stranách vidíš. Vráť IBA čistý JSON pole:
[{{"nazov":"Bravčové plecko","kategoria":"maso","cena":2.15,"povodna":4.49,"zlava":"−52 %","jednotka":"kg"}}]
Pravidlá:
- kategoria: jedno z maso|zelenina|ovocie|mliecne|trvanlive|pecivo|ine
- cena = cena na cenovke ako číslo s bodkou; povodna = pôvodná cena (ak nie je, daj null)
- zlava: "−52 %" alebo "1+1" alebo null (ak zľava nie je uvedená)
- jednotka: kg|ks|l|balenie
- nazov krátky (max 30 znakov), slovenčina s diakritikou

DÔLEŽITÉ — nevynechaj čerstvé:
- ZELENINU a OVOCIE zapíš VŽDY, keď majú cenu, aj keď pri nich NIE JE percento zľavy \
(paprika, paradajky, uhorky, zemiaky, cibuľa, mrkva, kaleráb, jablká, banány, hrozno…). \
Sú to najdôležitejšie suroviny na varenie.
- To isté platí pre MÄSO a HYDINU s cenou bez percenta.
- Ak sú na strane rôzne druhy (napr. "jablká, hrušky"), zapíš ich ako samostatné položky.

- IBA potraviny. Žiadna drogéria, alkohol, krmivo, nepotravinový tovar.
- Ceny musia presne sedieť s letákom. Radšej položku vynechaj, než uhádni cenu."""


def zbieraj(client, store):
    pages, src = store_pages(store)
    if not pages:
        log(f"[WARN] {store}: leták nenájdený")
        return []
    # 1) lacný sken náhľadov → ktoré strany sú potravinové
    thumbs = []
    for i, (turl, furl) in enumerate(pages, start=1):
        try:
            b = get_b64(turl or furl, SCAN_PX)
        except Exception:
            b = None
        if b:
            thumbs.append((i, b))
    log(f"[INFO] {store}: {len(thumbs)} strán ({src}), skenujem…")
    if not thumbs:
        return []
    content = []
    for n, b in thumbs:
        content.append({"type": "text", "text": f"Strana {n}:"})
        content.append(img_block(b))
    content.append({"type": "text", "text": SCAN_PROMPT})
    try:
        food = [n for n in claude_json(client, MODEL_SCAN, content, 500)
                if 1 <= n <= len(pages)]
    except Exception as e:
        log(f"[WARN] {store}: sken zlyhal ({e}) — beriem prvých {FOOD_CAP}")
        food = [t[0] for t in thumbs][:FOOD_CAP]
    if len(food) > FOOD_CAP:                     # rovnomerná vzorka naprieč letákom
        step = len(food) / FOOD_CAP
        food = [food[int(i * step)] for i in range(FOOD_CAP)]
    log(f"[INFO] {store}: potravinové strany {food} — čítam…")

    # 2) presné čítanie cien (Opus 5 vision)
    content = []
    for n in food:
        try:
            b = get_b64(pages[n - 1][1], READ_PX)
        except Exception:
            b = None
        if b:
            content.append(img_block(b))
    if not content:
        return []
    content.append({"type": "text", "text": EXTRACT_PROMPT.format(store=store.upper())})
    try:
        items = claude_json(client, MODEL_READ, content, READ_TOKENS, effort=READ_EFFORT)
    except Exception as e:
        log(f"[WARN] {store}: extrakcia zlyhala ({e})")
        return []
    out = []
    for it in items:
        if not it.get("nazov") or it.get("cena") is None:
            continue
        out.append({
            "obchod": store.capitalize(),
            "nazov": str(it["nazov"])[:40],
            "kategoria": (it.get("kategoria") or "ine")[:20],
            "cena": float(it["cena"]),
            "povodna": float(it["povodna"]) if it.get("povodna") else None,
            "zlava": it.get("zlava"),
            "jednotka": (it.get("jednotka") or "")[:12],
        })
    log(f"[INFO] {store}: {len(out)} akcií")
    return out


def main():
    import anthropic
    client = anthropic.Anthropic(api_key=load_key(), timeout=180.0, max_retries=1)
    tyz = monday()
    con = db()
    total = 0
    for store in STORES:
        akcie = zbieraj(client, store)
        if not akcie:
            continue
        try:
            replace_store_week(con, tyz, store.capitalize(), akcie)
        except ValueError as e:
            log(f"[WARN] {store}: neoverené akcie neboli uložené ({e})")
            continue
        total += len(akcie)
    n = con.execute("SELECT COUNT(*) c FROM akcie WHERE tyzden=?", (tyz,)).fetchone()["c"]
    con.close()
    log(f"[OK] Týždeň {tyz}: uložených {total} akcií (v DB spolu {n}).")
    if n < 20:
        raise SystemExit("Málo akcií — niečo je zle.")


if __name__ == "__main__":
    main()
