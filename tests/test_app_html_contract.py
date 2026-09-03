import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest


CSCRIPT = Path("C:/Windows/System32/cscript.exe")
NODE = os.environ.get("UVARSI_NODE") or shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="node runtime is not available")


def app_html():
    return Path("app/static/app.html").read_text(encoding="utf-8")


def declaration(html, signature):
    """Return the whole top-level function declaration that starts with signature."""
    match = re.search(re.escape(signature) + r"\{.*?\n\}", html, re.S)
    assert match, "app must declare " + signature.strip()
    return match.group(0)


def run_node(tmp_path, name, source):
    script = tmp_path / name
    script.write_text(source, encoding="utf-8")
    return subprocess.run([NODE, str(script)], capture_output=True, text=True)


def run_cscript(script):
    result = subprocess.run(
        [str(CSCRIPT), "//nologo", str(script)], capture_output=True, text=True
    )
    output = (result.stdout or "") + (result.stderr or "")
    if result.returncode and "Access is denied" in output:
        pytest.skip("Windows Script Host is blocked by the execution environment")
    return result


@needs_node
def test_plan_totals_support_deterministic_and_legacy_shapes(tmp_path):
    html = app_html()
    result = run_node(
        tmp_path,
        "plan-totals.js",
        declaration(html, "function moneyValue(text) ")
        + declaration(html, "function moneyText(amount) ")
        + declaration(html, "function planTotals(plan) ")
        + """
function same(actual, expected) {
  return actual.nakup === expected[0] && actual.bezna === expected[1] && actual.usetris === expected[2];
}
if (!same(planTotals({nakup_spolu:'12,50',bezna_cena:'20',usetrene:'7,50'}),['12,50','20,00','7,50'])) process.exit(1);
if (!same(planTotals({nakup_spolu:'12,50',bezne:'20',usetris:'7,50'}),['12,50','20,00','7,50'])) process.exit(2);
if (!same(planTotals({nakup_spolu:0,bezna_cena:0,bezne:99,usetrene:0,usetris:88}),['0,00','0,00','0,00'])) process.exit(3);
if (!same(planTotals({nakup_spolu:'10',bezna_cena:'15'}),['10,00','15,00','5,00'])) process.exit(4);
if (!same(planTotals({nakup_spolu:'10',usetrene:'5'}),['10,00','15,00','5,00'])) process.exit(5);
if (!same(planTotals({}),['0,00','0,00','0,00'])) process.exit(6);
process.exit(0);
""",
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_plan_view_uses_one_normalized_totals_contract():
    plan_view = declaration(app_html(), "function vPlan() ")
    assert "planTotals(PLAN)" in plan_view
    assert "PLAN.usetris" not in plan_view
    assert "PLAN.bezne" not in plan_view


@needs_node
def test_loading_skeleton_announces_status(tmp_path):
    html = app_html()
    result = run_node(
        tmp_path,
        "loading-status.js",
        "function esc(value){return String(value == null ? '' : value);}\n"
        + declaration(html, "function loadingSkeletonHtml(message) ")
        + """
var rendered=loadingSkeletonHtml('Pripravujem plán…');
if (!rendered.includes('role="status"')) process.exit(1);
if (!rendered.includes('aria-live="polite"')) process.exit(2);
if (!rendered.includes('Pripravujem plán…')) process.exit(3);
process.exit(0);
""",
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_initial_loading_shell_is_also_an_announced_status():
    html = app_html()
    marker = '<div class="plan-skeleton" aria-label="Načítavam jedálniček"'
    assert marker + ' role="status" aria-live="polite">' in html


def test_saving_profile_never_generates_a_plan_implicitly():
    """Zmena nastavení nesmie potichu minúť prepočet ani zavolať model."""
    html = app_html()
    onboarding = declaration(html, "function viewOnboarding() ")

    assert "nacitajPlan(true)" not in onboarding
    assert "/api/plan/generuj" not in onboarding
    assert "refreshPlanAfterProfileSave" in onboarding

    refresh = declaration(html, "async function refreshPlanAfterProfileSave() ")
    assert "api('/api/plan')" in refresh
    assert "/api/plan/generuj" not in refresh
    assert "nacitajPlan(true)" not in refresh


@needs_node
def test_free_selects_one_store_while_premium_can_select_more(tmp_path):
    html = app_html()
    result = run_node(
        tmp_path,
        "store-entitlements.js",
        declaration(html, "function nextStoreSelection(selected, clicked, premium) ")
        + """
function same(a,b){return JSON.stringify(a)===JSON.stringify(b);}
if (!same(nextStoreSelection(['Kaufland'], 'Lidl', false), ['Lidl'])) process.exit(1);
if (!same(nextStoreSelection(['Kaufland'], 'Lidl', true), ['Kaufland','Lidl'])) process.exit(2);
if (!same(nextStoreSelection(['Kaufland','Lidl'], 'Lidl', true), ['Kaufland'])) process.exit(3);
process.exit(0);
""",
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_store_choices_are_real_accessible_buttons():
    onboarding = declaration(app_html(), "function viewOnboarding() ")

    assert '<button type="button" class="chip' in onboarding
    assert 'aria-pressed="${' in onboarding
    assert "nextStoreSelection" in onboarding


def test_store_entitlement_change_refreshes_the_open_profile_form():
    onboarding = declaration(app_html(), "function viewOnboarding() ")

    assert "error.code === 'obchody_premium'" in onboarding
    assert "ME = await api('/api/me')" in onboarding
    assert "viewOnboarding()" in onboarding


def test_profile_save_empty_state_has_truthful_profile_copy_and_button_label():
    html = app_html()
    onboarding = declaration(html, "function viewOnboarding() ")
    refresh = declaration(html, "async function refreshPlanAfterProfileSave() ")
    plan_view = declaration(html, "function vPlan() ")

    assert "Uložiť nastavenia" in onboarding
    assert "Uložiť a prepočítať plán" not in onboarding
    assert "PLAN_EMPTY_REASON = empty ? 'profil' : ''" in refresh
    assert "PLAN_EMPTY_REASON === 'profil'" in plan_view
    assert "Nastavenia sa zmenili" in plan_view
    assert "Špajza sa zmenila" in plan_view


def test_app_shell_has_noindex_fallback_meta_tag():
    html = app_html()

    assert '<meta name="robots" content="noindex,nofollow,noarchive">' in html


def test_every_empty_plan_after_pantry_save_requires_an_explicit_cta():
    html = app_html()
    refresh = declaration(html, "async function refreshPlanAfterPantrySave() ")
    plan_view = declaration(html, "function vPlan() ")

    assert "const empty = !!(plan && plan.prazdny)" in refresh
    assert "PLAN_NEEDS_REGEN = empty" in refresh
    assert "PLAN_EMPTY_REASON = empty ? 'spajza' : ''" in refresh
    assert "plan.prazdny && plan.obnovit_cez" not in refresh
    assert "if (PLAN_NEEDS_REGEN)" in plan_view
    guarded = plan_view.split("if (!PLAN || !PLAN.jedla) return nacitajPlan(true)", 1)[0]
    assert "Vytvoriť aktuálny jedálniček" in guarded
    assert "return;" in guarded, "prázdny stav sa musí zastaviť na CTA"


def test_cached_shell_navigation_stays_locked_until_authoritative_profile_arrives():
    """Zapamätané meno je iba skeleton, nie oprávnenie klikať do appky."""
    html = app_html()
    shell = declaration(html, "function paintKnownShell() ")
    nav_click = re.search(r"\$\('#nav'\)\.onclick = e => \{.*?\n\};", html, re.S)
    start = declaration(html, "async function start() ")

    assert nav_click, "spodná navigácia musí mať jeden strážený click handler"
    assert "setNavigationReady(false)" in shell
    assert "if (!APP_READY) return" in nav_click.group(0)
    assert "ME = await readStartupResponse(STARTUP.me)" in start
    authoritative = start.index("ME = await readStartupResponse(STARTUP.me)")
    unlock = start.index("setNavigationReady(true)")
    assert unlock > authoritative, "menu sa smie odomknúť až po odpovedi /api/me"


def test_checked_shopping_state_is_namespaced_by_authenticated_user_and_plan_week(tmp_path):
    html = Path("app/static/app.html").read_text(encoding="utf-8")
    match = re.search(r"function checkedStateKey\(user, plan\) \{.*?\n\}", html, re.S)
    assert match, "app must provide checkedStateKey(user, plan) for scoped shopping state"
    script = tmp_path / "checked-state-contract.js"
    script.write_text(
        match.group(0)
        + "\nvar state = {};\n"
        + "var a = checkedStateKey({id: 7, email: 'first@uvar.si'}, {tyzden: '2026-08-17'});\n"
        + "var b = checkedStateKey({id: 8, email: 'second@uvar.si'}, {tyzden: '2026-08-17'});\n"
        + "var nextWeek = checkedStateKey({id: 7, email: 'first@uvar.si'}, {tyzden: '2026-08-24'});\n"
        + "state[a] = '{\\\"0-0\\\":true}';\n"
        + "if (a === b || a === nextWeek || state[b] || state[nextWeek]) WScript.Quit(1);\n"
        + "WScript.Quit(0);\n",
        encoding="utf-8",
    )

    result = run_cscript(script)

    assert result.returncode == 0, result.stderr


def test_shopping_quantity_label_combines_quantity_with_verified_unit(tmp_path):
    html = Path("app/static/app.html").read_text(encoding="utf-8")
    match = re.search(r"function shoppingQuantityLabel\(item\) \{.*?\n\}", html, re.S)
    assert match, "app must provide shoppingQuantityLabel(item)"
    script = tmp_path / "shopping-quantity-contract.js"
    script.write_text(
        match.group(0)
        + "\nif (shoppingQuantityLabel({mnozstvo:2,jednotka:'500 g'}) !== '2 × 500 g') WScript.Quit(1);\n"
        + "if (shoppingQuantityLabel({mnozstvo:1,jednotka:'1 l'}) !== '1 × 1 l') WScript.Quit(2);\n"
        + "WScript.Quit(0);\n",
        encoding="utf-8",
    )

    result = run_cscript(script)

    assert result.returncode == 0, result.stderr
    assert "esc(shoppingQuantityLabel(p))" in html


def test_recipe_disclosure_has_keyboard_focus_and_accessible_state():
    html = Path("app/static/app.html").read_text(encoding="utf-8")

    assert '<button type="button" class="meal-h"' in html
    assert 'aria-expanded="false"' in html and 'aria-controls=' in html
    assert ":focus-visible" in html


def test_later_api_401_clears_user_state_and_returns_to_login(tmp_path):
    html = Path("app/static/app.html").read_text(encoding="utf-8")
    clear_match = re.search(r"function clearAuthenticatedState\(\) \{.*?\n\}", html, re.S)
    assert clear_match, "all login transitions must clear authenticated visible state"
    match = re.search(r"function handleApiUnauthorized\(response\) \{.*?\n\}", html, re.S)
    assert match, "the shared API wrapper must expose its 401 state transition"
    script = tmp_path / "api-401-contract.js"
    script.write_text(
        "var ME={id:7}, PLAN={tyzden:'2026-08-17'}, DONE={'0-0':true}, loginCalls=0;\n"
        + "var header={textContent:'cook'};\n"
        + "function $(selector){if(selector==='#hdr')return header;throw new Error(selector);}\n"
        + "function viewLogin(){loginCalls++;}\n"
        + clear_match.group(0)
        + "\n"
        + match.group(0)
        + "\nif (handleApiUnauthorized({status:500}) !== false) WScript.Quit(1);\n"
        + "if (handleApiUnauthorized({status:401}) !== true) WScript.Quit(2);\n"
        + "if (ME !== null || PLAN !== null || loginCalls !== 1 || header.textContent !== '') WScript.Quit(3);\n"
        + "for (var key in DONE) WScript.Quit(4);\n"
        + "WScript.Quit(0);\n",
        encoding="utf-8",
    )

    result = run_cscript(script)

    assert result.returncode == 0, result.stderr
    assert "handleApiUnauthorized(r)" in html
    assert "if (e.authRequired) return" in html
    assert "function viewLogin(sent, previousEmail) {\n  clearAuthenticatedState();" in html


def test_resend_control_unlocks_after_exact_cooldown_without_sleeping(tmp_path):
    html = Path("app/static/app.html").read_text(encoding="utf-8")
    match = re.search(r"function armAuthResend\(button, schedule\) \{.*?\n\}", html, re.S)
    assert match, "accepted-login UX must expose deterministic resend cooldown behavior"
    script = tmp_path / "auth-resend-contract.js"
    script.write_text(
        "var button={disabled:false,textContent:''}, delay=-1, callback=null;\n"
        + "function fakeSchedule(fn, milliseconds){callback=fn;delay=milliseconds;}\n"
        + match.group(0)
        + "\narmAuthResend(button,fakeSchedule);\n"
        + "if (!button.disabled || delay !== 60000) WScript.Quit(1);\n"
        + "callback();\n"
        + "if (button.disabled || button.textContent !== 'Poslať odkaz znova') WScript.Quit(2);\n"
        + "WScript.Quit(0);\n",
        encoding="utf-8",
    )

    result = run_cscript(script)

    assert result.returncode == 0, result.stderr


def test_login_copy_reports_provider_acceptance_expiry_and_retry_without_delivery_claim():
    html = Path("app/static/app.html").read_text(encoding="utf-8")

    assert "Poskytovateľ prijal žiadosť" in html
    assert "Odkaz platí 60 minút" in html
    assert "Poslali sme ti prihlasovací odkaz" not in html
    assert "$('#err').textContent = e.message" in html
    assert "Skús to znova" in html


def test_failed_plan_load_keeps_bottom_navigation_and_offers_settings_and_pantry():
    html = app_html()
    show_nav = declaration(html, "function showNav() ")
    assert "$('#nav').classList.remove('hidden')" in show_nav
    assert "showNav();" in declaration(html, "function render() ")

    load_plan = declaration(html, "async function nacitajPlan(gen) ")
    assert "catch" in load_plan
    failure = load_plan.split("catch", 1)[1]
    assert "showNav()" in failure, "a dead end without navigation is the whole bug"
    assert "Skúsiť znova" in failure
    assert "TAB = 'nastavenia'" in failure, "user must be able to reach Nastavenia to change stores"
    assert "TAB = 'spajza'" in failure, "user must be able to reach Špajza"


@needs_node
def test_guarded_action_blocks_double_submit_and_reports_failure_in_slovak(tmp_path):
    html = app_html()
    guard = declaration(html, "async function runGuardedAction(button, errorNode, action) ")
    result = run_node(
        tmp_path,
        "guarded-action-contract.js",
        guard
        + """
(async function () {
  var calls = 0;
  var errorNode = {textContent: ''};
  var busy = {disabled: true, textContent: 'Pracujem…'};
  if (await runGuardedAction(busy, errorNode, function () { calls++; }) !== false) process.exit(1);
  if (calls !== 0) process.exit(2);

  var button = {disabled: false, textContent: 'Hotovo'};
  var release = null;
  var running = runGuardedAction(button, errorNode, function () {
    return new Promise(function (resolve) { release = resolve; });
  });
  if (!button.disabled) process.exit(3);
  if (await runGuardedAction(button, errorNode, function () { calls++; }) !== false) process.exit(4);
  if (calls !== 0) process.exit(5);
  release();
  if (await running !== true) process.exit(6);

  button.disabled = false;
  button.textContent = 'Hotovo';
  var failed = await runGuardedAction(button, errorNode, function () {
    throw new Error('Server má problém. Skús to o chvíľu znova.');
  });
  if (failed !== false) process.exit(7);
  if (button.disabled) process.exit(8);
  if (button.textContent !== 'Hotovo') process.exit(9);
  if (errorNode.textContent !== 'Server má problém. Skús to o chvíľu znova.') process.exit(10);
  process.exit(0);
})();
""",
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "runGuardedAction($('#save')" in html, "onboarding submit must be guarded"
    assert "runGuardedAction($('#sp-save')" in html, "pantry save must be guarded"
    assert "runGuardedAction($('#odhl')" in html, "logout must be guarded"


def test_change_settings_prefills_current_profile_and_can_be_left_without_saving():
    html = app_html()
    onboarding = declaration(html, "function viewOnboarding() ")

    assert "profil.adults" in onboarding, "adult count must come from ME"
    assert "profil.children" in onboarding, "child count must come from ME"
    assert "profil.frekvencia" in onboarding
    assert "profil.obchody" in onboarding
    assert 'id="c-dospeli"' in onboarding
    assert 'id="c-deti"' in onboarding
    assert "Dospelí" in onboarding and "Deti" in onboarding
    assert "3–12" in onboarding and "tínedžera" in onboarding
    assert "v===frekvencia?' on':''" in onboarding
    assert "obchody.indexOf(o)>=0?' on':''" in onboarding

    assert "n===4?' on':''" not in onboarding, "4 people must not be hardcoded as selected"
    assert '<div class="chip on" data-v="2">Raz za 2 dni</div>' not in onboarding
    assert "`<div class=\"chip on\" data-v=\"${o}\">${o}</div>`" not in onboarding

    assert "Späť bez zmeny" in onboarding, "an existing profile must be leavable without submitting"
    assert "$('#spat').onclick" in onboarding


def test_profile_submit_sends_both_household_counts_and_rejects_zero_people():
    html = app_html()
    onboarding = declaration(html, "function viewOnboarding() ")
    payload = declaration(html, "function profilePayload(adults, children, frequency, stores, diet) ")

    assert "profilePayload(val('#c-dospeli'), val('#c-deti')" in onboarding
    assert "adults:+adults" in payload and "children:+children" in payload
    assert "payload.stravovanie = diet" in payload
    assert "diet !== undefined" in payload
    assert "adults + children" in onboarding
    assert "aspoň" in onboarding.casefold()


def test_recipe_ui_explains_composition_without_nutrition_claims():
    html = app_html()

    assert "dospel" in html.casefold() and "deti" in html.casefold()
    assert "kuchársky odhad" in html
    assert "nie individuálne výživové odporúčanie" in html
    assert "odporúčaná denná dávka" not in html.casefold()


@needs_node
def test_recipe_composition_keeps_the_number_of_days_in_the_batch(tmp_path):
    html = app_html()
    result = run_node(
        tmp_path,
        "household-batch-days.js",
        "var ME={adults:2,children:2,osoby:4};\n"
        + declaration(html, "function householdLabel(profil) ")
        + "\n"
        + declaration(html, "function portionLine(recept) ")
        + "\n"
        + "if (portionLine({porcie:12,dni:3,pre:'2 dospelí + 2 deti × 3 dni'}) "
        + "!== '12 porcií · 2 dospelí + 2 deti · na 3 dni') process.exit(1);\n",
    )

    assert result.returncode == 0, result.stdout + result.stderr


@needs_node
def test_checked_shopping_state_is_scoped_to_plan_identity_not_only_week(tmp_path):
    html = app_html()
    key = declaration(html, "function checkedStateKey(user, plan) ")
    result = run_node(
        tmp_path,
        "checked-state-plan-identity.js",
        key
        + """
var user = {id: 7, email: 'first@uvar.si'};
function plan(week, items) {
  return {tyzden: week, nakupny_zoznam: [{obchod: 'Lidl', polozky: items}]};
}
var first = plan('2026-08-17', [{nazov: 'Kuracie prsia'}, {nazov: 'Ryža'}]);
var same = plan('2026-08-17', [{nazov: 'Kuracie prsia'}, {nazov: 'Ryža'}]);
var regenerated = plan('2026-08-17', [{nazov: 'Bravčové karé'}, {nazov: 'Zemiaky'}]);
var nextWeek = plan('2026-08-24', [{nazov: 'Kuracie prsia'}, {nazov: 'Ryža'}]);
if (checkedStateKey(user, first) !== checkedStateKey(user, same)) process.exit(1);
if (checkedStateKey(user, first) === checkedStateKey(user, regenerated)) process.exit(2);
if (checkedStateKey(user, first) === checkedStateKey({id: 8}, first)) process.exit(3);
if (checkedStateKey(user, first) === checkedStateKey(user, nextWeek)) process.exit(4);
if (checkedStateKey(user, null) === checkedStateKey(user, first)) process.exit(5);
process.exit(0);
""",
    )

    assert result.returncode == 0, result.stdout + result.stderr


@needs_node
def test_api_failures_are_readable_slovak_including_validation_lists_and_offline(tmp_path):
    html = app_html()
    api_message = declaration(html, "function apiErrorMessage(status, payload) ")
    network_message = declaration(html, "function networkErrorMessage(online) ")
    result = run_node(
        tmp_path,
        "api-error-message-contract.js",
        api_message
        + "\n"
        + network_message
        + """
function readable(text) {
  return typeof text === 'string' && text.length > 12 &&
    text.indexOf('[object Object]') === -1 && text.indexOf('undefined') === -1 &&
    text !== 'Chyba' && /[.!?…]$/.test(text);
}
if (!readable(apiErrorMessage(500, null))) process.exit(1);
if (!readable(apiErrorMessage(503, {}))) process.exit(2);
var validation = apiErrorMessage(422, {detail: [{msg: 'e-mail nie je platný'}, {msg: 'chýba pole osoby'}]});
if (!readable(validation)) process.exit(3);
if (validation.indexOf('e-mail nie je platný') === -1) process.exit(4);
if (validation.indexOf('chýba pole osoby') === -1) process.exit(5);
if (!readable(apiErrorMessage(422, {detail: [{}]}))) process.exit(6);
if (apiErrorMessage(400, {detail: 'Nemáš dosť akcií na plán.'}) !== 'Nemáš dosť akcií na plán.') process.exit(7);
var offline = networkErrorMessage(false);
var unreachable = networkErrorMessage(true);
if (!readable(offline) || !readable(unreachable)) process.exit(8);
if (offline === unreachable) process.exit(9);
if (/[a-z]/.test(offline) && /failed to fetch/i.test(offline)) process.exit(10);
process.exit(0);
""",
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "|| 'Chyba'" not in html, "the bare word 'Chyba' is what a beta tester screenshots"
    assert "catch (networkError)" in declaration(html, "async function api(url, opts) ")
    assert "navigator.onLine" in html, "offline must be detected explicitly"
    assert "e.offline" in html, "startup must not report an offline device as logged out"


@needs_node
def test_plan_screen_states_which_week_the_prices_come_from(tmp_path):
    html = app_html()
    label = declaration(html, "function weekLabel(tyzden) ")
    result = run_node(
        tmp_path,
        "week-label-contract.js",
        label
        + """
if (weekLabel('2026-08-17') !== 'Týždeň od 17. 8. 2026') process.exit(1);
if (weekLabel('2026-12-07') !== 'Týždeň od 7. 12. 2026') process.exit(2);
if (typeof weekLabel('') !== 'string' || weekLabel('').length < 6) process.exit(3);
process.exit(0);
""",
    )

    assert result.returncode == 0, result.stdout + result.stderr
    plan_view = declaration(html, "function vPlan() ")
    assert "esc(weekLabel(PLAN.tyzden))" in plan_view, "the plan must say which week it is for"
    assert "letákov" in plan_view, "the promise is prices from this week's flyers"


def escape_helper(html):
    match = re.search(r"const esc = .*", html)
    assert match, "app must define esc()"
    return match.group(0)


def provenance_helpers(html):
    return "\n".join([
        escape_helper(html),
        declaration(html, "function isoDay(value) "),
        declaration(html, "function dayMonthLabel(iso) "),
        declaration(html, "function offerValidity(validTo, today) "),
        declaration(html, "function leafletLabel(item) "),
        declaration(html, "function sourceHref(url) "),
        declaration(html, "function ingredientProvenance(item, today) "),
        declaration(html, "function ingredientRow(item, today) "),
    ])


@needs_node
def test_every_meal_price_states_its_leaflet_page_validity_and_linkable_source(tmp_path):
    html = app_html()
    result = run_node(
        tmp_path,
        "ingredient-provenance-contract.js",
        provenance_helpers(html)
        + """
var TODAY = '2026-08-18';
var item = {offer_key: 'offer_a', nazov: 'Mlieko', obchod: 'Lidl', jednotka: '1 l', mnozstvo: 2,
  cena: '2,20', povodna: '3,00', zlava: '-27 %', source_url: 'https://letak.test/lidl/32',
  source_page: 3, valid_from: '2026-08-17', valid_to: '2026-08-23'};
var row = ingredientRow(item, TODAY);
if (row.indexOf('Mlieko') === -1) process.exit(1);
if (row.indexOf('Lidl') === -1) process.exit(2);
if (row.indexOf('strana 3') === -1) process.exit(3);
if (row.indexOf('Platí do 23. 8.') === -1) process.exit(4);
if (row.indexOf('href="https://letak.test/lidl/32"') === -1) process.exit(5);
if (row.indexOf('rel="noopener noreferrer nofollow"') === -1) process.exit(6);
if (row.indexOf('target="_blank"') === -1) process.exit(7);
if (row.indexOf('2,20 €') === -1) process.exit(8);
if (row.indexOf('undefined') !== -1) process.exit(9);
process.exit(0);
""",
    )

    assert result.returncode == 0, result.stdout + result.stderr
    plan_view = declaration(html, "function vPlan() ")
    assert "ingredientRow(" in plan_view, "the meal view must render the checkable ingredient row"


@needs_node
def test_last_valid_day_is_announced_and_an_expired_price_is_never_shown_as_current(tmp_path):
    html = app_html()
    result = run_node(
        tmp_path,
        "offer-validity-contract.js",
        provenance_helpers(html)
        + """
var TODAY = '2026-08-18';
var item = {offer_key: 'offer_a', nazov: 'Mlieko', obchod: 'Lidl', jednotka: '1 l', mnozstvo: 1,
  cena: '2,20', zlava: '-27 %', source_url: 'https://letak.test/lidl/32', source_page: 3,
  valid_from: '2026-08-17', valid_to: '2026-08-23'};
function withValidTo(value) {
  var copy = {}; for (var k in item) copy[k] = item[k]; copy.valid_to = value; return copy;
}
if (offerValidity('2026-08-18', TODAY).text !== 'Platí len dnes') process.exit(1);
if (offerValidity('2026-08-19', TODAY).text !== 'Platí do zajtra') process.exit(2);
if (offerValidity('2026-08-23', TODAY).text !== 'Platí do 23. 8.') process.exit(3);
if (offerValidity('2026-08-17', TODAY).stav !== 'neplatna') process.exit(4);
if (offerValidity(null, TODAY).text !== '') process.exit(5);
if (offerValidity('nezmysel', TODAY).text !== '') process.exit(6);

var today = ingredientRow(withValidTo('2026-08-18'), TODAY);
if (today.indexOf('Platí len dnes') === -1) process.exit(7);
var tomorrow = ingredientRow(withValidTo('2026-08-19'), TODAY);
if (tomorrow.indexOf('Platí do zajtra') === -1) process.exit(8);

var expired = ingredientRow(withValidTo('2026-08-17'), TODAY);
if (expired.indexOf('Cena už neplatí') === -1) process.exit(9);
if (expired.indexOf('ing--neplatna') === -1) process.exit(10);
if (expired.indexOf('Platí do') !== -1) process.exit(11);
process.exit(0);
""",
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert ".ing--neplatna" in html, "an expired price needs a visibly non-current style"


@needs_node
def test_provenance_degrades_safely_for_pantry_items_stale_plans_and_hostile_urls(tmp_path):
    html = app_html()
    result = run_node(
        tmp_path,
        "provenance-safety-contract.js",
        provenance_helpers(html)
        + """
var TODAY = '2026-08-18';
if (sourceHref('javascript:alert(1)') !== '') process.exit(1);
if (sourceHref('  https://letak.test/x  ') !== 'https://letak.test/x') process.exit(2);
if (sourceHref(null) !== '') process.exit(3);
var hostile = ingredientRow({offer_key: 'o', nazov: 'X', obchod: 'Lidl', cena: '1,00',
  source_url: 'javascript:alert(1)', source_page: 2, valid_to: '2026-08-23'}, TODAY);
if (hostile.indexOf('javascript:') !== -1) process.exit(4);
if (hostile.indexOf('strana 2') === -1) process.exit(5);

if (ingredientProvenance({spajza: 'soľ'}, TODAY) !== '') process.exit(6);
var pantry = ingredientRow({spajza: 'soľ'}, TODAY);
if (pantry.indexOf('zo špajze') === -1) process.exit(7);
if (pantry.indexOf('Platí') !== -1) process.exit(8);

var stale = ingredientRow({offer_key: 'o', nazov: 'Chlieb', obchod: 'Tesco', cena: '1,20'}, TODAY);
if (stale.indexOf('Chlieb') === -1) process.exit(9);
if (stale.indexOf('undefined') !== -1) process.exit(10);
if (stale.indexOf('null') !== -1) process.exit(11);
if (stale.indexOf('Platí') !== -1) process.exit(12);
if (stale.indexOf('href') !== -1) process.exit(13);
if (stale.indexOf('class="prov"') !== -1) process.exit(14);
if (ingredientProvenance({offer_key: 'o', nazov: 'Chlieb', obchod: 'Tesco'}, TODAY) !== '') process.exit(15);

var quoted = ingredientRow({offer_key: 'o', nazov: 'X', obchod: 'Lidl', cena: '1,00',
  source_url: 'https://letak.test/a"onmouseover="alert(1)', source_page: 1,
  valid_to: '2026-08-23'}, TODAY);
if (quoted.indexOf('onmouseover="') !== -1) process.exit(16);
process.exit(0);
""",
    )

    assert result.returncode == 0, result.stdout + result.stderr


def test_plan_screen_promises_only_what_the_leaflet_actually_proves():
    html = app_html()
    plan_view = declaration(html, "function vPlan() ")

    assert "over" in plan_view.lower(), "the plan must invite the user to check the price"
    for overstated in ("overené u obchodníka", "overujeme priamo v obchode", "garantujeme",
                       "nezávisle overené", "potvrdené obchodom"):
        assert overstated not in html, "the app must not claim more than reading a leaflet page"


# --------------------------------------------------------------- špajza (defekt 1)
def test_editing_the_pantry_saves_then_refetches_but_never_regenerates_the_plan():
    """Majiteľ: „pridám vajíčka a zrazu mi preskladá celý jedálniček bez vyzvania"."""
    html = app_html()
    pantry_view = declaration(html, "function vSpajza() ")
    pantry_save = declaration(html, "async function savePantryList(list) ")

    assert "savePantryList" in pantry_view, "the explicit save button must persist the draft"
    assert "/api/spajza" in pantry_save, "the pantry still has to save"
    assert "refreshPlanAfterPantrySave" in pantry_save
    assert "/api/plan/generuj" not in pantry_view
    assert "/api/plan/generuj" not in pantry_save
    assert "nacitajPlan(true)" not in pantry_view, (
        "a pantry edit must never trigger a paid regeneration as a side effect"
    )
    assert "vSpajza()" in pantry_view, "the pantry redraws itself, so the edit feels instant"
    plan_view = declaration(html, "function vPlan() ")
    assert "PLAN_NEEDS_REGEN" in plan_view
    assert "nacitajPlan(true)" in plan_view, "prázdny zastaraný plán potrebuje výslovné CTA"
    load_plan = declaration(html, "async function nacitajPlan(gen) ")
    assert "vyzaduje_akciu" in load_plan
    assert "PLAN_NEEDS_REGEN" in load_plan


@needs_node
def test_pantry_save_refresh_replaces_or_clears_the_visible_plan_without_generation(tmp_path):
    html = app_html()
    refresh = declaration(html, "async function refreshPlanAfterPantrySave() ")
    result = run_node(
        tmp_path,
        "pantry-save-refresh.js",
        """
var PLAN={old:true}, PLAN_NEEDS_REGEN=false, PLAN_CONTEXT_VERSION=0, calls=[], rendered=0;
function invalidatePlanState() { PLAN_CONTEXT_VERSION++; }
function setPlan(plan) { PLAN=plan; }
function render() { rendered++; }
async function api(path, options) {
  calls.push([path, options]);
  return globalThis.nextPlan;
}
""" + refresh + """
(async function () {
  globalThis.nextPlan={jedla:[{nazov:'Aktuálny'}],nakup_spolu:'8,00'};
  await refreshPlanAfterPantrySave();
  if (!PLAN.jedla || PLAN.jedla[0].nazov !== 'Aktuálny') process.exit(1);
  globalThis.nextPlan={prazdny:true};
  await refreshPlanAfterPantrySave();
  if (PLAN !== null) process.exit(2);
  if (PLAN_NEEDS_REGEN !== true) process.exit(7);
  if (calls.length !== 2 || calls.some(c => c[0] !== '/api/plan')) process.exit(3);
  if (calls.some(c => c[1] && c[1].method === 'POST')) process.exit(4);
  if (rendered < 2) process.exit(5);
  process.exit(0);
})().catch(function () { process.exit(6); });
""",
    )
    assert result.returncode == 0, result.stdout + result.stderr


@needs_node
def test_a_changed_pantry_is_only_a_dismissible_hint_with_an_explicit_recompute(tmp_path):
    html = app_html()
    differs = declaration(html, "function pantryName(value) ") + "\n" + declaration(
        html, "function pantryDiffers(planPantry, currentPantry) "
    )
    result = run_node(
        tmp_path,
        "pantry-hint-contract.js",
        differs
        + """
if (pantryDiffers(['ryža'], ['ryža']) !== false) process.exit(1);
if (pantryDiffers(['ryža', 'soľ'], ['soľ', 'ryža']) !== false) process.exit(2);
if (pantryDiffers([' Ryža '], ['ryža']) !== false) process.exit(3);
if (pantryDiffers(['ryža'], ['ryža', 'vajcia']) !== true) process.exit(4);
if (pantryDiffers(['ryža', 'vajcia'], ['ryža']) !== true) process.exit(5);
if (pantryDiffers([], ['vajcia']) !== true) process.exit(6);
if (pantryDiffers(undefined, ['vajcia']) !== false) process.exit(7);
if (pantryDiffers(null, []) !== false) process.exit(8);
process.exit(0);
""",
    )

    assert result.returncode == 0, result.stdout + result.stderr

    plan_view = declaration(html, "function vPlan() ")
    assert "pantryDiffers(" in plan_view, "the plan screen decides whether the hint is warranted"
    assert "Špajza sa zmenila" in plan_view, "the hint has to say what happened, calmly"
    # Špajza sama jedálniček nepreskladá — dopočíta sa len nad nákupným zoznamom.
    # Tlačidlo preto neponúka „prepočítaj", ale výslovné skladanie zo špajze.
    assert "Navrhni jedlá z toho, čo mám doma" in plan_view, (
        "cooking from the pantry needs its own explicit button"
    )
    assert "navrhniZoSpajze()" in plan_view
    assert "sp-hint-off" in plan_view, "the hint must be dismissible"
    assert "PANTRY_HINT_HIDDEN" in html, "a dismissed hint must stay dismissed"
    assert "$('#sp-hint-go').onclick" in plan_view
    assert "$('#sp-hint-off').onclick" in plan_view


# --------------------------------------------------------------- týždeň (defekt 2)
@needs_node
def test_valid_cooking_frequencies_fill_the_entire_week_without_free_days(tmp_path):
    """Pri platnej frekvencii 1/2/3 nesmie kalendár vydávať žiadny deň za voľno."""
    html = app_html()
    week_days = declaration(html, "function planWeekDays(plan, frequency) ")
    result = run_node(
        tmp_path,
        "week-days-contract.js",
        week_days
        + """
var DNI = ['PO', 'UT', 'ST', 'ŠT', 'PI', 'SO', 'NE'];
function plan(days) {
  return {jedla: days.map(function (d) { return {den: d, nazov: 'Jedlo ' + d}; })};
}
function covered(rows) {
  if (rows.length !== 7) return false;
  for (var i = 0; i < 7; i++) {
    if (rows[i].den !== DNI[i]) return false;
    if (['jedlo', 'zvysok', 'volno'].indexOf(rows[i].typ) === -1) return false;
  }
  return true;
}
function exact(days, frequency, types, sources) {
  var rows = planWeekDays(plan(days), frequency);
  if (!covered(rows)) return false;
  for (var i = 0; i < 7; i++) {
    if (rows[i].typ !== types[i] || rows[i].typ === 'volno') return false;
    if (sources[i] && rows[i].zdroj !== sources[i]) return false;
  }
  return true;
}
if (!exact(['PO', 'UT', 'ST', 'ŠT', 'PI', 'SO', 'NE'], 1,
  ['jedlo','jedlo','jedlo','jedlo','jedlo','jedlo','jedlo'], [])) process.exit(1);
if (!exact(['PO', 'ST', 'PI', 'NE'], 2,
  ['jedlo','zvysok','jedlo','zvysok','jedlo','zvysok','jedlo'],
  [null,'PO',null,'ST',null,'PI',null])) process.exit(2);
if (!exact(['PO', 'ŠT', 'NE'], 3,
  ['jedlo','zvysok','zvysok','jedlo','zvysok','zvysok','jedlo'],
  [null,'PO','PO',null,'ŠT','ŠT',null])) process.exit(3);
process.exit(0);
""",
    )

    assert result.returncode == 0, result.stdout + result.stderr

    plan_view = declaration(html, "function vPlan() ")
    assert "planWeekDays(" in plan_view, "the week must be derived from the plan, not guessed"
    assert "voľno" in plan_view, "neplatný alebo neúplný plán sa stále musí priznať"


@needs_node
def test_settings_use_natural_slovak_cooking_frequency_text(tmp_path):
    """Frekvencia 1 nie je „raz za 1 dni“; všetky tri voľby majú čitateľné znenie."""
    html = app_html()
    result = run_node(
        tmp_path,
        "settings-frequency-copy.js",
        "var M={innerHTML:''}; function $(selector){return {onclick:null};}\n"
        + "function viewOnboarding() {}\n"
        + "function esc(value){return String(value == null ? '' : value);}\n"
        + "function passwordSetupCard(){return '';} function authV3Enabled(){return false;}\n"
        + "var localStorage={}; function loadAccountSecurity(){};\n"
        + declaration(html, "function householdLabel(profil) ")
        + "\n"
        + declaration(html, "function vNast() ")
        + """
function visible(html) { return html.replace(/<[^>]*>/g, ' ').replace(/\\s+/g, ' ').trim(); }
function settingsText(frequency) {
  ME = {email:'cook@example.test', osoby:4, frekvencia:frequency, obchody:['Lidl']};
  M.innerHTML = '';
  vNast();
  return visible(M.innerHTML);
}
var daily = settingsText(1);
var second = settingsText(2);
var third = settingsText(3);
if (daily.indexOf('Varím každý deň') === -1 || daily.indexOf('Varím raz za 1 dni') !== -1) process.exit(1);
if (second.indexOf('Varím raz za 2 dni') === -1) process.exit(2);
if (third.indexOf('Varím raz za 3 dni') === -1) process.exit(3);
process.exit(0);
""",
    )

    assert result.returncode == 0, result.stdout + result.stderr


@needs_node
def test_leftover_days_name_the_day_the_food_was_cooked_in_natural_slovak(tmp_path):
    html = app_html()
    result = run_node(
        tmp_path,
        "day-genitive-contract.js",
        declaration(html, "function dayGenitive(day) ")
        + """
var expected = {PO: 'pondelka', UT: 'utorka', ST: 'stredy', 'ŠT': 'štvrtka',
  PI: 'piatku', SO: 'soboty', NE: 'nedele'};
for (var day in expected) if (dayGenitive(day) !== expected[day]) process.exit(1);
if (dayGenitive(null).length < 3) process.exit(2);
if (dayGenitive('XX').indexOf('undefined') !== -1) process.exit(3);
process.exit(0);
""",
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "dayGenitive(" in declaration(html, "function vPlan() ")


# ------------------------------------------------------- hybridný prechod klienta (Task 5)
@needs_node
def test_immediate_plan_response_renders_without_preparing_or_polling(tmp_path):
    """Ak sa 200 plán omylom spracuje ako ack, UI zostane v nekonečnom čakaní."""
    html = app_html()
    functions = "\n".join(
        declaration(html, signature)
        for signature in (
            "function onePlanRequest(request) ",
            "function requestPlan(kind, url) ",
            "function generujPlan() ",
            "async function nacitajPlan(gen) ",
        )
    )
    result = run_node(
        tmp_path,
        "immediate-plan-contract.js",
        """
var immediate={jedla:[{den:'Po',nazov:'Polievka'}],tyzden:'2026-08-31'};
var PLAN_REQUEST_IN_FLIGHT=null, PLAN_PREPARATION=null, PLAN_FAILURE=null;
var PLAN_CONTEXT_VERSION=0, PLAN=null, M={innerHTML:''};
var calls=[], preparationCalls=0, renderCalls=0;
function api(url, options) { calls.push({url:url,options:options}); return Promise.resolve(immediate); }
function setPlanPreparation() { preparationCalls++; throw new Error('200 plan entered preparing'); }
function setPlan(plan) { PLAN=plan; }
function render() { renderCalls++; }
function takePlanPrefetch() { return null; }
function readStartupResponse(pending) { return pending; }
function loadingSkeletonHtml() { return 'loading'; }
"""
        + functions
        + """
(async function() {
  await nacitajPlan(true);
  if (PLAN !== immediate) throw new Error('immediate plan was not stored');
  if (preparationCalls !== 0 || PLAN_PREPARATION !== null) throw new Error('preparing state leaked');
  if (calls.length !== 1 || calls[0].url !== '/api/plan/generuj') throw new Error('wrong request count');
  if (!calls[0].options || calls[0].options.method !== 'POST') throw new Error('generation was not POST');
  if (renderCalls !== 1) throw new Error('immediate plan did not render exactly once');
})().catch(function(error) { console.error(error.stack || error); process.exit(1); });
""",
    )
    assert result.returncode == 0, result.stdout + result.stderr


@needs_node
def test_immediate_pantry_and_force_plans_render_before_profile_refresh(tmp_path):
    """A hanging auxiliary /api/me must not hide an already complete plan."""
    html = app_html()
    functions = "\n".join(
        declaration(html, signature)
        for signature in (
            "async function navrhniZoSpajze() ",
            "async function novyPlan() ",
        )
    )
    result = run_node(
        tmp_path,
        "immediate-plan-before-profile-contract.js",
        """
var pantryPlan={jedla:[{nazov:'Zo spajze'}]}, forcePlan={jedla:[{nazov:'Novy'}]};
var PLAN={jedla:[{nazov:'Povodny'}]}, ME={}, PLAN_CONTEXT_VERSION=0, PLAN_NOTE='';
var PANTRY_HINT_HIDDEN=false, TAB='plan', renders=0, profileResolvers=[];
function regenerationsLeft() { return 1; }
function planZoSpajze() { return Promise.resolve(pantryPlan); }
function preskladajPlan() { return Promise.resolve(forcePlan); }
function api(url) {
  if (url !== '/api/me') throw new Error('unexpected auxiliary request');
  return new Promise(function(resolve) { profileResolvers.push(resolve); });
}
function setPlan(plan) { PLAN=plan; }
function render() { renders++; }
function setPlanFailure() { throw new Error('immediate plan entered failure'); }
function failPlan() { throw new Error('immediate plan entered catch path'); }
"""
        + functions
        + """
(async function() {
  var pantryPending=navrhniZoSpajze();
  await Promise.resolve(); await Promise.resolve();
  if (PLAN !== pantryPlan || renders < 1) throw new Error('pantry plan waited for /api/me');
  profileResolvers.shift()({id:'pantry-user'});
  await pantryPending;

  renders=0;
  var forcePending=novyPlan();
  await Promise.resolve(); await Promise.resolve();
  if (PLAN !== forcePlan || renders < 1) throw new Error('force plan waited for /api/me');
  profileResolvers.shift()({id:'force-user'});
  await forcePending;
})().catch(function(error) { console.error(error.stack || error); process.exit(1); });
""",
    )
    assert result.returncode == 0, result.stdout + result.stderr


@needs_node
def test_legacy_preparing_response_starts_get_polling(tmp_path):
    """Rollback na off/shadow musí stále prijať 202 ack a pokračovať iba cez GET."""
    html = app_html()
    functions = "\n".join(
        declaration(html, signature)
        for signature in (
            "function onePlanRequest(request) ",
            "function requestPlan(kind, url) ",
            "function stopPlanPolling() ",
            "function startPlanPolling() ",
            "function setPlanPreparation(response) ",
            "function generujPlan() ",
            "async function nacitajPlan(gen) ",
        )
    )
    result = run_node(
        tmp_path,
        "legacy-preparing-contract.js",
        """
var ack={status:'preparing',job_id:'legacy-7'};
var document={visibilityState:'visible'}, timers=[], calls=[];
var PLAN_REQUEST_IN_FLIGHT=null, PLAN_PREPARATION=null, PLAN_FAILURE=null;
var PLAN_POLL_TIMER=null, PLAN_CONTEXT_VERSION=0, PLAN_NOTE='', PLAN=null;
var M={innerHTML:''};
function setTimeout(fn, milliseconds) { timers.push({fn:fn,milliseconds:milliseconds}); return timers.length; }
function clearTimeout() {}
function pollPlanStatus() {}
function api(url, options) { calls.push({url:url,options:options}); return Promise.resolve(ack); }
function setPlan(plan) { PLAN=plan; }
function render() {}
function takePlanPrefetch() { return null; }
function readStartupResponse(pending) { return pending; }
function loadingSkeletonHtml() { return 'loading'; }
"""
        + functions
        + """
(async function() {
  await nacitajPlan(true);
  if (!PLAN_PREPARATION || PLAN_PREPARATION.jobId !== 'legacy-7') throw new Error('legacy ack was not kept');
  if (PLAN !== null) throw new Error('legacy ack replaced the plan object');
  if (timers.length !== 1 || timers[0].milliseconds !== 4000) throw new Error('GET poll was not scheduled');
  if (calls.length !== 1 || calls[0].url !== '/api/plan/generuj') throw new Error('duplicate POST');
})().catch(function(error) { console.error(error.stack || error); process.exit(1); });
""",
    )
    assert result.returncode == 0, result.stdout + result.stderr


@needs_node
def test_typed_deterministic_failure_renders_safe_actions_without_retry(tmp_path):
    """Strata navrhov alebo retry CTA pri retry_allowed=false zavádza používateľa."""
    html = app_html()
    functions = "\n".join(
        declaration(html, signature)
        for signature in (
            "function apiErrorMessage(status, payload) ",
            "async function readApiResponse(r) ",
            "function onePlanRequest(request) ",
            "function requestPlan(kind, url) ",
            "function generujPlan() ",
            "function setPlanFailure(response, kind, version) ",
            "function failPlan(e, kind, version) ",
            "function planFailureHtml() ",
            "async function nacitajPlan(gen) ",
        )
    )
    result = run_node(
        tmp_path,
        "deterministic-plan-failure-contract.js",
        """
var payload={detail:'Pre zvolený spôsob stravovania nemáme v aktuálnych akciách dosť vhodných surovín.',
  kod:'diet_too_strict',retry_allowed:false,navrhy:[
    {kod:'add_store',text:'Pridaj ďalší obchod.'},
    {kod:'use_standard_mode',text:'Skús štandardný režim stravovania.'}]};
var response={ok:false,status:422,json:function(){return Promise.resolve(payload);}};
var PLAN_REQUEST_IN_FLIGHT=null, PLAN_PREPARATION=null, PLAN_FAILURE=null;
var PLAN_POLL_TIMER=null, PLAN_CONTEXT_VERSION=0, PLAN_NOTE='', PLAN=null;
var M={innerHTML:''}, renderCalls=0;
function handleApiUnauthorized() { return false; }
function api() { return readApiResponse(response); }
function stopPlanPolling() {}
function setPlanPreparation() { throw new Error('typed failure entered preparing'); }
function setPlan(plan) { PLAN=plan; }
function esc(value) { return String(value == null ? '' : value).replace(/&/g,'&amp;').replace(/</g,'&lt;'); }
function render() { renderCalls++; M.innerHTML=planFailureHtml(); }
function takePlanPrefetch() { return null; }
function readStartupResponse(pending) { return pending; }
function loadingSkeletonHtml() { return 'loading'; }
function showNav() {}
function $(selector) { return {}; }
"""
        + functions
        + """
(async function() {
  await nacitajPlan(true);
  if (!PLAN_FAILURE) throw new Error('typed failure did not enter failure state');
  if (PLAN_FAILURE.message !== payload.detail) throw new Error('safe server message changed');
  if (PLAN_FAILURE.retry_allowed !== false) throw new Error('non-retryable failure became retryable');
  if (renderCalls !== 1) throw new Error('failure did not render once');
  if (M.innerHTML.indexOf(payload.detail) === -1) throw new Error('safe message is missing');
  for (var i=0;i<payload.navrhy.length;i++)
    if (M.innerHTML.indexOf(payload.navrhy[i].text) === -1) throw new Error('actionable suggestion is missing');
  if (M.innerHTML.indexOf('Skúsiť znova') !== -1 || M.innerHTML.indexOf('plan-retry') !== -1)
    throw new Error('non-retryable failure encourages a blind retry');
})().catch(function(error) { console.error(error.stack || error); process.exit(1); });
""",
    )
    assert result.returncode == 0, result.stdout + result.stderr


@needs_node
def test_malformed_failure_suggestions_preserve_safe_server_message(tmp_path):
    """A non-array navrhy field must not replace the safe detail with a TypeError."""
    html = app_html()
    functions = "\n".join(
        declaration(html, signature)
        for signature in (
            "function apiErrorMessage(status, payload) ",
            "async function readApiResponse(r) ",
        )
    )
    result = run_node(
        tmp_path,
        "malformed-plan-suggestions-contract.js",
        """
var payload={detail:'Bezpecna sprava.',retry_allowed:false,navrhy:{text:'not-an-array'}};
var response={ok:false,status:422,json:function(){return Promise.resolve(payload);}};
function handleApiUnauthorized() { return false; }
"""
        + functions
        + """
(async function() {
  try { await readApiResponse(response); }
  catch (error) {
    if (error.message !== payload.detail) throw new Error('safe message was masked: '+error.message);
    if (!Array.isArray(error.suggestions) || error.suggestions.length)
      throw new Error('malformed suggestions were not ignored');
    return;
  }
  throw new Error('failure response resolved');
})().catch(function(error) { console.error(error.stack || error); process.exit(1); });
""",
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_normal_plan_actions_do_not_promise_background_or_long_wait():
    html = app_html()
    plan_view = declaration(html, "function vPlan() ")
    pantry_view = declaration(html, "function vSpajza() ")
    normal_copy = plan_view + pantry_view

    assert "pripraví na pozadí" not in normal_copy.lower()
    assert "pripraví sa na pozadí" not in normal_copy.lower()
    assert "dve minúty" not in normal_copy.lower()
    assert "2 min" not in normal_copy.lower()
    assert "120 sek" not in normal_copy.lower()


@needs_node
def test_profile_and_pantry_refresh_keep_plan_while_legacy_ack_is_pending(tmp_path):
    """Ack počas refreshu nesmie zahodiť synchronný plán a vyvolať druhý POST."""
    html = app_html()
    functions = "\n".join(
        declaration(html, signature)
        for signature in (
            "async function refreshPlanAfterPantrySave() ",
            "async function refreshPlanAfterProfileSave() ",
        )
    )
    result = run_node(
        tmp_path,
        "refresh-keeps-synchronous-plan-contract.js",
        """
var existing={jedla:[{den:'Po'}],tyzden:'2026-08-31'};
var PLAN=existing, PLAN_CONTEXT_VERSION=0, PLAN_PREPARATION=null, PLAN_FAILURE=null;
var PLAN_NEEDS_REGEN=false, PLAN_RESUME_ENDPOINT='', PLAN_EMPTY_REASON='', TAB='plan';
var calls=[], renders=0;
function invalidatePlanState() { PLAN_CONTEXT_VERSION++; PLAN_PREPARATION=null; PLAN_FAILURE=null; }
function api(url) { calls.push(url); return Promise.resolve({status:'preparing',job_id:'rollback'}); }
function setPlanPreparation(response, kind, version) {
  PLAN_PREPARATION={response:response,jobId:response.job_id,kind:kind,version:version}; return true;
}
function setPlanFailure() { throw new Error('preparing ack entered failure'); }
function setPlan(plan) { PLAN=plan; }
function render() { renders++; }
"""
        + functions
        + """
(async function() {
  await refreshPlanAfterPantrySave();
  if (PLAN !== existing || !PLAN_PREPARATION) throw new Error('pantry refresh discarded the synchronous plan');
  PLAN_PREPARATION=null;
  await refreshPlanAfterProfileSave();
  if (PLAN !== existing || !PLAN_PREPARATION) throw new Error('profile refresh discarded the synchronous plan');
  if (calls.length !== 2 || calls[0] !== '/api/plan' || calls[1] !== '/api/plan')
    throw new Error('refresh issued duplicate or non-GET requests');
  if (renders !== 2) throw new Error('refresh did not render exactly once per response');
})().catch(function(error) { console.error(error.stack || error); process.exit(1); });
""",
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_pending_plan_keeps_navigation_available_and_polls_get_only():
    html = app_html()
    pending = declaration(html, "function setPlanPreparation(response) ")
    polling = declaration(html, "async function pollPlanStatus() ")
    starter = declaration(html, "function startPlanPolling() ")

    assert "Plán pripravujeme. Pokojne pokračuj inde." in html
    assert "startPlanPolling()" in pending
    assert "api('/api/plan')" in polling
    assert "/api/plan/generuj" not in polling
    assert "method:'POST'" not in polling
    assert "4000" in starter
    assert "visibilityState" in starter
    assert "setNavigationReady(false)" not in pending


@needs_node
def test_plan_polling_survives_context_invalidation_while_an_old_get_is_in_flight(tmp_path):
    html = app_html()
    functions = "\n".join(
        declaration(html, signature)
        for signature in (
            "function stopPlanPolling() ",
            "function startPlanPolling() ",
            "function setPlanPreparation(response) ",
            "function invalidatePlanState() ",
            "function currentPlanPreparation(preparation, version) ",
            "async function pollPlanStatus() ",
        )
    )
    result = run_node(
        tmp_path,
        "plan-polling-context-race-contract.js",
        """
var document = {visibilityState:'visible'};
var timers = [], nextTimerId = 0, calls = [], renderCalls = 0;
var PLAN_PREPARATION = null, PLAN_FAILURE = null, PLAN_POLL_TIMER = null;
var PLAN_POLL_IN_FLIGHT = false, PLAN_CONTEXT_VERSION = 0, PLAN_NOTE = '';
var oldGet = deferred(), newGet = deferred();
var responses = [oldGet, newGet];
function deferred() {
  var resolve;
  var promise = new Promise(function(done) { resolve = done; });
  return {promise:promise, resolve:resolve};
}
function setTimeout(fn, milliseconds) {
  var timer = {id:++nextTimerId, fn:fn, milliseconds:milliseconds,
    fired:false, cleared:false};
  timers.push(timer);
  return timer.id;
}
function clearTimeout(id) {
  for (var i = 0; i < timers.length; i++)
    if (timers[i].id === id) timers[i].cleared = true;
}
function fireNextTimer() {
  for (var i = 0; i < timers.length; i++) {
    if (!timers[i].fired && !timers[i].cleared) {
      timers[i].fired = true;
      timers[i].fn();
      return timers[i];
    }
  }
  throw new Error('no active timer');
}
function activeTimerCount() {
  return timers.filter(function(timer) { return !timer.fired && !timer.cleared; }).length;
}
function api(url) {
  calls.push(url);
  var request = responses.shift();
  if (!request) return Promise.reject(new Error('unexpected GET'));
  return request.promise;
}
function render() { renderCalls += 1; }
"""
        + functions
        + """
(async function() {
  setPlanPreparation({status:'preparing', job_id:'old'}, 'regular');
  fireNextTimer();
  if (calls.length !== 1 || calls[0] !== '/api/plan') throw new Error('old GET did not start');

  invalidatePlanState();
  setPlanPreparation({status:'preparing', job_id:'new'}, 'regular');
  fireNextTimer();
  if (calls.length !== 1) throw new Error('new timer duplicated the old in-flight GET');

  oldGet.resolve({status:'ready', jedla:[{den:'stale'}]});
  await Promise.resolve();
  await Promise.resolve();
  if (renderCalls !== 0) throw new Error('stale response rendered');
  if (activeTimerCount() !== 1) throw new Error('new context was left without a follow-up timer');

  fireNextTimer();
  if (calls.length !== 2 || calls[1] !== '/api/plan') throw new Error('new poll did not continue');
  newGet.resolve({status:'preparing'});
  await Promise.resolve();
  await Promise.resolve();
  if (activeTimerCount() !== 1) throw new Error('new preparing poll did not reschedule');
})().catch(function(error) { console.error(error.stack || error); process.exit(1); });
""",
    )

    assert result.returncode == 0, result.stdout + result.stderr


def test_terminal_plan_failure_shows_server_message_and_an_explicit_retry():
    html = app_html()
    failure = declaration(html, "function setPlanFailure(response, kind, version) ")
    plan_view = declaration(html, "function vPlan() ")
    retry = declaration(html, "async function retryPlanPreparation() ")

    assert "response.message" in failure
    assert "Skúsiť znova" in html
    assert "plan-retry" in html
    assert "PLAN_FAILURE" in plan_view
    assert "retryPlanPreparation()" in plan_view
    assert "PLAN_FAILURE" in retry


def test_startup_accepts_a_preparing_plan_and_profile_refresh_invalidates_stale_work():
    html = app_html()
    startup = declaration(html, "async function nacitajPlan(gen) ")
    request = declaration(html, "function requestPlan(kind, url) ")
    profile = declaration(html, "async function refreshPlanAfterProfileSave() ")
    pantry = declaration(html, "async function refreshPlanAfterPantrySave() ")

    assert "status === 'preparing'" in startup
    assert "setPlanPreparation" in startup
    assert "PLAN_CONTEXT_VERSION" in request
    assert "version !== PLAN_CONTEXT_VERSION" in request
    assert "invalidatePlanState()" in profile
    assert "invalidatePlanState()" in pantry


# ------------------------------------------- špajza je Premium (majiteľ, 21. 8.)
# „zamknuté, že je v premium a nejaký náhľad, že ako to funguje“
#
# Zámok musí byť vidieť skôr, než človek čokoľvek napíše, a ukážka musí byť
# konkrétna: nie prázdny rámček, ale tie isté tri suroviny, ktoré vypadnú
# z nákupného zoznamu, a súčet, ktorý o ne klesne.
@needs_node
def test_the_pantry_lock_follows_the_server_and_falls_back_on_an_older_server(tmp_path):
    """O nároku rozhoduje server. Staršia verzia servera pole nemá — vtedy premium."""
    html = app_html()
    result = run_node(
        tmp_path,
        "pantry-unlocked-contract.js",
        declaration(html, "function pantryUnlocked(me) ")
        + """
if (pantryUnlocked({spajza_dostupna: true, premium: false}) !== true) process.exit(1);
if (pantryUnlocked({spajza_dostupna: false, premium: true}) !== false) process.exit(2);
if (pantryUnlocked({spajza_premium: true, premium: false}) !== true) process.exit(3);
if (pantryUnlocked({spajza_premium: false, premium: true}) !== false) process.exit(4);
if (pantryUnlocked({premium: true}) !== true) process.exit(5);
if (pantryUnlocked({premium: false}) !== false) process.exit(6);
if (pantryUnlocked({}) !== false) process.exit(7);
if (pantryUnlocked(null) !== false) process.exit(8);
if (pantryUnlocked(undefined) !== false) process.exit(9);
if (pantryUnlocked({premium: 1}) !== true) process.exit(10);
process.exit(0);
""",
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "pantryUnlocked(ME)" in declaration(html, "function vSpajza() "), (
        "the pantry screen has to ask the server answer, not guess"
    )
    assert "pantryUnlocked(ME)" in declaration(html, "function vPlan() "), (
        "the recompute hint belongs to whoever actually owns a pantry"
    )


def eur(text):
    """'24,90' -> 2490 centov. V centoch, aby sa sčítanie nedalo pokaziť floatom."""
    cele, _, halier = text.partition(",")
    return int(cele) * 100 + int(halier.ljust(2, "0"))


def test_the_locked_preview_shows_the_shopping_total_dropping_by_what_is_at_home():
    """Ukážka musí byť merateľná — pred, po, rozdiel — a čísla musia sedieť.

    Čísla sú v stránke napísané staticky (je to ukážka, nie jeho nákup), takže
    ich nestráži žiadny výpočet za behu. Stráži ich tento test: keby niekto
    prepísal cenu ryže a zabudol na súčet, ukážka by klamala.
    """
    html = app_html()
    zamknuta = declaration(html, "function vSpajzaZamknuta() ")
    ceny = dict(re.findall(r"'([^']+)': '(\d+,\d\d)'",
                           re.search(r"const SPAJZA_UKAZKA_CENY = \{([^}]*)\};", html).group(1)))
    suma = re.search(r"(\d+,\d\d) € → (\d+,\d\d) €", zamknuta)
    klesne = re.search(r"<b>(\d+,\d\d) €</b>", zamknuta)

    assert ceny, "sample prices belong in one named place"
    assert suma, "before and after have to be readable at a glance"
    assert klesne, "the drop itself is the number worth showing big"
    for surovina, cena in ceny.items():
        assert f"{cena} € · máš doma" in zamknuta or "SPAJZA_UKAZKA_CENY[item]" in zamknuta, (
            f"the removed item {surovina} must show what it would have cost"
        )

    pred, po = eur(suma.group(1)), eur(suma.group(2))
    assert sum(eur(c) for c in ceny.values()) == eur(klesne.group(1)), (
        "the drop must be exactly the sample pantry, not a nicer number"
    )
    assert pred - eur(klesne.group(1)) == po, "before minus the drop has to be after"
    assert 0 < po < pred, "a pantry lowers the bill, it does not empty it"


def test_the_locked_pantry_says_it_saves_money_and_food_without_pushing():
    """Konkrétne a poctivo: nekúpiš druhýkrát to, čo máš, a menej vyhodíš."""
    zamknuta = declaration(app_html(), "function vSpajzaZamknuta() ")

    assert "máš doma" in zamknuta
    assert "nákup" in zamknuta.casefold(), "the shopping list is where the money shows up"
    assert re.search(r"nekúpi|nekupuj", zamknuta.casefold()), (
        "say plainly that you stop buying it twice"
    )
    assert re.search(r"koš|nevyhod|menej vyhod", zamknuta.casefold()), (
        "the second benefit is less waste"
    )
    assert "zdarma" not in zamknuta.casefold() or "Premium" in zamknuta


def test_the_locked_pantry_is_locked_before_the_first_keystroke_not_after_saving():
    """Bait and switch je nechať človeka písať a odmietnuť ho až pri ukladaní."""
    zamknuta = declaration(app_html(), "function vSpajzaZamknuta() ")

    inputs = re.findall(r"<input[^>]*>", zamknuta)
    assert inputs, "the locked screen still shows the field, so the feature is understandable"
    for field in inputs:
        assert "disabled" in field, "every field on the locked screen is visibly locked"
    assert "sp-add" not in zamknuta and "sp-new" not in zamknuta, (
        "no editing wiring may exist on the locked screen"
    )
    assert "/api/spajza" not in zamknuta, "nothing on the locked screen may try to save"
    assert "zamknut" in zamknuta.casefold(), "the lock has to be said out loud, in Slovak"


# --------------------------------------------- denný strop prepočtov v rozhraní
@needs_node
def test_remaining_regenerations_are_unknown_on_an_older_server_and_never_guessed(tmp_path):
    html = app_html()
    result = run_node(
        tmp_path,
        "regenerations-left-contract.js",
        declaration(html, "function regenerationsLeft(me) ")
        + """
if (regenerationsLeft({zostava_prepoctov: 3}) !== 3) process.exit(1);
if (regenerationsLeft({zostava_prepoctov: 0}) !== 0) process.exit(2);
if (regenerationsLeft({zostava_prepoctov: -4}) !== 0) process.exit(3);
if (regenerationsLeft({}) !== null) process.exit(4);
if (regenerationsLeft(null) !== null) process.exit(5);
if (regenerationsLeft(undefined) !== null) process.exit(6);
if (regenerationsLeft({zostava_prepoctov: 'veľa'}) !== null) process.exit(7);
if (regenerationsLeft({limit_prepoctov: 5}) !== null) process.exit(8);
process.exit(0);
""",
    )

    assert result.returncode == 0, result.stdout + result.stderr


@needs_node
def test_an_exhausted_daily_limit_says_in_slovak_when_it_resets(tmp_path):
    html = app_html()
    result = run_node(
        tmp_path,
        "regeneration-blocked-contract.js",
        declaration(html, "function nextDayLabel(today) ")
        + "\n"
        + declaration(html, "function isoDay(value) ")
        + "\n"
        + declaration(html, "function regenerationBlockedNote(me, today) ")
        + """
function bad(text) {
  return typeof text !== 'string' || text.length < 20 ||
    text.indexOf('undefined') !== -1 || text.indexOf('NaN') !== -1;
}
var zdarma = regenerationBlockedNote(
  {limit_prepoctov: 1, zostava_prepoctov: 0, premium: false}, '2026-08-21');
if (bad(zdarma)) process.exit(1);
if (zdarma.indexOf('zajtra 22. 8.') === -1) process.exit(2);
if (zdarma.indexOf('po polnoci') === -1) process.exit(3);
if (zdarma.indexOf('Premium') === -1) process.exit(4);

var platene = regenerationBlockedNote(
  {limit_prepoctov: 5, zostava_prepoctov: 0, premium: true}, '2026-08-21');
if (bad(platene)) process.exit(5);
if (platene.indexOf('5') === -1) process.exit(6);
if (platene.indexOf('Premium') !== -1) process.exit(7);

var mesiac = regenerationBlockedNote({limit_prepoctov: 1, premium: true}, '2026-08-31');
if (mesiac.indexOf('zajtra 1. 9.') === -1) process.exit(8);
var rok = regenerationBlockedNote({limit_prepoctov: 1, premium: true}, '2026-12-31');
if (rok.indexOf('zajtra 1. 1.') === -1) process.exit(9);

var neznamy = regenerationBlockedNote({}, '');
if (bad(neznamy) || neznamy.indexOf('zajtra') === -1) process.exit(10);
if (bad(regenerationBlockedNote(null, null))) process.exit(11);
process.exit(0);
""",
    )

    assert result.returncode == 0, result.stdout + result.stderr


def test_the_plan_screen_drops_the_regeneration_button_once_the_limit_is_spent():
    """Vyčerpaný strop nesmie byť tlačidlo, ktoré vedie do chyby."""
    plan_view = declaration(app_html(), "function vPlan() ")
    branches = re.search(
        r"if \(zostava === 0\) \{(.*?)\n *\} else \{(.*?)\n *\}", plan_view, re.S
    )

    assert branches, "the plan screen has to branch on the remaining daily attempts"
    vycerpane, zostava = branches.group(1), branches.group(2)
    assert "<button" not in vycerpane, "a spent limit shows a sentence, never a dead button"
    assert "Chcem iný plán" not in vycerpane
    assert "regenerationBlockedNote(" in vycerpane, "it must say when it resets"
    assert "Chcem iný plán" in zostava and "novyPlan()" in zostava
    assert "regenerationNote(ME)" in zostava, "before the click, say honestly how many are left"
    assert "regenerationsLeft(ME)" in plan_view


def test_a_spent_daily_limit_never_reaches_the_paid_endpoint():
    html = app_html()
    novy = declaration(html, "async function novyPlan() ")
    guard = novy.find("regenerationsLeft(ME) === 0")
    work = novy.find("preskladajPlan()")

    assert guard != -1, "the click has to be stopped before it costs anything"
    assert work != -1 and guard < work, "the guard belongs before the paid call"
    assert "regenerationBlockedNote(" in novy, "the refusal explains itself in Slovak"
    assert "PLAN = null" not in novy, "the plan on screen survives a refused recompute"
