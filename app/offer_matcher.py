"""Fail-closed mapping of validated flyer offers to catalog ingredients."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from functools import lru_cache
from typing import Iterable, Mapping, Sequence

from .ingredient_catalog import Ingredient, IngredientCatalog, normalize_name
from .quantity_math import PackageSize, parse_quantity


@dataclass(frozen=True)
class MatchedOffer:
    offer_key: str
    store: str
    product_name: str
    ingredient: Ingredient
    package: PackageSize
    sale_price: Decimal
    original_price: Decimal | None
    valid_from: date
    valid_to: date
    source_url: str


@lru_cache(maxsize=8)
def _alias_token_index(
    catalog: IngredientCatalog,
) -> tuple[dict[tuple[str, ...], tuple[str, ...]], int]:
    """Index immutable catalog aliases once instead of rescanning them per row."""
    aliases: dict[tuple[str, ...], set[str]] = {}
    maximum_width = 0
    for item in catalog.all():
        for alias in (item.name, *item.synonyms):
            alias_tokens = tuple(normalize_name(alias).split())
            if not alias_tokens:
                continue
            aliases.setdefault(alias_tokens, set()).add(item.id)
            maximum_width = max(maximum_width, len(alias_tokens))
    return (
        {tokens: tuple(sorted(ids)) for tokens, ids in aliases.items()},
        maximum_width,
    )


def _match_ingredient(
    product_name: str, catalog: IngredientCatalog
) -> Ingredient | None:
    product_tokens = tuple(normalize_name(product_name).split())
    aliases, maximum_width = _alias_token_index(catalog)
    longest = 0
    candidates: set[str] = set()

    for width in range(1, min(maximum_width, len(product_tokens)) + 1):
        if width < longest:
            continue
        for start in range(len(product_tokens) - width + 1):
            ingredient_ids = aliases.get(product_tokens[start : start + width])
            if not ingredient_ids:
                continue
            if width > longest:
                longest = width
                candidates.clear()
            candidates.update(ingredient_ids)

    if len(candidates) != 1:
        return None
    return catalog.by_id(next(iter(candidates)))


def _package_is_compatible(package: PackageSize, ingredient: Ingredient) -> bool:
    unit = package.content.unit
    return (
        unit == "g"
        or unit == "ml" and ingredient.density_g_per_ml is not None
        or unit == "piece" and ingredient.grams_per_piece is not None
    )


def _matched_offer_from_values(
    catalog: IngredientCatalog,
    offer_key,
    store,
    product_name,
    unit,
    sale_price,
    original_price,
    valid_from,
    valid_to,
    source_url,
) -> MatchedOffer | None:
    ingredient = _match_ingredient(product_name, catalog)
    if ingredient is None:
        return None
    try:
        package = PackageSize(parse_quantity(unit))
    except (TypeError, ValueError):
        return None
    if not _package_is_compatible(package, ingredient):
        return None
    return MatchedOffer(
        offer_key=offer_key,
        store=store,
        product_name=product_name,
        ingredient=ingredient,
        package=package,
        sale_price=Decimal(str(sale_price)),
        original_price=(
            None if original_price is None else Decimal(str(original_price))
        ),
        valid_from=date.fromisoformat(valid_from),
        valid_to=date.fromisoformat(valid_to),
        source_url=source_url,
    )


@lru_cache(maxsize=4096)
def _matched_offer_from_hashable_values(*values) -> MatchedOffer | None:
    return _matched_offer_from_values(*values)


def match_offers(
    rows: Iterable[Mapping[str, object]], catalog: IngredientCatalog
) -> Sequence[MatchedOffer]:
    matched = []
    for row in rows:
        values = (
            catalog,
            row["offer_key"],
            row["obchod"],
            row["nazov"],
            row["jednotka"],
            row["cena"],
            row["povodna"],
            row["valid_from"],
            row["valid_to"],
            row["source_url"],
        )
        try:
            hash(values)
        except TypeError:
            offer = _matched_offer_from_values(*values)
        else:
            offer = _matched_offer_from_hashable_values(*values)
        if offer is not None:
            matched.append(offer)
    return tuple(matched)
