import json
import sqlite3
import sys
from datetime import date
from pathlib import Path

import pytest

from hetzner import refresh_blocek
from hetzner.refresh_blocek import landing_data_output_path, refresh_from_db
from app.deterministic_plan import NoCompatiblePlan
from app.offer_data import migrate_akcie_schema, offer_key_for
from app.receipt_data import StructuralFailure
from app.ingredient_catalog import load_ingredient_catalog
from tests.test_recipe_mode_matrix import MATCHABLE_PRODUCT_NAMES, VERIFIED_WEEKLY_OFFERS


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
    migrate_akcie_schema(con)
    con.row_factory = sqlite3.Row
    for row in con.execute("SELECT rowid, * FROM akcie").fetchall():
        offer = dict(row)
        con.execute(
            "UPDATE akcie SET offer_key=? WHERE rowid=?",
            (offer_key_for(offer["tyzden"], offer), row[0]),
        )
    con.commit()
    con.close()


def model_selection():
    return {
        "meals": [{
            "day": "PO",
            "name": "Raňajky",
            "instructions": ["Podávaj čerstvé."],
            "items": [{"offer_key": refresh_key(1)}, {"offer_key": refresh_key(2)}, {"offer_key": refresh_key(3)}],
        }]
    }


def refresh_key(offer_id):
    rows = [
        {"tyzden": "2026-08-17", "obchod": "Lidl", "nazov": "Mlieko", "kategoria": "mliecne",
         "cena": 1.0, "povodna": 1.5, "zlava": "-33 %", "jednotka": "1 l",
         "source_url": "https://source.test/lidl", "source_page": 2,
         "valid_from": "2026-08-17", "valid_to": "2026-08-23"},
        {"tyzden": "2026-08-17", "obchod": "Tesco", "nazov": "Chlieb", "kategoria": "pecivo",
         "cena": 1.2, "povodna": 1.8, "zlava": "-33 %", "jednotka": "500 g",
         "source_url": "https://source.test/tesco", "source_page": 4,
         "valid_from": "2026-08-17", "valid_to": "2026-08-23"},
        {"tyzden": "2026-08-17", "obchod": "Lidl", "nazov": "Maslo", "kategoria": "mliecne",
         "cena": 2.0, "povodna": 2.5, "zlava": "-20 %", "jednotka": "250 g",
         "source_url": "https://source.test/lidl", "source_page": 3,
         "valid_from": "2026-08-17", "valid_to": "2026-08-23"},
    ]
    return offer_key_for(rows[offer_id - 1]["tyzden"], rows[offer_id - 1])


def test_public_receipt_refresh_contains_no_recipe_model_or_api_key_path():
    source = Path(refresh_blocek.__file__).read_text(encoding="utf-8")

    for forbidden in (
        "anthropic",
        "ANTHROPIC_API_KEY",
        "compose_with_llm",
        "MODEL_BLOCEK",
        "strazeny_klient",
    ):
        assert forbidden not in source


def test_curated_receipt_composer_uses_stable_week_seed_and_real_plan_meals(
    monkeypatch,
):
    offers = [
        {"offer_key": "offer-chicken", "obchod": "Lidl"},
        {"offer_key": "offer-rice", "obchod": "Tesco"},
        {"offer_key": "offer-tomato", "obchod": "Kaufland"},
        {"offer_key": "offer-salad", "obchod": "Lidl"},
    ]
    calls = []

    def fake_builder(**kwargs):
        calls.append(kwargs)
        return {
            "jedla": [
                {
                    "den": "PO",
                    "nazov": "Kuracie soté s ryžou",
                    "recept": {"kroky": ["Opeč mäso.", "Uvar ryžu.", "Podávaj."]},
                    "suroviny": [
                        {"offer_key": "offer-chicken", "mnozstvo": 2},
                        {"offer_key": "offer-rice", "mnozstvo": 1},
                        {"nazov": "soľ", "bez_akcie": True},
                    ],
                },
                {
                    "den": "ŠT",
                    "nazov": "Paradajkové cestoviny",
                    "recept": {"kroky": ["Uvar cestoviny.", "Pridaj paradajky.", "Premiešaj."]},
                    "suroviny": [
                        {"offer_key": "offer-tomato", "mnozstvo": 1},
                    ],
                },
                {
                    "den": "NE",
                    "nazov": "Chrumkavý šalát",
                    "recept": {"kroky": ["Umy šalát.", "Nakráj ho.", "Podávaj."]},
                    "suroviny": [
                        {"offer_key": "offer-salad", "mnozstvo": 1},
                    ],
                },
            ]
        }

    monkeypatch.setattr(refresh_blocek, "build_deterministic_plan", fake_builder)
    monkeypatch.setattr(refresh_blocek, "load_ingredient_catalog", lambda: "ingredients")
    monkeypatch.setattr(
        refresh_blocek,
        "load_recipe_catalog",
        lambda ingredients: ("recipes", ingredients),
    )

    first = refresh_blocek.compose_curated_receipt(offers, TODAY)
    second = refresh_blocek.compose_curated_receipt(offers, TODAY)

    assert first == second == {
        "meals": [
            {
                "day": "PO",
                "name": "Kuracie soté s ryžou",
                "instructions": ["Opeč mäso.", "Uvar ryžu.", "Podávaj."],
                "items": [
                    {"offer_key": "offer-chicken", "quantity": 2},
                    {"offer_key": "offer-rice", "quantity": 1},
                ],
            },
            {
                "day": "ŠT",
                "name": "Paradajkové cestoviny",
                "instructions": ["Uvar cestoviny.", "Pridaj paradajky.", "Premiešaj."],
                "items": [{"offer_key": "offer-tomato", "quantity": 1}],
            },
            {
                "day": "NE",
                "name": "Chrumkavý šalát",
                "instructions": ["Umy šalát.", "Nakráj ho.", "Podávaj."],
                "items": [{"offer_key": "offer-salad", "quantity": 1}],
            },
        ]
    }
    assert len(calls) == 2
    assert calls[0]["seed"] == calls[1]["seed"]
    assert calls[0]["frequency"] == 3
    assert calls[0]["adults"] == 2 and calls[0]["children"] == 2
    assert calls[0]["pantry_driven"] is False
    assert calls[0]["ingredient_catalog"] == "ingredients"
    assert calls[0]["recipe_catalog"] == ("recipes", "ingredients")


def test_curated_receipt_composer_builds_three_practical_meals_from_real_catalog():
    ingredients = load_ingredient_catalog()
    offers = []
    for page, (ingredient_id, store, package, sale, ordinary) in enumerate(
        VERIFIED_WEEKLY_OFFERS, start=1
    ):
        ingredient = ingredients.by_id(ingredient_id)
        offers.append({
            "offer_key": f"landing-{ingredient_id}",
            "obchod": store,
            "nazov": MATCHABLE_PRODUCT_NAMES.get(ingredient_id, ingredient.name),
            "jednotka": package,
            "cena": sale,
            "povodna": ordinary,
            "zlava": "-25 %",
            "valid_from": "2026-08-17",
            "valid_to": "2026-08-23",
            "source_url": f"https://fixtures.uvar.si/{store.casefold()}",
            "source_page": page,
        })

    selection = refresh_blocek.compose_curated_receipt(offers, TODAY)

    assert len(selection["meals"]) == 3
    assert len({meal["name"] for meal in selection["meals"]}) == 3
    assert all(len(meal["instructions"]) >= 3 for meal in selection["meals"])
    selected_keys = [
        item["offer_key"]
        for meal in selection["meals"]
        for item in meal["items"]
    ]
    assert selected_keys
    assert len(selected_keys) == len(set(selected_keys))
    assert set(selected_keys) <= {offer["offer_key"] for offer in offers}


def test_curated_receipt_composer_tries_stable_variants_until_three_meals(
    monkeypatch,
):
    offers = [
        {"offer_key": "offer-a", "obchod": "Lidl"},
        {"offer_key": "offer-b", "obchod": "Tesco"},
        {"offer_key": "offer-c", "obchod": "Kaufland"},
    ]
    calls = []

    def meal(day, name, offer_key):
        return {
            "den": day,
            "nazov": name,
            "recept": {"kroky": ["Priprav suroviny.", "Uvar jedlo.", "Podávaj."]},
            "suroviny": [{"offer_key": offer_key, "mnozstvo": 1}],
        }

    def fake_builder(**kwargs):
        calls.append(kwargs["seed"])
        if len(calls) == 1:
            return {
                "jedla": [
                    meal("PO", "Prvé jedlo", "offer-a"),
                    meal("ŠT", "Druhé jedlo", "offer-a"),
                    meal("NE", "Tretie jedlo", "offer-a"),
                ]
            }
        return {
            "jedla": [
                meal("PO", "Prvé jedlo", "offer-a"),
                meal("ŠT", "Druhé jedlo", "offer-b"),
                meal("NE", "Tretie jedlo", "offer-c"),
            ]
        }

    monkeypatch.setattr(refresh_blocek, "build_deterministic_plan", fake_builder)
    monkeypatch.setattr(refresh_blocek, "load_ingredient_catalog", lambda: "ingredients")
    monkeypatch.setattr(refresh_blocek, "load_recipe_catalog", lambda ingredients: "recipes")

    selection = refresh_blocek.compose_curated_receipt(offers, TODAY)

    assert [meal["name"] for meal in selection["meals"]] == [
        "Prvé jedlo",
        "Druhé jedlo",
        "Tretie jedlo",
    ]
    assert len(calls) == 2
    assert calls[0] != calls[1]


def test_curated_receipt_composer_skips_an_incompatible_seed_variant(monkeypatch):
    offers = [
        {"offer_key": "offer-a", "obchod": "Lidl"},
        {"offer_key": "offer-b", "obchod": "Tesco"},
        {"offer_key": "offer-c", "obchod": "Kaufland"},
    ]
    calls = []

    def meal(day, name, offer_key):
        return {
            "den": day,
            "nazov": name,
            "recept": {"kroky": ["Priprav suroviny.", "Uvar jedlo.", "Podávaj."]},
            "suroviny": [{"offer_key": offer_key, "mnozstvo": 1}],
        }

    def fake_builder(**kwargs):
        calls.append(kwargs["seed"])
        if len(calls) == 1:
            raise NoCompatiblePlan("diet_too_strict", ("use_standard_mode",))
        return {
            "jedla": [
                meal("PO", "Prvé jedlo", "offer-a"),
                meal("ŠT", "Druhé jedlo", "offer-b"),
                meal("NE", "Tretie jedlo", "offer-c"),
            ]
        }

    monkeypatch.setattr(refresh_blocek, "build_deterministic_plan", fake_builder)
    monkeypatch.setattr(refresh_blocek, "load_ingredient_catalog", lambda: "ingredients")
    monkeypatch.setattr(refresh_blocek, "load_recipe_catalog", lambda ingredients: "recipes")

    selection = refresh_blocek.compose_curated_receipt(offers, TODAY)

    assert [meal["name"] for meal in selection["meals"]] == [
        "Prvé jedlo",
        "Druhé jedlo",
        "Tretie jedlo",
    ]
    assert len(calls) == 2


def test_refresh_publishes_a_complete_curated_receipt_without_a_composer(tmp_path):
    database = tmp_path / "uvarsi.db"
    output = tmp_path / "landing_data.json"
    ingredients = load_ingredient_catalog()
    con = sqlite3.connect(database)
    con.execute(
        """CREATE TABLE akcie (
            id INTEGER PRIMARY KEY, tyzden TEXT, obchod TEXT, nazov TEXT,
            kategoria TEXT, cena REAL, povodna REAL, zlava TEXT, jednotka TEXT,
            source_url TEXT, source_page INTEGER, valid_from TEXT, valid_to TEXT
        )"""
    )
    rows = []
    for page, (ingredient_id, store, package, sale, ordinary) in enumerate(
        VERIFIED_WEEKLY_OFFERS, start=1
    ):
        ingredient = ingredients.by_id(ingredient_id)
        rows.append((
            page,
            "2026-08-17",
            store,
            MATCHABLE_PRODUCT_NAMES.get(ingredient_id, ingredient.name),
            ingredient.category,
            float(sale),
            float(ordinary),
            "-25 %",
            package,
            f"https://fixtures.uvar.si/{store.casefold()}",
            page,
            "2026-08-17",
            "2026-08-23",
        ))
    con.executemany(
        "INSERT INTO akcie VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    migrate_akcie_schema(con)
    con.row_factory = sqlite3.Row
    for row in con.execute("SELECT rowid, * FROM akcie").fetchall():
        offer = dict(row)
        con.execute(
            "UPDATE akcie SET offer_key=? WHERE rowid=?",
            (offer_key_for(offer["tyzden"], offer), row[0]),
        )
    con.commit()
    con.close()

    payload = refresh_from_db(output, database, today=TODAY)

    assert output.exists()
    assert payload["week"] == "2026-08-17"
    assert len(payload["receipt"]["meals"]) == 3
    assert payload["receipt"]["polozky"] >= 3
    assert payload["receipt"]["nakup_spolu"] != "0,00"


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

    refresh_from_db(output, database, lambda offers, today: model_selection(), today=TODAY)

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
            lambda offers, today: compose_calls.append((offers, today)),
            today=TODAY,
        )

    assert compose_calls == []
    assert not output.exists()


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
        lambda path, database, today: calls.append((path, database, today)),
    )

    refresh_blocek.main()

    assert calls[0][0] == Path("/var/lib/uvarsi/landing_data.json")
    assert calls[0][1] == expected


def failing_main(monkeypatch, error):
    def explode(path, database, today):
        raise error

    monkeypatch.setattr(sys, "argv", ["refresh_blocek.py"])
    monkeypatch.setattr(refresh_blocek, "refresh_from_db", explode)
    with pytest.raises(SystemExit) as exit_info:
        refresh_blocek.main()
    return exit_info.value.code


def test_structural_failure_exits_with_a_code_that_stops_further_retries(monkeypatch):
    code = failing_main(monkeypatch, StructuralFailure("Málo overených ponúk — nechávam starý bloček."))

    assert code == StructuralFailure.EXIT_CODE
    assert code != refresh_blocek.EXIT_RETRY


@pytest.mark.parametrize(
    "error",
    [ValueError("Model nevrátil platný JSON."), sqlite3.OperationalError("database is locked")],
)
def test_transient_failure_exits_with_the_retryable_code(monkeypatch, error):
    assert failing_main(monkeypatch, error) == refresh_blocek.EXIT_RETRY


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


def test_index_checks_source_expiry_before_rendering_current_price_claims():
    html = Path("index.html").read_text(encoding="utf-8")

    assert "function sourcesAreCurrent(data, now)" in html
    assert "if(!sourcesAreCurrent(data,new Date()))" in html
    assert "Reálnu úsporu vidíš priamo na bločku vyššie" not in html
    assert "Za rok to vie byť pokojne pár stoviek eur" not in html
