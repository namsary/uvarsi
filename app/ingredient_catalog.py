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
SUPPORTED_CATALOG_VERSION = 1
SUPPORTED_NUTRITION_BASIS = "per 100 g edible portion"
ALLOWED_ROLES = frozenset(
    {"protein", "starch", "vegetable", "aromatic", "fat", "seasoning", "dairy"}
)
CATALOG_KEYS = frozenset({"catalog_version", "nutrition_basis", "ingredients"})
INGREDIENT_KEYS = frozenset(
    {
        "id",
        "name",
        "synonyms",
        "category",
        "roles",
        "diet_tags",
        "allergens",
        "edible_ratio",
        "grams_per_piece",
        "density_g_per_ml",
        "nutrition",
    }
)
NUTRITION_KEYS = frozenset(
    {"kcal", "protein_g", "fat_g", "carbs_g", "source", "verified_on"}
)
PRODUCT_FAMILIES = (
    frozenset(("chickpeas", "chickpeas_canned")),
    frozenset(("beans", "beans_canned")),
    frozenset(("chicken_thigh", "chicken_thigh_meat")),
)
PRODUCT_FAMILY_SHARED_FORMS = MappingProxyType(
    {
        PRODUCT_FAMILIES[0]: frozenset(("cícer", "cíceru")),
        PRODUCT_FAMILIES[1]: frozenset(
            (
                "fazuľa",
                "červená fazuľa",
                "červené fazule",
                "červenej fazule",
                "červenú fazuľu",
            )
        ),
        PRODUCT_FAMILIES[2]: frozenset(
            ("kuracie stehno", "kuracie stehná", "kuracích stehien")
        ),
    }
)
PRODUCT_FAMILY_AMBIGUOUS_OFFER_FORMS = MappingProxyType(
    {
        PRODUCT_FAMILIES[0]: PRODUCT_FAMILY_SHARED_FORMS[PRODUCT_FAMILIES[0]],
        PRODUCT_FAMILIES[1]: PRODUCT_FAMILY_SHARED_FORMS[PRODUCT_FAMILIES[1]],
    }
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


def product_family(ingredient_id: str) -> frozenset[str] | None:
    return next(
        (family for family in PRODUCT_FAMILIES if ingredient_id in family),
        None,
    )


def product_family_shared_forms(ingredient_id: str) -> frozenset[str]:
    family = product_family(ingredient_id)
    if family is None:
        return frozenset()
    return PRODUCT_FAMILY_SHARED_FORMS[family]


def product_family_ambiguous_offer_forms(ingredient_id: str) -> frozenset[str]:
    family = product_family(ingredient_id)
    if family is None:
        return frozenset()
    return PRODUCT_FAMILY_AMBIGUOUS_OFFER_FORMS.get(family, frozenset())


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


def _json_object_without_duplicates(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicitný JSON kľúč: {key}")
        result[key] = value
    return result


def _load_strict_json(path: Path):
    with path.open(encoding="utf-8") as stream:
        return json.load(stream, object_pairs_hook=_json_object_without_duplicates)


def _object(value, label: str) -> dict:
    if type(value) is not dict:
        raise ValueError(f"{label} musí byť objekt")
    return value


def _exact_keys(value: dict, expected: frozenset[str], label: str) -> None:
    actual = set(value)
    if actual == expected:
        return
    missing = sorted(expected - actual)
    extra = sorted(actual - expected)
    details = []
    if missing:
        details.append("chýba " + ", ".join(missing))
    if extra:
        details.append("navyše " + ", ".join(extra))
    raise ValueError(f"neplatná schéma {label}: {'; '.join(details)}")


def _text(value, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} nesmie byť prázdny text")
    return value


def _texts(value, label: str) -> tuple[str, ...]:
    if type(value) is not list:
        raise ValueError(f"{label} musí byť zoznam")
    return tuple(_text(item, label) for item in value)


def _ingredient_from_json(value) -> Ingredient:
    payload = _object(value, "suroviny")
    _exact_keys(payload, INGREDIENT_KEYS, "suroviny")
    nutrition = _object(payload["nutrition"], "výživových údajov")
    _exact_keys(nutrition, NUTRITION_KEYS, "výživových údajov")
    tags = frozenset(
        DietTag(tag) for tag in _texts(payload["diet_tags"], "diet tags")
    )
    if DietTag.VEGAN in tags and DietTag.VEGETARIAN not in tags:
        raise ValueError("vegan surovina musí byť aj vegetarian")
    verified_on = _text(nutrition["verified_on"], "dátum overenia")
    try:
        verified_date = date.fromisoformat(verified_on)
    except ValueError as exc:
        raise ValueError("dátum overenia musí byť ISO dátum") from exc
    return Ingredient(
        id=_text(payload["id"], "ID suroviny"),
        name=_text(payload["name"], "názov suroviny"),
        synonyms=_texts(payload["synonyms"], "synonymá"),
        category=_text(payload["category"], "kategória suroviny"),
        roles=frozenset(_texts(payload["roles"], "roly suroviny")),
        diet_tags=tags,
        allergens=_texts(payload["allergens"], "alergény"),
        edible_ratio=_decimal(payload["edible_ratio"], "jedlý podiel"),
        grams_per_piece=_optional_decimal(payload["grams_per_piece"], "gramov na kus"),
        density_g_per_ml=_optional_decimal(payload["density_g_per_ml"], "hustota"),
        nutrition=NutritionPer100(
            kcal=_decimal(nutrition["kcal"], "výživová energia"),
            protein_g=_decimal(nutrition["protein_g"], "výživové bielkoviny"),
            fat_g=_decimal(nutrition["fat_g"], "výživové tuky"),
            carbs_g=_decimal(nutrition["carbs_g"], "výživové sacharidy"),
            source=_text(nutrition["source"], "zdroj výživových hodnôt"),
            verified_on=verified_date,
        ),
    )


def load_ingredient_catalog(path=None) -> IngredientCatalog:
    source_path = DEFAULT_CATALOG_PATH if path is None else Path(path)
    payload = _object(_load_strict_json(source_path), "katalógu")
    _exact_keys(payload, CATALOG_KEYS, "katalógu")
    if (
        type(payload["catalog_version"]) is not int
        or payload["catalog_version"] != SUPPORTED_CATALOG_VERSION
    ):
        raise ValueError(
            f"catalog_version musí byť podporovaná verzia {SUPPORTED_CATALOG_VERSION}"
        )
    if payload["nutrition_basis"] != SUPPORTED_NUTRITION_BASIS:
        raise ValueError(
            f"nutrition_basis musí byť presne {SUPPORTED_NUTRITION_BASIS!r}"
        )
    ingredients = payload["ingredients"]
    if type(ingredients) is not list:
        raise ValueError("ingredients musí byť zoznam")
    return IngredientCatalog(_ingredient_from_json(value) for value in ingredients)
