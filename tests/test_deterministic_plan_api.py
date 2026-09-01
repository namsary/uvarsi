"""HTTP contract for the synchronous deterministic recipe-engine rollout."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
import json
import sys
import types

import pytest

from app.deterministic_plan import NoCompatiblePlan, build_deterministic_plan
from app.ingredient_catalog import load_ingredient_catalog
from app.weekly_data import current_monday
from tests.test_recipe_mode_matrix import VERIFIED_WEEKLY_OFFERS
from tests.test_server import (
    grant_premium,
    insert_hashed_session,
    load_server,
    plan_client,
)


@pytest.fixture(autouse=True)
def _clear_recipe_engine_flag_cache():
    yield
    module = sys.modules.get("config")
    if module is not None and hasattr(module, "recipe_engine_mode"):
        module.recipe_engine_mode.cache_clear()


def _realistic_offer_rows():
    ingredients = load_ingredient_catalog()
    today = date.today()
    week = current_monday(today)
    valid_to = today + timedelta(days=6)
    rows = []
    for page, (ingredient_id, store, package, sale, ordinary) in enumerate(
        VERIFIED_WEEKLY_OFFERS, start=1
    ):
        ingredient = ingredients.by_id(ingredient_id)
        discount = round((1 - Decimal(sale) / Decimal(ordinary)) * 100)
        rows.append(
            (
                week,
                ingredient.name,
                store,
                float(sale),
                float(ordinary),
                f"-{discount} %",
                package,
                ingredient.category,
                f"https://fixtures.uvar.si/{store.casefold()}/{page}",
                page,
                today.isoformat(),
                valid_to.isoformat(),
            )
        )
    return rows


def _bomb_model_modules(monkeypatch):
    class ForbiddenClient:
        def __init__(self, *args, **kwargs):
            raise AssertionError("synchronous deterministic API constructed a model client")

    poison = types.SimpleNamespace(
        Anthropic=ForbiddenClient,
        AsyncAnthropic=ForbiddenClient,
        OpenAI=ForbiddenClient,
        AsyncOpenAI=ForbiddenClient,
        Client=ForbiddenClient,
    )
    monkeypatch.setitem(sys.modules, "anthropic", poison)
    monkeypatch.setitem(sys.modules, "openai", poison)


def _server(
    monkeypatch, tmp_path, *, mode="on", premium=True, pantry=(), diet="standard",
    offer_count=None,
):
    monkeypatch.setenv("UVARSI_RECIPE_ENGINE", mode)
    offer_rows = _realistic_offer_rows()
    if offer_count is not None:
        offer_rows = offer_rows[:offer_count]
    server = load_server(monkeypatch, tmp_path, offer_rows)
    server.recipe_engine_mode.cache_clear()
    with server.db() as con:
        con.execute(
            """INSERT INTO pouzivatelia
               (id,email,osoby,dospeli,deti,frekvencia,obchody,stravovanie)
               VALUES (1,'plan@uvar.si',4,2,2,2,'Lidl,Kaufland,Tesco',?)""",
            (diet,),
        )
        insert_hashed_session(server, con, "session-1", 1)
        for name, amount, unit in pantry:
            con.execute(
                "INSERT INTO spajza (user_id,nazov,mnozstvo,jednotka) VALUES (1,?,?,?)",
                (name, amount, unit),
            )
        con.commit()
    if premium:
        grant_premium(server, 1)
    _bomb_model_modules(monkeypatch)
    return server


def _counts(server):
    with server.db() as con:
        return {
            "jobs": con.execute("SELECT COUNT(*) FROM plan_jobs").fetchone()[0],
            "costs": con.execute("SELECT COUNT(*) FROM naklady").fetchone()[0],
            "shared": con.execute("SELECT COUNT(*) FROM plany_zdielane").fetchone()[0],
            "personal": con.execute("SELECT COUNT(*) FROM plany").fetchone()[0],
        }


@pytest.mark.parametrize("mode", ("off", "shadow"))
def test_off_and_shadow_keep_the_existing_queue_contract(monkeypatch, tmp_path, mode):
    server = _server(monkeypatch, tmp_path, mode=mode)

    response = plan_client(server, 1, wait_for_worker=False).post("/api/plan/generuj")

    assert response.status_code == 202
    assert response.json()["status"] == "preparing"
    assert _counts(server)["jobs"] == 1


def test_on_returns_a_ready_regular_plan_without_jobs_or_model_costs(monkeypatch, tmp_path):
    server = _server(monkeypatch, tmp_path)
    before = _counts(server)

    response = plan_client(server, 1, wait_for_worker=False).post("/api/plan/generuj")

    assert response.status_code == 200
    payload = response.json()
    assert payload["meta"]["engine"] == "deterministic"
    assert payload["meta"]["mode"] == "standard"
    assert sum(meal["pokryva_dni"] for meal in payload["jedla"]) == 7
    after = _counts(server)
    assert after["jobs"] == before["jobs"] == 0
    assert after["costs"] == before["costs"] == 0
    assert after["shared"] == after["personal"] == 1


def test_on_uses_exact_seed_and_known_pantry_without_sharing_pantry_plan(
    monkeypatch, tmp_path
):
    server = _server(
        monkeypatch,
        tmp_path,
        pantry=(("ryža", 950, "g"), ("tofu", 400, "g"), ("cícer", 500, "g")),
    )
    captured = []

    def recording_builder(**kwargs):
        captured.append(kwargs)
        return build_deterministic_plan(**kwargs)

    monkeypatch.setattr(server, "build_deterministic_plan", recording_builder)

    client = plan_client(server, 1, wait_for_worker=False)
    response = client.post("/api/plan/zo-spajze")

    assert response.status_code == 200
    payload = response.json()
    assert payload["meta"]["engine"] == "deterministic"
    assert captured[0]["pantry_driven"] is True
    with server.db() as con:
        user = con.execute("SELECT * FROM pouzivatelia WHERE id=1").fetchone()
        raw_pantry = server.spajza_pouzivatela(con, 1, True)
    signature = server.podpis_planu(
        server.monday(), ["Lidl", "Kaufland", "Tesco"], 2,
        server.akcie_pre(["Lidl", "Kaufland", "Tesco"]), raw_pantry,
        adults=2, children=2, zo_spajze=True,
        stravovanie=server.efektivne_stravovanie(user, True),
    )
    assert captured[0]["seed"] == f"{server.monday()}:{signature}:1"
    pantry_by_id = {item.ingredient_id: item for item in captured[0]["pantry"]}
    assert pantry_by_id["rice"].quantity.amount == Decimal("950")
    assert pantry_by_id["tofu"].quantity.amount == Decimal("400")
    counts = _counts(server)
    assert counts["jobs"] == counts["costs"] == counts["shared"] == 0
    assert counts["personal"] == 1
    assert client.get("/api/plan").json()["nakupny_zoznam"] == payload["nakupny_zoznam"]


@pytest.mark.parametrize(
    ("premium", "stored", "effective"),
    ((False, "vegan", "standard"), (True, "vegan", "vegan")),
)
def test_on_uses_only_the_server_authorized_diet_mode(
    monkeypatch, tmp_path, premium, stored, effective
):
    server = _server(
        monkeypatch, tmp_path, premium=premium, diet=stored,
    )

    response = plan_client(server, 1, wait_for_worker=False).post("/api/plan/generuj")

    assert response.status_code == 200
    assert response.json()["meta"]["mode"] == effective


def test_on_cache_hit_does_not_consume_another_generation(monkeypatch, tmp_path):
    server = _server(monkeypatch, tmp_path)
    client = plan_client(server, 1, wait_for_worker=False)

    first = client.post("/api/plan/generuj")
    second = client.post("/api/plan/generuj")

    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()
    with server.db() as con:
        assert con.execute("SELECT SUM(pocet) FROM prepocty").fetchone()[0] == 1
    assert _counts(server)["jobs"] == _counts(server)["costs"] == 0


def test_on_maps_exactly_fourteen_offers_to_an_actionable_typed_error(
    monkeypatch, tmp_path
):
    server = _server(monkeypatch, tmp_path, offer_count=14)

    response = plan_client(server, 1, wait_for_worker=False).post("/api/plan/generuj")

    assert response.status_code == 503
    payload = response.json()
    assert payload["kod"] == "insufficient_offers"
    assert [item["kod"] for item in payload["navrhy"]] == [
        "add_store",
        "wait_for_complete_flyer_refresh",
    ]
    assert all(item["text"] for item in payload["navrhy"])
    counts = _counts(server)
    assert counts["jobs"] == counts["costs"] == 0
    with server.db() as con:
        assert con.execute("SELECT COALESCE(SUM(pocet),0) FROM prepocty").fetchone()[0] == 0


def test_identical_pantry_request_reuses_personal_cache_without_quota_or_cost(
    monkeypatch, tmp_path
):
    server = _server(
        monkeypatch,
        tmp_path,
        pantry=(("ryža", 950, "g"), ("tofu", 400, "g"), ("cícer", 500, "g")),
    )
    client = plan_client(server, 1, wait_for_worker=False)

    first = client.post("/api/plan/zo-spajze")
    second = client.post("/api/plan/zo-spajze")

    assert first.status_code == second.status_code == 200
    assert second.json() == first.json()
    counts = _counts(server)
    assert counts["personal"] == 1
    assert counts["jobs"] == counts["costs"] == 0
    with server.db() as con:
        assert con.execute("SELECT COALESCE(SUM(pocet),0) FROM prepocty").fetchone()[0] == 1


def test_regular_selection_ignores_pantry_but_personal_shopping_uses_it(
    monkeypatch, tmp_path
):
    pantry = (("ryža", 950, "g"), ("tofu", 400, "g"), ("cícer", 500, "g"))
    with_pantry = _server(monkeypatch, tmp_path, pantry=pantry)
    response = plan_client(with_pantry, 1, wait_for_worker=False).post("/api/plan/generuj")
    assert response.status_code == 200
    personal = response.json()

    with with_pantry.db() as con:
        shared = json.loads(con.execute("SELECT json FROM plany_zdielane").fetchone()[0])

    assert [meal["recept"]["template_id"] for meal in personal["jedla"]] == [
        meal["recept"]["template_id"] for meal in shared["jedla"]
    ]
    for personal_meal, shared_meal in zip(personal["jedla"], shared["jedla"]):
        assert personal_meal["recept"]["skontroluj_doma"] == (
            shared_meal["recept"]["skontroluj_doma"]
        )
        assert all("spajza" not in item for item in personal_meal["suroviny"])
        assert all("spajza" not in item for item in shared_meal["suroviny"])
        assert not any(
            dose.endswith(" zo špajze")
            for dose in personal_meal["recept"]["davky"]
        )
        assert not any(
            dose.endswith(" zo špajze")
            for dose in shared_meal["recept"]["davky"]
        )
    assert personal["nakupny_zoznam"] != shared["nakupny_zoznam"]


def test_force_uses_the_next_bounded_variant_and_still_respects_daily_limit(
    monkeypatch, tmp_path
):
    server = _server(monkeypatch, tmp_path, premium=True)
    client = plan_client(server, 1, wait_for_worker=False)

    first = client.post("/api/plan/generuj")
    second = client.post("/api/plan/generuj?force=1")

    assert first.status_code == second.status_code == 200
    assert [meal["recept"]["template_id"] for meal in first.json()["jedla"]] != [
        meal["recept"]["template_id"] for meal in second.json()["jedla"]
    ]
    for _ in range(server.LIMIT_PREPOCTOV_PREMIUM - 2):
        assert client.post("/api/plan/generuj?force=1").status_code == 200
    refused = client.post("/api/plan/generuj?force=1")
    assert refused.status_code == 429
    assert refused.json()["kod"] == server.KOD_LIMIT_PREPOCTOV
    assert _counts(server)["jobs"] == _counts(server)["costs"] == 0


@pytest.mark.parametrize(
    ("code", "status", "suggestions"),
    (
        ("insufficient_offers", 503, ["add_store", "wait_for_complete_flyer_refresh"]),
        ("diet_too_strict", 422, ["add_store", "use_standard_mode"]),
        ("unmeasurable_packages", 503, ["wait_for_complete_flyer_refresh"]),
    ),
)
def test_typed_no_plan_errors_are_actionable_and_do_not_burn_the_limit(
    monkeypatch, tmp_path, code, status, suggestions
):
    server = _server(monkeypatch, tmp_path)

    def fail(**_kwargs):
        raise NoCompatiblePlan(code, suggestions)

    monkeypatch.setattr(server, "build_deterministic_plan", fail)
    response = plan_client(server, 1, wait_for_worker=False).post("/api/plan/generuj")

    assert response.status_code == status
    payload = response.json()
    assert payload["kod"] == code
    assert [item["kod"] for item in payload["navrhy"]] == suggestions
    assert all(item["text"] for item in payload["navrhy"])
    assert _counts(server)["jobs"] == _counts(server)["costs"] == 0
    with server.db() as con:
        assert con.execute("SELECT COALESCE(SUM(pocet),0) FROM prepocty").fetchone()[0] == 0


def test_cache_write_is_atomic_when_shared_storage_fails(monkeypatch, tmp_path):
    server = _server(monkeypatch, tmp_path)

    def fail_shared(*_args, **_kwargs):
        raise RuntimeError("controlled storage failure")

    monkeypatch.setattr(server, "uloz_zdielany_plan", fail_shared)
    response = plan_client(server, 1, wait_for_worker=False).post("/api/plan/generuj")

    assert response.status_code == 500
    assert "controlled storage failure" not in response.text
    counts = _counts(server)
    assert counts["shared"] == counts["personal"] == 0
    with server.db() as con:
        assert con.execute("SELECT COALESCE(SUM(pocet),0) FROM prepocty").fetchone()[0] == 0
