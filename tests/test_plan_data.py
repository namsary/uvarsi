import sqlite3
from datetime import date

import pytest

from app.plan_data import build_personal_plan, meal_count_for_frequency


TODAY = date(2026, 8, 18)


def connection(rows):
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.execute(
        """CREATE TABLE akcie (
            id INTEGER PRIMARY KEY, tyzden TEXT, obchod TEXT, nazov TEXT,
            kategoria TEXT, cena REAL, povodna REAL, zlava TEXT, jednotka TEXT,
            source_url TEXT, source_page INTEGER, valid_from TEXT, valid_to TEXT
        )"""
    )
    con.executemany("INSERT INTO akcie VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
    return con


def verified_rows():
    return [
        (1, "2026-08-17", "Lidl", "Mlieko", "mliecne", 1.10, 1.50, "-27 %", "1 l",
         "https://source.test/lidl", 1, "2026-08-17", "2026-08-23"),
        (2, "2026-08-17", "Tesco", "Chlieb", "pecivo", 1.20, 1.80, "-33 %", "500 g",
         "https://source.test/tesco", 2, "2026-08-17", "2026-08-23"),
        (3, "2026-08-17", "Lidl", "Maslo", "mliecne", 2.00, 2.50, "-20 %", "250 g",
         "https://source.test/lidl", 3, "2026-08-17", "2026-08-23"),
        (4, "2026-08-17", "Lidl", "Neplatná ponuka", "mliecne", 1.00, 1.50, "-33 %", "1 l",
         "https://source.test/expired", 4, "2026-08-10", "2026-08-17"),
    ]


def model_output(items=None):
    return {
        "meals": [
            {
                "day": "PO", "name": "Mliečna večera",
                "instructions": ["Ohrej mlieko a podávaj s chlebom."],
                "items": items if items is not None else [{"offer_id": 1, "quantity": 2}],
                "pantry_ingredients": ["soľ"],
            },
            {
                "day": "ST", "name": "Chlieb s maslom",
                "instructions": ["Natieraj maslo na chlieb."],
                "items": [{"offer_id": 2, "quantity": 1}],
            },
            {
                "day": "PI", "name": "Maslový toast",
                "instructions": ["Opeč chlieb s maslom."],
                "items": [{"offer_id": 3, "quantity": 1}],
            },
        ]
    }


def test_reconstructs_grouped_purchases_and_totals_only_from_verified_offers():
    plan = build_personal_plan(
        connection(verified_rows()), model_output(), ["Lidl", "Tesco"], 2,
        pantry=["soľ"], today=TODAY
    )

    assert plan == {
        "tyzden": "2026-08-17",
        "jedla": [
            {
                "den": "PO", "nazov": "Mliečna večera",
                "recept": {"kroky": ["Ohrej mlieko a podávaj s chlebom."]},
                "suroviny": [
                    {"offer_id": 1, "nazov": "Mlieko", "obchod": "Lidl", "jednotka": "1 l",
                     "mnozstvo": 2, "cena": "2,20", "povodna": "3,00", "zlava": "-27 %"},
                    {"spajza": "soľ"},
                ],
            },
            {
                "den": "ST", "nazov": "Chlieb s maslom",
                "recept": {"kroky": ["Natieraj maslo na chlieb."]},
                "suroviny": [
                    {"offer_id": 2, "nazov": "Chlieb", "obchod": "Tesco", "jednotka": "500 g",
                     "mnozstvo": 1, "cena": "1,20", "povodna": "1,80", "zlava": "-33 %"},
                ],
            },
            {
                "den": "PI", "nazov": "Maslový toast",
                "recept": {"kroky": ["Opeč chlieb s maslom."]},
                "suroviny": [
                    {"offer_id": 3, "nazov": "Maslo", "obchod": "Lidl", "jednotka": "250 g",
                     "mnozstvo": 1, "cena": "2,00", "povodna": "2,50", "zlava": "-20 %"},
                ],
            },
        ],
        "nakupny_zoznam": [
            {"obchod": "Lidl", "polozky": [
                {"offer_id": 3, "nazov": "Maslo", "jednotka": "250 g", "mnozstvo": 1,
                 "cena": "2,00", "povodna": "2,50", "zlava": "-20 %"},
                {"offer_id": 1, "nazov": "Mlieko", "jednotka": "1 l", "mnozstvo": 2,
                 "cena": "2,20", "povodna": "3,00", "zlava": "-27 %"},
            ]},
            {"obchod": "Tesco", "polozky": [
                {"offer_id": 2, "nazov": "Chlieb", "jednotka": "500 g", "mnozstvo": 1,
                 "cena": "1,20", "povodna": "1,80", "zlava": "-33 %"},
            ]},
        ],
        "nakup_spolu": "5,40", "bezne": "7,30", "usetris": "1,90",
    }


@pytest.mark.parametrize(
    "mutate, message",
    [
        (lambda plan: plan.update(nakup_spolu="0,01"), "nepovolené"),
        (lambda plan: plan["meals"][0]["items"][0].update(price="0,01"), "nepovolené"),
        (lambda plan: plan["meals"][0].update(instructions=[""]), "pokyn"),
        (lambda plan: plan["meals"][0]["items"][0].update(quantity=0), "Množstvo"),
        (lambda plan: plan["meals"][1]["items"].__setitem__(0, {"offer_id": 1, "quantity": 1}), "duplicitné"),
        (lambda plan: plan["meals"][0]["items"][0].update(offer_id=4), "neznáme"),
    ],
)
def test_rejects_model_commercial_fields_malformed_content_duplicates_and_expired_ids(mutate, message):
    payload = model_output()
    mutate(payload)

    with pytest.raises(ValueError, match=message):
        build_personal_plan(
            connection(verified_rows()), payload, ["Lidl", "Tesco"], 2,
            pantry=["soľ"], today=TODAY
        )


def test_requires_the_current_frequency_recipe_count():
    assert meal_count_for_frequency(1) == 5
    assert meal_count_for_frequency(2) == 3
    assert meal_count_for_frequency(3) == 2
    assert meal_count_for_frequency(7) == 3

    with pytest.raises(ValueError, match="počet jedál"):
        build_personal_plan(
            connection(verified_rows()), {"meals": model_output()["meals"][:2]},
            ["Lidl", "Tesco"], 2, pantry=["soľ"], today=TODAY
        )
