import json
import os
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

try:
    from .weekly_data import current_monday
except ImportError:  # spúšťané FastAPI modulom priamo z /opt/uvarsi/app
    from weekly_data import current_monday


def _amount(value: object) -> Decimal:
    try:
        amount = Decimal(str(value).strip().replace(",", "."))
    except (InvalidOperation, ValueError) as error:
        raise ValueError("Neplatná suma v bločku.") from error
    if not amount.is_finite() or amount < 0:
        raise ValueError("Neplatná suma v bločku.")
    return amount


def _required_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Chýba {field} v bločku.")
    return value


def validate_landing_data(payload: dict, today: date | None = None) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("Letákové dáta musia byť objekt.")
    if payload.get("schema_version") != 1:
        raise ValueError("Nepodporovaná verzia letákových dát.")
    if payload.get("week") != current_monday(today):
        raise ValueError("Letákové dáta nie sú pre aktuálny týždeň.")
    if not isinstance(payload.get("generated_at"), str):
        raise ValueError("Letákové dáta musia obsahovať generated_at.")
    try:
        datetime.fromisoformat(payload["generated_at"])
    except ValueError as error:
        raise ValueError("Neplatný generated_at v letákových dátach.") from error
    if not isinstance(payload.get("week_label"), str) or not payload["week_label"].strip():
        raise ValueError("Letákové dáta musia obsahovať week_label.")
    if not isinstance(payload.get("sources"), list):
        raise ValueError("Letákové dáta musia obsahovať sources.")

    receipt = payload.get("receipt")
    if not isinstance(receipt, dict) or not isinstance(receipt.get("meals"), list) or not receipt["meals"]:
        raise ValueError("Bloček musí obsahovať aspoň jedno jedlo.")
    for meal in receipt["meals"]:
        if not isinstance(meal, dict):
            raise ValueError("Jedlo v bločku musí byť objekt.")
        _required_text(meal.get("day"), "day")
        _required_text(meal.get("name"), "name")
        if not isinstance(meal.get("items"), list):
            raise ValueError("Položky jedla musia byť zoznam.")
        for item in meal["items"]:
            if not isinstance(item, dict):
                raise ValueError("Položka jedla musí byť objekt.")
            _required_text(item.get("name"), "name")
            _required_text(item.get("store"), "store")
            if "price" in item:
                _amount(item["price"])

    total = _amount(receipt.get("nakup_spolu"))
    regular = _amount(receipt.get("bezne"))
    savings = _amount(receipt.get("usetris"))
    if (regular - total).quantize(Decimal("0.01")) != savings.quantize(Decimal("0.01")):
        raise ValueError("Nesedí úspora v bločku.")

    return payload


def write_landing_data_atomic(path: str | Path, payload: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8") as file:
        json.dump(payload, file, ensure_ascii=False, separators=(",", ":"))
        file.flush()
        os.fsync(file.fileno())
    os.replace(temporary, path)


def load_landing_data(path: str | Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def landing_data_is_current(path: str | Path, today: date | None = None) -> bool:
    try:
        validate_landing_data(load_landing_data(path), today)
        return True
    except (FileNotFoundError, json.JSONDecodeError, OSError, ValueError):
        return False
