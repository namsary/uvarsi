"""Čo musí platiť na obrazovke, keď je špajza platená vlastnosť.

Majiteľ chce tri veci naraz a všetky tri sa dajú overiť zo súboru:

  1. bezplatný účet špajzu VIDÍ — zamknutú, ale s poctivou ukážkou toho,
     čo sa s plánom stane, keď ju má (nie prázdnu stenu s výzvou na platbu),
  2. nič sa netvári, že to sú jeho údaje, a nikde sa netlačí na pílu,
  3. o Premium rozhoduje server; klient si ho nesmie „odvodiť" sám.

Testy sú čisto v Pythone (prípadne cez node), aby bežali aj na Linuxe — na
rozdiel od tests/test_app_html_contract.py, ktorý potrebuje cscript.exe.
"""
import base64
import hashlib
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest


APP = Path("app/static/app.html")
SW = Path("sw.js")
NODE = shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="node runtime is not available")

UKAZKA = ("ryža", "vajcia", "cibuľa")

# Nátlakové obraty, ktoré do pokojnej appky nepatria. Zoznam je zámerne
# konkrétny: nejde o zákaz slov, ale o zákaz vymyslenej naliehavosti.
NATLAK = (
    "Posledná šanca", "posledná šanca", "Nezmeškaj", "Iba dnes", "Len dnes",
    "Ponuka končí", "Ponáhľaj", "!!!", "Naozaj nechceš", "Škoda,",
    "Prichádzaš o", "Zostáva už len",
)


def app_html():
    return APP.read_text(encoding="utf-8")


def subscription_asset():
    html = app_html()
    match = re.search(
        r"url:'(/static/subscription-profile\.([0-9a-f]{12})\.js)'"
        r",integrity:'(sha384-[A-Za-z0-9+/=]+)'",
        html,
    )
    assert match, "profil musí odkazovať na obsahovo hashovaný modul so SRI"
    path = Path("app") / match.group(1).lstrip("/")
    assert path.is_file(), "profilový modul musí byť súčasťou release"
    return match, path, path.read_text(encoding="utf-8")


def declaration(html, signature):
    """Vráti celú deklaráciu funkcie, ktorá začína daným podpisom."""
    match = re.search(re.escape(signature) + r"\{.*?\n\}", html, re.S)
    assert match, "app musí deklarovať " + signature.strip()
    return match.group(0)


def profile_declaration(module, signature):
    """Vráti jednu funkciu z odsadeného profilového IIFE modulu."""
    match = re.search(
        re.escape(signature) + r"\{.*?\}(?=(?:async\s+)?function\s)",
        module,
        re.S,
    )
    assert match, "profil musí deklarovať " + signature.strip()
    return match.group(0)


def run_node(tmp_path, name, source):
    script = tmp_path / name
    script.write_text(source, encoding="utf-8")
    return subprocess.run(
        [NODE, str(script)], capture_output=True, text=True, encoding="utf-8"
    )


# ------------------------------------------------------------ zamknutá špajza
def test_a_free_account_still_reaches_the_pantry_tab():
    html = app_html()

    assert 'data-t="spajza"' in html, "špajza musí ostať v menu aj pre bezplatný účet"
    rozcestie = declaration(html, "function vSpajza() ")
    access = declaration(html, "function pantryUnlocked(me) ")
    assert "premium" in access, "obrazovka sa musí rozhodnúť podľa nároku zo servera"
    assert "pantryUnlocked(ME)" in rozcestie
    assert "vSpajzaZamknuta()" in rozcestie


def test_the_locked_pantry_shows_a_real_preview_of_what_changes():
    """Nie prázdna stena: konkrétne suroviny a konkrétny následok."""
    html = app_html()
    zamknuta = declaration(html, "function vSpajzaZamknuta() ")
    ukazka = re.search(r"const SPAJZA_UKAZKA = \[([^\]]*)\];", html)

    assert ukazka, "suroviny v ukážke musia byť pomenované na jednom mieste"
    for surovina in UKAZKA:
        assert surovina in ukazka.group(1), f"ukážka musí byť konkrétna — chýba {surovina}"
    assert "SPAJZA_UKAZKA" in zamknuta
    assert "nákupn" in zamknuta.casefold(), "musí ukázať, že položky vypadnú z nákupu"
    assert "máš doma" in zamknuta
    assert "ingredientRow({spajza:" in zamknuta, (
        "ukážka kreslí surovinu tým istým riadkom ako skutočný plán"
    )
    assert "meal-n" in zamknuta, "musí ukázať aj jedlo poskladané okolo špajze"


def test_the_locked_pantry_never_pretends_the_preview_is_the_users_data():
    zamknuta = declaration(app_html(), "function vSpajzaZamknuta() ")

    assert "Ukážka" in zamknuta, "ukážka musí byť pomenovaná ako ukážka"
    assert "nie tvoje údaje" in zamknuta
    assert not re.search(r"ME\.spajza(?!_)", zamknuta), (
        "zamknutá obrazovka nesmie zobrazovať cudzie/staré dáta"
    )


def test_the_locked_pantry_cannot_write_anything():
    zamknuta = declaration(app_html(), "function vSpajzaZamknuta() ")

    assert "/api/spajza" not in zamknuta, "zamknutá špajza nesmie nič ukladať"
    assert "disabled" in zamknuta, "zápis musí byť viditeľne zamknutý, nie ticho zahodený"


def test_the_locked_pantry_offers_one_line_and_one_button():
    zamknuta = declaration(app_html(), "function vSpajzaZamknuta() ")

    assert zamknuta.count("<button") == 1, "jedna obrazovka, jedno tlačidlo"
    assert "<b>Premium</b>" in zamknuta, "jedna jasná veta o tom, čo Premium dáva"
    assert zamknuta.count("<b>Premium</b>") == 1


def test_the_locked_pantry_tells_the_truth_when_payments_are_off():
    """Vypnuté platby nesmú viesť do slepej uličky s peknou hláškou."""
    zamknuta = declaration(app_html(), "function vSpajzaZamknuta() ")

    assert "platby_zapnute" in zamknuta, "stav platieb musí prísť zo servera"
    assert "Platby ešte nie sú spustené" in zamknuta
    assert "disabled" in zamknuta
    assert "vCheckout" in zamknuta, "keď platby bežia, tlačidlo musí otvoriť objednávku"


def test_checkout_screen_shows_the_complete_annual_offer_before_redirecting():
    html = app_html()
    checkout = declaration(html, "function vCheckout() ")

    for text in (
        "49 € ročne",
        "automaticky obnovuje",
        "do konca zaplateného obdobia",
        "14 dní",
        "Objednať Premium s povinnosťou platby",
    ):
        assert text.casefold() in checkout.casefold()
    assert "offer.title" in checkout
    assert "offer.amount_cents" in checkout
    assert "offer.summary" in checkout
    assert "premium-annual-founder-first-year-v1" in checkout
    assert "premium-annual-standard-v1" in checkout
    for feature in ("špajz", "obchod", "vegetari", "vegán", "bielkov"):
        assert feature in checkout.casefold()
    assert "zakladajuci_volne_miesta" in checkout
    assert 'href="/vop"' in checkout
    assert 'href="/ochrana-osobnych-udajov"' in checkout
    assert 'href="/odstupenie"' in checkout


def test_checkout_requires_four_explicit_consents_and_posts_their_version():
    checkout = declaration(app_html(), "function vCheckout() ")

    assert checkout.count('type="checkbox"') == 4
    for consent in (
        "checkout-terms", "checkout-renewal", "checkout-immediate",
        "checkout-proration",
    ):
        assert consent in checkout
    assert "disabled" in checkout
    for consent in (
        "accept_terms:true", "accept_automatic_renewal:true",
        "request_immediate_activation:true",
        "acknowledge_withdrawal_proration:true",
    ):
        assert consent in checkout
    assert "legal_version:ME.pravna_verzia" in checkout
    assert "JSON.stringify" in checkout
    assert "/api/platba/start" in checkout
    assert "marketing" not in checkout.casefold()


@needs_node
def test_zero_founder_slots_render_only_the_server_standard_offer(tmp_path):
    checkout = declaration(app_html(), "function vCheckout() ")
    result = run_node(
        tmp_path,
        "standard-checkout-offer.js",
        """
var ME={platby_zapnute:true,platby_pripravene:true,pravna_verzia:'2026-09-12-v5',
  zakladajuci_volne_miesta:0,checkout_offer:{offer_id:'premium-annual-standard-v1',
  founder:false,amount_cents:4900,renewal_amount_cents:4900,currency:'EUR',
  billing_interval:'year',auto_renews:true,title:'Premium',price_note:'ročne',
  summary:'49 € ročne. Predplatné sa automaticky obnovuje každý rok, kým ho nezrušíš.'}};
var nodes={}, checks=[], html='';
var M={querySelectorAll:function(){return checks;}};
Object.defineProperty(M,'innerHTML',{get:function(){return html;},set:function(value){
  html=value; checks=[]; for(var i=0;i<4;i++) checks.push({checked:false,onchange:null});
  nodes={'#checkout-pay':{disabled:true,textContent:'pay'},'#checkout-back':{},
    '#checkout-err':{textContent:''}};
}});
function $(selector){return nodes[selector];}
function esc(value){return String(value);}
function runGuardedAction(){throw new Error('checkout must not start while rendering');}
function api(){throw new Error('checkout must not start while rendering');}
function vSpajzaZamknuta(){throw new Error('valid server offer was rejected');}
var location={href:'unchanged'};
"""
        + checkout
        + """
vCheckout();
if(html.indexOf('49 €')<0) throw new Error('standard price missing');
if(html.indexOf('39 €')>=0) throw new Error('stale founder price rendered');
if(html.indexOf('Zakladajúce Premium')>=0) throw new Error('stale founder title rendered');
""",
    )

    assert result.returncode == 0, result.stdout + result.stderr


@needs_node
def test_price_change_rerenders_summary_and_requires_fresh_consent(tmp_path):
    html = app_html()
    functions = "\n".join(
        declaration(html, signature)
        for signature in (
            "async function runGuardedAction(button, errorNode, action) ",
            "function vCheckout() ",
        )
    )
    result = run_node(
        tmp_path,
        "checkout-price-change.js",
        """
var founder={offer_id:'premium-annual-founder-first-year-v1',founder:true,
  amount_cents:3900,renewal_amount_cents:4900,currency:'EUR',billing_interval:'year',
  auto_renews:true,title:'Zakladajúce Premium',price_note:'prvý rok',
  summary:'Prvý rok za 39 €. Potom 49 € ročne.'};
var standard={offer_id:'premium-annual-standard-v1',founder:false,
  amount_cents:4900,renewal_amount_cents:4900,currency:'EUR',billing_interval:'year',
  auto_renews:true,title:'Premium',price_note:'ročne',
  summary:'49 € ročne. Predplatné sa automaticky obnovuje každý rok, kým ho nezrušíš.'};
var ME={platby_zapnute:true,platby_pripravene:true,pravna_verzia:'2026-09-12-v5',
  zakladajuci_volne_miesta:1,checkout_offer:founder};
var nodes={},checks=[],rendered='';
var M={querySelectorAll:function(){return checks;}};
Object.defineProperty(M,'innerHTML',{get:function(){return rendered;},set:function(value){
  rendered=value; checks=[]; for(var i=0;i<4;i++) checks.push({checked:false,onchange:null});
  nodes={'#checkout-pay':{disabled:true,textContent:'Objednať'},'#checkout-back':{},
    '#checkout-err':{textContent:''}};
}});
function $(selector){return nodes[selector];}
function esc(value){return String(value);}
function vSpajzaZamknuta(){throw new Error('valid checkout offer was rejected');}
var location={href:'unchanged'};
var apiCalls=[];
function api(url,options){
  apiCalls.push(JSON.parse(options.body));
  var failure=new Error('Cena sa zmenila.'); failure.code='price_changed';
  failure.offer=standard; return Promise.reject(failure);
}
"""
        + functions
        + """
(async function(){
  vCheckout();
  checks.forEach(function(item){item.checked=true; item.onchange();});
  await nodes['#checkout-pay'].onclick();
  if(location.href!=='unchanged') throw new Error('navigated after price change');
  if(rendered.indexOf('49 €')<0 || rendered.indexOf('39 €')>=0)
    throw new Error('server standard summary did not replace founder offer');
  if(!nodes['#checkout-pay'].disabled || checks.some(function(item){return item.checked;}))
    throw new Error('fresh explicit consent was not required');
  if(ME.checkout_offer.offer_id!==standard.offer_id) throw new Error('offer was not refreshed');
  if(apiCalls.length!==1 || apiCalls[0].expected_offer_id!==founder.offer_id ||
      apiCalls[0].expected_amount_cents!==3900) throw new Error('price handshake missing');
})().catch(function(error){console.error(error.stack||error);process.exit(1);});
""",
    )

    assert result.returncode == 0, result.stdout + result.stderr


@needs_node
def test_api_error_preserves_only_the_server_price_change_offer(tmp_path):
    reader = declaration(app_html(), "async function readApiResponse(r) ")
    result = run_node(
        tmp_path,
        "price-change-response.js",
        """
var offer={offer_id:'premium-annual-standard-v1',amount_cents:4900};
var response={ok:false,status:409,json:function(){return Promise.resolve({
  detail:'Cena sa zmenila.',kod:'price_changed',offer:offer});}};
function handleApiUnauthorized(){return false;}
function apiErrorMessage(status,payload){return payload.detail;}
"""
        + reader
        + """
(async function(){
  try { await readApiResponse(response); }
  catch(error){
    if(error.code!=='price_changed' || error.offer!==offer)
      throw new Error('server offer was lost');
    return;
  }
  throw new Error('error response resolved');
})().catch(function(error){console.error(error.stack||error);process.exit(1);});
""",
    )

    assert result.returncode == 0, result.stdout + result.stderr


@needs_node
def test_customer_service_shows_all_invoices_and_requires_explicit_selection(tmp_path):
    _asset, _path, module = subscription_asset()
    functions = "\n".join(
        profile_declaration(module, signature)
        for signature in (
            "function d(epoch)",
            "function a(cents)",
            "function st(status)",
            "function service(data)",
        )
    )
    result = run_node(
        tmp_path,
        "invoice-selection.js",
        """
var ME={email:'owner@example.test'};
function esc(value){return String(value);}
"""
        + functions
        + """
var page=service({orders:[],requests:[],invoices:[
  {invoice_id:'inv_initial',invoice_kind:'initial',amount_cents:3900,paid_at:1},
  {invoice_id:'inv_renewal',invoice_kind:'renewal',amount_cents:4900,paid_at:2}
]});
var withdrawal=page.split('<form id="withdrawal-form"')[1].split('</form>')[0];
var complaint=page.split('<form id="complaint-form"')[1].split('</form>')[0];
if((page.match(/inv_initial/g)||[]).length<2 || (page.match(/inv_renewal/g)||[]).length<2)
  throw new Error('all owner invoices are not shown');
if(withdrawal.indexOf('id="withdrawal-invoice"')<0 ||
   withdrawal.indexOf('inv_initial')<0 || withdrawal.indexOf('inv_renewal')>=0)
  throw new Error('withdrawal is not bound to an explicit initial invoice');
if(complaint.indexOf('id="complaint-invoice"')<0 ||
   complaint.indexOf('inv_initial')<0 || complaint.indexOf('inv_renewal')<0)
  throw new Error('complaint cannot select a concrete invoice');
if(complaint.indexOf('id="complaint-reason"')<0 ||
   complaint.indexOf('duplicate_charge')<0 || complaint.indexOf('unauthorized_charge')<0)
  throw new Error('payment remedy selection is missing');
""",
    )

    assert result.returncode == 0, result.stdout + result.stderr


def test_customer_service_does_not_claim_that_submitting_cancels_renewal():
    _asset, _path, module = subscription_asset()
    renderer = profile_declaration(module, "function service(data)")
    handler = module.split("async function loadService()", 1)[1]

    assert "zastavíme iba budúcu obnovu" not in renderer
    assert "Customer Portal" in renderer
    assert "cez podporu" in renderer
    assert "withdrawal-invoice" in handler
    assert "complaint-invoice" in handler
    assert "complaint-reason" in handler


def test_payment_button_opens_summary_instead_of_starting_checkout_immediately():
    locked = declaration(app_html(), "function vSpajzaZamknuta() ")

    assert "vCheckout" in locked
    assert "/api/platba/start" not in locked
    assert "platby_pripravene" in locked


def test_nothing_on_the_locked_screen_pushes_or_counts_down():
    html = app_html()
    zamknuta = declaration(html, "function vSpajzaZamknuta() ")

    for obrat in NATLAK:
        assert obrat not in html, f"appka netlačí na pílu: {obrat!r}"
    assert "setInterval" not in zamknuta, "žiadne odpočty na obrazovke o platbe"
    assert "setInterval" not in declaration(html, "function vCheckout() ")
    assert "zakladajuci_volne_miesta" in declaration(html, "function vCheckout() "), (
        "checkout smie ukázať iba reálnu kapacitu zo servera"
    )


def test_premium_is_taken_from_the_server_answer_and_never_from_the_client():
    html = app_html()

    remembered = declaration(html, "function rememberProfile(me) ")
    for field in (
        "premium", "status", "subscription_has_access", "access_until",
        "needs_review", "renews_at", "ends_at", "next_amount_cents",
        "auto_renews", "can_manage",
    ):
        assert field not in remembered, (
            "zapamätaný profil ani platobný stav nesmú odomykať nič — "
            "o nároku rozhoduje server"
        )
    assert "localStorage" not in declaration(html, "function vSpajzaZamknuta() ")


# ----------------------------------------------------- správa predplatného
@needs_node
def test_profile_renders_server_owned_subscription_states_and_one_action(tmp_path):
    _asset, _path, module = subscription_asset()
    result = run_node(
        tmp_path,
        "subscription-status-contract.js",
        "const window=globalThis;\n"
        + module
        + """
const renewal = 1800000000;
const end = 1790000000;
const common = {ma_narok:true,subscription_has_access:true,can_manage:true,
  next_amount_cents:4900,renews_at:renewal,access_until:end,needs_review:false};
const states = {
  active:UvarsiSubscription.render({...common,status:'active'}),
  cancelledActive:UvarsiSubscription.render({...common,status:'cancelled',renews_at:null}),
  cancelledEnded:UvarsiSubscription.render({...common,status:'cancelled',renews_at:null,
    subscription_has_access:false}),
  past_due:UvarsiSubscription.render({...common,status:'past_due'}),
  pausedActive:UvarsiSubscription.render({...common,status:'paused',needs_review:true}),
  pausedEnded:UvarsiSubscription.render({...common,status:'paused',needs_review:true,
    subscription_has_access:false}),
  reviewActive:UvarsiSubscription.render({...common,status:'active',needs_review:true}),
  unpaid:UvarsiSubscription.render({...common,status:'unpaid',subscription_has_access:false}),
  expired:UvarsiSubscription.render({...common,status:'expired',subscription_has_access:false}),
  manual:UvarsiSubscription.render({ma_narok:true,can_manage:false,status:null}),
  free:UvarsiSubscription.render({ma_narok:false,can_manage:false,status:null}),
  missingAmount:UvarsiSubscription.render({...common,status:'active',next_amount_cents:null}),
  hidden:UvarsiSubscription.render({...common,status:'active',url:'SIGNED_URL_MUST_NOT_RENDER'})
};
console.log(JSON.stringify(states));
""",
    )

    assert result.returncode == 0, result.stdout + result.stderr
    states = json.loads(result.stdout)
    assert "Aktívne" in states["active"]
    assert "Ďalšia ročná platba" in states["active"]
    assert "Dátum obnovy" in states["active"]
    assert "49 €" in states["active"]
    assert "Obnovenie je zrušené" in states["cancelledActive"]
    assert "Premium je aktívne do" in states["cancelledActive"]
    assert "Prístup do" in states["cancelledActive"]
    assert "Ďalšia ročná platba" not in states["cancelledActive"]
    assert "skončilo" in states["cancelledEnded"]
    assert "aktívne" not in states["cancelledEnded"].casefold()
    assert "platba sa rieši" in states["past_due"].casefold()
    assert "Pôvodný dátum obnovy" in states["past_due"]
    assert "pokus" not in states["past_due"].casefold()
    assert "skúša" not in states["past_due"].casefold()
    assert "Potvrdený prístup do" in states["pausedActive"]
    assert "Premium teraz nie je aktívne" in states["pausedEnded"]
    assert "Premium je aktívne" not in states["pausedEnded"]
    assert "overujeme" in states["reviewActive"].casefold()
    assert "Potvrdený prístup do" in states["reviewActive"]
    assert "pozastavené" in states["unpaid"]
    assert "skončilo" in states["expired"]
    assert "Prístup skončil" in states["expired"]
    assert "nemá predplatné" in states["manual"]
    assert "Nemáš aktívne predplatné" in states["free"]
    for name in (
        "active", "cancelledActive", "cancelledEnded", "past_due",
        "pausedActive", "pausedEnded", "reviewActive", "unpaid", "expired",
    ):
        assert states[name].count("Spravovať predplatné") == 1
        assert "platobn" in states[name].casefold()
        assert "faktúr" in states[name].casefold()
    assert "Spravovať predplatné" not in states["manual"]
    assert "Spravovať predplatné" not in states["free"]
    assert "0 €" not in states["missingAmount"]
    assert "SIGNED_URL_MUST_NOT_RENDER" not in states["hidden"]


@needs_node
def test_profile_load_never_posts_and_repeat_click_creates_one_fresh_portal(tmp_path):
    _asset, _path, module = subscription_asset()
    guard = declaration(
        app_html(), "async function runGuardedAction(button, errorNode, action) "
    )
    result = run_node(
        tmp_path,
        "subscription-action-contract.js",
        """
const apiCalls = [];
const navigations = [];
const window=globalThis;
let resolvePortal;
const root = {innerHTML:''};
const status = {textContent:'',style:{}};
const attributes = {};
const button = {disabled:false,textContent:'Spravovať predplatné',
  setAttribute:(name,value)=>{attributes[name]=value;},
  removeAttribute:(name)=>{delete attributes[name];}};
function $(selector) {
  if (selector === '#subscription-management') return root;
  if (selector === '#subscription-manage') return button;
  if (selector === '#subscription-manage-status') return status;
  return null;
}
function subscriptionManagementHtml(data, state) {
  if (state && state.loading) return '<p role="status">Načítavam</p>';
  return '<button id="subscription-manage">Spravovať predplatné</button>';
}
async function api(url, options) {
  apiCalls.push({url:url,method:(options && options.method) || 'GET'});
  if (url === '/api/platba/stav') return {status:'active',can_manage:true};
  return new Promise(resolve => { resolvePortal = resolve; });
}
const location = {assign:url => navigations.push(url)};
"""
        + guard
        + "\n"
        + module
        + """
(async function(){
  await UvarsiSubscription.loadSubscription();
  const afterLoad = apiCalls.slice();
  const first = button.onclick();
  const second = button.onclick();
  const whilePending = {calls:apiCalls.slice(),disabled:button.disabled,
    busy:attributes['aria-busy'],status:status.textContent};
  resolvePortal({url:'https://store.lemonsqueezy.com/billing/fresh-value'});
  await first;
  await second;
  console.log(JSON.stringify({afterLoad:afterLoad,whilePending:whilePending,
    calls:apiCalls,navigations:navigations,busy:attributes['aria-busy'] || null}));
})().catch(error => { console.error(error); process.exit(1); });
""",
    )

    assert result.returncode == 0, result.stdout + result.stderr
    state = json.loads(result.stdout)
    assert state["afterLoad"] == [{"url": "/api/platba/stav", "method": "GET"}]
    assert state["whilePending"]["calls"] == [
        {"url": "/api/platba/stav", "method": "GET"},
        {"url": "/api/platba/portal", "method": "POST"},
    ]
    assert state["whilePending"]["disabled"] is True
    assert state["whilePending"]["busy"] == "true"
    assert "Otváram" in state["whilePending"]["status"]
    assert state["calls"] == state["whilePending"]["calls"]
    assert state["navigations"] == [
        "https://store.lemonsqueezy.com/billing/fresh-value"
    ]
    assert state["busy"] is None


@needs_node
def test_portal_failure_is_actionable_accessible_and_never_echoes_a_signed_url(
    tmp_path,
):
    _asset, _path, module = subscription_asset()
    guard = declaration(
        app_html(), "async function runGuardedAction(button, errorNode, action) "
    )
    result = run_node(
        tmp_path,
        "subscription-error-contract.js",
        """
const status = {textContent:'',style:{}};
const window=globalThis;
const attributes = {};
const button = {disabled:false,textContent:'Spravovať predplatné',
  setAttribute:(name,value)=>{attributes[name]=value;},
  removeAttribute:(name)=>{delete attributes[name];}};
async function api() {
  throw new Error('https://store.lemonsqueezy.com/billing/LEAKED_SIGNED_VALUE');
}
const location = {assign:()=>{throw new Error('must not navigate');}};
"""
        + guard
        + "\n"
        + module
        + """
(async function(){
  await UvarsiSubscription.open(button,status);
  console.log(JSON.stringify({text:status.textContent,disabled:button.disabled,
    label:button.textContent,busy:attributes['aria-busy'] || null,
    valid:UvarsiSubscription.safeDestination('https://app.lemonsqueezy.com/my-orders/fresh'),
    attacker:UvarsiSubscription.safeDestination('https://lemonsqueezy.com.attacker.test/x'),
    controls:['\\u0000','\\u001f','\\u007f'].map(control =>
      UvarsiSubscription.safeDestination('https://store.lemonsqueezy.com/billing/a'
        + control + 'b'))}));
})().catch(error => { console.error(error); process.exit(1); });
""",
    )

    assert result.returncode == 0, result.stdout + result.stderr
    state = json.loads(result.stdout)
    assert state["disabled"] is False
    assert state["label"] == "Spravovať predplatné"
    assert state["busy"] is None
    assert "+421 917 347 009" in state["text"]
    assert "pumaragency@gmail.com" in state["text"]
    assert "zostáva nezmenené" in state["text"]
    assert "LEAKED_SIGNED_VALUE" not in state["text"]
    assert state["valid"] is True
    assert state["attacker"] is False
    assert state["controls"] == [False, False, False]


@needs_node
def test_billing_dates_are_always_slovak_dates_in_bratislava_time(tmp_path):
    _asset, _path, module = subscription_asset()
    result = run_node(
        tmp_path,
        "subscription-bratislava-date.js",
        "const window=globalThis;\n"
        + module
        + "\nconsole.log(UvarsiSubscription.date(1725143400));\n",
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip() == "1. 9. 2024"
    assert "timeZone:'Europe/Bratislava'" in module.replace(" ", "")


def test_profile_module_is_content_addressed_integrity_checked_and_lazy():
    match, path, source = subscription_asset()
    payload = path.read_bytes()
    expected_sri = "sha384-" + base64.b64encode(
        hashlib.sha384(payload).digest()
    ).decode("ascii")
    html = app_html()
    loader = declaration(html, "async function loadProfilePayments() ")

    assert hashlib.sha256(payload).hexdigest().startswith(match.group(2))
    assert match.group(3) == expected_sri
    assert match.group(1).startswith("/static/")
    assert "eval(" not in source and "new Function" not in source
    assert "createElement('script')" in loader
    assert ".integrity=SUBSCRIPTION_ASSET.integrity" in loader.replace(" ", "")
    assert "crossOrigin='anonymous'" in loader.replace(" ", "")
    assert f'<script src="{match.group(1)}"' not in html
    assert f"<link rel=\"preload\" href=\"{match.group(1)}\"" not in html
    assert match.group(1) not in SW.read_text(encoding="utf-8").split(
        "const SHELL = [", 1
    )[1].split("];", 1)[0]


def test_lazy_subscription_module_has_a_tight_transfer_budget():
    import gzip

    _match, path, _source = subscription_asset()
    compressed = len(gzip.compress(path.read_bytes(), 5))

    assert compressed <= 4_500, (
        f"profilový modul má {compressed} B gzip; strop je 4500 B"
    )


def test_profile_has_accessible_subscription_loading_and_no_hidden_portal_url():
    html = app_html()
    profile = declaration(html, "function vNast() ")
    loader = declaration(html, "async function loadProfilePayments() ")

    assert 'id="subscription-management"' in profile
    assert "loadProfilePayments()" in profile
    assert 'role="status"' in profile
    assert 'aria-live="polite"' in profile
    assert "module.loadSubscription()" in loader
    assert "/api/platba/portal" not in html
    assert "lemonsqueezy.com/billing" not in html
    assert "signed-secret" not in html


# ------------------------------------- odobraty/refundovany narok pocas upravy
def test_a_pantry_403_keeps_the_server_code_for_the_entitlement_recovery_branch():
    """Bez kodu z odpovede klient nerozozna odobratie Premium od beznej chyby."""
    reader = declaration(app_html(), "async function readApiResponse(r) ")

    assert re.search(r"\.kod\s*=|\.code\s*=", reader), (
        "chyba z API musi zachovat serverovy kod spajza_premium"
    )
    assert re.search(r"\.status\s*=", reader), (
        "chyba musi zachovat aj HTTP status, aby sa 403 nespracoval ako bezna chyba"
    )


def test_a_revoked_pantry_save_refreshes_authoritative_me_and_renders_the_lock():
    """403 spajza_premium nesmie nechat na obrazovke stary editovatelny formular."""
    html = app_html()
    pantry = declaration(html, "async function savePantryList(list) ")

    assert "spajza_premium" in pantry, "ulozenie musi mat osobitnu vetvu pre odobraty narok"
    assert re.search(r"403|status", pantry), "vetva patri iba serverovemu odmietnutiu 403"
    assert "api('/api/me')" in pantry, "po odmietnuti sa musi nacitat aktualny profil zo servera"
    assert re.search(r"ME\s*=\s*await\s+api\('/api/me'\)", pantry), (
        "globalny profil sa musi nahradit autoritativnou odpovedou"
    )
    assert "vSpajza()" in pantry, "obrazovka sa musi hned prekreslit do zamknuteho stavu"


def test_a_locked_dormant_pantry_uses_only_the_server_summary_not_item_names():
    """Refund skryje nazvy, ale pravdivo povie, kolko poloziek server stale drzi."""
    locked = declaration(app_html(), "function vSpajzaZamknuta() ")

    assert "spajza_uspana" in locked
    assert "spajza_ulozenych" in locked
    assert "spajza_sprava" in locked
    assert not re.search(r"ME\.spajza(?!_)", locked), (
        "zamknuta obrazovka nesmie odhalit nazvy ulozenych poloziek"
    )
    assert re.search(r"spajza_ulozenych[\s\S]*Premium|Premium[\s\S]*spajza_ulozenych", locked), (
        "pocet ulozenych poloziek musi byt vysvetleny spolu s ich navratom po Premium"
    )


def test_a_live_entitlement_loss_is_explained_even_when_the_pantry_was_empty():
    html = app_html()
    pantry = declaration(html, "async function savePantryList(list) ")
    locked = declaration(html, "function vSpajzaZamknuta() ")

    assert "PANTRY_ACCESS_CHANGED = true" in pantry
    assert "PANTRY_ACCESS_CHANGED" in locked
    assert "Prístup" in locked and "zmenil" in locked


@needs_node
def test_dormant_and_generic_locked_accounts_render_different_truthful_states(tmp_path):
    """Dynamicky dokaz: uspana spajza ukaze pocet, prazdny free ucet iba ukazku."""
    html = app_html()
    result = run_node(
        tmp_path,
        "dormant-pantry-contract.js",
        """
var rendered = '';
var M = {};
Object.defineProperty(M, 'innerHTML', {set: function(value) { rendered = value; }});
var SPAJZA_UKAZKA = ['ryza', 'vajcia', 'cibula'];
var SPAJZA_UKAZKA_CENY = {'ryza':'1,00','vajcia':'2,00','cibula':'1,00'};
function esc(value) { return String(value == null ? '' : value); }
function ingredientRow(item) { return '<span>' + esc(item.spajza) + '</span>'; }
function runGuardedAction() {}
function $(selector) { return null; }
var PANTRY_ACCESS_CHANGED = false;
var ME = {platby_zapnute:false, spajza_uspana:true, spajza_ulozenych:3,
  spajza_sprava:'Tvoje 3 polozky zostavaju ulozene a vratia sa s Premium.',
  spajza:['TAJNE_MENO']};
"""
        + declaration(html, "function vSpajzaZamknuta() ")
        + """
vSpajzaZamknuta();
if (rendered.indexOf('3') === -1) process.exit(1);
if (rendered.indexOf('TAJNE_MENO') !== -1) process.exit(2);
if (rendered.indexOf('Premium') === -1) process.exit(3);
ME = {platby_zapnute:false, spajza_uspana:false, spajza_ulozenych:0,
  spajza_sprava:null, spajza:[]};
vSpajzaZamknuta();
if (rendered.indexOf('Ukazka') === -1 && rendered.indexOf('Ukážka') === -1) process.exit(4);
process.exit(0);
""",
    )

    assert result.returncode == 0, result.stdout + result.stderr


# ------------------------------------------------------- denný strop prepočtov
def test_only_the_explicit_button_asks_for_a_brand_new_paid_plan():
    html = app_html()
    prve = declaration(html, "function generujPlan() ")
    znova = declaration(html, "function preskladajPlan() ")

    assert "force=1" not in prve, "prvé poskladanie smie prevziať hotový zdieľaný plán"
    assert "force=1" in znova, "prepočet na vyžiadanie sa musí cache vyhnúť"
    assert "timeoutMs" not in znova, "prepočet sa nesmie blokovať na dlhom browser timeoute"
    assert "requestPlan" in prve
    assert "requestPlan" in znova

    plan_view = declaration(html, "function vPlan() ")
    assert "novyPlan()" in plan_view
    assert "Chcem iný plán" in plan_view


def test_a_refused_regeneration_keeps_the_plan_the_user_is_reading():
    html = app_html()
    novy = declaration(html, "async function novyPlan() ")

    assert "PLAN_NOTE" in novy, "hlášku o strope treba ukázať, nie prehltnúť"
    assert "PLAN = null" not in novy and "clearAuthenticatedState" not in novy
    assert "preskladajPlan" in novy, "prepočet musí prejsť do pravdivého pending stavu"
    assert "PLAN_NOTE" in declaration(html, "function vPlan() ")


@needs_node
def test_the_plan_screen_says_how_many_new_plans_are_left_today(tmp_path):
    html = app_html()
    note = declaration(html, "function regenerationNote(me) ")
    result = run_node(
        tmp_path,
        "regeneration-note-contract.js",
        note
        + """
var zdarma = regenerationNote({limit_prepoctov: 1, zostava_prepoctov: 1});
if (zdarma.indexOf('1 z 1') === -1) process.exit(1);
var minute = regenerationNote({limit_prepoctov: 1, zostava_prepoctov: 0});
if (minute.indexOf('0 z 1') === -1) process.exit(2);
var platene = regenerationNote({limit_prepoctov: 5, zostava_prepoctov: 3});
if (platene.indexOf('3 z 5') === -1) process.exit(3);
if (regenerationNote(null) !== '') process.exit(4);
if (regenerationNote({}) !== '') process.exit(5);
if (regenerationNote({limit_prepoctov: 5, zostava_prepoctov: -9}).indexOf('0 z 5') === -1) process.exit(6);
if (regenerationNote({limit_prepoctov: 'x', zostava_prepoctov: 'y'}) !== '') process.exit(7);
process.exit(0);
""",
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "regenerationNote(ME)" in declaration(html, "function vPlan() ")


def test_the_pantry_hint_belongs_to_premium_only():
    """Bezplatný účet špajzu nemá, takže ho nesmie oslovovať návrh na prepočet."""
    plan_view = declaration(app_html(), "function vPlan() ")

    assert "pantryDiffers(" in plan_view
    assert re.search(r"ME\.premium[^;]*pantryDiffers\(", plan_view), (
        "návrh na prepočet po zmene špajze patrí len tomu, kto špajzu naozaj má"
    )
