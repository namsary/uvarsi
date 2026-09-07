import re

import pytest
from fastapi.testclient import TestClient

from app.legal_pages import LEGAL_SLUGS, legal_text, render_legal_page
from app.operator_profile import LEGAL_VERSION
from test_server import load_server


@pytest.mark.parametrize("slug", sorted(LEGAL_SLUGS))
def test_legal_page_is_complete_versioned_and_indexable(slug):
    html = render_legal_page(slug)

    assert html.startswith("<!DOCTYPE html>")
    assert html.count("<h1") == 1
    assert "PUMAR s. r. o." in html
    assert "57 370 591" in html
    assert LEGAL_VERSION in html
    assert f'<link rel="canonical" href="https://uvar.si/{slug}">' in html
    assert f'href="/pravne/{slug}.txt"' in html
    assert "noindex" not in html
    assert "[DOPLNIŤ" not in html
    assert "[DOPLNIT" not in html
    assert "TODO" not in html
    assert "magic link pri každom prihlásení" not in html.casefold()


def test_terms_describe_exact_founder_offer_and_full_refund_policy():
    text = legal_text("vop")

    assert "39 €" in text
    assert "jednorazov" in text.casefold()
    assert "bez automatickej obnovy" in text.casefold()
    assert "50" in text
    assert "14 dní" in text


def test_privacy_and_cookie_text_match_the_current_product():
    privacy = legal_text("ochrana-osobnych-udajov").casefold()
    cookies = legal_text("cookies").casefold()

    assert "hesl" in privacy and "passkey" in privacy
    assert "spracovaní leták" in privacy
    assert "recepty negeneruje" in privacy
    assert "export" in privacy and "výmaz" in privacy
    assert "uvarsi_session" in cookies
    assert "uvarsi_setup" in cookies
    assert "90 dní" in cookies
    assert "analytické ani reklamné cookies" in cookies


def test_plain_text_has_no_html_markup_or_placeholders():
    text = legal_text("odstupenie")

    assert not re.search(r"<[a-z][^>]*>", text, re.I)
    assert "PUMAR s. r. o." in text
    assert "pumaragency@gmail.com" in text
    assert "[DOPLNIŤ" not in text


def test_unknown_legal_slug_is_rejected():
    with pytest.raises(KeyError):
        render_legal_page("neznamy-dokument")
    with pytest.raises(KeyError):
        legal_text("neznamy-dokument")


def test_server_publishes_html_and_downloadable_text_routes(monkeypatch, tmp_path):
    server = load_server(monkeypatch, tmp_path, [])
    client = TestClient(server.app)

    for slug in sorted(LEGAL_SLUGS):
        page = client.get(f"/{slug}")
        text = client.get(f"/pravne/{slug}.txt")
        assert page.status_code == 200
        assert page.headers["content-type"].startswith("text/html")
        assert "charset=utf-8" in page.headers["content-type"].casefold()
        assert page.text.count("<h1") == 1
        assert text.status_code == 200
        assert text.headers["content-type"].startswith("text/plain")
        assert "PUMAR s. r. o." in text.text
        assert page.headers.get("x-robots-tag") is None

    missing = client.get("/pravne/neznamy-dokument.txt")
    assert missing.status_code == 404
    assert "noindex" in missing.headers["x-robots-tag"]
