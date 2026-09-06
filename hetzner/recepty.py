#!/usr/bin/env python3
"""Kompatibilná kontrola landing dát po prechode na kurátorované recepty.

Staršie nasadenia môžu tento súbor stále spúšťať z cronu. Preto ho
ponechávame ako bezpečný, bezplatný validátor: načíta aktuálny landing JSON,
overí jeho štruktúru a skončí. Recepty nevytvára, neupravuje a nevolá žiadny
jazykový model. Používateľské jedálničky skladá výhradne lokálny
deterministický katalóg; platené API ostáva len v zbere letákov.
"""
import sys
from datetime import date
from pathlib import Path

try:
    from app.landing_data import load_landing_data, validate_landing_data
except ImportError:  # spúšťané priamo z /opt/uvarsi
    from landing_data import load_landing_data, validate_landing_data


LANDING_DATA_PATH = Path("/var/lib/uvarsi/landing_data.json")


def landing_data_input_path(arguments):
    """Nástroj smie čítať len kanonický, overený landing JSON."""
    if not arguments:
        return LANDING_DATA_PATH
    if len(arguments) == 1 and Path(arguments[0]) == LANDING_DATA_PATH:
        return LANDING_DATA_PATH
    raise SystemExit("Použitie: recepty.py /var/lib/uvarsi/landing_data.json")


def validate_existing_landing(path: str | Path, today: date | None = None) -> dict:
    payload = load_landing_data(path)
    return validate_landing_data(payload, today or date.today())


def main(today: date | None = None):
    path = landing_data_input_path(sys.argv[1:])
    try:
        payload = validate_existing_landing(path, today=today)
    except (OSError, ValueError) as error:
        raise SystemExit(f"Landing dáta nie sú použiteľné: {error}") from error
    recipes = sum(
        1 for meal in payload["receipt"]["meals"] if meal.get("recipe")
    )
    print(
        f"[OK] Landing dáta sú platné; hotových receptov: {recipes}. "
        "Nič nevytváram ani neprepisujem.",
        flush=True,
    )


if __name__ == "__main__":
    main()
