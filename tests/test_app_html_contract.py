import re
import shutil
import subprocess
from pathlib import Path

import pytest


CSCRIPT = Path("C:/Windows/System32/cscript.exe")
NODE = shutil.which("node")
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

    result = subprocess.run([str(CSCRIPT), "//nologo", str(script)], capture_output=True, text=True)

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

    result = subprocess.run([str(CSCRIPT), "//nologo", str(script)], capture_output=True, text=True)

    assert result.returncode == 0, result.stderr
    assert "esc(shoppingQuantityLabel(p))" in html


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

    result = subprocess.run([str(CSCRIPT), "//nologo", str(script)], capture_output=True, text=True)

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

    result = subprocess.run([str(CSCRIPT), "//nologo", str(script)], capture_output=True, text=True)

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
    assert "runGuardedAction($('#sp-add')" in html, "pantry save must be guarded"
    assert "runGuardedAction($('#odhl')" in html, "logout must be guarded"


def test_change_settings_prefills_current_profile_and_can_be_left_without_saving():
    html = app_html()
    onboarding = declaration(html, "function viewOnboarding() ")

    assert "profil.osoby" in onboarding, "household size must come from ME, not a hardcoded default"
    assert "profil.frekvencia" in onboarding
    assert "profil.obchody" in onboarding
    assert "n===osoby?' on':''" in onboarding
    assert "v===frekvencia?' on':''" in onboarding
    assert "obchody.indexOf(o)>=0?' on':''" in onboarding

    assert "n===4?' on':''" not in onboarding, "4 people must not be hardcoded as selected"
    assert '<div class="chip on" data-v="2">Raz za 2 dni</div>' not in onboarding
    assert "`<div class=\"chip on\" data-v=\"${o}\">${o}</div>`" not in onboarding

    assert "Späť bez zmeny" in onboarding, "an existing profile must be leavable without submitting"
    assert "$('#spat').onclick" in onboarding


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
