#!/usr/bin/env python3
"""Owner-run annual LemonSqueezy lifecycle smoke for one Uvar.si release.

The script runs on the Uvar.si server with payments publicly disabled. It
verifies the live and test provider economics, authenticates through the normal
password endpoint, validates the complete test-subscription lifecycle recorded
by the app and writes only a signed, privacy-safe release attestation. It never
accepts or stores payment-instrument data or prints provider portal URLs.
"""

from __future__ import annotations

import argparse
import datetime as dt
import getpass
import hmac
import http.cookiejar
import json
import math
import os
import re
from contextlib import closing
from pathlib import Path
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request


DEFAULT_BASE_URL = "https://uvar.si"
DEFAULT_APP_DIR = "/opt/uvarsi/app"
DEFAULT_ENV_FILE = "/opt/uvarsi/uvarsi.env"
DEFAULT_MARKER = "/var/lib/uvarsi/payment-smoke.json"
DEFAULT_ACTIVATION_MARKER = "/var/lib/uvarsi/payment-activation.json"

_REPAIRABLE_SUBSCRIPTION_SMOKE_BLOCKERS = frozenset({
    "subscription_smoke_missing",
    "subscription_smoke_invalid",
    "subscription_smoke_stale",
    "subscription_smoke_incomplete",
    "subscription_smoke_mismatch",
})
_BLOCKER_CODE_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


class SmokeFailed(RuntimeError):
    pass


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _env_value(name: str, *, env_file: str, default="") -> str:
    value = os.environ.get(name)
    if value:
        return value
    try:
        with open(env_file, encoding="utf-8") as source:
            for line in source:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                if line.startswith("export "):
                    line = line[len("export "):]
                key, raw = line.split("=", 1)
                if key.strip() == name:
                    return raw.strip().strip('"').strip("'")
    except OSError:
        pass
    return default


def _json_request(opener, url, *, method="GET", payload=None, headers=None):
    encoded = None
    request_headers = {"Accept": "application/vnd.api+json"}
    request_headers.update(headers or {})
    if payload is not None:
        encoded = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        request_headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        url, data=encoded, headers=request_headers, method=method
    )
    try:
        with opener.open(request, timeout=20) as response:
            return json.loads(response.read().decode("utf-8"))
    except (urllib.error.HTTPError, urllib.error.URLError, OSError,
            UnicodeDecodeError, json.JSONDecodeError) as error:
        raise SmokeFailed(f"HTTP krok zlyhal ({type(error).__name__}).") from None


def _provider_request(api_key, path, *, method="GET", payload=None):
    opener = urllib.request.build_opener(_NoRedirect())
    return _json_request(
        opener,
        f"https://api.lemonsqueezy.com{path}",
        method=method,
        payload=payload,
        headers={
            "Content-Type": "application/vnd.api+json",
            "Authorization": f"Bearer {api_key}",
        },
    )


def _resource(response, *, resource_type: str, resource_id: str | None = None):
    data = response.get("data") if isinstance(response, dict) else None
    if not isinstance(data, dict) or data.get("type") != resource_type:
        raise SmokeFailed("Poskytovateľ vrátil neplatnú odpoveď.")
    if resource_id is not None and str(data.get("id")) != str(resource_id):
        raise SmokeFailed("Poskytovateľ vrátil iný produkt.")
    attributes = data.get("attributes")
    if not isinstance(attributes, dict):
        raise SmokeFailed("Poskytovateľ vrátil neplatné atribúty.")
    return data, attributes


def _verified_variant(
    api_key, *, store_id, variant_id, expected_test_mode,
    request=_provider_request,
):
    response = request(api_key, f"/v1/variants/{variant_id}")
    data, attributes = _resource(
        response, resource_type="variants", resource_id=variant_id
    )
    mode_name = "testovacom" if expected_test_mode else "živom"
    if attributes.get("test_mode") is not expected_test_mode:
        raise SmokeFailed(f"Variant nie je v {mode_name} režime.")
    if attributes.get("status") != "published":
        raise SmokeFailed("Variant nie je zverejnený.")
    if attributes.get("is_subscription") is not False:
        raise SmokeFailed("Variant musí byť jednorazová platba bez obnovovania.")
    if attributes.get("price") != 3900:
        raise SmokeFailed("Variant nemá schválenú cenu 39 €.")
    product_id = attributes.get("product_id")
    if not isinstance(product_id, (str, int)) or not str(product_id).strip():
        raise SmokeFailed("Variant nemá platný produkt.")
    product_id = str(product_id)
    product_response = request(api_key, f"/v1/products/{product_id}")
    product, product_attributes = _resource(
        product_response, resource_type="products", resource_id=product_id
    )
    if product_attributes.get("test_mode") is not expected_test_mode:
        raise SmokeFailed(f"Produkt nie je v {mode_name} režime.")
    if str(product_attributes.get("store_id")) != str(store_id):
        raise SmokeFailed("Variant patrí inému obchodu.")
    if product_attributes.get("status") != "published":
        raise SmokeFailed("Produkt nie je zverejnený.")
    return {
        "variant": data,
        "variant_attributes": attributes,
        "product": product,
        "product_attributes": product_attributes,
    }


def _verified_test_variant(
    api_key, *, store_id, variant_id, request=_provider_request
):
    return _verified_variant(
        api_key,
        store_id=store_id,
        variant_id=variant_id,
        expected_test_mode=True,
        request=request,
    )


def _verified_live_configuration(
    api_key, *, store_id, variant_id, checkout_url, request=_provider_request
):
    verified = _verified_variant(
        api_key,
        store_id=store_id,
        variant_id=variant_id,
        expected_test_mode=False,
        request=request,
    )
    product_id = str(verified["product"]["id"])
    query = urllib.parse.urlencode({"filter[product_id]": product_id})
    response = request(api_key, f"/v1/variants?{query}")
    variants = response.get("data") if isinstance(response, dict) else None
    if not isinstance(variants, list) or len(variants) != 1:
        raise SmokeFailed("Živý produkt musí mať práve jeden variant.")
    listed = variants[0]
    listed_attributes = listed.get("attributes") if isinstance(listed, dict) else None
    if (
        not isinstance(listed, dict)
        or listed.get("type") != "variants"
        or str(listed.get("id")) != str(variant_id)
        or not isinstance(listed_attributes, dict)
        or str(listed_attributes.get("product_id")) != product_id
        or listed_attributes.get("test_mode") is not False
        or listed_attributes.get("status") != "published"
    ):
        raise SmokeFailed("Živý produkt obsahuje neočakávaný variant.")

    provider_url = verified["product_attributes"].get("buy_now_url")
    if not isinstance(checkout_url, str) or not isinstance(provider_url, str):
        raise SmokeFailed("Živá pokladňa nemá platnú adresu.")
    parsed = urllib.parse.urlsplit(checkout_url)
    try:
        port = parsed.port
    except ValueError:
        port = -1
    valid_host = (
        isinstance(parsed.hostname, str)
        and (
            parsed.hostname == "lemonsqueezy.com"
            or parsed.hostname.endswith(".lemonsqueezy.com")
        )
    )
    valid_path = parsed.path.startswith("/buy/") or parsed.path.startswith(
        "/checkout/buy/"
    )
    if (
        parsed.scheme != "https"
        or not valid_host
        or not valid_path
        or parsed.username is not None
        or parsed.password is not None
        or port is not None
        or parsed.query
        or parsed.fragment
        or checkout_url.rstrip("/") != provider_url.rstrip("/")
    ):
        raise SmokeFailed("Nastavená živá pokladňa nepatrí overenému produktu.")
    return verified


def _verified_annual_provider_evidence(
    api_key,
    *,
    config,
    signing_secret,
    request=_provider_request,
    evidence_type,
    fingerprint,
):
    """Read and validate one annual variant and its founder discount."""
    mode_name = "testovacom" if config.test_mode else "živom"
    variant_response = request(api_key, f"/v1/variants/{config.variant_id}")
    variant, variant_attributes = _resource(
        variant_response, resource_type="variants", resource_id=config.variant_id
    )
    if (
        variant_attributes.get("test_mode") is not config.test_mode
        or variant_attributes.get("status") != "published"
        or variant_attributes.get("is_subscription") is not True
        or type(variant_attributes.get("price")) is not int
        or variant_attributes.get("price") != 4_900
        or variant_attributes.get("interval") != "year"
        or type(variant_attributes.get("interval_count")) is not int
        or variant_attributes.get("interval_count") != 1
        or variant_attributes.get("has_free_trial") is not False
    ):
        raise SmokeFailed(
            f"Ročný variant v {mode_name} režime nemá schválenú ekonomiku."
        )
    product_id = variant_attributes.get("product_id")
    if not isinstance(product_id, (str, int)) or not str(product_id).strip():
        raise SmokeFailed("Ročný variant nemá platný produkt.")
    product_id = str(product_id)
    product_response = request(api_key, f"/v1/products/{product_id}")
    _product, product_attributes = _resource(
        product_response, resource_type="products", resource_id=product_id
    )
    if (
        product_attributes.get("test_mode") is not config.test_mode
        or str(product_attributes.get("store_id")) != str(config.store_id)
        or product_attributes.get("status") != "published"
    ):
        raise SmokeFailed("Ročný variant patrí inému alebo nezverejnenému obchodu.")
    store_response = request(api_key, f"/v1/stores/{config.store_id}")
    _store, store_attributes = _resource(
        store_response, resource_type="stores", resource_id=config.store_id
    )
    if store_attributes.get("currency") != "EUR":
        raise SmokeFailed("Ročný variant nie je účtovaný v eurách.")

    query = urllib.parse.urlencode({"filter[product_id]": product_id})
    variants_response = request(api_key, f"/v1/variants?{query}")
    variants = variants_response.get("data") if isinstance(variants_response, dict) else None
    if not isinstance(variants, list) or len(variants) != 1:
        raise SmokeFailed("Ročný produkt musí mať práve jeden variant.")
    listed = variants[0]
    listed_attributes = listed.get("attributes") if isinstance(listed, dict) else None
    if (
        not isinstance(listed, dict)
        or listed.get("type") != "variants"
        or str(listed.get("id")) != str(config.variant_id)
        or not isinstance(listed_attributes, dict)
        or str(listed_attributes.get("product_id")) != product_id
        or listed_attributes.get("test_mode") is not config.test_mode
        or listed_attributes.get("status") != "published"
    ):
        raise SmokeFailed("Ročný produkt obsahuje neočakávaný variant.")

    discount_response = request(api_key, f"/v1/discounts/{config.discount_id}")
    _discount, discount_attributes = _resource(
        discount_response, resource_type="discounts", resource_id=config.discount_id
    )
    related_response = request(
        api_key, f"/v1/discounts/{config.discount_id}/variants"
    )
    related_variants = (
        related_response.get("data") if isinstance(related_response, dict) else None
    )
    variant_ids = tuple(
        str(item.get("id"))
        for item in related_variants
        if isinstance(item, dict) and item.get("type") == "variants"
    ) if isinstance(related_variants, list) else ()
    if (
        discount_attributes.get("test_mode") is not config.test_mode
        or str(discount_attributes.get("store_id")) != str(config.store_id)
        or discount_attributes.get("amount_type") != "fixed"
        or type(discount_attributes.get("amount")) is not int
        or discount_attributes.get("amount") != 1_000
        or discount_attributes.get("duration") != "once"
        or discount_attributes.get("status") != "published"
        or discount_attributes.get("is_limited_redemptions") is not True
        or type(discount_attributes.get("max_redemptions")) is not int
        or discount_attributes.get("max_redemptions") != 50
        or variant_ids != (str(config.variant_id),)
    ):
        raise SmokeFailed("Zakladajúca zľava nemá schválené obmedzenia.")
    provider_code = discount_attributes.get("code")
    if not isinstance(provider_code, str) or not provider_code.strip():
        raise SmokeFailed("Poskytovateľ nevrátil kód zakladajúcej zľavy.")
    provider_code_digest = fingerprint(
        signing_secret=signing_secret,
        discount_code=provider_code,
    )
    configured_code_digest = fingerprint(
        signing_secret=signing_secret,
        discount_code=config.discount_code,
    )
    if not hmac.compare_digest(provider_code_digest, configured_code_digest):
        raise SmokeFailed("Kód zakladajúcej zľavy sa nezhoduje s providerom.")
    return evidence_type(
        store_id=str(config.store_id),
        variant_id=str(config.variant_id),
        discount_id=str(config.discount_id),
        test_mode=config.test_mode,
        annual_price_cents=4_900,
        currency="EUR",
        billing_interval="year",
        billing_interval_count=1,
        trial_days=0,
        variant_status="published",
        discount_kind="fixed",
        discount_amount_cents=1_000,
        discount_duration="once",
        discount_status="published",
        discount_variant_ids=(str(config.variant_id),),
        discount_redemption_limit=50,
        discount_code_fingerprint=provider_code_digest,
    )


def _lifecycle_evidence_from_records(
    *, subscription, invoices, events, portal_access_verified,
    unresolved_cases, evidence_type,
):
    """Derive lifecycle proof only from processed test-mode server records."""
    if type(unresolved_cases) is not int or unresolved_cases != 0:
        raise SmokeFailed("Po teste ostal nevyriešený platobný prípad.")
    if (
        not isinstance(subscription, dict)
        or subscription.get("test_mode") != 1
        or subscription.get("status") != "expired"
        or type(subscription.get("initial_amount_cents")) is not int
        or subscription.get("initial_amount_cents") != 3_900
        or type(subscription.get("renewal_amount_cents")) is not int
        or subscription.get("renewal_amount_cents") != 4_900
        or subscription.get("founder") != 1
        or subscription.get("initial_payment_verified") != 1
        or subscription.get("needs_review") != 0
        or portal_access_verified is not True
    ):
        raise SmokeFailed("Lokálny lifecycle predplatného nie je úplný.")
    if not isinstance(invoices, (list, tuple)) or not isinstance(events, (list, tuple)):
        raise SmokeFailed("Lokálny lifecycle predplatného nie je úplný.")
    initial_invoice = any(
        isinstance(invoice, dict)
        and invoice.get("invoice_kind") == "initial"
        and invoice.get("status") in {"paid", "refunded"}
        and type(invoice.get("amount_cents")) is int
        and invoice.get("amount_cents") == 3_900
        for invoice in invoices
    )
    renewal_invoice = any(
        isinstance(invoice, dict)
        and invoice.get("invoice_kind") == "renewal"
        and invoice.get("status") in {"paid", "refunded"}
        and type(invoice.get("amount_cents")) is int
        and invoice.get("amount_cents") == 4_900
        for invoice in invoices
    )
    refunded_invoice = any(
        isinstance(invoice, dict)
        and invoice.get("status") == "refunded"
        and type(invoice.get("amount_cents")) is int
        and type(invoice.get("refunded_amount_cents")) is int
        and invoice.get("amount_cents") > 0
        and invoice.get("refunded_amount_cents") == invoice.get("amount_cents")
        for invoice in invoices
    )
    clean_events = {
        event.get("event_type")
        for event in events
        if isinstance(event, dict)
        and event.get("processing_status") == "processed"
        and event.get("needs_review") == 0
        and event.get("source") in {"webhook", "odlozene"}
    }
    required_events = {
        "subscription_created",
        "subscription_payment_success",
        "subscription_payment_failed",
        "subscription_payment_recovered",
        "subscription_cancelled",
        "subscription_expired",
        "subscription_payment_refunded",
    }
    paid_through = subscription.get("paid_through")
    if (
        type(paid_through) not in {int, float}
        or not math.isfinite(paid_through)
        or paid_through <= 0
    ):
        raise SmokeFailed("Lokálny lifecycle predplatného nie je úplný.")

    def processed_time(event_type):
        values = [
            event.get("processed_at")
            for event in events
            if isinstance(event, dict)
            and event.get("event_type") == event_type
            and event.get("source") in {"webhook", "odlozene"}
            and event.get("processing_status") == "processed"
            and event.get("needs_review") == 0
        ]
        if not values or any(
            type(value) not in {int, float}
            or not math.isfinite(value)
            or value <= 0
            for value in values
        ):
            raise SmokeFailed("Lokálny lifecycle predplatného nie je úplný.")
        return max(values)

    cancelled_at = processed_time("subscription_cancelled")
    expired_at = processed_time("subscription_expired")
    failed_at = processed_time("subscription_payment_failed")
    recovered_at = processed_time("subscription_payment_recovered")
    reconciliation_verified = any(
        isinstance(event, dict)
        and event.get("processing_status") == "processed"
        and event.get("needs_review") == 0
        and event.get("source") in {"reconciliation", "rekonciliacia"}
        for event in events
    )
    if (
        not initial_invoice
        or not renewal_invoice
        or not refunded_invoice
        or not required_events <= clean_events
        or not reconciliation_verified
        or cancelled_at >= paid_through
        or expired_at < paid_through
        or recovered_at <= failed_at
    ):
        raise SmokeFailed("Lokálny lifecycle predplatného nie je úplný.")
    return evidence_type(
        initial_charge_cents=3_900,
        renewal_displayed_cents=4_900,
        activation_verified=True,
        renewal_invoice_cents=4_900,
        failed_payment_verified=True,
        recovery_verified=True,
        cancellation_verified=True,
        access_retained_until_period_end=True,
        expiration_verified=True,
        refund_verified=True,
        portal_access_verified=True,
        webhook_signature_verified=True,
        reconciliation_verified=True,
    )


def _build_annual_subscription_marker(
    *, expectation, live_provider, test_provider, lifecycle, completed_at,
    create_marker, sign_marker,
):
    """Create one short-lived signed marker from already verified evidence."""
    if not isinstance(completed_at, dt.datetime) or completed_at.utcoffset() is None:
        raise SmokeFailed("Čas annual smoke dôkazu nie je dôveryhodný.")
    completed_at = completed_at.astimezone(dt.timezone.utc).replace(microsecond=0)
    try:
        unsigned = create_marker(
            expectation=expectation,
            live_provider=live_provider,
            test_provider=test_provider,
            lifecycle=lifecycle,
            completed_at=completed_at.isoformat(),
            expires_at=(completed_at + dt.timedelta(hours=24)).isoformat(),
        )
        return sign_marker(unsigned, secret=expectation.signing_secret)
    except (TypeError, ValueError):
        raise SmokeFailed("Annual smoke dôkaz sa nedá bezpečne podpísať.") from None


def _build_schema5_subscription_marker(
    *, expectation, live_provider, test_provider, annual_commercial,
    probe_facts, completed_at, marker_module,
):
    """Sign annual commerce plus daily webhook mechanics without conflating them."""
    if not isinstance(completed_at, dt.datetime) or completed_at.utcoffset() is None:
        raise SmokeFailed("Čas schema-5 smoke dôkazu nie je dôveryhodný.")
    if not isinstance(probe_facts, dict):
        raise SmokeFailed("Denný probe neposkytol bezpečné schema-5 fakty.")
    try:
        provider = marker_module.DailyProbeProviderEvidence(
            **probe_facts["provider"]
        )
        lifecycle = marker_module.DailyProbeLifecycleEvidence(
            **probe_facts["lifecycle"]
        )
        completed = completed_at.astimezone(dt.timezone.utc).replace(microsecond=0)
        unsigned = marker_module.create_subscription_marker(
            expectation=expectation,
            live_provider=live_provider,
            test_provider=test_provider,
            annual_commercial=annual_commercial,
            probe_provider=provider,
            probe_lifecycle=lifecycle,
            completed_at=completed.isoformat(),
            expires_at=(completed + dt.timedelta(hours=24)).isoformat(),
        )
        return marker_module.sign_marker(
            unsigned, secret=expectation.signing_secret
        )
    except (KeyError, TypeError, ValueError):
        raise SmokeFailed("Schema-5 smoke dôkaz sa nedá bezpečne podpísať.") from None


def _create_verified_test_checkout(
    api_key, *, store_id, variant_id, user_id, attempt_id, email,
    request=_provider_request,
):
    payload = {
        "data": {
            "type": "checkouts",
            "attributes": {
                "test_mode": True,
                "checkout_data": {
                    "email": email,
                    "custom": {
                        "user_id": str(user_id),
                        "checkout_attempt": str(attempt_id),
                    },
                },
            },
            "relationships": {
                "store": {"data": {"type": "stores", "id": str(store_id)}},
                "variant": {
                    "data": {"type": "variants", "id": str(variant_id)}
                },
            },
        }
    }
    response = request(
        api_key, "/v1/checkouts", method="POST", payload=payload
    )
    _data, attributes = _resource(response, resource_type="checkouts")
    checkout_url = attributes.get("url")
    parsed = urllib.parse.urlsplit(checkout_url if isinstance(checkout_url, str) else "")
    valid_host = (
        parsed.scheme == "https"
        and isinstance(parsed.hostname, str)
        and (
            parsed.hostname == "lemonsqueezy.com"
            or parsed.hostname.endswith(".lemonsqueezy.com")
        )
    )
    if (
        attributes.get("test_mode") is not True
        or str(attributes.get("store_id")) != str(store_id)
        or str(attributes.get("variant_id")) != str(variant_id)
        or not valid_host
        or not parsed.path.startswith("/checkout/")
    ):
        raise SmokeFailed("Poskytovateľ nepotvrdil bezpečnú testovaciu pokladňu.")
    return checkout_url


def _authenticated_opener(base_url: str, email: str, password: str):
    cookies = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(
        _NoRedirect(), urllib.request.HTTPCookieProcessor(cookies)
    )
    result = _json_request(
        opener,
        f"{base_url}/api/auth/login",
        method="POST",
        payload={"email": email, "password": password,
                 "device_name": "Platobný smoke"},
        headers={"Origin": base_url},
    )
    if result.get("ok") is not True or not list(cookies):
        raise SmokeFailed("Testovací účet sa nepodarilo autentifikovať.")
    return opener


def _payment_status(opener, base_url: str) -> dict:
    result = _json_request(opener, f"{base_url}/api/platba/stav")
    if not isinstance(result.get("ma_narok"), bool):
        raise SmokeFailed("Autentifikovaný stav platby má neplatný tvar.")
    return result


def _validated_public_readiness(health: dict, expected_release: str) -> list[str]:
    readiness = health.get("payment_readiness")
    required_fields = {"ready", "blockers", "legal_version", "release"}
    if not isinstance(readiness, dict) or not required_fields.issubset(readiness):
        raise SmokeFailed("Verejný stav má neplatný tvar pripravenosti.")

    ready = readiness["ready"]
    blockers = readiness["blockers"]
    legal_version = readiness["legal_version"]
    release = readiness["release"]
    blockers_valid = (
        isinstance(blockers, list)
        and all(
            type(blocker) is str and _BLOCKER_CODE_PATTERN.fullmatch(blocker)
            for blocker in blockers
        )
        and len(blockers) == len(set(blockers))
    )
    readiness_consistent = (
        (ready is True and not blockers)
        or (ready is False and bool(blockers))
    )
    if (
        type(ready) is not bool
        or not blockers_valid
        or type(legal_version) is not str
        or not legal_version.strip()
        or type(release) is not str
        or release != expected_release
        or not readiness_consistent
    ):
        raise SmokeFailed("Verejný stav má neplatný tvar pripravenosti.")
    return blockers


def _public_preflight(base_url: str, expected_release: str) -> dict:
    opener = urllib.request.build_opener(_NoRedirect())
    health = _json_request(opener, f"{base_url}/api/health")
    if not isinstance(health, dict):
        raise SmokeFailed("Verejný stav má neplatný tvar pripravenosti.")
    if health.get("vydanie") != expected_release:
        raise SmokeFailed("Živá aplikácia nemá očakávané vydanie.")
    recipe = health.get("recipe_engine") or {}
    if recipe.get("payments_enabled") is not False:
        raise SmokeFailed("Verejné platby musia počas testu zostať vypnuté.")
    blockers = _validated_public_readiness(health, expected_release)
    if blockers and (
        len(blockers) != 1
        or blockers[0] not in _REPAIRABLE_SUBSCRIPTION_SMOKE_BLOCKERS
    ):
        raise SmokeFailed("Pred testom ostávajú iné blokátory pripravenosti.")
    return health


def _load_runtime(app_dir: str):
    if app_dir not in sys.path:
        sys.path.insert(0, app_dir)
    import server
    from payment_smoke_marker import (
        AnnualProviderEvidence,
        SubscriptionConfig,
        SubscriptionLifecycleEvidence,
        SubscriptionMarkerExpectation,
        create_subscription_activation_attestation,
        create_subscription_marker,
        discount_code_fingerprint,
        sign_marker,
    )
    return (
        server,
        AnnualProviderEvidence,
        SubscriptionConfig,
        SubscriptionLifecycleEvidence,
        SubscriptionMarkerExpectation,
        create_subscription_activation_attestation,
        create_subscription_marker,
        discount_code_fingerprint,
        sign_marker,
    )


def _load_schema5_runtime(app_dir: str):
    if app_dir not in sys.path:
        sys.path.insert(0, app_dir)
    try:
        from app import payment_smoke_marker as marker_module
        from app import subscription_lifecycle_probe as lifecycle_module
    except ImportError:
        import payment_smoke_marker as marker_module
        import subscription_lifecycle_probe as lifecycle_module
    return marker_module, lifecycle_module


def _with_probe_expectation(
    expectation, *, env_file, expectation_type, lifecycle_module,
):
    """Attach the isolated daily Test-mode probe to the annual expectation."""
    if getattr(expectation, "probe", None) is not None:
        return expectation
    try:
        probe = lifecycle_module.read_probe_config(
            lambda name, default="": _env_value(
                name, env_file=env_file, default=default
            ),
            annual_test_variant_id=expectation.test.variant_id,
            live_webhook_secret=expectation.live.webhook_secret,
            annual_test_webhook_secret=expectation.test.webhook_secret,
        )
    except lifecycle_module.ProbeConfigError as error:
        raise SmokeFailed(str(error)) from None
    return expectation_type(
        release=expectation.release,
        live=expectation.live,
        test=expectation.test,
        signing_secret=expectation.signing_secret,
        probe=probe,
    )


def _annual_expectation_from_env(
    *, release, env_file, config_type, expectation_type,
):
    def mode_config(test_mode: bool):
        prefix = "LEMON_TEST_" if test_mode else "LEMON_"
        values = {
            "api_key": _env_value(f"{prefix}API_KEY", env_file=env_file),
            "store_id": _env_value(f"{prefix}STORE_ID", env_file=env_file),
            "variant_id": _env_value(
                f"{prefix}SUBSCRIPTION_VARIANT_ID", env_file=env_file
            ),
            "discount_id": _env_value(
                f"{prefix}FOUNDER_DISCOUNT_ID", env_file=env_file
            ),
            "discount_code": _env_value(
                f"{prefix}FOUNDER_DISCOUNT_CODE", env_file=env_file
            ),
            "webhook_secret": _env_value(
                f"{prefix}WEBHOOK_SECRET", env_file=env_file
            ),
        }
        if not all(values.values()):
            label = "testovacej" if test_mode else "živej"
            raise SmokeFailed(
                f"Chýba časť {label} konfigurácie ročného predplatného."
            )
        return config_type(test_mode=test_mode, **values)

    signing_secret = _env_value(
        "UVARSI_PAYMENT_SMOKE_SIGNING_SECRET", env_file=env_file
    )
    if not signing_secret:
        raise SmokeFailed("Chýba podpisové tajomstvo smoke dôkazu.")
    return expectation_type(
        release=release,
        live=mode_config(False),
        test=mode_config(True),
        signing_secret=signing_secret,
    )


def _load_annual_lifecycle_records(server, *, email, test_config):
    """Load only the named account's matching test subscription evidence."""
    normalized = server.normalize_email(email)
    with closing(server.db()) as con:
        user = con.execute(
            "SELECT id FROM pouzivatelia WHERE email=?", (normalized,)
        ).fetchone()
        if user is None:
            raise SmokeFailed("Testovací účet v databáze neexistuje.")
        user_id = int(user[0])
        row = con.execute(
            """SELECT * FROM subscriptions
                 WHERE user_id=? AND provider='lemonsqueezy' AND test_mode=1
                   AND provider_variant_id=? AND discount_id=?""",
            (user_id, test_config.variant_id, test_config.discount_id),
        ).fetchone()
        if row is None:
            raise SmokeFailed(
                "Účet nemá lifecycle pre presný testovací variant a zľavu."
            )
        subscription = dict(row)
        subscription_id = subscription.get("provider_subscription_id")
        if not isinstance(subscription_id, str) or not subscription_id:
            raise SmokeFailed("Testovacie predplatné nemá bezpečný identifikátor.")
        checkout = con.execute(
            """SELECT 1 FROM checkout_attempts
                 WHERE user_id=? AND test_mode=1 AND status='paid'
                   AND provider_checkout_id IS NOT NULL
                   AND provider_order_id=?
                   AND amount_cents=3900 AND renewal_amount_cents=4900
                   AND currency='EUR' AND billing_interval='year'
                   AND auto_renews=1 AND founder=1
                 LIMIT 1""",
            (user_id, subscription.get("provider_order_id")),
        ).fetchone()
        subscription["_smoke_checkout_verified"] = checkout is not None
        invoices = [
            dict(item) for item in con.execute(
                """SELECT * FROM subscription_invoices
                     WHERE provider='lemonsqueezy' AND test_mode=1
                       AND provider_subscription_id=?""",
                (subscription_id,),
            ).fetchall()
        ]
        events = [
            dict(item) for item in con.execute(
                """SELECT * FROM subscription_events
                     WHERE provider='lemonsqueezy' AND test_mode=1
                       AND provider_subscription_id=?""",
                (subscription_id,),
            ).fetchall()
        ]
        open_cases = int(con.execute(
            "SELECT COUNT(*) FROM payment_cases WHERE user_id=? AND status='open'",
            (user_id,),
        ).fetchone()[0])
        unsafe_events = sum(
            1 for event in events
            if event.get("processing_status") != "processed"
            or event.get("needs_review") != 0
        )
    return subscription, invoices, events, open_cases + unsafe_events, subscription_id


def _annual_commercial_evidence_from_records(
    *, subscription, invoices, events, portal_access_verified,
    unresolved_cases, config, evidence_type, reconciliation_event_key, now,
):
    """Prove annual commerce without pretending that a year already elapsed."""
    if type(unresolved_cases) is not int or unresolved_cases != 0:
        raise SmokeFailed("Po teste ostal nevyriešený platobný prípad.")
    if not isinstance(subscription, dict) or not isinstance(invoices, (list, tuple)):
        raise SmokeFailed("Ročný testovací nákup nie je úplný.")
    if not isinstance(events, (list, tuple)):
        raise SmokeFailed("Ročný testovací nákup nie je úplný.")
    if not (
        subscription.get("_smoke_checkout_verified") is True
        and subscription.get("test_mode") == 1
        and subscription.get("founder") == 1
        and subscription.get("initial_amount_cents") == 3_900
        and subscription.get("renewal_amount_cents") == 4_900
        and subscription.get("currency") == "EUR"
        and subscription.get("provider_variant_id") == config.variant_id
        and subscription.get("discount_id") == config.discount_id
        and subscription.get("initial_payment_verified") == 1
        and subscription.get("needs_review") == 0
        and portal_access_verified is True
    ):
        raise SmokeFailed("Ročný testovací nákup nezodpovedá ponuke 39/49 EUR.")
    initial = next(
        (
            invoice for invoice in invoices
            if isinstance(invoice, dict)
            and invoice.get("invoice_kind") == "initial"
            and invoice.get("amount_cents") == 3_900
            and invoice.get("currency") == "EUR"
            and invoice.get("status") == "refunded"
            and invoice.get("refunded_amount_cents") == 3_900
        ),
        None,
    )
    clean_events = {
        event.get("event_type")
        for event in events
        if isinstance(event, dict)
        and event.get("processing_status") == "processed"
        and event.get("needs_review") == 0
        and event.get("source") in {"webhook", "odlozene", "reconciliation", "rekonciliacia"}
    }
    required = {
        "subscription_created",
        "subscription_payment_success",
        "subscription_cancelled",
        "subscription_resumed",
    }
    refund_verified = bool(
        initial is not None
        and {"subscription_payment_refunded", "order_refunded"} & clean_events
    )
    if isinstance(now, bool) or not isinstance(now, (int, float)) or not math.isfinite(float(now)):
        raise SmokeFailed("Ročný testovací lifecycle nemá platný čas kontroly.")
    now = float(now)
    reconciliation_verified = any(
        isinstance(event, dict)
        and event.get("event_key") == reconciliation_event_key
        and event.get("event_type") == "annual_reconciliation_verified"
        and event.get("processing_status") == "processed"
        and event.get("needs_review") == 0
        and event.get("source") == "reconciliation_probe"
        and type(event.get("processed_at")) in {int, float}
        and now - 24 * 60 * 60 <= float(event["processed_at"]) <= now + 5 * 60
        for event in events
    )
    if not (
        required <= clean_events
        and refund_verified
        and reconciliation_verified
    ):
        raise SmokeFailed("Ročný testovací lifecycle nie je úplný.")
    return evidence_type(
        founder_initial_cents=3_900,
        standard_and_renewal_cents=4_900,
        currency="EUR",
        billing_interval="year",
        billing_interval_count=1,
        annual_test_checkout_verified=True,
        annual_test_initial_payment_verified=True,
        annual_renewal_terms_verified=True,
        annual_domain_cancellation_verified=True,
        annual_domain_refund_verified=True,
        portal_access_verified=True,
        reconciliation_verified=True,
    )


def _load_daily_probe_facts(server, *, expectation, lifecycle_module):
    if getattr(expectation, "probe", None) is None:
        raise SmokeFailed("Chýba oddelený denný lifecycle probe.")
    try:
        with closing(server.db()) as con:
            digest = lifecycle_module.matching_probe_run_digest(
                con,
                config=expectation.probe,
                signing_secret=expectation.signing_secret,
                states=("ready",),
            )
            return lifecycle_module.probe_marker_facts_by_digest(
                con,
                token_digest=digest,
                config=expectation.probe,
                signing_secret=expectation.signing_secret,
            )
    except (
        lifecycle_module.ProbeConfigError,
        lifecycle_module.ProbeEventRejected,
    ) as error:
        raise SmokeFailed(str(error)) from None


def _verified_test_portal_access(
    api_key, *, subscription_id, variant_id, request=_provider_request,
) -> bool:
    response = request(api_key, f"/v1/subscriptions/{subscription_id}")
    _subscription, attributes = _resource(
        response, resource_type="subscriptions", resource_id=subscription_id
    )
    urls = attributes.get("urls")
    portal_url = urls.get("customer_portal") if isinstance(urls, dict) else None
    try:
        parsed = urllib.parse.urlsplit(portal_url)
        port = parsed.port
    except (TypeError, ValueError):
        raise SmokeFailed("Provider nepotvrdil bezpečný testovací portál.") from None
    valid_host = isinstance(parsed.hostname, str) and (
        parsed.hostname == "lemonsqueezy.com"
        or parsed.hostname.endswith(".lemonsqueezy.com")
    )
    if (
        attributes.get("test_mode") is not True
        or str(attributes.get("variant_id")) != str(variant_id)
        or parsed.scheme != "https"
        or not valid_host
        or parsed.username is not None
        or parsed.password is not None
        or port is not None
        or not parsed.path
    ):
        raise SmokeFailed("Provider nepotvrdil bezpečný testovací portál.")
    return True


def _verified_app_portal_access(opener, base_url: str) -> bool:
    """Exercise the authenticated Uvar.si portal endpoint without leaking its URL."""
    result = _json_request(
        opener,
        f"{base_url.rstrip('/')}/api/platba/portal",
        method="POST",
        headers={"Origin": base_url.rstrip("/")},
    )
    portal_url = result.get("url") if isinstance(result, dict) else None
    try:
        parsed = urllib.parse.urlsplit(portal_url)
        port = parsed.port
    except (TypeError, ValueError):
        raise SmokeFailed("Aplikácia nevrátila bezpečný testovací portál.") from None
    valid_host = isinstance(parsed.hostname, str) and (
        parsed.hostname == "lemonsqueezy.com"
        or parsed.hostname.endswith(".lemonsqueezy.com")
    )
    valid = bool(
        parsed.scheme == "https"
        and valid_host
        and parsed.username is None
        and parsed.password is None
        and port is None
        and parsed.path
        and parsed.fragment == ""
    )
    del portal_url
    if not valid:
        raise SmokeFailed("Aplikácia nevrátila bezpečný testovací portál.")
    return True


def _prepare_checkout(server, platby, *, email: str):
    normalized = server.normalize_email(email)
    with closing(server.db()) as con:
        row = con.execute(
            "SELECT id FROM pouzivatelia WHERE email=?", (normalized,)
        ).fetchone()
        if row is None:
            raise SmokeFailed("Testovací účet v databáze neexistuje.")
        user_id = int(row[0])
        if platby.ma_narok(con, user_id):
            raise SmokeFailed("Testovací účet už má Premium; použi čistý účet.")
        attempt = platby.create_checkout_attempt(
            con, user_id=user_id, legal_version=server.LEGAL_VERSION,
            now=time.time(),
        )
        con.commit()
    return user_id, attempt


def _matching_test_order(rekonciliacia, *, api_key, store_id, variant_id,
                         attempt_id):
    orders = rekonciliacia.stiahni_objednavky(
        api_key, store_id=store_id, max_stran=2
    )
    matches = []
    for order in orders:
        attributes = order.get("attributes") if isinstance(order, dict) else None
        attributes = attributes if isinstance(attributes, dict) else {}
        item = attributes.get("first_order_item")
        item = item if isinstance(item, dict) else {}
        if rekonciliacia.platby.checkout_attempt_z_objednavky(order) != attempt_id:
            continue
        if attributes.get("test_mode") is not True:
            raise SmokeFailed("Objednávka nie je v testovacom režime.")
        if str(attributes.get("store_id")) != str(store_id):
            raise SmokeFailed("Objednávka patrí inému obchodu.")
        if str(item.get("variant_id")) != str(variant_id):
            raise SmokeFailed("Objednávka patrí inému variantu.")
        matches.append(order)
    if len(matches) > 1:
        raise SmokeFailed("K jednému pokusu vzniklo viac objednávok.")
    return matches[0] if matches else None


def _wait_for_signed_webhook(
    server, platby, *, secret, variant_id, order_id, event_type,
    timeout_seconds,
):
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        with closing(server.db()) as con:
            try:
                result = platby.spracuj_odlozene_pre_smoke(
                    con,
                    tajomstvo=secret,
                    now=time.time(),
                    objednavka_id=order_id,
                    event_type=event_type,
                    variant_id=variant_id,
                )
            except platby.UdalostNepouzitelna:
                raise SmokeFailed(
                    "Podpis presného testovacieho webhooku sa nepodarilo overiť."
                ) from None
            event = result.get("udalost")
            if event is not None:
                server._doriesit_udalost(con, event, time.time())
                return event
        time.sleep(2)
    raise SmokeFailed(f"Podpísaný webhook {event_type} v limite neprišiel.")


def _refund_test_order(api_key: str, order_id: str) -> dict:
    # Omitting amount is LemonSqueezy's documented full-refund request.
    response = _provider_request(
        api_key,
        f"/v1/orders/{order_id}/refund",
        method="POST",
        payload={"data": {"type": "orders", "id": str(order_id),
                          "attributes": {}}},
    )
    data = response.get("data") if isinstance(response, dict) else None
    attributes = data.get("attributes") if isinstance(data, dict) else None
    if not isinstance(attributes, dict) or attributes.get("test_mode") is not True:
        raise SmokeFailed("Poskytovateľ nepotvrdil testovaciu refundáciu.")
    return data


def _wait_for_order(rekonciliacia, *, api_key, store_id, variant_id,
                    attempt_id, timeout_seconds):
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        order = _matching_test_order(
            rekonciliacia, api_key=api_key, store_id=store_id,
            variant_id=variant_id, attempt_id=attempt_id,
        )
        if order is not None:
            return order
        time.sleep(3)
    raise SmokeFailed("Testovacia objednávka sa v limite neobjavila.")


def _wait_for_refund(api_key: str, order_id: str, *, timeout_seconds):
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        response = _provider_request(api_key, f"/v1/orders/{order_id}")
        data = response.get("data") if isinstance(response, dict) else None
        attributes = data.get("attributes") if isinstance(data, dict) else None
        if isinstance(attributes, dict) and attributes.get("test_mode") is True:
            if attributes.get("refunded") is True:
                return data
        time.sleep(3)
    raise SmokeFailed("Úplná testovacia refundácia sa v limite nepotvrdila.")


def _write_marker(path: str, marker: dict) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=target.name + ".", suffix=".tmp", dir=target.parent
    )
    try:
        if hasattr(os, "fchmod"):
            os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(marker, output, sort_keys=True, separators=(",", ":"))
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, target)
        os.chmod(target, 0o600)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def _build_completed_marker(
    *, purchase_event, refund_event, release, live_checkout_url,
    live_webhook_secret, live_store_id, live_variant_id, live_api_key,
    test_checkout_url, test_webhook_secret,
    test_store_id, test_variant_id, test_api_key, order_id,
    receipt_email_verified, unresolved_cases, completed_at, signing_secret,
    create_marker, live_config_fingerprint, test_config_fingerprint, sign_marker,
):
    expected = (
        (purchase_event, "order_created", "udelene"),
        (refund_event, "order_refunded", "vratene"),
    )
    for event, event_type, action in expected:
        if not isinstance(event, dict) or event.get("zdroj") != "odlozene":
            raise SmokeFailed("Smoke nepreukázal podpísaný webhook.")
        if event.get("typ") != event_type or event.get("akcia") != action:
            raise SmokeFailed("Smoke webhook nemá očakávaný výsledok.")
        if event.get("objednavka") != order_id:
            raise SmokeFailed("Smoke webhook patrí inej objednávke.")
        if event.get("test_mode") is not True:
            raise SmokeFailed("Smoke webhook nie je v testovacom režime.")
    if receipt_email_verified is not True:
        raise SmokeFailed("Doručenie testovacieho dokladu nie je potvrdené.")
    if unresolved_cases != 0:
        raise SmokeFailed("Po teste ostal nevyriešený platobný prípad.")

    live_digest = live_config_fingerprint(
        secret=signing_secret,
        checkout_url=live_checkout_url,
        webhook_secret=live_webhook_secret,
        store_id=live_store_id,
        variant_id=live_variant_id,
        api_key=live_api_key,
    )
    test_digest = test_config_fingerprint(
        secret=signing_secret,
        checkout_url=test_checkout_url,
        webhook_secret=test_webhook_secret,
        store_id=test_store_id,
        variant_id=test_variant_id,
        api_key=test_api_key,
    )
    marker = create_marker(
        release=release,
        live_config_digest=live_digest,
        test_config_digest=test_digest,
        test_store_id=test_store_id,
        test_variant_id=test_variant_id,
        completed_at=completed_at,
        receipt_email_verified=True,
        test_mode_verified=True,
    )
    return sign_marker(marker, secret=signing_secret)


def _read_marker(path: str) -> dict:
    target = Path(path)
    try:
        if target.stat().st_size > 8_192:
            raise SmokeFailed("Smoke dôkaz je príliš veľký.")
        marker = json.loads(target.read_text(encoding="utf-8"))
    except SmokeFailed:
        raise
    except (OSError, ValueError, json.JSONDecodeError):
        raise SmokeFailed("Smoke dôkaz sa nedá bezpečne načítať.") from None
    if not isinstance(marker, dict):
        raise SmokeFailed("Smoke dôkaz má neplatný tvar.")
    return marker


def _build_activation_attestation(
    *, smoke_marker, activated_at, release, live_checkout_url,
    live_webhook_secret, live_store_id, live_variant_id, live_api_key,
    test_checkout_url, test_webhook_secret,
    test_store_id, test_variant_id, test_api_key, signing_secret,
    create_activation_attestation,
):
    """Create the production activation proof through the shared verifier."""
    try:
        return create_activation_attestation(
            smoke_marker,
            secret=signing_secret,
            release=release,
            checkout_url=live_checkout_url,
            webhook_secret=live_webhook_secret,
            store_id=live_store_id,
            variant_id=live_variant_id,
            api_key=live_api_key,
            test_checkout_url=test_checkout_url,
            test_webhook_secret=test_webhook_secret,
            test_store_id=test_store_id,
            test_variant_id=test_variant_id,
            test_api_key=test_api_key,
            activated_at=activated_at,
        )
    except ValueError as error:
        raise SmokeFailed(str(error)) from None


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Bezpečný test celého ročného predplatného Uvar.si"
    )
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--app-dir", default=DEFAULT_APP_DIR)
    parser.add_argument("--env-file", default=DEFAULT_ENV_FILE)
    parser.add_argument("--marker", default=DEFAULT_MARKER)
    parser.add_argument("--activation-marker", default=DEFAULT_ACTIVATION_MARKER)
    parser.add_argument(
        "--authorize-activation",
        action="store_true",
        help="podpíše aktiváciu z čerstvého smoke dôkazu; platby nezapne",
    )
    args = parser.parse_args(argv)

    (
        server,
        annual_provider_evidence_type,
        subscription_config_type,
        subscription_lifecycle_evidence_type,
        subscription_marker_expectation_type,
        create_subscription_activation_attestation,
        create_subscription_marker,
        discount_code_fingerprint,
        sign_marker,
    ) = _load_runtime(args.app_dir)
    release = server.release_id()
    marker_module, lifecycle_module = _load_schema5_runtime(args.app_dir)
    expectation = _annual_expectation_from_env(
        release=release,
        env_file=args.env_file,
        config_type=subscription_config_type,
        expectation_type=subscription_marker_expectation_type,
    )
    expectation = _with_probe_expectation(
        expectation,
        env_file=args.env_file,
        expectation_type=subscription_marker_expectation_type,
        lifecycle_module=lifecycle_module,
    )
    if args.authorize_activation:
        try:
            activation = create_subscription_activation_attestation(
                _read_marker(args.marker), expectation
            )
        except (TypeError, ValueError):
            raise SmokeFailed(
                "Aktivácia vyžaduje čerstvý annual smoke dôkaz."
            ) from None
        _write_marker(args.activation_marker, activation)
        print("OK: podpísaná aktivácia je pripravená pre aktuálnu konfiguráciu.")
        print("Platby ostali vypnuté; tento krok nemení PLATBY_ZAPNUTE.")
        return 0

    _public_preflight(args.base_url.rstrip("/"), release)
    live_provider = _verified_annual_provider_evidence(
        expectation.live.api_key,
        config=expectation.live,
        signing_secret=expectation.signing_secret,
        evidence_type=annual_provider_evidence_type,
        fingerprint=discount_code_fingerprint,
    )
    test_provider = _verified_annual_provider_evidence(
        expectation.test.api_key,
        config=expectation.test,
        signing_secret=expectation.signing_secret,
        evidence_type=annual_provider_evidence_type,
        fingerprint=discount_code_fingerprint,
    )

    email = input("E-mail účtu s dokončeným testovacím lifecycle: ").strip()
    password = getpass.getpass("Heslo testovacieho účtu: ")
    opener = _authenticated_opener(args.base_url.rstrip("/"), email, password)
    del password
    _payment_status(opener, args.base_url.rstrip("/"))
    subscription, invoices, events, unresolved, subscription_id = (
        _load_annual_lifecycle_records(
            server, email=email, test_config=expectation.test
        )
    )
    provider_portal_verified = _verified_test_portal_access(
        expectation.test.api_key,
        subscription_id=subscription_id,
        variant_id=expectation.test.variant_id,
    )
    app_portal_verified = _verified_app_portal_access(
        opener, args.base_url.rstrip("/")
    )
    portal_access_verified = provider_portal_verified and app_portal_verified
    reconciliation_event_key = marker_module.annual_reconciliation_event_key(
        signing_secret=expectation.signing_secret,
        release=expectation.release,
        config=expectation.test,
        provider_subscription_id=subscription_id,
    )
    annual_commercial = _annual_commercial_evidence_from_records(
        subscription=subscription,
        invoices=invoices,
        events=events,
        portal_access_verified=portal_access_verified,
        unresolved_cases=unresolved,
        config=expectation.test,
        evidence_type=marker_module.AnnualCommercialEvidence,
        reconciliation_event_key=reconciliation_event_key,
        now=time.time(),
    )
    probe_facts = _load_daily_probe_facts(
        server, expectation=expectation, lifecycle_module=lifecycle_module
    )
    marker = _build_schema5_subscription_marker(
        expectation=expectation,
        live_provider=live_provider,
        test_provider=test_provider,
        annual_commercial=annual_commercial,
        probe_facts=probe_facts,
        completed_at=dt.datetime.now(dt.timezone.utc),
        marker_module=marker_module,
    )
    _write_marker(args.marker, marker)
    print("OK: annual provider nastavenie a celý testovací lifecycle prešli.")
    print("Platby ostali vypnuté; ich zapnutie vyžaduje samostatné schválenie.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SmokeFailed as error:
        print(f"SMOKE ZLYHAL: {error}", file=sys.stderr)
        raise SystemExit(1)
