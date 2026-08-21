import sqlite3
from datetime import date

import pytest

from app.plan_data import build_personal_plan, meal_count_for_frequency, personal_plan_prompt
from app.plan_data import DAY_ORDER, cached_plan_is_current, cooking_days_for_frequency
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


PONDELOK_KROKY = [
    "V hrnci zohrej 1 l mlieka na strednom ohni 5 minút, nesmie zovrieť.",
    "Pridaj štipku soli a 2 lyžice medu, miešaj 2 minúty metličkou.",
    "Nakrájaj 4 hrubé krajce chleba a podávaj ich k horúcemu mlieku.",
]
STREDA_KROKY = [
    "Nakrájaj 500 g chleba na 8 krajcov hrubých približne 1 cm.",
    "Na každý krajec natri 20 g mäkkého masla a osoľ štipkou soli.",
    "Opeč krajce na panvici na strednom ohni 3 minúty z každej strany.",
]
PIATOK_KROKY = [
    "Rúru predhrej na 200 °C a plech vylož papierom na pečenie.",
    "Nakrájaj 250 g masla na 8 plátkov a rozlož ich na 8 krajcov chleba.",
    "Peč 6 minút, potom posyp mletým korením a hneď podávaj na stôl.",
]


def model_output(items=None):
    return {
        "meals": [
            {
                "day": "PO", "name": "Mliečna večera", "minutes": 25,
                "instructions": list(PONDELOK_KROKY),
                "items": items if items is not None else [{"offer_key": verified_key(1), "quantity": 2}],
                "pantry_ingredients": ["soľ"],
            },
            {
                "day": "ST", "name": "Chlieb s maslom", "minutes": 15,
                "instructions": list(STREDA_KROKY),
                "items": [{"offer_key": verified_key(2), "quantity": 1}],
            },
            {
                "day": "PI", "name": "Maslový toast", "minutes": 20,
                "instructions": list(PIATOK_KROKY),
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
                "recept": {"min": 25, "kroky": PONDELOK_KROKY},
                "suroviny": [
                    {"offer_key": verified_key(1), "nazov": "Mlieko", "obchod": "Lidl", "jednotka": "1 l",
                     "mnozstvo": 2, "cena": "2,20", "povodna": "3,00", "zlava": "-27 %",
                     "source_url": "https://source.test/lidl", "source_page": 1,
                     "valid_from": "2026-08-17", "valid_to": "2026-08-23"},
                    {"spajza": "soľ"},
                ],
            },
            {
                "den": "ST", "nazov": "Chlieb s maslom",
                "recept": {"min": 15, "kroky": STREDA_KROKY},
                "suroviny": [
                    {"offer_key": verified_key(2), "nazov": "Chlieb", "obchod": "Tesco", "jednotka": "500 g",
                     "mnozstvo": 1, "cena": "1,20", "povodna": "1,80", "zlava": "-33 %",
                     "source_url": "https://source.test/tesco", "source_page": 2,
                     "valid_from": "2026-08-17", "valid_to": "2026-08-23"},
                ],
            },
            {
                "den": "PI", "nazov": "Maslový toast",
                "recept": {"min": 20, "kroky": PIATOK_KROKY},
                "suroviny": [
                    {"offer_key": verified_key(3), "nazov": "Maslo", "obchod": "Lidl", "jednotka": "250 g",
                     "mnozstvo": 1, "cena": "2,00", "povodna": "2,50", "zlava": "-20 %",
                     "source_url": "https://source.test/lidl", "source_page": 3,
                     "valid_from": "2026-08-17", "valid_to": "2026-08-23"},
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
    payload["meals"][0]["items"] = [{"offer_key": keys["Mlieko"], "quantity": 2}]
    payload["meals"][1]["items"] = [{"offer_key": keys["Chlieb"], "quantity": 1}]
    payload["meals"][2]["items"] = [{"offer_key": keys["Maslo"], "quantity": 1}]

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


def offers_connection():
    con = reusable_rowid_connection()
    ingest_three(con, "family")
    return current_verified_offers(con, ["Lidl"], TODAY)


def test_cooking_days_are_spaced_by_the_requested_frequency():
    """„Varím raz za 2 dni" znamená PO, ST, PI — nie tri dni po sebe."""
    assert cooking_days_for_frequency(1) == ("PO", "UT", "ST", "ŠT", "PI")
    assert cooking_days_for_frequency(2) == ("PO", "ST", "PI")
    assert cooking_days_for_frequency(3) == ("PO", "ŠT")

    for frequency in (1, 2, 3):
        days = cooking_days_for_frequency(frequency)
        assert len(days) == meal_count_for_frequency(frequency)
        assert {DAY_ORDER.index(b) - DAY_ORDER.index(a) for a, b in zip(days, days[1:])} == {frequency}

    # Aj pri nezmyselnej frekvencii radšej rozostup než tri dni po sebe.
    assert cooking_days_for_frequency(7) == ("PO", "ŠT", "NE")
    assert cooking_days_for_frequency(None) == ("PO", "ST", "PI")
    for frequency in (0, -3, "2", None):
        assert cooking_days_for_frequency(frequency) == ("PO", "ST", "PI")


def test_prompt_names_the_exact_cooking_days_instead_of_only_a_count():
    rows = offers_connection()

    each_second_day = personal_plan_prompt(rows, 2, ["soľ"], household_size=4)
    each_third_day = personal_plan_prompt(rows, 3, ["soľ"], household_size=4)

    assert "na dni PO, ST a PI" in each_second_day
    assert "Varí sa len v dňoch PO, ST a PI" in each_second_day
    assert "Varí sa len v dňoch PO a ŠT" in each_third_day
    assert "zvyšk" in each_second_day


def test_rejects_a_plan_cooked_three_days_in_a_row_instead_of_every_second_day():
    """Presne to, čo appka vygenerovala majiteľovi: PO, UT, ST a potom nič."""
    payload = model_output()
    for meal, day in zip(payload["meals"], ("PO", "UT", "ST")):
        meal["day"] = day

    with pytest.raises(ValueError, match="dni varenia"):
        build_personal_plan(
            connection(verified_rows()), payload, ["Lidl", "Tesco"], 2, 4,
            pantry=["soľ"], today=TODAY,
        )


def test_meals_come_back_in_calendar_order_whatever_order_the_model_sent():
    payload = model_output()
    payload["meals"] = [payload["meals"][2], payload["meals"][0], payload["meals"][1]]

    plan = build_personal_plan(
        connection(verified_rows()), payload, ["Lidl", "Tesco"], 2, 4,
        pantry=["soľ"], today=TODAY,
    )

    assert [meal["den"] for meal in plan["jedla"]] == ["PO", "ST", "PI"]


def test_cooking_every_third_day_is_accepted_only_on_its_own_two_days():
    payload = {"meals": [dict(meal) for meal in model_output()["meals"][:2]]}
    payload["meals"][1]["day"] = "ŠT"

    plan = build_personal_plan(
        connection(verified_rows()), payload, ["Lidl", "Tesco"], 3, 4,
        pantry=["soľ"], today=TODAY,
    )
    assert [meal["den"] for meal in plan["jedla"]] == ["PO", "ŠT"]

    payload["meals"][1]["day"] = "UT"
    with pytest.raises(ValueError, match="dni varenia"):
        build_personal_plan(
            connection(verified_rows()), payload, ["Lidl", "Tesco"], 3, 4,
            pantry=["soľ"], today=TODAY,
        )


def with_steps(steps):
    payload = model_output()
    payload["meals"][0]["instructions"] = list(steps)
    return payload


def test_rejects_recipe_steps_too_generic_to_cook_from():
    """„Pridaj cibuľu a opeč" nepovie koľko, ako dlho ani na čom."""
    payload = with_steps([
        "Pridaj cibuľu a opeč.",
        "Uvar cestoviny.",
        "Podávaj.",
    ])

    with pytest.raises(ValueError, match="všeobecn"):
        build_personal_plan(
            connection(verified_rows()), payload, ["Lidl", "Tesco"], 2, 4,
            pantry=["soľ"], today=TODAY,
        )


def test_accepts_the_same_step_once_it_says_how_much_how_long_and_how_hot():
    payload = with_steps([
        "Na 2 lyžiciach oleja opeč 2 nakrájané cibule 5 minút do sklovita.",
        "Prilej 200 ml vody, osoľ štipkou soli a duste 15 minút pod pokrievkou.",
        "Na miernom ohni prevar ešte 3 minúty a rozdeľ na 4 taniere.",
    ])

    plan = build_personal_plan(
        connection(verified_rows()), payload, ["Lidl", "Tesco"], 2, 4,
        pantry=["soľ"], today=TODAY,
    )

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
                "Na 2 lyžiciach oleja opeč 2 nakrájané cibule 5 minút do sklovita.",
                "Pridaj cibuľu a opeč.",
                "Na miernom ohni duste 15 minút a rozdeľ na 4 taniere.",
            ],
            "všeobecn",
        ),
        (
            [
                "Cibuľu nakrájaj najemno a opeč ju na strednom ohni 5 minút do sklovita.",
                "Prilej vodu, osoľ podľa chuti a všetko poriadne premiešaj vareškou.",
                "Duste pod pokrievkou 20 minút a potom nechaj chvíľu odstáť.",
            ],
            "množstv",
        ),
        (
            [
                "Na 2 lyžiciach oleja opeč 2 nakrájané cibule na strednom ohni.",
                "Pridaj 400 g ryže, 800 ml vody a štipku soli, potom premiešaj.",
                "Duste pod pokrievkou, kým sa voda nevsiakne, a podávaj s petržlenom.",
            ],
            "čas",
        ),
        (
            [
                "Nakrájaj 2 cibule najemno a opeč ich na panvici 5 minút do sklovita.",
                "Pridaj 400 g ryže a 800 ml vody, osoľ štipkou soli a premiešaj.",
                "Nechaj odstáť 15 minút, potom rozdeľ na 4 taniere a podávaj.",
            ],
            "teplot",
        ),
        (
            [
                "Na 2 lyžiciach oleja opeč nakrájanú cibuľu na strednom ohni 5 minút.",
                "Pridaj nakrájanú mrkvu a zeler, premiešaj a duste pod pokrievkou.",
                "Osoľ, okoreň a nechaj odstáť, potom podávaj s petržlenovou vňaťou.",
            ],
            "Väčšina krokov",
        ),
    ],
)
def test_rejects_recipes_that_do_not_say_how_much_how_long_or_how_hot(steps, message):
    with pytest.raises(ValueError, match=message):
        build_personal_plan(
            connection(verified_rows()), with_steps(steps), ["Lidl", "Tesco"], 2, 4,
            pantry=["soľ"], today=TODAY,
        )


def test_cold_recipe_needs_no_temperature_when_nothing_is_heated():
    """Šalát sa nezohrieva — teplotu vyžadujeme len tam, kde sa naozaj varí."""
    payload = with_steps([
        "Nakrájaj 2 cibule najemno a 400 g mrkvy na kolieska hrubé 1 cm.",
        "Zmiešaj v mise 200 ml vody, 2 lyžice oleja a štipku soli s korením.",
        "Nechaj odstáť 15 minút a potom šalát ešte raz dôkladne premiešaj.",
    ])

    plan = build_personal_plan(
        connection(verified_rows()), payload, ["Lidl", "Tesco"], 2, 4,
        pantry=["soľ"], today=TODAY,
    )

    assert len(plan["jedla"][0]["recept"]["kroky"]) == 3


def test_staples_stay_in_the_steps_and_never_reach_the_shopping_list():
    payload = with_steps([
        "Na 2 lyžiciach oleja opeč 2 nakrájané cibule 5 minút do sklovita.",
        "Prilej 200 ml vody, osoľ štipkou soli a okoreň mletým čiernym korením.",
        "Duste na miernom ohni 15 minút a nakoniec ešte raz dochuť soľou.",
    ])

    plan = build_personal_plan(
        connection(verified_rows()), payload, ["Lidl", "Tesco"], 2, 4,
        pantry=["soľ"], today=TODAY,
    )

    kroky = " ".join(plan["jedla"][0]["recept"]["kroky"])
    assert all(zakladna in kroky for zakladna in ("oleja", "vody", "soli", "korením"))
    nakup = [item["nazov"].casefold() for store in plan["nakupny_zoznam"] for item in store["polozky"]]
    assert sorted(nakup) == ["chlieb", "maslo", "mlieko"]
    assert not any(zakladna in nazov for nazov in nakup for zakladna in ("soľ", "olej", "vod", "koren"))
    assert plan["nakup_spolu"] == "5,40"


def test_prompt_demands_cookable_steps_with_quantities_temperatures_and_times():
    prompt = personal_plan_prompt(offers_connection(), 2, ["soľ"], household_size=4)

    assert "aspoň 3 kroky" in prompt
    assert "°C" in prompt and "minút" in prompt
    assert "Na 2 lyžiciach oleja opeč 2 nakrájané cibule 5 minút do sklovita" in prompt
    assert "Pridaj cibuľu a opeč" in prompt


def test_prompt_allows_household_staples_but_keeps_them_out_of_the_offers():
    prompt = personal_plan_prompt(offers_connection(), 2, ["soľ"], household_size=4)

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
        meal["items"] = [{"offer_key": offer_key, "quantity": 1}]
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
    assert signature(pantry=["soľ", "vajcia"]) != signature()
    assert signature(pantry=[]) != signature()


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
    assert "4 osôb" in task


def test_messages_put_the_cached_catalogue_before_anything_personal():
    rows = offers_connection()

    blocks = personal_plan_messages(rows, 2, ["soľ"], household_size=4)

    assert [block["type"] for block in blocks] == ["text", "text"]
    assert blocks[0]["cache_control"] == {"type": "ephemeral"}, "prefix must be cached"
    assert "cache_control" not in blocks[1], "the personal tail must never be cached"
    assert rows[0]["offer_key"] in blocks[0]["text"]
    assert "4 osôb" in blocks[1]["text"]
    # Predpona sa nesmie hýbať s profilom, inak sa cache nikdy netrafí.
    assert blocks[0] == personal_plan_messages(rows, 3, [], household_size=9)[0]


def test_each_variant_asks_the_model_for_a_visibly_different_menu():
    rows = offers_connection()

    tails = [personal_plan_messages(rows, 2, ["soľ"], 4, variant=v)[1]["text"] for v in range(3)]

    assert len(set(tails)) == 3, "variants must not collapse into the same request"
