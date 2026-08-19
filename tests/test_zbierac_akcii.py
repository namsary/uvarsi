import re
import sqlite3
import sys
import types

import pytest

from app import zbierac_akcii as collector
from app.offer_data import replace_store_week


def flyer_fixture(page_count):
    pages = [
        (f"https://images.example/thumb-{page}.jpg", f"https://images.example/full-{page}.jpg")
        for page in range(1, page_count + 1)
    ]
    manifest = {
        "source_url": "https://flyers.example/lidl-current",
        "valid_from": "2026-08-17",
        "valid_to": "2026-08-23",
        "pages": [
            {
                "source_page": page,
                "thumbnail_url": thumb,
                "image_url": full,
            }
            for page, (thumb, full) in enumerate(pages, start=1)
        ],
    }
    return pages, manifest


def install_pipeline_fakes(monkeypatch, page_count, food_pages, extracted_pages=None):
    pages, manifest = flyer_fixture(page_count)
    monkeypatch.setattr(collector, "store_pages", lambda store: (pages, manifest))
    thumbnail_reads = []
    scan_batches = []
    read_batches = []

    def fake_get_b64(url, max_px):
        if max_px == collector.SCAN_PX:
            thumbnail_reads.append(url)
        return url

    def labeled_pages(content):
        labels = []
        for block in content:
            if block.get("type") == "text":
                match = re.fullmatch(r"(?:Strana|Zdrojová strana) (\d+):", block["text"])
                if match:
                    labels.append(int(match.group(1)))
        if labels:
            return labels
        for block in content:
            if block.get("type") == "image":
                match = re.search(r"full-(\d+)\.jpg", block["source"]["data"])
                if match:
                    labels.append(int(match.group(1)))
        return labels

    def fake_claude_json(client, model, content, max_tokens, effort=None):
        batch_pages = labeled_pages(content)
        if model == collector.MODEL_SCAN:
            scan_batches.append(batch_pages)
            return [page for page in batch_pages if page in food_pages]

        read_batches.append(batch_pages)
        result_pages = extracted_pages if extracted_pages is not None else batch_pages
        return [
            {
                "source_page": page,
                "nazov": f"Potravina {page}",
                "kategoria": "trvanlive",
                "cena": 1.0 + page / 100,
                "povodna": None,
                "zlava": None,
                "jednotka": "ks",
            }
            for page in result_pages
        ]

    monkeypatch.setattr(collector, "get_b64", fake_get_b64)
    monkeypatch.setattr(collector, "claude_json", fake_claude_json)
    return manifest, thumbnail_reads, scan_batches, read_batches


def test_discovers_ninety_sequential_pages_until_terminal_miss(monkeypatch):
    monkeypatch.setattr(
        collector,
        "kupino_meta",
        lambda store: ("42", "lidl-letak", "/letak/lidl-2026-08-17-2026-08-23"),
    )

    def page_marker(url):
        page = int(re.search(r"-(\d+)_320\.jpg$", url).group(1))
        return f"page-{page}" if page <= 90 else None

    monkeypatch.setattr(collector, "page_exists", page_marker)

    pages, manifest = collector.store_pages("lidl")

    assert len(pages) == 90
    assert manifest["source_url"] == "https://www.kupino.sk/letak/lidl-2026-08-17-2026-08-23"
    assert manifest["valid_from"] == "2026-08-17"
    assert manifest["valid_to"] == "2026-08-23"
    assert manifest["pages"][-1]["source_page"] == 90


def test_page_discovery_stops_when_provider_repeats_a_page(monkeypatch):
    monkeypatch.setattr(
        collector,
        "kupino_meta",
        lambda store: ("42", "lidl-letak", "/letak/lidl-2026-08-17-2026-08-23"),
    )

    def page_marker(url):
        page = int(re.search(r"-(\d+)_320\.jpg$", url).group(1))
        return {1: "first", 2: "second", 3: "second"}.get(page)

    monkeypatch.setattr(collector, "page_exists", page_marker)

    pages, manifest = collector.store_pages("lidl")

    assert len(pages) == 2
    assert [page["source_page"] for page in manifest["pages"]] == [1, 2]


def test_mletaky_selects_latest_finite_validity_source(monkeypatch):
    html = " ".join(
        [
            "https://app.mletaky.sk/260810_260804_lidl_older",
            "https://app.mletaky.sk/260817_260811_lidl_latest",
        ]
    )
    monkeypatch.setattr(
        collector.requests,
        "get",
        lambda *args, **kwargs: types.SimpleNamespace(text=html),
    )
    try:
        manifest = collector.mletaky_base("lidl")
    except Exception as exc:
        pytest.fail(f"multiple finite candidates could not be compared: {exc}")

    assert manifest == {
        "source_url": "https://app.mletaky.sk/260817_260811_lidl_latest",
        "valid_from": "2026-08-11",
        "valid_to": "2026-08-17",
    }


def test_mletaky_discovery_stops_after_two_terminal_misses_without_pages(monkeypatch):
    monkeypatch.setattr(collector, "kupino_meta", lambda store: None)
    monkeypatch.setattr(
        collector,
        "mletaky_base",
        lambda store: {
            "source_url": "https://app.mletaky.sk/260823_260817_lidl_current",
            "valid_from": "2026-08-17",
            "valid_to": "2026-08-23",
        },
    )
    calls = []

    def missing_page(url):
        calls.append(url)
        if len(calls) > 2:
            raise AssertionError("discovery continued past the terminal-miss rule")
        return None

    monkeypatch.setattr(collector, "page_exists", missing_page)

    try:
        pages, manifest = collector.store_pages("lidl")
    except AssertionError as exc:
        pytest.fail(str(exc))

    assert pages == []
    assert manifest is None
    assert len(calls) == 2


def test_ninety_page_flyer_scans_every_thumbnail_and_keeps_late_food_provenance(monkeypatch):
    manifest, thumbnail_reads, scan_batches, read_batches = install_pipeline_fakes(
        monkeypatch,
        page_count=90,
        food_pages={85},
    )

    offers = collector.zbieraj(object(), "lidl")

    assert thumbnail_reads == [page[0] for page in flyer_fixture(90)[0]]
    assert [page for batch in scan_batches for page in batch] == list(range(1, 91))
    assert max(map(len, scan_batches)) <= getattr(collector, "SCAN_BATCH_SIZE", 12)
    assert read_batches == [[85]]
    assert offers[0]["source_page"] == 85
    assert offers[0]["source_url"] == manifest["source_url"]
    assert offers[0]["valid_from"] == "2026-08-17"
    assert offers[0]["valid_to"] == "2026-08-23"


def test_every_food_page_is_read_in_bounded_batches(monkeypatch):
    read_batch_size = getattr(collector, "READ_BATCH_SIZE", 4)
    food_pages = set(range(1, read_batch_size + 4))
    _, _, _, read_batches = install_pipeline_fakes(
        monkeypatch,
        page_count=len(food_pages),
        food_pages=food_pages,
    )

    offers = collector.zbieraj(object(), "lidl")

    assert [page for batch in read_batches for page in batch] == sorted(food_pages)
    assert max(map(len, read_batches)) <= read_batch_size
    assert [offer["source_page"] for offer in offers] == sorted(food_pages)


@pytest.mark.parametrize(
    "validity",
    [
        {"valid_from": None, "valid_to": "2026-08-23"},
        {"valid_from": "not-a-date", "valid_to": "2026-08-23"},
        {"valid_from": "2026-08-24", "valid_to": "2026-08-23"},
    ],
)
def test_collection_rejects_missing_or_unparseable_flyer_validity(monkeypatch, validity):
    pages, manifest = flyer_fixture(1)
    manifest.update(validity)
    monkeypatch.setattr(collector, "store_pages", lambda store: (pages, manifest))

    with pytest.raises(ValueError):
        collector.zbieraj(object(), "lidl")


def test_collection_rejects_extracted_source_page_outside_food_manifest(monkeypatch):
    install_pipeline_fakes(
        monkeypatch,
        page_count=2,
        food_pages={1},
        extracted_pages=[2],
    )

    with pytest.raises(ValueError):
        collector.zbieraj(object(), "lidl")


def test_mocked_collection_output_is_persistable_through_atomic_replacement(monkeypatch):
    install_pipeline_fakes(monkeypatch, page_count=1, food_pages={1})
    offers = collector.zbieraj(object(), "lidl")
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.executescript(collector.SCHEMA)

    try:
        replace_store_week(con, "2026-08-17", "Lidl", offers)
    except ValueError as exc:
        pytest.fail(f"collector returned an offer rejected by the shared writer: {exc}")

    row = con.execute(
        "SELECT obchod, nazov, source_url, source_page, valid_from, valid_to FROM akcie"
    ).fetchone()
    assert tuple(row) == (
        "Lidl",
        "Potravina 1",
        "https://flyers.example/lidl-current",
        1,
        "2026-08-17",
        "2026-08-23",
    )


def test_failed_store_run_exits_nonzero_without_replacing_prior_rows(monkeypatch, tmp_path, capsys):
    database = tmp_path / "uvarsi.db"
    monkeypatch.setattr(collector, "DB", str(database))
    con = collector.db()
    prior = [
        {
            "obchod": "Lidl",
            "nazov": f"Predchádzajúca položka {index}",
            "kategoria": "trvanlive",
            "cena": 1.0,
            "povodna": None,
            "zlava": None,
            "jednotka": "ks",
            "source_url": "https://flyers.example/previous",
            "source_page": index,
            "valid_from": "2026-08-17",
            "valid_to": "2026-08-23",
        }
        for index in range(1, 21)
    ]
    replace_store_week(con, "2026-08-17", "Lidl", prior)
    con.close()

    monkeypatch.setattr(collector, "monday", lambda: "2026-08-17")
    monkeypatch.setattr(collector, "STORES", ["lidl"])
    monkeypatch.setattr(collector, "load_key", lambda: "unused-test-value")
    monkeypatch.setattr(
        collector,
        "zbieraj",
        lambda client, store: [{"obchod": "Lidl", "nazov": "Neoverená", "cena": 1.0}],
    )
    monkeypatch.setitem(
        sys.modules,
        "anthropic",
        types.SimpleNamespace(Anthropic=lambda **kwargs: object()),
    )

    with pytest.raises(SystemExit, match="lidl"):
        collector.main()

    con = sqlite3.connect(database)
    names = con.execute(
        "SELECT nazov FROM akcie WHERE tyzden=? AND obchod=? ORDER BY id",
        ("2026-08-17", "Lidl"),
    ).fetchall()
    assert len(names) == 20
    assert names[0] == ("Predchádzajúca položka 1",)
    assert "[OK]" not in capsys.readouterr().out
