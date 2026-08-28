import re
import sqlite3
from datetime import date
from decimal import Decimal

import pytest

import app.plan_data as plan_data
from app.plan_data import build_personal_plan, meal_count_for_frequency, personal_plan_prompt
from app.plan_data import DAY_ORDER, cached_plan_is_current, cooking_days_for_frequency
from app.plan_data import days_covered_by_meal, example_recipe
from app.plan_data import offers_catalog, personal_plan_messages, plan_signature, plan_variant_for
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


# ---------------------------------------------------------------- množstvá
# Recept je pre konkrétnu domácnosť: model povie množstvo na jednu porciu,
# počet porcií a všetky súčty dopočíta Python. Test si ich preto počíta sám,
# nezávisle od appky — inak by overoval len to, že kód sa rovná sám sebe.
MLIEKO_NA_OSOBU = 250   # ml
CHLIEB_NA_OSOBU = 60    # g
MASLO_NA_OSOBU = 30     # g


def portions(household=4, frequency=2):
    return household * {1: 1, 2: 2, 3: 3, 7: 3}[frequency]


def amount(per_person, unit, household=4, frequency=2):
    total = per_person * portions(household, frequency)
    if unit == "g" and total >= 1000:
        return _number(total / 1000) + " kg"
    if unit == "ml" and total >= 1000:
        return _number(total / 1000) + " l"
    return _number(total) + " " + unit


def _number(value):
    return f"{value:.3f}".rstrip("0").rstrip(".").replace(".", ",")


def item(offer_key, per_person, unit, quantity=1):
    return {
        "offer_key": offer_key, "quantity": quantity,
        "amount_per_person": per_person, "unit": unit,
    }


def pondelok_kroky(household=4, frequency=2):
    mlieko = amount(MLIEKO_NA_OSOBU, "ml", household, frequency)
    return [
        f"V hrnci zohrej {mlieko} mlieka na strednom ohni 5 minút, kým sa nezačne pariť.",
        "Vsyp 400 g krupice, osoľ štipkou soli a metličkou miešaj 3 minúty, kým kaša nezhustne.",
        "Kašu rozdeľ na taniere, posyp 2 lyžičkami škorice a hneď podávaj.",
    ]


def streda_kroky(household=4, frequency=2):
    chlieb = amount(CHLIEB_NA_OSOBU, "g", household, frequency)
    return [
        f"{chlieb} chleba nakrájaj na krajce hrubé 1 cm a jemne ich osoľ z oboch strán.",
        "Na suchej panvici opekaj krajce na strednom ohni 3 minúty z každej strany, kým nie sú zlatisté.",
        "Opečené krajce potri 2 lyžicami oleja, posyp mletým korením a podávaj teplé.",
    ]


def piatok_kroky(household=4, frequency=2):
    maslo = amount(MASLO_NA_OSOBU, "g", household, frequency)
    return [
        "Rúru predhrej na 200 °C a plech vylož papierom na pečenie.",
        f"1 kg zemiakov ošúp, nakrájaj na kolieska hrubé 1 cm, rozlož na plech a poukladaj"
        f" navrch {maslo} masla pokrájaného na plátky.",
        "Peč 25 minút, kým zemiaky nezmäknú a nie sú dozlata, potom ich rozdeľ na taniere a podávaj.",
    ]


PONDELOK_KROKY = pondelok_kroky()
STREDA_KROKY = streda_kroky()
PIATOK_KROKY = piatok_kroky()


def model_output(items=None, household=4, frequency=2):
    return {
        "meals": [
            {
                "day": "PO", "name": "Mliečna kaša", "minutes": 25,
                "instructions": pondelok_kroky(household, frequency),
                "items": items if items is not None else [
                    item(verified_key(1), MLIEKO_NA_OSOBU, "ml", quantity=2)
                ],
                "pantry_ingredients": ["soľ"],
            },
            {
                "day": "ST", "name": "Opekaný chlieb", "minutes": 15,
                "instructions": streda_kroky(household, frequency),
                "items": [item(verified_key(2), CHLIEB_NA_OSOBU, "g")],
            },
            {
                "day": "PI", "name": "Maslové zemiaky", "minutes": 20,
                "instructions": piatok_kroky(household, frequency),
                "items": [item(verified_key(3), MASLO_NA_OSOBU, "g")],
            },
            {
                "day": "NE", "name": "Mliečna kaša", "minutes": 25,
                "instructions": pondelok_kroky(household, 1),
                "items": [], "pantry_ingredients": ["soľ"],
            },
        ]
    }


def model_output_for_cooking_days(days, household=4, frequency=2):
    """Valid modelový výstup s jedným jedlom pre každý žiadaný deň.

    Jedlá sú zámerne len zo špajze: testuje rozvrh, porcie a validáciu dní cez
    verejný build kontrakt bez toho, aby limit troch testovacích ponúk maskoval
    sedemdňový kalendár.
    """
    template = model_output(household=household, frequency=frequency)["meals"][0]
    return {
        "meals": [
            dict(template, day=day, items=[], pantry_ingredients=["soľ"])
            for day in days
        ]
    }


def milk_rows(count):
    return [
        (index, "2026-08-17", "Lidl", f"Mlieko {index}", "mliecne", 1.10, 1.50,
         "-27 %", "1 l", "https://source.test/lidl", index, "2026-08-17", "2026-08-23")
        for index in range(1, count + 1)
    ]


def milk_amount_for_portions(portions):
    return amount(MLIEKO_NA_OSOBU, "ml", household=1, frequency=1) if portions == 1 else (
        _number(MLIEKO_NA_OSOBU * portions / 1000) + " l"
        if MLIEKO_NA_OSOBU * portions >= 1000 else f"{MLIEKO_NA_OSOBU * portions} ml"
    )


def milk_steps(portions):
    milk = milk_amount_for_portions(portions)
    return [
        f"V hrnci zohrej {milk} mlieka na strednom ohni 5 minút, kým sa nezačne pariť.",
        "Vsyp 400 g krupice, osoľ štipkou soli a metličkou miešaj 3 minúty, kým kaša nezhustne.",
        "Kašu rozdeľ na taniere, posyp 2 lyžičkami škorice a hneď podávaj.",
    ]


def milk_plan_output(con, days, day_portions):
    offers = current_verified_offers(con, ["Lidl"], TODAY)
    return {
        "meals": [
            {
                "day": day, "name": "Mliečna kaša", "minutes": 25,
                "instructions": milk_steps(portions),
                "items": [item(offers[index]["offer_key"], MLIEKO_NA_OSOBU, "ml")],
                "pantry_ingredients": [],
            }
            for index, (day, portions) in enumerate(zip(days, day_portions))
        ]
    }


def test_regular_plan_ignores_model_invented_pantry_ingredients():
    """Špajzu pri bežnom pláne neurčuje model; inak zhodí aj správny recept."""
    con = connection(milk_rows(7))
    days = cooking_days_for_frequency(1)
    output = milk_plan_output(con, days, [4] * len(days))
    for meal in output["meals"]:
        meal["pantry_ingredients"] = ["olej", "soľ"]

    plan = build_personal_plan(
        con, output, ["Lidl"], 1, 4, pantry=(), today=TODAY
    )

    assert len(plan["jedla"]) == 7
    assert all(
        "spajza" not in ingredient
        for meal in plan["jedla"]
        for ingredient in meal["suroviny"]
    )


def test_pantry_driven_plan_still_rejects_an_unknown_pantry_ingredient():
    con = connection(milk_rows(7))
    days = cooking_days_for_frequency(1)
    output = milk_plan_output(con, days, [4] * len(days))
    output["meals"][0]["pantry_ingredients"] = ["olej"]

    with pytest.raises(ValueError, match="neznámu alebo duplicitnú surovinu"):
        build_personal_plan(
            con, output, ["Lidl"], 1, 4, pantry=["soľ"], today=TODAY
        )


def test_reconstructs_grouped_purchases_and_totals_only_from_verified_offers():
    plan = build_personal_plan(
        connection(verified_rows()), model_output(), ["Lidl", "Tesco"], 2, 4,
        pantry=["soľ"], today=TODAY
    )

    assert plan == {
        "tyzden": "2026-08-17",
        "jedla": [
            {
                "den": "PO", "nazov": "Mliečna kaša",
                "recept": {"min": 25, "porcie": 8, "pre": "4 osoby × 2 dni",
                           "davky": ["Mlieko – 2 l", "soľ zo špajze"],
                           "kroky": PONDELOK_KROKY},
                "suroviny": [
                    {"offer_key": verified_key(1), "nazov": "Mlieko", "obchod": "Lidl", "jednotka": "1 l",
                     "mnozstvo": 2, "davka": "2 l", "cena": "2,20", "povodna": "3,00", "zlava": "-27 %",
                     "source_url": "https://source.test/lidl", "source_page": 1,
                     "valid_from": "2026-08-17", "valid_to": "2026-08-23"},
                    {"spajza": "soľ"},
                ],
            },
            {
                "den": "ST", "nazov": "Opekaný chlieb",
                "recept": {"min": 15, "porcie": 8, "pre": "4 osoby × 2 dni",
                           "davky": ["Chlieb – 480 g"], "kroky": STREDA_KROKY},
                "suroviny": [
                    {"offer_key": verified_key(2), "nazov": "Chlieb", "obchod": "Tesco", "jednotka": "500 g",
                     "mnozstvo": 1, "davka": "480 g", "cena": "1,20", "povodna": "1,80", "zlava": "-33 %",
                     "source_url": "https://source.test/tesco", "source_page": 2,
                     "valid_from": "2026-08-17", "valid_to": "2026-08-23"},
                ],
            },
            {
                "den": "PI", "nazov": "Maslové zemiaky",
                "recept": {"min": 20, "porcie": 8, "pre": "4 osoby × 2 dni",
                           "davky": ["Maslo – 240 g"], "kroky": PIATOK_KROKY},
                "suroviny": [
                    {"offer_key": verified_key(3), "nazov": "Maslo", "obchod": "Lidl", "jednotka": "250 g",
                     "mnozstvo": 1, "davka": "240 g", "cena": "2,00", "povodna": "2,50", "zlava": "-20 %",
                     "source_url": "https://source.test/lidl", "source_page": 3,
                    "valid_from": "2026-08-17", "valid_to": "2026-08-23"},
                ],
            },
            {
                "den": "NE", "nazov": "Mliečna kaša",
                "recept": {"min": 25, "porcie": 4, "pre": "4 osoby",
                           "davky": ["soľ zo špajze"], "kroky": pondelok_kroky(4, 1)},
                "suroviny": [{"spajza": "soľ"}],
            },
        ],
        "nakupny_zoznam": [
                {"obchod": "Lidl", "polozky": [
                    {"offer_key": verified_key(3), "nazov": "Maslo", "jednotka": "250 g", "mnozstvo": 1,
                     "cena": "2,00", "povodna": "2,50", "zlava": "-20 %",
                     "source_url": "https://source.test/lidl", "source_page": 3,
                     "valid_from": "2026-08-17", "valid_to": "2026-08-23"},
                    {"offer_key": verified_key(1), "nazov": "Mlieko", "jednotka": "1 l", "mnozstvo": 2,
                     "cena": "2,20", "povodna": "3,00", "zlava": "-27 %",
                     "source_url": "https://source.test/lidl", "source_page": 1,
                     "valid_from": "2026-08-17", "valid_to": "2026-08-23"},
                ]},
                {"obchod": "Tesco", "polozky": [
                    {"offer_key": verified_key(2), "nazov": "Chlieb", "jednotka": "500 g", "mnozstvo": 1,
                     "cena": "1,20", "povodna": "1,80", "zlava": "-33 %",
                     "source_url": "https://source.test/tesco", "source_page": 2,
                     "valid_from": "2026-08-17", "valid_to": "2026-08-23"},
                ]},
        ],
        "nakup_spolu": "5,40", "bezne": "7,30", "usetris": "1,90",
    }


def test_shopping_rows_retain_verified_price_provenance_from_the_offer_database():
    plan = build_personal_plan(
        connection(verified_rows()), model_output(), ["Lidl", "Tesco"], 2, 4,
        pantry=["soľ"], today=TODAY,
    )

    shopping_by_key = {
        item["offer_key"]: item
        for group in plan["nakupny_zoznam"]
        for item in group["polozky"]
    }
    assert {
        key: {
            field: shopping_by_key[key][field]
            for field in ("source_url", "source_page", "valid_from", "valid_to")
        }
        for key in shopping_by_key
    } == {
        verified_key(1): {
            "source_url": "https://source.test/lidl", "source_page": 1,
            "valid_from": "2026-08-17", "valid_to": "2026-08-23",
        },
        verified_key(2): {
            "source_url": "https://source.test/tesco", "source_page": 2,
            "valid_from": "2026-08-17", "valid_to": "2026-08-23",
        },
        verified_key(3): {
            "source_url": "https://source.test/lidl", "source_page": 3,
            "valid_from": "2026-08-17", "valid_to": "2026-08-23",
        },
    }


def test_every_meal_ingredient_carries_the_leaflet_provenance_of_its_price():
    """Bez zdroja, strany a platnosti sa cena na obrazovke nedá skontrolovať."""
    plan = build_personal_plan(
        connection(verified_rows()), model_output(), ["Lidl", "Tesco"], 2, 4,
        pantry=["soľ"], today=TODAY,
    )

    mlieko = plan["jedla"][0]["suroviny"][0]
    assert mlieko["source_url"] == "https://source.test/lidl"
    assert mlieko["source_page"] == 1
    assert mlieko["valid_from"] == "2026-08-17"
    assert mlieko["valid_to"] == "2026-08-23"

    checked = 0
    for meal in plan["jedla"]:
        for ingredient in meal["suroviny"]:
            if "offer_key" not in ingredient:
                continue
            checked += 1
            assert ingredient["source_url"].startswith("https://")
            assert isinstance(ingredient["source_page"], int) and ingredient["source_page"] > 0
            assert ingredient["valid_from"] <= TODAY.isoformat() <= ingredient["valid_to"]
    assert checked == 3


def test_provenance_comes_from_the_database_row_and_never_from_the_model():
    rows = verified_rows()
    rows[0] = tuple(list(rows[0][:9]) + ["https://letak.test/lidl/32", 7] + list(rows[0][11:]))
    con = connection(rows)
    keys = {row["nazov"]: row["offer_key"] for row in current_verified_offers(con, ["Lidl", "Tesco"], TODAY)}
    payload = model_output()
    payload["meals"][0]["items"] = [item(keys["Mlieko"], MLIEKO_NA_OSOBU, "ml", quantity=2)]
    payload["meals"][1]["items"] = [item(keys["Chlieb"], CHLIEB_NA_OSOBU, "g")]
    payload["meals"][2]["items"] = [item(keys["Maslo"], MASLO_NA_OSOBU, "g")]

    plan = build_personal_plan(con, payload, ["Lidl", "Tesco"], 2, 4, pantry=["soľ"], today=TODAY)

    mlieko = plan["jedla"][0]["suroviny"][0]
    assert mlieko["source_url"] == "https://letak.test/lidl/32"
    assert mlieko["source_page"] == 7
    assert mlieko["cena"] == "2,20"

    payload["meals"][0]["items"][0]["source_url"] = "https://podvrh.test/"
    with pytest.raises(ValueError, match="nepovolené"):
        build_personal_plan(con, payload, ["Lidl", "Tesco"], 2, 4, pantry=["soľ"], today=TODAY)


def test_missing_original_price_contributes_sale_price_and_never_negative_savings():
    rows = verified_rows()
    rows[0] = tuple(list(rows[0][:6]) + [None] + list(rows[0][7:]))
    con = connection(rows)
    keys = {row["nazov"]: row["offer_key"] for row in current_verified_offers(con, ["Lidl", "Tesco"], TODAY)}
    payload = model_output()
    payload["meals"][0]["items"] = [item(keys["Mlieko"], MLIEKO_NA_OSOBU, "ml", quantity=2)]
    payload["meals"][1]["items"] = [item(keys["Chlieb"], CHLIEB_NA_OSOBU, "g")]
    payload["meals"][2]["items"] = [item(keys["Maslo"], MASLO_NA_OSOBU, "g")]

    plan = build_personal_plan(
        con, payload, ["Lidl", "Tesco"], 2, 4, pantry=["soľ"], today=TODAY
    )

    assert plan["nakup_spolu"] == "5,40"
    assert plan["bezne"] == "6,50"
    assert plan["usetris"] == "1,10"


def test_positive_recipe_minutes_are_validated_and_emitted_for_every_meal():
    payload = model_output()
    for meal, minutes in zip(payload["meals"], (25, 35, 45, 30)):
        meal["minutes"] = minutes

    plan = build_personal_plan(
        connection(verified_rows()), payload, ["Lidl", "Tesco"], 2, 4,
        pantry=["soľ"], today=TODAY,
    )

    assert [meal["recept"]["min"] for meal in plan["jedla"]] == [25, 35, 45, 30]


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
        (lambda plan: plan["meals"][1]["items"].__setitem__(
            0, item(verified_key(1), MLIEKO_NA_OSOBU, "ml")), "duplicitné"),
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
    assert meal_count_for_frequency(1) == 7
    assert meal_count_for_frequency(2) == 4
    assert meal_count_for_frequency(3) == 3
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
    # Zadanie musí byť po slovensky: 1 osoba, 4 osoby, 12 osôb.
    assert "1 osoba" in for_one and "osôb" not in for_one
    assert "12 osôb" in for_twelve
    assert "množstvá aj porcie" in for_one
    assert "2 porcie" in for_one and "24 porcií" in for_twelve


def test_personal_plan_build_requires_validated_household_size():
    con = connection(verified_rows())

    plan = build_personal_plan(
        con, model_output(household=12), ["Lidl", "Tesco"], 2, pantry=["soľ"],
        household_size=12, today=TODAY,
    )

    assert plan["jedla"][0]["nazov"] == "Mliečna kaša"
    for invalid in (0, 13, True):
        with pytest.raises(ValueError, match="Počet osôb"):
            build_personal_plan(
                con, model_output(), ["Lidl", "Tesco"], 2, pantry=["soľ"],
                household_size=invalid, today=TODAY,
            )


def offers_connection():
    con = reusable_rowid_connection()
    ingest_three(con, "family")
    return current_verified_offers(con, ["Lidl"], TODAY)


@pytest.mark.parametrize(("frequency", "days", "day_portions"), [
    (1, ("PO", "UT", "ST", "ŠT", "PI", "SO", "NE"), (4, 4, 4, 4, 4, 4, 4)),
    (2, ("PO", "ST", "PI", "NE"), (8, 8, 8, 4)),
    (3, ("PO", "ŠT", "NE"), (12, 12, 4)),
])
def test_full_week_cooking_schedule_and_portions_end_exactly_on_sunday(
        frequency, days, day_portions):
    """PO-NE má 7/4/3 varení a presne 7 × N porcií, bez nedeľných zvyškov."""
    assert cooking_days_for_frequency(frequency) == days
    assert meal_count_for_frequency(frequency) == len(days)
    assert days_covered_by_meal(frequency) == frequency

    plan = build_personal_plan(
        connection(verified_rows()), model_output_for_cooking_days(days, frequency=frequency),
        ["Lidl", "Tesco"], frequency, 4, pantry=["soľ"], today=TODAY,
    )

    assert [meal["den"] for meal in plan["jedla"]] == list(days)
    assert [meal["recept"]["porcie"] for meal in plan["jedla"]] == list(day_portions)
    assert sum(meal["recept"]["porcie"] for meal in plan["jedla"]) == 7 * 4


@pytest.mark.parametrize(("frequency", "days", "day_portions"), [
    (1, ("PO", "UT", "ST", "ŠT", "PI", "SO", "NE"), (3, 3, 3, 3, 3, 3, 3)),
    (2, ("PO", "ST", "PI", "NE"), (6, 6, 6, 3)),
    (3, ("PO", "ŠT", "NE"), (9, 9, 3)),
])
def test_recipe_and_basket_quantities_scale_by_each_cooking_days_portions(
        frequency, days, day_portions):
    """Nákup sa ráta z dennej dávky, aby NE nekúpila zvyšky do ďalšieho týždňa."""
    con = connection(milk_rows(len(days)))
    plan = build_personal_plan(
        con, milk_plan_output(con, days, day_portions), ["Lidl"], frequency, 3,
        pantry=[], today=TODAY,
    )

    meals = plan["jedla"]
    assert [meal["recept"]["porcie"] for meal in meals] == list(day_portions)
    assert sum(meal["recept"]["porcie"] for meal in meals) == 7 * 3
    assert [meal["suroviny"][0]["davka"] for meal in meals] == [
        milk_amount_for_portions(portions) for portions in day_portions
    ]
    assert [meal["suroviny"][0]["mnozstvo"] for meal in meals] == [
        (MLIEKO_NA_OSOBU * portions + 999) // 1000 for portions in day_portions
    ]


@pytest.mark.parametrize(("frequency", "days", "message"), [
    (1, ("PO", "UT", "ST", "ŠT", "PI", "SO"), "počet jedál"),
    (2, ("PO", "UT", "PI", "NE"), "dni varenia"),
    (3, ("PO", "ŠT", "SO"), "dni varenia"),
])
def test_model_output_rejects_wrong_weekly_cooking_count_or_days(frequency, days, message):
    """Model nesmie nahradiť varenie zvyškom ani vynechať koniec týždňa."""
    with pytest.raises(ValueError, match=message):
        build_personal_plan(
            connection(verified_rows()), model_output_for_cooking_days(days, frequency=frequency),
            ["Lidl", "Tesco"], frequency, 4, pantry=["soľ"], today=TODAY,
        )


@pytest.mark.parametrize(("frequency", "count", "days_text", "day_portions"), [
    (1, 7, "PO, UT, ST, ŠT, PI, SO a NE", (4, 4, 4, 4, 4, 4, 4)),
    (2, 4, "PO, ST, PI a NE", (8, 8, 8, 4)),
    (3, 3, "PO, ŠT a NE", (12, 12, 4)),
])
def test_prompt_names_every_exact_cooking_day_and_its_portion_contract(
        frequency, count, days_text, day_portions):
    prompt = personal_plan_prompt(offers_connection(), frequency, ["soľ"], household_size=4)
    word = "jedál" if count >= 5 else "jedlá"

    assert f"Navrhni presne {count} {word} na dni {days_text}" in prompt
    assert f"Varí sa len v dňoch {days_text}" in prompt
    for day, portions in zip(cooking_days_for_frequency(frequency), day_portions):
        days = portions // 4
        portion_word = "porcie" if portions < 5 else "porcií"
        day_word = "deň" if days == 1 else "dni"
        assert re.search(
            rf"{day}[^\n]*{portions} {portion_word}[^\n]*{days} {day_word}", prompt
        ), f"{day} must carry its own portion and leftovers span"
    if frequency > 1:
        assert "zvyšk" in prompt


# ------------------------------------------------- koľko čoho pre počet osôb
def build(payload, household=4, frequency=2, rows=None):
    return build_personal_plan(
        connection(rows or verified_rows()), payload, ["Lidl", "Tesco"], frequency,
        household, pantry=["soľ"], today=TODAY,
    )


def test_recipe_states_how_much_of_everything_the_household_needs():
    """Majiteľ: „dobre by bolo napočítať, že koľko čoho pre počet osôb."""
    plan = build(model_output())

    recept = plan["jedla"][0]["recept"]
    assert recept["porcie"] == 8, "4 osoby × 2 dni, druhý deň sa jedia zvyšky"
    assert recept["pre"] == "4 osoby × 2 dni"
    assert recept["davky"] == ["Mlieko – 2 l", "soľ zo špajze"]
    assert plan["jedla"][0]["suroviny"][0]["davka"] == "2 l"


def test_amounts_are_recomputed_for_every_household_size():
    """Rovnaký recept, iná domácnosť: 250 ml na porciu musí dať iné litre."""
    for household, expected in ((1, "500 ml"), (2, "1 l"), (4, "2 l"), (6, "3 l")):
        plan = build(model_output(household=household), household=household)
        assert plan["jedla"][0]["suroviny"][0]["davka"] == expected
        assert plan["jedla"][0]["recept"]["porcie"] == household * 2

    solo = build(model_output(household=1), household=1)
    assert solo["jedla"][0]["recept"]["pre"] == "1 osoba × 2 dni"


def test_shopping_quantity_is_derived_from_what_the_recipe_actually_uses():
    """Nákupný zoznam nesmie byť odhad modelu — počíta sa z veľkosti balenia."""
    payload = model_output()
    payload["meals"][0]["items"] = [item(verified_key(1), MLIEKO_NA_OSOBU, "ml", quantity=9)]

    plan = build(payload)

    mlieko = plan["jedla"][0]["suroviny"][0]
    assert mlieko["mnozstvo"] == 2, "2 l receptu = 2 balenia po 1 l, nech model tvrdí čokoľvek"
    assert mlieko["cena"] == "2,20"
    assert plan["nakupny_zoznam"][0]["polozky"][1]["mnozstvo"] == 2


def test_partly_used_package_is_still_bought_whole():
    """480 g chleba z 500 g balenia je jedno balenie — nie 0,96."""
    plan = build(model_output())

    chlieb = plan["jedla"][1]["suroviny"][0]
    assert (chlieb["davka"], chlieb["mnozstvo"], chlieb["jednotka"]) == ("480 g", 1, "500 g")

    velka_rodina = build(model_output(household=6), household=6)
    chlieb = velka_rodina["jedla"][1]["suroviny"][0]
    assert (chlieb["davka"], chlieb["mnozstvo"]) == ("720 g", 2)


def test_rejects_a_recipe_whose_steps_contradict_the_amount_that_is_bought():
    """Recept hovorí 1 l, nakúpi sa 2 l — presne tá nedôvera, ktorú appka nesmie vyrobiť."""
    payload = model_output()
    payload["meals"][0]["instructions"] = [
        "V hrnci zohrej 1 l mlieka na strednom ohni 5 minút, kým sa nezačne pariť.",
        "Vsyp 400 g krupice, osoľ štipkou soli a metličkou miešaj 3 minúty, kým kaša nezhustne.",
        "Kašu rozdeľ na taniere, posyp 2 lyžičkami škorice a hneď podávaj.",
    ]

    with pytest.raises(ValueError, match="nesúhlas"):
        build(payload)


def test_requires_an_amount_per_person_for_every_bought_ingredient():
    payload = model_output()
    payload["meals"][0]["items"] = [{"offer_key": verified_key(1), "quantity": 2}]

    with pytest.raises(ValueError, match="na osobu"):
        build(payload)

    payload["meals"][0]["items"] = [item(verified_key(1), 250, "hrsť", quantity=2)]
    with pytest.raises(ValueError, match="jednotk"):
        build(payload)


def test_a_typo_in_the_unit_can_never_buy_a_hundred_kilos():
    """150 kg namiesto 150 g je nákup za stovky eur — to sa nesmie stať."""
    payload = model_output()
    payload["meals"][0]["items"] = [item(verified_key(1), 150, "l", quantity=1)]

    with pytest.raises(ValueError, match="nereálne"):
        build(payload)


@pytest.mark.parametrize("amount_per_person", [0, -5, True, "250", float("inf")])
def test_rejects_nonsense_amounts_per_person(amount_per_person):
    payload = model_output()
    payload["meals"][0]["items"] = [
        {"offer_key": verified_key(1), "quantity": 2,
         "amount_per_person": amount_per_person, "unit": "ml"}
    ]

    with pytest.raises(ValueError, match="na osobu"):
        build(payload)


def test_unparsable_package_size_falls_back_to_the_quantity_the_model_asked_for():
    """Pri balení „bal." sa počet kusov dopočítať nedá — tam model rozhoduje."""
    rows = verified_rows()
    rows[0] = tuple(list(rows[0][:8]) + ["bal."] + list(rows[0][9:]))
    con = connection(rows)
    keys = {row["nazov"]: row["offer_key"] for row in current_verified_offers(con, ["Lidl", "Tesco"], TODAY)}
    payload = model_output()
    payload["meals"][0]["items"] = [item(keys["Mlieko"], MLIEKO_NA_OSOBU, "ml", quantity=3)]
    payload["meals"][1]["items"] = [item(keys["Chlieb"], CHLIEB_NA_OSOBU, "g")]
    payload["meals"][2]["items"] = [item(keys["Maslo"], MASLO_NA_OSOBU, "g")]

    plan = build_personal_plan(
        con, payload, ["Lidl", "Tesco"], 2, 4, pantry=["soľ"], today=TODAY
    )

    mlieko = plan["jedla"][0]["suroviny"][0]
    assert (mlieko["mnozstvo"], mlieko["davka"]) == (3, "2 l")


# -------------------------------------------- profesionálny porciový štandard
def test_mixed_household_separates_served_plates_from_adult_equivalents():
    """2 dospelí + 2 deti na tri dni = 12 tanierov, ale 9,9 dospelej dávky."""
    assert plan_data.servings_for(2, 2, frequency=3, day="PO") == 12
    assert plan_data.adult_equivalents_for(2, 2, frequency=3, day="PO") == Decimal("9.90")

    assert plan_data.servings_for(2, 2, frequency=3, day="NE") == 4
    assert plan_data.adult_equivalents_for(2, 2, frequency=3, day="NE") == Decimal("3.30")


def test_mixed_household_recipe_uses_adult_equivalents_but_displays_real_servings():
    payload = model_output_for_cooking_days(("PO", "ŠT", "NE"), household=4, frequency=3)
    payload["meals"][0]["items"] = [{
        "offer_key": verified_key(1),
        "quantity": 1,
        "amount_per_adult": 250,
        "unit": "ml",
        "ingredient_role": "sauce_liquid",
    }]
    payload["meals"][0]["instructions"] = [
        "V hrnci zohrej 2,475 l mlieka na strednom ohni 5 minút, kým sa nezačne pariť.",
        "Vsyp 400 g krupice, osoľ štipkou soli a metličkou miešaj 3 minúty, kým kaša nezhustne.",
        "Kašu rozdeľ na 12 tanierov, posyp 2 lyžičkami škorice a hneď podávaj.",
    ]

    plan = build_personal_plan(
        connection(verified_rows()), payload, ["Lidl", "Tesco"], 3, None,
        pantry=["soľ"], today=TODAY, adults=2, children=2,
    )

    first = plan["jedla"][0]
    assert first["recept"]["porcie"] == 12
    assert first["recept"]["pre"] == "2 dospelí + 2 deti × 3 dni"
    assert first["suroviny"][0]["davka"] == "2,475 l"
    assert first["suroviny"][0]["mnozstvo"] == 3
    assert plan["jedla"][-1]["recept"]["porcie"] == 4


def test_prompt_distinguishes_servings_from_adult_equivalents_and_uses_new_field():
    prompt = personal_plan_prompt(
        [], 3, [], None, adults=2, children=2,
    )

    assert "2 dospelí" in prompt and "2 deti" in prompt
    assert "12 porcií" in prompt and "9,9" in prompt
    assert "amount_per_adult" in prompt
    assert "amount_per_person" not in prompt
    assert "3–12" in prompt and "0,65" in prompt


@pytest.mark.parametrize(
    "name,category,claimed,unit,minimum,maximum,role",
    [
        ("Kuracie prsia", "mäso", None, "g", 120, 200, "protein_main"),
        ("Ryža dlhozrnná", "trvanlivé", None, "g", 60, 110, "dry_starch"),
        ("Zemiaky", "zelenina", None, "g", 200, 400, "potato"),
        ("Šošovica", "trvanlivé", None, "g", 60, 110, "legume_dry"),
        ("Brokolica", "zelenina", None, "g", 120, 350, "vegetable"),
        ("Chlieb", "pečivo", None, "g", 60, 150, "bread"),
        ("Vajcia", "vajcia", None, "ks", 1, 3, "egg"),
        ("Tvaroh", "mliečne", None, "g", 60, 150, "dairy_main"),
        ("Parmezán", "mliečne", None, "g", 10, 60, "dairy_addition"),
        ("Mlieko", "mliečne", None, "ml", 100, 400, "sauce_liquid"),
        ("Olivový olej", "trvanlivé", None, "ml", 5, 40, "fat_addition"),
        ("Neznáma potravina", "iné", "other", "g", 1, 500, "other"),
    ],
)
def test_every_portion_role_enforces_its_approved_range(
        name, category, claimed, unit, minimum, maximum, role):
    assert plan_data.validate_portion_amount(
        name, category, minimum, unit, claimed
    ) == (role, unit, Decimal(str(minimum)))
    assert plan_data.validate_portion_amount(
        name, category, maximum, unit, claimed
    ) == (role, unit, Decimal(str(maximum)))
    with pytest.raises(ValueError, match="porciovú triedu"):
        plan_data.validate_portion_amount(name, category, maximum + 1, unit, claimed)


def test_known_food_name_wins_over_an_incompatible_model_role():
    assert plan_data.ingredient_role_for(
        "Olivový olej", "trvanlivé", claimed_role="dry_starch", base="ml"
    ) == "fat_addition"
    with pytest.raises(ValueError, match="porciovú triedu"):
        plan_data.validate_portion_amount(
            "Olivový olej", "trvanlivé", 75, "ml", claimed_role="dry_starch"
        )


def test_unknown_food_uses_the_conservative_fallback_instead_of_model_guessing():
    assert plan_data.ingredient_role_for(
        "Záhadná zmes", "mäso", claimed_role="dry_starch", base="g"
    ) == "protein_main"
    assert plan_data.ingredient_role_for(
        "Záhadná zmes", "iné", claimed_role="not-a-role", base="g"
    ) == "other"


def test_name_classifier_matches_whole_tokens_not_substrings_inside_adjectives():
    """Maslová tekvica je druh zeleniny, nie porcia masla."""
    assert plan_data.ingredient_role_for(
        "Maslová tekvica", "zelenina", claimed_role="vegetable", base="g"
    ) == "vegetable"
    assert plan_data.validate_portion_amount(
        "Maslová tekvica", "zelenina", 200, "g", "vegetable"
    )[0] == "vegetable"


@pytest.mark.parametrize(
    "name,amount",
    [("Cibuľa", 50), ("Cesnak", 10)],
)
def test_small_aromatics_use_a_closed_addition_role(name, amount):
    assert plan_data.validate_portion_amount(
        name, "zelenina", amount, "g", "vegetable_addition"
    ) == ("vegetable_addition", "g", Decimal(str(amount)))


def test_small_amount_does_not_turn_main_broccoli_into_an_addition():
    with pytest.raises(ValueError, match="porciovú triedu"):
        plan_data.validate_portion_amount(
            "Brokolica", "zelenina", 5, "g", "vegetable_addition"
        )


def test_ordinary_cheese_may_be_main_or_addition_but_not_an_incompatible_role():
    assert plan_data.validate_portion_amount(
        "Syr Eidam", "mliečne", 30, "g", "dairy_addition"
    ) == ("dairy_addition", "g", Decimal("30"))
    assert plan_data.validate_portion_amount(
        "Syr Eidam", "mliečne", 100, "g", "dairy_main"
    ) == ("dairy_main", "g", Decimal("100"))
    with pytest.raises(ValueError, match="porciov"):
        plan_data.validate_portion_amount(
            "Syr Eidam", "mliečne", 75, "g", "dry_starch"
        )


def test_common_valid_ingredients_do_not_trigger_a_paid_plan_retry():
    """Tieto bežné AI návrhy musia prejsť prvýkrát, nie spáliť platený retry."""
    cases = (
        ("Maslová tekvica", "zelenina", 200, "g", "vegetable"),
        ("Cibuľa", "zelenina", 50, "g", "vegetable_addition"),
        ("Cesnak", "zelenina", 10, "g", "vegetable_addition"),
        ("Syr Eidam", "mliečne", 30, "g", "dairy_addition"),
    )
    assert [
        plan_data.validate_portion_amount(name, category, amount, unit, role)[0]
        for name, category, amount, unit, role in cases
    ] == ["vegetable", "vegetable_addition", "vegetable_addition", "dairy_addition"]


def test_prompt_exposes_the_closed_addition_role_and_ambiguous_examples():
    rules = plan_data.recipe_rules()
    assert "vegetable_addition" in rules
    assert "Maslová tekvica" in rules
    assert "Cibuľa" in rules and "50" in rules
    assert "Syr Eidam" in rules and "dairy_addition" in rules


# ------------------------------------------------- názov musí sedieť na recept
def with_steps(steps, name="Dusená cibuľa"):
    payload = model_output()
    payload["meals"][0]["instructions"] = list(steps)
    payload["meals"][0]["name"] = name
    return payload


def test_rejects_a_dish_name_that_promises_an_ingredient_the_steps_never_use():
    """„Kuracie prsia na ryži" bez ryže v postupe je klamstvo v názve."""
    payload = model_output()
    payload["meals"][0]["name"] = "Mliečna kaša s hruškami"

    with pytest.raises(ValueError, match="Názov"):
        build(payload)


def test_rejects_rice_that_is_stirred_in_when_the_name_promises_it_underneath():
    """Presne majiteľova sťažnosť: „na ryži", ale recept je „s ryžou"."""
    mixed_in = [
        "V hrnci zohrej 2 l mlieka na strednom ohni 5 minút, kým sa nezačne pariť.",
        "Vsyp 400 g ryže, osoľ štipkou soli a na miernom ohni ju var 12 minút, kým nezmäkne.",
        "Kašu dôkladne premiešaj vareškou a povar ju ešte 3 minúty, kým nezhustne.",
        "Kašu rozdeľ na taniere, posyp 2 lyžičkami škorice a hneď podávaj.",
    ]

    with pytest.raises(ValueError, match="Názov"):
        build(with_steps(mixed_in, name="Mliečna kaša na ryži"))

    served_on_top = mixed_in[:3] + [
        "Ryžu rozdeľ na taniere, prelej ju horúcou kašou a hneď podávaj.",
    ]
    plan = build(with_steps(served_on_top, name="Mliečna kaša na ryži"))
    assert plan["jedla"][0]["nazov"] == "Mliečna kaša na ryži"


def test_the_recipe_the_owner_complained_about_is_rejected_even_when_it_looks_complete():
    """Doslova to, čo appka vygenerovala: názov „na ryži", ale ryža sa vmieša.

    Kroky majú množstvá, časy aj teplotu — staršia kontrola ich prepustila.
    """
    payload = with_steps([
        "1,2 kg kuracích pŕs nakrájaj na plátky hrubé 1 cm a osoľ ich štipkou soli.",
        "Na 2 lyžiciach oleja ich opekaj na strednom ohni 5 minút z každej strany do zlatista.",
        "Pridaj 600 g ryže, zalej 1,2 l vody a na miernom ohni var 20 minút, kým sa voda nevsiakne.",
        "Všetko dôkladne premiešaj vareškou, dochuť soľou a rozdeľ na štyri hlboké taniere.",
        "Podávaj hneď a každú porciu posyp nasekanou petržlenovou vňaťou.",
    ], name="Kuracie prsné plátky na ryži")

    with pytest.raises(ValueError, match="Názov sľubuje jedlo podávané na ryži"):
        build(payload)


def test_a_garnish_may_be_cut_without_naming_its_shape():
    """Tvar rezu pýtame pri surovine, ktorá tvorí jedlo — nie pri vňati na ozdobu."""
    payload = with_steps([
        "600 g mrkvy nakrájaj na kolieska hrubé 1 cm a daj ich do hrnca.",
        "Prilej 200 ml vody, osoľ a na miernom ohni duste 20 minút, kým mrkva nezmäkne.",
        "Rozdeľ na taniere, nakrájaj petržlenovú vňať, posyp ňou porcie a podávaj.",
    ], name="Dusená mrkva")

    plan = build(payload)

    assert len(plan["jedla"][0]["recept"]["kroky"]) == 3


def test_a_tip_about_leftovers_may_follow_the_serving_step():
    """Varí sa na dva dni, takže rada o zvyškoch je namieste — nesmie plán zhodiť."""
    payload = with_steps([
        "600 g mrkvy nakrájaj na kolieska hrubé 1 cm a daj ich do hrnca.",
        "Prilej 200 ml vody, osoľ a na miernom ohni duste 20 minút, kým mrkva nezmäkne.",
        "Rozdeľ na štyri taniere a podávaj s krajcom chleba.",
        "Zvyšok nechaj vychladnúť, v chladničke vydrží do ďalšieho dňa.",
    ], name="Dusená mrkva")

    plan = build(payload)

    assert len(plan["jedla"][0]["recept"]["kroky"]) == 4


def test_a_cold_dish_served_with_bread_is_not_asked_for_an_oven_temperature():
    """„Podávaj s pečivom" nie je pečenie — studený šalát nemá čo zohrievať."""
    payload = with_steps([
        "400 g paradajok nakrájaj na osminy a 200 g uhoriek na kolieska hrubé 1 cm.",
        "Do misy pridaj 2 konzervy tuniaka, 1 cibuľu nakrájanú najemno a premiešaj.",
        "Zalej 4 lyžicami oleja, osoľ a nechaj 10 minút odstáť, kým sa chute nespoja.",
        "Šalát rozdeľ do štyroch misiek a podávaj s pečivom.",
    ], name="Zeleninový šalát s tuniakom")

    plan = build(payload)

    assert len(plan["jedla"][0]["recept"]["kroky"]) == 4


def test_a_plain_dish_name_needs_no_serving_base_in_the_last_step():
    """„Bravčové na cibuľke" nie je jedlo podávané na cibuli — nesmieme ho odmietnuť."""
    steps = [
        "Cibuľu nakrájaj na kolieska a opeč ju na 2 lyžiciach oleja 5 minút do sklovita.",
        "Prilej 200 ml vody, osoľ štipkou soli a na miernom ohni duste 25 minút, kým nezmäkne.",
        "Rozdeľ na taniere a podávaj, každú porciu posyp 1 lyžičkou mletého korenia.",
    ]

    plan = build(with_steps(steps, name="Dusená cibuľa na oleji"))

    assert plan["jedla"][0]["nazov"] == "Dusená cibuľa na oleji"


# ------------------------------------------------------------ skutočný postup
def test_rejects_recipe_steps_too_generic_to_cook_from():
    """„Pridaj cibuľu a opeč" nepovie koľko, ako dlho ani na čom."""
    payload = with_steps([
        "Pridaj cibuľu a opeč.",
        "Uvar cestoviny.",
        "Podávaj.",
    ])

    with pytest.raises(ValueError, match="všeobecn"):
        build(payload)


def test_accepts_the_same_step_once_it_says_how_much_how_long_and_how_hot():
    payload = with_steps([
        "Na 2 lyžiciach oleja opeč 2 cibule nakrájané na kocky 5 minút do sklovita.",
        "Prilej 200 ml vody, osoľ štipkou soli a duste 15 minút pod pokrievkou, kým nezmäknú.",
        "Na miernom ohni prevar ešte 3 minúty a rozdeľ na 4 taniere a podávaj.",
    ])

    plan = build(payload)

    assert plan["jedla"][0]["recept"]["kroky"][0].startswith("Na 2 lyžiciach oleja opeč")


@pytest.mark.parametrize(
    "steps, message",
    [
        (
            ["Na 2 lyžiciach oleja opeč 2 nakrájané cibule 5 minút na miernom ohni."],
            "aspoň 3 kroky",
        ),
        (
            [
                "Na 2 lyžiciach oleja opeč 2 cibule nakrájané na kocky 5 minút do sklovita.",
                "Pridaj cibuľu a opeč.",
                "Na miernom ohni duste 15 minút a rozdeľ na 4 taniere.",
            ],
            "všeobecn",
        ),
        (
            [
                "Cibuľu nakrájaj najemno a opeč ju na strednom ohni 5 minút do sklovita.",
                "Prilej vodu, osoľ podľa chuti a všetko poriadne premiešaj vareškou.",
                "Duste pod pokrievkou 20 minút a potom rozdeľ na taniere a podávaj.",
            ],
            "množstv",
        ),
        (
            [
                "Na 2 lyžiciach oleja opeč 2 cibule nakrájané na kocky na strednom ohni.",
                "Pridaj 400 g ryže, 800 ml vody a štipku soli, potom premiešaj.",
                "Duste pod pokrievkou, kým sa voda nevsiakne, a rozdeľ na taniere.",
            ],
            "čas",
        ),
        (
            [
                "Nakrájaj 2 cibule na kocky a opeč ich na panvici 5 minút do sklovita.",
                "Pridaj 400 g ryže a 800 ml vody, osoľ štipkou soli a premiešaj.",
                "Nechaj odstáť 15 minút, kým nezmäkne, potom rozdeľ na 4 taniere a podávaj.",
            ],
            "teplot",
        ),
        (
            [
                "Na 2 lyžiciach oleja opeč cibuľu nakrájanú na kocky na strednom ohni 5 minút.",
                "Pridaj nakrájanú mrkvu na kolieska, premiešaj a duste pod pokrievkou do mäkka.",
                "Osoľ, okoreň a rozdeľ na taniere, potom podávaj s petržlenovou vňaťou.",
            ],
            "Väčšina krokov",
        ),
    ],
)
def test_rejects_recipes_that_do_not_say_how_much_how_long_or_how_hot(steps, message):
    with pytest.raises(ValueError, match=message):
        build(with_steps(steps))


def test_rejects_cutting_that_does_not_say_into_what_shape():
    """„Nakrájaj mäso" nevie zopakovať nikto, kto nevaril: na kocky? na plátky?"""
    payload = with_steps([
        "Nakrájaj 2 cibule a opeč ich na 2 lyžiciach oleja 5 minút do sklovita.",
        "Prilej 200 ml vody, osoľ štipkou soli a na miernom ohni duste 15 minút, kým nezmäknú.",
        "Rozdeľ na 4 taniere, posyp 1 lyžičkou korenia a hneď podávaj.",
    ])

    with pytest.raises(ValueError, match="krája"):
        build(payload)


def test_requires_the_last_step_to_put_the_finished_dish_on_the_table():
    payload = with_steps([
        "Nakrájaj 2 cibule na kocky a opeč ich na 2 lyžiciach oleja 5 minút do sklovita.",
        "Prilej 200 ml vody, osoľ štipkou soli a na miernom ohni duste 15 minút, kým nezmäknú.",
        "Nakoniec všetko ešte raz premiešaj vareškou a 2 minúty povar bez pokrievky.",
    ])

    with pytest.raises(ValueError, match="podáv"):
        build(payload)


def test_requires_the_recipe_to_say_what_the_result_should_look_like():
    payload = with_steps([
        "Nakrájaj 2 cibule na kocky a opeč ich na 2 lyžiciach oleja presne 5 minút.",
        "Prilej 200 ml vody, osoľ štipkou soli a na miernom ohni duste 15 minút.",
        "Rozdeľ na 4 taniere, posyp 1 lyžičkou korenia a hneď podávaj.",
    ])

    with pytest.raises(ValueError, match="vyzerať"):
        build(payload)


def test_cold_recipe_needs_no_temperature_when_nothing_is_heated():
    """Šalát sa nezohrieva — teplotu vyžadujeme len tam, kde sa naozaj varí."""
    payload = with_steps([
        "Nakrájaj 2 cibule najemno a 400 g mrkvy na kolieska hrubé 1 cm.",
        "Zmiešaj v mise 200 ml vody, 2 lyžice oleja a štipku soli s korením.",
        "Nechaj odstáť 15 minút, kým zelenina nepustí šťavu, potom šalát rozdeľ na taniere a podávaj.",
    ], name="Cibuľový šalát")

    plan = build(payload)

    assert len(plan["jedla"][0]["recept"]["kroky"]) == 3


def test_staples_stay_in_the_steps_and_never_reach_the_shopping_list():
    payload = with_steps([
        "Na 2 lyžiciach oleja opeč 2 cibule nakrájané na kocky 5 minút do sklovita.",
        "Prilej 200 ml vody, osoľ štipkou soli a okoreň mletým čiernym korením.",
        "Duste na miernom ohni 15 minút, dochuť soľou a rozdeľ na taniere a podávaj.",
    ])

    plan = build(payload)

    kroky = " ".join(plan["jedla"][0]["recept"]["kroky"])
    assert all(zakladna in kroky for zakladna in ("oleja", "vody", "soli", "korením"))
    nakup = [item["nazov"].casefold() for store in plan["nakupny_zoznam"] for item in store["polozky"]]
    assert sorted(nakup) == ["chlieb", "maslo", "mlieko"]
    assert not any(zakladna in nazov for nazov in nakup for zakladna in ("soľ", "olej", "vod", "koren"))
    assert plan["nakup_spolu"] == "5,40"
    assert all("davka" not in polozka
               for store in plan["nakupny_zoznam"] for polozka in store["polozky"])


# ------------------------------------------------------------------- prompt
def full_prompt(frequency=2, pantry=("soľ",), household_size=4):
    """Všetko, čo model naozaj dostane: cachovaná predpona aj osobný chvost."""
    blocks = personal_plan_messages(
        offers_connection(), frequency, list(pantry), household_size=household_size
    )
    return "\n\n".join(block["text"] for block in blocks)


def test_prompt_demands_cookable_steps_with_quantities_temperatures_and_times():
    prompt = full_prompt()

    assert "aspoň 3 kroky" in prompt
    assert "°C" in prompt and "minút" in prompt
    assert "Na 2 lyžiciach oleja opeč 2 cibule nakrájané na kocky 5 minút do sklovita" in prompt
    assert "Pridaj cibuľu a opeč" in prompt


def test_prompt_demands_a_shape_a_temperature_and_a_finished_plate():
    prompt = full_prompt()

    assert "na kocky, na plátky, na prúžky, na kolieska, najemno" in prompt
    assert "Recept sa končí na stole." in prompt
    assert "do sklovita, dozlata, kým nezmäkne" in prompt


def test_prompt_spells_out_the_portion_arithmetic_instead_of_hoping_the_model_guesses():
    prompt = full_prompt()
    tail = personal_plan_prompt(offers_connection(), 2, ["soľ"], household_size=4)

    assert "amount_per_adult" in prompt and "unit = g, ml alebo ks" in prompt
    assert "PO: navar 8 porcií na 2 dni" in tail, "4 osoby × 2 dni"
    assert "NE: navar 4 porcie na 1 deň" in tail, "nedeľa nesmie variť do pondelka"
    assert "amount_per_adult × počet dospelých kuchárskych" in tail


def test_prompt_shows_a_whole_worked_recipe_that_passes_our_own_validation():
    """Vzor v prompte je jediný spoľahlivý spôsob, ako opraviť formulácie —
    a nesmie byť taký, aký by nám vlastná kontrola vrátila."""
    prompt = full_prompt()
    example = example_recipe()

    assert example["name"] in prompt
    for step in example["instructions"]:
        assert step in prompt
    plan = build(with_steps(example["instructions"], name=example["name"]))
    assert plan["jedla"][0]["recept"]["kroky"] == example["instructions"]


def test_prompt_demands_a_name_that_matches_what_the_steps_do():
    prompt = full_prompt()

    assert "na ryži" in prompt and "s ryžou" in prompt
    assert "Názov musí opisovať presne to, čo kroky naozaj urobia." in prompt


def test_recipe_rules_ride_in_the_cached_prefix_and_never_in_the_personal_tail():
    """Pravidlá sú pre každého rovnaké — platiť ich pri každom prepočte je zbytočné."""
    rows = offers_connection()

    blocks = personal_plan_messages(rows, 2, ["soľ"], household_size=4)

    assert "na kocky" in blocks[0]["text"], "pravidlá receptu patria do cachovanej predpony"
    assert len(blocks[1]["text"]) < len(blocks[0]["text"]), "osobný chvost musí ostať malý"
    assert blocks[0] == personal_plan_messages(rows, 3, [], household_size=9)[0]


def test_prompt_allows_household_staples_but_keeps_them_out_of_the_offers():
    prompt = full_prompt()

    assert "soľ, korenie, olej, voda" in prompt
    assert "Nikdy ich neuvádzaj v items" in prompt


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
        meal["items"] = [item(offer_key, 100, "g")]
    ingest_three(con, "new")

    with pytest.raises(ValueError, match="neznáme"):
        build_personal_plan(con, delayed, ["Lidl"], 2, 4, pantry=["soľ"], today=TODAY)


# ------------------------------------------------------- zdieľaný plán (podpis)
SIGNATURE_KEYS = ("offer_a", "offer_b", "offer_c")


def signature(**overrides):
    arguments = {
        "week": "2026-08-17", "stores": ["Lidl", "Tesco"], "household_size": 4,
        "frequency": 2, "offer_keys": SIGNATURE_KEYS, "pantry": ["soľ"],
    }
    arguments.update(overrides)
    return plan_signature(**arguments)


def test_two_users_with_the_same_profile_share_one_plan_signature():
    """Rovnaký profil + rovnaké ponuky = rovnaký plán, takže sa nesmie počítať dvakrát."""
    assert signature() == signature()
    # Poradie obchodov ani poradie či veľkosť písmen v špajze nie sú iný profil.
    assert signature(stores=["Tesco", "Lidl"]) == signature()
    assert signature(pantry=["SOĽ"]) == signature()
    assert signature(offer_keys=("offer_c", "offer_a", "offer_b")) == signature()
    assert isinstance(signature(), str) and signature()


def test_signature_changes_whenever_the_plan_could_legitimately_differ():
    """Každý vstup, ktorý mení plán, musí meniť aj podpis — inak by sa podával starý plán."""
    assert signature(week="2026-08-24") != signature()
    assert signature(stores=["Lidl"]) != signature()
    assert signature(household_size=2) != signature()
    assert signature(frequency=3) != signature()


def test_the_pantry_deliberately_stays_out_of_the_shared_signature():
    """Špajza plán neskladá, tak ho ani nesmie rozdeliť na neZdieľateľné kusy.

    Kým v podpise bola, mal každý platiaci účet vlastný kľúč, nikdy sa netrafil
    do zdieľanej cache a čakal 60–120 sekúnd. Špajza sa teraz dopočíta až nad
    nákupným zoznamom (`apply_pantry_to_shopping_list`).
    """
    assert signature(pantry=["soľ", "vajcia"]) == signature()
    assert signature(pantry=[]) == signature()
    # Jediná výnimka: výslovné „navrhni jedlá z toho, čo mám doma".
    assert signature(pantry=["soľ"], pantry_driven=True) != signature()


def test_signature_changes_when_the_underlying_offers_change():
    """Žiadne staré dáta: nový leták aj vypršaná ponuka musia zdieľaný plán zneplatniť."""
    assert signature(offer_keys=("offer_a", "offer_b")) != signature()
    assert signature(offer_keys=SIGNATURE_KEYS + ("offer_d",)) != signature()


def test_variants_spread_households_over_a_small_deterministic_set_of_plans():
    """Nie každá štvorčlenná domácnosť smie dostať bajt na bajt ten istý jedálniček."""
    assert {plan_variant_for(user_id, 3) for user_id in range(1, 40)} == {0, 1, 2}
    assert plan_variant_for(7, 3) == plan_variant_for(7, 3)
    assert plan_variant_for(4, 1) == 0


def test_offer_catalogue_is_the_same_stable_prefix_for_every_user_of_the_week():
    """Blok ponúk je najväčšia časť promptu a pre celý týždeň rovnaký — inak sa nedá cachovať."""
    rows = offers_connection()

    catalog = offers_catalog(rows)

    assert catalog == offers_catalog(rows)
    assert all(row["offer_key"] in catalog for row in rows)
    # Nič osobné sa doň nesmie dostať, inak prestane byť spoločnou predponou.
    for personal in ("osôb", "Špajza", "PO, ST a PI"):
        assert personal not in catalog


def test_personal_task_carries_no_offer_catalogue_so_the_catalogue_can_be_cached():
    rows = offers_connection()

    task = personal_plan_prompt(rows, 2, ["soľ"], household_size=4)

    assert all(row["offer_key"] not in task for row in rows)
    assert "4 osoby" in task


def test_messages_put_the_cached_catalogue_before_anything_personal():
    rows = offers_connection()

    blocks = personal_plan_messages(rows, 2, ["soľ"], household_size=4)

    assert [block["type"] for block in blocks] == ["text", "text"]
    assert blocks[0]["cache_control"] == {"type": "ephemeral"}, "prefix must be cached"
    assert "cache_control" not in blocks[1], "the personal tail must never be cached"
    assert rows[0]["offer_key"] in blocks[0]["text"]
    assert "4 osoby" in blocks[1]["text"]
    # Predpona sa nesmie hýbať s profilom, inak sa cache nikdy netrafí.
    assert blocks[0] == personal_plan_messages(rows, 3, [], household_size=9)[0]


def test_messages_use_prompt_rows_but_plan_validation_keeps_full_verified_rows():
    con = connection(verified_rows())
    full_rows = current_verified_offers(con, ["Lidl", "Tesco"], TODAY)
    prompt_rows = [full_rows[0]]

    blocks = personal_plan_messages(full_rows, 2, [], household_size=4, prompt_rows=prompt_rows)

    assert prompt_rows[0]["offer_key"] in blocks[0]["text"]
    assert full_rows[1]["offer_key"] not in blocks[0]["text"]
    assert "z 1 overených ponúk" in blocks[1]["text"]

    payload = model_output()
    assert build_personal_plan(
        con, payload, ["Lidl", "Tesco"], 2, 4, pantry=["soľ"], today=TODAY
    )["jedla"][1]["suroviny"][0]["offer_key"] == full_rows[1]["offer_key"]


def test_each_variant_asks_the_model_for_a_visibly_different_menu():
    rows = offers_connection()

    tails = [personal_plan_messages(rows, 2, ["soľ"], 4, variant=v)[1]["text"] for v in range(3)]

    assert len(set(tails)) == 3, "variants must not collapse into the same request"
