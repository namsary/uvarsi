#!/usr/bin/env python3
"""
Uvar.si — doplní recepty k jedlám v už overených letákových dátach.

NESCRAPUJE letáky a NEPÍŠE do HTML. Jedlá aj ceny zostavuje refresh_blocek.py
z overených ponúk v databáze do /var/lib/uvarsi/landing_data.json; ten súbor je
jediná pravda a prehliadač si ho ťahá cez /api/public/landing. Tento nástroj
do neho jedným lacným TEXTOVÝM callom (bez obrázkov) dopíše k jedlám len
recepty — čas a kroky. Ceny, obchody, zdroje ani úsporu sa nedotkne.

Kedysi tu bol regex, ktorý jedlá a ceny lúskal z bloku RCPT v index.html a
prepísaným HTML nahrádzal sekciu „Modelový príklad". Odkedy bloček kreslí
JavaScript z JSONu, je ten blok prázdny a nástroj vždy skončil hláškou
„V bločku som nenašiel jedlá.". Bola to druhá cesta k tej istej pravde — a
horšia: zapísané HTML by po skončení týždňa ostalo na disku aj s cenami, ktoré
už neplatia. Preto je preč.

Je to NEPOVINNÁ nadstavba: keď nebeží, sekcia sa vykreslí z krokov, ktoré k
jedlám priložil už refresh_blocek.py (`instructions`). Recepty k nim pridajú
len čas varenia a kratší, čitateľnejší postup.

Beh (ručne, alebo cronom po dozorcovi — `30 6 * * *`):
  cd /opt/uvarsi && ./venv/bin/python recepty.py /var/lib/uvarsi/landing_data.json
"""
import json
import os
import re
import sys
from contextlib import closing
from datetime import date
from pathlib import Path

try:
    from app import naklady
    from app.landing_data import (
        load_landing_data,
        model_example_is_publishable,
        validate_landing_data,
        write_landing_data_atomic,
    )
except ImportError:  # spúšťané priamo z /opt/uvarsi
    import naklady
    from landing_data import (
        load_landing_data,
        model_example_is_publishable,
        validate_landing_data,
        write_landing_data_atomic,
    )

MODEL = "claude-sonnet-5"
ENV_FILE = "/opt/uvarsi/uvarsi.env"
DATABASE_PATH = "/opt/uvarsi/uvarsi.db"
LANDING_DATA_PATH = Path("/var/lib/uvarsi/landing_data.json")
# Na kartu sa vojdú tri kroky; zvyšok je dôvod otvoriť appku.
MAX_STEPS = 3
NEPUBLIKOVATELNE = ("Letákové dáta nie sú pre aktuálny týždeň alebo nedokladajú "
                    "úsporu overenou bežnou cenou — recepty nedopĺňam.")


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


def landing_data_input_path(arguments):
    """Nástroj má právo siahnuť na jediný súbor — na overený landing JSON."""
    if not arguments:
        return LANDING_DATA_PATH
    if len(arguments) == 1 and Path(arguments[0]) == LANDING_DATA_PATH:
        return LANDING_DATA_PATH
    raise SystemExit("Použitie: recepty.py /var/lib/uvarsi/landing_data.json")


def gen_recipes(meals, key):
    import anthropic
    lst = "\n".join(
        f'{m["day"]}: {m["name"]} — suroviny: '
        + ", ".join(i["name"] for i in m["items"]) for m in meals)
    prompt = (
        "Pre každé z týchto slovenských jedál napíš jednoduchý recept. "
        "Vráť IBA čistý JSON, kľúč = deň (PO/ST/PI), hodnota = "
        '{"min":45,"steps_total":6,"steps":["Krok 1.","Krok 2.","Krok 3."]}. '
        "min = čas v minútach (číslo), steps_total = celkový počet krokov, "
        "steps = prvé 3 kroky, krátke vety v rozkazovacom spôsobe, slovenčina s "
        "diakritikou. Ceny, obchody ani zľavy neuvádzaj. Jedlá:\n" + lst)
    # Aj lacné volanie ide cez strop: „pár centov“ krát rozbehnutá slučka je
    # presne tá aritmetika, ktorá minule vynulovala kredit.
    with closing(naklady.pripoj(os.environ.get("UVARSI_DB", DATABASE_PATH))) as ucty:
        client = naklady.strazeny_klient(
            ucty,
            anthropic.Anthropic(api_key=key, timeout=60.0, max_retries=1),
            "recepty",
        )
        msg = client.messages.create(model=MODEL, max_tokens=4000,
                                     messages=[{"role": "user", "content": prompt}])
    txt = "".join(b.text for b in msg.content
                  if getattr(b, "type", None) == "text").strip()
    txt = re.sub(r"^```(?:json)?|```$", "", txt.strip(), flags=re.M).strip()
    return json.loads(txt)


def _pocet(value):
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def normalized_recipe(raw):
    """Z odpovede modelu prijmi len to, čo vieme zobraziť — nič nedopĺňaj.

    Model dostáva iba názvy jedál a surovín. Keby aj tak poslal cenu či obchod,
    tu to spadne pod stôl: do JSONu prejdú výhradne minúty, kroky a ich počet.
    """
    if not isinstance(raw, dict):
        return None
    steps = [step.strip() for step in raw.get("steps") or []
             if isinstance(step, str) and step.strip()][:MAX_STEPS]
    if not steps:
        return None
    recipe = {"steps": steps, "steps_total": max(_pocet(raw.get("steps_total")) or 0, len(steps))}
    minutes = _pocet(raw.get("min"))
    if minutes is not None:
        recipe["min"] = minutes
    return recipe


def meals_without_recipe(payload):
    return [meal for meal in payload["receipt"]["meals"] if not meal.get("recipe")]


def add_recipes(payload, generate, today=None):
    """Doplň recepty do dát, ktoré už smú ísť von — inak sa nezaplatí nič.

    Recepty patria k modelovému príkladu. Keď ten nesmie byť zverejnený (starý
    týždeň, žiadna overená bežná cena), platené volanie by bolo za výsledok,
    ktorý nikto neuvidí.
    """
    if not model_example_is_publishable(payload, today):
        raise SystemExit(NEPUBLIKOVATELNE)
    chybajuce = meals_without_recipe(payload)
    if not chybajuce:
        return payload, 0
    recipes = generate(chybajuce) or {}
    added = 0
    for meal in chybajuce:
        recipe = normalized_recipe(recipes.get(meal["day"]) if isinstance(recipes, dict) else None)
        if recipe:
            meal["recipe"] = recipe
            added += 1
    # Zapisujeme do súboru, ktorý servíruje landing — nech ho validácia uvidí
    # skôr než návštevník.
    validate_landing_data(payload, today)
    return payload, added


def main(today=None):
    path = landing_data_input_path(sys.argv[1:])
    payload = load_landing_data(path)
    payload, added = add_recipes(
        payload, lambda meals: gen_recipes(meals, load_key()), today=today or date.today())
    if not added:
        print("[OK] Recepty už sedia — nič nedopĺňam.", flush=True)
        return
    write_landing_data_atomic(path, payload)
    print(f"[OK] Recepty doplnené k {added} jedlám.", flush=True)


if __name__ == "__main__":
    main()
