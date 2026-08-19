"""Trusted reconstruction of a personal plan from content-only model output."""
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
_MODEL_MEAL = frozenset({"day", "name", "instructions", "items", "pantry_ingredients"})
_MODEL_ITEM = frozenset({"offer_id", "quantity"})


def meal_count_for_frequency(frequency):
    return {1: 5, 2: 3, 3: 2}.get(frequency, 3)


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


def _model_meals(model_output, offers_by_id, frequency, pantry):
    _reject_extra(model_output, _MODEL_TOP_LEVEL)
    meals = model_output.get("meals")
    if not isinstance(meals, list) or len(meals) != meal_count_for_frequency(frequency):
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
        instructions = meal.get("instructions")
        if not isinstance(instructions, list) or not instructions:
            raise ValueError("Chýbajú pokyny k jedlu.")
        steps = [_text(instruction, "pokyn") for instruction in instructions]
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
            offer_id = item.get("offer_id")
            quantity = item.get("quantity")
            if isinstance(offer_id, bool) or not isinstance(offer_id, int) or offer_id not in offers_by_id:
                raise ValueError("Návrh obsahuje neznáme alebo neaktuálne offer_id.")
            if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity <= 0:
                raise ValueError("Množstvo musí byť kladné celé číslo.")
            if offer_id in seen_offers:
                raise ValueError("Návrh obsahuje duplicitné offer_id.")
            seen_offers.add(offer_id)
            selected_items.append((offers_by_id[offer_id], quantity))
        parsed.append((day, name, steps, selected_items, [pantry_by_name[item] for item in selected_pantry]))
    return parsed


def personal_plan_prompt(rows, frequency, pantry):
    """Expose only food content and opaque offer references to the model."""
    offers = "\n".join(
        f"- offer_id: {row['id']}; názov: {row['nazov']}; kategória: {row['kategoria'] or 'iné'}"
        for row in rows
    )
    pantry_text = ", ".join(pantry) or "nič"
    return f"""Navrhni presne {meal_count_for_frequency(frequency)} jedlá. Vráť iba JSON.

Povolený formát:
{{"meals":[{{"day":"PO","name":"...","instructions":["..."],"items":[{{"offer_id":123,"quantity":1}}],"pantry_ingredients":["..."]}}]}}

Pravidlá:
- Každá položka smie obsahovať iba offer_id a celé kladné quantity.
- Suroviny zo špajze uveď výlučne v pantry_ingredients a len z ponuky používateľa.
- Nesmieš uvádzať ani meniť obchod, názov položky, jednotku, cenu, bežnú cenu, úsporu, zdroj ani súčty.
- Každý offer_id a deň použi najviac raz. Pokyny musia byť neprázdne.

Špajza používateľa: {pantry_text}
Ponuky:
{offers}"""


def build_personal_plan(con, model_output, stores, frequency, pantry=(), today=None):
    """Validate selection content and deterministically derive all purchasable data."""
    today = today or date.today()
    offers = current_verified_offers(con, stores, today)
    offers_by_id = {row["id"]: row for row in offers}
    meals = _model_meals(model_output, offers_by_id, frequency, pantry)

    plan_meals = []
    purchases = []
    total = Decimal("0")
    regular = Decimal("0")
    for day, name, instructions, selected_items, pantry_names in meals:
        ingredients = []
        for row, quantity in selected_items:
            price = _price(row["cena"], "akciová cena") * quantity
            original = _price(row["povodna"], "bežná cena") * quantity if row["povodna"] is not None else None
            total += price
            if original is not None:
                regular += original
            ingredient = {
                "offer_id": row["id"], "nazov": row["nazov"], "obchod": row["obchod"],
                "jednotka": row["jednotka"], "mnozstvo": quantity, "cena": _format(price),
                "povodna": _format(original) if original is not None else None,
                "zlava": row["zlava"] or "",
            }
            ingredients.append(ingredient)
            purchases.append(ingredient)
        ingredients.extend({"spajza": item} for item in pantry_names)
        plan_meals.append({
            "den": day, "nazov": name, "recept": {"kroky": instructions}, "suroviny": ingredients,
        })

    grouped = {}
    for item in purchases:
        grouped.setdefault(item["obchod"], []).append({
            key: item[key] for key in ("offer_id", "nazov", "jednotka", "mnozstvo", "cena", "povodna", "zlava")
        })
    shopping = [
        {"obchod": store, "polozky": sorted(items, key=lambda item: (item["nazov"].casefold(), item["offer_id"]))}
        for store, items in sorted(grouped.items(), key=lambda pair: STORE_ORDER.index(pair[0]))
    ]
    return {
        "tyzden": current_monday(today), "jedla": plan_meals, "nakupny_zoznam": shopping,
        "nakup_spolu": _format(total), "bezne": _format(regular), "usetris": _format(regular - total),
    }


def cached_offer_ids(plan):
    """Return the verified offer IDs a reconstructed cached plan depends on."""
    try:
        ids = [ingredient["offer_id"] for meal in plan["jedla"] for ingredient in meal["suroviny"] if "offer_id" in ingredient]
    except (KeyError, TypeError):
        return None
    return set(ids) if ids and all(isinstance(item, int) and not isinstance(item, bool) for item in ids) else None


def cached_plan_is_current(plan, rows):
    selected = cached_offer_ids(plan)
    return selected is not None and selected.issubset({row["id"] for row in rows})
