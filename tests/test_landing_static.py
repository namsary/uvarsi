from datetime import date
from pathlib import Path

from app.landing_static import publish_landing_html


TODAY = date(2026, 9, 12)


def current_payload():
    return {
        "schema_version": 1,
        "offer_data_version": 2,
        "generated_at": "2026-09-12T12:20:43+02:00",
        "week": "2026-09-07",
        "week_label": "7.–13. 9. 2026",
        "sources": [{
            "store": "Lidl",
            "url": "https://example.test/lidl",
            "valid_from": "2026-09-07",
            "valid_to": "2026-09-13",
        }],
        "receipt": {
            "meals": [{
                "day": "PO",
                "name": "Paradajkové cestoviny",
                "items": [{
                    "offer_key": "offer-1",
                    "name": "Paradajky",
                    "store": "Lidl",
                    "unit": "1 kg",
                    "quantity": 1,
                    "price": "1,00",
                    "original_price": "1,50",
                    "savings": "0,50",
                    "off": "-33 %",
                }],
            }],
            "nakup_spolu": "1,00",
            "bezne": "1,50",
            "usetris": "0,50",
        },
    }


def blank_index(path: Path):
    path.write_text(
        '<div class="rcpt-wrap" id="landing-data" aria-live="polite" hidden>'
        '<!-- RCPT:START --><!-- RCPT:END --></div>'
        '<p class="rcpt-proof" id="landing-status" aria-live="polite">'
        'Aktuálne ceny práve obnovujeme.</p>',
        encoding="utf-8",
    )


def test_current_receipt_is_atomically_prerendered_for_people_and_crawlers(tmp_path):
    index = tmp_path / "index.html"
    blank_index(index)

    state = publish_landing_html(index, current_payload(), today=TODAY)
    html = index.read_text(encoding="utf-8")

    assert state == "current"
    assert 'id="landing-data" aria-live="polite">' in html
    assert 'id="landing-data" aria-live="polite" hidden>' not in html
    assert 'id="landing-status" aria-live="polite" hidden>' in html
    assert "Paradajkové cestoviny" in html
    assert "Paradajky · Lidl" in html
    assert "7.–13. 9. 2026" in html
    assert "Ušetríš" in html and "0,50 €" in html
    assert not index.with_suffix(".tmp").exists()


def test_expired_receipt_becomes_an_explicit_historical_example(tmp_path):
    index = tmp_path / "index.html"
    blank_index(index)

    state = publish_landing_html(
        index, current_payload(), today=date(2026, 9, 14)
    )
    html = index.read_text(encoding="utf-8")

    assert state == "historical_example"
    assert "Ukážka z minulého týždňa" in html
    assert "Ušetríš" not in html
    assert "-33 %" not in html


def test_prerender_escapes_all_flyer_text(tmp_path):
    index = tmp_path / "index.html"
    blank_index(index)
    payload = current_payload()
    payload["receipt"]["meals"][0]["name"] = '<script>alert("x")</script>'
    payload["receipt"]["meals"][0]["items"][0]["name"] = "Mlieko <b>lacné</b>"

    publish_landing_html(index, payload, today=TODAY)
    html = index.read_text(encoding="utf-8")

    assert '<script>alert("x")</script>' not in html
    assert "&lt;script&gt;alert(&quot;x&quot;)&lt;/script&gt;" in html
    assert "Mlieko &lt;b&gt;lacné&lt;/b&gt;" in html


def test_invalid_payload_hides_any_old_static_prices(tmp_path):
    index = tmp_path / "index.html"
    blank_index(index)
    publish_landing_html(index, current_payload(), today=TODAY)

    state = publish_landing_html(index, {"broken": True}, today=TODAY)
    html = index.read_text(encoding="utf-8")

    assert state == "unavailable"
    assert "Paradajky" not in html
    assert '<!-- RCPT:START --><!-- RCPT:END -->' in html
    assert 'id="landing-data" aria-live="polite" hidden>' in html
    assert 'id="landing-status" aria-live="polite">' in html
