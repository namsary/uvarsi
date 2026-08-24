"""Čo musí platiť na obrazovke, keď špajza riadi nákup a nie jedálniček.

Testy sú čisto v Pythone (prípadne cez node), aby bežali aj na Linuxe — rovnako
ako tests/test_premium_frontend_contract.py.
"""
import re
import shutil
import subprocess
from pathlib import Path

import pytest


APP = Path("app/static/app.html")
NODE = shutil.which("node")
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
