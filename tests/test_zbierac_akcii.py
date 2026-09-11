import json
import re
import sqlite3
import sys
import types
from datetime import date, datetime, timezone

import pytest

from app import zbierac_akcii as collector
from app import plan_jobs
from app.offer_data import replace_store_week
from app.plan_jobs import JobRequest


TODAY = date(2026, 8, 20)
NOW = datetime(2026, 8, 20, 9, 0, 0)


def test_collection_week_uses_bratislava_monday_during_utc_sunday_rollover():
    instant = datetime(2026, 9, 6, 22, 30, tzinfo=timezone.utc)

    assert collector.monday(instant) == "2026-09-07"


def test_store_with_fourteen_offers_is_never_considered_complete():
    assert collector.MIN_VERIFIED_OFFERS_PER_STORE > 14


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
        "collector_kind": "kupino-aggregator",
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


def _json_response(payload):
    return types.SimpleNamespace(json=lambda: payload, text=json.dumps(payload))


def _official_lidl_payload(page_count=105, valid_from="2026-08-17", valid_to="2026-08-23"):
    return {
        "success": True,
        "flyer": {
            "id": "01a-test-current-flyer",
            "name": "Aktuálny leták",
            "apiCountryCode": "SK",
            "isActive": True,
            "status": "current",
            "offerStartDate": valid_from,
            "offerEndDate": valid_to,
            "flyerUrlAbsolute": (
                "https://www.lidl.sk/l/sk/letak/"
                "online-letak-platny-od-17-08-2026/ar/0"
            ),
            "pages": [
                {
                    "number": page,
                    "thumbnail": f"https://imgproxy.leaflets.schwarz/thumb-{page}.jpg",
                    "image": f"https://imgproxy.leaflets.schwarz/image-{page}.jpg",
                    "zoom": f"https://imgproxy.leaflets.schwarz/zoom-{page}.jpg",
                }
                for page in range(1, page_count + 1)
            ],
        },
    }


def test_official_lidl_reads_the_complete_current_weekly_flyer(monkeypatch):
    overview = (
        '<a href="https://www.lidl.sk/l/sk/letak/'
        'online-letak-platny-od-17-08-2026/ar/1">Pozri si leták</a>'
    )
    requested = []

    def get(url, **_kwargs):
        requested.append(url)
        if url == collector.LIDL_OVERVIEW_URL:
            return types.SimpleNamespace(text=overview)
        return _json_response(_official_lidl_payload())

    monkeypatch.setattr(collector.requests, "get", get)

    pages, manifest = collector.official_lidl_pages(today=TODAY)

    assert len(pages) == 105
    assert pages[0] == (
        "https://imgproxy.leaflets.schwarz/thumb-1.jpg",
        "https://imgproxy.leaflets.schwarz/zoom-1.jpg",
    )
    assert pages[-1][1].endswith("zoom-105.jpg")
    assert manifest["valid_from"] == "2026-08-17"
    assert manifest["valid_to"] == "2026-08-23"
    assert manifest["declared_pages"] == 105
    assert manifest["pages"][-1]["source_page"] == 105
    assert requested[-1].endswith("flyer_identifier=online-letak-platny-od-17-08-2026")


def test_store_pages_prefers_official_lidl_over_third_party_sources(monkeypatch):
    expected = flyer_fixture(105)
    monkeypatch.setattr(collector, "official_lidl_pages", lambda today=None: expected)
    monkeypatch.setattr(
        collector,
        "kupino_meta",
        lambda store: pytest.fail("official Lidl must be tried before Kupino"),
    )

    assert collector.store_pages("lidl", today=TODAY) == expected


def test_expired_official_lidl_falls_back_instead_of_publishing_old_prices(monkeypatch):
    overview = (
        '<a href="https://www.lidl.sk/l/sk/letak/'
        'online-letak-platny-od-10-08-2026/ar/1">Pozri si leták</a>'
    )

    def get(url, **_kwargs):
        if url == collector.LIDL_OVERVIEW_URL:
            return types.SimpleNamespace(text=overview)
        return _json_response(
            _official_lidl_payload(valid_from="2026-08-10", valid_to="2026-08-16")
        )

    monkeypatch.setattr(collector.requests, "get", get)
    monkeypatch.setattr(collector, "kupino_meta", lambda store: kupino_flyer())
    monkeypatch.setattr(
        collector,
        "page_exists",
        lambda url: "current-page" if "-1_320.jpg" in url else None,
    )

    pages, manifest = collector.store_pages("lidl", today=TODAY)

    assert pages
    assert manifest["source_url"].startswith("https://www.kupino.sk/")


def flyer_fixture(page_count):
    pages = [
        (f"https://images.example/thumb-{page}.jpg", f"https://images.example/full-{page}.jpg")
        for page in range(1, page_count + 1)
    ]
    manifest = {
        "source_url": "https://www.kupino.sk/letak/lidl-test-current",
        "collector_kind": "kupino-aggregator",
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


def test_manifest_rejects_a_collector_registered_only_for_another_store():
    pages, manifest = flyer_fixture(1)
    manifest.update(
        source_url=(
            "https://www.lidl.sk/l/sk/letak/"
            "online-letak-platny-od-17-08-2026/view/flyer/page/1"
        ),
        collector_kind="official-lidl-viewer",
    )

    with pytest.raises(ValueError, match="dôveryhodný typ zdroja"):
        collector.validate_flyer_manifest(pages, manifest, store="kaufland")


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


def test_flyer_extraction_contract_keeps_public_and_loyalty_prices_separate():
    item = collector.EXTRACT_OUTPUT_SCHEMA["items"]
    assert item["required"] == list(item["properties"])
    assert {
        "cena_s_kartou",
        "zlava_s_kartou",
        "vernostny_program",
        "minimalny_nakup",
        "podmienka_s_kartou",
    } <= set(item["properties"])
    condition_schema = item["properties"]["podmienka_s_kartou"]["anyOf"][0]
    assert "maxLength" not in condition_schema, (
        "raw Anthropic structured-output schemas reject maxLength with HTTP 400"
    )
    assert "160" in condition_schema["description"]


def test_collection_keeps_unconditional_price_primary_and_card_price_conditional(monkeypatch):
    pages, manifest = flyer_fixture(1)
    monkeypatch.setattr(collector, "store_pages", lambda store: (pages, manifest))
    monkeypatch.setattr(collector, "get_b64", lambda url, max_px: url)

    def fake_claude_json(client, model, content, max_tokens, effort=None):
        if model == collector.MODEL_SCAN:
            return [1]
        return [{
            "source_page": 1,
            "nazov": "Repkový olej Raciol",
            "kategoria": "trvanlive",
            "cena": 1.69,
            "povodna": 2.99,
            "zlava": "-43 %",
            "jednotka": "l",
            "cena_s_kartou": 1.55,
            "zlava_s_kartou": "-48 %",
            "vernostny_program": "Kaufland Card",
            "minimalny_nakup": 20.0,
            "podmienka_s_kartou": "aktivuj kupón v aplikácii",
        }]

    monkeypatch.setattr(collector, "claude_json", fake_claude_json)

    offers = collector.zbieraj(object(), "kaufland")

    assert offers == [{
        "obchod": "Kaufland",
        "nazov": "Repkový olej Raciol",
        "kategoria": "trvanlive",
        "cena": 1.69,
        "povodna": 2.99,
        "zlava": "-43 %",
        "jednotka": "l",
        "cena_s_kartou": 1.55,
        "zlava_s_kartou": "-48 %",
        "vernostny_program": "Kaufland Card",
        "minimalny_nakup": 20.0,
        "podmienka_s_kartou": "aktivuj kupón v aplikácii",
        "source_url": manifest["source_url"],
        "source_page": 1,
        "valid_from": manifest["valid_from"],
        "valid_to": manifest["valid_to"],
    }]


def test_collection_derives_loyalty_program_from_the_known_store(monkeypatch):
    """A model label mix-up must not reject a whole otherwise valid store batch."""
    pages, manifest = flyer_fixture(1)
    monkeypatch.setattr(collector, "store_pages", lambda store: (pages, manifest))
    monkeypatch.setattr(collector, "get_b64", lambda url, max_px: url)
    models = []

    def fake_claude_json(client, model, content, max_tokens, effort=None):
        models.append(model)
        if model == collector.MODEL_SCAN:
            return [1]
        return [{
            "source_page": 1,
            "nazov": "Repkový olej Raciol",
            "kategoria": "trvanlive",
            "cena": 1.69,
            "povodna": 2.99,
            "zlava": "-43 %",
            "jednotka": "l",
            "cena_s_kartou": 1.55,
            "zlava_s_kartou": "-48 %",
            # Produkčný incident: model zamenil názov programu iného obchodu.
            "vernostny_program": "Clubcard",
            "minimalny_nakup": 20.0,
            "podmienka_s_kartou": None,
        }]

    monkeypatch.setattr(collector, "claude_json", fake_claude_json)

    offers = collector.zbieraj(object(), "kaufland")

    assert offers[0]["vernostny_program"] == "Kaufland Card"
    assert models == [collector.MODEL_SCAN, collector.MODEL_READ]


def test_flyer_pages_use_sonnet_first_and_opus_only_for_suspicious_prices(monkeypatch):
    pages, manifest = flyer_fixture(1)
    monkeypatch.setattr(collector, "store_pages", lambda store: (pages, manifest))
    monkeypatch.setattr(collector, "get_b64", lambda url, max_px: url)
    models = []

    def item(*, price, discount, card_price=None, card_discount=None):
        return {
            "source_page": 1,
            "nazov": "Repkový olej Raciol",
            "kategoria": "trvanlive",
            "cena": price,
            "povodna": 2.99,
            "zlava": discount,
            "jednotka": "l",
            "cena_s_kartou": card_price,
            "zlava_s_kartou": card_discount,
            "vernostny_program": "Kaufland Card" if card_price else None,
            "minimalny_nakup": 20.0 if card_price else None,
            "podmienka_s_kartou": None,
        }

    def fake_claude_json(client, model, content, max_tokens, effort=None):
        models.append(model)
        if model == collector.MODEL_SCAN:
            return [1]
        if model == collector.MODEL_READ:
            # Reprodukcia produkčného preklepu: 0,07 € nezodpovedá zľave 48 %
            # z 2,99 €. Takýto batch sa nesmie uložiť.
            return [item(price=0.07, discount="-48 %")]
        assert model == collector.MODEL_READ_FALLBACK
        return [item(
            price=1.69,
            discount="-43 %",
            card_price=1.55,
            card_discount="-48 %",
        )]

    monkeypatch.setattr(collector, "claude_json", fake_claude_json)

    offers = collector.zbieraj(object(), "kaufland")

    assert models == [
        collector.MODEL_SCAN,
        "claude-sonnet-5",
        "claude-opus-5",
    ]
    assert offers[0]["cena"] == 1.69
    assert offers[0]["cena_s_kartou"] == 1.55


def test_clean_sonnet_flyer_batch_does_not_call_opus(monkeypatch):
    pages, manifest = flyer_fixture(1)
    monkeypatch.setattr(collector, "store_pages", lambda store: (pages, manifest))
    monkeypatch.setattr(collector, "get_b64", lambda url, max_px: url)
    models = []

    def fake_claude_json(client, model, content, max_tokens, effort=None):
        models.append(model)
        if model == collector.MODEL_SCAN:
            return [1]
        return [{
            "source_page": 1,
            "nazov": "Ryža",
            "kategoria": "trvanlive",
            "cena": 1.49,
            "povodna": 1.99,
            "zlava": "-25 %",
            "jednotka": "kg",
            "cena_s_kartou": None,
            "zlava_s_kartou": None,
            "vernostny_program": None,
            "minimalny_nakup": None,
            "podmienka_s_kartou": None,
        }]

    monkeypatch.setattr(collector, "claude_json", fake_claude_json)

    offers = collector.zbieraj(object(), "tesco")

    assert models == [collector.MODEL_SCAN, "claude-sonnet-5"]
    assert offers[0]["cena"] == 1.49


def test_sonnet_batch_missing_a_selected_food_page_is_reread_by_opus(monkeypatch):
    pages, manifest = flyer_fixture(2)
    monkeypatch.setattr(collector, "store_pages", lambda store: (pages, manifest))
    monkeypatch.setattr(collector, "get_b64", lambda url, max_px: url)
    models = []

    def extracted(page):
        return {
            "source_page": page,
            "nazov": f"Potravina {page}",
            "kategoria": "trvanlive",
            "cena": 1.0 + page / 10,
            "povodna": None,
            "zlava": None,
            "jednotka": "ks",
            "cena_s_kartou": None,
            "zlava_s_kartou": None,
            "vernostny_program": None,
            "minimalny_nakup": None,
            "podmienka_s_kartou": None,
        }

    def fake_claude_json(client, model, content, max_tokens, effort=None):
        models.append(model)
        if model == collector.MODEL_SCAN:
            return [1, 2]
        if model == collector.MODEL_READ:
            return [extracted(1)]
        return [extracted(1), extracted(2)]

    monkeypatch.setattr(collector, "claude_json", fake_claude_json)

    offers = collector.zbieraj(object(), "lidl")

    assert models == [
        collector.MODEL_SCAN,
        "claude-sonnet-5",
        "claude-opus-5",
    ]
    assert {offer["source_page"] for offer in offers} == {1, 2}


def test_collection_rejects_instead_of_silently_truncating_a_loyalty_condition(monkeypatch):
    pages, manifest = flyer_fixture(1)
    monkeypatch.setattr(collector, "store_pages", lambda store: (pages, manifest))
    monkeypatch.setattr(collector, "get_b64", lambda url, max_px: url)

    def fake_claude_json(client, model, content, max_tokens, effort=None):
        if model == collector.MODEL_SCAN:
            return [1]
        return [{
            "source_page": 1, "nazov": "Repkový olej", "kategoria": "trvanlive",
            "cena": 1.69, "povodna": 2.99, "zlava": "-43 %", "jednotka": "l",
            "cena_s_kartou": 1.55, "zlava_s_kartou": "-48 %",
            "vernostny_program": "Kaufland Card", "minimalny_nakup": 20.0,
            "podmienka_s_kartou": "x" * 161,
        }]

    monkeypatch.setattr(collector, "claude_json", fake_claude_json)

    with pytest.raises(ValueError, match="bounded"):
        collector.zbieraj(object(), "kaufland")


def test_collection_skips_ambiguous_loyalty_item_without_losing_verified_prices(
    monkeypatch, capsys
):
    pages, manifest = flyer_fixture(1)
    monkeypatch.setattr(collector, "store_pages", lambda store: (pages, manifest))
    monkeypatch.setattr(collector, "get_b64", lambda url, max_px: url)

    def fake_claude_json(client, model, content, max_tokens, effort=None):
        if model == collector.MODEL_SCAN:
            return [1]
        return [
            {
                "source_page": 1, "nazov": "Neúplná Clubcard cena",
                "kategoria": "trvanlive", "cena": 1.29, "povodna": 1.99,
                "zlava": "-35 %", "jednotka": "ks", "cena_s_kartou": None,
                "zlava_s_kartou": "-35 %", "vernostny_program": "Clubcard",
                "minimalny_nakup": None, "podmienka_s_kartou": None,
            },
            {
                "source_page": 1, "nazov": "Ryža", "kategoria": "trvanlive",
                "cena": 1.49, "povodna": 1.99, "zlava": "-25 %",
                "jednotka": "kg", "cena_s_kartou": None,
                "zlava_s_kartou": None, "vernostny_program": None,
                "minimalny_nakup": None, "podmienka_s_kartou": None,
            },
        ]

    monkeypatch.setattr(collector, "claude_json", fake_claude_json)

    offers = collector.zbieraj(object(), "tesco")

    assert [offer["nazov"] for offer in offers] == ["Ryža"]
    assert "Neúplná Clubcard cena" in capsys.readouterr().out


def test_collection_budget_purpose_is_migration_until_every_selected_store_is_current():
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.executescript(collector.SCHEMA)
    week = "2026-08-31"
    con.executemany(
        "INSERT INTO zber_stav (tyzden, obchod, stav, pocet, data_version) VALUES (?,?,?,?,?)",
        [
            (week, "Kaufland", "ok", 40, collector.COLLECTION_DATA_VERSION),
            (week, "Tesco", "ok", 40, collector.COLLECTION_DATA_VERSION - 1),
        ],
    )

    assert collector.collection_budget_purpose(
        con, week, ["kaufland", "tesco"]
    ) == "zber_migracia"

    con.execute(
        "UPDATE zber_stav SET data_version=? WHERE obchod='Tesco'",
        (collector.COLLECTION_DATA_VERSION,),
    )
    assert collector.collection_budget_purpose(
        con, week, ["kaufland", "tesco"]
    ) == "zber_letakov"


def test_failed_current_schema_collection_stays_in_bounded_migration_recovery_budget():
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.executescript(collector.SCHEMA)
    week = "2026-08-31"
    con.execute(
        "INSERT INTO zber_stav (tyzden, obchod, stav, pocet, data_version) VALUES (?,?,?,?,?)",
        (week, "Lidl", "fail", 0, collector.COLLECTION_DATA_VERSION),
    )

    assert collector.collection_budget_purpose(
        con, week, ["lidl"]
    ) == "zber_migracia"


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
        "collector_kind": "mletaky-aggregator",
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
        "collector_kind": "mletaky-aggregator",
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
        "collector_kind": "mletaky-aggregator",
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
        "https://www.kupino.sk/letak/lidl-test-current",
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
        "collector_kind": "mletaky-aggregator",
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
        "source_url": f"https://www.kupino.sk/letak/{store}-test-current",
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


def test_recovery_waits_for_fresh_daily_budget_without_consuming_run(monkeypatch, tmp_path):
    """Drahý opravný beh sa nesmie rozbehnúť, keď sa dnes už nemôže dokončiť."""
    database = run_main_over_stores(
        monkeypatch, tmp_path, {"tesco": True, "lidl": True}
    )
    monkeypatch.setattr(
        collector, "collection_budget_purpose", lambda con, week, stores: "zber_migracia"
    )
    called = []
    monkeypatch.setattr(collector, "zbieraj", lambda client, store: called.append(store))

    con = collector.naklady.pripoj(database)
    try:
        collector.naklady.zapis(
            con,
            "plan",
            "claude-opus-5",
            types.SimpleNamespace(
                input_tokens=0,
                output_tokens=120_000,
                cache_creation_input_tokens=0,
                cache_read_input_tokens=0,
            ),
            notifikuj=lambda _sprava: None,
        )
    finally:
        con.close()

    with pytest.raises(SystemExit, match="odkladám"):
        collector.main(["tesco", "lidl"])

    assert called == []
    con = sqlite3.connect(database)
    try:
        runs = con.execute(
            "SELECT COUNT(*) FROM naklady_behy WHERE ucel='zber_migracia'"
        ).fetchone()[0]
    finally:
        con.close()
    assert runs == 0


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
