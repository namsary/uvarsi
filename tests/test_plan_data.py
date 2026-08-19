import sqlite3
from datetime import date

import pytest

from app.plan_data import build_personal_plan, meal_count_for_frequency, personal_plan_prompt
from app.plan_data import cached_plan_is_current
from app.offer_data import migrate_akcie_schema, offer_key_for, replace_store_week
from app.weekly_data import current_verified_offers


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
    migrate_akcie_schema(con)
    for row in con.execute("SELECT rowid, * FROM akcie").fetchall():
        offer = dict(row)
        key = offer_key_for(offer["tyzden"], offer)
        con.execute("UPDATE akcie SET offer_key=? WHERE rowid=?", (key, row[0]))
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


def verified_key(offer_id):
    fields = (
        "id", "tyzden", "obchod", "nazov", "kategoria", "cena", "povodna", "zlava",
        "jednotka", "source_url", "source_page", "valid_from", "valid_to",
    )
    row = dict(zip(fields, verified_rows()[offer_id - 1]))
    return offer_key_for(row["tyzden"], row)


def model_output(items=None):
    return {
        "meals": [
            {
                "day": "PO", "name": "Mliečna večera", "minutes": 25,
                "instructions": ["Ohrej mlieko a podávaj s chlebom."],
                "items": items if items is not None else [{"offer_key": verified_key(1), "quantity": 2}],
                "pantry_ingredients": ["soľ"],
            },
            {
                "day": "ST", "name": "Chlieb s maslom", "minutes": 15,
                "instructions": ["Natieraj maslo na chlieb."],
                "items": [{"offer_key": verified_key(2), "quantity": 1}],
            },
            {
                "day": "PI", "name": "Maslový toast", "minutes": 20,
                "instructions": ["Opeč chlieb s maslom."],
                "items": [{"offer_key": verified_key(3), "quantity": 1}],
            },
        ]
    }


def test_reconstructs_grouped_purchases_and_totals_only_from_verified_offers():
    plan = build_personal_plan(
        connection(verified_rows()), model_output(), ["Lidl", "Tesco"], 2, 4,
        pantry=["soľ"], today=TODAY
    )

    assert plan == {
        "tyzden": "2026-08-17",
        "jedla": [
            {
                "den": "PO", "nazov": "Mliečna večera",
                "recept": {"min": 25, "kroky": ["Ohrej mlieko a podávaj s chlebom."]},
                "suroviny": [
                    {"offer_key": verified_key(1), "nazov": "Mlieko", "obchod": "Lidl", "jednotka": "1 l",
                     "mnozstvo": 2, "cena": "2,20", "povodna": "3,00", "zlava": "-27 %"},
                    {"spajza": "soľ"},
                ],
            },
            {
                "den": "ST", "nazov": "Chlieb s maslom",
                "recept": {"min": 15, "kroky": ["Natieraj maslo na chlieb."]},
                "suroviny": [
                    {"offer_key": verified_key(2), "nazov": "Chlieb", "obchod": "Tesco", "jednotka": "500 g",
                     "mnozstvo": 1, "cena": "1,20", "povodna": "1,80", "zlava": "-33 %"},
                ],
            },
            {
                "den": "PI", "nazov": "Maslový toast",
                "recept": {"min": 20, "kroky": ["Opeč chlieb s maslom."]},
                "suroviny": [
                    {"offer_key": verified_key(3), "nazov": "Maslo", "obchod": "Lidl", "jednotka": "250 g",
                     "mnozstvo": 1, "cena": "2,00", "povodna": "2,50", "zlava": "-20 %"},
                ],
            },
        ],
        "nakupny_zoznam": [
            {"obchod": "Lidl", "polozky": [
                {"offer_key": verified_key(3), "nazov": "Maslo", "jednotka": "250 g", "mnozstvo": 1,
                 "cena": "2,00", "povodna": "2,50", "zlava": "-20 %"},
                {"offer_key": verified_key(1), "nazov": "Mlieko", "jednotka": "1 l", "mnozstvo": 2,
                 "cena": "2,20", "povodna": "3,00", "zlava": "-27 %"},
            ]},
            {"obchod": "Tesco", "polozky": [
                {"offer_key": verified_key(2), "nazov": "Chlieb", "jednotka": "500 g", "mnozstvo": 1,
                 "cena": "1,20", "povodna": "1,80", "zlava": "-33 %"},
            ]},
        ],
        "nakup_spolu": "5,40", "bezne": "7,30", "usetris": "1,90",
    }


def test_missing_original_price_contributes_sale_price_and_never_negative_savings():
    rows = verified_rows()
    rows[0] = tuple(list(rows[0][:6]) + [None] + list(rows[0][7:]))
    con = connection(rows)
    keys = {row["nazov"]: row["offer_key"] for row in current_verified_offers(con, ["Lidl", "Tesco"], TODAY)}
    payload = model_output()
    payload["meals"][0]["items"] = [{"offer_key": keys["Mlieko"], "quantity": 2}]
    payload["meals"][1]["items"] = [{"offer_key": keys["Chlieb"], "quantity": 1}]
    payload["meals"][2]["items"] = [{"offer_key": keys["Maslo"], "quantity": 1}]

    plan = build_personal_plan(
        con, payload, ["Lidl", "Tesco"], 2, 4, pantry=["soľ"], today=TODAY
    )

    assert plan["nakup_spolu"] == "5,40"
    assert plan["bezne"] == "6,50"
    assert plan["usetris"] == "1,10"


def test_positive_recipe_minutes_are_validated_and_emitted_for_every_meal():
    payload = model_output()
    for meal, minutes in zip(payload["meals"], (25, 35, 45)):
        meal["minutes"] = minutes

    plan = build_personal_plan(
        connection(verified_rows()), payload, ["Lidl", "Tesco"], 2, 4,
        pantry=["soľ"], today=TODAY,
    )

    assert [meal["recept"]["min"] for meal in plan["jedla"]] == [25, 35, 45]


@pytest.mark.parametrize("minutes", [0, -1, True, "30"])
def test_invalid_recipe_minutes_are_rejected(minutes):
    payload = model_output()
    payload["meals"][0]["minutes"] = minutes

    with pytest.raises(ValueError, match="minút"):
        build_personal_plan(
            connection(verified_rows()), payload, ["Lidl", "Tesco"], 2, 4,
            pantry=["soľ"], today=TODAY,
        )


@pytest.mark.parametrize(
    "mutate, message",
    [
        (lambda plan: plan.update(nakup_spolu="0,01"), "nepovolené"),
        (lambda plan: plan["meals"][0]["items"][0].update(price="0,01"), "nepovolené"),
        (lambda plan: plan["meals"][0].update(instructions=[""]), "pokyn"),
        (lambda plan: plan["meals"][0]["items"][0].update(quantity=0), "Množstvo"),
        (lambda plan: plan["meals"][1]["items"].__setitem__(0, {"offer_key": verified_key(1), "quantity": 1}), "duplicitné"),
        (lambda plan: plan["meals"][0]["items"][0].update(offer_key=verified_key(4)), "neznáme"),
    ],
)
def test_rejects_model_commercial_fields_malformed_content_duplicates_and_expired_ids(mutate, message):
    payload = model_output()
    mutate(payload)

    with pytest.raises(ValueError, match=message):
        build_personal_plan(
            connection(verified_rows()), payload, ["Lidl", "Tesco"], 2, 4,
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
            ["Lidl", "Tesco"], 2, 4, pantry=["soľ"], today=TODAY
        )


def test_personal_prompt_changes_quantities_and_servings_context_for_household_size():
    con = reusable_rowid_connection()
    ingest_three(con, "family")
    rows = current_verified_offers(con, ["Lidl"], TODAY)

    for_one = personal_plan_prompt(rows, 2, ["soľ"], household_size=1)
    for_twelve = personal_plan_prompt(rows, 2, ["soľ"], household_size=12)

    assert for_one != for_twelve
    assert "1 osôb" in for_one
    assert "12 osôb" in for_twelve
    assert "množstvá aj porcie" in for_one


def test_personal_plan_build_requires_validated_household_size():
    con = connection(verified_rows())

    plan = build_personal_plan(
        con, model_output(), ["Lidl", "Tesco"], 2, pantry=["soľ"],
        household_size=12, today=TODAY,
    )

    assert plan["jedla"][0]["nazov"] == "Mliečna večera"
    for invalid in (0, 13, True):
        with pytest.raises(ValueError, match="Počet osôb"):
            build_personal_plan(
                con, model_output(), ["Lidl", "Tesco"], 2, pantry=["soľ"],
                household_size=invalid, today=TODAY,
            )


def reusable_rowid_connection():
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.execute(
        """CREATE TABLE akcie (
            id INTEGER PRIMARY KEY, tyzden TEXT, obchod TEXT, nazov TEXT,
            kategoria TEXT, cena REAL, povodna REAL, zlava TEXT, jednotka TEXT
        )"""
    )
    return con


def ingest_three(con, prefix):
    offers = [
        {
            "obchod": "Lidl", "nazov": f"{prefix} {index}", "kategoria": "trvanlive",
            "cena": 1.00 + index / 10, "povodna": 2.00 + index / 10, "zlava": "-50 %",
            "jednotka": "1 ks", "source_url": f"https://source.test/{prefix}/{index}",
            "source_page": index, "valid_from": "2026-08-17", "valid_to": "2026-08-23",
        }
        for index in range(1, 4)
    ]
    replace_store_week(con, "2026-08-17", "Lidl", offers)


def test_cached_integer_ids_are_rejected_after_legacy_rowids_are_reused():
    con = reusable_rowid_connection()
    ingest_three(con, "old")
    stale_cache = {
        "jedla": [{"suroviny": [{"offer_id": 1}, {"offer_id": 2}, {"offer_id": 3}]}]
    }
    ingest_three(con, "new")
    current = current_verified_offers(con, ["Lidl"], TODAY)

    assert [row["id"] for row in current] == [1, 2, 3]
    assert cached_plan_is_current(stale_cache, current) is False


def test_delayed_model_keys_are_rejected_after_legacy_rowids_are_reused():
    con = reusable_rowid_connection()
    ingest_three(con, "old")
    old_keys = [row["offer_key"] for row in current_verified_offers(con, ["Lidl"], TODAY)]
    delayed = model_output()
    for meal, offer_key in zip(delayed["meals"], old_keys):
        meal["items"] = [{"offer_key": offer_key, "quantity": 1}]
    ingest_three(con, "new")

    with pytest.raises(ValueError, match="neznáme"):
        build_personal_plan(con, delayed, ["Lidl"], 2, 4, pantry=["soľ"], today=TODAY)
