"""Practical package defaults for ingredients that are not in a current flyer."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

from .ingredient_catalog import Ingredient
from .quantity_math import Quantity


@dataclass(frozen=True)
class RegularPurchaseRule:
    package: Quantity
    pricing_basis: Literal["package", "weight"] = "package"


_PACKAGES: dict[str, Quantity] = {
    "apple_cider_vinegar": Quantity(Decimal("500"), "ml"),
    "barley": Quantity(Decimal("500"), "g"),
    "basil_pesto": Quantity(Decimal("190"), "g"),
    "beans": Quantity(Decimal("500"), "g"),
    "beans_canned": Quantity(Decimal("400"), "g"),
    "bread": Quantity(Decimal("500"), "g"),
    "bryndza": Quantity(Decimal("125"), "g"),
    "bulgur": Quantity(Decimal("500"), "g"),
    "butter": Quantity(Decimal("250"), "g"),
    "chickpeas_canned": Quantity(Decimal("400"), "g"),
    "cinnamon": Quantity(Decimal("20"), "g"),
    "coconut_milk": Quantity(Decimal("400"), "ml"),
    "cottage_cheese": Quantity(Decimal("180"), "g"),
    "couscous": Quantity(Decimal("500"), "g"),
    "cream": Quantity(Decimal("200"), "ml"),
    "cumin": Quantity(Decimal("20"), "g"),
    "curry_powder": Quantity(Decimal("20"), "g"),
    "dry_peas": Quantity(Decimal("500"), "g"),
    "egg": Quantity(Decimal("10"), "piece"),
    "egg_noodles": Quantity(Decimal("250"), "g"),
    "feta": Quantity(Decimal("200"), "g"),
    "garlic": Quantity(Decimal("60"), "g"),
    "gnocchi": Quantity(Decimal("500"), "g"),
    "grilling_cheese": Quantity(Decimal("200"), "g"),
    "ham": Quantity(Decimal("100"), "g"),
    "hard_cheese": Quantity(Decimal("200"), "g"),
    "chili_powder": Quantity(Decimal("20"), "g"),
    "lentils": Quantity(Decimal("500"), "g"),
    "marjoram": Quantity(Decimal("10"), "g"),
    "milk": Quantity(Decimal("1000"), "ml"),
    "mozzarella": Quantity(Decimal("125"), "g"),
    "oil": Quantity(Decimal("1000"), "ml"),
    "oregano": Quantity(Decimal("10"), "g"),
    "paprika_powder": Quantity(Decimal("20"), "g"),
    "pasta": Quantity(Decimal("500"), "g"),
    "peas": Quantity(Decimal("450"), "g"),
    "plain_yogurt": Quantity(Decimal("500"), "g"),
    "red_lentils": Quantity(Decimal("500"), "g"),
    "rice": Quantity(Decimal("1000"), "g"),
    "salt": Quantity(Decimal("500"), "g"),
    "sauerkraut": Quantity(Decimal("500"), "g"),
    "skyr": Quantity(Decimal("350"), "g"),
    "smoked_sausage": Quantity(Decimal("250"), "g"),
    "soy_sauce": Quantity(Decimal("150"), "ml"),
    "sugar": Quantity(Decimal("1000"), "g"),
    "tofu": Quantity(Decimal("180"), "g"),
    "tortilla": Quantity(Decimal("6"), "piece"),
    "tuna": Quantity(Decimal("160"), "g"),
    "wheat_flour": Quantity(Decimal("1000"), "g"),
}

_FRESH_WEIGHT_CATEGORIES = frozenset({"mäso", "ryby", "zelenina", "ovocie"})


def regular_purchase_rule(
    ingredient: Ingredient, recipe_unit: str
) -> RegularPurchaseRule:
    """Return an honest, deterministic buying unit without inventing a price."""
    package = _PACKAGES.get(ingredient.id)
    if package is not None:
        return RegularPurchaseRule(package)
    if recipe_unit == "piece":
        return RegularPurchaseRule(Quantity(Decimal("1"), "piece"))
    if ingredient.category in _FRESH_WEIGHT_CATEGORIES:
        return RegularPurchaseRule(
            Quantity(Decimal("1000"), "g"), pricing_basis="weight"
        )
    if recipe_unit == "ml":
        return RegularPurchaseRule(Quantity(Decimal("1000"), "ml"))
    return RegularPurchaseRule(Quantity(Decimal("500"), "g"))
