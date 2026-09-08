import hashlib
import hmac
import importlib.util
import json
from pathlib import Path
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
SMOKE_SCRIPT = ROOT / "hetzner" / "payment-smoke.py"
MARKER_MODULE = ROOT / "app" / "payment_smoke_marker.py"
SAMOPULL = ROOT / "hetzner" / "samopull.sh"
DEPLOY_STATE = ROOT / "hetzner" / "uvarsi-deploy-state.sh"
MANUAL_DEPLOY = ROOT / "nasad.ps1"


def _load_marker_module():
    spec = importlib.util.spec_from_file_location("payment_smoke_marker", MARKER_MODULE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_smoke_module():
    app_dir = str(ROOT / "app")
    if app_dir not in sys.path:
        sys.path.insert(0, app_dir)
    spec = importlib.util.spec_from_file_location("payment_smoke_tool", SMOKE_SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_deployment_starts_with_payments_off_and_migrates_before_health():
    samopull = SAMOPULL.read_text(encoding="utf-8")

    off_gate = samopull.index("uvarsi_require_payments_off")
    live_mutation = samopull.index("# --- 3. záloha aktuálneho stavu a prepnutie ---")
    migration = samopull.index("uvarsi_migrate_release")
    health = samopull.index("&& zdravie")

    assert off_gate < live_mutation
    assert live_mutation < migration < health


def test_manual_release_prepares_dependencies_and_checks_running_payment_flag_first():
    source = MANUAL_DEPLOY.read_text(encoding="utf-8")

    dependencies = source.index('Krok "4/8  Python venv a zavislosti"')
    preflight = source.index("$releasePreflight = @'")
    runtime_gate = source.index("uvarsi_require_runtime_payments_off")
    mutation = source.index("$script:LiveMutationStarted = $true")

    assert dependencies < preflight < runtime_gate < mutation
    assert source.count("uvarsi_require_runtime_payments_off") >= 3
    assert "requirements-auth.txt" in source


def test_automatic_rollback_never_restores_the_live_database():
    deploy = DEPLOY_STATE.read_text(encoding="utf-8")
    restore = deploy.split("uvarsi_restore()", 1)[1].split(
        "_uvarsi_apply_core()", 1
    )[0]

    assert "_uvarsi_restore_database" not in restore
    assert "uvarsi.db" not in restore
    assert "_uvarsi_snapshot_database" in deploy


def test_smoke_tool_has_no_card_input_and_never_logs_secrets():
    source = SMOKE_SCRIPT.read_text(encoding="utf-8")
    lower = source.casefold()

    assert "card_number" not in lower
    assert "cvv" not in lower
    assert "cvc" not in lower
    assert "authorization: bearer" not in lower
    assert "print(api_key" not in lower
    assert "print(webhook_secret" not in lower
    assert "/api/platba/stav" in source
    assert "/v1/orders/" in source and "/refund" in source


def test_signed_marker_is_bound_to_release_store_variant_and_full_lifecycle():
    marker_module = _load_marker_module()
    secret = "test-only-signing-secret"
    raw_order_id = "provider-test-order-do-not-persist-4f891a"
    live_fingerprint = marker_module.live_config_fingerprint(
        secret=secret,
        checkout_url="https://uvarsi.lemonsqueezy.com/checkout/buy/live-product",
        store_id="123",
        variant_id="456",
    )
    marker = marker_module.create_marker(
        release="2026.09.07.29",
        live_config_digest=live_fingerprint,
        test_store_id="test-123",
        test_variant_id="test-456",
        completed_at="2026-09-07T20:15:00+00:00",
        receipt_email_verified=True,
        test_mode_verified=True,
    )

    assert marker_module.verify_marker(
        marker,
        secret=secret,
        release="2026.09.07.29",
        checkout_url="https://uvarsi.lemonsqueezy.com/checkout/buy/live-product",
        store_id="123",
        variant_id="456",
    ) is False

    marker = marker_module.sign_marker(marker, secret=secret)
    assert marker_module.verify_marker(
        marker,
        secret=secret,
        release="2026.09.07.29",
        checkout_url="https://uvarsi.lemonsqueezy.com/checkout/buy/live-product",
        store_id="123",
        variant_id="456",
    ) is True
    assert marker["purchase_webhook_verified"] is True
    assert marker["entitlement_verified"] is True
    assert marker["receipt_email_verified"] is True
    assert marker["refund_webhook_verified"] is True
    assert marker["entitlement_revoked"] is True
    assert marker["test_mode_verified"] is True
    assert marker["unresolved_cases"] == 0
    assert "order_digest" not in marker
    assert raw_order_id not in json.dumps(marker)

    for field, wrong in (
        ("release", "2026.09.07.30"),
        ("checkout_url", "https://uvarsi.lemonsqueezy.com/checkout/buy/other"),
        ("store_id", "999"),
        ("variant_id", "999"),
    ):
        values = {
            "release": "2026.09.07.29",
            "checkout_url": "https://uvarsi.lemonsqueezy.com/checkout/buy/live-product",
            "store_id": "123",
            "variant_id": "456",
        }
        values[field] = wrong
        assert marker_module.verify_marker(marker, secret=secret, **values) is False

    tampered = dict(marker, unresolved_cases=1)
    assert marker_module.verify_marker(
        tampered,
        secret=secret,
        release="2026.09.07.29",
        checkout_url="https://uvarsi.lemonsqueezy.com/checkout/buy/live-product",
        store_id="123",
        variant_id="456",
    ) is False

    for bad_mode in (False, None):
        without_test_proof = dict(marker)
        if bad_mode is None:
            without_test_proof.pop("test_mode_verified")
        else:
            without_test_proof["test_mode_verified"] = bad_mode
        without_test_proof = marker_module.sign_marker(
            without_test_proof, secret=secret
        )
        assert marker_module.verify_marker(
            without_test_proof,
            secret=secret,
            release="2026.09.07.29",
            checkout_url="https://uvarsi.lemonsqueezy.com/checkout/buy/live-product",
            store_id="123",
            variant_id="456",
        ) is False


def test_marker_signature_uses_canonical_hmac_sha256():
    marker_module = _load_marker_module()
    secret = "secret"
    unsigned = marker_module.create_marker(
        release="r1",
        live_config_digest=marker_module.live_config_fingerprint(
            secret=secret,
            checkout_url="https://uvarsi.lemonsqueezy.com/checkout/buy/live",
            store_id="s1",
            variant_id="v1",
        ),
        test_store_id="ts1",
        test_variant_id="tv1",
        completed_at="2026-09-07T20:15:00+00:00",
        receipt_email_verified=True,
        test_mode_verified=True,
    )
    signed = marker_module.sign_marker(unsigned, secret=secret)
    payload = json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode()

    assert hmac.compare_digest(
        signed["signature"], hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
    )


def test_server_reads_only_a_local_release_bound_smoke_marker():
    source = (ROOT / "app" / "server.py").read_text(encoding="utf-8")

    assert "UVARSI_PAYMENT_SMOKE_MARKER" in source
    assert "verify_marker" in source
    assert "UVARSI_PAYMENT_SMOKE_SIGNING_SECRET" in source
    assert "payment_smoke_missing" not in source.split(
        "def _payment_smoke_verified", 1
    )[1].split("def _recipe_gate_ready", 1)[0]


def test_test_checkout_is_rejected_before_url_is_returned_when_variant_is_live():
    smoke = _load_smoke_module()
    calls = []

    def provider_request(_api_key, path, **kwargs):
        calls.append((path, kwargs.get("method", "GET")))
        return {
            "data": {
                "type": "variants",
                "id": "456",
                "attributes": {
                    "test_mode": False,
                    "status": "published",
                    "price": 3900,
                    "is_subscription": False,
                    "product_id": 77,
                },
            }
        }

    with pytest.raises(smoke.SmokeFailed, match="testovacom režime"):
        smoke._verified_test_variant(
            "test-api-key", store_id="123", variant_id="456",
            request=provider_request,
        )

    assert calls == [("/v1/variants/456", "GET")]


def test_variant_store_is_verified_through_its_product_not_a_fake_variant_field():
    smoke = _load_smoke_module()
    calls = []

    def provider_request(_api_key, path, **kwargs):
        calls.append((path, kwargs.get("method", "GET")))
        if path == "/v1/variants/456":
            return {
                "data": {
                    "type": "variants",
                    "id": "456",
                    "attributes": {
                        "product_id": 77,
                        "test_mode": True,
                        "status": "published",
                        "price": 3900,
                        "is_subscription": False,
                    },
                }
            }
        if path == "/v1/products/77":
            return {
                "data": {
                    "type": "products",
                    "id": "77",
                    "attributes": {
                        "store_id": 999,
                        "test_mode": True,
                        "status": "published",
                        "buy_now_url": "https://uvarsi.lemonsqueezy.com/buy/founder",
                    },
                }
            }
        raise AssertionError(path)

    with pytest.raises(smoke.SmokeFailed, match="inému obchodu"):
        smoke._verified_test_variant(
            "test-api-key",
            store_id="123",
            variant_id="456",
            request=provider_request,
        )

    assert calls == [
        ("/v1/variants/456", "GET"),
        ("/v1/products/77", "GET"),
    ]


def test_live_checkout_is_bound_to_provider_product_and_its_only_variant():
    smoke = _load_smoke_module()
    calls = []
    checkout_url = "https://uvarsi.lemonsqueezy.com/buy/founder"

    def provider_request(_api_key, path, **kwargs):
        calls.append(path)
        if path == "/v1/variants/456":
            return {
                "data": {
                    "type": "variants",
                    "id": "456",
                    "attributes": {
                        "product_id": 77,
                        "test_mode": False,
                        "status": "published",
                        "price": 3900,
                        "is_subscription": False,
                    },
                }
            }
        if path == "/v1/products/77":
            return {
                "data": {
                    "type": "products",
                    "id": "77",
                    "attributes": {
                        "store_id": 123,
                        "test_mode": False,
                        "status": "published",
                        "buy_now_url": checkout_url,
                    },
                }
            }
        if path == "/v1/variants?filter%5Bproduct_id%5D=77":
            return {
                "data": [{
                    "type": "variants",
                    "id": "456",
                    "attributes": {
                        "product_id": 77,
                        "test_mode": False,
                        "status": "published",
                    },
                }]
            }
        raise AssertionError(path)

    result = smoke._verified_live_configuration(
        "live-api-key",
        store_id="123",
        variant_id="456",
        checkout_url=checkout_url,
        request=provider_request,
    )

    assert result["product"]["id"] == "77"
    assert calls == [
        "/v1/variants/456",
        "/v1/products/77",
        "/v1/variants?filter%5Bproduct_id%5D=77",
    ]


@pytest.mark.parametrize(
    "configured_url, extra_variant",
    [
        ("https://uvarsi.lemonsqueezy.com/buy/iny", False),
        ("https://uvarsi.lemonsqueezy.com/buy/founder", True),
    ],
)
def test_live_checkout_rejects_wrong_url_or_an_ambiguous_multi_variant_product(
    configured_url, extra_variant
):
    smoke = _load_smoke_module()
    provider_url = "https://uvarsi.lemonsqueezy.com/buy/founder"

    def provider_request(_api_key, path, **_kwargs):
        if path == "/v1/variants/456":
            return {"data": {"type": "variants", "id": "456", "attributes": {
                "product_id": 77, "test_mode": False, "status": "published",
                "price": 3900, "is_subscription": False,
            }}}
        if path == "/v1/products/77":
            return {"data": {"type": "products", "id": "77", "attributes": {
                "store_id": 123, "test_mode": False, "status": "published",
                "buy_now_url": provider_url,
            }}}
        if path == "/v1/variants?filter%5Bproduct_id%5D=77":
            variants = [{"type": "variants", "id": "456", "attributes": {
                "product_id": 77, "test_mode": False, "status": "published",
            }}]
            if extra_variant:
                variants.append({"type": "variants", "id": "789", "attributes": {
                    "product_id": 77, "test_mode": False, "status": "published",
                }})
            return {"data": variants}
        raise AssertionError(path)

    with pytest.raises(smoke.SmokeFailed, match="poklad|variant"):
        smoke._verified_live_configuration(
            "live-api-key",
            store_id="123",
            variant_id="456",
            checkout_url=configured_url,
            request=provider_request,
        )


def test_test_checkout_url_comes_from_provider_confirmed_test_checkout():
    smoke = _load_smoke_module()
    payloads = []

    def provider_request(_api_key, path, **kwargs):
        payloads.append((path, kwargs))
        return {
            "data": {
                "type": "checkouts",
                "id": "checkout-1",
                "attributes": {
                    "store_id": 123,
                    "variant_id": 456,
                    "test_mode": True,
                    "url": "https://example.lemonsqueezy.com/checkout/custom/test",
                },
            }
        }

    url = smoke._create_verified_test_checkout(
        "test-api-key",
        store_id="123",
        variant_id="456",
        user_id=7,
        attempt_id="attempt-token",
        email="test@example.com",
        request=provider_request,
    )

    assert url == "https://example.lemonsqueezy.com/checkout/custom/test"
    path, request_args = payloads[0]
    assert path == "/v1/checkouts"
    assert request_args["method"] == "POST"
    body = request_args["payload"]["data"]
    assert body["attributes"]["test_mode"] is True
    assert body["attributes"]["checkout_data"]["custom"] == {
        "user_id": "7",
        "checkout_attempt": "attempt-token",
    }


def test_smoke_tool_requires_explicit_receipt_confirmation_and_never_reconciles():
    source = SMOKE_SCRIPT.read_text(encoding="utf-8")

    assert "POTVRDZUJEM" in source
    assert "receipt_email_verified=True" in source
    assert "spracuj_odlozene(" not in source
    assert "rekonciluj(" not in source
    assert "LEMON_TEST_API_KEY" in source
    assert "LEMON_TEST_WEBHOOK_SECRET" in source
    assert "LEMON_TEST_STORE_ID" in source
    assert "LEMON_TEST_VARIANT_ID" in source


def test_invalid_exact_smoke_signature_fails_cleanly_without_traceback():
    smoke = _load_smoke_module()

    class FakeConnection:
        def close(self):
            pass

    class FakeServer:
        @staticmethod
        def db():
            return FakeConnection()

    class FakePayments:
        class UdalostNepouzitelna(ValueError):
            pass

        @staticmethod
        def spracuj_odlozene_pre_smoke(*_args, **_kwargs):
            raise FakePayments.UdalostNepouzitelna("citlivý interný detail")

    with pytest.raises(smoke.SmokeFailed, match="[Pp]odpis") as error:
        smoke._wait_for_signed_webhook(
            FakeServer,
            FakePayments,
            secret="test-secret",
            variant_id="variant-1",
            order_id="order-1",
            event_type="order_created",
            timeout_seconds=1,
        )

    assert "citlivý interný detail" not in str(error.value)


def test_marker_can_only_be_built_from_exact_signed_webhook_lifecycle():
    smoke = _load_smoke_module()
    marker_module = _load_marker_module()
    common = {
        "release": "release-1",
        "live_checkout_url": "https://uvarsi.lemonsqueezy.com/checkout/buy/live",
        "live_store_id": "live-store",
        "live_variant_id": "live-variant",
        "test_store_id": "test-store",
        "test_variant_id": "test-variant",
        "order_id": "test-order",
        "receipt_email_verified": True,
        "unresolved_cases": 0,
        "completed_at": "2026-09-08T10:00:00+00:00",
        "signing_secret": "local-marker-secret",
        "create_marker": marker_module.create_marker,
        "live_config_fingerprint": marker_module.live_config_fingerprint,
        "sign_marker": marker_module.sign_marker,
    }
    purchase = {
        "typ": "order_created",
        "objednavka": "test-order",
        "akcia": "udelene",
        "zdroj": "odlozene",
        "test_mode": True,
    }
    refund = {
        "typ": "order_refunded",
        "objednavka": "test-order",
        "akcia": "vratene",
        "zdroj": "odlozene",
        "test_mode": True,
    }

    marker = smoke._build_completed_marker(
        purchase_event=purchase, refund_event=refund, **common
    )
    assert marker_module.verify_marker(
        marker,
        secret=common["signing_secret"],
        release=common["release"],
        checkout_url=common["live_checkout_url"],
        store_id=common["live_store_id"],
        variant_id=common["live_variant_id"],
    ) is True

    with pytest.raises(smoke.SmokeFailed, match="webhook"):
        smoke._build_completed_marker(
            purchase_event=dict(purchase, zdroj="rekonciliacia"),
            refund_event=refund,
            **common,
        )

    with pytest.raises(smoke.SmokeFailed, match="objednávk"):
        smoke._build_completed_marker(
            purchase_event=purchase,
            refund_event=dict(refund, objednavka="other-order"),
            **common,
        )

    with pytest.raises(smoke.SmokeFailed, match="testovacom režime"):
        smoke._build_completed_marker(
            purchase_event=dict(purchase, test_mode=False),
            refund_event=refund,
            **common,
        )
