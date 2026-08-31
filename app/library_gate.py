"""Deterministic, fail-closed release audit for the recipe library."""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from decimal import Decimal
from itertools import product
import re
import unicodedata
from typing import Iterable, Sequence

from .ingredient_catalog import IngredientCatalog, load_ingredient_catalog
from .recipe_catalog import RecipeCatalog, RecipeTemplate, load_recipe_catalog
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
_DISTINCTION_MARKERS = (
    "bolonsk",
    "cili",
    "cesnak",
    "fasi",
    "frittat",
    "gulas",
    "jogurt",
    "kari",
    "kokos",
    "oregano",
    "paprik",
    "paradajk",
    "pesto",
    "polievk",
    "rag",
    "salat",
)
_SEASONING_ROOTS = {
    "black_pepper": ("cierne koren", "ciernym koren", "cierneho koren"),
    "curry_powder": ("kari",),
    "garlic": ("cesnak",),
    "oregano": ("oregan",),
    "paprika_powder": ("mleta paprik", "mletou paprik", "mletej paprik"),
    "salt": ("sol",),
}


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


def _recipes(values: RecipeCatalog | Iterable[RecipeTemplate]) -> tuple[RecipeTemplate, ...]:
    if isinstance(values, RecipeCatalog):
        return values.all()
    return tuple(values)


def _counts(values: Iterable[str]) -> tuple[tuple[str, int], ...]:
    return tuple(sorted(Counter(values).items()))


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


def _instruction_snapshot(recipe: RecipeTemplate) -> tuple[str, ...]:
    return tuple(" ".join(_fold(step.text).split()) for step in recipe.instructions)


def _named_distinction(recipe: RecipeTemplate) -> tuple[str, ...]:
    text = _fold(
        " ".join(
            (recipe.name_template, *(step.text for step in recipe.instructions))
        )
    )
    return tuple(marker for marker in _DISTINCTION_MARKERS if marker in text)


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
        snapshots = {_instruction_snapshot(recipe) for recipe in group}
        distinctions = {_named_distinction(recipe) for recipe in group}
        if (
            len(snapshots) != len(group)
            or () in distinctions
            or len(distinctions) != len(group)
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


def audit_library(
    ingredients: IngredientCatalog,
    recipes: RecipeCatalog | Iterable[RecipeTemplate],
) -> LibraryAudit:
    """Audit every active recipe variant and return stable release evidence."""
    errors: set[str] = set()
    try:
        active = tuple(
            sorted(
                (item for item in _recipes(recipes) if item.active),
                key=lambda item: item.id,
            )
        )
    except (AttributeError, TypeError, ValueError):
        active = ()
        errors.add("invalid_recipe")

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
