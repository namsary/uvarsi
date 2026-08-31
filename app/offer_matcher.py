"""Fail-closed mapping of validated flyer offers to catalog ingredients."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
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


def _contains_token_sequence(
    product_tokens: tuple[str, ...], alias_tokens: tuple[str, ...]
) -> bool:
    width = len(alias_tokens)
    return any(
        product_tokens[start : start + width] == alias_tokens
        for start in range(len(product_tokens) - width + 1)
    )


def _match_ingredient(
    product_name: str, catalog: IngredientCatalog
) -> Ingredient | None:
    product_tokens = tuple(normalize_name(product_name).split())
    longest = 0
    candidates: dict[str, Ingredient] = {}

    for item in catalog.all():
        for alias in (item.name, *item.synonyms):
            alias_tokens = tuple(normalize_name(alias).split())
            if len(alias_tokens) < longest or not _contains_token_sequence(
                product_tokens, alias_tokens
            ):
                continue
            resolved = catalog.resolve(alias)
            if resolved is None:
                continue
            if len(alias_tokens) > longest:
                longest = len(alias_tokens)
                candidates = {resolved.id: resolved}
            else:
                candidates[resolved.id] = resolved

    if len(candidates) != 1:
        return None
    return next(iter(candidates.values()))


def _package_is_compatible(package: PackageSize, ingredient: Ingredient) -> bool:
    unit = package.content.unit
    return (
        unit == "g"
        or unit == "ml" and ingredient.density_g_per_ml is not None
        or unit == "piece" and ingredient.grams_per_piece is not None
    )


def match_offers(
    rows: Iterable[Mapping[str, object]], catalog: IngredientCatalog
) -> Sequence[MatchedOffer]:
    matched = []
    for row in rows:
        ingredient = _match_ingredient(row["nazov"], catalog)
        if ingredient is None:
            continue
        try:
            package = PackageSize(parse_quantity(row["jednotka"]))
        except (TypeError, ValueError):
            continue
        if not _package_is_compatible(package, ingredient):
            continue

        original_price = row["povodna"]
        matched.append(
            MatchedOffer(
                offer_key=row["offer_key"],
                store=row["obchod"],
                product_name=row["nazov"],
                ingredient=ingredient,
                package=package,
                sale_price=Decimal(str(row["cena"])),
                original_price=(
                    None
                    if original_price is None
                    else Decimal(str(original_price))
                ),
                valid_from=date.fromisoformat(row["valid_from"]),
                valid_to=date.fromisoformat(row["valid_to"]),
                source_url=row["source_url"],
            )
        )
    return tuple(matched)
