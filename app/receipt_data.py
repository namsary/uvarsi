"""Deterministic public-receipt construction from verified offer rows."""
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

try:
    from .weekly_data import current_monday
except ImportError:
    from weekly_data import current_monday


CENT = Decimal("0.01")
MIN_ELIGIBLE_OFFERS = 3
_MODEL_TOP_LEVEL = frozenset({"meals"})
_MODEL_MEAL = frozenset({"day", "name", "instructions", "items"})
_MODEL_ITEM = frozenset({"offer_id", "quantity"})


def _text(value, field):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Chýba {field} v návrhu bločku.")
    return value.strip()


def _cents(value, field):
    try:
        amount = Decimal(str(value).strip().replace(",", "."))
    except (InvalidOperation, ValueError) as error:
        raise ValueError(f"Neplatná {field} v overenej ponuke.") from error
    if not amount.is_finite() or amount < 0 or amount != amount.quantize(CENT):
        raise ValueError(f"Neplatná {field} v overenej ponuke.")
    return amount


def _format(amount):
    return format(amount.quantize(CENT), "f").replace(".", ",")


def current_verified_offers(con, today=None):
    """Return only rows whose own evidence proves they are current today."""
    today = today or date.today()
    return con.execute(
        """SELECT id, obchod, nazov, kategoria, cena, povodna, zlava, jednotka,
                  source_url, valid_from, valid_to
             FROM akcie
             WHERE tyzden=?
               AND source_url IS NOT NULL AND source_page IS NOT NULL
               AND valid_from IS NOT NULL AND valid_to IS NOT NULL
               AND valid_from<=? AND valid_to>=?
             ORDER BY id""",
        (current_monday(today), today.isoformat(), today.isoformat()),
    ).fetchall()


def eligible_offers(rows):
    """Only offers with an auditable regular price may substantiate savings."""
    eligible = []
    for row in rows:
        price = _cents(row["cena"], "akciová cena")
        original = _cents(row["povodna"], "bežná cena") if row["povodna"] is not None else None
        if original is not None and original >= price:
            eligible.append(row)
    return eligible


def composition_prompt(rows):
    """Give the model food content and opaque identifiers, never commercial facts."""
    offers = "\n".join(
        f"- offer_id: {row['id']}; názov: {row['nazov']}; kategória: {row['kategoria'] or 'iné'}"
        for row in rows
    )
    return f"""Navrhni jedlá z týchto overených surovín. Vráť iba JSON.

Povolený formát:
{{"meals":[{{"day":"PO","name":"...","instructions":["..."],"items":[{{"offer_id":123,"quantity":1}}]}}]}}

Pravidlá:
- Každá položka smie obsahovať iba offer_id a celé quantity.
- Nesmieš uvádzať ani meniť obchod, názov položky, jednotku, cenu, bežnú cenu, úsporu ani zdroj.
- Každý offer_id použi najviac raz.

Ponuky:
{offers}"""


def _reject_extra(mapping, allowed):
    if not isinstance(mapping, dict) or set(mapping) - allowed:
        raise ValueError("Návrh obsahuje nepovolené obchodné údaje.")


def _model_selection(model_output, offered_ids):
    _reject_extra(model_output, _MODEL_TOP_LEVEL)
    meals = model_output.get("meals")
    if not isinstance(meals, list) or not meals:
        raise ValueError("Návrh bločku neobsahuje jedlá.")

    selected = []
    seen = set()
    for meal in meals:
        _reject_extra(meal, _MODEL_MEAL)
        _text(meal.get("day"), "deň")
        _text(meal.get("name"), "názov jedla")
        instructions = meal.get("instructions")
        if not isinstance(instructions, list) or not instructions:
            raise ValueError("Chýbajú pokyny k jedlu.")
        for instruction in instructions:
            _text(instruction, "pokyn")
        items = meal.get("items")
        if not isinstance(items, list) or not items:
            raise ValueError("Jedlo nemá vybrané ponuky.")
        for item in items:
            _reject_extra(item, _MODEL_ITEM)
            offer_id = item.get("offer_id")
            quantity = item.get("quantity", 1)
            if isinstance(offer_id, bool) or not isinstance(offer_id, int) or offer_id not in offered_ids:
                raise ValueError("Návrh obsahuje neznáme alebo nevybrané offer_id.")
            if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity <= 0:
                raise ValueError("Množstvo musí byť kladné celé číslo.")
            if offer_id in seen:
                raise ValueError("Návrh obsahuje duplicitné offer_id.")
            seen.add(offer_id)
            selected.append((meal, offer_id, quantity))
    return selected


def _week_label(today):
    monday = today - timedelta(days=today.weekday())
    sunday = monday + timedelta(days=6)
    return f"{monday.day}.–{sunday.day}. {sunday.month}. {sunday.year}"


def build_public_receipt(con, model_output, today=None, generated_at=None):
    """Validate model content then derive every commercial value from the DB."""
    today = today or date.today()
    offers = eligible_offers(current_verified_offers(con, today))
    if len(offers) < MIN_ELIGIBLE_OFFERS:
        raise SystemExit("Málo overených ponúk s bežnou cenou — nechávam starý bloček.")
    offers_by_id = {row["id"]: row for row in offers}
    selected = _model_selection(model_output, offers_by_id)

    meals = []
    sources = []
    source_keys = set()
    total = Decimal("0")
    regular = Decimal("0")
    for meal in model_output["meals"]:
        items = []
        for selected_meal, offer_id, quantity in selected:
            if selected_meal is not meal:
                continue
            row = offers_by_id[offer_id]
            price = _cents(row["cena"], "akciová cena") * quantity
            original = _cents(row["povodna"], "bežná cena") * quantity
            total += price
            regular += original
            items.append({
                "offer_id": offer_id,
                "name": row["nazov"],
                "store": row["obchod"],
                "unit": row["jednotka"],
                "quantity": quantity,
                "price": _format(price),
                "original_price": _format(original),
                "savings": _format(original - price),
                "off": row["zlava"] or "",
            })
            source = {
                "store": row["obchod"],
                "url": row["source_url"],
                "valid_from": row["valid_from"],
                "valid_to": row["valid_to"],
            }
            source_key = tuple(source.values())
            if source_key not in source_keys:
                source_keys.add(source_key)
                sources.append(source)
        meals.append({
            "day": meal["day"].strip(),
            "name": meal["name"].strip(),
            "instructions": [instruction.strip() for instruction in meal["instructions"]],
            "items": items,
        })

    generated_at = generated_at or datetime.now(timezone.utc).astimezone().isoformat()
    return {
        "schema_version": 1,
        "generated_at": generated_at,
        "week": current_monday(today),
        "week_label": _week_label(today),
        "sources": sources,
        "receipt": {
            "meals": meals,
            "nakup_spolu": _format(total),
            "bezne": _format(regular),
            "usetris": _format(regular - total),
        },
    }
