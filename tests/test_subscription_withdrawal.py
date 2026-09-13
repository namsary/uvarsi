"""Annual subscription withdrawal classification and refund-preview contracts."""

from __future__ import annotations

import datetime
import inspect
import json
import sqlite3
import threading
from contextlib import closing
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from app import customer_requests, platby, predplatne
from app.operator_profile import LEGAL_VERSION
from test_platby import load_server, prihlaseny, vytvor_pouzivatela


DAY = 24 * 60 * 60
P0_START = datetime.datetime(2026, 9, 12, tzinfo=datetime.timezone.utc).timestamp()
P0_END = datetime.datetime(2027, 9, 12, tzinfo=datetime.timezone.utc).timestamp()
P1_END = datetime.datetime(2028, 9, 12, tzinfo=datetime.timezone.utc).timestamp()
_DEFAULT_CONTRACT_TIME = object()
VALID_CONSENT = {
    "accept_terms": True,
    "accept_automatic_renewal": True,
    "request_immediate_activation": True,
    "acknowledge_withdrawal_proration": True,
    "legal_version": LEGAL_VERSION,
}
BRATISLAVA = ZoneInfo("Europe/Bratislava")


def _local_timestamp(year, month, day, hour=0, minute=0, second=0, microsecond=0):
    return datetime.datetime(
        year,
        month,
        day,
        hour,
        minute,
        second,
        microsecond,
        tzinfo=BRATISLAVA,
    ).timestamp()


def test_statutory_deadline_uses_local_calendar_and_dst_weekend_extension():
    concluded = _local_timestamp(2026, 3, 15, 23, 30)

    deadline = customer_requests.statutory_withdrawal_deadline(concluded)

    assert deadline == _local_timestamp(2026, 3, 30, 23, 59, 59, 999_999)


def test_statutory_deadline_includes_the_whole_last_local_day():
    concluded = _local_timestamp(2026, 4, 16, 0, 1)

    deadline = customer_requests.statutory_withdrawal_deadline(concluded)

    assert deadline == _local_timestamp(2026, 4, 30, 23, 59, 59, 999_999)


def test_statutory_deadline_moves_past_christmas_and_weekend():
    concluded = _local_timestamp(2026, 12, 10, 12)

    deadline = customer_requests.statutory_withdrawal_deadline(concluded)

    assert deadline == _local_timestamp(2026, 12, 28, 23, 59, 59, 999_999)


def test_statutory_deadline_moves_past_good_friday_and_easter_monday():
    concluded = _local_timestamp(2026, 3, 20, 12)

    deadline = customer_requests.statutory_withdrawal_deadline(concluded)

    assert deadline == _local_timestamp(2026, 4, 7, 23, 59, 59, 999_999)


@pytest.mark.parametrize(
    ("concluded", "expected"),
    (
        (
            _local_timestamp(2026, 4, 24, 12),
            _local_timestamp(2026, 5, 8, 23, 59, 59, 999_999),
        ),
        (
            _local_timestamp(2026, 9, 1, 12),
            _local_timestamp(2026, 9, 15, 23, 59, 59, 999_999),
        ),
    ),
)
def test_2026_may_and_september_exceptions_remain_working_days(
    concluded, expected
):
    assert customer_requests.statutory_withdrawal_deadline(concluded) == expected


@pytest.mark.parametrize("value", (None, True, -1, float("inf"), "2026-01-01"))
def test_statutory_deadline_fails_closed_for_untrusted_contract_time(value):
    with pytest.raises(ValueError):
        customer_requests.statutory_withdrawal_deadline(value)


@pytest.mark.parametrize(
    ("amount_cents", "expected"),
    ((3_900, 3_825), (4_900, 4_806)),
)
def test_pro_rata_preview_uses_the_exact_paid_amount_and_period(
    amount_cents, expected
):
    assert predplatne.pro_rata_refund_preview(
        amount_cents=amount_cents,
        period_start=0,
        period_end=365 * DAY,
        withdrawn_at=7 * DAY,
    ) == expected


def test_pro_rata_preview_rounds_an_exact_half_cent_up():
    assert predplatne.pro_rata_refund_preview(
        amount_cents=1,
        period_start=0,
        period_end=2,
        withdrawn_at=1,
    ) == 1


@pytest.mark.parametrize(
    ("withdrawn_at", "expected"),
    ((-1, 3_900), (0, 3_900), (365 * DAY, 0), (400 * DAY, 0)),
)
def test_pro_rata_preview_is_bounded_at_period_edges(withdrawn_at, expected):
    assert predplatne.pro_rata_refund_preview(
        amount_cents=3_900,
        period_start=0,
        period_end=365 * DAY,
        withdrawn_at=withdrawn_at,
    ) == expected


def test_pro_rata_preview_uses_actual_leap_year_period_length():
    start = datetime.datetime(2027, 3, 1, tzinfo=datetime.timezone.utc).timestamp()
    end = datetime.datetime(2028, 3, 1, tzinfo=datetime.timezone.utc).timestamp()

    assert (end - start) / DAY == 366
    assert predplatne.pro_rata_refund_preview(
        amount_cents=4_900,
        period_start=start,
        period_end=end,
        withdrawn_at=start + 14 * DAY,
    ) == 4_713


@pytest.mark.parametrize(
    "values",
    (
        {"amount_cents": -1, "period_start": 0, "period_end": 1, "withdrawn_at": 0},
        {"amount_cents": True, "period_start": 0, "period_end": 1, "withdrawn_at": 0},
        {"amount_cents": 100, "period_start": 1, "period_end": 1, "withdrawn_at": 1},
        {"amount_cents": 100, "period_start": 2, "period_end": 1, "withdrawn_at": 1},
        {"amount_cents": 100, "period_start": 0, "period_end": float("inf"), "withdrawn_at": 1},
    ),
)
def test_pro_rata_preview_rejects_invalid_money_and_periods(values):
    with pytest.raises(ValueError):
        predplatne.pro_rata_refund_preview(**values)


@pytest.fixture
def subscription_db():
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.executescript(
        """
        CREATE TABLE pouzivatelia (
          id INTEGER PRIMARY KEY,
          email TEXT NOT NULL,
          platiaci INTEGER NOT NULL DEFAULT 0
        );
        INSERT INTO pouzivatelia (id,email) VALUES (1,'one@example.test');
        INSERT INTO pouzivatelia (id,email) VALUES (2,'two@example.test');
        """
    )
    platby.migrate_platby_schema(con)
    predplatne.migrate_subscription_schema(con)
    customer_requests.migrate_customer_requests_schema(con)
    _seed_subscription(con)
    try:
        yield con
    finally:
        con.close()


def _seed_subscription(
    con,
    *,
    user_id=1,
    consent=VALID_CONSENT,
    accepted_at=P0_START - 60,
    current_start=P0_START,
    current_end=P0_END,
    provider_order_id="ord_annual_1",
    provider_subscription_id="sub_annual_1",
):
    consent_json = consent if isinstance(consent, str) else json.dumps(consent)
    con.execute(
        """INSERT INTO checkout_attempts
           (public_id,user_id,product,amount_cents,renewal_amount_cents,
            currency,billing_interval,auto_renews,founder,discount_id,
            discount_code,legal_version,privacy_version,consent_json,
            accepted_at,expires_at,founder_reserved_until,test_mode,status,
            provider_checkout_id,provider_order_id)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            f"attempt_{user_id}_" + "a" * 40,
            user_id,
            "premium_annual",
            3_900,
            4_900,
            "EUR",
            "year",
            1,
            1,
            "discount_founders",
            "FOUNDERS",
            LEGAL_VERSION,
            LEGAL_VERSION,
            consent_json,
            accepted_at,
            P0_START + DAY,
            P0_START + DAY,
            1,
            "paid",
            "checkout_annual_1",
            provider_order_id,
        ),
    )
    con.execute(
        """INSERT INTO subscriptions
           (user_id,product,provider,provider_customer_id,provider_order_id,
            provider_subscription_id,provider_variant_id,currency,test_mode,
            status,period_start,period_end,renews_at,ends_at,paid_through,
            initial_amount_cents,renewal_amount_cents,discount_id,founder,
            initial_payment_verified,needs_review,review_reason,
            last_verified_event_at,provider_updated_at,created_at,updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            user_id,
            "premium_annual",
            "lemonsqueezy",
            "customer_annual_1",
            provider_order_id,
            provider_subscription_id,
            "variant_annual",
            "EUR",
            1,
            "active",
            current_start,
            current_end,
            current_end,
            None,
            current_end,
            3_900,
            4_900,
            "discount_founders",
            1,
            1,
            0,
            None,
            P0_START,
            P0_START,
            P0_START,
            P0_START,
        ),
    )
    con.commit()


def _seed_invoice(
    con,
    *,
    invoice_id="inv_initial",
    invoice_kind="initial",
    amount_cents=3_900,
    period_start=P0_START,
    period_end=P0_END,
    paid_at=P0_START,
    refunded_amount_cents=0,
    status="paid",
    contract_concluded_at=_DEFAULT_CONTRACT_TIME,
):
    if contract_concluded_at is _DEFAULT_CONTRACT_TIME:
        contract_concluded_at = P0_START if invoice_kind == "initial" else None
    con.execute(
        """INSERT INTO subscription_invoices
           (provider,test_mode,provider_invoice_id,provider_subscription_id,
            provider_order_id,invoice_kind,status,amount_cents,currency,
            period_start,period_end,paid_at,contract_concluded_at,
            refunded_amount_cents,created_at,updated_at)
           VALUES ('lemonsqueezy',1,?,'sub_annual_1','ord_annual_1',?,?,?,'EUR',?,?,?,?,?,?,?)""",
        (
            invoice_id,
            invoice_kind,
            status,
            amount_cents,
            period_start,
            period_end,
            paid_at,
            contract_concluded_at,
            refunded_amount_cents,
            paid_at,
            paid_at,
        ),
    )
    con.commit()


def test_subscription_withdrawal_interface_matches_the_approved_plan():
    assert str(inspect.signature(customer_requests.create_subscription_withdrawal)) == (
        "(con, *, user_id, invoice_id, message, now) -> 'ConsumerRequest'"
    )


def test_initial_weekend_extended_deadline_is_statutory_through_end_of_day(
    subscription_db,
):
    _seed_invoice(subscription_db)

    request = customer_requests.create_subscription_withdrawal(
        subscription_db,
        user_id=1,
        invoice_id="inv_initial",
        message="Odstupujem od zmluvy.",
        now=_local_timestamp(2026, 9, 28, 23, 59, 59),
    )

    assert request.refund_scope == customer_requests.REFUND_STATUTORY_REVIEW
    assert request.status == customer_requests.STATUS_REQUIRES_REVIEW
    assert request.refund_preview_cents == 3_719
    assert request.consumed_charge_preview_cents == 181
    assert request.consent_valid_for_proration is True
    assert request.invoice_kind == "initial"
    assert request.contract_concluded_at == P0_START
    assert request.statutory_deadline_at == _local_timestamp(
        2026, 9, 28, 23, 59, 59, 999_999
    )


def test_after_initial_calendar_deadline_is_unexecuted_period_end_cancellation(
    subscription_db,
):
    _seed_invoice(subscription_db)

    request = customer_requests.create_subscription_withdrawal(
        subscription_db,
        user_id=1,
        invoice_id="inv_initial",
        message="Končím.",
        now=_local_timestamp(2026, 9, 29),
    )

    assert request.refund_scope == customer_requests.REFUND_CANCEL_AT_PERIOD_END
    assert request.refund_preview_cents == 0
    assert request.consumed_charge_preview_cents is None
    assert request.status == customer_requests.STATUS_REQUIRES_REVIEW


@pytest.mark.parametrize(
    "consent",
    (
        None,
        "not-json",
        {**VALID_CONSENT, "request_immediate_activation": False},
        {**VALID_CONSENT, "acknowledge_withdrawal_proration": False},
        {**VALID_CONSENT, "legal_version": "stale-version"},
    ),
)
def test_missing_or_invalid_consent_never_charges_consumed_service(consent):
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.executescript(
        "CREATE TABLE pouzivatelia (id INTEGER PRIMARY KEY,email TEXT);"
        "INSERT INTO pouzivatelia VALUES (1,'private@example.test');"
    )
    platby.migrate_platby_schema(con)
    predplatne.migrate_subscription_schema(con)
    customer_requests.migrate_customer_requests_schema(con)
    _seed_subscription(con, consent=consent)
    _seed_invoice(con)

    request = customer_requests.create_subscription_withdrawal(
        con,
        user_id=1,
        invoice_id="inv_initial",
        message="",
        now=P0_START + 7 * DAY,
    )

    assert request.refund_scope == customer_requests.REFUND_STATUTORY_REVIEW
    assert request.refund_preview_cents == 3_900
    assert request.consumed_charge_preview_cents == 0
    assert request.consent_valid_for_proration is False
    con.close()


def test_renewal_change_of_mind_never_opens_a_new_statutory_window(subscription_db):
    subscription_db.execute(
        """UPDATE subscriptions
              SET period_start=?,period_end=?,renews_at=?,paid_through=?
            WHERE user_id=1""",
        (P0_END, P1_END, P1_END, P1_END),
    )
    _seed_invoice(subscription_db)
    _seed_invoice(
        subscription_db,
        invoice_id="inv_renewal",
        invoice_kind="renewal",
        amount_cents=4_900,
        period_start=P0_END,
        period_end=P1_END,
        paid_at=P0_END,
    )

    request = customer_requests.create_subscription_withdrawal(
        subscription_db,
        user_id=1,
        invoice_id="inv_renewal",
        message="Odstupujem.",
        now=P0_END + 7 * DAY,
    )

    assert request.invoice_id == "inv_renewal"
    assert request.invoice_amount_cents == 4_900
    assert request.period_start == P0_END
    assert request.period_end == P1_END
    assert request.refund_scope == customer_requests.REFUND_CANCEL_AT_PERIOD_END
    assert request.refund_preview_cents == 0
    assert request.consumed_charge_preview_cents is None


def test_renewal_defect_remains_a_separate_remedy_review(subscription_db):
    subscription_db.execute(
        "UPDATE subscriptions SET period_start=?,period_end=?,renews_at=?,paid_through=?",
        (P0_END, P1_END, P1_END, P1_END),
    )
    _seed_invoice(
        subscription_db,
        invoice_id="inv_renewal",
        invoice_kind="renewal",
        amount_cents=4_900,
        period_start=P0_END,
        period_end=P1_END,
        paid_at=P0_END,
    )

    request = customer_requests.create_subscription_remedy(
        subscription_db,
        user_id=1,
        invoice_id="inv_renewal",
        remedy_type=customer_requests.REMEDY_DEFECT,
        message="Obnovená služba má vadu.",
        now=P0_END + 30 * DAY,
    )

    assert request.request_classification == customer_requests.CLASSIFICATION_REMEDY_REVIEW
    assert request.refund_scope is None
    assert request.refund_preview_cents is None


def test_initial_invoice_uses_contract_date_instead_of_payment_receipt_time(
    subscription_db,
):
    contract_time = _local_timestamp(2026, 9, 1, 12)
    paid_at = _local_timestamp(2026, 9, 10, 12)
    subscription_db.execute(
        "UPDATE checkout_attempts SET accepted_at=?", (contract_time - 60,)
    )
    _seed_invoice(
        subscription_db,
        paid_at=paid_at,
        contract_concluded_at=contract_time,
    )

    request = customer_requests.create_subscription_withdrawal(
        subscription_db,
        user_id=1,
        invoice_id="inv_initial",
        message="",
        now=_local_timestamp(2026, 9, 16),
    )

    assert request.refund_scope == customer_requests.REFUND_CANCEL_AT_PERIOD_END
    assert request.refund_preview_cents == 0


@pytest.mark.parametrize(
    "contract_time",
    (None, float("nan"), P0_START - 120, P0_START + DAY),
)
def test_untrusted_initial_contract_time_fails_closed_to_manual_legal_review(
    subscription_db, contract_time
):
    _seed_invoice(subscription_db, contract_concluded_at=contract_time)

    request = customer_requests.create_subscription_withdrawal(
        subscription_db,
        user_id=1,
        invoice_id="inv_initial",
        message="",
        now=P0_START + DAY,
    )

    assert request.refund_scope == "manual_legal_review"
    assert request.request_classification == "manual_legal_review"
    assert request.refund_preview_cents is None
    assert request.consumed_charge_preview_cents is None


def test_prior_partial_refund_is_subtracted_from_the_exact_invoice_preview(
    subscription_db,
):
    _seed_invoice(
        subscription_db,
        refunded_amount_cents=1_000,
        status="partial_refund",
    )

    request = customer_requests.create_subscription_withdrawal(
        subscription_db,
        user_id=1,
        invoice_id="inv_initial",
        message="",
        now=P0_START + 7 * DAY,
    )

    assert request.refund_preview_cents == 2_825
    assert request.consumed_charge_preview_cents == 75


def test_foreign_or_unverified_invoice_cannot_create_a_request(subscription_db):
    _seed_invoice(subscription_db)

    with pytest.raises(customer_requests.RequestNotAllowed):
        customer_requests.create_subscription_withdrawal(
            subscription_db,
            user_id=2,
            invoice_id="inv_initial",
            message="",
            now=P0_START + DAY,
        )

    with pytest.raises(customer_requests.RequestNotAllowed):
        customer_requests.create_subscription_withdrawal(
            subscription_db,
            user_id=1,
            invoice_id="inv_missing",
            message="",
            now=P0_START + DAY,
        )
    assert subscription_db.execute(
        "SELECT COUNT(*) FROM consumer_requests"
    ).fetchone()[0] == 0


@pytest.mark.parametrize(
    ("column", "value"),
    (("initial_payment_verified", 0), ("product", "other_product")),
)
def test_invoice_must_belong_to_a_verified_annual_subscription(
    subscription_db, column, value
):
    _seed_invoice(subscription_db)
    subscription_db.execute(f"UPDATE subscriptions SET {column}=?", (value,))
    subscription_db.commit()

    with pytest.raises(customer_requests.RequestNotAllowed):
        customer_requests.create_subscription_withdrawal(
            subscription_db,
            user_id=1,
            invoice_id="inv_initial",
            message="",
            now=P0_START + DAY,
        )

    assert subscription_db.execute(
        "SELECT COUNT(*) FROM consumer_requests"
    ).fetchone()[0] == 0


def test_request_snapshots_minimal_invoice_and_consent_facts_without_mutation(
    subscription_db,
):
    subscription_db.execute(
        "UPDATE checkout_attempts SET consent_json=?",
        (json.dumps({**VALID_CONSENT, "private_note": "do not retain"}),),
    )
    _seed_invoice(subscription_db)
    subscription_before = tuple(
        subscription_db.execute("SELECT * FROM subscriptions").fetchone()
    )
    invoices_before = [
        tuple(row)
        for row in subscription_db.execute(
            "SELECT * FROM subscription_invoices ORDER BY id"
        )
    ]

    request = customer_requests.create_subscription_withdrawal(
        subscription_db,
        user_id=1,
        invoice_id="inv_initial",
        message="",
        now=P0_START + DAY,
    )
    row = subscription_db.execute(
        "SELECT * FROM consumer_requests WHERE public_id=?", (request.public_id,)
    ).fetchone()
    stored = json.dumps(dict(row), ensure_ascii=False)

    assert row["invoice_id"] == "inv_initial"
    assert row["subscription_id"] == "sub_annual_1"
    assert row["purchased_at"] == P0_START
    assert row["period_start"] == P0_START
    assert row["period_end"] == P0_END
    assert row["consent_accepted_at"] == P0_START - 60
    assert "private_note" not in row["consent_snapshot"]
    assert "one@example.test" not in stored
    assert "customer_annual_1" not in stored
    assert tuple(subscription_db.execute("SELECT * FROM subscriptions").fetchone()) == subscription_before
    assert [
        tuple(item)
        for item in subscription_db.execute(
            "SELECT * FROM subscription_invoices ORDER BY id"
        )
    ] == invoices_before


def test_repeat_submission_returns_the_original_immutable_request(subscription_db):
    _seed_invoice(subscription_db)
    first = customer_requests.create_subscription_withdrawal(
        subscription_db,
        user_id=1,
        invoice_id="inv_initial",
        message="Prvé podanie.",
        now=P0_START + DAY,
    )
    subscription_db.execute(
        "UPDATE checkout_attempts SET consent_json=NULL WHERE user_id=1"
    )
    subscription_db.commit()

    replay = customer_requests.create_subscription_withdrawal(
        subscription_db,
        user_id=1,
        invoice_id="inv_initial",
        message="Zmenený text.",
        now=P0_START + 2 * DAY,
    )

    assert replay.public_id == first.public_id
    assert replay.created is False
    assert replay.message == "Prvé podanie."
    assert replay.consent_valid_for_proration is True
    assert subscription_db.execute(
        "SELECT COUNT(*) FROM consumer_requests"
    ).fetchone()[0] == 1


def test_resolved_remedy_replay_returns_the_original_closed_request(subscription_db):
    _seed_invoice(subscription_db)
    first = customer_requests.create_subscription_remedy(
        subscription_db,
        user_id=1,
        invoice_id="inv_initial",
        remedy_type=customer_requests.REMEDY_DEFECT,
        message="Pôvodná vada.",
        now=P0_START + DAY,
    )
    subscription_db.execute(
        "UPDATE consumer_requests SET status=? WHERE public_id=?",
        (customer_requests.STATUS_RESOLVED, first.public_id),
    )
    subscription_db.commit()

    replay = customer_requests.create_subscription_remedy(
        subscription_db,
        user_id=1,
        invoice_id="inv_initial",
        remedy_type=customer_requests.REMEDY_DEFECT,
        message="Opakované podanie.",
        now=P0_START + 2 * DAY,
    )

    assert replay.public_id == first.public_id
    assert replay.status == customer_requests.STATUS_RESOLVED
    assert replay.message == "Pôvodná vada."
    assert replay.created is False
    assert subscription_db.execute(
        "SELECT COUNT(*) FROM consumer_requests"
    ).fetchone()[0] == 1


def test_concurrent_replays_after_resolved_return_one_closed_request(tmp_path):
    database_path = tmp_path / "resolved-subscription-request.db"
    with closing(sqlite3.connect(database_path)) as con:
        con.row_factory = sqlite3.Row
        con.executescript(
            "CREATE TABLE pouzivatelia (id INTEGER PRIMARY KEY,email TEXT);"
            "INSERT INTO pouzivatelia VALUES (1,'one@example.test');"
        )
        platby.migrate_platby_schema(con)
        predplatne.migrate_subscription_schema(con)
        customer_requests.migrate_customer_requests_schema(con)
        _seed_subscription(con)
        _seed_invoice(con)
        first = customer_requests.create_subscription_remedy(
            con,
            user_id=1,
            invoice_id="inv_initial",
            remedy_type=customer_requests.REMEDY_DEFECT,
            message="Pôvodná vada.",
            now=P0_START + DAY,
        )
        con.execute(
            "UPDATE consumer_requests SET status=? WHERE public_id=?",
            (customer_requests.STATUS_RESOLVED, first.public_id),
        )
        con.commit()

    barrier = threading.Barrier(2)
    results = []
    errors = []

    def replay():
        try:
            with closing(sqlite3.connect(database_path, timeout=5)) as con:
                con.row_factory = sqlite3.Row
                barrier.wait(timeout=5)
                results.append(
                    customer_requests.create_subscription_remedy(
                        con,
                        user_id=1,
                        invoice_id="inv_initial",
                        remedy_type=customer_requests.REMEDY_DEFECT,
                        message="Opakované podanie.",
                        now=P0_START + 2 * DAY,
                    )
                )
        except BaseException as error:
            errors.append(error)

    workers = [threading.Thread(target=replay) for _ in range(2)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=10)

    assert errors == []
    assert all(not worker.is_alive() for worker in workers)
    assert len(results) == 2
    assert {request.public_id for request in results} == {first.public_id}
    assert {request.status for request in results} == {
        customer_requests.STATUS_RESOLVED
    }
    assert all(request.created is False for request in results)
    with closing(sqlite3.connect(database_path)) as con:
        assert con.execute("SELECT COUNT(*) FROM consumer_requests").fetchone()[0] == 1


@pytest.mark.parametrize(
    "remedy_type",
    (
        "defect",
        "nonconformity",
        "unavailable_service",
        "duplicate_charge",
        "unauthorized_charge",
    ),
)
def test_separate_remedies_are_not_reclassified_as_late_change_of_mind(
    subscription_db, remedy_type
):
    _seed_invoice(subscription_db)

    request = customer_requests.create_subscription_remedy(
        subscription_db,
        user_id=1,
        invoice_id="inv_initial",
        remedy_type=remedy_type,
        message="Žiadam kontrolu.",
        now=P0_START + 100 * DAY,
    )

    assert request.request_type == customer_requests.TYPE_COMPLAINT
    assert request.request_classification == customer_requests.CLASSIFICATION_REMEDY_REVIEW
    assert request.remedy_type == remedy_type
    assert request.refund_scope is None
    assert request.refund_preview_cents is None


def test_concurrent_replays_create_one_exact_invoice_request(tmp_path):
    database_path = tmp_path / "subscription-withdrawal.db"
    with closing(sqlite3.connect(database_path)) as con:
        con.row_factory = sqlite3.Row
        con.executescript(
            "CREATE TABLE pouzivatelia (id INTEGER PRIMARY KEY,email TEXT);"
            "INSERT INTO pouzivatelia VALUES (1,'one@example.test');"
        )
        platby.migrate_platby_schema(con)
        predplatne.migrate_subscription_schema(con)
        customer_requests.migrate_customer_requests_schema(con)
        _seed_subscription(con)
        _seed_invoice(con)

    barrier = threading.Barrier(2)
    results = []
    errors = []

    def submit():
        try:
            with closing(sqlite3.connect(database_path, timeout=5)) as con:
                con.row_factory = sqlite3.Row
                barrier.wait(timeout=5)
                results.append(
                    customer_requests.create_subscription_withdrawal(
                        con,
                        user_id=1,
                        invoice_id="inv_initial",
                        message="Súbežné podanie.",
                        now=P0_START + DAY,
                    )
                )
        except BaseException as error:
            errors.append(error)

    workers = [threading.Thread(target=submit) for _ in range(2)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=10)

    assert errors == []
    assert all(not worker.is_alive() for worker in workers)
    assert len(results) == 2
    assert len({request.public_id for request in results}) == 1
    assert sorted(request.created for request in results) == [False, True]
    with closing(sqlite3.connect(database_path)) as con:
        assert con.execute("SELECT COUNT(*) FROM consumer_requests").fetchone()[0] == 1


def test_owner_can_list_historical_annual_invoices_after_subscription_expiry(
    subscription_db,
):
    _seed_invoice(subscription_db)
    subscription_db.execute(
        "UPDATE subscriptions SET status='expired',renews_at=NULL,ends_at=?",
        (P0_END,),
    )
    subscription_db.commit()

    invoices = customer_requests.subscription_invoices_for_user(
        subscription_db, user_id=1
    )

    assert invoices == [
        {
            "invoice_id": "inv_initial",
            "invoice_kind": "initial",
            "status": "paid",
            "amount_cents": 3_900,
            "refunded_amount_cents": 0,
            "currency": "EUR",
            "period_start": P0_START,
            "period_end": P0_END,
            "paid_at": P0_START,
        }
    ]
    assert customer_requests.subscription_invoices_for_user(
        subscription_db, user_id=2
    ) == []
    encoded = json.dumps(invoices)
    assert "sub_annual_1" not in encoded
    assert "ord_annual_1" not in encoded
    assert "customer_annual_1" not in encoded


def _subscription_server(
    monkeypatch,
    tmp_path,
    *,
    now=P0_START + DAY,
    current_start=P0_START,
    current_end=P0_END,
):
    server = load_server(
        monkeypatch,
        tmp_path,
        PLATBY_ZAPNUTE="0",
        UVARSI_RECIPE_ENGINE_V2="0",
    )
    monkeypatch.setattr(server, "AUTH_CLOCK", lambda: now)
    vytvor_pouzivatela(server, user_id=1, email="annual@example.test")
    with closing(server.db()) as con:
        _seed_subscription(
            con,
            current_start=current_start,
            current_end=current_end,
        )
    monkeypatch.setattr(
        server, "posli_mail", lambda *_args, **_kwargs: None
    )
    return server


def test_subscription_withdrawal_route_requires_authentication_and_origin(
    monkeypatch, tmp_path
):
    server = _subscription_server(monkeypatch, tmp_path)
    with closing(server.db()) as con:
        _seed_invoice(con)

    unauthenticated = TestClient(
        server.app, raise_server_exceptions=False
    ).post(
        "/api/consumer/withdrawal",
        json={"invoice_id": "inv_initial", "message": "Končím."},
        headers={"Origin": "https://uvar.si"},
    )
    missing_origin = prihlaseny(server).post(
        "/api/consumer/withdrawal",
        json={"invoice_id": "inv_initial", "message": "Končím."},
    )

    assert unauthenticated.status_code == 401
    assert missing_origin.status_code == 403


def test_flags_off_route_records_statutory_preview_without_provider_or_ai(
    monkeypatch, tmp_path
):
    server = _subscription_server(monkeypatch, tmp_path, now=P0_START + 7 * DAY)
    with closing(server.db()) as con:
        _seed_invoice(con)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("withdrawal preview must stay local")

    monkeypatch.setattr(server, "_subscription_portal_provider", forbidden)
    monkeypatch.setattr(server, "_subscription_checkout_provider", forbidden)
    monkeypatch.setattr(server, "_open_authenticated_lemon_request", forbidden)
    monkeypatch.setattr(server, "_generuj_plan_sync", forbidden)

    response = prihlaseny(server).post(
        "/api/consumer/withdrawal",
        json={"invoice_id": "inv_initial", "message": "Odstupujem."},
        headers={"Origin": "https://uvar.si"},
    )

    assert response.status_code == 202
    body = response.json()
    assert body["request_type"] == customer_requests.TYPE_WITHDRAWAL
    assert body["refund_scope"] == customer_requests.REFUND_STATUTORY_REVIEW
    assert body["refund_preview_cents"] == 3_825
    assert body["refund_executed"] is False
    assert body["subscription_changed"] is False
    assert body["provider_action_required"] is False
    assert body["invoice_id"] == "inv_initial"
    assert "consent" not in json.dumps(body).casefold()
    assert "customer_annual_1" not in json.dumps(body)


def test_route_selects_the_current_renewal_invoice_when_client_sends_no_id(
    monkeypatch, tmp_path
):
    server = _subscription_server(
        monkeypatch,
        tmp_path,
        now=P0_END + 7 * DAY,
        current_start=P0_END,
        current_end=P1_END,
    )
    with closing(server.db()) as con:
        _seed_invoice(con)
        _seed_invoice(
            con,
            invoice_id="inv_renewal",
            invoice_kind="renewal",
            amount_cents=4_900,
            period_start=P0_END,
            period_end=P1_END,
            paid_at=P0_END,
        )

    response = prihlaseny(server).post(
        "/api/consumer/withdrawal",
        json={"message": "Odstupujem."},
        headers={"Origin": "https://uvar.si"},
    )

    assert response.status_code == 202
    assert response.json()["invoice_id"] == "inv_renewal"
    assert response.json()["refund_scope"] == customer_requests.REFUND_CANCEL_AT_PERIOD_END
    assert response.json()["refund_preview_cents"] == 0
    with closing(server.db()) as con:
        row = con.execute(
            "SELECT invoice_id,period_start,period_end FROM consumer_requests"
        ).fetchone()
    assert tuple(row) == ("inv_renewal", P0_END, P1_END)


def test_late_change_of_mind_records_action_needed_without_claiming_cancellation(
    monkeypatch, tmp_path
):
    server = _subscription_server(monkeypatch, tmp_path, now=P0_START + 17 * DAY)
    with closing(server.db()) as con:
        _seed_invoice(con)
        before = tuple(con.execute("SELECT * FROM subscriptions").fetchone())

    response = prihlaseny(server).post(
        "/api/consumer/withdrawal",
        json={"invoice_id": "inv_initial", "message": "Končím."},
        headers={"Origin": "https://uvar.si"},
    )

    assert response.status_code == 202
    body = response.json()
    assert body["refund_scope"] == customer_requests.REFUND_CANCEL_AT_PERIOD_END
    assert body["refund_preview_cents"] == 0
    assert body["provider_action_required"] is True
    assert body["subscription_changed"] is False
    assert body["cancellation_state"] == "action_required"
    assert "zrušen" not in body["message"].casefold()
    with closing(server.db()) as con:
        assert tuple(con.execute("SELECT * FROM subscriptions").fetchone()) == before


@pytest.mark.parametrize(
    "reason",
    (
        "defect",
        "nonconformity",
        "unavailable_service",
        "duplicate_charge",
        "unauthorized_charge",
    ),
)
def test_route_keeps_explicit_remedies_separate_even_after_fourteen_days(
    monkeypatch, tmp_path, reason
):
    server = _subscription_server(monkeypatch, tmp_path, now=P0_START + 100 * DAY)
    with closing(server.db()) as con:
        _seed_invoice(con)

    response = prihlaseny(server).post(
        "/api/consumer/withdrawal",
        json={
            "invoice_id": "inv_initial",
            "reason": reason,
            "message": "Žiadam samostatné posúdenie.",
        },
        headers={"Origin": "https://uvar.si"},
    )

    assert response.status_code == 202
    body = response.json()
    assert body["request_type"] == customer_requests.TYPE_COMPLAINT
    assert body["request_classification"] == customer_requests.CLASSIFICATION_REMEDY_REVIEW
    assert body["remedy_type"] == reason
    assert body["refund_scope"] is None
    assert body["cancellation_state"] == "not_requested"


def test_annual_complaint_route_defaults_to_defect_review(monkeypatch, tmp_path):
    server = _subscription_server(monkeypatch, tmp_path, now=P0_START + 100 * DAY)
    with closing(server.db()) as con:
        _seed_invoice(con)

    response = prihlaseny(server).post(
        "/api/consumer/complaint",
        json={
            "invoice_id": "inv_initial",
            "message": "Služba nefunguje podľa zmluvy.",
        },
        headers={"Origin": "https://uvar.si"},
    )

    assert response.status_code == 202
    assert response.json()["remedy_type"] == customer_requests.REMEDY_DEFECT
    assert response.json()["refund_scope"] is None


def test_foreign_invoice_route_is_generic_and_creates_nothing(monkeypatch, tmp_path):
    server = _subscription_server(monkeypatch, tmp_path)
    with closing(server.db()) as con:
        _seed_invoice(con)

    response = prihlaseny(server).post(
        "/api/consumer/withdrawal",
        json={"invoice_id": "inv_foreign", "message": "Končím."},
        headers={"Origin": "https://uvar.si"},
    )

    assert response.status_code == 404
    body = response.json()
    assert body["ok"] is False
    assert body["request_received"] is False
    assert body["invoice_id"] is None
    assert "inv_foreign" not in json.dumps(body)
    with closing(server.db()) as con:
        assert con.execute("SELECT COUNT(*) FROM consumer_requests").fetchone()[0] == 0


def test_unselected_payment_never_returns_false_accepted_response(monkeypatch, tmp_path):
    server = _subscription_server(monkeypatch, tmp_path)

    response = prihlaseny(server).post(
        "/api/consumer/withdrawal",
        json={"message": "Končím."},
        headers={"Origin": "https://uvar.si"},
    )

    assert response.status_code == 422
    assert "vyber" in response.json()["detail"].casefold()
    with closing(server.db()) as con:
        assert con.execute("SELECT COUNT(*) FROM consumer_requests").fetchone()[0] == 0


def test_expired_owner_lists_and_submits_remedy_for_historical_invoice(
    monkeypatch, tmp_path
):
    server = _subscription_server(monkeypatch, tmp_path, now=P0_END + 30 * DAY)
    with closing(server.db()) as con:
        _seed_invoice(con)
        con.execute(
            "UPDATE subscriptions SET status='expired',renews_at=NULL,ends_at=?",
            (P0_END,),
        )
        con.commit()
    client = prihlaseny(server)

    listing = client.get("/api/consumer/requests")
    response = client.post(
        "/api/consumer/complaint",
        json={
            "invoice_id": "inv_initial",
            "reason": "unavailable_service",
            "message": "Služba bola počas zaplateného obdobia nedostupná.",
        },
        headers={"Origin": "https://uvar.si"},
    )

    assert listing.status_code == 200
    assert listing.json()["invoices"][0]["invoice_id"] == "inv_initial"
    assert response.status_code == 202
    assert response.json()["request_received"] is True
    assert response.json()["remedy_type"] == "unavailable_service"
    with closing(server.db()) as con:
        stored = con.execute(
            "SELECT invoice_id,remedy_type FROM consumer_requests"
        ).fetchone()
    assert tuple(stored) == ("inv_initial", "unavailable_service")


def test_route_replay_returns_same_request_without_reapplying_anything(
    monkeypatch, tmp_path
):
    server = _subscription_server(monkeypatch, tmp_path)
    with closing(server.db()) as con:
        _seed_invoice(con)
    client = prihlaseny(server)
    request = {
        "invoice_id": "inv_initial",
        "message": "Odstupujem.",
    }
    headers = {"Origin": "https://uvar.si"}

    first = client.post("/api/consumer/withdrawal", json=request, headers=headers)
    second = client.post("/api/consumer/withdrawal", json=request, headers=headers)

    assert first.status_code == second.status_code == 202
    assert first.json()["request_id"] == second.json()["request_id"]
    with closing(server.db()) as con:
        assert con.execute("SELECT COUNT(*) FROM consumer_requests").fetchone()[0] == 1
        assert con.execute(
            "SELECT COUNT(*) FROM subscription_events"
        ).fetchone()[0] == 0


@pytest.mark.parametrize("now", (P0_START + DAY, P0_START + 100 * DAY))
def test_legacy_initial_invoice_uses_manual_legal_review_at_any_possible_age(
    monkeypatch, tmp_path, now
):
    server = _subscription_server(monkeypatch, tmp_path, now=now)
    with closing(server.db()) as con:
        _seed_invoice(con, contract_concluded_at=None)
    sent = []
    monkeypatch.setattr(
        server,
        "posli_mail",
        lambda komu, predmet, telo, html, **kw: sent.append(telo),
    )

    def forbidden(*_args, **_kwargs):
        raise AssertionError("manual legal review must stay local")

    monkeypatch.setattr(server, "_subscription_portal_provider", forbidden)
    monkeypatch.setattr(server, "_subscription_checkout_provider", forbidden)
    monkeypatch.setattr(server, "_open_authenticated_lemon_request", forbidden)

    response = prihlaseny(server).post(
        "/api/consumer/withdrawal",
        json={"invoice_id": "inv_initial", "message": "Odstupujem."},
        headers={"Origin": "https://uvar.si"},
    )

    assert response.status_code == 202
    body = response.json()
    assert body["request_classification"] == "manual_legal_review"
    assert body["refund_scope"] == "manual_legal_review"
    assert body["refund_preview_cents"] is None
    assert body["provider_action_required"] is False
    assert body["refund_executed"] is False
    assert body["cancellation_state"] == "not_requested"
    assert len(sent) == 1
    assert (
        "dátum uzavretia zmluvy sa nedá automaticky overiť"
        in sent[0].casefold()
    )
    assert "netvrdíme, že 14-dňová lehota uplynula" in sent[0]
    assert "lehota už uplynula" not in sent[0]
    assert "Po 14-dňovej lehote" not in sent[0]
    assert "Zrušenie treba dokončiť" not in sent[0]


def test_resolved_remedy_replay_returns_closed_status_without_new_receipt_claim(
    monkeypatch, tmp_path
):
    server = _subscription_server(monkeypatch, tmp_path)
    with closing(server.db()) as con:
        _seed_invoice(con)
        original = customer_requests.create_subscription_remedy(
            con,
            user_id=1,
            invoice_id="inv_initial",
            remedy_type=customer_requests.REMEDY_DEFECT,
            message="Pôvodná reklamácia.",
            now=P0_START + DAY,
        )
        con.execute(
            """UPDATE consumer_requests
                  SET status=?,confirmation_state='sent',confirmation_sent_at=?
                WHERE public_id=?""",
            (customer_requests.STATUS_RESOLVED, P0_START + DAY, original.public_id),
        )
        con.commit()

    response = prihlaseny(server).post(
        "/api/consumer/complaint",
        json={
            "invoice_id": "inv_initial",
            "reason": customer_requests.REMEDY_DEFECT,
            "message": "Opakované podanie.",
        },
        headers={"Origin": "https://uvar.si"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["created"] is False
    assert body["request_received"] is False
    assert body["request_status"] == customer_requests.STATUS_RESOLVED
    assert body["request_id"] == original.public_id
    assert "už uzavretú žiadosť" in body["message"]
    assert "žiadosť sme prijali" not in body["message"].casefold()
    assert "customer_annual_1" not in json.dumps(body)
    with closing(server.db()) as con:
        assert con.execute("SELECT COUNT(*) FROM consumer_requests").fetchone()[0] == 1


def test_late_change_of_mind_receipt_says_provider_action_is_still_required(
    monkeypatch, tmp_path
):
    server = _subscription_server(monkeypatch, tmp_path, now=P0_START + 17 * DAY)
    with closing(server.db()) as con:
        _seed_invoice(con)
    sent = []
    monkeypatch.setattr(
        server,
        "posli_mail",
        lambda komu, predmet, telo, html, **kw: sent.append(
            (komu, predmet, telo, html)
        ),
    )

    response = prihlaseny(server).post(
        "/api/consumer/withdrawal",
        json={"invoice_id": "inv_initial", "message": "Končím."},
        headers={"Origin": "https://uvar.si"},
    )

    assert response.status_code == 202
    assert response.json()["confirmation"] == {
        "state": "sent",
        "sent": True,
        "pending": False,
    }
    assert len(sent) == 1
    receipt = sent[0][2]
    assert "Faktúra: inv_initial" in receipt
    assert "ešte nezrušili ani nezmenili" in receipt
    assert "Customer Portal" in receipt
    assert "do konca zaplateného obdobia" in receipt


@pytest.mark.parametrize(
    ("status", "now", "expected_state", "expected_text"),
    (
        (
            "cancelled",
            P0_START + 17 * DAY,
            "already_cancelled",
            "Predplatné už bolo zrušené",
        ),
        (
            "expired",
            P0_END + 30 * DAY,
            "already_expired",
            "Predplatné už skončilo",
        ),
    ),
)
def test_late_request_uses_verified_terminal_subscription_state_and_end_date(
    monkeypatch, tmp_path, status, now, expected_state, expected_text
):
    server = _subscription_server(monkeypatch, tmp_path, now=now)
    with closing(server.db()) as con:
        _seed_invoice(con)
        con.execute(
            "UPDATE subscriptions SET status=?,renews_at=NULL,ends_at=?",
            (status, P0_END),
        )
        con.commit()
    sent = []
    monkeypatch.setattr(
        server,
        "posli_mail",
        lambda komu, predmet, telo, html, **kw: sent.append(telo),
    )

    response = prihlaseny(server).post(
        "/api/consumer/withdrawal",
        json={"invoice_id": "inv_initial", "message": "Končím."},
        headers={"Origin": "https://uvar.si"},
    )

    assert response.status_code == 202
    body = response.json()
    assert body["refund_scope"] == customer_requests.REFUND_CANCEL_AT_PERIOD_END
    assert body["cancellation_state"] == expected_state
    assert body["provider_action_required"] is False
    assert body["subscription_status"] == status
    assert body["subscription_ends_at"] == P0_END
    assert len(sent) == 1
    assert expected_text in sent[0]
    assert "12. 09. 2027" in sent[0]
    assert "ešte nezrušili ani nezmenili" not in sent[0]
    assert "Zrušenie treba dokončiť" not in sent[0]


def test_subscription_status_snapshot_is_immutable_and_pii_minimal(subscription_db):
    _seed_invoice(subscription_db)
    subscription_db.execute(
        "UPDATE subscriptions SET status='cancelled',renews_at=NULL,ends_at=?",
        (P0_END,),
    )
    subscription_db.commit()

    first = customer_requests.create_subscription_withdrawal(
        subscription_db,
        user_id=1,
        invoice_id="inv_initial",
        message="Končím.",
        now=P0_START + 17 * DAY,
    )
    subscription_db.execute(
        "UPDATE subscriptions SET status='active',renews_at=?,ends_at=NULL",
        (P1_END,),
    )
    subscription_db.commit()
    replay = customer_requests.create_subscription_withdrawal(
        subscription_db,
        user_id=1,
        invoice_id="inv_initial",
        message="Iný text.",
        now=P0_START + 16 * DAY,
    )
    listing = customer_requests.requests_for_user(subscription_db, user_id=1)

    assert first.subscription_status == replay.subscription_status == "cancelled"
    assert first.subscription_ends_at == replay.subscription_ends_at == P0_END
    assert listing[0]["subscription_status"] == "cancelled"
    assert listing[0]["subscription_ends_at"] == P0_END
    assert "customer_annual_1" not in json.dumps(listing)


def test_missing_consent_receipt_does_not_deduct_consumed_service(
    monkeypatch, tmp_path
):
    server = _subscription_server(monkeypatch, tmp_path, now=P0_START + 7 * DAY)
    with closing(server.db()) as con:
        con.execute("UPDATE checkout_attempts SET consent_json=NULL WHERE user_id=1")
        _seed_invoice(con)
        con.commit()
    sent = []
    monkeypatch.setattr(
        server,
        "posli_mail",
        lambda komu, predmet, telo, html, **kw: sent.append(telo),
    )

    response = prihlaseny(server).post(
        "/api/consumer/withdrawal",
        json={"invoice_id": "inv_initial", "message": "Odstupujem."},
        headers={"Origin": "https://uvar.si"},
    )

    assert response.status_code == 202
    assert response.json()["refund_preview_cents"] == 3_900
    assert len(sent) == 1
    assert "neodpočítava" in sent[0]
    assert "Refundácia ešte nebola vykonaná" in sent[0]
