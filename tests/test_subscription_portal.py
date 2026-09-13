"""Customer Portal is explicit, owner-scoped, and never stores signed URLs."""

from __future__ import annotations

from contextlib import closing
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import config as app_config
from test_server import insert_hashed_session, plan_client, premium_user_server
from test_subscription_access import seed_subscription


PORTAL_URL = "https://store.lemonsqueezy.com/billing/signed-secret"


class FakePortalProvider:
    """Provider boundary fake with the full identity shape used by the route."""

    def __init__(self, *, test_mode=False, urls=None):
        self.test_mode = test_mode
        self.store_id = "store-test" if test_mode else "store-live"
        self.variant_id = "annual-variant"
        self.urls = list(urls or [PORTAL_URL])
        self.calls = []
        self.overrides = {}
        self.failure = None

    def retrieve_subscription(self, subscription_id):
        self.calls.append(subscription_id)
        if self.failure is not None:
            raise self.failure
        portal_url = self.urls[min(len(self.calls) - 1, len(self.urls) - 1)]
        attributes = {
            "store_id": self.store_id,
            "variant_id": self.variant_id,
            "customer_id": "customer-1",
            "order_id": "order-1",
            "product_id": "premium-annual",
            "product_name": "Uvar.si Premium",
            "variant_name": "Ročné Premium",
            "test_mode": self.test_mode,
            "status": "active",
            "renews_at": "2027-09-13T00:00:00Z",
            "ends_at": None,
            "urls": {
                "customer_portal": portal_url,
                "update_payment_method": (
                    "https://app.lemonsqueezy.com/my-orders/payment-method"
                ),
            },
        }
        attributes.update(self.overrides.get("attributes", {}))
        data = {
            "type": "subscriptions",
            "id": self.overrides.get("subscription_id", subscription_id),
            "attributes": attributes,
            "relationships": {
                "store": {"links": {"related": "https://api.lemonsqueezy.com/v1/stores/1"}},
                "customer": {"links": {"related": "https://api.lemonsqueezy.com/v1/customers/1"}},
                "variant": {"links": {"related": "https://api.lemonsqueezy.com/v1/variants/1"}},
            },
        }
        return self.overrides.get("response", {"data": data})


def _server_with_provider(monkeypatch, tmp_path, provider):
    server = premium_user_server(monkeypatch, tmp_path)
    seen_modes = []

    def factory(*, test_mode):
        seen_modes.append(test_mode)
        provider.test_mode = test_mode
        provider.store_id = "store-test" if test_mode else "store-live"
        return provider

    monkeypatch.setattr(
        server, "_subscription_portal_provider", factory, raising=False
    )
    return server, seen_modes


def _dump_database(server):
    with closing(server.db()) as con:
        return "\n".join(con.iterdump())


def _billing_rows(server):
    tables = (
        "subscriptions",
        "subscription_invoices",
        "subscription_events",
        "checkout_attempts",
        "naroky",
    )
    with closing(server.db()) as con:
        existing = {
            row[0]
            for row in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        return {
            table: [tuple(row) for row in con.execute(f"SELECT * FROM {table}")]
            for table in tables
            if table in existing
        }


def _seed_portal_subscription(server, *, test_mode=False, **changes):
    snapshot = seed_subscription(server, **changes)
    if test_mode:
        with closing(server.db()) as con:
            con.execute("UPDATE subscriptions SET test_mode=1 WHERE user_id=?", (snapshot.user_id,))
            con.commit()
    return replace(snapshot, test_mode=test_mode)


def test_portal_requires_login_and_an_owned_subscription(monkeypatch, tmp_path):
    provider = FakePortalProvider()
    server, _seen_modes = _server_with_provider(monkeypatch, tmp_path, provider)
    anonymous = TestClient(server.app, raise_server_exceptions=False)

    assert anonymous.post("/api/platba/portal").status_code == 401

    with closing(server.db()) as con:
        con.execute("INSERT INTO pouzivatelia (id,email) VALUES (2,'other@uvar.si')")
        con.commit()
    seed_subscription(server, user_id=2)

    response = plan_client(server, 1, wait_for_worker=False).post(
        "/api/platba/portal"
    )

    assert response.status_code == 404
    assert response.json()["kod"] == "subscription_not_manageable"
    assert provider.calls == []


def test_flags_off_and_client_selected_identity_cannot_change_portal_owner(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("PLATBY_ZAPNUTE", "0")
    provider = FakePortalProvider()
    server, _seen_modes = _server_with_provider(monkeypatch, tmp_path, provider)
    seed_subscription(server, user_id=1)
    with closing(server.db()) as con:
        con.execute("INSERT INTO pouzivatelia (id,email) VALUES (2,'other@uvar.si')")
        con.commit()
    seed_subscription(server, user_id=2)

    response = plan_client(server, 1, wait_for_worker=False).post(
        "/api/platba/portal?user_id=2&subscription_id=subscription-2",
        json={"user_id": 2, "subscription_id": "subscription-2"},
    )

    assert response.status_code == 200
    assert response.json() == {"url": PORTAL_URL}
    assert provider.calls == ["subscription-1"]


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("provider", "other-provider"),
        ("product", "other-product"),
        ("provider_customer_id", ""),
        ("provider_subscription_id", ""),
        ("provider_variant_id", ""),
    ),
)
def test_portal_rejects_unverified_local_provider_identity_before_contact(
    monkeypatch, tmp_path, field, value
):
    provider = FakePortalProvider()
    server, _seen_modes = _server_with_provider(monkeypatch, tmp_path, provider)
    seed_subscription(server)
    with closing(server.db()) as con:
        con.execute(f"UPDATE subscriptions SET {field}=? WHERE user_id=1", (value,))
        con.commit()

    response = plan_client(server, 1, wait_for_worker=False).post(
        "/api/platba/portal"
    )

    assert response.status_code == 404
    assert response.json()["kod"] == "subscription_not_manageable"
    assert provider.calls == []


@pytest.mark.parametrize("test_mode", (False, True))
@pytest.mark.parametrize(
    "status", ("active", "cancelled", "past_due", "unpaid", "expired")
)
def test_every_provider_managed_subscription_state_can_open_its_own_portal(
    monkeypatch, tmp_path, status, test_mode
):
    provider = FakePortalProvider(test_mode=test_mode)
    server, seen_modes = _server_with_provider(monkeypatch, tmp_path, provider)
    now = server.AUTH_CLOCK()
    changes = {}
    if status in {"expired", "unpaid"}:
        changes.update(period_end=now - 1, paid_through=now - 1)
    _seed_portal_subscription(
        server, status=status, now=now, test_mode=test_mode, **changes
    )
    client = plan_client(server, 1, wait_for_worker=False)

    local_status = client.get("/api/platba/stav").json()
    response = client.post("/api/platba/portal")

    assert local_status["can_manage"] is True
    assert response.status_code == 200
    assert response.json() == {"url": PORTAL_URL}
    assert seen_modes == [test_mode]
    assert provider.calls == ["subscription-1"]


def test_portal_config_selects_live_or_test_identity_without_checkout_discount():
    values = {
        "LEMON_API_KEY": "live-api-secret",
        "LEMON_STORE_ID": "store-live",
        "LEMON_SUBSCRIPTION_VARIANT_ID": "variant-live",
        "LEMON_TEST_API_KEY": "test-api-secret",
        "LEMON_TEST_STORE_ID": "store-test",
        "LEMON_TEST_SUBSCRIPTION_VARIANT_ID": "variant-test",
    }

    live = app_config.lemon_customer_portal_config(
        test_mode=False, getenv=values.get
    )
    test = app_config.lemon_customer_portal_config(
        test_mode=True, getenv=values.get
    )

    assert (live.store_id, live.variant_id, live.test_mode) == (
        "store-live", "variant-live", False
    )
    assert (test.store_id, test.variant_id, test.test_mode) == (
        "store-test", "variant-test", True
    )
    assert live.api_key == "live-api-secret"
    assert test.api_key == "test-api-secret"
    assert "api-secret" not in repr(live)
    assert "api-secret" not in repr(test)


def test_provider_adapter_uses_one_fixed_api_read_without_live_network(
    monkeypatch, tmp_path
):
    server = premium_user_server(monkeypatch, tmp_path)
    requests = []

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, limit):
            assert limit == 1_048_577
            return b'{"data":{"type":"subscriptions","id":"subscription/one"}}'

    def fake_urlopen(request, *, timeout):
        requests.append((request, timeout))
        return FakeResponse()

    monkeypatch.setattr(server, "urlopen", fake_urlopen)

    result = server._lemon_subscription_request(
        "provider-api-secret", "subscription/one"
    )

    assert result["data"]["id"] == "subscription/one"
    assert len(requests) == 1
    request, timeout = requests[0]
    assert request.full_url == (
        "https://api.lemonsqueezy.com/v1/subscriptions/subscription%2Fone"
    )
    assert request.method == "GET"
    assert request.get_header("Authorization") == "Bearer provider-api-secret"
    assert timeout == 20


@pytest.mark.parametrize(
    ("override", "value"),
    (
        ("subscription_id", "subscription-other"),
        ("customer_id", "customer-other"),
        ("store_id", "store-other"),
        ("variant_id", "variant-other"),
        ("test_mode", True),
    ),
)
def test_portal_rejects_provider_identity_mismatch_with_safe_error(
    monkeypatch, tmp_path, caplog, override, value
):
    provider = FakePortalProvider()
    if override == "subscription_id":
        provider.overrides[override] = value
    else:
        provider.overrides["attributes"] = {override: value}
    server, _seen_modes = _server_with_provider(monkeypatch, tmp_path, provider)
    seed_subscription(server)

    response = plan_client(server, 1, wait_for_worker=False).post(
        "/api/platba/portal"
    )

    assert response.status_code == 503
    assert "customer-other" not in response.text
    assert "subscription-other" not in response.text
    assert "store-other" not in response.text
    assert "variant-other" not in response.text
    if isinstance(value, str):
        assert value not in caplog.text


@pytest.mark.parametrize(
    "provider_response",
    (
        None,
        {},
        {"data": []},
        {"data": {"type": "customers", "id": "subscription-1"}},
        {"data": {"type": "subscriptions", "id": "subscription-1"}},
    ),
)
def test_portal_rejects_malformed_provider_response_without_exposing_it(
    monkeypatch, tmp_path, caplog, provider_response
):
    provider = FakePortalProvider()
    provider.overrides["response"] = provider_response
    server, _seen_modes = _server_with_provider(monkeypatch, tmp_path, provider)
    seed_subscription(server)

    response = plan_client(server, 1, wait_for_worker=False).post(
        "/api/platba/portal"
    )

    assert response.status_code == 503
    assert response.json()["kod"] == "portal_unavailable"
    assert PORTAL_URL not in response.text
    assert PORTAL_URL not in caplog.text


@pytest.mark.parametrize(
    "url",
    (
        "http://store.lemonsqueezy.com/billing/signed-secret",
        "https://attacker.example/billing/signed-secret",
        "https://store.lemonsqueezy.com.attacker.example/billing/signed-secret",
        "https://attacker@store.lemonsqueezy.com/billing/signed-secret",
        "https://store.lemonsqueezy.com:443/billing/signed-secret",
        "https://store.lemonsqueezy.com/billing/signed-secret#fragment",
        "javascript:alert(1)",
        "",
        None,
    ),
)
def test_portal_rejects_non_https_or_non_lemon_signed_url_without_leaking_it(
    monkeypatch, tmp_path, caplog, url
):
    provider = FakePortalProvider(urls=[url])
    server, _seen_modes = _server_with_provider(monkeypatch, tmp_path, provider)
    seed_subscription(server)

    response = plan_client(server, 1, wait_for_worker=False).post(
        "/api/platba/portal"
    )

    assert response.status_code == 503
    assert "signed-secret" not in response.text
    assert "signed-secret" not in caplog.text
    assert "signed-secret" not in _dump_database(server)


def test_portal_url_is_fresh_and_not_persisted_logged_printed_or_in_html(
    monkeypatch, tmp_path, caplog, capsys
):
    first = "https://store.lemonsqueezy.com/billing/fresh-signed-secret-one"
    second = "https://app.lemonsqueezy.com/my-orders/fresh-signed-secret-two"
    provider = FakePortalProvider(urls=[first, second])
    server, _seen_modes = _server_with_provider(monkeypatch, tmp_path, provider)
    seed_subscription(server)
    client = plan_client(server, 1, wait_for_worker=False)

    before = _billing_rows(server)
    first_response = client.post("/api/platba/portal")
    second_response = client.post("/api/platba/portal")
    after = _billing_rows(server)
    database_dump = _dump_database(server)
    output = capsys.readouterr()
    html = Path("app/static/app.html").read_text(encoding="utf-8")

    assert first_response.json() == {"url": first}
    assert second_response.json() == {"url": second}
    assert first_response.headers["cache-control"] == "no-store"
    assert second_response.headers["cache-control"] == "no-store"
    assert provider.calls == ["subscription-1", "subscription-1"]
    assert after == before
    for secret in ("fresh-signed-secret-one", "fresh-signed-secret-two"):
        assert secret not in database_dump
        assert secret not in caplog.text
        assert secret not in output.out
        assert secret not in output.err
        assert secret not in html


def test_ordinary_page_profile_and_status_loads_never_contact_portal_provider(
    monkeypatch, tmp_path
):
    provider = FakePortalProvider()
    server, seen_modes = _server_with_provider(monkeypatch, tmp_path, provider)
    seed_subscription(server)
    monkeypatch.setattr(
        server, "STATIC", str(Path(__file__).resolve().parents[1] / "app" / "static")
    )
    client = plan_client(server, 1, wait_for_worker=False)

    assert client.get("/app").status_code == 200
    assert client.get("/api/me").status_code == 200
    assert client.get("/api/platba/stav").status_code == 200
    assert client.get("/api/consumer/requests").status_code == 200
    assert client.get("/api/platba/portal").status_code == 405
    assert seen_modes == []
    assert provider.calls == []


@pytest.mark.parametrize("status", ("active", "cancelled", "past_due"))
def test_provider_failure_keeps_paid_access_and_returns_canonical_support(
    monkeypatch, tmp_path, caplog, status
):
    leaked = "https://store.lemonsqueezy.com/billing/do-not-log-this-secret"
    provider = FakePortalProvider()
    provider.failure = RuntimeError(leaked)
    server, _seen_modes = _server_with_provider(monkeypatch, tmp_path, provider)
    seed_subscription(server, status=status)
    client = plan_client(server, 1, wait_for_worker=False)
    before_me = client.get("/api/me").json()
    before_status = client.get("/api/platba/stav").json()

    response = client.post("/api/platba/portal")

    assert response.status_code == 503
    assert "zostáva" in response.text.casefold()
    assert server.OPERATOR.support_phone in response.text
    assert server.OPERATOR.support_email in response.text
    assert leaked not in response.text
    assert leaked not in caplog.text
    assert client.get("/api/me").json()["premium"] is before_me["premium"] is True
    after_status = client.get("/api/platba/stav").json()
    assert after_status == before_status


@pytest.mark.parametrize("status", ("unpaid", "expired"))
def test_provider_failure_does_not_restore_suspended_or_expired_access(
    monkeypatch, tmp_path, status
):
    provider = FakePortalProvider()
    provider.failure = OSError("provider unavailable")
    server, _seen_modes = _server_with_provider(monkeypatch, tmp_path, provider)
    now = server.AUTH_CLOCK()
    seed_subscription(
        server,
        status=status,
        now=now,
        period_end=now - 1,
        paid_through=now - 1,
    )
    client = plan_client(server, 1, wait_for_worker=False)

    response = client.post("/api/platba/portal")

    assert response.status_code == 503
    assert client.get("/api/me").json()["premium"] is False
    local_status = client.get("/api/platba/stav").json()
    assert local_status["status"] == status
    assert local_status["can_manage"] is True


def test_missing_portal_configuration_fails_safely_without_network(
    monkeypatch, tmp_path
):
    server = premium_user_server(monkeypatch, tmp_path)
    seed_subscription(server)
    for name in (
        "LEMON_API_KEY", "LEMON_STORE_ID", "LEMON_SUBSCRIPTION_VARIANT_ID"
    ):
        monkeypatch.delenv(name, raising=False)

    network_calls = []

    def forbidden_network(*args, **_kwargs):
        network_calls.append(args)
        raise AssertionError("missing configuration reached the network")

    monkeypatch.setattr(server, "urlopen", forbidden_network)
    response = plan_client(server, 1, wait_for_worker=False).post(
        "/api/platba/portal"
    )

    assert response.status_code == 503
    assert server.OPERATOR.support_phone in response.text
    assert server.OPERATOR.support_email in response.text
    assert network_calls == []
