"""Trusted reconstruction of a personal plan from content-only model output."""
import hashlib
import json
import re
from datetime import date
from decimal import Decimal, InvalidOperation

try:
    from .weekly_data import current_monday, current_verified_offers
except ImportError:
    from weekly_data import current_monday, current_verified_offers


CENT = Decimal("0.01")
DAY_ORDER = ("PO", "UT", "ST", "ŠT", "PI", "SO", "NE")
STORE_ORDER = ("Kaufland", "Lidl", "Tesco")
_MODEL_TOP_LEVEL = frozenset({"meals"})
_MODEL_MEAL = frozenset({"day", "name", "minutes", "instructions", "items", "pantry_ingredients"})
_MODEL_ITEM = frozenset({"offer_key", "quantity"})

# Recept je použiteľný až vtedy, keď povie koľko, ako dlho a na čom.
# „Pridaj cibuľu a opeč" je nič; „Na 2 lyžiciach oleja opeč 2 nakrájané
# cibule 5 minút do sklovita" sa dá vziať a uvariť podľa toho.
MIN_STEPS_PER_MEAL = 3
MIN_STEP_WORDS = 6
MIN_STEP_CHARS = 30
STAPLES = ("soľ", "korenie", "olej", "voda")
GOOD_STEP_EXAMPLE = "Na 2 lyžiciach oleja opeč 2 nakrájané cibule 5 minút do sklovita."
BAD_STEP_EXAMPLE = "Pridaj cibuľu a opeč."

_NUMBER = re.compile(r"\d")
_QUANTITY = re.compile(
    r"\d+(?:[.,]\d+)?\s*(?:kg|g|ml|dl|l|ks|cm|lyžic|lyžičk|hrst|hrsť|štipk|plátk|"
    r"strúčik|balen|konzerv|porci|vajc|kus|téglik|pohár|kock)",
    re.IGNORECASE,
)
_DURATION = re.compile(r"\d+\s*(?:-\s*\d+\s*)?(?:min|sek|hodin|hod\b|h\b)", re.IGNORECASE)
_COOKS_WITH_HEAT = re.compile(
    r"opeč|peč|smaž|vypráž|opraž|resto|restu|zohrej|ohrej|vari|uvar|dus|griluj|zapeč|blanš|prevar",
    re.IGNORECASE,
)
_HEAT_LEVEL = re.compile(r"°\s*c|stupň|ohni|oheň|plamen|plameň|predhri|predhrej|teplot|výkon", re.IGNORECASE)


def meal_count_for_frequency(frequency):
    return {1: 5, 2: 3, 3: 2}.get(frequency, 3)


def cooking_days_for_frequency(frequency):
    """Kalendár varenia odvodíme v Pythone; model si vyberá už len jedlá.

    „Raz za dva dni" musí vyjsť ako PO, ST, PI — nie tri dni po sebe a
    potom prázdny zvyšok týždňa.
    """
    count = meal_count_for_frequency(frequency)
    usable = isinstance(frequency, int) and not isinstance(frequency, bool) and frequency >= 1
    spacing = frequency if usable else 2
    if count > 1:
        spacing = min(spacing, (len(DAY_ORDER) - 1) // (count - 1))
    return tuple(DAY_ORDER[index * spacing] for index in range(count))


def _day_list(days):
    return days[0] if len(days) == 1 else f"{', '.join(days[:-1])} a {days[-1]}"


def _meals_word(count):
    return "jedlo" if count == 1 else "jedlá" if count < 5 else "jedál"


def _cookable_steps(instructions):
    """Odmietni recept, podľa ktorého sa v kuchyni nedá postupovať."""
    if not isinstance(instructions, list) or not instructions:
        raise ValueError("Chýbajú pokyny k jedlu.")
    steps = [_text(instruction, "pokyn") for instruction in instructions]
    for step in steps:
        if len(step) < MIN_STEP_CHARS or len(step.split()) < MIN_STEP_WORDS:
            raise ValueError("Pokyn je príliš všeobecný na to, aby sa podľa neho dalo variť.")
    if len(steps) < MIN_STEPS_PER_MEAL:
        raise ValueError(f"Recept musí mať aspoň {MIN_STEPS_PER_MEAL} kroky v poradí varenia.")
    recipe = " ".join(steps)
    if not _QUANTITY.search(recipe):
        raise ValueError("Recept neuvádza konkrétne množstvá surovín.")
    if not _DURATION.search(recipe):
        raise ValueError("Recept neuvádza čas prípravy pri krokoch.")
    if _COOKS_WITH_HEAT.search(recipe) and not _HEAT_LEVEL.search(recipe):
        raise ValueError("Recept neuvádza teplotu ani intenzitu ohrevu.")
    if sum(1 for step in steps if _NUMBER.search(step)) * 2 < len(steps):
        raise ValueError("Väčšina krokov musí uvádzať konkrétne množstvo, čas alebo teplotu.")
    return steps


def _validated_household_size(value):
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 12:
        raise ValueError("Počet osôb musí byť celé číslo od 1 do 12.")
    return value


def _text(value, field):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Chýba {field} v návrhu plánu.")
    return value.strip()


def _price(value, field):
    try:
        amount = Decimal(str(value).strip().replace(",", "."))
    except (InvalidOperation, ValueError) as error:
        raise ValueError(f"Neplatná {field} v overenej ponuke.") from error
    if not amount.is_finite() or amount <= 0 or amount != amount.quantize(CENT):
        raise ValueError(f"Neplatná {field} v overenej ponuke.")
    return amount


def _format(amount):
    return format(amount.quantize(CENT), "f").replace(".", ",")


def _reject_extra(mapping, allowed):
    if not isinstance(mapping, dict) or set(mapping) - allowed:
        raise ValueError("Návrh obsahuje nepovolené obchodné údaje.")


def _model_meals(model_output, offers_by_key, frequency, pantry):
    _reject_extra(model_output, _MODEL_TOP_LEVEL)
    meals = model_output.get("meals")
    cooking_days = cooking_days_for_frequency(frequency)
    if not isinstance(meals, list) or len(meals) != len(cooking_days):
        raise ValueError("Návrh nemá správny počet jedál.")

    pantry_by_name = {item.casefold(): item for item in pantry}
    seen_days = set()
    seen_offers = set()
    parsed = []
    for meal in meals:
        _reject_extra(meal, _MODEL_MEAL)
        day = _text(meal.get("day"), "deň")
        if day not in DAY_ORDER or day in seen_days:
            raise ValueError("Návrh obsahuje duplicitný alebo neplatný deň.")
        seen_days.add(day)
        name = _text(meal.get("name"), "názov jedla")
        minutes = meal.get("minutes")
        if isinstance(minutes, bool) or not isinstance(minutes, int) or minutes <= 0:
            raise ValueError("Počet minút musí byť kladné celé číslo.")
        steps = _cookable_steps(meal.get("instructions"))
        pantry_names = meal.get("pantry_ingredients", [])
        if not isinstance(pantry_names, list):
            raise ValueError("Neplatné suroviny zo špajze.")
        selected_pantry = []
        for ingredient in pantry_names:
            normalized = _text(ingredient, "surovinu zo špajze").casefold()
            if normalized not in pantry_by_name or normalized in selected_pantry:
                raise ValueError("Návrh obsahuje neznámu alebo duplicitnú surovinu zo špajze.")
            selected_pantry.append(normalized)
        items = meal.get("items")
        if not isinstance(items, list) or (not items and not selected_pantry):
            raise ValueError("Jedlo nemá vybrané ponuky ani suroviny zo špajze.")
        selected_items = []
        for item in items:
            _reject_extra(item, _MODEL_ITEM)
            offer_key = item.get("offer_key")
            quantity = item.get("quantity")
            if not isinstance(offer_key, str) or offer_key not in offers_by_key:
                raise ValueError("Návrh obsahuje neznáme alebo neaktuálne offer_key.")
            if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity <= 0:
                raise ValueError("Množstvo musí byť kladné celé číslo.")
            if offer_key in seen_offers:
                raise ValueError("Návrh obsahuje duplicitné offer_key.")
            seen_offers.add(offer_key)
            selected_items.append((offers_by_key[offer_key], quantity))
        parsed.append((day, name, minutes, steps, selected_items, [pantry_by_name[item] for item in selected_pantry]))
    if seen_days != set(cooking_days):
        raise ValueError(f"Návrh nedodržal dni varenia: {_day_list(cooking_days)}.")
    return sorted(parsed, key=lambda meal: DAY_ORDER.index(meal[0]))


# Rovnaký profil = rovnaký plán, takže ho stačí poskladať raz a podať všetkým.
# Aby však susedia s rovnakou domácnosťou nedostali bajt na bajt to isté menu,
# podpis sa delí na malú sadu variantov a každý z nich pýta iný smer kuchyne.
PLAN_VARIANT_HINTS = (
    "Drž sa domácej slovenskej klasiky: polievky, guláše, zemiakové a múčne jedlá.",
    "Stav na rýchle jedlá z panvice a pekáča: cestoviny, rizoto, zapekané misy.",
    "Uprednostni ľahšie a zeleninovejšie jedlá: strukoviny, wok, pečená zelenina.",
)


def plan_variant_for(user_id, variants):
    """Deterministicky rozdelí používateľov medzi varianty jedného podpisu."""
    if not isinstance(variants, int) or isinstance(variants, bool) or variants < 2:
        return 0
    return int(user_id) % variants


def plan_signature(week, stores, household_size, frequency, offer_keys, pantry=()):
    """Všetko, od čoho plán závisí, v jednom kľúči — a nič iné.

    Podpis je zároveň pravidlo neplatnosti: keď sa zmení týždeň, profil, špajza
    alebo ponuková sada, zmení sa kľúč a starý plán sa už nikdy netrafí. Preto
    sa do neho dávajú `offer_keys` — obsahujú overené fakty aj týždeň, takže
    nový leták či vypršaná ponuka zdieľaný plán automaticky odstavia.
    """
    facts = {
        "week": week,
        "stores": sorted({str(store) for store in stores}),
        "household_size": household_size,
        "frequency": frequency,
        "pantry": sorted({item.strip().casefold() for item in map(str, pantry) if item.strip()}),
        "offers": sorted({str(key) for key in offer_keys}),
    }
    canonical = json.dumps(facts, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def offers_catalog(rows):
    """Ponuky ako samostatný blok — pre celý týždeň a rovnaké obchody rovnaký.

    Je to zďaleka najväčšia časť promptu a jediná, ktorá sa medzi používateľmi
    nelíši. Preto stojí na začiatku správy a nesmie obsahovať nič osobné:
    len tak sa dá poslať s cache_control a čítať z cache namiesto prepočítania.
    """
    offers = "\n".join(
        f"- offer_key: {row['offer_key']}; názov: {row['nazov']}; kategória: {row['kategoria'] or 'iné'}"
        for row in rows
    )
    return f"""Overené ponuky z tohtotýždňových letákov. Vyberať smieš výhradne z nich
a odkazovať sa na ne výhradne cez offer_key.

{offers}"""


def personal_plan_messages(rows, frequency, pantry, household_size, variant=0):
    """Správa pre model: cachovaná predpona s ponukami + osobný zvyšok."""
    return [
        {
            "type": "text",
            "text": offers_catalog(rows),
            "cache_control": {"type": "ephemeral"},
        },
        {
            "type": "text",
            "text": personal_plan_prompt(rows, frequency, pantry, household_size, variant),
        },
    ]


def personal_plan_prompt(rows, frequency, pantry, household_size, variant=0):
    """Expose only food content and opaque offer references to the model."""
    household_size = _validated_household_size(household_size)
    pantry_text = ", ".join(pantry) or "nič"
    style = PLAN_VARIANT_HINTS[variant % len(PLAN_VARIANT_HINTS)] if PLAN_VARIANT_HINTS else ""
    days = cooking_days_for_frequency(frequency)
    days_text = _day_list(days)
    spacing = DAY_ORDER.index(days[1]) - DAY_ORDER.index(days[0]) if len(days) > 1 else 1
    leftovers = (
        f"- Varí sa raz za {spacing} dni: každé jedlo navar na {spacing} dni dopredu"
        f" (pre {household_size} osôb na deň), ďalšie dni sa jedia zvyšky."
        " Množstvá v krokoch uveď pre celú dávku.\n"
        if spacing > 1 else ""
    )
    return f"""Navrhni presne {len(days)} {_meals_word(len(days))} na dni {days_text}. Vráť iba JSON.

Povolený formát:
{{"meals":[{{"day":"PO","name":"...","minutes":30,"instructions":["..."],"items":[{{"offer_key":"offer_...","quantity":1}}],"pantry_ingredients":["..."]}}]}}

Pravidlá:
- Varí sa len v dňoch {days_text}. Presne tieto dni použi ako "day", každý práve raz, iný deň nepoužívaj.
{leftovers}- Jedlá sú pre {household_size} osôb; množstvá aj porcie prispôsob presne tejto domácnosti.
- minutes musí byť kladný celý počet minút prípravy.
- Recept musí byť naozaj uvarateľný: aspoň {MIN_STEPS_PER_MEAL} kroky v poradí, v akom sa varí.
- V každom kroku napíš konkrétne množstvá pre {household_size} osôb (g, ml, ks, lyžice), teplotu
  alebo intenzitu ohrevu (napr. rúra 180 °C, stredný oheň) a čas v minútach.
- Píš „{GOOD_STEP_EXAMPLE}", nikdy nie „{BAD_STEP_EXAMPLE}"
- Základné suroviny ({", ".join(STAPLES)}) používateľ doma má — pokojne ich v krokoch použi
  a vyčísli. Nikdy ich neuvádzaj v items ani ako ponuku s cenou či zľavou.
- Každá položka smie obsahovať iba offer_key a celé kladné quantity.
- Suroviny zo špajze uveď výlučne v pantry_ingredients a len z ponuky používateľa.
- Nesmieš uvádzať ani meniť obchod, názov položky, jednotku, cenu, bežnú cenu, úsporu, zdroj ani súčty.
- Každý offer_key a deň použi najviac raz. Pokyny musia byť neprázdne.
- Vyberaj výhradne z {len(rows)} overených ponúk uvedených vyššie.
- Smerovanie tohto jedálnička: {style}

Špajza používateľa: {pantry_text}"""


def build_personal_plan(con, model_output, stores, frequency, household_size, pantry=(), today=None):
    """Validate selection content and deterministically derive all purchasable data."""
    _validated_household_size(household_size)
    today = today or date.today()
    offers = current_verified_offers(con, stores, today)
    offers_by_key = {row["offer_key"]: row for row in offers}
    meals = _model_meals(model_output, offers_by_key, frequency, pantry)

    plan_meals = []
    purchases = []
    total = Decimal("0")
    regular = Decimal("0")
    for day, name, minutes, instructions, selected_items, pantry_names in meals:
        ingredients = []
        for row, quantity in selected_items:
            price = _price(row["cena"], "akciová cena") * quantity
            original = _price(row["povodna"], "bežná cena") * quantity if row["povodna"] is not None else None
            total += price
            regular += original if original is not None else price
            ingredient = {
                "offer_key": row["offer_key"], "nazov": row["nazov"], "obchod": row["obchod"],
                "jednotka": row["jednotka"], "mnozstvo": quantity, "cena": _format(price),
                "povodna": _format(original) if original is not None else None,
                "zlava": row["zlava"] or "",
                # Sľub „skontroluj si ich" platí len vtedy, keď zdroj ceny dôjde
                # až na obrazovku: ktorý leták, ktorá strana a dokedy cena platí.
                "source_url": row["source_url"], "source_page": row["source_page"],
                "valid_from": row["valid_from"], "valid_to": row["valid_to"],
            }
            ingredients.append(ingredient)
            purchases.append(ingredient)
        ingredients.extend({"spajza": item} for item in pantry_names)
        plan_meals.append({
            "den": day, "nazov": name, "recept": {"min": minutes, "kroky": instructions},
            "suroviny": ingredients,
        })

    grouped = {}
    for item in purchases:
        grouped.setdefault(item["obchod"], []).append({
            key: item[key] for key in ("offer_key", "nazov", "jednotka", "mnozstvo", "cena", "povodna", "zlava")
        })
    shopping = [
        {"obchod": store, "polozky": sorted(items, key=lambda item: (item["nazov"].casefold(), item["offer_key"]))}
        for store, items in sorted(grouped.items(), key=lambda pair: STORE_ORDER.index(pair[0]))
    ]
    return {
        "tyzden": current_monday(today), "jedla": plan_meals, "nakupny_zoznam": shopping,
        "nakup_spolu": _format(total), "bezne": _format(regular),
        "usetris": _format(max(Decimal("0"), regular - total)),
    }


def cached_offer_keys(plan):
    """Return the stable offer keys a reconstructed cached plan depends on."""
    try:
        keys = [ingredient["offer_key"] for meal in plan["jedla"] for ingredient in meal["suroviny"] if "offer_key" in ingredient]
    except (KeyError, TypeError):
        return None
    return set(keys) if keys and all(isinstance(item, str) and item for item in keys) else None


def cached_plan_is_current(plan, rows):
    selected = cached_offer_keys(plan)
    return selected is not None and selected.issubset({row["offer_key"] for row in rows})
