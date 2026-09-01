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
        + "login:authScreenHtml('login',false),"
        + "passkeyLogin:authScreenHtml('login',true),"
        + "register:authScreenHtml('register'),"
        + "forgot:authScreenHtml('forgot')}));\n",
    )

    assert result.returncode == 0, result.stdout + result.stderr
    screens = json.loads(result.stdout)
    assert "Prihlásiť sa" in screens["login"]
    assert "Prihlásiť biometriou" not in screens["login"]
    assert "Prihlásiť biometriou" in screens["passkeyLogin"]
    assert 'autocomplete="current-password"' in screens["passkeyLogin"]
    assert "Vytvoriť účet" in screens["login"]
    assert 'autocomplete="email"' in screens["login"]
    assert 'autocomplete="current-password"' in screens["login"]
    assert "Zabudol som heslo" in screens["login"]
    assert 'autocomplete="new-password"' in screens["register"]
    assert "10 až 128 znakov" in screens["register"]
    assert "Poslať odkaz na obnovu" in screens["forgot"]
    assert 'type="password"' not in screens["forgot"]
    assert all('role="status"' in screen for screen in screens.values())
    assert all(
        "passkey" not in screens[name].lower()
        for name in ("login", "register", "forgot")
    )
    assert all('role="tab"' not in screen for screen in screens.values())
    assert all('role="tablist"' not in screen for screen in screens.values())
    assert all("aria-selected" not in screen for screen in screens.values())
    assert 'id="auth-login-mode"' in screens["forgot"]
    assert 'id="auth-login-mode" type="button" aria-current="page"' in screens[
        "forgot"
    ]


@needs_node
def test_passkey_login_is_hidden_without_flag_or_browser_capability(tmp_path):
    html = app_html()
    capability = function_source(html, "passkeyUiAvailable")
    markup = function_source(html, "authScreenHtml")
    result = run_node(
        tmp_path,
        "passkey-capability.js",
        capability
        + "\n"
        + markup
        + r"""
const unsupported={};
const supported={PublicKeyCredential:function PublicKeyCredential(){}};
const states={
  flagOff:passkeyUiAvailable(false,supported),
  unsupported:passkeyUiAvailable(true,unsupported),
  supported:passkeyUiAvailable(true,supported)
};
const screens={
  flagOff:authScreenHtml('login',states.flagOff),
  unsupported:authScreenHtml('login',states.unsupported),
  supported:authScreenHtml('login',states.supported),
  register:authScreenHtml('register',states.supported)
};
process.stdout.write(JSON.stringify({states,screens}));
""",
    )

    assert result.returncode == 0, result.stdout + result.stderr
    state = json.loads(result.stdout)
    assert state["states"] == {
        "flagOff": False,
        "unsupported": False,
        "supported": True,
    }
    assert "Prihlásiť biometriou" not in state["screens"]["flagOff"]
    assert "Prihlásiť biometriou" not in state["screens"]["unsupported"]
    assert "Prihlásiť biometriou" in state["screens"]["supported"]
    assert "Prihlásiť biometriou" not in state["screens"]["register"]
    assert all(
        'id="auth-password"' in state["screens"][name]
        for name in ("flagOff", "unsupported", "supported")
    )


@needs_node
def test_passkey_binary_conversion_and_navigator_option_mapping(tmp_path):
    html = app_html()
    names = [
        "base64urlToBuffer",
        "bufferToBase64url",
        "prepareRegistrationOptions",
        "prepareAuthenticationOptions",
        "registrationCredentialJson",
        "authenticationCredentialJson",
    ]
    source = "\n".join(function_source(html, name) for name in names)
    result = run_node(
        tmp_path,
        "passkey-binary-contract.js",
        source
        + r"""
const roundTrip=bufferToBase64url(base64urlToBuffer('-__vAAE'));
const createOptions=prepareRegistrationOptions({
  challenge:'-_8A',user:{id:'AQID',name:'cook@example.com'},
  excludeCredentials:[{type:'public-key',id:'BAUG',transports:['internal']}],
  rp:{id:'uvar.si',name:'Uvar.si'}
});
const getOptions=prepareAuthenticationOptions({
  challenge:'BwgJ',rpId:'uvar.si',
  allowCredentials:[{type:'public-key',id:'CgsM',transports:['hybrid']}]
});
const registration=registrationCredentialJson({
  id:'credential-id',type:'public-key',rawId:new Uint8Array([13,14]).buffer,
  response:{
    clientDataJSON:new Uint8Array([15]).buffer,
    attestationObject:new Uint8Array([16,17]).buffer,
    getTransports(){return ['internal','hybrid']}
  },
  getClientExtensionResults(){return {credProps:{rk:true}}}
});
const assertion=authenticationCredentialJson({
  id:'credential-id',type:'public-key',rawId:new Uint8Array([18]).buffer,
  response:{
    clientDataJSON:new Uint8Array([19]).buffer,
    authenticatorData:new Uint8Array([20]).buffer,
    signature:new Uint8Array([21]).buffer,
    userHandle:null
  },
  getClientExtensionResults(){return {}}
});
process.stdout.write(JSON.stringify({roundTrip,
  create:{challenge:[...new Uint8Array(createOptions.challenge)],
    user:[...new Uint8Array(createOptions.user.id)],
    excluded:[...new Uint8Array(createOptions.excludeCredentials[0].id)],
    rp:createOptions.rp},
  get:{challenge:[...new Uint8Array(getOptions.challenge)],
    allowed:[...new Uint8Array(getOptions.allowCredentials[0].id)],
    rpId:getOptions.rpId},registration,assertion}));
""",
    )

    assert result.returncode == 0, result.stdout + result.stderr
    state = json.loads(result.stdout)
    assert state["roundTrip"] == "-__vAAE"
    assert state["create"] == {
        "challenge": [251, 255, 0],
        "user": [1, 2, 3],
        "excluded": [4, 5, 6],
        "rp": {"id": "uvar.si", "name": "Uvar.si"},
    }
    assert state["get"] == {
        "challenge": [7, 8, 9],
        "allowed": [10, 11, 12],
        "rpId": "uvar.si",
    }
    assert state["registration"] == {
        "credential": {
            "id": "credential-id",
            "rawId": "DQ4",
            "type": "public-key",
            "response": {"clientDataJSON": "Dw", "attestationObject": "EBE"},
            "clientExtensionResults": {"credProps": {"rk": True}},
        },
        "transports": ["internal", "hybrid"],
    }
    assert state["assertion"] == {
        "id": "credential-id",
        "rawId": "Eg",
        "type": "public-key",
        "response": {
            "clientDataJSON": "Ew",
            "authenticatorData": "FA",
            "signature": "FQ",
            "userHandle": None,
        },
        "clientExtensionResults": {},
    }


@needs_node
def test_passkey_ceremonies_map_create_and_get_to_backend_verify_posts(tmp_path):
    html = app_html()
    names = [
        "base64urlToBuffer",
        "bufferToBase64url",
        "prepareRegistrationOptions",
        "prepareAuthenticationOptions",
        "registrationCredentialJson",
        "authenticationCredentialJson",
        "performPasskeyLogin",
        "performPasskeyRegistration",
    ]
    source = "\n".join(function_source(html, name) for name in names)
    result = run_node(
        tmp_path,
        "passkey-ceremony-mapping.js",
        source
        + r"""
const calls=[];const navigatorCalls=[];
async function accountApi(request){
  calls.push(request);
  if(request.url.endsWith('/register/options'))return {
    challenge:'AQID',user:{id:'BAUG',name:'cook@example.com'},rp:{id:'uvar.si'},
    excludeCredentials:[]
  };
  if(request.url.endsWith('/login/options'))return {
    challenge:'BwgJ',rpId:'uvar.si',allowCredentials:[]
  };
  return {ok:true,redirect:'/app'};
}
const registration={
  id:'register-id',type:'public-key',rawId:new Uint8Array([10]).buffer,
  response:{clientDataJSON:new Uint8Array([11]).buffer,
    attestationObject:new Uint8Array([12]).buffer,
    getTransports(){return ['internal']}},
  getClientExtensionResults(){return {}}
};
const assertion={
  id:'login-id',type:'public-key',rawId:new Uint8Array([13]).buffer,
  response:{clientDataJSON:new Uint8Array([14]).buffer,
    authenticatorData:new Uint8Array([15]).buffer,
    signature:new Uint8Array([16]).buffer,userHandle:new Uint8Array([17]).buffer},
  getClientExtensionResults(){return {}}
};
const credentialsApi={
  async create(argument){navigatorCalls.push(['create',[...new Uint8Array(argument.publicKey.challenge)]]);return registration},
  async get(argument){navigatorCalls.push(['get',[...new Uint8Array(argument.publicKey.challenge)]]);return assertion}
};
(async()=>{
  await performPasskeyRegistration(credentialsApi);
  await performPasskeyLogin(credentialsApi);
  process.stdout.write(JSON.stringify({navigatorCalls,calls:calls.map(call=>({
    url:call.url,method:call.options.method,body:JSON.parse(call.options.body)}))}));
})().catch(error=>{console.error(error);process.exit(1)});
""",
    )

    assert result.returncode == 0, result.stdout + result.stderr
    state = json.loads(result.stdout)
    assert state["navigatorCalls"] == [["create", [1, 2, 3]], ["get", [7, 8, 9]]]
    assert [call["url"] for call in state["calls"]] == [
        "/api/auth/passkey/register/options",
        "/api/auth/passkey/register/verify",
        "/api/auth/passkey/login/options",
        "/api/auth/passkey/login/verify",
    ]
    assert all(call["method"] == "POST" for call in state["calls"])
    assert state["calls"][2]["body"] == {}
    register = state["calls"][1]["body"]
    assert register == {
        "challenge": "AQID",
        "credential": {
            "id": "register-id",
            "rawId": "Cg",
            "type": "public-key",
            "response": {"clientDataJSON": "Cw", "attestationObject": "DA"},
            "clientExtensionResults": {},
        },
        "transports": ["internal"],
        "name": "Toto zariadenie",
    }
    login = state["calls"][3]["body"]
    assert login["challenge"] == "BwgJ"
    assert login["device_name"] == "Zariadenie s Passkey"
    assert login["credential"]["response"] == {
        "clientDataJSON": "Dg",
        "authenticatorData": "Dw",
        "signature": "EA",
        "userHandle": "EQ",
    }
    assert "localStorage" not in source
    assert "sessionStorage" not in source
    assert "location.search" not in source


@needs_node
def test_passkey_ceremony_error_keeps_password_fallback_enabled(tmp_path):
    html = app_html()
    names = [
        "runGuardedAction",
        "base64urlToBuffer",
        "bufferToBase64url",
        "prepareAuthenticationOptions",
        "authenticationCredentialJson",
        "performPasskeyLogin",
    ]
    source = "\n".join(function_source(html, name) for name in names)
    result = run_node(
        tmp_path,
        "passkey-cancel-fallback.js",
        source
        + r"""
let calls=[];
async function accountApi(request){
  calls.push(request);
  return {challenge:'AQID',rpId:'uvar.si',allowCredentials:[]};
}
const credentialsApi={get:async()=>{const error=new Error('cancelled');error.name='NotAllowedError';throw error}};
const passkeyButton={disabled:false,textContent:'Prihlásiť biometriou'};
const passwordButton={disabled:false,textContent:'Prihlásiť sa'};
const password={value:'heslo zostáva použiteľné'};
const status={textContent:'',style:{}};
(async()=>{
  const ok=await runGuardedAction(passkeyButton,status,
    ()=>performPasskeyLogin(credentialsApi));
  process.stdout.write(JSON.stringify({ok,calls,password,passwordButton,passkeyButton,status}));
})().catch(error=>{console.error(error);process.exit(1)});
""",
    )

    assert result.returncode == 0, result.stdout + result.stderr
    state = json.loads(result.stdout)
    assert state["ok"] is False
    assert state["password"] == {"value": "heslo zostáva použiteľné"}
    assert state["passwordButton"]["disabled"] is False
    assert state["passkeyButton"] == {
        "disabled": False,
        "textContent": "Prihlásiť biometriou",
    }
    assert "zrušen" in state["status"]["textContent"].lower()
    assert state["calls"][0]["url"] == "/api/auth/passkey/login/options"
    assert len(state["calls"]) == 1


@needs_node
def test_device_and_passkey_controls_use_only_valid_opaque_list_identifiers(tmp_path):
    html = app_html()
    source = function_source(html, "deviceRevocationRequest")
    source += "\n" + function_source(html, "passkeyDeletionRequest")
    result = run_node(
        tmp_path,
        "device-revocation-contract.js",
        source
        + r"""
const currentHash='a'.repeat(32),otherHash='b'.repeat(32);
const current=deviceRevocationRequest({management_id:currentHash,current:true});
const other=deviceRevocationRequest({management_id:otherHash,current:false});
const malformed=deviceRevocationRequest({management_id:'../../foreign',current:false});
const passkey=passkeyDeletionRequest({credential_id:'AQID-_8'});
const longPasskey=passkeyDeletionRequest({credential_id:'A'.repeat(1500)});
const tooLongPasskey=passkeyDeletionRequest({credential_id:'A'.repeat(2049)});
const badPasskey=passkeyDeletionRequest({credential_id:'../foreign'});
process.stdout.write(JSON.stringify({current,other,malformed,passkey,longPasskey,
  tooLongPasskey,badPasskey}));
""",
    )

    assert result.returncode == 0, result.stdout + result.stderr
    state = json.loads(result.stdout)
    assert state["current"] == {
        "url": "/api/auth/sessions/" + "a" * 32,
        "options": {"method": "DELETE"},
        "logsOutCurrent": True,
    }
    assert state["other"] == {
        "url": "/api/auth/sessions/" + "b" * 32,
        "options": {"method": "DELETE"},
        "logsOutCurrent": False,
    }
    assert state["malformed"] is None
    assert state["passkey"] == {
        "url": "/api/auth/passkeys/AQID-_8",
        "options": {"method": "DELETE"},
    }
    assert state["longPasskey"] == {
        "url": "/api/auth/passkeys/" + "A" * 1500,
        "options": {"method": "DELETE"},
    }
    assert state["tooLongPasskey"] is None
    assert state["badPasskey"] is None

    profile = function_source(html, "securityPanelHtml")
    assert "Toto zariadenie" in profile
    assert "Odhlásiť zariadenie" in profile
    assert "Odhlásiť ostatné zariadenia" in profile
    assert "Pridať biometriu/Passkey" in profile
    assert "Biometrická kontrola ostáva" in profile
    assert "verejné poverenie" in profile
    assert "localStorage" not in source + profile
    assert "sessionStorage" not in source + profile
    assert "document.cookie" not in source + profile

    handler_source = source + "\n" + function_source(html, "bindSecurityControls")
    behavior = run_node(
        tmp_path,
        "device-handler-idor.js",
        handler_source
        + r"""
const ownCurrent={management_id:'a'.repeat(32),current:true};
const ownOther={management_id:'b'.repeat(32),current:false};
const foreign={management_id:'f'.repeat(32),current:false};
const buttons={
  current:{dataset:{sessionRevoke:ownCurrent.management_id}},
  other:{dataset:{sessionRevoke:ownOther.management_id}},
  foreign:{dataset:{sessionRevoke:foreign.management_id}}
};
const calls=[];const loaded=[];const locations=[];
const M={querySelectorAll(selector){
  if(selector==='[data-session-revoke]')return [buttons.current,buttons.other,buttons.foreign];
  if(selector==='[data-passkey-delete]')return [];
  return [];
}};
function $(selector){return selector==='#security-status'?{textContent:''}:null}
function confirm(){return true}
async function runGuardedAction(_button,_status,action){await action();return true}
async function api(url,options){calls.push({url,options});return {ok:true}}
async function loadAccountSecurity(message){loaded.push(message)}
const location={replace(url){locations.push(url)}};
bindSecurityControls([], [ownCurrent,ownOther]);
(async()=>{
  await buttons.foreign.onclick();
  await buttons.other.onclick();
  await buttons.current.onclick();
  process.stdout.write(JSON.stringify({calls,loaded,locations}));
})().catch(error=>{console.error(error);process.exit(1)});
""",
    )
    assert behavior.returncode == 0, behavior.stdout + behavior.stderr
    handled = json.loads(behavior.stdout)
    assert [call["url"] for call in handled["calls"]] == [
        "/api/auth/sessions/" + "b" * 32,
        "/api/auth/sessions/" + "a" * 32,
    ]
    assert handled["loaded"] == ["Zariadenie bolo odhlásené."]
    assert handled["locations"] == ["/app"]


@needs_node
def test_security_load_isolates_each_endpoint_failure(tmp_path):
    html = app_html()
    names = [
        "passkeyUiAvailable",
        "securityDateLabel",
        "securityPanelHtml",
        "loadAccountSecurity",
    ]
    source = "\n".join(function_source(html, name) for name in names)
    result = run_node(
        tmp_path,
        "security-load-isolation.js",
        source
        + r"""
let SECURITY_LOAD_GENERATION=0;const AUTH_V3_ENABLED=true;
const window={PublicKeyCredential:function PublicKeyCredential(){}};
const status={textContent:'',style:{},connected:true};
const content={innerHTML:''};
const root={set innerHTML(value){this.rendered=value;status.connected=false}};
const nodes={'#account-security':root,'#account-security-content':content,
  '#security-status':status};
function $(selector){return nodes[selector]||null}
function esc(value){return String(value??'').replace(/[&<>"]/g,char=>
  ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[char]))}
let bound=null;let failedEndpoint='passkeys';
function bindSecurityControls(passkeys,sessions){bound={passkeys,sessions}}
async function api(url){
  if(url.endsWith('/'+failedEndpoint))throw new Error(
    failedEndpoint==='passkeys'?'Passkey služba je nedostupná.':'Relácie sú nedostupné.');
  if(url==='/api/auth/passkeys')return {passkeys:[{
    credential_id:'AQID',name:'Laptop',created_at:1,last_used_at:null
  }]};
  if(url==='/api/auth/sessions')return {sessions:[{
    management_id:'a'.repeat(32),device_name:'Tablet',created_at:1,
    last_seen_at:2,expires_at:3,current:true
  }]};
  throw new Error('unexpected URL');
}
(async()=>{
  await loadAccountSecurity();
  const passkeyFailure={html:content.innerHTML,statusText:status.textContent,bound};
  failedEndpoint='sessions';
  await loadAccountSecurity();
  const sessionFailure={html:content.innerHTML,statusText:status.textContent,bound};
  process.stdout.write(JSON.stringify({passkeyFailure,sessionFailure,
    connected:status.connected,rootRendered:root.rendered||''}));
})().catch(error=>{console.error(error);process.exit(1)});
""",
    )

    assert result.returncode == 0, result.stdout + result.stderr
    state = json.loads(result.stdout)
    passkey_failure = state["passkeyFailure"]
    assert "Tablet" in passkey_failure["html"]
    assert "Passkey sa nepodarilo načítať" in passkey_failure["html"]
    assert "Passkey služba je nedostupná" in passkey_failure["statusText"]
    assert passkey_failure["bound"]["passkeys"] == []
    assert passkey_failure["bound"]["sessions"][0]["device_name"] == "Tablet"
    session_failure = state["sessionFailure"]
    assert "Laptop" in session_failure["html"]
    assert "Zariadenia sa nepodarilo načítať" in session_failure["html"]
    assert "Relácie sú nedostupné" in session_failure["statusText"]
    assert session_failure["bound"]["passkeys"][0]["name"] == "Laptop"
    assert session_failure["bound"]["sessions"] == []
    assert state["connected"] is True
    assert state["rootRendered"] == ""


@needs_node
def test_security_load_generation_prevents_stale_overlap_from_overwriting_newer_state(
    tmp_path,
):
    html = app_html()
    names = [
        "passkeyUiAvailable",
        "securityDateLabel",
        "securityPanelHtml",
        "loadAccountSecurity",
    ]
    source = "\n".join(function_source(html, name) for name in names)
    result = run_node(
        tmp_path,
        "security-load-generation.js",
        source
        + r"""
let SECURITY_LOAD_GENERATION=0;const AUTH_V3_ENABLED=true;
const window={PublicKeyCredential:function PublicKeyCredential(){}};
const status={textContent:'',style:{},connected:true};
const content={innerHTML:''};
const root={set innerHTML(value){this.rendered=value;status.connected=false}};
const nodes={'#account-security':root,'#account-security-content':content,
  '#security-status':status,'#passkey-add':{focus(){}}};
function $(selector){return nodes[selector]||null}
function esc(value){return String(value??'')}
const bound=[];
function bindSecurityControls(passkeys,sessions){bound.push({passkeys,sessions})}
function deferred(){let resolve,reject;const promise=new Promise((ok,no)=>{resolve=ok;reject=no});
  return {promise,resolve,reject}}
const oldPasskeys=deferred(),newPasskeys=deferred(),oldSessions=deferred(),newSessions=deferred();
const queues={
  '/api/auth/passkeys':[oldPasskeys.promise,newPasskeys.promise],
  '/api/auth/sessions':[oldSessions.promise,newSessions.promise]
};
function api(url){return queues[url].shift()}
(async()=>{
  const older=loadAccountSecurity('Starý stav.','#passkey-add');
  const newer=loadAccountSecurity('Nový stav.','#passkey-add');
  newPasskeys.resolve({passkeys:[{credential_id:'new-id',name:'Nový Passkey',created_at:20}]});
  newSessions.resolve({sessions:[{management_id:'b'.repeat(32),device_name:'Nový telefón',
    created_at:20,last_seen_at:20,expires_at:30,current:true}]});
  await newer;
  oldPasskeys.resolve({passkeys:[{credential_id:'old-id',name:'Starý Passkey',created_at:10}]});
  oldSessions.resolve({sessions:[{management_id:'a'.repeat(32),device_name:'Starý telefón',
    created_at:10,last_seen_at:10,expires_at:30,current:true}]});
  await older;
  process.stdout.write(JSON.stringify({html:content.innerHTML,status,bound,
    rootRendered:root.rendered||''}));
})().catch(error=>{console.error(error);process.exit(1)});
""",
    )

    assert result.returncode == 0, result.stdout + result.stderr
    state = json.loads(result.stdout)
    assert "Nový Passkey" in state["html"]
    assert "Nový telefón" in state["html"]
    assert "Starý Passkey" not in state["html"]
    assert "Starý telefón" not in state["html"]
    assert state["status"]["textContent"] == "Nový stav."
    assert state["status"]["connected"] is True
    assert state["rootRendered"] == ""
    assert len(state["bound"]) == 1
    assert state["bound"][0]["passkeys"][0]["name"] == "Nový Passkey"


@needs_node
def test_security_refresh_preserves_live_region_and_restores_requested_focus(tmp_path):
    html = app_html()
    names = [
        "passkeyUiAvailable",
        "securityDateLabel",
        "securityPanelHtml",
        "loadAccountSecurity",
    ]
    source = "\n".join(function_source(html, name) for name in names)
    result = run_node(
        tmp_path,
        "security-load-focus.js",
        source
        + r"""
let SECURITY_LOAD_GENERATION=0;const AUTH_V3_ENABLED=true;
const window={PublicKeyCredential:function PublicKeyCredential(){}};
const focused=[];
const target={focus(){focused.push('device-security-title')}};
const status={textContent:'',style:{},connected:true};
const content={innerHTML:''};
const root={set innerHTML(value){this.rendered=value;status.connected=false}};
const nodes={'#account-security':root,'#account-security-content':content,
  '#security-status':status,'#device-security-title':target};
function $(selector){return nodes[selector]||null}
function esc(value){return String(value??'')}
function bindSecurityControls(){}
async function api(url){return url.endsWith('/passkeys')?{passkeys:[]}:{sessions:[]}}
(async()=>{
  const originalStatus=status;
  await loadAccountSecurity('Zariadenie bolo odhlásené.','#device-security-title');
  process.stdout.write(JSON.stringify({sameStatus:originalStatus===nodes['#security-status'],
    connected:status.connected,statusText:status.textContent,focused,
    rootRendered:root.rendered||'',content:content.innerHTML}));
})().catch(error=>{console.error(error);process.exit(1)});
""",
    )

    assert result.returncode == 0, result.stdout + result.stderr
    state = json.loads(result.stdout)
    assert state["sameStatus"] is True
    assert state["connected"] is True
    assert state["statusText"] == "Zariadenie bolo odhlásené."
    assert state["focused"] == ["device-security-title"]
    assert state["rootRendered"] == ""
    assert "Tvoje zariadenia" in state["content"]


@needs_node
def test_security_mutations_request_sensible_focus_after_refresh(tmp_path):
    html = app_html()
    source = function_source(html, "deviceRevocationRequest") + "\n"
    source += function_source(html, "passkeyDeletionRequest") + "\n"
    source += function_source(html, "bindSecurityControls")
    result = run_node(
        tmp_path,
        "security-mutation-focus.js",
        source
        + r"""
const passkey={credential_id:'AQID',name:'Telefón'};
const session={management_id:'b'.repeat(32),device_name:'Tablet',current:false};
const add={};const remove={dataset:{passkeyDelete:passkey.credential_id}};
const revoke={dataset:{sessionRevoke:session.management_id}};
const status={textContent:'',style:{}};
const M={querySelectorAll(selector){
  if(selector==='[data-passkey-delete]')return [remove];
  if(selector==='[data-session-revoke]')return [revoke];
  return [];
}};
function $(selector){
  if(selector==='#security-status')return status;
  if(selector==='#passkey-add')return add;
  return null;
}
function confirm(){return true}
function runGuardedAction(_button,_status,action){return action()}
async function performPasskeyRegistration(){}
const navigator={credentials:{}};
async function api(){}
const refreshed=[];
async function loadAccountSecurity(message,focusTarget){refreshed.push([message,focusTarget])}
bindSecurityControls([passkey],[session]);
(async()=>{
  await add.onclick();
  await remove.onclick();
  await revoke.onclick();
  process.stdout.write(JSON.stringify(refreshed));
})().catch(error=>{console.error(error);process.exit(1)});
""",
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout) == [
        ["Passkey bol pridaný.", "#passkey-add"],
        ["Passkey bol odstránený.", "#passkey-security-title"],
        ["Zariadenie bolo odhlásené.", "#device-security-title"],
    ]


def test_task8_security_controls_are_mobile_accessible_and_do_not_claim_biometrics_are_stored():
    html = app_html()

    assert ".security-list{" in html
    assert ".security-row{" in html
    assert "minmax(0,1fr)" in html
    assert "@media (max-width:360px)" in html
    status = function_source(html, "securityStatusHtml")
    assert 'role="status"' in status
    assert 'aria-live="polite"' in status
    assert 'aria-atomic="true"' in status
    assert "securityStatusHtml()" in function_source(html, "vNast")
    assert "biometrick" in html.lower()
    assert "ostáva v zariadení" in html.lower()
    assert "server ukladá odtlačok" not in html.lower()
    assert "server ukladá tvár" not in html.lower()


@needs_node
def test_account_mode_buttons_preserve_email_and_focus_new_heading(tmp_path):
    html = app_html()
    source = function_source(html, "passkeyUiAvailable") + "\n"
    source += function_source(html, "authScreenHtml") + "\n"
    source += function_source(html, "viewAccountAuth")
    result = run_node(
        tmp_path,
        "account-mode-focus.js",
        source
        + r"""
const AUTH_V3_ENABLED=false;
const nodes=new Map();const focused=[];
const M={html:'',set innerHTML(value){this.html=value;nodes.clear()},get innerHTML(){return this.html}};
function $(selector){
  const id=selector.startsWith('#')?selector.slice(1):selector;
  if(!M.html.includes('id="'+id+'"'))return null;
  if(!nodes.has(id))nodes.set(id,{value:'',onclick:null,onsubmit:null,
    focus(){focused.push(id)},style:{},setAttribute(){}});
  return nodes.get(id);
}
viewAccountAuth('forgot','cook@example.com');
const forgotMarkup=M.innerHTML;const forgotEmail=$('#auth-email').value;
$('#auth-register-mode').onclick();
process.stdout.write(JSON.stringify({forgotMarkup,forgotEmail,
  registerMarkup:M.innerHTML,registerEmail:$('#auth-email').value,focused}));
""",
    )

    assert result.returncode == 0, result.stdout + result.stderr
    state = json.loads(result.stdout)
    assert 'aria-current="page">Prihlásiť sa' in state["forgotMarkup"]
    assert 'aria-current="page">Vytvoriť účet' in state["registerMarkup"]
    assert state["forgotEmail"] == state["registerEmail"] == "cook@example.com"
    assert state["focused"] == ["auth-title", "auth-title"]


def test_account_controls_fit_320px_and_keep_native_visible_focus():
    html = app_html()

    assert ".auth-switcher{" in html
    assert "grid-template-columns:minmax(0,1fr) minmax(0,1fr)" in html
    switcher = html.split(".auth-switcher{", 1)[1].split("}", 1)[0]
    switch = html.split(".auth-switch{", 1)[1].split("}", 1)[0]
    assert "margin:0 0 16px" in switcher
    assert "min-width:0" in switch
    assert ".password-field{display:grid;grid-template-columns:minmax(0,1fr) auto" in html
    assert ".password-field input{min-width:0}" in html
    assert "@media (max-width:360px)" in html
    assert ":focus-visible{outline:3px solid var(--accent)" in html


@needs_node
def test_profile_cache_migrates_away_email_and_keeps_only_non_pii_hints(tmp_path):
    html = app_html()
    source = "\n".join(
        function_source(html, name)
        for name in ("rememberProfile", "cachedProfile", "paintKnownShell")
    )
    result = run_node(
        tmp_path,
        "profile-cache-privacy.js",
        "const PROFILE_KEY='uvarsi_profil';\n"
        + source
        + r"""
const values=new Map([[PROFILE_KEY,JSON.stringify({email:'person@example.com',
  osoby:3,adults:2,children:1,frekvencia:2,obchody:['Lidl'],onboarding:true})]]);
const localStorage={
  getItem(key){return values.has(key)?values.get(key):null},
  setItem(key,value){values.set(key,String(value))},removeItem(key){values.delete(key)}
};
const header={textContent:''};function $(selector){return selector==='#hdr'?header:null}
function showNav(){}function setNavigationReady(){}
const migrated=cachedProfile();const painted=paintKnownShell();
rememberProfile({email:'new@example.com',osoby:4,adults:2,children:2,
  frekvencia:1,obchody:['Tesco'],onboarding:true});
process.stdout.write(JSON.stringify({migrated,painted,header,stored:values.get(PROFILE_KEY)}));
""",
    )

    assert result.returncode == 0, result.stdout + result.stderr
    state = json.loads(result.stdout)
    assert state["painted"] is True
    assert state["header"]["textContent"] == "Tvoj plán"
    assert state["migrated"] == {"onboarding": True}
    assert json.loads(state["stored"]) == {"onboarding": True}
    assert "email" not in state["stored"].lower()
    assert "example.com" not in state["stored"]


@needs_node
def test_profile_cache_purges_pii_when_onboarding_is_false_or_json_is_invalid(tmp_path):
    source = function_source(app_html(), "cachedProfile")
    result = run_node(
        tmp_path,
        "profile-cache-purge.js",
        "const PROFILE_KEY='uvarsi_profil';\n"
        + source
        + r"""
function exercise(initial){
  const values=new Map([[PROFILE_KEY,initial]]);
  global.localStorage={
    getItem(key){return values.has(key)?values.get(key):null},
    setItem(key,value){values.set(key,String(value))},
    removeItem(key){values.delete(key)}
  };
  const returned=cachedProfile();
  return {returned,stored:values.has(PROFILE_KEY)?values.get(PROFILE_KEY):null};
}
process.stdout.write(JSON.stringify({
  notOnboarded:exercise(JSON.stringify({email:'old@example.com',onboarding:false})),
  invalid:exercise('{"email":"old@example.com",broken')
}));
""",
    )

    assert result.returncode == 0, result.stdout + result.stderr
    state = json.loads(result.stdout)
    assert state["notOnboarded"] == {
        "returned": None,
        "stored": '{"onboarding":false}',
    }
    assert state["invalid"] == {"returned": None, "stored": None}
    assert "email" not in json.dumps(state).lower()
    assert "example.com" not in json.dumps(state)


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
    assert "Mám starý účet – nastaviť heslo" in function_source(html, "authScreenHtml")
    assert "legacySetupRequest" in account


@needs_node
def test_legacy_setup_bridge_and_profile_password_change_are_guarded_post_actions(tmp_path):
    html = app_html()
    legacy_source = function_source(html, "legacySetupRequest")
    password_source = function_source(html, "passwordChangeRequest")
    result = run_node(
        tmp_path,
        "auth-ux-actions.js",
        legacy_source
        + "\n"
        + password_source
        + r"""
const legacy=legacySetupRequest(' old@example.com ');
const change=passwordChangeRequest('current secret','new secure password');
process.stdout.write(JSON.stringify({legacy,change}));
""",
    )

    assert result.returncode == 0, result.stdout + result.stderr
    state = json.loads(result.stdout)
    assert state["legacy"] == {
        "url": "/api/auth/request",
        "options": {
            "method": "POST",
            "body": json.dumps({"email": "old@example.com"}, separators=(",", ":")),
        },
    }
    assert state["change"] == {
        "url": "/api/auth/password/change",
        "options": {
            "method": "POST",
            "body": json.dumps(
                {
                    "current_password": "current secret",
                    "password": "new secure password",
                },
                separators=(",", ":"),
            ),
        },
    }
    profile = function_source(html, "vNast")
    assert 'autocomplete="current-password"' in profile
    assert 'autocomplete="new-password"' in profile
    assert "passwordChangeRequest" in profile
    assert "runGuardedAction" in profile
    assert "email/change" not in profile


@needs_node
def test_auth_v3_ui_capability_requires_literal_boolean_true(tmp_path):
    html = app_html()
    source = function_source(html, "authV3Enabled")
    result = run_node(
        tmp_path,
        "auth-v3-boolean-capability.js",
        source
        + r"""
const values=[true,false,'1','true',1,{},null,undefined];
process.stdout.write(JSON.stringify(values.map(value=>authV3Enabled({auth_v3:value}))));
""",
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout) == [True, False, False, False, False, False, False, False]


@needs_node
def test_password_setup_card_is_non_blocking_and_only_shown_when_needed(tmp_path):
    html = app_html()
    capability_source = function_source(html, "authV3Enabled")
    key_source = function_source(html, "passwordSetupDismissalKey")
    source = function_source(html, "passwordSetupCard")
    dismiss_source = function_source(html, "dismissPasswordSetup")
    result = run_node(
        tmp_path,
        "password-setup-card.js",
        capability_source
        + "\n"
        + key_source
        + "\n"
        + source
        + "\n"
        + dismiss_source
        + r"""
const values=new Map();const operations=[];
const storage={
  getItem(key){operations.push(['get',key]);return values.has(key)?values.get(key):null},
  setItem(key,value){operations.push(['set',key,String(value)]);values.set(key,String(value))},
  removeItem(key){operations.push(['remove',key]);values.delete(key)}
};
const first={id:7,email:'secret@example.com',auth_v3:true,password_configured:false};
const other={id:8,email:'other@example.com',auth_v3:true,password_configured:false};
const disabled=passwordSetupCard({...first,auth_v3:false},storage);
const before=passwordSetupCard(first,storage);
dismissPasswordSetup(first,storage);
const dismissed=passwordSetupCard(first,storage);
const otherUser=passwordSetupCard(other,storage);
const configured=passwordSetupCard({...first,password_configured:true},storage);
const resetAfterConfiguration=passwordSetupCard(first,storage);
process.stdout.write(JSON.stringify({disabled,before,dismissed,otherUser,configured,
  resetAfterConfiguration,entries:[...values.entries()],operations}));
""",
    )

    assert result.returncode == 0, result.stdout + result.stderr
    state = json.loads(result.stdout)
    assert state["disabled"] == ""
    assert "Nastaviť heslo" in state["before"]
    assert "Pokračovať bez nastavenia" in state["before"]
    assert 'href="/api/auth/pages/heslo"' in state["before"]
    assert state["dismissed"] == ""
    assert "Nastaviť heslo" in state["otherUser"]
    assert state["configured"] == ""
    assert "Nastaviť heslo" in state["resetAfterConfiguration"]
    assert state["entries"] == []
    writes = [entry for entry in state["operations"] if entry[0] == "set"]
    assert writes == [["set", "uvarsi.password-setup-dismissed.v1:7", "1"]]
    assert "secret@example.com" not in json.dumps(state["operations"])
    profile_source = function_source(html, "vNast")
    assert "passwordSetupCard(ME, localStorage)" in profile_source
    assert "dismissPasswordSetup(ME, localStorage)" in profile_source
