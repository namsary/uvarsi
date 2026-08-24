#!/usr/bin/env python3
"""Build the public landing receipt exclusively from verified SQLite offers."""
import json
import os
import sqlite3
import sys
from contextlib import closing
from datetime import date
from pathlib import Path

from app import naklady
from app.landing_data import validate_landing_data, write_landing_data_atomic
from app.offer_data import ALLOWED_STORES
from app.receipt_data import (
    MIN_COMPOSABLE_OFFERS,
    TOO_FEW_OFFERS,
    StructuralFailure,
    build_public_receipt,
    composition_prompt,
    priceable_offers,
)
from app.weekly_data import current_verified_offers


LANDING_DATA_PATH = Path("/var/lib/uvarsi/landing_data.json")
DATABASE_PATH = "/opt/uvarsi/uvarsi.db"
ENV_FILE = "/opt/uvarsi/uvarsi.env"
# Dohoda s dozorcom: 1 = skús o hodinu znova, 3 = opakovanie nemá zmysel.
EXIT_RETRY = 1


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
        offers = priceable_offers(current_verified_offers(con, ALLOWED_STORES, today))
        if len(offers) < MIN_COMPOSABLE_OFFERS:
            raise StructuralFailure(TOO_FEW_OFFERS)
        model_output = compose(composition_prompt(offers))
        payload = build_public_receipt(con, model_output, today=today)
    validate_landing_data(payload, today)
    write_landing_data_atomic(path, payload)
    return payload


def load_api_key():
    key = os.environ.get("ANTHROPIC_API_KEY")
    if key:
        return key
    try:
        with open(ENV_FILE, encoding="utf-8") as env_file:
            for line in env_file:
                name, separator, value = line.partition("=")
                if separator and name.strip() == "ANTHROPIC_API_KEY":
                    key = value.strip().strip('"').strip("'")
                    if key:
                        return key
    except FileNotFoundError:
        pass
    raise StructuralFailure("Chýba ANTHROPIC_API_KEY — nechávam starý bloček.")


MODEL_BLOCEK = "claude-sonnet-5"


def compose_with_llm(prompt):
    """The model may choose stable keys and write meal content; it never supplies prices."""
    api_key = load_api_key()
    import anthropic

    # Strop sa overuje TU, tesne pri platenom volaní — nie o poschodie vyššie,
    # kde by sa na neho dalo zabudnúť. Keď rozpočet nestačí, volanie sa vôbec
    # neuskutoční a starý bloček ostáva nedotknutý.
    with closing(naklady.pripoj(os.environ.get("UVARSI_DB", DATABASE_PATH))) as ucty:
        client = naklady.strazeny_klient(
            ucty,
            anthropic.Anthropic(api_key=api_key, timeout=120.0, max_retries=1),
            "blocek",
        )
        message = client.messages.create(
            model=MODEL_BLOCEK, max_tokens=4000,
            messages=[{"role": "user", "content": prompt}],
        )
    text = "".join(block.text for block in message.content if getattr(block, "type", None) == "text").strip()
    try:
        return json.loads(text.removeprefix("```json").removesuffix("```").strip())
    except json.JSONDecodeError as error:
        raise ValueError("Model nevrátil platný JSON.") from error


def main():
    """Odlíš štrukturálny pád od dočasného, nech dozorca nepáli kredit nadarmo."""
    path = landing_data_output_path(sys.argv[1:])
    database = os.environ.get("UVARSI_DB", DATABASE_PATH)
    try:
        refresh_from_db(path, database, compose_with_llm, today=date.today())
    except naklady.RozpocetVycerpany as odmietnutie:
        # Opakovanie by nič nezmenilo a majiteľ musí vedieť, že sa minul rozpočet,
        # nie len že „bloček je starý“. Starý JSON ostáva na disku nedotknutý —
        # nič sa nevymýšľa a landing radšej prizná, že dáta nie sú aktuálne.
        print(f"ROZPOČET VYČERPANÝ: {odmietnutie}", file=sys.stderr)
        raise SystemExit(StructuralFailure.EXIT_CODE) from None
    except StructuralFailure as failure:
        print(f"ŠTRUKTURÁLNA CHYBA: {failure}", file=sys.stderr)
        raise SystemExit(StructuralFailure.EXIT_CODE) from None
    except Exception as error:  # sieť, model, zamknutá DB — o hodinu to môže vyjsť
        print(f"DOČASNÁ CHYBA: {type(error).__name__}: {error}", file=sys.stderr)
        raise SystemExit(EXIT_RETRY) from None


if __name__ == "__main__":
    main()
