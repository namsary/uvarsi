from datetime import date

import pytest

from app.landing_data import (
    landing_data_is_current,
    load_landing_data,
    validate_landing_data,
    write_landing_data_atomic,
)


def payload():
    return {
        "schema_version": 1,
        "generated_at": "2026-08-18T05:02:20+02:00",
        "week": "2026-08-17",
        "week_label": "17.–23. 8. 2026",
        "sources": [],
        "receipt": {
            "meals": [{"day": "PO", "name": "Test", "items": []}],
            "nakup_spolu": "1,00",
            "bezne": "2,00",
            "usetris": "1,00",
        },
    }


def item(**overrides):
    base = {
        "offer_key": "offer_test", "name": "Mlieko", "store": "Lidl", "unit": "1 l",
        "quantity": 1, "price": "1,00", "original_price": "1,50", "savings": "0,50",
        "off": "-33 %",
    }
    base.update(overrides)
    return base


def receipt_with(items, **totals):
    data = payload()
    data["receipt"]["meals"][0]["items"] = items
    data["receipt"].update(totals)
    return data


def test_rejects_previous_week():
    data = payload()
    data["week"] = "2026-08-10"

    with pytest.raises(ValueError, match="aktuálny týždeň"):
        validate_landing_data(data, date(2026, 8, 18))


def test_rejects_wrong_savings_math():
    data = payload()
    data["receipt"]["usetris"] = "0,50"

    with pytest.raises(ValueError, match="úspora"):
        validate_landing_data(data, date(2026, 8, 18))


@pytest.mark.parametrize(
    "mutate",
    [
        lambda data: data["receipt"].update(meals="not-a-list"),
        lambda data: data["receipt"].update(meals=[{"day": "PO", "name": "", "items": []}]),
        lambda data: data["receipt"]["meals"][0].update(items="not-a-list"),
        lambda data: data["receipt"]["meals"][0].update(items=["not-an-item"]),
        lambda data: data["receipt"]["meals"][0].update(items=[{"name": "Mlieko", "store": "Lidl", "price": "-1,00"}]),
        lambda data: data["receipt"].update(nakup_spolu="NaN"),
    ],
)
def test_rejects_semantically_invalid_receipt_schema(mutate):
    data = payload()
    mutate(data)

    with pytest.raises(ValueError):
        validate_landing_data(data, date(2026, 8, 18))


def test_rejects_payload_without_required_public_metadata():
    data = payload()
    del data["generated_at"]

    with pytest.raises(ValueError, match="generated_at"):
        validate_landing_data(data, date(2026, 8, 18))


def test_atomic_write_round_trips_current_payload(tmp_path):
    path = tmp_path / "landing_data.json"
    data = validate_landing_data(payload(), date(2026, 8, 18))

    write_landing_data_atomic(path, data)

    assert load_landing_data(path) == data
    assert landing_data_is_current(path, date(2026, 8, 18)) is True
    assert not path.with_suffix(".tmp").exists()


def test_accepts_items_without_a_verified_regular_price():
    data = receipt_with(
        [item(original_price=None, savings=None), item(name="Chlieb")],
        nakup_spolu="2,00", bezne="2,50", usetris="0,50",
        polozky=2, polozky_s_beznou_cenou=1,
    )

    assert validate_landing_data(data, date(2026, 8, 18)) is data


def test_rejects_item_saving_claimed_without_a_verified_regular_price():
    data = receipt_with(
        [item(original_price=None, savings="0,50")],
        nakup_spolu="1,00", bezne="1,50", usetris="0,50",
    )

    with pytest.raises(ValueError, match="bez overenej bežnej ceny"):
        validate_landing_data(data, date(2026, 8, 18))


@pytest.mark.parametrize("bad", [{"savings": "0,90"}, {"savings": None}, {"original_price": "0,50"}])
def test_rejects_item_saving_that_contradicts_its_regular_price(bad):
    data = receipt_with([item(**bad)], nakup_spolu="1,00", bezne="1,50", usetris="0,50")

    with pytest.raises(ValueError):
        validate_landing_data(data, date(2026, 8, 18))


def test_rejects_savings_claim_when_no_item_has_a_verified_regular_price():
    data = receipt_with(
        [item(original_price=None, savings=None)],
        nakup_spolu="1,00", bezne="2,00", usetris="1,00",
        polozky=1, polozky_s_beznou_cenou=0,
    )

    with pytest.raises(ValueError, match="bez overenej bežnej ceny"):
        validate_landing_data(data, date(2026, 8, 18))


def test_rejects_verified_price_count_that_does_not_match_the_items():
    data = receipt_with(
        [item(original_price=None, savings=None)],
        nakup_spolu="1,00", bezne="1,00", usetris="0,00",
        polozky=1, polozky_s_beznou_cenou=1,
    )

    with pytest.raises(ValueError, match="počet"):
        validate_landing_data(data, date(2026, 8, 18))


def test_current_check_rejects_stale_or_invalid_json(tmp_path):
    path = tmp_path / "landing_data.json"
    path.write_text('{"week":"2026-08-10"}', encoding="utf-8")

    assert landing_data_is_current(path, date(2026, 8, 18)) is False

    path.write_text("not-json", encoding="utf-8")
    assert landing_data_is_current(path, date(2026, 8, 18)) is False
