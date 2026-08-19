import json
import sqlite3
import sys
import types
from datetime import date
from pathlib import Path

import pytest

from hetzner import refresh_blocek
from hetzner.refresh_blocek import compose_with_llm, landing_data_output_path, refresh_from_db


TODAY = date(2026, 8, 18)


def verified_database(path):
    con = sqlite3.connect(path)
    con.execute(
        """CREATE TABLE akcie (
            id INTEGER PRIMARY KEY, tyzden TEXT, obchod TEXT, nazov TEXT, kategoria TEXT,
            cena REAL, povodna REAL, zlava TEXT, jednotka TEXT, source_url TEXT,
            source_page INTEGER, valid_from TEXT, valid_to TEXT
        )"""
    )
    con.executemany(
        "INSERT INTO akcie VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (1, "2026-08-17", "Lidl", "Mlieko", "mliecne", 1.0, 1.5, "-33 %", "1 l",
             "https://source.test/lidl", 2, "2026-08-17", "2026-08-23"),
            (2, "2026-08-17", "Tesco", "Chlieb", "pecivo", 1.2, 1.8, "-33 %", "500 g",
             "https://source.test/tesco", 4, "2026-08-17", "2026-08-23"),
            (3, "2026-08-17", "Lidl", "Maslo", "mliecne", 2.0, 2.5, "-20 %", "250 g",
             "https://source.test/lidl", 3, "2026-08-17", "2026-08-23"),
        ],
    )
    con.commit()
    con.close()


def model_selection():
    return {
        "meals": [{
            "day": "PO",
            "name": "Raňajky",
            "instructions": ["Podávaj čerstvé."],
            "items": [{"offer_id": 1}, {"offer_id": 2}, {"offer_id": 3}],
        }]
    }


def test_refresh_publishes_from_verified_db_without_http(monkeypatch, tmp_path):
    database = tmp_path / "uvarsi.db"
    output = tmp_path / "landing_data.json"
    verified_database(database)

    def forbidden_http(*args, **kwargs):
        raise AssertionError("receipt refresh must not use HTTP")

    try:
        import requests
    except ImportError:
        pass
    else:
        monkeypatch.setattr(requests, "get", forbidden_http)
        monkeypatch.setattr(requests, "post", forbidden_http)

    refresh_from_db(output, database, lambda prompt: model_selection(), today=TODAY)

    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["receipt"]["nakup_spolu"] == "4,20"
    assert payload["receipt"]["bezne"] == "5,80"
    assert payload["sources"][0]["url"] == "https://source.test/lidl"


def test_malformed_non_null_offer_blocks_publication_before_compose(tmp_path):
    database = tmp_path / "uvarsi.db"
    output = tmp_path / "landing_data.json"
    verified_database(database)
    with sqlite3.connect(database) as con:
        con.execute("UPDATE akcie SET source_url='' WHERE id=3")
        con.commit()
    compose_calls = []

    with pytest.raises(SystemExit, match="overených"):
        refresh_from_db(
            output,
            database,
            lambda prompt: compose_calls.append(prompt),
            today=TODAY,
        )

    assert compose_calls == []
    assert not output.exists()


def fake_anthropic(constructors):
    class Messages:
        def create(self, **kwargs):
            return types.SimpleNamespace(
                content=[types.SimpleNamespace(type="text", text='{"meals": []}')]
            )

    class Anthropic:
        def __init__(self, **kwargs):
            constructors.append(kwargs)
            self.messages = Messages()

    return types.SimpleNamespace(Anthropic=Anthropic)


def test_model_adapter_uses_api_key_from_environment(monkeypatch, tmp_path):
    constructors = []
    monkeypatch.setenv("ANTHROPIC_API_KEY", "environment-test-key")
    monkeypatch.setattr(refresh_blocek, "ENV_FILE", str(tmp_path / "missing.env"), raising=False)
    monkeypatch.setitem(sys.modules, "anthropic", fake_anthropic(constructors))

    compose_with_llm("prompt")

    assert constructors == [{"api_key": "environment-test-key", "timeout": 120.0, "max_retries": 1}]


def test_model_adapter_uses_api_key_from_env_file(monkeypatch, tmp_path):
    constructors = []
    env_file = tmp_path / "uvarsi.env"
    env_file.write_text("IGNORED=x\nANTHROPIC_API_KEY=file-test-key\n", encoding="utf-8")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(refresh_blocek, "ENV_FILE", str(env_file), raising=False)
    monkeypatch.setitem(sys.modules, "anthropic", fake_anthropic(constructors))

    compose_with_llm("prompt")

    assert constructors == [{"api_key": "file-test-key", "timeout": 120.0, "max_retries": 1}]


def test_missing_api_key_fails_before_anthropic_client(monkeypatch, tmp_path):
    constructors = []
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(refresh_blocek, "ENV_FILE", str(tmp_path / "missing.env"), raising=False)
    monkeypatch.setitem(sys.modules, "anthropic", fake_anthropic(constructors))

    with pytest.raises(SystemExit, match="ANTHROPIC_API_KEY"):
        compose_with_llm("prompt")

    assert constructors == []


@pytest.mark.parametrize(
    "configured, expected",
    [(None, "/opt/uvarsi/uvarsi.db"), ("D:/data/uvarsi.db", "D:/data/uvarsi.db")],
)
def test_main_uses_default_or_explicit_database_path(monkeypatch, configured, expected):
    calls = []
    if configured is None:
        monkeypatch.delenv("UVARSI_DB", raising=False)
    else:
        monkeypatch.setenv("UVARSI_DB", configured)
    monkeypatch.setattr(sys, "argv", ["refresh_blocek.py"])
    monkeypatch.setattr(
        refresh_blocek,
        "refresh_from_db",
        lambda path, database, compose, today: calls.append((path, database, compose, today)),
    )

    refresh_blocek.main()

    assert calls[0][0] == Path("/var/lib/uvarsi/landing_data.json")
    assert calls[0][1] == expected


def test_refresh_rejects_any_output_path_except_the_landing_json():
    assert landing_data_output_path([]) == Path("/var/lib/uvarsi/landing_data.json")
    assert landing_data_output_path(["/var/lib/uvarsi/landing_data.json"]) == Path("/var/lib/uvarsi/landing_data.json")

    with pytest.raises(SystemExit, match="landing_data.json"):
        landing_data_output_path(["/var/www/uvarsi/index.html"])


def test_index_hides_receipt_and_savings_claims_until_current_data_arrives():
    html = Path("index.html").read_text(encoding="utf-8")

    assert 'id="landing-data" aria-live="polite" hidden' in html
    assert 'id="landing-model" hidden' in html
    assert 'fetch("/api/public/landing")' in html
    assert "Reálnu úsporu vidíš priamo na bločku vyššie" not in html
    assert "Za rok to vie byť pokojne pár stoviek eur" not in html
