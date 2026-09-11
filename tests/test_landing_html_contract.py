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


def test_landing_footer_links_every_customer_document_and_contact():
    html = index_html()

    for href in (
        "/vop", "/ochrana-osobnych-udajov", "/cookies", "/odstupenie",
        "/reklamacie", "mailto:pumaragency@gmail.com",
    ):
        assert f'href="{href}"' in html
    assert "uvedieme v konečnom checkoute" not in html


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
def test_receipt_proof_shows_week_without_publishing_source_details(tmp_path):
    html = index_html()
    helpers = DOM_STUB + "\n".join([
        nested(html, "function node(tag, className, text)"),
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
if (text.indexOf('Kaufland') !== -1 || text.indexOf('Lidl') !== -1) process.exit(2);
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


@needs_node
def test_historical_receipt_is_rendered_with_a_warning_and_without_current_saving_claims(
    tmp_path,
):
    html = index_html()
    helpers = COMMUNITY_DOM_STUB + "\n".join([
        nested(html, "function node(tag, className, text)"),
        nested(html, "function amount(value)"),
        nested(html, "function proofNode(data)"),
        nested(html, "function render(data)"),
    ])
    result = run_node(
        tmp_path,
        "landing-historical-receipt.js",
        helpers
        + """
var landing = makeElement('div'); landing.hidden = true;
var status = makeElement('p'); status.hidden = false;
render({
  state: 'historical_example',
  notice: 'Uk\u00e1\u017eka z minul\u00e9ho t\u00fd\u017ed\u0148a \u2013 ceny u\u017e nemusia plati\u0165.',
  receipt: {meals: [{day: 'PO', name: 'Cestoviny', items: [
    {name: 'Cestoviny', store: 'Tesco hypermarket', price: '1,29'}
  ]}], nakup_spolu: '1,29'}
});
var text = textOf(landing);
if (landing.hidden !== false || status.hidden !== true) process.exit(1);
if (text.indexOf('Uk\u00e1\u017eka z minul\u00e9ho t\u00fd\u017ed\u0148a') === -1) process.exit(2);
if (text.indexOf('N\u00e1kup v uk\u00e1\u017eke') === -1) process.exit(3);
['Aktu\u00e1lne let\u00e1ky', 'U\u0161etr\u00ed\u0161', 'Be\u017ene by st\u00e1l', 'undefined'].forEach(function (claim) {
  if (text.indexOf(claim) !== -1) process.exit(4);
});
process.exit(0);
""",
    )

    assert result.returncode == 0, result.stdout + result.stderr


@needs_node
def test_landing_loader_accepts_server_validated_historical_state_without_current_sources(
    tmp_path,
):
    html = index_html()
    result = run_node(
        tmp_path,
        "landing-historical-load.js",
        COMMUNITY_DOM_STUB
        + nested(html, "function loadLanding()")
        + """
var payload = {state:'historical_example', receipt:{meals:[{items:[]}]}};
var landing = {hidden:true}, model = {hidden:true};
var status = {hidden:false, textContent:''};
var rendered = false, communityRendered = false;
function fetch() { return Promise.resolve({ok:true, json:function(){return Promise.resolve(payload);}}); }
function sourcesAreCurrent() { throw new Error('historical state must not claim current sources'); }
function render(data) { if (data === payload) rendered = true; }
function renderModel() { throw new Error('historical receipt must not power the savings model'); }
function renderCommunity() { communityRendered = true; }
loadLanding().then(function () {
  if (!rendered || !communityRendered) process.exit(1);
  process.exit(0);
}).catch(function (error) { console.error(error); process.exit(2); });
""",
    )

    assert result.returncode == 0, result.stdout + result.stderr


def test_landing_keeps_its_current_title_and_description():
    html = index_html()

    assert "<title>Uvar.si — z letáka rovno na tanier</title>" in html
    assert (
        '<meta name="description" content="Uvar.si spojí aktuálne akcie z Lidla, Kauflandu a Tesca '
        's tým, čo máš doma. Dostaneš jedálniček, recepty a nákupný zoznam na celý týždeň.">'
    ) in html


def test_premium_is_a_nonbinding_email_interest_action_not_checkout():
    html = index_html()
    section = html.split('<section class="plans-band"', 1)[1].split("</section>", 1)[0]
    premium_marker = '<div class="plan-name">Premium</div>'
    assert premium_marker in section
    premium = section.split(premium_marker, 1)[1]

    action = re.search(
        r'<button[^>]+class="[^"]*js-plan[^"]*"[^>]+data-plan="([^"]+)"[^>]*>'
        r'Chcem vedieť o spustení</button>',
        premium,
    )
    assert action
    assert action.group(1) == "Premium (49 € / rok po spustení)"
    assert 'type="button"' in action.group(0)
    assert "checkout" not in premium.lower()
    assert 'href=' not in action.group(0)
    assert '<input type="hidden" name="fields[plan]" id="planField"' in html
    assert "planField.value=plan" in html.replace(" ", "")


def test_waitlist_consent_names_operator_scope_privacy_and_unsubscribe():
    html = index_html()
    modal = html.split('<div class="modal" id="modal"', 1)[1].split(
        '<iframe name="ml_sink"', 1
    )[0]

    assert "PUMAR s. r. o." in modal
    assert "spustení Uvar.si" in modal
    assert "zakladajúcej ponuke" in modal
    assert 'href="/ochrana-osobnych-udajov"' in modal
    assert "odhlásiť" in modal
    assert "odvolať súhlas" in modal
    assert re.search(r'<input[^>]+id="waitlist-consent"[^>]+required', modal)
    assert "všeobecný marketing" not in modal.casefold()


def test_founding_and_premium_share_the_same_core_functionality():
    html = index_html()
    section = html.split('<section class="plans-band"', 1)[1].split("</section>", 1)[0]
    premium_marker = '<div class="plan-name">Premium</div>'
    assert premium_marker in section
    founding = section.split('<div class="plan-name">Zakladajúci</div>', 1)[1]
    founding = founding.split(premium_marker, 1)[0]
    premium = section.split(premium_marker, 1)[1]

    founding_features = re.findall(r"<li>(.*?)</li>", founding)
    premium_features = re.findall(r"<li>(.*?)</li>", premium)
    assert founding_features == premium_features == [
        "Všetky podporované obchody",
        "Celý týždeň, recepty a špajza",
        "Budúce aktualizácie",
    ]
    assert "39 € raz. Premium bez predplatného počas prevádzky služby Uvar.si." in founding
    assert "cena natrvalo" not in founding.casefold()
    assert "premium natrvalo" not in founding.casefold()
    assert "/ rok" in premium


@needs_node
def test_community_counter_is_truthful_progressive_accessible_and_capped(tmp_path):
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
var fallback = '50 zakladajúcich miest za 39 € jednorazovo';
function freshCounter() {
  communityCounter = makeElement('div');
  communityCounter.textContent = fallback;
}

freshCounter();
renderCommunity({visible: false, founders: 0, goal: 50});
if (textOf(communityCounter) !== fallback || communityCounter.children.length) process.exit(1);

freshCounter();
renderCommunity({visible: true, founders: 1, goal: 50});
var tenLabel = 'Obsadené: 1 z 50 zakladajúcich miest';
var tenLabelNode = communityCounter.children[0];
var tenBar = communityCounter.children[1];
if (tenLabelNode.textContent !== tenLabel) process.exit(2);
if (tenBar.attributes['aria-valuetext'] !== tenLabel) process.exit(3);
if (tenBar.attributes.role !== 'progressbar') process.exit(4);
if (tenBar.attributes['aria-valuemin'] !== '0') process.exit(5);
if (tenBar.attributes['aria-valuemax'] !== '50') process.exit(6);
if (tenBar.attributes['aria-valuenow'] !== '1') process.exit(7);
if (tenBar.children[0].style.width !== '2%') process.exit(8);
if (!tenLabelNode.id || tenBar.attributes['aria-labelledby'] !== tenLabelNode.id) process.exit(14);

freshCounter();
renderCommunity({visible: true, founders: 51, goal: 50});
var overLabel = 'Obsadené: 50 z 50 zakladajúcich miest';
var overLabelNode = communityCounter.children[0];
var overBar = communityCounter.children[1];
if (overLabelNode.textContent !== overLabel) process.exit(9);
if (overBar.children[0].style.width !== '100%') process.exit(10);
if (overBar.attributes['aria-valuenow'] !== '50') process.exit(11);
if (overBar.attributes['aria-valuetext'] !== overLabel) process.exit(12);
if (!overLabelNode.id || overBar.attributes['aria-labelledby'] !== overLabelNode.id) process.exit(15);

[null, {}, {visible: true, founders: '1', goal: 50},
 {visible: true, founders: 1.5, goal: 50},
 {visible: true, founders: 1, goal: '50'}].forEach(function (community) {
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
var fallback = '50 zakladajúcich miest za 39 € jednorazovo';
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
