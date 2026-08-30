import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Literal


Unit = Literal["g", "ml", "piece"]


@dataclass(frozen=True)
class Quantity:
    amount: Decimal
    unit: Unit

    def __post_init__(self) -> None:
        if not isinstance(self.amount, Decimal):
            raise TypeError("quantity amount must be a Decimal")
        if self.unit not in ("g", "ml", "piece"):
            raise ValueError("quantity unit must be g, ml, or piece")
        if not self.amount.is_finite():
            raise ValueError("quantity amount must be finite")
        if self.amount < 0:
            raise ValueError("quantity amount cannot be negative")


@dataclass(frozen=True)
class PackageSize:
    content: Quantity

    def __post_init__(self) -> None:
        if self.content.amount == 0:
            raise ValueError("package content must be greater than zero")


@dataclass(frozen=True)
class PantryEntry:
    ingredient_id: str
    name: str
    quantity: Quantity | None


@dataclass(frozen=True)
class PurchaseRequirement:
    required: Quantity
    used_from_pantry: Quantity
    missing: Quantity
    packages: int
    to_buy: Quantity
    used_from_purchase: Quantity
    leftover: Quantity


_UNIT_CONVERSIONS: dict[str, tuple[Unit, int]] = {
    "g": ("g", 0),
    "kg": ("g", 3),
    "ml": ("ml", 0),
    "l": ("ml", 3),
    "piece": ("piece", 0),
}
_QUANTITY_PATTERN = re.compile(r"([0-9]+(?:\.[0-9]+)?) (g|kg|ml|l|piece)")


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


def _shift_exponent(value: Decimal, places: int) -> Decimal:
    coefficient, exponent = _coefficient_and_exponent(value)
    return _from_coefficient(coefficient, exponent + places)


def _subtract_exact(minuend: Decimal, subtrahend: Decimal) -> Decimal:
    if subtrahend.is_zero():
        return minuend
    if minuend == subtrahend:
        return Decimal("0")

    minuend_coefficient, minuend_exponent = _coefficient_and_exponent(minuend)
    subtrahend_coefficient, subtrahend_exponent = _coefficient_and_exponent(
        subtrahend
    )
    exponent = min(minuend_exponent, subtrahend_exponent)
    minuend_coefficient *= 10 ** (minuend_exponent - exponent)
    subtrahend_coefficient *= 10 ** (subtrahend_exponent - exponent)
    return _from_coefficient(minuend_coefficient - subtrahend_coefficient, exponent)


def _multiply_by_int(value: Decimal, multiplier: int) -> Decimal:
    coefficient, exponent = _coefficient_and_exponent(value)
    return _from_coefficient(coefficient * multiplier, exponent)


def _ceil_exact_ratio(dividend: Decimal, divisor: Decimal) -> int:
    if dividend.is_zero():
        return 0
    dividend_numerator, dividend_denominator = dividend.as_integer_ratio()
    divisor_numerator, divisor_denominator = divisor.as_integer_ratio()
    numerator = dividend_numerator * divisor_denominator
    denominator = dividend_denominator * divisor_numerator
    quotient, remainder = divmod(numerator, denominator)
    return quotient + int(remainder != 0)


def parse_quantity(text: str) -> Quantity:
    match = _QUANTITY_PATTERN.fullmatch(text)
    if match is None:
        raise ValueError("quantity must use '<decimal> <g|kg|ml|l|piece>' syntax")
    amount_text, unit_text = match.groups()
    unit, exponent_shift = _UNIT_CONVERSIONS[unit_text]
    return Quantity(_shift_exponent(Decimal(amount_text), exponent_shift), unit)


def purchase_requirement(
    required: Quantity,
    pantry: Quantity,
    package: PackageSize,
) -> PurchaseRequirement:
    if required.unit != pantry.unit or required.unit != package.content.unit:
        raise ValueError("required, pantry, and package quantities must use compatible units")

    used_from_pantry_amount = min(required.amount, pantry.amount)
    missing_amount = _subtract_exact(required.amount, used_from_pantry_amount)
    packages = _ceil_exact_ratio(missing_amount, package.content.amount)
    to_buy_amount = _multiply_by_int(package.content.amount, packages)
    leftover_amount = _subtract_exact(to_buy_amount, missing_amount)

    return PurchaseRequirement(
        required=required,
        used_from_pantry=Quantity(used_from_pantry_amount, required.unit),
        missing=Quantity(missing_amount, required.unit),
        packages=packages,
        to_buy=Quantity(to_buy_amount, required.unit),
        used_from_purchase=Quantity(missing_amount, required.unit),
        leftover=Quantity(leftover_amount, required.unit),
    )
