"""Deterministic, fail-closed release audit for the recipe library."""

from __future__ import annotations

from collections.abc import Sequence as SequenceABC
from collections import Counter, defaultdict
from dataclasses import dataclass
from decimal import Decimal
from itertools import product
import re
import unicodedata
from typing import Iterable, Sequence

from .ingredient_catalog import IngredientCatalog, load_ingredient_catalog
from .recipe_catalog import (
    ALLOWED_METHODS,
    ALLOWED_MODES,
    ALLOWED_UNITS,
    ALLOWED_USES,
    PANTRY_BASIC_NAMES,
    SLOT_KEY_PATTERN,
    IngredientSlot,
    InstructionTemplate,
    RecipeCatalog,
    RecipeTemplate,
    load_recipe_catalog,
)
from .recipe_matcher import RecipeCandidate, SlotSelection
from .recipe_renderer import RenderedMeal, render_meal


_FORBIDDEN_LANGUAGE = (
    re.compile(r"\bscedok\w*\b"),
    re.compile(r"\breziek\w*\b"),
    re.compile(r"\brezky\w*\b"),
)
_DECIMAL_GRAMS = re.compile(r"(?<!\w)\d+[.,]\d+\s*g\b", re.IGNORECASE)
_SERVING_ACTION = re.compile(r"\b(?:rozdel\w*|podavaj\w*|serviruj\w*|naloz\w*)\b")
_GENERIC_ONLY = re.compile(
    r"^(?:(?:priprav|dochut|premiesaj|podavaj|serviruj|uvar|opec|dokonc)"
    r"(?:\s+(?:vsetko|suroviny|jedlo|spolu|podla|chuti))*|"
    r"(?:vsetko|suroviny|jedlo)(?:\s+spolu)?\s+"
    r"(?:priprav|dochut|premiesaj|podavaj|serviruj|uvar|opec|dokonc))[.!]?$"
)
_SEASONING_ROOTS = {
    "black_pepper": ("cierne koren", "ciernym koren", "cierneho koren"),
    "curry_powder": ("kari",),
    "garlic": ("cesnak",),
    "oregano": ("oregan",),
    "paprika_powder": ("mleta paprik", "mletou paprik", "mletej paprik"),
    "salt": ("sol",),
}
_MODE_FLOORS = {
    "standard": 50,
    "high_protein": 24,
    "vegetarian": 20,
    "vegan": 12,
}
_MINIMUM_ACTIVE_RECIPES = 60
_MINIMUM_MODE_FAMILIES = 3
_MINIMUM_MODE_METHODS = 3

# The launch catalog schema permits at most four slots with at most three
# candidates in each slot. The audit derives its hard Cartesian ceiling from
# those two schema limits instead of trusting unbounded catalog input.
_LAUNCH_MAX_SLOTS = 4
_LAUNCH_MAX_CANDIDATES_PER_SLOT = 3
_MAX_AUDITED_VARIANTS = _LAUNCH_MAX_CANDIDATES_PER_SLOT**_LAUNCH_MAX_SLOTS

_ACTION_PATTERNS = (
    ("rinse", re.compile(r"\b(?:preplach\w*|oplach\w*|sced\w*)\b")),
    ("cut", re.compile(r"\bnakraj\w*\b")),
    ("boil", re.compile(r"\b(?:uvar\w*|var)\b")),
    ("simmer", re.compile(r"\bdus\w*\b")),
    ("fry", re.compile(r"\b(?:opek\w*|opraz\w*|restuj\w*)\b")),
    ("bake", re.compile(r"\b(?:pec\w*|zapec\w*)\b")),
    ("blend", re.compile(r"\brozmix\w*\b")),
    ("add", re.compile(r"\b(?:pridaj\w*|prisyp\w*|prilej\w*|vloz\w*)\b")),
    ("mix", re.compile(r"\b(?:premiesaj\w*|spoj\w*)\b")),
    ("rest", re.compile(r"\bnechaj\w*\b")),
    ("reduce", re.compile(r"\bredukuj\w*\b")),
    ("thicken", re.compile(r"\bzahusti\w*\b")),
    ("marinate", re.compile(r"\bmarinuj\w*\b")),
    ("coat", re.compile(r"\bobal\w*\b")),
    ("store", re.compile(r"\b(?:uchovaj\w*|ochlad\w*)\b")),
    ("serve", _SERVING_ACTION),
)
_NAME_STOPWORDS = frozenset(
    {
        "a",
        "chutne",
        "domace",
        "jednoduche",
        "na",
        "pre",
        "rychle",
        "s",
        "so",
        "vynikajuce",
        "z",
        "zo",
    }
)


@dataclass(frozen=True)
class LibraryAudit:
    """Stable audit result used by tests and the release CLI."""

    active_recipes: int
    mode_counts: tuple[tuple[str, int], ...]
    method_counts: tuple[tuple[str, int], ...]
    family_counts: tuple[tuple[str, int], ...]
    errors: tuple[str, ...]

    def coverage_lines(self) -> tuple[str, ...]:
        lines = [f"recipes.active={self.active_recipes}"]
        lines.extend(f"modes.{name}={count}" for name, count in self.mode_counts)
        lines.extend(f"methods.{name}={count}" for name, count in self.method_counts)
        lines.extend(f"families.{name}={count}" for name, count in self.family_counts)
        lines.extend(f"error.{code}=1" for code in self.errors)
        lines.append(f"errors={len(self.errors)}")
        return tuple(lines)


def _fold(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value).casefold()
    return "".join(character for character in normalized if not unicodedata.combining(character))


def _counts(values: Iterable[str]) -> tuple[tuple[str, int], ...]:
    return tuple(sorted(Counter(values).items()))


def _is_sequence(value: object) -> bool:
    return isinstance(value, SequenceABC) and not isinstance(value, (str, bytes))


def _is_text(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _is_positive_decimal(value: object) -> bool:
    return (
        isinstance(value, Decimal)
        and value.is_finite()
        and value > 0
    )


def _is_valid_slot(ingredients: IngredientCatalog, value: object) -> bool:
    if not isinstance(value, IngredientSlot):
        return False
    if not all(
        (
            _is_text(value.key),
            SLOT_KEY_PATTERN.fullmatch(value.key) is not None,
            _is_text(value.role),
            _is_sequence(value.candidates),
            bool(value.candidates),
            all(_is_text(candidate) for candidate in value.candidates),
            len(value.candidates) == len(set(value.candidates)),
            _is_positive_decimal(value.amount_per_adult),
            _is_positive_decimal(value.child_factor),
            type(value.required) is bool,
            value.unit in ALLOWED_UNITS,
            value.use in ALLOWED_USES,
            value.cut is None or _is_text(value.cut),
        )
    ):
        return False
    try:
        candidates = tuple(ingredients.by_id(candidate) for candidate in value.candidates)
        if not all(value.role in ingredient.roles for ingredient in candidates):
            return False
        if value.unit == "piece" and any(
            ingredient.grams_per_piece is None for ingredient in candidates
        ):
            return False
        if value.unit == "ml" and any(
            ingredient.density_g_per_ml is None for ingredient in candidates
        ):
            return False
        return True
    except Exception:
        return False


def _is_valid_recipe(ingredients: IngredientCatalog, value: object) -> bool:
    """Validate public audit input before any summary or render access."""
    if not isinstance(value, RecipeTemplate):
        return False
    try:
        if not all(
            (
                _is_text(value.id),
                type(value.version) is int and value.version > 0,
                type(value.active) is bool,
                _is_text(value.name_template),
                _is_text(value.family),
                value.method in ALLOWED_METHODS,
                type(value.minutes) is int and value.minutes > 0,
                isinstance(value.modes, frozenset),
                bool(value.modes),
                value.modes <= ALLOWED_MODES,
                _is_sequence(value.equipment),
                all(_is_text(item) for item in value.equipment),
                _is_sequence(value.slots),
                bool(value.slots),
                all(_is_valid_slot(ingredients, slot) for slot in value.slots),
                _is_sequence(value.pantry_basics),
                all(_is_text(item) for item in value.pantry_basics),
                _is_sequence(value.instructions),
                len(value.instructions) >= 3,
                all(
                    isinstance(step, InstructionTemplate) and _is_text(step.text)
                    for step in value.instructions
                ),
            )
        ):
            return False
        slot_keys = tuple(slot.key for slot in value.slots)
        if len(slot_keys) != len(set(slot_keys)):
            return False
        if len(value.pantry_basics) != len(set(value.pantry_basics)):
            return False
        for ingredient_id in value.pantry_basics:
            if ingredient_id not in PANTRY_BASIC_NAMES:
                ingredients.by_id(ingredient_id)
        _fingerprint(ingredients, value)
        return True
    except Exception:
        return False


def _collect_valid_recipes(
    ingredients: IngredientCatalog,
    values: RecipeCatalog | Iterable[RecipeTemplate],
    errors: set[str],
) -> tuple[RecipeTemplate, ...]:
    try:
        source = values.all() if isinstance(values, RecipeCatalog) else values
        iterator = iter(source)
    except Exception:
        errors.add("invalid_recipe")
        return ()

    valid: list[RecipeTemplate] = []
    while True:
        try:
            value = next(iterator)
        except StopIteration:
            break
        except Exception:
            errors.add("invalid_recipe")
            break
        if not _is_valid_recipe(ingredients, value):
            errors.add("invalid_recipe")
            continue
        valid.append(value)

    id_counts = Counter(recipe.id for recipe in valid)
    if any(count > 1 for count in id_counts.values()):
        errors.add("invalid_recipe")
        valid = [recipe for recipe in valid if id_counts[recipe.id] == 1]
    return tuple(sorted(valid, key=lambda recipe: recipe.id))


def _fingerprint(
    ingredients: IngredientCatalog,
    recipe: RecipeTemplate,
) -> tuple[str, str, tuple[str, ...], tuple[str, ...]]:
    required = tuple(slot for slot in recipe.slots if slot.required)
    roles = tuple(sorted(slot.role for slot in required))
    categories = tuple(
        sorted(ingredients.by_id(slot.candidates[0]).category for slot in required)
    )
    return recipe.family, recipe.method, roles, categories


def _seasoning_patterns(ingredients: IngredientCatalog) -> tuple[re.Pattern[str], ...]:
    roots: set[str] = set()
    for seasoning in ingredients.all():
        if "seasoning" not in seasoning.roles:
            continue
        roots.update(_SEASONING_ROOTS.get(seasoning.id, ()))
        roots.update(_fold(value) for value in (seasoning.name, *seasoning.synonyms))
    return tuple(
        re.compile(rf"\b{re.escape(root)}\w*\b")
        for root in sorted(roots, key=lambda item: (-len(item), item))
        if root
    )


def _ingredient_roots(ingredients: IngredientCatalog) -> frozenset[str]:
    roots: set[str] = set()
    for ingredient in ingredients.all():
        if "seasoning" in ingredient.roles:
            continue
        for value in (ingredient.name, *ingredient.synonyms):
            for token in re.findall(r"\w+", _fold(value)):
                if len(token) >= 4:
                    roots.add(token[: max(3, min(4, len(token) - 1))])
    return frozenset(roots)


def _normalized_name(
    ingredients: IngredientCatalog,
    recipe: RecipeTemplate,
) -> tuple[str, ...]:
    text = _fold(recipe.name_template)
    text = re.sub(r"\{[^{}]+\}", " ingredient ", text)
    text = re.sub(r"\d+(?:[.,]\d+)?", " quantity ", text)
    for pattern in _seasoning_patterns(ingredients):
        text = pattern.sub(" ", text)
    ingredient_roots = _ingredient_roots(ingredients)
    tokens = []
    for token in re.findall(r"\w+", text):
        if token in _NAME_STOPWORDS:
            continue
        if any(token.startswith(root) for root in ingredient_roots):
            tokens.append("ingredient")
        else:
            tokens.append(token)
    collapsed = []
    for token in tokens:
        if token != "ingredient" or not collapsed or collapsed[-1] != token:
            collapsed.append(token)
    return tuple(collapsed)


def _process_structure(recipe: RecipeTemplate) -> tuple[tuple[str, ...], ...]:
    structure = []
    for step in recipe.instructions:
        folded = _fold(step.text)
        actions = []
        for label, pattern in _ACTION_PATTERNS:
            actions.extend((match.start(), label) for match in pattern.finditer(folded))
        ordered = tuple(label for _, label in sorted(actions))
        if ordered:
            structure.append(ordered)
    return tuple(structure)


def _audit_duplicates(
    ingredients: IngredientCatalog,
    recipes: Sequence[RecipeTemplate],
    errors: set[str],
) -> None:
    groups: dict[tuple[object, ...], list[RecipeTemplate]] = defaultdict(list)
    try:
        for recipe in recipes:
            groups[_fingerprint(ingredients, recipe)].append(recipe)
    except (AttributeError, IndexError, KeyError, TypeError, ValueError):
        errors.add("invalid_recipe")
        return

    for group in groups.values():
        if len(group) <= 2:
            continue
        normalized_names = {_normalized_name(ingredients, recipe) for recipe in group}
        process_structures = {_process_structure(recipe) for recipe in group}
        if (
            () in normalized_names
            or len(normalized_names) != len(group)
            or () in process_structures
            or len(process_structures) != len(group)
        ):
            errors.add("duplicate_fingerprint")


def _raw_language_errors(recipe: RecipeTemplate) -> set[str]:
    errors: set[str] = set()
    texts = (recipe.name_template, *(step.text for step in recipe.instructions))
    joined = " ".join(texts)
    folded = _fold(joined)

    if any(pattern.search(folded) for pattern in _FORBIDDEN_LANGUAGE):
        errors.add("forbidden_language")
    if "{" in joined or "}" in joined:
        # Valid placeholders are resolved during rendering; malformed or leftover
        # braces are classified there. A brace that cannot be parsed is rejected now.
        try:
            fields = tuple(field for text in texts for field in _safe_fields(text))
            allowed_fields = {"portions"}
            allowed_fields.update(
                f"{slot.key}.{attribute}"
                for slot in recipe.slots
                for attribute in ("name", "amount", "cut")
            )
            if any(field not in allowed_fields for field in fields):
                errors.add("unresolved_braces")
        except ValueError:
            errors.add("unresolved_braces")
    if _DECIMAL_GRAMS.search(joined):
        errors.add("decimal_grams")
    if not _SERVING_ACTION.search(folded):
        errors.add("missing_serving_action")
    if any(_GENERIC_ONLY.fullmatch(_fold(step.text).strip()) for step in recipe.instructions):
        errors.add("generic_only_step")
    return errors


def _safe_fields(value: str):
    """Yield format fields while converting malformed braces to ValueError."""
    from string import Formatter

    try:
        for _, field, _, _ in Formatter().parse(value):
            if field is not None:
                yield field
    except ValueError as exc:
        raise ValueError("unresolved brace") from exc


def _rendered_language_errors(meal: RenderedMeal) -> set[str]:
    errors: set[str] = set()
    joined = " ".join((meal.name, *meal.instructions))
    folded = _fold(joined)

    if any(pattern.search(folded) for pattern in _FORBIDDEN_LANGUAGE):
        errors.add("forbidden_language")
    if "{" in joined or "}" in joined:
        errors.add("unresolved_braces")
    if _DECIMAL_GRAMS.search(joined):
        errors.add("decimal_grams")
    if not _SERVING_ACTION.search(folded):
        errors.add("missing_serving_action")
    if any(_GENERIC_ONLY.fullmatch(_fold(step).strip()) for step in meal.instructions):
        errors.add("generic_only_step")
    return errors


def _seasoning_errors(
    ingredients: IngredientCatalog,
    recipe: RecipeTemplate,
    meal: RenderedMeal,
) -> set[str]:
    errors: set[str] = set()
    selected_ids = {item.ingredient.id for item in meal.ingredients}
    pantry_ids = set(recipe.pantry_basics)
    declared = selected_ids | pantry_ids
    rendered_pantry = {_fold(name) for name in meal.pantry_basics}
    text = _fold(" ".join(meal.instructions))

    seasonings = tuple(
        item for item in ingredients.all() if "seasoning" in item.roles
    )
    for seasoning in seasonings:
        roots = _SEASONING_ROOTS.get(
            seasoning.id,
            tuple(_fold(value) for value in (seasoning.name, *seasoning.synonyms)),
        )
        if any(root in text for root in roots) and seasoning.id not in declared:
            errors.add("undeclared_seasoning")

    for seasoning_id in pantry_ids:
        try:
            seasoning = ingredients.by_id(seasoning_id)
        except KeyError:
            continue
        if "seasoning" in seasoning.roles and _fold(seasoning.name) not in rendered_pantry:
            errors.add("missing_seasoning_declaration")
    return errors


def _raw_seasoning_errors(
    ingredients: IngredientCatalog,
    recipe: RecipeTemplate,
) -> set[str]:
    declared = {
        ingredient_id
        for slot in recipe.slots
        for ingredient_id in slot.candidates
    } | set(recipe.pantry_basics)
    text = _fold(" ".join(step.text for step in recipe.instructions))
    for seasoning in ingredients.all():
        if "seasoning" not in seasoning.roles:
            continue
        roots = _SEASONING_ROOTS.get(
            seasoning.id,
            tuple(_fold(value) for value in (seasoning.name, *seasoning.synonyms)),
        )
        if any(root in text for root in roots) and seasoning.id not in declared:
            return {"undeclared_seasoning"}
    return set()


def _variant_count_within_launch_schema(recipe: RecipeTemplate) -> int | None:
    if len(recipe.slots) > _LAUNCH_MAX_SLOTS:
        return None
    count = 1
    for slot in recipe.slots:
        candidate_count = len(slot.candidates)
        if candidate_count > _LAUNCH_MAX_CANDIDATES_PER_SLOT:
            return None
        count *= candidate_count
        if count > _MAX_AUDITED_VARIANTS:
            return None
    return count


def _candidate_variants(
    ingredients: IngredientCatalog,
    recipe: RecipeTemplate,
):
    candidate_groups = tuple(tuple(slot.candidates) for slot in recipe.slots)
    if not candidate_groups or any(not group for group in candidate_groups):
        raise ValueError("recipe has no candidate variants")
    for candidate_ids in product(*candidate_groups):
        selections = tuple(
            SlotSelection(
                slot=slot,
                ingredient=ingredients.by_id(ingredient_id),
                offer=None,
                pantry=None,
            )
            for slot, ingredient_id in zip(recipe.slots, candidate_ids, strict=True)
        )
        yield RecipeCandidate(
            template=recipe,
            selections=selections,
            score=Decimal("0"),
            key=f"library-gate:{recipe.id}:{'+'.join(candidate_ids)}",
        )


def _audit_recipe(
    ingredients: IngredientCatalog,
    recipe: RecipeTemplate,
    errors: set[str],
) -> None:
    if _variant_count_within_launch_schema(recipe) is None:
        errors.add("variant_limit_exceeded")
        return
    try:
        errors.update(_raw_language_errors(recipe))
        errors.update(_raw_seasoning_errors(ingredients, recipe))
    except (AttributeError, TypeError, ValueError):
        errors.add("invalid_recipe")
        return

    try:
        candidates = _candidate_variants(ingredients, recipe)
        rendered_any = False
        for candidate in candidates:
            rendered_any = True
            try:
                meal = render_meal(candidate, adults=1, children=0, covered_days=1)
            except (ArithmeticError, KeyError, TypeError, ValueError):
                errors.add("render_failure")
                continue
            errors.update(_rendered_language_errors(meal))
            errors.update(_seasoning_errors(ingredients, recipe, meal))
            if (
                "high_protein" in recipe.modes
                and meal.nutrition.serving.protein_g < Decimal("30")
            ):
                errors.add("high_protein_below_30g")
        if not rendered_any:
            errors.add("invalid_recipe")
    except (AttributeError, KeyError, TypeError, ValueError):
        errors.add("invalid_recipe")


def _audit_content_floors(
    active: Sequence[RecipeTemplate],
    errors: set[str],
) -> None:
    if len(active) < _MINIMUM_ACTIVE_RECIPES:
        errors.add(f"total_below_{_MINIMUM_ACTIVE_RECIPES}")
    for mode, floor in _MODE_FLOORS.items():
        eligible = tuple(recipe for recipe in active if mode in recipe.modes)
        if len(eligible) < floor:
            errors.add(f"mode_{mode}_below_{floor}")
        if len({recipe.family for recipe in eligible}) < _MINIMUM_MODE_FAMILIES:
            errors.add(f"mode_{mode}_families_below_{_MINIMUM_MODE_FAMILIES}")
        if len({recipe.method for recipe in eligible}) < _MINIMUM_MODE_METHODS:
            errors.add(f"mode_{mode}_methods_below_{_MINIMUM_MODE_METHODS}")


def audit_library(
    ingredients: IngredientCatalog,
    recipes: RecipeCatalog | Iterable[RecipeTemplate],
) -> LibraryAudit:
    """Audit every active recipe variant and return stable release evidence."""
    errors: set[str] = set()
    valid = _collect_valid_recipes(ingredients, recipes, errors)
    active = tuple(recipe for recipe in valid if recipe.active)

    _audit_content_floors(active, errors)
    _audit_duplicates(ingredients, active, errors)
    for recipe in active:
        _audit_recipe(ingredients, recipe, errors)

    return LibraryAudit(
        active_recipes=len(active),
        mode_counts=_counts(mode for recipe in active for mode in recipe.modes),
        method_counts=_counts(recipe.method for recipe in active),
        family_counts=_counts(recipe.family for recipe in active),
        errors=tuple(sorted(errors)),
    )


def main() -> int:
    """Print stable coverage and return a shell-friendly release status."""
    try:
        ingredients = load_ingredient_catalog()
        recipes = load_recipe_catalog(ingredients)
        audit = audit_library(ingredients, recipes)
    except Exception:  # CLI must fail closed even for corrupt release input.
        print("recipes.active=0")
        print("error.catalog_load=1")
        print("errors=1")
        return 1

    for line in audit.coverage_lines():
        print(line)
    return 0 if not audit.errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
