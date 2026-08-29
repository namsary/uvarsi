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


COMMUNITY_DOM_STUB = """
function makeElement(tag) {
  var ownText = '';
  return {tag: tag, className: '', attributes: {}, style: {}, children: [],
    get textContent() { return ownText; },
    set textContent(value) { ownText = String(value); this.children = []; },
    append: function () {
      ownText = '';
      for (var i = 0; i < arguments.length; i++) this.children.push(arguments[i]);
    },
    replaceChildren: function () {
      ownText = '';
      this.children = Array.prototype.slice.call(arguments);
    },
    setAttribute: function (name, value) { this.attributes[name] = String(value); }};
}
var document = {createElement: makeElement};
function textOf(element) {
  return [element.textContent].concat((element.children || []).map(textOf)).filter(Boolean).join('');
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


@needs_node
def test_community_counter_is_truthful_progressive_and_capped(tmp_path):
    html = index_html()
    helpers = COMMUNITY_DOM_STUB + "\n".join([
        nested(html, "function node(tag, className, text)"),
        nested(html, "function renderCommunity(c)"),
    ])
    result = run_node(
        tmp_path,
        "landing-community-counter.js",
        helpers
        + """
var fallback = 'Prvých 250 získa zakladajúcu cenu';
function freshCounter() {
  communityCounter = makeElement('div');
  communityCounter.textContent = fallback;
}

freshCounter();
renderCommunity({visible: true, accounts: 9, goal: 250});
if (textOf(communityCounter) !== fallback || communityCounter.children.length) process.exit(1);

freshCounter();
renderCommunity({visible: true, accounts: 10, goal: 250});
var tenLabel = 'Testovacia komunita: 10 z cieľa 250 účtov';
var tenBar = communityCounter.children[1];
if (communityCounter.children[0].textContent !== tenLabel) process.exit(2);
if (tenBar.attributes['aria-valuetext'] !== tenLabel) process.exit(3);
if (tenBar.attributes.role !== 'progressbar') process.exit(4);
if (tenBar.attributes['aria-valuemin'] !== '0') process.exit(5);
if (tenBar.attributes['aria-valuemax'] !== '250') process.exit(6);
if (tenBar.attributes['aria-valuenow'] !== '10') process.exit(7);
if (tenBar.children[0].style.width !== '4%') process.exit(8);

freshCounter();
renderCommunity({visible: true, accounts: 251, goal: 250});
var overLabel = 'Testovacia komunita: 251 z cieľa 250 účtov';
var overBar = communityCounter.children[1];
if (communityCounter.children[0].textContent !== overLabel) process.exit(9);
if (overBar.children[0].style.width !== '100%') process.exit(10);
if (overBar.attributes['aria-valuenow'] !== '250') process.exit(11);
if (overBar.attributes['aria-valuetext'] !== overLabel) process.exit(12);

[null, {}, {visible: true, accounts: '10', goal: 250},
 {visible: true, accounts: 10.5, goal: 250},
 {visible: true, accounts: 10, goal: '250'}].forEach(function (community) {
  freshCounter();
  renderCommunity(community);
  if (textOf(communityCounter) !== fallback || communityCounter.children.length) process.exit(13);
});
process.exit(0);
""",
    )

    assert result.returncode == 0, result.stdout + result.stderr


@needs_node
def test_failed_landing_fetch_keeps_counter_fallback_and_uses_no_second_request(tmp_path):
    html = index_html()
    result = run_node(
        tmp_path,
        "landing-community-fetch-failure.js",
        COMMUNITY_DOM_STUB
        + nested(html, "function loadLanding()")
        + """
var fallback = 'Prvých 250 získa zakladajúcu cenu';
var communityCounter = makeElement('div');
communityCounter.textContent = fallback;
var landing = {hidden: false};
var model = {hidden: false};
var status = {hidden: true, textContent: ''};
var calls = [];
function fetch(url) { calls.push(url); return Promise.reject(new Error('offline')); }
function sourcesAreCurrent() { throw new Error('must not inspect failed payload'); }
function render() { throw new Error('must not render failed payload'); }
function renderModel() { throw new Error('must not render failed payload'); }
function renderCommunity() { throw new Error('must not render failed payload'); }

loadLanding().then(function () {
  if (calls.length !== 1 || calls[0] !== '/api/public/landing') process.exit(1);
  if (textOf(communityCounter) !== fallback || communityCounter.children.length) process.exit(2);
  process.exit(0);
}).catch(function (error) {
  console.error(error);
  process.exit(3);
});
""",
    )

    assert result.returncode == 0, result.stdout + result.stderr
