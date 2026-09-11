#!/usr/bin/env python3
"""Build the public landing receipt from verified offers and curated recipes."""
import hashlib
import os
import sqlite3
import sys
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path

from app.deterministic_plan import NoCompatiblePlan, build_deterministic_plan
from app.ingredient_catalog import load_ingredient_catalog
from app.landing_data import validate_landing_data, write_landing_data_atomic
from app.offer_data import ALLOWED_STORES, CURRENT_COLLECTION_DATA_VERSION
from app.recipe_catalog import load_recipe_catalog
from app.receipt_data import (
    MIN_COMPOSABLE_OFFERS,
    TOO_FEW_OFFERS,
    StructuralFailure,
    build_public_receipt,
    priceable_offers,
)
from app.weekly_data import (
    current_monday,
    current_verified_offers,
    stores_missing_this_week,
)


LANDING_DATA_PATH = Path("/var/lib/uvarsi/landing_data.json")
DATABASE_PATH = "/opt/uvarsi/uvarsi.db"
# Dohoda s dozorcom: 1 = skús o hodinu znova, 3 = opakovanie nemá zmysel.
EXIT_RETRY = 1
LANDING_ADULTS = 2
LANDING_CHILDREN = 2
LANDING_FREQUENCY = 3
LANDING_PLAN_VARIANTS = 12


def landing_data_output_path(arguments):
    if not arguments:
        return LANDING_DATA_PATH
    if len(arguments) == 1 and Path(arguments[0]) == LANDING_DATA_PATH:
        return LANDING_DATA_PATH
    raise SystemExit("Použitie: refresh_blocek.py /var/lib/uvarsi/landing_data.json")


def _landing_seed(today):
    week = current_monday(today)
    digest = hashlib.sha256(f"uvarsi-landing-v1:{week}".encode("utf-8")).hexdigest()
    return f"landing:{week}:{digest[:12]}"


def _line_amount(value, field):
    try:
        amount = Decimal(str(value).strip().replace(",", "."))
    except (InvalidOperation, ValueError) as error:
        raise ValueError(f"Plán obsahuje neplatnú {field} pre bloček.") from error
    if not amount.is_finite() or amount <= 0:
        raise ValueError(f"Plán obsahuje neplatnú {field} pre bloček.")
    return amount


def _money_text(value):
    return format(value.quantize(Decimal("0.01")), "f").replace(".", ",")


def _shopping_quantities(plan, offered_keys):
    """Return the plan's real, already aggregated package counts by offer."""
    quantities = {}
    weighted_totals = {}
    for group in plan.get("nakupny_zoznam", ()):
        for item in group.get("polozky", ()):
            offer_key = item.get("offer_key")
            if offer_key not in offered_keys:
                continue
            quantity = item.get("mnozstvo")
            if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity <= 0:
                raise ValueError("Plán obsahuje neplatný počet balení pre bloček.")
            quantities[offer_key] = quantities.get(offer_key, 0) + quantity
            if item.get("predaj_na_vahu") is not True:
                continue
            price = _line_amount(item.get("cena"), "cenu váženej položky")
            original = (
                None
                if item.get("povodna") is None
                else _line_amount(item["povodna"], "bežnú cenu váženej položky")
            )
            loyalty = (
                None
                if item.get("cena_s_kartou") is None
                else _line_amount(item["cena_s_kartou"], "vernostnú cenu váženej položky")
            )
            current = weighted_totals.get(offer_key)
            if current is None:
                weighted_totals[offer_key] = {
                    "price": price,
                    "original_price": original,
                    "loyalty_price": loyalty,
                }
                continue
            for field, amount in (
                ("price", price),
                ("original_price", original),
                ("loyalty_price", loyalty),
            ):
                if (current[field] is None) != (amount is None):
                    raise ValueError("Plán obsahuje nejednotné ceny váženej položky.")
                if amount is not None:
                    current[field] += amount
    return quantities, {
        offer_key: {
            field: None if amount is None else _money_text(amount)
            for field, amount in totals.items()
        }
        for offer_key, totals in weighted_totals.items()
    }


def _distinct_meal_offers(candidates):
    """Choose one different purchased offer for every meal, if possible."""
    assigned = [None] * len(candidates)
    meal_order = sorted(range(len(candidates)), key=lambda index: len(candidates[index]))

    def reserve(position, used):
        if position == len(meal_order):
            return True
        meal_index = meal_order[position]
        for offer_key in candidates[meal_index]:
            if offer_key in used:
                continue
            assigned[meal_index] = offer_key
            used.add(offer_key)
            if reserve(position + 1, used):
                return True
            used.remove(offer_key)
            assigned[meal_index] = None
        return False

    return assigned if reserve(0, set()) else None


def _receipt_selection(plan, offered_keys, *, include_verified_totals=False):
    source_meals = list(plan.get("jedla", ()))
    quantities, verified_totals = _shopping_quantities(plan, offered_keys)
    candidates = []
    for meal in source_meals:
        meal_keys = []
        for ingredient in meal.get("suroviny", ()):
            offer_key = ingredient.get("offer_key")
            if offer_key in quantities and offer_key not in meal_keys:
                meal_keys.append(offer_key)
        candidates.append(meal_keys)

    reserved = _distinct_meal_offers(candidates)
    if reserved is None:
        empty = {"meals": []}
        return (empty, verified_totals) if include_verified_totals else empty

    owners = {offer_key: index for index, offer_key in enumerate(reserved)}
    for meal_index, meal_keys in enumerate(candidates):
        for offer_key in meal_keys:
            owners.setdefault(offer_key, meal_index)

    meals = []
    for meal_index, meal in enumerate(source_meals):
        recipe = meal.get("recept") or {}
        instructions = recipe.get("kroky") or []
        if not instructions:
            raise ValueError("Kurátorovaný recept nemá postup.")
        items = [
            {"offer_key": offer_key, "quantity": quantities[offer_key]}
            for offer_key in candidates[meal_index]
            if owners.get(offer_key) == meal_index
        ]
        meals.append({
            "day": meal.get("den"),
            "name": meal.get("nazov"),
            "instructions": list(instructions),
            "items": items,
        })
    selection = {"meals": meals}
    return (selection, verified_totals) if include_verified_totals else selection


def compose_curated_receipt(offers, today, *, include_verified_totals=False):
    """Choose one stable weekly showcase plan without network or model calls."""
    ingredients = load_ingredient_catalog()
    recipes = load_recipe_catalog(ingredients)
    stores = tuple(
        store for store in ALLOWED_STORES
        if any(row["obchod"] == store for row in offers)
    )
    base_seed = _landing_seed(today)
    offered_keys = {row["offer_key"] for row in offers}
    for variant in range(LANDING_PLAN_VARIANTS):
        seed = base_seed if variant == 0 else f"{base_seed}:variant-{variant}"
        try:
            plan = build_deterministic_plan(
                week=current_monday(today),
                rows=offers,
                stores=stores,
                adults=LANDING_ADULTS,
                children=LANDING_CHILDREN,
                frequency=LANDING_FREQUENCY,
                pantry=(),
                pantry_driven=False,
                mode="standard",
                seed=seed,
                ingredient_catalog=ingredients,
                recipe_catalog=recipes,
            )
        except NoCompatiblePlan:
            continue
        selection, verified_totals = _receipt_selection(
            plan, offered_keys, include_verified_totals=True
        )
        if len(selection["meals"]) == LANDING_FREQUENCY:
            return (
                (selection, verified_totals)
                if include_verified_totals
                else selection
            )
    raise ValueError("Z aktuálnych akcií sa nepodarilo zostaviť tri odlišné jedlá.")


def refresh_from_db(path, database, compose=None, today=None):
    """Build after the DB gate, then atomically publish derived data."""
    today = today or date.today()
    with sqlite3.connect(database) as con:
        con.row_factory = sqlite3.Row
        has_status = con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='zber_stav'"
        ).fetchone()
        if has_status:
            missing = stores_missing_this_week(con, ALLOWED_STORES, today)
            if missing:
                raise StructuralFailure(
                    "Bloček nevytváram z neúplného zberu: " + ", ".join(missing)
                )
        offers = priceable_offers(current_verified_offers(con, ALLOWED_STORES, today))
        if len(offers) < MIN_COMPOSABLE_OFFERS:
            raise StructuralFailure(TOO_FEW_OFFERS)
        if compose is None:
            selection, verified_totals = compose_curated_receipt(
                offers, today, include_verified_totals=True
            )
        else:
            selection = compose(offers, today)
            verified_totals = None
        payload = build_public_receipt(
            con,
            selection,
            today=today,
            verified_line_totals=verified_totals,
        )
    payload["offer_data_version"] = CURRENT_COLLECTION_DATA_VERSION
    validate_landing_data(
        payload, today, required_offer_data_version=CURRENT_COLLECTION_DATA_VERSION
    )
    write_landing_data_atomic(path, payload)
    return payload


def main():
    """Odlíš štrukturálny pád od dočasného a zachovaj posledný dobrý bloček."""
    path = landing_data_output_path(sys.argv[1:])
    database = os.environ.get("UVARSI_DB", DATABASE_PATH)
    try:
        refresh_from_db(path, database, today=date.today())
    except StructuralFailure as failure:
        print(f"ŠTRUKTURÁLNA CHYBA: {failure}", file=sys.stderr)
        raise SystemExit(StructuralFailure.EXIT_CODE) from None
    except Exception as error:  # zamknutá DB alebo meniaci sa katalóg — o hodinu to môže vyjsť
        print(f"DOČASNÁ CHYBA: {type(error).__name__}: {error}", file=sys.stderr)
        raise SystemExit(EXIT_RETRY) from None


if __name__ == "__main__":
    main()
