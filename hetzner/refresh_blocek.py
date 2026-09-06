#!/usr/bin/env python3
"""Build the public landing receipt from verified offers and curated recipes."""
import hashlib
import os
import sqlite3
import sys
from datetime import date
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


def _receipt_selection(plan, offered_keys):
    meals = []
    seen = set()
    for meal in plan.get("jedla", ()):
        items = []
        for ingredient in meal.get("suroviny", ()):
            offer_key = ingredient.get("offer_key")
            if offer_key not in offered_keys or offer_key in seen:
                continue
            quantity = ingredient.get("mnozstvo")
            if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity <= 0:
                raise ValueError("Plán obsahuje neplatný počet balení pre bloček.")
            seen.add(offer_key)
            items.append({"offer_key": offer_key, "quantity": quantity})
        if not items:
            continue
        recipe = meal.get("recept") or {}
        instructions = recipe.get("kroky") or []
        if not instructions:
            raise ValueError("Kurátorovaný recept nemá postup.")
        meals.append({
            "day": meal.get("den"),
            "name": meal.get("nazov"),
            "instructions": list(instructions),
            "items": items,
        })
    if not meals:
        raise ValueError("Kurátorovaný plán neobsahuje ponuky použiteľné na bloček.")
    return {"meals": meals}


def compose_curated_receipt(offers, today):
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
        selection = _receipt_selection(plan, offered_keys)
        if len(selection["meals"]) == LANDING_FREQUENCY:
            return selection
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
        selection = (compose or compose_curated_receipt)(offers, today)
        payload = build_public_receipt(con, selection, today=today)
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
