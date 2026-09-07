"""Auditable consent and one-time checkout-attempt contracts."""

import sqlite3

import pytest

from app.operator_profile import LEGAL_VERSION
from app.platby import (
    AKCIA_UDELENE,
    CHECKOUT_ATTEMPT_TTL_SECONDS,
    PlatbyNenastavene,
    UdalostNepouzitelna,
    checkout_url,
    create_checkout_attempt,
    get_checkout_attempt,
    migrate_platby_schema,
    payload_z_objednavky,
    spracuj_udalost,
)


@pytest.fixture
def con():
    database = sqlite3.connect(":memory:")
    database.row_factory = sqlite3.Row
    database.execute(
        "CREATE TABLE pouzivatelia (id INTEGER PRIMARY KEY, email TEXT, platiaci INTEGER DEFAULT 0)"
    )
    database.executemany(
        "INSERT INTO pouzivatelia (id, email) VALUES (?, ?)",
        [(7, "seven@example.test"), (8, "eight@example.test")],
    )
    migrate_platby_schema(database)
    yield database
    database.close()


def order_payload(*, user_id=7, attempt_id, order_id="order-1", variant_id="67890"):
    return {
        "meta": {
            "event_name": "order_created",
            "custom_data": {
                "user_id": str(user_id),
                "checkout_attempt": attempt_id,
            },
        },
        "data": {
            "id": order_id,
            "type": "orders",
            "attributes": {
                "total": 3900,
                "currency": "EUR",
                "status": "paid",
                "first_order_item": {"variant_id": variant_id},
            },
        },
    }


def test_checkout_attempt_records_the_exact_one_time_offer_and_legal_versions(con):
    attempt = create_checkout_attempt(
        con, user_id=7, legal_version=LEGAL_VERSION, now=1000
    )

    row = con.execute(
        """SELECT product, amount_cents, currency, legal_version,
                  privacy_version, status, provider_order_id
             FROM checkout_attempts WHERE public_id=?""",
        (attempt,),
    ).fetchone()

    assert tuple(row) == (
        "zakladajuci_clen",
        3900,
        "EUR",
        LEGAL_VERSION,
        LEGAL_VERSION,
        "pending",
        None,
    )
    assert len(attempt) >= 43, "verejný identifikátor musí mať aspoň 256 bitov entropie"


def test_stale_legal_version_cannot_create_checkout_attempt(con):
    with pytest.raises(ValueError, match="právna verzia"):
        create_checkout_attempt(con, user_id=7, legal_version="stara", now=1000)


def test_checkout_attempt_expires_and_cannot_be_reused(con):
    attempt = create_checkout_attempt(
        con, user_id=7, legal_version=LEGAL_VERSION, now=1000
    )

    assert get_checkout_attempt(
        con, attempt, now=1000 + CHECKOUT_ATTEMPT_TTL_SECONDS + 1
    ) is None
    status = con.execute(
        "SELECT status FROM checkout_attempts WHERE public_id=?", (attempt,)
    ).fetchone()[0]
    assert status == "expired"


def test_checkout_url_contains_user_product_and_attempt_but_no_session_token(con):
    attempt = create_checkout_attempt(
        con, user_id=7, legal_version=LEGAL_VERSION, now=1000
    )

    url = checkout_url(
        "https://uvarsi.lemonsqueezy.com/buy/test",
        user_id=7,
        attempt_id=attempt,
        email="seven@example.test",
    )

    assert "checkout%5Bcustom%5D%5Buser_id%5D=7" in url
    assert f"checkout%5Bcustom%5D%5Bcheckout_attempt%5D={attempt}" in url
    assert "checkout%5Bcustom%5D%5Bprodukt%5D=zakladajuci_clen" in url
    assert "session" not in url.casefold()


def test_checkout_url_requires_a_valid_attempt_identifier():
    for attempt in (None, "", "short", "contains spaces", "x" * 129):
        with pytest.raises((ValueError, PlatbyNenastavene)):
            checkout_url(
                "https://uvarsi.lemonsqueezy.com/buy/test",
                user_id=7,
                attempt_id=attempt,
            )


def test_paid_order_must_match_an_unused_attempt_and_marks_it_paid(con):
    attempt = create_checkout_attempt(
        con, user_id=7, legal_version=LEGAL_VERSION, now=1000
    )

    result = spracuj_udalost(
        con,
        payload=order_payload(attempt_id=attempt),
        now=1010,
        variant_id="67890",
    )

    assert result["akcia"] == AKCIA_UDELENE
    stored = con.execute(
        "SELECT status, provider_order_id FROM checkout_attempts WHERE public_id=?",
        (attempt,),
    ).fetchone()
    assert tuple(stored) == ("paid", "order-1")


def test_paid_order_cannot_claim_another_users_attempt(con):
    attempt = create_checkout_attempt(
        con, user_id=7, legal_version=LEGAL_VERSION, now=1000
    )

    with pytest.raises(UdalostNepouzitelna, match="pokus"):
        spracuj_udalost(
            con,
            payload=order_payload(user_id=8, attempt_id=attempt),
            now=1010,
            variant_id="67890",
        )

    assert con.execute("SELECT COUNT(*) FROM naroky").fetchone()[0] == 0


def test_one_attempt_cannot_authorize_two_different_orders(con):
    attempt = create_checkout_attempt(
        con, user_id=7, legal_version=LEGAL_VERSION, now=1000
    )
    spracuj_udalost(
        con,
        payload=order_payload(attempt_id=attempt),
        now=1010,
        variant_id="67890",
    )

    with pytest.raises(UdalostNepouzitelna, match="pokus"):
        spracuj_udalost(
            con,
            payload=order_payload(attempt_id=attempt, order_id="order-2"),
            now=1020,
            variant_id="67890",
        )

    assert con.execute("SELECT COUNT(*) FROM naroky").fetchone()[0] == 1


def test_reconciliation_payload_preserves_the_provider_checkout_attempt(con):
    attempt = create_checkout_attempt(
        con, user_id=7, legal_version=LEGAL_VERSION, now=1000
    )
    provider_order = {
        "id": "order-api-1",
        "attributes": {
            "total": 3900,
            "currency": "EUR",
            "status": "paid",
            "custom_data": {
                "user_id": "7",
                "checkout_attempt": attempt,
            },
            "first_order_item": {"variant_id": "67890"},
        },
    }

    payload = payload_z_objednavky(provider_order, user_id=7)

    assert payload["meta"]["custom_data"] == {
        "user_id": "7",
        "produkt": "zakladajuci_clen",
        "checkout_attempt": attempt,
    }
