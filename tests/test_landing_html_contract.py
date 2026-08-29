"""Landing ukazuje pôvod cien bez odkazov na nestabilné cudzie agregátory."""
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


_BUNDLED_NODE = Path(sys.executable).resolve().parent.parent / "node" / "bin" / "node.exe"
NODE = shutil.which("node") or (str(_BUNDLED_NODE) if _BUNDLED_NODE.exists() else None)
needs_node = pytest.mark.skipif(NODE is None, reason="node runtime is not available")


def index_html():
    return Path("index.html").read_text(encoding="utf-8")


def nested(html, signature):
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
"""


@needs_node
def test_receipt_proof_names_each_store_once_without_publishing_source_urls(tmp_path):
    html = index_html()
    helpers = DOM_STUB + "\n".join([
        nested(html, "function node(tag, className, text)"),
        nested(html, "function sourceStores(sources)"),
        nested(html, "function proofNode(data)"),
    ])
    result = run_node(
        tmp_path,
        "landing-proof-contract.js",
        helpers
        + """
var proof = proofNode({
  week_label: '17.\u201323. 8. 2026',
  sources: [
    {store: 'Kaufland', url: 'https://letak.test/kaufland/32'},
    {store: 'Lidl', url: 'https://letak.test/lidl/11'},
    {store: 'Kaufland', url: 'https://letak.test/kaufland/33'}
  ]
});
var text = textOf(proof);
if (text.indexOf('17.\u201323. 8. 2026') === -1) process.exit(1);
if ((text.match(/Kaufland/g) || []).length !== 1) process.exit(2);
if ((text.match(/Lidl/g) || []).length !== 1) process.exit(3);
if (text.indexOf('letak.test') !== -1) process.exit(4);
process.exit(0);
""",
    )

    assert result.returncode == 0, result.stdout + result.stderr


def test_landing_does_not_publish_breakable_external_leaflet_links():
    html = index_html()
    render = nested(html, "function render(data)")

    assert "sourcesNode" not in html
    assert "sourceUrl" not in html
    assert "rcpt-src-link" not in html
    assert "proofNode(data)" in render
    assert "landing.replaceChildren" in render


def test_landing_claims_only_that_prices_come_from_current_leaflets():
    html = index_html()
    proof = nested(html, "function proofNode(data)")

    assert "aktuálne letáky" in proof.lower()
    assert "over" in proof.lower()
    for overstated in (
        "overili sme u",
        "nezávisle overené",
        "garantujeme",
        "potvrdené obchodom",
        "overené priamo v obchode",
    ):
        assert overstated not in html


def test_landing_keeps_its_current_title_and_description():
    html = index_html()

    assert "<title>Uvar.si — z letáka rovno na tanier</title>" in html
    assert (
        '<meta name="description" content="Uvar.si spojí aktuálne akcie z Lidla, Kauflandu a Tesca '
        's tým, čo máš doma. Dostaneš jedálniček, recepty a nákupný zoznam na celý týždeň.">'
    ) in html
