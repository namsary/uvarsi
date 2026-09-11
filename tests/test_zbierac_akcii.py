import json
import re
import sqlite3
import sys
import tempfile
import types
from datetime import date, datetime, timezone
from io import BytesIO

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
    return types.SimpleNamespace(
        status_code=200,
        json=lambda: payload,
        text=json.dumps(payload),
        raise_for_status=lambda: None,
    )


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


def _official_tesco_payload(*leaflets):
    return {
        "data": {
            "leaflets": {
                "totalItems": len(leaflets),
                "items": [
                    {"__typename": "Leaflet", **leaflet}
                    for leaflet in leaflets
                ],
            },
        },
    }


def _official_tesco_leaflet(
        *, leaflet_id=691, leaflet_type="HM", page_count=46,
        valid_from="2026-08-17T06:00:00.000Z",
        valid_to="2026-08-23T21:59:59.000Z"):
    suffix = "HM-CHM" if leaflet_type == "HM" else "SM"
    page_numbers = list(range(1, page_count + 1))
    page_numbers = page_numbers[::2] + page_numbers[1::2]
    return {
        "country": "sk",
        "countryId": 3,
        "id": leaflet_id,
        "leafletUrl": (
            "https://digitalcontent.api.tesco.com/v2/media/dotcom-hu/"
            f"pdf-id/20260812_2026_P23_SK_{suffix}.pdf"
        ),
        "pages": [
            {
                "__typename": "LeafletMetadataPage",
                "pagePNG": (
                    "https://digitalcontent.api.tesco.com/v2/media/dotcom-hu/"
                    f"page-{page}/20260812_2026_P23_SK_{suffix}.{page}.jpeg"
                ),
            }
            for page in page_numbers
        ],
        "promoP1Name": f"2026_P23_SK_{suffix}_Product-Data",
        "slug": "tesco-letak-2026-08-17",
        "type": leaflet_type,
        "validFrom": valid_from,
        "validTo": valid_to,
    }


def _bridge_tesco_leaflet(*, leaflet_format="HM", page_count=8):
    segment = "hypermarkety" if leaflet_format == "HM" else "supermarkety"
    return {
        "country": "sk",
        "format": leaflet_format,
        "slug": "tesco-letak-2026-08-17",
        "valid_from": "2026-08-17",
        "valid_to": "2026-08-23",
        "source_url": (
            "https://www.tesco.sk/akciove-ponuky/letaky-a-katalogy/"
            f"{segment}/tesco-letak-2026-08-17/1"
        ),
        "declared_pages": page_count,
        "pages": [
            {
                "source_page": page,
                "thumbnail_url": f"https://tesco-bridge.example/v1/tesco/media/token-{page}",
                "image_url": f"https://tesco-bridge.example/v1/tesco/media/token-{page}",
            }
            for page in range(1, page_count + 1)
        ],
    }


def _use_local_tesco(monkeypatch):
    monkeypatch.delenv("UVARSI_TESCO_BRIDGE_URL", raising=False)
    monkeypatch.delenv("UVARSI_TESCO_BRIDGE_SECRET", raising=False)
    monkeypatch.delenv("UVARSI_ENV", raising=False)


def test_official_tesco_reads_complete_current_hypermarket_flyer(monkeypatch):
    _use_local_tesco(monkeypatch)
    payload = _official_tesco_payload(
        _official_tesco_leaflet(leaflet_id=692, leaflet_type="SM", page_count=26),
        _official_tesco_leaflet(leaflet_id=691, leaflet_type="HM", page_count=46),
    )
    requested = []

    def post(url, **kwargs):
        requested.append((url, kwargs))
        return _json_response(payload)

    monkeypatch.setattr(
        collector.requests,
        "post",
        post,
    )
    monkeypatch.setattr(
        collector.requests,
        "get",
        lambda *_args, **_kwargs: pytest.fail("Tesco must use its direct API"),
    )

    pages, manifest = collector.official_tesco_pages(
        today=date(2026, 8, 20), leaflet_format="HM"
    )

    assert len(pages) == 46
    assert pages[0][1].endswith("HM-CHM.1.jpeg")
    assert pages[-1][1].endswith("HM-CHM.46.jpeg")
    assert manifest["collector_kind"] == "official-tesco-viewer"
    assert manifest["source_identity"] == "tesco-leaflet:691"
    assert manifest["valid_from"] == "2026-08-17"
    assert manifest["valid_to"] == "2026-08-23"
    assert manifest["declared_pages"] == 46
    assert manifest["leaflet_format"] == "HM"
    assert manifest["store_label"] == "Tesco hypermarket"
    assert manifest["pages"][-1]["source_page"] == 46
    assert manifest["source_url"] == (
        "https://www.tesco.sk/akciove-ponuky/letaky-a-katalogy/"
        "hypermarkety/tesco-letak-2026-08-17/1"
    )
    assert requested[0][0] == collector.TESCO_API_URL
    assert 'validTo: { after: "2026-08-20T00:00:00.000Z" }' in (
        requested[0][1]["json"]["query"]
    )
    assert "type: { eq: HM }" in requested[0][1]["json"]["query"]


def test_official_tesco_uses_authenticated_bridge_contract_without_leaking_secret(
        monkeypatch):
    secret = "bridge-secret-must-never-leak"
    monkeypatch.setenv("UVARSI_TESCO_BRIDGE_URL", "https://tesco-bridge.example/")
    monkeypatch.setenv("UVARSI_TESCO_BRIDGE_SECRET", secret)
    monkeypatch.setenv("UVARSI_ENV", "production")
    requested = []

    def post(url, **kwargs):
        requested.append((url, kwargs))
        return _json_response({"leaflet": _bridge_tesco_leaflet()})

    monkeypatch.setattr(collector.requests, "post", post)

    pages, manifest = collector.official_tesco_pages(today=TODAY)

    assert requested == [(
        "https://tesco-bridge.example/v1/tesco/leaflets",
        {
            "headers": {
                "Accept": "application/json",
                "Authorization": f"Bearer {secret}",
                "Content-Type": "application/json",
            },
            "json": {"date": "2026-08-20", "format": "HM"},
            "timeout": 30,
        },
    )]
    assert manifest["collector_kind"] == "official-tesco-viewer"
    assert manifest["leaflet_format"] == "HM"
    assert manifest["store_label"] == "Tesco hypermarket"
    assert all("/v1/tesco/media/" in image for _thumb, image in pages)
    assert secret not in repr((pages, manifest))


def test_tesco_bridge_failure_is_secret_safe_and_production_skips_aggregators(
        monkeypatch, capsys):
    secret = "do-not-print-this-secret"
    monkeypatch.setenv("UVARSI_TESCO_BRIDGE_URL", "https://tesco-bridge.example")
    monkeypatch.setenv("UVARSI_TESCO_BRIDGE_SECRET", secret)
    monkeypatch.setenv("UVARSI_ENV", "production")
    monkeypatch.setattr(
        collector.requests,
        "post",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError(f"upstream failed with Bearer {secret}")
        ),
    )
    monkeypatch.setattr(
        collector,
        "kupino_meta",
        lambda _store: pytest.fail("production Tesco must never use Kupino"),
    )
    monkeypatch.setattr(
        collector,
        "mletaky_base",
        lambda _store, _today: pytest.fail("production Tesco must never use mLetaky"),
    )

    assert collector.store_pages("tesco", today=TODAY) == ([], None)
    assert secret not in capsys.readouterr().out


def test_official_tesco_isolates_malformed_candidate_and_keeps_valid_requested_hm(
        monkeypatch):
    _use_local_tesco(monkeypatch)
    malformed = _official_tesco_leaflet(leaflet_id=690, leaflet_type="HM", page_count=8)
    malformed["pages"][-1]["pagePNG"] = "https://evil.example/offer.8.jpeg"
    valid = _official_tesco_leaflet(leaflet_id=691, leaflet_type="HM", page_count=12)
    payload = _official_tesco_payload(
        malformed,
        _official_tesco_leaflet(leaflet_id=692, leaflet_type="SM", page_count=20),
        valid,
    )
    monkeypatch.setattr(
        collector.requests, "post", lambda _url, **_kwargs: _json_response(payload)
    )

    pages, manifest = collector.official_tesco_pages(
        today=TODAY, leaflet_format="HM"
    )

    assert len(pages) == 12
    assert manifest["leaflet_format"] == "HM"
    assert manifest["store_label"] == "Tesco hypermarket"


@pytest.mark.parametrize("page_count", [7, 121])
def test_official_tesco_rejects_manifest_outside_eight_to_120_pages(
        monkeypatch, page_count):
    _use_local_tesco(monkeypatch)
    payload = _official_tesco_payload(_official_tesco_leaflet(page_count=page_count))
    monkeypatch.setattr(
        collector.requests, "post", lambda _url, **_kwargs: _json_response(payload)
    )

    with pytest.raises(ValueError, match="aktuálny týždenný leták"):
        collector.official_tesco_pages(today=TODAY)


def test_official_tesco_records_exact_supermarket_format_provenance(monkeypatch):
    _use_local_tesco(monkeypatch)
    payload = _official_tesco_payload(
        _official_tesco_leaflet(leaflet_type="HM", page_count=12),
        _official_tesco_leaflet(leaflet_id=692, leaflet_type="SM", page_count=10),
    )
    monkeypatch.setattr(
        collector.requests, "post", lambda _url, **_kwargs: _json_response(payload)
    )

    _pages, manifest = collector.official_tesco_pages(
        today=TODAY, leaflet_format="SM"
    )

    assert manifest["leaflet_format"] == "SM"
    assert manifest["store_label"] == "Tesco supermarket"
    assert "/supermarkety/" in manifest["source_url"]


def test_production_tesco_without_bridge_fails_closed_before_any_network(
        monkeypatch, capsys):
    monkeypatch.setenv("UVARSI_ENV", "production")
    monkeypatch.delenv("UVARSI_TESCO_BRIDGE_URL", raising=False)
    monkeypatch.delenv("UVARSI_TESCO_BRIDGE_SECRET", raising=False)
    monkeypatch.setattr(
        collector.requests,
        "post",
        lambda *_args, **_kwargs: pytest.fail("production must not call Tesco directly"),
    )
    monkeypatch.setattr(
        collector,
        "kupino_meta",
        lambda _store: pytest.fail("production Tesco must not use an aggregator"),
    )

    assert collector.store_pages("tesco", today=TODAY) == ([], None)
    assert "bridge" in capsys.readouterr().out.lower()


def test_official_tesco_downloads_each_bridge_page_once_and_resizes_locally(
        monkeypatch, tmp_path):
    from PIL import Image

    secret = "media-bridge-secret"
    monkeypatch.setenv("UVARSI_TESCO_BRIDGE_URL", "https://tesco-bridge.example")
    monkeypatch.setenv("UVARSI_TESCO_BRIDGE_SECRET", secret)
    leaflet = _bridge_tesco_leaflet(page_count=8)
    pages = [
        (page["thumbnail_url"], page["image_url"])
        for page in leaflet["pages"]
    ]
    leaflet_format = leaflet.pop("format")
    manifest = {
        **leaflet,
        "collector_kind": "official-tesco-viewer",
        "leaflet_format": leaflet_format,
        "store_label": "Tesco hypermarket",
    }
    monkeypatch.setattr(collector, "store_pages", lambda _store: (pages, manifest))

    image = BytesIO()
    Image.new("RGB", (24, 24), color=(250, 245, 230)).save(image, format="JPEG")
    image_bytes = image.getvalue()
    downloads = []

    def get(url, **kwargs):
        downloads.append((url, kwargs))
        return types.SimpleNamespace(status_code=200, content=image_bytes)

    monkeypatch.setattr(collector.requests, "get", get)

    resize_calls = []
    original_resize = collector.image_bytes_b64

    def tracked_resize(content, max_px):
        resize_calls.append(max_px)
        return original_resize(content, max_px)

    monkeypatch.setattr(collector, "image_bytes_b64", tracked_resize)
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    staged_names = []

    def claude_json(_client, model, _content, _max_tokens, effort=None):
        if model == collector.MODEL_SCAN:
            staged_names[:] = sorted(path.name for path in tmp_path.rglob("*.jpeg"))
            return [1]
        return [{
            "source_page": 1,
            "nazov": "Ryža",
            "kategoria": "trvanlive",
            "cena": 1.49,
            "povodna": None,
            "zlava": None,
            "jednotka": "kg",
            "cena_s_kartou": None,
            "zlava_s_kartou": None,
            "vernostny_program": None,
            "minimalny_nakup": None,
            "podmienka_s_kartou": None,
        }]

    monkeypatch.setattr(collector, "claude_json", claude_json)

    offers = collector.zbieraj(object(), "tesco")

    assert offers[0]["obchod"] == "Tesco"
    assert offers[0]["store_label"] == "Tesco hypermarket"
    assert [url for url, _kwargs in downloads] == [image for _thumb, image in pages]
    assert resize_calls.count(collector.SCAN_PX) == 8
    assert resize_calls.count(collector.READ_PX) == 1
    assert staged_names == [f"tesco-page-{page:03d}.jpeg" for page in range(1, 9)]
    assert list(tmp_path.iterdir()) == []
    assert all(
        call["headers"]["Authorization"] == f"Bearer {secret}"
        for _url, call in downloads
    )


def test_tesco_bridge_fingerprints_actual_page_bytes_before_paid_ai(monkeypatch):
    monkeypatch.setenv("UVARSI_TESCO_BRIDGE_URL", "https://tesco-bridge.example")
    monkeypatch.setenv("UVARSI_TESCO_BRIDGE_SECRET", "media-bridge-secret")
    monkeypatch.setattr(collector, "business_day", lambda: TODAY)
    leaflet = _bridge_tesco_leaflet(page_count=8)
    monkeypatch.setattr(
        collector.requests,
        "post",
        lambda _url, **_kwargs: _json_response({"leaflet": leaflet}),
    )
    content = {"revision": b"first"}

    def get(url, **_kwargs):
        page = url.rsplit("-", 1)[-1].encode("ascii")
        return types.SimpleNamespace(
            status_code=200, content=content["revision"] + b":" + page
        )

    monkeypatch.setattr(collector.requests, "get", get)

    first = collector.prepare_store_collection("tesco")
    content["revision"] = b"corrected"
    second = collector.prepare_store_collection("tesco")

    assert first.manifest["source_identity"].startswith("page-content-sha256:")
    assert first.provenance.source_fingerprint != second.provenance.source_fingerprint


def test_official_tesco_cleans_staged_pages_when_scan_fails(monkeypatch, tmp_path):
    from PIL import Image

    monkeypatch.setenv("UVARSI_TESCO_BRIDGE_URL", "https://tesco-bridge.example")
    monkeypatch.setenv("UVARSI_TESCO_BRIDGE_SECRET", "media-bridge-secret")
    leaflet = _bridge_tesco_leaflet(page_count=8)
    pages = [(page["thumbnail_url"], page["image_url"]) for page in leaflet["pages"]]
    manifest = {
        **leaflet,
        "collector_kind": "official-tesco-viewer",
        "leaflet_format": leaflet.pop("format"),
        "store_label": "Tesco hypermarket",
    }
    monkeypatch.setattr(collector, "store_pages", lambda _store: (pages, manifest))

    image = BytesIO()
    Image.new("RGB", (24, 24), color=(250, 245, 230)).save(image, format="JPEG")
    monkeypatch.setattr(
        collector.requests,
        "get",
        lambda _url, **_kwargs: types.SimpleNamespace(status_code=200, content=image.getvalue()),
    )
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    staged_names = []

    def fail_scan(_client, _model, _content, _max_tokens, effort=None):
        staged_names[:] = sorted(path.name for path in tmp_path.rglob("*.jpeg"))
        raise RuntimeError("scan boom")

    monkeypatch.setattr(collector, "claude_json", fail_scan)

    with pytest.raises(ValueError, match="sken strán zlyhal"):
        collector.zbieraj(object(), "tesco")

    assert staged_names == [f"tesco-page-{page:03d}.jpeg" for page in range(1, 9)]
    assert list(tmp_path.iterdir()) == []


def test_every_declared_manifest_is_rejected_above_120_pages_before_ai_work():
    pages, manifest = flyer_fixture(121)
    manifest["declared_pages"] = 121

    with pytest.raises(ValueError, match="120"):
        collector.validate_flyer_manifest(pages, manifest, store="lidl")


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda manifest: manifest.pop("declared_pages"), "deklarovaný počet"),
        (lambda manifest: manifest.update(declared_pages=7), "deklarovaný počet"),
        (lambda manifest: manifest.pop("leaflet_format"), "formát"),
        (lambda manifest: manifest.update(leaflet_format="XX"), "formát"),
        (lambda manifest: manifest.pop("store_label"), "označenie predajne"),
        (lambda manifest: manifest.update(store_label="Tesco"), "označenie predajne"),
    ],
)
def test_official_tesco_manifest_requires_complete_hm_provenance(mutation, message):
    leaflet = _bridge_tesco_leaflet(page_count=8)
    pages = [(page["thumbnail_url"], page["image_url"]) for page in leaflet["pages"]]
    manifest = {
        **leaflet,
        "collector_kind": "official-tesco-viewer",
        "leaflet_format": leaflet.pop("format"),
        "store_label": "Tesco hypermarket",
    }
    mutation(manifest)

    with pytest.raises(ValueError, match=message):
        collector.validate_flyer_manifest(pages, manifest, store="tesco")


def test_official_tesco_manifest_rejects_complete_leaflet_below_eight_pages():
    leaflet = _bridge_tesco_leaflet(page_count=7)
    pages = [(page["thumbnail_url"], page["image_url"]) for page in leaflet["pages"]]
    manifest = {
        **leaflet,
        "collector_kind": "official-tesco-viewer",
        "leaflet_format": leaflet.pop("format"),
        "store_label": "Tesco hypermarket",
    }

    with pytest.raises(ValueError, match="8"):
        collector.validate_flyer_manifest(pages, manifest, store="tesco")


def test_non_tesco_manifest_does_not_require_tesco_provenance_fields():
    pages, manifest = flyer_fixture(1)

    page_manifest = collector.validate_flyer_manifest(pages, manifest, store="lidl")

    assert list(page_manifest) == [1]


def test_store_pages_prefers_official_tesco_over_aggregators(monkeypatch):
    expected = flyer_fixture(46)
    monkeypatch.setattr(
        collector,
        "official_tesco_pages",
        lambda today=None, leaflet_format="HM": expected,
    )
    monkeypatch.setattr(
        collector,
        "kupino_meta",
        lambda store: pytest.fail("official Tesco must be tried before an aggregator"),
    )

    assert collector.store_pages("tesco", today=TODAY) == expected


@pytest.mark.parametrize(
    ("store", "official_name"),
    [
        ("lidl", "official_lidl_pages"),
        ("tesco", "official_tesco_pages"),
    ],
)
def test_store_pages_preserves_partial_official_manifest_failure(
    monkeypatch, store, official_name,
):
    attempted = prepared_collection(store).provenance

    def partial_failure(*_args, **_kwargs):
        raise collector.ManifestPreparationError("partial official manifest", attempted)

    monkeypatch.setattr(collector, official_name, partial_failure)
    monkeypatch.setattr(
        collector,
        "kupino_meta",
        lambda _store: pytest.fail("partial official provenance must not be discarded"),
    )

    with pytest.raises(collector.ManifestPreparationError) as failure:
        collector.store_pages(store, today=TODAY)

    assert failure.value.attempted_provenance == attempted


def test_expired_official_tesco_falls_back_without_publishing_old_prices(monkeypatch):
    _use_local_tesco(monkeypatch)
    expired = _official_tesco_payload(
        _official_tesco_leaflet(
            valid_from="2026-08-10T06:00:00.000Z",
            valid_to="2026-08-16T21:59:59.000Z",
        )
    )
    monkeypatch.setattr(
        collector.requests,
        "post",
        lambda url, **_kwargs: _json_response(expired),
    )
    monkeypatch.setattr(
        collector.requests,
        "get",
        lambda url, **_kwargs: types.SimpleNamespace(text=""),
    )
    monkeypatch.setattr(collector, "kupino_meta", lambda store: kupino_flyer())
    monkeypatch.setattr(
        collector,
        "page_exists",
        lambda url: "current-page" if "-1_320.jpg" in url else None,
    )

    pages, manifest = collector.store_pages("tesco", today=TODAY)

    assert pages
    assert manifest["collector_kind"] == "kupino-aggregator"


def test_official_tesco_rejects_truncated_or_foreign_page_manifest(monkeypatch):
    _use_local_tesco(monkeypatch)
    leaflet = _official_tesco_leaflet(page_count=8)
    leaflet["pages"][-1]["pagePNG"] = "https://evil.example/offer.8.jpeg"
    payload = _official_tesco_payload(leaflet)
    monkeypatch.setattr(
        collector.requests,
        "post",
        lambda url, **_kwargs: _json_response(payload),
    )
    monkeypatch.setattr(
        collector.requests,
        "get",
        lambda *_args, **_kwargs: pytest.fail("Tesco must use its direct API"),
    )

    with pytest.raises(ValueError, match="manifest"):
        collector.official_tesco_pages(today=date(2026, 8, 20))


def test_actual_tesco_validation_failure_preserves_safe_attempted_identity(
        monkeypatch, tmp_path):
    _use_local_tesco(monkeypatch)
    leaflet = _official_tesco_leaflet(page_count=8)
    leaflet["pages"][-1]["pagePNG"] = "https://evil.example/offer.8.jpeg"
    monkeypatch.setattr(
        collector.requests,
        "post",
        lambda _url, **_kwargs: _json_response(_official_tesco_payload(leaflet)),
    )
    monkeypatch.setattr(collector, "kupino_meta", lambda _store: None)
    monkeypatch.setattr(collector, "mletaky_base", lambda _store, _today: None)

    with pytest.raises(collector.ManifestPreparationError) as failure:
        collector.store_pages("tesco", today=TODAY)

    attempted = failure.value.attempted_provenance
    assert attempted.collector_kind == "official-tesco-viewer"
    assert attempted.valid_from == "2026-08-17"
    assert attempted.valid_to == "2026-08-23"
    assert re.fullmatch(r"[0-9a-f]{64}", attempted.source_fingerprint)

    database = tmp_path / "uvarsi.db"
    monkeypatch.setattr(collector, "DB", str(database))
    monkeypatch.setattr(collector, "monday", lambda: "2026-08-17")
    monkeypatch.setattr(collector, "business_day", lambda: TODAY)
    with pytest.raises(SystemExit, match="tesco"):
        collector.main(["tesco"])

    con = sqlite3.connect(database)
    persisted = con.execute(
        "SELECT attempted_collector_kind,attempted_fingerprint,"
        "attempted_valid_from,attempted_valid_to,failure_kind "
        "FROM zber_staging_stav WHERE obchod='Tesco'"
    ).fetchone()
    con.close()
    assert persisted == (
        "official-tesco-viewer",
        attempted.source_fingerprint,
        "2026-08-17",
        "2026-08-23",
        "structural",
    )


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


def test_actual_lidl_validation_failure_preserves_safe_attempted_identity(
        monkeypatch, tmp_path):
    overview = (
        '<a href="https://www.lidl.sk/l/sk/letak/'
        'online-letak-platny-od-17-08-2026/ar/1">Pozri si leták</a>'
    )
    payload = _official_lidl_payload(page_count=8)
    payload["flyer"]["pages"][-1]["zoom"] = "https://evil.example/page-8.jpg"

    def get(url, **_kwargs):
        if url == collector.LIDL_OVERVIEW_URL:
            return types.SimpleNamespace(text=overview)
        return _json_response(payload)

    monkeypatch.setattr(collector.requests, "get", get)
    monkeypatch.setattr(collector, "kupino_meta", lambda _store: None)
    monkeypatch.setattr(collector, "mletaky_base", lambda _store, _today: None)

    with pytest.raises(collector.ManifestPreparationError) as failure:
        collector.store_pages("lidl", today=TODAY)

    attempted = failure.value.attempted_provenance
    assert attempted.collector_kind == "official-lidl-viewer"
    assert attempted.valid_from == "2026-08-17"
    assert attempted.valid_to == "2026-08-23"
    assert re.fullmatch(r"[0-9a-f]{64}", attempted.source_fingerprint)

    database = tmp_path / "uvarsi.db"
    monkeypatch.setattr(collector, "DB", str(database))
    monkeypatch.setattr(collector, "monday", lambda: "2026-08-17")
    monkeypatch.setattr(collector, "business_day", lambda: TODAY)
    with pytest.raises(SystemExit, match="lidl"):
        collector.main(["lidl"])

    con = sqlite3.connect(database)
    persisted = con.execute(
        "SELECT attempted_collector_kind,attempted_fingerprint,"
        "attempted_valid_from,attempted_valid_to,failure_kind "
        "FROM zber_staging_stav WHERE obchod='Lidl'"
    ).fetchone()
    con.close()
    assert persisted == (
        "official-lidl-viewer",
        attempted.source_fingerprint,
        "2026-08-17",
        "2026-08-23",
        "structural",
    )


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


def _official_kaufland_html():
    payload = {
        "component": "OfferTemplate",
        "props": {"offerData": {"cycles": [{"categories": [
            {
                "displayName": "Trvanlivé potraviny",
                "dateFrom": "2026-08-17",
                "dateTo": "2026-08-23",
                "offers": [
                    {
                        "offerId": "oil",
                        "dateFrom": "2026-08-17",
                        "dateTo": "2026-08-23",
                        "title": "Raciol",
                        "subtitle": "Repkový olej",
                        "unit": "1 l",
                        "price": 1.69,
                        "discount": 43,
                        "formattedOldPrice": "2,99",
                        "loyaltyDiscount": 48,
                        "loyaltyFormattedPrice": "1,55",
                        "detailDescription": "Nakúpte nad 20 € a získate cenu s kartou.",
                    },
                    {
                        "offerId": "future",
                        "dateFrom": "2026-08-22",
                        "dateTo": "2026-08-23",
                        "title": "Budúca ryža",
                        "unit": "1 kg",
                        "price": 0.99,
                        "discount": 50,
                        "formattedOldPrice": "1,99",
                    },
                ],
            },
            {
                "displayName": "Dom, domácnosť",
                "dateFrom": "2026-08-17",
                "dateTo": "2026-08-23",
                "offers": [{
                    "offerId": "pan",
                    "dateFrom": "2026-08-17",
                    "dateTo": "2026-08-23",
                    "title": "Panvica",
                    "unit": "1 kus",
                    "price": 9.99,
                    "discount": 50,
                    "formattedOldPrice": "19,99",
                }],
            },
            {
                "displayName": "Kaufland Card XTRA  17.08.2026 - 23.08.2026",
                "offers": [
                    {
                        "offerId": "cream",
                        "dateFrom": "2026-08-17",
                        "dateTo": "2026-08-23",
                        "unit": "200 ml",
                        "price": 1.19,
                        "discount": 20,
                        "formattedOldPrice": "1,49",
                        "loyaltyDiscount": 33,
                        "loyaltyFormattedPrice": "0,99",
                        "detailTitle": "Cena s Kaufland XTRA\nSmotana na varenie 15 %",
                        "detailDescription": "Rama Crema\nSmotana na varenie 15 %\n200 ml",
                    },
                    {
                        "offerId": "wine",
                        "dateFrom": "2026-08-17",
                        "dateTo": "2026-08-23",
                        "title": "Červené víno",
                        "unit": "0,75 l",
                        "price": 3.99,
                        "discount": 20,
                        "formattedOldPrice": "4,99",
                        "loyaltyDiscount": 30,
                        "loyaltyFormattedPrice": "3,49",
                        "detailDescription": "Červené víno 12 % alk.",
                    },
                    {
                        "offerId": "brumik",
                        "dateFrom": "2026-08-17",
                        "dateTo": "2026-08-23",
                        "title": "Brumík",
                        "subtitle": "Mliečny rez",
                        "unit": "5 x 30 g",
                        "price": 2.49,
                        "discount": 24,
                        "formattedOldPrice": "3,29",
                        "loyaltyDiscount": 30,
                        "loyaltyFormattedPrice": "2,29",
                    },
                ],
            },
            {
                "displayName": "Nápoje",
                "offers": [
                    {
                        "offerId": "water",
                        "dateFrom": "2026-08-17",
                        "dateTo": "2026-08-23",
                        "title": "Minerálna voda",
                        "unit": "1,5 l",
                        "price": 0.49,
                        "discount": 28,
                        "formattedOldPrice": "0,69",
                    },
                    {
                        "offerId": "drink-wine",
                        "dateFrom": "2026-08-17",
                        "dateTo": "2026-08-23",
                        "title": "Biele víno",
                        "unit": "0,75 l",
                        "price": 3.99,
                        "discount": 20,
                        "formattedOldPrice": "4,99",
                    },
                ],
            },
            {
                "displayName": "Ponuka OD DO  19.08.2026 - 20.08.2026",
                "offers": [{
                    "offerId": "short-tomato",
                    "dateFrom": "2026-08-19",
                    "dateTo": "2026-08-20",
                    "title": "Paradajky",
                    "unit": "1 kg",
                    "price": 1.49,
                    "discount": 25,
                    "formattedOldPrice": "1,99",
                }],
            },
            {
                "displayName": "Proteín  01.08.2026 - 31.08.2026",
                "offers": [{
                    "offerId": "protein-pasta",
                    "dateFrom": "2026-08-01",
                    "dateTo": "2026-08-31",
                    "title": "Proteínové cestoviny",
                    "unit": "250 g",
                    "price": 1.49,
                    "discount": 25,
                    "formattedOldPrice": "1,99",
                }],
            },
            {
                "displayName": "Aktuálna ponuka – top produkty",
                "offers": [
                    {
                        "offerId": "top-rice",
                        "dateFrom": "2026-08-17",
                        "dateTo": "2026-08-23",
                        "title": "Ryža dlhozrnná",
                        "unit": "1 kg",
                        "price": 1.49,
                        "discount": 25,
                        "formattedOldPrice": "1,99",
                    },
                    {
                        "offerId": "top-pan",
                        "dateFrom": "2026-08-17",
                        "dateTo": "2026-08-23",
                        "title": "Panvica",
                        "unit": "1 kus",
                        "price": 9.99,
                        "discount": 50,
                        "formattedOldPrice": "19,99",
                    },
                    {
                        "offerId": "top-binder",
                        "dateFrom": "2026-08-17",
                        "dateTo": "2026-08-23",
                        "title": "Talentus Zakladač",
                        "unit": "1 kus",
                        "price": 1.99,
                        "discount": 50,
                        "formattedOldPrice": "3,99",
                    },
                ],
            },
        ]}]}}
    }
    return (
        "<html><script>window.SSR = window.SSR || {}; "
        "window.SSR['fixture'] = " + json.dumps(payload, ensure_ascii=False)
        + ";</script></html>"
    )


def test_official_kaufland_reads_current_food_prices_without_ai(monkeypatch):
    monkeypatch.setattr(
        collector.requests,
        "get",
        lambda *args, **kwargs: types.SimpleNamespace(
            status_code=200,
            text=_official_kaufland_html(),
            headers={"content-type": "text/html; charset=UTF-8"},
        ),
    )

    offers = collector.official_kaufland_offers(today=TODAY)

    assert len(offers) == 7
    assert offers[0] == {
        "obchod": "Kaufland",
        "nazov": "Raciol Repkový olej 1 l",
        "kategoria": "trvanlive",
        "cena": 1.69,
        "povodna": 2.99,
        "zlava": "-43 %",
        "jednotka": "l",
        "cena_s_kartou": 1.55,
        "zlava_s_kartou": "-48 %",
        "vernostny_program": "Kaufland Card",
        "minimalny_nakup": 20.0,
        "podmienka_s_kartou": "Nákup aspoň za 20 €",
        "source_url": collector.KAUFLAND_OFFERS_URL,
        "source_page": 1,
        "valid_from": "2026-08-17",
        "valid_to": "2026-08-23",
    }
    assert offers[1]["nazov"] == "Rama Crema Smotana na varenie 15 % 200 ml"
    assert offers[1]["kategoria"] == "mliecne"
    assert offers[1]["cena"] == 1.19
    assert offers[1]["cena_s_kartou"] == 0.99
    by_name = {offer["nazov"]: offer for offer in offers}
    assert "Brumík Mliečny rez 5 x 30 g" in by_name
    assert "Minerálna voda 1,5 l" in by_name
    assert "Proteínové cestoviny 250 g" in by_name
    assert "Ryža dlhozrnná 1 kg" in by_name
    assert "Panvica 1 kus" not in by_name
    assert "Talentus Zakladač 1 kus" not in by_name
    assert by_name["Paradajky 1 kg"]["valid_from"] == "2026-08-19"
    assert by_name["Paradajky 1 kg"]["valid_to"] == "2026-08-20"


def test_official_kaufland_recovery_persists_without_loading_anthropic_key(
    monkeypatch, tmp_path
):
    database = tmp_path / "uvarsi.db"
    monkeypatch.setattr(collector, "DB", str(database))
    monkeypatch.setattr(collector, "monday", lambda: "2026-08-17")
    offers = []
    for index in range(20):
        offer = valid_offer("kaufland", index + 1)
        offer.update(
            source_url=collector.KAUFLAND_OFFERS_URL,
            source_page=1,
        )
        offers.append(offer)
    monkeypatch.setattr(collector, "official_kaufland_offers", lambda: offers)
    monkeypatch.setattr(
        collector,
        "load_key",
        lambda: pytest.fail("oficiálny Kaufland nesmie načítať Anthropic kľúč"),
    )

    collector.official_kaufland_main()

    con = sqlite3.connect(database)
    assert con.execute("SELECT COUNT(*) FROM akcie WHERE obchod='Kaufland'").fetchone()[0] == 0
    assert con.execute(
        "SELECT COUNT(*) FROM akcie_staging WHERE obchod='Kaufland'"
    ).fetchone()[0] == 20
    assert con.execute(
        "SELECT collector_kind,stav FROM zber_staging_stav WHERE obchod='Kaufland'"
    ).fetchone() == ("official-kaufland-offers", "ok")
    con.close()


def test_official_kaufland_failure_preserves_previous_collection_state(
    monkeypatch, tmp_path
):
    database = tmp_path / "uvarsi.db"
    monkeypatch.setattr(collector, "DB", str(database))
    monkeypatch.setattr(collector, "monday", lambda: "2026-08-17")
    con = collector.db()
    collector.record_store_outcome(
        con, "2026-08-17", "Kaufland", "fail", 0, "pôvodný platený pád"
    )
    con.execute(
        "UPDATE zber_stav SET updated='2026-08-20 07:00:00' "
        "WHERE tyzden='2026-08-17' AND obchod='Kaufland'"
    )
    con.commit()
    before = tuple(con.execute(
        "SELECT stav,detail,updated FROM zber_stav "
        "WHERE tyzden='2026-08-17' AND obchod='Kaufland'"
    ).fetchone())
    con.close()
    monkeypatch.setattr(
        collector,
        "official_kaufland_offers",
        lambda: (_ for _ in ()).throw(ValueError("oficiálny zdroj je nedostupný")),
    )

    with pytest.raises(SystemExit, match="oficiálny zdroj je nedostupný"):
        collector.official_kaufland_main()

    con = sqlite3.connect(database)
    after = con.execute(
        "SELECT stav,detail,updated FROM zber_stav "
        "WHERE tyzden='2026-08-17' AND obchod='Kaufland'"
    ).fetchone()
    con.close()
    assert after == before


def test_official_kaufland_success_is_atomic_with_collection_state(
    monkeypatch, tmp_path
):
    database = tmp_path / "uvarsi.db"
    monkeypatch.setattr(collector, "DB", str(database))
    monkeypatch.setattr(collector, "monday", lambda: "2026-08-17")
    previous = valid_offer("kaufland", 1)
    previous["nazov"] = "Pôvodná bezpečná položka"
    con = collector.db()
    replace_store_week(con, "2026-08-17", "Kaufland", [previous])
    con.close()
    fresh = [valid_offer("kaufland", index) for index in range(1, 21)]
    monkeypatch.setattr(collector, "official_kaufland_offers", lambda: fresh)
    original_record = collector.record_store_outcome

    def fail_success_state(con, week, store, status, *args, **kwargs):
        if status == "ok":
            raise sqlite3.OperationalError("stav zberu sa nedá zapísať")
        return original_record(con, week, store, status, *args, **kwargs)

    monkeypatch.setattr(collector, "record_store_outcome", fail_success_state)

    with pytest.raises(SystemExit, match="stav zberu sa nedá zapísať"):
        collector.official_kaufland_main()

    con = sqlite3.connect(database)
    names = [row[0] for row in con.execute(
        "SELECT nazov FROM akcie WHERE tyzden='2026-08-17' AND obchod='Kaufland'"
    )]
    status = con.execute(
        "SELECT stav FROM zber_stav "
        "WHERE tyzden='2026-08-17' AND obchod='Kaufland'"
    ).fetchone()
    con.close()
    assert names == ["Pôvodná bezpečná položka"]
    assert status is None


def test_collection_state_covers_all_active_official_kaufland_windows(
    monkeypatch, tmp_path
):
    database = tmp_path / "uvarsi.db"
    old = valid_offer("kaufland", 1)
    new = valid_offer("kaufland", 2)
    for offer in (old, new):
        offer["source_url"] = collector.KAUFLAND_OFFERS_URL
        offer["source_page"] = 1
    old.update(valid_from="2026-08-01", valid_to="2026-08-31")
    new.update(valid_from="2026-08-19", valid_to="2026-08-20")
    monkeypatch.setattr(collector, "DB", str(database))
    con = collector.db()
    collector.record_store_outcome(
        con, "2026-08-17", "Kaufland", "ok", 2, offers=[old, new]
    )
    window = con.execute(
        "SELECT valid_from,valid_to FROM zber_stav "
        "WHERE tyzden='2026-08-17' AND obchod='Kaufland'"
    ).fetchone()
    con.close()
    assert tuple(window) == ("2026-08-01", "2026-08-31")


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


def test_one_invalid_price_is_quarantined_without_rereading_a_healthy_page(monkeypatch):
    """Jedna zle prečítaná cenovka nesmie zahodiť ostatné ceny ani platiť Opus."""
    pages, manifest = flyer_fixture(1)
    monkeypatch.setattr(collector, "store_pages", lambda store: (pages, manifest))
    monkeypatch.setattr(collector, "get_b64", lambda url, max_px: url)
    models = []

    def item(name, price, discount):
        return {
            "source_page": 1,
            "nazov": name,
            "kategoria": "trvanlive",
            "cena": price,
            "povodna": 2.99,
            "zlava": discount,
            "jednotka": "l",
            "cena_s_kartou": None,
            "zlava_s_kartou": None,
            "vernostny_program": None,
            "minimalny_nakup": None,
            "podmienka_s_kartou": None,
        }

    def fake_claude_json(client, model, content, max_tokens, effort=None):
        models.append(model)
        if model == collector.MODEL_SCAN:
            return [1]
        return [
            item("Repkový olej Raciol", 0.07, "-48 %"),
            item("Olivový olej", 1.99, "-33 %"),
        ]

    monkeypatch.setattr(collector, "claude_json", fake_claude_json)

    offers = collector.zbieraj(object(), "kaufland")

    assert [offer["nazov"] for offer in offers] == ["Olivový olej"]
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
        "INSERT INTO zber_staging_stav (tyzden, obchod, stav, pocet, data_version) VALUES (?,?,?,?,?)",
        [
            (week, "Kaufland", "ok", 40, collector.COLLECTION_DATA_VERSION),
            (week, "Tesco", "ok", 40, collector.COLLECTION_DATA_VERSION - 1),
        ],
    )

    assert collector.collection_budget_purpose(
        con, week, ["kaufland", "tesco"]
    ) == "zber_migracia"

    con.execute(
        "UPDATE zber_staging_stav SET data_version=? WHERE obchod='Tesco'",
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
        "INSERT INTO zber_staging_stav (tyzden, obchod, stav, pocet, data_version) VALUES (?,?,?,?,?)",
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
    official_urls = {
        "kaufland": collector.KAUFLAND_OFFERS_URL,
        "lidl": "https://www.lidl.sk/c/akcny-letak/s10023254",
        "tesco": (
            "https://www.tesco.sk/akciove-ponuky/letaky-a-katalogy/"
            "hypermarkety/tesco-letak-2026-08-17/1"
        ),
    }
    return {
        "obchod": store.capitalize(),
        "nazov": f"Položka {index}",
        "kategoria": "trvanlive",
        "cena": 1.0 + index / 100,
        "povodna": 2.0,
        "zlava": "-50 %",
        "jednotka": "ks",
        "source_url": official_urls[store],
        "source_page": index,
        "valid_from": "2026-08-17",
        "valid_to": "2026-08-23",
    }


def prepared_collection(store):
    offer = valid_offer(store, 1)
    kind = collector.source_policy.collector_kind_for_url(offer["source_url"])
    pages = [
        (f"https://images.example/{store}-{page}-thumb.jpg",
         f"https://images.example/{store}-{page}-full.jpg")
        for page in range(1, 21)
    ]
    manifest = {
        "source_url": offer["source_url"],
        "collector_kind": kind,
        "source_identity": f"test-manifest:{store}:2026-08-17",
        "valid_from": "2026-08-17",
        "valid_to": "2026-08-23",
        "declared_pages": len(pages),
        "pages": [
            {
                "source_page": page,
                "thumbnail_url": urls[0],
                "image_url": urls[1],
            }
            for page, urls in enumerate(pages, start=1)
        ],
    }
    return collector.PreparedCollection(
        pages=pages,
        manifest=manifest,
        page_manifest={row["source_page"]: row for row in manifest["pages"]},
        provenance=collector.provenance_from_manifest(manifest),
    )


def run_main_over_stores(monkeypatch, tmp_path, outcomes):
    database = tmp_path / "uvarsi.db"
    monkeypatch.setattr(collector, "DB", str(database))
    monkeypatch.setattr(collector, "monday", lambda: "2026-08-17")
    monkeypatch.setattr(collector, "business_day", lambda: date(2026, 8, 19))
    monkeypatch.setattr(collector, "STORES", list(outcomes))
    monkeypatch.setattr(collector, "load_key", lambda: "unused-test-value")
    monkeypatch.setattr(collector.source_policy, "approved_source", lambda *_args: True)
    monkeypatch.setattr(collector, "prepare_store_collection", prepared_collection)

    def zbieraj(client, store, prepared=None):
        assert prepared == prepared_collection(store)
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
            for row in con.execute(
                "SELECT obchod, stav FROM zber_staging_stav WHERE tyzden=?",
                ("2026-08-17",),
            )
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
        "SELECT obchod, stav, pocet FROM zber_staging_stav "
        "WHERE tyzden=? ORDER BY obchod", ("2026-08-17",)
    ).fetchall()
    assert con.execute("SELECT COUNT(*) FROM akcie").fetchone()[0] == 0
    assert con.execute("SELECT COUNT(*) FROM zber_stav").fetchone()[0] == 0
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
        lambda client, store, prepared=None: [valid_offer(store, 1)],
    )

    with pytest.raises(SystemExit, match="lidl"):
        collector.main()

    con = sqlite3.connect(database)
    outcome = con.execute(
        "SELECT stav, pocet FROM zber_staging_stav WHERE tyzden=? AND obchod='Lidl'",
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
        "SELECT obchod, stav, pocet FROM zber_staging_stav ORDER BY obchod"
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


def test_a_current_healthy_stage_is_reused_without_calling_the_source_again(monkeypatch, tmp_path):
    database = run_main_over_stores(monkeypatch, tmp_path, {"lidl": True})
    collector.main()

    monkeypatch.setattr(
        collector, "zbieraj", lambda client, store: (_ for _ in ()).throw(ValueError("prázdny leták"))
    )
    collector.main()

    con = sqlite3.connect(database)
    con.row_factory = sqlite3.Row
    row = con.execute(
        "SELECT stav, pocet FROM zber_staging_stav WHERE tyzden=?",
        ("2026-08-17",),
    ).fetchone()
    con.close()

    assert (row["stav"], row["pocet"]) == ("ok", 20)


def seed_stage(con, week, store, count=20):
    offers = [valid_offer(store.lower(), index) for index in range(1, count + 1)]
    collector.stage_store_collection(
        con, week, store, offers,
        provenance=prepared_collection(store.lower()).provenance,
    )
    return offers


def test_record_store_outcome_does_not_commit_an_outer_transaction():
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.executescript(collector.SCHEMA)

    con.execute("BEGIN IMMEDIATE")
    collector.record_store_outcome(con, "2026-08-17", "Lidl", "fail", detail="test")
    assert con.in_transaction is True
    con.rollback()

    assert con.execute("SELECT COUNT(*) FROM zber_stav").fetchone()[0] == 0


def test_partial_three_store_run_keeps_active_week_untouched(monkeypatch, tmp_path):
    database = run_main_over_stores(
        monkeypatch, tmp_path, {"kaufland": True, "tesco": True, "lidl": False}
    )
    con = collector.db()
    old = valid_offer("lidl", 99)
    old["nazov"] = "Pôvodný aktívny týždeň"
    replace_store_week(con, "2026-08-17", "Lidl", [old])
    con.close()
    monkeypatch.setattr(collector.source_policy, "approved_source", lambda *_args: True)

    with pytest.raises(SystemExit, match="lidl"):
        collector.main()

    con = sqlite3.connect(database)
    assert con.execute(
        "SELECT obchod,nazov FROM akcie WHERE tyzden='2026-08-17'"
    ).fetchall() == [("Lidl", "Pôvodný aktívny týždeň")]
    assert con.execute(
        "SELECT obchod,stav,pocet FROM zber_staging_stav ORDER BY obchod"
    ).fetchall() == [
        ("Kaufland", "ok", 20),
        ("Lidl", "fail", 0),
        ("Tesco", "ok", 20),
    ]
    con.close()


def test_three_valid_approved_stages_promote_in_one_active_snapshot(monkeypatch, tmp_path):
    database = tmp_path / "uvarsi.db"
    monkeypatch.setattr(collector, "DB", str(database))
    monkeypatch.setattr(collector.source_policy, "approved_source", lambda *_args: True)
    con = collector.db()
    for store in ("Kaufland", "Tesco", "Lidl"):
        seed_stage(con, "2026-08-17", store)
    staged_fingerprints = dict(con.execute(
        "SELECT obchod,source_fingerprint FROM zber_staging_stav"
    ).fetchall())

    assert collector.promote_staged_week(
        con, "2026-08-17", today=date(2026, 8, 19)
    ) is True

    assert [tuple(row) for row in con.execute(
        "SELECT obchod,COUNT(*) FROM akcie WHERE tyzden='2026-08-17' "
        "GROUP BY obchod ORDER BY obchod"
    ).fetchall()] == [("Kaufland", 20), ("Lidl", 20), ("Tesco", 20)]
    assert [tuple(row) for row in con.execute(
        "SELECT obchod,stav,pocet FROM zber_stav WHERE tyzden='2026-08-17' "
        "ORDER BY obchod"
    ).fetchall()] == [
        ("Kaufland", "ok", 20),
        ("Lidl", "ok", 20),
        ("Tesco", "ok", 20),
    ]
    assert dict(con.execute(
        "SELECT obchod,source_fingerprint FROM zber_stav"
    ).fetchall()) == staged_fingerprints
    con.close()


@pytest.mark.parametrize(
    "break_stage",
    [
        lambda con: con.execute(
            "UPDATE zber_staging_stav SET stav='fail' WHERE obchod='Tesco'"
        ),
        lambda con: con.execute(
            "UPDATE zber_staging_stav SET data_version=data_version-1 WHERE obchod='Tesco'"
        ),
        lambda con: con.execute(
            "UPDATE zber_staging_stav SET valid_to='2026-08-18' WHERE obchod='Tesco'"
        ),
        lambda con: con.execute(
            "UPDATE zber_staging_stav SET source_fingerprint='bad' WHERE obchod='Tesco'"
        ),
        lambda con: con.execute(
            "DELETE FROM akcie_staging WHERE obchod='Tesco' AND source_page > 19"
        ),
    ],
    ids=("status", "data-version", "validity", "fingerprint", "minimum"),
)
def test_invalid_stage_cannot_replace_active_week(monkeypatch, break_stage):
    monkeypatch.setattr(collector.source_policy, "approved_source", lambda *_args: True)
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.executescript(collector.SCHEMA)
    collector.migrate_offer_staging_schema(con)
    old = valid_offer("lidl", 99)
    old["nazov"] = "Stará aktívna položka"
    replace_store_week(con, "2026-08-17", "Lidl", [old])
    for store in ("Kaufland", "Tesco", "Lidl"):
        seed_stage(con, "2026-08-17", store)
    break_stage(con)
    con.commit()

    assert collector.promote_staged_week(
        con, "2026-08-17", today=date(2026, 8, 19)
    ) is False

    assert [tuple(row) for row in con.execute("SELECT nazov FROM akcie").fetchall()] == [
        ("Stará aktívna položka",)
    ]


def test_unapproved_collector_cannot_promote(monkeypatch):
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.executescript(collector.SCHEMA)
    collector.migrate_offer_staging_schema(con)
    for store in ("Kaufland", "Tesco", "Lidl"):
        seed_stage(con, "2026-08-17", store)
    monkeypatch.setattr(
        collector.source_policy,
        "approved_source",
        lambda store, _kind: store != "Tesco",
    )

    assert collector.promote_staged_week(
        con, "2026-08-17", today=date(2026, 8, 19)
    ) is False
    assert con.execute("SELECT COUNT(*) FROM akcie").fetchone()[0] == 0


def test_targeted_retry_reuses_two_healthy_stages_and_promotes(monkeypatch):
    monkeypatch.setattr(collector.source_policy, "approved_source", lambda *_args: True)
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.executescript(collector.SCHEMA)
    collector.migrate_offer_staging_schema(con)
    seed_stage(con, "2026-08-17", "Kaufland")
    seed_stage(con, "2026-08-17", "Tesco")
    before = con.execute(
        "SELECT obchod,offer_key FROM akcie_staging ORDER BY obchod,offer_key"
    ).fetchall()

    seed_stage(con, "2026-08-17", "Lidl")
    assert collector.promote_staged_week(
        con, "2026-08-17", today=date(2026, 8, 19)
    ) is True

    after = con.execute(
        "SELECT obchod,offer_key FROM akcie_staging "
        "WHERE obchod IN ('Kaufland','Tesco') ORDER BY obchod,offer_key"
    ).fetchall()
    assert [tuple(row) for row in after] == [tuple(row) for row in before]
    assert con.execute("SELECT COUNT(*) FROM akcie").fetchone()[0] == 60


def test_complete_current_stage_waits_for_receipt_without_loading_anthropic(monkeypatch, tmp_path):
    database = tmp_path / "uvarsi.db"
    monkeypatch.setattr(collector, "DB", str(database))
    monkeypatch.setattr(collector, "monday", lambda: "2026-08-17")
    monkeypatch.setattr(collector, "business_day", lambda: date(2026, 8, 19))
    monkeypatch.setattr(collector.source_policy, "approved_source", lambda *_args: True)
    monkeypatch.setattr(collector, "prepare_store_collection", prepared_collection)
    con = collector.db()
    for store in ("Kaufland", "Tesco", "Lidl"):
        seed_stage(con, "2026-08-17", store)
    con.close()
    monkeypatch.setattr(
        collector,
        "load_key",
        lambda: pytest.fail("zdravý stage nesmie znovu načítať Anthropic kľúč"),
    )
    monkeypatch.setitem(
        sys.modules,
        "anthropic",
        types.SimpleNamespace(
            Anthropic=lambda **_kwargs: pytest.fail(
                "zdravý stage nesmie vytvoriť Anthropic klienta"
            )
        ),
    )

    collector.main()

    con = sqlite3.connect(database)
    assert con.execute("SELECT COUNT(*) FROM akcie").fetchone()[0] == 0
    assert con.execute("SELECT COUNT(*) FROM akcie_staging").fetchone()[0] == 60
    con.close()


def test_default_retry_collects_only_the_missing_stage(monkeypatch, tmp_path):
    database = tmp_path / "uvarsi.db"
    monkeypatch.setattr(collector, "DB", str(database))
    monkeypatch.setattr(collector, "monday", lambda: "2026-08-17")
    monkeypatch.setattr(collector, "business_day", lambda: date(2026, 8, 19))
    monkeypatch.setattr(collector, "STORES", ["kaufland", "tesco", "lidl"])
    monkeypatch.setattr(collector.source_policy, "approved_source", lambda *_args: True)
    con = collector.db()
    seed_stage(con, "2026-08-17", "Kaufland")
    seed_stage(con, "2026-08-17", "Tesco")
    con.close()
    requested = []

    monkeypatch.setattr(collector, "prepare_store_collection", prepared_collection)

    def collect_only_missing(_client, store, prepared=None):
        assert prepared == prepared_collection(store)
        requested.append(store)
        return [valid_offer(store, index) for index in range(1, 21)]

    monkeypatch.setattr(collector, "load_key", lambda: "unused-test-value")
    monkeypatch.setattr(collector, "zbieraj", collect_only_missing)
    monkeypatch.setitem(
        sys.modules,
        "anthropic",
        types.SimpleNamespace(Anthropic=lambda **_kwargs: object()),
    )

    collector.main()

    assert requested == ["lidl"]
    con = sqlite3.connect(database)
    assert con.execute("SELECT COUNT(*) FROM akcie").fetchone()[0] == 0
    assert con.execute("SELECT COUNT(*) FROM akcie_staging").fetchone()[0] == 60
    con.close()


def test_promotion_write_failure_rolls_back_active_rows_and_collection_state(monkeypatch):
    monkeypatch.setattr(collector.source_policy, "approved_source", lambda *_args: True)
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.executescript(collector.SCHEMA)
    collector.migrate_offer_staging_schema(con)
    old = valid_offer("lidl", 99)
    old["nazov"] = "Bezpečný aktívny snapshot"
    replace_store_week(con, "2026-08-17", "Lidl", [old])
    con.execute(
        "INSERT INTO zber_stav (tyzden,obchod,stav,pocet) VALUES (?,?,?,?)",
        ("2026-08-17", "Lidl", "ok", 1),
    )
    con.commit()
    for store in ("Kaufland", "Tesco", "Lidl"):
        seed_stage(con, "2026-08-17", store)
    con.execute(
        """CREATE TRIGGER reject_tesco_promotion
           BEFORE INSERT ON akcie WHEN NEW.obchod='Tesco'
           BEGIN SELECT RAISE(FAIL, 'simulated promotion failure'); END"""
    )
    con.commit()

    with pytest.raises(sqlite3.IntegrityError, match="simulated promotion failure"):
        collector.promote_staged_week(con, "2026-08-17", today=date(2026, 8, 19))

    assert [tuple(row) for row in con.execute("SELECT nazov FROM akcie").fetchall()] == [
        ("Bezpečný aktívny snapshot",)
    ]
    assert [tuple(row) for row in con.execute(
        "SELECT obchod,stav,pocet FROM zber_stav"
    ).fetchall()] == [("Lidl", "ok", 1)]


def test_twenty_duplicate_rows_do_not_satisfy_the_verified_minimum():
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.executescript(collector.SCHEMA)
    collector.migrate_offer_staging_schema(con)
    duplicate = valid_offer("lidl", 1)

    with pytest.raises(ValueError, match="unikátnych"):
        collector.stage_store_collection(
            con, "2026-08-17", "Lidl", [duplicate.copy() for _ in range(20)]
        )

    assert con.execute("SELECT COUNT(*) FROM akcie_staging").fetchone()[0] == 0


def test_reuse_rejects_even_allowlisted_aggregator_stage(monkeypatch):
    monkeypatch.setattr(collector.source_policy, "approved_source", lambda *_args: True)
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.executescript(collector.SCHEMA)
    collector.migrate_offer_staging_schema(con)
    offers = [valid_offer("lidl", index) for index in range(1, 21)]
    for offer in offers:
        offer["source_url"] = "https://www.kupino.sk/letak/lidl-test-current"
    collector.stage_store_collection(con, "2026-08-17", "Lidl", offers)

    assert collector.staged_store_problem(
        con, "2026-08-17", "Lidl", today=date(2026, 8, 19)
    ) == "source_not_official"


def test_main_recollects_an_aggregator_stage_from_current_official_manifest(
    monkeypatch, tmp_path,
):
    database = run_main_over_stores(monkeypatch, tmp_path, {"lidl": True})
    con = collector.db()
    offers = [valid_offer("lidl", index) for index in range(1, 21)]
    for offer in offers:
        offer["source_url"] = "https://www.kupino.sk/letak/lidl-test-current"
    collector.stage_store_collection(con, "2026-08-17", "Lidl", offers)
    con.close()
    calls = []

    def collect_official(client, store, prepared=None):
        calls.append(store)
        return [valid_offer(store, index) for index in range(1, 21)]

    monkeypatch.setattr(collector, "zbieraj", collect_official)
    collector.main(["lidl"])

    assert calls == ["lidl"]
    con = sqlite3.connect(database)
    assert con.execute(
        "SELECT collector_kind FROM zber_staging_stav WHERE obchod='Lidl'"
    ).fetchone()[0] == "official-lidl-viewer"
    con.close()


def test_manifest_fingerprint_changes_when_content_manifest_changes():
    base = {
        "source_url": "https://www.lidl.sk/c/akcny-letak/s10023254",
        "collector_kind": "official-lidl-viewer",
        "valid_from": "2026-08-17",
        "valid_to": "2026-08-23",
        "declared_pages": 2,
        "pages": [
            {"source_page": 1, "thumbnail_url": "https://img.test/a", "image_url": "https://img.test/A"},
            {"source_page": 2, "thumbnail_url": "https://img.test/b", "image_url": "https://img.test/B"},
        ],
    }
    changed = json.loads(json.dumps(base))
    changed["pages"][1]["image_url"] = "https://img.test/B-v2"

    assert collector.manifest_fingerprint(base) != collector.manifest_fingerprint(changed)


def test_manifest_fingerprint_ignores_rotating_tesco_bridge_access_tokens():
    first = prepared_collection("tesco").manifest
    second = json.loads(json.dumps(first))
    first["pages"][0]["image_url"] = (
        "https://tesco-bridge.example/v1/tesco/media/token-exp-100"
    )
    second["pages"][0]["image_url"] = (
        "https://tesco-bridge.example/v1/tesco/media/token-exp-200"
    )

    assert collector.manifest_fingerprint(first) == collector.manifest_fingerprint(second)


def test_db_claim_blocks_parallel_spend_and_allows_only_bounded_stale_recovery():
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.executescript(collector.SCHEMA)
    now = datetime(2026, 8, 19, 7, 0, tzinfo=timezone.utc)

    assert collector.claim_store_collection(
        con, "2026-08-17", "Lidl", "owner-a", "a" * 64, now=now
    ) is True
    assert collector.claim_store_collection(
        con, "2026-08-17", "Lidl", "owner-b", "a" * 64, now=now
    ) is False
    assert collector.claim_store_collection(
        con,
        "2026-08-17",
        "Lidl",
        "owner-b",
        "b" * 64,
        now=now.replace(hour=8),
    ) is True


def test_claim_owner_can_extend_lease_before_another_collector_reclaims_it():
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.executescript(collector.SCHEMA)
    start = datetime(2026, 8, 19, 7, 0, tzinfo=timezone.utc)
    assert collector.claim_store_collection(
        con,
        "2026-08-17",
        "Lidl",
        "owner-a",
        "a" * 64,
        now=start,
        lease_seconds=600,
    )

    assert collector.renew_store_claim(
        con,
        "2026-08-17",
        "Lidl",
        "owner-a",
        "a" * 64,
        now=start.replace(minute=9),
        lease_seconds=600,
    )
    assert collector.claim_store_collection(
        con,
        "2026-08-17",
        "Lidl",
        "owner-b",
        "a" * 64,
        now=start.replace(minute=11),
        lease_seconds=600,
    ) is False


def test_late_failure_cannot_overwrite_healthy_stage_from_new_lease_owner():
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.executescript(collector.SCHEMA)
    collector.migrate_offer_staging_schema(con)
    now = datetime(2026, 8, 19, 7, 0, tzinfo=timezone.utc)
    assert collector.claim_store_collection(
        con, "2026-08-17", "Lidl", "old", "a" * 64, now=now
    )
    assert collector.claim_store_collection(
        con, "2026-08-17", "Lidl", "new", "b" * 64, now=now.replace(hour=8)
    )
    offers = [valid_offer("lidl", index) for index in range(1, 21)]
    provenance = collector.CollectionProvenance(
        "official-lidl-viewer", "b" * 64, "2026-08-17", "2026-08-23"
    )
    collector.stage_store_collection(
        con, "2026-08-17", "Lidl", offers, owner="new", provenance=provenance
    )

    assert collector.record_stage_failure(
        con,
        "2026-08-17",
        "Lidl",
        "starý proces zlyhal",
        owner="old",
        attempted_provenance=collector.CollectionProvenance(
            "official-lidl-viewer", "a" * 64, "2026-08-17", "2026-08-23"
        ),
        structural=True,
    ) is False
    assert tuple(con.execute(
        "SELECT stav,source_fingerprint FROM zber_staging_stav"
    ).fetchone()) == ("ok", "b" * 64)


def test_structural_failure_is_suppressed_only_for_unchanged_manifest():
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.executescript(collector.SCHEMA)
    provenance = collector.CollectionProvenance(
        "official-lidl-viewer", "a" * 64, "2026-08-17", "2026-08-23"
    )
    collector.record_stage_failure(
        con,
        "2026-08-17",
        "Lidl",
        "nečitateľný layout",
        attempted_provenance=provenance,
        structural=True,
    )

    assert collector.unchanged_structural_failure(
        con, "2026-08-17", "Lidl", "a" * 64
    ) is True
    assert collector.unchanged_structural_failure(
        con, "2026-08-17", "Lidl", "b" * 64
    ) is False
    assert tuple(con.execute(
        "SELECT failure_kind,attempted_fingerprint FROM zber_staging_stav"
    ).fetchone()) == (None, None)


def test_structural_failure_is_retried_after_source_policy_approval(monkeypatch):
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.executescript(collector.SCHEMA)
    collector.migrate_offer_staging_schema(con)
    provenance = prepared_collection("lidl").provenance
    monkeypatch.setattr(collector.source_policy, "approved_source", lambda *_args: False)
    pending_identity = collector.structural_failure_identity("Lidl", provenance)
    collector.record_stage_failure(
        con,
        "2026-08-17",
        "Lidl",
        "zdroj čaká na schválenie",
        attempted_provenance=provenance,
        failure_identity=pending_identity,
        structural=True,
    )

    assert collector.unchanged_structural_failure(
        con,
        "2026-08-17",
        "Lidl",
        provenance.source_fingerprint,
        failure_identity=pending_identity,
    ) is True

    monkeypatch.setattr(collector.source_policy, "approved_source", lambda *_args: True)
    approved_identity = collector.structural_failure_identity("Lidl", provenance)
    assert approved_identity != pending_identity
    assert collector.unchanged_structural_failure(
        con,
        "2026-08-17",
        "Lidl",
        provenance.source_fingerprint,
        failure_identity=approved_identity,
    ) is False


def test_structural_failure_identity_tracks_deploy_and_full_source_policy(
    monkeypatch,
):
    provenance = prepared_collection("lidl").provenance
    monkeypatch.setattr(collector.source_policy, "approved_source", lambda *_args: True)
    monkeypatch.setenv("UVARSI_RELEASE_SHA", "a" * 40)

    original = collector.structural_failure_identity("Lidl", provenance)
    monkeypatch.setenv("UVARSI_RELEASE_SHA", "b" * 40)
    after_deploy = collector.structural_failure_identity("Lidl", provenance)

    registry = {
        key: dict(value) for key, value in collector.source_policy._REGISTRY.items()
    }
    registry[("Lidl", "official-lidl-viewer")]["reviewer"] = "new-reviewer"
    monkeypatch.setattr(collector.source_policy, "_REGISTRY", registry)
    after_policy_review = collector.structural_failure_identity("Lidl", provenance)

    assert after_deploy != original
    assert after_policy_review != after_deploy


def test_main_retries_same_manifest_immediately_after_policy_approval(
    monkeypatch, tmp_path,
):
    database = tmp_path / "uvarsi.db"
    approved = {"value": False}
    calls = []
    monkeypatch.setattr(collector, "DB", str(database))
    monkeypatch.setattr(collector, "monday", lambda: "2026-08-17")
    monkeypatch.setattr(collector, "business_day", lambda: date(2026, 8, 19))
    monkeypatch.setattr(collector, "STORES", ["lidl"])
    monkeypatch.setattr(collector, "prepare_store_collection", prepared_collection)
    monkeypatch.setattr(
        collector.source_policy,
        "approved_source",
        lambda *_args: approved["value"],
    )

    with pytest.raises(SystemExit, match="lidl"):
        collector.main(["lidl"])

    approved["value"] = True
    monkeypatch.setattr(collector, "load_key", lambda: "unused-test-value")
    monkeypatch.setitem(
        sys.modules,
        "anthropic",
        types.SimpleNamespace(Anthropic=lambda **_kwargs: object()),
    )

    def collect(_client, store, prepared=None):
        calls.append(store)
        return [valid_offer(store, index) for index in range(1, 21)]

    monkeypatch.setattr(collector, "zbieraj", collect)
    collector.main(["lidl"])

    assert calls == ["lidl"]


def test_official_manifest_ids_change_fingerprint_even_when_page_urls_stay_same(
    monkeypatch,
):
    overview = (
        '<a href="https://www.lidl.sk/l/sk/letak/'
        'online-letak-platny-od-17-08-2026/ar/1">Leták</a>'
    )
    payload = _official_lidl_payload(page_count=8)

    def get(url, **_kwargs):
        if url == collector.LIDL_OVERVIEW_URL:
            return types.SimpleNamespace(text=overview)
        return _json_response(payload)

    monkeypatch.setattr(collector.requests, "get", get)
    _pages, first = collector.official_lidl_pages(today=TODAY)
    payload["flyer"]["id"] = "01b-corrected-current-flyer"
    _pages, second = collector.official_lidl_pages(today=TODAY)

    assert first["source_identity"] == "lidl-flyer:01a-test-current-flyer"
    assert second["source_identity"] == "lidl-flyer:01b-corrected-current-flyer"
    assert collector.manifest_fingerprint(first) != collector.manifest_fingerprint(second)


def test_official_source_identity_cannot_hide_changed_page_bytes(monkeypatch):
    fixture = prepared_collection("lidl")
    revision = {"value": b"first"}
    monkeypatch.setattr(
        collector,
        "store_pages",
        lambda _store: (
            fixture.pages,
            json.loads(json.dumps(fixture.manifest)),
        ),
    )
    monkeypatch.setattr(
        collector,
        "get_image_bytes",
        lambda url: revision["value"] + b":" + url.encode("ascii"),
    )

    first = collector.prepare_store_collection("lidl")
    revision["value"] = b"corrected"
    second = collector.prepare_store_collection("lidl")

    assert first.manifest["source_identity"] == second.manifest["source_identity"]
    assert first.provenance.source_fingerprint != second.provenance.source_fingerprint


def test_content_fingerprint_failure_keeps_safe_partial_official_identity(monkeypatch):
    fixture = prepared_collection("lidl")
    monkeypatch.setattr(
        collector,
        "store_pages",
        lambda _store: (
            fixture.pages,
            json.loads(json.dumps(fixture.manifest)),
        ),
    )
    calls = {"count": 0}

    def fail_second_page(_url):
        calls["count"] += 1
        return b"first-page" if calls["count"] == 1 else None

    monkeypatch.setattr(collector, "get_image_bytes", fail_second_page)

    with pytest.raises(collector.ManifestPreparationError) as failure:
        collector.prepare_store_collection("lidl")

    attempted = failure.value.attempted_provenance
    assert attempted.collector_kind == "official-lidl-viewer"
    assert re.fullmatch(r"[0-9a-f]{64}", attempted.source_fingerprint)
    assert attempted.source_fingerprint != fixture.provenance.source_fingerprint


def test_lidl_reuses_disk_spooled_source_pages_during_paid_read(monkeypatch):
    from PIL import Image

    fixture = prepared_collection("lidl")
    image = BytesIO()
    Image.new("RGB", (24, 24), color=(250, 245, 230)).save(image, format="JPEG")
    downloads = []
    monkeypatch.setattr(
        collector,
        "store_pages",
        lambda _store: (
            fixture.pages,
            json.loads(json.dumps(fixture.manifest)),
        ),
    )

    def download(url):
        downloads.append(url)
        return image.getvalue()

    monkeypatch.setattr(collector, "get_image_bytes", download)
    prepared = collector.prepare_store_collection("lidl")
    monkeypatch.setattr(
        collector,
        "get_b64",
        lambda *_args, **_kwargs: pytest.fail(
            "paid Lidl read must reuse the already fingerprinted page bytes"
        ),
    )

    def claude_json(_client, model, _content, _max_tokens, effort=None):
        if model == collector.MODEL_SCAN:
            return [1] if any(
                block.get("text") == "Strana 1:"
                for block in _content
                if isinstance(block, dict)
            ) else []
        return [{
            "source_page": 1,
            "nazov": "Ryža",
            "kategoria": "trvanlive",
            "cena": 1.49,
            "povodna": None,
            "zlava": None,
            "jednotka": "kg",
            "cena_s_kartou": None,
            "zlava_s_kartou": None,
            "vernostny_program": None,
            "minimalny_nakup": None,
            "podmienka_s_kartou": None,
        }]

    monkeypatch.setattr(collector, "claude_json", claude_json)

    offers = collector.zbieraj(object(), "lidl", prepared)

    assert offers[0]["nazov"] == "Ryža"
    assert len(downloads) == len(fixture.pages)


def test_manifest_validation_failure_carries_a_safe_attempted_fingerprint(monkeypatch):
    prepared = prepared_collection("lidl")
    broken = json.loads(json.dumps(prepared.manifest))
    broken["pages"][0]["source_page"] = 2
    monkeypatch.setattr(
        collector, "store_pages", lambda _store: (prepared.pages, broken)
    )

    with pytest.raises(collector.ManifestPreparationError) as failure:
        collector.prepare_store_collection("lidl")

    attempted = failure.value.attempted_provenance
    assert attempted.collector_kind == "official-lidl-viewer"
    assert re.fullmatch(r"[0-9a-f]{64}", attempted.source_fingerprint)


def test_main_persists_attempted_fingerprint_from_manifest_preparation_failure(
    monkeypatch, tmp_path,
):
    database = tmp_path / "uvarsi.db"
    attempted = prepared_collection("lidl").provenance
    monkeypatch.setattr(collector, "DB", str(database))
    monkeypatch.setattr(collector, "monday", lambda: "2026-08-17")
    monkeypatch.setattr(collector, "business_day", lambda: date(2026, 8, 19))
    monkeypatch.setattr(collector, "STORES", ["lidl"])
    monkeypatch.setattr(
        collector,
        "prepare_store_collection",
        lambda _store: (_ for _ in ()).throw(
            collector.ManifestPreparationError("broken manifest", attempted)
        ),
    )

    with pytest.raises(SystemExit, match="lidl"):
        collector.main(["lidl"])

    con = sqlite3.connect(database)
    row = con.execute(
        "SELECT attempted_fingerprint,failure_identity "
        "FROM zber_staging_stav WHERE obchod='Lidl'"
    ).fetchone()
    con.close()
    assert row[0] == attempted.source_fingerprint
    assert re.fullmatch(r"[0-9a-f]{64}", row[1])


def test_main_bootstraps_exact_official_kaufland_without_aggregator_discovery_or_ai(
    monkeypatch, tmp_path,
):
    database = tmp_path / "uvarsi.db"
    calls = []
    monkeypatch.setattr(collector, "DB", str(database))
    monkeypatch.setattr(collector, "monday", lambda: "2026-08-17")
    monkeypatch.setattr(collector, "business_day", lambda: date(2026, 8, 19))
    monkeypatch.setattr(collector, "STORES", ["kaufland"])
    monkeypatch.setattr(collector.source_policy, "approved_source", lambda *_args: True)
    offers = [valid_offer("kaufland", index) for index in range(1, 21)]

    def official(today=None):
        calls.append("official")
        return offers

    def aggregator(_store):
        calls.append("aggregator")
        pytest.fail("Kupino fallback must run only after the official Kaufland path")

    monkeypatch.setattr(collector, "official_kaufland_offers", official)
    monkeypatch.setattr(collector, "prepare_store_collection", aggregator)
    monkeypatch.setattr(
        collector,
        "load_key",
        lambda: pytest.fail("official Kaufland facts must not call Anthropic"),
    )

    collector.main(["kaufland"])

    assert calls == ["official"]
    con = sqlite3.connect(database)
    assert con.execute(
        "SELECT COUNT(*) FROM akcie_staging WHERE obchod='Kaufland'"
    ).fetchone()[0] == 20
    assert con.execute("SELECT COUNT(*) FROM akcie").fetchone()[0] == 0
    con.close()


def test_main_reuses_exact_official_kaufland_stage_without_discovery_or_ai(
    monkeypatch, tmp_path,
):
    database = tmp_path / "uvarsi.db"
    monkeypatch.setattr(collector, "DB", str(database))
    monkeypatch.setattr(collector, "monday", lambda: "2026-08-17")
    monkeypatch.setattr(collector, "business_day", lambda: date(2026, 8, 19))
    monkeypatch.setattr(collector.source_policy, "approved_source", lambda *_args: True)
    offers = [valid_offer("kaufland", index) for index in range(1, 21)]
    con = collector.db()
    collector.stage_store_collection(con, "2026-08-17", "Kaufland", offers)
    con.close()
    monkeypatch.setattr(
        collector,
        "prepare_store_collection",
        lambda _store: pytest.fail("presný Kaufland staging nepotrebuje discovery"),
    )
    monkeypatch.setattr(
        collector,
        "load_key",
        lambda: pytest.fail("presný Kaufland staging nesmie volať Anthropic"),
    )

    collector.main(["kaufland"])
    monkeypatch.setattr(
        collector,
        "prepare_store_collection",
        lambda _store: pytest.fail("overený Kaufland nesmie ísť cez agregátor"),
    )
    monkeypatch.setattr(
        collector,
        "load_key",
        lambda: pytest.fail("overený Kaufland nesmie volať Anthropic"),
    )

    collector.main(["kaufland"])

    con = sqlite3.connect(database)
    assert con.execute(
        "SELECT COUNT(*) FROM akcie_staging WHERE obchod='Kaufland'"
    ).fetchone()[0] == 20
    con.close()


def test_bootstrap_refuses_to_relabel_legacy_url_fingerprint_as_current_manifest(
    monkeypatch,
):
    monkeypatch.setattr(collector.source_policy, "approved_source", lambda *_args: True)
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.executescript(collector.SCHEMA)
    collector.migrate_offer_staging_schema(con)
    offers = [valid_offer("lidl", index) for index in range(1, 21)]
    replace_store_week(con, "2026-08-17", "Lidl", offers)
    collector.record_store_outcome(
        con, "2026-08-17", "Lidl", "ok", 20, offers=offers
    )
    con.execute(
        "UPDATE zber_stav SET source_fingerprint=? WHERE obchod='Lidl'",
        (collector.source_policy.source_fingerprint(offers[0]["source_url"]),),
    )
    con.commit()
    expected = collector.CollectionProvenance(
        "official-lidl-viewer", "c" * 64, "2026-08-17", "2026-08-23"
    )

    assert collector.bootstrap_active_store_stage(
        con,
        "2026-08-17",
        "Lidl",
        today=date(2026, 8, 19),
        expected_provenance=expected,
    ) is False
    assert con.execute("SELECT COUNT(*) FROM akcie_staging").fetchone()[0] == 0


def test_main_claims_store_before_entering_paid_collection(monkeypatch, tmp_path):
    database = run_main_over_stores(monkeypatch, tmp_path, {"lidl": True})
    original_collect = collector.zbieraj

    def assert_claimed(client, store, prepared=None):
        con = sqlite3.connect(database)
        owner = con.execute(
            "SELECT owner FROM zber_claim WHERE tyzden=? AND obchod=?",
            ("2026-08-17", "Lidl"),
        ).fetchone()
        con.close()
        assert owner is not None
        return original_collect(client, store, prepared)

    monkeypatch.setattr(collector, "zbieraj", assert_claimed)
    collector.main(["lidl"])


def test_main_rechecks_exact_stage_after_claim_before_paid_collection(
    monkeypatch, tmp_path,
):
    database = run_main_over_stores(monkeypatch, tmp_path, {"lidl": True})
    original_claim = collector.claim_store_collection
    offers = [valid_offer("lidl", index) for index in range(1, 21)]
    provenance = prepared_collection("lidl").provenance

    def competing_collector_finished(
        con, week, store, owner, source_fingerprint, **kwargs,
    ):
        collector.stage_store_collection(
            con, week, store, offers, provenance=provenance
        )
        return original_claim(
            con, week, store, owner, source_fingerprint, **kwargs
        )

    monkeypatch.setattr(
        collector, "claim_store_collection", competing_collector_finished
    )
    monkeypatch.setattr(
        collector,
        "zbieraj",
        lambda *_args, **_kwargs: pytest.fail(
            "exact staged data found after claim must suppress paid collection"
        ),
    )

    collector.main(["lidl"])

    con = sqlite3.connect(database)
    assert con.execute(
        "SELECT COUNT(*) FROM akcie_staging WHERE obchod='Lidl'"
    ).fetchone()[0] == 20
    con.close()


def test_main_renews_store_lease_before_each_paid_model_request(
    monkeypatch, tmp_path,
):
    database = run_main_over_stores(monkeypatch, tmp_path, {"lidl": True})
    start = datetime(2026, 8, 19, 7, 0, tzinfo=timezone.utc)
    renewal = start.replace(minute=29)
    original_claim = collector.claim_store_collection
    original_renew = collector.renew_store_claim
    observed_expiries = []

    def fixed_claim(con, week, store, owner, fingerprint, **_kwargs):
        return original_claim(
            con, week, store, owner, fingerprint, now=start
        )

    def fixed_renew(con, week, store, owner, fingerprint, **_kwargs):
        return original_renew(
            con, week, store, owner, fingerprint, now=renewal
        )

    class RawMessages:
        def create(self, **_kwargs):
            con = sqlite3.connect(database)
            observed_expiries.append(con.execute(
                "SELECT expires_at FROM zber_claim "
                "WHERE tyzden='2026-08-17' AND obchod='Lidl'"
            ).fetchone()[0])
            con.close()
            return types.SimpleNamespace(usage=None)

    monkeypatch.setattr(collector, "claim_store_collection", fixed_claim)
    monkeypatch.setattr(collector, "renew_store_claim", fixed_renew)
    monkeypatch.setitem(
        sys.modules,
        "anthropic",
        types.SimpleNamespace(
            Anthropic=lambda **_kwargs: types.SimpleNamespace(messages=RawMessages())
        ),
    )

    def one_paid_call(client, store, prepared=None):
        client.messages.create(model=collector.MODEL_SCAN, max_tokens=1, messages=[])
        return [valid_offer(store, index) for index in range(1, 21)]

    monkeypatch.setattr(collector, "zbieraj", one_paid_call)

    collector.main(["lidl"])

    assert observed_expiries == ["2026-08-19T07:59:00+00:00"]


def test_main_claims_each_store_only_immediately_before_its_paid_collection(
    monkeypatch, tmp_path,
):
    database = run_main_over_stores(
        monkeypatch, tmp_path, {"tesco": True, "lidl": True}
    )
    calls = []

    def assert_only_current_store_is_claimed(_client, store, prepared=None):
        con = sqlite3.connect(database)
        claimed = [
            row[0] for row in con.execute(
                "SELECT obchod FROM zber_claim ORDER BY obchod"
            ).fetchall()
        ]
        con.close()
        assert claimed == [store.capitalize()]
        calls.append(store)
        return [valid_offer(store, index) for index in range(1, 21)]

    monkeypatch.setattr(collector, "zbieraj", assert_only_current_store_is_claimed)

    collector.main(["tesco", "lidl"])

    assert calls == ["tesco", "lidl"]


def test_unchanged_structural_manifest_skips_another_paid_read(monkeypatch, tmp_path):
    database = tmp_path / "uvarsi.db"
    monkeypatch.setattr(collector, "DB", str(database))
    monkeypatch.setattr(collector, "monday", lambda: "2026-08-17")
    monkeypatch.setattr(collector, "business_day", lambda: date(2026, 8, 19))
    monkeypatch.setattr(collector, "STORES", ["lidl"])
    monkeypatch.setattr(collector.source_policy, "approved_source", lambda *_args: True)
    monkeypatch.setattr(collector, "prepare_store_collection", prepared_collection)
    prepared = prepared_collection("lidl")
    con = collector.db()
    collector.record_stage_failure(
        con,
        "2026-08-17",
        "Lidl",
        "nezmenený chybný layout",
        attempted_provenance=prepared.provenance,
        structural=True,
    )
    con.close()
    monkeypatch.setattr(
        collector,
        "load_key",
        lambda: pytest.fail("nezmenený štrukturálny vstup nesmie míňať API"),
    )

    collector.main(["lidl"])


def test_bootstrapped_active_store_is_reused_by_main_without_anthropic(
    monkeypatch, tmp_path,
):
    database = tmp_path / "uvarsi.db"
    monkeypatch.setattr(collector, "DB", str(database))
    monkeypatch.setattr(collector, "monday", lambda: "2026-08-17")
    monkeypatch.setattr(collector, "business_day", lambda: date(2026, 8, 19))
    monkeypatch.setattr(collector, "STORES", ["lidl"])
    monkeypatch.setattr(collector.source_policy, "approved_source", lambda *_args: True)
    monkeypatch.setattr(collector, "prepare_store_collection", prepared_collection)
    offers = [valid_offer("lidl", index) for index in range(1, 21)]
    con = collector.db()
    replace_store_week(con, "2026-08-17", "Lidl", offers)
    collector.record_store_outcome(
        con,
        "2026-08-17",
        "Lidl",
        "ok",
        20,
        offers=offers,
        provenance=prepared_collection("lidl").provenance,
    )
    con.commit()
    con.close()
    monkeypatch.setattr(
        collector,
        "load_key",
        lambda: pytest.fail("overený aktívny zber sa má bootstrapnúť bez API"),
    )

    collector.main(["lidl"])

    con = sqlite3.connect(database)
    assert con.execute("SELECT COUNT(*) FROM akcie_staging").fetchone()[0] == 20
    con.close()
