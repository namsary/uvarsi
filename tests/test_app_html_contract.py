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
