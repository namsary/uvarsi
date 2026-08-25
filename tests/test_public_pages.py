import json
import re
import xml.etree.ElementTree as ET
from datetime import date

import pytest

from app.public_pages import ROBOTS_TXT, render_evergreen_page, render_sitemap, render_weekly_page


def payload():
    return {
        "schema_version": 1,
        "generated_at": "2026-08-18T05:02:20+02:00",
        "week": "2026-08-17",
        "week_label": "17.–23. 8. 2026",
        "sources": [
            {
                "store": "Lidl",
                "url": "https://letak.test/lidl?ref=\"akcia\"",
                "source_page": 2,
                "valid_from": "2026-08-17",
                "valid_to": "2026-08-23",
            },
            {
                "store": "Tesco",
                "url": "https://letak.test/tesco",
                "source_page": 4,
                "valid_from": "2026-08-17",
                "valid_to": "2026-08-23",
            },
        ],
        "receipt": {
            "meals": [
                {
                    "day": "PO",
                    "name": "Cestoviny s paradajkami",
                    "instructions": ["Uvar cestoviny."],
                    "items": [
                        {
                            "offer_key": "offer_a",
                            "name": "Paradajky",
                            "store": "Lidl",
                            "unit": "500 g",
                            "quantity": 1,
                            "price": "1,49",
                            "original_price": "2,19",
                            "savings": "0,70",
                            "off": "-31 %",
                        },
                        {
                            "offer_key": "offer_b",
                            "name": "Cestoviny",
                            "store": "Tesco",
                            "unit": "400 g",
                            "quantity": 1,
                            "price": "0,89",
                            "original_price": None,
                            "savings": None,
                            "off": "",
                        },
                    ],
                    "recipe": {"min": 20, "steps_total": 2, "steps": ["Uvar.", "Podávaj."]},
                }
            ],
            "nakup_spolu": "2,38",
            "bezne": "3,08",
            "usetris": "0,70",
            "polozky": 2,
            "polozky_s_beznou_cenou": 1,
        },
    }


def title_of(html):
    match = re.search(r"<title>(.*?)</title>", html, re.S)
    assert match
    return match.group(1)


def meta_content(html, *, name=None, property_name=None):
    key = name or property_name
    attr = "name" if name else "property"
    match = re.search(
        rf'<meta\s+{attr}="{re.escape(key)}"\s+content="([^"]*)"',
        html,
    )
    assert match, key
    return match.group(1)


def json_ld(html):
    match = re.search(r'<script type="application/ld\+json">(.*?)</script>', html, re.S)
    assert match
    return json.loads(match.group(1))


def without_json_ld(html):
    return re.sub(r'<script type="application/ld\+json">.*?</script>', "", html, flags=re.S)


def test_weekly_page_renders_validated_current_data_with_metadata_and_structured_data():
    page = render_weekly_page(payload(), today=date(2026, 8, 18))

    assert page.indexable is True
    assert page.last_modified == date(2026, 8, 18)
    assert page.html.startswith("<!DOCTYPE html>")
    assert '<html lang="sk">' in page.html
    assert page.html.count("<h1") == 1
    assert title_of(page.html) == "Čo variť tento týždeň z akcií | Uvar.si"
    assert meta_content(page.html, name="description").startswith("Aktuálny týždenný jedálniček")
    assert '<link rel="canonical" href="https://uvar.si/co-varit-tento-tyzden">' in page.html
    assert meta_content(page.html, property_name="og:title") == title_of(page.html)
    assert meta_content(page.html, property_name="og:url") == "https://uvar.si/co-varit-tento-tyzden"
    assert meta_content(page.html, name="twitter:card") == "summary"
    assert 'href="https://uvar.si/lacny-jedalnicek"' in page.html
    assert 'href="https://uvar.si/ako-varime-z-akcii"' in page.html
    assert "17.–23. 8. 2026" in page.html
    assert "Cestoviny s paradajkami" in page.html
    assert "Paradajky" in page.html
    assert "Cestoviny" in page.html
    assert "1,49 €" in page.html
    assert "2,19 €" in page.html
    assert "0,89 €" in page.html
    assert "strana 2" in page.html
    assert "Aktualizované: 18. 8. 2026 05:02" in page.html
    assert "Ako pracujeme s AI a dátami" in page.html
    assert "Otvor aplikáciu Uvar.si" in page.html
    assert "https://letak.test/lidl?ref=&quot;akcia&quot;" in page.html
    assert "Pôvodná cena: 2,19 €" in page.html
    assert "Pôvodná cena: 0,89 €" not in page.html

    structured = json_ld(page.html)
    assert [entry["@type"] for entry in structured] == ["Article", "BreadcrumbList"]
    assert structured[0]["dateModified"] == "2026-08-18T05:02:20+02:00"
    assert structured[0]["mainEntityOfPage"] == "https://uvar.si/co-varit-tento-tyzden"
    assert structured[1]["itemListElement"][-1]["item"] == "https://uvar.si/co-varit-tento-tyzden"


def test_weekly_page_escapes_payload_strings_and_serializes_json_ld_safely():
    dangerous = payload()
    dangerous["week_label"] = '<img src=x onerror="alert(1)">'
    dangerous["receipt"]["meals"][0]["name"] = 'Polievka </script><script>alert("x")</script>'
    dangerous["receipt"]["meals"][0]["items"][0]["name"] = '<b>Paradajky</b>'
    dangerous["sources"][0]["store"] = 'Lidl & spol.'

    page = render_weekly_page(dangerous, today=date(2026, 8, 18))
    visible_html = without_json_ld(page.html)

    assert "<img src=x" not in visible_html
    assert "<b>Paradajky</b>" not in visible_html
    assert "&lt;b&gt;Paradajky&lt;/b&gt;" in visible_html
    assert "<\\/script><script>" in page.html
    assert "&lt;img src=x onerror=&quot;alert(1)&quot;&gt;" in visible_html
    assert "Lidl &amp; spol." in visible_html


def test_weekly_page_fails_closed_when_data_is_missing_invalid_or_stale():
    stale = render_weekly_page(payload(), today=date(2026, 8, 25))
    missing = render_weekly_page(None, today=date(2026, 8, 25))

    for page in (stale, missing):
        assert page.indexable is False
        assert page.last_modified is None
        assert page.html.count("<h1") == 1
        assert 'content="noindex,follow"' in page.html
        assert "Týždenné ceny práve overujeme" in page.html
        assert "1,49 €" not in page.html
        assert "2,19 €" not in page.html
        assert "https://letak.test/" not in page.html


@pytest.mark.parametrize(
    ("slug", "title"),
    [
        ("lacny-jedalnicek", "Lacný jedálniček bez vymyslených zliav | Uvar.si"),
        ("ako-varime-z-akcii", "Ako varíme z akcií bez klamlivých tvrdení | Uvar.si"),
    ],
)
def test_evergreen_pages_are_indexable_structured_and_price_stable(slug, title):
    page = render_evergreen_page(slug)

    assert page.indexable is True
    assert page.last_modified is None
    assert page.html.count("<h1") == 1
    assert title_of(page.html) == title
    assert meta_content(page.html, property_name="og:title") == title
    assert meta_content(page.html, name="twitter:card") == "summary"
    assert "Priamy záver" in page.html
    assert "Praktický postup" in page.html
    assert 'href="https://uvar.si/co-varit-tento-tyzden"' in page.html
    assert "€" not in page.html

    structured = json_ld(page.html)
    assert [entry["@type"] for entry in structured] == ["Article", "BreadcrumbList"]
    assert structured[0]["mainEntityOfPage"] == f"https://uvar.si/{slug}"


def test_evergreen_pages_reject_unknown_slugs():
    with pytest.raises(KeyError):
        render_evergreen_page("neznamy-slug")


def test_sitemap_contains_only_exact_public_urls_and_optional_weekly_lastmod():
    xml = render_sitemap(date(2026, 8, 25), weekly_modified=date(2026, 8, 18))
    root = ET.fromstring(xml)
    namespace = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}

    urls = root.findall("sm:url", namespace)
    locs = [node.findtext("sm:loc", namespaces=namespace) for node in urls]
    lastmods = {node.findtext("sm:loc", namespaces=namespace): node.findtext("sm:lastmod", namespaces=namespace) for node in urls}

    assert locs == [
        "https://uvar.si/",
        "https://uvar.si/co-varit-tento-tyzden",
        "https://uvar.si/lacny-jedalnicek",
        "https://uvar.si/ako-varime-z-akcii",
    ]
    assert lastmods["https://uvar.si/co-varit-tento-tyzden"] == "2026-08-18"
    assert lastmods["https://uvar.si/"] is None
    assert lastmods["https://uvar.si/lacny-jedalnicek"] is None
    assert lastmods["https://uvar.si/ako-varime-z-akcii"] is None

    xml_without_weekly = render_sitemap(date(2026, 8, 25), weekly_modified=None)
    assert "<lastmod>" not in xml_without_weekly


def test_robots_txt_allows_public_crawling_and_names_the_sitemap():
    assert "User-agent: *" in ROBOTS_TXT
    assert "User-agent: OAI-SearchBot" in ROBOTS_TXT
    assert "Disallow: /api/" in ROBOTS_TXT
    assert "Disallow: /app" not in ROBOTS_TXT
    assert "Disallow: /prihlasenie" not in ROBOTS_TXT
    assert "Sitemap: https://uvar.si/sitemap.xml" in ROBOTS_TXT
