import io
import json

import pytest

from nastroje import release_gate as gate


WEEKLY_PATH = "/co-varit-tento-tyzden"
ALTERNATE_REDIRECTS = {
    "https://www.uvar.si": "www.uvar.si weekly redirect",
    "https://uvarsi.sk": "uvarsi.sk weekly redirect",
    "https://www.uvarsi.sk": "www.uvarsi.sk weekly redirect",
    "https://uvarsi.89.167.72.159.sslip.io": "uvarsi.89.167.72.159.sslip.io weekly redirect",
}


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
    responses = {
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
    }
    for index, host in enumerate(ALTERNATE_REDIRECTS):
        responses[f"{host}{WEEKLY_PATH}"] = response(
            status=301 if index % 2 == 0 else 308,
            headers={"Location": f"https://uvar.si{WEEKLY_PATH}"},
        )
    return responses


def findings_by_name(findings):
    return {item.nazov: item for item in findings}


def test_git_gate_ignores_untracked_workspace_notes(monkeypatch):
    commands = []

    def fake_run(command, timeout=None):
        commands.append(command)
        if command[:3] == ["git", "rev-parse", "HEAD"]:
            return 0, "a" * 40
        if command[:3] == ["git", "status", "--porcelain"]:
            return 0, ""
        if command[:3] == ["git", "rev-parse", "origin/main"]:
            return 1, ""
        raise AssertionError(command)

    monkeypatch.setattr(gate, "_spusti", fake_run)

    findings = findings_by_name(gate.brana_git())

    assert findings["nezapisane zmeny"].ok is True
    status_command = next(command for command in commands if command[:2] == ["git", "status"])
    assert "--untracked-files=no" in status_command


def run_gate(monkeypatch, responses):
    calls = []

    def fake_fetch(
        path,
        timeout=25,
        follow_redirects=True,
        max_body_bytes=gate.BODY_LIMIT,
    ):
        calls.append((path, follow_redirects, max_body_bytes))
        return responses[path]

    monkeypatch.setattr(gate, "_ziskaj", fake_fetch)
    monkeypatch.setattr(gate, "pondelok", lambda d=None: "2026-08-24")
    return gate.brana_produkcia("2026.08.25.1"), calls


def test_release_gate_passes_seo_contract_and_checks_every_alternate_host_without_following_redirects(monkeypatch):
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
        *ALTERNATE_REDIRECTS.values(),
        "landing canonical",
        "landing JSON-LD",
        "landing interné odkazy",
    ):
        assert by_name[name].ok, name

    for host in ALTERNATE_REDIRECTS:
        assert (f"{host}{WEEKLY_PATH}", False, gate.BODY_LIMIT) in calls


def test_release_gate_reads_the_complete_real_homepage_with_a_bounded_override(monkeypatch):
    homepage = (gate.KOREN / "index.html").read_bytes()
    read_sizes = []

    class FakeResponse(io.BytesIO):
        status = 200
        headers = {}

        def read(self, size=-1):
            read_sizes.append(size)
            return super().read(size)

    class FakeOpener:
        def open(self, request, timeout):
            return FakeResponse(homepage)

    monkeypatch.setattr(gate.urllib.request, "build_opener", lambda *args: FakeOpener())

    fetched = gate._ziskaj("/", max_body_bytes=64 * 1024)

    assert len(homepage) > gate.BODY_LIMIT
    assert fetched.body == homepage
    assert read_sizes == [64 * 1024]
    assert gate._canonical_links(fetched.text()) == ["https://uvar.si/"]
    assert all(gate._has_internal_link(fetched.text(), path) for path in gate.CONTENT_LINKS)


def test_release_gate_requests_a_larger_but_bounded_homepage_body(monkeypatch):
    _, calls = run_gate(monkeypatch, healthy_responses())

    homepage_calls = [call for call in calls if call[0] == "/"]
    assert len(homepage_calls) == 1
    assert gate.BODY_LIMIT < homepage_calls[0][2] <= 128 * 1024


def test_homepage_internal_link_parser_accepts_single_quotes_but_ignores_hidden_links():
    path = "/lacny-jedalnicek"

    assert gate._has_internal_link(f"<a href='{path}'>Jedálniček</a>", path)
    assert not gate._has_internal_link(f'<a hidden href="{path}">Skrytý</a>', path)
    assert not gate._has_internal_link(
        f'<a href="{path}" aria-hidden="true">Skrytý</a>',
        path,
    )


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
    assert by_name["www.uvar.si weekly redirect"].ok is False
    assert by_name["landing canonical"].ok is False
    assert by_name["landing JSON-LD"].ok is False
    assert by_name["landing interné odkazy"].ok is False


@pytest.mark.parametrize(("host", "finding_name"), ALTERNATE_REDIRECTS.items())
def test_release_gate_blocks_each_alternate_host_independently(
    monkeypatch,
    host,
    finding_name,
):
    responses = healthy_responses()
    responses[f"{host}{WEEKLY_PATH}"] = response(
        status=302,
        headers={"Location": f"{host}{WEEKLY_PATH}"},
    )

    findings, _ = run_gate(monkeypatch, responses)
    by_name = findings_by_name(findings)

    assert by_name[finding_name].ok is False
    assert by_name[finding_name].blokuje is True
    for other_name in set(ALTERNATE_REDIRECTS.values()) - {finding_name}:
        assert by_name[other_name].ok is True


def test_release_gate_accepts_canonical_link_when_href_precedes_rel(monkeypatch):
    responses = healthy_responses()
    responses["/"] = response(
        body=(
            '<html><head><link href="https://uvar.si/" rel="canonical">'
            '<script type="application/ld+json">{"@context":"https://schema.org"}</script>'
            "</head><body>"
            '<a href="/co-varit-tento-tyzden">Čo variť tento týždeň</a>'
            '<a href="/lacny-jedalnicek">Lacný jedálniček</a>'
            '<a href="/ako-varime-z-akcii">Ako varíme z akcií</a>'
            "</body></html>"
        )
    )

    findings, _ = run_gate(monkeypatch, responses)
    by_name = findings_by_name(findings)

    assert by_name["landing canonical"].ok is True


def test_release_gate_accepts_canonical_link_with_mixed_case_and_whitespace(monkeypatch):
    responses = healthy_responses()
    responses["/"] = response(
        body=(
            "<html><head>"
            '<LINK   HREF = "https://uvar.si/"   REL = "Canonical"   >'
            '<script type="application/ld+json">{"@context":"https://schema.org"}</script>'
            "</head><body>"
            '<a href="/co-varit-tento-tyzden">Čo variť tento týždeň</a>'
            '<a href="/lacny-jedalnicek">Lacný jedálniček</a>'
            '<a href="/ako-varime-z-akcii">Ako varíme z akcií</a>'
            "</body></html>"
        )
    )

    findings, _ = run_gate(monkeypatch, responses)
    by_name = findings_by_name(findings)

    assert by_name["landing canonical"].ok is True
