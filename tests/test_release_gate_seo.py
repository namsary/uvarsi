import json

from nastroje import release_gate as gate


def response(status=200, body="", headers=None):
    payload = body if isinstance(body, bytes) else body.encode("utf-8")
    return gate.HttpResponse(
        status=status,
        body=payload,
        headers=headers or {},
        url="mock://test",
    )


def healthy_responses():
    sitemap = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://uvar.si/</loc></url>
  <url><loc>https://uvar.si/co-varit-tento-tyzden</loc><lastmod>2026-08-18</lastmod></url>
  <url><loc>https://uvar.si/lacny-jedalnicek</loc></url>
  <url><loc>https://uvar.si/ako-varime-z-akcii</loc></url>
</urlset>"""
    landing_json = {
        "week": "2026-08-24",
        "week_label": "24.–30. 8. 2026",
        "sources": [
            {
                "store": "Lidl",
                "valid_from": "2026-08-24",
                "valid_to": "2026-08-30",
            }
        ],
    }
    weekly_html = """
<!DOCTYPE html>
<html lang="sk"><body>
  <h1>Čo variť tento týždeň</h1>
  <p>24.–30. 8. 2026</p>
</body></html>
"""
    homepage = """
<!DOCTYPE html>
<html lang="sk"><head>
  <link rel="canonical" href="https://uvar.si/">
  <script type="application/ld+json">{"@context":"https://schema.org","@graph":[{"@type":"WebSite","url":"https://uvar.si/"}]}</script>
</head><body>
  <a href="/co-varit-tento-tyzden">Čo variť tento týždeň</a>
  <a href="/lacny-jedalnicek">Lacný jedálniček</a>
  <a href="/ako-varime-z-akcii">Ako varíme z akcií</a>
</body></html>
"""
    return {
        "/api/health": response(
            body=json.dumps(
                {"vydanie": "2026.08.25.1", "tyzden": "2026-08-24", "pocet": 42}
            )
        ),
        "/": response(body=homepage),
        "/app": response(headers={"X-Robots-Tag": "noindex, nofollow, noarchive"}),
        "/api/public/landing": response(body=json.dumps(landing_json)),
        "/prihlasenie": response(headers={"X-Robots-Tag": "noindex, nofollow, noarchive"}),
        "/robots.txt": response(
            body=(
                "User-agent: *\nAllow: /\nDisallow: /api/\n\n"
                "User-agent: OAI-SearchBot\nAllow: /\nDisallow: /api/\n\n"
                "Sitemap: https://uvar.si/sitemap.xml\n"
            )
        ),
        "/sitemap.xml": response(body=sitemap, headers={"Content-Type": "application/xml"}),
        "/co-varit-tento-tyzden": response(body=weekly_html),
        "/lacny-jedalnicek": response(body="<html><body><h1>Lacný jedálniček</h1></body></html>"),
        "/ako-varime-z-akcii": response(body="<html><body><h1>Ako varíme z akcií</h1></body></html>"),
        "/static/fonts/manrope-400-800.7101939e.woff2": response(
            headers={"Cache-Control": "public, max-age=31536000, immutable"}
        ),
        "https://www.uvar.si/co-varit-tento-tyzden": response(
            status=308,
            headers={"Location": "https://uvar.si/co-varit-tento-tyzden"},
        ),
    }


def findings_by_name(findings):
    return {item.nazov: item for item in findings}


def run_gate(monkeypatch, responses):
    calls = []

    def fake_fetch(path, timeout=25, follow_redirects=True):
        calls.append((path, follow_redirects))
        return responses[path]

    monkeypatch.setattr(gate, "_ziskaj", fake_fetch)
    monkeypatch.setattr(gate, "pondelok", lambda d=None: "2026-08-24")
    return gate.brana_produkcia("2026.08.25.1"), calls


def test_release_gate_passes_seo_contract_and_checks_www_without_following_redirects(monkeypatch):
    findings, calls = run_gate(monkeypatch, healthy_responses())
    by_name = findings_by_name(findings)

    for name in (
        "robots.txt",
        "robots.txt OAI-SearchBot",
        "robots.txt blokuje /api/",
        "robots.txt sitemap",
        "sitemap.xml",
        "sitemap.xml XML",
        "sitemap obsahuje https://uvar.si/",
        "sitemap obsahuje https://uvar.si/co-varit-tento-tyzden",
        "sitemap obsahuje https://uvar.si/lacny-jedalnicek",
        "sitemap obsahuje https://uvar.si/ako-varime-z-akcii",
        "SEO /co-varit-tento-tyzden",
        "SEO /lacny-jedalnicek",
        "SEO /ako-varime-z-akcii",
        "týždenný SEO signál",
        "/app noindex",
        "/prihlasenie noindex",
        "font immutable cache",
        "www weekly redirect",
        "landing canonical",
        "landing JSON-LD",
        "landing interné odkazy",
    ):
        assert by_name[name].ok, name

    assert ("https://www.uvar.si/co-varit-tento-tyzden", False) in calls


def test_release_gate_blocks_on_robots_requirements(monkeypatch):
    responses = healthy_responses()
    responses["/robots.txt"] = response(body="User-agent: *\nAllow: /\n")

    findings, _ = run_gate(monkeypatch, responses)
    by_name = findings_by_name(findings)

    assert by_name["robots.txt OAI-SearchBot"].ok is False
    assert by_name["robots.txt blokuje /api/"].ok is False
    assert by_name["robots.txt sitemap"].ok is False
    assert by_name["robots.txt sitemap"].blokuje is True


def test_release_gate_blocks_on_invalid_sitemap_and_missing_canonical_urls(monkeypatch):
    responses = healthy_responses()
    responses["/sitemap.xml"] = response(body="<urlset><url><loc>https://uvar.si/</loc></url>")

    findings, _ = run_gate(monkeypatch, responses)
    by_name = findings_by_name(findings)

    assert by_name["sitemap.xml XML"].ok is False
    assert by_name["sitemap obsahuje https://uvar.si/co-varit-tento-tyzden"].ok is False
    assert by_name["sitemap obsahuje https://uvar.si/lacny-jedalnicek"].ok is False
    assert by_name["sitemap obsahuje https://uvar.si/ako-varime-z-akcii"].ok is False


def test_release_gate_blocks_on_missing_week_signal_and_private_noindex_headers(monkeypatch):
    responses = healthy_responses()
    responses["/co-varit-tento-tyzden"] = response(body="<html><body>bez rozsahu</body></html>")
    responses["/app"] = response(headers={"X-Robots-Tag": "index, follow"})
    responses["/prihlasenie"] = response(headers={})

    findings, _ = run_gate(monkeypatch, responses)
    by_name = findings_by_name(findings)

    assert by_name["týždenný SEO signál"].ok is False
    assert "24.–30. 8. 2026" in by_name["týždenný SEO signál"].detail
    assert by_name["/app noindex"].ok is False
    assert by_name["/prihlasenie noindex"].ok is False


def test_release_gate_blocks_on_font_cache_redirect_and_homepage_metadata(monkeypatch):
    responses = healthy_responses()
    responses["/static/fonts/manrope-400-800.7101939e.woff2"] = response(
        headers={"Cache-Control": "public, max-age=300"}
    )
    responses["https://www.uvar.si/co-varit-tento-tyzden"] = response(
        status=302,
        headers={"Location": "https://www.uvar.si/co-varit-tento-tyzden"},
    )
    responses["/"] = response(
        body=(
            '<html><head><link rel="canonical" href="https://www.uvar.si/">'
            '<script type="application/ld+json">{oops}</script></head><body>'
            '<a href="/co-varit-tento-tyzden">Čo variť tento týždeň</a></body></html>'
        )
    )

    findings, _ = run_gate(monkeypatch, responses)
    by_name = findings_by_name(findings)

    assert by_name["font immutable cache"].ok is False
    assert by_name["www weekly redirect"].ok is False
    assert by_name["landing canonical"].ok is False
    assert by_name["landing JSON-LD"].ok is False
    assert by_name["landing interné odkazy"].ok is False
