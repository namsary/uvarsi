"""Špajza už neriadi skladanie plánu — a nesmie pretiecť medzi ľuďmi.

Doteraz bola špajza v podpise zdieľaného plánu. Malo to dva dôsledky:

  * každý platiaci účet mal vlastný podpis, takže sa NIKDY netrafil do
    zdieľanej cache a vždy čakal 60–120 sekúnd (platiaci čakali najdlhšie),
  * jedno pridané vajíčko preskladalo celý týždeň bez vyzvania.

Po zmene sa plán skladá bez špajze a špajza sa dopočíta až nad nákupným
zoznamom. Tým ale zmizla ochrana, ktorú podpis mimochodom poskytoval: špajza
sa nemohla dostať k inému používateľovi, lebo s inou špajzou bol iný podpis.
Túto ochranu tu preberá test — zdieľaný riadok nesmie obsahovať nič zo špajze
a každý čitateľ musí vidieť výhradne svoju vlastnú.
"""
import json
import sys
import types

import pytest
from fastapi.testclient import TestClient

from app.plan_data import personal_plan_prompt, plan_signature
from app import plan_data

from tests.test_server import (
    current_plan_rows,
    fake_anthropic,
    grant_premium,
    insert_hashed_session,
    load_server,
    model_plan,
    plan_client,
    prompt_text,
    shared_plan_server,
    SHARED_VARIANT_USERS,
)


ZAKLAD = dict(
    week="2026-08-17", stores=["Lidl", "Tesco"], household_size=4,
    frequency=2, offer_keys=("offer_a", "offer_b", "offer_c"),
)


# ------------------------------------------------------------------- podpis
def test_the_pantry_no_longer_changes_the_shared_signature():
    """Bez tohto platiaci účet nikdy netrafí zdieľanú cache a vždy čaká."""
    bez = plan_signature(**ZAKLAD)

    assert plan_signature(**ZAKLAD, pantry=["vajcia"]) == bez
    assert plan_signature(**ZAKLAD, pantry=["vajcia", "ryža", "mlieko"]) == bez
    assert plan_signature(**ZAKLAD, pantry=[]) == bez


def test_only_the_explicit_cook_from_my_pantry_action_puts_the_pantry_in_the_key():
    zdielany = plan_signature(**ZAKLAD, pantry=["vajcia"])
    zo_spajze = plan_signature(**ZAKLAD, pantry=["vajcia"], pantry_driven=True)

    assert zo_spajze != zdielany, "vyžiadaný plán zo špajze sa nesmie zliať so zdieľaným"
    assert plan_signature(**ZAKLAD, pantry=["ryža"], pantry_driven=True) != zo_spajze
    assert plan_signature(**ZAKLAD, pantry=["VAJCIA"], pantry_driven=True) == zo_spajze


def test_the_profile_and_the_offers_still_drive_the_signature():
    zaklad = plan_signature(**ZAKLAD)

    assert plan_signature(**dict(ZAKLAD, household_size=2)) != zaklad
    assert plan_signature(**dict(ZAKLAD, frequency=3)) != zaklad
    assert plan_signature(**dict(ZAKLAD, week="2026-08-24")) != zaklad
    assert plan_signature(**dict(ZAKLAD, offer_keys=("offer_a",))) != zaklad


# ------------------------------------------------------------------- prompt
def offer_rows():
    return [{"offer_key": "offer_a", "nazov": "Ryža", "kategoria": "trvanlive"}]


def test_the_shared_prompt_never_mentions_a_pantry():
    """Do zdieľaného promptu nesmie prísť nič osobné — inak sa nedá zdieľať."""
    task = personal_plan_prompt(offer_rows(), 2, [], household_size=4)

    assert "špajz" not in task.casefold()
    assert "pantry" not in task.casefold()


def test_a_pantry_handed_to_the_shared_prompt_is_ignored_not_leaked():
    """Poistka: aj keby volajúci špajzu omylom podal, do promptu sa nedostane."""
    task = personal_plan_prompt(offer_rows(), 2, ["vajcia", "ryža"], household_size=4)

    assert "vajcia" not in task and "ryža" not in task


def test_the_explicit_pantry_prompt_does_ask_the_model_to_cook_from_it():
    task = personal_plan_prompt(
        offer_rows(), 2, ["vajcia", "ryža"], household_size=4, pantry_driven=True
    )

    assert "vajcia" in task and "ryža" in task
    assert "špajz" in task.casefold()


# --------------------------------------------------- neúnik medzi používateľmi
def test_a_shared_plan_stored_for_one_user_shows_the_reader_only_his_own_pantry(
        monkeypatch, tmp_path):
    """Toto je ochrana, ktorú predtým zabezpečoval podpis. Teraz ju drží kód."""
    prvy, druhy = SHARED_VARIANT_USERS
    server = shared_plan_server(
        monkeypatch, tmp_path,
        pantry={prvy: ["Ponuka 1", "tajná surovina Adama"], druhy: ["Ponuka 2"]},
    )
    calls = []
    monkeypatch.setitem(sys.modules, "anthropic", fake_anthropic(model_plan(), [], calls))

    vygenerovany = plan_client(server, prvy).post("/api/plan/generuj")
    assert vygenerovany.status_code == 200

    citany = plan_client(server, druhy).post("/api/plan/generuj")

    assert citany.status_code == 200
    assert len(calls) == 1, "rovnaký profil s inou špajzou musí zdieľať plán"
    telo = json.dumps(citany.json(), ensure_ascii=False)
    assert "tajná surovina Adama" not in telo, "špajza jedného človeka sa nesmie ukázať druhému"
    assert citany.json()["spajza"] == ["Ponuka 2"]
    assert [item["spajza"] for item in citany.json()["spajza_pokryte"]] == ["Ponuka 2"]


def test_the_shared_row_itself_carries_no_pantry_at_all(monkeypatch, tmp_path):
    prvy, druhy = SHARED_VARIANT_USERS
    server = shared_plan_server(
        monkeypatch, tmp_path, pantry={prvy: ["Ponuka 1", "tajná surovina Adama"], druhy: []})
    monkeypatch.setitem(sys.modules, "anthropic", fake_anthropic(model_plan(), []))

    plan_client(server, prvy).post("/api/plan/generuj")

    with server.db() as con:
        ulozene = [row[0] for row in con.execute("SELECT json FROM plany_zdielane")]
    assert ulozene
    for zdielany in ulozene:
        assert "tajná surovina Adama" not in zdielany
        plan = json.loads(zdielany)
        assert "spajza" not in plan
        assert "spajza_pokryte" not in plan and "spajza_usetri" not in plan
        for meal in plan["jedla"]:
            assert all("offer_key" in item for item in meal["suroviny"])


def test_uloz_zdielany_plan_strips_the_pantry_before_it_touches_the_database(
        monkeypatch, tmp_path):
    """Priama kontrola tej jedinej funkcie, ktorá do zdieľanej tabuľky zapisuje."""
    server = load_server(monkeypatch, tmp_path, current_plan_rows())
    plan = {
        "tyzden": "2026-08-17", "nakup_spolu": "6,49", "spajza": ["tajná surovina"],
        "spajza_pokryte": [{"nazov": "Ryža", "spajza": "tajná surovina"}],
        "spajza_usetri": "1,49", "nakup_bez_spajze": "5,00",
        "jedla": [{
            "den": "PO", "nazov": "Rizoto",
            "recept": {"davky": ["Ryža – 600 g", "tajná surovina zo špajze"], "kroky": ["krok"]},
            "suroviny": [{"offer_key": "offer_aaa", "nazov": "Ryža"}, {"spajza": "tajná surovina"}],
        }],
        "nakupny_zoznam": [{"obchod": "Lidl", "polozky": [
            {"offer_key": "offer_aaa", "nazov": "Ryža", "cena": "1,49",
             "mas_doma": True, "spajza": "tajná surovina"},
        ]}],
    }

    with server.db() as con:
        server.uloz_zdielany_plan(con, "podpis", 0, "2026-08-17", plan)
        con.commit()
        ulozene = con.execute("SELECT json FROM plany_zdielane").fetchone()[0]

    assert "tajná surovina" not in ulozene
    zdielany = json.loads(ulozene)
    assert "spajza" not in zdielany and "spajza_pokryte" not in zdielany
    assert zdielany["jedla"][0]["suroviny"] == [{"offer_key": "offer_aaa", "nazov": "Ryža"}]
    assert zdielany["jedla"][0]["recept"]["davky"] == ["Ryža – 600 g"]
    assert zdielany["nakupny_zoznam"][0]["polozky"][0] == {
        "offer_key": "offer_aaa", "nazov": "Ryža", "cena": "1,49"}
    # A pôvodný plán ostal nedotknutý — volajúci ho ešte podáva používateľovi.
    assert plan["spajza"] == ["tajná surovina"]


def test_the_pantry_view_is_recomputed_per_request_not_stored(monkeypatch, tmp_path):
    """Zmena špajze musí byť vidieť okamžite — bez prepočtu a bez volania modelu."""
    server = shared_plan_server(monkeypatch, tmp_path, users=(1,), pantry={1: []})
    constructors = []
    monkeypatch.setitem(sys.modules, "anthropic", fake_anthropic(model_plan(), constructors))
    client = plan_client(server, 1)
    assert client.post("/api/plan/generuj").status_code == 200
    assert len(constructors) == 1

    client.post("/api/spajza", json={"polozky": ["Ponuka 1"]})
    po_zmene = client.get("/api/plan")

    assert po_zmene.status_code == 200
    assert len(constructors) == 1, "špajza sa nesmie dotknúť plateného volania"
    assert [item["nazov"] for item in po_zmene.json()["spajza_pokryte"]] == ["Ponuka 1"]
    assert po_zmene.json()["jedla"] == client.get("/api/plan").json()["jedla"]


def test_adding_to_the_pantry_never_reshapes_the_menu(monkeypatch, tmp_path):
    server = shared_plan_server(monkeypatch, tmp_path, users=(1,), pantry={1: []})
    monkeypatch.setitem(sys.modules, "anthropic", fake_anthropic(model_plan(), []))
    client = plan_client(server, 1)
    pred = client.post("/api/plan/generuj").json()

    client.post("/api/spajza", json={"polozky": ["Ponuka 1", "vajcia"]})
    po = client.get("/api/plan").json()

    assert po["jedla"] == pred["jedla"], "pridané vajíčko nesmie preskladať týždeň"
    assert po["nakup_spolu"] == pred["nakup_spolu"]


# -------------------------------- osobná cache: verzia algoritmu a pôvod plánu
def test_get_invalidates_a_legacy_personal_plan_without_calling_the_model(
        monkeypatch, tmp_path):
    """Plán uložený pred v4 sa nesmie ďalej podávať ani potichu preskladať."""
    server = shared_plan_server(monkeypatch, tmp_path, users=(1,), pantry={1: []})
    with server.db() as con:
        legacy = server.build_personal_plan(
            con, model_plan(), ["Lidl"], 2, 4, pantry=[])
        con.execute(
            "INSERT OR REPLACE INTO plany (user_id,tyzden,json) VALUES (?,?,?)",
            (1, server.monday(), json.dumps(legacy, ensure_ascii=False)),
        )
        con.commit()
    constructors = []
    monkeypatch.setitem(sys.modules, "anthropic", fake_anthropic(model_plan(), constructors))

    response = plan_client(server, 1).get("/api/plan")

    assert response.status_code == 200
    assert response.json()["prazdny"] is True
    assert response.json()["vyzaduje_akciu"] is True
    assert constructors == [], "invalidácia cache nesmie sama zavolať platený model"
    with server.db() as con:
        assert con.execute("SELECT COUNT(*) FROM plany WHERE user_id=1").fetchone()[0] == 0


def test_explicit_generate_replaces_a_legacy_personal_plan_with_a_current_one(
        monkeypatch, tmp_path):
    server = shared_plan_server(monkeypatch, tmp_path, users=(1,), pantry={1: []})
    with server.db() as con:
        legacy = server.build_personal_plan(con, model_plan(), ["Lidl"], 2, 4, pantry=[])
        con.execute(
            "INSERT OR REPLACE INTO plany (user_id,tyzden,json) VALUES (?,?,?)",
            (1, server.monday(), json.dumps(legacy, ensure_ascii=False)),
        )
        con.commit()
    constructors = []
    monkeypatch.setitem(sys.modules, "anthropic", fake_anthropic(model_plan(), constructors))

    response = plan_client(server, 1).post("/api/plan/generuj")

    assert response.status_code == 200
    assert response.json().get("prazdny") is not True
    assert len(constructors) == 1, "explicitný POST smie vytvoriť čerstvý plán"
    with server.db() as con:
        stored = json.loads(con.execute(
            "SELECT json FROM plany WHERE user_id=1").fetchone()[0])
    assert stored["_uvarsi_meta"]["algo_version"] == plan_data.PLAN_ALGO_VERSION


def test_a_personal_plan_with_a_different_algorithm_version_is_invalidated(monkeypatch, tmp_path):
    server = shared_plan_server(monkeypatch, tmp_path, users=(1,), pantry={1: []})
    with server.db() as con:
        stale = server.build_personal_plan(con, model_plan(), ["Lidl"], 2, 4, pantry=[])
        stale["_uvarsi_meta"] = {
            "algo_version": plan_data.PLAN_ALGO_VERSION - 1,
            "pantry_driven": False,
        }
        con.execute(
            "INSERT OR REPLACE INTO plany (user_id,tyzden,json) VALUES (?,?,?)",
            (1, server.monday(), json.dumps(stale, ensure_ascii=False)),
        )
        con.commit()

    response = plan_client(server, 1).get("/api/plan")

    assert response.status_code == 200
    assert response.json()["prazdny"] is True
    assert response.json()["vyzaduje_akciu"] is True


def test_generated_personal_plan_stores_current_metadata_but_never_exposes_it(
        monkeypatch, tmp_path):
    server = shared_plan_server(monkeypatch, tmp_path, users=(1,), pantry={1: []})
    monkeypatch.setitem(sys.modules, "anthropic", fake_anthropic(model_plan(), []))

    response = plan_client(server, 1).post("/api/plan/generuj")

    assert response.status_code == 200
    assert "_uvarsi_meta" not in response.json()
    with server.db() as con:
        stored = json.loads(con.execute(
            "SELECT json FROM plany WHERE user_id=1").fetchone()[0])
        shared = [json.loads(row[0]) for row in con.execute("SELECT json FROM plany_zdielane")]
    assert stored["_uvarsi_meta"] == {
        "algo_version": plan_data.PLAN_ALGO_VERSION,
        "pantry_driven": False,
    }
    assert all("_uvarsi_meta" not in plan for plan in shared)


def test_a_pantry_driven_plan_is_invalidated_when_the_normalized_pantry_changes(
        monkeypatch, tmp_path):
    server = shared_plan_server(monkeypatch, tmp_path, users=(1,), pantry={1: ["soľ"]})
    constructors = []
    monkeypatch.setitem(
        sys.modules, "anthropic",
        fake_anthropic(model_plan(pantry=["soľ"]), constructors),
    )
    client = plan_client(server, 1)
    assert client.post("/api/plan/zo-spajze").status_code == 200
    assert len(constructors) == 1

    assert client.post("/api/spajza", json={"polozky": ["soľ", "vajcia"]}).status_code == 200
    response = client.get("/api/plan")

    assert response.status_code == 200
    assert response.json()["prazdny"] is True
    assert response.json()["vyzaduje_akciu"] is True
    assert len(constructors) == 1, "zmena špajze nesmie automaticky minúť ďalší prepočet"
    with server.db() as con:
        assert con.execute("SELECT COUNT(*) FROM plany WHERE user_id=1").fetchone()[0] == 0


def test_pantry_signature_is_order_case_and_whitespace_insensitive(monkeypatch, tmp_path):
    server = shared_plan_server(
        monkeypatch, tmp_path, users=(1,), pantry={1: ["soľ"]})
    constructors = []
    monkeypatch.setitem(
        sys.modules, "anthropic",
        fake_anthropic(model_plan(pantry=["soľ"]), constructors),
    )
    client = plan_client(server, 1)
    assert client.post("/api/plan/zo-spajze").status_code == 200

    assert client.post("/api/spajza", json={"polozky": [" SOĽ "]}).status_code == 200
    response = client.get("/api/plan")

    assert response.status_code == 200
    assert response.json().get("prazdny") is not True
    assert len(constructors) == 1


# ------------------------------------------- výslovné „uvar z toho, čo mám doma"
def test_cooking_from_the_pantry_is_an_explicit_premium_only_action(monkeypatch, tmp_path):
    server = shared_plan_server(
        monkeypatch, tmp_path, users=(1,), pantry={1: ["soľ"]}, premium=False)
    constructors = []
    monkeypatch.setitem(sys.modules, "anthropic", fake_anthropic(model_plan(), constructors))

    odmietnutie = plan_client(server, 1).post("/api/plan/zo-spajze")

    assert odmietnutie.status_code == 403
    assert odmietnutie.json()["kod"] == server.KOD_SPAJZA_PREMIUM
    assert constructors == [], "bezplatný účet nesmie spustiť platené volanie"


def test_cooking_from_the_pantry_puts_the_pantry_into_the_prompt(monkeypatch, tmp_path):
    server = shared_plan_server(monkeypatch, tmp_path, users=(1,), pantry={1: ["soľ"]})
    calls = []
    monkeypatch.setitem(
        sys.modules, "anthropic", fake_anthropic(model_plan(pantry=["soľ"]), [], calls))

    odpoved = plan_client(server, 1).post("/api/plan/zo-spajze")

    assert odpoved.status_code == 200
    assert len(calls) == 1
    assert "soľ" in prompt_text(calls[0])


def test_the_pantry_driven_plan_is_never_written_into_the_shared_cache(monkeypatch, tmp_path):
    server = shared_plan_server(monkeypatch, tmp_path, users=(1,), pantry={1: ["soľ"]})
    monkeypatch.setitem(
        sys.modules, "anthropic", fake_anthropic(model_plan(pantry=["soľ"]), []))

    assert plan_client(server, 1).post("/api/plan/zo-spajze").status_code == 200

    with server.db() as con:
        assert con.execute("SELECT COUNT(*) FROM plany_zdielane").fetchone()[0] == 0, (
            "plán poskladaný z osobnej špajze nikdy nesmie skončiť v zdieľanom riadku"
        )
        osobny = con.execute("SELECT json FROM plany WHERE user_id=1").fetchone()
    assert osobny is not None


def test_cooking_from_the_pantry_consumes_one_of_the_daily_recomputes(monkeypatch, tmp_path):
    server = shared_plan_server(monkeypatch, tmp_path, users=(1,), pantry={1: ["soľ"]})
    monkeypatch.setitem(
        sys.modules, "anthropic", fake_anthropic(model_plan(pantry=["soľ"]), []))
    client = plan_client(server, 1)
    pred = client.get("/api/me").json()["zostava_prepoctov"]

    assert client.post("/api/plan/zo-spajze").status_code == 200

    assert client.get("/api/me").json()["zostava_prepoctov"] == pred - 1


def test_the_daily_ceiling_stops_cooking_from_the_pantry_too(monkeypatch, tmp_path):
    server = shared_plan_server(monkeypatch, tmp_path, users=(1,), pantry={1: ["soľ"]})
    constructors = []
    monkeypatch.setitem(
        sys.modules, "anthropic", fake_anthropic(model_plan(pantry=["soľ"]), constructors))
    monkeypatch.setattr(server, "limit_prepoctov", lambda premium: 0)

    odmietnutie = plan_client(server, 1).post("/api/plan/zo-spajze")

    assert odmietnutie.status_code == 429
    assert odmietnutie.json()["kod"] == server.KOD_LIMIT_PREPOCTOV
    assert constructors == []
