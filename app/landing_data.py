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


def _optional_amount(value: object) -> Decimal | None:
    return None if value is None else _amount(value)


def _validate_item_saving(item: dict) -> bool:
    """Úsporu smie tvrdiť len položka s overenou prečiarknutou cenou.

    Vráti True, keď položka bežnú cenu naozaj má. Cenovka bez prečiarknutej
    ceny je bežná — taká položka je platná, len nesmie nič ušetriť.
    """
    regular = _optional_amount(item.get("original_price"))
    savings = _optional_amount(item.get("savings"))
    if regular is None:
        if savings not in (None, Decimal("0")):
            raise ValueError("Položka bločku tvrdí úsporu bez overenej bežnej ceny.")
        return False
    if "price" not in item:
        raise ValueError("Položka s bežnou cenou musí mať aj akciovú cenu.")
    price = _amount(item["price"])
    if regular < price:
        raise ValueError("Bežná cena položky nesmie byť nižšia ako akciová.")
    if savings is None or savings != regular - price:
        raise ValueError("Nesedí úspora položky v bločku.")
    return True


def _validate_recipe(meal: dict) -> None:
    """Recept je nepovinný — keď tam je, musí sa dať zobraziť bez dopočítavania.

    Kroky píše model (recepty.py). Komerčné údaje sa doňho nikdy nedostanú:
    ceny, obchody aj úspora žijú v položkách a pochádzajú výhradne z DB.
    """
    recipe = meal.get("recipe")
    if recipe is None:
        return
    if not isinstance(recipe, dict) or set(recipe) - {"min", "steps", "steps_total"}:
        raise ValueError("Recept v bločku obsahuje nepovolené polia.")
    steps = recipe.get("steps")
    if not isinstance(steps, list) or not steps:
        raise ValueError("Recept v bločku musí mať kroky.")
    for step in steps:
        _required_text(step, "krok receptu")
    total = _validate_count(recipe.get("steps_total", len(steps)), "krokov receptu")
    if total < len(steps):
        raise ValueError("Recept tvrdí menej krokov, než sám vypisuje.")
    if "min" in recipe and _validate_count(recipe["min"], "minút receptu") <= 0:
        raise ValueError("Neplatný počet minút receptu.")


def _validate_count(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"Neplatný počet {field} v bločku.")
    return value


def _validate_current_sources(sources: object, today: date) -> None:
    if not isinstance(sources, list) or not sources:
        raise ValueError("Bloček nemá doložené zdroje s údajom o platnosti cien.")
    for source in sources:
        if not isinstance(source, dict):
            raise ValueError("Bloček má neplatný zdroj s platnosťou cien.")
        valid_to = source.get("valid_to")
        if not isinstance(valid_to, str):
            raise ValueError("Zdroj bločku nemá platný dátum platnosti cien.")
        try:
            expires = date.fromisoformat(valid_to)
        except ValueError as error:
            raise ValueError("Zdroj bločku nemá platný dátum platnosti cien.") from error
        if expires < today:
            raise ValueError("Zdroj bločku je po platnosti.")


def validate_landing_data(payload: dict, today: date | None = None) -> dict:
    today = today or date.today()
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
    _validate_current_sources(payload["sources"], today)

    receipt = payload.get("receipt")
    if not isinstance(receipt, dict) or not isinstance(receipt.get("meals"), list) or not receipt["meals"]:
        raise ValueError("Bloček musí obsahovať aspoň jedno jedlo.")
    items_seen = 0
    items_with_regular_price = 0
    for meal in receipt["meals"]:
        if not isinstance(meal, dict):
            raise ValueError("Jedlo v bločku musí byť objekt.")
        _required_text(meal.get("day"), "day")
        _required_text(meal.get("name"), "name")
        _validate_recipe(meal)
        if not isinstance(meal.get("items"), list):
            raise ValueError("Položky jedla musia byť zoznam.")
        for item in meal["items"]:
            if not isinstance(item, dict):
                raise ValueError("Položka jedla musí byť objekt.")
            _required_text(item.get("name"), "name")
            _required_text(item.get("store"), "store")
            if "price" in item:
                _amount(item["price"])
            items_seen += 1
            items_with_regular_price += _validate_item_saving(item)

    total = _amount(receipt.get("nakup_spolu"))
    regular = _amount(receipt.get("bezne"))
    savings = _amount(receipt.get("usetris"))
    if (regular - total).quantize(Decimal("0.01")) != savings.quantize(Decimal("0.01")):
        raise ValueError("Nesedí úspora v bločku.")

    # Nepovinné počítadlá — keď ich bloček nesie, musia sedieť s položkami,
    # aby sa úspora nedala tvrdiť bez jedinej doloženej prečiarknutej ceny.
    if "polozky" in receipt or "polozky_s_beznou_cenou" in receipt:
        counted = _validate_count(receipt.get("polozky"), "položiek")
        substantiated = _validate_count(receipt.get("polozky_s_beznou_cenou"), "bežných cien")
        if counted != items_seen or substantiated != items_with_regular_price:
            raise ValueError("Nesedí počet položiek s overenou bežnou cenou v bločku.")
        if substantiated == 0 and savings != Decimal("0"):
            raise ValueError("Bloček tvrdí úsporu bez overenej bežnej ceny.")

    return payload


def model_example_is_publishable(payload: object, today: date | None = None) -> bool:
    """Smie modelový príklad na landingu vôbec ísť von?

    Sekcia tvrdí konkrétnu týždennú úsporu a z nej odvodenú ročnú projekciu.
    Nedoložené tvrdenie o úspore je klamlivá obchodná praktika, takže stačí
    jediná diera a nesmie sa vykresliť nič:

    * dáta neprejdú `validate_landing_data` (starý týždeň, rozbitá matematika),
    * ani jedna položka nemá overenú prečiarknutú bežnú cenu,
    * úspora vyjde nula — vtedy niet čo tvrdiť.

    Rovnaké pravidlo drží aj prehliadač (`modelIsPublishable` v index.html),
    aby odobratie atribútu `hidden` nikdy nestačilo na zverejnenie čísel.
    """
    if not isinstance(payload, dict):
        return False
    try:
        validate_landing_data(payload, today)
    except ValueError:
        return False
    meals = payload["receipt"]["meals"]
    substantiated = any(
        item.get("original_price") is not None
        for meal in meals
        for item in meal["items"]
    )
    return substantiated and _amount(payload["receipt"]["usetris"]) > 0


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
