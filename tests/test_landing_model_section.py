"""Modelový príklad smie ukázať len doložené čísla z aktuálneho týždňa.

Sekcia tvrdí konkrétnu úsporu a projekciu na rok. Nedoložené tvrdenie o úspore
je podľa smernice 2005/29/ES klamlivá obchodná praktika, takže tu nejde o štýl,
ale o právo: číslo musí pochádzať z overených ponúk aktuálneho týždňa, alebo
sa sekcia nesmie vykresliť vôbec.
"""
import re
import shutil
import subprocess
from datetime import date
from pathlib import Path

import pytest

from app.landing_data import model_example_is_publishable


NODE = shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="node runtime is not available")

# „8,20 €", „2,69 €/kg", „−33 %" — čokoľvek, čo čitateľ prečíta ako cenu či zľavu.
PRICE_LIKE = re.compile(r"\d+[,.]\d{1,2}\s*€|\d+\s*€|[−–-]\s*\d+\s*%")


def index_html():
    return Path("index.html").read_text(encoding="utf-8")


def model_section(html=None):
    html = html if html is not None else index_html()
    match = re.search(r'<section[^>]*id="landing-model".*?</section>', html, re.S)
    assert match, "landing musí mať sekciu modelového príkladu"
    return match.group(0)


def nested(html, signature):
    """Vytiahni funkciu deklarovanú vnútri landing IIFE (odsadenie dve medzery)."""
    match = re.search(re.escape(signature) + r"\{.*?\n  \}", html, re.S)
    assert match, "landing musí deklarovať " + signature.strip()
    return match.group(0)


# ------------------------------------------------------------------ HTML
def test_model_section_ships_no_price_like_string_at_all():
    """Ani za `hidden` nesmie v zdrojáku ostať vymyslená cena či zľava."""
    found = PRICE_LIKE.findall(model_section())

    assert not found, f"v modelovom príklade ostali cenové reťazce: {found}"


def test_model_section_states_no_frozen_annual_or_weekly_saving():
    section = model_section()

    for frozen in ("430", "8,20", "36 €", "za rok približne", "Ak takto varíš celý rok"):
        assert frozen not in section, f"zamrznuté číslo {frozen!r} v modelovom príklade"


def test_model_section_body_is_empty_and_hidden_until_data_arrives():
    html = index_html()

    assert 'id="landing-model" hidden' in html
    body = re.search(r'id="landing-model-body"[^>]*>(.*?)</div>', model_section(html), re.S)
    assert body, "sekcia musí mať prázdny kontajner, ktorý plní renderModel()"
    assert not body.group(1).strip(), "šablóna nesmie niesť žiadny predvyplnený obsah"


def test_removing_the_hidden_attribute_alone_publishes_nothing():
    """Skript sekciu skryje sám — atribút v zdrojáku je len druhý zámok."""
    html = index_html()

    assert re.search(r"model\.hidden\s*=\s*true", html), (
        "renderModel() musí sekciu držať skrytú, kým dáta neprejdú kontrolou"
    )


def test_faq_and_the_model_section_tell_one_story():
    html = index_html()
    answer = re.search(r"<summary>Koľko reálne ušetrím\?</summary><p>(.*?)</p>", html, re.S)

    assert answer, "FAQ o úspore musí ostať"
    text = answer.group(1)
    assert "projekcia" in text.lower(), "FAQ musí priznať, že ročné číslo je projekcia"
    assert not PRICE_LIKE.search(text), "FAQ nesmie tvrdiť konkrétnu sumu"


# ------------------------------------------------------- python: publikovateľnosť
def payload(**overrides):
    data = {
        "schema_version": 1,
        "generated_at": "2026-08-18T05:02:20+02:00",
        "week": "2026-08-17",
        "week_label": "17.–23. 8. 2026",
        "sources": [{"store": "Lidl", "url": "https://letak.test/lidl",
                     "valid_from": "2026-08-17", "valid_to": "2026-08-23"}],
        "receipt": {
            "meals": [{
                "day": "PO", "name": "Kuracie stehná",
                "instructions": ["Osoľ.", "Opeč.", "Duste."],
                "items": [{
                    "offer_key": "offer_a", "name": "Kuracie stehná", "store": "Lidl",
                    "unit": "1 kg", "quantity": 1, "price": "2,69",
                    "original_price": "4,00", "savings": "1,31", "off": "-33 %",
                }],
            }],
            "nakup_spolu": "2,69", "bezne": "4,00", "usetris": "1,31",
            "polozky": 1, "polozky_s_beznou_cenou": 1,
        },
    }
    data.update(overrides)
    return data


TODAY = date(2026, 8, 18)


def test_publishable_only_with_a_substantiated_saving_this_week():
    assert model_example_is_publishable(payload(), TODAY) is True


def test_stale_week_is_never_publishable():
    assert model_example_is_publishable(payload(week="2026-08-10"), TODAY) is False


def test_week_without_a_verified_regular_price_is_never_publishable():
    data = payload()
    item = data["receipt"]["meals"][0]["items"][0]
    item["original_price"] = None
    item["savings"] = None
    data["receipt"].update(nakup_spolu="2,69", bezne="2,69", usetris="0,00",
                           polozky=1, polozky_s_beznou_cenou=0)

    assert model_example_is_publishable(data, TODAY) is False


def test_zero_saving_is_never_publishable():
    data = payload()
    data["receipt"]["meals"][0]["items"][0].update(original_price="2,69", savings="0,00")
    data["receipt"].update(bezne="2,69", usetris="0,00")

    assert model_example_is_publishable(data, TODAY) is False


def test_broken_payload_is_never_publishable():
    assert model_example_is_publishable({"week": "2026-08-17"}, TODAY) is False
    assert model_example_is_publishable(None, TODAY) is False


# --------------------------------------------------------------- javascript
DOM_STUB = """
var document = {createElement: function (tag) {
  return {tag: tag, className: '', textContent: '', children: [], hidden: false,
    append: function () {
      for (var i = 0; i < arguments.length; i++) this.children.push(arguments[i]);
    },
    replaceChildren: function () { this.children = []; this.append.apply(this, arguments); }};
}};
var model = {hidden: true};
var modelBody = document.createElement('div');
function flatten(element, collected) {
  if (!element) return collected;
  collected.push(element);
  (element.children || []).forEach(function (child) { flatten(child, collected); });
  return collected;
}
function textOf(element) {
  return flatten(element, []).map(function (n) { return n.textContent || ''; }).join(' | ');
}
function data() {
  return {
    week: '2026-08-17', week_label: '17.\\u201323. 8. 2026',
    sources: [{store: 'Lidl', url: 'https://letak.test/lidl',
               valid_from: '2026-08-17', valid_to: '2026-08-23'}],
    receipt: {
      meals: [{day: 'PO', name: 'Kuracie stehna', instructions: ['Osol.', 'Opec.', 'Duste.', 'Podavaj.'],
               items: [{name: 'Kuracie stehna', store: 'Lidl', price: '2,69',
                        original_price: '4,00', savings: '1,31', off: '-33 %'},
                       {name: 'Sosovica', store: 'Kaufland', price: '1,19',
                        original_price: '1,70', savings: '0,51', off: '-30 %'}]}],
      nakup_spolu: '3,88', bezne: '5,70', usetris: '1,82'
    }
  };
}
var NOW = new Date(2026, 7, 20);
"""


def model_helpers(html):
    return DOM_STUB + "\n".join([
        nested(html, "function node(tag, className, text)"),
        nested(html, "function amount(value)"),
        nested(html, "function money(value)"),
        nested(html, "function itemsOf(data)"),
        nested(html, "function mondayIso(now)"),
        nested(html, "function sourcesAreCurrent(data, now)"),
        nested(html, "function plural(count, one, few, many)"),
        nested(html, "function modelIsPublishable(data, now)"),
        nested(html, "function checkCard(key, lines, tail)"),
        nested(html, "function renderModel(data, now)"),
    ])


def run_node(tmp_path, name, source):
    script = tmp_path / name
    script.write_text(source, encoding="utf-8")
    return subprocess.run([NODE, str(script)], capture_output=True, text=True)


@needs_node
def test_model_renders_real_prices_and_a_conditional_annual_projection(tmp_path):
    result = run_node(
        tmp_path,
        "landing-model-render.js",
        model_helpers(index_html())
        + """
renderModel(data(), NOW);
if (model.hidden !== false) process.exit(1);
var text = textOf(modelBody);
if (text.indexOf('1,82 \\u20ac') === -1) process.exit(2);   // skutočná týždenná úspora
if (text.indexOf('2,69 ') === -1) process.exit(3);          // skutočná akciová cena
if (text.indexOf('Lidl') === -1) process.exit(4);
if (text.indexOf('Kaufland') === -1) process.exit(5);
if (text.indexOf('90 \\u20ac ro\\u010dne') === -1) process.exit(6);  // 1,82 x 52, zaokruhlene
if (text.toLowerCase().indexOf('ak by si') === -1) process.exit(7);  // podmienene, nie fakt
if (text.indexOf('430') !== -1) process.exit(8);
if (text.indexOf('undefined') !== -1) process.exit(9);
// recepty: bez recepty.py sa kroky vezmu z instructions, ktore prilozil blocek
if (text.indexOf('Kuracie stehna') === -1) process.exit(10);
if (text.indexOf('4 kroky') === -1) process.exit(11);
if (text.indexOf('+1 krok v appke') === -1) process.exit(12);
process.exit(0);
""",
    )

    assert result.returncode == 0, result.stdout + result.stderr


@needs_node
def test_recipes_added_by_recepty_win_over_the_receipt_instructions(tmp_path):
    result = run_node(
        tmp_path,
        "landing-model-recipes.js",
        model_helpers(index_html())
        + """
var enriched = data();
enriched.receipt.meals[0].recipe = {min: 45, steps_total: 6, steps: ['Osol a opec.', 'Podlej vodou.']};
renderModel(enriched, NOW);
var text = textOf(modelBody);
if (text.indexOf('45 min \\u00b7 6 krokov') === -1) process.exit(1);
if (text.indexOf('Osol a opec.') === -1) process.exit(2);
if (text.indexOf('+4 kroky v appke') === -1) process.exit(3);
process.exit(0);
""",
    )

    assert result.returncode == 0, result.stdout + result.stderr


@needs_node
def test_annual_projection_follows_the_weekly_figure_instead_of_a_constant(tmp_path):
    result = run_node(
        tmp_path,
        "landing-model-projection.js",
        model_helpers(index_html())
        + """
function annualFor(usetris) {
  modelBody.children = [];
  model.hidden = true;
  var payload = data();
  payload.receipt.usetris = usetris;
  renderModel(payload, NOW);
  return textOf(modelBody);
}
if (annualFor('1,00').indexOf('50 \\u20ac ro\\u010dne') === -1) process.exit(1);
if (annualFor('2,00').indexOf('100 \\u20ac ro\\u010dne') === -1) process.exit(2);
process.exit(0);
""",
    )

    assert result.returncode == 0, result.stdout + result.stderr


@needs_node
def test_model_stays_hidden_when_the_receipt_is_not_for_the_current_week(tmp_path):
    result = run_node(
        tmp_path,
        "landing-model-stale.js",
        model_helpers(index_html())
        + """
var stale = data();
stale.week = '2026-08-10';
renderModel(stale, NOW);
if (model.hidden !== true) process.exit(1);
if (modelBody.children.length !== 0) process.exit(2);
if (modelIsPublishable(stale, NOW) !== false) process.exit(3);
process.exit(0);
""",
    )

    assert result.returncode == 0, result.stdout + result.stderr


@needs_node
def test_browser_fails_closed_for_expired_or_undated_receipt_sources(tmp_path):
    result = run_node(
        tmp_path,
        "landing-model-expired-source.js",
        model_helpers(index_html())
        + """
var expired = data();
expired.sources[0].valid_to = '2026-08-19';
renderModel(expired, NOW);
if (sourcesAreCurrent(expired, NOW) !== false) process.exit(1);
if (model.hidden !== true) process.exit(2);

var missing = data();
delete missing.sources[0].valid_to;
if (sourcesAreCurrent(missing, NOW) !== false) process.exit(3);

var boundary = data();
boundary.sources[0].valid_to = '2026-08-20';
if (sourcesAreCurrent(boundary, NOW) !== true) process.exit(4);
process.exit(0);
""",
    )

    assert result.returncode == 0, result.stdout + result.stderr


@needs_node
def test_model_stays_hidden_without_a_substantiated_regular_price(tmp_path):
    result = run_node(
        tmp_path,
        "landing-model-unsubstantiated.js",
        model_helpers(index_html())
        + """
var bare = data();
bare.receipt.meals[0].items.forEach(function (item) {
  item.original_price = null;
  item.savings = null;
});
bare.receipt.bezne = '3,88';
bare.receipt.usetris = '0,00';
renderModel(bare, NOW);
if (model.hidden !== true) process.exit(1);
if (modelBody.children.length !== 0) process.exit(2);

var lying = data();
lying.receipt.meals[0].items.forEach(function (item) { item.original_price = null; });
if (modelIsPublishable(lying, NOW) !== false) process.exit(3);
process.exit(0);
""",
    )

    assert result.returncode == 0, result.stdout + result.stderr
