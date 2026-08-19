#!/usr/bin/env python3
"""Build the public landing receipt exclusively from verified SQLite offers."""
import json
import os
import sqlite3
import sys
from datetime import date
from pathlib import Path

from app.landing_data import validate_landing_data, write_landing_data_atomic
from app.receipt_data import build_public_receipt, composition_prompt, current_verified_offers, eligible_offers


LANDING_DATA_PATH = Path("/var/lib/uvarsi/landing_data.json")


def landing_data_output_path(arguments):
    if not arguments:
        return LANDING_DATA_PATH
    if len(arguments) == 1 and Path(arguments[0]) == LANDING_DATA_PATH:
        return LANDING_DATA_PATH
    raise SystemExit("Použitie: refresh_blocek.py /var/lib/uvarsi/landing_data.json")


def refresh_from_db(path, database, compose, today=None):
    """Compose content after the DB gate, then atomically publish derived data."""
    today = today or date.today()
    with sqlite3.connect(database) as con:
        con.row_factory = sqlite3.Row
        offers = eligible_offers(current_verified_offers(con, today))
        if len(offers) < 3:
            raise SystemExit("Málo overených ponúk s bežnou cenou — nechávam starý bloček.")
        model_output = compose(composition_prompt(offers))
        payload = build_public_receipt(con, model_output, today=today)
    validate_landing_data(payload, today)
    write_landing_data_atomic(path, payload)
    return payload


def compose_with_llm(prompt):
    """The model may choose IDs and write meal content; it never supplies prices."""
    import anthropic

    client = anthropic.Anthropic(timeout=120.0, max_retries=1)
    message = client.messages.create(
        model="claude-sonnet-5", max_tokens=4000,
        messages=[{"role": "user", "content": prompt}],
    )
    text = "".join(block.text for block in message.content if getattr(block, "type", None) == "text").strip()
    try:
        return json.loads(text.removeprefix("```json").removesuffix("```").strip())
    except json.JSONDecodeError as error:
        raise ValueError("Model nevrátil platný JSON.") from error


def main():
    path = landing_data_output_path(sys.argv[1:])
    database = os.environ.get("UVARSI_DB")
    if not database:
        raise SystemExit("Chýba UVARSI_DB — nechávam starý bloček.")
    refresh_from_db(path, database, compose_with_llm, today=date.today())


if __name__ == "__main__":
    main()
