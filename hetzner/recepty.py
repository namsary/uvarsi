#!/usr/bin/env python3
"""
Uvar.si — doplní sekciu 'Modelový príklad' (recepty) z UŽ hotového bločku.

NESCRAPUJE letáky. Prečíta jedlá + ceny z RCPT bloku v index.html a jedným lacným
TEXTOVÝM callom (bez obrázkov) vygeneruje k nim recepty. Rýchle (~5 s), pár centov,
žiadny rate-limit. Sekcia tak vždy sedí s aktuálnym bločkom.

Beh: ./venv/bin/python recepty.py /var/www/uvarsi/index.html
"""
import sys, os, re, json, html
from collections import Counter

MODEL = "claude-sonnet-5"
ENV_FILE = "/opt/uvarsi/uvarsi.env"


def load_key():
    if os.environ.get("ANTHROPIC_API_KEY"):
        return os.environ["ANTHROPIC_API_KEY"]
    try:
        for line in open(ENV_FILE, encoding="utf-8"):
            if line.strip().startswith("ANTHROPIC_API_KEY"):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    except FileNotFoundError:
        pass
    raise SystemExit("Chýba ANTHROPIC_API_KEY.")


def parse_blocek(page):
    m = re.search(r"<!-- RCPT:START.*?<!-- RCPT:END -->", page, re.S)
    if not m:
        raise SystemExit("RCPT blok nenájdený.")
    blk = m.group(0)
    meals, cur = [], None
    for line in blk.splitlines():
        d = re.search(r'class="day"><b>([^<]+)</b><span>([^<]+)</span>', line)
        if d:
            cur = {"day": d.group(1).strip(),
                   "name": html.unescape(d.group(2)).strip(), "items": []}
            meals.append(cur)
            continue
        it = re.search(r'class="item"><span>(.+?)\s*·\s*(.+?)</span>'
                       r'(?:.*?<b class="price">(.+?)</b>)?'
                       r'<span class="off">(.+?)</span>', line)
        if it and cur is not None:
            cur["items"].append({
                "name": html.unescape(it.group(1)).strip(),
                "store": html.unescape(it.group(2)).strip(),
                "price": (it.group(3) or "").replace("€", "").strip(),
                "off": html.unescape(it.group(4)).strip()})
    meals = [x for x in meals if x["items"]]
    us = re.search(r'class="save"><span>Ušetríš</span><span>(.+?)\s*€', blk)
    usetris = us.group(1).strip() if us else "0,00"
    return meals, usetris


def gen_recipes(meals, key):
    import anthropic
    client = anthropic.Anthropic(api_key=key, timeout=60.0, max_retries=1)
    lst = "\n".join(
        f'{m["day"]}: {m["name"]} — suroviny: '
        + ", ".join(i["name"] for i in m["items"]) for m in meals)
    prompt = (
        "Pre každé z týchto slovenských jedál napíš jednoduchý recept. "
        "Vráť IBA čistý JSON, kľúč = deň (PO/ST/PI), hodnota = "
        '{"min":45,"steps_total":6,"steps":["Krok 1.","Krok 2.","Krok 3."]}. '
        "min = čas v minútach (číslo), steps_total = celkový počet krokov, "
        "steps = prvé 3 kroky, krátke vety v rozkazovacom spôsobe, slovenčina s "
        "diakritikou. Jedlá:\n" + lst)
    msg = client.messages.create(model=MODEL, max_tokens=4000,
                                 messages=[{"role": "user", "content": prompt}])
    txt = "".join(b.text for b in msg.content
                  if getattr(b, "type", None) == "text").strip()
    txt = re.sub(r"^```(?:json)?|```$", "", txt.strip(), flags=re.M).strip()
    return json.loads(txt)


def _kroky(n):
    return "krok" if n == 1 else "kroky" if 2 <= n <= 4 else "krokov"


def render_example(meals, usetris):
    esc = html.escape
    items = [it for m in meals for it in m["items"]]
    stores = []
    for it in items:
        if it["store"] not in stores:
            stores.append(it["store"])
    obchody = " · ".join(stores) or "Kaufland · Tesco"
    cnt = Counter(it["store"] for it in items)
    zoznam = " · ".join(f"{s} {cnt[s]}" for s in stores) or "—"
    deals = ""
    for it in items[:6]:
        pr = f'{esc(it["price"])} € ' if it.get("price") else ""
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
        u = float(usetris.replace(",", ".").replace(" ", "").strip())
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
        <div class="ex-save"><span>Ušetrí</span><b>{esc(usetris)} €</b></div>
      </div>
    </div>
    <div class="cost-band">{cost}</div>
    <span class="eyebrow">A tie 3 recepty? Napríklad takto:</span>
    <div class="rec-grid">
{recs}    </div>
    <!-- EX:END -->"""


def main():
    if len(sys.argv) < 2:
        raise SystemExit("Použitie: recepty.py /cesta/k/index.html")
    path = sys.argv[1]
    page = open(path, encoding="utf-8").read()
    meals, usetris = parse_blocek(page)
    if len(meals) < 1:
        raise SystemExit("V bločku som nenašiel jedlá.")
    print(f"[INFO] jedlá z bločku: {[m['name'] for m in meals]}", flush=True)
    recipes = gen_recipes(meals, load_key())
    for m in meals:
        m["recipe"] = recipes.get(m["day"], {})
    ex = render_example(meals, usetris)
    new = re.sub(r"<!-- EX:START -->.*?<!-- EX:END -->", lambda _: ex, page, flags=re.S)
    if new == page:
        raise SystemExit("Značky EX:START/END som v index.html nenašiel.")
    open(path, "w", encoding="utf-8").write(new)
    print(f"[OK] Recepty doplnené k {len(meals)} jedlám.", flush=True)


if __name__ == "__main__":
    main()
