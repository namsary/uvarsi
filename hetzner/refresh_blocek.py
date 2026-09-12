#!/usr/bin/env python3
"""Build the public landing receipt from verified offers and curated recipes."""
import hashlib
import json
import os
import re
import sqlite3
import sys
from contextlib import contextmanager
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path

from app.deterministic_plan import NoCompatiblePlan, build_deterministic_plan
from app.ingredient_catalog import load_ingredient_catalog
from app.landing_data import (
    landing_data_is_current,
    load_landing_data,
    validate_landing_data,
    write_landing_data_atomic,
)
from app.offer_data import ALLOWED_STORES, CURRENT_COLLECTION_DATA_VERSION
from app.recipe_catalog import load_recipe_catalog
from app.receipt_data import (
    MIN_COMPOSABLE_OFFERS,
    TOO_FEW_OFFERS,
    StructuralFailure,
    build_public_receipt,
    priceable_offers,
    public_receipt_matches_verified_offers,
)
from app.weekly_data import (
    current_monday,
    current_verified_offers,
)
from app.source_policy import (
    MIN_FACTS_PER_STORE,
    collector_kind_for_url,
    known_source,
)
from app.zbierac_akcii import promote_staged_week, staged_week_readiness


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


def _decimal_text(value):
    return format(value.normalize(), "f")


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
            weight_multiplier = _line_amount(
                item.get("weight_multiplier"), "hmotnostný násobok váženej položky"
            )
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
                    "weight_multiplier": weight_multiplier,
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
            current["weight_multiplier"] += weight_multiplier
    return quantities, {
        offer_key: {
            field: (
                None
                if amount is None
                else _decimal_text(amount)
                if field == "weight_multiplier"
                else _money_text(amount)
            )
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


def _validated_candidate(payload, today):
    """Return a JSON-round-tripped receipt only after every public gate passes."""
    try:
        validate_landing_data(
            payload,
            today,
            required_offer_data_version=CURRENT_COLLECTION_DATA_VERSION,
        )
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        )
        candidate = json.loads(encoded)
        validate_landing_data(
            candidate,
            today,
            required_offer_data_version=CURRENT_COLLECTION_DATA_VERSION,
        )
    except (TypeError, ValueError) as error:
        raise StructuralFailure(f"Neplatný kandidát bločka: {error}") from error
    return candidate


@contextmanager
def _staged_offer_view(con, week, today):
    """Expose one validated staged week to existing receipt builders, read-only."""
    ready, reasons = staged_week_readiness(con, week, today=today)
    if not ready:
        detail = ", ".join(
            f"{store}={reason}" for store, reason in sorted(reasons.items())
        )
        raise StructuralFailure(
            "Bloček nevytváram z neúplného stagingu"
            + (f": {detail}" if detail else ".")
        )

    con.execute(
        "CREATE TEMP TABLE akcie AS "
        "SELECT * FROM main.akcie_staging WHERE tyzden=?",
        (week,),
    )
    try:
        offers = priceable_offers(
            current_verified_offers(con, ALLOWED_STORES, today)
        )
        if len(offers) < MIN_COMPOSABLE_OFFERS:
            raise StructuralFailure(TOO_FEW_OFFERS)
        present_stores = {offer["obchod"] for offer in offers}
        missing_stores = sorted(ALLOWED_STORES - present_stores)
        if missing_stores:
            raise StructuralFailure(
                "Bloček nevytváram bez obchodu: " + ", ".join(missing_stores)
            )
        yield offers
    finally:
        con.execute("DROP TABLE temp.akcie")


@contextmanager
def _active_offer_view(con, today):
    """Expose complete current weekly offers from registered live sources."""
    offers = priceable_offers(
        current_verified_offers(con, ALLOWED_STORES, today)
    )
    counts = {store: 0 for store in ALLOWED_STORES}
    for offer in offers:
        store = offer["obchod"]
        source_kind = collector_kind_for_url(offer.get("source_url"))
        if not known_source(store, source_kind):
            raise StructuralFailure(
                f"Aktívne ponuky obchodu {store} nemajú známy týždenný zdroj."
            )
        counts[store] += 1
    incomplete = sorted(
        store for store, count in counts.items()
        if count < MIN_FACTS_PER_STORE
    )
    if incomplete:
        raise StructuralFailure(
            "Bloček nevytváram z neúplných aktívnych ponúk: "
            + ", ".join(incomplete)
        )
    yield offers


def _candidate_from_offers(con, offers, compose, today):
    try:
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
        return _validated_candidate(payload, today)
    except StructuralFailure:
        raise
    except (KeyError, TypeError, ValueError) as error:
        raise StructuralFailure(f"Neplatný kandidát bločka: {error}") from error


def refresh_from_db(path, database, compose=None, today=None):
    """Validate staged data and receipt, promote, then atomically publish JSON."""
    today = today or date.today()
    week = current_monday(today)
    with sqlite3.connect(database) as con:
        con.row_factory = sqlite3.Row
        with _staged_offer_view(con, week, today) as offers:
            candidate = _candidate_from_offers(con, offers, compose, today)

        if not promote_staged_week(con, week, today=today):
            raise StructuralFailure(
                "Staging sa pred promotion zmenil alebo už nie je kompletný."
            )
    write_landing_data_atomic(path, candidate)
    return candidate


def refresh_from_active_db(
    path, database, compose=None, today=None, require_registered_status=False
):
    """Rebuild a stale receipt from one snapshot of published offers."""
    today = today or date.today()
    if not Path(database).is_file():
        raise StructuralFailure("Databáza aktívnych ponúk neexistuje.")
    with sqlite3.connect(database) as con:
        con.row_factory = sqlite3.Row
        con.execute("BEGIN")
        with _active_offer_view(con, today) as offers:
            if require_registered_status and not _active_offer_set_matches_status(
                con, offers, today
            ):
                raise StructuralFailure(
                    "Aktívne ponuky sa nezhodujú s registrovaným stavom zberu."
                )
            candidate = _candidate_from_offers(con, offers, compose, today)
    write_landing_data_atomic(path, candidate)
    return candidate


def _active_offer_set_matches_status(con, offers, today):
    """Match every offer bucket to exactly one signed collection outcome."""
    status_columns = {
        row[1] for row in con.execute("PRAGMA table_info(zber_stav)")
    }
    required = {
        "tyzden", "obchod", "stav", "pocet", "data_version",
        "collector_kind", "source_fingerprint", "valid_from", "valid_to",
    }
    if not required <= status_columns:
        return False
    by_bucket = {}
    for offer in offers:
        bucket = (offer["obchod"], offer["tyzden"])
        details = by_bucket.setdefault(bucket, {"count": 0, "urls": set()})
        details["count"] += 1
        details["urls"].add(offer["source_url"])
    healthy_stores = set()
    for (store, week), details in by_bucket.items():
        if details["count"] < 1 or len(details["urls"]) != 1:
            return False
        row = con.execute(
            "SELECT stav,pocet,data_version,collector_kind,source_fingerprint,"
            "valid_from,valid_to FROM zber_stav WHERE obchod=? AND tyzden=?",
            (store, week),
        ).fetchone()
        if row is None:
            return False
        try:
            status_from = date.fromisoformat(row[5])
            status_to = date.fromisoformat(row[6])
            recorded_count = int(row[1])
            data_version = int(row[2])
        except (TypeError, ValueError):
            return False
        raw_rows = con.execute(
            "SELECT offer_key,source_url,valid_from,valid_to FROM akcie "
            "WHERE obchod=? AND tyzden=?",
            (store, week),
        ).fetchall()
        try:
            raw_keys = {item[0] for item in raw_rows}
            raw_urls = {item[1] for item in raw_rows}
            raw_starts = [date.fromisoformat(item[2]) for item in raw_rows]
            raw_ends = [date.fromisoformat(item[3]) for item in raw_rows]
        except (TypeError, ValueError):
            return False
        if (
            len(raw_urls) != 1
            or not raw_starts
            or not raw_ends
            or any(not isinstance(key, str) or not key for key in raw_keys)
        ):
            return False
        source_url = next(iter(raw_urls))
        source_kind = collector_kind_for_url(source_url)
        if not (
            row[0] == "ok"
            and recorded_count == len(raw_keys)
            and recorded_count >= details["count"]
            and data_version >= CURRENT_COLLECTION_DATA_VERSION
            and isinstance(row[4], str)
            and re.fullmatch(r"[0-9a-f]{64}", row[4]) is not None
            and row[3] == source_kind
            and details["urls"] == raw_urls
            and status_from == min(raw_starts)
            and status_to == max(raw_ends)
            and status_from <= today <= status_to
        ):
            return False
        healthy_stores.add(store)
    return healthy_stores == set(ALLOWED_STORES)


def active_offers_are_reusable(database, today=None):
    """Non-publishing decision helper used by tests and diagnostics."""
    today = today or date.today()
    if not Path(database).is_file():
        return False
    try:
        with sqlite3.connect(database) as con:
            con.row_factory = sqlite3.Row
            con.execute("BEGIN")
            with _active_offer_view(con, today) as offers:
                return _active_offer_set_matches_status(con, offers, today)
    except (
        OSError, sqlite3.Error, StructuralFailure, KeyError, TypeError, ValueError
    ):
        return False


def landing_data_is_verified_current(path, database, today=None):
    """Require a current receipt that can be rebuilt exactly from live offers."""
    today = today or date.today()
    try:
        if not landing_data_is_current(
            path,
            today,
            required_offer_data_version=CURRENT_COLLECTION_DATA_VERSION,
        ):
            return False
        payload = load_landing_data(path)
        with sqlite3.connect(database) as con:
            con.row_factory = sqlite3.Row
            return public_receipt_matches_verified_offers(con, payload, today=today)
    except (OSError, sqlite3.Error, UnicodeDecodeError, ValueError, TypeError):
        return False


def main():
    """Odlíš štrukturálny pád od dočasného a zachovaj posledný dobrý bloček."""
    if sys.argv[1:2] == ["--verify-current"]:
        if len(sys.argv) != 4:
            raise SystemExit(
                "Použitie: refresh_blocek.py --verify-current LANDING_JSON DATABAZA"
            )
        valid = landing_data_is_verified_current(
            Path(sys.argv[2]), sys.argv[3], today=date.today()
        )
        raise SystemExit(0 if valid else 1)
    verified_active = sys.argv[1:2] == ["--active-current-verified"]
    active_current = verified_active or sys.argv[1:2] == ["--active-current"]
    arguments = sys.argv[2:] if active_current else sys.argv[1:]
    path = landing_data_output_path(arguments)
    database = os.environ.get("UVARSI_DB", DATABASE_PATH)
    try:
        if active_current:
            refresh_from_active_db(
                path,
                database,
                today=date.today(),
                require_registered_status=verified_active,
            )
        else:
            refresh_from_db(path, database, today=date.today())
    except StructuralFailure as failure:
        print(f"ŠTRUKTURÁLNA CHYBA: {failure}", file=sys.stderr)
        raise SystemExit(StructuralFailure.EXIT_CODE) from None
    except Exception as error:  # zamknutá DB alebo meniaci sa katalóg — o hodinu to môže vyjsť
        print(f"DOČASNÁ CHYBA: {type(error).__name__}: {error}", file=sys.stderr)
        raise SystemExit(EXIT_RETRY) from None


if __name__ == "__main__":
    main()
