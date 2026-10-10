import types

import pytest

from app import zdrojove_podklady as podklady


LIDL_MANIFEST = {
    "collector_kind": "official-lidl-viewer",
    "source_url": "https://www.lidl.sk/l/sk/letak/online-letak-platny-od-05-10-2026/view/flyer/page/1",
    "source_identity": "lidl-flyer:01a0fc8c",
}


@pytest.fixture(autouse=True)
def hints_on(monkeypatch):
    monkeypatch.setenv("UVARSI_SOURCE_HINTS", "1")


def _json(payload):
    return types.SimpleNamespace(status_code=200, json=lambda: payload)


def _lidl_flyer(flyer_id="01a0fc8c"):
    return {"flyer": {
        "id": flyer_id,
        "pdfUrl": "https://assets.leaflets.schwarz/leaflets/pdfs/x/Letak.pdf",
        "pages": [
            {"number": 1, "altText": "Syry, pečivo a údeniny.",
             "keyWords": "Eidam Gouda Leerdammer Plátky Bryndza"},
            {"number": 2, "altText": "Posteľná bielizeň.",
             "keyWords": "Obliečky Pyžamo Bavlna"},
        ],
    }}


def test_keyword_overlap_ignores_diacritics_and_case():
    assert podklady.keyword_overlap("Bryndza Gouda", "GOUDA 150 g, bryndza") == 1.0
    assert podklady.keyword_overlap("Eidam Gouda", "Obliečky 2 ks") == 0.0
    assert podklady.keyword_overlap("", "cokolvek") == 0.0


def test_lidl_hints_use_pdf_text_only_when_it_matches_the_same_page(monkeypatch):
    monkeypatch.setattr(podklady.requests, "get", lambda *_a, **_k: _json(_lidl_flyer()))
    monkeypatch.setattr(podklady, "_download_pdf", lambda _url, directory: f"{directory}/l.pdf")
    monkeypatch.setattr(podklady, "pdf_page_texts", lambda _path: [
        "-20% 1.19 Eidam 150 g\n1.29 Gouda plátky\n3.45 Leerdammer plátky\nLiptov Bryndza 1.65",
        "Eidam Gouda Leerdammer — text inej strany",  # nesedí s kľúčovými slovami strany 2
    ])

    hints = podklady.lidl_page_hints(LIDL_MANIFEST, log=lambda *_: None)

    assert "Text z oficiálneho PDF" in hints[1]
    assert "1.29 Gouda" in hints[1]
    assert hints[2] == "Popis strany od Lidla: Posteľná bielizeň."


def test_lidl_hints_refuse_a_different_flyer(monkeypatch):
    monkeypatch.setattr(podklady.requests, "get", lambda *_a, **_k: _json(_lidl_flyer("iny")))
    monkeypatch.setattr(podklady, "_download_pdf", lambda *_a: pytest.fail("PDF iného letáku"))

    assert podklady.lidl_page_hints(LIDL_MANIFEST, log=lambda *_: None) == {}


def test_lidl_pdf_url_must_be_the_official_asset_host():
    assert podklady._safe_pdf_url("https://assets.leaflets.schwarz/a/b.pdf")
    assert podklady._safe_pdf_url("https://evil.example/a/b.pdf") is None
    assert podklady._safe_pdf_url("http://assets.leaflets.schwarz/a/b.pdf") is None


TESCO_MANIFEST = {
    "collector_kind": "official-tesco-viewer",
    "leaflet_format": "HM",
    "valid_from": "2026-10-07",
    "valid_to": "2026-10-13",
    "declared_pages": 2,
}


def _tesco_item(valid_from="2026-10-07T06:00:00.000Z", pages=2):
    page = {"positions": [{"products": [
        {"product": {"productName": "Pomaranče voľné ", "measure": "KG"}},
        {"product": {"productName": "Tesco citróny 500 g", "measure": "KG"}},
    ]}]}
    return {"id": 728, "validFrom": valid_from, "validTo": "2026-10-13T21:59:59.000Z",
            "pages": [page] + [{"positions": []}] * (pages - 1)}


def test_tesco_hints_list_products_of_the_matching_leaflet(monkeypatch):
    payload = {"data": {"leaflets": {"items": [_tesco_item(), _tesco_item("2026-09-30T06:00:00.000Z")]}}}
    monkeypatch.setattr(podklady.requests, "post", lambda *_a, **_k: _json(payload))

    hints = podklady.tesco_page_hints(TESCO_MANIFEST, log=lambda *_: None)

    assert list(hints) == [1]
    assert "Pomaranče voľné; Tesco citróny 500 g" in hints[1]


def test_tesco_hints_skip_ambiguous_or_different_page_count(monkeypatch):
    for items in ([_tesco_item(), _tesco_item()], [_tesco_item(pages=3)]):
        payload = {"data": {"leaflets": {"items": items}}}
        monkeypatch.setattr(podklady.requests, "post", lambda *_a, _p=payload, **_k: _json(_p))
        assert podklady.tesco_page_hints(TESCO_MANIFEST, log=lambda *_: None) == {}


def test_page_hints_never_raise_and_respect_the_switch(monkeypatch):
    def broken(*_a, **_k):
        raise ConnectionError("down")

    monkeypatch.setattr(podklady.requests, "get", broken)
    assert podklady.page_hints("lidl", LIDL_MANIFEST, log=lambda *_: None) == {}

    monkeypatch.setenv("UVARSI_SOURCE_HINTS", "0")
    monkeypatch.setattr(podklady, "lidl_page_hints", lambda *_a, **_k: pytest.fail("vypnuté"))
    assert podklady.page_hints("lidl", LIDL_MANIFEST) == {}
