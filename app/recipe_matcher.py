"""Fail-closed recipe compatibility and deterministic candidate ranking."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from decimal import Context, Decimal, ROUND_HALF_EVEN
from fractions import Fraction
from hashlib import sha256
from typing import Iterable, Sequence

from .ingredient_catalog import DietTag, Ingredient, load_ingredient_catalog
from .nutrition import estimate_recipe_nutrition
from .offer_matcher import MatchedOffer
from .quantity_math import PackageSize, PantryEntry, Quantity, purchase_requirement
from .recipe_catalog import IngredientSlot, RecipeTemplate


SCORE_SAVING = 30
SCORE_OFFER_COVERAGE = 20
SCORE_PANTRY_USE = 14
SCORE_STORE_PREFERENCE = 8
PENALTY_PACKAGE_LEFTOVER = 6
PENALTY_RECENT_FAMILY = 18
PENALTY_RECENT_METHOD = 12

_ZERO = Decimal("0")
_ONE = Decimal("1")
_MINIMUM_HIGH_PROTEIN_G = Decimal("30")


@dataclass(frozen=True)
class SlotSelection:
    slot: IngredientSlot
    ingredient: Ingredient
    offer: MatchedOffer | None
    pantry: Quantity | None


@dataclass(frozen=True)
class RecipeCandidate:
    template: RecipeTemplate
    selections: Sequence[SlotSelection]
    score: Decimal
    key: str


def _decimal_from_fraction(value: Fraction) -> Decimal:
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
        coefficient = value.numerator
        coefficient *= 2 ** (scale - twos)
        coefficient *= 5 ** (scale - fives)
        digits = tuple(map(int, str(abs(coefficient))))
        return Decimal((coefficient < 0, digits, -scale))

    integer_digits = max(
        1,
        len(str(abs(value.numerator))) - len(str(value.denominator)) + 1,
    )
    context = Context(prec=integer_digits + 64, rounding=ROUND_HALF_EVEN)
    return context.divide(Decimal(value.numerator), Decimal(value.denominator))


def _ratio(numerator: Decimal, denominator: Decimal) -> Decimal:
    if denominator <= _ZERO:
        return _ZERO
    value = _decimal_from_fraction(Fraction(numerator) / Fraction(denominator))
    return min(_ONE, max(_ZERO, value))


def _multiply(left: Decimal, right: Decimal) -> Decimal:
    return _decimal_from_fraction(Fraction(left) * Fraction(right))


def _quantity_in_unit(
    quantity: Quantity, target_unit: str, ingredient: Ingredient
) -> Quantity | None:
    if quantity.unit == target_unit:
        return quantity

    if quantity.unit == "g":
        grams = quantity.amount
    elif quantity.unit == "ml" and ingredient.density_g_per_ml is not None:
        grams = _multiply(quantity.amount, ingredient.density_g_per_ml)
    elif quantity.unit == "piece" and ingredient.grams_per_piece is not None:
        grams = _multiply(quantity.amount, ingredient.grams_per_piece)
    else:
        return None

    if target_unit == "g":
        amount = grams
    elif target_unit == "ml" and ingredient.density_g_per_ml is not None:
        amount = _decimal_from_fraction(
            Fraction(grams) / Fraction(ingredient.density_g_per_ml)
        )
    elif target_unit == "piece" and ingredient.grams_per_piece is not None:
        amount = _decimal_from_fraction(
            Fraction(grams) / Fraction(ingredient.grams_per_piece)
        )
    else:
        return None
    return Quantity(amount, target_unit)


def _diet_compatible(ingredient: Ingredient, mode: str) -> bool:
    if mode == "vegan":
        return DietTag.VEGAN in ingredient.diet_tags
    if mode == "vegetarian":
        return DietTag.VEGETARIAN in ingredient.diet_tags
    return True


def _ingredient_index(offers: Sequence[MatchedOffer]) -> dict[str, Ingredient]:
    result = {item.ingredient.id: item.ingredient for item in offers}
    for ingredient in load_ingredient_catalog().all():
        result.setdefault(ingredient.id, ingredient)
    return result


def _pantry_quantity(
    entries: Sequence[PantryEntry], slot: IngredientSlot, ingredient: Ingredient
) -> Quantity | None:
    amounts = []
    for entry in entries:
        if entry.ingredient_id != ingredient.id or entry.quantity is None:
            continue
        converted = _quantity_in_unit(entry.quantity, slot.unit, ingredient)
        if converted is not None and converted.amount > _ZERO:
            amounts.append(converted.amount)
    if not amounts:
        return None
    total = sum((Fraction(amount) for amount in amounts), Fraction(0))
    return Quantity(_decimal_from_fraction(total), slot.unit)


def _offer_leftover_ratio(
    slot: IngredientSlot,
    ingredient: Ingredient,
    selected_offer: MatchedOffer,
    pantry: Quantity | None,
) -> tuple[Decimal, int] | None:
    package = _quantity_in_unit(
        selected_offer.package.content, slot.unit, ingredient
    )
    if package is None:
        return None
    available = pantry or Quantity(_ZERO, slot.unit)
    requirement = purchase_requirement(
        Quantity(slot.amount_per_adult, slot.unit),
        available,
        PackageSize(package),
    )
    if requirement.packages == 0:
        return _ZERO, 0
    return (
        _ratio(requirement.leftover.amount, requirement.to_buy.amount),
        requirement.packages,
    )


def _saving_ratio(offer: MatchedOffer) -> Decimal:
    original = offer.original_price
    if (
        not offer.sale_price.is_finite()
        or offer.sale_price < _ZERO
        or original is None
        or not original.is_finite()
        or original <= _ZERO
        or offer.sale_price >= original
    ):
        return _ZERO
    return _ratio(original - offer.sale_price, original)


def _selection_value(
    slot: IngredientSlot,
    ingredient: Ingredient,
    selected_offer: MatchedOffer | None,
    pantry: Quantity | None,
) -> Decimal:
    required = slot.amount_per_adult
    pantry_ratio = _ZERO if pantry is None else _ratio(pantry.amount, required)
    value = pantry_ratio * SCORE_PANTRY_USE
    if selected_offer is None:
        return value
    leftover = _offer_leftover_ratio(
        slot, ingredient, selected_offer, pantry
    )
    if leftover is None:
        return value
    leftover_ratio, packages = leftover
    if packages == 0:
        return value
    return (
        value
        + _saving_ratio(selected_offer) * SCORE_SAVING
        + SCORE_OFFER_COVERAGE
        + SCORE_STORE_PREFERENCE
        - leftover_ratio * PENALTY_PACKAGE_LEFTOVER
    )


def _best_offer(
    slot: IngredientSlot,
    ingredient: Ingredient,
    offers: Sequence[MatchedOffer],
    pantry: Quantity | None,
) -> MatchedOffer | None:
    if pantry is not None and pantry.amount >= slot.amount_per_adult:
        return None
    compatible = []
    for candidate in offers:
        if candidate.ingredient.id != ingredient.id:
            continue
        leftover = _offer_leftover_ratio(slot, ingredient, candidate, pantry)
        if leftover is None or leftover[1] == 0:
            continue
        compatible.append(candidate)
    if not compatible:
        return None
    return min(
        compatible,
        key=lambda item: (
            -_selection_value(slot, ingredient, item, pantry),
            item.sale_price,
            item.store,
            item.offer_key,
        ),
    )


def _select_slot(
    slot: IngredientSlot,
    offers: Sequence[MatchedOffer],
    pantry_entries: Sequence[PantryEntry],
    ingredients: dict[str, Ingredient],
    mode: str,
) -> SlotSelection | None:
    options = []
    for ingredient_id in sorted(slot.candidates):
        ingredient = ingredients.get(ingredient_id)
        if ingredient is None or not _diet_compatible(ingredient, mode):
            continue
        pantry = _pantry_quantity(pantry_entries, slot, ingredient)
        selected_offer = _best_offer(slot, ingredient, offers, pantry)
        if pantry is None and selected_offer is None:
            continue
        options.append(
            SlotSelection(
                slot=slot,
                ingredient=ingredient,
                offer=selected_offer,
                pantry=pantry,
            )
        )
    if not options:
        return None
    return min(
        options,
        key=lambda item: (
            -_selection_value(
                item.slot, item.ingredient, item.offer, item.pantry
            ),
            item.ingredient.id,
            "" if item.offer is None else item.offer.offer_key,
        ),
    )


def _candidate_score(selections: Sequence[SlotSelection], slot_count: int) -> Decimal:
    offer_rows = []
    pantry_total = _ZERO
    leftover_total = _ZERO
    for selection in selections:
        required = selection.slot.amount_per_adult
        if selection.pantry is not None:
            pantry_total += _ratio(selection.pantry.amount, required)
        if selection.offer is None:
            continue
        leftover = _offer_leftover_ratio(
            selection.slot,
            selection.ingredient,
            selection.offer,
            selection.pantry,
        )
        if leftover is None or leftover[1] == 0:
            continue
        leftover_ratio, packages = leftover
        offer_rows.append((selection.offer, packages))
        leftover_total += leftover_ratio

    denominator = Decimal(slot_count)
    offer_coverage = _ratio(Decimal(len(offer_rows)), denominator)
    pantry_use = _ratio(pantry_total, denominator)

    if offer_rows:
        stores = Counter(item.store for item, _ in offer_rows)
        store_preference = _ratio(
            Decimal(max(stores.values())), Decimal(len(offer_rows))
        )
        leftover_ratio = _ratio(leftover_total, Decimal(len(offer_rows)))
    else:
        store_preference = _ZERO
        leftover_ratio = _ZERO

    original_total = Fraction(0)
    saving_total = Fraction(0)
    for selected_offer, packages in offer_rows:
        original = selected_offer.original_price
        if (
            original is None
            or not original.is_finite()
            or original <= _ZERO
            or not selected_offer.sale_price.is_finite()
            or selected_offer.sale_price < _ZERO
            or selected_offer.sale_price >= original
        ):
            continue
        original_total += Fraction(original) * packages
        saving_total += Fraction(original - selected_offer.sale_price) * packages
    saving = (
        _ZERO
        if original_total == 0
        else _ratio(
            _decimal_from_fraction(saving_total),
            _decimal_from_fraction(original_total),
        )
    )

    return (
        saving * SCORE_SAVING
        + offer_coverage * SCORE_OFFER_COVERAGE
        + pantry_use * SCORE_PANTRY_USE
        + store_preference * SCORE_STORE_PREFERENCE
        - leftover_ratio * PENALTY_PACKAGE_LEFTOVER
    )


def _protein_per_adult(selections: Sequence[SlotSelection]) -> Decimal | None:
    lines = []
    for selection in selections:
        quantity = Quantity(
            selection.slot.amount_per_adult, selection.slot.unit
        )
        grams = _quantity_in_unit(quantity, "g", selection.ingredient)
        if grams is None:
            return None
        edible_grams = _multiply(grams.amount, selection.ingredient.edible_ratio)
        lines.append((selection.ingredient, edible_grams))
    return estimate_recipe_nutrition(
        lines, adult_servings=_ONE
    ).serving.protein_g


def _candidate_key(
    seed: str, template: RecipeTemplate, selections: Sequence[SlotSelection]
) -> str:
    offer_keys = ",".join(
        sorted(
            selection.offer.offer_key
            for selection in selections
            if selection.offer is not None
        )
    )
    return sha256(f"{seed}:{template.id}:{offer_keys}".encode()).hexdigest()


def rank_candidates(
    templates: Iterable[RecipeTemplate],
    offers: Iterable[MatchedOffer],
    pantry: Iterable[PantryEntry],
    mode: str,
    seed: str,
) -> Sequence[RecipeCandidate]:
    """Return compatible candidates ordered by score and stable SHA-256 key."""
    offer_rows = tuple(offers)
    pantry_entries = tuple(pantry)
    ingredients = _ingredient_index(offer_rows)
    candidates = []

    for recipe in templates:
        if not recipe.active or mode not in recipe.modes:
            continue
        selections = []
        compatible = True
        for recipe_slot in recipe.slots:
            selection = _select_slot(
                recipe_slot,
                offer_rows,
                pantry_entries,
                ingredients,
                mode,
            )
            if selection is None:
                if recipe_slot.required:
                    compatible = False
                    break
                continue
            selections.append(selection)
        if not compatible:
            continue

        selection_rows = tuple(selections)
        if mode == "high_protein":
            protein_g = _protein_per_adult(selection_rows)
            if protein_g is None or protein_g < _MINIMUM_HIGH_PROTEIN_G:
                continue
        candidates.append(
            RecipeCandidate(
                template=recipe,
                selections=selection_rows,
                score=_candidate_score(selection_rows, len(recipe.slots)),
                key=_candidate_key(seed, recipe, selection_rows),
            )
        )

    return tuple(sorted(candidates, key=lambda item: (-item.score, item.key)))
