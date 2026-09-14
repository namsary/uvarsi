#!/usr/bin/env python3
"""Private operator CLI for Lemon Test-mode subscription evidence.

The tool has no HTTP route and never enables production payments.  Checkout
and portal URLs may only cross an interactive terminal; raw webhook bodies are
owned and scrubbed by the B1 probe/annual processors after exact validation.
"""

from __future__ import annotations

import argparse
from contextlib import closing
import datetime as dt
import getpass
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request


DEFAULT_APP_DIR = "/opt/uvarsi/app"
DEFAULT_ENV_FILE = "/opt/uvarsi/uvarsi.env"
DEFAULT_MARKER = "/var/lib/uvarsi/payment-smoke.json"
MAX_PROVIDER_BODY = 1_048_576
MAX_QUEUE_BODY = 64 * 1024


class ProbeToolFailed(RuntimeError):
    pass


def _runtime(app_dir=DEFAULT_APP_DIR):
    if app_dir not in sys.path:
        sys.path.insert(0, app_dir)
    try:
        from app import payment_smoke_marker, platby, predplatne
        from app import subscription_lifecycle_probe as lifecycle
        from app import server
    except ImportError:
        import payment_smoke_marker
        import platby
        import predplatne
        import subscription_lifecycle_probe as lifecycle
        import server
    return server, platby, predplatne, lifecycle, payment_smoke_marker


def _lifecycle_runtime(app_dir=DEFAULT_APP_DIR):
    """Load only B1; pure probe operations must not import the web server."""
    if app_dir not in sys.path:
        sys.path.insert(0, app_dir)
    try:
        from app import subscription_lifecycle_probe as lifecycle
    except ImportError:
        import subscription_lifecycle_probe as lifecycle
    return lifecycle


def _env_value(name, *, env_file=DEFAULT_ENV_FILE, environ=None):
    environ = os.environ if environ is None else environ
    value = environ.get(name, "")
    if isinstance(value, str) and value.strip():
        return value.strip()
    try:
        lines = Path(env_file).read_text(encoding="utf-8").splitlines()
    except OSError:
        return ""
    found = []
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, candidate = stripped.split("=", 1)
        if key.strip() == name:
            found.append(candidate.strip().strip("\"'"))
    if len(found) != 1:
        return ""
    return found[0]


def _require_payments_off(*, env_file=DEFAULT_ENV_FILE, environ=None):
    for name in ("PLATBY_ZAPNUTE", "UVARSI_PAYMENTS_ENABLED"):
        value = _env_value(name, env_file=env_file, environ=environ).casefold()
        if value not in {"0", "false", "off"}:
            raise ProbeToolFailed(
                "Testovací lifecycle je povolený iba s oboma produkčnými príznakmi OFF."
            )


def _require_tty(*, stdin, stdout, stderr):
    if not all(
        callable(getattr(stream, "isatty", None)) and stream.isatty()
        for stream in (stdin, stdout, stderr)
    ):
        raise ProbeToolFailed(
            "Krátkodobú URL možno zobraziť iba priamo v interaktívnom termináli."
        )


def _provider_request(api_key, path, *, method="GET", payload=None):
    if not isinstance(api_key, str) or not api_key.strip():
        raise ProbeToolFailed("Chýba testovací Lemon API kľúč.")
    encoded = None
    if payload is not None:
        encoded = json.dumps(payload, separators=(",", ":")).encode()
    request = urllib.request.Request(
        "https://api.lemonsqueezy.com" + path,
        data=encoded,
        method=method,
        headers={
            "Accept": "application/vnd.api+json",
            "Content-Type": "application/vnd.api+json",
            "Authorization": "Bearer " + api_key.strip(),
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            body = response.read(MAX_PROVIDER_BODY + 1)
    except (OSError, ValueError, urllib.error.HTTPError):
        raise ProbeToolFailed("Lemon Test mode je nedostupný.") from None
    if len(body) > MAX_PROVIDER_BODY:
        raise ProbeToolFailed("Lemon vrátil priveľkú odpoveď.")
    try:
        decoded = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ProbeToolFailed("Lemon vrátil neplatnú odpoveď.") from None
    if not isinstance(decoded, dict):
        raise ProbeToolFailed("Lemon vrátil neplatnú odpoveď.")
    return decoded


def _resource(response, *, kind, identity=None):
    data = response.get("data") if isinstance(response, dict) else None
    if (
        not isinstance(data, dict)
        or data.get("type") != kind
        or (identity is not None and str(data.get("id")) != str(identity))
        or not isinstance(data.get("attributes"), dict)
    ):
        raise ProbeToolFailed("Lemon nepotvrdil očakávaný testovací objekt.")
    return data, data["attributes"]


def _relationship_id(data, name):
    relationships = data.get("relationships")
    relation = relationships.get(name) if isinstance(relationships, dict) else None
    related = relation.get("data") if isinstance(relation, dict) else None
    value = related.get("id") if isinstance(related, dict) else None
    return str(value) if value is not None else None


def _verified_probe_metadata(config, *, request=_provider_request):
    response = request(config.api_key, f"/v1/variants/{config.variant_id}")
    variant, attrs = _resource(
        response, kind="variants", identity=config.variant_id
    )
    product_id = attrs.get("product_id") or _relationship_id(variant, "product")
    if not isinstance(product_id, str) or not product_id:
        raise ProbeToolFailed("Denný probe variant nemá jednoznačný produkt.")
    if not (
        attrs.get("test_mode") is True
        and attrs.get("price") == config.price_cents
        and attrs.get("interval") == "day"
        and attrs.get("interval_count") == 1
        and attrs.get("has_free_trial") is False
        and attrs.get("status") == "published"
    ):
        raise ProbeToolFailed("Lemon nepotvrdil presnú dennú Test-mode konfiguráciu.")
    product_response = request(config.api_key, f"/v1/products/{product_id}")
    product, product_attrs = _resource(
        product_response, kind="products", identity=product_id
    )
    store_id = product_attrs.get("store_id") or _relationship_id(product, "store")
    if not (
        str(store_id) == config.store_id
        and product_attrs.get("test_mode") is True
        and product_attrs.get("status") == "published"
    ):
        raise ProbeToolFailed("Denný probe nie je v presnom testovacom obchode.")


def _utc_iso(timestamp):
    return dt.datetime.fromtimestamp(timestamp, dt.timezone.utc).isoformat().replace(
        "+00:00", "Z"
    )


def _probe_checkout_payload(config, *, token, expires_at):
    return {
        "data": {
            "type": "checkouts",
            "attributes": {
                "test_mode": True,
                "product_options": {
                    "enabled_variants": [config.variant_id],
                    "redirect_url": "https://uvar.si/app",
                },
                "checkout_options": {
                    "discount": False,
                    "skip_trial": True,
                    "subscription_preview": True,
                },
                "checkout_data": {
                    "custom": {"lifecycle_probe_token": token},
                },
                "expires_at": _utc_iso(expires_at),
                "preview": True,
            },
            "relationships": {
                "store": {"data": {"type": "stores", "id": config.store_id}},
                "variant": {
                    "data": {"type": "variants", "id": config.variant_id}
                },
            },
        }
    }


def _safe_checkout_url(value):
    try:
        parsed = urllib.parse.urlsplit(value)
        port = parsed.port
    except (TypeError, ValueError):
        raise ProbeToolFailed("Lemon nepotvrdil bezpečnú testovaciu pokladňu.") from None
    query = dict(urllib.parse.parse_qsl(parsed.query, keep_blank_values=True))
    host = parsed.hostname
    if not (
        parsed.scheme == "https"
        and isinstance(host, str)
        and (host == "lemonsqueezy.com" or host.endswith(".lemonsqueezy.com"))
        and parsed.username is None
        and parsed.password is None
        and port is None
        and parsed.path.startswith("/checkout/")
        and not parsed.fragment
        and query.get("expires")
        and query.get("signature")
    ):
        raise ProbeToolFailed("Lemon nepotvrdil bezpečnú testovaciu pokladňu.")
    return value


def _create_probe_checkout(config, *, token, expires_at, request=_provider_request):
    payload = _probe_checkout_payload(config, token=token, expires_at=expires_at)
    response = request(
        config.api_key, "/v1/checkouts", method="POST", payload=payload
    )
    _data, attrs = _resource(response, kind="checkouts")
    preview = attrs.get("preview")
    try:
        returned_expiry = dt.datetime.fromisoformat(
            str(attrs.get("expires_at")).replace("Z", "+00:00")
        ).timestamp()
    except (TypeError, ValueError):
        raise ProbeToolFailed("Lemon nepotvrdil expiráciu testovacej pokladne.") from None
    if not (
        attrs.get("test_mode") is True
        and str(attrs.get("store_id")) == config.store_id
        and str(attrs.get("variant_id")) == config.variant_id
        and isinstance(preview, dict)
        and preview.get("currency") == "EUR"
        and preview.get("total") == config.price_cents
        and abs(returned_expiry - expires_at) < 0.001
    ):
        raise ProbeToolFailed("Lemon nepotvrdil presnú cenu dennej pokladne.")
    return _safe_checkout_url(attrs.get("url"))


def prepare_probe(
    con,
    *,
    config,
    signing_secret,
    annual_test_variant_id,
    live_webhook_secret,
    annual_test_webhook_secret,
    now,
    token=None,
    stdin=sys.stdin,
    stdout=sys.stdout,
    stderr=sys.stderr,
    request=_provider_request,
    lifecycle_module=None,
):
    """Create one isolated daily run; only its short URL reaches a TTY."""
    _require_tty(stdin=stdin, stdout=stdout, stderr=stderr)
    lifecycle = lifecycle_module or _lifecycle_runtime()
    try:
        config = lifecycle.validate_probe_config(
            config,
            annual_test_variant_id=annual_test_variant_id,
            live_webhook_secret=live_webhook_secret,
            annual_test_webhook_secret=annual_test_webhook_secret,
        )
        lifecycle.matching_probe_run_digest(
            con, config=config, signing_secret=signing_secret
        )
    except lifecycle.ProbeEventRejected:
        pass
    except lifecycle.ProbeConfigError as error:
        raise ProbeToolFailed(str(error)) from None
    else:
        raise ProbeToolFailed("Pre túto konfiguráciu už existuje aktívny probe beh.")
    token = token or secrets.token_urlsafe(48)
    expires_at = float(now) + 60 * 60
    _verified_probe_metadata(config, request=request)
    checkout_url = _create_probe_checkout(
        config, token=token, expires_at=expires_at, request=request
    )
    try:
        lifecycle.create_probe_run(
            con,
            config=config,
            signing_secret=signing_secret,
            now=now,
            token=token,
        )
        lifecycle.record_probe_provider_verified(
            con,
            token=token,
            config=config,
            signing_secret=signing_secret,
            test_mode=True,
            store_id=config.store_id,
            variant_id=config.variant_id,
            price_cents=config.price_cents,
            currency="EUR",
            interval="day",
            interval_count=1,
            trial_days=0,
            discount_applied_cents=0,
            variant_status="published",
            now=now,
        )
    except (sqlite3.Error, ValueError) as error:
        raise ProbeToolFailed("Izolovaný probe beh sa nepodarilo bezpečne uložiť.") from error
    stdout.write("Otvor túto krátkodobú Test-mode pokladňu:\n")
    stdout.write(checkout_url + "\n")
    stdout.flush()
    del checkout_url, token
    return {"prepared": True}


def safe_probe_status(con, *, config, signing_secret, lifecycle_module=None):
    lifecycle = lifecycle_module or _lifecycle_runtime()
    try:
        digest = lifecycle.matching_probe_run_digest(
            con, config=config, signing_secret=signing_secret
        )
        return lifecycle.probe_status_by_digest(con, token_digest=digest)
    except (lifecycle.ProbeConfigError, lifecycle.ProbeEventRejected) as error:
        raise ProbeToolFailed(str(error)) from None


def process_probe(
    con,
    *,
    config,
    signing_secret,
    now,
    request=_provider_request,
    lifecycle_module=None,
):
    """Process only dedicated signed probe rows; verify a real renewal by API."""
    lifecycle = lifecycle_module or _lifecycle_runtime()
    candidates = []
    rows = con.execute(
        "SELECT telo,podpis FROM platobne_odlozene WHERE spracovane_o IS NULL ORDER BY id"
    ).fetchall()
    for row in rows:
        body, signature = bytes(row[0]), row[1]
        if not isinstance(signature, str) or len(body) > MAX_QUEUE_BODY:
            continue
        expected = hmac.new(config.webhook_secret.encode(), body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            continue
        try:
            payload = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        meta = payload.get("meta") if isinstance(payload, dict) else None
        attrs = payload.get("data", {}).get("attributes", {}) if isinstance(payload, dict) else {}
        custom = meta.get("custom_data") if isinstance(meta, dict) else None
        token = custom.get(lifecycle.PROBE_TOKEN_FIELD) if isinstance(custom, dict) else None
        if meta.get("event_name") == "subscription_payment_success" and attrs.get("billing_reason") == "renewal" and isinstance(token, str):
            candidates.append((token, dict(attrs)))
    result = lifecycle.process_queued_probe_events(
        con, config=config, signing_secret=signing_secret, now=now
    )
    for token, attrs in candidates:
        subscription_id = attrs.get("subscription_id")
        invoice_id = attrs.get("invoice_id")
        subscription_response = request(
            config.api_key, f"/v1/subscriptions/{urllib.parse.quote(str(subscription_id), safe='')}"
        )
        _sub, sub_attrs = _resource(
            subscription_response, kind="subscriptions", identity=subscription_id
        )
        invoice_response = request(
            config.api_key,
            f"/v1/subscription-invoices/{urllib.parse.quote(str(invoice_id), safe='')}",
        )
        _invoice, invoice_attrs = _resource(
            invoice_response, kind="subscription-invoices", identity=invoice_id
        )
        lifecycle.record_genuine_daily_renewal(
            con,
            token=token,
            config=config,
            signing_secret=signing_secret,
            provider_subscription_id=subscription_id,
            provider_invoice_id=invoice_id,
            amount_cents=invoice_attrs.get("total"),
            currency=str(invoice_attrs.get("currency", "")).upper(),
            interval=sub_attrs.get("billing_anchor_interval", sub_attrs.get("interval")),
            interval_count=sub_attrs.get("billing_anchor_interval_count", sub_attrs.get("interval_count")),
            period_start=invoice_attrs.get("billing_period_start"),
            period_end=invoice_attrs.get("billing_period_end"),
            now=now,
        )
        del token
    return result


def cleanup_probe(
    con,
    *,
    config,
    signing_secret,
    signed_marker,
    marker_is_valid,
    confirmation,
    now=None,
    lifecycle_module=None,
):
    lifecycle = lifecycle_module or _lifecycle_runtime()
    if not callable(marker_is_valid) or marker_is_valid(signed_marker) is not True:
        raise ProbeToolFailed("Cleanup vyžaduje čerstvý platný schema-5 marker.")
    try:
        digest = lifecycle.matching_probe_run_digest(
            con, config=config, signing_secret=signing_secret,
            states=("collecting", "ready", "evidenced"),
        )
        state = lifecycle.probe_status_by_digest(con, token_digest=digest)["state"]
        if state != "evidenced":
            lifecycle.mark_probe_digest_evidenced(
                con,
                token_digest=digest,
                config=config,
                signing_secret=signing_secret,
                signed_marker=signed_marker,
                now=time.time() if now is None else now,
            )
        return lifecycle.cleanup_probe_run_by_digest(
            con, token_digest=digest, confirmation=confirmation
        )
    except (lifecycle.ProbeCleanupRejected, lifecycle.ProbeEventRejected) as error:
        raise ProbeToolFailed(str(error)) from None


class _AnnualCheckoutProvider:
    def __init__(self, config, request):
        self._config = config
        self._request = request
        self.store_id = config.store_id
        self.variant_id = config.variant_id
        self.founder_discount_id = config.discount_id
        self.founder_discount_code = config.discount_code

    def __repr__(self):
        return "_AnnualCheckoutProvider(<redacted>)"

    def create_checkout(self, payload):
        return self._request(
            self._config.api_key, "/v1/checkouts", method="POST", payload=payload
        )


def prepare_annual_checkout(
    con,
    *,
    user_id,
    email,
    config,
    legal_version,
    now,
    stdin=sys.stdin,
    stdout=sys.stdout,
    stderr=sys.stderr,
    request=_provider_request,
    platby_module=None,
):
    """Create one paired founder checkout for one clean Test-mode account."""
    _require_tty(stdin=stdin, stdout=stdout, stderr=stderr)
    platby_module = platby_module or _runtime()[1]
    if getattr(config, "test_mode", None) is not True:
        raise ProbeToolFailed("Ročný smoke checkout musí byť výlučne v Test mode.")
    if not isinstance(user_id, int) or isinstance(user_id, bool) or user_id < 1:
        raise ProbeToolFailed("Ročný smoke účet nie je platný.")
    if con.execute(
        "SELECT 1 FROM pouzivatelia WHERE id=?", (user_id,)
    ).fetchone() is None:
        raise ProbeToolFailed("Ročný smoke účet neexistuje.")
    if con.execute(
        "SELECT 1 FROM subscriptions WHERE user_id=? LIMIT 1", (user_id,)
    ).fetchone() is not None or con.execute(
        "SELECT 1 FROM checkout_attempts WHERE user_id=? LIMIT 1", (user_id,)
    ).fetchone() is not None:
        raise ProbeToolFailed("Použi čistý vyhradený účet bez starej platby alebo pokusu.")
    consent = {
        "accept_terms": True,
        "accept_automatic_renewal": True,
        "request_immediate_activation": True,
        "acknowledge_withdrawal_proration": True,
        "legal_version": legal_version,
        "expected_offer_id": platby_module.FOUNDER_OFFER_ID,
        "expected_amount_cents": 3_900,
    }
    attempt = platby_module.create_subscription_checkout_attempt(
        con,
        user_id=user_id,
        legal_version=legal_version,
        consent=consent,
        now=now,
        founder_discount_id=config.discount_id,
        founder_discount_code=config.discount_code,
        test_mode=True,
    )
    if not (
        attempt.founder is True
        and attempt.amount_cents == 3_900
        and attempt.renewal_amount_cents == 4_900
        and attempt.currency == "EUR"
        and attempt.billing_interval == "year"
        and attempt.auto_renews is True
        and attempt.test_mode is True
    ):
        raise ProbeToolFailed("Ročný pokus nezodpovedá zmluve 39/49 EUR bez trialu.")
    provider = _AnnualCheckoutProvider(config, request)
    try:
        checkout_url = platby_module.create_provider_subscription_checkout(
            attempt=attempt,
            email=email,
            provider=provider,
            test_mode=True,
        )
        platby_module.record_provider_checkout(
            con,
            attempt=attempt,
            provider_checkout_id=checkout_url.provider_checkout_id,
            test_mode=True,
            discount_id=config.discount_id,
            discount_code=config.discount_code,
        )
        con.commit()
    except Exception:
        con.rollback()
        con.execute(
            "DELETE FROM checkout_attempts WHERE public_id=? AND test_mode=1 "
            "AND status='pending' AND provider_order_id IS NULL",
            (attempt.public_id,),
        )
        con.commit()
        raise
    stdout.write("Otvor túto krátkodobú ročnú Test-mode pokladňu:\n")
    stdout.write(str(checkout_url) + "\n")
    stdout.flush()
    del checkout_url, email
    return {"prepared": True}


def _annual_payload_owned(con, *, payload, attempt_id, config):
    if not isinstance(payload, dict):
        return False
    meta = payload.get("meta")
    data = payload.get("data")
    attrs = data.get("attributes") if isinstance(data, dict) else None
    custom = meta.get("custom_data") if isinstance(meta, dict) else None
    if not isinstance(attrs, dict) or not isinstance(custom, dict):
        return False
    if "lifecycle_probe_token" in custom:
        return False
    if not (
        attrs.get("test_mode") is True
        and str(attrs.get("store_id")) == config.store_id
        and str(attrs.get("variant_id")) == config.variant_id
        and str(attrs.get("currency", "")).upper() == "EUR"
    ):
        return False
    custom_attempt = custom.get("attempt_id", custom.get("checkout_attempt"))
    if custom_attempt is not None:
        return hmac.compare_digest(str(custom_attempt), attempt_id)
    attempt = con.execute(
        "SELECT user_id,provider_order_id FROM checkout_attempts "
        "WHERE public_id=? AND test_mode=1",
        (attempt_id,),
    ).fetchone()
    if attempt is None:
        return False
    subscription_id = attrs.get("subscription_id")
    order_id = attrs.get("order_id")
    if subscription_id is None and data.get("type") == "subscriptions":
        subscription_id = data.get("id")
    row = con.execute(
        "SELECT provider_subscription_id,provider_order_id FROM subscriptions "
        "WHERE user_id=? AND test_mode=1 AND provider_variant_id=? "
        "ORDER BY updated_at DESC LIMIT 1",
        (attempt[0], config.variant_id),
    ).fetchone()
    return bool(
        row is not None
        and str(subscription_id) == str(row[0])
        and str(order_id) == str(row[1])
        and (attempt[1] is None or str(order_id) == str(attempt[1]))
    )


def process_annual_queue(
    con,
    *,
    attempt_id,
    config,
    now,
    predplatne_module=None,
    limit=200,
):
    """Run the existing deferred annual processor for one exact test attempt."""
    predplatne_module = predplatne_module or _runtime()[2]
    if getattr(config, "test_mode", None) is not True:
        raise ProbeToolFailed("Ročný processor musí zostať v Test mode.")
    if not isinstance(attempt_id, str) or len(attempt_id) < 43:
        raise ProbeToolFailed("Chýba bezpečný ročný checkout pokus.")
    rows = con.execute(
        "SELECT id,telo,podpis FROM platobne_odlozene "
        "WHERE spracovane_o IS NULL ORDER BY id LIMIT ?",
        (limit,),
    ).fetchall()
    result = {"processed": 0, "left_unrelated": 0, "rejected": 0}
    expected_config = {
        "store_id": config.store_id,
        "variant_id": config.variant_id,
        "founder_discount_id": config.discount_id,
        "currency": "EUR",
        "test_mode": True,
    }
    for row in rows:
        body, signature = bytes(row[1]), row[2]
        if (
            not isinstance(signature, str)
            or len(body) == 0
            or len(body) > MAX_QUEUE_BODY
            or len(signature) != 64
        ):
            result["left_unrelated"] += 1
            continue
        wanted = hmac.new(config.webhook_secret.encode(), body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, wanted):
            result["left_unrelated"] += 1
            continue
        try:
            payload = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError):
            result["rejected"] += 1
            continue
        if not _annual_payload_owned(
            con, payload=payload, attempt_id=attempt_id, config=config
        ):
            result["left_unrelated"] += 1
            continue
        try:
            processed = predplatne_module.process_subscription_event(
                con,
                payload=payload,
                now=now,
                expected=expected_config,
                source="odlozene",
                delivery_key=hashlib.sha256(body).hexdigest(),
            )
        except Exception:
            result["rejected"] += 1
            continue
        if processed.get("review_required") is True:
            result["rejected"] += 1
            continue
        updated = con.execute(
            "UPDATE platobne_odlozene SET telo=?,podpis=NULL,spracovane_o=?,"
            "vysledok=? WHERE id=? AND spracovane_o IS NULL",
            (b"", now, "annual_test:" + str(processed.get("event_type")), row[0]),
        ).rowcount
        con.commit()
        result["processed"] += int(updated == 1)
    return result


def annual_commercial_status(
    *,
    attempt,
    subscription,
    invoices,
    events,
    config,
    portal_verified,
    unresolved_cases,
    annual_domain_contract_verified,
):
    """Return only annual facts actually needed by schema 5, never fake time travel."""
    event_names = {
        event.get("event_type")
        for event in events
        if event.get("processing_status") == "processed"
        and event.get("needs_review") in {0, False}
    }
    initial = next(
        (
            item for item in invoices
            if item.get("invoice_kind") == "initial"
            and item.get("amount_cents") == 3_900
            and item.get("currency") == "EUR"
        ),
        None,
    )
    refund_verified = bool(
        initial is not None
        and initial.get("status") == "refunded"
        and initial.get("refunded_amount_cents") == 3_900
        and {"subscription_payment_refunded", "order_refunded"} & event_names
    )
    complete = bool(
        isinstance(attempt, dict)
        and attempt.get("status") == "paid"
        and attempt.get("provider_checkout_id")
        and attempt.get("amount_cents") == 3_900
        and attempt.get("renewal_amount_cents") == 4_900
        and attempt.get("currency") == "EUR"
        and attempt.get("billing_interval") == "year"
        and attempt.get("auto_renews") == 1
        and attempt.get("founder") == 1
        and attempt.get("test_mode") == 1
        and isinstance(subscription, dict)
        and subscription.get("test_mode") == 1
        and subscription.get("founder") == 1
        and subscription.get("initial_amount_cents") == 3_900
        and subscription.get("renewal_amount_cents") == 4_900
        and subscription.get("currency") == "EUR"
        and subscription.get("provider_variant_id") == config.variant_id
        and subscription.get("initial_payment_verified") == 1
        and initial is not None
        and "subscription_created" in event_names
        and "subscription_payment_success" in event_names
        and "subscription_cancelled" in event_names
        and "subscription_resumed" in event_names
        and refund_verified
        and portal_verified is True
        and type(unresolved_cases) is int
        and unresolved_cases == 0
        and annual_domain_contract_verified is True
    )
    return {
        "complete": complete,
        "initial_charge_cents": 3_900 if initial is not None else None,
        "renewal_display_cents": 4_900,
        "billing_interval": "year",
        "checkout_verified": bool(
            isinstance(attempt, dict) and attempt.get("provider_checkout_id")
        ),
        "initial_payment_verified": initial is not None,
        "portal_verified": portal_verified is True,
        "cancellation_verified": "subscription_cancelled" in event_names,
        "resume_verified": "subscription_resumed" in event_names,
        "refund_verified": refund_verified,
        "reconciliation_verified": type(unresolved_cases) is int and unresolved_cases == 0,
        "annual_domain_contract_verified": annual_domain_contract_verified is True,
        "events": [
            {"name": event.get("event_type"), "processed_at": event.get("processed_at")}
            for event in events
            if event.get("event_type") in event_names
        ],
    }


def _annual_account(con, *, server, email, config):
    normalized = server.normalize_email(email)
    user = con.execute(
        "SELECT id FROM pouzivatelia WHERE email=?", (normalized,)
    ).fetchone()
    if user is None:
        raise ProbeToolFailed("Vyhradený ročný Test-mode účet neexistuje.")
    user_id = int(user[0])
    attempts = con.execute(
        """SELECT * FROM checkout_attempts
             WHERE user_id=? AND test_mode=1 AND discount_id=?
               AND amount_cents=3900 AND renewal_amount_cents=4900
               AND currency='EUR' AND billing_interval='year'
               AND auto_renews=1 AND founder=1
             ORDER BY accepted_at DESC""",
        (user_id, config.discount_id),
    ).fetchall()
    if len(attempts) > 1:
        raise ProbeToolFailed(
            "Ročný Test-mode účet má viac pokusov; použi nový čistý účet."
        )
    attempt = dict(attempts[0]) if attempts else None
    subscriptions = con.execute(
        """SELECT * FROM subscriptions
             WHERE user_id=? AND provider='lemonsqueezy' AND test_mode=1
               AND provider_variant_id=? AND discount_id=?""",
        (user_id, config.variant_id, config.discount_id),
    ).fetchall()
    if len(subscriptions) > 1:
        raise ProbeToolFailed("Ročný Test-mode účet má nejednoznačné predplatné.")
    subscription = dict(subscriptions[0]) if subscriptions else None
    subscription_id = (
        subscription.get("provider_subscription_id")
        if isinstance(subscription, dict) else None
    )
    invoices = []
    events = []
    if isinstance(subscription_id, str) and subscription_id:
        invoices = [
            dict(row) for row in con.execute(
                """SELECT * FROM subscription_invoices
                     WHERE provider='lemonsqueezy' AND test_mode=1
                       AND provider_subscription_id=?""",
                (subscription_id,),
            ).fetchall()
        ]
        events = [
            dict(row) for row in con.execute(
                """SELECT * FROM subscription_events
                     WHERE provider='lemonsqueezy' AND test_mode=1
                       AND provider_subscription_id=?""",
                (subscription_id,),
            ).fetchall()
        ]
    unresolved = int(con.execute(
        "SELECT COUNT(*) FROM payment_cases WHERE user_id=? AND status='open'",
        (user_id,),
    ).fetchone()[0])
    unresolved += sum(
        1 for event in events
        if event.get("processing_status") != "processed"
        or event.get("needs_review") != 0
    )
    return user_id, attempt, subscription, invoices, events, unresolved


def _annual_attempt_id(con, *, server, email, config):
    _user_id, attempt, _subscription, _invoices, _events, _unresolved = (
        _annual_account(con, server=server, email=email, config=config)
    )
    attempt_id = attempt.get("public_id") if isinstance(attempt, dict) else None
    if not isinstance(attempt_id, str) or len(attempt_id) < 43:
        raise ProbeToolFailed("Pre účet neexistuje jednoznačný ročný checkout pokus.")
    return attempt_id


def _annual_safe_status(con, *, server, email, config):
    _user_id, attempt, subscription, invoices, events, unresolved = (
        _annual_account(con, server=server, email=email, config=config)
    )
    return annual_commercial_status(
        attempt=attempt,
        subscription=subscription,
        invoices=invoices,
        events=events,
        config=config,
        portal_verified=False,
        unresolved_cases=unresolved,
        annual_domain_contract_verified=True,
    )


def _probe_config_from_env(lifecycle, *, env_file):
    annual_variant = _env_value(
        "LEMON_TEST_SUBSCRIPTION_VARIANT_ID", env_file=env_file
    )
    live_secret = _env_value("LEMON_WEBHOOK_SECRET", env_file=env_file)
    annual_secret = _env_value("LEMON_TEST_WEBHOOK_SECRET", env_file=env_file)
    try:
        config = lifecycle.read_probe_config(
            lambda name, default="": _env_value(name, env_file=env_file),
            annual_test_variant_id=annual_variant,
            live_webhook_secret=live_secret,
            annual_test_webhook_secret=annual_secret,
        )
    except lifecycle.ProbeConfigError as error:
        raise ProbeToolFailed(str(error)) from None
    signing_secret = _env_value(
        "UVARSI_PAYMENT_SMOKE_SIGNING_SECRET", env_file=env_file
    )
    if not signing_secret:
        raise ProbeToolFailed("Chýba podpisové tajomstvo smoke dôkazu.")
    return config, signing_secret, annual_variant, live_secret, annual_secret


def _read_marker(path):
    try:
        target = Path(path)
        if target.stat().st_size > 8_192:
            raise ValueError
        result = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        raise ProbeToolFailed("Schema-5 marker sa nedá bezpečne načítať.") from None
    if not isinstance(result, dict):
        raise ProbeToolFailed("Schema-5 marker má neplatný tvar.")
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Súkromná Lemon Test-mode lifecycle brána Uvar.si"
    )
    parser.add_argument(
        "command",
        choices=(
            "annual-prepare", "annual-process", "annual-status",
            "prepare", "process", "status", "cleanup",
        ),
    )
    parser.add_argument("--app-dir", default=DEFAULT_APP_DIR)
    parser.add_argument("--env-file", default=DEFAULT_ENV_FILE)
    parser.add_argument("--marker", default=DEFAULT_MARKER)
    args = parser.parse_args(argv)
    _require_payments_off(env_file=args.env_file)
    server, _platby, _predplatne, lifecycle, marker_module = _runtime(args.app_dir)
    config, signing_secret, annual_variant, live_secret, annual_secret = (
        _probe_config_from_env(lifecycle, env_file=args.env_file)
    )
    expectation = server._subscription_marker_expectation()
    if expectation is None or getattr(expectation.test, "test_mode", None) is not True:
        raise ProbeToolFailed("Chýba presná ročná Test-mode konfigurácia.")
    annual_config = expectation.test
    with closing(server.db()) as con:
        if args.command == "annual-prepare":
            _require_tty(stdin=sys.stdin, stdout=sys.stdout, stderr=sys.stderr)
            email = getpass.getpass("E-mail čistého ročného Test-mode účtu: ").strip()
            normalized = server.normalize_email(email)
            user = con.execute(
                "SELECT id FROM pouzivatelia WHERE email=?", (normalized,)
            ).fetchone()
            if user is None:
                raise ProbeToolFailed("Vyhradený ročný Test-mode účet neexistuje.")
            prepare_annual_checkout(
                con,
                user_id=int(user[0]),
                email=normalized,
                config=annual_config,
                legal_version=server.LEGAL_VERSION,
                now=time.time(),
            )
        elif args.command == "annual-process":
            email = getpass.getpass("E-mail ročného Test-mode účtu: ").strip()
            attempt_id = _annual_attempt_id(
                con, server=server, email=email, config=annual_config
            )
            print(json.dumps(process_annual_queue(
                con,
                attempt_id=attempt_id,
                config=annual_config,
                now=time.time(),
            ), sort_keys=True))
        elif args.command == "annual-status":
            email = getpass.getpass("E-mail ročného Test-mode účtu: ").strip()
            print(json.dumps(_annual_safe_status(
                con, server=server, email=email, config=annual_config
            ), sort_keys=True))
        elif args.command == "prepare":
            prepare_probe(
                con,
                config=config,
                signing_secret=signing_secret,
                annual_test_variant_id=annual_variant,
                live_webhook_secret=live_secret,
                annual_test_webhook_secret=annual_secret,
                now=time.time(),
                lifecycle_module=lifecycle,
            )
        elif args.command == "process":
            print(json.dumps(process_probe(
                con, config=config, signing_secret=signing_secret,
                now=time.time(), lifecycle_module=lifecycle,
            ), sort_keys=True))
        elif args.command == "status":
            print(json.dumps(safe_probe_status(
                con, config=config, signing_secret=signing_secret,
                lifecycle_module=lifecycle,
            ), sort_keys=True))
        else:
            signed_marker = _read_marker(args.marker)
            digest = lifecycle.matching_probe_run_digest(
                con, config=config, signing_secret=signing_secret
            )
            phrase = lifecycle.cleanup_confirmation(token_digest=digest)
            confirmation = getpass.getpass(
                f"Na vymazanie iba tohto probe behu napíš presne {phrase}: "
            )
            result = cleanup_probe(
                con,
                config=config,
                signing_secret=signing_secret,
                signed_marker=signed_marker,
                marker_is_valid=lambda marker: marker_module.valid_subscription_marker(
                    marker, expectation
                ),
                confirmation=confirmation,
                lifecycle_module=lifecycle,
            )
            print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ProbeToolFailed as error:
        print(f"PROBE ZLYHAL: {error}", file=sys.stderr)
        raise SystemExit(1)
