import sqlite3
from datetime import date

import pytest

from app.receipt_data import build_public_receipt, composition_prompt, eligible_offers
from hetzner.refresh_blocek import refresh_from_db


TODAY = date(2026, 8, 18)


def connection(rows):
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.execute(
        """CREATE TABLE akcie (
            id INTEGER PRIMARY KEY,
            tyzden TEXT, obchod TEXT, nazov TEXT, kategoria TEXT,
            cena REAL, povodna REAL, zlava TEXT, jednotka TEXT,
            source_url TEXT, source_page INTEGER, valid_from TEXT, valid_to TEXT
        )"""
    )
    con.executemany(
        "INSERT INTO akcie VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows
    )
    return con


def verified_rows():
    return [
        (1, "2026-08-17", "Lidl", "Mlieko", "mliecne", 1.0, 1.5, "-33 %", "1 l",
         "https://source.test/lidl", 2, "2026-08-16", "2026-08-19"),
        (2, "2026-08-17", "Tesco", "Chlieb", "pecivo", 1.2, 1.8, "-33 %", "500 g",
         "https://source.test/tesco", 4, "2026-08-18", "2026-08-24"),
        (3, "2026-08-17", "Lidl", "Maslo", "mliecne", 2.0, 2.5, "-20 %", "250 g",
         "https://source.test/lidl", 3, "2026-08-16", "2026-08-19"),
    ]


def selection(items=None):
    return {
        "meals": [
            {
                "day": "PO", "name": "Raňajky", "instructions": ["Podávaj čerstvé."],
                "items": items if items is not None else [
                    {"offer_id": 1, "quantity": 2},
                    {"offer_id": 2, "quantity": 1},
                    {"offer_id": 3, "quantity": 1},
                ],
            }
        ]
    }


def test_reconstructs_every_item_total_and_exact_deduped_sources_from_db():
    payload = build_public_receipt(connection(verified_rows()), selection(), today=TODAY,
                                   generated_at="2026-08-18T06:00:00+00:00")

    assert payload["receipt"]["meals"][0]["items"] == [
        {"offer_id": 1, "name": "Mlieko", "store": "Lidl", "unit": "1 l", "quantity": 2,
         "price": "2,00", "original_price": "3,00", "savings": "1,00", "off": "-33 %"},
        {"offer_id": 2, "name": "Chlieb", "store": "Tesco", "unit": "500 g", "quantity": 1,
         "price": "1,20", "original_price": "1,80", "savings": "0,60", "off": "-33 %"},
        {"offer_id": 3, "name": "Maslo", "store": "Lidl", "unit": "250 g", "quantity": 1,
         "price": "2,00", "original_price": "2,50", "savings": "0,50", "off": "-20 %"},
    ]
    assert payload["receipt"]["nakup_spolu"] == "5,20"
    assert payload["receipt"]["bezne"] == "7,30"
    assert payload["receipt"]["usetris"] == "2,10"
    assert payload["sources"] == [
        {"store": "Lidl", "url": "https://source.test/lidl", "valid_from": "2026-08-16", "valid_to": "2026-08-19"},
        {"store": "Tesco", "url": "https://source.test/tesco", "valid_from": "2026-08-18", "valid_to": "2026-08-24"},
    ]


def test_model_prompt_exposes_only_food_content_and_offer_references():
    rows = connection(verified_rows()).execute("SELECT * FROM akcie ORDER BY id").fetchall()

    prompt = composition_prompt(rows)

    assert "offer_id: 1" in prompt
    assert "Mlieko" in prompt
    assert "Lidl" not in prompt
    assert "1.0" not in prompt
    assert "source.test" not in prompt


@pytest.mark.parametrize(
    "price, original",
    [(0, 1.5), (1.0, 0)],
)
def test_zero_prices_cannot_be_receipt_eligible(price, original):
    row = {
        "cena": price,
        "povodna": original,
    }

    assert eligible_offers([row]) == []


@pytest.mark.parametrize(
    "items, message",
    [
        ([{"offer_id": 99, "quantity": 1}], "neznáme"),
        ([{"offer_id": 1, "quantity": 1, "price": "0,01"}], "nepovolené"),
        ([{"offer_id": 1, "quantity": 1, "store": "Vymyslený obchod"}], "nepovolené"),
        ([{"offer_id": 1, "quantity": 1}, {"offer_id": 1, "quantity": 1}], "duplicitné"),
    ],
)
def test_rejects_unknown_tampered_or_duplicate_model_offer_references(items, message):
    with pytest.raises(ValueError, match=message):
        build_public_receipt(connection(verified_rows()), selection(items), today=TODAY)


def test_rejects_model_authored_totals():
    model_output = selection()
    model_output["nakup_spolu"] = "0,01"

    with pytest.raises(ValueError, match="nepovolené"):
        build_public_receipt(connection(verified_rows()), model_output, today=TODAY)


@pytest.mark.parametrize("kind", ["expired", "legacy"])
def test_expired_or_legacy_only_data_does_not_construct_with_ai(tmp_path, kind):
    rows = verified_rows()
    if kind == "expired":
        rows = [tuple(list(row[:-2]) + ["2026-08-10", "2026-08-17"]) for row in rows]
    else:
        rows = [tuple(list(row[:-4]) + [None, None, None, None]) for row in rows]
    database = tmp_path / "uvarsi.db"
    disk = sqlite3.connect(database)
    disk.execute("""CREATE TABLE akcie (
        id INTEGER PRIMARY KEY, tyzden TEXT, obchod TEXT, nazov TEXT, kategoria TEXT,
        cena REAL, povodna REAL, zlava TEXT, jednotka TEXT, source_url TEXT,
        source_page INTEGER, valid_from TEXT, valid_to TEXT)""")
    disk.executemany("INSERT INTO akcie VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
    disk.commit()
    disk.close()
    called = []

    with pytest.raises(SystemExit, match="overených"):
        refresh_from_db(tmp_path / "landing_data.json", database, lambda prompt: called.append(prompt), today=TODAY)

    assert called == []


def test_insufficient_valid_regular_prices_does_not_construct_with_ai(tmp_path):
    rows = verified_rows()
    rows[2] = tuple(list(rows[2][:6]) + [1.0] + list(rows[2][7:]))
    database = tmp_path / "uvarsi.db"
    disk = sqlite3.connect(database)
    disk.execute("""CREATE TABLE akcie (
        id INTEGER PRIMARY KEY, tyzden TEXT, obchod TEXT, nazov TEXT, kategoria TEXT,
        cena REAL, povodna REAL, zlava TEXT, jednotka TEXT, source_url TEXT,
        source_page INTEGER, valid_from TEXT, valid_to TEXT)""")
    disk.executemany("INSERT INTO akcie VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
    disk.commit()
    disk.close()
    called = []

    with pytest.raises(SystemExit, match="overených"):
        refresh_from_db(tmp_path / "landing_data.json", database, lambda prompt: called.append(prompt), today=TODAY)

    assert called == []


def test_malformed_model_output_preserves_existing_landing_json(tmp_path):
    old = ('{"schema_version":1,"generated_at":"2026-08-18T05:02:20+02:00",'
           '"week":"2026-08-17","week_label":"17.–23. 8. 2026","sources":[],'
           '"receipt":{"meals":[{"day":"PO","name":"Test","items":[]}],'
           '"nakup_spolu":"1,00","bezne":"2,00","usetris":"1,00"}}')
    path = tmp_path / "landing_data.json"
    path.write_text(old, encoding="utf-8")
    database = tmp_path / "uvarsi.db"
    disk = sqlite3.connect(database)
    disk.execute("""CREATE TABLE akcie (
        id INTEGER PRIMARY KEY, tyzden TEXT, obchod TEXT, nazov TEXT, kategoria TEXT,
        cena REAL, povodna REAL, zlava TEXT, jednotka TEXT, source_url TEXT,
        source_page INTEGER, valid_from TEXT, valid_to TEXT)""")
    disk.executemany("INSERT INTO akcie VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", verified_rows())
    disk.commit()
    disk.close()

    with pytest.raises(ValueError, match="neznáme"):
        refresh_from_db(path, database, lambda prompt: selection([{"offer_id": 99, "quantity": 1}]), today=TODAY)

    assert path.read_text(encoding="utf-8") == old
