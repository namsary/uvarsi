"""Public owner alerts must never carry customer or provider identifiers."""

import inspect
import json
import sqlite3
from contextlib import closing

import pytest
from fastapi.testclient import TestClient

from app import platby
from test_platby import (
    aktivne,
    objednavka,
    posli_webhook,
    vytvor_pouzivatela,
    zapnute_platby,
)


@pytest.mark.parametrize(
    "forbidden",
    ["order-123", "user@example.com", "ls_987", "user_id=7"],
)
def test_public_payment_alert_contains_no_customer_identifier(forbidden):
    alert = platby.priprav_upozornenie(
        platby.DRUH_DUPLICITA,
        pocet=1,
        den="2026-09-07",
        objednavka="order-123",
    )

    assert forbidden not in json.dumps(alert)


def test_alert_builder_does_not_interpolate_provider_order_ids():
    source = inspect.getsource(platby.priprav_upozornenie)

    assert "{objednavka}" not in source
    assert "Objednávka {" not in source


def test_protected_payment_case_keeps_the_actionable_provider_reference():
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.execute(
        "CREATE TABLE pouzivatelia (id INTEGER PRIMARY KEY, email TEXT, platiaci INTEGER DEFAULT 0)"
    )
    platby.migrate_platby_schema(con)

    case_id = platby.create_payment_case(
        con,
        case_type=platby.DRUH_DUPLICITA,
        provider_order_id="order-123",
        user_id=7,
        now=1000,
    )

    row = con.execute(
        "SELECT case_type, provider_order_id, user_id, status FROM payment_cases WHERE id=?",
        (case_id,),
    ).fetchone()
    assert tuple(row) == (platby.DRUH_DUPLICITA, "order-123", 7, "open")
    con.close()


def test_duplicate_webhook_stores_reference_but_sends_only_aggregate_alert(
    monkeypatch, tmp_path
):
    server = zapnute_platby(monkeypatch, tmp_path)
    vytvor_pouzivatela(server)
    sent = []
    monkeypatch.setattr(server, "posli_upozornenie_majitelovi", sent.append)
    client = TestClient(server.app, raise_server_exceptions=False)
    assert posli_webhook(client, objednavka(order_id="order-first")).status_code == 200
    sent.clear()

    response = posli_webhook(client, objednavka(order_id="order-secret-123"))

    assert response.status_code == 200
    assert len(aktivne(server)) == 1
    assert len(sent) == 1
    public = json.dumps(sent, ensure_ascii=False)
    assert "order-secret-123" not in public
    assert "nevyriešen" in public
    with closing(server.db()) as con:
        stored = con.execute(
            "SELECT provider_order_id, user_id FROM payment_cases WHERE case_type=?",
            (server.DRUH_DUPLICITA,),
        ).fetchone()
    assert tuple(stored) == ("order-secret-123", 1)


def test_unassignable_webhook_stores_reference_but_never_publishes_it(
    monkeypatch, tmp_path
):
    server = zapnute_platby(monkeypatch, tmp_path)
    vytvor_pouzivatela(server)
    sent = []
    monkeypatch.setattr(server, "posli_upozornenie_majitelovi", sent.append)
    client = TestClient(server.app, raise_server_exceptions=False)

    response = posli_webhook(
        client,
        objednavka(order_id="order-orphan-secret", user_id=4242),
    )

    assert response.status_code == 400
    assert len(sent) == 1
    assert "order-orphan-secret" not in json.dumps(sent, ensure_ascii=False)
    with closing(server.db()) as con:
        stored = con.execute(
            "SELECT provider_order_id, user_id FROM payment_cases WHERE case_type=?",
            (server.DRUH_NEPOUZITELNA,),
        ).fetchone()
    assert tuple(stored) == ("order-orphan-secret", 4242)
