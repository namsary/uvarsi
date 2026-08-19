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
import os, re, json, base64, datetime, hashlib, sqlite3, requests
from io import BytesIO
from urllib.parse import urlparse

try:
    from offer_data import migrate_akcie_schema, replace_store_week, validate_offer
except ImportError:
    from app.offer_data import migrate_akcie_schema, replace_store_week, validate_offer

DB = os.environ.get("UVARSI_DB", "/opt/uvarsi/uvarsi.db")
ENV_FILE = "/opt/uvarsi/uvarsi.env"

MODEL_READ = "claude-opus-5"                 # najsilnejšia vision → presné ceny
READ_EFFORT = "high"
READ_TOKENS = 16000
MODEL_SCAN = "claude-haiku-4-5-20251001"     # lacné triedenie strán

STORES = ["kaufland", "tesco", "lidl"]
SCAN_BATCH_SIZE = 12
READ_BATCH_SIZE = 4
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
  offer_key TEXT,
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
def parse_finite_validity(text):
    if not isinstance(text, str):
        raise ValueError("zdroj nemá konečnú platnosť")

    iso = re.search(r"(\d{4}-\d{2}-\d{2}).{0,40}?(\d{4}-\d{2}-\d{2})", text)
    european = re.search(
        r"(\d{1,2})\.\s*(\d{1,2})\.\s*(\d{4}).{0,40}?"
        r"(\d{1,2})\.\s*(\d{1,2})\.\s*(\d{4})",
        text,
    )
    try:
        if iso:
            valid_from = datetime.date.fromisoformat(iso.group(1))
            valid_to = datetime.date.fromisoformat(iso.group(2))
        elif european:
            valid_from = datetime.date(int(european.group(3)), int(european.group(2)), int(european.group(1)))
            valid_to = datetime.date(int(european.group(6)), int(european.group(5)), int(european.group(4)))
        else:
            raise ValueError("zdroj nemá konečnú platnosť")
    except ValueError as exc:
        raise ValueError("zdroj má nečitateľnú platnosť") from exc
    if valid_from > valid_to:
        raise ValueError("začiatok platnosti je po konci platnosti")
    return valid_from.isoformat(), valid_to.isoformat()


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
    if not om:
        return None
    valid_from, valid_to = parse_finite_validity(" ".join((slug, idx, pg)))
    return {
        "flyer_id": om.group(1),
        "image_name": om.group(2),
        "source_url": f"{base}{slug}",
        "valid_from": valid_from,
        "valid_to": valid_to,
    }


def mletaky_base(store):
    html_ = requests.get(f"https://mletaky.sk/obchody/{store}", headers=H, timeout=20).text
    cands = set(re.findall(r'https?://app\.mletaky\.sk/(\d{6})_(\d{6})_'
                           + store + r'_([a-z0-9]+)', html_))
    today, best = datetime.date.today(), None
    for vto, vfrom, h in cands:
        try:
            d_from = datetime.datetime.strptime(vfrom, "%y%m%d").date()
            d_to = datetime.datetime.strptime(vto, "%y%m%d").date()
        except ValueError:
            continue
        if d_from > d_to:
            continue
        if d_from <= today and (best is None or d_from > best["sort_date"]):
            best = {
                "sort_date": d_from,
                "source_url": f"https://app.mletaky.sk/{vto}_{vfrom}_{store}_{h}",
                "valid_from": d_from.isoformat(),
                "valid_to": d_to.isoformat(),
            }
    if best:
        best.pop("sort_date")
    return best


def page_exists(url):
    try:
        r = requests.get(url, headers=H, timeout=25, stream=True, allow_redirects=False)
        ok = r.status_code == 200 and r.headers.get("content-type", "").startswith("image")
        marker = hashlib.sha256(r.content).hexdigest() if ok else None
        r.close()
        return marker
    except Exception:
        return None


def _page_marker(marker, url):
    return url if marker is True else marker


def _manifest(source, page_rows):
    return {
        "source_url": source["source_url"],
        "valid_from": source["valid_from"],
        "valid_to": source["valid_to"],
        "pages": page_rows,
    }


def store_pages(store):
    """Return all sequential pages plus their exact finite-validity manifest."""
    try:
        meta = kupino_meta(store)
    except Exception as e:
        log(f"[WARN] {store}: kupino zlyhalo ({e})")
        meta = None
    if meta:
        if isinstance(meta, tuple):
            lid, name, slug = meta
            valid_from, valid_to = parse_finite_validity(slug)
            meta = {
                "flyer_id": lid,
                "image_name": name,
                "source_url": f"https://www.kupino.sk{slug}",
                "valid_from": valid_from,
                "valid_to": valid_to,
            }
        lid, name = meta["flyer_id"], meta["image_name"]
        pages, page_rows, seen, n = [], [], set(), 1
        while True:
            t = f"https://img.kupino.sk/letaky/{lid}/thumbs/{name}-{n}_320.jpg"
            marker = page_exists(t)
            if not marker:
                break
            marker = _page_marker(marker, t)
            if marker in seen:
                break
            seen.add(marker)
            full = f"https://img.kupino.sk/letaky/{lid}/{name}-{n}.jpg"
            pages.append((t, full))
            page_rows.append({"source_page": n, "thumbnail_url": t, "image_url": full})
            n += 1
        if pages:
            return pages, _manifest(meta, page_rows)
    try:
        base = mletaky_base(store)
    except Exception as e:
        log(f"[WARN] {store}: mletaky zlyhalo ({e})")
        base = None
    if base:
        pages, page_rows, seen, misses, n = [], [], set(), 0, 0
        while True:
            u = f"{base['source_url']}/image{n:02d}.webp"
            marker = page_exists(u)
            if marker:
                marker = _page_marker(marker, u)
                if marker in seen:
                    break
                seen.add(marker)
                pages.append((None, u))
                page_rows.append({"source_page": n + 1, "thumbnail_url": None, "image_url": u})
                misses = 0
            else:
                misses += 1
                if misses >= 2:
                    break
            n += 1
        if pages:
            return pages, _manifest(base, page_rows)
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


def validate_flyer_manifest(pages, manifest):
    if not pages or not isinstance(manifest, dict):
        raise ValueError("leták nemá úplný manifest")
    source_url = manifest.get("source_url")
    if not isinstance(source_url, str) or not source_url or source_url != source_url.strip():
        raise ValueError("manifest nemá presnú URL zdroja")
    parsed_url = urlparse(source_url)
    if parsed_url.scheme not in ("http", "https") or not parsed_url.netloc:
        raise ValueError("manifest nemá presnú URL zdroja")

    valid_from = manifest.get("valid_from")
    valid_to = manifest.get("valid_to")
    try:
        from_date = datetime.date.fromisoformat(valid_from)
        to_date = datetime.date.fromisoformat(valid_to)
    except (TypeError, ValueError) as exc:
        raise ValueError("manifest nemá čitateľnú konečnú platnosť") from exc
    if valid_from != from_date.isoformat() or valid_to != to_date.isoformat() or from_date > to_date:
        raise ValueError("manifest nemá čitateľnú konečnú platnosť")

    page_rows = manifest.get("pages")
    if not isinstance(page_rows, list) or len(page_rows) != len(pages):
        raise ValueError("manifest nepokrýva všetky strany")
    seen = set()
    for page, urls in zip(page_rows, pages):
        source_page = page.get("source_page") if isinstance(page, dict) else None
        if isinstance(source_page, bool) or not isinstance(source_page, int) or source_page <= 0:
            raise ValueError("manifest má neplatné číslo strany")
        if source_page in seen:
            raise ValueError("manifest opakuje číslo strany")
        if page.get("thumbnail_url") != urls[0] or page.get("image_url") != urls[1]:
            raise ValueError("manifest nesedí so zdrojovými obrázkami")
        seen.add(source_page)
    return {page["source_page"]: page for page in page_rows}


def batches(items, size):
    for start in range(0, len(items), size):
        yield items[start:start + size]


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
[{{"source_page":12,"nazov":"Bravčové plecko","kategoria":"maso","cena":2.15,"povodna":4.49,"zlava":"−52 %","jednotka":"kg"}}]
Pravidlá:
- source_page = presné číslo označené pri obrázku; každá položka ho MUSÍ zopakovať
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
    pages, manifest = store_pages(store)
    if not pages:
        raise ValueError(f"{store}: leták s konečnou platnosťou nebol nájdený")
    page_manifest = validate_flyer_manifest(pages, manifest)

    # 1) lacný sken náhľadov → ktoré strany sú potravinové
    thumbs = []
    for source_page, page in page_manifest.items():
        try:
            encoded = get_b64(page["thumbnail_url"] or page["image_url"], SCAN_PX)
        except Exception as exc:
            raise ValueError(f"{store}: náhľad strany {source_page} sa nepodarilo načítať") from exc
        if not encoded:
            raise ValueError(f"{store}: náhľad strany {source_page} sa nepodarilo načítať")
        thumbs.append((source_page, encoded))
    log(f"[INFO] {store}: {len(thumbs)} strán ({manifest['source_url']}), skenujem…")

    food = set()
    for batch in batches(thumbs, SCAN_BATCH_SIZE):
        content = []
        batch_pages = {source_page for source_page, _ in batch}
        for source_page, encoded in batch:
            content.append({"type": "text", "text": f"Strana {source_page}:"})
            content.append(img_block(encoded))
        content.append({"type": "text", "text": SCAN_PROMPT})
        try:
            selected = claude_json(client, MODEL_SCAN, content, 500)
        except Exception as exc:
            raise ValueError(f"{store}: sken strán zlyhal") from exc
        if not isinstance(selected, list):
            raise ValueError(f"{store}: sken nevrátil zoznam strán")
        for source_page in selected:
            if isinstance(source_page, bool) or not isinstance(source_page, int) or source_page not in batch_pages:
                raise ValueError(f"{store}: sken vrátil neznámu stranu")
            food.add(source_page)
    food = sorted(food)
    if not food:
        raise ValueError(f"{store}: v letáku neboli potvrdené potravinové strany")
    log(f"[INFO] {store}: potravinové strany {food} — čítam…")

    # 2) presné čítanie cien (Opus 5 vision)
    out = []
    for batch_pages in batches(food, READ_BATCH_SIZE):
        content = []
        for source_page in batch_pages:
            try:
                encoded = get_b64(page_manifest[source_page]["image_url"], READ_PX)
            except Exception as exc:
                raise ValueError(f"{store}: strana {source_page} sa nepodarilo načítať") from exc
            if not encoded:
                raise ValueError(f"{store}: strana {source_page} sa nepodarilo načítať")
            content.append({"type": "text", "text": f"Zdrojová strana {source_page}:"})
            content.append(img_block(encoded))
        content.append({"type": "text", "text": EXTRACT_PROMPT.format(store=store.upper())})
        try:
            items = claude_json(client, MODEL_READ, content, READ_TOKENS, effort=READ_EFFORT)
        except Exception as exc:
            raise ValueError(f"{store}: extrakcia strán zlyhala") from exc
        if not isinstance(items, list):
            raise ValueError(f"{store}: extrakcia nevrátila zoznam akcií")
        for item in items:
            source_page = item.get("source_page") if isinstance(item, dict) else None
            if source_page not in batch_pages:
                raise ValueError(f"{store}: akcia odkazuje na nevybranú zdrojovú stranu")
            try:
                offer = {
                    "obchod": store.capitalize(),
                    "nazov": str(item["nazov"])[:40],
                    "kategoria": (item.get("kategoria") or "ine")[:20],
                    "cena": float(item["cena"]),
                    "povodna": float(item["povodna"]) if item.get("povodna") is not None else None,
                    "zlava": item.get("zlava"),
                    "jednotka": (item.get("jednotka") or "")[:12],
                    "source_url": manifest["source_url"],
                    "source_page": source_page,
                    "valid_from": manifest["valid_from"],
                    "valid_to": manifest["valid_to"],
                }
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError(f"{store}: extrakcia obsahuje neplatnú akciu") from exc
            validate_offer(offer)
            out.append(offer)
    if not out:
        raise ValueError(f"{store}: extrakcia nevrátila žiadne overené akcie")
    log(f"[INFO] {store}: {len(out)} akcií")
    return out


def main():
    import anthropic
    client = anthropic.Anthropic(api_key=load_key(), timeout=180.0, max_retries=1)
    tyz = monday()
    con = db()
    total, failures = 0, []
    try:
        for store in STORES:
            try:
                akcie = zbieraj(client, store)
                replace_store_week(con, tyz, store.capitalize(), akcie)
            except Exception as exc:
                failures.append(store)
                log(f"[ERROR] {store}: zber zlyhal ({exc})")
                continue
            total += len(akcie)
        n = con.execute("SELECT COUNT(*) c FROM akcie WHERE tyzden=?", (tyz,)).fetchone()["c"]
    finally:
        con.close()
    if failures:
        raise SystemExit(f"Zber zlyhal pre obchody: {', '.join(failures)}")
    log(f"[OK] Týždeň {tyz}: uložených {total} akcií (v DB spolu {n}).")
    if n < 20:
        raise SystemExit("Málo akcií — niečo je zle.")


if __name__ == "__main__":
    main()
