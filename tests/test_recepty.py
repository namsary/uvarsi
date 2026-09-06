"""Starý cron recepty.py ostáva iba ako bezplatný validátor landing dát."""
from datetime import date
from pathlib import Path

import pytest

from app.landing_data import write_landing_data_atomic
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


def test_legacy_tool_cannot_call_a_model_or_change_recipe_data():
    source = Path("hetzner/recepty.py").read_text(encoding="utf-8")
    code = source.split('"""', 2)[2]

    assert "anthropic" not in code.lower()
    assert "messages.create" not in code
    assert "ANTHROPIC_API_KEY" not in code
    assert "write_landing_data" not in code
    assert not hasattr(recepty, "gen_recipes")


def test_recepty_never_touches_html():
    source = Path("hetzner/recepty.py").read_text(encoding="utf-8")
    code = source.split('"""', 2)[2]

    assert "index.html" not in code
    assert ".html" not in code
    assert "landing_data" in code


def test_input_path_is_only_the_landing_json():
    assert recepty.landing_data_input_path([]) == recepty.LANDING_DATA_PATH
    assert recepty.landing_data_input_path([str(recepty.LANDING_DATA_PATH)]) == recepty.LANDING_DATA_PATH

    with pytest.raises(SystemExit, match="landing_data.json"):
        recepty.landing_data_input_path(["/var/www/uvarsi/index.html"])


def test_main_only_validates_and_never_rewrites(monkeypatch, tmp_path, capsys):
    path = tmp_path / "landing_data.json"
    write_landing_data_atomic(path, payload())
    before = path.read_bytes()
    before_mtime = path.stat().st_mtime_ns
    monkeypatch.setattr(recepty, "LANDING_DATA_PATH", path)
    monkeypatch.setattr(recepty.sys, "argv", ["recepty.py"])

    recepty.main(today=TODAY)

    assert path.read_bytes() == before
    assert path.stat().st_mtime_ns == before_mtime
    assert "Nič nevytváram ani neprepisujem" in capsys.readouterr().out


def test_main_rejects_stale_data_without_rewriting(monkeypatch, tmp_path):
    path = tmp_path / "landing_data.json"
    data = payload()
    data["week"] = "2026-08-10"
    write_landing_data_atomic(path, data)
    before = path.read_bytes()
    monkeypatch.setattr(recepty, "LANDING_DATA_PATH", path)
    monkeypatch.setattr(recepty.sys, "argv", ["recepty.py"])

    with pytest.raises(SystemExit, match="nie sú použiteľné"):
        recepty.main(today=TODAY)

    assert path.read_bytes() == before
