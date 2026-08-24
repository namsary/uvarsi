"""recepty.py dopĺňa recepty do overených letákových dát, nie do HTML.

Pôvodná verzia čítala jedlá regexom z bloku RCPT v index.html. Odkedy bloček
kreslí prehliadač z /api/public/landing, je ten blok prázdny a nástroj vždy
skončil hláškou „V bločku som nenašiel jedlá.". Druhá cesta k tej istej pravde
sa tým rozpadla — tieto testy držia, aby ostala jediná: landing_data.json.
"""
from datetime import date
from pathlib import Path

import pytest

from app.landing_data import load_landing_data, validate_landing_data, write_landing_data_atomic
from hetzner import recepty


TODAY = date(2026, 8, 18)


def payload():
    return {
        "schema_version": 1,
        "generated_at": "2026-08-18T05:02:20+02:00",
        "week": "2026-08-17",
        "week_label": "17.–23. 8. 2026",
        "sources": [{"store": "Lidl", "url": "https://letak.test/lidl",
                     "valid_from": "2026-08-17", "valid_to": "2026-08-23"}],
        "receipt": {
            "meals": [{
                "day": "PO", "name": "Kuracie stehná",
                "instructions": ["Osoľ.", "Opeč."],
                "items": [{
                    "offer_key": "offer_a", "name": "Kuracie stehná", "store": "Lidl",
                    "unit": "1 kg", "quantity": 1, "price": "2,69",
                    "original_price": "4,00", "savings": "1,31", "off": "-33 %",
                }],
            }],
            "nakup_spolu": "2,69", "bezne": "4,00", "usetris": "1,31",
            "polozky": 1, "polozky_s_beznou_cenou": 1,
        },
    }


def recipe_from_model(_meals):
    return {"PO": {"min": 45, "steps_total": 6,
                   "steps": ["Stehná osoľ.", "Opeč na masti.", "Duste 35 minút."]}}


# ------------------------------------------------------- žiadny druhý zdroj pravdy
def test_recepty_never_touches_index_html_again():
    source = Path("hetzner/recepty.py").read_text(encoding="utf-8")
    # Docstring smie o starom parseri hovoriť; kód sa ho už nesmie dotknúť.
    code = source.split('"""', 2)[2]

    assert "RCPT" not in code, "bloček už v HTML nie je — parsovať sa nedá"
    assert "EX:START" not in code, "modelový príklad kreslí prehliadač, nie tento nástroj"
    assert "index.html" not in code
    assert ".html" not in code, "nástroj nesmie zapisovať do žiadnej stránky"
    assert "landing_data" in code, "jediná pravda je overený landing JSON"


def test_input_path_is_only_the_landing_json():
    assert recepty.landing_data_input_path([]) == recepty.LANDING_DATA_PATH
    assert recepty.landing_data_input_path([str(recepty.LANDING_DATA_PATH)]) == recepty.LANDING_DATA_PATH

    with pytest.raises(SystemExit, match="landing_data.json"):
        recepty.landing_data_input_path(["/var/www/uvarsi/index.html"])


# ------------------------------------------------------------- kedy sa neplatí nič
def test_stale_data_is_refused_before_any_paid_call():
    data = payload()
    data["week"] = "2026-08-10"
    calls = []

    with pytest.raises(SystemExit, match="nedopĺňam"):
        recepty.add_recipes(data, lambda meals: calls.append(meals), today=TODAY)

    assert calls == []


def test_receipt_without_a_substantiated_saving_is_refused_before_any_paid_call():
    data = payload()
    data["receipt"]["meals"][0]["items"][0].update(original_price=None, savings=None)
    data["receipt"].update(bezne="2,69", usetris="0,00", polozky_s_beznou_cenou=0)
    calls = []

    with pytest.raises(SystemExit, match="nedopĺňam"):
        recepty.add_recipes(data, lambda meals: calls.append(meals), today=TODAY)

    assert calls == []


def test_meals_that_already_have_a_recipe_cost_nothing():
    data = payload()
    data["receipt"]["meals"][0]["recipe"] = {"min": 30, "steps_total": 3, "steps": ["Uvar."]}
    calls = []

    _, added = recepty.add_recipes(data, lambda meals: calls.append(meals), today=TODAY)

    assert added == 0
    assert calls == []


# ------------------------------------------------------------------ šťastná cesta
def test_recipes_are_added_without_touching_a_single_commercial_value():
    data = payload()
    before = {key: value for key, value in data["receipt"].items() if key != "meals"}

    result, added = recepty.add_recipes(data, recipe_from_model, today=TODAY)

    assert added == 1
    meal = result["receipt"]["meals"][0]
    assert meal["recipe"]["min"] == 45
    assert meal["recipe"]["steps_total"] == 6
    assert meal["recipe"]["steps"] == ["Stehná osoľ.", "Opeč na masti.", "Duste 35 minút."]
    assert meal["items"][0]["price"] == "2,69"
    assert meal["items"][0]["original_price"] == "4,00"
    assert {key: value for key, value in result["receipt"].items() if key != "meals"} == before
    assert validate_landing_data(result, TODAY) is result


@pytest.mark.parametrize(
    "broken",
    [{"PO": "recept"}, {"PO": {"steps": []}}, {"PO": {"steps": ["   "]}}, {}, None],
)
def test_a_malformed_recipe_from_the_model_is_dropped_not_published(broken):
    data = payload()

    result, added = recepty.add_recipes(data, lambda meals: broken, today=TODAY)

    assert added == 0
    assert "recipe" not in result["receipt"]["meals"][0]
    assert validate_landing_data(result, TODAY) is result


def test_model_may_not_smuggle_prices_into_the_recipe():
    data = payload()

    result, _ = recepty.add_recipes(
        data,
        lambda meals: {"PO": {"steps": ["Uvar."], "price": "0,99", "store": "Tesco"}},
        today=TODAY,
    )

    assert set(result["receipt"]["meals"][0]["recipe"]) <= {"min", "steps", "steps_total"}


def test_main_publishes_atomically_and_the_result_stays_valid(monkeypatch, tmp_path):
    path = tmp_path / "landing_data.json"
    write_landing_data_atomic(path, payload())
    monkeypatch.setattr(recepty, "LANDING_DATA_PATH", path)
    monkeypatch.setattr(recepty, "load_key", lambda: "kluc")
    monkeypatch.setattr(recepty, "gen_recipes", lambda meals, key: recipe_from_model(meals))
    monkeypatch.setattr(recepty.sys, "argv", ["recepty.py"])

    recepty.main(today=TODAY)

    published = load_landing_data(path)
    assert published["receipt"]["meals"][0]["recipe"]["steps_total"] == 6
    assert validate_landing_data(published, TODAY) is published
    assert not path.with_suffix(".tmp").exists()
