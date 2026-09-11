import hashlib
import hmac
import importlib.util
import json
from datetime import datetime, timezone
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
        webhook_secret="live-webhook-secret",
        store_id="123",
        variant_id="456",
        api_key="live-api-key",
    )
    test_fingerprint = marker_module.test_config_fingerprint(
        secret=secret,
        checkout_url="https://uvarsi.lemonsqueezy.com/checkout/test-product",
        webhook_secret="test-webhook-secret",
        store_id="test-123",
        variant_id="test-456",
        api_key="test-api-key",
    )
    marker = marker_module.create_marker(
        release="2026.09.07.29",
        live_config_digest=live_fingerprint,
        test_config_digest=test_fingerprint,
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
        webhook_secret="live-webhook-secret",
        store_id="123",
        variant_id="456",
        api_key="live-api-key",
    ) is False

    marker = marker_module.sign_marker(marker, secret=secret)
    assert marker_module.verify_marker(
        marker,
        secret=secret,
        release="2026.09.07.29",
        checkout_url="https://uvarsi.lemonsqueezy.com/checkout/buy/live-product",
        webhook_secret="live-webhook-secret",
        store_id="123",
        variant_id="456",
        api_key="live-api-key",
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
    assert "test-webhook-secret" not in json.dumps(marker)
    assert "test-api-key" not in json.dumps(marker)
    assert "test-product" not in json.dumps(marker)
    assert "live-webhook-secret" not in json.dumps(marker)
    assert "live-api-key" not in json.dumps(marker)

    for field, wrong in (
        ("release", "2026.09.07.30"),
        ("checkout_url", "https://uvarsi.lemonsqueezy.com/checkout/buy/other"),
        ("webhook_secret", "other-live-webhook-secret"),
        ("store_id", "999"),
        ("variant_id", "999"),
        ("api_key", "other-live-api-key"),
    ):
        values = {
            "release": "2026.09.07.29",
            "checkout_url": "https://uvarsi.lemonsqueezy.com/checkout/buy/live-product",
            "webhook_secret": "live-webhook-secret",
            "store_id": "123",
            "variant_id": "456",
            "api_key": "live-api-key",
        }
        values[field] = wrong
        assert marker_module.verify_marker(marker, secret=secret, **values) is False

    tampered = dict(marker, unresolved_cases=1)
    assert marker_module.verify_marker(
        tampered,
        secret=secret,
        release="2026.09.07.29",
        checkout_url="https://uvarsi.lemonsqueezy.com/checkout/buy/live-product",
        webhook_secret="live-webhook-secret",
        store_id="123",
        variant_id="456",
        api_key="live-api-key",
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
            webhook_secret="live-webhook-secret",
            store_id="123",
            variant_id="456",
            api_key="live-api-key",
        ) is False


def test_marker_signature_uses_canonical_hmac_sha256():
    marker_module = _load_marker_module()
    secret = "secret"
    unsigned = marker_module.create_marker(
        release="r1",
        live_config_digest=marker_module.live_config_fingerprint(
            secret=secret,
            checkout_url="https://uvarsi.lemonsqueezy.com/checkout/buy/live",
            webhook_secret="live-webhook",
            store_id="s1",
            variant_id="v1",
            api_key="live-api",
        ),
        test_config_digest=marker_module.test_config_fingerprint(
            secret=secret,
            checkout_url="https://uvarsi.lemonsqueezy.com/checkout/test",
            webhook_secret="test-webhook",
            store_id="ts1",
            variant_id="tv1",
            api_key="test-api",
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
    assert "LEMON_TEST_CHECKOUT_URL" in source
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
        "live_webhook_secret": "live-webhook-secret",
        "live_store_id": "live-store",
        "live_variant_id": "live-variant",
        "live_api_key": "live-api-key",
        "test_store_id": "test-store",
        "test_variant_id": "test-variant",
        "test_checkout_url": "https://uvarsi.lemonsqueezy.com/checkout/test",
        "test_webhook_secret": "test-webhook-secret",
        "test_api_key": "test-api-key",
        "order_id": "test-order",
        "receipt_email_verified": True,
        "unresolved_cases": 0,
        "completed_at": "2026-09-08T10:00:00+00:00",
        "signing_secret": "local-marker-secret",
        "create_marker": marker_module.create_marker,
        "live_config_fingerprint": marker_module.live_config_fingerprint,
        "test_config_fingerprint": marker_module.test_config_fingerprint,
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
        webhook_secret=common["live_webhook_secret"],
        store_id=common["live_store_id"],
        variant_id=common["live_variant_id"],
        api_key=common["live_api_key"],
    ) is True
    assert marker["test_config_digest"] == marker_module.test_config_fingerprint(
        secret=common["signing_secret"],
        checkout_url=common["test_checkout_url"],
        webhook_secret=common["test_webhook_secret"],
        store_id=common["test_store_id"],
        variant_id=common["test_variant_id"],
        api_key=common["test_api_key"],
    )
    serialized = json.dumps(marker)
    assert common["test_checkout_url"] not in serialized
    assert common["test_webhook_secret"] not in serialized
    assert common["test_api_key"] not in serialized
    assert common["live_webhook_secret"] not in serialized
    assert common["live_api_key"] not in serialized

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


def test_production_activation_builder_requires_fresh_signed_smoke_and_stays_valid():
    smoke = _load_smoke_module()
    marker_module = _load_marker_module()
    config = {
        "release": "release-1",
        "live_checkout_url": "https://uvarsi.lemonsqueezy.com/checkout/buy/live",
        "live_webhook_secret": "live-webhook-secret",
        "live_store_id": "live-store",
        "live_variant_id": "live-variant",
        "live_api_key": "live-api-key",
        "test_checkout_url": "https://uvarsi.lemonsqueezy.com/checkout/test",
        "test_webhook_secret": "test-webhook-secret",
        "test_store_id": "test-store",
        "test_variant_id": "test-variant",
        "test_api_key": "test-api-key",
        "signing_secret": "local-marker-secret",
    }
    test_digest = marker_module.test_config_fingerprint(
        secret=config["signing_secret"],
        checkout_url=config["test_checkout_url"],
        webhook_secret=config["test_webhook_secret"],
        store_id=config["test_store_id"],
        variant_id=config["test_variant_id"],
        api_key=config["test_api_key"],
    )
    marker = marker_module.sign_marker(
        marker_module.create_marker(
            release=config["release"],
            live_config_digest=marker_module.live_config_fingerprint(
                secret=config["signing_secret"],
                checkout_url=config["live_checkout_url"],
                webhook_secret=config["live_webhook_secret"],
                store_id=config["live_store_id"],
                variant_id=config["live_variant_id"],
                api_key=config["live_api_key"],
            ),
            test_config_digest=test_digest,
            test_store_id=config["test_store_id"],
            test_variant_id=config["test_variant_id"],
            completed_at="2026-09-12T10:00:00+00:00",
            receipt_email_verified=True,
            test_mode_verified=True,
        ),
        secret=config["signing_secret"],
    )

    activation = smoke._build_activation_attestation(
        smoke_marker=marker,
        activated_at="2026-09-12T10:30:00+00:00",
        create_activation_attestation=marker_module.create_activation_attestation,
        **config,
    )

    assert marker_module.verify_activation_attestation(
        activation,
        secret=config["signing_secret"],
        release=config["release"],
        checkout_url=config["live_checkout_url"],
        webhook_secret=config["live_webhook_secret"],
        store_id=config["live_store_id"],
        variant_id=config["live_variant_id"],
        api_key=config["live_api_key"],
        test_checkout_url=config["test_checkout_url"],
        test_webhook_secret=config["test_webhook_secret"],
        test_store_id=config["test_store_id"],
        test_variant_id=config["test_variant_id"],
        test_api_key=config["test_api_key"],
    ) is True
    serialized = json.dumps(activation)
    assert config["test_checkout_url"] not in serialized
    assert config["test_webhook_secret"] not in serialized
    assert config["test_api_key"] not in serialized
    assert config["live_webhook_secret"] not in serialized
    assert config["live_api_key"] not in serialized

    with pytest.raises(smoke.SmokeFailed, match="čerstv"):
        smoke._build_activation_attestation(
            smoke_marker=marker,
            activated_at="2026-09-13T10:00:01+00:00",
            create_activation_attestation=marker_module.create_activation_attestation,
            **config,
        )


def test_authorize_activation_command_writes_a_verified_marker_without_network(
        monkeypatch, tmp_path):
    smoke = _load_smoke_module()
    marker_module = _load_marker_module()
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    values = {
        "LEMON_API_KEY": "live-api-key",
        "LEMON_CHECKOUT_URL": "https://uvarsi.lemonsqueezy.com/checkout/buy/live",
        "LEMON_WEBHOOK_SECRET": "live-webhook-secret",
        "LEMON_STORE_ID": "live-store",
        "LEMON_VARIANT_ID": "live-variant",
        "LEMON_TEST_API_KEY": "test-api-key",
        "LEMON_TEST_CHECKOUT_URL": "https://uvarsi.lemonsqueezy.com/checkout/test",
        "LEMON_TEST_WEBHOOK_SECRET": "test-webhook-secret",
        "LEMON_TEST_STORE_ID": "test-store",
        "LEMON_TEST_VARIANT_ID": "test-variant",
        "UVARSI_PAYMENT_SMOKE_SIGNING_SECRET": "marker-secret",
    }
    signed_smoke = marker_module.sign_marker(
        marker_module.create_marker(
            release="release-1",
            live_config_digest=marker_module.live_config_fingerprint(
                secret=values["UVARSI_PAYMENT_SMOKE_SIGNING_SECRET"],
                checkout_url=values["LEMON_CHECKOUT_URL"],
                webhook_secret=values["LEMON_WEBHOOK_SECRET"],
                store_id=values["LEMON_STORE_ID"],
                variant_id=values["LEMON_VARIANT_ID"],
                api_key=values["LEMON_API_KEY"],
            ),
            test_config_digest=marker_module.test_config_fingerprint(
                secret=values["UVARSI_PAYMENT_SMOKE_SIGNING_SECRET"],
                checkout_url=values["LEMON_TEST_CHECKOUT_URL"],
                webhook_secret=values["LEMON_TEST_WEBHOOK_SECRET"],
                store_id=values["LEMON_TEST_STORE_ID"],
                variant_id=values["LEMON_TEST_VARIANT_ID"],
                api_key=values["LEMON_TEST_API_KEY"],
            ),
            test_store_id=values["LEMON_TEST_STORE_ID"],
            test_variant_id=values["LEMON_TEST_VARIANT_ID"],
            completed_at=now,
            receipt_email_verified=True,
            test_mode_verified=True,
        ),
        secret=values["UVARSI_PAYMENT_SMOKE_SIGNING_SECRET"],
    )
    smoke_path = tmp_path / "payment-smoke.json"
    activation_path = tmp_path / "payment-activation.json"
    smoke_path.write_text(json.dumps(signed_smoke), encoding="utf-8")

    class Server:
        @staticmethod
        def release_id():
            return "release-1"

    monkeypatch.setattr(
        smoke,
        "_load_runtime",
        lambda _app_dir: (
            Server,
            object(),
            object(),
            marker_module.create_activation_attestation,
            marker_module.create_marker,
            marker_module.live_config_fingerprint,
            marker_module.sign_marker,
            marker_module.test_config_fingerprint,
        ),
    )
    monkeypatch.setattr(
        smoke, "_env_value", lambda name, **_kwargs: values.get(name, "")
    )
    monkeypatch.setattr(
        smoke,
        "_public_preflight",
        lambda *_args, **_kwargs: pytest.fail("aktivácia nesmie volať sieť"),
    )

    result = smoke.main([
        "--authorize-activation",
        "--marker", str(smoke_path),
        "--activation-marker", str(activation_path),
    ])

    assert result == 0
    activation = json.loads(activation_path.read_text(encoding="utf-8"))
    assert marker_module.verify_activation_attestation(
        activation,
        secret=values["UVARSI_PAYMENT_SMOKE_SIGNING_SECRET"],
        release="release-1",
        checkout_url=values["LEMON_CHECKOUT_URL"],
        webhook_secret=values["LEMON_WEBHOOK_SECRET"],
        store_id=values["LEMON_STORE_ID"],
        variant_id=values["LEMON_VARIANT_ID"],
        api_key=values["LEMON_API_KEY"],
        test_checkout_url=values["LEMON_TEST_CHECKOUT_URL"],
        test_webhook_secret=values["LEMON_TEST_WEBHOOK_SECRET"],
        test_store_id=values["LEMON_TEST_STORE_ID"],
        test_variant_id=values["LEMON_TEST_VARIANT_ID"],
        test_api_key=values["LEMON_TEST_API_KEY"],
    ) is True
    serialized = json.dumps(activation)
    assert values["LEMON_TEST_WEBHOOK_SECRET"] not in serialized
    assert values["LEMON_TEST_API_KEY"] not in serialized
    assert values["LEMON_WEBHOOK_SECRET"] not in serialized
    assert values["LEMON_API_KEY"] not in serialized
