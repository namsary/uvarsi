"""Executable server-local lifecycle gate contracts (no provider network)."""

from __future__ import annotations

import hashlib
import hmac
import importlib.util
import io
import json
import sqlite3
from pathlib import Path

import pytest

from app import platby, predplatne
from app import subscription_lifecycle_probe as probe


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "hetzner" / "payment-lifecycle-probe.py"
NOW = 1_789_200_000.0
TOKEN = "opaque-probe-" + "x" * 64
SIGNING_SECRET = "marker-signing-secret"


class TTY(io.StringIO):
    def isatty(self):
        return True


class Redirected(io.StringIO):
    def isatty(self):
        return False


def load_tool():
    spec = importlib.util.spec_from_file_location("payment_lifecycle_probe", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def config():
    return probe.LifecycleProbeConfig(
        api_key="test-api",
        store_id="test-store",
        variant_id="daily-variant",
        webhook_secret="dedicated-probe-hook",
        price_cents=100,
    )


@pytest.fixture
def db():
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    platby.migrate_platby_schema(con)
    predplatne.migrate_subscription_schema(con)
    probe.migrate_probe_schema(con)
    yield con
    con.close()


def variant_response(**changes):
    attrs = {
        "test_mode": True,
        "price": 100,
        "interval": "day",
        "interval_count": 1,
        "has_free_trial": False,
        "status": "published",
        "product_id": "daily-product",
    }
    attrs.update(changes)
    return {"data": {"type": "variants", "id": "daily-variant", "attributes": attrs}}


def product_response(**changes):
    attrs = {"test_mode": True, "store_id": "test-store", "status": "published"}
    attrs.update(changes)
    return {"data": {"type": "products", "id": "daily-product", "attributes": attrs}}


def checkout_response(url="https://store.lemonsqueezy.com/checkout/buy/abc?expires=1&signature=s"):
    return {"data": {"type": "checkouts", "id": "checkout-1", "attributes": {
        "test_mode": True,
        "store_id": "test-store",
        "variant_id": "daily-variant",
        "expires_at": "2026-09-13T13:00:00Z",
        "preview": {"currency": "EUR", "total": 100},
        "url": url,
    }}}


def provider_request(calls, *, checkout_url=None, variant_changes=None):
    def request(api_key, path, *, method="GET", payload=None):
        calls.append((api_key, path, method, payload))
        if path == "/v1/variants/daily-variant":
            return variant_response(**(variant_changes or {}))
        if path == "/v1/products/daily-product":
            return product_response()
        if path == "/v1/checkouts":
            response = checkout_response(
                checkout_url or checkout_response()["data"]["attributes"]["url"]
            )
            response["data"]["attributes"]["expires_at"] = (
                payload["data"]["attributes"]["expires_at"]
            )
            return response
        raise AssertionError(path)
    return request


def real_probe_event(event_type, *, invoice_id=None, billing_reason=None, created_at):
    is_invoice = event_type.startswith("subscription_payment_")
    attributes = {
        "test_mode": True,
        "store_id": "test-store",
        "created_at": created_at,
        "updated_at": created_at,
    }
    if is_invoice:
        attributes.update({
            "subscription_id": "sub-probe-1",
            "billing_reason": billing_reason,
            "currency": "EUR",
            "total": 100,
            "refunded_amount": 0,
            "status": "paid",
        })
        data_type = "subscription-invoices"
        data_id = invoice_id
    else:
        attributes.update({
            "variant_id": "daily-variant",
            "order_id": "order-probe-1",
            "status": "active",
        })
        data_type = "subscriptions"
        data_id = "sub-probe-1"
    return {
        "meta": {
            "event_name": event_type,
            "custom_data": {probe.PROBE_TOKEN_FIELD: TOKEN},
        },
        "data": {"type": data_type, "id": data_id, "attributes": attributes},
    }


def queue_probe_event(con, event):
    body = json.dumps(event, sort_keys=True, separators=(",", ":")).encode()
    signature = hmac.new(
        config().webhook_secret.encode(), body, hashlib.sha256
    ).hexdigest()
    platby.odloz_webhook(
        con, telo=body, podpis=signature, now=NOW, dovod="payments_off"
    )


def test_provider_request_refuses_redirects_before_authorization_can_leave_lemon(
    monkeypatch,
):
    tool = load_tool()
    opened = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _limit):
            return b'{"data": {"type": "variants", "id": "1", "attributes": {}}}'

    class Opener:
        def open(self, request, *, timeout):
            opened.append((request.full_url, request.get_header("Authorization"), timeout))
            return Response()

    def build_opener(handler):
        assert isinstance(handler, tool.urllib.request.HTTPRedirectHandler)
        assert handler.redirect_request(None, None, 302, "Found", {}, "https://evil.test") is None
        return Opener()

    monkeypatch.setattr(tool.urllib.request, "build_opener", build_opener)
    monkeypatch.setattr(
        tool.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("globálny urlopen nesmie automaticky nasledovať presmerovanie")
        ),
    )

    result = tool._provider_request("secret-api", "/v1/variants/1")

    assert result["data"]["id"] == "1"
    assert opened == [
        ("https://api.lemonsqueezy.com/v1/variants/1", "Bearer secret-api", 20)
    ]


def test_prepare_builds_exact_test_daily_checkout_and_persists_no_url_or_token(db):
    tool = load_tool()
    calls = []
    output = TTY()

    result = tool.prepare_probe(
        db,
        config=config(),
        signing_secret=SIGNING_SECRET,
        annual_test_variant_id="annual-variant",
        live_webhook_secret="live-hook",
        annual_test_webhook_secret="annual-hook",
        now=NOW,
        token=TOKEN,
        stdout=output,
        stderr=TTY(),
        stdin=TTY(),
        request=provider_request(calls),
    )

    assert result == {"prepared": True}
    checkout_call = calls[-1]
    assert checkout_call[:3] == ("test-api", "/v1/checkouts", "POST")
    attrs = checkout_call[3]["data"]["attributes"]
    assert attrs["test_mode"] is True
    assert attrs["checkout_options"] == {
        "discount": False, "skip_trial": True, "subscription_preview": True,
    }
    assert attrs["checkout_data"] == {
        "custom": {probe.PROBE_TOKEN_FIELD: TOKEN}
    }
    assert "custom_price" not in json.dumps(checkout_call[3])
    persisted = json.dumps([
        dict(row) for row in db.execute("SELECT * FROM subscription_lifecycle_probe_runs")
    ], sort_keys=True)
    assert TOKEN not in persisted
    assert "lemonsqueezy.com" not in persisted
    assert checkout_response()["data"]["attributes"]["url"] in output.getvalue()


def test_prepare_refuses_redirected_output_before_provider_or_database_mutation(db):
    tool = load_tool()
    calls = []

    with pytest.raises(tool.ProbeToolFailed, match="terminál"):
        tool.prepare_probe(
            db, config=config(), signing_secret=SIGNING_SECRET,
            annual_test_variant_id="annual-variant",
            live_webhook_secret="live-hook",
            annual_test_webhook_secret="annual-hook", now=NOW,
            stdout=Redirected(), stderr=TTY(), stdin=TTY(),
            request=provider_request(calls),
        )

    assert calls == []
    assert db.execute("SELECT COUNT(*) FROM subscription_lifecycle_probe_runs").fetchone()[0] == 0


@pytest.mark.parametrize("url", [
    "http://store.lemonsqueezy.com/checkout/buy/x?expires=1&signature=s",
    "https://evil.example/checkout/buy/x?expires=1&signature=s",
    "https://user:pw@store.lemonsqueezy.com/checkout/buy/x?expires=1&signature=s",
    "https://store.lemonsqueezy.com/checkout/buy/x",
    "https://store.lemonsqueezy.com/checkout/buy/x?expires=1&signature=s#leak",
])
def test_prepare_rejects_unsafe_checkout_url_without_printing_it(db, url):
    tool = load_tool()
    output = TTY()
    with pytest.raises(tool.ProbeToolFailed, match="poklad"):
        tool.prepare_probe(
            db, config=config(), signing_secret=SIGNING_SECRET,
            annual_test_variant_id="annual-variant",
            live_webhook_secret="live-hook",
            annual_test_webhook_secret="annual-hook", now=NOW, token=TOKEN,
            stdout=output, stderr=TTY(), stdin=TTY(),
            request=provider_request([], checkout_url=url),
        )
    assert url not in output.getvalue()
    assert db.execute("SELECT COUNT(*) FROM subscription_lifecycle_probe_runs").fetchone()[0] == 0


@pytest.mark.parametrize("change", [
    {"test_mode": False}, {"price": 101}, {"interval": "year"},
    {"interval_count": 2}, {"has_free_trial": True}, {"status": "draft"},
])
def test_prepare_rejects_wrong_provider_metadata_before_checkout(db, change):
    tool = load_tool()
    calls = []
    with pytest.raises(tool.ProbeToolFailed, match="denn"):
        tool.prepare_probe(
            db, config=config(), signing_secret=SIGNING_SECRET,
            annual_test_variant_id="annual-variant",
            live_webhook_secret="live-hook",
            annual_test_webhook_secret="annual-hook", now=NOW,
            stdout=TTY(), stderr=TTY(), stdin=TTY(),
            request=provider_request(calls, variant_changes=change),
        )
    assert all(path != "/v1/checkouts" for _, path, _, _ in calls)


def test_safe_status_lists_only_event_names_counts_and_timestamps(db):
    tool = load_tool()
    probe.create_probe_run(
        db, config=config(), signing_secret=SIGNING_SECRET, now=NOW, token=TOKEN,
    )
    body = json.dumps({
        "meta": {"event_name": "subscription_created", "custom_data": {
            probe.PROBE_TOKEN_FIELD: TOKEN,
        }},
        "data": {"type": "subscriptions", "id": "sub-private", "attributes": {
            "test_mode": True, "store_id": "test-store", "variant_id": "daily-variant",
            "order_id": "order-private",
            "billing_period_start": "2026-09-13T00:00:00Z",
            "billing_period_end": "2026-09-14T00:00:00Z", "status": "active",
            "updated_at": "2026-09-13T12:00:00Z",
        }},
    }, separators=(",", ":")).encode()
    sig = hmac.new(config().webhook_secret.encode(), body, hashlib.sha256).hexdigest()
    platby.odloz_webhook(db, telo=body, podpis=sig, now=NOW, dovod="payments_off")
    probe.process_queued_probe_events(
        db, config=config(), signing_secret=SIGNING_SECRET, now=NOW + 1,
    )

    status = tool.safe_probe_status(db, config=config(), signing_secret=SIGNING_SECRET)
    rendered = json.dumps(status, sort_keys=True)
    assert status["event_count"] == 1
    assert status["events"][0]["name"] == "subscription_created"
    assert "received_at" in status["events"][0]
    for forbidden in (TOKEN, "sub-private", "order-private", "invoice-private",
                      "test-store", "daily-variant", "test-api", "dedicated-probe-hook"):
        assert forbidden not in rendered


def test_process_probe_verifies_renewal_from_real_subscription_and_invoice_fields(db):
    tool = load_tool()
    probe.create_probe_run(
        db, config=config(), signing_secret=SIGNING_SECRET, now=NOW, token=TOKEN,
    )
    probe.record_probe_provider_verified(
        db,
        token=TOKEN,
        config=config(),
        signing_secret=SIGNING_SECRET,
        test_mode=True,
        store_id="test-store",
        variant_id="daily-variant",
        price_cents=100,
        currency="EUR",
        interval="day",
        interval_count=1,
        trial_days=0,
        discount_applied_cents=0,
        variant_status="published",
        now=NOW + 1,
    )
    queue_probe_event(
        db,
        real_probe_event(
            "subscription_created", created_at="2026-09-13T12:00:00Z"
        ),
    )
    queue_probe_event(
        db,
        real_probe_event(
            "subscription_payment_success",
            invoice_id="invoice-initial-real",
            billing_reason="initial",
            created_at="2026-09-13T12:00:00Z",
        ),
    )
    queue_probe_event(
        db,
        real_probe_event(
            "subscription_payment_success",
            invoice_id="invoice-renewal-real",
            billing_reason="renewal",
            created_at="2026-09-14T12:00:00Z",
        ),
    )
    calls = []

    def request(api_key, path, *, method="GET", payload=None):
        calls.append((api_key, path, method, payload))
        if path == "/v1/subscriptions/sub-probe-1":
            return {"data": {"type": "subscriptions", "id": "sub-probe-1", "attributes": {
                "test_mode": True,
                "store_id": "test-store",
                "variant_id": "daily-variant",
                "status": "active",
            }}}
        if path == "/v1/subscription-invoices/invoice-renewal-real":
            return {"data": {"type": "subscription-invoices", "id": "invoice-renewal-real", "attributes": {
                "test_mode": True,
                "store_id": "test-store",
                "subscription_id": "sub-probe-1",
                "billing_reason": "renewal",
                "currency": "EUR",
                "total": 100,
                "status": "paid",
                "created_at": "2026-09-14T12:00:00Z",
            }}}
        raise AssertionError(path)

    result = tool.process_probe(
        db,
        config=config(),
        signing_secret=SIGNING_SECRET,
        now=NOW + 20,
        request=request,
        lifecycle_module=probe,
    )

    assert result["processed"] == 3
    assert probe.probe_status(db, token=TOKEN)[
        "genuine_daily_renewal_verified"
    ] is True
    assert [path for _key, path, _method, _payload in calls] == [
        "/v1/subscriptions/sub-probe-1",
        "/v1/subscription-invoices/invoice-renewal-real",
    ]


def test_process_probe_can_retry_provider_verification_after_queue_was_scrubbed(db):
    tool = load_tool()
    probe.create_probe_run(
        db, config=config(), signing_secret=SIGNING_SECRET, now=NOW, token=TOKEN,
    )
    probe.record_probe_provider_verified(
        db,
        token=TOKEN,
        config=config(),
        signing_secret=SIGNING_SECRET,
        test_mode=True,
        store_id="test-store",
        variant_id="daily-variant",
        price_cents=100,
        currency="EUR",
        interval="day",
        interval_count=1,
        trial_days=0,
        discount_applied_cents=0,
        variant_status="published",
        now=NOW + 1,
    )
    for event in (
        real_probe_event("subscription_created", created_at="2026-09-13T12:00:00Z"),
        real_probe_event(
            "subscription_payment_success", invoice_id="invoice-initial-real",
            billing_reason="initial", created_at="2026-09-13T12:00:00Z",
        ),
        real_probe_event(
            "subscription_payment_success", invoice_id="invoice-renewal-real",
            billing_reason="renewal", created_at="2026-09-14T12:00:00Z",
        ),
    ):
        queue_probe_event(db, event)

    def unavailable(*_args, **_kwargs):
        raise OSError("provider unavailable")

    with pytest.raises(tool.ProbeToolFailed, match="nedostupné"):
        tool.process_probe(
            db, config=config(), signing_secret=SIGNING_SECRET, now=NOW + 20,
            request=unavailable, lifecycle_module=probe,
        )
    assert db.execute(
        "SELECT COUNT(*) FROM platobne_odlozene WHERE spracovane_o IS NULL"
    ).fetchone()[0] == 0

    def available(_api_key, path, *, method="GET", payload=None):
        if path == "/v1/subscriptions/sub-probe-1":
            return {"data": {"type": "subscriptions", "id": "sub-probe-1", "attributes": {
                "test_mode": True, "store_id": "test-store",
                "variant_id": "daily-variant", "status": "active",
            }}}
        if path == "/v1/subscription-invoices/invoice-renewal-real":
            return {"data": {"type": "subscription-invoices", "id": "invoice-renewal-real", "attributes": {
                "test_mode": True, "store_id": "test-store",
                "subscription_id": "sub-probe-1", "billing_reason": "renewal",
                "currency": "EUR", "total": 100, "status": "paid",
                "created_at": "2026-09-14T12:00:00Z",
            }}}
        raise AssertionError(path)

    tool.process_probe(
        db, config=config(), signing_secret=SIGNING_SECRET, now=NOW + 30,
        request=available, lifecycle_module=probe,
    )
    assert probe.probe_status(db, token=TOKEN)[
        "genuine_daily_renewal_verified"
    ] is True


def test_process_probe_skips_refunded_older_candidate_and_verifies_newer_paid_one(db):
    tool = load_tool()
    probe.create_probe_run(
        db, config=config(), signing_secret=SIGNING_SECRET, now=NOW, token=TOKEN,
    )
    probe.record_probe_provider_verified(
        db,
        token=TOKEN,
        config=config(),
        signing_secret=SIGNING_SECRET,
        test_mode=True,
        store_id="test-store",
        variant_id="daily-variant",
        price_cents=100,
        currency="EUR",
        interval="day",
        interval_count=1,
        trial_days=0,
        discount_applied_cents=0,
        variant_status="published",
        now=NOW + 1,
    )
    for event in (
        real_probe_event("subscription_created", created_at="2026-09-13T12:00:00Z"),
        real_probe_event(
            "subscription_payment_success", invoice_id="invoice-initial-real",
            billing_reason="initial", created_at="2026-09-13T12:00:00Z",
        ),
        real_probe_event(
            "subscription_payment_success", invoice_id="invoice-refunded-renewal",
            billing_reason="renewal", created_at="2026-09-14T12:00:00Z",
        ),
        real_probe_event(
            "subscription_payment_success", invoice_id="invoice-paid-renewal",
            billing_reason="renewal", created_at="2026-09-15T12:00:00Z",
        ),
    ):
        queue_probe_event(db, event)

    calls = []

    def request(_api_key, path, *, method="GET", payload=None):
        calls.append(path)
        if path == "/v1/subscriptions/sub-probe-1":
            return {"data": {"type": "subscriptions", "id": "sub-probe-1", "attributes": {
                "test_mode": True, "store_id": "test-store",
                "variant_id": "daily-variant", "status": "active",
            }}}
        invoice_id = path.rsplit("/", 1)[-1]
        if invoice_id in {"invoice-refunded-renewal", "invoice-paid-renewal"}:
            return {"data": {"type": "subscription-invoices", "id": invoice_id, "attributes": {
                "test_mode": True, "store_id": "test-store",
                "subscription_id": "sub-probe-1", "billing_reason": "renewal",
                "currency": "EUR", "total": 100,
                "status": "refunded" if invoice_id == "invoice-refunded-renewal" else "paid",
                "created_at": (
                    "2026-09-14T12:00:00Z"
                    if invoice_id == "invoice-refunded-renewal"
                    else "2026-09-15T12:00:00Z"
                ),
            }}}
        raise AssertionError(path)

    tool.process_probe(
        db,
        config=config(),
        signing_secret=SIGNING_SECRET,
        now=NOW + 20,
        request=request,
        lifecycle_module=probe,
    )

    assert probe.probe_status(db, token=TOKEN)[
        "genuine_daily_renewal_verified"
    ] is True
    assert db.execute(
        "SELECT provider_renewal_invoice_id FROM subscription_lifecycle_probe_runs"
    ).fetchone()[0] == "invoice-paid-renewal"
    assert calls == [
        "/v1/subscriptions/sub-probe-1",
        "/v1/subscription-invoices/invoice-refunded-renewal",
        "/v1/subscriptions/sub-probe-1",
        "/v1/subscription-invoices/invoice-paid-renewal",
    ]


def test_cleanup_requires_valid_schema5_marker_and_exact_b1_confirmation(db):
    tool = load_tool()
    # This unit focuses on orchestration: B1 owns the detailed event/cleanup scope tests.
    digest = hashlib.sha256(TOKEN.encode()).hexdigest()
    db.execute(
        "INSERT INTO subscription_lifecycle_probe_runs "
        "(token_digest,config_fingerprint,state,provider_verified,genuine_renewal_verified,created_at,updated_at) "
        "VALUES (?,?, 'evidenced',1,1,?,?)",
        (digest, probe.probe_config_fingerprint(config(), signing_secret=SIGNING_SECRET), NOW, NOW),
    )
    db.commit()
    confirmation = probe.cleanup_confirmation(token_digest=digest)

    with pytest.raises(tool.ProbeToolFailed, match="marker"):
        tool.cleanup_probe(
            db, config=config(), signing_secret=SIGNING_SECRET,
            signed_marker={}, marker_is_valid=lambda _marker: False,
            confirmation=confirmation,
        )

    result = tool.cleanup_probe(
        db, config=config(), signing_secret=SIGNING_SECRET,
        signed_marker={"schema_version": 5}, marker_is_valid=lambda _marker: True,
        confirmation=confirmation,
    )
    assert result["deleted_runs"] == 1


def test_source_has_no_probe_user_env_key_or_public_endpoint():
    source = SCRIPT.read_text(encoding="utf-8")
    assert "LEMON_TEST_LIFECYCLE_PROBE_USER" not in source
    assert "@app." not in source
    assert "FastAPI" not in source


@pytest.mark.parametrize(
    ("command", "target"),
    [
        ("annual-prepare", "prepare_annual_checkout"),
        ("annual-process", "process_annual_queue"),
        ("annual-reconcile", "reconcile_annual_account"),
        ("annual-status", "_annual_safe_status"),
    ],
)
def test_main_executes_each_private_annual_command(monkeypatch, command, target):
    tool = load_tool()
    marker = __import__("app.payment_smoke_marker", fromlist=["x"])
    calls = []

    class Server:
        LEGAL_VERSION = "2026-09-12-v5"

        @staticmethod
        def db():
            con = sqlite3.connect(":memory:")
            con.execute("CREATE TABLE pouzivatelia (id INTEGER, email TEXT)")
            con.execute(
                "INSERT INTO pouzivatelia VALUES (1, 'annual@example.test')"
            )
            return con

        @staticmethod
        def normalize_email(value):
            return value.strip().lower()

        @staticmethod
        def _subscription_marker_expectation():
            return marker.SubscriptionMarkerExpectation(
                release="r1",
                live=marker.SubscriptionConfig(
                    "live-store", "live-annual", "live-discount", "LIVE",
                    "live-hook", "live-api", False,
                ),
                test=marker.SubscriptionConfig(
                    "test-store", "annual-variant", "annual-discount", "TEST",
                    "annual-hook", "test-api", True,
                ),
                signing_secret=SIGNING_SECRET,
                probe=config(),
            )

    monkeypatch.setattr(tool, "_require_payments_off", lambda **_kwargs: None)
    monkeypatch.setattr(
        tool, "_runtime", lambda _app_dir: (Server, object(), object(), probe, marker)
    )
    monkeypatch.setattr(
        tool,
        "_probe_config_from_env",
        lambda *_args, **_kwargs: (
            config(), SIGNING_SECRET, "annual-variant", "live-hook", "annual-hook"
        ),
    )
    monkeypatch.setattr(tool.getpass, "getpass", lambda _prompt: "annual@example.test")
    monkeypatch.setattr(tool, "_require_tty", lambda **_kwargs: None)
    monkeypatch.setattr(
        tool,
        "_annual_attempt_id",
        lambda *_args, **_kwargs: "a" * 43,
    )

    def invoked(*_args, **_kwargs):
        calls.append(target)
        return {"ok": True}

    monkeypatch.setattr(tool, target, invoked)
    assert tool.main([command, "--app-dir", "unused", "--env-file", "unused"]) == 0
    assert calls == [target]
