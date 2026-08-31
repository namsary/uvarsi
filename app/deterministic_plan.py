"""Deterministic orchestration of one complete seven-day meal plan."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from fractions import Fraction
from typing import Literal, Mapping, Sequence

from .ingredient_catalog import Ingredient, IngredientCatalog, load_ingredient_catalog
from .nutrition import (
    MacroValues,
    NutritionEstimate,
    estimate_recipe_nutrition,
    qualifies_high_protein,
)
from .offer_matcher import match_offers
from .plan_data import (
    cooking_days_for_frequency,
    days_covered_by_meal,
    home_ingredients_in,
    leftover_storage_note,
)
from .quantity_math import PantryEntry, Quantity, parse_quantity
from .recipe_catalog import RecipeCatalog, RecipeTemplate, load_recipe_catalog
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
    adult_nutrition: NutritionEstimate


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
    template: RecipeTemplate,
    adults: int,
    children: int,
    covered_days: int,
    required_reserve: Mapping[str, Fraction],
) -> tuple[PantryEntry, ...]:
    required: dict[str, tuple[Fraction, Fraction]] = {}
    optional: dict[str, tuple[Fraction, Fraction]] = {}
    for slot in template.slots:
        current_equivalents = (
            Fraction(adults)
            + Fraction(children) * Fraction(slot.child_factor)
        ) * covered_days
        for ingredient_id in slot.candidates:
            ingredient = catalog.by_id(ingredient_id)
            per_adult = _grams(
                Quantity(slot.amount_per_adult, slot.unit), ingredient
            )
            if per_adult is None:
                continue
            if slot.required:
                normalized, current = required.get(
                    ingredient_id, (Fraction(0), Fraction(0))
                )
                required[ingredient_id] = (
                    normalized + per_adult,
                    current + per_adult * current_equivalents,
                )
            else:
                normalized, current = optional.get(
                    ingredient_id, (Fraction(0), Fraction(0))
                )
                optional[ingredient_id] = (
                    normalized + per_adult,
                    current + per_adult * current_equivalents,
                )

    entries = []
    for ingredient_id in sorted(required.keys() | optional.keys()):
        available = balances.get(ingredient_id, Fraction(0))
        if available <= 0:
            continue
        normalized_required, current_required = required.get(
            ingredient_id, (Fraction(0), Fraction(0))
        )
        normalized_optional, current_optional = optional.get(
            ingredient_id, (Fraction(0), Fraction(0))
        )
        required_available = (
            min(
                normalized_required,
                available * normalized_required / current_required,
            )
            if current_required > 0
            else Fraction(0)
        )
        optional_surplus = max(
            Fraction(0),
            available - required_reserve.get(ingredient_id, Fraction(0)),
        )
        optional_available = (
            min(
                normalized_optional,
                optional_surplus * normalized_optional / current_optional,
            )
            if current_optional > 0
            else Fraction(0)
        )
        normalized_available = required_available + optional_available
        if normalized_available <= 0:
            continue
        ingredient = catalog.by_id(ingredient_id)
        entries.append(
            PantryEntry(
                ingredient_id,
                ingredient.name,
                Quantity(
                    _fraction_to_decimal(normalized_available),
                    "g",
                ),
            )
        )
    return tuple(entries)


def _rank_for_day(
    *,
    templates: Sequence[RecipeTemplate],
    offers,
    balances: Mapping[str, Fraction],
    pantry_driven: bool,
    mode: str,
    seed: str,
    ingredient_catalog: IngredientCatalog,
    adults: int,
    children: int,
    covered_days: int,
    required_reserve: Mapping[str, Fraction],
    recent_families,
    recent_methods,
) -> tuple[RecipeCandidate, ...]:
    if not pantry_driven:
        return tuple(
            rank_candidates(
                templates,
                offers,
                (),
                mode,
                seed,
                ingredient_catalog=ingredient_catalog,
                recent_families=recent_families,
                recent_methods=recent_methods,
            )
        )

    candidates = []
    for template in templates:
        candidates.extend(
            rank_candidates(
                (template,),
                offers,
                _ranking_pantry(
                    balances,
                    ingredient_catalog,
                    template,
                    adults,
                    children,
                    covered_days,
                    required_reserve,
                ),
                mode,
                seed,
                ingredient_catalog=ingredient_catalog,
                recent_families=recent_families,
                recent_methods=recent_methods,
            )
        )
    return tuple(sorted(candidates, key=lambda item: (-item.score, item.key)))


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


def _adult_serving_nutrition(
    rendered: RenderedMeal,
    *,
    adults: int,
    children: int,
) -> NutritionEstimate:
    lines = []
    for item in rendered.ingredients:
        batch_equivalents = (
            Fraction(adults)
            + Fraction(children) * Fraction(item.slot.child_factor)
        ) * rendered.covered_days
        grams = _grams(item.quantity, item.ingredient)
        if grams is None or batch_equivalents <= 0:
            raise ValueError("Surovina nemá merateľnú dospelú porciu.")
        edible_grams = (
            grams * Fraction(item.ingredient.edible_ratio) / batch_equivalents
        )
        lines.append((item.ingredient, _fraction_to_decimal(edible_grams)))
    return estimate_recipe_nutrition(lines, adult_servings=Decimal("1"))


def _future_required_reserve(
    *,
    days: Sequence[str],
    frequency: int,
    templates: Sequence[RecipeTemplate],
    offers,
    balances: Mapping[str, Fraction],
    mode: str,
    seed: str,
    week: str,
    adults: int,
    children: int,
    ingredient_catalog: IngredientCatalog,
    recent_families: Sequence[str],
    recent_methods: Sequence[str],
) -> dict[str, Fraction]:
    reserve: dict[str, Fraction] = {}
    for day in days:
        coverage = days_covered_by_meal(frequency, day)
        candidates = _rank_for_day(
            templates=templates,
            offers=offers,
            balances=balances,
            pantry_driven=True,
            mode=mode,
            seed=f"{seed}:{week}:{day}",
            ingredient_catalog=ingredient_catalog,
            adults=adults,
            children=children,
            covered_days=coverage,
            required_reserve=balances,
            recent_families=recent_families,
            recent_methods=recent_methods,
        )[:_MAX_CANDIDATES_PER_DAY]
        daily_maximum: dict[str, Fraction] = {}
        for candidate in candidates:
            try:
                rendered = render_meal(
                    candidate,
                    adults=adults,
                    children=children,
                    covered_days=coverage,
                )
                if (
                    mode == "high_protein"
                    and _adult_serving_nutrition(
                        rendered,
                        adults=adults,
                        children=children,
                    ).serving.protein_g
                    < _MINIMUM_HIGH_PROTEIN_G
                ):
                    continue
            except (TypeError, ValueError):
                continue

            candidate_required: dict[str, Fraction] = {}
            measurable = True
            for item in rendered.ingredients:
                if not item.slot.required:
                    continue
                grams = _grams(item.quantity, item.ingredient)
                if grams is None:
                    measurable = False
                    break
                candidate_required[item.ingredient.id] = (
                    candidate_required.get(item.ingredient.id, Fraction(0))
                    + grams
                )
            if not measurable:
                continue
            for ingredient_id in sorted(candidate_required):
                daily_maximum[ingredient_id] = max(
                    daily_maximum.get(ingredient_id, Fraction(0)),
                    candidate_required[ingredient_id],
                )

        for ingredient_id in sorted(daily_maximum):
            reserve[ingredient_id] = (
                reserve.get(ingredient_id, Fraction(0))
                + daily_maximum[ingredient_id]
            )
    return reserve


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
    for index, day in enumerate(days):
        coverage = days_covered_by_meal(frequency, day)
        required_reserve = (
            _future_required_reserve(
                days=days[index:],
                frequency=frequency,
                templates=templates,
                offers=offers,
                balances=initial_balances,
                mode=mode,
                seed=seed,
                week=week,
                adults=adults,
                children=children,
                ingredient_catalog=ingredient_catalog,
                recent_families=(),
                recent_methods=(),
            )
            if pantry_driven
            else {}
        )
        available_methods.update(
            candidate.template.method
            for candidate in _rank_for_day(
                templates=templates,
                offers=offers,
                balances=initial_balances,
                pantry_driven=pantry_driven,
                mode=mode,
                seed=f"{seed}:{week}:{day}",
                ingredient_catalog=ingredient_catalog,
                adults=adults,
                children=children,
                covered_days=coverage,
                required_reserve=required_reserve,
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
        family_history = tuple(
            item.candidate.template.family for item in selected
        )
        method_history = tuple(
            item.candidate.template.method for item in selected
        )
        required_reserve = (
            _future_required_reserve(
                days=days[index:],
                frequency=frequency,
                templates=templates,
                offers=offers,
                balances=balances,
                mode=mode,
                seed=seed,
                week=week,
                adults=adults,
                children=children,
                ingredient_catalog=ingredient_catalog,
                recent_families=family_history,
                recent_methods=method_history,
            )
            if pantry_driven
            else {}
        )
        candidates = _rank_for_day(
            templates=templates,
            offers=offers,
            balances=balances,
            pantry_driven=pantry_driven,
            mode=mode,
            seed=f"{seed}:{week}:{day}",
            ingredient_catalog=ingredient_catalog,
            adults=adults,
            children=children,
            covered_days=coverage,
            required_reserve=required_reserve,
            recent_families=family_history,
            recent_methods=method_history,
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
                adult_nutrition = _adult_serving_nutrition(
                    rendered,
                    adults=adults,
                    children=children,
                )
            except (TypeError, ValueError):
                continue
            if (
                mode == "high_protein"
                and adult_nutrition.serving.protein_g < _MINIMUM_HIGH_PROTEIN_G
            ):
                continue

            result = search(
                index + 1,
                (
                    *selected,
                    _SelectedMeal(day, candidate, rendered, adult_nutrition),
                ),
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


def _ingredient_payload(
    item, offer_rows: Mapping[str, Mapping[str, object]]
) -> dict:
    if item.offer is None:
        return {"spajza": item.ingredient.name}

    try:
        source = offer_rows[item.offer.offer_key]
    except KeyError as exc:
        raise ValueError("Ponuke chýbajú pôvodné verejné údaje.") from exc

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
        "zlava": source.get("zlava") or "",
        "source_url": item.offer.source_url,
        "source_page": source.get("source_page"),
        "valid_from": item.offer.valid_from.isoformat(),
        "valid_to": item.offer.valid_to.isoformat(),
    }


def _household_text(adults: int, children: int) -> str:
    parts = []
    if adults:
        word = "dospelý" if adults == 1 else "dospelí" if adults < 5 else "dospelých"
        parts.append(f"{adults} {word}")
    if children:
        word = "dieťa" if children == 1 else "deti" if children < 5 else "detí"
        parts.append(f"{children} {word}")
    return " + ".join(parts)


def _days_word(count: int) -> str:
    return "deň" if count == 1 else "dni" if count < 5 else "dní"


def _slovak_decimal_text(value: Fraction) -> str:
    return _decimal_text(_fraction_to_decimal(value)).replace(".", ",")


def _primary_adult_equivalents(
    item: _SelectedMeal, adults: int, children: int
) -> Fraction:
    slots = item.candidate.template.slots
    primary = next(
        (slot for slot in slots if slot.required and slot.use == "main"),
        next(slot for slot in slots if slot.required),
    )
    return (
        Fraction(adults) + Fraction(children) * Fraction(primary.child_factor)
    ) * item.rendered.covered_days


def _meal_payload(
    item: _SelectedMeal,
    mode: str,
    *,
    adults: int,
    children: int,
    offer_rows: Mapping[str, Mapping[str, object]],
) -> dict:
    template = item.candidate.template
    rendered = item.rendered
    for_whom = _household_text(adults, children)
    if rendered.covered_days > 1:
        for_whom += f" × {rendered.covered_days} {_days_word(rendered.covered_days)}"
    doses = [
        (
            f"{value.offer.product_name} – {value.display_amount}"
            if value.offer is not None
            else f"{value.ingredient.name} – {value.display_amount} zo špajze"
        )
        for value in rendered.ingredients
    ]
    doses.extend(f"{name} zo špajze" for name in rendered.pantry_basics)
    recipe = {
        "template_id": rendered.template_id,
        "family": template.family,
        "method": template.method,
        "min": template.minutes,
        "porcie": rendered.portions,
        "pre": for_whom,
        "davky": doses,
        "skontroluj_doma": home_ingredients_in(rendered.instructions),
        "dni": rendered.covered_days,
        "domacnost": {"dospeli": adults, "deti": children},
        "dospely_ekvivalent": _slovak_decimal_text(
            _primary_adult_equivalents(item, adults, children)
        ),
        "poznamka": "Kuchársky odhad na plánovanie nákupu.",
        "kroky": list(rendered.instructions),
        "nutrition": {
            "estimated": item.adult_nutrition.estimated,
            "total": _macro_payload(rendered.nutrition.total),
            "serving": _macro_payload(item.adult_nutrition.serving),
        },
    }
    storage = leftover_storage_note(rendered.instructions, rendered.covered_days)
    if storage:
        recipe["uchovanie"] = storage
    if mode == "high_protein" and qualifies_high_protein(item.adult_nutrition):
        recipe["high_protein_claim"] = True
    ingredients = [
        _ingredient_payload(value, offer_rows) for value in rendered.ingredients
    ]
    ingredients.extend({"spajza": name} for name in rendered.pantry_basics)
    return {
        "den": item.day,
        "nazov": rendered.name,
        "pokryva_dni": rendered.covered_days,
        "recept": recipe,
        "suroviny": ingredients,
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
    offer_rows = {str(row["offer_key"]): row for row in selected_rows}
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
        "jedla": [
            _meal_payload(
                item,
                mode,
                adults=adults,
                children=children,
                offer_rows=offer_rows,
            )
            for item in selected
        ],
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
