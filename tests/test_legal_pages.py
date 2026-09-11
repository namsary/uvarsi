import re

import pytest
from fastapi.testclient import TestClient

from app.legal_pages import LEGAL_SLUGS, legal_text, render_legal_page
from app.operator_profile import LEGAL_VERSION
from test_server import load_server


FOUNDER_PROMISE = "39 € raz. Premium bez predplatného počas prevádzky služby Uvar.si."


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

    assert FOUNDER_PROMISE in text
    assert "bez automatickej obnovy" in text.casefold()
    assert "50" in text
    assert "14 dní" in text
    assert "cena natrvalo" not in text.casefold()
    assert "premium natrvalo" not in text.casefold()


def test_terms_define_service_duration_termination_and_preserve_statutory_remedies():
    text = legal_text("vop").casefold()

    assert "počas prevádzky služby uvar.si" in text
    assert "ukončiť prevádzku" in text
    assert "v predstihu" in text
    assert "trvanlivom médiu" in text
    assert "ak je to vzhľadom na dôvod ukončenia možné" in text
    assert "zákonné práva" in text
    assert "ukončením prevádzky nezanikajú" in text


def test_terms_name_the_checkout_seller_operator_and_paid_activation_remedy():
    text = legal_text("vop").casefold()

    assert "lemon squeezy" in text
    assert "merchant of record" in text
    assert "predávajúci" in text
    assert "pumar s. r. o." in text
    assert "prevádzkovateľ" in text
    assert "potvrdením objednávky" in text
    assert "aktivuje" in text
    assert "platba prebehne" in text
    assert "ručne aktivujeme" in text
    assert "úplné vrátenie platby" in text
    assert "zaplatiť znova" in text


def test_terms_give_materially_disadvantaged_users_the_statutory_change_rights():
    text = legal_text("vop").casefold()

    assert "podstatnej nepriaznivej zmene" in text
    assert "trvanlivom médiu" in text
    assert "bezplatne ukončiť zmluvu" in text
    assert "30 dní" in text
    assert "nezmenenú verziu" in text
    assert "bez dodatočných nákladov" in text


def test_withdrawal_deadline_and_optional_template_are_complete():
    text = legal_text("odstupenie").casefold()

    assert "lehota je zachovaná" in text
    assert "odošlete pred uplynutím" in text
    assert "použitie vzoru nie je povinné" in text
    for field in (
        "adresát",
        "meno a priezvisko spotrebiteľa",
        "adresa spotrebiteľa",
        "dátum objednávky",
        "dátum odoslania",
        "podpis spotrebiteľa (iba ak",
    ):
        assert field in text


def test_complaints_cover_the_service_period_and_written_rejection_reasons():
    vop = legal_text("vop").casefold()
    complaints = legal_text("reklamacie").casefold()

    assert "počas celej dohodnutej doby poskytovania služby" in vop
    assert "počas celej dohodnutej doby poskytovania služby" in complaints
    assert "písomné dôvody" in complaints
    assert "zamiet" in complaints


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


def test_privacy_states_required_data_consequences_and_real_transfer_safeguards():
    privacy = legal_text("ochrana-osobnych-udajov").casefold()

    assert "povinné údaje" in privacy
    assert "bez e-mailu" in privacy
    assert "bez údajov potrebných pre objednávku" in privacy
    assert "premium nemožno kúpiť" in privacy
    assert "štandardné zmluvné doložky" in privacy
    for provider in ("anthropic", "resend", "mailerlite"):
        assert provider in privacy
    assert "napríklad rozhodnutie o primeranosti" not in privacy


def test_waitlist_privacy_is_limited_in_scope_retention_and_unsubscribe():
    privacy = legal_text("ochrana-osobnych-udajov").casefold()

    assert "spustení uvar.si" in privacy
    assert "zakladajúcej ponuke" in privacy
    assert "neobmedzený marketing" in privacy
    assert "12 mesiacov od prihlásenia" in privacy
    assert "30 dní po poslednej požadovanej správe" in privacy
    assert "odhlásenie" in privacy
    assert "odvolať súhlas" in privacy


def test_every_browser_storage_key_has_a_real_lifetime_or_deletion_criterion():
    cookies = legal_text("cookies").casefold()

    assert "uvarsi_session" in cookies and "najviac 90 dní" in cookies
    assert "uvarsi_setup" in cookies and "najviac jednu hodinu" in cookies
    assert "uvarsi_profil" in cookies and "do odhlásenia" in cookies
    assert "uvarsi_done:*" in cookies and "automaticky neexpirujú" in cookies
    assert "uvarsi_kupim:*" in cookies and "vymazania účtu" in cookies


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
