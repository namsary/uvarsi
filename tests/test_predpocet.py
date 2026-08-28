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
import json
import sqlite3
import sys
import types
from contextlib import closing
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.weekly_data import current_monday
from app import plan_jobs
from tests.test_server import (
    current_plan_rows,
    fake_anthropic,
    insert_hashed_session,
    load_server,
    model_plan,
    plan_offer,
    plan_key,
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


def rows_for_stores(stores, per_store=16):
    fields = (
        "tyzden", "nazov", "obchod", "cena", "povodna", "zlava", "jednotka",
        "kategoria", "source_url", "source_page", "valid_from", "valid_to",
    )
    rows = []
    for store_number, store in enumerate(stores):
        for index in range(1, per_store + 1):
            offer = plan_offer(index)
            offer["obchod"] = store
            offer["source_page"] = store_number * per_store + index
            rows.append(tuple(offer[field] for field in fields))
    return rows


def create_active_user(server, user_id=1, stores="Lidl", adults=4, children=0,
                       frequency=2):
    with closing(server.db()) as con:
        con.execute(
            "INSERT INTO pouzivatelia "
            "(id, email, osoby, dospeli, deti, frekvencia, obchody) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (user_id, f"active-{user_id}@uvar.si", adults + children, adults,
             children, frequency, stores),
        )
        con.commit()


def queued_jobs(server):
    with closing(server.db()) as con:
        return con.execute(
            "SELECT * FROM plan_jobs WHERE state IN ('queued', 'running') "
            "ORDER BY id"
        ).fetchall()


# -------------------------------------------------------- Task 6: queueing
def test_precompute_queues_active_exact_profiles_before_demand_and_defaults(
        monkeypatch, tmp_path):
    server, predpocet = priprav(monkeypatch, tmp_path, rows_for_stores(("Lidl", "Tesco")))
    create_active_user(server, stores="Lidl,Tesco", adults=2, children=2, frequency=3)
    zapis_dopyt(server, predpocet, minuly_tyzden(), obchody="Lidl", osoby=4,
                frekvencia=2, variant=0)
    zakazane = []
    monkeypatch.setitem(sys.modules, "anthropic", zakazany_anthropic(zakazane))

    result = predpocet.enqueue_popular_profiles(
        count=3, now=datetime(2026, 8, 28, 2, 0, 0)
    )
    jobs = queued_jobs(server)

    assert jobs[0]["payload_json"]
    assert json.loads(jobs[0]["payload_json"])["stores"] == ["Lidl", "Tesco"]
    assert all(job["priority"] == 20 for job in jobs)
    assert result["queued"] <= 3
    assert zakazane == []


def test_live_job_claims_before_low_priority_precompute_job(monkeypatch, tmp_path):
    server, predpocet = priprav(monkeypatch, tmp_path)
    create_active_user(server)
    now = datetime(2026, 8, 28, 2, 0, 0)
    predpocet.enqueue_popular_profiles(count=1, now=now)

    with closing(server.db()) as con:
        live = plan_jobs.JobRequest(
            job_key="regular:live:0",
            signature="live",
            variant=0,
            kind="regular",
            user_id=1,
            week="2026-08-24",
            priority=100,
            payload={},
            regeneration_limit=1,
            regeneration_day="2026-08-28",
        )
        live_job = plan_jobs.enqueue(con, live, now=now).job
        claimed = plan_jobs.claim_next(con, "worker", now=now)

    assert claimed.id == live_job.id


def test_precompute_deduplicates_an_active_job_by_signature_and_variant(
        monkeypatch, tmp_path):
    server, predpocet = priprav(monkeypatch, tmp_path)
    create_active_user(server)

    first = predpocet.enqueue_popular_profiles(
        count=1, now=datetime(2026, 8, 28, 2, 0, 0)
    )
    second = predpocet.enqueue_popular_profiles(
        count=1, now=datetime(2026, 8, 28, 2, 1, 0)
    )

    assert first["queued"] == 1
    assert second["queued"] == 0
    assert second["skipped"] == 1
    assert len(queued_jobs(server)) == 1


def test_precompute_skips_a_matching_active_live_job(monkeypatch, tmp_path):
    server, predpocet = priprav(monkeypatch, tmp_path)
    create_active_user(server)
    now = datetime(2026, 8, 28, 2, 0, 0)
    with closing(server.db()) as con:
        rows = server.akcie_pre(["Lidl"])
        profile = predpocet.Profil(("Lidl",), 4, 0, 2, 1)
        signature = predpocet._podpis_pre_profil(server, current_monday(), profile, rows)
        plan_jobs.enqueue(
            con,
            plan_jobs.JobRequest(
                job_key="regular:matching-live",
                signature=signature,
                variant=1,
                kind="regular",
                user_id=1,
                week=current_monday(),
                priority=100,
                payload={},
                regeneration_limit=1,
                regeneration_day="2026-08-28",
            ),
            now=now,
        )

    result = predpocet.enqueue_popular_profiles(count=1, now=now)

    assert result["queued"] == 0
    assert result["skipped"] == 1
    assert len(queued_jobs(server)) == 1


def test_precompute_respects_historical_spend_and_outstanding_reservations(
        monkeypatch, tmp_path):
    server, predpocet = priprav(monkeypatch, tmp_path)
    monkeypatch.setenv("UVARSI_DENNY_STROP_EUR", "0.50")
    monkeypatch.setenv("UVARSI_MESACNY_STROP_EUR", "25.00")
    monkeypatch.setenv("UVARSI_TYZDENNY_STROP_PREDPOCET_EUR", "1.00")
    create_active_user(server)
    now = datetime(2026, 8, 28, 2, 0, 0)
    with closing(server.db()) as con:
        con.execute(
            "INSERT INTO naklady "
            "(cas, den, mesiac, tyzden, ucel, model, eur) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (now.isoformat(), "2026-08-28", "2026-08", "2026-08-24",
             "plan", "historical", 0.10),
        )
        con.commit()

    result = predpocet.enqueue_popular_profiles(count=3, now=now)

    assert result["queued"] == 1
    assert result["blocked"] >= 1
    assert len(queued_jobs(server)) == 1


def test_zahrej_cli_reports_queued_skipped_and_blocked_without_model_call(
        monkeypatch, capsys):
    predpocet = importlib.import_module("predpocet")
    monkeypatch.setattr(predpocet, "enqueue_popular_profiles", lambda **_kw: {
        "tyzden": "2026-08-24", "queued": 2, "skipped": 1, "blocked": 3,
        "profilov": 6, "zahriatych": 0, "preskocenych": 1, "zlyhanych": 0,
        "eur": 0.0, "dovod": predpocet.DOVOD_HOTOVO,
    })

    assert predpocet.cli(["--zahrej"]) == 0
    output = capsys.readouterr().out
    assert "zaradených 2" in output
    assert "preskočených 1" in output
    assert "blokovaných 3" in output


# ------------------------------------------------------------- 1. idempotencia
def test_druhy_beh_predpoctu_nezavola_model_ani_raz(monkeypatch, tmp_path):
    """Cron aj dozorca môžu predpočet spustiť viackrát — druhý raz je idempotný."""
    server, predpocet = priprav(monkeypatch, tmp_path)
    zapis_dopyt(server, predpocet, minuly_tyzden())
    volania = []
    monkeypatch.setitem(sys.modules, "anthropic", fake_anthropic(model_plan(), [], volania))

    prvy = predpocet.zahrej(pocet=3)
    po_prvom = len(volania)
    druhy = predpocet.zahrej(pocet=3)

    assert prvy["queued"] == 3, prvy
    assert po_prvom == 0, "predpočet nesmie volať model"
    assert len(volania) == po_prvom
    assert druhy["queued"] == 0 and druhy["skipped"] == 3
    assert len(queued_jobs(server)) == 3, "druhý beh nesmie vyrobiť duplicitné úlohy"


def test_predpocet_preskoci_podpis_ktory_uz_niekto_vygeneroval(monkeypatch, tmp_path):
    """Keď už existuje zdieľaný plán, predpočet ho znovu nezaradí."""
    server, predpocet = priprav(monkeypatch, tmp_path)
    user_id = uzivatel(server)
    zapis_dopyt(server, predpocet, minuly_tyzden(), variant=user_id % 3)
    with closing(server.db()) as con:
        tyzden = current_monday()
        rows = server.akcie_pre(["Lidl"])
        profil = predpocet.Profil(("Lidl",), 4, 0, 2, user_id % 3)
        podpis = predpocet._podpis_pre_profil(server, tyzden, profil, rows)
        con.execute(
            "INSERT INTO plany_zdielane (podpis, variant, tyzden, json) VALUES (?, ?, ?, ?)",
            (podpis, profil.variant, tyzden, "{}"),
        )
        con.commit()

    vysledok = predpocet.zahrej(pocet=1)

    assert vysledok["skipped"] == 1 and vysledok["queued"] == 0


# ---------------------------------------------------------------- 2. rozpočet
def test_predpocet_zastane_pred_dennym_stropom_a_povie_preco(monkeypatch, tmp_path):
    """Rozbehnutý predpočet nesmie minúť denný rozpočet do posledného centa."""
    server, predpocet = priprav(monkeypatch, tmp_path)
    zapis_dopyt(server, predpocet, minuly_tyzden())
    monkeypatch.setenv("UVARSI_DENNY_STROP_EUR", "0.45")
    monkeypatch.setenv("UVARSI_PREDPOCET_REZERVA_EUR", "0.20")
    volania = []
    monkeypatch.setitem(sys.modules, "anthropic", fake_anthropic(model_plan(), [], volania))

    vysledok = predpocet.zahrej(pocet=9)

    assert vysledok["dovod"] == "rozpocet", vysledok
    assert vysledok["queued"] == 2
    assert vysledok["blocked"] >= 1
    assert len(volania) == 0
    with closing(server.db()) as con:
        minute = con.execute("SELECT COALESCE(SUM(eur), 0) FROM naklady").fetchone()[0]
        reserved = plan_jobs.active_reservations_eur(con)
    assert minute == pytest.approx(0.0)
    assert reserved == pytest.approx(0.24)


def test_po_predpocte_ostane_ziveho_pouzivatela_z_coho_zaplatit(monkeypatch, tmp_path):
    """Zmysel rezervy: ráno si človek musí vedieť vypýtať vlastný plán."""
    server, predpocet = priprav(monkeypatch, tmp_path)
    zapis_dopyt(server, predpocet, minuly_tyzden())
    monkeypatch.setenv("UVARSI_DENNY_STROP_EUR", "0.45")
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

    assert prvy["queued"] == 1
    assert druhy["dovod"] == "behy" and druhy["blocked"] == 1
    assert len(volania) == 0


def test_predpocet_sa_po_uspechu_a_docasnom_pade_moze_zotavit(monkeypatch, tmp_path):
    """Hodinový dozor môže frontu skontrolovať znova bez ďalšieho jobu."""
    server, predpocet = priprav(monkeypatch, tmp_path)
    zapis_dopyt(server, predpocet, minuly_tyzden())
    zakazane = []
    monkeypatch.setitem(sys.modules, "anthropic", zakazany_anthropic(zakazane))
    prvy = predpocet.zahrej(pocet=1)
    druhy = predpocet.zahrej(pocet=1)

    assert prvy["queued"] == 1
    assert druhy["skipped"] == 1
    assert zakazane == []


def test_beh_ktory_nic_neminul_nezabera_miesto_v_tyzdennom_pocte(monkeypatch, tmp_path):
    """Strop počtu behov je poistka proti míňaniu — nie proti zbytočnému behu.

    Keď je všetko zahriate, druhý beh nespotrebuje ani token, takže by bola
    chyba nechať ho zožrať týždňový počet behov: po zmene letáku by sa už
    nemalo čím dohriať.
    """
    server, predpocet = priprav(monkeypatch, tmp_path)
    zapis_dopyt(server, predpocet, minuly_tyzden())
    monkeypatch.setenv("UVARSI_TYZDENNE_BEHY_PREDPOCET", "2")
    volania = []
    monkeypatch.setitem(sys.modules, "anthropic", fake_anthropic(model_plan(), [], volania))

    predpocet.zahrej(pocet=1)                      # zaberie miesto vo fronte
    for _ in range(4):
        predpocet.zahrej(pocet=1)                  # aktívnu úlohu iba preskočí

    assert len(queued_jobs(server)) == 1
    assert len(volania) == 0


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
    assert predpocet.zahrej(pocet=1)["queued"] == 1

    zakazane = []
    monkeypatch.setitem(sys.modules, "anthropic", zakazany_anthropic(zakazane))
    odpoved = klient_pouzivatela(server, user_id).post("/api/plan/generuj")

    assert odpoved.status_code == 202
    assert zakazane == [], "zahriaty plán sa nesmie skladať znova"
    assert odpoved.json()["status"] == "preparing"


def test_zasah_do_predpocitaneho_planu_je_vidiet_v_prehlade(monkeypatch, tmp_path):
    """Majiteľ musí vedieť, koľko živých generovaní predpočet ušetril."""
    server, predpocet = priprav(monkeypatch, tmp_path)
    user_id = uzivatel(server)
    zapis_dopyt(server, predpocet, minuly_tyzden(), variant=user_id % 3)
    monkeypatch.setitem(sys.modules, "anthropic", fake_anthropic(model_plan(), [], []))
    predpocet.zahrej(pocet=1)

    monkeypatch.setitem(sys.modules, "anthropic", zakazany_anthropic([]))
    klient_pouzivatela(server, user_id).post("/api/plan/generuj")

    monkeypatch.setenv("UVARSI_ADMIN_EMAILS", "u1@uvar.si")
    prehlad = klient_pouzivatela(server, user_id).get("/api/naklady").json()["predpocet"]
    assert prehlad["usetrenych_generovani"] == 0, prehlad


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
    vysledok = predpocet.zahrej(pocet=1)          # nesmie vyhodiť výnimku

    assert vysledok["queued"] == 1 and vysledok["zlyhanych"] == 0
    assert pocet_zdielanych(server) == 0, "nedokončený plán sa nesmie uložiť"

    volania = []
    monkeypatch.setitem(sys.modules, "anthropic", fake_anthropic(model_plan(), [], volania))
    odpoved = klient_pouzivatela(server, user_id).post("/api/plan/generuj")

    assert odpoved.status_code == 202 and len(volania) == 0


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
    assert len({
        (p.obchody, p.dospeli, p.deti, p.frekvencia, p.variant) for p in profily
    }) == 5


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


def test_demand_is_aggregated_by_adults_and_children_separately(monkeypatch, tmp_path):
    server, predpocet = priprav(monkeypatch, tmp_path)
    with closing(server.db()) as con:
        predpocet.zaznamenaj_dopyt(
            con, minuly_tyzden(), ["Lidl"], 2, 2, 3, 1
        )
        predpocet.zaznamenaj_dopyt(
            con, minuly_tyzden(), ["Lidl"], 4, 0, 3, 1
        )
        con.commit()
        rows = con.execute(
            "SELECT dospeli, deti, pocet FROM dopyt_profilov ORDER BY deti"
        ).fetchall()

    assert [tuple(row) for row in rows] == [(4, 0, 1), (2, 2, 1)]


def test_default_precompute_profiles_cover_common_mixed_households(monkeypatch, tmp_path):
    _server, predpocet = priprav(monkeypatch, tmp_path)
    households = {(p.dospeli, p.deti) for p in predpocet.vychodzie_profily()}

    assert {(1, 0), (2, 0), (2, 1), (2, 2), (3, 0), (4, 0)}.issubset(households)
    assert all(1 <= adults + children <= 12 for adults, children in households)


def test_legacy_demand_rows_migrate_to_adults_without_losing_counts(monkeypatch, tmp_path):
    _server, predpocet = priprav(monkeypatch, tmp_path)
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.executescript(
        """
        CREATE TABLE plany_zdielane (
          podpis TEXT NOT NULL, variant INTEGER NOT NULL, tyzden TEXT NOT NULL,
          json TEXT NOT NULL, PRIMARY KEY (podpis, variant)
        );
        CREATE TABLE dopyt_profilov (
          tyzden TEXT NOT NULL, obchody TEXT NOT NULL, osoby INTEGER NOT NULL,
          frekvencia INTEGER NOT NULL, variant INTEGER NOT NULL,
          pocet INTEGER NOT NULL DEFAULT 0, posledny TEXT,
          PRIMARY KEY (tyzden, obchody, osoby, frekvencia, variant)
        );
        INSERT INTO dopyt_profilov
          (tyzden, obchody, osoby, frekvencia, variant, pocet, posledny)
        VALUES ('2026-08-17', 'Lidl', 4, 3, 1, 7, '2026-08-20');
        """
    )

    predpocet.migrate_predpocet_schema(con)
    predpocet.migrate_predpocet_schema(con)

    columns = {row[1] for row in con.execute("PRAGMA table_info(dopyt_profilov)")}
    row = con.execute(
        "SELECT osoby, dospeli, deti, pocet, posledny FROM dopyt_profilov"
    ).fetchone()
    assert {"osoby", "dospeli", "deti"}.issubset(columns)
    assert tuple(row) == (4, 4, 0, 7, "2026-08-20")
    con.close()


def test_popular_profiles_keep_same_size_mixed_households_separate(monkeypatch, tmp_path):
    server, predpocet = priprav(monkeypatch, tmp_path)
    with closing(server.db()) as con:
        for _ in range(3):
            predpocet.zaznamenaj_dopyt(
                con, minuly_tyzden(), ["Lidl"], 2, 2, 3, 1
            )
        for _ in range(2):
            predpocet.zaznamenaj_dopyt(
                con, minuly_tyzden(), ["Lidl"], 4, 0, 3, 1
            )
        con.commit()
        profiles = predpocet.oblubene_profily(con, current_monday(), 2)

    assert [(p.dospeli, p.deti) for p in profiles] == [(2, 2), (4, 0)]


def test_precompute_passes_one_household_contract_to_signature_prompt_and_builder(
        monkeypatch, tmp_path):
    server, predpocet = priprav(monkeypatch, tmp_path)
    profile = predpocet.Profil(("Lidl",), 2, 2, 3, 1)
    calls = {}

    def signature(week, stores, household_size, frequency, rows, pantry,
                  *, adults, children):
        calls["signature"] = (household_size, adults, children)
        return "signature"

    def messages(rows, frequency, pantry, household_size, variant,
                 pantry_driven, *, prompt_rows=None, adults, children):
        calls["prompt"] = (household_size, adults, children, prompt_rows)
        return [{"type": "text", "text": "prompt"}]

    def builder(con, model_output, stores, frequency, household_size,
                *, adults, children):
        calls["builder"] = (household_size, adults, children)
        return {"jedla": []}

    class Messages:
        def create(self, **_kwargs):
            return types.SimpleNamespace(
                stop_reason="end_turn",
                content=[types.SimpleNamespace(type="text", text=json.dumps({"meals": []}))],
            )

    monkeypatch.setattr(server, "podpis_planu", signature)
    monkeypatch.setattr(predpocet, "personal_plan_messages", messages)
    monkeypatch.setattr(predpocet, "build_personal_plan", builder)
    monkeypatch.setitem(sys.modules, "anthropic", types.SimpleNamespace())
    monkeypatch.setattr(
        predpocet.naklady, "strazeny_klient",
        lambda *_args, **_kwargs: types.SimpleNamespace(messages=Messages()),
    )

    assert predpocet._podpis_pre_profil(server, "2026-08-24", profile, []) == "signature"
    with closing(server.db()) as con:
        predpocet._poskladaj(con, server, [], profile, klient=object())

    assert calls == {
        "signature": (None, 2, 2),
        "prompt": (None, 2, 2, []),
        "builder": (None, 2, 2),
    }


def test_predpocet_uses_the_same_low_effort_as_live_plans(monkeypatch, tmp_path):
    """Nočný predpočet nesmie zopakovať produkčné max_tokens zlyhania."""
    server, predpocet = priprav(monkeypatch, tmp_path)
    profile = predpocet.Profil(("Lidl",), 4, 0, 2, 0)
    calls = []

    class Messages:
        def create(self, **kwargs):
            calls.append(kwargs)
            return types.SimpleNamespace(
                stop_reason="end_turn",
                content=[types.SimpleNamespace(
                    type="text", text=json.dumps(model_plan())
                )],
            )

    monkeypatch.setitem(sys.modules, "anthropic", types.SimpleNamespace())
    monkeypatch.setattr(
        predpocet.naklady, "strazeny_klient",
        lambda *_args, **_kwargs: types.SimpleNamespace(messages=Messages()),
    )

    with closing(server.db()) as con:
        predpocet._poskladaj(
            con, server, server.akcie_pre(["Lidl"]), profile, klient=object()
        )

    assert calls[0]["output_config"] == {"effort": "low"}
    assert calls[0]["max_tokens"] >= 10_000


def test_precompute_shortlists_the_prompt_but_validates_an_offer_outside_it(monkeypatch, tmp_path):
    server, predpocet = priprav(monkeypatch, tmp_path, current_plan_rows(130))
    profile = predpocet.Profil(("Lidl",), 4, 0, 2, 0)
    calls = []
    outside_shortlist = plan_key(121)

    class Messages:
        def create(self, **kwargs):
            calls.append(kwargs)
            return types.SimpleNamespace(
                stop_reason="end_turn",
                content=[types.SimpleNamespace(
                    type="text", text=json.dumps(model_plan(first_offer_key=outside_shortlist))
                )],
            )

    client = types.SimpleNamespace(messages=Messages())
    monkeypatch.setitem(sys.modules, "anthropic", types.SimpleNamespace())
    monkeypatch.setattr(predpocet.naklady, "strazeny_klient", lambda *_args, **_kwargs: client)

    with closing(server.db()) as con:
        plan = predpocet._poskladaj(con, server, server.akcie_pre(["Lidl"]), profile, klient=client)

    prompt = "\n".join(block["text"] for block in calls[0]["messages"][0]["content"])
    shown = [plan_key(index) for index in range(1, 131) if plan_key(index) in prompt]
    assert len(shown) <= 120
    assert outside_shortlist not in shown
    assert plan["jedla"][0]["suroviny"][0]["offer_key"] == outside_shortlist


def test_predpocet_never_retries_a_typeerror_and_double_charges(monkeypatch, tmp_path):
    server, predpocet = priprav(monkeypatch, tmp_path)
    profile = predpocet.Profil(("Lidl",), 4, 0, 2, 0)
    calls = []

    class Messages:
        def create(self, **kwargs):
            calls.append(kwargs)
            raise TypeError("chyba po odoslaní požiadavky")

    monkeypatch.setitem(sys.modules, "anthropic", types.SimpleNamespace())
    monkeypatch.setattr(
        predpocet.naklady, "strazeny_klient",
        lambda *_args, **_kwargs: types.SimpleNamespace(messages=Messages()),
    )

    with closing(server.db()) as con, pytest.raises(TypeError, match="chyba po odoslaní"):
        predpocet._poskladaj(con, server, server.akcie_pre(["Lidl"]), profile,
                             klient=object())

    assert len(calls) == 1


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

    user_id = uzivatel(server)
    monkeypatch.setenv("UVARSI_ADMIN_EMAILS", "u1@uvar.si")
    telo = klient_pouzivatela(server, user_id).get("/api/naklady").json()

    assert "predpocet" in telo, "majiteľ musí vidieť, či predpočet vôbec beží"
    p = telo["predpocet"]
    assert p["zahriatych"] == 0
    assert p["eur"] == 0
    assert p["queued"] == 2
    assert p["cena_za_profil_eur"] > 0
    assert p["skutocna_cena_za_profil_eur"] is None
    assert p["odhad_plneho_behu_eur"] > 0
    assert p["usetrenych_generovani"] == 0
    assert p["dovod"] == "hotovo"


def test_health_ukazuje_predpocet_a_neprepadne_na_cerstvej_databaze(monkeypatch, tmp_path):
    server, _ = priprav(monkeypatch, tmp_path, rows=[])

    odpoved = TestClient(server.app).get("/api/health")

    assert odpoved.status_code == 200
    assert odpoved.json()["predpocet"]["zahriatych"] == 0


def test_prehlad_predpoctu_neprezradi_tajomstva(monkeypatch, tmp_path):
    server, _ = priprav(monkeypatch, tmp_path)
    user_id = uzivatel(server)
    monkeypatch.setenv("UVARSI_ADMIN_EMAILS", "u1@uvar.si")
    telo = klient_pouzivatela(server, user_id).get("/api/naklady").text
    for zakazane in ("API_KEY", "sk-ant", "re_", "@uvar.si"):
        assert zakazane not in telo


def test_cena_za_profil_je_zname_a_striezlive_cislo(monkeypatch, tmp_path):
    """Koľko stojí jeden zahriaty profil musí byť číslo, nie dojem."""
    server, predpocet = priprav(monkeypatch, tmp_path)
    naklady = importlib.import_module("naklady")

    assert predpocet.CENA_ZA_PROFIL_EUR == naklady.ODHAD_EUR["predpocet"]
    assert predpocet.CENA_ZA_PROFIL_EUR == pytest.approx(0.12)
    assert naklady.VYCHODZI_DENNY_STROP_EUR == pytest.approx(4.00)
    assert naklady.VYCHODZI_MESACNY_STROP_EUR == pytest.approx(25.00)
    assert predpocet.CENA_ZA_PROFIL_EUR + predpocet.VYCHODZIA_REZERVA_EUR <= (
        naklady.VYCHODZI_DENNY_STROP_EUR
    )
    # Plný predpočet ani po zvýšení odhadu nesmie sám osebe zjesť mesačný rozpočet.
    assert predpocet.MAX_POCET_PROFILOV * predpocet.CENA_ZA_PROFIL_EUR <= (
        naklady.VYCHODZI_MESACNY_STROP_EUR / 2
    )


# ------------------------------------------------------------------- napojenie
def test_dozorca_spusta_predpocet_len_nad_kompletnymi_ponukami():
    text = (ROOT / "hetzner" / "dozorca.sh").read_text(encoding="utf-8")
    assert "predpocet.py" in text, (
        "predpočet musí byť napojený na dozorcu — inak ho nikto nikdy nespustí"
    )
    zbierac = text.index('"$PY" -u zbierac_akcii.py')
    podmienka = text.index('if [ "${POCET:-0}" -ge 30 ] && [ "${CHYBA_ZBER:-3}" -eq 0 ]')
    volanie = text.index("zahrej_plany", podmienka)
    assert zbierac < podmienka < volanie, (
        "predpočet sa smie opakovať nezávisle od zberu, ale až po novom overení, "
        "že máme dosť ponúk a nechýba Kaufland, Tesco ani Lidl"
    )


def test_dozorca_obnovi_neaktualny_blocek_pred_dlhotrvajucim_predpoctom():
    text = (ROOT / "hetzner" / "dozorca.sh").read_text(encoding="utf-8")
    refresh = text.index('VYSTUP=$(cd "$DIR" && "$PY" -u refresh_blocek.py')
    uspesny_refresh = text.index('if [ "$RC" -eq 0 ] && landing_data_is_current; then')
    predpocet_po_refreshe = text.find("zahrej_plany", uspesny_refresh)
    assert refresh < uspesny_refresh < predpocet_po_refreshe, (
        "bloček je verejný a časovo kritický; predpočet môže trvať minúty, "
        "preto sa pri neaktuálnom landingu smie spustiť až po jeho úspešnej obnove"
    )


def test_dozorca_ma_procesovy_zamok_a_nasadenie_overi_flock():
    text = (ROOT / "hetzner" / "dozorca.sh").read_text(encoding="utf-8")
    assert 'exec 9>"$DIR/.dozorca.lock"' in text
    assert "flock -n 9" in text, (
        "dva prekryté hodinové behy nesmú zaplatiť rovnaký predpočet dvakrát"
    )
    nasad = (ROOT / "nasad.ps1").read_text(encoding="utf-8")
    assert "command -v flock" in nasad, (
        "release musí overiť, že produkčný server vie procesový zámok použiť"
    )


def test_predpocet_cli_vrati_chybu_ked_model_nezahrial_plan(monkeypatch, capsys):
    predpocet = importlib.import_module("predpocet")
    monkeypatch.setattr(predpocet, "zahrej", lambda **_kw: {
        "tyzden": "2026-08-24", "queued": 0, "skipped": 0, "blocked": 1,
        "zahriatych": 0, "preskocenych": 0,
        "zlyhanych": 1, "eur": 0.0, "dovod": predpocet.DOVOD_CHYBY,
    })

    assert predpocet.cli(["--zahrej"]) == 1
    assert "blokovaných 1" in capsys.readouterr().out


def test_zlyhanie_predpoctu_nesmie_zmenit_navratovy_kod_dozorcu():
    text = (ROOT / "hetzner" / "dozorca.sh").read_text(encoding="utf-8")
    riadok = [line for line in text.splitlines() if "predpocet.py" in line]
    assert riadok, "očakávam volanie predpočtu v dozorcovi"
    okolie = text[text.index("predpocet.py"):text.index("predpocet.py") + 400]
    assert "||" in okolie, (
        "predpočet je zrýchlenie, nie povinnosť — jeho pád nesmie zhodiť dozorcu"
    )


def test_nastavenie_predpoctu_ma_na_serveri_trvale_miesto():
    """Bez toho by sa `UVARSI_PREDPOCET_PROFILOV` stratilo pri každom nasadení."""
    text = (ROOT / "hetzner" / "dozorca.sh").read_text(encoding="utf-8")
    assert "predpocet.env" in text
    nasad = (ROOT / "nasad.ps1").read_text(encoding="utf-8")
    samopull = (ROOT / "hetzner" / "samopull.sh").read_text(encoding="utf-8")
    assert "predpocet.env" not in nasad and "predpocet.env" not in samopull, (
        "nastavenie patrí serveru — nasadenie ho nesmie prenášať ani prepisovať"
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
