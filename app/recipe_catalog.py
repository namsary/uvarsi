"""Versioned, fail-closed recipe template catalog."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, DecimalException
import json
from pathlib import Path
import re
from string import Formatter
from typing import Iterable, Literal, Sequence

if __package__:
    from .ingredient_catalog import ALLOWED_ROLES, DietTag, IngredientCatalog
else:
    from ingredient_catalog import ALLOWED_ROLES, DietTag, IngredientCatalog


DEFAULT_RECIPE_ROOT = Path(__file__).with_name("catalog") / "recipes"
ALLOWED_MODES = frozenset(
    {"standard", "high_protein", "vegetarian", "vegan"}
)
ALLOWED_METHODS = frozenset({"pan", "oven", "pot", "one_pot", "salad", "soup"})
ALLOWED_UNITS = frozenset({"g", "ml", "piece"})
ALLOWED_USES = frozenset({"main", "addition"})
ALLOWED_PLACEHOLDER_ATTRIBUTES = frozenset({"name", "amount", "cut"})
SLOT_KEY_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")


@dataclass(frozen=True)
class IngredientSlot:
    key: str
    role: str
    candidates: Sequence[str]
    amount_per_adult: Decimal
    unit: Literal["g", "ml", "piece"]
    child_factor: Decimal
    required: bool
    use: Literal["main", "addition"]
    cut: str | None


@dataclass(frozen=True)
class InstructionTemplate:
    text: str


@dataclass(frozen=True)
class RecipeTemplate:
    id: str
    version: int
    active: bool
    name_template: str
    family: str
    method: str
    minutes: int
    modes: frozenset[str]
    equipment: Sequence[str]
    slots: Sequence[IngredientSlot]
    pantry_basics: Sequence[str]
    instructions: Sequence[InstructionTemplate]


class RecipeCatalog:
    def __init__(self, version: int, recipes: Iterable[RecipeTemplate]):
        values = tuple(recipes)
        ids = [recipe.id for recipe in values]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicitné ID receptu")
        self.version = version
        self._values = values

    def all(self) -> tuple[RecipeTemplate, ...]:
        return self._values


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


def _exact_keys(value: dict, expected: set[str], label: str) -> None:
    actual = set(value)
    if actual != expected:
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


def _texts(value, label: str, *, allow_empty: bool = True) -> tuple[str, ...]:
    if type(value) is not list:
        raise ValueError(f"{label} musí byť zoznam")
    result = tuple(_text(item, label) for item in value)
    if not allow_empty and not result:
        raise ValueError(f"{label} nesmie byť prázdny")
    return result


def _positive_int(value, label: str) -> int:
    if type(value) is not int:
        raise ValueError(f"{label} musí byť celé číslo")
    if value <= 0:
        raise ValueError(f"{label} musí byť kladná hodnota")
    return value


def _positive_decimal(value, label: str) -> Decimal:
    if isinstance(value, bool):
        raise ValueError(f"{label} musí byť číslo")
    try:
        result = Decimal(str(value))
    except (DecimalException, TypeError, ValueError) as exc:
        raise ValueError(f"{label} musí byť číslo") from exc
    if not result.is_finite() or result <= 0:
        raise ValueError(f"{label} musí byť kladná konečná hodnota")
    return result


def _ingredient(ingredient_catalog: IngredientCatalog, ingredient_id: str):
    try:
        return ingredient_catalog.by_id(ingredient_id)
    except KeyError as exc:
        raise ValueError(f"neznáma surovina: {ingredient_id}") from exc


def _slot_from_json(value, ingredient_catalog: IngredientCatalog) -> IngredientSlot:
    payload = _object(value, "pozície")
    _exact_keys(
        payload,
        {
            "key",
            "role",
            "candidates",
            "amount_per_adult",
            "unit",
            "child_factor",
            "required",
            "use",
            "cut",
        },
        "pozície",
    )
    key = _text(payload["key"], "kľúč pozície")
    if SLOT_KEY_PATTERN.fullmatch(key) is None:
        raise ValueError(f"neplatný kľúč pozície: {key}")
    role = _text(payload["role"], "rola pozície")
    if role not in ALLOWED_ROLES:
        raise ValueError(f"neznáma rola pozície: {role}")
    candidates = _texts(payload["candidates"], "kandidáti pozície", allow_empty=False)
    if len(candidates) != len(set(candidates)):
        raise ValueError(f"duplicitná surovina v pozícii: {key}")
    ingredients = tuple(_ingredient(ingredient_catalog, item) for item in candidates)
    for ingredient in ingredients:
        if role not in ingredient.roles:
            raise ValueError(
                f"rola {role} nie je povolená pre surovinu {ingredient.id}"
            )

    unit = _text(payload["unit"], "jednotka pozície")
    if unit not in ALLOWED_UNITS:
        raise ValueError(f"neznáma jednotka pozície: {unit}")
    if unit == "piece":
        missing = [item.id for item in ingredients if item.grams_per_piece is None]
        if missing:
            raise ValueError(
                "surovina pre jednotku piece nemá gramov na kus: "
                + ", ".join(missing)
            )
    if unit == "ml":
        missing = [item.id for item in ingredients if item.density_g_per_ml is None]
        if missing:
            raise ValueError(
                "surovina pre jednotku ml nemá hustota g/ml: " + ", ".join(missing)
            )

    required = payload["required"]
    if type(required) is not bool:
        raise ValueError("povinnosť pozície musí byť boolean")
    use = _text(payload["use"], "použitie pozície")
    if use not in ALLOWED_USES:
        raise ValueError(f"neznáme použitie pozície: {use}")
    cut_value = payload["cut"]
    cut = None if cut_value is None else _text(cut_value, "krájanie pozície")

    return IngredientSlot(
        key=key,
        role=role,
        candidates=candidates,
        amount_per_adult=_positive_decimal(
            payload["amount_per_adult"], "množstvo na dospelého"
        ),
        unit=unit,
        child_factor=_positive_decimal(payload["child_factor"], "detský koeficient"),
        required=required,
        use=use,
        cut=cut,
    )


def _validate_placeholders(
    templates: Sequence[str], slot_keys: frozenset[str]
) -> frozenset[tuple[str, str]]:
    references: set[tuple[str, str]] = set()
    for template in templates:
        try:
            parsed = Formatter().parse(template)
            for _, field, format_spec, conversion in parsed:
                if field is None:
                    continue
                if format_spec or conversion:
                    raise ValueError(f"nepovolený placeholder: {field}")
                if field == "portions":
                    continue
                parts = field.split(".")
                if len(parts) != 2 or parts[1] not in ALLOWED_PLACEHOLDER_ATTRIBUTES:
                    raise ValueError(f"nepovolený placeholder: {field}")
                if parts[0] not in slot_keys:
                    raise ValueError(f"neznáma pozícia v placeholderi: {parts[0]}")
                references.add((parts[0], parts[1]))
        except ValueError as exc:
            if str(exc).startswith(("nepovolený placeholder", "neznáma pozícia")):
                raise
            raise ValueError(f"neplatný placeholder v šablóne: {template}") from exc
    return frozenset(references)


def _validate_diets(
    modes: frozenset[str], ingredient_ids: Sequence[str], ingredient_catalog
) -> None:
    ingredients = tuple(
        _ingredient(ingredient_catalog, ingredient_id)
        for ingredient_id in ingredient_ids
    )
    if "vegan" in modes:
        invalid = [
            ingredient.id
            for ingredient in ingredients
            if DietTag.VEGAN not in ingredient.diet_tags
        ]
        if invalid:
            raise ValueError("vegan recept obsahuje: " + ", ".join(invalid))
    if "vegetarian" in modes:
        invalid = [
            ingredient.id
            for ingredient in ingredients
            if DietTag.VEGETARIAN not in ingredient.diet_tags
        ]
        if invalid:
            raise ValueError("vegetarian recept obsahuje: " + ", ".join(invalid))


def _recipe_from_json(value, ingredient_catalog: IngredientCatalog) -> RecipeTemplate:
    payload = _object(value, "receptu")
    _exact_keys(
        payload,
        {
            "id",
            "version",
            "active",
            "name_template",
            "family",
            "method",
            "minutes",
            "modes",
            "equipment",
            "slots",
            "pantry_basics",
            "instructions",
        },
        "receptu",
    )
    recipe_id = _text(payload["id"], "ID receptu")
    version = _positive_int(payload["version"], "verzia receptu")
    active = payload["active"]
    if type(active) is not bool:
        raise ValueError("active musí byť boolean")
    name_template = _text(payload["name_template"], "šablóna názvu")
    family = _text(payload["family"], "rodina receptu")
    method = _text(payload["method"], "spôsob prípravy")
    if method not in ALLOWED_METHODS:
        raise ValueError(f"neznámy spôsob prípravy: {method}")
    minutes = _positive_int(payload["minutes"], "čas prípravy")

    modes = frozenset(_texts(payload["modes"], "režimy", allow_empty=False))
    unknown_modes = modes - ALLOWED_MODES
    if unknown_modes:
        raise ValueError(f"neznámy režim: {', '.join(sorted(unknown_modes))}")
    equipment = _texts(payload["equipment"], "vybavenie")

    slots_value = payload["slots"]
    if type(slots_value) is not list or not slots_value:
        raise ValueError("pozície musia byť neprázdny zoznam")
    slots = tuple(_slot_from_json(item, ingredient_catalog) for item in slots_value)
    slot_keys = [slot.key for slot in slots]
    if len(slot_keys) != len(set(slot_keys)):
        raise ValueError("duplicitná pozícia receptu")

    pantry_basics = _texts(payload["pantry_basics"], "základné suroviny")
    if len(pantry_basics) != len(set(pantry_basics)):
        raise ValueError("duplicitná základná surovina")
    for ingredient_id in pantry_basics:
        _ingredient(ingredient_catalog, ingredient_id)

    instructions_value = payload["instructions"]
    if type(instructions_value) is not list or len(instructions_value) < 3:
        raise ValueError("recept musí mať najmenej tri kroky")
    instructions = []
    for value in instructions_value:
        instruction = _object(value, "kroku")
        _exact_keys(instruction, {"text"}, "kroku")
        instructions.append(InstructionTemplate(_text(instruction["text"], "krok")))
    instructions_tuple = tuple(instructions)

    references = _validate_placeholders(
        (name_template, *(item.text for item in instructions_tuple)),
        frozenset(slot_keys),
    )
    for slot in slots:
        if slot.required and (slot.key, "name") not in references:
            raise ValueError(f"povinná pozícia nemá placeholder názvu: {slot.key}")

    all_ingredient_ids = tuple(
        ingredient_id
        for slot in slots
        for ingredient_id in slot.candidates
    ) + pantry_basics
    _validate_diets(modes, all_ingredient_ids, ingredient_catalog)

    return RecipeTemplate(
        id=recipe_id,
        version=version,
        active=active,
        name_template=name_template,
        family=family,
        method=method,
        minutes=minutes,
        modes=modes,
        equipment=equipment,
        slots=slots,
        pantry_basics=pantry_basics,
        instructions=instructions_tuple,
    )


def _load_manifest(root: Path) -> int:
    path = root / "manifest.json"
    try:
        payload = _object(_load_strict_json(path), "manifestu")
    except FileNotFoundError as exc:
        raise ValueError("chýba manifest receptovej knižnice") from exc
    _exact_keys(payload, {"library_version"}, "manifestu")
    return _positive_int(payload["library_version"], "library_version")


def load_recipe_catalog(
    ingredient_catalog: IngredientCatalog,
    root=None,
    include_inactive: bool = False,
) -> RecipeCatalog:
    source_root = DEFAULT_RECIPE_ROOT if root is None else Path(root)
    version = _load_manifest(source_root)
    recipes = []
    for path in sorted(source_root.glob("*.json")):
        if path.name == "manifest.json":
            continue
        payload = _object(_load_strict_json(path), f"súboru {path.name}")
        _exact_keys(payload, {"recipes"}, f"súboru {path.name}")
        values = payload["recipes"]
        if type(values) is not list:
            raise ValueError(f"recipes v {path.name} musí byť zoznam")
        recipes.extend(_recipe_from_json(value, ingredient_catalog) for value in values)

    validated = RecipeCatalog(version, recipes)
    if include_inactive:
        return validated
    return RecipeCatalog(version, (recipe for recipe in recipes if recipe.active))
