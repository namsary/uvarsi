#!/usr/bin/env python3
"""Uvar.si — rekonciliácia platieb: čo nepriniesol webhook, dobehne toto.

PREČO VÔBEC. Webhook je jediné, čím sa appka doteraz dozvedela o platbe. Keď
nedorazí — výpadok siete, reštart služby, zle nastavený vypínač, chyba na strane
poskytovateľa — zákazník zaplatil, členstvo nedostal a NIKTO sa to nedozvie.
Poskytovateľ doručenie pár ráz zopakuje a potom ho zahodí. Majiteľ sa o tom
dozvie z reklamácie, ak vôbec.

AKO. Raz za hodinu si vypýtame zoznam objednávok z API poskytovateľa (zdroj
pravdy o peniazoch) a porovnáme ho s tým, čo máme v databáze. Chýbajúce doplníme,
zmeškané vrátenia dobehneme.

ČO SA ZÁMERNE NEROBÍ. Rekonciliácia NEMÁ vlastnú cestu k udeleniu nároku. Každú
objednávku prepíše do presne toho tvaru, v akom chodí webhook, a pošle ju cez
`platby.spracuj_udalost` — teda cez tú istú idempotenciu, tú istú kontrolu
kapacity a tie isté UNIQUE obmedzenia. Preto je bezpečné spustiť ju koľkokrát
treba: kľúč udalosti je odvodený z ID OBJEDNÁVKY, takže druhý beh skončí na
`uz_spracovane` a druhý nárok nevznikne.

PORADIE V JEDNOM BEHU:
  1. odložené telá (webhooky prijaté s vypnutými platbami) — s overením podpisu,
  2. objednávky z API poskytovateľa,
  3. upratanie starých kľúčov udalostí,
  4. upozornenia majiteľovi (ntfy) — bez akéhokoľvek osobného údaju.

VYPÍNAČ. Beží aj s vypnutým PLATBY_ZAPNUTE, a to zámerne: práve zle nastavený
vypínač je jedna z ciest, ako sa peniaze strácajú, a keby ho rekonciliácia
rešpektovala, dieru by nezaplátala. Bránou je namiesto toho prítomnosť kľúčov
v /opt/uvarsi/uvarsi.env — bez nich skript neurobí nič. Nárok tak stále nevie
udeliť nikto z internetu, len majiteľ so svojimi kľúčmi.

Nastavenie (do /opt/uvarsi/uvarsi.env, nikdy nie do repozitára):
    LEMON_API_KEY=...          # API kľúč z LemonSqueezy (Settings → API)
    LEMON_STORE_ID=...         # nepovinné, ale odporúčané: len tvoj obchod
    LEMON_WEBHOOK_SECRET=...   # už je nastavený kvôli webhooku
    LEMON_VARIANT_ID=...       # už je nastavený kvôli webhooku

Beh (cron, každú hodinu — riadok inštaluje nasad.ps1):
    5 * * * * cd /opt/uvarsi/app && /opt/uvarsi/venv/bin/python rekonciliacia.py \
              >> /var/log/uvarsi-platby.log 2>&1
"""
import hashlib
import json
import math
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import db_rezim  # noqa: E402
import customer_requests  # noqa: E402
import naklady  # noqa: E402
import platby  # noqa: E402
import predplatne  # noqa: E402

DB = os.environ.get("UVARSI_DB", "/opt/uvarsi/uvarsi.db")
ENV_FILE = os.environ.get("UVARSI_ENV_FILE", "/opt/uvarsi/uvarsi.env")
API_URL = "https://api.lemonsqueezy.com/v1/orders"
SUBSCRIPTIONS_API_URL = "https://api.lemonsqueezy.com/v1/subscriptions"
SUBSCRIPTION_INVOICES_API_URL = (
    "https://api.lemonsqueezy.com/v1/subscription-invoices"
)
STRANA = 100
# Ochrana pred poškodeným alebo nepriateľským pagination metadata. Na rozdiel
# od pôvodného päťstranového limitu sa po dosiahnutí tejto hranice nikdy
# nevráti čiastočný výsledok, ale celý snapshot sa odmietne.
MAX_PROVIDER_PAGES = 10_000
CAS_SPOJENIA = 20


def env(kluc, default=None):
    """Tajomstvá výhradne z prostredia alebo env súboru servera — nikdy z kódu."""
    if os.environ.get(kluc):
        return os.environ[kluc]
    try:
        with open(ENV_FILE, "r", encoding="utf-8") as subor:
            for riadok in subor:
                riadok = riadok.strip()
                if not riadok or riadok.startswith("#"):
                    continue
                if riadok.startswith("export "):
                    riadok = riadok[len("export "):]
                meno, _, hodnota = riadok.partition("=")
                if meno.strip() == kluc:
                    return hodnota.strip().strip('"').strip("'")
    except OSError:
        pass
    return default


# ---------------------------------------------------------------- API
def stiahni_stranu(
    api_key, *, store_id=None, strana=1, otvor=None, api_url=API_URL
):
    """Jedna strana objednávok z API poskytovateľa. Vracia (zoznam, je_dalsia)."""
    parametre = {"page[size]": STRANA, "page[number]": strana, "sort": "-createdAt"}
    if store_id:
        parametre["filter[store_id]"] = str(store_id)
    adresa = api_url + "?" + urllib.parse.urlencode(parametre)
    ziadost = urllib.request.Request(
        adresa,
        headers={
            "Accept": "application/vnd.api+json",
            "Content-Type": "application/vnd.api+json",
            "Authorization": f"Bearer {api_key}",
        },
    )
    otvor = otvor or urllib.request.urlopen
    try:
        with otvor(ziadost, timeout=CAS_SPOJENIA) as odpoved:
            telo = json.loads(odpoved.read().decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise ProviderUnavailable("provider_unavailable") from error
    return _validated_provider_page(
        telo, requested_page=strana, api_url=api_url
    )


def stiahni_objednavky(
    api_key, *, store_id=None, otvor=None, api_url=API_URL,
):
    objednavky = []
    expected_pagination = None
    seen_ids = set()
    strana = 1
    while True:
        davka, pagination = stiahni_stranu(
            api_key, store_id=store_id, strana=strana, otvor=otvor,
            api_url=api_url,
        )
        identity = (
            pagination["last_page"],
            pagination["per_page"],
            pagination["total"],
        )
        if expected_pagination is None:
            expected_pagination = identity
        elif identity != expected_pagination:
            raise ProviderUnavailable("provider_unavailable")
        for row in davka:
            identifier = row.get("id") if isinstance(row, dict) else None
            if (
                not isinstance(identifier, (str, int))
                or isinstance(identifier, bool)
                or str(identifier) in seen_ids
            ):
                raise ProviderUnavailable("provider_unavailable")
            seen_ids.add(str(identifier))
        objednavky.extend(davka)
        if pagination["next_page"] is None:
            break
        strana = pagination["next_page"]
    return objednavky


class ProviderUnavailable(RuntimeError):
    """The provider snapshot was not obtained; local access stays unchanged."""


def _page_integer(value, *, minimum=0):
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise ProviderUnavailable("provider_unavailable")
    return value


def _linked_page(url, *, api_url):
    if not isinstance(url, str) or not url:
        raise ProviderUnavailable("provider_unavailable")
    parsed = urllib.parse.urlparse(url)
    expected = urllib.parse.urlparse(api_url)
    if (
        parsed.scheme != expected.scheme
        or parsed.netloc != expected.netloc
        or parsed.path != expected.path
    ):
        raise ProviderUnavailable("provider_unavailable")
    values = urllib.parse.parse_qs(parsed.query).get("page[number]")
    if not isinstance(values, list) or len(values) != 1:
        raise ProviderUnavailable("provider_unavailable")
    try:
        return int(values[0])
    except (TypeError, ValueError) as error:
        raise ProviderUnavailable("provider_unavailable") from error


def _validated_provider_page(body, *, requested_page, api_url):
    """Validate Lemon's documented JSON:API page contract completely."""
    if not isinstance(body, dict):
        raise ProviderUnavailable("provider_unavailable")
    data = body.get("data")
    links = body.get("links")
    meta = body.get("meta")
    page = meta.get("page") if isinstance(meta, dict) else None
    if not isinstance(data, list) or not isinstance(links, dict) or not isinstance(page, dict):
        raise ProviderUnavailable("provider_unavailable")

    current = _page_integer(page.get("currentPage"), minimum=1)
    last = _page_integer(page.get("lastPage"), minimum=1)
    per_page = _page_integer(page.get("perPage"), minimum=1)
    total = _page_integer(page.get("total"), minimum=0)
    if (
        current != requested_page
        or current > last
        or per_page != STRANA
        or last > MAX_PROVIDER_PAGES
        or last != max(1, math.ceil(total / per_page))
    ):
        raise ProviderUnavailable("provider_unavailable")

    expected_count = min(per_page, max(0, total - (current - 1) * per_page))
    expected_from = None if total == 0 else (current - 1) * per_page + 1
    expected_to = None if total == 0 else expected_from + expected_count - 1
    if (
        len(data) != expected_count
        or page.get("from") != expected_from
        or page.get("to") != expected_to
    ):
        raise ProviderUnavailable("provider_unavailable")

    if _linked_page(links.get("first"), api_url=api_url) != 1:
        raise ProviderUnavailable("provider_unavailable")
    if _linked_page(links.get("last"), api_url=api_url) != last:
        raise ProviderUnavailable("provider_unavailable")
    next_url = links.get("next")
    if current < last:
        next_page = _linked_page(next_url, api_url=api_url)
        if next_page != current + 1:
            raise ProviderUnavailable("provider_unavailable")
    else:
        if next_url is not None:
            raise ProviderUnavailable("provider_unavailable")
        next_page = None
    return data, {
        "last_page": last,
        "per_page": per_page,
        "total": total,
        "next_page": next_page,
    }


def _row_attributes(row):
    if not isinstance(row, dict):
        return None
    attributes = row.get("attributes")
    return attributes if isinstance(attributes, dict) else None


def _row_subscription_id(row, attributes):
    candidate = row.get("id") if row.get("type") == "subscriptions" else None
    if candidate is None:
        candidate = attributes.get("subscription_id")
    return candidate


def _event_payload(
    row, *, event_name, expected, snapshot=None, invoice=False,
    subscription_row=None,
):
    """Project one provider row into the signed-webhook domain contract."""
    attributes = _row_attributes(row)
    if attributes is None:
        return None
    subscription_attributes = _row_attributes(subscription_row) or {}
    subscription_id = _row_subscription_id(row, attributes)
    if subscription_id is None:
        return None

    def value(name, *, fallback=None):
        current = attributes.get(name)
        if current is not None:
            return current
        current = subscription_attributes.get(name)
        if current is not None:
            return current
        return fallback

    projected = {
        "store_id": value("store_id"),
        "variant_id": value("variant_id"),
        "currency": value(
            "currency", fallback=(snapshot.currency if snapshot is not None else None)
        ),
        "test_mode": value("test_mode"),
        "order_id": value("order_id"),
        "customer_id": value("customer_id"),
        "subscription_id": subscription_id,
        "status": value("status"),
        "subscription_status": value("subscription_status"),
        "billing_reason": value("billing_reason"),
        "billing_period_start": value(
            "billing_period_start",
            fallback=(snapshot.period_start if snapshot is not None else None),
        ),
        "billing_period_end": value(
            "billing_period_end",
            fallback=(snapshot.period_end if snapshot is not None else None),
        ),
        "renews_at": value("renews_at"),
        "ends_at": value("ends_at"),
        "updated_at": value("updated_at"),
        "created_at": value("created_at"),
        "total": value("total"),
        "discount_id": value("discount_id"),
        "refunded_amount": value("refunded_amount", fallback=0),
    }
    resource_id = row.get("id")
    if resource_id is None:
        return None
    return {
        "meta": {"event_name": event_name, "custom_data": {}},
        "data": {
            "type": "subscription-invoices" if invoice else "subscriptions",
            "id": resource_id,
            "attributes": projected,
        },
    }


def _delivery_key(kind, payload):
    # Only the already-redacted event projection contributes to durable keys.
    projection = predplatne._event_payload_json(payload)
    digest = hashlib.sha256(
        f"{kind}\0{projection}".encode("utf-8")
    ).hexdigest()
    return f"{kind}:{digest}"


def _subscription_row_by_id(rows):
    result = {}
    for row in rows:
        attributes = _row_attributes(row)
        if attributes is None or row.get("type") != "subscriptions":
            continue
        identifier = row.get("id")
        if isinstance(identifier, (str, int)) and not isinstance(identifier, bool):
            result[str(identifier)] = row
    return result


def _normalized_invoice_row(row, *, snapshot, subscription_row):
    """Fill only facts provable from the invoice, subscription, and local state.

    Lemon subscription invoices do not repeat the subscription's order,
    customer, variant, or current billing boundary.  Reconciliation may join
    those authenticated resources, but it must not invent a paid period.
    """
    attributes = _row_attributes(row)
    subscription_attributes = _row_attributes(subscription_row) or {}
    if attributes is None or snapshot is None:
        return row

    normalized = dict(attributes)
    for name in (
        "store_id",
        "variant_id",
        "order_id",
        "customer_id",
        "test_mode",
        "subscription_status",
        "renews_at",
    ):
        if normalized.get(name) is None:
            source_name = "status" if name == "subscription_status" else name
            if subscription_attributes.get(source_name) is not None:
                normalized[name] = subscription_attributes[source_name]

    status = normalized.get("status")
    status = status.strip().casefold() if isinstance(status, str) else ""
    subscription_status = normalized.get("subscription_status")
    subscription_status = (
        subscription_status.strip().casefold()
        if isinstance(subscription_status, str)
        else ""
    )
    billing_reason = normalized.get("billing_reason")
    billing_reason = (
        billing_reason.strip().casefold()
        if isinstance(billing_reason, str)
        else ""
    )

    # A pending renewal plus a confirmed past-due subscription is the
    # provider's dunning state.  Project it into the same strict event contract
    # used by the webhook transition engine.
    if status == "pending" and subscription_status == "past_due":
        normalized["status"] = "failed"
        status = "failed"

    period_start = predplatne._timestamp(normalized.get("billing_period_start"))
    period_end = predplatne._timestamp(normalized.get("billing_period_end"))
    if period_start is None or period_end is None:
        if status == "paid" and billing_reason == "renewal":
            candidate_start = snapshot.paid_through
            candidate_end = predplatne._timestamp(
                subscription_attributes.get("renews_at")
            )
        else:
            candidate_start = snapshot.period_start
            candidate_end = snapshot.period_end
        if (
            candidate_start is not None
            and candidate_end is not None
            and candidate_start < candidate_end
        ):
            normalized["billing_period_start"] = candidate_start
            normalized["billing_period_end"] = candidate_end

    return {
        "type": row.get("type"),
        "id": row.get("id"),
        "attributes": normalized,
    }


def _apply_reconciliation_event(
    con, *, payload, now, expected, kind, summary, review_code
):
    try:
        result = predplatne.process_subscription_event(
            con,
            payload=payload,
            now=now,
            expected=expected,
            source="reconciliation",
            delivery_key=_delivery_key(kind, payload),
        )
    except (predplatne.SubscriptionEventRejected, ValueError, TypeError):
        summary["error_codes"].add(
            "invalid_invoice_row" if kind == "invoice" else "invalid_subscription_row"
        )
        summary["_current_drift"] += 1
        return None
    if result["duplicate"]:
        summary["duplicates"] += 1
    elif result["review_required"]:
        summary["error_codes"].add(review_code)
        summary["_current_drift"] += 1
    else:
        summary["applied_transitions"] += 1
    return result


def _invoice_event_name(attributes, snapshot, con, invoice_id):
    status = attributes.get("status")
    status = status.strip().casefold() if isinstance(status, str) else ""
    if status in {"refunded", "partial_refund"}:
        return "subscription_payment_refunded"
    if status in {"failed", "past_due"}:
        return "subscription_payment_failed"
    if status != "paid":
        return None

    period_start = predplatne._timestamp(attributes.get("billing_period_start"))
    period_end = predplatne._timestamp(attributes.get("billing_period_end"))
    explicitly_recovered = attributes.get("recovered") is True
    same_period_recovery = (
        snapshot is not None
        and snapshot.status in {"past_due", "unpaid", "expired"}
        and period_start == snapshot.period_start
        and period_end == snapshot.period_end
    )
    if explicitly_recovered or same_period_recovery:
        return "subscription_payment_recovered"

    existing = con.execute(
        "SELECT 1 FROM subscription_invoices "
        "WHERE provider='lemonsqueezy' AND test_mode=? "
        "AND provider_invoice_id=?",
        (int(snapshot.test_mode) if snapshot is not None else -1, str(invoice_id)),
    ).fetchone()
    return "duplicate" if existing is not None else "subscription_payment_success"


def _subscription_event_name(status, snapshot):
    if status == "expired":
        return "subscription_expired"
    if status == "cancelled":
        return "subscription_cancelled"
    if status == "paused":
        return "subscription_paused"
    if status == "active" and snapshot is not None and snapshot.status == "cancelled":
        return "subscription_resumed"
    return "subscription_updated"


def _reconcile_subscriptions_batch(
    con, *, provider_rows, invoice_rows, now, expected
) -> dict:
    """Reconcile provider snapshots through the webhook transition engine."""
    if provider_rows is None or invoice_rows is None:
        raise ProviderUnavailable("provider_unavailable")
    if not isinstance(provider_rows, (list, tuple)) or not isinstance(
        invoice_rows, (list, tuple)
    ):
        raise ProviderUnavailable("provider_unavailable")
    if (
        isinstance(now, bool)
        or not isinstance(now, (int, float))
        or not math.isfinite(float(now))
        or float(now) < 0
    ):
        raise ValueError("invalid reconciliation time")
    now = float(now)
    summary = {
        "seen_subscriptions": len(provider_rows),
        "seen_invoices": len(invoice_rows),
        "recovered_invoices": 0,
        "recovered_payments": 0,
        "applied_transitions": 0,
        "duplicates": 0,
        "subscription_drift": 0,
        "past_due": 0,
        "unpaid": 0,
        "expired": 0,
        "queued_webhooks": 0,
        "error_codes": set(),
        "_current_drift": 0,
    }
    subscription_rows = _subscription_row_by_id(provider_rows)

    # Payment evidence is applied first. A later generic active snapshot may
    # then confirm, but can never revive unpaid/expired access by itself.
    for row in invoice_rows:
        if not isinstance(row, dict) or row.get("type") != "subscription-invoices":
            summary["error_codes"].add("invalid_invoice_row")
            summary["_current_drift"] += 1
            continue
        attributes = _row_attributes(row)
        invoice_id = row.get("id") if isinstance(row, dict) else None
        subscription_id = (
            _row_subscription_id(row, attributes) if attributes is not None else None
        )
        test_mode = attributes.get("test_mode") if attributes is not None else None
        snapshot = (
            predplatne.subscription_for_provider(
                con,
                provider_subscription_id=subscription_id,
                test_mode=test_mode,
            )
            if subscription_id is not None and type(test_mode) is bool
            else None
        )
        normalized_row = _normalized_invoice_row(
            row,
            snapshot=snapshot,
            subscription_row=subscription_rows.get(str(subscription_id)),
        )
        attributes = _row_attributes(normalized_row)
        event_name = (
            _invoice_event_name(attributes, snapshot, con, invoice_id)
            if attributes is not None and invoice_id is not None
            else None
        )
        if event_name == "duplicate":
            summary["duplicates"] += 1
            continue
        if event_name is None:
            summary["error_codes"].add("invalid_invoice_row")
            summary["_current_drift"] += 1
            continue
        payload = _event_payload(
            normalized_row,
            event_name=event_name,
            expected=expected,
            snapshot=snapshot,
            invoice=True,
            subscription_row=subscription_rows.get(str(subscription_id)),
        )
        if payload is None:
            summary["error_codes"].add("invalid_invoice_row")
            summary["_current_drift"] += 1
            continue
        previous_status = snapshot.status if snapshot is not None else None
        result = _apply_reconciliation_event(
            con,
            payload=payload,
            now=now,
            expected=expected,
            kind="invoice",
            summary=summary,
            review_code="invoice_requires_review",
        )
        if result is not None and not result["review_required"] and not result["duplicate"]:
            if result["action"] == "invoice_recorded":
                summary["recovered_invoices"] += 1
                if previous_status in {"past_due", "unpaid", "expired"}:
                    summary["recovered_payments"] += 1
            elif result["action"] == "payment_recovered":
                summary["recovered_payments"] += 1

    for row in provider_rows:
        if not isinstance(row, dict) or row.get("type") != "subscriptions":
            summary["error_codes"].add("invalid_subscription_row")
            summary["_current_drift"] += 1
            continue
        attributes = _row_attributes(row)
        subscription_id = (
            _row_subscription_id(row, attributes) if attributes is not None else None
        )
        test_mode = attributes.get("test_mode") if attributes is not None else None
        snapshot = (
            predplatne.subscription_for_provider(
                con,
                provider_subscription_id=subscription_id,
                test_mode=test_mode,
            )
            if subscription_id is not None and type(test_mode) is bool
            else None
        )
        status = attributes.get("status") if attributes is not None else None
        status = status.strip().casefold() if isinstance(status, str) else ""
        payload = _event_payload(
            row,
            event_name=_subscription_event_name(status, snapshot),
            expected=expected,
            snapshot=snapshot,
        )
        if payload is None:
            summary["error_codes"].add("invalid_subscription_row")
            summary["_current_drift"] += 1
            continue
        _apply_reconciliation_event(
            con,
            payload=payload,
            now=now,
            expected=expected,
            kind="subscription",
            summary=summary,
            review_code="subscription_requires_review",
        )

    current_drift = summary.pop("_current_drift")
    summary.update(predplatne.subscription_health_counters(
        con, test_mode=predplatne._expected_value(expected, "test_mode")
    ))
    summary["subscription_drift"] = max(
        summary["subscription_drift"], current_drift
    )
    summary["error_codes"] = sorted(summary["error_codes"])
    return summary


def reconcile_subscriptions(
    con, *, provider_rows, invoice_rows, now, expected
) -> dict:
    """Apply one complete provider snapshot atomically and fail closed."""
    if provider_rows is None or invoice_rows is None:
        raise ProviderUnavailable("provider_unavailable")
    if not isinstance(provider_rows, (list, tuple)) or not isinstance(
        invoice_rows, (list, tuple)
    ):
        raise ProviderUnavailable("provider_unavailable")

    savepoint = "annual_subscription_reconciliation_batch"
    con.execute(f"SAVEPOINT {savepoint}")
    try:
        result = _reconcile_subscriptions_batch(
            con,
            provider_rows=provider_rows,
            invoice_rows=invoice_rows,
            now=now,
            expected=expected,
        )
        con.execute(f"RELEASE SAVEPOINT {savepoint}")
        return result
    except Exception:
        try:
            con.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
            con.execute(f"RELEASE SAVEPOINT {savepoint}")
        except sqlite3.OperationalError:
            pass
        return {
            "seen_subscriptions": len(provider_rows),
            "seen_invoices": len(invoice_rows),
            "recovered_invoices": 0,
            "recovered_payments": 0,
            "applied_transitions": 0,
            "duplicates": 0,
            "subscription_drift": 1,
            "past_due": 0,
            "unpaid": 0,
            "expired": 0,
            "queued_webhooks": 0,
            "error_codes": ["reconciliation_batch_failed"],
        }


def reconcile_subscriptions_from_api(
    con, *, api_key, store_id, now, expected, fetch=stiahni_objednavky
) -> dict:
    """Fetch both provider datasets before applying either one."""
    try:
        provider_rows = fetch(
            api_key, store_id=store_id, api_url=SUBSCRIPTIONS_API_URL
        )
        invoice_rows = fetch(
            api_key, store_id=store_id, api_url=SUBSCRIPTION_INVOICES_API_URL
        )
    except (
        urllib.error.URLError,
        urllib.error.HTTPError,
        OSError,
        ValueError,
        json.JSONDecodeError,
    ) as error:
        raise ProviderUnavailable("provider_unavailable") from error
    return reconcile_subscriptions(
        con,
        provider_rows=provider_rows,
        invoice_rows=invoice_rows,
        now=now,
        expected=expected,
    )


# ---------------------------------------------------------------- porovnanie
def rekonciluj(con, *, objednavky, now, variant_id=None, notifikuj=None,
               mailuj=None) -> dict:
    """Doplň, čo webhook nepriniesol. Nič neobchádza — všetko ide cez spracuj_udalost."""
    suhrn = {
        "videne": 0, "udelene": 0, "uz_spracovane": 0, "vratene": 0,
        "bez_uctu": 0, "ignorovane": 0, "nad_kapacitu": 0, "duplicitne": 0,
        "nepouzitelne": 0, "nespravny_rezim": 0,
    }
    # Platby, za ktoré zákazník nič nedostal. Keď ich objaví rekonciliácia (a nie
    # webhook), musí povedať to isté, čo by povedal webhook: majiteľovi na ntfy,
    # zákazníkovi e-mailom.
    nevybavene = []
    for objednavka in objednavky or ():
        suhrn["videne"] += 1
        if platby._atributy_objednavky(objednavka).get("test_mode") is not False:
            suhrn["nespravny_rezim"] += 1
            continue
        ref = platby._bezpecne_id(
            objednavka.get("id") if isinstance(objednavka, dict) else None
        )
        if not ref:
            continue
        stav = platby.stav_objednavky(objednavka)
        if stav in ("refunded", "partial_refund"):
            _vrat(con, objednavka, ref, now=now, suhrn=suhrn)
            continue
        if stav != "paid":
            # pending / failed: peniaze ešte (alebo už) nie sú naše.
            continue
        user_id = platby.user_id_z_objednavky(objednavka)
        if user_id is None:
            user_id = platby.ucet_podla_emailu(con, platby.email_z_objednavky(objednavka))
        if user_id is None:
            # Zaplatil pod inou adresou, než akou sa prihlasuje. Priradiť to za
            # neho by bolo hádanie; majiteľ to vyrieši cez premium_cli.py.
            if platby.narok_objednavky(con, ref) is None:
                suhrn["bez_uctu"] += 1
                platby.create_payment_case(
                    con,
                    case_type=platby.DRUH_BEZ_UCTU,
                    provider_order_id=ref,
                    user_id=None,
                    now=now,
                )
            continue
        payload = platby.payload_z_objednavky(objednavka, user_id=user_id)
        if payload is None:
            suhrn["nepouzitelne"] += 1
            platby.create_payment_case(
                con,
                case_type=platby.DRUH_NEPOUZITELNA,
                provider_order_id=ref,
                user_id=user_id,
                now=now,
            )
            continue
        try:
            vysledok = platby.spracuj_udalost(
                con, payload=payload, now=now, variant_id=variant_id,
                zdroj=platby.ZDROJ_REKONCILIACIA,
                expected_test_mode=False,
            )
        except platby.UdalostNepouzitelna:
            suhrn["nepouzitelne"] += 1
            platby.create_payment_case(
                con,
                case_type=platby.DRUH_NEPOUZITELNA,
                provider_order_id=ref,
                user_id=user_id,
                now=now,
            )
            continue
        akcia = vysledok["akcia"]
        if akcia == platby.AKCIA_UDELENE:
            suhrn["udelene"] += 1
        elif akcia == platby.AKCIA_UZ_SPRACOVANE:
            suhrn["uz_spracovane"] += 1
        elif akcia == platby.AKCIA_NAD_KAPACITU:
            suhrn["nad_kapacitu"] += 1
            nevybavene.append((platby.DRUH_NAD_KAPACITU, vysledok))
        elif akcia == platby.AKCIA_IGNOROVANE:
            suhrn["ignorovane"] += 1
            platby.create_payment_case(
                con,
                case_type=platby.DRUH_IGNOROVANE,
                provider_order_id=ref,
                user_id=user_id,
                now=now,
            )
        elif vysledok.get("stav") == platby.STAV_DUPLICITNY:
            suhrn["duplicitne"] += 1
            nevybavene.append((platby.DRUH_DUPLICITA, vysledok))
    _ohlas(con, suhrn, now=now, notifikuj=notifikuj)
    for druh, vysledok in nevybavene:
        _bez_protihodnoty(con, druh, vysledok, now=now, notifikuj=notifikuj,
                          mailuj=mailuj)
    return suhrn


TEXTY_BEZ_PROTIHODNOTY = {
    platby.DRUH_NAD_KAPACITU: (platby.MAIL_PREDMET_NAD_KAPACITU,
                               platby.SPRAVA_NAD_KAPACITU_ZAKAZNIK),
    platby.DRUH_DUPLICITA: (platby.MAIL_PREDMET_DUPLICITA,
                            platby.SPRAVA_DUPLICITA_ZAKAZNIK),
}


def _bez_protihodnoty(con, druh, vysledok, *, now, notifikuj=None, mailuj=None) -> None:
    """Zaplatil a nič nedostal. Povie to majiteľovi aj jemu — presne raz."""
    platby.create_payment_case(
        con,
        case_type=druh,
        provider_order_id=vysledok.get("objednavka"),
        user_id=vysledok.get("user_id"),
        now=now,
    )
    sprava = platby.upozornenie_raz(
        con,
        druh,
        now=now,
        pocet=platby.count_open_payment_cases(con, druh),
    )
    if sprava is None:
        return
    posli = naklady.posli_ntfy if notifikuj is None else notifikuj
    try:
        posli(sprava)
    except Exception:
        pass
    predmet, text = TEXTY_BEZ_PROTIHODNOTY[druh]
    napis = _posli_mail if mailuj is None else mailuj
    komu = platby.email_uctu(con, vysledok.get("user_id"))
    if not komu:
        return
    try:
        napis(komu, predmet, text)
    except Exception:
        # E-mail je najlepšia snaha. Aj keď neodíde, správa čaká na zákazníka
        # v appke (/api/platba/stav) a majiteľ o prípade vie z ntfy.
        pass


def _posli_mail(komu, predmet, text) -> None:
    import auth_data

    telo = f"Ahoj!\n\n{text}\n\nUvar.si — z letáka rovno na tanier\nhttps://uvar.si"
    auth_data.send_resend_message(
        api_key=env("RESEND_API_KEY"),
        sender=env("MAIL_FROM", "Uvar.si <info@uvar.si>"),
        recipient=komu,
        subject=predmet,
        text=telo,
        html="<p>" + text.replace("\n", "<br>") + "</p>",
    )


def _vrat(con, objednavka, ref, *, now, suhrn) -> None:
    """Zmeškané vrátenie: bez neho by Premium bežalo ďalej za vrátené peniaze."""
    payload = platby.payload_z_objednavky(objednavka, typ="order_refunded")
    if payload is None or platby.refund_kind(payload) != "full":
        suhrn["nepouzitelne"] += 1
        platby.create_payment_case(
            con,
            case_type=platby.DRUH_NEPOUZITELNA,
            provider_order_id=ref,
            user_id=platby.user_id_z_objednavky(objednavka),
            now=now,
        )
        return
    narok = platby.narok_objednavky(con, ref)
    if narok is None:
        return
    platby.close_payment_cases_for_refund(
        con, provider_order_id=ref, now=now
    )
    customer_requests.close_requests_for_refund(con, order_id=ref, now=now)
    if narok["stav"] != platby.STAV_AKTIVNY:
        con.commit()
        return
    payload = platby.payload_z_objednavky(
        objednavka, user_id=narok["user_id"], typ="order_refunded"
    )
    if payload is None:
        return
    try:
        vysledok = platby.spracuj_udalost(
            con, payload=payload, now=now, zdroj=platby.ZDROJ_REKONCILIACIA,
            expected_test_mode=False,
        )
    except platby.UdalostNepouzitelna:
        suhrn["nepouzitelne"] += 1
        return
    if vysledok["akcia"] == platby.AKCIA_VRATENE:
        suhrn["vratene"] += 1


def _ohlas(con, suhrn, *, now, notifikuj=None) -> None:
    """Upozornenia majiteľovi. Text skladá platby.py — bez osobných údajov."""
    posli = naklady.posli_ntfy if notifikuj is None else notifikuj
    spravy = []
    if suhrn["udelene"]:
        spravy.append(platby.upozornenie_raz(
            con, platby.DRUH_REKONCILIACIA, now=now, pocet=suhrn["udelene"]))
    if suhrn["bez_uctu"]:
        spravy.append(platby.upozornenie_raz(
            con, platby.DRUH_BEZ_UCTU, now=now,
            pocet=platby.count_open_payment_cases(con, platby.DRUH_BEZ_UCTU)))
    if suhrn["nepouzitelne"]:
        spravy.append(platby.upozornenie_raz(
            con, platby.DRUH_NEPOUZITELNA, now=now,
            pocet=platby.count_open_payment_cases(con, platby.DRUH_NEPOUZITELNA)))
    for sprava in spravy:
        if sprava is None:
            continue
        try:
            posli(sprava)
        except Exception:
            # Upozornenie je najlepšia snaha; nesmie zhodiť dobehnutie platieb.
            pass


# ---------------------------------------------------------------- beh
def main() -> int:
    api_key = env("LEMON_API_KEY")
    tajomstvo = env("LEMON_WEBHOOK_SECRET")
    variant = env("LEMON_VARIANT_ID")
    store_id = env("LEMON_STORE_ID")
    subscription_variant = env("LEMON_SUBSCRIPTION_VARIANT_ID")
    founder_discount = env("LEMON_FOUNDER_DISCOUNT_ID")
    now = time.time()

    if not api_key and not tajomstvo:
        print("REKONCILIACIA: v uvarsi.env nie je LEMON_API_KEY ani "
              "LEMON_WEBHOOK_SECRET — niet čo rekonciliovať, končím.")
        return 0

    con = db_rezim.otvor(DB)
    try:
        platby.migrate_platby_schema(con)
        predplatne.migrate_subscription_schema(con)
        customer_requests.migrate_customer_requests_schema(con)
        con.commit()

        if tajomstvo:
            odlozene = platby.spracuj_odlozene(
                con,
                tajomstvo=tajomstvo,
                now=now,
                variant_id=variant,
                expected_test_mode=False,
                subscription_expected={
                    "store_id": store_id,
                    "variant_id": subscription_variant,
                    "founder_discount_id": founder_discount,
                    "currency": "EUR",
                    "test_mode": False,
                },
            )
            if odlozene["spracovane"]:
                print("REKONCILIACIA: odložené telá — spracovaných "
                      f"{odlozene['spracovane']}, udelených {odlozene['udelene']}, "
                      f"neplatný podpis {odlozene['neplatny_podpis']}, "
                      f"nepoužiteľných {odlozene['nepouzitelne']}")
        else:
            print("REKONCILIACIA: bez LEMON_WEBHOOK_SECRET nevieme overiť podpisy "
                  "odložených tiel — preskakujem ich.")

        if api_key:
            try:
                objednavky = stiahni_objednavky(api_key, store_id=store_id)
            except (urllib.error.URLError, urllib.error.HTTPError, OSError,
                    ValueError, json.JSONDecodeError) as chyba:
                # Nedostupné API je dôvod skúsiť to o hodinu znova, nie dôvod
                # na paniku. Typ chyby stačí; telo odpovede môže niesť tajomstvá.
                print(f"REKONCILIACIA: API poskytovateľa neodpovedalo "
                      f"({type(chyba).__name__}) — skúsim o hodinu.")
                return 1
            suhrn = rekonciluj(
                con, objednavky=objednavky, now=now, variant_id=variant
            )
            print("REKONCILIACIA: objednávok " + ", ".join(
                f"{kluc} {hodnota}" for kluc, hodnota in sorted(suhrn.items())
            ))
            if all((store_id, subscription_variant, founder_discount)):
                try:
                    annual = reconcile_subscriptions_from_api(
                        con,
                        api_key=api_key,
                        store_id=store_id,
                        now=now,
                        expected={
                            "store_id": store_id,
                            "variant_id": subscription_variant,
                            "founder_discount_id": founder_discount,
                            "currency": "EUR",
                            "test_mode": False,
                        },
                    )
                except ProviderUnavailable:
                    print(
                        "REKONCILIACIA: ročné predplatné sa neoverilo "
                        "(provider_unavailable); lokálny prístup ostal nezmenený."
                    )
                    return 1
                print(
                    "REKONCILIACIA: ročné predplatné — "
                    + ", ".join(
                        f"{key} {value}"
                        for key, value in sorted(annual.items())
                    )
                )
                if any(
                    annual[name]
                    for name in (
                        "subscription_drift",
                        "past_due",
                        "unpaid",
                        "expired",
                        "queued_webhooks",
                    )
                ) or annual["error_codes"]:
                    alert = platby.priprav_subscription_reconciliation_alert(
                        annual, den=time.strftime("%Y-%m-%d", time.gmtime(now))
                    )
                    if platby.zaznamenaj_upozornenie(
                        con, kluc=alert["kluc"], now=now
                    ):
                        try:
                            naklady.posli_ntfy(alert)
                        except Exception:
                            pass
            else:
                print(
                    "REKONCILIACIA: ročné predplatné preskočené "
                    "(subscription_config_incomplete)."
                )
        else:
            print("REKONCILIACIA: bez LEMON_API_KEY sa objednávky nedajú overiť "
                  "— dopĺňam len odložené telá.")

        zmazane = platby.uprac_udalosti(con, now=now)
        if zmazane:
            print(f"REKONCILIACIA: upratanych starych kľúčov udalostí: {zmazane}")
    finally:
        con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
