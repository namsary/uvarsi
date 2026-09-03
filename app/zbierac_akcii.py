#!/usr/bin/env python3
"""
Uvar.si — ZBIERAČ AKCIÍ (beží raz týždenne pre VŠETKÝCH používateľov).

Prečíta letáky (Kaufland, Tesco, Lidl) cez vision a uloží VŠETKY nájdené
potravinové akcie do SQLite. Osobné plány sa potom skladajú z tejto databázy
lacnými textovými volaniami — takže jeden drahý beh týždenne obslúži
neobmedzený počet používateľov.

Zdroje strán letákov: oficiálny Lidl endpoint; kupino.sk a mletaky.sk ako zálohy.
Beh:  /opt/uvarsi/venv/bin/python -u zbierac_akcii.py
Opravný beh jedného zdroja:  ... zbierac_akcii.py --store lidl
"""
import os, re, json, base64, datetime, hashlib, sqlite3, requests
from io import BytesIO
from urllib.parse import quote, urlparse

try:
    from offer_data import migrate_akcie_schema, replace_store_week, validate_offer
except ImportError:
    from app.offer_data import migrate_akcie_schema, replace_store_week, validate_offer

try:
    import db_rezim
except ImportError:
    from app import db_rezim

try:
    import naklady
except ImportError:
    from app import naklady

try:
    import plan_jobs
except ImportError:
    from app import plan_jobs

DB = os.environ.get("UVARSI_DB", "/opt/uvarsi/uvarsi.db")
ENV_FILE = "/opt/uvarsi/uvarsi.env"

MODEL_READ = "claude-opus-5"                 # najsilnejšia vision → presné ceny
READ_EFFORT = "high"
READ_TOKENS = 16000
MODEL_SCAN = "claude-haiku-4-5-20251001"     # lacné triedenie strán

STORES = ["kaufland", "tesco", "lidl"]
MIN_VERIFIED_OFFERS_PER_STORE = 10
SCAN_BATCH_SIZE = 12
READ_BATCH_SIZE = 4
READ_PX = 1500
SCAN_PX = 320
SKIP_SLUG = ("nova-predajna", "brozura", "back-to-school", "special",
             "shop", "nabytok", "zahrada")
PAGE_GAP_TOLERANCE = 3      # koľko po sebe chýbajúcich strán ešte preklenieme
MIN_PLAUSIBLE_PAGES = 8     # menej strán je podozrivé — zdroj je asi neúplný
MAX_PAGES = 200             # poistka proti nekonečnému prechádzaniu
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
H = {"User-Agent": UA}
LIDL_OVERVIEW_URL = "https://www.lidl.sk/c/online-letak/"
LIDL_API_URL = "https://endpoints.leaflets.schwarz/v4/flyer"


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

-- Výsledok zberu PRE KAŽDÝ OBCHOD ZVLÁŠŤ. Bez toho sa čiastočný beh
-- (2 z 3 obchodov) nedá odlíšiť od úspešného: riadkov je dosť, dozorca
-- nič nespustí a appka celý týždeň ticho plánuje bez chýbajúceho obchodu.
CREATE TABLE IF NOT EXISTS zber_stav (
  tyzden  TEXT NOT NULL,
  obchod  TEXT NOT NULL,
  stav    TEXT NOT NULL,           -- 'ok' | 'fail'
  pocet   INTEGER NOT NULL DEFAULT 0,
  detail  TEXT,
  updated TEXT DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (tyzden, obchod)
);
"""


def db():
    # Zberač píše dlhé dávky, kým appka číta. Bez WAL by si navzájom blokovali
    # databázu a bez timeoutu (default 5 s) by sa zberač vzdal skôr, než appka
    # stihne dokončiť transakciu — týždenný beh za 0,37 € by padol nadarmo.
    con = db_rezim.otvor(DB)
    con.executescript(SCHEMA)
    migrate_akcie_schema(con)
    naklady.migrate_naklady_schema(con)
    plan_jobs.migrate_plan_jobs_schema(con)
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


_LABELLED_FROM = re.compile(
    r'(?:validFrom|valid_from|valid-from|platnost_od|platnost-od|dateFrom)"?\s*[:=]\s*"?'
    r'(\d{4}-\d{2}-\d{2})',
    re.I,
)
_LABELLED_TO = re.compile(
    r'(?:validThrough|validTo|valid_to|valid-to|validUntil|platnost_do|platnost-do|dateTo)"?\s*[:=]\s*"?'
    r'(\d{4}-\d{2}-\d{2})',
    re.I,
)


def kupino_flyer_validity(slug, page_html):
    """Platnosť VYBRANÉHO letáku — nikdy nie z indexu obchodu.

    kupino uvádza rozsah platnosti v slugu samotného letáku. To je jediné
    miesto, ktoré preukázateľne patrí TOMUTO letáku; index obchodu aj samotná
    stránka obsahujú aj konkurenčné letáky, takže dátum nájdený kdekoľvek v
    HTML môže patriť inému letáku. Keď slug rozsah nenesie, prijmeme iba
    explicitne označenú (strojovo čitateľnú) platnosť na stránke letáku, a to
    len vtedy, keď je na stránke JEDINÁ — inak leták odmietneme.
    Radšej žiadny leták než leták s cudzími dátumami.
    """
    try:
        return parse_finite_validity(slug)
    except ValueError:
        pass

    starts = set(_LABELLED_FROM.findall(page_html or ""))
    ends = set(_LABELLED_TO.findall(page_html or ""))
    if len(starts) != 1 or len(ends) != 1:
        raise ValueError("leták nemá jednoznačnú vlastnú platnosť")
    return parse_finite_validity(f"{starts.pop()} {ends.pop()}")


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
    valid_from, valid_to = kupino_flyer_validity(slug, pg)
    return {
        "flyer_id": om.group(1),
        "image_name": om.group(2),
        "source_url": f"{base}{slug}",
        "valid_from": valid_from,
        "valid_to": valid_to,
    }


def flyer_is_current(valid_from, valid_to, today):
    """Leták je použiteľný, len ak DNES spadá do jeho platnosti."""
    return valid_from <= today.isoformat() <= valid_to


def _safe_lidl_image_url(value):
    if not isinstance(value, str):
        return None
    parsed = urlparse(value)
    if parsed.scheme != "https" or parsed.hostname not in {
        "imgproxy.leaflets.schwarz",
        "assets.leaflets.schwarz",
    }:
        return None
    return value


def official_lidl_pages(today=None):
    """Read the national weekly flyer from Lidl's own public viewer API.

    The overview supplies the current weekly slug.  The viewer endpoint then
    supplies a finite offer window and a complete, explicitly numbered page
    manifest.  We never guess hashes or stop after the first missing image.
    """
    today = today or datetime.date.today()
    overview = requests.get(LIDL_OVERVIEW_URL, headers=H, timeout=30).text
    slugs = re.findall(
        r'href=["\'](?:https://www\.lidl\.sk)?/l/sk/letak/'
        r'(online-letak-platny-od-[^/"\'?]+)/(?:ar/1|view/flyer/page/1)',
        overview,
        flags=re.I,
    )
    slug = next(iter(dict.fromkeys(slugs)), None)
    if not slug:
        raise ValueError("oficiálna stránka neuvádza aktuálny týždenný leták")

    endpoint = f"{LIDL_API_URL}?flyer_identifier={quote(slug, safe='')}"
    payload = requests.get(endpoint, headers=H, timeout=30).json()
    flyer = payload.get("flyer") if isinstance(payload, dict) and payload.get("success") is True else None
    if not isinstance(flyer, dict):
        raise ValueError("oficiálny endpoint nevrátil leták")
    if flyer.get("apiCountryCode") != "SK":
        raise ValueError("oficiálny endpoint vrátil leták pre inú krajinu")
    if flyer.get("isActive") is not True or flyer.get("status") != "current":
        raise ValueError("oficiálny endpoint neoznačil leták ako aktuálny")

    raw_from = flyer.get("offerStartDate")
    raw_to = flyer.get("offerEndDate")
    if not isinstance(raw_from, str) or not isinstance(raw_to, str):
        raise ValueError("oficiálny leták nemá konečnú platnosť ponuky")
    valid_from, valid_to = parse_finite_validity(f"{raw_from[:10]} {raw_to[:10]}")
    if not flyer_is_current(valid_from, valid_to, today):
        raise ValueError(f"oficiálny leták dnes neplatí ({valid_from} – {valid_to})")

    raw_pages = flyer.get("pages")
    if not isinstance(raw_pages, list):
        raise ValueError("oficiálny leták nemá zoznam strán")
    normalized = []
    seen = set()
    for item in raw_pages:
        if not isinstance(item, dict) or isinstance(item.get("number"), bool):
            raise ValueError("oficiálny leták má neplatné číslo strany")
        try:
            number = int(item["number"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("oficiálny leták má neplatné číslo strany") from exc
        thumbnail = _safe_lidl_image_url(item.get("thumbnail") or item.get("image"))
        image = _safe_lidl_image_url(item.get("zoom") or item.get("image"))
        if number < 1 or number in seen or not thumbnail or not image:
            raise ValueError("oficiálny leták má neúplný manifest strán")
        seen.add(number)
        normalized.append((number, thumbnail, image))
    normalized.sort(key=lambda row: row[0])
    if len(normalized) < MIN_PLAUSIBLE_PAGES:
        raise ValueError("oficiálny týždenný leták má podozrivo málo strán")
    if [row[0] for row in normalized] != list(range(1, len(normalized) + 1)):
        raise ValueError("oficiálnemu letáku chýbajú strany")

    source_url = flyer.get("flyerUrlAbsolute")
    parsed_source = urlparse(source_url) if isinstance(source_url, str) else None
    if not parsed_source or parsed_source.scheme != "https" or parsed_source.hostname != "www.lidl.sk":
        source_url = f"https://www.lidl.sk/l/sk/letak/{slug}/view/flyer/page/1"
    pages = [(thumbnail, image) for _, thumbnail, image in normalized]
    manifest = {
        "source_url": source_url,
        "valid_from": valid_from,
        "valid_to": valid_to,
        "declared_pages": len(normalized),
        "pages": [
            {
                "source_page": number,
                "thumbnail_url": thumbnail,
                "image_url": image,
            }
            for number, thumbnail, image in normalized
        ],
    }
    return pages, manifest


def _mletaky_declared_page_counts(page_html, store):
    """Map CDN flyer base URLs to page counts declared by mLetaky cards.

    The listing is streamed as escaped Next.js data.  Each card contains the
    first CDN image and, later in the same card, its declared page count.  We
    deliberately bind the count to that card instead of treating every valid
    URL as an equivalent flyer: Lidl publishes the national weekly flyer next
    to tiny city/selected-store inserts with identical dates.
    """
    normalized = (page_html or "").replace('\\"', '"')
    image_pattern = re.compile(
        r'(https?://app\.mletaky\.sk/\d{6}_\d{6}_'
        + re.escape(store) + r'_[a-z0-9]+)/image00\.webp'
    )
    matches = list(image_pattern.finditer(normalized))
    counts = {}
    for index, match in enumerate(matches):
        source_url = match.group(1)
        if source_url in counts:
            continue
        end = len(normalized)
        for following in matches[index + 1:]:
            if following.group(1) != source_url:
                end = following.start()
                break
        card = normalized[match.end():end]
        page_count = re.search(
            r'card-description[^"\r\n]*"\s*,\s*"children"\s*:\s*(\d+)',
            card,
        )
        if page_count:
            counts[source_url] = int(page_count.group(1))
    return counts


def mletaky_candidates(store, today=None):
    today = today or datetime.date.today()
    html_ = requests.get(f"https://mletaky.sk/obchody/{store}", headers=H, timeout=20).text
    cands = set(re.findall(r'https?://app\.mletaky\.sk/(\d{6})_(\d{6})_'
                           + store + r'_([a-z0-9]+)', html_))
    page_counts = _mletaky_declared_page_counts(html_, store)
    current = []
    for vto, vfrom, h in cands:
        try:
            d_from = datetime.datetime.strptime(vfrom, "%y%m%d").date()
            d_to = datetime.datetime.strptime(vto, "%y%m%d").date()
        except ValueError:
            continue
        if d_from > d_to:
            continue
        # Nestačí, že leták začal — musí aj STÁLE platiť. Najneskôr začatý
        # leták môže byť už skončený a jeho ceny by sa ticho zahodili.
        if not flyer_is_current(d_from.isoformat(), d_to.isoformat(), today):
            continue
        source_url = f"https://app.mletaky.sk/{vto}_{vfrom}_{store}_{h}"
        candidate = {
            "source_url": source_url,
            "valid_from": d_from.isoformat(),
            "valid_to": d_to.isoformat(),
            "_duration": (d_to - d_from).days + 1,
            "_start": d_from.toordinal(),
        }
        if source_url in page_counts:
            candidate["declared_pages"] = page_counts[source_url]
        current.append(candidate)

    # Hlavný potravinový leták má bežne 5–14 dní (cez sviatky aj dlhšie než
    # presný týždeň). Krátke 4-dňové lokálne/víkendové vložky preto nemajú
    # prednosť iba preto, že začali neskôr. Medzi hlavnými kandidátmi rozhoduje
    # blízkosť siedmim dňom, deklarovaný počet strán a potom novší začiatok.
    current.sort(key=lambda item: (
        0 if 5 <= item["_duration"] <= 14 else 1,
        abs(item["_duration"] - 7),
        -item.get("declared_pages", 0),
        -item["_start"],
        item["source_url"],
    ))
    for candidate in current:
        candidate.pop("_duration")
        candidate.pop("_start")
    return current


def mletaky_base(store, today=None):
    candidates = mletaky_candidates(store, today)
    return candidates[0] if candidates else None


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
    manifest = {
        "source_url": source["source_url"],
        "valid_from": source["valid_from"],
        "valid_to": source["valid_to"],
        "pages": page_rows,
    }
    if source.get("declared_pages"):
        manifest["declared_pages"] = source["declared_pages"]
    return manifest


def _usable_flyer(store, source, meta, today):
    """Zahoď leták, ktorý dnes neplatí — jeho ceny by sa aj tak ticho zahodili."""
    if not meta:
        return None
    if not flyer_is_current(meta["valid_from"], meta["valid_to"], today):
        log(f"[WARN] {store}: {source} leták dnes neplatí "
            f"({meta['valid_from']} – {meta['valid_to']}) — preskakujem")
        return None
    return meta


def discover_pages(store, page_urls, start):
    """Prejdi strany letáka po sebe a preklen malé diery na CDN.

    Jedna chýbajúca strana na CDN nesmie ukončiť celý leták — 48-stranový
    leták sa inak prečíta ako 8-stranový a nikto sa to nedozvie.
    """
    pages, page_rows, seen, misses, gaps, n = [], [], set(), 0, [], start
    while n < start + MAX_PAGES:
        thumb, full = page_urls(n)
        probe = thumb or full
        marker = page_exists(probe)
        if marker:
            marker = _page_marker(marker, probe)
            if marker in seen:
                break                       # zdroj opakuje stranu → koniec letáka
            seen.add(marker)
            pages.append((thumb, full))
            page_rows.append({
                "source_page": n - start + 1,
                "thumbnail_url": thumb,
                "image_url": full,
            })
            if misses:
                gaps.append(n - start + 1)
            misses = 0
        else:
            # Kým sme nenašli ani jednu stranu, dve chyby znamenajú, že leták
            # tam nie je. Potom už preklenujeme diery.
            misses += 1
            if misses >= (PAGE_GAP_TOLERANCE if pages else 2):
                break
        n += 1
    if gaps:
        log(f"[WARN] {store}: preklenuté chýbajúce strany pred {gaps} — CDN má diery")
    if pages and len(pages) < MIN_PLAUSIBLE_PAGES:
        log(f"[WARN] {store}: leták má len {len(pages)} strán — "
            f"to je nepravdepodobne málo, zdroj môže byť neúplný")
    return pages, page_rows


def store_pages(store, today=None):
    """Return all sequential pages plus their exact finite-validity manifest."""
    today = today or datetime.date.today()
    if store == "lidl":
        try:
            pages, manifest = official_lidl_pages(today=today)
            log(f"[INFO] {store}: oficiálny leták má {len(pages)} strán")
            return pages, manifest
        except Exception as e:
            log(f"[WARN] {store}: oficiálny leták odmietnutý ({e})")
    try:
        meta = kupino_meta(store)
    except Exception as e:
        log(f"[WARN] {store}: kupino leták odmietnutý ({e})")
        meta = None
    meta = _usable_flyer(store, "kupino", meta, today)
    if meta:
        lid, name = meta["flyer_id"], meta["image_name"]
        pages, page_rows = discover_pages(
            store,
            lambda n: (f"https://img.kupino.sk/letaky/{lid}/thumbs/{name}-{n}_320.jpg",
                       f"https://img.kupino.sk/letaky/{lid}/{name}-{n}.jpg"),
            start=1,
        )
        if pages:
            return pages, _manifest(meta, page_rows)
    try:
        base = mletaky_base(store, today)
    except Exception as e:
        log(f"[WARN] {store}: mletaky zlyhalo ({e})")
        base = None
    base = _usable_flyer(store, "mletaky", base, today)
    if base:
        pages, page_rows = discover_pages(
            store,
            lambda n: (None, f"{base['source_url']}/image{n:02d}.webp"),
            start=0,
        )
        declared_pages = base.get("declared_pages")
        if pages and declared_pages and len(pages) < declared_pages:
            log(f"[WARN] {store}: zdroj deklaruje {declared_pages} strán, "
                f"ale dostupných je len {len(pages)} — neúplný leták odmietam")
            pages, page_rows = [], []
        if pages:
            return pages, _manifest(base, page_rows)
    log(f"[WARN] {store}: žiadny leták s dôveryhodnou platnosťou — obchod preskakujem")
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
SCAN_OUTPUT_SCHEMA = {
    "type": "array",
    "items": {"type": "integer"},
}
EXTRACT_OUTPUT_SCHEMA = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {
            "source_page": {"type": "integer"},
            "nazov": {"type": "string"},
            "kategoria": {
                "type": "string",
                "enum": ["maso", "zelenina", "ovocie", "mliecne", "trvanlive", "pecivo", "ine"],
            },
            "cena": {"type": "number"},
            "povodna": {"anyOf": [{"type": "number"}, {"type": "null"}]},
            "zlava": {"anyOf": [{"type": "string"}, {"type": "null"}]},
            "jednotka": {
                "type": "string",
                "enum": ["kg", "ks", "l", "balenie"],
            },
        },
        "required": [
            "source_page", "nazov", "kategoria", "cena",
            "povodna", "zlava", "jednotka",
        ],
        "additionalProperties": False,
    },
}


def _schema_pre_model(model):
    return SCAN_OUTPUT_SCHEMA if model == MODEL_SCAN else EXTRACT_OUTPUT_SCHEMA


def _parse_json_response(text):
    """Read strict JSON and tolerate wrappers from an older SDK fallback.

    Structured outputs should make the first ``json.loads`` succeed. The raw
    decoder is a backwards-compatible safety net for an SDK that rejected the
    format parameter and a model that surrounded otherwise valid JSON with a
    short sentence or a Markdown fence.
    """
    cleaned = text.strip().lstrip("\ufeff")
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError as original:
        decoder = json.JSONDecoder()
        for index, char in enumerate(cleaned):
            if char not in "[{":
                continue
            try:
                value, _end = decoder.raw_decode(cleaned[index:])
            except json.JSONDecodeError:
                continue
            return value
        raise original


def claude_json(client, model, content, max_tokens, effort=None):
    output_config = {
        "format": {
            "type": "json_schema",
            "schema": _schema_pre_model(model),
        }
    }
    if effort:
        output_config["effort"] = effort
    try:
        msg = client.messages.create(model=model, max_tokens=max_tokens,
                                     messages=[{"role": "user", "content": content}],
                                     output_config=output_config)
    except TypeError:
        # Starší SDK nepozná `output_config`. Nezastavíme celý týždenný zber;
        # odpoveď ešte prísne parsujeme a ďalej kontrolujeme proti letáku.
        msg = client.messages.create(model=model, max_tokens=max_tokens,
                                     messages=[{"role": "user", "content": content}])
    if getattr(msg, "stop_reason", None) == "max_tokens":
        raise ValueError("odseknuté na max_tokens")
    txt = "".join(b.text for b in msg.content
                  if getattr(b, "type", None) == "text").strip()
    txt = re.sub(r"^```(?:json)?|```$", "", txt, flags=re.M).strip()
    return _parse_json_response(txt)


def guarded_client(con, client):
    """Guard collector calls without consuming capacity reserved by queued plans."""
    return naklady.strazeny_klient(
        con,
        client,
        "zber_letakov",
        rezervovane_eur=lambda: plan_jobs.active_reservations_eur(con),
    )


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
        except naklady.KreditVycerpany:
            # Nie je to chyba OBCHODU, ale celého účtu: ďalšie obchody by len
            # zopakovali to isté odmietnutie. Preto ide von nezabalené.
            raise
        except Exception as exc:
            raise ValueError(
                f"{store}: sken strán zlyhal ({type(exc).__name__}: {exc})"
            ) from exc
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
        except naklady.KreditVycerpany:
            raise
        except Exception as exc:
            raise ValueError(
                f"{store}: extrakcia strán zlyhala ({type(exc).__name__}: {exc})"
            ) from exc
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


def record_store_outcome(con, week, store, status, count=0, detail=None):
    """Zapíš výsledok zberu jedného obchodu, aby bol čiastočný beh viditeľný."""
    con.execute(
        """INSERT INTO zber_stav (tyzden, obchod, stav, pocet, detail, updated)
           VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
           ON CONFLICT(tyzden, obchod) DO UPDATE SET
             stav=excluded.stav, pocet=excluded.pocet,
             detail=excluded.detail, updated=excluded.updated""",
        (week, store, status, count, detail),
    )
    con.commit()


def main(stores=None):
    import anthropic
    selected_stores = list(dict.fromkeys(stores or STORES))
    unknown = [store for store in selected_stores if store not in STORES]
    if unknown:
        raise ValueError(f"Neznámy obchod: {', '.join(unknown)}")
    tyz = monday()
    con = db()
    # Vision beh je najdrahšia operácia v celej appke (~0,37 € za obchod). Miesto
    # v týždennom počte behov sa berie EŠTE PRED prvým volaním — vďaka tomu je
    # rozbehnutá slučka štrukturálne nemožná, nie iba nepravdepodobná. Presne
    # toto chýbalo, keď dozorca 12× po sebe zaplatil za ten istý márny beh.
    try:
        naklady.rezervuj_beh(con, "zber_letakov")
    except naklady.RozpocetVycerpany as odmietnutie:
        con.close()
        raise SystemExit(f"Zber nespúšťam — {odmietnutie}")
    # Cez strážený klient sa nedá zavolať model bez zaúčtovania a bez stropu.
    client = guarded_client(
        con,
        anthropic.Anthropic(api_key=load_key(), timeout=180.0, max_retries=1),
    )
    total, failures, collected = 0, [], []
    try:
        for store in selected_stores:
            try:
                akcie = zbieraj(client, store)
                if len(akcie) < MIN_VERIFIED_OFFERS_PER_STORE:
                    raise ValueError(
                        f"{store}: iba {len(akcie)} overených akcií; "
                        f"minimum je {MIN_VERIFIED_OFFERS_PER_STORE}"
                    )
                replace_store_week(con, tyz, store.capitalize(), akcie)
            except naklady.KreditVycerpany as odmietnutie:
                # API odmietlo request EŠTE PRED prácou — nespotreboval sa ani
                # token, takže zabraté miesto v týždennom počte behov patrí
                # späť. Inak by zbierač po dobití kreditu ostal zablokovaný do
                # konca týždňa za behy, ktoré nikdy nebežali (incident 24. 8.).
                naklady.uvolni_beh(con, "zber_letakov")
                log(f"[ERROR] {store}: {odmietnutie}")
                raise SystemExit(f"Zber zastavený — {odmietnutie}") from None
            except Exception as exc:
                failures.append(store)
                record_store_outcome(con, tyz, store.capitalize(), "fail", 0, str(exc)[:300])
                log(f"[ERROR] {store}: zber zlyhal ({exc})")
                continue
            total += len(akcie)
            collected.append(store)
            record_store_outcome(con, tyz, store.capitalize(), "ok", len(akcie))
        n = con.execute("SELECT COUNT(*) c FROM akcie WHERE tyzden=?", (tyz,)).fetchone()["c"]
    finally:
        con.close()
    # Strojovo čitateľný súhrn: dozorca sa nesmie spoliehať na počet riadkov,
    # dva zdravé obchody ho vždy prevýšia a tretí sa už nikdy nedozberá.
    log("[SUMMARY] " + json.dumps(
        {"tyzden": tyz, "ok": collected, "fail": failures, "akcie": total},
        ensure_ascii=False, sort_keys=True))
    if failures:
        raise SystemExit(f"Zber zlyhal pre obchody: {', '.join(failures)}")
    log(f"[OK] Týždeň {tyz}: uložených {total} akcií (v DB spolu {n}).")
    if n < 20:
        raise SystemExit("Málo akcií — niečo je zle.")


def cli(argv=None):
    import argparse

    parser = argparse.ArgumentParser(
        description="Zozbieraj akcie zo všetkých letákov alebo iba zvolený obchod."
    )
    parser.add_argument(
        "--store", action="append", choices=STORES, dest="stores",
        help="opravný zber iba jedného obchodu; možno uviesť opakovane",
    )
    args = parser.parse_args(argv)
    main(args.stores)
    return 0


if __name__ == "__main__":
    raise SystemExit(cli())
