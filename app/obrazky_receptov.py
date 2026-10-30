"""Uvar.si: fotky k receptom z katalógu cez OpenAI Images API.

Každý recept dostane jednu fotku natrvalo. Scéna (nádoba, uhol, podklad)
sa odvodí z receptu a jeho id, takže opakované generovanie ukáže to isté
jedlo v tej istej scéne a sada nepôsobí ako 164 rovnakých záberov.

Na fotke smú byť len suroviny z receptu. Modely radi pridávajú syr a vňať,
preto ich prompt výslovne zakazuje, ak ich recept nemá.

Použitie na serveri (kľúč OPENAI_API_KEY je v /opt/uvarsi/uvarsi.env):
  python obrazky_receptov.py --modely                         # aké modely má účet
  python obrazky_receptov.py --model <model> --skuska         # 9 receptov
  python obrazky_receptov.py --model <model> --vsetky
  python obrazky_receptov.py --len classic_french_potatoes --znova
  python obrazky_receptov.py --prompty --skuska               # bez platenia
"""
import argparse
import base64
import glob
import hashlib
import html
import json
import os
import re
import sys
import urllib.error
import urllib.request

APP = os.path.dirname(os.path.abspath(__file__))
KATALOG = os.path.join(APP, "catalog")
VYSTUP = os.environ.get("UVARSI_OBRAZKY_DIR", "/opt/uvarsi/obrazky_receptov")
API = "https://api.openai.com/v1/images/generations"

# Suroviny, ktoré na fotke nevidno alebo ich netreba menovať.
NEVIDITELNE = {"oil", "water", "salt", "black_pepper", "sugar", "flour",
               "vinegar", "baking_powder", "stock_cube"}

# Ako sa jedlo podáva a čím sa naberá, podľa spôsobu varenia.
PODANIE = {
    "oven": ("straight from the oven in a well-used rectangular glass or enamel "
             "baking dish, one portion already taken out so the inside is visible",
             "a slotted metal kitchen turner (flat spatula with long narrow slots, not a cake server) rests in the gap"),
    "pot": ("in the cooking pot it was made in, lid set aside",
            "a ladle rests in the pot"),
    "one_pot": ("in the wide pot or deep pan it was cooked in",
                "a wooden spoon rests on the rim"),
    "soup": ("served in a deep soup plate, the pot partly visible behind it",
             "one ordinary soup spoon dipped in the soup"),
    "pan": ("served on an everyday plate, the frying pan with the rest of the "
            "food partly visible at the edge of the frame",
            "a single ordinary fork resting on the rim of the plate, as if someone just started eating"),
    "salad": ("in a simple serving bowl", "one ordinary fork stuck in the salad"),
}
UHLY = ["camera about 45 degrees, slightly off-center",
        "overhead shot from directly above",
        "low side angle close to the table, close-up of the food"]
PODKLADY = ["a worn wooden kitchen table", "a light laminate kitchen counter",
            "a wooden cutting board on the counter",
            "an old cotton tablecloth with a faint pattern"]
UTIERKY = ["a folded linen kitchen towel", "a blue and white checked tea towel",
           "no towel", "a crumpled paper napkin"]
DETAILY = ["a few crumbs", "a drip of sauce on the rim", "a little steam",
           "a smear of sauce on the side of the dish"]


def nacitaj_recepty():
    recepty = []
    for subor in sorted(glob.glob(os.path.join(KATALOG, "recipes", "0*.json"))):
        data = json.load(open(subor, encoding="utf-8"))
        for recept in data["recipes"] if isinstance(data, dict) else data:
            if recept.get("active", True):
                recepty.append(recept)
    return recepty


def nazvy_surovin():
    data = json.load(open(os.path.join(KATALOG, "ingredients.json"), encoding="utf-8"))
    return {s["id"]: s["name"] for s in data["ingredients"]}


def _vyber(zoznam, recept_id, sol):
    h = hashlib.sha256(f"{recept_id}:{sol}".encode()).digest()
    return zoznam[h[0] % len(zoznam)]


def prompt_pre(recept, nazvy):
    viditelne = []
    for slot in recept["slots"]:
        moznosti = [c for c in slot["candidates"] if c not in NEVIDITELNE]
        if not moznosti:
            continue
        # Fotka ukazuje prvú možnosť; ostatné sú náhrady podľa akcií.
        surovina = nazvy.get(moznosti[0], moznosti[0].replace("_", " "))
        rez = f" ({slot['cut']})" if slot.get("cut") else ""
        viditelne.append(surovina + rez)
    nadoba, nacinie = PODANIE.get(recept["method"], PODANIE["pan"])
    nazov_jedla = recept["name_template"].lower()
    if recept["method"] in ("pot", "one_pot") and any(
            slovo in nazov_jedla for slovo in ("špagety", "cestovin", "penne", "rezanc")):
        # Cestoviny sa z hrnca naberačkou nedvíhajú, podávajú sa na tanieri.
        nadoba, nacinie = PODANIE["pan"][0].replace("frying pan", "pot"), PODANIE["pan"][1]
    if any(slovo in nazov_jedla for slovo in ("prívarok", "halušk")):
        # Prívarky a halušky sa nejedia z hrnca, ale z hlbokého taniera.
        nadoba = ("served in a deep plate, the pot with the rest partly visible behind it; "
                  "any boiled egg lies halved or sliced on top of the food on the plate, "
                  "never floating in the pot")
        nacinie = "a single ordinary tablespoon resting in the food"
    if nacinie == PODANIE["pan"][1]:
        # Pri tanieri sa príbor strieda, aby fotky neboli všetky rovnaké.
        nacinie = _vyber([nacinie, "an ordinary fork and table knife lying side by side "
                          "on the table next to the plate"], recept["id"], "pribor")
    if any(slovo in nazov_jedla for slovo in ("halušk", "strapačk")):
        # Halušky a strapačky sa na Slovensku jedia lyžicou.
        nacinie = "a single ordinary tablespoon resting in the food on the plate"
    postup = " ".join(krok["text"] for krok in recept.get("instructions", []))
    for slot in recept["slots"]:
        nazov = nazvy.get(slot["candidates"][0], slot["candidates"][0])
        postup = (postup.replace("{%s.amount} " % slot["key"], "")
                  .replace("{%s.cut}" % slot["key"], slot.get("cut") or "")
                  .replace("{%s.name}" % slot["key"], nazov)
                  .replace("{%s.reference_name}" % slot["key"], nazov))
    postup = re.sub(r"\s*\{[^}]*\}", "", postup)
    zakazane = []
    vsetko = " ".join(json.dumps(recept["slots"]).split())
    if "cheese" not in vsetko and "mozzarella" not in vsetko and "feta" not in vsetko:
        zakazane.append("no cheese")
    if not any(b in vsetko for b in ("parsley", "herb", "basil", "chives", "dill")):
        zakazane.append("no herbs or green garnish")
    return (
        f'A real home-cooked photo of the Slovak dish "{recept["name_template"]}", '
        "taken by a home cook with a good phone in their own kitchen, not a food stylist.\n"
        f"The food is {nadoba}; {nacinie}. Honest home portion.\n"
        f"Visible ingredients (Slovak names): {', '.join(viditelne)}. "
        "Only these may appear in the food, plus salt and black pepper. "
        f"How it was assembled and finished (Slovak recipe steps, follow them so "
        f"the layers and toppings look right): {postup}\n"
        f"{', '.join(zakazane + ['no other vegetables or toppings'])}.\n"
        f"Scene: {_vyber(PODKLADY, recept['id'], 'podklad')}, "
        f"{_vyber(UTIERKY, recept['id'], 'utierka')}, "
        f"{_vyber(UHLY, recept['id'], 'uhol')}. Natural daylight from a side window, "
        f"soft shadows, {_vyber(DETAILY, recept['id'], 'detail')}.\n"
        "Cutlery and utensils must look like ordinary, slightly used everyday kitchen items, "
        "correctly shaped and naturally placed; fewer is better. "
        "Realistic natural colors. No props that are not part of the meal, no text, "
        "no logo, no people. Not glossy, not advertising, not a stock photo."
    )


def generuj(model, prompt, kluc, kvalita):
    telo = json.dumps({"model": model, "prompt": prompt, "n": 1,
                       "size": "1536x1024", "quality": kvalita}).encode()
    poziadavka = urllib.request.Request(API, data=telo, headers={
        "Authorization": f"Bearer {kluc}", "Content-Type": "application/json"})
    with urllib.request.urlopen(poziadavka, timeout=300) as odpoved:
        data = json.load(odpoved)
    return base64.b64decode(data["data"][0]["b64_json"])


def prehlad(vybrane):
    riadky = []
    for recept in vybrane:
        for pripona in ("png", "webp", "jpg"):
            if os.path.exists(os.path.join(VYSTUP, f"{recept['id']}.{pripona}")):
                riadky.append(
                    f'<figure><img src="{recept["id"]}.{pripona}" loading="lazy">'
                    f'<figcaption>{html.escape(recept["name_template"])}<br>'
                    f'<code>{recept["id"]}</code></figcaption></figure>')
                break
    with open(os.path.join(VYSTUP, "index.html"), "w", encoding="utf-8") as f:
        f.write('<!doctype html><meta charset="utf-8"><title>Fotky receptov</title>'
                "<style>body{font:14px sans-serif;margin:16px;display:grid;"
                "grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:16px}"
                "img{width:100%;border-radius:6px}</style>" + "".join(riadky))


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("--model", default=os.environ.get("UVARSI_IMAGE_MODEL"))
    p.add_argument("--kvalita", default="high", choices=["low", "medium", "high"])
    p.add_argument("--skuska", action="store_true", help="10 receptov z rôznych skupín")
    p.add_argument("--vsetky", action="store_true")
    p.add_argument("--len", nargs="*", default=[], help="len tieto id receptov")
    p.add_argument("--znova", action="store_true", help="prepíš existujúce fotky")
    p.add_argument("--modely", action="store_true", help="vypíš obrazové modely účtu")
    p.add_argument("--prompty", action="store_true", help="iba vypíš prompty, nič neplať")
    a = p.parse_args(argv)

    if a.modely:
        poziadavka = urllib.request.Request(
            "https://api.openai.com/v1/models",
            headers={"Authorization": f"Bearer {os.environ.get('OPENAI_API_KEY', '')}"})
        with urllib.request.urlopen(poziadavka, timeout=60) as odpoved:
            for m in sorted(x["id"] for x in json.load(odpoved)["data"]):
                if "image" in m or "dall" in m:
                    print(m)
        return 0

    recepty = nacitaj_recepty()
    if a.len:
        vybrane = [r for r in recepty if r["id"] in set(a.len)]
    elif a.skuska:
        vybrane, videne = [], {}
        for r in recepty:
            strop = 1 if r["method"] in ("soup", "salad", "one_pot") else 2
            if videne.get(r["method"], 0) < strop:
                vybrane.append(r)
                videne[r["method"]] = videne.get(r["method"], 0) + 1
        vybrane = vybrane[:10]
    elif a.vsetky:
        vybrane = recepty
    else:
        p.error("zvoľ --skuska, --vsetky alebo --len")

    nazvy = nazvy_surovin()
    if a.prompty:
        for r in vybrane:
            print(f"### {r['id']}\n{prompt_pre(r, nazvy)}\n")
        return 0

    kluc = os.environ.get("OPENAI_API_KEY")
    if not kluc or not a.model:
        print("Chýba OPENAI_API_KEY alebo --model.", file=sys.stderr)
        return 2
    os.makedirs(VYSTUP, exist_ok=True)
    chyby = 0
    for i, r in enumerate(vybrane, 1):
        ciel = os.path.join(VYSTUP, f"{r['id']}.png")
        if os.path.exists(ciel) and not a.znova:
            print(f"[{i}/{len(vybrane)}] {r['id']}: už existuje")
            continue
        try:
            obrazok = generuj(a.model, prompt_pre(r, nazvy), kluc, a.kvalita)
        except urllib.error.HTTPError as e:
            chyby += 1
            telo = e.read()[:300]
            print(f"[{i}/{len(vybrane)}] {r['id']}: CHYBA {e.code} {telo!r}")
            if e.code in (401, 403, 404) or b"insufficient_quota" in telo:
                break  # zlý kľúč alebo model, ďalšie pokusy by len míňali čas
            continue
        with open(ciel, "wb") as f:
            f.write(obrazok)
        print(f"[{i}/{len(vybrane)}] {r['id']}: OK")
    prehlad(vybrane)
    print(f"HOTOVO, chýb: {chyby}. Prehľad: {os.path.join(VYSTUP, 'index.html')}")
    return 1 if chyby else 0


if __name__ == "__main__":
    sys.exit(main())
