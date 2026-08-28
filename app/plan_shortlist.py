"""Deterministic, prompt-only selection from the current verified offer set."""
from datetime import date
from decimal import Decimal, InvalidOperation
import math
from typing import Sequence
import unicodedata

try:
    from .offer_data import canonical_offer_key, validate_offer
    from .weekly_data import current_monday
except ImportError:
    from offer_data import canonical_offer_key, validate_offer
    from weekly_data import current_monday


DEFAULT_LIMIT = 120
CORE_CATEGORIES = ("maso", "zelenina", "mliecne", "trvanlive")


def _normalized(value):
    text = unicodedata.normalize("NFKD", str(value).casefold())
    return "".join(character for character in text if not unicodedata.combining(character)).strip()


def _discount_ratio(row):
    try:
        price = Decimal(str(row.get("cena")))
        original = Decimal(str(row.get("povodna")))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal("0")
    if not price.is_finite() or not original.is_finite() or original <= 0:
        return Decimal("0")
    return max(Decimal("0"), (original - price) / original)


def _has_numeric_price(row):
    price = row.get("cena")
    return isinstance(price, (int, float)) and not isinstance(price, bool) and math.isfinite(price)


def _rank(row):
    category = _normalized(row["kategoria"])
    return (
        category not in CORE_CATEGORIES,
        -_discount_ratio(row),
        not _has_numeric_price(row),
        canonical_offer_key(row["offer_key"]),
    )


def _row_tiebreaker(row):
    return tuple(sorted((str(key), repr(value)) for key, value in row.items()))


def _eligible_rows(rows, stores, today):
    requested_week = current_monday(today)
    deduplicated = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            validate_offer(row)
        except (TypeError, ValueError):
            continue
        key = canonical_offer_key(row.get("offer_key"))
        if not isinstance(key, str) or not key:
            continue
        if _normalized(row["obchod"]) not in stores:
            continue
        if row.get("tyzden") != requested_week:
            continue
        if not row["valid_from"] <= today.isoformat() <= row["valid_to"]:
            continue
        previous = deduplicated.get(key)
        if previous is None or (_rank(row), _row_tiebreaker(row)) < (
                _rank(previous), _row_tiebreaker(previous)):
            deduplicated[key] = row
    return sorted(deduplicated.values(), key=_rank)


def _take_first(rows, selected, predicate, limit):
    if len(selected) >= limit:
        return
    for row in rows:
        key = canonical_offer_key(row["offer_key"])
        if key not in selected and predicate(row):
            selected[key] = row
            return


def select_offers(rows, stores: Sequence[str], limit: int = DEFAULT_LIMIT) -> list:
    """Choose a stable, bounded prompt catalogue without weakening validation.

    Callers pass the complete current offer rows.  This function is deliberately
    only a prompt optimization: final plan validation still receives every row.
    """
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
        raise ValueError("limit must be a non-negative integer")
    if limit == 0:
        return []

    requested_stores = sorted({_normalized(store) for store in stores if str(store).strip()})
    if not requested_stores:
        return []
    candidates = _eligible_rows(rows, set(requested_stores), date.today())
    selected = {}
    for store in requested_stores:
        _take_first(candidates, selected, lambda row, store=store: _normalized(row["obchod"]) == store, limit)
    for category in CORE_CATEGORIES:
        _take_first(candidates, selected, lambda row, category=category: _normalized(row["kategoria"]) == category, limit)
    for row in candidates:
        if len(selected) >= limit:
            break
        selected.setdefault(canonical_offer_key(row["offer_key"]), row)
    return sorted(selected.values(), key=_rank)
