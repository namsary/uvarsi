"""Paid checkout must rely only on reviewed, facts-only price sources."""

import datetime
import json
import sqlite3

from app import source_policy


def collection_db():
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.execute(
        """CREATE TABLE zber_stav (
          tyzden TEXT, obchod TEXT, stav TEXT, pocet INTEGER,
          collector_kind TEXT, source_fingerprint TEXT,
          valid_from TEXT, valid_to TEXT,
          PRIMARY KEY (tyzden, obchod)
        )"""
    )
    return con


def add_source(con, store, *, kind="manual-reviewed-facts", start="2026-09-07",
               end="2026-09-13", status="ok", count=30, fingerprint="a" * 64):
    con.execute(
        """INSERT INTO zber_stav
           (tyzden,obchod,stav,pocet,collector_kind,source_fingerprint,valid_from,valid_to)
           VALUES ('2026-09-07',?,?,?,?,?,?,?)""",
        (store, status, count, kind, fingerprint, start, end),
    )


def test_unknown_or_unapproved_collector_blocks_payment_readiness():
    assert source_policy.approved_source("Lidl", "unknown-proxy") is False
    assert source_policy.approved_source("Lidl", "official-lidl-viewer") is False
    assert source_policy.approved_source("Tesco", "mletaky-aggregator") is False
    assert source_policy.approved_source("Kaufland", "kupino-aggregator") is False
    assert source_policy.approved_source("Lidl", "manual-reviewed-facts") is True


def test_public_status_contains_no_collector_url_or_foreign_asset():
    status = source_policy.source_policy_status()
    encoded = json.dumps(status).lower()

    assert "http" not in encoded
    assert "source_url" not in encoded
    assert "image" not in encoded
    assert status["facts_only"] is True
    assert status["current_collectors_approved"] is False


def test_all_three_current_sources_with_internal_provenance_are_required():
    con = collection_db()
    for store in source_policy.REQUIRED_STORES:
        add_source(con, store)
    con.commit()

    assert source_policy.collection_is_approved(
        con, week="2026-09-07", today=datetime.date(2026, 9, 9)
    ) is True

    con.execute("DELETE FROM zber_stav WHERE obchod='Tesco'")
    assert source_policy.collection_is_approved(
        con, week="2026-09-07", today=datetime.date(2026, 9, 9)
    ) is False


def test_expired_incomplete_or_current_unreviewed_source_is_never_approved():
    scenarios = (
        {"end": "2026-09-08"},
        {"count": 0},
        {"status": "fail"},
        {"fingerprint": ""},
        {"kind": "official-lidl-viewer"},
    )
    for change in scenarios:
        con = collection_db()
        for store in source_policy.REQUIRED_STORES:
            add_source(con, store, **(change if store == "Lidl" else {}))
        assert source_policy.collection_is_approved(
            con, week="2026-09-07", today=datetime.date(2026, 9, 9)
        ) is False
        con.close()


def test_collector_kind_is_derived_only_from_strict_known_hosts():
    assert source_policy.collector_kind_for_url(
        "https://www.lidl.sk/l/sk/letak/weekly/view/flyer/page/1"
    ) == "official-lidl-viewer"
    assert source_policy.collector_kind_for_url(
        "https://www.kupino.sk/letak/tesco-letak"
    ) == "kupino-aggregator"
    assert source_policy.collector_kind_for_url(
        "https://app.mletaky.sk/260913_260907_lidl_a"
    ) == "mletaky-aggregator"
    assert source_policy.collector_kind_for_url(
        "https://www.lidl.sk.evil.example/fake"
    ) is None
    assert source_policy.collector_kind_for_url(
        "https://www.lidl.sk:invalid/fake"
    ) is None


def test_customer_payload_keeps_price_facts_but_strips_technical_provenance():
    plan = {
        "jedla": [{
            "nazov": "Rizoto",
            "suroviny": [{
                "nazov": "Ryža", "obchod": "Tesco", "cena": "1,39",
                "valid_to": "2026-09-13", "source_page": 12,
                "source_url": "https://internal.example/flyer",
                "thumbnail_url": "https://internal.example/thumb.jpg",
                "image_url": "https://internal.example/page.jpg",
            }],
        }],
    }

    public = source_policy.public_plan_payload(plan)
    encoded = json.dumps(public)

    assert public["jedla"][0]["suroviny"][0]["cena"] == "1,39"
    assert public["jedla"][0]["suroviny"][0]["valid_to"] == "2026-09-13"
    assert "source_url" not in encoded
    assert "source_page" not in encoded
    assert "thumbnail_url" not in encoded
    assert "image_url" not in encoded
