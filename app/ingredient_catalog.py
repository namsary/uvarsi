"""Canonical ingredients and nutrition values used by the recipe engine."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, DecimalException
from enum import Enum
import json
from pathlib import Path
from types import MappingProxyType
from typing import Iterable, Sequence
import unicodedata


DEFAULT_CATALOG_PATH = Path(__file__).with_name("catalog") / "ingredients.json"
ALLOWED_ROLES = frozenset(
    {"protein", "starch", "vegetable", "aromatic", "fat", "seasoning", "dairy"}
)


class DietTag(str, Enum):
    VEGETARIAN = "vegetarian"
    VEGAN = "vegan"


@dataclass(frozen=True)
class NutritionPer100:
    kcal: Decimal
    protein_g: Decimal
    fat_g: Decimal
    carbs_g: Decimal
    source: str
    verified_on: date


@dataclass(frozen=True)
class Ingredient:
    id: str
    name: str
    synonyms: Sequence[str]
    category: str
    roles: frozenset[str]
    diet_tags: frozenset[DietTag]
    allergens: Sequence[str]
    edible_ratio: Decimal
    grams_per_piece: Decimal | None
    density_g_per_ml: Decimal | None
    nutrition: NutritionPer100


def normalize_name(text: str) -> str:
    """Return the exact alias key without stemming or fuzzy matching."""
    return " ".join(unicodedata.normalize("NFKC", text).split()).casefold()


def build_alias_index(ingredients: Iterable[Ingredient]):
    aliases = {}
    for item in ingredients:
        for position, alias in enumerate((item.name, *item.synonyms)):
            key = normalize_name(alias)
            if key in aliases:
                previous_item, previous_is_synonym = aliases[key]
                if position > 0 or previous_is_synonym:
                    raise ValueError(f"duplicitné synonymum: {alias}")
                raise ValueError(f"duplicitný názov: {alias}")
            aliases[key] = (item, position > 0)
    return MappingProxyType({key: value[0] for key, value in aliases.items()})


def _validate_ingredient(item: Ingredient) -> None:
    if not item.id.strip():
        raise ValueError("ID suroviny nesmie byť prázdne")
    if not normalize_name(item.name):
        raise ValueError("názov suroviny nesmie byť prázdny")
    if any(not normalize_name(value) for value in item.synonyms):
        raise ValueError("synonymum suroviny nesmie byť prázdne")
    unknown_roles = item.roles - ALLOWED_ROLES
    if not item.roles or unknown_roles:
        raise ValueError(f"neznáma rola suroviny: {', '.join(sorted(unknown_roles))}")
    if DietTag.VEGAN in item.diet_tags and DietTag.VEGETARIAN not in item.diet_tags:
        raise ValueError("vegan surovina musí byť aj vegetarian")

    if not item.edible_ratio.is_finite() or not Decimal("0") < item.edible_ratio <= 1:
        raise ValueError("jedlý podiel musí byť v intervale (0, 1]")
    for value, label in (
        (item.grams_per_piece, "gramov na kus"),
        (item.density_g_per_ml, "hustota"),
    ):
        if value is not None and (not value.is_finite() or value <= 0):
            raise ValueError(f"{label} musí byť kladná hodnota")

    nutrition_values = (
        item.nutrition.kcal,
        item.nutrition.protein_g,
        item.nutrition.fat_g,
        item.nutrition.carbs_g,
    )
    if any(not value.is_finite() for value in nutrition_values):
        raise ValueError("výživové hodnoty musia byť konečné čísla")
    if any(value < 0 for value in nutrition_values[1:]):
        raise ValueError("výživové makrá nesmú byť záporné")
    if item.nutrition.kcal > 0 and all(value == 0 for value in nutrition_values[1:]):
        raise ValueError("výživové makrá nemôžu byť všetky nulové")
    zero_energy_mineral = item.roles == {"seasoning"} and all(
        value == 0 for value in nutrition_values
    )
    if item.nutrition.kcal <= 0 and not zero_energy_mineral:
        raise ValueError("výživová energia musí byť kladná")
    if not item.nutrition.source.strip():
        raise ValueError("zdroj výživových hodnôt nesmie byť prázdny")


class IngredientCatalog:
    def __init__(self, ingredients: Iterable[Ingredient]):
        values = tuple(ingredients)
        for item in values:
            _validate_ingredient(item)
        ids = [item.id for item in values]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicitné ID suroviny")
        self._values = values
        self._by_id = MappingProxyType({item.id: item for item in values})
        self._by_alias = build_alias_index(values)

    def all(self) -> tuple[Ingredient, ...]:
        return self._values

    def by_id(self, ingredient_id: str) -> Ingredient:
        return self._by_id[ingredient_id]

    def resolve(self, text: str) -> Ingredient | None:
        return self._by_alias.get(normalize_name(text))


def _decimal(value, label: str) -> Decimal:
    try:
        return Decimal(str(value))
    except (DecimalException, TypeError, ValueError) as exc:
        raise ValueError(f"{label} musí byť číslo") from exc


def _optional_decimal(value, label: str) -> Decimal | None:
    return None if value is None else _decimal(value, label)


def _ingredient_from_json(value: dict) -> Ingredient:
    nutrition = value["nutrition"]
    tags = frozenset(DietTag(tag) for tag in value["diet_tags"])
    if DietTag.VEGAN in tags and DietTag.VEGETARIAN not in tags:
        raise ValueError("vegan surovina musí byť aj vegetarian")
    return Ingredient(
        id=value["id"],
        name=value["name"],
        synonyms=tuple(value["synonyms"]),
        category=value["category"],
        roles=frozenset(value["roles"]),
        diet_tags=tags,
        allergens=tuple(value["allergens"]),
        edible_ratio=_decimal(value["edible_ratio"], "jedlý podiel"),
        grams_per_piece=_optional_decimal(value["grams_per_piece"], "gramov na kus"),
        density_g_per_ml=_optional_decimal(value["density_g_per_ml"], "hustota"),
        nutrition=NutritionPer100(
            kcal=_decimal(nutrition["kcal"], "výživová energia"),
            protein_g=_decimal(nutrition["protein_g"], "výživové bielkoviny"),
            fat_g=_decimal(nutrition["fat_g"], "výživové tuky"),
            carbs_g=_decimal(nutrition["carbs_g"], "výživové sacharidy"),
            source=nutrition["source"],
            verified_on=date.fromisoformat(nutrition["verified_on"]),
        ),
    )


def load_ingredient_catalog(path=None) -> IngredientCatalog:
    source_path = DEFAULT_CATALOG_PATH if path is None else Path(path)
    with source_path.open(encoding="utf-8") as stream:
        payload = json.load(stream)
    return IngredientCatalog(_ingredient_from_json(value) for value in payload["ingredients"])
