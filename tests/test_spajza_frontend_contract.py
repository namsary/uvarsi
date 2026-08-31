"""Čo musí platiť na obrazovke, keď špajza riadi nákup a nie jedálniček.

Testy sú čisto v Pythone (prípadne cez node), aby bežali aj na Linuxe — rovnako
ako tests/test_premium_frontend_contract.py.
"""
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest


APP = Path("app/static/app.html")
NODE = os.environ.get("UVARSI_NODE") or shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="node runtime is not available")


def app_html():
    return APP.read_text(encoding="utf-8")


def declaration(html, signature):
    match = re.search(re.escape(signature) + r"\{.*?\n\}", html, re.S)
    assert match, "app musí deklarovať " + signature.strip()
    return match.group(0)


def run_node(tmp_path, name, source):
    script = tmp_path / name
    script.write_text(source, encoding="utf-8")
    return subprocess.run([NODE, str(script)], capture_output=True, text=True)


# ------------------------------------------------ nákupný zoznam so špajzou
def test_the_shopping_list_marks_what_the_user_already_has_at_home():
    zoznam = declaration(app_html(), "function vZoznam() ")

    assert "mas_doma" in zoznam or "pantryOwned(" in zoznam, (
        "nákupný zoznam musí vedieť, čo už používateľ doma má"
    )
    assert "máš doma" in zoznam, "musí to byť po slovensky napísané pri položke"


def test_the_user_is_told_which_pantry_item_matched_so_he_can_check_it():
    """Párovanie je odhad nad voľným textom; človek musí vidieť, čo sa spárovalo."""
    zoznam = declaration(app_html(), "function vZoznam() ")

    assert "spajza_pokryte" in zoznam or ".spajza" in zoznam


def test_a_wrong_match_can_be_overruled_with_one_tap():
    """Zlá zhoda pošle človeka do obchodu bez suroviny — musí sa dať zrušiť."""
    html = app_html()
    zoznam = declaration(html, "function vZoznam() ")

    assert "PANTRY_OVERRIDE" in html, "zrušená zhoda si musí pamätať, že je zrušená"
    assert "data-doma" in zoznam, "zrušenie musí byť samostatný ovládací prvok"
    assert "Predsa kúpiť" in zoznam


@needs_node
def test_owned_and_overruled_items_are_decided_by_one_pure_function(tmp_path):
    html = app_html()
    result = run_node(
        tmp_path,
        "pantry-owned-contract.js",
        declaration(html, "function pantryOwned(item, overrides) ")
        + """
if (pantryOwned({mas_doma: true, offer_key: 'a'}, {}) !== true) process.exit(1);
if (pantryOwned({mas_doma: false, offer_key: 'a'}, {}) !== false) process.exit(2);
if (pantryOwned({mas_doma: true, offer_key: 'a'}, {a: true}) !== false) process.exit(3);
if (pantryOwned({mas_doma: true, offer_key: 'a'}, {b: true}) !== true) process.exit(4);
if (pantryOwned(null, {}) !== false) process.exit(5);
if (pantryOwned(undefined, undefined) !== false) process.exit(6);
if (pantryOwned({mas_doma: true}, {}) !== true) process.exit(7);
process.exit(0);
""",
    )

    assert result.returncode == 0, result.stdout + result.stderr


@needs_node
def test_the_remaining_total_really_adds_up_from_what_is_still_being_bought(tmp_path):
    """Číslo „kúpiš za" musí sedieť aj po zrušení zhody, inak je to klamstvo."""
    html = app_html()
    result = run_node(
        tmp_path,
        "pantry-total-contract.js",
        declaration(html, "function pantryOwned(item, overrides) ")
        + declaration(html, "function moneyValue(text) ")
        + declaration(html, "function moneyText(amount) ")
        + declaration(html, "function shoppingItems(plan) ")
        + declaration(html, "function remainingTotal(plan, overrides) ")
        + """
var plan = {nakupny_zoznam: [{obchod: 'Lidl', polozky: [
  {offer_key: 'a', cena: '1,49', mas_doma: true},
  {offer_key: 'b', cena: '5,00', mas_doma: false}
]}]};
if (remainingTotal(plan, {}) !== '5,00') process.exit(1);
if (remainingTotal(plan, {a: true}) !== '6,49') process.exit(2);
if (remainingTotal({nakupny_zoznam: []}, {}) !== '0,00') process.exit(3);
if (remainingTotal(null, {}) !== '0,00') process.exit(4);
if (moneyValue('1,49') !== 1.49) process.exit(5);
if (moneyValue('nezmysel') !== 0) process.exit(6);
if (moneyText(4.5) !== '4,50') process.exit(7);
if (moneyText(0) !== '0,00') process.exit(8);
process.exit(0);
""",
    )

    assert result.returncode == 0, result.stdout + result.stderr


# ------------------------------------------- výslovné „uvar z toho, čo mám doma"
def test_the_pantry_screen_offers_the_explicit_cook_from_home_action():
    html = app_html()
    spajza = declaration(html, "function vSpajza() ")

    assert "sp-navrhni" in spajza, "špajza musí ponúkať vlastné tlačidlo"
    assert "z toho, čo mám doma" in spajza
    assert "/api/plan/zo-spajze" in html, "musí volať vyhradenú cestu, nie bežné skladanie"


def test_the_explicit_action_warns_that_it_costs_a_recompute():
    """Je to platené volanie z denného stropu — nesmie to byť prekvapenie."""
    spajza = declaration(app_html(), "function vSpajza() ")

    assert "prepočet" in spajza or "prepočt" in spajza


def test_the_pantry_screen_promises_only_what_the_pantry_now_does():
    """Sľub sa musel zmeniť: špajza sama od seba jedálniček nepreskladá."""
    spajza = declaration(app_html(), "function vSpajza() ")

    assert "Jedálniček sa tým nemení" in spajza


def test_the_pantry_hint_on_the_plan_screen_leads_to_the_explicit_action():
    """Návrh po zmene špajze už neponúka „prepočítaj", ale „uvar z toho, čo mám"."""
    plan_view = declaration(app_html(), "function vPlan() ")

    assert "sp-hint-go" in plan_view
    assert "navrhniZoSpajze()" in plan_view, (
        "tlačidlo musí viesť na vyžiadané skladanie zo špajze, nie na obyčajný prepočet"
    )


def test_the_pantry_hint_is_still_premium_only_and_dismissible():
    plan_view = declaration(app_html(), "function vPlan() ")

    assert "pantryDiffers(" in plan_view
    assert re.search(r"ME\.premium[^;]*pantryDiffers\(", plan_view)
    assert "sp-hint-off" in plan_view and "PANTRY_HINT_HIDDEN" in app_html()


# ------------------------------------------------------- dávky, porcie, pre koho
def test_the_recipe_finally_says_how_much_of_what_and_for_whom():
    """Server tieto polia posiela od začiatku; obrazovka ich zamlčiavala."""
    html = app_html()
    plan_view = declaration(html, "function vPlan() ")
    portions = declaration(html, "function portionLine(recept) ")

    assert "recept?.davky" in plan_view or "recept.davky" in plan_view
    assert "portionLine(j.recept)" in plan_view
    assert "recept.pre" in portions and "recept.porcie" in portions


@needs_node
def test_the_portion_line_reads_naturally_in_slovak(tmp_path):
    html = app_html()
    result = run_node(
        tmp_path,
        "portions-contract.js",
        declaration(html, "function portionLine(recept) ")
        + """
if (portionLine({porcie: 1, pre: '1 osoba'}) !== '1 porcia · pre 1 osoba') process.exit(1);
if (portionLine({porcie: 4, pre: '2 osoby × 2 dni'}) !== '4 porcie · pre 2 osoby × 2 dni') process.exit(2);
if (portionLine({porcie: 8, pre: '4 osoby × 2 dni'}) !== '8 porcií · pre 4 osoby × 2 dni') process.exit(3);
if (portionLine({porcie: 6}) !== '6 porcií') process.exit(4);
if (portionLine({pre: '4 osoby'}) !== 'pre 4 osoby') process.exit(5);
if (portionLine({}) !== '') process.exit(6);
if (portionLine(null) !== '') process.exit(7);
process.exit(0);
""",
    )

    assert result.returncode == 0, result.stdout + result.stderr


# ------------------------------------- množstevná špajza a zvyšky z nákupu
@needs_node
def test_old_string_pantry_and_structured_me_are_compared_by_name(tmp_path):
    html = app_html()
    result = run_node(
        tmp_path,
        "pantry-diff-objects.js",
        declaration(html, "function pantryName(value) ")
        + declaration(html, "function pantryDiffers(planPantry, currentPantry) ")
        + """
if (pantryDiffers([' Ryža ','VAJCIA'],[
  {nazov:'ryža',mnozstvo:500,jednotka:'g'},
  {nazov:'vajcia',mnozstvo:6,jednotka:'piece'}
])) process.exit(1);
if (!pantryDiffers(['ryža'],[{nazov:'cestoviny',mnozstvo:500,jednotka:'g'}])) process.exit(2);
if (pantryDiffers([],[])) process.exit(3);
if (pantryDiffers(null,[{nazov:'ryža',mnozstvo:1,jednotka:'g'}])) process.exit(4);
process.exit(0);
""",
    )
    assert result.returncode == 0, result.stdout + result.stderr


@needs_node
def test_pantry_rows_show_real_quantities_and_legacy_prompt(tmp_path):
    html = app_html()
    result = run_node(
        tmp_path,
        "pantry-row-html.js",
        "function esc(value){return String(value == null ? '' : value);}\n"
        + declaration(html, "function pantryRowHtml(item, index) ")
        + """
var measured=pantryRowHtml({nazov:'ryža',mnozstvo:500,jednotka:'g'},0);
if (!measured.includes('ryža') || !measured.includes('500') || !measured.includes('g')) process.exit(1);
if (measured.includes('[object Object]')) process.exit(2);
if (!measured.includes('Upraviť') || !measured.includes('Odstrániť')) process.exit(3);
var legacy=pantryRowHtml({nazov:'vajcia',mnozstvo:null,jednotka:null},1);
if (!legacy.includes('vajcia') || !legacy.includes('Doplň množstvo')) process.exit(4);
if (!legacy.includes('data-legacy="true"')) process.exit(5);
process.exit(0);
""",
    )
    assert result.returncode == 0, result.stdout + result.stderr


@needs_node
def test_new_pantry_item_requires_positive_amount_and_normalizes_units(tmp_path):
    html = app_html()
    result = run_node(
        tmp_path,
        "pantry-item-fields.js",
        declaration(html, "function pantryItemFromFields(name, amount, unit, allowLegacy) ")
        + """
var rice=pantryItemFromFields(' ryža ', '500', 'g', false);
if (rice.nazov !== 'ryža' || rice.mnozstvo !== 500 || rice.jednotka !== 'g') process.exit(1);
var eggs=pantryItemFromFields('vajcia','6','ks',false);
if (eggs.jednotka !== 'piece' || eggs.mnozstvo !== 6) process.exit(2);
for (const bad of ['', '0', '-2', 'x']) {
  var failed=false; try { pantryItemFromFields('ryža',bad,'g',false); } catch(e) { failed=true; }
  if (!failed) process.exit(3);
}
var legacy=pantryItemFromFields('soľ','','g',true);
if (legacy.mnozstvo !== null || legacy.jednotka !== null) process.exit(4);
process.exit(0);
""",
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_pantry_uses_one_explicit_save_not_implicit_posts():
    html = app_html()
    pantry = declaration(html, "function vSpajza() ")
    row = declaration(html, "function pantryRowHtml(item, index) ")
    assert 'id="sp-name"' in pantry and 'id="sp-amount"' in pantry and 'id="sp-unit"' in pantry
    assert 'id="sp-save"' in pantry and "Uložiť špajzu" in pantry
    assert "savePantryList" in pantry
    assert "/api/spajza" not in pantry, "vSpajza only edits a draft; persistence has one helper"
    assert "sp-add" in pantry and "sp-remove" in row and "sp-edit" in row


@needs_node
def test_one_save_is_one_post_then_refetch_and_plan_refresh(tmp_path):
    html = app_html()
    result = run_node(
        tmp_path,
        "pantry-one-save.js",
        """
var ME={premium:true}, PANTRY_HINT_HIDDEN=true, PANTRY_ACCESS_CHANGED=false;
var calls=[], refreshed=0;
async function api(url, options){calls.push([url,options]);return url==='/api/me'?{premium:true,spajza:[]}:{ok:true};}
async function refreshPlanAfterPantrySave(){refreshed++;}
"""
        + declaration(html, "async function savePantryList(list) ")
        + """
(async function(){
  await savePantryList([{nazov:'ryža',mnozstvo:500,jednotka:'g'}]);
  if (calls.filter(c=>c[0]==='/api/spajza').length !== 1) process.exit(1);
  if (calls.filter(c=>c[0]==='/api/me').length !== 1) process.exit(2);
  if (refreshed !== 1) process.exit(3);
  var body=JSON.parse(calls[0][1].body);
  if (body.polozky[0].mnozstvo !== 500) process.exit(4);
  process.exit(0);
})().catch(e=>{console.error(e);process.exit(99)});
""",
    )
    assert result.returncode == 0, result.stdout + result.stderr


@needs_node
def test_machine_readable_leftover_is_normalized_and_merged_safely(tmp_path):
    html = app_html()
    result = run_node(
        tmp_path,
        "leftover-pantry.js",
        declaration(html, "function pantryName(value) ")
        + declaration(html, "function leftoverPantryItem(item) ")
        + declaration(html, "function mergePantryItem(items, incoming) ")
        + """
var rice=leftoverPantryItem({nazov:'Ryža',zostane:'0,7 kg'});
if (!rice || rice.mnozstvo !== 700 || rice.jednotka !== 'g') process.exit(1);
var oil=leftoverPantryItem({nazov:'Olej',zostane_po_spajzi:'0.25 l',zostane:'800 ml'});
if (!oil || oil.mnozstvo !== 250 || oil.jednotka !== 'ml') process.exit(2);
var deterministic=leftoverPantryItem({nazov:'Cestoviny',zostava:'0,4 kg',zostane:'800 g'});
if (!deterministic || deterministic.mnozstvo !== 400 || deterministic.jednotka !== 'g') process.exit(7);
if (leftoverPantryItem({nazov:'Ryža',zostane:'približne polovica balenia'}) !== null) process.exit(3);
if (leftoverPantryItem({nazov:'',zostane:'500 g'}) !== null) process.exit(4);
var merged=mergePantryItem([{nazov:' ryža ',mnozstvo:300,jednotka:'g'}],rice);
if (merged.length !== 1 || merged[0].mnozstvo !== 1000 || merged[0].nazov !== 'ryža') process.exit(5);
var split=mergePantryItem([{nazov:'ryža',mnozstvo:1,jednotka:'piece'}],rice);
if (split.length !== 2) process.exit(6);
process.exit(0);
""",
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_shopping_list_adds_leftover_only_on_an_explicit_guarded_button():
    html = app_html()
    shopping = declaration(html, "function vZoznam() ")
    add = declaration(html, "async function addLeftoverToPantry(button, item) ")
    button = declaration(html, "function leftoverButtonHtml(item, key, done, pantryAvailable) ")
    assert "Pridať zvyšok do špajze" in button
    assert "leftoverPantryItem" in button
    assert "data-leftover" in button
    assert "leftoverButtonHtml" in shopping
    assert "stopPropagation" in shopping
    assert "button.disabled = true" in add
    assert "savePantryList" in add
    assert "/api/spajza" not in shopping, "render and shopping checkbox must never persist pantry"


@needs_node
def test_leftover_action_exists_only_for_a_checked_shopping_row(tmp_path):
    html = app_html()
    result = run_node(
        tmp_path,
        "leftover-checked.js",
        "function esc(value){return String(value == null ? '' : value);}\n"
        + declaration(html, "function pantryName(value) ")
        + declaration(html, "function leftoverPantryItem(item) ")
        + declaration(html, "function leftoverButtonHtml(item, key, done, pantryAvailable) ")
        + """
var item={nazov:'Ryža',zostava:'500 g'};
if (leftoverButtonHtml(item,'0-0',false,true) !== '') process.exit(1);
var checked=leftoverButtonHtml(item,'0-0',true,true);
if (!checked.includes('Pridať zvyšok do špajze') || !checked.includes('data-leftover="0-0"')) process.exit(2);
if (leftoverButtonHtml(item,'0-0',true,false) !== '') process.exit(3);
if (leftoverButtonHtml({nazov:'Ryža',zostava:'asi polovica'},'0-0',true,true) !== '') process.exit(4);
process.exit(0);
""",
    )
    assert result.returncode == 0, result.stdout + result.stderr


@needs_node
def test_toggling_a_shopping_row_rerenders_leftover_state(tmp_path):
    html = app_html()
    result = run_node(
        tmp_path,
        "shopping-done-rerender.js",
        "var DONE={}, ME={id:1}, PLAN={tyzden:'2026-08-31'}, renders=0, writes=0;\n"
        "var localStorage={setItem:function(){writes++;}};\n"
        "function checkedStateKey(){return 'checked';}\n"
        "function vZoznam(){renders++;}\n"
        + declaration(html, "function toggleShoppingDone(key) ")
        + """
if (toggleShoppingDone('0-0') !== true || DONE['0-0'] !== true) process.exit(1);
if (renders !== 1 || writes !== 1) process.exit(2);
if (toggleShoppingDone('0-0') !== false || DONE['0-0'] !== false) process.exit(3);
if (renders !== 2 || writes !== 2) process.exit(4);
process.exit(0);
""",
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_pantry_editors_have_visible_labels_and_controls_relationships():
    html = app_html()
    row = declaration(html, "function pantryRowHtml(item, index) ")
    pantry = declaration(html, "function vSpajza() ")
    assert "aria-controls" in row and "pantry-editor-" in row
    assert 'id="${editorId}"' in row
    for label in ["Názov", "Množstvo", "Jednotka"]:
        assert f">{label}<" in row
    assert '<label for="sp-name">Surovina</label>' in pantry
    assert '<label for="sp-amount">Množstvo</label>' in pantry
    assert '<label for="sp-unit">Jednotka</label>' in pantry
