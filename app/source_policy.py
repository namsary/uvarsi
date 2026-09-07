"""Fail-closed registry for price-source provenance and public data shaping."""

from __future__ import annotations

import copy
import datetime
import hashlib
import re
from urllib.parse import urlsplit


REQUIRED_STORES = ("Kaufland", "Tesco", "Lidl")
MIN_FACTS_PER_STORE = 20
FACTS_ONLY_SHAPE = "product-price-validity"
APPROVED = "approved"
PENDING_PERMISSION = "pending_permission"

# Approval is code-reviewed server configuration, never a browser flag.  The
# current automated readers remain useful to the free beta, but cannot unlock
# paid checkout until their terms/licence have a documented defensible basis.
_REGISTRY = {
    ("Lidl", "official-lidl-viewer"): {
        "shape": FACTS_ONLY_SHAPE,
        "review_date": "2026-09-07",
        "reviewer": "external-review-required",
        "status": PENDING_PERMISSION,
    },
    ("Lidl", "kupino-aggregator"): {
        "shape": FACTS_ONLY_SHAPE,
        "review_date": "2026-09-07",
        "reviewer": "external-review-required",
        "status": PENDING_PERMISSION,
    },
    ("Lidl", "mletaky-aggregator"): {
        "shape": FACTS_ONLY_SHAPE,
        "review_date": "2026-09-07",
        "reviewer": "external-review-required",
        "status": PENDING_PERMISSION,
    },
    ("Kaufland", "kupino-aggregator"): {
        "shape": FACTS_ONLY_SHAPE,
        "review_date": "2026-09-07",
        "reviewer": "external-review-required",
        "status": PENDING_PERMISSION,
    },
    ("Kaufland", "mletaky-aggregator"): {
        "shape": FACTS_ONLY_SHAPE,
        "review_date": "2026-09-07",
        "reviewer": "external-review-required",
        "status": PENDING_PERMISSION,
    },
    ("Tesco", "kupino-aggregator"): {
        "shape": FACTS_ONLY_SHAPE,
        "review_date": "2026-09-07",
        "reviewer": "external-review-required",
        "status": PENDING_PERMISSION,
    },
    ("Tesco", "mletaky-aggregator"): {
        "shape": FACTS_ONLY_SHAPE,
        "review_date": "2026-09-07",
        "reviewer": "external-review-required",
        "status": PENDING_PERMISSION,
    },
    # A controlled import may be approved only after the operator has checked
    # its licence/permission and uploads facts, not pages or visual assets.
    ("Kaufland", "manual-reviewed-facts"): {
        "shape": FACTS_ONLY_SHAPE,
        "review_date": "2026-09-07",
        "reviewer": "PUMAR",
        "status": APPROVED,
    },
    ("Tesco", "manual-reviewed-facts"): {
        "shape": FACTS_ONLY_SHAPE,
        "review_date": "2026-09-07",
        "reviewer": "PUMAR",
        "status": APPROVED,
    },
    ("Lidl", "manual-reviewed-facts"): {
        "shape": FACTS_ONLY_SHAPE,
        "review_date": "2026-09-07",
        "reviewer": "PUMAR",
        "status": APPROVED,
    },
}

_TECHNICAL_SOURCE_FIELDS = frozenset({
    "source_url", "source_page", "thumbnail_url", "image_url", "flyer_id",
})
_FINGERPRINT = re.compile(r"^[0-9a-f]{64}$")


def approved_source(store: str, collector_kind: str) -> bool:
    record = _REGISTRY.get((str(store).strip().capitalize(), collector_kind))
    return bool(
        record
        and record["status"] == APPROVED
        and record["shape"] == FACTS_ONLY_SHAPE
        and record["review_date"]
        and record["reviewer"]
    )


def known_source(store: str, collector_kind: str) -> bool:
    return (str(store).strip().capitalize(), collector_kind) in _REGISTRY


def collector_kind_for_url(value) -> str | None:
    if not isinstance(value, str) or value != value.strip():
        return None
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        return None
    if parsed.scheme != "https" or parsed.username or parsed.password or port:
        return None
    host = (parsed.hostname or "").lower()
    if host == "www.lidl.sk":
        return "official-lidl-viewer"
    if host == "www.kupino.sk":
        return "kupino-aggregator"
    if host == "app.mletaky.sk":
        return "mletaky-aggregator"
    return None


def source_fingerprint(source_url: str) -> str:
    kind = collector_kind_for_url(source_url)
    if kind is None:
        raise ValueError("unknown source URL")
    return hashlib.sha256(source_url.encode("utf-8")).hexdigest()


def source_policy_status() -> dict:
    current = {
        "Kaufland": ("kupino-aggregator", "mletaky-aggregator"),
        "Tesco": ("kupino-aggregator", "mletaky-aggregator"),
        "Lidl": (
            "official-lidl-viewer", "kupino-aggregator", "mletaky-aggregator",
        ),
    }
    stores = []
    for store in REQUIRED_STORES:
        kinds = current[store]
        stores.append({
            "store": store,
            "collectors": [
                {"kind": kind, "status": _REGISTRY[(store, kind)]["status"]}
                for kind in kinds
            ],
        })
    return {
        "facts_only": True,
        "current_collectors_approved": all(
            approved_source(store, kind)
            for store, kinds in current.items()
            for kind in kinds
        ),
        "stores": stores,
    }


def collection_is_approved(con, *, week: str, today: datetime.date) -> bool:
    """Require current, complete, internally attributable rows for all stores."""
    if not isinstance(today, datetime.date) or isinstance(today, datetime.datetime):
        return False
    columns = {row[1] for row in con.execute("PRAGMA table_info(zber_stav)")}
    required = {
        "tyzden", "obchod", "stav", "pocet", "collector_kind",
        "source_fingerprint", "valid_from", "valid_to",
    }
    if not required <= columns:
        return False
    rows = con.execute(
        """SELECT obchod,stav,pocet,collector_kind,source_fingerprint,
                  valid_from,valid_to
           FROM zber_stav WHERE tyzden=?""",
        (week,),
    ).fetchall()
    by_store = {str(row[0]).capitalize(): row for row in rows}
    for store in REQUIRED_STORES:
        row = by_store.get(store)
        if row is None or row[1] != "ok" or int(row[2] or 0) < MIN_FACTS_PER_STORE:
            return False
        if not approved_source(store, row[3]):
            return False
        if not isinstance(row[4], str) or _FINGERPRINT.fullmatch(row[4]) is None:
            return False
        try:
            valid_from = datetime.date.fromisoformat(row[5])
            valid_to = datetime.date.fromisoformat(row[6])
        except (TypeError, ValueError):
            return False
        if not valid_from <= today <= valid_to:
            return False
    return True


def public_plan_payload(plan):
    """Deep-copy a plan while removing internal flyer locations and assets."""
    def scrub(value):
        if isinstance(value, dict):
            return {
                key: scrub(item)
                for key, item in value.items()
                if key not in _TECHNICAL_SOURCE_FIELDS
            }
        if isinstance(value, list):
            return [scrub(item) for item in value]
        return copy.deepcopy(value)

    return scrub(plan)
