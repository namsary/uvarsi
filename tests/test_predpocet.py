"""Predpočítanie plánov: v noci hotové, ráno okamžité.

Zbierač letákov dobehne v pondelok nadránom. Od tej chvíle sú ponuky týždňa
dané a KAŽDÝ zdieľaný plán sa dá poskladať — len o ňom ešte nikto nepožiadal.
Doteraz sa preto prvý človek s daným profilom díval 60–120 sekúnd na točiace sa
koliesko a zaplatil volanie, ktoré mohlo prebehnúť o tretej ráno, keď nikto
nečaká.

Tieto testy držia štyri sľuby, na ktorých celá vec stojí:

  1. predpočet sa smie spustiť dvakrát a druhýkrát nesmie minúť ani cent,
  2. nikdy nezje rozpočet živým používateľom — pred stropom zastane sám,
  3. zahriaty profil sa naozaj podá z cache a model sa nezavolá,
  4. keď predpočet zlyhá, vypne sa alebo sa preskočí, appka skladá plány
     naživo presne ako doteraz.
"""
import importlib
import sys
import types
from contextlib import closing
from datetime import date, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.weekly_data import current_monday
from tests.test_server import (
    current_plan_rows,
    fake_anthropic,
    insert_hashed_session,
    load_server,
    model_plan,
)


ROOT = Path(__file__).resolve().parents[1]

# Prostredie vývojára nesmie meniť výsledok testov o stropoch a počtoch.
PREMENNE = (
    "UVARSI_DENNY_STROP_EUR",
    "UVARSI_MESACNY_STROP_EUR",
    "UVARSI_TYZDENNY_STROP_ZBER_EUR",
    "UVARSI_TYZDENNE_BEHY_ZBER",
    "UVARSI_TYZDENNY_STROP_PREDPOCET_EUR",
    "UVARSI_TYZDENNE_BEHY_PREDPOCET",
    "UVARSI_PREDPOCET",
    "UVARSI_PREDPOCET_PROFILOV",
    "UVARSI_PREDPOCET_REZERVA_EUR",
)


@pytest.fixture(autouse=True)
def ciste_prostredie(monkeypatch):
    for nazov in PREMENNE:
        monkeypatch.delenv(nazov, raising=False)


# ------------------------------------------------------------------ pomocníci
def priprav(monkeypatch, tmp_path, rows=None):
    """Server nad testovacou databázou + modul predpočtu, ktorý ho používa."""
    server = load_server(
        monkeypatch, tmp_path, current_plan_rows() if rows is None else rows)
    return server, importlib.import_module("predpocet")


def uzivatel(server, user_id=1, obchody="Lidl", osoby=4, frekvencia=2):
    with closing(server.db()) as con:
        con.execute(
            "INSERT INTO pouzivatelia (id, email, osoby, frekvencia, obchody)"
            " VALUES (?, ?, ?, ?, ?)",
            (user_id, f"u{user_id}@uvar.si", osoby, frekvencia, obchody),
        )
        insert_hashed_session(server, con, f"session-{user_id}", user_id)
        con.commit()
    return user_id


def klient_pouzivatela(server, user_id=1):
    client = TestClient(server.app)
    client.cookies.set(server.COOKIE, f"session-{user_id}")
    return client


def minuly_tyzden():
    return current_monday(date.today() - timedelta(days=7))


def zapis_dopyt(server, predpocet, tyzden, obchody="Lidl", osoby=4, frekvencia=2,
                variant=1, kolko=5):
    with closing(server.db()) as con:
        for _ in range(kolko):
            predpocet.zaznamenaj_dopyt(
                con, tyzden, obchody.split(","), osoby, frekvencia, variant)
        con.commit()


def zakazany_anthropic(volania):
    class Zakazany:
        def __init__(self, *args, **kwargs):
            volania.append((args, kwargs))

    return types.SimpleNamespace(Anthropic=Zakazany)


def pocet_zdielanych(server):
    with closing(server.db()) as con:
        return con.execute("SELECT COUNT(*) FROM plany_zdielane").fetchone()[0]


# ------------------------------------------------------------- 1. idempotencia
def test_druhy_beh_predpoctu_nezavola_model_ani_raz(monkeypatch, tmp_path):
    """Cron aj dozorca môžu predpočet spustiť viackrát — druhý raz je zadarmo."""
    server, predpocet = priprav(monkeypatch, tmp_path)
    zapis_dopyt(server, predpocet, minuly_tyzden())
    volania = []
    monkeypatch.setitem(sys.modules, "anthropic", fake_anthropic(model_plan(), [], volania))

    prvy = predpocet.zahrej(pocet=3)
    po_prvom = len(volania)
    druhy = predpocet.zahrej(pocet=3)

    assert prvy["zahriatych"] == 3, prvy
    assert po_prvom == 3, "prvý beh musí poskladať práve toľko plánov, koľko sa žiada"
    assert len(volania) == po_prvom, "druhý beh nesmie zaplatiť ani jedno volanie"
    assert druhy["zahriatych"] == 0 and druhy["preskocenych"] == 3
    assert pocet_zdielanych(server) == 3, "druhý beh nesmie vyrobiť duplicitné riadky"


def test_predpocet_preskoci_podpis_ktory_uz_niekto_vygeneroval(monkeypatch, tmp_path):
    """Keď plán poskladal živý používateľ, predpočet ho už neplatí druhýkrát."""
    server, predpocet = priprav(monkeypatch, tmp_path)
    user_id = uzivatel(server)
    zapis_dopyt(server, predpocet, minuly_tyzden(), variant=user_id % 3)
    volania = []
    monkeypatch.setitem(sys.modules, "anthropic", fake_anthropic(model_plan(), [], volania))
    assert klient_pouzivatela(server, user_id).post("/api/plan/generuj").status_code == 200
    assert len(volania) == 1

    vysledok = predpocet.zahrej(pocet=1)

    assert vysledok["preskocenych"] == 1 and vysledok["zahriatych"] == 0
    assert len(volania) == 1, "hotový zdieľaný plán sa nesmie skladať znova"


# ---------------------------------------------------------------- 2. rozpočet
def test_predpocet_zastane_pred_dennym_stropom_a_povie_preco(monkeypatch, tmp_path):
    """Rozbehnutý predpočet nesmie minúť denný rozpočet do posledného centa."""
    server, predpocet = priprav(monkeypatch, tmp_path)
    zapis_dopyt(server, predpocet, minuly_tyzden())
    monkeypatch.setenv("UVARSI_DENNY_STROP_EUR", "0.25")
    monkeypatch.setenv("UVARSI_PREDPOCET_REZERVA_EUR", "0.20")
    volania = []
    monkeypatch.setitem(sys.modules, "anthropic", fake_anthropic(model_plan(), [], volania))

    vysledok = predpocet.zahrej(pocet=9)

    assert vysledok["dovod"] == "rozpocet", vysledok
    assert 0 < vysledok["zahriatych"] < 9, "musí niečo stihnúť, ale nie všetko"
    assert len(volania) == vysledok["zahriatych"]
    with closing(server.db()) as con:
        minute = con.execute("SELECT COALESCE(SUM(eur), 0) FROM naklady").fetchone()[0]
    assert minute <= 0.05 + 1e-9, (
        f"predpočet minul {minute:.3f} € z 0,25 € stropu — rezerva 0,20 € pre "
        "živých používateľov musí ostať nedotknutá"
    )


def test_po_predpocte_ostane_ziveho_pouzivatela_z_coho_zaplatit(monkeypatch, tmp_path):
    """Zmysel rezervy: ráno si človek musí vedieť vypýtať vlastný plán."""
    server, predpocet = priprav(monkeypatch, tmp_path)
    zapis_dopyt(server, predpocet, minuly_tyzden())
    monkeypatch.setenv("UVARSI_DENNY_STROP_EUR", "0.25")
    monkeypatch.setenv("UVARSI_PREDPOCET_REZERVA_EUR", "0.20")
    monkeypatch.setitem(sys.modules, "anthropic", fake_anthropic(model_plan(), [], []))

    predpocet.zahrej(pocet=9)

    naklady = importlib.import_module("naklady")
    with closing(server.db()) as con:
        naklady.skontroluj(con, "plan")      # nesmie vyhodiť RozpocetVycerpany


def test_predpocet_respektuje_tyzdenny_pocet_behov(monkeypatch, tmp_path):
    """Štrukturálna poistka: rozbehnutý cron narazí na strop počtu behov."""
    server, predpocet = priprav(monkeypatch, tmp_path)
    zapis_dopyt(server, predpocet, minuly_tyzden())
    monkeypatch.setenv("UVARSI_TYZDENNE_BEHY_PREDPOCET", "1")
    volania = []
    monkeypatch.setitem(sys.modules, "anthropic", fake_anthropic(model_plan(), [], volania))

    prvy = predpocet.zahrej(pocet=1)
    druhy = predpocet.zahrej(pocet=1)

    assert prvy["zahriatych"] == 1
    assert druhy["dovod"] == "behy" and druhy["zahriatych"] == 0
    assert len(volania) == 1


def test_predpocet_ma_vlastny_tyzdenny_strop_v_eurach(monkeypatch, tmp_path):
    server, predpocet = priprav(monkeypatch, tmp_path)
    zapis_dopyt(server, predpocet, minuly_tyzden())
    monkeypatch.setenv("UVARSI_TYZDENNY_STROP_PREDPOCET_EUR", "0.05")
    volania = []
    monkeypatch.setitem(sys.modules, "anthropic", fake_anthropic(model_plan(), [], volania))

    vysledok = predpocet.zahrej(pocet=9)

    assert vysledok["dovod"] == "rozpocet"
    assert len(volania) <= 3, "vlastný týždenný strop účelu musí beh zastaviť"


# ------------------------------------------------------- 3. zásah do cache
def test_zahriaty_profil_sa_podava_bez_jedineho_volania_modelu(monkeypatch, tmp_path):
    """Toto je celý zmysel predpočtu: ráno nula sekúnd a nula centov."""
    server, predpocet = priprav(monkeypatch, tmp_path)
    user_id = uzivatel(server)
    zapis_dopyt(server, predpocet, minuly_tyzden(), variant=user_id % 3)
    monkeypatch.setitem(sys.modules, "anthropic", fake_anthropic(model_plan(), [], []))
    assert predpocet.zahrej(pocet=1)["zahriatych"] == 1

    zakazane = []
    monkeypatch.setitem(sys.modules, "anthropic", zakazany_anthropic(zakazane))
    odpoved = klient_pouzivatela(server, user_id).post("/api/plan/generuj")

    assert odpoved.status_code == 200
    assert zakazane == [], "zahriaty plán sa nesmie skladať znova"
    assert odpoved.json()["jedla"], "z cache musí prísť skutočný jedálniček"


def test_zasah_do_predpocitaneho_planu_je_vidiet_v_prehlade(monkeypatch, tmp_path):
    """Majiteľ musí vedieť, koľko živých generovaní predpočet ušetril."""
    server, predpocet = priprav(monkeypatch, tmp_path)
    user_id = uzivatel(server)
    zapis_dopyt(server, predpocet, minuly_tyzden(), variant=user_id % 3)
    monkeypatch.setitem(sys.modules, "anthropic", fake_anthropic(model_plan(), [], []))
    predpocet.zahrej(pocet=1)

    monkeypatch.setitem(sys.modules, "anthropic", zakazany_anthropic([]))
    klient_pouzivatela(server, user_id).post("/api/plan/generuj")

    prehlad = TestClient(server.app).get("/api/naklady").json()["predpocet"]
    assert prehlad["usetrenych_generovani"] == 1, prehlad


# --------------------------------------------------------- 4. zlyhanie je neškodné
def test_zlyhanie_predpoctu_nezhodi_beh_a_zive_generovanie_funguje(monkeypatch, tmp_path):
    server, predpocet = priprav(monkeypatch, tmp_path)
    user_id = uzivatel(server)
    zapis_dopyt(server, predpocet, minuly_tyzden(), variant=user_id % 3)

    class Rozbity:
        def __init__(self, **kwargs):
            self.messages = self

        def create(self, **kwargs):
            raise RuntimeError("model je nedostupný")

    monkeypatch.setitem(sys.modules, "anthropic", types.SimpleNamespace(Anthropic=Rozbity))
    vysledok = predpocet.zahrej(pocet=2)          # nesmie vyhodiť výnimku

    assert vysledok["zahriatych"] == 0 and vysledok["zlyhanych"] >= 1
    assert pocet_zdielanych(server) == 0, "nedokončený plán sa nesmie uložiť"

    volania = []
    monkeypatch.setitem(sys.modules, "anthropic", fake_anthropic(model_plan(), [], volania))
    odpoved = klient_pouzivatela(server, user_id).post("/api/plan/generuj")

    assert odpoved.status_code == 200 and len(volania) == 1


def test_vypnuty_predpocet_nespusti_nic(monkeypatch, tmp_path):
    server, predpocet = priprav(monkeypatch, tmp_path)
    zapis_dopyt(server, predpocet, minuly_tyzden())
    monkeypatch.setenv("UVARSI_PREDPOCET", "0")
    zakazane = []
    monkeypatch.setitem(sys.modules, "anthropic", zakazany_anthropic(zakazane))

    vysledok = predpocet.zahrej(pocet=9)

    assert vysledok["dovod"] == "vypnute" and vysledok["zahriatych"] == 0
    assert zakazane == []


def test_nula_profilov_je_platne_vypnutie(monkeypatch, tmp_path):
    server, predpocet = priprav(monkeypatch, tmp_path)
    zapis_dopyt(server, predpocet, minuly_tyzden())
    monkeypatch.setenv("UVARSI_PREDPOCET_PROFILOV", "0")
    zakazane = []
    monkeypatch.setitem(sys.modules, "anthropic", zakazany_anthropic(zakazane))

    vysledok = predpocet.zahrej()

    assert vysledok["zahriatych"] == 0 and zakazane == []


def test_predpocet_mlci_ked_v_databaze_nie_su_ponuky(monkeypatch, tmp_path):
    """Bez letákov niet z čoho skladať — a nie je to dôvod na paniku ani na platbu."""
    server, predpocet = priprav(monkeypatch, tmp_path, rows=[])
    zakazane = []
    monkeypatch.setitem(sys.modules, "anthropic", zakazany_anthropic(zakazane))

    vysledok = predpocet.zahrej(pocet=3)

    assert vysledok["zahriatych"] == 0 and zakazane == []


# ------------------------------------------------------------ výber profilov
def test_prvy_tyzden_bez_historie_pouzije_vychodzie_profily(monkeypatch, tmp_path):
    server, predpocet = priprav(monkeypatch, tmp_path)

    with closing(server.db()) as con:
        profily = predpocet.oblubene_profily(con, current_monday(), 5)

    assert len(profily) == 5
    assert profily[0] == predpocet.vychodzie_profily()[0], (
        "bez histórie musí ísť na začiatok najpravdepodobnejší profil"
    )
    assert len({(p.obchody, p.osoby, p.frekvencia, p.variant) for p in profily}) == 5


def test_dalsie_tyzdne_riadi_skutocny_dopyt_pouzivatelov(monkeypatch, tmp_path):
    """Zoznam na zahriatie sa učí sám z toho, o čo ľudia naozaj žiadali."""
    server, predpocet = priprav(monkeypatch, tmp_path)
    zapis_dopyt(server, predpocet, minuly_tyzden(), obchody="Lidl", osoby=2,
                frekvencia=3, variant=2, kolko=9)
    zapis_dopyt(server, predpocet, minuly_tyzden(), obchody="Tesco", osoby=6,
                frekvencia=1, variant=0, kolko=3)

    with closing(server.db()) as con:
        profily = predpocet.oblubene_profily(con, current_monday(), 3)

    assert (profily[0].obchody, profily[0].osoby, profily[0].frekvencia,
            profily[0].variant) == (("Lidl",), 2, 3, 2)
    assert (profily[1].obchody, profily[1].osoby) == (("Tesco",), 6)
    assert profily[2] in predpocet.vychodzie_profily(), (
        "keď je história kratšia než N, zvyšok sa doplní východzími profilmi"
    )


def test_dopyt_z_tohto_tyzdna_neprebije_historiu_predoslych(monkeypatch, tmp_path):
    """Zahrieva sa podľa minulých týždňov — tento sa práve len začal."""
    server, predpocet = priprav(monkeypatch, tmp_path)
    zapis_dopyt(server, predpocet, current_monday(), obchody="Tesco", osoby=6,
                frekvencia=1, variant=0, kolko=50)
    zapis_dopyt(server, predpocet, minuly_tyzden(), obchody="Lidl", osoby=2,
                frekvencia=3, variant=2, kolko=2)

    with closing(server.db()) as con:
        profily = predpocet.oblubene_profily(con, current_monday(), 1)

    assert (profily[0].obchody, profily[0].osoby) == (("Lidl",), 2)


def test_pocet_profilov_sa_da_nastavit_z_prostredia(monkeypatch, tmp_path):
    server, predpocet = priprav(monkeypatch, tmp_path)
    assert predpocet.pocet_profilov() == predpocet.VYCHODZI_POCET_PROFILOV

    monkeypatch.setenv("UVARSI_PREDPOCET_PROFILOV", "4")
    assert predpocet.pocet_profilov() == 4


@pytest.mark.parametrize("hodnota", ["-3", "nezmysel", "", "9999"])
def test_pokazeny_alebo_privelky_pocet_sa_ohranici_a_nezhodi_beh(monkeypatch, tmp_path, hodnota):
    """Preklep v prostredí nesmie ani zhodiť beh, ani otvoriť neohraničené míňanie."""
    server, predpocet = priprav(monkeypatch, tmp_path)
    monkeypatch.setenv("UVARSI_PREDPOCET_PROFILOV", hodnota)

    pocet = predpocet.pocet_profilov()

    assert 0 <= pocet <= predpocet.MAX_POCET_PROFILOV


def test_pocet_variantov_sedi_so_serverom(monkeypatch, tmp_path):
    """Keby sa rozišli, zahrialo by sa niečo, čo nikto nedostane."""
    server, predpocet = priprav(monkeypatch, tmp_path)
    assert predpocet.POCET_VARIANTOV == server.PLAN_VARIANTS


# ----------------------------------------------------------------- evidencia dopytu
def test_ziadost_o_plan_zapise_dopyt_profilu(monkeypatch, tmp_path):
    server, predpocet = priprav(monkeypatch, tmp_path)
    user_id = uzivatel(server, osoby=3, frekvencia=1)
    monkeypatch.setitem(sys.modules, "anthropic", fake_anthropic(model_plan(), [], []))

    klient_pouzivatela(server, user_id).post("/api/plan/generuj")

    with closing(server.db()) as con:
        riadky = con.execute(
            "SELECT obchody, osoby, frekvencia, variant, pocet FROM dopyt_profilov"
        ).fetchall()
    assert [tuple(r) for r in riadky] == [("Lidl", 3, 1, user_id % 3, 1)]


def test_evidencia_dopytu_je_agregat_a_nie_zaznam_o_cloveku(monkeypatch, tmp_path):
    """Je to takmer osobný údaj — do databázy patrí počet, nie kto to bol."""
    server, predpocet = priprav(monkeypatch, tmp_path)
    with closing(server.db()) as con:
        stlpce = {r[1] for r in con.execute("PRAGMA table_info(dopyt_profilov)")}

    assert "user_id" not in stlpce and "email" not in stlpce, stlpce
    assert "pocet" in stlpce


def test_evidencia_dopytu_sa_neplni_donekonecna(monkeypatch, tmp_path):
    server, predpocet = priprav(monkeypatch, tmp_path)
    stary = current_monday(date.today() - timedelta(weeks=40))
    zapis_dopyt(server, predpocet, stary, kolko=1)

    zapis_dopyt(server, predpocet, minuly_tyzden(), kolko=1)

    with closing(server.db()) as con:
        tyzdne = {r[0] for r in con.execute("SELECT DISTINCT tyzden FROM dopyt_profilov")}
    assert stary not in tyzdne, "staré týždne sa musia samy upratať"


# ---------------------------------------------------------------- viditeľnosť
def test_prehlad_nakladov_ukazuje_ako_sa_predpoctu_darilo(monkeypatch, tmp_path):
    server, predpocet = priprav(monkeypatch, tmp_path)
    zapis_dopyt(server, predpocet, minuly_tyzden())
    monkeypatch.setitem(sys.modules, "anthropic", fake_anthropic(model_plan(), [], []))
    predpocet.zahrej(pocet=2)

    telo = TestClient(server.app).get("/api/naklady").json()

    assert "predpocet" in telo, "majiteľ musí vidieť, či predpočet vôbec beží"
    p = telo["predpocet"]
    assert p["zahriatych"] == 2
    assert p["eur"] > 0
    assert p["cena_za_profil_eur"] > 0
    assert p["usetrenych_generovani"] == 0
    assert p["dovod"] == "hotovo"


def test_health_ukazuje_predpocet_a_neprepadne_na_cerstvej_databaze(monkeypatch, tmp_path):
    server, _ = priprav(monkeypatch, tmp_path, rows=[])

    odpoved = TestClient(server.app).get("/api/health")

    assert odpoved.status_code == 200
    assert odpoved.json()["predpocet"]["zahriatych"] == 0


def test_prehlad_predpoctu_neprezradi_tajomstva(monkeypatch, tmp_path):
    server, _ = priprav(monkeypatch, tmp_path)
    telo = TestClient(server.app).get("/api/naklady").text
    for zakazane in ("API_KEY", "sk-ant", "re_", "@uvar.si"):
        assert zakazane not in telo


def test_cena_za_profil_je_zname_a_striezlive_cislo(monkeypatch, tmp_path):
    """Koľko stojí jeden zahriaty profil musí byť číslo, nie dojem."""
    server, predpocet = priprav(monkeypatch, tmp_path)
    naklady = importlib.import_module("naklady")

    assert predpocet.CENA_ZA_PROFIL_EUR == naklady.ODHAD_EUR["predpocet"]
    assert 0 < predpocet.CENA_ZA_PROFIL_EUR <= 0.05
    # Plný predpočet nesmie sám osebe zjesť mesačný rozpočet.
    assert predpocet.MAX_POCET_PROFILOV * predpocet.CENA_ZA_PROFIL_EUR <= (
        naklady.VYCHODZI_MESACNY_STROP_EUR / 2
    )


# ------------------------------------------------------------------- napojenie
def test_dozorca_spusta_predpocet_az_po_uspesnom_zbere():
    text = (ROOT / "hetzner" / "dozorca.sh").read_text(encoding="utf-8")
    assert "predpocet.py" in text, (
        "predpočet musí byť napojený na zbierač — inak ho nikto nikdy nespustí"
    )
    zbierac = text.index('"$PY" -u zbierac_akcii.py')
    predpocet_riadok = text.index("predpocet.py")
    zlyhal = text.index("zbierač zlyhal")
    assert zbierac < predpocet_riadok < zlyhal, (
        "predpočet patrí do vetvy „zbierač OK“; nad neúplnými ponukami by "
        "skladal plány, ktoré sa zajtra aj tak zneplatnia"
    )


def test_zlyhanie_predpoctu_nesmie_zmenit_navratovy_kod_dozorcu():
    text = (ROOT / "hetzner" / "dozorca.sh").read_text(encoding="utf-8")
    riadok = [line for line in text.splitlines() if "predpocet.py" in line]
    assert riadok, "očakávam volanie predpočtu v dozorcovi"
    okolie = text[text.index("predpocet.py"):text.index("predpocet.py") + 400]
    assert "||" in okolie, (
        "predpočet je zrýchlenie, nie povinnosť — jeho pád nesmie zhodiť dozorcu"
    )


def test_predpocet_sa_nasadzuje():
    nasad = (ROOT / "nasad.ps1").read_text(encoding="utf-8")
    samopull = (ROOT / "hetzner" / "samopull.sh").read_text(encoding="utf-8")
    assert '"$B\\app\\predpocet.py"' in nasad
    assert "app/predpocet.py" in samopull, (
        "samopull musí vydanie bez predpočtu odmietnuť rovnako ako bez servera"
    )


def test_predpocet_sa_neplete_do_druhej_appky_na_serveri():
    text = (ROOT / "hetzner" / "dozorca.sh").read_text(encoding="utf-8")
    assert "taktik-mapa" not in text and "mapa." not in text
