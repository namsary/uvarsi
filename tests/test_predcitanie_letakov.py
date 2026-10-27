"""Kalendár a predčítanie letákov: nový leták sa prečíta vopred a v deň
platnosti sa zverejní bez ďalšieho plateného čítania (incident 26. 10.)."""
import sqlite3
import sys
import types
from datetime import date

import pytest

from app import zbierac_akcii as collector
from tests.test_zbierac_akcii import prepared_collection, valid_offer

PIATOK = date(2026, 8, 21)
NEDELA = date(2026, 8, 23)
PONDELOK = date(2026, 8, 24)


def next_offer(store, index):
    offer = valid_offer(store, index)
    offer["valid_from"], offer["valid_to"] = "2026-08-24", "2026-08-30"
    return offer


def next_prepared(store, *, page_tag="v1"):
    prepared = prepared_collection(store)
    manifest = dict(prepared.manifest)
    manifest["source_identity"] = f"test-manifest:{store}:2026-08-24"
    manifest["valid_from"], manifest["valid_to"] = "2026-08-24", "2026-08-30"
    manifest["pages"] = [
        dict(page, image_url=page["image_url"].replace(".jpg", f"-{page_tag}.jpg"))
        for page in manifest["pages"]
    ]
    return collector.PreparedCollection(
        pages=prepared.pages,
        manifest=manifest,
        page_manifest={row["source_page"]: row for row in manifest["pages"]},
        provenance=collector.provenance_from_manifest(manifest),
    )


@pytest.fixture
def svet(monkeypatch, tmp_path):
    """Lidl a Tesco: aktuálny leták do nedele, nový od pondelka už ohlásený."""
    database = tmp_path / "uvarsi.db"
    monkeypatch.setattr(collector, "DB", str(database))
    monkeypatch.setattr(collector, "load_key", lambda: "unused-test-value")
    monkeypatch.setattr(collector.source_policy, "approved_source", lambda *_args: True)
    monkeypatch.setitem(
        sys.modules, "anthropic",
        types.SimpleNamespace(Anthropic=lambda **kwargs: object()),
    )
    stav = {"ohlaseny": {"lidl", "tesco"}, "citania": [], "tag": "v1"}

    def store_pages(store, today=None):
        if today >= PONDELOK and store in stav["ohlaseny"]:
            prepared = next_prepared(store, page_tag=stav["tag"])
        else:
            prepared = prepared_collection(store)
        return prepared.pages, prepared.manifest

    def prepare(store, today=None):
        if today is not None and today >= PONDELOK and store in stav["ohlaseny"]:
            return next_prepared(store, page_tag=stav["tag"])
        if today is None and collector.business_day() >= PONDELOK:
            return next_prepared(store, page_tag=stav["tag"])
        return prepared_collection(store)

    def zbieraj(client, store, prepared=None):
        stav["citania"].append(store)
        return [next_offer(store, index) for index in range(1, 21)]

    monkeypatch.setattr(collector, "store_pages", store_pages)
    monkeypatch.setattr(collector, "prepare_store_collection", prepare)
    monkeypatch.setattr(collector, "zbieraj", zbieraj)
    stav["database"] = database
    return stav


def kalendar(database):
    con = sqlite3.connect(database)
    rows = con.execute(
        "SELECT obchod, valid_from, valid_to, stav, pocet FROM letaky_kalendar "
        "ORDER BY obchod, valid_from"
    ).fetchall()
    con.close()
    return rows


def test_announced_leaflet_is_read_ahead_and_written_to_calendar(svet):
    result = collector.prefetch_upcoming_leaflets(today=PIATOK)

    assert sorted(result["precitane"]) == ["lidl", "tesco"]
    assert sorted(svet["citania"]) == ["lidl", "tesco"]
    assert ("Lidl", "2026-08-24", "2026-08-30", "precitany", 20) in kalendar(svet["database"])


def test_read_ahead_leaflet_is_not_paid_for_twice(svet):
    collector.prefetch_upcoming_leaflets(today=PIATOK)
    svet["citania"].clear()

    result = collector.prefetch_upcoming_leaflets(today=date(2026, 8, 22))

    assert svet["citania"] == []
    assert sorted(result["pripravene"]) == ["lidl", "tesco"]


def test_currently_valid_leaflet_is_left_to_the_regular_collection(svet):
    svet["ohlaseny"].clear()

    result = collector.prefetch_upcoming_leaflets(today=PIATOK)

    assert svet["citania"] == []
    assert result["precitane"] == []


def test_monday_publishes_read_ahead_leaflet_without_another_paid_read(svet, monkeypatch):
    collector.prefetch_upcoming_leaflets(today=PIATOK)
    monkeypatch.setattr(collector, "monday", lambda: "2026-08-24")
    monkeypatch.setattr(collector, "business_day", lambda: PONDELOK)
    monkeypatch.setattr(collector, "STORES", ["lidl"])
    monkeypatch.setattr(
        collector, "zbieraj",
        lambda *_args, **_kwargs: pytest.fail("vopred prečítaný leták sa nemá platiť znova"),
    )

    collector.main(["lidl"])

    con = collector.db()
    try:
        assert collector.staged_store_problem(
            con, "2026-08-24", "Lidl", today=PONDELOK
        ) is None
    finally:
        con.close()


def test_leaflet_changed_after_read_ahead_is_read_again(svet, monkeypatch):
    collector.prefetch_upcoming_leaflets(today=PIATOK)
    svet["tag"] = "v2"  # obchod medzitým vymenil strany letáku
    svet["citania"].clear()
    monkeypatch.setattr(collector, "monday", lambda: "2026-08-24")
    monkeypatch.setattr(collector, "business_day", lambda: PONDELOK)
    monkeypatch.setattr(collector, "STORES", ["lidl"])

    collector.main(["lidl"])

    assert svet["citania"] == ["lidl"]


def test_failed_read_ahead_is_retried_only_within_limit(svet, monkeypatch):
    def broken(client, store, prepared=None):
        svet["citania"].append(store)
        raise ValueError("nečitateľná strana")

    monkeypatch.setattr(collector, "zbieraj", broken)
    for day in (21, 21, 22):
        collector.prefetch_upcoming_leaflets(today=date(2026, 8, day))

    assert svet["citania"].count("lidl") == collector.PREDCITANIE_MAX_POKUSOV


def _active_leaflet_ends(database, store, valid_to):
    con = collector.db()
    try:
        with con:
            con.execute(
                """INSERT INTO zber_stav (tyzden, obchod, stav, pocet, valid_from, valid_to)
                   VALUES ('2026-08-17', ?, 'ok', 20, '2026-08-17', ?)""",
                (store, valid_to),
            )
    finally:
        con.close()


def test_owner_is_warned_when_store_has_not_announced_tomorrows_leaflet(svet):
    svet["ohlaseny"].clear()
    _active_leaflet_ends(svet["database"], "Lidl", "2026-08-23")

    result = collector.prefetch_upcoming_leaflets(today=NEDELA)

    assert "lidl:neohlaseny" in result["nepripravene_zajtra"]


def test_owner_is_warned_when_tomorrows_leaflet_is_not_read(svet, monkeypatch):
    def broken(client, store, prepared=None):
        raise ValueError("nečitateľná strana")

    monkeypatch.setattr(collector, "zbieraj", broken)

    result = collector.prefetch_upcoming_leaflets(today=NEDELA)

    assert "lidl:neprecitany" in result["nepripravene_zajtra"]


def test_ready_tomorrow_is_quiet(svet):
    collector.prefetch_upcoming_leaflets(today=PIATOK)

    result = collector.prefetch_upcoming_leaflets(today=NEDELA)

    assert result["nepripravene_zajtra"] == []


def test_prefetch_cli_prints_lines_for_the_supervisor(monkeypatch, capsys):
    monkeypatch.setattr(
        collector, "prefetch_upcoming_leaflets",
        lambda today=None: {"precitane": ["lidl"], "pripravene": [], "zlyhane": [],
                            "nepripravene_zajtra": ["tesco:neohlaseny"]},
    )

    assert collector.cli(["--prefetch"]) == 0

    out = capsys.readouterr().out
    assert "PRECITANE_LETAKY=lidl\n" in out
    assert "NEPRIPRAVENE_ZAJTRA=tesco:neohlaseny\n" in out


def test_calendar_cli_lists_leaflet_dates(svet, capsys):
    collector.prefetch_upcoming_leaflets(today=PIATOK)

    assert collector.cli(["--kalendar"]) == 0

    out = capsys.readouterr().out
    assert "Lidl" in out and "2026-08-24 – 2026-08-30" in out and "precitany" in out
