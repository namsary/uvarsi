"""Odmietnutie pre NULOVÝ KREDIT je tretí druh zlyhania — a nesmie sa účtovať.

Incident 24. 8. 2026: majiteľovi došiel kredit na Anthropic API. Každé volanie
odvtedy padalo okamžite s HTTP 400 („Your credit balance is too low…"). Strop
na míňanie fungoval, ale `s_rozpoctom` každé spadnuté volanie zaúčtoval
konzervatívnym odhadom — čo je pri timeoute správne (mohol minúť tokeny), ale
pri odmietnutí PRED vykonaním práce je to výmysel. Škody:

  • oba týždňové behy zbierača (UVARSI_TYZDENNE_BEHY_ZBER = 2) padli na volania,
    ktoré nikdy nebežali — zbierač je zablokovaný do konca týždňa aj po dobití;
  • /api/naklady hlásilo 0,66 € „minutých", ktoré nikto nikdy nezaplatil;
  • log to volal DOČASNÁ CHYBA a dozorca to skúšal každú hodinu, hoci nulový
    kredit nie je dočasný stav — treba zásah človeka.

Tieto testy strážia, že sa to nezopakuje.
"""
import datetime
import importlib
import json
import re
import sqlite3
import sys
import types
from pathlib import Path

import pytest

from app import naklady
from app.receipt_data import StructuralFailure

from tests.test_server import (
    current_plan_rows,
    insert_hashed_session,
    load_server,
)


ROOT = Path(__file__).resolve().parents[1]

PONDELOK = datetime.datetime(2026, 8, 17, 9, 0, 0)      # ISO týždeň 2026-08-17
UTOROK = datetime.datetime(2026, 8, 18, 9, 0, 0)


@pytest.fixture
def con(tmp_path):
    spojenie = naklady.pripoj(tmp_path / "uvarsi.db")
    yield spojenie
    spojenie.close()


@pytest.fixture(autouse=True)
def ciste_prostredie(monkeypatch):
    for nazov in naklady.PREMENNE_PROSTREDIA:
        monkeypatch.delenv(nazov, raising=False)


def usage(vstup=0, vystup=0, cache_write=0, cache_read=0):
    return types.SimpleNamespace(
        input_tokens=vstup, output_tokens=vystup,
        cache_creation_input_tokens=cache_write, cache_read_input_tokens=cache_read,
    )


VISION_USAGE = usage(vstup=80_000, vystup=2_000)

SPRAVA_Z_INCIDENTU = (
    "Your credit balance is too low to access the Anthropic API. "
    "Please go to Plans & Billing to upgrade or purchase credits."
)


class FalosnaChybaAPI(Exception):
    """Verná napodobenina `anthropic.APIStatusError` — vrátane `body` a `status_code`.

    SDK stavia výnimku z rozparsovanej odpovede, takže detekcia sa má oprieť
    o štruktúrované polia, nie o text výnimky. Test to musí napodobniť presne,
    inak by strážil niečo iné než produkčný kód.
    """

    def __init__(self, sprava=SPRAVA_Z_INCIDENTU, *, status=400, typ="invalid_request_error"):
        telo = {"type": "error", "error": {"type": typ, "message": sprava}}
        super().__init__(f"Error code: {status} - {telo}")
        self.status_code = status
        self.body = telo
        self.response = types.SimpleNamespace(status_code=status)


def bad_request_kredit():
    return FalosnaChybaAPI()


# ------------------------------------------------------------------ detekcia
def test_odmietnutie_pre_nulovy_kredit_sa_rozpozna_zo_struktury_chyby():
    assert naklady.je_nedostatok_kreditu(bad_request_kredit()) is True


def test_ina_chyba_400_nie_je_nedostatok_kreditu():
    """`invalid_request_error` je zberný typ — sám o sebe nič nehovorí."""
    ina = FalosnaChybaAPI("max_tokens: must be less than or equal to 8192")
    assert naklady.je_nedostatok_kreditu(ina) is False


def test_chyba_s_inym_stavovym_kodom_nie_je_nedostatok_kreditu():
    """Rate limit (429) je dočasný stav — ten sa opakovať OPLATÍ."""
    limit = FalosnaChybaAPI(SPRAVA_Z_INCIDENTU, status=429, typ="rate_limit_error")
    assert naklady.je_nedostatok_kreditu(limit) is False


def test_obycajna_vynimka_s_podobnym_textom_sa_nezamieňa():
    """Bez štruktúry API netipujeme — inak by hocijaká chyba vypla účtovanie."""
    assert naklady.je_nedostatok_kreditu(RuntimeError(SPRAVA_Z_INCIDENTU)) is False
    assert naklady.je_nedostatok_kreditu(TimeoutError("spojenie vypršalo")) is False


def test_kredit_je_vlastna_trieda_zlyhania():
    """Ani dočasná chyba, ani normálne spadnuté volanie — vlastný kód."""
    chyba = naklady.KreditVycerpany()
    assert isinstance(chyba, naklady.RozpocetVycerpany)
    assert chyba.kod == naklady.KOD_KREDIT
    assert naklady.KOD_KREDIT not in (naklady.KOD_DENNY, naklady.KOD_MESACNY,
                                      naklady.KOD_UCEL, naklady.KOD_BEHY,
                                      naklady.KOD_NECITATELNY)


def test_sprava_o_kredite_je_po_slovensky_a_hovori_co_treba_urobit():
    text = str(naklady.KreditVycerpany())
    assert "kredit" in text.lower()
    assert "€" not in text, "nič sa neminulo — číslo v eurách by klamalo"


# ------------------------------------------------------------------ neúčtuje sa
def test_odmietnute_volanie_sa_vobec_nezauctuje(con):
    """Jadro opravy: za prácu, ktorá sa nevykonala, sa neplatí ani odhadom."""
    with pytest.raises(naklady.KreditVycerpany):
        naklady.s_rozpoctom(
            con, "plan", "claude-sonnet-5",
            lambda: (_ for _ in ()).throw(bad_request_kredit()),
            teraz=PONDELOK, notifikuj=lambda sprava: None,
        )

    assert con.execute("SELECT COUNT(*) FROM naklady").fetchone()[0] == 0
    assert con.execute("SELECT COALESCE(SUM(eur),0) FROM naklady").fetchone()[0] == 0


def test_ine_spadnute_volanie_sa_zauctuje_aj_nadalej(con):
    """Timeout mohol tokeny minúť — tá poistka sa opravou nesmie stratiť."""
    with pytest.raises(RuntimeError):
        naklady.s_rozpoctom(
            con, "plan", "claude-sonnet-5",
            lambda: (_ for _ in ()).throw(RuntimeError("timeout")), teraz=PONDELOK,
        )
    riadok = con.execute("SELECT * FROM naklady").fetchone()
    assert riadok["odhad"] == 1 and riadok["eur"] > 0


def test_strazeny_klient_prelozi_odmietnutie_na_typovanu_chybu(con):
    class Klient:
        def __init__(self):
            self.messages = types.SimpleNamespace(
                create=lambda **kw: (_ for _ in ()).throw(bad_request_kredit()))

    strazeny = naklady.strazeny_klient(con, Klient(), "zber_letakov",
                                       teraz=PONDELOK, notifikuj=lambda s: None)
    with pytest.raises(naklady.KreditVycerpany):
        strazeny.messages.create(model="claude-opus-5", max_tokens=100, messages=[])
    assert con.execute("SELECT COUNT(*) FROM naklady").fetchone()[0] == 0


# ------------------------------------------------------------------ miesto v behoch
def test_rezervovane_miesto_sa_da_vratit_a_nikdy_nejde_pod_nulu(con):
    naklady.rezervuj_beh(con, "zber_letakov", teraz=PONDELOK)
    assert naklady.uvolni_beh(con, "zber_letakov", teraz=PONDELOK) == 0
    assert naklady.uvolni_beh(con, "zber_letakov", teraz=PONDELOK) == 0

    # a miesto sa dá znova zabrať — strop teda ostáva v platnosti
    for _ in range(naklady.limit_behov("zber_letakov")):
        naklady.rezervuj_beh(con, "zber_letakov", teraz=PONDELOK)
    with pytest.raises(naklady.RozpocetVycerpany):
        naklady.rezervuj_beh(con, "zber_letakov", teraz=PONDELOK)


# ------------------------------------------------------------------ jedno upozornenie
def test_odmietnute_volania_poslu_prave_jedno_upozornenie(con):
    poslane = []
    for _ in range(12):
        with pytest.raises(naklady.KreditVycerpany):
            naklady.s_rozpoctom(
                con, "plan", "claude-sonnet-5",
                lambda: (_ for _ in ()).throw(bad_request_kredit()),
                teraz=PONDELOK, notifikuj=poslane.append,
            )
    assert len(poslane) == 1, "hodinový dozorca nesmie spustiť lavínu notifikácií"
    text = poslane[0]["titul"] + " " + poslane[0]["sprava"]
    assert "kredit" in text.lower()
    assert "dobi" in text.lower(), "majiteľ musí vedieť, čo má urobiť"


def test_uspesne_volanie_zrusi_priznak_vycerpaneho_kreditu(con):
    with pytest.raises(naklady.KreditVycerpany):
        naklady.s_rozpoctom(con, "plan", "claude-sonnet-5",
                            lambda: (_ for _ in ()).throw(bad_request_kredit()),
                            teraz=PONDELOK, notifikuj=lambda s: None)
    assert naklady.stav(con, teraz=PONDELOK)["kredit"]["vycerpany"] is True

    naklady.s_rozpoctom(con, "plan", "claude-sonnet-5",
                        lambda: types.SimpleNamespace(usage=usage(vstup=1_000)),
                        teraz=PONDELOK, notifikuj=lambda s: None)
    assert naklady.stav(con, teraz=PONDELOK)["kredit"]["vycerpany"] is False


# ------------------------------------------------------------------ INCIDENT
def test_incident_n_odmietnutych_volani_nezmeni_ani_ledger_ani_pocitadlo(con):
    """Presná rekonštrukcia incidentu 24. 8. 2026 na module nákladov.

    Dvanásť pokusov (dozorca 6× denne, dva dni). Každý si vypýta miesto v
    týždennom počte behov, spustí volanie a API ho okamžite odmietne pre nulový
    kredit. Po nich musí byť evidencia PRESNE taká, ako pred nimi: nula eur,
    nula zabratých behov — a jedno jediné upozornenie.
    """
    poslane = []
    for hodina in range(12):
        teraz = PONDELOK + datetime.timedelta(hours=hodina)
        naklady.rezervuj_beh(con, "zber_letakov", teraz=teraz)
        try:
            naklady.s_rozpoctom(
                con, "zber_letakov", "claude-opus-5",
                lambda: (_ for _ in ()).throw(bad_request_kredit()),
                teraz=teraz, notifikuj=poslane.append,
            )
        except naklady.KreditVycerpany:
            naklady.uvolni_beh(con, "zber_letakov", teraz=teraz)

    assert con.execute("SELECT COUNT(*) FROM naklady").fetchone()[0] == 0
    assert con.execute("SELECT COALESCE(SUM(eur),0) FROM naklady").fetchone()[0] == 0
    stav = naklady.stav(con, teraz=PONDELOK)
    assert stav["behy"]["zber_letakov"]["pocet"] == 0, "zbierač musí ostať spustiteľný"
    assert stav["dnes_eur"] == 0 and stav["mesiac_eur"] == 0
    assert len(poslane) == 1


def test_stropy_na_minanie_ostavaju_v_platnosti(con, monkeypatch):
    """Oprava sa nesmie dať zneužiť na obídenie stropov — tie fungovali."""
    monkeypatch.setenv("UVARSI_DENNY_STROP_EUR", "0.40")
    naklady.zapis(con, "plan", "claude-opus-5", VISION_USAGE, teraz=PONDELOK,
                  notifikuj=lambda s: None)
    with pytest.raises(naklady.RozpocetVycerpany) as chyba:
        naklady.skontroluj(con, "plan", teraz=PONDELOK)
    assert chyba.value.kod == naklady.KOD_DENNY


# ------------------------------------------------------------------ viditeľnosť
def test_stav_prizna_ze_api_odmieta_pre_nulovy_kredit(con):
    with pytest.raises(naklady.KreditVycerpany):
        naklady.s_rozpoctom(con, "blocek", "claude-sonnet-5",
                            lambda: (_ for _ in ()).throw(bad_request_kredit()),
                            teraz=PONDELOK, notifikuj=lambda s: None)

    kredit = naklady.stav(con, teraz=PONDELOK)["kredit"]
    assert kredit["vycerpany"] is True
    assert kredit["od"], "musí byť vidieť, odkedy API odmieta"
    assert "kredit" in kredit["sprava"].lower()


def test_stav_na_zdravej_evidencii_nehlasi_vycerpany_kredit(con):
    assert naklady.stav(con, teraz=PONDELOK)["kredit"]["vycerpany"] is False


def test_stav_prizna_kredit_aj_ked_je_evidencia_pokazena(tmp_path):
    """Diagnostika nesmie mlčať práve vtedy, keď je najviac potrebná."""
    spojenie = naklady.pripoj(tmp_path / "uvarsi.db")
    spojenie.execute("DROP TABLE naklady")
    spojenie.commit()
    stav = naklady.stav(spojenie, teraz=PONDELOK)
    assert "kredit" in stav
    spojenie.close()


# ------------------------------------------------------------------ oprava stavu
def poskodena_evidencia(con, pocet_behov=2, volani=3):
    """Presne to, čo po incidente ostalo v produkčnej databáze."""
    for _ in range(pocet_behov):
        naklady.rezervuj_beh(con, "zber_letakov", teraz=PONDELOK)
    for _ in range(volani):
        naklady.zapis(con, "zber_letakov", "claude-opus-5", None, teraz=PONDELOK,
                      detail="zlyhalo: BadRequestError", notifikuj=lambda s: None)


def test_oprava_vynuluje_falosnu_utratu_aj_zabrate_behy(con):
    poskodena_evidencia(con)
    assert naklady.stav(con, teraz=PONDELOK)["behy"]["zber_letakov"]["pocet"] == 2

    vysledok = naklady.oprav_kredit(con, teraz=PONDELOK, vykonaj=True)

    stav = naklady.stav(con, teraz=PONDELOK)
    assert stav["dnes_eur"] == pytest.approx(0.0, abs=1e-9)
    assert stav["behy"]["zber_letakov"]["pocet"] == 0
    assert vysledok["stornovanych"] == 3
    assert vysledok["vratene_behy"]["zber_letakov"] == 2


def test_oprava_nezmaze_historiu_iba_ju_protizapise(con):
    poskodena_evidencia(con, volani=2)
    naklady.oprav_kredit(con, teraz=PONDELOK, vykonaj=True)

    riadky = con.execute("SELECT eur, detail FROM naklady ORDER BY id").fetchall()
    assert len(riadky) == 4, "pôvodné pokusy zostávajú, pribudli protizápisy"
    assert sum(r["eur"] for r in riadky) == pytest.approx(0.0, abs=1e-9)
    assert any("storno" in (r["detail"] or "").lower() for r in riadky)
    assert any("BadRequestError" in (r["detail"] or "") for r in riadky)


def test_oprava_je_idempotentna(con):
    poskodena_evidencia(con)
    naklady.oprav_kredit(con, teraz=PONDELOK, vykonaj=True)
    po_prvej = con.execute("SELECT COUNT(*) FROM naklady").fetchone()[0]

    druha = naklady.oprav_kredit(con, teraz=PONDELOK, vykonaj=True)

    assert druha["stornovanych"] == 0
    assert con.execute("SELECT COUNT(*) FROM naklady").fetchone()[0] == po_prvej
    assert naklady.stav(con, teraz=PONDELOK)["dnes_eur"] == pytest.approx(0.0, abs=1e-9)


def test_nahlad_opravy_nic_nemeni(con):
    poskodena_evidencia(con)
    vysledok = naklady.oprav_kredit(con, teraz=PONDELOK)          # bez --vykonaj

    assert vysledok["stornovanych"] == 3
    assert vysledok["vykonane"] is False
    assert con.execute("SELECT COUNT(*) FROM naklady").fetchone()[0] == 3
    assert naklady.stav(con, teraz=PONDELOK)["behy"]["zber_letakov"]["pocet"] == 2


def test_oprava_sa_nedotkne_skutocnej_spotreby(con):
    """Poctivo zaplatený beh sa nesmie „opraviť" preč — to by bola diera."""
    poskodena_evidencia(con, pocet_behov=1, volani=1)
    naklady.zapis(con, "zber_letakov", "claude-opus-5", VISION_USAGE, teraz=PONDELOK,
                  notifikuj=lambda s: None)
    naklady.zapis(con, "plan", "claude-sonnet-5", None, teraz=PONDELOK,
                  detail="zlyhalo: APITimeoutError", notifikuj=lambda s: None)

    naklady.oprav_kredit(con, teraz=PONDELOK, vykonaj=True)

    skutocna = naklady.cena_eur("claude-opus-5", vstup=80_000, vystup=2_000)
    timeout_odhad = naklady.ODHAD_EUR["plan"]
    assert naklady.spolu_za_den(con, "2026-08-17") == pytest.approx(
        skutocna + timeout_odhad, abs=1e-9)
    # v týždni ostala skutočná útrata → miesto v behoch sa NEVRACIA
    assert naklady.stav(con, teraz=PONDELOK)["behy"]["zber_letakov"]["pocet"] == 1


def test_oprava_sa_da_spustit_z_prikazoveho_riadku(tmp_path, capsys):
    cesta = tmp_path / "uvarsi.db"
    spojenie = naklady.pripoj(cesta)
    poskodena_evidencia(spojenie)
    spojenie.close()

    assert naklady.cli(["--oprav-kredit", "--db", str(cesta),
                        "--tyzden", "2026-08-17"]) == 0
    assert "náhľad" in capsys.readouterr().out.lower()

    assert naklady.cli(["--oprav-kredit", "--vykonaj", "--db", str(cesta),
                        "--tyzden", "2026-08-17"]) == 0
    vypis = capsys.readouterr().out
    assert "3" in vypis

    spojenie = naklady.pripoj(cesta)
    assert naklady.stav(spojenie, teraz=PONDELOK)["behy"]["zber_letakov"]["pocet"] == 0
    spojenie.close()


# ------------------------------------------------------------------ zbierač
@pytest.fixture
def collector(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "app"))
    sys.modules.pop("zbierac_akcii", None)
    return importlib.import_module("zbierac_akcii")


def priprav_zbierac(monkeypatch, tmp_path, collector, create):
    database = tmp_path / "uvarsi.db"
    monkeypatch.setattr(collector, "DB", str(database))
    monkeypatch.setattr(collector, "monday", lambda: "2026-08-17")
    monkeypatch.setattr(collector, "load_key", lambda: "unused-test-value")
    # Zbierač importuje `naklady` ako top-level modul, test cez `app.naklady` —
    # sú to dva objekty tej istej triedy, tak sa umlčia oba.
    monkeypatch.setattr(naklady, "posli_ntfy", lambda sprava: None)
    monkeypatch.setattr(collector.naklady, "posli_ntfy", lambda sprava: None)
    monkeypatch.setitem(sys.modules, "anthropic", types.SimpleNamespace(
        Anthropic=lambda **kw: types.SimpleNamespace(
            messages=types.SimpleNamespace(create=create))))
    return database


def test_zbierac_pri_nulovom_kredite_nespotrebuje_tyzdenny_beh(monkeypatch, tmp_path, collector):
    """Incident v priamom prenose: zbierač nesmie prísť o behy zadarmo."""
    volania = []

    def create(**kw):
        volania.append(kw)
        raise bad_request_kredit()

    database = priprav_zbierac(monkeypatch, tmp_path, collector, create)
    monkeypatch.setattr(collector, "zbieraj", lambda client, store: client.messages.create(
        model="claude-opus-5", max_tokens=100, messages=[]))

    for _ in range(6):                      # dozorca skúša každú hodinu
        with pytest.raises(SystemExit) as koniec:
            collector.main()
        assert "kredit" in str(koniec.value).lower()

    spojenie = naklady.pripoj(database)
    assert spojenie.execute("SELECT COALESCE(SUM(eur),0) FROM naklady").fetchone()[0] == 0
    assert naklady.stav(spojenie)["behy"]["zber_letakov"]["pocet"] == 0
    spojenie.close()
    assert len(volania) == 6, "každý pokus končí na PRVOM odmietnutí, nešaltuje po obchodoch"


def test_zbieraj_neschova_odmietnutie_za_zlyhanie_jedneho_obchodu(con, collector, monkeypatch):
    """Zbierač inak každú chybu zabalí na „obchod zlyhal" a pokračuje ďalším.

    Pri nulovom kredite by to znamenalo tri márne volania namiesto jedného a
    tri riadky „fail" v zber_stav, hoci s obchodmi nie je nič.
    """
    strany = [(f"https://t/{n}.jpg", f"https://f/{n}.jpg") for n in (1, 2)]
    manifest = {
        "source_url": "https://letak.example/lidl",
        "valid_from": "2026-08-17", "valid_to": "2026-08-23",
        "pages": [{"source_page": i + 1, "thumbnail_url": t, "image_url": f}
                  for i, (t, f) in enumerate(strany)],
    }
    monkeypatch.setattr(collector, "store_pages", lambda store: (strany, manifest))
    monkeypatch.setattr(collector, "get_b64", lambda url, px: "AAAA")

    class Klient:
        def __init__(self):
            self.messages = types.SimpleNamespace(
                create=lambda **kw: (_ for _ in ()).throw(bad_request_kredit()))

    # Klient musí byť z toho istého objektu modulu, aký používa zbierač.
    strazeny = collector.naklady.strazeny_klient(con, Klient(), "zber_letakov",
                                                 notifikuj=lambda sprava: None)
    with pytest.raises(collector.naklady.KreditVycerpany):
        collector.zbieraj(strazeny, "lidl")


# ------------------------------------------------------------------ bloček
@pytest.fixture
def refresh():
    from hetzner import refresh_blocek

    return refresh_blocek


def test_blocek_pri_nulovom_kredite_konci_strukturalnym_kodom(monkeypatch, refresh, capsys):
    def odmietni(path, database, compose, today):
        raise naklady.KreditVycerpany()

    monkeypatch.setattr(sys, "argv", ["refresh_blocek.py"])
    monkeypatch.setattr(refresh, "refresh_from_db", odmietni)

    with pytest.raises(SystemExit) as koniec:
        refresh.main()

    assert koniec.value.code == StructuralFailure.EXIT_CODE
    assert koniec.value.code != refresh.EXIT_RETRY
    chyby = capsys.readouterr().err
    assert refresh.MARKER_KREDIT in chyby, "dozorca to musí vedieť strojovo prečítať"
    assert "DOČASNÁ CHYBA" not in chyby


def test_blocek_pri_nulovom_kredite_neprepise_stary_json(monkeypatch, tmp_path, refresh):
    vystup = tmp_path / "landing_data.json"
    vystup.write_text(json.dumps({"stary": True}), encoding="utf-8")

    monkeypatch.setattr(sys, "argv", ["refresh_blocek.py"])
    monkeypatch.setattr(refresh, "refresh_from_db",
                        lambda *a, **kw: (_ for _ in ()).throw(naklady.KreditVycerpany()))
    with pytest.raises(SystemExit):
        refresh.main()

    assert json.loads(vystup.read_text(encoding="utf-8")) == {"stary": True}


# ------------------------------------------------------------------ dozorca
@pytest.fixture(scope="module")
def dozorca() -> str:
    return (ROOT / "hetzner" / "dozorca.sh").read_text(encoding="utf-8")


def test_dozorca_pozna_marker_vycerpaneho_kreditu(dozorca):
    from hetzner import refresh_blocek

    assert refresh_blocek.MARKER_KREDIT in dozorca, (
        "dozorca musí rozoznať, že pád bol pre nulový kredit"
    )


def test_dozorca_po_vycerpanom_kredite_dalsie_pokusy_nespusta(dozorca):
    assert re.search(r'BLOKNUTE_NA["\s]*=?["\s]*.*KREDIT|"KREDIT"', dozorca), (
        "kredit sa musí zapísať do stavu ako vlastný blok"
    )
    assert re.search(r'\[\s*"\$BLOKNUTE_NA"\s*=\s*"KREDIT"\s*\]', dozorca), (
        "pri zapísanom kreditovom bloku sa refresh nesmie ani spustiť"
    )


def test_dozorca_neposiela_vlastnu_notifikaciu_o_kredite(dozorca):
    """Upozornenie posiela `naklady` presne raz — dozorca ho nesmie zdvojiť."""
    assert "notify_kredit_preskoc" in dozorca or "# kredit: upozornenie posiela naklady.py" in dozorca


# ------------------------------------------------------------------ používateľ
def zaloz_pouzivatela(server):
    with server.db() as con:
        con.execute("INSERT INTO pouzivatelia (id,email) VALUES (1,'a@b.sk')")
        insert_hashed_session(server, con, "session-token", 1)
        con.commit()


def klient_ktoremu_dosiel_kredit():
    class Messages:
        def create(self, **kwargs):
            raise bad_request_kredit()

    class Anthropic:
        def __init__(self, **kwargs):
            self.messages = Messages()

    return types.SimpleNamespace(Anthropic=Anthropic)


def test_pouzivatel_dostane_pravdivu_slovensku_hlasku_nie_vymysleny_plan(monkeypatch, tmp_path):
    server = load_server(monkeypatch, tmp_path, current_plan_rows())
    zaloz_pouzivatela(server)
    monkeypatch.setattr(naklady, "posli_ntfy", lambda sprava: None)
    monkeypatch.setitem(sys.modules, "anthropic", klient_ktoremu_dosiel_kredit())
    from fastapi.testclient import TestClient

    client = TestClient(server.app, raise_server_exceptions=False)
    client.cookies.set(server.COOKIE, "session-token")
    odpoved = client.post("/api/plan/generuj?force=1")

    assert odpoved.status_code == 503, "nie 500 — nie je to náhodný pád servera"
    detail = odpoved.json()["detail"]
    assert "kredit" in detail.lower()
    assert "jedálniček" in detail.lower() or "plán" in detail.lower()
    assert "€" not in detail, "nič sa neminulo, euro číslo by klamalo"
    assert "jedla" not in odpoved.text, "žiadny vymyslený plán"


def test_odmietnutie_pre_kredit_nezoberie_ledger_ani_denny_prepocet(monkeypatch, tmp_path):
    server = load_server(monkeypatch, tmp_path, current_plan_rows())
    zaloz_pouzivatela(server)
    monkeypatch.setattr(naklady, "posli_ntfy", lambda sprava: None)
    monkeypatch.setitem(sys.modules, "anthropic", klient_ktoremu_dosiel_kredit())
    from fastapi.testclient import TestClient

    client = TestClient(server.app, raise_server_exceptions=False)
    client.cookies.set(server.COOKIE, "session-token")
    for _ in range(3):
        client.post("/api/plan/generuj?force=1")

    with server.db() as con:
        assert server.pouzite_prepocty(con, 1, server.dnesok()) == 0
    spojenie = sqlite3.connect(tmp_path / "uvarsi.db")
    assert spojenie.execute("SELECT COALESCE(SUM(eur),0) FROM naklady").fetchone()[0] == 0
    spojenie.close()


def test_chybajuce_akcie_pri_nulovom_kredite_nesľubuju_ze_to_bude_o_chvilu(monkeypatch, tmp_path):
    """Zber letákov nemá ako dobehnúť — „skús to o chvíľu" by bolo klamstvo."""
    server = load_server(monkeypatch, tmp_path, [])          # zber nemohol bežať
    zaloz_pouzivatela(server)
    spojenie = naklady.pripoj(tmp_path / "uvarsi.db")
    naklady.zapamataj_kredit(spojenie, ucel="zber_letakov")
    spojenie.close()
    from fastapi.testclient import TestClient

    client = TestClient(server.app, raise_server_exceptions=False)
    client.cookies.set(server.COOKIE, "session-token")
    odpoved = client.post("/api/plan/generuj?force=1")

    assert odpoved.status_code == 503
    detail = odpoved.json()["detail"]
    assert "kredit" in detail.lower()
    assert "o chvíľu" not in detail.lower()


def test_chybajuce_akcie_bez_problemu_s_kreditom_hovoria_po_starom(monkeypatch, tmp_path):
    server = load_server(monkeypatch, tmp_path, [])
    zaloz_pouzivatela(server)
    from fastapi.testclient import TestClient

    client = TestClient(server.app, raise_server_exceptions=False)
    client.cookies.set(server.COOKIE, "session-token")
    odpoved = client.post("/api/plan/generuj?force=1")

    assert odpoved.json()["detail"] == "Aktuálne letákové dáta sa obnovujú. Skús to o chvíľu."


def test_health_a_naklady_povedia_ze_api_odmieta_pre_kredit(monkeypatch, tmp_path):
    monkeypatch.setenv("UVARSI_ADMIN_EMAILS", "a@b.sk")
    server = load_server(monkeypatch, tmp_path, current_plan_rows())
    zaloz_pouzivatela(server)
    monkeypatch.setattr(naklady, "posli_ntfy", lambda sprava: None)
    monkeypatch.setitem(sys.modules, "anthropic", klient_ktoremu_dosiel_kredit())
    from fastapi.testclient import TestClient

    client = TestClient(server.app, raise_server_exceptions=False)
    client.cookies.set(server.COOKIE, "session-token")
    client.post("/api/plan/generuj?force=1")

    zdravie = TestClient(server.app).get("/api/health").json()
    assert zdravie["naklady"]["kredit"]["vycerpany"] is True
    assert zdravie["naklady"]["dnes_eur"] == 0, "nič sa neminulo — žiadne falošné euro"

    prehlad = client.get("/api/naklady").json()
    assert prehlad["kredit"]["vycerpany"] is True
    assert "kredit" in prehlad["kredit"]["sprava"].lower()
