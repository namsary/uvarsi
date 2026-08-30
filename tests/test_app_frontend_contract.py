import json
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
    assert opening >= 0
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
def test_account_screens_have_compact_tabs_correct_autocomplete_and_slovak_states(
    tmp_path,
):
    source = function_source(app_html(), "authScreenHtml")
    result = run_node(
        tmp_path,
        "account-screen-markup.js",
        source
        + "\nprocess.stdout.write(JSON.stringify({"
        + "login:authScreenHtml('login'),"
        + "register:authScreenHtml('register'),"
        + "forgot:authScreenHtml('forgot')}));\n",
    )

    assert result.returncode == 0, result.stdout + result.stderr
    screens = json.loads(result.stdout)
    assert "Prihlásiť sa" in screens["login"]
    assert "Vytvoriť účet" in screens["login"]
    assert 'autocomplete="email"' in screens["login"]
    assert 'autocomplete="current-password"' in screens["login"]
    assert "Zabudol som heslo" in screens["login"]
    assert 'autocomplete="new-password"' in screens["register"]
    assert "10 až 128 znakov" in screens["register"]
    assert "Poslať odkaz na obnovu" in screens["forgot"]
    assert 'type="password"' not in screens["forgot"]
    assert all('role="status"' in screen for screen in screens.values())
    assert all("passkey" not in screen.lower() for screen in screens.values())


@needs_node
def test_account_requests_keep_credentials_in_post_body_and_never_in_urls_or_storage(
    tmp_path,
):
    html = app_html()
    request_source = function_source(html, "authRequest")
    view_source = function_source(html, "viewAccountAuth")
    result = run_node(
        tmp_path,
        "account-request-contract.js",
        request_source
        + "\nconst secret='tajné heslo s medzerou';"
        + "\nconst flows=["
        + "authRequest('login','cook@example.com',secret),"
        + "authRequest('register','new@example.com',secret),"
        + "authRequest('forgot','reset@example.com','')];"
        + "\nprocess.stdout.write(JSON.stringify(flows));\n",
    )

    assert result.returncode == 0, result.stdout + result.stderr
    login, register, forgot = json.loads(result.stdout)
    assert [login["url"], register["url"], forgot["url"]] == [
        "/api/auth/login",
        "/api/auth/register",
        "/api/auth/password/request",
    ]
    assert json.loads(login["options"]["body"]) == {
        "email": "cook@example.com",
        "password": "tajné heslo s medzerou",
    }
    assert json.loads(register["options"]["body"])["password"] == (
        "tajné heslo s medzerou"
    )
    assert json.loads(forgot["options"]["body"]) == {
        "email": "reset@example.com"
    }
    assert all("?" not in flow["url"] for flow in (login, register, forgot))
    scoped_auth_source = request_source + view_source
    assert "localStorage" not in scoped_auth_source
    assert "sessionStorage" not in scoped_auth_source
    assert "location.search" not in scoped_auth_source
    assert "history.pushState" not in scoped_auth_source
    assert "runGuardedAction" in view_source


@needs_node
def test_wrong_password_stays_on_account_form_with_a_slovak_error(tmp_path):
    html = app_html()
    source = function_source(html, "accountApi")
    view_source = function_source(html, "viewAccountAuth")
    result = run_node(
        tmp_path,
        "account-api-error.js",
        source
        + r"""
let calls=0;
global.fetch=async()=>{calls+=1;return {
  ok:false,status:401,json:async()=>({detail:'E-mail alebo heslo nesedia.'})
}};
(async()=>{
  try{await accountApi({url:'/api/auth/login',options:{method:'POST'}});process.exit(2)}
  catch(error){process.stdout.write(JSON.stringify({calls,message:error.message}))}
})().catch(error=>{console.error(error);process.exit(1)});
""",
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout) == {
        "calls": 1,
        "message": "E-mail alebo heslo nesedia.",
    }
    assert "accountApi" in view_source


@needs_node
def test_password_visibility_toggle_updates_control_without_changing_value(tmp_path):
    source = function_source(app_html(), "togglePasswordVisibility")
    result = run_node(
        tmp_path,
        "password-visibility-contract.js",
        source
        + r"""
const attributes={};
const input={type:'password',value:'heslo zostáva rovnaké'};
const button={textContent:'',setAttribute(name,value){attributes[name]=String(value)}};
togglePasswordVisibility(input,button);
if(input.type!=='text'||input.value!=='heslo zostáva rovnaké')process.exit(1);
if(button.textContent!=='Skryť heslo'||attributes['aria-pressed']!=='true')process.exit(2);
togglePasswordVisibility(input,button);
if(input.type!=='password'||button.textContent!=='Zobraziť heslo')process.exit(3);
if(attributes['aria-pressed']!=='false')process.exit(4);
""",
    )

    assert result.returncode == 0, result.stdout + result.stderr


def test_feature_flag_keeps_legacy_magic_ui_and_enabled_ui_uses_guarded_actions():
    html = app_html()
    dispatcher = function_source(html, "viewLogin")
    legacy = function_source(html, "viewLegacyLogin")
    account = function_source(html, "viewAccountAuth")

    assert "AUTH_V3_ENABLED" in dispatcher
    assert "viewLegacyLogin" in dispatcher
    assert "viewAccountAuth" in dispatcher
    assert "/api/auth/request" in legacy
    assert "Poslať odkaz" in legacy
    assert "/api/auth/request" not in account
    assert "runGuardedAction" in account
    assert "authRequest" in account


@needs_node
def test_password_setup_card_is_non_blocking_and_only_shown_when_needed(tmp_path):
    html = app_html()
    source = function_source(html, "passwordSetupCard")
    result = run_node(
        tmp_path,
        "password-setup-card.js",
        source
        + "\nprocess.stdout.write(JSON.stringify(["
        + "passwordSetupCard({auth_v3:false,password_configured:false}),"
        + "passwordSetupCard({auth_v3:true,password_configured:true}),"
        + "passwordSetupCard({auth_v3:true,password_configured:false})]));\n",
    )

    assert result.returncode == 0, result.stdout + result.stderr
    disabled, configured, needed = json.loads(result.stdout)
    assert disabled == ""
    assert configured == ""
    assert "Nastaviť heslo" in needed
    assert "Pokračovať bez nastavenia" in needed
    assert "/heslo" in needed
    assert "passwordSetupCard(ME)" in function_source(html, "vNast")
