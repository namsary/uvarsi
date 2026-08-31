"""Render ranked recipe candidates into exact, cookable Slovak meals."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING, ROUND_HALF_UP
from functools import lru_cache
from string import Formatter
import re
import unicodedata
from typing import Mapping, Sequence

from .ingredient_catalog import Ingredient, load_ingredient_catalog
from .nutrition import NutritionEstimate, estimate_recipe_nutrition
from .plan_data import validate_recipe_language
from .quantity_math import Quantity
from .recipe_catalog import IngredientSlot
from .recipe_matcher import RecipeCandidate, SlotSelection


_ONE = Decimal("1")
_THOUSAND = Decimal("1000")


@dataclass(frozen=True)
class RenderedIngredient:
    """One selected ingredient with exact and display-only quantities."""

    selection: SlotSelection
    quantity: Quantity
    display_amount: str
    label: str

    @property
    def slot(self) -> IngredientSlot:
        return self.selection.slot

    @property
    def ingredient(self) -> Ingredient:
        return self.selection.ingredient

    @property
    def offer(self):
        return self.selection.offer

    @property
    def pantry(self):
        return self.selection.pantry


@dataclass(frozen=True)
class RenderedMeal:
    """A deterministic meal ready for planning, nutrition, and display."""

    template_id: str
    candidate_key: str
    name: str
    portions: int
    covered_days: int
    ingredients: Sequence[RenderedIngredient]
    pantry_basics: Sequence[str]
    instructions: Sequence[str]
    nutrition: NutritionEstimate


@dataclass(frozen=True)
class _SlotWording:
    name: str
    amount: str
    cut: str


# Verified forms used after a displayed quantity. Titles and ingredient labels
# continue to use Ingredient.name unchanged.
_QUANTITY_NAMES: Mapping[str, str] = {
    "chicken_breast": "kuracích pŕs",
    "chicken_thigh": "kuracích stehien",
    "pork_shoulder": "bravčového pliecka",
    "beef_mince": "mletého hovädzieho mäsa",
    "salmon": "lososa",
    "tofu": "tofu",
    "red_lentils": "červenej šošovice",
    "chickpeas": "cíceru",
    "egg": "vajec",
    "cottage_cheese": "cottage cheesu",
    "rice": "ryže",
    "pasta": "cestovín",
    "potato": "zemiakov",
    "bread": "bieleho chleba",
    "zucchini": "cukety",
    "tomato": "paradajok",
    "onion": "cibule",
    "garlic": "cesnaku",
    "carrot": "mrkvy",
    "broccoli": "brokolice",
    "milk": "plnotučného mlieka",
    "cream": "smotany na šľahanie",
    "hard_cheese": "tvrdého syra",
    "oil": "oleja",
    "salt": "soli",
    "black_pepper": "čierneho korenia",
}

_EXTRA_INGREDIENT_FORMS: Mapping[str, tuple[str, ...]] = {
    "chicken_breast": ("kuracie prsia", "kuracích pŕs"),
    "chicken_thigh": ("kuracie stehná", "kuracích stehien"),
    "rice": ("ryža", "ryže", "ryžu"),
    "pasta": ("cestoviny", "cestovín", "cestoviny"),
    "zucchini": ("cuketa", "cukety", "cuketu"),
    "tofu": ("tofu",),
    "carrot": ("mrkva", "mrkvy", "mrkvu"),
    "egg": ("vajce", "vajcia", "vajec", "vajca"),
    "milk": ("plnotučné mlieko", "plnotučného mlieka", "mlieko", "mlieka"),
}

_ALLOWED_IMPERATIVES = frozenset(
    {
        "dochuť",
        "dus",
        "nechaj",
        "nalej",
        "nakrájaj",
        "odober",
        "odokry",
        "odstav",
        "opeč",
        "opekaj",
        "opláchni",
        "osuš",
        "ošúp",
        "peč",
        "podávaj",
        "posyp",
        "potri",
        "predhrej",
        "prehrievaj",
        "premiešaj",
        "prepláchni",
        "pridaj",
        "prikry",
        "prilej",
        "priprav",
        "prisyp",
        "priveď",
        "rozdeľ",
        "rozdrv",
        "rozlož",
        "rozmixuj",
        "rozohrej",
        "rozšľahaj",
        "sceď",
        "stíš",
        "upeč",
        "uvar",
        "var",
        "vlej",
        "vlož",
        "vmiešaj",
        "vráť",
        "vsyp",
        "vyber",
        "zalej",
        "zohrej",
        "zohrievaj",
    }
)

_GENERIC_STEPS = frozenset(
    {
        "dochuť podľa chuti",
        "podávaj",
        "priprav podľa potreby",
        "priprav podľa chuti",
        "priprav suroviny",
        "spracuj",
        "uprav",
        "uvar jedlo",
        "var do hotova",
    }
)

_COOKING_ACTION = re.compile(
    r"\b(?:dus|opec|opekaj|pec|predhrej|prehrievaj|rozohrej|smaz|var|zohrej|zohrievaj)\b"
)
_VESSEL = re.compile(
    r"\b(?:hrnc\w*|panvic\w*|pekac\w*|plech\w*|rur\w*|wok\w*|rajnic\w*)\b"
)
_HEAT = re.compile(
    r"(?:\b(?:miernom|strednom|silnom|nizkom|vysokom)\s+ohni\b|"
    r"\b(?:prudkeho|mierneho)\s+varu\b|\bdo\s+varu\b|\d+\s*°\s*c\b)"
)
_TIME = re.compile(
    r"\b\d+(?:[,.]\d+)?\s*(?:sekund|sekundy|minut|minuty|hodin|hodiny)\b"
)
_DONENESS = re.compile(
    r"(?:\bkym\b|\bdozlatista\b|\bdosklovita\b|\bzlatist\w*\b|"
    r"\bchrumkav\w*\b|\bcira\b|\bciru\b|\bvsiakn\w*\b|"
    r"\bzmakn\w*\b|\bstuh\w*\b|\b74\s*°\s*c\b)"
)
_AMOUNT = re.compile(
    r"\b\d+(?:[,.]\d+)?\s*(?:g|kg|ml|l|ks|kus|kusy|kusov|"
    r"polievkov\w*\s+lyzic\w*|cajov\w*\s+lyzic\w*)\b"
)
_PREHEAT_READY = re.compile(r"\b(?:kontrolk\w*|dosiahn\w*|nahriat\w*|signal\w*)\b")


def _coefficient_and_exponent(value: Decimal) -> tuple[int, int]:
    parts = value.as_tuple()
    coefficient = 0
    for digit in parts.digits:
        coefficient = coefficient * 10 + digit
    if parts.sign:
        coefficient = -coefficient
    return coefficient, parts.exponent


def _from_coefficient(coefficient: int, exponent: int) -> Decimal:
    parts = Decimal(coefficient).as_tuple()
    return Decimal((parts.sign, parts.digits, exponent))


def _multiply_exact(left: Decimal, right: Decimal) -> Decimal:
    left_coefficient, left_exponent = _coefficient_and_exponent(left)
    right_coefficient, right_exponent = _coefficient_and_exponent(right)
    return _from_coefficient(
        left_coefficient * right_coefficient,
        left_exponent + right_exponent,
    )


def _add_exact(left: Decimal, right: Decimal) -> Decimal:
    left_coefficient, left_exponent = _coefficient_and_exponent(left)
    right_coefficient, right_exponent = _coefficient_and_exponent(right)
    exponent = min(left_exponent, right_exponent)
    left_coefficient *= 10 ** (left_exponent - exponent)
    right_coefficient *= 10 ** (right_exponent - exponent)
    return _from_coefficient(left_coefficient + right_coefficient, exponent)


def _shift_exponent(value: Decimal, places: int) -> Decimal:
    coefficient, exponent = _coefficient_and_exponent(value)
    return _from_coefficient(coefficient, exponent + places)


def _round_to_step(value: Decimal, step: Decimal) -> Decimal:
    return (value / step).quantize(_ONE, rounding=ROUND_HALF_UP) * step


def _decimal_text(value: Decimal) -> str:
    integral = value.to_integral_value()
    if value == integral:
        return f"{int(integral):,}".replace(",", " ")
    return format(value.normalize(), "f").replace(".", ",")


def _display_amount(quantity: Quantity) -> str:
    amount = quantity.amount
    if quantity.unit == "piece":
        rounded = amount.to_integral_value(rounding=ROUND_CEILING)
        return f"{_decimal_text(rounded)} ks"

    if amount < Decimal("10"):
        step = Decimal("1")
    elif amount < Decimal("100"):
        step = Decimal("5")
    elif amount < _THOUSAND:
        step = Decimal("10")
    else:
        step = Decimal("100")
    rounded = _round_to_step(amount, step)

    if rounded >= _THOUSAND:
        larger = _shift_exponent(rounded, -3)
        unit = "kg" if quantity.unit == "g" else "l"
        return f"{_decimal_text(larger)} {unit}"
    return f"{_decimal_text(rounded)} {quantity.unit}"


def _fold(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value.casefold())
    return "".join(
        character
        for character in normalized
        if not unicodedata.combining(character)
    )


_FOLDED_IMPERATIVES = frozenset(_fold(word) for word in _ALLOWED_IMPERATIVES)
_FOLDED_GENERIC_STEPS = frozenset(_fold(value) for value in _GENERIC_STEPS)


def _phrase_pattern(value: str) -> re.Pattern[str]:
    words = re.findall(r"\w+", _fold(value), re.UNICODE)
    return re.compile(r"(?<!\w)" + r"\s+".join(map(re.escape, words)) + r"(?!\w)")


@lru_cache(maxsize=1)
def _catalog_ingredients() -> tuple[Ingredient, ...]:
    return load_ingredient_catalog().all()


def _ingredient_forms(ingredient: Ingredient) -> tuple[str, ...]:
    forms = {
        ingredient.name,
        _QUANTITY_NAMES.get(ingredient.id, ingredient.name),
        *_EXTRA_INGREDIENT_FORMS.get(ingredient.id, ()),
    }
    return tuple(sorted(forms))


def _quantity_name(rendered: RenderedIngredient) -> str:
    if rendered.quantity.unit == "piece" and rendered.quantity.amount <= _ONE:
        if rendered.ingredient.id == "egg":
            return "vajca"
    return _QUANTITY_NAMES.get(rendered.ingredient.id, rendered.ingredient.name)


def _normalize_rendered_text(value: str) -> str:
    value = re.sub(r"\s+", " ", value).strip()
    return re.sub(r"\s+([,.;:])", r"\1", value)


def _render_template(
    template: str,
    slots: Mapping[str, _SlotWording],
    portions: int,
    *,
    label: str,
) -> str:
    chunks = []
    try:
        parsed = Formatter().parse(template)
        for literal, field, format_spec, conversion in parsed:
            chunks.append(literal)
            if field is None:
                continue
            if format_spec or conversion:
                raise ValueError(f"nepovolený placeholder: {field}")
            if field == "portions":
                chunks.append(str(portions))
                continue
            parts = field.split(".")
            if len(parts) != 2 or parts[1] not in {"name", "amount", "cut"}:
                raise ValueError(f"nepovolený placeholder: {field}")
            wording = slots.get(parts[0])
            if wording is None:
                raise ValueError(f"neznáma pozícia v placeholderi: {parts[0]}")
            chunks.append(str(getattr(wording, parts[1])))
    except (AttributeError, KeyError, ValueError) as exc:
        raise ValueError(f"neplatný placeholder v {label}: {template}") from exc

    result = _normalize_rendered_text("".join(chunks))
    if "{" in result or "}" in result:
        raise ValueError(f"nevyriešený placeholder v {label}: {template}")
    return result


def _validate_inputs(adults: int, children: int, covered_days: int) -> None:
    if (
        isinstance(adults, bool)
        or isinstance(children, bool)
        or not isinstance(adults, int)
        or not isinstance(children, int)
        or adults < 0
        or children < 0
        or adults + children < 1
    ):
        raise ValueError("Počet dospelých a detí musí tvoriť aspoň jednu osobu.")
    if (
        isinstance(covered_days, bool)
        or not isinstance(covered_days, int)
        or not 1 <= covered_days <= 3
    ):
        raise ValueError("Počet pokrytých dní musí byť celé číslo od 1 do 3.")


def _validated_selections(candidate: RecipeCandidate) -> tuple[SlotSelection, ...]:
    template_slots = {slot.key: slot for slot in candidate.template.slots}
    if len(template_slots) != len(candidate.template.slots):
        raise ValueError("Šablóna obsahuje duplicitnú pozíciu suroviny.")

    result = []
    selected_keys = set()
    for selection in candidate.selections:
        slot = template_slots.get(selection.slot.key)
        if slot is None or slot != selection.slot:
            raise ValueError("Vybraná pozícia nezodpovedá šablóne receptu.")
        if slot.key in selected_keys:
            raise ValueError("Recept obsahuje duplicitne vybranú pozíciu.")
        if selection.ingredient.id not in slot.candidates:
            raise ValueError("Vybraná surovina nie je povolená v pozícii receptu.")
        selected_keys.add(slot.key)
        result.append(selection)

    missing = [
        slot.key
        for slot in candidate.template.slots
        if slot.required and slot.key not in selected_keys
    ]
    if missing:
        raise ValueError("Receptu chýba povinná surovina: " + ", ".join(missing))
    return tuple(result)


def _render_ingredient(
    selection: SlotSelection,
    *,
    adults: int,
    children: int,
    covered_days: int,
) -> RenderedIngredient:
    child_equivalents = _multiply_exact(
        Decimal(children), selection.slot.child_factor
    )
    adult_equivalents = _add_exact(Decimal(adults), child_equivalents)
    batch_equivalents = _multiply_exact(adult_equivalents, Decimal(covered_days))
    amount = _multiply_exact(selection.slot.amount_per_adult, batch_equivalents)
    quantity = Quantity(amount, selection.slot.unit)
    display_amount = _display_amount(quantity)
    return RenderedIngredient(
        selection=selection,
        quantity=quantity,
        display_amount=display_amount,
        label=f"{display_amount} · {selection.ingredient.name}",
    )


def _edible_grams(rendered: RenderedIngredient) -> Decimal:
    quantity = rendered.quantity
    ingredient = rendered.ingredient
    if quantity.unit == "g":
        grams = quantity.amount
    elif quantity.unit == "piece":
        if ingredient.grams_per_piece is None:
            raise ValueError(
                f"Surovina {ingredient.name} nemá prepočet kusov na gramy."
            )
        grams = _multiply_exact(quantity.amount, ingredient.grams_per_piece)
    else:
        if ingredient.density_g_per_ml is None:
            raise ValueError(
                f"Surovina {ingredient.name} nemá prepočet mililitrov na gramy."
            )
        grams = _multiply_exact(quantity.amount, ingredient.density_g_per_ml)
    return _multiply_exact(grams, ingredient.edible_ratio)


def _pantry_names(candidate: RecipeCandidate) -> tuple[str, ...]:
    by_id = {ingredient.id: ingredient for ingredient in _catalog_ingredients()}
    names = []
    for ingredient_id in candidate.template.pantry_basics:
        ingredient = by_id.get(ingredient_id)
        if ingredient is None:
            raise ValueError(f"Neznáma základná surovina: {ingredient_id}")
        names.append(ingredient.name)
    return tuple(names)


def _validate_step_detail(step: str) -> None:
    folded = _fold(step)
    first = re.match(r"\w+", step, re.UNICODE)
    if first is None or _fold(first.group()) not in _FOLDED_IMPERATIVES:
        raise ValueError("Každý krok musí začínať slovesom v rozkazovacom spôsobe.")

    concise = re.sub(r"[^\w\s]", "", folded).strip()
    if concise in _FOLDED_GENERIC_STEPS:
        raise ValueError("Samostatný pokyn je príliš všeobecný na varenie.")

    if _COOKING_ACTION.search(folded) is None:
        return
    if folded.startswith("predhrej"):
        if _HEAT.search(folded) is None:
            raise ValueError("Predhriatie musí uvádzať teplotu.")
        if _PREHEAT_READY.search(folded) is None:
            raise ValueError("Predhriatie musí uvádzať kontrolný znak hotovosti.")
        return
    if "prived" in folded and "do varu" in folded:
        if _VESSEL.search(folded) is None or _HEAT.search(folded) is None:
            raise ValueError("Privedenie do varu musí uvádzať nádobu a ohrev.")
        return
    if not folded.startswith("rozohrej") and _AMOUNT.search(folded) is None:
        raise ValueError("Tepelný krok musí uvádzať množstvo suroviny.")
    if _VESSEL.search(folded) is None:
        raise ValueError("Tepelný krok musí uvádzať nádobu alebo pomôcku.")
    if _HEAT.search(folded) is None:
        raise ValueError("Tepelný krok musí uvádzať intenzitu ohrevu alebo teplotu.")
    if _TIME.search(folded) is None:
        raise ValueError("Tepelný krok musí uvádzať čas prípravy v minútach.")
    if _DONENESS.search(folded) is None:
        raise ValueError("Tepelný krok musí uvádzať viditeľný výsledok hotovosti.")


def _validate_ingredient_mentions(
    instructions: Sequence[str],
    rendered: Sequence[RenderedIngredient],
    pantry_ids: Sequence[str],
) -> None:
    folded_steps = _fold(" ".join(instructions))
    selected_ids = {item.ingredient.id for item in rendered}
    allowed_ids = selected_ids | set(pantry_ids)

    for item in rendered:
        if not any(
            _phrase_pattern(form).search(folded_steps)
            for form in _ingredient_forms(item.ingredient)
        ):
            raise ValueError(
                f"Surovina {item.ingredient.name} zo zoznamu chýba v postupe."
            )

    known = {ingredient.id: ingredient for ingredient in _catalog_ingredients()}
    for ingredient_id, ingredient in known.items():
        if ingredient_id in allowed_ids:
            continue
        terms = {
            ingredient.name,
            *ingredient.synonyms,
            _QUANTITY_NAMES.get(ingredient_id, ingredient.name),
            *_EXTRA_INGREDIENT_FORMS.get(ingredient_id, ()),
        }
        if any(_phrase_pattern(term).search(folded_steps) for term in terms):
            raise ValueError(
                f"Surovina {ingredient.name} je v postupe, ale chýba v zozname surovín."
            )


def _validate_rendered_language(
    name: str,
    instructions: Sequence[str],
    rendered: Sequence[RenderedIngredient],
    pantry_ids: Sequence[str],
) -> None:
    if len(instructions) < 3:
        raise ValueError("Recept musí mať aspoň tri konkrétne kroky.")
    if any("{" in step or "}" in step for step in instructions):
        raise ValueError("Recept obsahuje nevyriešený placeholder.")

    folded = _fold(f"{name} {' '.join(instructions)}")
    if re.search(r"\bstehennych\s+rezikov\b", folded):
        raise ValueError("Recept obsahuje nesprávnu slovenčinu: stehenných rezíkov.")

    for step in instructions:
        _validate_step_detail(step)
    _validate_ingredient_mentions(instructions, rendered, pantry_ids)

    # Existing high-confidence checks remain the final release gate. They do
    # not create or rewrite recipe text.
    validate_recipe_language(name, list(instructions))


def render_meal(
    candidate: RecipeCandidate,
    *,
    adults: int,
    children: int,
    covered_days: int,
) -> RenderedMeal:
    """Render one ranked candidate without rounding its structured values."""
    if not isinstance(candidate, RecipeCandidate):
        raise TypeError("candidate must be a RecipeCandidate")
    _validate_inputs(adults, children, covered_days)
    selections = _validated_selections(candidate)
    rendered = tuple(
        _render_ingredient(
            selection,
            adults=adults,
            children=children,
            covered_days=covered_days,
        )
        for selection in selections
    )
    portions = (adults + children) * covered_days
    nutrition = estimate_recipe_nutrition(
        [(item.ingredient, _edible_grams(item)) for item in rendered],
        adult_servings=Decimal(portions),
    )

    title_slots = {
        item.slot.key: _SlotWording(
            name=item.ingredient.name,
            amount=item.display_amount,
            cut=item.slot.cut or "",
        )
        for item in rendered
    }
    instruction_slots = {
        item.slot.key: _SlotWording(
            name=_quantity_name(item),
            amount=item.display_amount,
            cut=item.slot.cut or "",
        )
        for item in rendered
    }
    name = _render_template(
        candidate.template.name_template,
        title_slots,
        portions,
        label="názve receptu",
    )
    name = name[:1].upper() + name[1:]
    instructions = tuple(
        _render_template(
            instruction.text,
            instruction_slots,
            portions,
            label="kroku receptu",
        )
        for instruction in candidate.template.instructions
    )
    _validate_rendered_language(
        name,
        instructions,
        rendered,
        candidate.template.pantry_basics,
    )

    return RenderedMeal(
        template_id=candidate.template.id,
        candidate_key=candidate.key,
        name=name,
        portions=portions,
        covered_days=covered_days,
        ingredients=rendered,
        pantry_basics=_pantry_names(candidate),
        instructions=instructions,
        nutrition=nutrition,
    )
