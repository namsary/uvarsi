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
            "currency": "EUR", "total": 100, "subscription_id": "sub-private",
            "order_id": "order-private", "invoice_id": "invoice-private",
            "billing_period_start": "2026-09-13T00:00:00Z",
            "billing_period_end": "2026-09-14T00:00:00Z", "status": "active",
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
