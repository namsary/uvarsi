import re
import sqlite3
import sys
import types
from datetime import date, datetime

import pytest

from app import zbierac_akcii as collector
from app import plan_jobs
from app.offer_data import replace_store_week
from app.plan_jobs import JobRequest


TODAY = date(2026, 8, 20)
NOW = datetime(2026, 8, 20, 9, 0, 0)


def test_collector_model_gate_preserves_capacity_reserved_by_the_plan_queue(
        tmp_path, monkeypatch):
    database = tmp_path / "uvarsi.db"
    monkeypatch.setenv("UVARSI_DENNY_STROP_EUR", "0.20")
    monkeypatch.setattr(collector, "DB", str(database))
    con = collector.db()
    try:
        plan_jobs.migrate_plan_jobs_schema(con)
        plan_jobs.enqueue(
            con,
            JobRequest(
                job_key="pre:reserved:0",
                signature="reserved",
                variant=0,
                kind="precompute",
                user_id=None,
                week="2026-08-17",
                priority=20,
                payload={},
                reserved_eur=0.12,
            ),
            now=NOW,
        )

        class Model:
            def __init__(self):
                self.messages = self
                self.calls = 0

            def create(self, **_kwargs):
                self.calls += 1
                return types.SimpleNamespace(usage=None)

        model = Model()
        guarded = collector.guarded_client(con, model)
        with pytest.raises(collector.naklady.RozpocetVycerpany) as refusal:
            guarded.messages.create(model="claude-opus-5", max_tokens=1, messages=[])

        assert refusal.value.kod == "rozpocet_denny"
        assert model.calls == 0
        assert con.execute("SELECT COUNT(*) FROM naklady").fetchone()[0] == 0
    finally:
        con.close()


def kupino_flyer(slug="/letak/lidl-letak-2026-08-17-2026-08-23", flyer_id="42", image_name="lidl-letak"):
    """Dict shape that the real kupino_meta returns."""
    return {
        "flyer_id": flyer_id,
        "image_name": image_name,
        "source_url": f"https://www.kupino.sk{slug}",
        "valid_from": "2026-08-17",
        "valid_to": "2026-08-23",
    }


def fake_kupino_site(monkeypatch, index_html, page_html):
    """Serve the store index and the selected flyer's own page separately."""
    requested = []

    def get(url, **kwargs):
        requested.append(url)
        body = page_html if "/strana-2" in url else index_html
        return types.SimpleNamespace(text=body)

    monkeypatch.setattr(collector.requests, "get", get)
    return requested


FLYER_PAGE = (
    '<img src="https://img.kupino.sk/letaky/42/thumbs/lidl-letak-1_320.jpg">'
)


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


def _text_response(text):
    return types.SimpleNamespace(
        stop_reason="end_turn",
        content=[types.SimpleNamespace(type="text", text=text)],
    )


def test_claude_json_uses_structured_array_output_for_flyer_scan():
    calls = []

    class Messages:
        def create(self, **kwargs):
            calls.append(kwargs)
            return _text_response("[1,2]")

    client = types.SimpleNamespace(messages=Messages())

    assert collector.claude_json(client, collector.MODEL_SCAN, [], 500) == [1, 2]
    output = calls[0]["output_config"]
    assert output["format"]["type"] == "json_schema"
    assert output["format"]["schema"] == {
        "type": "array",
        "items": {"type": "integer"},
    }


def test_claude_json_recovers_a_valid_array_from_legacy_markdown_wrapper():
    class Messages:
        def create(self, **_kwargs):
            return _text_response("Výsledok:\n```json\n[1, 2]\n```\nHotovo.")

    client = types.SimpleNamespace(messages=Messages())

    assert collector.claude_json(client, collector.MODEL_SCAN, [], 500) == [1, 2]


def test_discovers_ninety_sequential_pages_until_terminal_miss(monkeypatch):
    monkeypatch.setattr(collector, "kupino_meta", lambda store: kupino_flyer())

    def page_marker(url):
        page = int(re.search(r"-(\d+)_320\.jpg$", url).group(1))
        return f"page-{page}" if page <= 90 else None

    monkeypatch.setattr(collector, "page_exists", page_marker)

    pages, manifest = collector.store_pages("lidl", today=TODAY)

    assert len(pages) == 90
    assert manifest["source_url"] == "https://www.kupino.sk/letak/lidl-letak-2026-08-17-2026-08-23"
    assert manifest["valid_from"] == "2026-08-17"
    assert manifest["valid_to"] == "2026-08-23"
    assert manifest["pages"][-1]["source_page"] == 90


def test_page_discovery_stops_when_provider_repeats_a_page(monkeypatch):
    monkeypatch.setattr(collector, "kupino_meta", lambda store: kupino_flyer())

    def page_marker(url):
        page = int(re.search(r"-(\d+)_320\.jpg$", url).group(1))
        return {1: "first", 2: "second", 3: "second"}.get(page)

    monkeypatch.setattr(collector, "page_exists", page_marker)

    pages, manifest = collector.store_pages("lidl", today=TODAY)

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
        manifest = collector.mletaky_base("lidl", today=date(2026, 8, 14))
    except Exception as exc:
        pytest.fail(f"multiple finite candidates could not be compared: {exc}")

    assert manifest == {
        "source_url": "https://app.mletaky.sk/260817_260811_lidl_latest",
        "valid_from": "2026-08-11",
        "valid_to": "2026-08-17",
    }


def test_mletaky_prefers_main_weekly_flyer_over_newer_weekend_flyer(monkeypatch):
    """Lidl's 99-page weekly flyer must beat an 8-page local/weekend insert."""
    html = " ".join(
        [
            "https://app.mletaky.sk/260830_260824_lidl_mainweekly",
            "https://app.mletaky.sk/260830_260827_lidl_weekend",
        ]
    )
    monkeypatch.setattr(
        collector.requests,
        "get",
        lambda *args, **kwargs: types.SimpleNamespace(text=html),
    )

    manifest = collector.mletaky_base("lidl", today=date(2026, 8, 28))

    assert manifest == {
        "source_url": "https://app.mletaky.sk/260830_260824_lidl_mainweekly",
        "valid_from": "2026-08-24",
        "valid_to": "2026-08-30",
    }


def test_mletaky_keeps_extended_holiday_main_flyer_over_four_day_insert(monkeypatch):
    html = " ".join(
        [
            "https://app.mletaky.sk/261227_261217_lidl_holidaymain",
            "https://app.mletaky.sk/261227_261224_lidl_weekend",
        ]
    )
    monkeypatch.setattr(
        collector.requests,
        "get",
        lambda *args, **kwargs: types.SimpleNamespace(text=html),
    )

    manifest = collector.mletaky_base("lidl", today=date(2026, 12, 24))

    assert manifest["source_url"].endswith("_holidaymain")


def test_mletaky_prefers_largest_same_week_flyer_and_keeps_declared_page_count(monkeypatch):
    html = """\
    ["card","https://app.mletaky.sk/260830_260824_lidl_main/image00.webp",
      {"className":"card-description lg:text-sm lg:font-normal","children":99}]
    ["$","$L59","next-card"]
    ["card","https://app.mletaky.sk/260830_260824_lidl_selected/image00.webp",
      {"className":"card-description lg:text-sm lg:font-normal","children":4}]
    ["$","$L59","end"]
    """
    monkeypatch.setattr(
        collector.requests,
        "get",
        lambda *args, **kwargs: types.SimpleNamespace(text=html),
    )

    manifest = collector.mletaky_base("lidl", today=date(2026, 8, 28))

    assert manifest == {
        "source_url": "https://app.mletaky.sk/260830_260824_lidl_main",
        "valid_from": "2026-08-24",
        "valid_to": "2026-08-30",
        "declared_pages": 99,
    }


def test_mletaky_discovery_stops_after_two_terminal_misses_without_pages(monkeypatch):
    monkeypatch.setattr(collector, "kupino_meta", lambda store: None)
    monkeypatch.setattr(
        collector,
        "mletaky_base",
        lambda store, today=None: {
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
        pages, manifest = collector.store_pages("lidl", today=TODAY)
    except AssertionError as exc:
        pytest.fail(str(exc))

    assert pages == []
    assert manifest is None
    assert len(calls) == 2


def test_mletaky_rejects_a_truncated_flyer_against_its_declared_page_count(monkeypatch):
    monkeypatch.setattr(collector, "kupino_meta", lambda store: None)
    monkeypatch.setattr(
        collector,
        "mletaky_base",
        lambda store, today=None: {
            "source_url": "https://app.mletaky.sk/260830_260824_lidl_main",
            "valid_from": "2026-08-24",
            "valid_to": "2026-08-30",
            "declared_pages": 99,
        },
    )

    def only_four_pages(url):
        page = int(re.search(r"image(\d+)\.webp$", url).group(1))
        return f"page-{page}" if page < 4 else None

    monkeypatch.setattr(collector, "page_exists", only_four_pages)

    pages, manifest = collector.store_pages("lidl", today=date(2026, 8, 28))

    assert pages == []
    assert manifest is None


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


# --------------------------------------------------------- validity provenance
def test_kupino_never_takes_validity_from_another_flyer_in_the_index(monkeypatch):
    """The store index lists competing leaflets; their dates are not ours."""
    index = (
        '<a href="/letak/lidl-brozura-2026-01-05-2026-01-11">brožúra</a>'
        '<a href="/letak/lidl-letak-tyzden">aktuálny leták</a>'
    )
    fake_kupino_site(monkeypatch, index, FLYER_PAGE)

    with pytest.raises(ValueError):
        collector.kupino_meta("lidl")


def test_kupino_reads_validity_from_the_selected_flyer_own_slug(monkeypatch):
    index = (
        '<a href="/letak/lidl-brozura-2026-01-05-2026-01-11">brožúra</a>'
        '<a href="/letak/lidl-letak-2026-08-17-2026-08-23">aktuálny leták</a>'
    )
    page = FLYER_PAGE + '<aside><a href="/letak/tesco-letak-2026-02-02-2026-02-08">iný</a></aside>'
    fake_kupino_site(monkeypatch, index, page)

    meta = collector.kupino_meta("lidl")

    assert meta["valid_from"] == "2026-08-17"
    assert meta["valid_to"] == "2026-08-23"
    assert meta["source_url"] == "https://www.kupino.sk/letak/lidl-letak-2026-08-17-2026-08-23"


def test_kupino_accepts_one_unambiguous_labelled_validity_on_the_flyer_page(monkeypatch):
    index = '<a href="/letak/lidl-letak-tyzden">aktuálny leták</a>'
    page = FLYER_PAGE + '"validFrom":"2026-08-17","validThrough":"2026-08-23"'
    fake_kupino_site(monkeypatch, index, page)

    meta = collector.kupino_meta("lidl")

    assert (meta["valid_from"], meta["valid_to"]) == ("2026-08-17", "2026-08-23")


def test_kupino_refuses_when_the_flyer_page_lists_competing_validities(monkeypatch):
    index = '<a href="/letak/lidl-letak-tyzden">aktuálny leták</a>'
    page = (
        FLYER_PAGE
        + '"validFrom":"2026-08-17","validThrough":"2026-08-23"'
        + '"validFrom":"2026-08-24","validThrough":"2026-08-30"'
    )
    fake_kupino_site(monkeypatch, index, page)

    with pytest.raises(ValueError):
        collector.kupino_meta("lidl")


def test_kupino_validity_is_never_scraped_from_the_store_index_html(monkeypatch):
    """Even a fully dated index must not supply the selected flyer's validity."""
    index = (
        '<a href="/letak/lidl-brozura-2026-01-05-2026-01-11">brožúra</a>'
        '<a href="/letak/lidl-letak-tyzden">aktuálny leták</a>'
    )
    fake_kupino_site(monkeypatch, index, FLYER_PAGE)

    try:
        meta = collector.kupino_meta("lidl")
    except ValueError:
        return
    assert meta["valid_from"] != "2026-01-05"
    assert meta["valid_to"] != "2026-01-11"


def test_store_pages_skips_a_kupino_flyer_that_is_not_valid_today(monkeypatch, capsys):
    expired = kupino_flyer()
    expired.update(valid_from="2026-08-03", valid_to="2026-08-09")
    monkeypatch.setattr(collector, "kupino_meta", lambda store: expired)
    monkeypatch.setattr(collector, "mletaky_base", lambda store, today=None: None)
    monkeypatch.setattr(collector, "page_exists", lambda url: "marker")

    pages, manifest = collector.store_pages("lidl", today=TODAY)

    assert pages == []
    assert manifest is None
    assert "2026-08-09" in capsys.readouterr().out


# ------------------------------------------------------------ mletaky currency
def test_mletaky_never_selects_a_flyer_that_has_already_ended(monkeypatch):
    """The latest-started flyer may already be over; it must not be chosen."""
    html = " ".join(
        [
            "https://app.mletaky.sk/260819_260813_lidl_ended",
            "https://app.mletaky.sk/260826_260812_lidl_running",
        ]
    )
    monkeypatch.setattr(
        collector.requests,
        "get",
        lambda *args, **kwargs: types.SimpleNamespace(text=html),
    )

    manifest = collector.mletaky_base("lidl", today=TODAY)

    assert manifest == {
        "source_url": "https://app.mletaky.sk/260826_260812_lidl_running",
        "valid_from": "2026-08-12",
        "valid_to": "2026-08-26",
    }


def test_mletaky_returns_nothing_when_every_candidate_has_expired(monkeypatch):
    html = "https://app.mletaky.sk/260819_260813_lidl_ended"
    monkeypatch.setattr(
        collector.requests,
        "get",
        lambda *args, **kwargs: types.SimpleNamespace(text=html),
    )

    assert collector.mletaky_base("lidl", today=TODAY) is None


# -------------------------------------------------------------- page discovery
def test_page_discovery_bridges_a_single_missing_page_on_the_cdn(monkeypatch):
    monkeypatch.setattr(collector, "kupino_meta", lambda store: kupino_flyer())

    def page_marker(url):
        page = int(re.search(r"-(\d+)_320\.jpg$", url).group(1))
        if page == 5 or page > 12:
            return None
        return f"page-{page}"

    monkeypatch.setattr(collector, "page_exists", page_marker)

    pages, manifest = collector.store_pages("lidl", today=TODAY)

    assert [page["source_page"] for page in manifest["pages"]] == [1, 2, 3, 4, 6, 7, 8, 9, 10, 11, 12]
    assert len(pages) == 11


def test_page_discovery_warns_when_the_page_count_is_implausibly_low(monkeypatch, capsys):
    monkeypatch.setattr(collector, "kupino_meta", lambda store: kupino_flyer())

    def page_marker(url):
        page = int(re.search(r"-(\d+)_320\.jpg$", url).group(1))
        return f"page-{page}" if page <= 3 else None

    monkeypatch.setattr(collector, "page_exists", page_marker)

    pages, _ = collector.store_pages("lidl", today=TODAY)

    assert len(pages) == 3
    output = capsys.readouterr().out
    assert "[WARN]" in output
    assert "3" in output


# --------------------------------------------------- per-store run bookkeeping
def valid_offer(store, index):
    return {
        "obchod": store.capitalize(),
        "nazov": f"Položka {index}",
        "kategoria": "trvanlive",
        "cena": 1.0 + index / 100,
        "povodna": 2.0,
        "zlava": "-50 %",
        "jednotka": "ks",
        "source_url": f"https://flyers.example/{store}",
        "source_page": index,
        "valid_from": "2026-08-17",
        "valid_to": "2026-08-23",
    }


def run_main_over_stores(monkeypatch, tmp_path, outcomes):
    database = tmp_path / "uvarsi.db"
    monkeypatch.setattr(collector, "DB", str(database))
    monkeypatch.setattr(collector, "monday", lambda: "2026-08-17")
    monkeypatch.setattr(collector, "STORES", list(outcomes))
    monkeypatch.setattr(collector, "load_key", lambda: "unused-test-value")

    def zbieraj(client, store):
        if not outcomes[store]:
            raise ValueError(f"{store}: leták sa nepodarilo prečítať")
        return [valid_offer(store, index) for index in range(1, 21)]

    monkeypatch.setattr(collector, "zbieraj", zbieraj)
    monkeypatch.setitem(
        sys.modules,
        "anthropic",
        types.SimpleNamespace(Anthropic=lambda **kwargs: object()),
    )
    return database


def test_partial_run_records_which_stores_succeeded_for_the_week(monkeypatch, tmp_path):
    database = run_main_over_stores(
        monkeypatch, tmp_path, {"kaufland": True, "tesco": True, "lidl": False}
    )

    with pytest.raises(SystemExit, match="lidl"):
        collector.main()

    con = sqlite3.connect(database)
    con.row_factory = sqlite3.Row
    outcomes = {
        row["obchod"]: row["stav"]
        for row in con.execute("SELECT obchod, stav FROM zber_stav WHERE tyzden=?", ("2026-08-17",))
    }
    con.close()

    assert outcomes == {"Kaufland": "ok", "Tesco": "ok", "Lidl": "fail"}


def test_successful_run_marks_every_store_as_collected(monkeypatch, tmp_path):
    database = run_main_over_stores(
        monkeypatch, tmp_path, {"kaufland": True, "tesco": True, "lidl": True}
    )

    collector.main()

    con = sqlite3.connect(database)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT obchod, stav, pocet FROM zber_stav WHERE tyzden=? ORDER BY obchod", ("2026-08-17",)
    ).fetchall()
    con.close()

    assert [(row["obchod"], row["stav"], row["pocet"]) for row in rows] == [
        ("Kaufland", "ok", 20),
        ("Lidl", "ok", 20),
        ("Tesco", "ok", 20),
    ]


def test_implausibly_small_store_result_is_failed_and_not_published(monkeypatch, tmp_path):
    """Jedna náhodne prečítaná akcia nesmie prepísať zdravý obsah obchodu."""
    database = run_main_over_stores(monkeypatch, tmp_path, {"lidl": True})
    monkeypatch.setattr(
        collector,
        "zbieraj",
        lambda client, store: [valid_offer(store, 1)],
    )

    with pytest.raises(SystemExit, match="lidl"):
        collector.main()

    con = sqlite3.connect(database)
    outcome = con.execute(
        "SELECT stav, pocet FROM zber_stav WHERE tyzden=? AND obchod='Lidl'",
        ("2026-08-17",),
    ).fetchone()
    offers = con.execute("SELECT COUNT(*) FROM akcie").fetchone()[0]
    con.close()

    assert outcome == ("fail", 0)
    assert offers == 0


def test_targeted_recovery_collects_only_the_requested_store(monkeypatch, tmp_path):
    database = run_main_over_stores(
        monkeypatch, tmp_path, {"kaufland": True, "tesco": True, "lidl": True}
    )

    collector.main(["lidl"])

    con = sqlite3.connect(database)
    rows = con.execute(
        "SELECT obchod, stav, pocet FROM zber_stav ORDER BY obchod"
    ).fetchall()
    con.close()
    assert rows == [("Lidl", "ok", 20)]


def test_cli_passes_repeated_store_arguments_to_targeted_collection(monkeypatch):
    selected = []
    monkeypatch.setattr(collector, "main", lambda stores=None: selected.extend(stores or []))

    assert collector.cli(["--store", "lidl", "--store", "tesco"]) == 0
    assert selected == ["lidl", "tesco"]


def test_a_stores_stale_success_is_replaced_by_a_later_failure(monkeypatch, tmp_path):
    database = run_main_over_stores(monkeypatch, tmp_path, {"lidl": True})
    collector.main()

    monkeypatch.setattr(
        collector, "zbieraj", lambda client, store: (_ for _ in ()).throw(ValueError("prázdny leták"))
    )
    with pytest.raises(SystemExit, match="lidl"):
        collector.main()

    con = sqlite3.connect(database)
    con.row_factory = sqlite3.Row
    row = con.execute("SELECT stav, pocet FROM zber_stav WHERE tyzden=?", ("2026-08-17",)).fetchone()
    con.close()

    assert (row["stav"], row["pocet"]) == ("fail", 0)
