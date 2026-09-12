"""Annual subscription checkout pricing, reservation, and provider contracts."""

from __future__ import annotations

import json
import sqlite3
import threading
import time

import pytest

from app import platby, predplatne
from app.operator_profile import LEGAL_VERSION


VALID_CONSENT = {
    "accept_terms": True,
    "accept_automatic_renewal": True,
    "request_immediate_activation": True,
    "acknowledge_withdrawal_proration": True,
    "legal_version": LEGAL_VERSION,
}


class FakeCheckoutProvider:
    api_key = "test-api-key"
    store_id = "store-test"
    variant_id = "variant-test"
    founder_discount_id = "discount-test"
    founder_discount_code = "FOUNDERS"

    def __init__(self):
        self.last_checkout_payload = None
        self.overrides = {}

    def create_checkout(self, payload):
        self.last_checkout_payload = payload
        attributes = payload["data"]["attributes"]
        checkout_data = attributes["checkout_data"]
        total = 3900 if "discount_code" in checkout_data else 4900
        response_attributes = {
            "store_id": self.store_id,
            "variant_id": self.variant_id,
            "test_mode": attributes["test_mode"],
            "expires_at": attributes["expires_at"],
            "preview": {"currency": "EUR", "total": total},
            "url": (
                "https://uvarsi.lemonsqueezy.com/checkout/custom/checkout-123"
                "?expires=3700&signature=signed-secret-value"
            ),
        }
        response_attributes.update(self.overrides)
        return {
            "data": {
                "type": "checkouts",
                "id": self.overrides.get("checkout_id", "checkout-123"),
                "attributes": response_attributes,
            }
        }


def _open_database(path=":memory:"):
    con = sqlite3.connect(path, timeout=2)
    con.row_factory = sqlite3.Row
    con.execute(
        "CREATE TABLE pouzivatelia "
        "(id INTEGER PRIMARY KEY, email TEXT, platiaci INTEGER DEFAULT 0)"
    )
    con.executemany(
        "INSERT INTO pouzivatelia (id, email) VALUES (?, ?)",
        [(user_id, f"user-{user_id}@example.test") for user_id in range(1, 80)],
    )
    platby.migrate_platby_schema(con)
    predplatne.migrate_subscription_schema(con)
    con.commit()
    return con


@pytest.fixture
def db():
    con = _open_database()
    yield con
    con.close()


def _insert_verified_founders(con, count, *, first_user_id=1):
    values = []
    for offset in range(count):
        user_id = first_user_id + offset
        values.append(
            (
                user_id,
                predplatne.ANNUAL_PREMIUM_PRODUCT,
                "lemonsqueezy",
                f"customer-{user_id}",
                f"order-{user_id}",
                f"subscription-{user_id}",
                "variant-test",
                "EUR",
                1,
                "active",
                10.0,
                20.0,
                20.0,
                None,
                20.0,
                3900,
                4900,
                "discount-test",
                1,
                1,
                0,
                None,
                10.0,
                10.0,
                10.0,
            )
        )
    con.executemany(
        """INSERT INTO subscriptions
           (user_id,product,provider,provider_customer_id,provider_order_id,
            provider_subscription_id,provider_variant_id,currency,test_mode,
            status,period_start,period_end,renews_at,ends_at,paid_through,
            initial_amount_cents,renewal_amount_cents,discount_id,founder,
            initial_payment_verified,needs_review,review_reason,
            last_verified_event_at,created_at,updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        values,
    )


def _create_attempt(db, *, user_id=60, now=100.0, with_discount=True):
    extra = {}
    if with_discount:
        extra = {
            "founder_discount_id": "discount-test",
            "founder_discount_code": "FOUNDERS",
        }
    return platby.create_subscription_checkout_attempt(
        db,
        user_id=user_id,
        legal_version=LEGAL_VERSION,
        consent=dict(VALID_CONSENT),
        now=now,
        **extra,
    )


def test_founder_attempt_records_39_then_49_and_exact_consent(db):
    attempt = platby.create_subscription_checkout_attempt(
        db,
        user_id=1,
        legal_version=LEGAL_VERSION,
        consent=dict(VALID_CONSENT),
        now=100.0,
    )

    row = db.execute(
        "SELECT * FROM checkout_attempts WHERE public_id=?", (attempt.public_id,)
    ).fetchone()

    assert row["product"] == "premium_annual"
    assert row["amount_cents"] == 3900
    assert row["renewal_amount_cents"] == 4900
    assert row["currency"] == "EUR"
    assert row["billing_interval"] == "year"
    assert row["auto_renews"] == 1
    assert row["founder"] == 1
    assert row["founder_reserved_until"] == 3700.0
    assert json.loads(row["consent_json"]) == VALID_CONSENT


def test_50th_attempt_is_founder_and_51st_is_regular(db):
    _insert_verified_founders(db, 49)
    db.commit()

    fiftieth = _create_attempt(db, user_id=50, now=100.0)
    fifty_first = _create_attempt(db, user_id=51, now=100.0)

    assert fiftieth.founder is True
    assert fiftieth.amount_cents == 3900
    assert fiftieth.renewal_amount_cents == 4900
    assert fifty_first.founder is False
    assert fifty_first.amount_cents == 4900
    assert fifty_first.renewal_amount_cents == 4900
    assert fifty_first.discount_id is None
    assert fifty_first.discount_code is None
    assert fifty_first.founder_reserved_until is None


def test_founder_places_used_counts_only_verified_founder_payments(db):
    _insert_verified_founders(db, 1)
    db.execute(
        "UPDATE subscriptions SET initial_payment_verified=0 WHERE user_id=1"
    )
    _insert_verified_founders(db, 1, first_user_id=2)
    db.execute("UPDATE subscriptions SET founder=0 WHERE user_id=2")
    _insert_verified_founders(db, 1, first_user_id=3)

    assert platby.founder_places_used(db) == 1


def test_abandoned_founder_reservation_expires_and_frees_the_slot(db):
    _insert_verified_founders(db, 49)
    db.commit()
    held = _create_attempt(db, user_id=50, now=100.0)

    regular = _create_attempt(db, user_id=51, now=100.0)
    after_expiry = _create_attempt(
        db,
        user_id=52,
        now=100.0 + platby.CHECKOUT_ATTEMPT_TTL_SECONDS + 1,
    )

    assert held.founder is True
    assert regular.founder is False
    assert after_expiry.founder is True
    assert db.execute(
        "SELECT status FROM checkout_attempts WHERE public_id=?", (held.public_id,)
    ).fetchone()[0] == "expired"


def test_founder_reservation_waits_for_the_immediate_writer_before_counting(tmp_path):
    path = tmp_path / "atomic-founder.db"
    setup = _open_database(path)
    _insert_verified_founders(setup, 49)
    setup.commit()
    setup.execute("BEGIN IMMEDIATE")
    _insert_verified_founders(setup, 1, first_user_id=50)

    started = threading.Event()
    finished = threading.Event()
    result = {}

    def reserve_from_second_connection():
        con = sqlite3.connect(path, timeout=2)
        con.row_factory = sqlite3.Row
        try:
            started.set()
            result["attempt"] = _create_attempt(con, user_id=51, now=100.0)
        except Exception as error:  # surfaced by the assertion below
            result["error"] = error
        finally:
            con.close()
            finished.set()

    worker = threading.Thread(target=reserve_from_second_connection)
    worker.start()
    assert started.wait(1)
    time.sleep(0.05)
    assert finished.is_set() is False
    setup.commit()
    assert finished.wait(2)
    worker.join(timeout=1)
    setup.close()

    assert "error" not in result
    assert result["attempt"].founder is False


def test_subscription_checkout_never_sets_custom_price_and_sends_only_attempt_id(db):
    attempt = _create_attempt(db, user_id=1, now=100.0)
    provider = FakeCheckoutProvider()

    url = platby.create_provider_subscription_checkout(
        attempt=attempt,
        email="a@example.sk",
        provider=provider,
        test_mode=True,
    )

    payload = provider.last_checkout_payload
    attributes = payload["data"]["attributes"]
    assert "custom_price" not in attributes
    assert attributes["preview"] is True
    assert attributes["checkout_options"]["subscription_preview"] is True
    assert attributes["checkout_options"]["skip_trial"] is True
    assert attributes["product_options"]["enabled_variants"] == ["variant-test"]
    assert attributes["checkout_data"] == {
        "email": "a@example.sk",
        "discount_code": "FOUNDERS",
        "custom": {"attempt_id": attempt.public_id},
    }
    assert payload["data"]["relationships"] == {
        "store": {"data": {"type": "stores", "id": "store-test"}},
        "variant": {"data": {"type": "variants", "id": "variant-test"}},
    }
    assert url.endswith("signature=signed-secret-value")
    assert attempt.provider_checkout_id == "checkout-123"


def test_regular_checkout_has_no_founder_discount(db):
    _insert_verified_founders(db, 50)
    db.commit()
    attempt = _create_attempt(db, user_id=60, now=100.0)
    provider = FakeCheckoutProvider()

    platby.create_provider_subscription_checkout(
        attempt=attempt,
        email="regular@example.sk",
        provider=provider,
        test_mode=True,
    )

    checkout_data = provider.last_checkout_payload["data"]["attributes"][
        "checkout_data"
    ]
    assert "discount_code" not in checkout_data


@pytest.mark.parametrize(
    ("override", "match"),
    [
        ({"store_id": "other-store"}, "obchod"),
        ({"variant_id": "other-variant"}, "variant"),
        ({"test_mode": False}, "režim"),
        ({"checkout_id": ""}, "identifikátor"),
        ({"expires_at": "2030-01-01T00:00:00Z"}, "expir"),
        ({"preview": {"currency": "EUR", "total": 4900}}, "sumu"),
    ],
)
def test_provider_checkout_is_rejected_before_url_return_when_identity_is_wrong(
    db, override, match
):
    attempt = _create_attempt(db, user_id=1, now=100.0)
    provider = FakeCheckoutProvider()
    provider.overrides = override

    with pytest.raises(platby.PlatbyNenastavene, match=match):
        platby.create_provider_subscription_checkout(
            attempt=attempt,
            email="a@example.sk",
            provider=provider,
            test_mode=True,
        )

    assert attempt.provider_checkout_id is None


def test_provider_checkout_id_is_stored_but_signed_url_is_never_persisted_or_logged(
    db, caplog, capsys
):
    attempt = _create_attempt(db, user_id=1, now=100.0)
    provider = FakeCheckoutProvider()

    url = platby.create_provider_subscription_checkout(
        attempt=attempt,
        email="a@example.sk",
        provider=provider,
        test_mode=True,
    )
    platby.record_provider_checkout(db, attempt=attempt)
    db.commit()

    stored = db.execute(
        "SELECT provider_checkout_id FROM checkout_attempts WHERE public_id=?",
        (attempt.public_id,),
    ).fetchone()[0]
    dump = "\n".join(db.iterdump())
    output = capsys.readouterr()
    assert stored == "checkout-123"
    assert url not in dump
    assert "signed-secret-value" not in dump
    assert "signed-secret-value" not in output.out
    assert "signed-secret-value" not in output.err
    assert "signed-secret-value" not in caplog.text


def test_checkout_migration_preserves_existing_account_plan_pantry_and_attempt_data():
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE pouzivatelia (id INTEGER PRIMARY KEY, email TEXT)")
    con.execute("CREATE TABLE plany (id INTEGER PRIMARY KEY, obsah TEXT)")
    con.execute("CREATE TABLE spajza (id INTEGER PRIMARY KEY, nazov TEXT)")
    con.execute(
        """CREATE TABLE checkout_attempts (
           public_id TEXT PRIMARY KEY,user_id INTEGER NOT NULL,product TEXT NOT NULL,
           amount_cents INTEGER NOT NULL,currency TEXT NOT NULL,
           legal_version TEXT NOT NULL,privacy_version TEXT NOT NULL,
           accepted_at REAL NOT NULL,expires_at REAL NOT NULL,status TEXT NOT NULL,
           provider_order_id TEXT)"""
    )
    con.execute("INSERT INTO pouzivatelia VALUES (7, 'kept@example.test')")
    con.execute("INSERT INTO plany VALUES (8, 'kept-plan')")
    con.execute("INSERT INTO spajza VALUES (9, 'kept-pantry')")
    con.execute(
        "INSERT INTO checkout_attempts VALUES "
        "('legacy-attempt',7,'zakladajuci_clen',3900,'EUR','v4','v4',1,2,"
        "'paid','legacy-order')"
    )
    con.commit()

    platby.migrate_platby_schema(con)

    assert con.execute("SELECT * FROM pouzivatelia").fetchall() == [
        (7, "kept@example.test")
    ]
    assert con.execute("SELECT * FROM plany").fetchall() == [(8, "kept-plan")]
    assert con.execute("SELECT * FROM spajza").fetchall() == [(9, "kept-pantry")]
    legacy = con.execute(
        "SELECT public_id,provider_order_id FROM checkout_attempts"
    ).fetchone()
    assert legacy == ("legacy-attempt", "legacy-order")
    con.close()
