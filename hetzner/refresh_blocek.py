#!/usr/bin/env python3
"""
Uvar.si — L1 auto-refresh bločku (účtenky). VERZIA 5 — VISION, 2-fázová, rýchla.

Obchody blokujú dátacentrum a letáky sú OBRÁZKOVÉ. Riešenie (robustné voči layoutu):
  FÁZA 1 (lacná): malé náhľady (320 px) VŠETKÝCH strán -> Haiku povie, ktoré strany
                  obsahujú potraviny v akcii (funguje aj pri rozhádzanom/vianočnom letáku).
  FÁZA 2 (presná): vzorka potravinových strán v plnom rozlíšení -> Sonnet prečíta ceny
                  a poskladá 3 večere (najlacnejší obchod / surovinu).
Zápis medzi <!-- RCPT:START/END --> v index.html. Chyba => NEPREPÍŠE (starý ostáva).

Rýchlosť/stabilita: náhľady posielame ako base64 (nie URL — to sa zasekávalo),
klientský timeout 120 s, priebežný výpis (spúšťaj s python -u).

Závislosti (venv): pip install requests anthropic pillow
Kľúč: /opt/uvarsi/uvarsi.env -> ANTHROPIC_API_KEY=sk-ant-...
Beh:  ./venv/bin/python -u refresh_blocek.py /var/www/uvarsi/index.html
Cron: 0 7 * * 4 cd /opt/uvarsi && ./venv/bin/python -u refresh_blocek.py /var/www/uvarsi/index.html >> /var/log/uvarsi.log 2>&1
"""
import sys, os, re, json, html, base64, datetime, requests
from io import BytesIO

# Opus 5 = najsilnejšia vision (dokumenty/letáky) → presné ceny = dôveryhodný bloček.
# Thinking je uňho ZAPNUTÝ by default a max_tokens je strop na thinking+odpoveď dokopy,
# preto veľká rezerva v READ_TOKENS (toto nám raz odseklo JSON).
MODEL_READ = "claude-opus-5"                   # presné čítanie cien + skladanie jedál
READ_EFFORT = "high"                           # low|medium|high|xhigh|max
READ_TOKENS = 16000
MODEL_SCAN = "claude-haiku-4-5-20251001"       # lacný sken náhľadov (len triedenie strán)
STORES = ["kaufland", "tesco", "lidl"]
MAX_PAGES = 45                                 # bezpečnostný strop
FOOD_CAP = 12                                  # koľko potravinových strán / obchod čítať
READ_PX = 1500                                 # rozlíšenie strán na čítanie
ENV_FILE = "/opt/uvarsi/uvarsi.env"
SKIP_SLUG = ("nova-predajna", "nová-predajna", "brozura", "brožúra",
             "back-to-school", "special", "špeciál", "shop", "nabytok", "zahrada")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
H = {"User-Agent": UA}


def log(*a):
    print(*a, flush=True)


def load_key():
    if os.environ.get("ANTHROPIC_API_KEY"):
        return os.environ["ANTHROPIC_API_KEY"]
    try:
        for line in open(ENV_FILE, encoding="utf-8"):
            if line.strip().startswith("ANTHROPIC_API_KEY"):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    except FileNotFoundError:
        pass
    raise SystemExit("Chýba ANTHROPIC_API_KEY (env alebo /opt/uvarsi/uvarsi.env).")


def leaflet_meta(store):
    base = "https://www.kupino.sk"
    idx = requests.get(f"{base}/letaky/{store}", headers=H, timeout=15).text
    cands = re.findall(r'href="(/letak/' + store + r'-letak[a-z0-9-]*)"', idx)
    cands += re.findall(r'href="(/letak/[a-z0-9-]*' + store + r'[a-z0-9-]*)"', idx)
    slug = next((c for c in cands if not any(b in c for b in SKIP_SLUG)), None)
    if not slug:
        return None
    pg = requests.get(f"{base}{slug}/strana-2", headers=H, timeout=15).text
    om = re.search(r'img\.kupino\.sk/letaky/(\d+)/thumbs/([a-z0-9-]+)-1_320\.jpg', pg)
    if not om:
        return None
    return om.group(1), om.group(2), slug


def thumb_url(lid, name, n):
    return f"https://img.kupino.sk/letaky/{lid}/thumbs/{name}-{n}_320.jpg"


def full_url(lid, name, n):
    return f"https://img.kupino.sk/letaky/{lid}/{name}-{n}.jpg"


def page_exists(url):
    """Existuje strana letáku? (bez nasledovania presmerovaní — tie znamenajú koniec)"""
    try:
        r = requests.get(url, headers=H, timeout=25, stream=True, allow_redirects=False)
        ok = (r.status_code == 200 and
              r.headers.get("content-type", "").startswith("image"))
        r.close()
        return ok
    except Exception:
        return False


def mletaky_base(store):
    """ZÁLOŽNÝ zdroj: mletaky.sk. Vráti prefix URL aktuálne platného letáku."""
    html_ = requests.get(f"https://mletaky.sk/obchody/{store}", headers=H, timeout=20).text
    cands = set(re.findall(r'https?://app\.mletaky\.sk/(\d{6})_(\d{6})_'
                           + store + r'_([a-z0-9]+)', html_))
    today = datetime.date.today()
    best = None
    for vto, vfrom, h in cands:
        try:
            d_from = datetime.datetime.strptime(vfrom, "%y%m%d").date()
        except ValueError:
            continue
        if vto.startswith("99"):                       # bez konca platnosti
            d_to = today
        else:
            try:
                d_to = datetime.datetime.strptime(vto, "%y%m%d").date()
            except ValueError:
                continue
        if d_from <= today <= max(d_to, today) and d_from <= today:
            if best is None or d_from > best[0]:
                best = (d_from, f"https://app.mletaky.sk/{vto}_{vfrom}_{store}_{h}")
    return best[1] if best else None


def store_pages(store):
    """Vráti (list[(thumb_url|None, full_url)], popis_zdroja) — kupino, inak mletaky."""
    # 1) primárny zdroj: kupino.sk (má lacné náhľady)
    try:
        meta = leaflet_meta(store)
    except Exception as e:
        log(f"[WARN] {store}: kupino index zlyhal ({e})")
        meta = None
    if meta:
        lid, name, slug = meta
        pages = []
        for n in range(1, MAX_PAGES + 1):
            t = thumb_url(lid, name, n)
            if page_exists(t):
                pages.append((t, full_url(lid, name, n)))
            else:
                break
        if pages:
            return pages, f"kupino{slug}"
    # 2) záložný zdroj: mletaky.sk (bez náhľadov — zmenšíme lokálne)
    try:
        base = mletaky_base(store)
    except Exception as e:
        log(f"[WARN] {store}: mletaky index zlyhal ({e})")
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


def get_b64(url, max_px=None, follow=True):
    """Stiahne obrázok, príp. zmenší, vráti base64 JPEG alebo None."""
    from PIL import Image
    r = requests.get(url, headers=H, timeout=15, allow_redirects=follow)
    if r.status_code != 200 or not r.headers.get("content-type", "").startswith("image"):
        return None
    im = Image.open(BytesIO(r.content)).convert("RGB")
    if max_px:
        w, h = im.size
        s = min(1.0, max_px / max(w, h))
        if s < 1.0:
            im = im.resize((int(w * s), int(h * s)), Image.LANCZOS)
    buf = BytesIO()
    im.save(buf, format="JPEG", quality=82)
    return base64.standard_b64encode(buf.getvalue()).decode()


def b64_block(b):
    return {"type": "image", "source": {
        "type": "base64", "media_type": "image/jpeg", "data": b}}


def claude_json(client, model, content, max_tokens, effort=None):
    kw = {}
    if effort:
        kw["output_config"] = {"effort": effort}
    try:
        msg = client.messages.create(model=model, max_tokens=max_tokens,
                                     messages=[{"role": "user", "content": content}], **kw)
    except TypeError:
        # staršie SDK bez output_config — pokračuj bez effortu
        msg = client.messages.create(model=model, max_tokens=max_tokens,
                                     messages=[{"role": "user", "content": content}])
    txt = "".join(b.text for b in msg.content
                  if getattr(b, "type", None) == "text").strip()
    txt = re.sub(r"^```(?:json)?|```$", "", txt.strip(), flags=re.M).strip()
    if getattr(msg, "stop_reason", None) == "max_tokens":
        raise ValueError("Odpoveď sa odsekla na limite tokenov (max_tokens).")
    return json.loads(txt)


def claude_json_retry(client, model, content, max_tokens, tries=2, effort=None):
    """Ak sa odpoveď oseká alebo nie je platný JSON, skús znova a žiadaj stručnejšie."""
    last = None
    for i in range(tries):
        try:
            body = content if i == 0 else content + [{
                "type": "text",
                "text": "DÔLEŽITÉ: odpovedz VÝRAZNE stručnejšie — kroky receptov maj "
                        "krátke (max 8 slov), aby sa JSON celý zmestil. Vráť iba JSON."}]
            return claude_json(client, model, body, max_tokens, effort=effort)
        except (json.JSONDecodeError, ValueError) as e:
            last = e
            log(f"[WARN] pokus {i+1}/{tries} zlyhal na JSON ({e}) — skúšam znova…")
    raise SystemExit(f"Model nevrátil platný JSON ani na {tries}. pokus: {last}")


SCAN_PROMPT = """Toto sú náhľady strán letáku. Pri každej je číslo. Vráť IBA JSON zoznam \
čísel strán, ktoré obsahujú POTRAVINY v akcii (mäso, hydina, ryby, zelenina, ovocie, \
mliečne, syry, vajcia, pečivo, ryža, cestoviny, múka, oleje, strukoviny, konzervy). \
Vynechaj drogériu, kozmetiku, textil, hračky, elektroniku, domácnosť, záhradu, nábytok. \
Formát: [1,2,5,6]"""

READ_PROMPT = """Si asistent slovenskej appky Uvar.si. Toto sú potravinové strany \
aktuálnych letákov (Kaufland, Tesco, Lidl). Úloha:
1) Vyber potraviny reálne v akcii (zľava % / akciová cena / 1+1) — bežné suroviny na varenie.
2) Poskladaj 3 realistické večere (PO, ST, PI). Každé jedlo = 2–3 suroviny, ktoré spolu \
dávajú zmysel. Pre každú surovinu vyber obchod, kde je najlacnejšia.
3) nakup_spolu = súčet akciových cien vybraných surovín; bezne = súčet ich bežných cien; \
usetris = bezne - nakup_spolu.
Vráť IBA čistý JSON:
{"meals":[{"day":"PO","name":"...","items":[{"name":"Surovina","store":"Kaufland","price":"2,15","off":"−52 %"}],
"recipe":{"min":45,"steps_total":6,"steps":["Krok 1.","Krok 2.","Krok 3."]}},
{"day":"ST","name":"...","items":[...],"recipe":{...}},{"day":"PI","name":"...","items":[...],"recipe":{...}}],
"nakup_spolu":"8,75","bezne":"17,13","usetris":"8,38"}
Pravidlá: store iba Kaufland/Tesco/Lidl; price = akciová cena položky (číslo s čiarkou, \
bez €), musí sedieť s cenou v letáku; off napr. "−52 %" alebo "1+1"; nakup_spolu = súčet \
price všetkých položiek; názvy krátke (max ~28 znakov). recipe = jednoduchý recept na to \
jedlo z vybraných surovín: min = čas v minútach (číslo), steps_total = celkový počet \
krokov (číslo), steps = prvé 3 kroky, každý krátka veta v rozkazovacom spôsobe. Slovenčina \
s diakritikou."""


def collect(client):
    """Vráti list (store, base64) potravinových strán, cez 2-fázový sken."""
    out = []
    for store in STORES:
        pages, src = store_pages(store)
        if not pages:
            log(f"[WARN] {store}: leták nenájdený (ani kupino, ani mletaky)")
            continue
        # 1) stiahni náhľady všetkých strán (malé, rýchle)
        thumbs = []
        for i, (turl, furl) in enumerate(pages, start=1):
            try:
                b = get_b64(turl or furl, max_px=320)
            except Exception:
                b = None
            if b:
                thumbs.append((i, b))
        log(f"[INFO] {store}: {len(thumbs)} strán ({src}), skenujem náhľady…")
        if not thumbs:
            continue
        # 2) Haiku vyberie potravinové strany
        content = []
        for n, b in thumbs:
            content.append({"type": "text", "text": f"Strana {n}:"})
            content.append(b64_block(b))
        content.append({"type": "text", "text": SCAN_PROMPT})
        try:
            nums = claude_json(client, MODEL_SCAN, content, 400)
            food = [n for n in nums if any(n == t[0] for t in thumbs)]
        except Exception as e:
            log(f"[WARN] {store}: sken zlyhal ({e}) — beriem každú stranu")
            food = [t[0] for t in thumbs]
        if len(food) > FOOD_CAP:                 # rovnomerná vzorka naprieč letákom
            step = len(food) / FOOD_CAP
            food = [food[int(i * step)] for i in range(FOOD_CAP)]
        log(f"[INFO] {store}: potravinové strany {food} — čítam v plnom rozlíšení…")
        got = 0
        for n in food:
            try:
                b = get_b64(pages[n - 1][1], max_px=READ_PX)
            except Exception:
                b = None
            if b:
                out.append((store, b))
                got += 1
        log(f"[INFO] {store}: načítaných {got} strán")
    log(f"[INFO] spolu potravinových strán: {len(out)}")
    return out


def compose(client, items):
    content, last = [], None
    for store, b in items:
        if store != last:
            content.append({"type": "text", "text": f"--- Leták obchodu: {store.upper()} ---"})
            last = store
        content.append(b64_block(b))
    content.append({"type": "text", "text": READ_PROMPT})
    return claude_json_retry(client, MODEL_READ, content, READ_TOKENS, effort=READ_EFFORT)


def week_range():
    t = datetime.date.today()
    mon = t - datetime.timedelta(days=t.weekday())
    sun = mon + datetime.timedelta(days=6)
    return f"{mon.day}.–{sun.day}. {sun.month}. {sun.year}"


def render(data):
    date_str = week_range()
    meals = {m["day"]: m for m in data["meals"]}
    rows = []

    def meal_rows(day):
        m = meals.get(day)
        if not m:
            return
        rows.append(f'        <div class="day"><b>{day}</b>'
                    f'<span>{html.escape(m["name"])}</span></div>')
        for it in m["items"]:
            price = it.get("price", "")
            pr = f'<b class="price">{html.escape(price)} €</b>' if price else ""
            rows.append(
                f'        <div class="item"><span>{html.escape(it["name"])} · '
                f'{html.escape(it["store"])}</span>'
                f'<span class="item-r">{pr}<span class="off">{html.escape(it["off"])}</span>'
                f'</span></div>')

    meal_rows("PO")
    rows.append('        <div class="day day--rest"><b>UT</b>'
                '<span>Zvyšok z pondelka</span></div>')
    meal_rows("ST")
    rows.append('        <div class="day day--rest"><b>ŠT</b>'
                '<span>Zvyšok zo stredy</span></div>')
    meal_rows("PI")
    body = "\n".join(rows)
    ns, bz, us = data["nakup_spolu"], data["bezne"], data["usetris"]
    return f"""<!-- RCPT:START (auto {datetime.date.today()}) -->
        <div class="rcpt-head">
          <div class="rcpt-logo">Uvar.si</div>
          <div class="rcpt-sub">Tvoj týždeň · varíš 3×</div>
          <div class="rcpt-sub">{date_str}</div>
        </div>
        <hr class="rule-solid">

{body}

        <hr class="rule">
        <div class="row"><span>Nákup spolu</span><span>{ns} €</span></div>
        <div class="row"><span>Bežne by stál</span><span>{bz} €</span></div>
        <div class="save"><span>Ušetríš</span><span>{us} €</span></div>
        <div class="rcpt-foot">Dobrú chuť</div>
        <!-- RCPT:END -->"""


def _kroky(n):
    if n == 1:
        return "krok"
    if 2 <= n <= 4:
        return "kroky"
    return "krokov"


def render_example(data):
    """Sekcia 'Modelový príklad' z tých istých reálnych dát ako bloček."""
    from collections import Counter
    esc = html.escape
    meals = data["meals"]
    items = [it for m in meals for it in m.get("items", [])]
    stores = []
    for it in items:
        if it["store"] not in stores:
            stores.append(it["store"])
    obchody = " · ".join(stores) or "Kaufland · Tesco"
    cnt = Counter(it["store"] for it in items)
    zoznam = " · ".join(f"{s} {cnt[s]}" for s in stores) or "—"
    deals = ""
    for it in items[:6]:
        price = it.get("price", "")
        pr = f'{esc(price)} € ' if price else ""
        deals += (f'        <div class="deal"><span>{esc(it["name"])} · '
                  f'{esc(it["store"])}</span><span class="d-off">{pr}'
                  f'<em>{esc(it["off"])}</em></span></div>\n')
    recs = ""
    for m in meals:
        r = m.get("recipe") or {}
        steps = (r.get("steps") or [])[:3]
        try:
            total = int(r.get("steps_total") or len(steps))
        except (ValueError, TypeError):
            total = len(steps)
        more = max(0, total - len(steps))
        steps_html = "".join(f"<li>{esc(s)}</li>" for s in steps)
        more_html = (f'<div class="rec-more">…+{more} {_kroky(more)} v appke</div>'
                     if more > 0 else "")
        recs += (f'      <div class="rec-card"><div class="rec-head">'
                 f'<h3>{esc(m["name"])}</h3><span class="rec-day">{esc(m["day"])}</span>'
                 f'</div><div class="rec-meta">{esc(str(r.get("min", "")))} min · '
                 f'4 porcie · {total} {_kroky(total)}</div>'
                 f'<ol class="rec-steps">{steps_html}</ol>{more_html}</div>\n')
    try:
        u = float(str(data["usetris"]).replace(",", ".").replace(" ", "").strip())
    except Exception:
        u = 0.0
    if u > 0:
        cost = (f'<span class="c-lbl">Ak takto varíš celý rok:</span> '
                f'<b>~{round(u * 52 / 12)} € mesačne</b> späť — za rok '
                f'<b>približne {int(u * 52 // 10 * 10)} €</b> ušetrené oproti plným cenám.')
    else:
        cost = ('<span class="c-lbl">Nakupovať v akciách sa oplatí:</span> '
                'na každom týždennom pláne ušetríš — za rok sa to poriadne nazbiera.')
    return f"""<!-- EX:START -->
    <div class="ex-grid">
      <div class="ex-card">
        <div class="ex-k">01 · Ty zadáš</div>
        <div class="ex-row"><span>varím</span><b>každé 2 dni</b></div>
        <div class="ex-row"><span>porcie</span><b>4</b></div>
        <div class="ex-row"><span>obchody</span><b>{obchody}</b></div>
        <div class="ex-row"><span>v špajze</span><b>ryža · vajcia · cibuľa</b></div>
        <div class="ex-note">Nastavíš raz, zmeníš kedykoľvek.</div>
      </div>
      <div class="ex-card">
        <div class="ex-k">02 · Appka nájde akcie</div>
{deals}        <div class="ex-note">Z aktuálnych letákov tvojich obchodov.</div>
      </div>
      <div class="ex-card">
        <div class="ex-k">03 · Ty dostaneš</div>
        <ul class="check">
          <li>jedálniček na celý týždeň</li>
          <li>3 recepty krok za krokom</li>
          <li>nákupný zoznam: {zoznam}</li>
          <li>zvyšky zarátané do ďalších dní</li>
        </ul>
        <div class="ex-save"><span>Ušetrí</span><b>{data["usetris"]} €</b></div>
      </div>
    </div>
    <div class="cost-band">{cost}</div>
    <span class="eyebrow">A tie 3 recepty? Napríklad takto:</span>
    <div class="rec-grid">
{recs}    </div>
    <!-- EX:END -->"""


def main():
    if len(sys.argv) < 2:
        raise SystemExit("Použitie: refresh_blocek.py /cesta/k/index.html")
    path = sys.argv[1]
    import anthropic
    client = anthropic.Anthropic(api_key=load_key(), timeout=120.0, max_retries=1)
    items = collect(client)
    if len(items) < 3:
        raise SystemExit("Málo potravinových strán — nechávam starý bloček.")
    log("[INFO] skladám jedlá (Sonnet)…")
    data = compose(client, items)
    if not data.get("meals") or len(data["meals"]) < 3:
        raise SystemExit("Vision nevrátil 3 jedlá — nechávam starý bloček.")
    block = render(data)
    ex = render_example(data)
    page = open(path, encoding="utf-8").read()
    new = re.sub(r"<!-- RCPT:START.*?<!-- RCPT:END -->", lambda m: block, page, flags=re.S)
    if new == page:
        raise SystemExit("Značky RCPT som v index.html nenašiel.")
    new = re.sub(r"<!-- EX:START -->.*?<!-- EX:END -->", lambda m: ex, new, flags=re.S)
    open(path, "w", encoding="utf-8").write(new)
    log(f"[OK] Bloček {week_range()}: nákup {data['nakup_spolu']} €, "
        f"ušetríš {data['usetris']} € (z {len(items)} potravinových strán).")


if __name__ == "__main__":
    main()
