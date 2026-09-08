#!/usr/bin/env python3
"""Owner-run LemonSqueezy test purchase/refund smoke for one Uvar.si release.

The script runs on the Uvar.si server.  It keeps payments publicly disabled,
authenticates through the normal password endpoint, creates one audited test
checkout, verifies the resulting entitlement, issues a full test refund and
verifies revocation.  It never accepts or stores payment-instrument data.
"""

from __future__ import annotations

import argparse
import datetime as dt
import getpass
import http.cookiejar
import json
import os
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


def _public_preflight(base_url: str, expected_release: str) -> dict:
    opener = urllib.request.build_opener(_NoRedirect())
    health = _json_request(opener, f"{base_url}/api/health")
    if health.get("vydanie") != expected_release:
        raise SmokeFailed("Živá aplikácia nemá očakávané vydanie.")
    recipe = health.get("recipe_engine") or {}
    if recipe.get("payments_enabled") is not False:
        raise SmokeFailed("Verejné platby musia počas testu zostať vypnuté.")
    readiness = health.get("payment_readiness") or {}
    blockers = set(readiness.get("blockers") or ())
    if blockers - {"payment_smoke_missing"}:
        raise SmokeFailed("Pred testom ostávajú iné blokátory pripravenosti.")
    return health


def _load_runtime(app_dir: str):
    if app_dir not in sys.path:
        sys.path.insert(0, app_dir)
    import platby
    import rekonciliacia
    import server
    from payment_smoke_marker import (
        create_marker,
        live_config_fingerprint,
        sign_marker,
    )
    return (
        server,
        platby,
        rekonciliacia,
        create_marker,
        live_config_fingerprint,
        sign_marker,
    )


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
    live_store_id, live_variant_id, test_store_id, test_variant_id, order_id,
    receipt_email_verified, unresolved_cases, completed_at, signing_secret,
    create_marker, live_config_fingerprint, sign_marker,
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
        store_id=live_store_id,
        variant_id=live_variant_id,
    )
    marker = create_marker(
        release=release,
        live_config_digest=live_digest,
        test_store_id=test_store_id,
        test_variant_id=test_variant_id,
        completed_at=completed_at,
        receipt_email_verified=True,
        test_mode_verified=True,
    )
    return sign_marker(marker, secret=signing_secret)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Bezpečný test nákupu a úplnej refundácie Uvar.si"
    )
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--app-dir", default=DEFAULT_APP_DIR)
    parser.add_argument("--env-file", default=DEFAULT_ENV_FILE)
    parser.add_argument("--marker", default=DEFAULT_MARKER)
    parser.add_argument("--timeout", type=int, default=300)
    args = parser.parse_args(argv)

    (
        server,
        platby,
        rekonciliacia,
        create_marker,
        live_config_fingerprint,
        sign_marker,
    ) = _load_runtime(args.app_dir)
    release = server.release_id()
    test_values = {
        name: _env_value(name, env_file=args.env_file)
        for name in (
            "LEMON_TEST_API_KEY",
            "LEMON_TEST_WEBHOOK_SECRET",
            "LEMON_TEST_STORE_ID",
            "LEMON_TEST_VARIANT_ID",
        )
    }
    live_values = {
        name: _env_value(name, env_file=args.env_file)
        for name in (
            "LEMON_API_KEY",
            "LEMON_CHECKOUT_URL",
            "LEMON_STORE_ID",
            "LEMON_VARIANT_ID",
        )
    }
    signing_secret = _env_value(
        "UVARSI_PAYMENT_SMOKE_SIGNING_SECRET", env_file=args.env_file
    )
    if not all(test_values.values()):
        raise SmokeFailed("Chýba časť testovacej konfigurácie poskytovateľa.")
    if not all(live_values.values()) or not signing_secret:
        raise SmokeFailed("Chýba časť plánovanej živej platobnej konfigurácie.")
    _public_preflight(args.base_url.rstrip("/"), release)
    _verified_test_variant(
        test_values["LEMON_TEST_API_KEY"],
        store_id=test_values["LEMON_TEST_STORE_ID"],
        variant_id=test_values["LEMON_TEST_VARIANT_ID"],
    )
    _verified_live_configuration(
        live_values["LEMON_API_KEY"],
        store_id=live_values["LEMON_STORE_ID"],
        variant_id=live_values["LEMON_VARIANT_ID"],
        checkout_url=live_values["LEMON_CHECKOUT_URL"],
    )

    email = input("E-mail čistého testovacieho účtu: ").strip()
    password = getpass.getpass("Heslo testovacieho účtu: ")
    opener = _authenticated_opener(args.base_url.rstrip("/"), email, password)
    del password
    initial = _payment_status(opener, args.base_url.rstrip("/"))
    if initial["ma_narok"]:
        raise SmokeFailed("Testovací účet už má Premium.")

    user_id, attempt = _prepare_checkout(server, platby, email=email)
    checkout = _create_verified_test_checkout(
        test_values["LEMON_TEST_API_KEY"],
        store_id=test_values["LEMON_TEST_STORE_ID"],
        variant_id=test_values["LEMON_TEST_VARIANT_ID"],
        user_id=user_id,
        attempt_id=attempt,
        email=email,
    )
    print("\nOtvor túto TESTOVACIU pokladňu v prehliadači:")
    print(checkout)
    print("Platobné údaje zadávaj iba v hosťovanej pokladni poskytovateľa.")
    input("Po dokončení testovacieho nákupu stlač Enter...")

    order = _wait_for_order(
        rekonciliacia,
        api_key=test_values["LEMON_TEST_API_KEY"],
        store_id=test_values["LEMON_TEST_STORE_ID"],
        variant_id=test_values["LEMON_TEST_VARIANT_ID"],
        attempt_id=attempt,
        timeout_seconds=args.timeout,
    )
    order_id = str(order["id"])
    purchase_event = _wait_for_signed_webhook(
        server,
        platby,
        secret=test_values["LEMON_TEST_WEBHOOK_SECRET"],
        variant_id=test_values["LEMON_TEST_VARIANT_ID"],
        order_id=order_id,
        event_type="order_created",
        timeout_seconds=args.timeout,
    )
    if purchase_event.get("akcia") != platby.AKCIA_UDELENE:
        raise SmokeFailed("Podpísaný nákup neudelil nový Premium nárok.")
    if _payment_status(opener, args.base_url.rstrip("/"))["ma_narok"] is not True:
        raise SmokeFailed("Testovací nákup neudelil Premium.")
    receipt_confirmation = input(
        "Over testovací doklad v mailboxe a napíš POTVRDZUJEM: "
    ).strip()
    if receipt_confirmation != "POTVRDZUJEM":
        raise SmokeFailed("Doručenie testovacieho dokladu nebolo potvrdené.")

    _refund_test_order(test_values["LEMON_TEST_API_KEY"], order_id)
    refunded = _wait_for_refund(
        test_values["LEMON_TEST_API_KEY"], order_id, timeout_seconds=args.timeout
    )
    if str(refunded.get("id")) != order_id:
        raise SmokeFailed("Poskytovateľ potvrdil refundáciu inej objednávky.")
    refund_event = _wait_for_signed_webhook(
        server,
        platby,
        secret=test_values["LEMON_TEST_WEBHOOK_SECRET"],
        variant_id=test_values["LEMON_TEST_VARIANT_ID"],
        order_id=order_id,
        event_type="order_refunded",
        timeout_seconds=args.timeout,
    )
    if refund_event.get("akcia") != platby.AKCIA_VRATENE:
        raise SmokeFailed("Podpísaná refundácia neodobrala Premium nárok.")
    if _payment_status(opener, args.base_url.rstrip("/"))["ma_narok"] is not False:
        raise SmokeFailed("Úplná refundácia neodobrala Premium.")

    with closing(server.db()) as con:
        unresolved = int(con.execute(
            """SELECT COUNT(*) FROM payment_cases
                 WHERE provider_order_id=? AND status='open'""",
            (order_id,),
        ).fetchone()[0])
    if unresolved:
        raise SmokeFailed("Po teste ostal nevyriešený platobný prípad.")

    marker = _build_completed_marker(
        purchase_event=purchase_event,
        refund_event=refund_event,
        release=release,
        live_checkout_url=live_values["LEMON_CHECKOUT_URL"],
        live_store_id=live_values["LEMON_STORE_ID"],
        live_variant_id=live_values["LEMON_VARIANT_ID"],
        test_store_id=test_values["LEMON_TEST_STORE_ID"],
        test_variant_id=test_values["LEMON_TEST_VARIANT_ID"],
        order_id=order_id,
        receipt_email_verified=True,
        unresolved_cases=unresolved,
        completed_at=dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        signing_secret=signing_secret,
        create_marker=create_marker,
        live_config_fingerprint=live_config_fingerprint,
        sign_marker=sign_marker,
    )
    _write_marker(args.marker, marker)
    print("OK: nákup, Premium, úplná refundácia aj odobratie Premium prešli.")
    print("Platby ostali vypnuté; ich zapnutie vyžaduje samostatné schválenie.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SmokeFailed as error:
        print(f"SMOKE ZLYHAL: {error}", file=sys.stderr)
        raise SystemExit(1)
