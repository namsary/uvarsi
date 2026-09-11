"""Withdrawal and complaint workflows must be private, auditable, and honest."""

import asyncio
import json
import sqlite3
import threading
from contextlib import closing
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import customer_requests, platby
from test_platby import (
    aktivne,
    objednavka,
    posli_webhook,
    prihlaseny,
    vytvor_pouzivatela,
    zapnute_platby,
)


@pytest.fixture
def database():
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.execute(
        "CREATE TABLE pouzivatelia (id INTEGER PRIMARY KEY, email TEXT, platiaci INTEGER DEFAULT 0)"
    )
    con.execute("INSERT INTO pouzivatelia (id,email) VALUES (7,'seven@example.test')")
    con.execute("INSERT INTO pouzivatelia (id,email) VALUES (8,'eight@example.test')")
    platby.migrate_platby_schema(con)
    customer_requests.migrate_customer_requests_schema(con)
    con.execute(
        """INSERT INTO naroky
           (user_id,produkt,poskytovatel,objednavka_id,suma_centy,mena,stav,ziskany_o,zmeneny_o)
           VALUES (7,?,?,?,?,?,?,?,?)""",
        (
            platby.PRODUKT_ZAKLADAJUCI,
            platby.POSKYTOVATEL,
            "order-1",
            3900,
            "EUR",
            platby.STAV_AKTIVNY,
            1000.0,
            1000.0,
        ),
    )
    con.commit()
    yield con
    con.close()


def test_withdrawal_within_14_days_requests_full_refund(database):
    request = customer_requests.create_withdrawal(
        database,
        user_id=7,
        order_id="order-1",
        purchased_at=1000,
        now=1000 + 13 * 86400,
    )

    assert request.refund_scope == customer_requests.REFUND_FULL
    assert request.status == customer_requests.STATUS_RECEIVED
    assert request.created is True


def test_withdrawal_after_14_days_is_received_for_manual_review(database):
    request = customer_requests.create_withdrawal(
        database,
        user_id=7,
        order_id="order-1",
        purchased_at=1000,
        now=1000 + 15 * 86400,
    )

    assert request.refund_scope == customer_requests.REFUND_REVIEW
    assert request.status == customer_requests.STATUS_REQUIRES_REVIEW


def test_user_can_request_only_their_own_order(database):
    with pytest.raises(customer_requests.RequestNotAllowed):
        customer_requests.create_withdrawal(
            database, user_id=8, order_id="order-1", now=2000
        )

    assert database.execute("SELECT COUNT(*) FROM consumer_requests").fetchone()[0] == 0


def test_duplicate_submission_returns_the_same_request(database):
    first = customer_requests.create_withdrawal(
        database, user_id=7, order_id="order-1", now=2000
    )
    second = customer_requests.create_withdrawal(
        database, user_id=7, order_id="order-1", now=2001
    )

    assert second.public_id == first.public_id
    assert second.created is False
    assert database.execute("SELECT COUNT(*) FROM consumer_requests").fetchone()[0] == 1


def test_migration_never_labels_a_historical_request_with_the_current_legal_version():
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.execute(
        """CREATE TABLE consumer_requests (
             id INTEGER PRIMARY KEY AUTOINCREMENT,
             public_id TEXT NOT NULL UNIQUE,
             user_id INTEGER NOT NULL,
             order_id TEXT NOT NULL,
             request_type TEXT NOT NULL,
             message TEXT NOT NULL,
             status TEXT NOT NULL,
             refund_scope TEXT,
             purchased_at REAL NOT NULL,
             created_at REAL NOT NULL,
             updated_at REAL NOT NULL,
             legal_version TEXT
           )"""
    )
    con.execute(
        """INSERT INTO consumer_requests
             (public_id,user_id,order_id,request_type,message,status,
              purchased_at,created_at,updated_at,legal_version)
           VALUES ('historical-1',7,'order-1','complaint','Historické podanie',
                   'received',1000,1100,1100,?)""",
        (customer_requests.LEGAL_VERSION,),
    )
    con.commit()

    customer_requests.migrate_customer_requests_schema(con)

    legal_version, legal_snapshot = con.execute(
        """SELECT legal_version,legal_snapshot FROM consumer_requests
            WHERE public_id='historical-1'"""
    ).fetchone()
    assert legal_version == "legacy-unknown"
    assert legal_version != customer_requests.LEGAL_VERSION
    assert "nebola v pôvodnej evidencii uložená" in legal_snapshot
    con.close()


def test_confirmation_delivery_is_leased_to_only_one_concurrent_worker(tmp_path):
    database_path = tmp_path / "consumer-requests.db"
    with closing(sqlite3.connect(database_path)) as con:
        con.row_factory = sqlite3.Row
        con.execute(
            "CREATE TABLE pouzivatelia (id INTEGER PRIMARY KEY, email TEXT, platiaci INTEGER DEFAULT 0)"
        )
        con.execute("INSERT INTO pouzivatelia (id,email) VALUES (7,'seven@example.test')")
        platby.migrate_platby_schema(con)
        customer_requests.migrate_customer_requests_schema(con)
        con.execute(
            """INSERT INTO naroky
               (user_id,produkt,poskytovatel,objednavka_id,suma_centy,mena,stav,ziskany_o,zmeneny_o)
               VALUES (7,?,?,?,?,?,?,?,?)""",
            (
                platby.PRODUKT_ZAKLADAJUCI,
                platby.POSKYTOVATEL,
                "order-1",
                3900,
                "EUR",
                platby.STAV_AKTIVNY,
                1000.0,
                1000.0,
            ),
        )
        con.commit()
        request = customer_requests.create_withdrawal(
            con, user_id=7, order_id="order-1", now=2000
        )

    barrier = threading.Barrier(2)
    claims = []

    def claim(worker_id):
        with closing(sqlite3.connect(database_path, timeout=3)) as con:
            con.row_factory = sqlite3.Row
            barrier.wait(timeout=3)
            claims.append(
                customer_requests.claim_confirmation_delivery(
                    con,
                    worker_id=worker_id,
                    public_id=request.public_id,
                    now=2000,
                )
            )

    workers = [threading.Thread(target=claim, args=(f"worker-{i}",)) for i in range(2)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=5)

    assert all(not worker.is_alive() for worker in workers)
    assert len([item for item in claims if item is not None]) == 1


def test_expired_confirmation_lease_is_retryable_after_process_restart(tmp_path):
    database_path = tmp_path / "consumer-restart.db"
    with closing(sqlite3.connect(database_path)) as con:
        con.row_factory = sqlite3.Row
        con.execute(
            "CREATE TABLE pouzivatelia (id INTEGER PRIMARY KEY, email TEXT)"
        )
        con.execute("INSERT INTO pouzivatelia VALUES (7,'seven@example.test')")
        platby.migrate_platby_schema(con)
        customer_requests.migrate_customer_requests_schema(con)
        con.execute(
            """INSERT INTO naroky
               (user_id,produkt,poskytovatel,objednavka_id,suma_centy,mena,
                stav,ziskany_o,zmeneny_o)
               VALUES (7,?,?,?,?,?,?,?,?)""",
            (
                platby.PRODUKT_ZAKLADAJUCI,
                platby.POSKYTOVATEL,
                "order-restart",
                3900,
                "EUR",
                platby.STAV_AKTIVNY,
                1000.0,
                1000.0,
            ),
        )
        con.commit()
        request = customer_requests.create_withdrawal(
            con, user_id=7, order_id="order-restart", now=2000.0
        )
        first = customer_requests.claim_confirmation_delivery(
            con, worker_id="worker-before-restart", now=2000.0
        )
        assert first is not None

    with closing(sqlite3.connect(database_path)) as con:
        con.row_factory = sqlite3.Row
        recovered = customer_requests.claim_confirmation_delivery(
            con,
            worker_id="worker-after-restart",
            public_id=request.public_id,
            now=2000.0 + customer_requests.CONFIRMATION_LEASE_SECONDS,
        )

    assert recovered is not None
    assert recovered.attempt == 2
    assert recovered.idempotency_key == first.idempotency_key


def test_failed_confirmation_has_bounded_exponential_retry_and_safe_state(database):
    request = customer_requests.create_withdrawal(
        database, user_id=7, order_id="order-1", now=2000
    )
    now = 2000.0
    idempotency_keys = set()

    for attempt in range(1, customer_requests.CONFIRMATION_MAX_ATTEMPTS + 1):
        delivery = customer_requests.claim_confirmation_delivery(
            database,
            worker_id=f"worker-{attempt}",
            public_id=request.public_id,
            now=now,
        )
        assert delivery is not None
        idempotency_keys.add(delivery.idempotency_key)
        assert customer_requests.finish_confirmation_delivery(
            database,
            delivery,
            sent=False,
            failure_code="provider_unavailable",
            now=now,
        )
        row = customer_requests.requests_for_user(database, user_id=7)[0]
        assert row["confirmation_attempts"] == attempt
        assert row["confirmation_failure_code"] == "provider_unavailable"
        if attempt < customer_requests.CONFIRMATION_MAX_ATTEMPTS:
            assert row["confirmation_state"] == customer_requests.CONFIRMATION_PENDING
            assert row["confirmation_next_attempt_at"] > now
            now = row["confirmation_next_attempt_at"]
        else:
            assert row["confirmation_state"] == customer_requests.CONFIRMATION_FAILED
            assert row["confirmation_next_attempt_at"] is None

    assert len(idempotency_keys) == 1
    assert customer_requests.claim_confirmation_delivery(
        database,
        worker_id="one-too-many",
        public_id=request.public_id,
        now=now + 86_400,
    ) is None


@pytest.mark.parametrize("message", ["<b>pokazené</b>", "x" * 4001])
def test_complaint_rejects_html_and_oversized_text(database, message):
    with pytest.raises(ValueError):
        customer_requests.create_complaint(
            database, user_id=7, order_id="order-1", message=message, now=2000
        )


def test_request_listing_never_leaks_another_users_case(database):
    customer_requests.create_complaint(
        database,
        user_id=7,
        order_id="order-1",
        message="Nákupný zoznam nesedí.",
        now=2000,
    )

    assert customer_requests.requests_for_user(database, user_id=8) == []
    encoded = json.dumps(customer_requests.requests_for_user(database, user_id=7))
    assert "eight@example.test" not in encoded


def test_full_refund_closes_withdrawal_request(database):
    created = customer_requests.create_withdrawal(
        database, user_id=7, order_id="order-1", now=2000
    )

    changed = customer_requests.close_requests_for_refund(
        database, order_id="order-1", now=3000
    )

    assert changed == 1
    row = customer_requests.requests_for_user(database, user_id=7)[0]
    assert row["public_id"] == created.public_id
    assert row["status"] == customer_requests.STATUS_REFUNDED


def _insert_paid_order(server, *, user_id=1, order_id="order-1", purchased_at=None):
    purchased_at = server.AUTH_CLOCK() if purchased_at is None else purchased_at
    with closing(server.db()) as con:
        con.execute(
            """INSERT INTO naroky
               (user_id,produkt,poskytovatel,objednavka_id,suma_centy,mena,stav,ziskany_o,zmeneny_o)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (
                user_id,
                "zakladajuci_clen",
                "lemonsqueezy",
                order_id,
                3900,
                "EUR",
                "aktivny",
                purchased_at,
                purchased_at,
            ),
        )
        con.execute("UPDATE pouzivatelia SET platiaci=1 WHERE id=?", (user_id,))
        con.commit()


def test_authenticated_withdrawal_api_records_request_and_sends_service_receipt(
    monkeypatch, tmp_path
):
    server = zapnute_platby(monkeypatch, tmp_path)
    vytvor_pouzivatela(server, user_id=1, email="buyer@example.test")
    _insert_paid_order(server)
    sent = []
    monkeypatch.setattr(
        server,
        "posli_mail",
        lambda komu, predmet, telo, html, **kw: sent.append((komu, predmet, telo)),
    )
    client = prihlaseny(server)

    response = client.post(
        "/api/consumer/withdrawal",
        json={"order_id": "order-1"},
        headers={"Origin": "https://uvar.si"},
    )

    assert response.status_code == 202
    assert response.json()["ok"] is True
    assert response.json()["request_received"] is True
    assert response.json()["confirmation"] == {"state": "sent", "sent": True, "pending": False}
    assert response.json()["request_id"]
    assert sent and sent[0][0] == "buyer@example.test"
    assert "marketing" not in sent[0][2].casefold()
    listing = client.get("/api/consumer/requests").json()
    assert listing["requests"][0]["status"] == customer_requests.STATUS_RECEIVED
    assert listing["orders"][0]["order_id"] == "order-1"


def test_provider_failure_after_commit_returns_received_and_pending_retry(monkeypatch, tmp_path):
    server = zapnute_platby(monkeypatch, tmp_path)
    monkeypatch.setattr(server, "AUTH_CLOCK", lambda: 20_000.0)
    vytvor_pouzivatela(server, user_id=1, email="buyer@example.test")
    _insert_paid_order(server, purchased_at=19_000.0)

    def unavailable(*_args, **_kwargs):
        raise server.DeliveryError("provider response must not escape")

    monkeypatch.setattr(server, "posli_mail", unavailable)
    response = prihlaseny(server).post(
        "/api/consumer/complaint",
        json={"order_id": "order-1", "message": "Jedálniček nezohľadnil alergiu."},
        headers={"Origin": "https://uvar.si"},
    )

    assert response.status_code == 202
    body = response.json()
    assert body["request_received"] is True
    assert body["confirmation"] == {
        "state": "pending_retry",
        "sent": False,
        "pending": True,
    }
    assert "provider response" not in json.dumps(body)
    with closing(server.db()) as con:
        row = con.execute(
            """SELECT confirmation_state,confirmation_attempts,
                      confirmation_last_attempt_at,confirmation_next_attempt_at,
                      confirmation_failure_code
                 FROM consumer_requests WHERE public_id=?""",
            (body["request_id"],),
        ).fetchone()
    assert tuple(row[:3]) == ("pending", 1, 20_000.0)
    assert row[3] > 20_000.0
    assert row[4] == "provider_unavailable"


def test_pending_confirmation_retries_with_same_key_and_never_resends_after_success(
    monkeypatch, tmp_path
):
    server = zapnute_platby(monkeypatch, tmp_path)
    now = [30_000.0]
    monkeypatch.setattr(server, "AUTH_CLOCK", lambda: now[0])
    vytvor_pouzivatela(server, user_id=1, email="buyer@example.test")
    _insert_paid_order(server, purchased_at=29_000.0)
    keys = []

    def fail_once(_to, _subject, _text, _html, *, idempotency_key):
        keys.append(idempotency_key)
        raise server.DeliveryError("temporary")

    monkeypatch.setattr(server, "posli_mail", fail_once)
    client = prihlaseny(server)
    first = client.post(
        "/api/consumer/withdrawal",
        json={"order_id": "order-1", "message": "Odstupujem od zmluvy."},
        headers={"Origin": "https://uvar.si"},
    ).json()
    with closing(server.db()) as con:
        retry_at = con.execute(
            "SELECT confirmation_next_attempt_at FROM consumer_requests"
        ).fetchone()[0]

    sent_messages = []

    def accepted(to, subject, text, html, *, idempotency_key):
        keys.append(idempotency_key)
        sent_messages.append((to, subject, text, html))

    monkeypatch.setattr(server, "posli_mail", accepted)
    now[0] = retry_at
    assert server.process_consumer_request_confirmation_queue("retry-worker", limit=1) == 1
    assert server.process_consumer_request_confirmation_queue("retry-worker", limit=1) == 0

    duplicate = client.post(
        "/api/consumer/withdrawal",
        json={"order_id": "order-1", "message": "Odstupujem od zmluvy."},
        headers={"Origin": "https://uvar.si"},
    ).json()
    assert duplicate["request_id"] == first["request_id"]
    assert duplicate["confirmation"] == {"state": "sent", "sent": True, "pending": False}
    assert len(sent_messages) == 1
    assert len(set(keys)) == 1

    text = sent_messages[0][2]
    assert "Odstupujem od zmluvy." in text
    assert "order-1" in text
    assert first["request_id"] in text
    assert "PUMAR s. r. o." in text and "57 370 591" in text
    assert "pumaragency@gmail.com" in text
    assert server.LEGAL_VERSION in text
    assert "39 € raz. Premium bez predplatného počas prevádzky služby Uvar.si." in text
    assert "Lemon Squeezy" in text and "Merchant of Record" in text
    assert "Pri prijatí žiadosti refundácia ešte nebola vykonaná" in text
    assert "https://uvar.si/pravne/vop.txt" in text


def test_confirmation_retries_with_the_immutable_legal_snapshot_stored_at_receipt(
    monkeypatch, tmp_path
):
    server = zapnute_platby(monkeypatch, tmp_path)
    now = [40_000.0]
    monkeypatch.setattr(server, "AUTH_CLOCK", lambda: now[0])
    vytvor_pouzivatela(server, user_id=1, email="buyer@example.test")
    _insert_paid_order(server, purchased_at=39_000.0)
    expected_vop = server.customer_requests.legal_text("vop")
    expected_complaints = server.customer_requests.legal_text("reklamacie")

    def unavailable(*_args, **_kwargs):
        raise server.DeliveryError("temporary")

    monkeypatch.setattr(server, "posli_mail", unavailable)
    response = prihlaseny(server).post(
        "/api/consumer/complaint",
        json={"order_id": "order-1", "message": "Služba sa nenačítala."},
        headers={"Origin": "https://uvar.si"},
    )
    assert response.status_code == 202
    with closing(server.db()) as con:
        row = con.execute(
            """SELECT legal_snapshot,confirmation_next_attempt_at
                 FROM consumer_requests WHERE public_id=?""",
            (response.json()["request_id"],),
        ).fetchone()
    stored_snapshot = row[0]
    now[0] = row[1]
    assert expected_vop in stored_snapshot
    assert expected_complaints in stored_snapshot

    monkeypatch.setattr(
        server.customer_requests,
        "legal_text",
        lambda _slug: "NESKORŠIA ZMENENÁ PRÁVNA VERZIA",
    )
    sent = []
    monkeypatch.setattr(
        server,
        "posli_mail",
        lambda _to, _subject, text, html, **_kwargs: sent.append((text, html)),
    )

    assert server.process_consumer_request_confirmation_queue("retry-worker", limit=1) == 1
    assert len(sent) == 1
    assert stored_snapshot in sent[0][0]
    assert "NESKORŠIA ZMENENÁ PRÁVNA VERZIA" not in sent[0][0]


def test_unexpected_confirmation_worker_crash_schedules_one_bounded_retry(
    monkeypatch, tmp_path
):
    server = zapnute_platby(monkeypatch, tmp_path)
    monkeypatch.setattr(server, "AUTH_CLOCK", lambda: 50_000.0)
    monkeypatch.setattr(server, "CONSUMER_REQUEST_BACKGROUND_TASKS", set())
    monkeypatch.setattr(server, "CONSUMER_REQUEST_OUTBOX_WAKE_HANDLES", {})
    monkeypatch.setattr(server, "CONSUMER_REQUEST_OUTBOX_SHUTTING_DOWN", False)

    def crash(*_args, **_kwargs):
        raise sqlite3.OperationalError("temporary database outage")

    monkeypatch.setattr(server, "process_consumer_request_confirmation_queue", crash)
    scheduled = []

    class Handle:
        def cancel(self):
            return None

    def capture_schedule(_loop, delay, _callback):
        scheduled.append(delay)
        return Handle()

    monkeypatch.setattr(server, "CONSUMER_REQUEST_OUTBOX_CALL_LATER", capture_schedule)

    async def exercise_worker():
        task = server.ensure_consumer_request_confirmation_worker()
        await asyncio.gather(task, return_exceptions=True)
        await asyncio.sleep(0)

    asyncio.run(exercise_worker())

    assert len(scheduled) == 1
    assert customer_requests.CONFIRMATION_RETRY_BASE_SECONDS <= scheduled[0]
    assert scheduled[0] <= customer_requests.CONFIRMATION_RETRY_MAX_SECONDS


def test_post_commit_queue_crash_still_returns_received_and_pending(
    monkeypatch, tmp_path
):
    server = zapnute_platby(monkeypatch, tmp_path)
    vytvor_pouzivatela(server, user_id=1, email="buyer@example.test")
    _insert_paid_order(server)
    monkeypatch.setattr(server, "_notify_consumer_requests", lambda *_args, **_kwargs: None)

    def crash(*_args, **_kwargs):
        raise sqlite3.OperationalError("sensitive internal detail")

    monkeypatch.setattr(server, "process_consumer_request_confirmation_queue", crash)
    scheduled = []
    monkeypatch.setattr(
        server,
        "schedule_consumer_request_confirmation_wake",
        lambda wake_at: scheduled.append(wake_at),
    )

    response = prihlaseny(server).post(
        "/api/consumer/complaint",
        json={"order_id": "order-1", "message": "Jedálniček sa neotvoril."},
        headers={"Origin": "https://uvar.si"},
    )

    assert response.status_code == 202
    body = response.json()
    assert body["request_received"] is True
    assert body["confirmation"] == {
        "state": "pending_retry",
        "sent": False,
        "pending": True,
    }
    assert "sensitive internal detail" not in json.dumps(body)
    assert len(scheduled) == 1
    with closing(server.db()) as con:
        assert con.execute("SELECT COUNT(*) FROM consumer_requests").fetchone()[0] == 1


def test_post_commit_owner_notification_crash_does_not_change_received_response(
    monkeypatch, tmp_path
):
    server = zapnute_platby(monkeypatch, tmp_path)
    vytvor_pouzivatela(server, user_id=1, email="buyer@example.test")
    _insert_paid_order(server)

    def owner_notification_crash(*_args, **_kwargs):
        raise OSError("public notifier unavailable")

    monkeypatch.setattr(server, "_notify_consumer_requests", owner_notification_crash)
    monkeypatch.setattr(
        server,
        "process_consumer_request_confirmation_queue",
        lambda *_args, **_kwargs: 0,
    )
    monkeypatch.setattr(
        server, "schedule_consumer_request_confirmation_wake", lambda _wake_at: None
    )

    response = prihlaseny(server).post(
        "/api/consumer/complaint",
        json={"order_id": "order-1", "message": "Jedálniček sa neotvoril."},
        headers={"Origin": "https://uvar.si"},
    )

    assert response.status_code == 202
    assert response.json()["request_received"] is True
    with closing(server.db()) as con:
        assert con.execute("SELECT COUNT(*) FROM consumer_requests").fetchone()[0] == 1


def test_post_commit_confirmation_state_read_failure_still_returns_received_and_pending(
    monkeypatch, tmp_path
):
    server = zapnute_platby(monkeypatch, tmp_path)
    vytvor_pouzivatela(server, user_id=1, email="buyer@example.test")
    _insert_paid_order(server)
    monkeypatch.setattr(server, "_notify_consumer_requests", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        server,
        "process_consumer_request_confirmation_queue",
        lambda *_args, **_kwargs: 0,
    )

    def state_read_crash(*_args, **_kwargs):
        raise sqlite3.OperationalError("temporary state read failure")

    monkeypatch.setattr(
        server.customer_requests, "public_confirmation_state", state_read_crash
    )
    scheduled = []
    monkeypatch.setattr(
        server,
        "schedule_consumer_request_confirmation_wake",
        lambda wake_at: scheduled.append(wake_at),
    )

    response = prihlaseny(server).post(
        "/api/consumer/withdrawal",
        json={"order_id": "order-1"},
        headers={"Origin": "https://uvar.si"},
    )

    assert response.status_code == 202
    assert response.json()["request_received"] is True
    assert response.json()["confirmation"] == {
        "state": "pending_retry",
        "sent": False,
        "pending": True,
    }
    assert len(scheduled) == 1


def test_foreign_order_submission_returns_generic_response_without_creating_case(
    monkeypatch, tmp_path
):
    server = zapnute_platby(monkeypatch, tmp_path)
    vytvor_pouzivatela(server, user_id=1, email="one@example.test")
    vytvor_pouzivatela(server, user_id=2, email="two@example.test", session="other")
    _insert_paid_order(server, user_id=2, order_id="private-order")
    client = prihlaseny(server)

    response = client.post(
        "/api/consumer/withdrawal",
        json={"order_id": "private-order"},
        headers={"Origin": "https://uvar.si"},
    )

    assert response.status_code == 202
    assert response.json()["ok"] is True
    with closing(server.db()) as con:
        assert con.execute("SELECT COUNT(*) FROM consumer_requests").fetchone()[0] == 0


def test_full_refund_webhook_closes_request_and_revokes_entitlement(monkeypatch, tmp_path):
    server = zapnute_platby(monkeypatch, tmp_path)
    vytvor_pouzivatela(server)
    sent = []
    monkeypatch.setattr(
        server,
        "posli_mail",
        lambda komu, predmet, telo, html, **kw: sent.append((komu, predmet, telo)),
    )
    client = TestClient(server.app, raise_server_exceptions=False)
    assert posli_webhook(client, objednavka(order_id="order-1")).status_code == 200
    with closing(server.db()) as con:
        customer_requests.create_withdrawal(
            con, user_id=1, order_id="order-1", now=server.AUTH_CLOCK()
        )
    refund = objednavka(order_id="order-1", udalost="order_refunded")
    refund["data"]["attributes"].update(
        status="refunded", refunded=True, refunded_amount=3900
    )

    response = posli_webhook(client, refund)

    assert response.status_code == 200
    assert aktivne(server) == []
    with closing(server.db()) as con:
        request = customer_requests.requests_for_user(con, user_id=1)[0]
    assert request["status"] == customer_requests.STATUS_REFUNDED
    assert sent and "vrátili" in sent[-1][1]


def test_partial_refund_is_quarantined_without_revoking_entitlement(monkeypatch, tmp_path):
    server = zapnute_platby(monkeypatch, tmp_path)
    vytvor_pouzivatela(server)
    client = TestClient(server.app, raise_server_exceptions=False)
    assert posli_webhook(client, objednavka(order_id="order-1")).status_code == 200
    partial = objednavka(order_id="order-1", udalost="order_refunded")
    partial["data"]["attributes"].update(
        status="partial_refund", refunded=False, refunded_amount=100
    )

    response = posli_webhook(client, partial)

    assert response.status_code == 400
    assert len(aktivne(server)) == 1
    with closing(server.db()) as con:
        assert con.execute(
            "SELECT COUNT(*) FROM platobne_odlozene WHERE dovod='nepouzitelna'"
        ).fetchone()[0] == 1


def test_profile_contains_honest_withdrawal_and_complaint_controls():
    html = Path("app/static/app.html").read_text(encoding="utf-8")

    assert "/api/consumer/requests" in html
    assert "/api/consumer/withdrawal" in html
    assert "/api/consumer/complaint" in html
    assert "Refundácia ešte neprebehla" in html
    assert "Odoslať žiadosť o odstúpenie" in html
    assert "Odoslať reklamáciu" in html
    assert 'maxlength="4000"' in html
    assert 'type="file"' not in html


def test_customer_request_cli_never_grants_premium():
    source = Path("app/customer_requests_cli.py").read_text(encoding="utf-8")

    assert "udel_narok" not in source
    assert "processing" in source
