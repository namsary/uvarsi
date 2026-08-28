"""Čo musí platiť, aby sa appka aj landing otvorili rýchlo na mobilných dátach.

Rýchlosť je merateľná vlastnosť, nie dojem. Tento súbor stráži tri veci, ktoré
sa v praxi pokazia ako prvé:

  1. prvé vykreslenie nesmie čakať na cudziu doménu (fonty z Google),
  2. service worker musí naozaj ovládať /app, inak PWA nemá offline škrupinu,
  3. prenesené bajty nesmú ticho narásť späť.

Testy sú čisto v Pythone (prípadne cez node), aby bežali aj na Linuxe —
na rozdiel od tests/test_app_html_contract.py, ktorý potrebuje cscript.exe.
"""
import gzip
import posixpath
import re
import shutil
import subprocess
from pathlib import Path

import pytest


APP = Path("app/static/app.html")
LANDING = Path("index.html")
FONT_DIR = Path("app/static/fonts")
NODE = shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="node runtime is not available")

PAGES = (APP, LANDING)

# Slovenská diakritika z dizajnu — malé aj VEĽKÉ. Veľké písmená nie sú
# teoretické: .display má text-transform:uppercase, takže „ľahké" sa vykreslí
# ako „ĽAHKÉ" a chýbajúce Ľ by bolo vidieť na nadpise.
SLOVAK = "ľščťžýáíéôúäňóŕĺďĽŠČŤŽÝÁÍÉÔÚÄŇÓŔĹĎ"

THIRD_PARTY_FONT_HOSTS = ("fonts.googleapis.com", "fonts.gstatic.com")


def read(path):
    return path.read_text(encoding="utf-8")


def head_of(html):
    return html.split("</head>", 1)[0]


def font_faces(html):
    """Vráti (rodina, váha, [zdroje]) pre každé @font-face na stránke."""
    faces = []
    for block in re.findall(r"@font-face\s*\{[^}]*\}", html):
        family = re.search(r"font-family:\s*['\"]?([^;'\"]+)", block)
        weight = re.search(r"font-weight:\s*([^;]+)", block)
        faces.append((
            family.group(1).strip() if family else "",
            weight.group(1).strip() if weight else "",
            re.findall(r"url\(([^)]+)\)", block),
            block,
        ))
    return faces


def repo_path_for_url(url):
    """Mapovanie verejnej adresy na súbor v repozitári (podľa Caddy blokov).

    /static/*  obsluhuje uvicorn z app/static/, všetko ostatné je koreň webu,
    kam sa nasadzuje index.html.
    """
    path = url.split("?", 1)[0]
    if path.startswith("/static/"):
        return Path("app") / path.lstrip("/")
    return Path(path.lstrip("/"))


# ------------------------------------------------------------------ fonty
@pytest.mark.parametrize("page", PAGES, ids=lambda p: p.name)
def test_first_paint_never_waits_for_a_third_party_font_origin(page):
    """Render-blocking CSS z fonts.googleapis.com = prvé písmeno až po cudzej doméne.

    Zároveň je to GDPR vec: každý návštevník posielal svoju IP Googlu.
    """
    html = read(page)
    for host in THIRD_PARTY_FONT_HOSTS:
        assert host not in html, (
            f"{page} stále siaha na {host} — prvé vykreslenie čaká na cudziu doménu"
        )


@pytest.mark.parametrize("page", PAGES, ids=lambda p: p.name)
def test_every_font_is_declared_and_served_from_our_own_origin(page):
    html = read(page)
    faces = font_faces(html)
    families = {family for family, _, _, _ in faces}

    for needed in ("Anton", "IBM Plex Mono", "Manrope"):
        assert needed in families, f"{page} používa {needed}, musí ho aj sama servírovať"

    assert faces, "žiadne @font-face — dizajn by spadol na systémové písmo"
    for family, _, sources, block in faces:
        assert "font-display:swap" in block.replace(" ", ""), (
            f"{family}: bez font-display:swap je text neviditeľný, kým sa font sťahuje"
        )
        assert sources, f"{family}: @font-face bez zdroja"
        for source in sources:
            url = source.strip("'\"")
            assert url.startswith("/static/fonts/"), (
                f"{family}: {url} nie je z nášho pôvodu"
            )
            assert repo_path_for_url(url).is_file(), (
                f"{family}: {url} nie je v repozitári — nasadenie by ho neprenieslo"
            )


@pytest.mark.parametrize("page", PAGES, ids=lambda p: p.name)
def test_declared_weights_cover_every_weight_the_design_uses(page):
    """Manrope 400/600/800 a IBM Plex Mono 400/600 — inak prehliadač písmo falšuje."""
    html = read(page)
    covered = {}
    for family, weight, _, _ in font_faces(html):
        numbers = [int(n) for n in re.findall(r"\d+", weight)] or [400]
        low, high = min(numbers), max(numbers)
        covered.setdefault(family, set()).update(
            w for w in (400, 500, 600, 700, 800, 900) if low <= w <= high
        )

    assert {400, 600, 800} <= covered.get("Manrope", set())
    assert {400, 600} <= covered.get("IBM Plex Mono", set())
    assert 400 in covered.get("Anton", set())


@pytest.mark.parametrize("page", PAGES, ids=lambda p: p.name)
def test_the_two_fonts_visible_above_the_fold_are_preloaded(page):
    """Anton (nadpisy, logo) a Manrope (text) sa načítajú hneď, nie až po layoute."""
    head = head_of(read(page))
    preloads = re.findall(r"<link[^>]+rel=[\"']preload[\"'][^>]*>", head)
    hrefs = " ".join(preloads)

    assert "anton" in hrefs, "Anton drží značku aj nadpis — patrí do preloadu"
    assert "manrope" in hrefs, "Manrope je bežný text — patrí do preloadu"
    for tag in preloads:
        assert 'as="font"' in tag and "crossorigin" in tag, (
            "preload fontu bez as=font a crossorigin sťahuje súbor dvakrát: " + tag
        )


def test_shipped_font_files_are_real_woff2_and_stay_small():
    files = sorted(FONT_DIR.glob("*.woff2"))
    assert files, "žiadne self-hostované fonty v app/static/fonts/"

    total = 0
    for path in files:
        data = path.read_bytes()
        assert data[:4] == b"wOF2", f"{path.name} nie je woff2"
        total += len(data)

    # Google dnes posiela 8 unikátnych súborov = 147 928 B (latin + latin-ext).
    assert total <= 90_000, f"fonty narástli na {total} B; strop je 90 000 B"
    assert len(files) <= 5, f"{len(files)} súborov = {len(files)} spojení navyše"


def test_self_hosted_fonts_really_contain_the_slovak_diacritics():
    """Subsetovanie je tichý zabijak: font sa načíta, ale ľ/š/č chýbajú."""
    pytest.importorskip("fontTools", reason="fontTools is not installed")
    from fontTools.ttLib import TTFont

    files = sorted(FONT_DIR.glob("*.woff2"))
    assert files
    for path in files:
        cmap = TTFont(path).getBestCmap()
        missing = [c for c in SLOVAK if ord(c) not in cmap]
        assert not missing, f"{path.name} nemá znaky: {''.join(missing)}"
        assert ord("€") in cmap, f"{path.name} nemá €, a celá appka je o cenách"


def test_the_font_licence_ships_with_the_fonts():
    licence = FONT_DIR / "LICENSE.txt"
    assert licence.is_file(), "OFL fonty sa nesmú šíriť bez licencie"
    text = read(licence)
    for family in ("Anton", "IBM Plex", "Manrope"):
        assert family in text
    assert "SIL Open Font License" in text


# ------------------------------------------------------- service worker
def registered_worker(html):
    match = re.search(r"navigator\.serviceWorker\.register\(\s*['\"]([^'\"]+)['\"]", html)
    assert match, "appka musí registrovať service worker"
    return match.group(1)


def test_service_worker_scope_actually_covers_the_app():
    """Worker z /static/sw.js má scope /static/ — /app teda nikdy neovláda."""
    url = registered_worker(read(APP))
    scope = posixpath.dirname(url).rstrip("/") + "/"

    assert "/app".startswith(scope), (
        f"worker z {url} má scope {scope} a na /app nedosiahne"
    )
    assert repo_path_for_url(url).is_file(), (
        f"{url} nie je v repozitári — registrácia by na serveri spadla na 404"
    )


def test_the_old_static_scoped_worker_is_unregistered_on_upgrade():
    html = read(APP)
    assert "getRegistrations" in html, (
        "starý worker z /static/sw.js ostane zaregistrovaný navždy, kým ho niekto nezruší"
    )
    assert "/static/sw.js" in html, "odregistrovať treba menovite starú cestu"


def worker_source():
    return read(repo_path_for_url(registered_worker(read(APP))))


def test_service_worker_never_answers_an_api_call_from_cache():
    """Ceny z letákov sa nesmú nikdy podávať zo zásoby — to je celý produkt."""
    source = worker_source()
    fetch_handler = source.split("addEventListener('fetch'", 1)[1]
    guard = fetch_handler.split("respondWith", 1)[0]

    assert "/api/" in guard, "API musí vypadnúť z cache ešte pred respondWith"
    assert "prihlasenie" in guard, "jednorazový prihlasovací odkaz sa nesmie cachovať"


def test_service_worker_bypasses_public_seo_and_crawler_paths():
    source = worker_source()
    fetch_handler = source.split("addEventListener('fetch'", 1)[1]
    guard = fetch_handler.split("respondWith", 1)[0]

    for cesta in ("/co-varit-tento-tyzden", "/robots.txt", "/sitemap.xml"):
        assert cesta in guard, (
            f"{cesta} musí vypadnúť z workera pred respondWith, inak môže ostať starý týždeň"
        )


def test_service_worker_precaches_the_shell_and_the_fonts():
    source = worker_source()
    assert "'/app'" in source, "bez /app v cache nemá PWA offline škrupinu"

    app_fonts = {
        url.strip("'\"")
        for _, _, sources, _ in font_faces(read(APP))
        for url in sources
    }
    assert app_fonts
    for url in app_fonts:
        assert url in source, f"{url} chýba v predvyplnenej cache workera"


def test_service_worker_cache_name_changed_so_the_old_shell_is_dropped():
    source = worker_source()
    assert "uvarsi-v1" not in source, "stará cache by prežila a servírovala starú škrupinu"
    assert "uvarsi-v2" not in source, "po zmene SEO cache politiky treba aktivovať novú cache"
    assert re.search(r"CACHE\s*=\s*'uvarsi-v\d+'", source)


def test_service_worker_skips_urls_with_a_query_string():
    """count.json?v=<čas> je zakaždým iná adresa — cache by rástla donekonečna."""
    source = worker_source()
    fetch_handler = source.split("addEventListener('fetch'", 1)[1]
    assert "url.search" in fetch_handler.split("respondWith", 1)[0]


SW_HARNESS = """
// Minimálny ServiceWorkerGlobalScope: cache v pamäti a sieť, ktorú vieme vypnúť.
const store = new Map();
let networkUp = true;
const fetched = [];
function Res(body) { return {body, ok: true, clone() { return Res(body); }}; }
const cache = {
  put: async (req, res) => { store.set(String(req.url || req), res); },
  add: async (url) => { if (!networkUp) throw new Error('offline'); store.set(url, Res('net:' + url)); },
  match: async (req) => store.get(String(req.url || req))
};
const caches = {
  open: async () => cache,
  match: async (req) => store.get(String(req.url || req)),
  keys: async () => ['uvarsi-v1'],
  delete: async () => true
};
async function fetch(req) {
  const url = String(req.url || req);
  fetched.push(url);
  if (!networkUp) throw new Error('offline');
  return Res('net:' + url);
}
const handlers = {};
const self = {
  location: {origin: 'https://uvar.si'},
  addEventListener: (type, fn) => { handlers[type] = fn; },
  skipWaiting: async () => {},
  clients: {claim: async () => {}},
  registration: {unregister: async () => {}}
};
async function dispatch(type, extra) {
  let waited = null, answered = null;
  const event = Object.assign({
    waitUntil: p => { waited = p; },
    respondWith: p => { answered = p; }
  }, extra);
  await handlers[type](event);
  if (waited) await waited;
  return answered === null ? null : await answered;
}
const request = (url) => ({url: 'https://uvar.si' + url, method: 'GET'});
"""


@needs_node
def test_service_worker_behaviour_offline_shell_and_always_fresh_prices(tmp_path):
    """Naozaj spustený worker: škrupina offline, ceny nikdy zo zásoby."""
    script = tmp_path / "sw-behaviour.js"
    script.write_text(
        SW_HARNESS
        + "\n(async () => {\n"
        + worker_source()
        + """
  await dispatch('install');
  if (!store.has('/app')) process.exit(1);                       // škrupina v cache
  if (!store.has('/static/manifest.json')) process.exit(2);

  // 1. API sa workera vôbec netýka — prehliadač ide rovno na sieť.
  if (await dispatch('fetch', {request: request('/api/plan')}) !== null) process.exit(3);
  if (await dispatch('fetch', {request: request('/api/me')}) !== null) process.exit(4);
  // 2. Jednorazový prihlasovací odkaz a adresy s ?query tiež nie.
  if (await dispatch('fetch', {request: request('/prihlasenie')}) !== null) process.exit(5);
  if (await dispatch('fetch', {request: {url: 'https://uvar.si/count.json?v=9', method: 'GET'}}) !== null) process.exit(6);
  if (await dispatch('fetch', {request: request('/co-varit-tento-tyzden')}) !== null) process.exit(14);
  if (await dispatch('fetch', {request: request('/robots.txt')}) !== null) process.exit(15);
  if (await dispatch('fetch', {request: request('/sitemap.xml')}) !== null) process.exit(16);
  // 3. POST sa nikdy nezachytáva.
  if (await dispatch('fetch', {request: {url: 'https://uvar.si/app', method: 'POST'}}) !== null) process.exit(7);
  // 4. Cudzí pôvod (napr. MailerLite) necháme tak.
  if (await dispatch('fetch', {request: {url: 'https://assets.mailerlite.com/x', method: 'GET'}}) !== null) process.exit(8);

  // 5. Offline: /app musí prísť z cache, inak PWA nemá škrupinu.
  store.set('https://uvar.si/app', Res('shell'));
  store.set('https://uvar.si/static/fonts/f.woff2', Res('font'));
  networkUp = false;
  const shell = await dispatch('fetch', {request: request('/app')});
  if (!shell || shell.body !== 'shell') process.exit(9);
  // 6. Font s odtlačkom v názve je nemenný — z cache a bez siete.
  fetched.length = 0;
  const font = await dispatch('fetch', {request: request('/static/fonts/f.woff2')});
  if (!font || font.body !== 'font') process.exit(10);
  if (fetched.length !== 0) process.exit(11);

  // 7. Online: škrupina sa ticho obnoví na pozadí.
  networkUp = true;
  fetched.length = 0;
  const again = await dispatch('fetch', {request: request('/app')});
  if (!again || again.body !== 'shell') process.exit(12);        // hneď zo zásoby
  await new Promise(r => setTimeout(r, 20));
  if (fetched.indexOf('https://uvar.si/app') === -1) process.exit(13);
  process.exit(0);
})().catch(e => { console.error(e); process.exit(99); });
""",
        encoding="utf-8",
    )
    result = subprocess.run([NODE, str(script)], capture_output=True, text=True)

    assert result.returncode == 0, f"exit {result.returncode}\n{result.stdout}{result.stderr}"


# ------------------------------------------------------------- štart appky
@needs_node
def test_profile_and_plan_are_requested_at_the_same_time_not_one_after_the_other(tmp_path):
    """Dve volania za sebou = dve obrátky k serveru, kým je obrazovka prázdna."""
    html = read(APP)
    match = re.search(r"function startupRequests\(request\) \{.*?\n\}", html, re.S)
    assert match, "štart musí mať jedno miesto, kde sa spúšťajú obe požiadavky"

    script = tmp_path / "startup-parallel.js"
    script.write_text(
        match.group(0)
        + """
var calls = [], resolvers = [];
function request(url) {
  calls.push(url);
  return new Promise(function (resolve) { resolvers.push(resolve); });
}
var started = startupRequests(request);
// Obe volania musia byť vonku EŠTE PREDTÝM, než ktorékoľvek doletí naspäť.
if (calls.length !== 2) process.exit(1);
if (calls.indexOf('/api/me') === -1) process.exit(2);
if (calls.indexOf('/api/plan') === -1) process.exit(3);
if (!started.me || !started.plan) process.exit(4);
if (typeof started.me.then !== 'function') process.exit(5);
process.exit(0);
""",
        encoding="utf-8",
    )
    result = subprocess.run([NODE, str(script)], capture_output=True, text=True)

    assert result.returncode == 0, result.stdout + result.stderr


def test_the_prefetched_plan_is_used_instead_of_a_second_round_trip():
    html = read(APP)
    match = re.search(r"async function nacitajPlan\(gen\) \{.*?\n\}", html, re.S)
    assert match
    load_plan = match.group(0)

    assert "PlanPrefetch" in load_plan or "PLAN_PREFETCH" in load_plan, (
        "načítanie plánu musí siahnuť po už bežiacej požiadavke"
    )
    take = re.search(r"function takePlanPrefetch\(\) \{.*?\n\}", html, re.S)
    assert take, "predbežnú požiadavku možno použiť len raz — Response sa nedá čítať dvakrát"
    assert "= null" in take.group(0)


def test_every_plan_generation_path_uses_one_shared_in_flight_guard():
    html = read(APP)
    assert "let PLAN_REQUEST_IN_FLIGHT = null" in html
    assert "function onePlanRequest(request)" in html
    assert "function requestPlan(kind, url)" in html

    for name in ("generujPlan", "preskladajPlan", "planZoSpajze"):
        fn = re.search(rf"function {name}\(\) \{{.*?\n\}}", html, re.S)
        assert fn, f"chýba {name}()"
        assert "requestPlan" in fn.group(0), f"{name} obchádza spoločný guard"


def test_a_202_acknowledgement_is_reused_after_the_first_request_finishes():
    html = read(APP)
    request = re.search(r"function requestPlan\(kind, url\) \{.*?\n\}", html, re.S)
    assert request, "generovanie potrebuje stavový guard aj po HTTP 202"
    assert "PLAN_PREPARATION" in request.group(0)
    assert "Promise.resolve" in request.group(0)
    assert "method:'POST'" in request.group(0)


@needs_node
def test_pending_acknowledgement_blocks_a_second_post_and_discards_stale_response(tmp_path):
    html = read(APP)
    one = re.search(r"function onePlanRequest\(request\) \{.*?\n\}", html, re.S)
    request = re.search(r"function requestPlan\(kind, url\) \{.*?\n\}", html, re.S)
    assert one and request
    script = tmp_path / "pending-plan-guard.js"
    script.write_text(
        "var PLAN_REQUEST_IN_FLIGHT=null, PLAN_PREPARATION=null, PLAN_CONTEXT_VERSION=0;\n"
        "var calls=0, resolveApi;\n"
        "function api(url, options) { calls++; return new Promise(function(resolve){resolveApi=resolve;}); }\n"
        "function setPlanPreparation(response, kind, version) { PLAN_PREPARATION={response:response,jobId:response.job_id,kind:kind,version:version}; return true; }\n"
        + one.group(0) + "\n" + request.group(0) + "\n"
        + "(async function(){\n"
        + "  var first=requestPlan('regular','/api/plan/generuj',{method:'POST'});\n"
        + "  resolveApi({status:'preparing',job_id:7,message:'Plán pripravujeme. Pokojne pokračuj inde.'});\n"
        + "  await first;\n"
        + "  await requestPlan('regular','/api/plan/generuj',{method:'POST'});\n"
        + "  if (calls !== 1) process.exit(1);\n"
        + "  PLAN_PREPARATION=null; PLAN_CONTEXT_VERSION=1;\n"
        + "  var stale=await requestPlan('regular','/api/plan/generuj',{method:'POST'});\n"
        + "  PLAN_CONTEXT_VERSION=2;\n"
        + "  resolveApi({status:'preparing',job_id:8,message:'Plán pripravujeme. Pokojne pokračuj inde.'});\n"
        + "  stale=await stale;\n"
        + "  if (!stale.stale || stale.plan_version !== 1) process.exit(2);\n"
        + "  if (PLAN_PREPARATION !== null) process.exit(3);\n"
        + "  process.exit(0);\n"
        + "})().catch(function(error){console.error(error);process.exit(99);});\n",
        encoding="utf-8",
    )
    result = subprocess.run([NODE, str(script)], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr


@needs_node
def test_plan_generation_requests_share_one_in_flight_promise(tmp_path):
    """Štart a rýchle dvojkliknutie nesmú spustiť dva platené modelové behy."""
    html = read(APP)
    guard = re.search(r"function onePlanRequest\(request\) \{.*?\n\}", html, re.S)
    assert guard, "všetky generatívne cesty potrebujú jeden spoločný in-flight guard"

    script = tmp_path / "one-plan-request.js"
    script.write_text(
        "var PLAN_REQUEST_IN_FLIGHT = null;\n"
        + guard.group(0)
        + """
var calls = 0, finish;
function request() {
  calls++;
  return new Promise(function(resolve) { finish = resolve; });
}
(async function() {
  var first = onePlanRequest(request);
  var second = onePlanRequest(request);
  if (calls !== 1 || first !== second) process.exit(1);
  finish('ok');
  if (await first !== 'ok') process.exit(2);
  await new Promise(function(resolve) { setTimeout(resolve, 0); });
  var third = onePlanRequest(request);
  if (calls !== 2 || third === first) process.exit(3);
  finish('again');
  await third;
  process.exit(0);
})().catch(function(error) { console.error(error); process.exit(99); });
""",
        encoding="utf-8",
    )
    result = subprocess.run([NODE, str(script)], capture_output=True, text=True)

    assert result.returncode == 0, result.stdout + result.stderr


@needs_node
def test_a_known_returning_user_sees_the_shell_before_the_network_answers(tmp_path):
    """Meno v hlavičke a spodné menu vieme nakresliť z pamäte, bez čakania."""
    html = read(APP)
    key = re.search(r"const PROFILE_KEY = '[^']+';", html)
    assert key, "kľúč v localStorage musí byť pomenovaný na jednom mieste"
    pieces = [key.group(0)]
    for signature in (r"function cachedProfile\(\) \{",
                      r"function rememberProfile\(me\) \{",
                      r"function paintKnownShell\(\) \{"):
        match = re.search(signature + r".*?\n\}", html, re.S)
        assert match, "chýba " + signature
        pieces.append(match.group(0))

    script = tmp_path / "instant-shell.js"
    script.write_text(
        """
var store = {};
var localStorage = {
  getItem: function (k) { return Object.prototype.hasOwnProperty.call(store, k) ? store[k] : null; },
  setItem: function (k, v) { store[k] = String(v); },
  removeItem: function (k) { delete store[k]; }
};
var header = {textContent: ''};
var navVisible = false;
var navigationReady = null;
function $(s) { if (s === '#hdr') return header; throw new Error(s); }
function showNav() { navVisible = true; }
function setNavigationReady(ready) { navigationReady = !!ready; }
"""
        + "\n".join(pieces)
        + """
if (paintKnownShell() !== false) process.exit(1);      // neznámy človek: nič nekreslíme
rememberProfile({email: 'jano@uvar.si', osoby: 4, frekvencia: 2,
                 obchody: ['Lidl'], onboarding: true, spajza: ['ryza']});
if (paintKnownShell() !== true) process.exit(2);
if (header.textContent !== 'jano') process.exit(3);
if (!navVisible) process.exit(4);
if (navigationReady !== false) process.exit(7);
var saved = JSON.stringify(store);
// Ceny sa neukladajú NIKDY — inak by sme raz ukázali neplatnú akciu.
if (saved.indexOf('cena') !== -1 || saved.indexOf('nakupny_zoznam') !== -1) process.exit(5);
if (saved.indexOf('spajza') !== -1) process.exit(6);
process.exit(0);
""",
        encoding="utf-8",
    )
    result = subprocess.run([NODE, str(script)], capture_output=True, text=True)

    assert result.returncode == 0, result.stdout + result.stderr


@needs_node
def test_retrying_after_an_outage_issues_brand_new_requests(tmp_path):
    """Zamietnutý sľub ostane zamietnutý — „Skúsiť znova" musí začať odznova."""
    html = read(APP)
    pieces = []
    for signature in (r"function startupRequests\(request\) \{",
                      r"function beginStartup\(\) \{"):
        match = re.search(signature + r".*?\n\}", html, re.S)
        assert match, "chýba " + signature
        pieces.append(match.group(0))

    offline = re.search(r"function viewOffline\(message\) \{.*?\n\}", html, re.S)
    assert offline
    assert "beginStartup()" in offline.group(0), (
        "tlačidlo Skúsiť znova by inak navždy čakalo na už zlyhanú požiadavku"
    )

    script = tmp_path / "startup-retry.js"
    script.write_text(
        "var STARTUP = null, PLAN_PREFETCH = null;\nvar attempts = 0;\n"
        "function fetch(url) { attempts++; return Promise.reject(new Error('offline')); }\n"
        + "\n".join(pieces)
        + """
var first = beginStartup();
if (attempts !== 2) process.exit(1);
var second = beginStartup();
if (attempts !== 4) process.exit(2);          // nové požiadavky, nie tie staré
if (first.me === second.me) process.exit(3);
if (PLAN_PREFETCH !== second.plan) process.exit(4);
process.exit(0);
""",
        encoding="utf-8",
    )
    result = subprocess.run([NODE, str(script)], capture_output=True, text=True)

    assert result.returncode == 0, result.stdout + result.stderr


def test_the_remembered_profile_is_forgotten_when_the_session_ends():
    html = read(APP)
    login = re.search(r"function viewLogin\(sent, previousEmail\) \{.*?\n\}", html, re.S)
    assert login
    assert "forgetProfile()" in login.group(0), (
        "po odhlásení nesmie v hlavičke ostať cudzie meno"
    )


# ------------------------------------------------------------------ payload
@pytest.mark.parametrize("page", PAGES, ids=lambda p: p.name)
def test_no_page_blocks_first_paint_on_an_external_subresource(page):
    """V <head> nesmie byť nič, čo drží prvé vykreslenie a beží po cudzej sieti."""
    head = head_of(read(page))
    for tag in re.findall(r"<(?:link|script)\b[^>]*>", head):
        blocking = "<script" in tag or "stylesheet" in tag or "preload" in tag
        remote = re.search(r"(?:href|src)=[\"'](?:https?:)?//", tag)
        assert not (blocking and remote), "render-blocking cudzí zdroj v <head>: " + tag
    assert "preconnect" not in head, (
        "preconnect na cudziu doménu je len obväz na chybu, ktorú riešime self-hostingom"
    )


def test_the_landing_ships_no_second_copy_of_the_receipt():
    """Bloček kreslí JavaScript z aktuálnych dát; zamrznutá kópia v HTML je mŕtvy balast."""
    html = read(LANDING)
    receipt = re.search(r"<!-- RCPT:START -->(.*?)<!-- RCPT:END -->", html, re.S)
    assert receipt, "značky musia ostať, tvoj nástroj recepty.py ich hľadá"

    frozen = receipt.group(1)
    assert "rcpt-logo" not in frozen, "statická kópia bločku sa nikdy nezobrazí"
    assert "2,89" not in frozen, "zamrznuté ceny z minulého týždňa nesmú ísť do sveta"
    assert 'id="landing-data" aria-live="polite" hidden' in html


@pytest.mark.parametrize(
    "page,budget",
    [(APP, 19_100), (LANDING, 12_400)],
    ids=lambda value: getattr(value, "name", str(value)),
)
def test_pages_stay_within_their_transfer_budget(page, budget):
    """Strop, aby stránky ticho nenarástli späť.

    Caddy posiela gzip, takže rozhoduje komprimovaná veľkosť, nie tá na disku.
    Merané 21. 8. 2026: app.html 13 064 B, index.html 11 161 B — stropy majú
    zopár percent rezervy na bežné úpravy textov, nie na ďalšiu vrstvu kódu.

    21. 8. 2026 pribudla zamknutá špajza s ukážkou (celá obrazovka navyše) a
    denný strop prepočtov: app.html 14 909 B. Strop appky sa raz zdvihol na
    15 400 B — vedome a s číslom, nie potichu. Ďalší rast musí opäť obhájiť
    vlastná obrazovka, nie pár riadkov navyše.

    24. 8. 2026: modelový príklad prestal byť zamrznutou fikciou v HTML a kreslí
    ho renderModel() z overených letákových dát. Odišlo 66 riadkov opakujúceho
    sa markupu (gzip ich stláčal takmer na nič), prišiel generátor — index.html
    11 161 → 12 183 B. Za tých ~1 kB sa kupuje to, že sekcia buď ukáže doložené
    čísla aktuálneho týždňa, alebo sa nevykreslí vôbec. Strop 12 400 B.

    24. 8. 2026: špajza sa oddelila od skladania plánu. Nákupný zoznam dostal
    celý nový režim — položky „máš doma", zhrnutie „zaplatíš X namiesto Y" a
    zrušenie zle spárovanej zhody, ktoré si pamätá localStorage; k tomu pribudla
    vyhradená cesta „navrhni jedlá z toho, čo mám doma" a recept konečne ukazuje
    dávky, porcie a pre koho sa varí. app.html 14 909 → 17 165 B. Strop appky sa
    druhýkrát dvíha vedome a s číslom: 17 400 B. Toto je posledný raz, čo sa
    zmestí do jedného kroku — ďalší rast už chce revíziu, nie zdvihnutie stropu.

    25. 8. 2026: profil domácnosti rozlíšil dospelých a deti, pridal prístupné
    počítadlá, validáciu, vysvetlenie detskej porcie a zloženie domácnosti aj
    počet dní pri recepte. app.html 17 310 → 18 273 B. Zdokumentovaný strop je
    18 500 B; pôvodné vysvetľujúce komentáre zostali zachované.

    28. 8. 2026: plán sa po HTTP 202 pripravuje na pozadí, navigácia zostáva
    aktívna a klient kontroluje iba GET stav. Strop sa zdvíha na 19 100 B.
    """
    compressed = len(gzip.compress(page.read_bytes(), 9))
    assert compressed <= budget, (
        f"{page} sa prenáša ako {compressed} B, strop je {budget} B"
    )
