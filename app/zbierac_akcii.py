#!/usr/bin/env python3
"""
Uvar.si — ZBIERAČ AKCIÍ (beží raz týždenne pre VŠETKÝCH používateľov).

Prečíta letáky (Kaufland, Tesco, Lidl) cez vision a uloží VŠETKY nájdené
potravinové akcie do SQLite. Osobné plány sa potom skladajú z tejto databázy
lacnými textovými volaniami — takže jeden drahý beh týždenne obslúži
neobmedzený počet používateľov.

Zdroje strán letákov: oficiálne zdroje obchodov; agregátory iba ako núdzová záloha.
Beh:  /opt/uvarsi/venv/bin/python -u zbierac_akcii.py
Opravný beh jedného zdroja:  ... zbierac_akcii.py --store lidl
"""
import os, re, json, base64, datetime, hashlib, sqlite3, tempfile, requests
from io import BytesIO
from pathlib import Path
from urllib.parse import quote, urlparse

try:
    from offer_data import (
        CURRENT_COLLECTION_DATA_VERSION,
        LOYALTY_PROGRAM_BY_STORE,
        migrate_akcie_schema,
        replace_store_week,
        validate_offer,
    )
except ImportError:
    from app.offer_data import (
        CURRENT_COLLECTION_DATA_VERSION,
        LOYALTY_PROGRAM_BY_STORE,
        migrate_akcie_schema,
        replace_store_week,
        validate_offer,
    )

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

try:
    import source_policy
except ImportError:
    from app import source_policy

try:
    from plan_calendar import bratislava_day, bratislava_monday
except ImportError:
    from app.plan_calendar import bratislava_day, bratislava_monday

DB = os.environ.get("UVARSI_DB", "/opt/uvarsi/uvarsi.db")
ENV_FILE = "/opt/uvarsi/uvarsi.env"

MODEL_READ = "claude-sonnet-5"               # bežné presné čítanie potravinových strán
READ_EFFORT = "low"
MODEL_READ_FALLBACK = "claude-opus-5"        # iba neistá alebo chybná dávka
READ_FALLBACK_EFFORT = "high"
READ_TOKENS = 16000
MODEL_SCAN = "claude-haiku-4-5-20251001"     # lacné triedenie strán

STORES = ["kaufland", "tesco", "lidl"]
MIN_VERIFIED_OFFERS_PER_STORE = 20
# Opravný zber nesmie minúť posledný povolený beh v deň, keď už zostávajúci
# denný rozpočet zjavne nestačí ani na jeden celý obchod. Je to spodná, nie
# cenová, rezervácia: presnú cenu určí až počet potravinových strán. Každé
# jednotlivé API volanie naďalej kontroluje tvrdý denný, týždenný aj mesačný
# strop v naklady.py.
MIN_START_BUDGET_PER_STORE_EUR = 1.00
SCAN_BATCH_SIZE = 12
READ_BATCH_SIZE = 4
READ_PX = 1500
SCAN_PX = 320
SKIP_SLUG = ("nova-predajna", "brozura", "back-to-school", "special",
             "shop", "nabytok", "zahrada")
PAGE_GAP_TOLERANCE = 3      # koľko po sebe chýbajúcich strán ešte preklenieme
MIN_PLAUSIBLE_PAGES = 8     # menej strán je podozrivé — zdroj je asi neúplný
MAX_PAGES = 200             # poistka proti nekonečnému prechádzaniu
MAX_MANIFEST_PAGES = 120    # žiadny deklarovaný leták nejde nad tento strop do AI
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
H = {"User-Agent": UA}
LIDL_OVERVIEW_URL = "https://www.lidl.sk/c/online-letak/"
LIDL_API_URL = "https://endpoints.leaflets.schwarz/v4/flyer"
TESCO_API_URL = "https://api.prod.retail.tesco.com/marketing/leaflets-be/graphql"
KAUFLAND_OFFERS_URL = (
    "https://predajne.kaufland.sk/aktualna-ponuka/prehlad.html"
    "?kloffer-week=current"
)
COLLECTION_DATA_VERSION = CURRENT_COLLECTION_DATA_VERSION


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
  cena_s_kartou REAL,              -- nižšia cena iba s kartou/aplikáciou
  zlava_s_kartou TEXT,
  vernostny_program TEXT,
  minimalny_nakup REAL,
  podmienka_s_kartou TEXT,
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
  data_version INTEGER NOT NULL DEFAULT 1,
  collector_kind TEXT,
  source_fingerprint TEXT,
  valid_from TEXT,
  valid_to TEXT,
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
    columns = {row[1] for row in con.execute("PRAGMA table_info(zber_stav)")}
    if "data_version" not in columns:
        con.execute(
            "ALTER TABLE zber_stav ADD COLUMN data_version INTEGER NOT NULL DEFAULT 1"
        )
    for name in ("collector_kind", "source_fingerprint", "valid_from", "valid_to"):
        if name not in columns:
            con.execute(f"ALTER TABLE zber_stav ADD COLUMN {name} TEXT")
    naklady.migrate_naklady_schema(con)
    plan_jobs.migrate_plan_jobs_schema(con)
    return con


def business_day(now=None):
    """Dátum zberu je vždy slovenský, nezávisle od časovej zóny servera."""
    return bratislava_day(now)


def monday(now=None):
    return bratislava_monday(now)


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
        "collector_kind": "kupino-aggregator",
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
    today = today or business_day()
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
        "collector_kind": "official-lidl-viewer",
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


def _safe_tesco_media_url(value, suffix):
    if not isinstance(value, str) or value != value.strip():
        return None
    try:
        parsed = urlparse(value)
        port = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme != "https"
        or parsed.hostname != "digitalcontent.api.tesco.com"
        or parsed.username
        or parsed.password
        or port
        or parsed.query
        or parsed.fragment
        or not parsed.path.startswith("/v2/media/dotcom-hu/")
        or not parsed.path.lower().endswith(suffix)
    ):
        return None
    return value


def _tesco_bridge_config():
    raw_url = os.environ.get("UVARSI_TESCO_BRIDGE_URL", "").strip()
    secret = os.environ.get("UVARSI_TESCO_BRIDGE_SECRET", "")
    if bool(raw_url) != bool(secret):
        raise ValueError("Tesco bridge nie je úplne nakonfigurovaný")
    if not raw_url:
        return None
    try:
        parsed = urlparse(raw_url)
        port = parsed.port
    except ValueError:
        raise ValueError("Tesco bridge má neplatnú adresu") from None
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or port
        or parsed.query
        or parsed.fragment
        or parsed.path not in ("", "/")
    ):
        raise ValueError("Tesco bridge má neplatnú adresu")
    return raw_url.rstrip("/"), secret


def _production_environment():
    return os.environ.get("UVARSI_ENV", "").strip().lower() == "production"


def _safe_tesco_bridge_media_url(value, bridge_url):
    if not isinstance(value, str) or value != value.strip():
        return None
    try:
        parsed = urlparse(value)
        bridge = urlparse(bridge_url)
        port = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme != bridge.scheme
        or parsed.hostname != bridge.hostname
        or parsed.username
        or parsed.password
        or port
        or parsed.query
        or parsed.fragment
        or not re.fullmatch(r"/v1/tesco/media/[A-Za-z0-9._-]{1,4096}", parsed.path)
    ):
        return None
    return value


def _direct_tesco_candidates(today, leaflet_format):
    after = f"{today.isoformat()}T00:00:00.000Z"
    query = f'''query CurrentSlovakLeaflets {{
      leaflets(options: {{ filter: {{
        country: {{ eq: sk }}
        type: {{ eq: {leaflet_format} }}
        validTo: {{ after: "{after}" }}
      }} }}) {{
        totalItems
        items {{
          __typename id country countryId leafletUrl pages {{ pagePNG }}
          promoP1Name slug type validFrom validTo
        }}
      }}
    }}'''
    try:
        response = requests.post(
            TESCO_API_URL,
            headers={**H, "Accept": "application/json", "Content-Type": "application/json"},
            json={"query": query},
            timeout=30,
        )
        if getattr(response, "status_code", 200) != 200:
            raise ValueError("status")
        payload = response.json()
        if not isinstance(payload, dict) or payload.get("errors"):
            raise ValueError("payload")
        raw_items = payload["data"]["leaflets"]["items"]
    except Exception:
        raise ValueError("oficiálne Tesco API nevrátilo zoznam letákov") from None
    if not isinstance(raw_items, list):
        raise ValueError("oficiálne Tesco API nemá zoznam letákov")
    return raw_items, None


def _bridge_tesco_candidates(today, leaflet_format, bridge_url, secret):
    try:
        response = requests.post(
            f"{bridge_url}/v1/tesco/leaflets",
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {secret}",
                "Content-Type": "application/json",
            },
            json={"date": today.isoformat(), "format": leaflet_format},
            timeout=30,
        )
        if getattr(response, "status_code", 200) != 200:
            raise ValueError("status")
        payload = response.json()
        leaflet = payload.get("leaflet") if isinstance(payload, dict) else None
        if not isinstance(leaflet, dict):
            raise ValueError("payload")
    except Exception:
        # Nikdy neprebaľujeme text cudzej výnimky: mohol by obsahovať hlavičky.
        raise ValueError("Tesco bridge nevrátil platný manifest") from None
    return [leaflet], bridge_url


def _canonical_tesco_candidate(value, leaflet_format, bridge_url=None):
    if not isinstance(value, dict):
        raise ValueError("Tesco kandidát nie je objekt")
    if bridge_url is not None:
        country = value.get("country")
        candidate_format = value.get("format")
        slug = value.get("slug")
        raw_from, raw_to = value.get("valid_from"), value.get("valid_to")
        source_url = value.get("source_url")
        declared_pages = value.get("declared_pages")
        raw_pages = value.get("pages")
        if not isinstance(raw_pages, list):
            raise ValueError("Tesco kandidát nemá manifest strán")
        pages = []
        for page in raw_pages:
            if not isinstance(page, dict):
                raise ValueError("Tesco kandidát má neplatnú stranu")
            number = page.get("source_page")
            thumbnail = _safe_tesco_bridge_media_url(page.get("thumbnail_url"), bridge_url)
            image = _safe_tesco_bridge_media_url(page.get("image_url"), bridge_url)
            if not thumbnail or not image:
                raise ValueError("Tesco bridge vrátil neplatnú adresu strany")
            pages.append((number, thumbnail, image))
    else:
        if value.get("__typename") != "Leaflet":
            raise ValueError("Tesco kandidát nemá správny typ")
        country = value.get("country")
        candidate_format = value.get("type")
        slug = value.get("slug")
        raw_from, raw_to = value.get("validFrom"), value.get("validTo")
        if not _safe_tesco_media_url(value.get("leafletUrl"), ".pdf"):
            raise ValueError("Tesco kandidát nemá dôveryhodný PDF súbor")
        segment = "hypermarkety" if candidate_format == "HM" else "supermarkety"
        source_url = (
            "https://www.tesco.sk/akciove-ponuky/letaky-a-katalogy/"
            f"{segment}/{slug}/1"
        )
        raw_pages = value.get("pages")
        if not isinstance(raw_pages, list):
            raise ValueError("Tesco kandidát nemá manifest strán")
        pages = []
        for page in raw_pages:
            image = _safe_tesco_media_url(
                page.get("pagePNG") if isinstance(page, dict) else None,
                ".jpeg",
            )
            parsed = urlparse(image) if image else None
            match = re.search(r"\.([1-9]\d*)\.jpeg$", parsed.path, re.I) if parsed else None
            if not match:
                raise ValueError("Tesco kandidát má neplatnú stranu")
            pages.append((int(match.group(1)), image, image))
        declared_pages = len(pages)

    if country != "sk" or candidate_format != leaflet_format:
        raise ValueError("Tesco kandidát nesedí s požadovaným formátom")
    if not isinstance(slug, str) or not re.fullmatch(
        r"tesco-letak-\d{4}-\d{2}-\d{2}", slug
    ):
        raise ValueError("Tesco kandidát má neplatný slug")
    if not isinstance(raw_from, str) or not isinstance(raw_to, str):
        raise ValueError("Tesco kandidát nemá konečnú platnosť")
    valid_from, valid_to = parse_finite_validity(f"{raw_from[:10]} {raw_to[:10]}")
    expected_segment = "hypermarkety" if leaflet_format == "HM" else "supermarkety"
    expected_source = (
        "https://www.tesco.sk/akciove-ponuky/letaky-a-katalogy/"
        f"{expected_segment}/{slug}/1"
    )
    if source_url != expected_source:
        raise ValueError("Tesco kandidát nemá oficiálnu provenienciu")
    if (
        isinstance(declared_pages, bool)
        or not isinstance(declared_pages, int)
        or declared_pages != len(pages)
        or not MIN_PLAUSIBLE_PAGES <= len(pages) <= MAX_MANIFEST_PAGES
    ):
        raise ValueError("Tesco kandidát má neplatný počet strán")
    pages.sort(key=lambda row: row[0] if isinstance(row[0], int) else -1)
    if [row[0] for row in pages] != list(range(1, len(pages) + 1)):
        raise ValueError("Tesco kandidát nemá súvislý manifest strán")
    return {
        "format": leaflet_format,
        "slug": slug,
        "valid_from": valid_from,
        "valid_to": valid_to,
        "source_url": source_url,
        "pages": pages,
    }


def _normalize_official_tesco_leaflet(flyer, today, leaflet_format="HM", bridge_url=None):
    candidate = _canonical_tesco_candidate(flyer, leaflet_format, bridge_url)
    if not flyer_is_current(candidate["valid_from"], candidate["valid_to"], today):
        raise ValueError("Tesco kandidát dnes neplatí")
    return candidate


def official_tesco_pages(today=None, leaflet_format="HM"):
    """Read one exact current Tesco format through the bridge or local diagnostics."""
    today = today or business_day()
    if not isinstance(today, datetime.date) or isinstance(today, datetime.datetime):
        raise ValueError("dátum Tesco letáka nie je platný deň")
    if leaflet_format not in {"HM", "SM"}:
        raise ValueError("formát Tesco letáka musí byť HM alebo SM")

    config = _tesco_bridge_config()
    if config:
        raw_items, bridge_url = _bridge_tesco_candidates(
            today, leaflet_format, config[0], config[1]
        )
    elif _production_environment():
        raise ValueError("produkčný Tesco zber vyžaduje oficiálny bridge")
    else:
        raw_items, bridge_url = _direct_tesco_candidates(today, leaflet_format)

    candidates = []
    for value in raw_items:
        try:
            candidates.append(_normalize_official_tesco_leaflet(
                value, today, leaflet_format, bridge_url
            ))
        except (TypeError, ValueError):
            # Kandidáti sa posudzujú izolovane; chybný nesmie zahodiť zdravý.
            continue
    if not candidates:
        raise ValueError(
            "oficiálna stránka Tesca neuvádza aktuálny týždenný leták "
            "s platným manifestom"
        )
    candidates.sort(
        key=lambda item: (item["valid_from"], len(item["pages"])),
        reverse=True,
    )
    flyer = candidates[0]
    pages = [(thumbnail, image) for _, thumbnail, image in flyer["pages"]]
    manifest = {
        "source_url": flyer["source_url"],
        "collector_kind": "official-tesco-viewer",
        "valid_from": flyer["valid_from"],
        "valid_to": flyer["valid_to"],
        "declared_pages": len(pages),
        "leaflet_format": flyer["format"],
        "store_label": (
            "Tesco hypermarket" if flyer["format"] == "HM" else "Tesco supermarket"
        ),
        "pages": [
            {
                "source_page": number,
                "thumbnail_url": thumbnail,
                "image_url": image,
            }
            for number, thumbnail, image in flyer["pages"]
        ],
    }
    return pages, manifest


_KAUFLAND_FOOD_CATEGORIES = (
    "Čerstvé ovocie a zelenina",
    "Mäso, hydina, údeniny",
    "Čerstvé ryby",
    "Čerstvé výrobky",
    "Mrazené výrobky",
    "Lahôdky",
    "Trvanlivé potraviny",
    "Pečivo",
    "Káva, čaj, sladké, slané",
    "Nápoje",
    "Kaufland Card XTRA",
    "Ponuka OD DO",
    "Proteín",
    "Aktuálna ponuka",
    "Polovičné ceny",
)
_KAUFLAND_PLANT_WORDS = (
    "kvetináč", "kytica", "ruža", "ľalia", "chryzantém", "antúria",
    "orchidea", "azalka", "hortenzia", "cyklámen", "okrasná tráva",
)
_KAUFLAND_ALCOHOL_WORDS = (
    "alk.", "alkohol", "víno", "pivo", "liehovina", "destilát",
    "slivovica", "vodka", "rum", "whisky", "gin",
)
_KAUFLAND_NONFOOD_WORDS = (
    "panvica", "hrniec", "naberačka", "obracačka", "šampón", "kondicionér",
    "zubná pasta", "plienky", "granuly pre psa", "granuly pre mačku", "vysávač",
    "kanvica", "žehlička", "tričko", "mikina", "nohavice", "hračka",
)
_KAUFLAND_MIXED_CATEGORIES = ("Aktuálna ponuka", "Polovičné ceny")
_KAUFLAND_FOOD_WORDS = (
    "ryža", "cestovin", "múka", "cukor", "soľ", "olej", "ocot", "korenie",
    "mäso", "kurac", "morčac", "bravč", "hovädz", "šunka", "saláma", "klobása",
    "párky", "slanina", "ryba", "losos", "tuniak", "sardink", "vajc",
    "mlie", "syr", "jogurt", "smotan", "tvaroh", "maslo", "kefír", "puding",
    "chlieb", "pečivo", "rožok", "žemľa", "vianočka", "croissant", "tortilla",
    "paradaj", "paprik", "zemiak", "cibuľ", "cesnak", "mrkv", "cuketa",
    "brokolic", "karfiol", "uhork", "kapust", "zeler", "špenát", "šalát",
    "jabl", "hrušk", "banán", "hrozno", "citrón", "pomaranč", "mandarín",
    "nektár", "brosky", "slivk", "melón", "avokádo", "čučoried", "malin",
    "jahod", "mango", "ananás", "kiwi", "strukovin", "fazuľ", "šošovic",
    "cícer", "konzerv", "kečup", "horčic", "majonéz", "omáčk", "polievk",
    "džem", "med", "káva", "čaj", "kakao", "čokolád", "sušien", "oblátk",
    "cukrík", "dezert", "koláč", "minerálna voda", "džús", "nápoj",
)


def _kaufland_offer_template(page_html):
    decoder = json.JSONDecoder()
    assignment = re.compile(r"window\.SSR\['[^']+'\]\s*=\s*")
    for match in assignment.finditer(page_html or ""):
        try:
            payload, _ = decoder.raw_decode(page_html, match.end())
        except (TypeError, ValueError):
            continue
        if isinstance(payload, dict) and payload.get("component") == "OfferTemplate":
            return payload
    raise ValueError("oficiálna stránka Kauflandu nemá čitateľné dáta ponúk")


def _decimal_price(value):
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str):
        match = re.search(r"\d+(?:[.,]\d+)?", value.replace(" ", ""))
        if not match:
            return None
        number = float(match.group(0).replace(",", "."))
    else:
        return None
    return number if number > 0 else None


def _contains_whole_word(text, words):
    return any(
        re.search(rf"(?<!\w){re.escape(word)}(?!\w)", text, re.I)
        for word in words
    )


def _kaufland_is_alcohol(text):
    if re.search(r"(?<!\w)nealkohol", text, re.I):
        return False
    return _contains_whole_word(text, _KAUFLAND_ALCOHOL_WORDS)


def _kaufland_category(display_name, name):
    lowered = name.lower()
    if display_name.startswith(("Mäso", "Čerstvé ryby")) or any(
        word in lowered for word in (
            "mäso", "šunka", "saláma", "klobása", "párky", "držky", "ryba",
            "losos", "tuniak", "kurča", "kuracie", "morčacie", "bravčové",
            "hovädzie",
        )
    ):
        return "maso"
    if display_name.startswith("Čerstvé ovocie a zelenina"):
        fruits = (
            "jabl", "hrušk", "banán", "hrozno", "citrón", "pomaranč",
            "mandarín", "nektár", "brosky", "slivk", "melón", "avokádo",
            "čučoried", "malin", "jahod", "mango", "ananás", "kiwi",
        )
        return "ovocie" if any(word in lowered for word in fruits) else "zelenina"
    if any(word in lowered for word in (
        "jabl", "hrušk", "banán", "hrozno", "citrón", "pomaranč", "mandarín",
        "nektár", "brosky", "slivk", "melón", "avokádo", "čučoried", "malin",
        "jahod", "mango", "ananás", "kiwi",
    )):
        return "ovocie"
    if any(word in lowered for word in (
        "paradaj", "paprik", "zemiak", "cibuľ", "cesnak", "mrkv", "cuketa",
        "brokolic", "karfiol", "uhork", "kapust", "zeler", "špenát", "šalát",
    )):
        return "zelenina"
    if display_name.startswith("Pečivo") or any(
        word in lowered for word in ("chlieb", "pečivo", "rožok", "žemľa", "vianočka")
    ):
        return "pecivo"
    if display_name.startswith("Čerstvé výrobky") or any(
        word in lowered for word in (
            "mlieko", "syr", "jogurt", "smotana", "tvaroh", "maslo", "kefír",
            "nátierka", "puding",
        )
    ):
        dairy = ("mlie", "syr", "jogurt", "smotan", "tvaroh", "maslo", "kefír")
        return "mliecne" if any(word in lowered for word in dairy) else "ine"
    if display_name.startswith(("Trvanlivé", "Káva")):
        return "trvanlive"
    return "ine"


def _kaufland_offer_name(item):
    parts = [
        str(value).strip()
        for value in (item.get("title"), item.get("subtitle"))
        if str(value or "").strip()
    ]
    if not parts:
        detail_lines = [
            line.strip()
            for line in str(item.get("detailDescription") or "").splitlines()
            if line.strip() and line.strip() != "."
        ]
        useful = [
            line for line in detail_lines
            if line.lower() != "rôzne druhy"
            and not re.fullmatch(r"\d+(?:[.,]\d+)?\s*(?:g|kg|ml|l|ks)", line, re.I)
        ]
        parts = useful[:2]
    if not parts:
        parts = [
            line.strip()
            for line in str(item.get("detailTitle") or "").splitlines()
            if line.strip() and not line.lower().startswith("cena s kaufland")
        ][:2]
    return " ".join(parts)


def _kaufland_unit(raw_unit):
    value = str(raw_unit or "").lower()
    if re.search(r"(?:^|\s)1\s*kg(?:\s|$)", value):
        return "kg"
    if re.search(r"(?:^|\s)1\s*l(?:\s|$)", value):
        return "l"
    if "kus" in value and not re.search(r"\d+\s*x\s*", value):
        return "ks"
    return "balenie"


def official_kaufland_offers(today=None):
    """Čítaj cenové fakty priamo z verejného Kaufland OfferTemplate bez AI."""
    today = today or business_day()
    response = requests.get(KAUFLAND_OFFERS_URL, headers=H, timeout=45)
    content_type = str(response.headers.get("content-type", "")).lower()
    if response.status_code != 200 or "text/html" not in content_type:
        raise ValueError(
            f"oficiálny Kaufland zdroj vrátil HTTP {response.status_code}"
        )
    payload = _kaufland_offer_template(response.text)
    try:
        cycles = payload["props"]["offerData"]["cycles"]
    except (KeyError, TypeError) as exc:
        raise ValueError("oficiálny Kaufland zdroj nemá zoznam kampaní") from exc

    candidates = []
    for cycle in cycles if isinstance(cycles, list) else []:
        categories = cycle.get("categories", []) if isinstance(cycle, dict) else []
        for category in categories:
            if not isinstance(category, dict):
                continue
            display_name = str(category.get("displayName") or "")
            if not display_name.startswith(_KAUFLAND_FOOD_CATEGORIES):
                continue
            for item in category.get("offers", []):
                if not isinstance(item, dict):
                    continue
                valid_from = item.get("dateFrom")
                valid_to = item.get("dateTo")
                try:
                    start = datetime.date.fromisoformat(valid_from)
                    end = datetime.date.fromisoformat(valid_to)
                except (TypeError, ValueError):
                    continue
                if start <= today <= end:
                    candidates.append((display_name, item, valid_from, valid_to))
    if not candidates:
        raise ValueError("oficiálny Kaufland zdroj nemá dnešné potravinové ponuky")

    offers, seen = [], set()
    for display_name, item, start, end in candidates:
        raw_name = _kaufland_offer_name(item)
        raw_unit = str(item.get("unit") or "").strip()
        if raw_unit and raw_unit.lower() not in raw_name.lower():
            raw_name = f"{raw_name} {raw_unit}".strip()
        plant_text = " ".join(
            (raw_name, str(item.get("detailDescription") or ""))
        ).lower()
        if (
            not raw_name
            or (
                display_name.startswith(_KAUFLAND_MIXED_CATEGORIES)
                and not any(word in plant_text for word in _KAUFLAND_FOOD_WORDS)
            )
            or any(word in plant_text for word in _KAUFLAND_PLANT_WORDS)
            or _kaufland_is_alcohol(plant_text)
            or _contains_whole_word(plant_text, _KAUFLAND_NONFOOD_WORDS)
        ):
            continue

        price = _decimal_price(item.get("price"))
        original = _decimal_price(item.get("formattedOldPrice"))
        discount = _decimal_price(item.get("discount"))
        card_price = _decimal_price(item.get("loyaltyFormattedPrice"))
        card_discount = _decimal_price(item.get("loyaltyDiscount"))
        if price is None or original is None or original < price:
            continue
        if not discount and not card_discount:
            continue
        if card_price is None or card_price >= price:
            card_price = card_discount = None

        detail = str(item.get("detailDescription") or "")
        minimum_match = re.search(r"nad\s*(\d+(?:[.,]\d+)?)\s*€", detail, re.I)
        minimum = (
            float(minimum_match.group(1).replace(",", "."))
            if card_price is not None and minimum_match else None
        )
        identity = item.get("offerId") or (raw_name, raw_unit, price, card_price)
        if identity in seen:
            continue
        seen.add(identity)
        offer = {
            "obchod": "Kaufland",
            "nazov": raw_name[:120],
            "kategoria": _kaufland_category(display_name, raw_name),
            "cena": price,
            "povodna": original,
            "zlava": f"-{int(round(discount))} %" if discount else None,
            "jednotka": _kaufland_unit(raw_unit),
            "cena_s_kartou": card_price,
            "zlava_s_kartou": (
                f"-{int(round(card_discount))} %" if card_discount else None
            ),
            "vernostny_program": "Kaufland Card" if card_price else None,
            "minimalny_nakup": minimum,
            "podmienka_s_kartou": (
                f"Nákup aspoň za {minimum:g} €" if minimum is not None else None
            ),
            "source_url": KAUFLAND_OFFERS_URL,
            "source_page": 1,
            "valid_from": start,
            "valid_to": end,
        }
        try:
            validate_offer(offer)
            _validate_discount_arithmetic(offer)
        except ValueError as exc:
            log(
                f"[WARN] kaufland: oficiálnu položku {raw_name[:40]} "
                f"vynechávam ({exc})"
            )
            continue
        offers.append(offer)
    if not offers:
        raise ValueError("oficiálny Kaufland zdroj nevrátil overiteľné akciové ceny")
    log(f"[INFO] kaufland: oficiálny zdroj, {len(offers)} akcií bez AI")
    return offers


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
    today = today or business_day()
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
            "collector_kind": "mletaky-aggregator",
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
    collector_kind = source.get("collector_kind")
    if collector_kind is None:
        collector_kind = source_policy.collector_kind_for_url(source.get("source_url"))
    manifest = {
        "source_url": source["source_url"],
        "collector_kind": collector_kind,
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
    today = today or business_day()
    if store == "lidl":
        try:
            pages, manifest = official_lidl_pages(today=today)
            log(f"[INFO] {store}: oficiálny leták má {len(pages)} strán")
            return pages, manifest
        except Exception as e:
            log(f"[WARN] {store}: oficiálny leták odmietnutý ({e})")
    if store == "tesco":
        try:
            pages, manifest = official_tesco_pages(today=today, leaflet_format="HM")
            log(f"[INFO] {store}: oficiálny leták má {len(pages)} strán")
            return pages, manifest
        except Exception as e:
            log(f"[WARN] {store}: oficiálny leták odmietnutý ({e})")
        if _production_environment():
            log("[WARN] tesco: produkcia nepoužije agregátor namiesto oficiálneho bridge")
            return [], None
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


def get_image_bytes(url, *, headers=None, allow_redirects=True):
    response = requests.get(
        url,
        headers=headers or H,
        timeout=45,
        allow_redirects=allow_redirects,
    )
    if response.status_code != 200:
        return None
    return response.content


def image_bytes_b64(content, max_px):
    from PIL import Image
    if not content:
        return None
    im = Image.open(BytesIO(content)).convert("RGB")
    w, h = im.size
    s = min(1.0, max_px / max(w, h))
    if s < 1.0:
        im = im.resize((int(w * s), int(h * s)), Image.LANCZOS)
    buf = BytesIO()
    im.save(buf, format="JPEG", quality=82)
    return base64.standard_b64encode(buf.getvalue()).decode()


def get_b64(url, max_px):
    return image_bytes_b64(get_image_bytes(url), max_px)


def _download_official_tesco_page(url):
    config = _tesco_bridge_config()
    if config and _safe_tesco_bridge_media_url(url, config[0]):
        return get_image_bytes(
            url,
            headers={
                **H,
                "Accept": "image/jpeg",
                "Authorization": f"Bearer {config[1]}",
            },
            allow_redirects=False,
        )
    return get_image_bytes(url)


def img_block(b):
    return {"type": "image", "source": {
        "type": "base64", "media_type": "image/jpeg", "data": b}}


def validate_flyer_manifest(pages, manifest, *, store):
    if not pages or not isinstance(manifest, dict):
        raise ValueError("leták nemá úplný manifest")
    if len(pages) > MAX_MANIFEST_PAGES:
        raise ValueError(f"manifest môže mať najviac {MAX_MANIFEST_PAGES} strán")
    declared_pages = manifest.get("declared_pages")
    if declared_pages is not None and (
        isinstance(declared_pages, bool)
        or not isinstance(declared_pages, int)
        or declared_pages != len(pages)
        or declared_pages < 1
        or declared_pages > MAX_MANIFEST_PAGES
    ):
        raise ValueError(
            f"manifest má neplatný deklarovaný počet; maximum je {MAX_MANIFEST_PAGES} strán"
        )
    source_url = manifest.get("source_url")
    if not isinstance(source_url, str) or not source_url or source_url != source_url.strip():
        raise ValueError("manifest nemá presnú URL zdroja")
    parsed_url = urlparse(source_url)
    if parsed_url.scheme not in ("http", "https") or not parsed_url.netloc:
        raise ValueError("manifest nemá presnú URL zdroja")
    collector_kind = manifest.get("collector_kind")
    derived_kind = source_policy.collector_kind_for_url(source_url)
    if (
        not isinstance(collector_kind, str)
        or collector_kind != derived_kind
        or not source_policy.known_source(store, collector_kind)
    ):
        raise ValueError("manifest nemá dôveryhodný typ zdroja")

    if store == "tesco" and collector_kind == "official-tesco-viewer":
        if (
            isinstance(declared_pages, bool)
            or not isinstance(declared_pages, int)
            or declared_pages < MIN_PLAUSIBLE_PAGES
            or declared_pages > MAX_MANIFEST_PAGES
        ):
            raise ValueError(
                "oficiálny Tesco manifest musí mať deklarovaný počet "
                f"{MIN_PLAUSIBLE_PAGES}..{MAX_MANIFEST_PAGES} strán"
            )
        leaflet_format = manifest.get("leaflet_format")
        expected = {
            "HM": ("Tesco hypermarket", "/hypermarkety/"),
            "SM": ("Tesco supermarket", "/supermarkety/"),
        }.get(leaflet_format)
        if expected is None:
            raise ValueError("oficiálny Tesco manifest má neplatný formát letáku")
        expected_label, expected_path = expected
        if manifest.get("store_label") != expected_label:
            raise ValueError("oficiálny Tesco manifest má neplatné označenie predajne")
        if expected_path not in parsed_url.path:
            raise ValueError("formát Tesco letáku nesedí so zdrojovou URL")

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
            "cena_s_kartou": {"anyOf": [{"type": "number"}, {"type": "null"}]},
            "zlava_s_kartou": {"anyOf": [{"type": "string"}, {"type": "null"}]},
            "vernostny_program": {
                "anyOf": [
                    {"type": "string", "enum": ["Kaufland Card", "Clubcard", "Lidl Plus"]},
                    {"type": "null"},
                ]
            },
            "minimalny_nakup": {"anyOf": [{"type": "number"}, {"type": "null"}]},
            "podmienka_s_kartou": {
                "anyOf": [
                    {"type": "string", "description": "Najviac 160 znakov."},
                    {"type": "null"},
                ]
            },
        },
        "required": [
            "source_page", "nazov", "kategoria", "cena",
            "povodna", "zlava", "jednotka",
            "cena_s_kartou", "zlava_s_kartou", "vernostny_program", "minimalny_nakup",
            "podmienka_s_kartou",
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


def guarded_client(con, client, purpose="zber_letakov"):
    """Guard collector calls without consuming capacity reserved by queued plans."""
    return naklady.strazeny_klient(
        con,
        client,
        purpose,
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
[{{"source_page":12,"nazov":"Bravčové plecko","kategoria":"maso","cena":2.15,"povodna":4.49,"zlava":"−52 %","jednotka":"kg","cena_s_kartou":null,"zlava_s_kartou":null,"vernostny_program":null,"minimalny_nakup":null,"podmienka_s_kartou":null}}]
Pravidlá:
- source_page = presné číslo označené pri obrázku; každá položka ho MUSÍ zopakovať
- kategoria: jedno z maso|zelenina|ovocie|mliecne|trvanlive|pecivo|ine
- cena = najnižšia cena dostupná KAŽDÉMU bez karty, aplikácie, kupónu a bez podmienky minimálneho nákupu. Ak je zľavnená iba cena s kartou, do cena daj bežnú cenu dostupnú bez karty.
- povodna = pôvodná prečiarknutá cena (ak nie je, daj null); zlava patrí výhradne k cene dostupnej každému
- zlava a zlava_s_kartou zapisuj iba ako percento vytlačené v letáku; ak je uvedená iba úspora v eurách, daj null
- cena_s_kartou = nižšia podmienená cena alebo null. Nikdy ňou nenahrádzaj cenu dostupnú každému.
- Ak cena_s_kartou je null, MUSIA byť null aj zlava_s_kartou, vernostny_program, minimalny_nakup a podmienka_s_kartou. Ak pri kartovej akcii nevieš spoľahlivo prečítať bežnú aj kartovú cenu, položku úplne vynechaj.
- vernostny_program: presne Kaufland Card, Clubcard alebo Lidl Plus; inak null
- minimalny_nakup = minimálna celková hodnota nákupu pre cenu s kartou (napr. 20.0) alebo null
- zlava_s_kartou = percento patriace k cene s kartou alebo null
- podmienka_s_kartou = iba ďalšia podmienka, ktorú ostatné polia nevystihujú, napr. "aktivuj kupón v aplikácii" alebo "kúp aspoň 2 kusy"; inak null
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


_PERCENT_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*%")
_DISCOUNT_TOLERANCE_PERCENTAGE_POINTS = 2.0


def _validate_discount_arithmetic(offer):
    """Odhaľ prečítanú cifru, ktorá odporuje percentu vytlačenému v letáku.

    Letáky percentá zaokrúhľujú na celé body, preto tolerujeme dva percentuálne
    body. Chybu typu 0,07 € namiesto 1,69 € však takáto kontrola bezpečne
    odmietne ešte pred zápisom do databázy.
    """
    original = offer.get("povodna")
    if original is None:
        return
    for price_field, discount_field in (
        ("cena", "zlava"),
        ("cena_s_kartou", "zlava_s_kartou"),
    ):
        price = offer.get(price_field)
        discount = offer.get(discount_field)
        if price is None or discount in (None, ""):
            continue
        match = _PERCENT_RE.search(str(discount))
        if match is None:
            raise ValueError(f"{discount_field} nemá čitateľné percento")
        printed = float(match.group(1).replace(",", "."))
        calculated = (1.0 - float(price) / float(original)) * 100.0
        if abs(calculated - printed) > _DISCOUNT_TOLERANCE_PERCENTAGE_POINTS:
            raise ValueError(
                f"{price_field} nezodpovedá {discount_field} a pôvodnej cene"
            )


def _offers_from_extraction(items, *, store, manifest, batch_pages):
    if not isinstance(items, list):
        raise ValueError(f"{store}: extrakcia nevrátila zoznam akcií")
    offers = []
    first_rejection = None
    for item in items:
        try:
            source_page = item.get("source_page") if isinstance(item, dict) else None
            if source_page not in batch_pages:
                raise ValueError(
                    f"{store}: akcia odkazuje na nevybranú zdrojovú stranu"
                )
            offer = {
                "obchod": store.capitalize(),
                "nazov": str(item["nazov"])[:40],
                "kategoria": (item.get("kategoria") or "ine")[:20],
                "cena": float(item["cena"]),
                "povodna": float(item["povodna"]) if item.get("povodna") is not None else None,
                "zlava": item.get("zlava"),
                "jednotka": (item.get("jednotka") or "")[:12],
                "cena_s_kartou": (
                    float(item["cena_s_kartou"])
                    if item.get("cena_s_kartou") is not None else None
                ),
                "zlava_s_kartou": item.get("zlava_s_kartou"),
                "vernostny_program": item.get("vernostny_program"),
                "minimalny_nakup": (
                    float(item["minimalny_nakup"])
                    if item.get("minimalny_nakup") is not None else None
                ),
                "podmienka_s_kartou": (
                    str(item["podmienka_s_kartou"]).strip()
                    if item.get("podmienka_s_kartou") is not None else None
                ),
                "source_url": manifest["source_url"],
                "source_page": source_page,
                "valid_from": manifest["valid_from"],
                "valid_to": manifest["valid_to"],
            }
            if store == "tesco" and manifest.get("collector_kind") == "official-tesco-viewer":
                offer["store_label"] = manifest["store_label"]
            conditional_metadata = (
                offer["zlava_s_kartou"], offer["vernostny_program"],
                offer["minimalny_nakup"], offer["podmienka_s_kartou"],
            )
            if offer["cena_s_kartou"] is None and any(
                value not in (None, "") for value in conditional_metadata
            ):
                raise ValueError("neúplná vernostná cena")
            if offer["cena_s_kartou"] is not None:
                # Obchod poznáme z aktuálne spracúvaného letáku. Názov jeho
                # vernostného programu preto nie je údaj, ktorý má model hádať.
                offer["vernostny_program"] = LOYALTY_PROGRAM_BY_STORE[offer["obchod"]]
            validate_offer(offer)
            _validate_discount_arithmetic(offer)
        except (KeyError, TypeError, ValueError) as exc:
            # Vision občas zle prečíta jedinú cenovku. Taká položka nesmie
            # zhodiť všetky ostatné overené ceny na rovnakej strane. Ak po
            # karanténe na strane nič nezostane, kontrola pokrytia nižšie aj
            # tak vynúti druhé čítanie alebo bezpečný pád celého batchu.
            if first_rejection is None:
                first_rejection = exc
            page = item.get("source_page") if isinstance(item, dict) else "?"
            name = item.get("nazov") if isinstance(item, dict) else "neplatná položka"
            log(
                f"[WARN] {store}: vynechávam neoverenú položku "
                f"na strane {page} ({str(name)[:40]}): {exc}"
            )
            continue
        offers.append(offer)
    if not offers and first_rejection is not None:
        raise first_rejection
    return offers


def _require_every_page(offers, batch_pages):
    represented = {offer["source_page"] for offer in offers}
    missing = set(batch_pages) - represented
    if missing:
        raise ValueError(
            "bez overenej položky zo strán " + ", ".join(map(str, sorted(missing)))
        )
    return offers


def _read_offer_batch(client, *, store, manifest, batch_pages, content):
    """Sonnet first; Opus only when the whole batch cannot be trusted."""
    fallback_reason = None
    try:
        items = claude_json(
            client, MODEL_READ, content, READ_TOKENS, effort=READ_EFFORT
        )
        offers = _offers_from_extraction(
            items, store=store, manifest=manifest, batch_pages=batch_pages
        )
        return _require_every_page(offers, batch_pages)
    except naklady.KreditVycerpany:
        raise
    except Exception as exc:
        fallback_reason = f"{type(exc).__name__}: {exc}"

    log(
        f"[WARN] {store}: Sonnet dávka {list(batch_pages)} je neistá "
        f"({fallback_reason}) — overujem Opusom"
    )
    try:
        items = claude_json(
            client,
            MODEL_READ_FALLBACK,
            content,
            READ_TOKENS,
            effort=READ_FALLBACK_EFFORT,
        )
        return _require_every_page(
            _offers_from_extraction(
                items, store=store, manifest=manifest, batch_pages=batch_pages
            ),
            batch_pages,
        )
    except naklady.KreditVycerpany:
        raise
    except Exception as exc:
        raise ValueError(
            f"{store}: extrakcia strán zlyhala aj po overení Opusom "
            f"({type(exc).__name__}: {exc})"
        ) from exc


def _collect_validated_flyer(
        client, store, manifest, page_manifest, *, tesco_page_scans=None,
        tesco_page_paths=None):
    tesco_page_scans = tesco_page_scans or {}
    tesco_page_paths = tesco_page_paths or {}

    # 1) lacný sken náhľadov → ktoré strany sú potravinové
    thumbs = []
    for source_page, page in page_manifest.items():
        try:
            if source_page in tesco_page_scans:
                encoded = tesco_page_scans[source_page]
            else:
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

    # 2) presné čítanie cien: Sonnet 5, pri neistote iba daná dávka Opusom 5
    out = []
    for batch_pages in batches(food, READ_BATCH_SIZE):
        content = []
        for source_page in batch_pages:
            try:
                if source_page in tesco_page_paths:
                    encoded = image_bytes_b64(
                        tesco_page_paths[source_page].read_bytes(), READ_PX
                    )
                else:
                    encoded = get_b64(page_manifest[source_page]["image_url"], READ_PX)
            except Exception as exc:
                raise ValueError(f"{store}: strana {source_page} sa nepodarilo načítať") from exc
            if not encoded:
                raise ValueError(f"{store}: strana {source_page} sa nepodarilo načítať")
            content.append({"type": "text", "text": f"Zdrojová strana {source_page}:"})
            content.append(img_block(encoded))
        content.append({"type": "text", "text": EXTRACT_PROMPT.format(store=store.upper())})
        out.extend(
            _read_offer_batch(
                client,
                store=store,
                manifest=manifest,
                batch_pages=batch_pages,
                content=content,
            )
        )
    if not out:
        raise ValueError(f"{store}: extrakcia nevrátila žiadne overené akcie")
    log(f"[INFO] {store}: {len(out)} akcií")
    return out


def _stage_official_tesco_pages(page_manifest, directory):
    """Download each protected page once and retain only small scans in RAM."""
    directory = Path(directory)
    scans = {}
    paths = {}
    for source_page, page in page_manifest.items():
        # The filename is derived solely from the already validated integer page
        # number. Neither the bridge URL nor its secret token reaches the path.
        path = directory / f"tesco-page-{source_page:03d}.jpeg"
        try:
            content = _download_official_tesco_page(page["image_url"])
            if not content:
                raise ValueError("prázdna odpoveď")
            path.write_bytes(content)
            scan_image = image_bytes_b64(content, SCAN_PX)
        except Exception:
            raise ValueError(
                f"tesco: strana {source_page} sa nepodarilo načítať"
            ) from None
        if not scan_image:
            raise ValueError(f"tesco: strana {source_page} sa nepodarilo načítať")
        scans[source_page] = scan_image
        paths[source_page] = path
    return scans, paths


def zbieraj(client, store):
    pages, manifest = store_pages(store)
    if not pages:
        raise ValueError(f"{store}: leták s konečnou platnosťou nebol nájdený")
    page_manifest = validate_flyer_manifest(pages, manifest, store=store)

    if store == "tesco" and manifest.get("collector_kind") == "official-tesco-viewer":
        # TemporaryDirectory removes protected originals after success and after
        # every exception. Read-size images are created only for the active batch.
        with tempfile.TemporaryDirectory(prefix="uvarsi-tesco-") as directory:
            scans, paths = _stage_official_tesco_pages(page_manifest, directory)
            return _collect_validated_flyer(
                client,
                store,
                manifest,
                page_manifest,
                tesco_page_scans=scans,
                tesco_page_paths=paths,
            )

    return _collect_validated_flyer(client, store, manifest, page_manifest)


def _collection_provenance(offers):
    if not offers:
        return (None, None, None, None)
    urls = {item.get("source_url") for item in offers if isinstance(item, dict)}
    starts = {item.get("valid_from") for item in offers if isinstance(item, dict)}
    ends = {item.get("valid_to") for item in offers if isinstance(item, dict)}
    if len(urls) != 1 or not starts or not ends:
        raise ValueError("zber nemá jednotnú internú provenienciu")
    source_url = urls.pop()
    collector_kind = source_policy.collector_kind_for_url(source_url)
    if collector_kind is None:
        raise ValueError("zber používa neznámy zdroj")
    return (
        collector_kind,
        source_policy.source_fingerprint(source_url),
        min(starts),
        max(ends),
    )


def record_store_outcome(con, week, store, status, count=0, detail=None, offers=None):
    """Zapíš výsledok zberu jedného obchodu, aby bol čiastočný beh viditeľný."""
    provenance = _collection_provenance(offers) if status == "ok" else (None,) * 4
    con.execute(
        """INSERT INTO zber_stav
           (tyzden, obchod, stav, pocet, detail, data_version,
            collector_kind,source_fingerprint,valid_from,valid_to,updated)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
           ON CONFLICT(tyzden, obchod) DO UPDATE SET
             stav=excluded.stav, pocet=excluded.pocet,
             detail=excluded.detail, data_version=excluded.data_version,
             collector_kind=excluded.collector_kind,
             source_fingerprint=excluded.source_fingerprint,
             valid_from=excluded.valid_from,valid_to=excluded.valid_to,
             updated=excluded.updated""",
        (week, store, status, count, detail, COLLECTION_DATA_VERSION, *provenance),
    )
    con.commit()


def official_kaufland_main():
    """Bezplatná opravná cesta pre Kaufland; nikdy nenačíta Anthropic kľúč."""
    tyz = monday()
    con = db()
    try:
        offers = official_kaufland_offers()
        if len(offers) < MIN_VERIFIED_OFFERS_PER_STORE:
            raise ValueError(
                f"kaufland: iba {len(offers)} overených oficiálnych akcií; "
                f"minimum je {MIN_VERIFIED_OFFERS_PER_STORE}"
            )
        con.commit()
        con.execute("BEGIN IMMEDIATE")
        replace_store_week(con, tyz, "Kaufland", offers)
        record_store_outcome(
            con, tyz, "Kaufland", "ok", len(offers), offers=offers
        )
    except Exception as exc:
        con.rollback()
        raise SystemExit(f"Oficiálny zber Kauflandu zlyhal: {exc}") from None
    finally:
        con.close()
    log(f"[OK] Kaufland: uložených {len(offers)} oficiálnych akcií bez AI.")


def collection_budget_purpose(con, week, selected_stores):
    """Use a separate bounded budget for a schema reread and one safe repair."""
    stores = [store.capitalize() for store in selected_stores]
    if not stores:
        return "zber_letakov"
    placeholders = ",".join("?" for _ in stores)
    rows = con.execute(
        f"SELECT stav, data_version FROM zber_stav WHERE tyzden=? AND obchod IN ({placeholders})",
        (week, *stores),
    ).fetchall()
    if rows and any(
        row["stav"] != "ok" or int(row["data_version"] or 0) < COLLECTION_DATA_VERSION
        for row in rows
    ):
        return "zber_migracia"
    return "zber_letakov"


def main(stores=None):
    import anthropic
    selected_stores = list(dict.fromkeys(stores or STORES))
    unknown = [store for store in selected_stores if store not in STORES]
    if unknown:
        raise ValueError(f"Neznámy obchod: {', '.join(unknown)}")
    tyz = monday()
    con = db()
    budget_purpose = collection_budget_purpose(con, tyz, selected_stores)
    # Najprv over dostatočnú štartovaciu rezervu, až potom zaber jeden z mála
    # týždenných pokusov. Tak sa cielená obnova Tesca a Lidla môže po polnoci
    # sama rozbehnúť s čerstvým denným rozpočtom namiesto zlyhania tesne pred
    # cieľom a spálenia posledného povoleného behu.
    try:
        naklady.skontroluj(
            con,
            budget_purpose,
            odhad_eur=0.0,
            rezervovane_eur=MIN_START_BUDGET_PER_STORE_EUR * len(selected_stores),
        )
    except naklady.KreditVycerpany as odmietnutie:
        con.close()
        raise SystemExit(f"Zber zastavený — KREDIT_VYCERPANY: {odmietnutie}") from None
    except naklady.RozpocetVycerpany as odmietnutie:
        con.close()
        raise SystemExit(f"Zber odkladám — {odmietnutie}") from None
    # Vision beh je najdrahšia operácia v celej appke (~0,37 € za obchod). Miesto
    # v týždennom počte behov sa berie EŠTE PRED prvým volaním — vďaka tomu je
    # rozbehnutá slučka štrukturálne nemožná, nie iba nepravdepodobná. Presne
    # toto chýbalo, keď dozorca 12× po sebe zaplatil za ten istý márny beh.
    try:
        raw_client = anthropic.Anthropic(
            api_key=load_key(), timeout=180.0, max_retries=1
        )
    except BaseException:
        con.close()
        raise
    try:
        naklady.rezervuj_beh(con, budget_purpose)
    except naklady.RozpocetVycerpany as odmietnutie:
        con.close()
        raise SystemExit(f"Zber nespúšťam — {odmietnutie}")
    # Cez strážený klient sa nedá zavolať model bez zaúčtovania a bez stropu.
    client = guarded_client(
        con,
        raw_client,
        budget_purpose,
    )
    total, failures, structural_failures, collected = 0, [], [], []
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
                naklady.uvolni_beh(con, budget_purpose)
                log(f"[ERROR] {store}: {odmietnutie}")
                raise SystemExit(
                    f"Zber zastavený — KREDIT_VYCERPANY: {odmietnutie}"
                ) from None
            except ValueError as exc:
                failures.append(store)
                structural_failures.append(store)
                record_store_outcome(con, tyz, store.capitalize(), "fail", 0, str(exc)[:300])
                log(f"[ERROR] {store}: zber zlyhal ({exc})")
                continue
            except Exception as exc:
                failures.append(store)
                record_store_outcome(con, tyz, store.capitalize(), "fail", 0, str(exc)[:300])
                log(f"[ERROR] {store}: dočasný zber zlyhal ({exc})")
                continue
            total += len(akcie)
            collected.append(store)
            record_store_outcome(
                con, tyz, store.capitalize(), "ok", len(akcie), offers=akcie
            )
        n = con.execute("SELECT COUNT(*) c FROM akcie WHERE tyzden=?", (tyz,)).fetchone()["c"]
    finally:
        con.close()
    # Strojovo čitateľný súhrn: dozorca sa nesmie spoliehať na počet riadkov,
    # dva zdravé obchody ho vždy prevýšia a tretí sa už nikdy nedozberá.
    log("[SUMMARY] " + json.dumps(
        {
            "tyzden": tyz,
            "ok": collected,
            "fail": failures,
            "structural_fail": structural_failures,
            "akcie": total,
        },
        ensure_ascii=False, sort_keys=True))
    if failures:
        if len(structural_failures) == len(failures):
            log("ZBER_STRUKTURALNY: všetky neúspešné obchody zlyhali "
                "na rovnakej validácii vstupu alebo extrakcie")
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
    parser.add_argument(
        "--official-kaufland-only",
        action="store_true",
        help="bezplatný cielený zber z oficiálneho Kaufland prehľadu",
    )
    args = parser.parse_args(argv)
    if args.official_kaufland_only:
        if args.stores:
            parser.error("--official-kaufland-only nemožno kombinovať s --store")
        official_kaufland_main()
        return 0
    main(args.stores)
    return 0


if __name__ == "__main__":
    raise SystemExit(cli())
