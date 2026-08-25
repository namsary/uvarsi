"""Deterministic public-receipt construction from verified offer rows."""
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

try:
    from .offer_data import ALLOWED_STORES
    from .weekly_data import current_monday, current_verified_offers
except ImportError:
    from offer_data import ALLOWED_STORES
    from weekly_data import current_monday, current_verified_offers


CENT = Decimal("0.01")
# Koľko overených ponúk treba, aby sa z nich vôbec dal poskladať týždeň jedál.
# Prah sa týka ponúk s overiteľnou AKCIOVOU cenou — bežná cena je nepovinná,
# lebo väčšina cenoviek v letáku prečiarknutú bežnú cenu vôbec neuvádza.
MIN_COMPOSABLE_OFFERS = 3
TOO_FEW_OFFERS = "Málo overených ponúk pre aktuálny týždeň — nechávam starý bloček."
_MODEL_TOP_LEVEL = frozenset({"meals"})
_MODEL_MEAL = frozenset({"day", "name", "instructions", "items"})
_MODEL_ITEM = frozenset({"offer_key", "quantity"})


class StructuralFailure(SystemExit):
    """Deterministický pád: rovnaké vstupy zlyhajú znova, opakovanie je zbytočné.

    Dozorca podľa toho odlíši dočasnú chybu (sieť, model, zamknutá DB), ktorú
    má zmysel skúsiť o hodinu, od štrukturálnej, ktorá len páli kredit.
    """

    EXIT_CODE = 3


def _text(value, field):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Chýba {field} v návrhu bločku.")
    return value.strip()


def _cents(value, field):
    try:
        amount = Decimal(str(value).strip().replace(",", "."))
    except (InvalidOperation, ValueError) as error:
        raise ValueError(f"Neplatná {field} v overenej ponuke.") from error
    if not amount.is_finite() or amount <= 0 or amount != amount.quantize(CENT):
        raise ValueError(f"Neplatná {field} v overenej ponuke.")
    return amount


def _format(amount):
    return format(amount.quantize(CENT), "f").replace(".", ",")


def _regular_price(row):
    """Bežná cena len vtedy, keď ju leták naozaj niesol — inak None."""
    original = row["povodna"]
    if original is None:
        return None
    return _cents(original, "bežná cena")


def priceable_offers(rows):
    """Offers the receipt may be built from: an auditable promo price is enough.

    A leaflet price without a crossed-out regular price is normal and common.
    Such an offer is real merchandise at a real price — it simply may not
    substantiate any saving, so it is kept here and excluded from
    `eligible_offers`. An offer whose stated regular price is unusable is
    dropped entirely; we never guess what it should have been.
    """
    usable = []
    for row in rows:
        try:
            price = _cents(row["cena"], "akciová cena")
            original = _regular_price(row)
        except ValueError:
            continue
        if original is not None and original < price:
            continue
        usable.append(row)
    return usable


def eligible_offers(rows):
    """Only offers with an auditable regular price may substantiate savings."""
    return [row for row in priceable_offers(rows) if row["povodna"] is not None]


def composition_prompt(rows):
    """Give the model food content and opaque identifiers, never commercial facts."""
    offers = "\n".join(
        f"- offer_key: {row['offer_key']}; názov: {row['nazov']}; kategória: {row['kategoria'] or 'iné'}"
        for row in rows
    )
    return f"""Navrhni jedlá z týchto overených surovín. Vráť iba JSON.

Povolený formát:
{{"meals":[{{"day":"PO","name":"...","instructions":["..."],"items":[{{"offer_key":"offer_...","quantity":1}}]}}]}}

Pravidlá:
- Každá položka smie obsahovať iba offer_key a celé quantity.
- Nesmieš uvádzať ani meniť obchod, názov položky, jednotku, cenu, bežnú cenu, úsporu ani zdroj.
- Každý offer_key použi najviac raz.

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
            offer_key = item.get("offer_key")
            quantity = item.get("quantity", 1)
            if not isinstance(offer_key, str) or offer_key not in offered_ids:
                raise ValueError("Návrh obsahuje neznáme alebo nevybrané offer_key.")
            if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity <= 0:
                raise ValueError("Množstvo musí byť kladné celé číslo.")
            if offer_key in seen:
                raise ValueError("Návrh obsahuje duplicitné offer_key.")
            seen.add(offer_key)
            selected.append((meal, offer_key, quantity))
    return selected


def _week_label(today):
    monday = today - timedelta(days=today.weekday())
    sunday = monday + timedelta(days=6)
    return f"{monday.day}.–{sunday.day}. {sunday.month}. {sunday.year}"


def build_public_receipt(con, model_output, today=None, generated_at=None):
    """Validate model content then derive every commercial value from the DB."""
    today = today or date.today()
    offers = priceable_offers(current_verified_offers(con, ALLOWED_STORES, today))
    if len(offers) < MIN_COMPOSABLE_OFFERS:
        raise StructuralFailure(TOO_FEW_OFFERS)
    offers_by_key = {row["offer_key"]: row for row in offers}
    selected = _model_selection(model_output, offers_by_key)

    meals = []
    sources = []
    source_keys = set()
    total = Decimal("0")
    regular = Decimal("0")
    counted = 0
    substantiated = 0
    for meal in model_output["meals"]:
        items = []
        for selected_meal, offer_key, quantity in selected:
            if selected_meal is not meal:
                continue
            row = offers_by_key[offer_key]
            price = _cents(row["cena"], "akciová cena") * quantity
            verified = _regular_price(row)
            original = verified * quantity if verified is not None else None
            total += price
            # Bez overenej bežnej ceny položka do úspory neprispieva ničím.
            regular += original if original is not None else price
            counted += 1
            substantiated += original is not None
            items.append({
                "offer_key": offer_key,
                "name": row["nazov"],
                "store": row["obchod"],
                "unit": row["jednotka"],
                "quantity": quantity,
                "price": _format(price),
                "original_price": _format(original) if original is not None else None,
                "savings": _format(original - price) if original is not None else None,
                "off": row["zlava"] or "",
            })
            source = {
                "store": row["obchod"],
                "url": row["source_url"],
                "source_page": row["source_page"],
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
            # Koľko položiek bločku vie úsporu doložiť prečiarknutou cenou.
            "polozky": counted,
            "polozky_s_beznou_cenou": substantiated,
        },
    }
