import importlib
import sqlite3
import sys
from pathlib import Path

import pytest

from app import predplatne


ROOT = Path(__file__).resolve().parents[1]


def connection():
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    return db


def schema_objects(db):
    return [
        tuple(row)
        for row in db.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_master "
            "WHERE name LIKE 'subscription%' ORDER BY type,name"
        )
    ]


def valid_subscription_values():
    return (
        1,
        "premium_annual",
        "lemonsqueezy",
        "cus_1",
        "ord_1",
        "sub_1",
        "var_annual",
        "EUR",
        1,
        "active",
        900.0,
        2_000.0,
        2_000.0,
        None,
        2_000.0,
        3_900,
        4_900,
        "discount_founders",
        1,
        1,
        0,
        None,
        1_000.0,
        1_000.0,
        1_000.0,
    )


def test_subscription_migration_is_additive_and_idempotent():
    db = connection()
    db.executescript(
        """
        CREATE TABLE pouzivatelia (id INTEGER PRIMARY KEY, email TEXT, platiaci INTEGER);
        CREATE TABLE sedenia (token TEXT PRIMARY KEY, user_id INTEGER);
        CREATE TABLE auth_passkeys (credential_id TEXT PRIMARY KEY, user_id INTEGER);
        CREATE TABLE spajza (id INTEGER PRIMARY KEY, user_id INTEGER, nazov TEXT);
        CREATE TABLE plany (id INTEGER PRIMARY KEY, user_id INTEGER, json TEXT);
        CREATE TABLE naroky (id INTEGER PRIMARY KEY, user_id INTEGER, poskytovatel TEXT);
        CREATE TABLE platobne_udalosti (udalost_kluc TEXT PRIMARY KEY, typ TEXT);
        INSERT INTO pouzivatelia VALUES (1,'old@example.sk',1);
        INSERT INTO sedenia VALUES ('session-1',1);
        INSERT INTO auth_passkeys VALUES ('passkey-1',1);
        INSERT INTO spajza VALUES (1,1,'ryža');
        INSERT INTO plany VALUES (1,1,'{"week":"kept"}');
        INSERT INTO naroky VALUES (1,1,'rucne');
        INSERT INTO platobne_udalosti VALUES ('old-event','order_created');
        """
    )
    legacy_rows = {
        table: [tuple(row) for row in db.execute(f"SELECT * FROM {table}")]
        for table in (
            "pouzivatelia",
            "sedenia",
            "auth_passkeys",
            "spajza",
            "plany",
            "naroky",
            "platobne_udalosti",
        )
    }

    predplatne.migrate_subscription_schema(db)
    first_schema = schema_objects(db)
    predplatne.migrate_subscription_schema(db)

    assert schema_objects(db) == first_schema
    assert {
        table: [tuple(row) for row in db.execute(f"SELECT * FROM {table}")]
        for table in legacy_rows
    } == legacy_rows
    assert {
        row[0]
        for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    } >= {"subscriptions", "subscription_invoices", "subscription_events"}


def test_subscription_schema_has_required_fields_and_indexes():
    db = connection()
    predplatne.migrate_subscription_schema(db)

    subscription_columns = {
        row[1] for row in db.execute("PRAGMA table_info(subscriptions)")
    }
    assert subscription_columns >= {
        "user_id",
        "product",
        "provider_customer_id",
        "provider_order_id",
        "provider_subscription_id",
        "provider_variant_id",
        "currency",
        "test_mode",
        "status",
        "period_start",
        "period_end",
        "renews_at",
        "ends_at",
        "paid_through",
        "initial_amount_cents",
        "renewal_amount_cents",
        "discount_id",
        "founder",
        "initial_payment_verified",
        "needs_review",
        "review_reason",
        "last_verified_event_at",
        "created_at",
        "updated_at",
    }
    index_names = {
        row[1] for row in db.execute("PRAGMA index_list(subscriptions)")
    }
    assert index_names >= {
        "subscriptions_user_idx",
        "subscriptions_status_idx",
        "subscriptions_renewal_idx",
        "subscriptions_provider_order_idx",
        "subscriptions_provider_subscription_idx",
    }


def test_provider_ids_are_unique_within_provider_mode():
    db = connection()
    predplatne.migrate_subscription_schema(db)
    columns = (
        "user_id,product,provider,provider_customer_id,provider_order_id,"
        "provider_subscription_id,provider_variant_id,currency,test_mode,status,"
        "period_start,period_end,renews_at,ends_at,paid_through,"
        "initial_amount_cents,renewal_amount_cents,discount_id,founder,"
        "initial_payment_verified,needs_review,review_reason,"
        "last_verified_event_at,created_at,updated_at"
    )
    placeholders = ",".join("?" for _ in range(25))
    db.execute(
        f"INSERT INTO subscriptions ({columns}) VALUES ({placeholders})",
        valid_subscription_values(),
    )

    duplicate = list(valid_subscription_values())
    duplicate[0] = 2
    duplicate[1] = "another_product"
    duplicate[4] = "ord_2"
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            f"INSERT INTO subscriptions ({columns}) VALUES ({placeholders})",
            duplicate,
        )

    live_copy = list(duplicate)
    live_copy[8] = 0
    db.execute(
        f"INSERT INTO subscriptions ({columns}) VALUES ({placeholders})",
        live_copy,
    )

    invoice = (
        "lemonsqueezy",
        1,
        "inv_1",
        "sub_1",
        "ord_1",
        "initial",
        "paid",
        3_900,
        "EUR",
        900.0,
        2_000.0,
        1_000.0,
        0,
        1_000.0,
        1_000.0,
    )
    db.execute(
        "INSERT INTO subscription_invoices "
        "(provider,test_mode,provider_invoice_id,provider_subscription_id,"
        "provider_order_id,invoice_kind,status,amount_cents,currency,"
        "period_start,period_end,paid_at,refunded_amount_cents,created_at,updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        invoice,
    )
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO subscription_invoices "
            "(provider,test_mode,provider_invoice_id,provider_subscription_id,"
            "provider_order_id,invoice_kind,status,amount_cents,currency,"
            "period_start,period_end,paid_at,refunded_amount_cents,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            invoice,
        )


def test_server_database_migration_installs_subscription_schema(monkeypatch, tmp_path):
    monkeypatch.setenv("UVARSI_URL", "https://uvar.si")
    monkeypatch.setenv("UVARSI_VERSION_FILE", str(ROOT / "VERSION"))
    monkeypatch.setenv("UVARSI_STATIC", str(tmp_path / "static"))
    monkeypatch.delenv("PLATBY_ZAPNUTE", raising=False)
    monkeypatch.syspath_prepend(str(ROOT / "app"))
    sys.modules.pop("server", None)
    server = importlib.import_module("server")
    db = connection()

    server.migruj_schemu(db)
    server.migruj_schemu(db)

    tables = {
        row[0]
        for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    assert tables >= {
        "subscriptions",
        "subscription_invoices",
        "subscription_events",
    }
