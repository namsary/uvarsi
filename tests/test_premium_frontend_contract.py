"""Čo musí platiť na obrazovke, keď je špajza platená vlastnosť.

Majiteľ chce tri veci naraz a všetky tri sa dajú overiť zo súboru:

  1. bezplatný účet špajzu VIDÍ — zamknutú, ale s poctivou ukážkou toho,
     čo sa s plánom stane, keď ju má (nie prázdnu stenu s výzvou na platbu),
  2. nič sa netvári, že to sú jeho údaje, a nikde sa netlačí na pílu,
  3. o Premium rozhoduje server; klient si ho nesmie „odvodiť" sám.

Testy sú čisto v Pythone (prípadne cez node), aby bežali aj na Linuxe — na
rozdiel od tests/test_app_html_contract.py, ktorý potrebuje cscript.exe.
"""
import re
import shutil
import subprocess
from pathlib import Path

import pytest


APP = Path("app/static/app.html")
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


def declaration(html, signature):
    """Vráti celú deklaráciu funkcie, ktorá začína daným podpisom."""
    match = re.search(re.escape(signature) + r"\{.*?\n\}", html, re.S)
    assert match, "app musí deklarovať " + signature.strip()
    return match.group(0)


def run_node(tmp_path, name, source):
    script = tmp_path / name
    script.write_text(source, encoding="utf-8")
    return subprocess.run([NODE, str(script)], capture_output=True, text=True)


# ------------------------------------------------------------ zamknutá špajza
def test_a_free_account_still_reaches_the_pantry_tab():
    html = app_html()

    assert 'data-t="spajza"' in html, "špajza musí ostať v menu aj pre bezplatný účet"
    rozcestie = declaration(html, "function vSpajza() ")
    assert "premium" in rozcestie, "obrazovka sa musí rozhodnúť podľa nároku zo servera"
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
    assert "/api/platba/start" in zamknuta, "keď platby bežia, tlačidlo musí niekam viesť"


def test_nothing_on_the_locked_screen_pushes_or_counts_down():
    html = app_html()
    zamknuta = declaration(html, "function vSpajzaZamknuta() ")

    for obrat in NATLAK:
        assert obrat not in html, f"appka netlačí na pílu: {obrat!r}"
    assert "setInterval" not in zamknuta, "žiadne odpočty na obrazovke o platbe"
    assert "volne_miesta" not in zamknuta, "žiadna umelá vzácnosť miest"


def test_premium_is_taken_from_the_server_answer_and_never_from_the_client():
    html = app_html()

    remembered = declaration(html, "function rememberProfile(me) ")
    assert "premium" not in remembered, (
        "zapamätaný profil nesmie odomykať nič — o nároku rozhoduje server"
    )
    assert "localStorage" not in declaration(html, "function vSpajzaZamknuta() ")


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
    pantry = declaration(html, "function vSpajza() ")

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
    pantry = declaration(html, "function vSpajza() ")
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
    assert "timeoutMs: PLAN_TIMEOUT_MS" in znova, "aj prepočet je volanie, čo môže visieť"

    plan_view = declaration(html, "function vPlan() ")
    assert "novyPlan()" in plan_view
    assert "Chcem iný plán" in plan_view


def test_a_refused_regeneration_keeps_the_plan_the_user_is_reading():
    html = app_html()
    novy = declaration(html, "async function novyPlan() ")

    assert "PLAN_NOTE" in novy, "hlášku o strope treba ukázať, nie prehltnúť"
    assert "PLAN = null" not in novy and "clearAuthenticatedState" not in novy
    assert "stopPlanProgress()" in novy, "koliesko sa musí zastaviť aj pri odmietnutí"
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
