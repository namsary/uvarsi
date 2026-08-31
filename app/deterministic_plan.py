"""Deterministic orchestration of one complete seven-day meal plan."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from fractions import Fraction
from typing import Literal, Mapping, Sequence

from .ingredient_catalog import Ingredient, IngredientCatalog, load_ingredient_catalog
from .nutrition import MacroValues, qualifies_high_protein
from .offer_matcher import match_offers
from .plan_data import cooking_days_for_frequency, days_covered_by_meal
from .quantity_math import PantryEntry, Quantity, parse_quantity
from .recipe_catalog import RecipeCatalog, load_recipe_catalog
from .recipe_matcher import RecipeCandidate, rank_candidates
from .recipe_renderer import RenderedMeal, build_shopping_list, render_meal


_MAX_CANDIDATES_PER_DAY = 12
_MINIMUM_HIGH_PROTEIN_G = Decimal("30")
_ERROR_SUGGESTIONS = {
    "insufficient_offers": ("add_store", "wait_for_complete_flyer_refresh"),
    "diet_too_strict": ("add_store", "use_standard_mode"),
    "unmeasurable_packages": ("wait_for_complete_flyer_refresh",),
}


class NoCompatiblePlan(RuntimeError):
    """A truthful, actionable failure to assemble the requested full week."""

    def __init__(self, code: str, suggestions: Sequence[str]):
        if code not in _ERROR_SUGGESTIONS:
            raise ValueError(f"unknown deterministic plan error: {code}")
        self.code = code
        self.suggestions = tuple(suggestions)
        super().__init__(code)


@dataclass(frozen=True)
class _SelectedMeal:
    day: str
    candidate: RecipeCandidate
    rendered: RenderedMeal


def _raise_no_plan(code: str) -> None:
    raise NoCompatiblePlan(code, _ERROR_SUGGESTIONS[code])


def _grams(quantity: Quantity, ingredient: Ingredient) -> Fraction | None:
    if quantity.unit == "g":
        return Fraction(quantity.amount)
    if quantity.unit == "ml" and ingredient.density_g_per_ml is not None:
        return Fraction(quantity.amount) * Fraction(ingredient.density_g_per_ml)
    if quantity.unit == "piece" and ingredient.grams_per_piece is not None:
        return Fraction(quantity.amount) * Fraction(ingredient.grams_per_piece)
    return None


def _fraction_to_decimal(value: Fraction) -> Decimal:
    denominator = value.denominator
    twos = 0
    fives = 0
    while denominator % 2 == 0:
        denominator //= 2
        twos += 1
    while denominator % 5 == 0:
        denominator //= 5
        fives += 1
    if denominator == 1:
        scale = max(twos, fives)
        coefficient = value.numerator * 2 ** (scale - twos) * 5 ** (scale - fives)
        digits = tuple(map(int, str(abs(coefficient))))
        return Decimal((coefficient < 0, digits, -scale))
    return Decimal(value.numerator) / Decimal(value.denominator)


def _pantry_balances(
    pantry: Sequence[PantryEntry], catalog: IngredientCatalog
) -> dict[str, Fraction]:
    balances: dict[str, Fraction] = {}
    for entry in pantry:
        if entry.quantity is None:
            continue
        try:
            ingredient = catalog.by_id(entry.ingredient_id)
        except KeyError:
            continue
        grams = _grams(entry.quantity, ingredient)
        if grams is None or grams <= 0:
            continue
        balances[entry.ingredient_id] = balances.get(
            entry.ingredient_id, Fraction(0)
        ) + grams
    return balances


def _ranking_pantry(
    balances: Mapping[str, Fraction],
    catalog: IngredientCatalog,
    household_servings: int,
) -> tuple[PantryEntry, ...]:
    if household_servings <= 0:
        return ()
    entries = []
    for ingredient_id in sorted(balances):
        amount = balances[ingredient_id]
        if amount <= 0:
            continue
        ingredient = catalog.by_id(ingredient_id)
        entries.append(
            PantryEntry(
                ingredient_id,
                ingredient.name,
                Quantity(
                    _fraction_to_decimal(amount / household_servings),
                    "g",
                ),
            )
        )
    return tuple(entries)


def _consume_pantry(
    balances: Mapping[str, Fraction], rendered: RenderedMeal
) -> dict[str, Fraction]:
    remaining = dict(balances)
    for item in rendered.ingredients:
        available = remaining.get(item.ingredient.id, Fraction(0))
        required = _grams(item.quantity, item.ingredient)
        if available <= 0 or required is None:
            continue
        remaining[item.ingredient.id] = max(Fraction(0), available - required)
    return remaining


def _has_unmeasurable_rows(rows: Sequence[Mapping[str, object]]) -> bool:
    for row in rows:
        try:
            parse_quantity(row["jednotka"])
        except (KeyError, TypeError, ValueError):
            return True
    return False


def _failure_code(
    *, mode: str, selected_rows: Sequence[Mapping[str, object]], matched_count: int
) -> str:
    if matched_count == 0 and _has_unmeasurable_rows(selected_rows):
        return "unmeasurable_packages"
    if mode != "standard":
        return "diet_too_strict"
    return "insufficient_offers"


def _select_week(
    *,
    days: Sequence[str],
    frequency: int,
    templates,
    offers,
    pantry: Sequence[PantryEntry],
    pantry_driven: bool,
    mode: str,
    seed: str,
    week: str,
    adults: int,
    children: int,
    ingredient_catalog: IngredientCatalog,
) -> tuple[_SelectedMeal, ...] | None:
    initial_balances = (
        _pantry_balances(pantry, ingredient_catalog) if pantry_driven else {}
    )

    available_methods = set()
    for day in days:
        coverage = days_covered_by_meal(frequency, day)
        ranking_pantry = (
            _ranking_pantry(
                initial_balances,
                ingredient_catalog,
                (adults + children) * coverage,
            )
            if pantry_driven
            else ()
        )
        available_methods.update(
            candidate.template.method
            for candidate in rank_candidates(
                templates,
                offers,
                ranking_pantry,
                mode,
                f"{seed}:{week}:{day}",
                ingredient_catalog=ingredient_catalog,
                recent_families=(),
                recent_methods=(),
            )
        )
    require_three_methods = len(available_methods) >= 3

    def search(
        index: int,
        selected: tuple[_SelectedMeal, ...],
        balances: Mapping[str, Fraction],
    ) -> tuple[_SelectedMeal, ...] | None:
        if index == len(days):
            if require_three_methods and len(
                {item.candidate.template.method for item in selected}
            ) < 3:
                return None
            return selected

        day = days[index]
        coverage = days_covered_by_meal(frequency, day)
        ranking_pantry = (
            _ranking_pantry(
                balances,
                ingredient_catalog,
                (adults + children) * coverage,
            )
            if pantry_driven
            else ()
        )
        candidates = rank_candidates(
            templates,
            offers,
            ranking_pantry,
            mode,
            f"{seed}:{week}:{day}",
            ingredient_catalog=ingredient_catalog,
            recent_families=(
                item.candidate.template.family for item in selected
            ),
            recent_methods=(
                item.candidate.template.method for item in selected
            ),
        )[:_MAX_CANDIDATES_PER_DAY]

        for candidate in candidates:
            if selected:
                previous = selected[-1].candidate.template
                current = candidate.template
                if (previous.family, previous.method) == (
                    current.family,
                    current.method,
                ):
                    continue
            try:
                rendered = render_meal(
                    candidate,
                    adults=adults,
                    children=children,
                    covered_days=coverage,
                )
            except (TypeError, ValueError):
                continue
            if (
                mode == "high_protein"
                and rendered.nutrition.serving.protein_g
                < _MINIMUM_HIGH_PROTEIN_G
            ):
                continue

            result = search(
                index + 1,
                (*selected, _SelectedMeal(day, candidate, rendered)),
                (
                    _consume_pantry(balances, rendered)
                    if pantry_driven
                    else balances
                ),
            )
            if result is not None:
                return result
        return None

    return search(0, (), initial_balances)


def _decimal_text(value: Decimal) -> str:
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _macro_payload(value: MacroValues) -> dict[str, str]:
    return {
        "kcal": _decimal_text(value.kcal),
        "protein_g": _decimal_text(value.protein_g),
        "fat_g": _decimal_text(value.fat_g),
        "carbs_g": _decimal_text(value.carbs_g),
    }


def _ingredient_payload(item) -> dict:
    if item.offer is None:
        return {"spajza": item.ingredient.name}

    required_grams = _grams(item.quantity, item.ingredient)
    package_grams = _grams(item.offer.package.content, item.ingredient)
    if required_grams is None or package_grams is None or package_grams <= 0:
        raise ValueError("Balenie ponuky nemá merateľnú veľkosť.")
    package_ratio = required_grams / package_grams
    packages = -(-package_ratio.numerator // package_ratio.denominator)
    original = item.offer.original_price
    package = item.offer.package.content
    package_unit = "ks" if package.unit == "piece" else package.unit
    return {
        "offer_key": item.offer.offer_key,
        "nazov": item.offer.product_name,
        "obchod": item.offer.store,
        "jednotka": f"{_decimal_text(package.amount)} {package_unit}",
        "mnozstvo": packages,
        "davka": item.display_amount,
        "cena": _money(item.offer.sale_price * packages),
        "povodna": (
            None if original is None else _money(original * packages)
        ),
        "source_url": item.offer.source_url,
        "valid_from": item.offer.valid_from.isoformat(),
        "valid_to": item.offer.valid_to.isoformat(),
    }


def _meal_payload(item: _SelectedMeal, mode: str) -> dict:
    template = item.candidate.template
    rendered = item.rendered
    recipe = {
        "template_id": rendered.template_id,
        "family": template.family,
        "method": template.method,
        "min": template.minutes,
        "porcie": rendered.portions,
        "dni": rendered.covered_days,
        "kroky": list(rendered.instructions),
        "nutrition": {
            "estimated": rendered.nutrition.estimated,
            "total": _macro_payload(rendered.nutrition.total),
            "serving": _macro_payload(rendered.nutrition.serving),
        },
    }
    if mode == "high_protein" and qualifies_high_protein(rendered.nutrition):
        recipe["high_protein_claim"] = True
    return {
        "den": item.day,
        "nazov": rendered.name,
        "pokryva_dni": rendered.covered_days,
        "recept": recipe,
        "suroviny": [_ingredient_payload(value) for value in rendered.ingredients],
    }


def _money(value: Decimal) -> str:
    return format(value, ".2f").replace(".", ",")


def _shopping_totals(shopping: Sequence[Mapping[str, object]]):
    sale_total = Decimal("0")
    regular_total = Decimal("0")
    for group in shopping:
        for row in group["polozky"]:
            sale = Decimal(row["cena"].replace(",", "."))
            original = row["povodna"]
            sale_total += sale
            regular_total += (
                sale
                if original is None
                else Decimal(original.replace(",", "."))
            )
    return sale_total, regular_total


def build_deterministic_plan(
    *,
    week: str,
    rows: Sequence[Mapping[str, object]],
    stores: Sequence[str],
    adults: int,
    children: int,
    frequency: Literal[1, 2, 3],
    pantry: Sequence[PantryEntry],
    pantry_driven: bool,
    mode: str,
    seed: str,
    ingredient_catalog: IngredientCatalog | None = None,
    recipe_catalog: RecipeCatalog | None = None,
) -> dict:
    """Build one complete deterministic week without network or model calls."""
    if frequency not in (1, 2, 3) or isinstance(frequency, bool):
        raise ValueError("frequency must be 1, 2 or 3")
    if (
        isinstance(adults, bool)
        or isinstance(children, bool)
        or not isinstance(adults, int)
        or not isinstance(children, int)
        or adults < 0
        or children < 0
        or adults + children < 1
    ):
        raise ValueError("adults and children must describe at least one person")

    ingredients = ingredient_catalog or load_ingredient_catalog()
    recipes = recipe_catalog or load_recipe_catalog(ingredients)
    selected_stores = frozenset(stores)
    selected_rows = tuple(
        row for row in rows if row["obchod"] in selected_stores
    )
    offers = tuple(match_offers(selected_rows, ingredients))

    if not offers and not pantry_driven:
        _raise_no_plan(
            _failure_code(
                mode=mode,
                selected_rows=selected_rows,
                matched_count=0,
            )
        )

    days = cooking_days_for_frequency(frequency)
    selected = _select_week(
        days=days,
        frequency=frequency,
        templates=recipes.all(),
        offers=offers,
        pantry=pantry,
        pantry_driven=pantry_driven,
        mode=mode,
        seed=seed,
        week=week,
        adults=adults,
        children=children,
        ingredient_catalog=ingredients,
    )
    if selected is None:
        _raise_no_plan(
            _failure_code(
                mode=mode,
                selected_rows=selected_rows,
                matched_count=len(offers),
            )
        )

    if sum(item.rendered.covered_days for item in selected) != 7:
        _raise_no_plan("insufficient_offers")
    try:
        shopping = build_shopping_list(
            [item.rendered for item in selected], pantry
        )
    except ValueError as exc:
        raise NoCompatiblePlan(
            "unmeasurable_packages",
            _ERROR_SUGGESTIONS["unmeasurable_packages"],
        ) from exc
    sale_total, regular_total = _shopping_totals(shopping)
    return {
        "tyzden": week,
        "jedla": [_meal_payload(item, mode) for item in selected],
        "nakupny_zoznam": shopping,
        "nakup_spolu": _money(sale_total),
        "bezna_cena": _money(regular_total),
        "usetrene": _money(max(Decimal("0"), regular_total - sale_total)),
        "meta": {
            "engine": "deterministic",
            "library_version": recipes.version,
            "mode": mode,
        },
    }
