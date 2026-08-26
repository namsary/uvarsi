#!/usr/bin/env python3
"""Uvar.si — RELEASE GATE (spustiteľná časť release procesu).

Nie je to kontrolný zoznam na čítanie. Spustí testy, overí živú produkciu
a vydá jeden z troch verdiktov:

    BLOCKED             niečo neprešlo — nesmie sa tvrdiť, že je hotovo
    LOCAL PASS          lokálne zelené, ale v produkcii to ešte nie je
    PRODUCTION VERIFIED  živý web dokázateľne beží na tejto verzii

Každý beh sa zapíše do RELEASE_LOG.md, aby sa nikdy nestalo
„opravené v kóde, ale nie na serveri".

Použitie:
    python3 nastroje/release_gate.py              # testy + produkcia
    python3 nastroje/release_gate.py --len-testy  # bez siete
"""
from __future__ import annotations

import argparse
import datetime
import json
import re
import subprocess
import sys
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

KOREN = Path(__file__).resolve().parent.parent
LOG = KOREN / "RELEASE_LOG.md"
WEB = "https://uvar.si"
WWW = "https://www.uvar.si"
SAFE_HEADERS = ("cache-control", "content-type", "location", "x-robots-tag")
BODY_LIMIT = 20_000
DETAIL_LIMIT = 180
FONT_PATH = "/static/fonts/manrope-400-800.7101939e.woff2"
CANONICKE_URL = (
    "https://uvar.si/",
    "https://uvar.si/co-varit-tento-tyzden",
    "https://uvar.si/lacny-jedalnicek",
    "https://uvar.si/ako-varime-z-akcii",
)
CONTENT_LINKS = (
    "/co-varit-tento-tyzden",
    "/lacny-jedalnicek",
    "/ako-varime-z-akcii",
)

# Windows-only testy (volajú cscript.exe / Git bash) sa mimo Windows nedajú spustiť
MIMO_WINDOWS = ["tests/test_app_html_contract.py", "tests/test_dozorca_contract.py"]

MIN_PONUK = 30          # rovnaký prah ako hetzner/dozorca.sh


class Zistenie:
    def __init__(self, nazov: str, ok: bool, detail: str, blokuje: bool = True):
        self.nazov, self.ok, self.detail, self.blokuje = nazov, ok, detail, blokuje

    def __str__(self) -> str:
        znak = "OK  " if self.ok else ("!!  " if self.blokuje else "--  ")
        return f"  {znak}{self.nazov}: {self.detail}"


@dataclass(frozen=True)
class HttpResponse:
    status: int
    body: bytes
    headers: dict[str, str]
    url: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "headers",
            {str(name).lower(): str(value) for name, value in self.headers.items()},
        )

    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")


def pondelok(d: datetime.date | None = None) -> str:
    d = d or datetime.date.today()
    return (d - datetime.timedelta(days=d.weekday())).isoformat()


def _spusti(prikaz: list[str], timeout: int = 600) -> tuple[int, str]:
    try:
        v = subprocess.run(prikaz, cwd=KOREN, capture_output=True, text=True, timeout=timeout)
        return v.returncode, (v.stdout + v.stderr)
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        return 1, str(e)


def _normalize_whitespace(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _snippet(text: str, limit: int = DETAIL_LIMIT) -> str:
    normalized = _normalize_whitespace(text)
    if len(normalized) <= limit:
        return normalized
    return normalized[: limit - 1] + "…"


def _http_detail(response: HttpResponse) -> str:
    if response.status:
        return f"HTTP {response.status}"
    detail = _snippet(response.text())
    return f"chyba spojenia: {detail or 'bez detailu'}"


def _safe_headers(headers) -> dict[str, str]:
    safe: dict[str, str] = {}
    for name in SAFE_HEADERS:
        value = headers.get(name)
        if value:
            safe[name] = value
    return safe


def _read_bounded_body(stream) -> bytes:
    try:
        return stream.read(BODY_LIMIT)
    except Exception:
        return b""


class _BezPresmerovania(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _cela_url(cesta: str) -> str:
    if cesta.startswith("http://") or cesta.startswith("https://"):
        return cesta
    return WEB + cesta


# ----------------------------------------------------------------- lokálne
def brana_testy() -> list[Zistenie]:
    prikaz = [sys.executable, "-m", "pytest", "tests/", "-q"]
    for s in MIMO_WINDOWS:
        prikaz += ["--deselect", s]
    kod, vystup = _spusti(prikaz)
    m = re.search(r"(\d+) passed", vystup)
    preslo = int(m.group(1)) if m else 0
    zlyhalo = int(m.group(1)) if (m := re.search(r"(\d+) failed", vystup)) else 0
    return [Zistenie(
        "testy", kod == 0 and zlyhalo == 0,
        f"{preslo} presly, {zlyhalo} zlyhalo"
        + ("" if kod == 0 else f" (pytest kod {kod})"))]


def brana_git() -> list[Zistenie]:
    z = []
    kod, sha = _spusti(["git", "rev-parse", "HEAD"], timeout=30)
    sha = sha.strip()[:12] if kod == 0 else "?"
    z.append(Zistenie("git revizia", kod == 0, sha))

    kod, stav = _spusti(["git", "status", "--porcelain"], timeout=30)
    nezapisane = [r for r in stav.splitlines() if r.strip()]
    z.append(Zistenie("nezapisane zmeny", not nezapisane,
                      "ziadne" if not nezapisane
                      else f"{len(nezapisane)} suborov nie je commitnutych"))

    kod, vzdialene = _spusti(["git", "rev-parse", "origin/main"], timeout=30)
    if kod == 0:
        rovnake = vzdialene.strip()[:12] == sha
        z.append(Zistenie("push na origin/main", rovnake,
                          "lokalne == vzdialene" if rovnake
                          else f"vzdialene je {vzdialene.strip()[:12]} — treba pushnut"))
    return z


def brana_verzia() -> list[Zistenie]:
    v = KOREN / "VERSION"
    if not v.is_file():
        return [Zistenie("VERSION", False, "subor chyba")]
    return [Zistenie("VERSION", True, v.read_text(encoding="utf-8").strip())]


# ----------------------------------------------------------------- produkcia
def _ziskaj(cesta: str, timeout: int = 25, follow_redirects: bool = True) -> HttpResponse:
    url = _cela_url(cesta)
    request = urllib.request.Request(url, headers={"User-Agent": "uvarsi-release-gate/1.0"})
    opener = urllib.request.build_opener() if follow_redirects else urllib.request.build_opener(_BezPresmerovania)
    try:
        with opener.open(request, timeout=timeout) as o:
            return HttpResponse(o.status, _read_bounded_body(o), _safe_headers(o.headers), url)
    except urllib.error.HTTPError as e:
        return HttpResponse(e.code, _read_bounded_body(e), _safe_headers(e.headers), url)
    except Exception as e:                                  # sieť/TLS
        return HttpResponse(0, str(e).encode("utf-8", errors="replace"), {}, url)


def _signal_tyzdna(landing: dict) -> str | None:
    week_label = landing.get("week_label")
    if isinstance(week_label, str) and week_label.strip():
        return week_label.strip()

    shared_from: datetime.date | None = None
    shared_to: datetime.date | None = None
    sources = landing.get("sources")
    if isinstance(sources, list):
        for source in sources:
            if not isinstance(source, dict):
                continue
            valid_from = source.get("valid_from")
            valid_to = source.get("valid_to")
            if not isinstance(valid_from, str) or not isinstance(valid_to, str):
                continue
            try:
                parsed_from = datetime.date.fromisoformat(valid_from)
                parsed_to = datetime.date.fromisoformat(valid_to)
            except ValueError:
                continue
            shared_from = parsed_from if shared_from is None or parsed_from > shared_from else shared_from
            shared_to = parsed_to if shared_to is None or parsed_to < shared_to else shared_to
    if shared_from and shared_to and shared_from <= shared_to:
        return f"{shared_from.day}. {shared_from.month}. {shared_from.year} - {shared_to.day}. {shared_to.month}. {shared_to.year}"

    week = landing.get("week")
    if isinstance(week, str) and week.strip():
        return week.strip()
    return None


def _json_ld_ok(html: str) -> tuple[bool, str]:
    blocks = re.findall(
        r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if not blocks:
        return False, "script type=application/ld+json chýba"
    for index, block in enumerate(blocks, start=1):
        try:
            json.loads(block)
        except ValueError as error:
            return False, f"blok {index}: {error}"
    return True, f"{len(blocks)} JSON-LD blok(y)"


def _canonical_links(html: str) -> list[str]:
    return re.findall(
        r'<link[^>]*rel=["\']canonical["\'][^>]*href=["\']([^"\']+)["\']',
        html,
        flags=re.IGNORECASE,
    )


def _has_internal_link(html: str, path: str) -> bool:
    return f'href="{path}"' in html or f'href="{WEB}{path}"' in html


def _seo_zistenia(fetch, landing_payload: dict | None) -> list[Zistenie]:
    z: list[Zistenie] = []

    robots = fetch("/robots.txt")
    robots_text = robots.text() if robots.status == 200 else ""
    z.append(Zistenie("robots.txt", robots.status == 200, _http_detail(robots)))
    z.append(Zistenie(
        "robots.txt OAI-SearchBot",
        robots.status == 200 and "OAI-SearchBot" in robots_text,
        "obsahuje OAI-SearchBot" if robots.status == 200 and "OAI-SearchBot" in robots_text else (_http_detail(robots) if robots.status != 200 else "chýba OAI-SearchBot"),
    ))
    z.append(Zistenie(
        "robots.txt blokuje /api/",
        robots.status == 200 and "Disallow: /api/" in robots_text,
        "obsahuje Disallow: /api/" if robots.status == 200 and "Disallow: /api/" in robots_text else (_http_detail(robots) if robots.status != 200 else "chýba Disallow: /api/"),
    ))
    z.append(Zistenie(
        "robots.txt sitemap",
        robots.status == 200 and "Sitemap: https://uvar.si/sitemap.xml" in robots_text,
        "obsahuje sitemap URL" if robots.status == 200 and "Sitemap: https://uvar.si/sitemap.xml" in robots_text else (_http_detail(robots) if robots.status != 200 else "chýba Sitemap: https://uvar.si/sitemap.xml"),
    ))

    sitemap = fetch("/sitemap.xml")
    sitemap_text = sitemap.text() if sitemap.status == 200 else ""
    locs: set[str] = set()
    parse_error: str | None = None
    if sitemap.status == 200:
        try:
            root = ET.fromstring(sitemap_text)
            namespace = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
            locs = {
                (node.text or "").strip()
                for node in root.findall("sm:url/sm:loc", namespace)
                if (node.text or "").strip()
            }
        except ET.ParseError as error:
            parse_error = str(error)
    z.append(Zistenie("sitemap.xml", sitemap.status == 200, _http_detail(sitemap)))
    z.append(Zistenie(
        "sitemap.xml XML",
        sitemap.status == 200 and parse_error is None,
        "validné XML" if sitemap.status == 200 and parse_error is None else (_http_detail(sitemap) if sitemap.status != 200 else f"neplatné XML: {parse_error}"),
    ))
    for canonical in CANONICKE_URL:
        z.append(Zistenie(
            f"sitemap obsahuje {canonical}",
            sitemap.status == 200 and parse_error is None and canonical in locs,
            canonical if sitemap.status == 200 and parse_error is None and canonical in locs else (_http_detail(sitemap) if sitemap.status != 200 else f"chýba {canonical}"),
        ))

    weekly = fetch("/co-varit-tento-tyzden")
    budget = fetch("/lacny-jedalnicek")
    method = fetch("/ako-varime-z-akcii")
    for path, response in (
        ("SEO /co-varit-tento-tyzden", weekly),
        ("SEO /lacny-jedalnicek", budget),
        ("SEO /ako-varime-z-akcii", method),
    ):
        z.append(Zistenie(path, response.status == 200, _http_detail(response)))

    expected_signal = _signal_tyzdna(landing_payload or {})
    weekly_text = weekly.text() if weekly.status == 200 else ""
    signal_ok = weekly.status == 200 and isinstance(expected_signal, str) and expected_signal in weekly_text
    if signal_ok:
        signal_detail = expected_signal
    elif weekly.status != 200:
        signal_detail = _http_detail(weekly)
    elif not expected_signal:
        signal_detail = "v landing JSON chýba week_label alebo rozsah"
    else:
        signal_detail = f"čakám '{expected_signal}', našiel som '{_snippet(weekly_text)}'"
    z.append(Zistenie("týždenný SEO signál", signal_ok, signal_detail))

    for path, name in (("/app", "/app noindex"), ("/prihlasenie", "/prihlasenie noindex")):
        response = fetch(path)
        x_robots = response.headers.get("x-robots-tag", "")
        ok = response.status == 200 and "noindex" in x_robots.lower()
        detail = x_robots or (_http_detail(response) if response.status != 200 else "hlavička X-Robots-Tag chýba")
        z.append(Zistenie(name, ok, detail))

    font = fetch(FONT_PATH)
    cache_control = font.headers.get("cache-control", "")
    immutable = cache_control.lower()
    z.append(Zistenie(
        "font immutable cache",
        font.status == 200 and "max-age=31536000" in immutable and "immutable" in immutable,
        cache_control or (_http_detail(font) if font.status != 200 else "Cache-Control chýba"),
    ))

    redirect = fetch(f"{WWW}/co-varit-tento-tyzden", follow_redirects=False)
    location = redirect.headers.get("location", "")
    redirect_ok = redirect.status in (301, 308) and location == f"{WEB}/co-varit-tento-tyzden"
    z.append(Zistenie(
        "www weekly redirect",
        redirect_ok,
        f"HTTP {redirect.status}, Location {location or '?'}",
    ))

    homepage = fetch("/")
    homepage_html = homepage.text() if homepage.status == 200 else ""
    canonicals = _canonical_links(homepage_html) if homepage.status == 200 else []
    z.append(Zistenie(
        "landing canonical",
        homepage.status == 200 and canonicals == [f"{WEB}/"],
        canonicals[0] if canonicals == [f"{WEB}/"] else (_http_detail(homepage) if homepage.status != 200 else f"canonical: {canonicals or ['chýba']}"),
    ))
    json_ld_ok, json_ld_detail = _json_ld_ok(homepage_html) if homepage.status == 200 else (False, _http_detail(homepage))
    z.append(Zistenie("landing JSON-LD", homepage.status == 200 and json_ld_ok, json_ld_detail))
    missing_links = [path for path in CONTENT_LINKS if not _has_internal_link(homepage_html, path)]
    z.append(Zistenie(
        "landing interné odkazy",
        homepage.status == 200 and not missing_links,
        "všetky tri odkazy sú prítomné" if homepage.status == 200 and not missing_links else (_http_detail(homepage) if homepage.status != 200 else f"chýbajú {', '.join(missing_links)}"),
    ))

    return z


def brana_produkcia(ocakavana_verzia: str) -> list[Zistenie]:
    z = []
    cache: dict[tuple[str, bool], HttpResponse] = {}

    def fetch(cesta: str, follow_redirects: bool = True) -> HttpResponse:
        key = (cesta, follow_redirects)
        if key not in cache:
            cache[key] = _ziskaj(cesta, follow_redirects=follow_redirects)
        return cache[key]

    health = fetch("/api/health")
    if health.status != 200:
        z.append(Zistenie("/api/health", False, f"{_http_detail(health)} — appka neodpoveda"))
        return z
    try:
        h = json.loads(health.text())
    except ValueError:
        z.append(Zistenie("/api/health", False, "odpoved nie je JSON"))
        return z

    z.append(Zistenie("/api/health", True, json.dumps(h, ensure_ascii=False)))

    ziva = str(h.get("vydanie", "?"))
    z.append(Zistenie("verzia na webe", ziva == ocakavana_verzia,
                      f"{ziva} (ocakavam {ocakavana_verzia})"))

    tyz = str(h.get("tyzden", ""))
    z.append(Zistenie("tyzden dat", tyz == pondelok(),
                      f"{tyz} (aktualny pondelok {pondelok()})"))

    pocet = int(h.get("pocet") or 0)
    z.append(Zistenie("pocet ponuk", pocet >= MIN_PONUK, f"{pocet} (prah {MIN_PONUK})"))

    for cesta, popis in (("/", "landing"), ("/app", "appka"),
                         ("/api/public/landing", "landing JSON"),
                         ("/prihlasenie", "prihlasovacia stranka")):
        odpoved = fetch(cesta)
        z.append(Zistenie(popis, odpoved.status == 200, f"HTTP {odpoved.status}"))

    # legitimnost: landing nesmie tvrdit uspory bez dat
    landing_payload: dict | None = None
    landing_response = fetch("/api/public/landing")
    if landing_response.status == 200:
        try:
            landing_payload = json.loads(landing_response.text())
            sedi = landing_payload.get("week") == pondelok()
            z.append(Zistenie("landing je na aktualny tyzden", sedi,
                              f"{landing_payload.get('week')} vs {pondelok()}"))
        except ValueError:
            z.append(Zistenie("landing JSON", False, "nie je platny JSON"))
    z += _seo_zistenia(fetch, landing_payload)
    return z


# ----------------------------------------------------------------- verdikt
def zapis_log(verdikt: str, zistenia: list[Zistenie], verzia: str) -> None:
    riadky = [f"\n## {datetime.datetime.now():%Y-%m-%d %H:%M} — {verdikt} (vydanie {verzia})\n"]
    riadky += [str(x).rstrip() + "\n" for x in zistenia]
    with LOG.open("a", encoding="utf-8") as f:
        f.writelines(riadky)


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--len-testy", action="store_true", help="preskoci overenie produkcie")
    a = p.parse_args()

    zistenia = brana_verzia() + brana_testy() + brana_git()
    verzia = zistenia[0].detail

    if not a.len_testy:
        zistenia += brana_produkcia(verzia)

    blokery = [x for x in zistenia if not x.ok and x.blokuje]
    if blokery:
        verdikt = "BLOCKED"
    elif a.len_testy:
        verdikt = "LOCAL PASS"
    else:
        verdikt = "PRODUCTION VERIFIED"

    print(f"\n=== RELEASE GATE — {verdikt}\n")
    for x in zistenia:
        print(x)
    if blokery:
        print("\nBlokuje:")
        for x in blokery:
            print(f"  - {x.nazov}: {x.detail}")
    print()

    zapis_log(verdikt, zistenia, verzia)
    return 0 if verdikt == "PRODUCTION VERIFIED" else (0 if verdikt == "LOCAL PASS" else 1)


if __name__ == "__main__":
    raise SystemExit(main())
