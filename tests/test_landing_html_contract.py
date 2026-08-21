"""Bloček na landingu musí ukázať, z ktorých letákov ceny pochádzajú."""
import re
import shutil
import subprocess
from pathlib import Path

import pytest


NODE = shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="node runtime is not available")


def index_html():
    return Path("index.html").read_text(encoding="utf-8")


def nested(html, signature):
    """Return a whole function declared inside the landing IIFE (two-space indent)."""
    match = re.search(re.escape(signature) + r"\{.*?\n  \}", html, re.S)
    assert match, "landing must declare " + signature.strip()
    return match.group(0)


def run_node(tmp_path, name, source):
    script = tmp_path / name
    script.write_text(source, encoding="utf-8")
    return subprocess.run([NODE, str(script)], capture_output=True, text=True)


DOM_STUB = """
var document = {createElement: function (tag) {
  return {tag: tag, className: '', textContent: '', children: [],
    append: function () {
      for (var i = 0; i < arguments.length; i++) this.children.push(arguments[i]);
    }};
}};
function flatten(element, collected) {
  if (!element) return collected;
  collected.push(element);
  (element.children || []).forEach(function (child) { flatten(child, collected); });
  return collected;
}
function textOf(element) {
  return flatten(element, []).map(function (n) { return n.textContent || ''; }).join(' | ');
}
function linksOf(element) {
  return flatten(element, []).filter(function (n) { return n.href; });
}
"""


def sources_helpers(html):
    return DOM_STUB + "\n".join([
        nested(html, "function node(tag, className, text)"),
        nested(html, "function dayMonth(iso)"),
        nested(html, "function dayMonthYear(iso)"),
        nested(html, "function sourceUrl(url)"),
        nested(html, "function sourceLabel(source)"),
        nested(html, "function sourcesNode(sources)"),
    ])


@needs_node
def test_landing_receipt_lists_the_leaflets_its_prices_were_read_from(tmp_path):
    html = index_html()
    result = run_node(
        tmp_path,
        "landing-sources-contract.js",
        sources_helpers(html)
        + """
var box = sourcesNode([
  {store: 'Kaufland', url: 'https://letak.test/kaufland/32', valid_from: '2026-08-17', valid_to: '2026-08-23'},
  {store: 'Lidl', url: 'https://letak.test/lidl/11', valid_from: '2026-08-20', valid_to: '2026-08-26'}
]);
var text = textOf(box);
if (text.indexOf('Kaufland') === -1) process.exit(1);
if (text.indexOf('Lidl') === -1) process.exit(2);
if (text.indexOf('17. 8.') === -1) process.exit(3);
if (text.indexOf('23. 8. 2026') === -1) process.exit(4);
if (text.indexOf('undefined') !== -1) process.exit(5);
var links = linksOf(box);
if (links.length !== 2) process.exit(6);
if (links[0].href !== 'https://letak.test/kaufland/32') process.exit(7);
if (links[0].target !== '_blank') process.exit(8);
if (String(links[0].rel).indexOf('noopener') === -1) process.exit(9);
process.exit(0);
""",
    )

    assert result.returncode == 0, result.stdout + result.stderr


@needs_node
def test_landing_sources_never_link_a_non_http_url_and_vanish_when_absent(tmp_path):
    html = index_html()
    result = run_node(
        tmp_path,
        "landing-sources-safety.js",
        sources_helpers(html)
        + """
if (sourceUrl('javascript:alert(1)') !== '') process.exit(1);
if (sourceUrl('https://letak.test/x') !== 'https://letak.test/x') process.exit(2);
var box = sourcesNode([
  {store: 'Kaufland', url: 'https://letak.test/kaufland/32', valid_from: '2026-08-17', valid_to: '2026-08-23'},
  {store: 'Lidl', url: 'javascript:alert(1)', valid_from: '2026-08-17', valid_to: '2026-08-23'}
]);
if (linksOf(box).length !== 1) process.exit(3);
if (textOf(box).indexOf('Lidl') === -1) process.exit(4);
if (sourcesNode([]) !== null) process.exit(5);
if (sourcesNode(null) !== null) process.exit(6);
process.exit(0);
""",
    )

    assert result.returncode == 0, result.stdout + result.stderr


def test_landing_render_actually_puts_the_sources_under_the_receipt():
    html = index_html()
    render = nested(html, "function render(data)")

    assert "sourcesNode(data.sources)" in render, "receipt_data already ships sources — use them"
    assert "landing.replaceChildren" in render
    assert re.search(r"landing\.append\(\s*sources\s*\)", render), "sources belong under the receipt"


def test_landing_claims_only_that_prices_were_read_from_a_named_leaflet():
    html = index_html()
    render = nested(html, "function render(data)")

    assert "skontroluj" in render.lower(), "the public claim must stay actionable"
    for overstated in ("overili sme u", "nezávisle overené", "garantujeme", "potvrdené obchodom",
                       "overené priamo v obchode"):
        assert overstated not in html, "reading a leaflet page is not retailer verification"
