"""Annual Premium authorization reads only verified local state."""

from contextlib import closing
from dataclasses import replace
from datetime import datetime
import importlib
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from test_deterministic_plan_api import _server as deterministic_server
from test_server import (
    grant_premium,
    insert_hashed_session,
    plan_client,
    premium_user_server,
)


YEAR = 365 * 24 * 60 * 60


def seed_subscription(server, *, user_id=1, status="active", now=None, **changes):
    """Insert one valid Task 1 snapshot through the real domain boundary."""
    now = server.AUTH_CLOCK() if now is None else float(now)
    period_end = float(changes.pop("period_end", now + YEAR))
    paid_through = float(changes.pop("paid_through", period_end))
    renews_at = changes.pop(
        "renews_at", period_end if status in {"active", "past_due"} else None
    )
    ends_at = changes.pop(
        "ends_at", paid_through if status == "cancelled" else None
    )
    snapshot = server.predplatne.SubscriptionSnapshot(
        user_id=user_id,
        product="premium_annual",
        provider="lemonsqueezy",
        provider_customer_id=f"customer-{user_id}",
        provider_order_id=f"order-{user_id}",
        provider_subscription_id=f"subscription-{user_id}",
        provider_variant_id="annual-variant",
        currency="EUR",
        test_mode=False,
        status=status,
        period_start=float(changes.pop("period_start", now - 60)),
        period_end=period_end,
        renews_at=renews_at,
        ends_at=ends_at,
        paid_through=paid_through,
        initial_amount_cents=4900,
        renewal_amount_cents=4900,
        discount_id=None,
        founder=False,
        initial_payment_verified=True,
        provider_updated_at=float(changes.pop("provider_updated_at", now)),
        **changes,
    )
    with closing(server.db()) as con:
        assert server.predplatne.upsert_snapshot(con, snapshot, now=now) is True
        con.commit()
    return snapshot


def forbid_external_clients(monkeypatch, server):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("ordinary Premium authorization contacted an external client")

    monkeypatch.setattr(server, "_subscription_checkout_provider", forbidden)
    monkeypatch.setattr(server, "_lemon_checkout_request", forbidden)
    monkeypatch.setattr(server, "_new_plan_model_client", forbidden)


def current_job(server, con):
    today = server.bratislava_day()
    user = con.execute("SELECT * FROM pouzivatelia WHERE id=1").fetchone()
    stores = server.ulozene_obchody(user)
    rows = server.measurable_offers(
        server.offers_for_current_week(con, stores, today)
    )
    pantry = server.spajza_pouzivatela(con, 1, True)
    signature = server.podpis_planu(
        server.monday(today),
        stores,
        user["frekvencia"],
        rows,
        pantry,
        adults=2,
        children=2,
        stravovanie="standard",
    )
    job = SimpleNamespace(
        user_id=1,
        week=server.monday(today),
        signature=signature,
        kind="regular",
        payload={
            "stores": stores,
            "frequency": user["frekvencia"],
            "adults": 2,
            "children": 2,
            "algo_version": server.PLAN_ALGO_VERSION,
        },
    )
    return job, stores


def test_has_premium_combines_manual_and_verified_subscription_state(
    monkeypatch, tmp_path
):
    server = premium_user_server(monkeypatch, tmp_path)
    now = server.AUTH_CLOCK()

    with closing(server.db()) as con:
        assert server.has_premium(con, user_id=1, now=now) is False

    seed_subscription(server, now=now)
    with closing(server.db()) as con:
        assert server.has_premium(con, user_id=1, now=now) is True


def test_cancelled_paid_period_keeps_every_premium_gate_local_and_open(
    monkeypatch, tmp_path
):
    server = deterministic_server(
        monkeypatch,
        tmp_path,
        premium=False,
        pantry=(("ryža", 950, "g"), ("tofu", 400, "g")),
    )
    now = server.AUTH_CLOCK()
    monkeypatch.setattr(server, "AUTH_CLOCK", lambda: now)
    snapshot = seed_subscription(server, status="cancelled", now=now)
    forbid_external_clients(monkeypatch, server)
    monkeypatch.setattr(
        server, "STATIC", str(Path(__file__).resolve().parents[1] / "app" / "static")
    )
    client = plan_client(server, 1, wait_for_worker=False)

    assert client.get("/app").status_code == 200
    me = client.get("/api/me")
    assert me.status_code == 200
    assert me.json()["premium"] is True
    assert me.json()["spajza_dostupna"] is True
    assert me.json()["limit_prepoctov"] == server.LIMIT_PREPOCTOV_PREMIUM

    status = client.get("/api/platba/stav")
    assert status.status_code == 200
    expected_status = {
        "status": "cancelled",
        "renews_at": None,
        "ends_at": snapshot.ends_at,
        "next_amount_cents": 4900,
        "auto_renews": False,
        "can_manage": True,
    }
    assert {
        field: status.json()[field] for field in expected_status
    } == expected_status

    saved = client.post(
        "/api/profil",
        json={
            "adults": 2,
            "children": 2,
            "frekvencia": 2,
            "obchody": ["Kaufland", "Lidl", "Tesco"],
            "stravovanie": "standard",
        },
    )
    assert saved.status_code == 200
    assert client.post(
        "/api/spajza",
        json={"polozky": [{"nazov": "ryža", "mnozstvo": 900, "jednotka": "g"}]},
    ).status_code == 200

    pantry_plan = client.post("/api/plan/zo-spajze")
    assert pantry_plan.status_code == 200, pantry_plan.text
    assert pantry_plan.json()["meta"]["engine"] == "deterministic"
    assert client.get("/api/plan").status_code == 200
    assert client.post("/api/plan/generuj?force=1").status_code == 200

    with closing(server.db()) as con:
        profiles = server.predpocet.aktivne_profily(con, server)
        assert profiles[0].obchody == ("Kaufland", "Lidl", "Tesco")
        job, stores = current_job(server, con)
        _rows, current_pantry, _identity = server._current_job_context(
            job,
            stores,
            2,
            2,
            2,
            con=con,
            now=datetime.fromtimestamp(now),
        )
        assert current_pantry == [
            {"nazov": "ryža", "mnozstvo": 900, "jednotka": "g"}
        ]


@pytest.mark.parametrize("status", ("active", "past_due"))
@pytest.mark.parametrize("paid_through_offset", (0, -1))
def test_active_and_past_due_keep_premium_routes_open_at_and_after_paid_through(
    monkeypatch, tmp_path, status, paid_through_offset
):
    server = deterministic_server(
        monkeypatch,
        tmp_path,
        premium=False,
        pantry=(("ryža", 950, "g"),),
    )
    now = server.AUTH_CLOCK()
    monkeypatch.setattr(server, "AUTH_CLOCK", lambda: now)
    paid_through = now + paid_through_offset
    seed_subscription(
        server,
        status=status,
        now=now,
        period_start=now - YEAR,
        period_end=paid_through,
        paid_through=paid_through,
        renews_at=paid_through,
    )
    forbid_external_clients(monkeypatch, server)
    client = plan_client(server, 1, wait_for_worker=False)

    me = client.get("/api/me")
    assert me.status_code == 200
    assert me.json()["premium"] is True
    assert me.json()["spajza_dostupna"] is True
    assert client.post(
        "/api/profil",
        json={
            "adults": 2,
            "children": 2,
            "frekvencia": 2,
            "obchody": ["Lidl", "Tesco"],
            "stravovanie": "standard",
        },
    ).status_code == 200
    assert client.post(
        "/api/spajza",
        json={"polozky": [{"nazov": "ryža", "mnozstvo": 900, "jednotka": "g"}]},
    ).status_code == 200
    pantry_plan = client.post("/api/plan/zo-spajze")
    assert pantry_plan.status_code == 200, pantry_plan.text
    assert pantry_plan.json()["meta"]["engine"] == "deterministic"
    payment = client.get("/api/platba/stav")
    assert payment.status_code == 200
    assert payment.json()["ma_narok"] is True
    assert payment.json()["status"] == status


@pytest.mark.parametrize("status", ("unpaid", "expired"))
def test_unpaid_and_expired_snapshots_close_every_premium_gate(
    monkeypatch, tmp_path, status
):
    server = premium_user_server(
        monkeypatch, tmp_path, pantry=("ryža",), premium=False
    )
    now = server.AUTH_CLOCK()
    monkeypatch.setattr(server, "AUTH_CLOCK", lambda: now)
    seed_subscription(
        server,
        status=status,
        now=now,
        period_end=now - 1 if status == "expired" else now + YEAR,
        paid_through=now - 1 if status == "expired" else now + YEAR,
    )
    forbid_external_clients(monkeypatch, server)
    client = plan_client(server, 1, wait_for_worker=False)

    profile = client.get("/api/me").json()
    assert profile["premium"] is False
    assert profile["spajza"] == []
    assert profile["limit_prepoctov"] == server.LIMIT_PREPOCTOV_ZDARMA
    assert client.post(
        "/api/profil",
        json={
            "adults": 2,
            "children": 2,
            "frekvencia": 2,
            "obchody": ["Lidl", "Tesco"],
            "stravovanie": "standard",
        },
    ).status_code == 403
    assert client.post(
        "/api/spajza", json={"polozky": ["soľ"]}
    ).status_code == 403
    assert client.post("/api/plan/zo-spajze").status_code == 403

    payment = client.get("/api/platba/stav").json()
    assert payment["ma_narok"] is False
    assert payment["status"] == status
    assert payment["auto_renews"] is False
    assert payment["can_manage"] is True


def test_cancelled_snapshot_stops_authorizing_at_ends_at(monkeypatch, tmp_path):
    server = premium_user_server(monkeypatch, tmp_path, pantry=("ryža",))
    now = server.AUTH_CLOCK()
    snapshot = seed_subscription(
        server,
        status="cancelled",
        now=now,
        period_end=now + 10,
        paid_through=now + 10,
        ends_at=now + 10,
    )
    monkeypatch.setattr(server, "AUTH_CLOCK", lambda: snapshot.ends_at)
    client = plan_client(server, 1, wait_for_worker=False)

    assert client.get("/api/me").json()["premium"] is False
    assert client.post(
        "/api/spajza", json={"polozky": ["soľ"]}
    ).status_code == 403
    assert client.post("/api/plan/zo-spajze").status_code == 403


@pytest.mark.parametrize(
    (
        "status",
        "cutoff_offset",
        "ends_at",
        "needs_review",
        "expected_access",
    ),
    (
        ("cancelled", 1, "cutoff", False, True),
        ("cancelled", 0, "cutoff", False, False),
        ("paused", 1, None, False, True),
        ("paused", 0, None, False, False),
        ("expired", -1, None, False, False),
        ("active", 1, None, True, True),
    ),
)
def test_payment_status_exposes_authoritative_subscription_access_and_cutoff(
    monkeypatch,
    tmp_path,
    status,
    cutoff_offset,
    ends_at,
    needs_review,
    expected_access,
):
    server = premium_user_server(monkeypatch, tmp_path)
    now = server.AUTH_CLOCK()
    cutoff = now + cutoff_offset
    seed_subscription(
        server,
        status=status,
        now=now,
        period_end=cutoff,
        paid_through=cutoff,
        ends_at=cutoff if ends_at == "cutoff" else None,
        renews_at=cutoff if status == "active" else None,
        needs_review=needs_review,
        review_reason="provider update needs review" if needs_review else None,
    )
    monkeypatch.setattr(server, "AUTH_CLOCK", lambda: now)

    state = plan_client(server, 1, wait_for_worker=False).get(
        "/api/platba/stav"
    ).json()

    assert state["subscription_has_access"] is expected_access
    assert state["access_until"] == cutoff
    assert state["needs_review"] is (needs_review or status == "paused")


def test_expired_subscription_is_not_called_active_when_manual_premium_exists(
    monkeypatch, tmp_path
):
    server = premium_user_server(monkeypatch, tmp_path, premium=True)
    now = server.AUTH_CLOCK()
    seed_subscription(
        server,
        status="expired",
        now=now,
        period_end=now - 10,
        paid_through=now - 10,
    )

    state = plan_client(server, 1, wait_for_worker=False).get(
        "/api/platba/stav"
    ).json()

    assert state["ma_narok"] is True
    assert state["subscription_has_access"] is False
    assert state["access_until"] == now - 10


@pytest.mark.parametrize("entitlement", ("manual", "legacy_payment"))
def test_legacy_entitlements_remain_premium_without_subscription_management(
    monkeypatch, tmp_path, entitlement
):
    server = premium_user_server(monkeypatch, tmp_path)
    now = server.AUTH_CLOCK()
    if entitlement == "manual":
        with closing(server.db()) as con:
            importlib.import_module("platby").udel_narok_rucne(
                con, user_id=1, now=now
            )
    else:
        grant_premium(server, 1)

    with closing(server.db()) as con:
        assert server.has_premium(con, user_id=1, now=now) is True
    client = plan_client(server, 1, wait_for_worker=False)
    assert client.get("/api/me").json()["premium"] is True
    status = client.get("/api/platba/stav").json()
    assert status["ma_narok"] is True
    assert status["status"] is None
    assert status["next_amount_cents"] is None
    assert status["auto_renews"] is False
    assert status["can_manage"] is False


def test_unknown_snapshot_without_verified_baseline_never_grants_access(
    monkeypatch, tmp_path
):
    server = premium_user_server(monkeypatch, tmp_path)
    now = server.AUTH_CLOCK()
    unknown = seed_subscription(server, now=now)
    with closing(server.db()) as con:
        con.execute("DELETE FROM subscriptions WHERE user_id=1")
        assert server.predplatne.upsert_snapshot(
            con,
            replace(unknown, status="provider_mystery", provider_updated_at=now + 1),
            now=now + 1,
        ) is False
        con.commit()

    profile = plan_client(server, 1, wait_for_worker=False).get("/api/me").json()
    assert profile["premium"] is False


def test_quarantined_unknown_update_preserves_verified_access_and_status(
    monkeypatch, tmp_path
):
    server = premium_user_server(monkeypatch, tmp_path)
    now = server.AUTH_CLOCK()
    verified = seed_subscription(server, now=now)
    with closing(server.db()) as con:
        assert server.predplatne.upsert_snapshot(
            con,
            replace(
                verified,
                status="provider_mystery",
                renews_at=None,
                provider_updated_at=now + 1,
            ),
            now=now + 1,
        ) is False
        con.commit()

    client = plan_client(server, 1, wait_for_worker=False)
    assert client.get("/api/me").json()["premium"] is True
    status = client.get("/api/platba/stav").json()
    assert status["status"] == "active"
    assert status["renews_at"] == verified.renews_at
    assert status["auto_renews"] is True


def test_payment_status_is_server_owned_and_scoped_to_authenticated_user(
    monkeypatch, tmp_path
):
    server = premium_user_server(monkeypatch, tmp_path)
    now = server.AUTH_CLOCK()
    first = seed_subscription(server, status="cancelled", now=now)
    with closing(server.db()) as con:
        con.execute(
            "INSERT INTO pouzivatelia (id,email) VALUES (2,'other@uvar.si')"
        )
        insert_hashed_session(server, con, "session-2", 2)
        con.commit()
    seed_subscription(server, user_id=2, status="active", now=now)

    client = plan_client(server, 1, wait_for_worker=False)
    profile_write = client.post(
        "/api/profil",
        json={
            "adults": 2,
            "children": 2,
            "frekvencia": 2,
            "obchody": ["Lidl"],
            "stravovanie": "standard",
            "status": "active",
            "renews_at": now + 99 * YEAR,
            "ends_at": None,
            "next_amount_cents": 1,
            "auto_renews": True,
            "can_manage": False,
        },
    )
    assert profile_write.status_code == 200
    response = client.get(
        "/api/platba/stav",
        params={
            "user_id": 2,
            "status": "active",
            "renews_at": now + 99 * YEAR,
            "ends_at": 0,
            "next_amount_cents": 1,
            "auto_renews": True,
            "can_manage": False,
        },
    )

    assert response.status_code == 200
    assert response.json()["status"] == "cancelled"
    assert response.json()["renews_at"] is None
    assert response.json()["ends_at"] == first.ends_at
    assert response.json()["next_amount_cents"] == 4900
    assert response.json()["auto_renews"] is False
    assert response.json()["can_manage"] is True


def test_payment_status_derives_access_and_fields_from_one_read_snapshot(
    monkeypatch, tmp_path
):
    server = premium_user_server(monkeypatch, tmp_path)
    now = server.AUTH_CLOCK()
    active = seed_subscription(server, now=now)
    expired = replace(
        active,
        status="expired",
        renews_at=None,
        ends_at=None,
        paid_through=now - 1,
    )
    snapshots = iter((active, expired))
    reads = []

    def read_legacy(con, user_id):
        reads.append(("legacy", id(con), con.in_transaction))
        return False

    def read_subscription(con, user_id):
        reads.append(("subscription", id(con), con.in_transaction))
        return next(snapshots)

    monkeypatch.setattr(server, "ma_narok", read_legacy)
    monkeypatch.setattr(
        server.predplatne, "subscription_for_user", read_subscription
    )
    response = plan_client(server, 1, wait_for_worker=False).get(
        "/api/platba/stav"
    )

    assert response.status_code == 200
    assert response.json()["ma_narok"] is True
    assert response.json()["status"] == "active"
    assert response.json()["renews_at"] == active.renews_at
    assert response.json()["auto_renews"] is True
    assert [kind for kind, _connection, _transaction in reads] == [
        "legacy",
        "subscription",
    ]
    assert len({connection for _kind, connection, _transaction in reads}) == 1
    assert all(transaction for _kind, _connection, transaction in reads)


def test_payment_status_requires_authentication(monkeypatch, tmp_path):
    server = premium_user_server(monkeypatch, tmp_path)

    response = TestClient(server.app, raise_server_exceptions=False).get(
        "/api/platba/stav"
    )

    assert response.status_code == 401


def test_existing_subscription_blocks_checkout_before_provider_contact(
    monkeypatch, tmp_path
):
    server = premium_user_server(monkeypatch, tmp_path)
    now = server.AUTH_CLOCK()
    seed_subscription(server, now=now)
    ready = server.PaymentReadiness(
        ready=True,
        blockers=(),
        legal_version=server.LEGAL_VERSION,
        release=server.release_id(),
    )
    monkeypatch.setattr(server, "vyzaduj_zapnute_platby", lambda: None)
    monkeypatch.setattr(server, "_runtime_payment_readiness", lambda _con: ready)

    def forbidden_provider(*_args, **_kwargs):
        raise AssertionError("subscribed account reached checkout provider")

    monkeypatch.setattr(server, "_subscription_checkout_provider", forbidden_provider)
    client = plan_client(server, 1, wait_for_worker=False)
    response = client.post(
        "/api/platba/start",
        json={
            "accept_terms": True,
            "accept_automatic_renewal": True,
            "request_immediate_activation": True,
            "acknowledge_withdrawal_proration": True,
            "legal_version": server.LEGAL_VERSION,
        },
    )

    assert response.status_code == 409


@pytest.mark.parametrize("status", ("expired", "unpaid", "cancelled"))
def test_ended_subscription_checkout_returns_state_conflict_without_provider_call(
    monkeypatch, tmp_path, status
):
    server = premium_user_server(monkeypatch, tmp_path)
    now = server.AUTH_CLOCK()
    seed_subscription(
        server,
        status=status,
        now=now,
        period_start=now - YEAR,
        period_end=now - 1,
        paid_through=now - 1,
        ends_at=now - 1 if status == "cancelled" else None,
    )
    ready = server.PaymentReadiness(
        ready=True,
        blockers=(),
        legal_version=server.LEGAL_VERSION,
        release=server.release_id(),
    )
    monkeypatch.setattr(server, "vyzaduj_zapnute_platby", lambda: None)
    monkeypatch.setattr(server, "_runtime_payment_readiness", lambda _con: ready)

    def forbidden_checkout(_payload):
        raise AssertionError("existing subscription reached the provider")

    provider = SimpleNamespace(
        store_id="store-test",
        variant_id="variant-test",
        founder_discount_id="discount-test",
        founder_discount_code="FOUNDERS",
        create_checkout=forbidden_checkout,
    )
    monkeypatch.setattr(
        server,
        "_subscription_checkout_provider",
        lambda *, test_mode: provider,
    )
    response = plan_client(server, 1, wait_for_worker=False).post(
        "/api/platba/start",
        json={
            "accept_terms": True,
            "accept_automatic_renewal": True,
            "request_immediate_activation": True,
            "acknowledge_withdrawal_proration": True,
            "legal_version": server.LEGAL_VERSION,
        },
    )

    assert response.status_code == 409
    assert response.json()["kod"] == "subscription_exists"
    assert response.json()["subscription_status"] == status
    assert status in response.json()["detail"]


@pytest.mark.parametrize("entitlement", ("manual", "legacy_payment"))
def test_legacy_only_premium_still_blocks_checkout_before_provider_contact(
    monkeypatch, tmp_path, entitlement
):
    server = premium_user_server(monkeypatch, tmp_path)
    if entitlement == "manual":
        with closing(server.db()) as con:
            importlib.import_module("platby").udel_narok_rucne(
                con, user_id=1, now=server.AUTH_CLOCK()
            )
    else:
        grant_premium(server, 1)
    ready = server.PaymentReadiness(
        ready=True,
        blockers=(),
        legal_version=server.LEGAL_VERSION,
        release=server.release_id(),
    )
    monkeypatch.setattr(server, "vyzaduj_zapnute_platby", lambda: None)
    monkeypatch.setattr(server, "_runtime_payment_readiness", lambda _con: ready)

    def forbidden_provider(*_args, **_kwargs):
        raise AssertionError("legacy Premium reached checkout configuration")

    monkeypatch.setattr(server, "_subscription_checkout_provider", forbidden_provider)
    response = plan_client(server, 1, wait_for_worker=False).post(
        "/api/platba/start",
        json={
            "accept_terms": True,
            "accept_automatic_renewal": True,
            "request_immediate_activation": True,
            "acknowledge_withdrawal_proration": True,
            "legal_version": server.LEGAL_VERSION,
        },
    )

    assert response.status_code == 409
    assert response.json()["detail"] == server.SPRAVA_UZ_MAS
