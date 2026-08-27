import json
import re
import xml.etree.ElementTree as ET
from datetime import date

import pytest

from app.landing_data import validate_landing_data
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


def sparse_payload():
    data = payload()
    data["sources"][0].pop("valid_from")
    first_item = data["receipt"]["meals"][0]["items"][0]
    first_item.pop("unit")
    first_item.pop("price")
    first_item["original_price"] = None
    first_item["savings"] = None
    data["receipt"].update(nakup_spolu="0,89", bezne="0,89", usetris="0,00", polozky_s_beznou_cenou=0)
    return data


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


def visible_text(html):
    without_structured_data = without_json_ld(html)
    without_tags = re.sub(r"<[^>]+>", " ", without_structured_data)
    return re.sub(r"\s+", " ", without_tags).strip()


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
    dangerous["receipt"]["meals"][0]["items"][0]["store"] = 'Lidl & spol.'

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


def test_weekly_page_fails_closed_for_sparse_validator_accepted_payload():
    sparse = sparse_payload()

    assert validate_landing_data(sparse, date(2026, 8, 18)) is sparse

    page = render_weekly_page(sparse, today=date(2026, 8, 18))

    assert page.indexable is False
    assert page.last_modified is None
    assert 'content="noindex,follow"' in page.html
    assert "Týždenné ceny práve overujeme" in page.html
    assert "0,89 €" not in page.html
    assert "17.–23. 8. 2026" not in page.html


def test_weekly_page_uses_shared_intersection_for_mixed_source_validity_windows():
    mixed = payload()
    mixed["sources"][1]["valid_from"] = "2026-08-19"
    mixed["sources"][1]["valid_to"] = "2026-08-21"

    page = render_weekly_page(mixed, today=date(2026, 8, 20))

    assert "Platnosť cien: 19. 8. 2026 - 21. 8. 2026" in page.html
    assert "Platnosť cien: 17. 8. 2026 - 23. 8. 2026" not in page.html
    assert "Lidl: 17. 8. 2026 - 23. 8. 2026, strana 2" in page.html
    assert "Tesco: 19. 8. 2026 - 21. 8. 2026, strana 4" in page.html


@pytest.mark.parametrize(
    "url",
    [None, "", "/letak/lidl", "ftp://letak.test/lidl", "https:///bez-hosta"],
)
def test_weekly_page_fails_closed_when_a_cited_source_url_is_missing_or_invalid(url):
    invalid = payload()
    invalid["sources"][0]["url"] = url

    page = render_weekly_page(invalid, today=date(2026, 8, 18))

    assert page.indexable is False
    assert "Týždenné ceny práve overujeme" in page.html
    assert "1,49 €" not in page.html
    assert "https://letak.test/tesco" not in page.html


def test_weekly_page_fails_closed_when_an_item_store_has_no_validated_source_store():
    mismatched = payload()
    mismatched["receipt"]["meals"][0]["items"][0]["store"] = "Kaufland"

    page = render_weekly_page(mismatched, today=date(2026, 8, 18))

    assert page.indexable is False
    assert "Týždenné ceny práve overujeme" in page.html
    assert "Kaufland" not in without_json_ld(page.html)
    assert "1,49 €" not in page.html


@pytest.mark.parametrize(
    ("valid_from", "valid_to"),
    [
        ("2026-08-19", "2026-08-23"),
        ("2026-08-10", "2026-08-17"),
    ],
)
def test_weekly_page_fails_closed_outside_each_source_validity_window(valid_from, valid_to):
    invalid = payload()
    for source in invalid["sources"]:
        source["valid_from"] = valid_from
        source["valid_to"] = valid_to

    page = render_weekly_page(invalid, today=date(2026, 8, 18))

    assert page.indexable is False
    assert "Týždenné ceny práve overujeme" in page.html
    assert "1,49 €" not in page.html
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


def test_budget_evergreen_page_explains_batch_portions_leftovers_and_pantry_planning():
    page = render_evergreen_page("lacny-jedalnicek")
    text = visible_text(page.html)
    folded = text.casefold()

    assert "Varenie na viac dní" in text
    assert "počet porcií" in folded
    assert "domácnosť" in folded
    assert "zvyšné porcie" in folded
    assert "špajze" in folded
    assert "odpočítajú z nákupného zoznamu" in folded
    assert "nepridávajú novú cenu" in folded
    assert 'href="https://uvar.si/co-varit-tento-tyzden"' in page.html
    assert 'href="https://uvar.si/app"' in page.html
    assert "garant" not in folded


def test_method_evergreen_page_names_coverage_validation_and_fail_closed_boundary():
    page = render_evergreen_page("ako-varime-z-akcii")
    text = visible_text(page.html)

    assert "Lidl, Kaufland a Tesco" in text
    assert "Fresh momentálne nepokrývame" in text
    assert all(term in text for term in ("URL zdroja", "obchod", "cenu", "rozsah platnosti"))
    assert "AI skladá jedlá a návrhy receptov" in text
    assert "programové kontroly" in text
    assert all(term in text for term in ("zdroj", "dátumy", "ceny", "matematiku"))
    assert "nemusí zahŕňať každú ponuku ani každý produkt" in text
    assert "nezobrazíme nič ako aktuálne" in text
    assert 'href="https://uvar.si/co-varit-tento-tyzden"' in page.html
    assert 'href="https://uvar.si/app"' in page.html
    assert "garant" not in text.casefold()


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
