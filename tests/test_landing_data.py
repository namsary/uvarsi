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


def test_current_check_rejects_stale_or_invalid_json(tmp_path):
    path = tmp_path / "landing_data.json"
    path.write_text('{"week":"2026-08-10"}', encoding="utf-8")

    assert landing_data_is_current(path, date(2026, 8, 18)) is False

    path.write_text("not-json", encoding="utf-8")
    assert landing_data_is_current(path, date(2026, 8, 18)) is False
