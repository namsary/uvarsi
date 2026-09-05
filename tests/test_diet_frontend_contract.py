import os
import shutil
import subprocess
from pathlib import Path

import pytest


APP = Path("app/static/app.html")
NODE = os.environ.get("UVARSI_NODE") or shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="node runtime is not available")


def app_html():
    return APP.read_text(encoding="utf-8")


def function_source(html, name):
    needle = f"function {name}("
    start = html.find(needle)
    assert start >= 0, f"app must declare {name}()"
    async_start = start - len("async ")
    if async_start >= 0 and html[async_start:start] == "async ":
        start = async_start
    opening = html.find("{", start)
    depth = 0
    quote = None
    escaped = False
    for index in range(opening, len(html)):
        char = html[index]
        if quote is not None:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            continue
        if char in {"'", '"', "`"}:
            quote = char
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return html[start : index + 1]
    raise AssertionError(f"unterminated {name}()")


def run_node(tmp_path, name, source):
    script = tmp_path / name
    script.write_text(source, encoding="utf-8")
    return subprocess.run(
        [NODE, str(script)], capture_output=True, text=True, encoding="utf-8"
    )


@needs_node
def test_diet_modes_are_a_real_accessible_2_by_2_choice(tmp_path):
    html = app_html()
    result = run_node(
        tmp_path,
        "diet-options.js",
        "function esc(value){return String(value == null ? '' : value);}\n"
        + function_source(html, "dietModeAvailable")
        + "\n"
        + function_source(html, "dietOptionsHtml")
        + """
var available=['standard','high_protein','vegetarian','vegan'];
var free=dietOptionsHtml('vegetarian', false, 'on', available);
for (const label of ['Bez obmedzenia','Viac bielkovín','Vegetariánsky','Vegánsky']) {
  if (!free.includes(label)) process.exit(1);
}
if ((free.match(/type="button"/g)||[]).length !== 4) process.exit(2);
if ((free.match(/aria-pressed="true"/g)||[]).length !== 1) process.exit(3);
if ((free.match(/Premium/g)||[]).length !== 3) process.exit(4);
if (!free.includes('data-mode="standard" aria-pressed="true"')) process.exit(5);
if ((free.match(/aria-disabled="true"/g)||[]).length !== 3) process.exit(6);
if ((free.match(/ disabled/g)||[]).length !== 3) process.exit(7);
var paid=dietOptionsHtml('vegan', true, 'on', available);
if ((paid.match(/Premium/g)||[]).length !== 0) process.exit(8);
if (!paid.includes('data-mode="vegan" aria-pressed="true"')) process.exit(9);
if (paid.includes(' disabled') || paid.includes('aria-disabled="true"')) process.exit(10);
var shadow=dietOptionsHtml('vegan', true, 'shadow');
if (!shadow.includes('Bez obmedzenia') || shadow.includes('data-mode="vegan"')) process.exit(11);
if (!shadow.includes('Režimy stravovania')) process.exit(12);
var absent=dietOptionsHtml('high_protein', true);
if (absent.includes('data-mode="high_protein"')) process.exit(13);
var noMode=dietOptionsHtml('standard', true, 'on', []);
if (!noMode.includes('data-mode="standard" aria-pressed="true" aria-disabled="true" disabled')) process.exit(14);
if (!noMode.includes('Tento týždeň nedostupné')) process.exit(15);
if (!noMode.includes('Pridaj obchod alebo vyber iný režim.')) process.exit(16);
process.exit(0);
""",
    )
    assert result.returncode == 0, result.stdout + result.stderr


@needs_node
def test_diet_selection_updates_aria_and_profile_payload(tmp_path):
    html = app_html()
    result = run_node(
        tmp_path,
        "diet-selection.js",
        function_source(html, "dietModeAvailable")
        + "\n"
        + function_source(html, "selectDietMode")
        + "\n"
        + function_source(html, "profilePayload")
        + """
function button(mode,disabled){return {dataset:{mode:mode},disabled:!!disabled,classList:{toggle:function(n,on){this[n]=on;}},attrs:{'aria-pressed':mode==='standard'?'true':'false','aria-disabled':disabled?'true':'false'},setAttribute:function(k,v){this.attrs[k]=v;},getAttribute:function(k){return this.attrs[k];}}}
var buttons=[button('standard',false),button('high_protein',true),button('vegetarian',true),button('vegan',true)];
var root={querySelectorAll:function(){return buttons;}};
if (selectDietMode(root,'vegan') !== false) process.exit(1);
if (buttons[0].attrs['aria-pressed'] !== 'true' || buttons[3].attrs['aria-pressed'] !== 'false') process.exit(2);
buttons[3].disabled=false; buttons[3].attrs['aria-disabled']='false';
if (selectDietMode(root,'vegan') !== true) process.exit(3);
if (buttons.filter(b=>b.attrs['aria-pressed']==='true').length !== 1) process.exit(4);
if (buttons[3].attrs['aria-pressed'] !== 'true' || !buttons[3].classList.on) process.exit(5);
    var paid={premium:true,recipe_engine:'on',stravovanie_dostupne:['standard','high_protein','vegetarian','vegan']};
var payload=profilePayload(2,2,3,['Lidl','Tesco'],'high_protein');
if (payload.stravovanie !== 'high_protein') process.exit(4);
if (payload.adults !== 2 || payload.children !== 2 || payload.frekvencia !== 3) process.exit(6);
if (payload.obchody.join('|') !== 'Lidl|Tesco') process.exit(7);
if (profilePayload(2,0,2,['Lidl'],'vegan').stravovanie !== 'vegan') process.exit(8);
var preserved=profilePayload(2,0,2,['Lidl'],undefined);
if (Object.prototype.hasOwnProperty.call(preserved,'stravovanie')) process.exit(9);
    if (dietModeAvailable('vegetarian',paid) !== true) process.exit(10);
    if (dietModeAvailable('vegetarian',{premium:true}) !== false) process.exit(11);
    if (dietModeAvailable('vegetarian',{premium:true,recipe_engine:'on'}) !== false) process.exit(12);
    if (dietModeAvailable('standard',{premium:true,recipe_engine:'on',stravovanie_dostupne:[]}) !== false) process.exit(13);
    if (dietModeAvailable('standard',{premium:false,recipe_engine:'on',stravovanie_dostupne:['standard']}) !== true) process.exit(14);
process.exit(0);
""",
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_profile_save_sends_the_selected_mode_and_keeps_server_errors_visible():
    onboarding = function_source(app_html(), "viewOnboarding")
    assert "dietOptionsHtml" in onboarding
    assert "selectDietMode" in onboarding
    assert "profilePayload" in onboarding
    assert "profil.recipe_engine" in onboarding
    assert "stravovanie" in onboarding
    assert "profil.recipe_engine === 'on'" in onboarding
    assert "selectedDiet : undefined" in onboarding
    assert "runGuardedAction($('#save'), $('#ob-err')" in onboarding
    assert onboarding.index("await api('/api/profil'") < onboarding.index("ME = await api('/api/me')")
    assert "dietModeAvailable(ME.stravovanie, ME)" in onboarding
    assert onboarding.index("dietModeAvailable(ME.stravovanie, ME)") < onboarding.index("await refreshPlanAfterProfileSave()")


@needs_node
def test_recipe_nutrition_and_allergens_are_truthful_and_slovak(tmp_path):
    html = app_html()
    result = run_node(
        tmp_path,
        "recipe-facts.js",
        "function esc(value){return String(value == null ? '' : value);}\n"
        + function_source(html, "recipeFactsHtml")
        + """
var claimed=recipeFactsHtml({nutrition:{serving:{kcal:637,protein_g:33.6}},high_protein_claim:true,allergens:['milk','egg','fish','soy','wheat']});
if (!claimed.includes('≈ 640 kcal · 34 g bielkovín na dospelú porciu')) process.exit(1);
if (!claimed.includes('Vysoký obsah bielkovín')) process.exit(2);
for (const word of ['mlieko','vajcia','ryby','sója','lepok']) if (!claimed.includes(word)) process.exit(3);
if (!claimed.includes('Pri alergii skontroluj zloženie konkrétneho výrobku na obale.')) process.exit(4);
var unverified=recipeFactsHtml({nutrition:{serving:{kcal:614,protein_g:42}},high_protein_claim:false,allergens:[]});
if (unverified.includes('Vysoký obsah bielkovín')) process.exit(5);
if (!unverified.includes('≈ 610 kcal · 42 g bielkovín na dospelú porciu')) process.exit(6);
if (unverified.includes('Pri alergii')) process.exit(7);
var malformed=recipeFactsHtml({nutrition:{serving:{kcal:'n/a',protein_g:'n/a'}},high_protein_claim:true,allergens:['unknown']});
if (malformed.includes('na dospelú porciu')) process.exit(8);
if (!malformed.includes('Vysoký obsah bielkovín')) process.exit(9);
if (!malformed.includes('unknown')) process.exit(10);
for (const serving of [
  {kcal:true,protein_g:33},
  {kcal:630,protein_g:[33]},
  {kcal:'',protein_g:33},
  {kcal:630,protein_g:''},
  {kcal:0,protein_g:33},
  {kcal:630,protein_g:0},
  {kcal:630},
  {protein_g:33}
]) {
  if (recipeFactsHtml({nutrition:{serving:serving}}).includes('na dospelú porciu')) process.exit(11);
}
process.exit(0);
""",
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_plan_renders_recipe_facts_inside_each_recipe():
    plan = function_source(app_html(), "vPlan")
    assert "recipeFactsHtml(j.recept)" in plan
    assert "nutrition.serving.protein_g" not in plan, "formatting belongs in one tested helper"


@needs_node
def test_legacy_recipe_without_facts_stays_silent(tmp_path):
    html = app_html()
    result = run_node(
        tmp_path,
        "recipe-facts-legacy.js",
        "function esc(value){return String(value == null ? '' : value);}\n"
        + function_source(html, "recipeFactsHtml")
        + """
if (recipeFactsHtml({}) !== '') process.exit(1);
if (recipeFactsHtml(null) !== '') process.exit(2);
process.exit(0);
""",
    )
    assert result.returncode == 0, result.stdout + result.stderr
