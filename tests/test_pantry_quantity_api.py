import json

import pytest

from app.plan_data import apply_pantry_to_shopping_list
from test_server import grant_premium, plan_client, premium_user_server


def quantified_item(name="ryža", amount=450, unit="g"):
    return {"nazov": name, "mnozstvo": amount, "jednotka": unit}


def test_structured_pantry_round_trips_and_normalizes_slovak_piece_units(
        monkeypatch, tmp_path):
    server = premium_user_server(monkeypatch, tmp_path, premium=True)
    client = plan_client(server, 1)

    response = client.post("/api/spajza", json={"polozky": [
        quantified_item("  ryža  ", 450, "g"),
        quantified_item("vajcia", 6, "kusy"),
    ]})

    assert response.status_code == 200
    assert response.json() == {"ok": True, "pocet": 2}
    assert client.get("/api/me").json()["spajza"] == [
        {"nazov": "ryža", "mnozstvo": 450, "jednotka": "g"},
        {"nazov": "vajcia", "mnozstvo": 6, "jednotka": "piece"},
    ]
    with server.db() as con:
        rows = con.execute(
            "SELECT nazov,mnozstvo,jednotka FROM spajza WHERE user_id=1 ORDER BY id"
        ).fetchall()
    assert [tuple(row) for row in rows] == [
        ("ryža", 450, "g"), ("vajcia", 6, "piece"),
    ]


def test_legacy_database_rows_and_safe_legacy_payloads_are_returned_structured(
        monkeypatch, tmp_path):
    server = premium_user_server(
        monkeypatch, tmp_path, pantry=["ryža", "vajcia"], premium=True,
    )
    client = plan_client(server, 1)

    assert client.get("/api/me").json()["spajza"] == [
        {"nazov": "ryža", "mnozstvo": None, "jednotka": None},
        {"nazov": "vajcia", "mnozstvo": None, "jednotka": None},
    ]

    response = client.post("/api/spajza", json={"polozky": ["soľ", "olej"]})

    assert response.status_code == 200
    assert client.get("/api/me").json()["spajza"] == [
        {"nazov": "soľ", "mnozstvo": None, "jednotka": None},
        {"nazov": "olej", "mnozstvo": None, "jednotka": None},
    ]


@pytest.mark.parametrize("payload", [
    {},
    {"polozky": "ryža"},
    {"polozky": [quantified_item("", 100, "g")]},
    {"polozky": [quantified_item("x" * 81, 100, "g")]},
    {"polozky": [quantified_item("ryža", 0, "g")]},
    {"polozky": [quantified_item("ryža", -1, "g")]},
    {"polozky": [quantified_item("ryža", 100_000, "g")]},
    {"polozky": [quantified_item("mlieko", 100_000, "ml")]},
    {"polozky": [quantified_item("vajcia", 10_000, "piece")]},
    {"polozky": [quantified_item("ryža", 450, "kg")]},
    {"polozky": [{"nazov": "ryža", "mnozstvo": 450, "jednotka": None}]},
    {"polozky": [{"nazov": "ryža", "mnozstvo": None, "jednotka": "g"}]},
    {"polozky": [42]},
    {"polozky": [quantified_item(f"položka {index}", 1, "piece") for index in range(61)]},
])
def test_invalid_payload_is_rejected_without_changing_the_existing_pantry(
        monkeypatch, tmp_path, payload):
    server = premium_user_server(monkeypatch, tmp_path, premium=True)
    client = plan_client(server, 1)
    original = {"polozky": [quantified_item("ryža", 500, "g")]}
    assert client.post("/api/spajza", json=original).status_code == 200

    response = client.post("/api/spajza", json=payload)

    assert response.status_code == 422
    assert client.get("/api/me").json()["spajza"] == original["polozky"]


def test_non_finite_amount_is_rejected_atomically(monkeypatch, tmp_path):
    server = premium_user_server(monkeypatch, tmp_path, premium=True)
    client = plan_client(server, 1)
    original = {"polozky": [quantified_item("ryža", 500, "g")]}
    assert client.post("/api/spajza", json=original).status_code == 200

    response = client.post(
        "/api/spajza",
        content=json.dumps({"polozky": [quantified_item("vajcia", 6, "piece")]})
        .replace("6", "NaN", 1),
        headers={"content-type": "application/json"},
    )

    assert response.status_code == 422
    assert client.get("/api/me").json()["spajza"] == original["polozky"]


def test_malformed_json_is_rejected_without_changing_the_existing_pantry(
        monkeypatch, tmp_path):
    server = premium_user_server(monkeypatch, tmp_path, premium=True)
    client = plan_client(server, 1)
    original = {"polozky": [quantified_item("ryža", 500, "g")]}
    assert client.post("/api/spajza", json=original).status_code == 200

    response = client.post(
        "/api/spajza",
        content=b'{"polozky": [',
        headers={"content-type": "application/json"},
    )

    assert response.status_code == 422
    assert client.get("/api/me").json()["spajza"] == original["polozky"]


def test_all_items_are_validated_before_the_single_transaction_replaces_any_rows(
        monkeypatch, tmp_path):
    server = premium_user_server(monkeypatch, tmp_path, premium=True)
    client = plan_client(server, 1)
    original = {"polozky": [quantified_item("ryža", 500, "g")]}
    assert client.post("/api/spajza", json=original).status_code == 200

    response = client.post("/api/spajza", json={"polozky": [
        quantified_item("vajcia", 6, "piece"),
        quantified_item("mlieko", 0, "ml"),
    ]})

    assert response.status_code == 422
    with server.db() as con:
        rows = con.execute(
            "SELECT nazov,mnozstvo,jednotka FROM spajza WHERE user_id=1 ORDER BY id"
        ).fetchall()
    assert [tuple(row) for row in rows] == [("ryža", 500, "g")]


@pytest.mark.parametrize("item", [
    quantified_item("x" * 80, 99_999.999, "g"),
    quantified_item("mlieko", 99_999.999, "ml"),
    quantified_item("vajcia", 9_999.999, "piece"),
])
def test_documented_size_boundaries_below_the_practical_ceiling_are_accepted(
        monkeypatch, tmp_path, item):
    server = premium_user_server(monkeypatch, tmp_path, premium=True)
    client = plan_client(server, 1)

    response = client.post("/api/spajza", json={"polozky": [item]})

    assert response.status_code == 200
    assert client.get("/api/me").json()["spajza"] == [item]


def test_entitlement_loss_hides_but_never_deletes_quantified_pantry(
        monkeypatch, tmp_path):
    server = premium_user_server(monkeypatch, tmp_path, premium=True)
    client = plan_client(server, 1)
    pantry = [quantified_item("ryža", 450, "g")]
    assert client.post("/api/spajza", json={"polozky": pantry}).status_code == 200

    with server.db() as con:
        con.execute("UPDATE naroky SET stav='vrateny' WHERE user_id=1")
        con.commit()

    profile = client.get("/api/me").json()
    assert profile["spajza"] == []
    assert profile["spajza_ulozenych"] == 1
    assert client.post("/api/spajza", json={"polozky": []}).status_code == 403

    grant_premium(server, 1, order_id="ord-1-returned")
    assert client.get("/api/me").json()["spajza"] == pantry


def test_pantry_signature_is_order_insensitive_quantity_sensitive_and_legacy_safe(
        monkeypatch, tmp_path):
    server = premium_user_server(monkeypatch, tmp_path, premium=True)
    rice = quantified_item(" Ryža ", 450.0, "g")
    eggs = quantified_item("VAJCIA", 6, "piece")

    assert server.podpis_spajze([rice, eggs]) == server.podpis_spajze([
        quantified_item("vajcia", 6.0, "piece"),
        quantified_item("ryža", 450, "g"),
    ])
    assert server.podpis_spajze([rice]) != server.podpis_spajze([
        quantified_item("ryža", 451, "g")
    ])
    assert server.podpis_spajze([" Ryža "]) == server.podpis_spajze([
        {"nazov": "ryža", "mnozstvo": None, "jednotka": None}
    ])


def test_pantry_signature_handles_same_legacy_and_measured_name_deterministically(
        monkeypatch, tmp_path):
    server = premium_user_server(monkeypatch, tmp_path, premium=True)
    mixed = [
        {"nazov": "ryža", "mnozstvo": None, "jednotka": None},
        quantified_item(" Ryža ", 450, "g"),
    ]

    assert server.podpis_spajze(mixed) == server.podpis_spajze(list(reversed(mixed)))


def test_quantified_pantry_reduces_required_amount_instead_of_removing_the_whole_item():
    plan = {
        "nakup_spolu": "2,98",
        "nakupny_zoznam": [{
            "obchod": "Lidl",
            "polozky": [{
                "offer_key": "rice", "nazov": "Ryža guľatozrnná",
                "jednotka": "1 kg", "mnozstvo": 2,
                "potrebne": "1200", "potrebna_jednotka": "g",
                "cena": "2,98", "cena_za_balenie": "1,49",
                "povodna": "3,98", "zlava": "-25 %",
            }],
        }],
    }

    adjusted = apply_pantry_to_shopping_list(
        plan, [quantified_item("ryža", 500, "g")],
    )

    rice = adjusted["nakupny_zoznam"][0]["polozky"][0]
    assert rice["ciastocne_doma"] is True
    assert rice["zo_spajze"] == "500 g"
    assert rice["zostava"] == "700 g"
    assert rice["mnozstvo_po_spajzi"] == 1
    assert rice["cena_po_spajzi"] == "1,49"


def test_quantified_pantry_never_removes_a_legacy_row_with_unknown_requirement():
    plan = {
        "nakup_spolu": "1,49",
        "nakupny_zoznam": [{
            "obchod": "Lidl",
            "polozky": [{
                "offer_key": "rice", "nazov": "Ryža guľatozrnná",
                "jednotka": "1 kg", "mnozstvo": 1,
                "cena": "1,49", "cena_za_balenie": "1,49",
                "povodna": "1,99", "zlava": "-25 %",
            }],
        }],
    }

    adjusted = apply_pantry_to_shopping_list(
        plan, [quantified_item("ryža", 1, "g")],
    )

    rice = adjusted["nakupny_zoznam"][0]["polozky"][0]
    assert rice["mnozstvo_po_spajzi"] == 1
    assert rice["cena_po_spajzi"] == "1,49"
    assert rice["mas_doma"] is False
    assert rice["ciastocne_doma"] is False


def test_quantified_pantry_carries_remaining_stock_to_the_next_matching_row():
    def rice_row(key):
        return {
            "offer_key": key, "nazov": "Ryža guľatozrnná",
            "jednotka": "1 kg", "mnozstvo": 1,
            "potrebne": "600", "potrebna_jednotka": "g",
            "cena": "1,49", "cena_za_balenie": "1,49",
            "povodna": "1,99", "zlava": "-25 %",
        }

    plan = {
        "nakup_spolu": "2,98",
        "nakupny_zoznam": [
            {"obchod": "Lidl", "polozky": [rice_row("rice-1")]},
            {"obchod": "Tesco", "polozky": [rice_row("rice-2")]},
        ],
    }

    adjusted = apply_pantry_to_shopping_list(
        plan, [quantified_item("ryža", 1000, "g")],
    )

    first = adjusted["nakupny_zoznam"][0]["polozky"][0]
    second = adjusted["nakupny_zoznam"][1]["polozky"][0]
    assert first["mas_doma"] is True
    assert first["zo_spajze"] == "600 g"
    assert first["mnozstvo_po_spajzi"] == 0
    assert second["ciastocne_doma"] is True
    assert second["zo_spajze"] == "400 g"
    assert second["zostava"] == "200 g"
    assert second["mnozstvo_po_spajzi"] == 1
