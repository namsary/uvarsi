import re
from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING
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


_UNIT_CONVERSIONS: dict[str, tuple[Unit, Decimal]] = {
    "g": ("g", Decimal("1")),
    "kg": ("g", Decimal("1000")),
    "ml": ("ml", Decimal("1")),
    "l": ("ml", Decimal("1000")),
    "piece": ("piece", Decimal("1")),
}
_QUANTITY_PATTERN = re.compile(r"([0-9]+(?:\.[0-9]+)?) (g|kg|ml|l|piece)")


def parse_quantity(text: str) -> Quantity:
    match = _QUANTITY_PATTERN.fullmatch(text)
    if match is None:
        raise ValueError("quantity must use '<decimal> <g|kg|ml|l|piece>' syntax")
    amount_text, unit_text = match.groups()
    unit, multiplier = _UNIT_CONVERSIONS[unit_text]
    return Quantity(Decimal(amount_text) * multiplier, unit)


def purchase_requirement(
    required: Quantity,
    pantry: Quantity,
    package: PackageSize,
) -> PurchaseRequirement:
    if required.unit != pantry.unit or required.unit != package.content.unit:
        raise ValueError("required, pantry, and package quantities must use compatible units")

    used_from_pantry_amount = min(required.amount, pantry.amount)
    missing_amount = required.amount - used_from_pantry_amount
    packages = int(
        (missing_amount / package.content.amount).to_integral_value(
            rounding=ROUND_CEILING
        )
    )
    to_buy_amount = package.content.amount * packages
    leftover_amount = to_buy_amount - missing_amount

    return PurchaseRequirement(
        required=required,
        used_from_pantry=Quantity(used_from_pantry_amount, required.unit),
        missing=Quantity(missing_amount, required.unit),
        packages=packages,
        to_buy=Quantity(to_buy_amount, required.unit),
        used_from_purchase=Quantity(missing_amount, required.unit),
        leftover=Quantity(leftover_amount, required.unit),
    )
