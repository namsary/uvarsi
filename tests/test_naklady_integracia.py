"""Strop na míňanie musí platiť na KAŽDOM mieste, kde sa platí modelu.

Modul app/naklady.py sám o sebe nič nechráni — chráni až vtedy, keď ním
prechádza platené čítanie letákov. Recepty a osobné plány sú lokálne a
deterministické. Testy strážia túto hranicu aj prehľad na /api/health.
"""
import importlib
import json
import sqlite3
import sys
import types
from datetime import date
from pathlib import Path

import pytest

from app import naklady
from app.receipt_data import StructuralFailure
from tests.test_deterministic_plan_api import _realistic_offer_rows

from tests.test_server import (
    current_plan_rows,
    grant_premium,
    insert_hashed_session,
    load_server,
)


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def ciste_stropy(monkeypatch):
    for nazov in naklady.PREMENNE_PROSTREDIA:
        monkeypatch.delenv(nazov, raising=False)


def vycerpaj_denny_strop(cesta_db, ucel="plan"):
    """Naplň evidenciu tak, aby bol dnešný rozpočet preukázateľne minutý."""
    con = naklady.pripoj(cesta_db)
    naklady.zapis(con, ucel, "claude-opus-5", types.SimpleNamespace(
        input_tokens=10_000_000, output_tokens=0,
        cache_creation_input_tokens=0, cache_read_input_tokens=0,
    ), notifikuj=lambda sprava: None)
    con.close()


# ------------------------------------------------------------------ zbierač
@pytest.fixture
def collector(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "app"))
    sys.modules.pop("zbierac_akcii", None)
    return importlib.import_module("zbierac_akcii")


def priprav_zbierac(monkeypatch, tmp_path, collector):
    database = tmp_path / "uvarsi.db"
    monkeypatch.setattr(collector, "DB", str(database))
    monkeypatch.setattr(collector, "monday", lambda: "2026-08-17")
    monkeypatch.setattr(collector, "STORES", ["lidl"])
    monkeypatch.setattr(collector, "load_key", lambda: "unused-test-value")
    monkeypatch.setattr(collector, "business_day", lambda: date(2026, 8, 19))

    def prepared(store):
        display = store.capitalize()
        kind = collector.OFFICIAL_COLLECTOR_BY_STORE[display]
        return collector.PreparedCollection(
            pages=[],
            manifest={
                "source_url": f"https://www.{store}.sk/test-letak",
                "collector_kind": kind,
                "valid_from": "2026-08-17",
                "valid_to": "2026-08-23",
                "pages": [],
            },
            page_manifest={},
            provenance=collector.CollectionProvenance(
                kind, "a" * 64, "2026-08-17", "2026-08-23"
            ),
        )

    monkeypatch.setattr(collector, "prepare_store_collection", prepared)
    monkeypatch.setitem(
        sys.modules, "anthropic",
        types.SimpleNamespace(Anthropic=lambda **kwargs: object()),
    )
    return database


def test_zbierac_ma_tyzdenny_strop_poctu_behov(monkeypatch, tmp_path, collector):
    """Presne tá poistka, ktorá chýbala: vision beh sa nedá spustiť donekonečna."""
    database = priprav_zbierac(monkeypatch, tmp_path, collector)
    behy = []

    def zbieraj(client, store, prepared=None):
        behy.append(store)
        raise ValueError("lidl: bezpečný test opakovaného neúspešného zberu")

    monkeypatch.setattr(collector, "zbieraj", zbieraj)

    odmietnutia = 0
    for _ in range(12):                      # rozbehnutá slučka dozorcu
        try:
            collector.main()
        except SystemExit as koniec:
            if "rozpočet" in str(koniec).lower() or "strop" in str(koniec).lower():
                odmietnutia += 1

    assert len(behy) == 1, (
        "nezmenená štrukturálna chyba sa má potlačiť ešte skôr než narazí na "
        "týždenný strop"
    )
    with sqlite3.connect(database) as con:
        pocet = con.execute(
            "SELECT pocet FROM naklady_behy WHERE ucel='zber_letakov'"
        ).fetchone()[0]
    assert pocet == 1 <= naklady.limit_behov("zber_letakov")
    assert odmietnutia == 0


def test_zbierac_zauctuje_kazde_volanie_modelu(monkeypatch, tmp_path, collector):
    """Klient odovzdaný do zbieraj() musí byť strážený, nie holý."""
    database = priprav_zbierac(monkeypatch, tmp_path, collector)
    zachyteny = {}

    def zbieraj(client, store, prepared=None):
        zachyteny["client"] = client
        raise ValueError("lidl: leták sa nepodarilo prečítať")

    monkeypatch.setattr(collector, "zbieraj", zbieraj)
    with pytest.raises(SystemExit):
        collector.main()

    # Zbierač importuje `naklady` ako top-level modul, test cez `app.naklady` —
    # sú to dva objekty tej istej triedy, tak porovnávame meno a účel.
    klient = zachyteny["client"]
    assert type(klient).__name__ == "_LeaseRenewingClient"
    strazene_spravy = klient.messages._messages
    assert type(strazene_spravy).__name__ == "_StrazeneSpravy"
    assert strazene_spravy._s.ucel == "zber_letakov"


def test_zbierac_stale_zapise_skutocnu_spotrebu_modelu(monkeypatch, tmp_path, collector):
    """Deterministické recepty nesmú vypnúť účtovanie AI čítania letákov."""
    database = priprav_zbierac(monkeypatch, tmp_path, collector)
    usage = types.SimpleNamespace(
        input_tokens=8_000,
        output_tokens=900,
        cache_creation_input_tokens=0,
        cache_read_input_tokens=2_000,
    )

    class Messages:
        def create(self, **_kwargs):
            return types.SimpleNamespace(usage=usage)

    monkeypatch.setitem(
        sys.modules,
        "anthropic",
        types.SimpleNamespace(
            Anthropic=lambda **_kwargs: types.SimpleNamespace(messages=Messages())
        ),
    )

    def zbieraj(client, store, prepared=None):
        client.messages.create(model=collector.MODEL_READ, messages=[])
        return [{
            "obchod": store.capitalize(),
            "nazov": f"Potravina {index}",
            "kategoria": "trvanlive",
            "cena": 1.0,
            "povodna": 2.0,
            "zlava": "-50 %",
            "jednotka": "1 kg",
            "source_url": "https://www.lidl.sk/l/test-letak",
            "source_page": index,
            "valid_from": "2026-08-17",
            "valid_to": "2026-08-23",
        } for index in range(1, 21)]

    monkeypatch.setattr(collector, "zbieraj", zbieraj)
    collector.main()

    with sqlite3.connect(database) as con:
        row = con.execute(
            """SELECT ucel,model,vstup,vystup,cache_read,eur,odhad
               FROM naklady ORDER BY id DESC LIMIT 1"""
        ).fetchone()

    assert row[:5] == ("zber_letakov", "claude-sonnet-5", 8_000, 900, 2_000)
    assert row[5] > 0
    assert row[6] == 0


# ------------------------------------------------------------------ bloček
@pytest.fixture
def refresh():
    from hetzner import refresh_blocek

    return refresh_blocek


def test_blocek_uz_nema_platenu_modelovu_cestu(refresh):
    source = Path(refresh.__file__).read_text(encoding="utf-8")

    assert "anthropic" not in source
    assert "ANTHROPIC_API_KEY" not in source
    assert "naklady" not in source


# ------------------------------------------------------------------ recepty
def test_recepty_uz_nemaju_platenu_modelovu_cestu():
    """Platené API číta letáky; recepty sa skladajú z lokálneho katalógu."""
    from hetzner import recepty

    assert not hasattr(recepty, "gen_recipes")
    assert "ANTHROPIC_API_KEY" not in Path(recepty.__file__).read_text(encoding="utf-8")


# ------------------------------------------------------------------ osobný plán
def test_lokalny_plan_funguje_aj_ked_je_ai_rozpocet_vycerpany(monkeypatch, tmp_path):
    server = load_server(monkeypatch, tmp_path, _realistic_offer_rows())
    vycerpaj_denny_strop(tmp_path / "uvarsi.db", ucel="plan")
    with server.db() as con:
        con.execute(
            """INSERT INTO pouzivatelia
               (id,email,osoby,dospeli,deti,frekvencia,obchody)
               VALUES (1,'a@b.sk',4,2,2,2,'Lidl,Kaufland,Tesco')"""
        )
        insert_hashed_session(server, con, "session-token", 1)
        con.commit()

    grant_premium(server, 1)

    konstruktory = []

    class ZakazanyAnthropic:
        def __init__(self, *a, **kw):
            konstruktory.append(kw)

    monkeypatch.setitem(sys.modules, "anthropic",
                        types.SimpleNamespace(Anthropic=ZakazanyAnthropic))
    from fastapi.testclient import TestClient

    client = TestClient(server.app, raise_server_exceptions=False)
    client.cookies.set(server.COOKIE, "session-token")
    odpoved = client.post("/api/plan/generuj?force=1")

    assert odpoved.status_code == 200, odpoved.text
    assert odpoved.json()["meta"]["engine"] == "deterministic"
    assert konstruktory == [], "lokálny plán nesmie ani vytvoriť modelového klienta"


def test_odmietnuty_plan_nezobere_pouzivatelovi_denny_prepocet(monkeypatch, tmp_path):
    """Za odmietnutie sa neplatí — ani peniazmi, ani miestom v dennom strope."""
    server = load_server(monkeypatch, tmp_path, current_plan_rows())
    vycerpaj_denny_strop(tmp_path / "uvarsi.db", ucel="plan")
    with server.db() as con:
        con.execute("INSERT INTO pouzivatelia (id,email) VALUES (1,'a@b.sk')")
        insert_hashed_session(server, con, "session-token", 1)
        con.commit()

    monkeypatch.setitem(sys.modules, "anthropic",
                        types.SimpleNamespace(Anthropic=lambda **kw: object()))
    from fastapi.testclient import TestClient

    client = TestClient(server.app, raise_server_exceptions=False)
    client.cookies.set(server.COOKIE, "session-token")
    client.post("/api/plan/generuj?force=1")

    with server.db() as con:
        pouzite = server.pouzite_prepocty(con, 1, server.dnesok())
    assert pouzite == 0


def test_uspesny_lokalny_plan_nezaeviduje_ai_spotrebu(monkeypatch, tmp_path):
    server = load_server(monkeypatch, tmp_path, _realistic_offer_rows())
    with server.db() as con:
        con.execute(
            """INSERT INTO pouzivatelia
               (id,email,osoby,dospeli,deti,frekvencia,obchody)
               VALUES (1,'a@b.sk',4,2,2,2,'Lidl,Kaufland,Tesco')"""
        )
        insert_hashed_session(server, con, "session-token", 1)
        con.commit()
    grant_premium(server, 1)

    class ZakazanyAnthropic:
        def __init__(self, *args, **kwargs):
            raise AssertionError("lokálny jedálniček nesmie volať AI")

    monkeypatch.setitem(sys.modules, "anthropic",
                        types.SimpleNamespace(Anthropic=ZakazanyAnthropic))
    from fastapi.testclient import TestClient

    client = TestClient(server.app)
    client.cookies.set(server.COOKIE, "session-token")
    odpoved = client.post("/api/plan/generuj?force=1")
    assert odpoved.status_code == 200
    assert odpoved.json()["jedla"]

    with server.db() as con:
        assert con.execute("SELECT COUNT(*) FROM plan_jobs").fetchone()[0] == 0
        assert con.execute("SELECT COUNT(*) FROM naklady").fetchone()[0] == 0
        assert server.pouzite_prepocty(con, 1, server.dnesok()) == 1


# ------------------------------------------------------------------ viditeľnosť
def test_health_ukazuje_dnesnu_a_mesacnu_utratu(monkeypatch, tmp_path):
    server = load_server(monkeypatch, tmp_path, [])
    vycerpaj_denny_strop(tmp_path / "uvarsi.db", ucel="zber_letakov")
    from fastapi.testclient import TestClient

    telo = TestClient(server.app).get("/api/health").json()

    assert "naklady" in telo, "majiteľ musí vidieť útratu bez SSH"
    n = telo["naklady"]
    assert n["dnes_eur"] > 0
    assert n["mesiac_eur"] >= n["dnes_eur"]
    assert n["denny_strop_eur"] > 0 and n["mesacny_strop_eur"] > 0
    assert "zostatok_dnes_eur" in n and "zostatok_mesiac_eur" in n
    assert n["posledne"][0]["ucel"] == "zber_letakov"


def test_health_s_nakladmi_stale_neprezradi_tajomstva(monkeypatch, tmp_path):
    server = load_server(monkeypatch, tmp_path, [])
    vycerpaj_denny_strop(tmp_path / "uvarsi.db")
    from fastapi.testclient import TestClient

    telo = TestClient(server.app).get("/api/health").text
    for zakazane in ("API_KEY", "sk-ant", "re_"):
        assert zakazane not in telo


def test_health_neprepadne_na_cerstvej_databaze(monkeypatch, tmp_path):
    server = load_server(monkeypatch, tmp_path, [])
    from fastapi.testclient import TestClient

    odpoved = TestClient(server.app).get("/api/health")
    assert odpoved.status_code == 200
    assert odpoved.json()["naklady"]["dnes_eur"] == 0


def test_sesterske_api_naklady_da_podrobny_prehlad(monkeypatch, tmp_path):
    monkeypatch.setenv("UVARSI_ADMIN_EMAILS", "owner@example.test")
    server = load_server(monkeypatch, tmp_path, [])
    vycerpaj_denny_strop(tmp_path / "uvarsi.db", ucel="zber_letakov")
    from fastapi.testclient import TestClient

    with server.db() as con:
        con.execute("INSERT INTO pouzivatelia (id, email) VALUES (1, 'owner@example.test')")
        insert_hashed_session(server, con, "owner-session", 1)
        con.commit()
    client = TestClient(server.app)
    client.cookies.set(server.COOKIE, "owner-session")
    odpoved = client.get("/api/naklady")

    assert odpoved.status_code == 200
    telo = odpoved.json()
    assert telo["behy"]["zber_letakov"]["limit"] == naklady.limit_behov("zber_letakov")
    assert telo["posledne"]


# ------------------------------------------------------------------ incident
def test_cely_incident_sa_zastavi_na_strope(monkeypatch, tmp_path, collector):
    """Rekonštrukcia incidentu 21. 8. 2026 na skutočnom kóde zbierača.

    Dozorca spúšťal obnovu 6× denne dva dni po sebe a každý pokus zaplatil plný
    vision beh Opusom 5. Tu robíme to isté — a čakáme, že sa míňanie zastaví
    hlboko pod 4,60 €, ktoré vtedy zmizli.
    """
    monkeypatch.setenv("UVARSI_MESACNY_STROP_EUR", "5.00")
    database = priprav_zbierac(monkeypatch, tmp_path, collector)
    vision_usage = types.SimpleNamespace(
        input_tokens=80_000, output_tokens=2_000,
        cache_creation_input_tokens=0, cache_read_input_tokens=0,
    )
    volania = []

    def zbieraj(client, store, prepared=None):
        """Vision beh, ktorý sa podarí zaplatiť a potom deterministicky padne."""
        for _ in range(9):                     # ~36 strán po 4 v dávke
            try:
                client.messages.create(model="claude-opus-5", max_tokens=16000, messages=[])
            except naklady.RozpocetVycerpany:
                break
        raise ValueError("lidl: bloček sa nedá zostaviť")

    def create(**kw):
        volania.append(kw)
        return types.SimpleNamespace(usage=vision_usage)

    monkeypatch.setattr(collector, "zbieraj", zbieraj)
    monkeypatch.setitem(sys.modules, "anthropic", types.SimpleNamespace(
        Anthropic=lambda **kw: types.SimpleNamespace(
            messages=types.SimpleNamespace(create=create))))

    for _ in range(12):
        try:
            collector.main()
        except SystemExit:
            pass

    con = sqlite3.connect(database)
    minute = con.execute("SELECT COALESCE(SUM(eur), 0) FROM naklady").fetchone()[0]
    con.close()

    assert minute < 4.60, f"incident stál 4,60 €; so stropom {minute:.2f} €"
    assert minute <= 5.00
    assert volania, "test musí naozaj prejsť cez platenú cestu"
