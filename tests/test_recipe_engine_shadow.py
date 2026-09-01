"""Shadow rollout gate for the deterministic recipe engine.

The scheduled sampler may inspect flyer offers and generated plans in memory,
but its durable output is deliberately anonymous and aggregate-only.
"""

from __future__ import annotations

from contextlib import closing
from datetime import date, datetime, timedelta
from decimal import Decimal
import json
import sys
import threading
import types

from fastapi.testclient import TestClient

from app.deterministic_plan import build_deterministic_plan
from app.ingredient_catalog import load_ingredient_catalog
from app.weekly_data import current_monday
from tests.test_recipe_mode_matrix import VERIFIED_WEEKLY_OFFERS
from tests.test_server import insert_hashed_session, load_server


MODES = ("standard", "high_protein", "vegetarian", "vegan")
HOUSEHOLDS = ((1, 0), (2, 2), (4, 0))
FREQUENCIES = (1, 2, 3)
EXPECTED_MATRIX = {
    (mode, adults, children, frequency)
    for mode in MODES
    for adults, children in HOUSEHOLDS
    for frequency in FREQUENCIES
}


def _offer_rows():
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


def _server(monkeypatch, tmp_path, *, mode="shadow"):
    monkeypatch.setenv("UVARSI_RECIPE_ENGINE", mode)
    server = load_server(monkeypatch, tmp_path, _offer_rows())
    server.recipe_engine_mode.cache_clear()
    with closing(server.db()) as con:
        con.execute(
            """INSERT INTO pouzivatelia
               (id,email,osoby,dospeli,deti,frekvencia,obchody,stravovanie)
               VALUES (1,'shadow-person@example.test',4,2,2,2,
                       'Lidl,Kaufland,Tesco','standard')"""
        )
        insert_hashed_session(server, con, "shadow-session", 1)
        con.commit()
    return server


def _forbid_model_clients(monkeypatch):
    class ForbiddenClient:
        def __init__(self, *_args, **_kwargs):
            raise AssertionError("shadow sampler attempted to construct a model client")

    poison = types.SimpleNamespace(
        Anthropic=ForbiddenClient,
        AsyncAnthropic=ForbiddenClient,
        OpenAI=ForbiddenClient,
        AsyncOpenAI=ForbiddenClient,
        Client=ForbiddenClient,
    )
    monkeypatch.setitem(sys.modules, "anthropic", poison)
    monkeypatch.setitem(sys.modules, "openai", poison)


def _clock(step_seconds=0.100):
    value = -step_seconds

    def tick():
        nonlocal value
        value += step_seconds
        return value

    return tick


def test_shadow_user_request_never_runs_deterministic_builder_inline(
    monkeypatch, tmp_path
):
    server = _server(monkeypatch, tmp_path)

    def bomb(**_kwargs):
        raise AssertionError("user request ran the shadow builder inline")

    monkeypatch.setattr(server, "build_deterministic_plan", bomb)
    client = TestClient(server.app)
    client.cookies.set(server.COOKIE, "shadow-session")

    response = client.post("/api/plan/generuj")

    assert response.status_code == 202
    assert response.json()["status"] == "preparing"


def test_scheduled_shadow_builds_the_fixed_anonymous_matrix_and_only_persists_aggregates(
    monkeypatch, tmp_path, caplog
):
    server = _server(monkeypatch, tmp_path)
    predpocet = server.predpocet
    _forbid_model_clients(monkeypatch)
    seen = []

    def recording_builder(**kwargs):
        seen.append(
            (kwargs["mode"], kwargs["adults"], kwargs["children"], kwargs["frequency"])
        )
        return build_deterministic_plan(**kwargs)

    monkeypatch.setattr(predpocet, "build_deterministic_plan", recording_builder)
    monkeypatch.setattr(predpocet.time, "perf_counter", _clock())

    result = predpocet.run_recipe_engine_shadow(
        server=server, now=datetime.combine(date.today(), datetime.min.time())
    )

    assert set(seen) == EXPECTED_MATRIX
    assert len(seen) == len(EXPECTED_MATRIX) == 36
    assert result["samples_total"] == result["samples_success"] == 36
    assert result["success_rate"] == 1.0
    assert result["p95_ms"] == 100.0
    assert result["complete"] is True
    assert result["library_gate"] == "pass"
    assert result["dietary_violations"] == 0
    assert result["negative_quantities"] == 0
    assert result["invalid_package_counts"] == 0

    with closing(server.db()) as con:
        row = dict(con.execute("SELECT * FROM recipe_engine_shadow").fetchone())
        columns = {
            value[1] for value in con.execute("PRAGMA table_info(recipe_engine_shadow)")
        }
    forbidden_columns = {
        "email", "user_id", "pantry", "spajza", "recipe", "recept", "plan_json"
    }
    assert not (columns & forbidden_columns)
    assert row["samples_total"] == 36
    assert json.loads(row["error_counts"]) == {}
    durable = json.dumps(row, ensure_ascii=False)
    assert "shadow-person@example.test" not in durable
    assert "shadow-session" not in durable
    assert "jedla" not in durable
    assert "shadow-person@example.test" not in caplog.text


def test_shadow_records_only_error_codes_and_never_exception_text(monkeypatch, tmp_path):
    server = _server(monkeypatch, tmp_path)
    predpocet = server.predpocet

    def broken_builder(**kwargs):
        if kwargs["mode"] == "vegan":
            raise RuntimeError("shadow-person@example.test secret recipe text")
        return build_deterministic_plan(**kwargs)

    monkeypatch.setattr(predpocet, "build_deterministic_plan", broken_builder)
    monkeypatch.setattr(predpocet.time, "perf_counter", _clock())

    result = predpocet.run_recipe_engine_shadow(server=server)

    assert result["samples_success"] == 27
    assert result["error_counts"] == {"internal_error": 9}
    with closing(server.db()) as con:
        durable = con.execute(
            "SELECT error_counts FROM recipe_engine_shadow"
        ).fetchone()[0]
    assert json.loads(durable) == {"internal_error": 9}
    assert "example.test" not in durable
    assert "secret recipe" not in durable


def test_scheduled_precompute_invokes_shadow_sampler_only_in_shadow_mode(
    monkeypatch, tmp_path
):
    server = _server(monkeypatch, tmp_path)
    predpocet = server.predpocet
    calls = []
    monkeypatch.setattr(
        predpocet,
        "enqueue_popular_profiles",
        lambda **_kwargs: {
            "tyzden": current_monday(), "profilov": 0, "queued": 0,
            "skipped": 0, "blocked": 0, "zahriatych": 0,
            "preskocenych": 0, "zlyhanych": 0, "eur": 0.0,
            "dovod": predpocet.DOVOD_HOTOVO,
        },
    )
    monkeypatch.setattr(
        predpocet,
        "run_recipe_engine_shadow",
        lambda **kwargs: calls.append(kwargs) or {"complete": True},
    )

    result = predpocet.zahrej(pocet=0)

    assert len(calls) == 1
    assert calls[0]["server"] is server
    assert result["shadow"] == {"complete": True}

    server.recipe_engine_mode.cache_clear()
    monkeypatch.setenv("UVARSI_RECIPE_ENGINE", "off")
    server.recipe_engine_mode.cache_clear()
    predpocet.zahrej(pocet=0)
    assert len(calls) == 1


def test_activation_is_eligible_only_for_fresh_complete_metrics_that_meet_every_floor(
    monkeypatch, tmp_path
):
    server = _server(monkeypatch, tmp_path)
    predpocet = server.predpocet
    monkeypatch.setattr(predpocet.time, "perf_counter", _clock())
    predpocet.run_recipe_engine_shadow(server=server)

    with closing(server.db()) as con:
        status = server.recipe_engine_shadow_status(con, today=date.today())

    assert status["eligible"] is True
    assert status["reasons"] == []
    assert status["week"] == current_monday()
    assert status["success_rate"] == 1.0
    assert status["p95_ms"] == 100.0


def test_activation_fails_closed_when_metrics_are_missing_incomplete_or_stale(
    monkeypatch, tmp_path
):
    server = _server(monkeypatch, tmp_path)
    predpocet = server.predpocet

    with closing(server.db()) as con:
        missing = predpocet.shadow_activation_status(
            con, server=server, today=date.today()
        )
    assert missing["eligible"] is False
    assert missing["reasons"] == ["missing_metrics"]

    monkeypatch.setattr(predpocet.time, "perf_counter", _clock())
    predpocet.run_recipe_engine_shadow(server=server)
    with closing(server.db()) as con:
        con.execute("UPDATE recipe_engine_shadow SET complete=0")
        con.commit()
        incomplete = predpocet.shadow_activation_status(
            con, server=server, today=date.today()
        )
    assert incomplete["eligible"] is False
    assert "incomplete_metrics" in incomplete["reasons"]

    with closing(server.db()) as con:
        con.execute(
            "UPDATE recipe_engine_shadow SET complete=1, offer_fingerprint='stale'"
        )
        con.commit()
        stale = predpocet.shadow_activation_status(
            con, server=server, today=date.today()
        )
    assert stale["eligible"] is False
    assert "stale_metrics" in stale["reasons"]

    monkeypatch.setattr(server, "PLAN_ALGO_VERSION", server.PLAN_ALGO_VERSION + 1)
    with closing(server.db()) as con:
        con.execute(
            "UPDATE recipe_engine_shadow SET offer_fingerprint=?",
            (predpocet._shadow_offer_fingerprint(
                predpocet._shadow_offer_rows(con, server, date.today())
            ),),
        )
        con.commit()
        stale_algorithm = predpocet.shadow_activation_status(
            con, server=server, today=date.today()
        )
    assert stale_algorithm["eligible"] is False
    assert "stale_metrics" in stale_algorithm["reasons"]


def test_activation_checks_every_numeric_and_library_floor(monkeypatch, tmp_path):
    server = _server(monkeypatch, tmp_path)
    predpocet = server.predpocet
    monkeypatch.setattr(predpocet.time, "perf_counter", _clock())
    predpocet.run_recipe_engine_shadow(server=server)

    cases = (
        ("p95_ms", 500.0, "p95_too_slow"),
        ("dietary_violations", 1, "dietary_violations"),
        ("negative_quantities", 1, "negative_quantities"),
        ("invalid_package_counts", 1, "invalid_package_counts"),
        ("library_gate_pass", 0, "library_gate_failed"),
    )
    with closing(server.db()) as con:
        original = dict(con.execute("SELECT * FROM recipe_engine_shadow").fetchone())
        con.execute(
            """UPDATE recipe_engine_shadow
                  SET samples_success=35, success_rate=?, error_counts=?
                WHERE tyzden=?""",
            (35 / 36, '{"internal_error":1}', original["tyzden"]),
        )
        con.commit()
        below_floor = predpocet.shadow_activation_status(
            con, server=server, today=date.today()
        )
        assert below_floor["eligible"] is False
        assert "success_rate_below_floor" in below_floor["reasons"]
        con.execute(
            """UPDATE recipe_engine_shadow
                  SET samples_success=?, success_rate=?, error_counts=?
                WHERE tyzden=?""",
            (
                original["samples_success"], original["success_rate"],
                original["error_counts"], original["tyzden"],
            ),
        )
        con.commit()
        for column, value, reason in cases:
            con.execute(
                f"UPDATE recipe_engine_shadow SET {column}=? WHERE tyzden=?",
                (value, original["tyzden"]),
            )
            con.commit()
            status = predpocet.shadow_activation_status(
                con, server=server, today=date.today()
            )
            assert status["eligible"] is False
            assert reason in status["reasons"]
            con.execute(
                f"UPDATE recipe_engine_shadow SET {column}=? WHERE tyzden=?",
                (original[column], original["tyzden"]),
            )
            con.commit()


def test_partial_store_week_passes_shadow_and_on_smoke_when_offers_are_sufficient(
    monkeypatch, tmp_path
):
    server = _server(monkeypatch, tmp_path)
    predpocet = server.predpocet
    monkeypatch.setattr(predpocet.time, "perf_counter", _clock())
    with closing(server.db()) as con:
        con.execute("DELETE FROM akcie WHERE obchod='Lidl'")
        con.execute(
            "DELETE FROM zber_stav WHERE tyzden=? AND obchod='Lidl'",
            (current_monday(),),
        )
        con.commit()
    shadow = predpocet.run_recipe_engine_shadow(server=server)
    with closing(server.db()) as con:
        status = predpocet.shadow_activation_status(
            con, server=server, today=date.today()
        )

    monkeypatch.setenv("UVARSI_RECIPE_ENGINE", "on")
    server.recipe_engine_mode.cache_clear()
    smoke = server.run_recipe_engine_synthetic_smoke(
        state_path=tmp_path / "partial-store-smoke.json"
    )

    assert shadow["complete"] is True
    assert status["eligible"] is True
    assert status["reasons"] == []
    assert smoke["ok"] is True
    assert smoke["blockers"] == []


def test_shadow_still_rejects_a_week_below_safe_offer_minimum(monkeypatch, tmp_path):
    server = _server(monkeypatch, tmp_path)
    predpocet = server.predpocet
    with closing(server.db()) as con:
        kept = [row[0] for row in con.execute(
            "SELECT rowid FROM akcie ORDER BY rowid LIMIT 14"
        )]
        marks = ",".join("?" for _ in kept)
        con.execute(f"DELETE FROM akcie WHERE rowid NOT IN ({marks})", kept)
        con.commit()

    shadow = predpocet.run_recipe_engine_shadow(server=server)

    assert shadow == {
        "week": current_monday(),
        "complete": False,
        "reason": "incomplete_flyer_week",
    }


def test_concurrent_shadow_run_cannot_finalize_another_fingerprint(
    monkeypatch, tmp_path
):
    server = _server(monkeypatch, tmp_path)
    predpocet = server.predpocet
    with closing(server.db()) as con:
        offers_a = tuple(
            dict(row) for row in predpocet._shadow_offer_rows(con, server, date.today())
        )
    offers_b = tuple(dict(row) for row in offers_a)
    offers_b[0]["valid_to"] = (date.today() + timedelta(days=7)).isoformat()
    fingerprint_b = predpocet._shadow_offer_fingerprint(offers_b)

    a_started = threading.Event()
    release_a = threading.Event()
    results = {}
    failures = []

    def offers_for_run(_con, _server, _today):
        return offers_a if threading.current_thread().name == "shadow-A" else offers_b

    def builder_for_run(**_kwargs):
        if threading.current_thread().name == "shadow-A":
            if not a_started.is_set():
                a_started.set()
                assert release_a.wait(10)
            return {
                "jedla": [], "nakupny_zoznam": [], "nakup_spolu": "0,00",
                "bezna_cena": "0,00", "usetrene": "0,00",
            }
        raise RuntimeError("B deliberately records a failed sample")

    monkeypatch.setattr(predpocet, "_shadow_offer_rows", offers_for_run)
    monkeypatch.setattr(predpocet, "build_deterministic_plan", builder_for_run)

    def run(label):
        try:
            results[label] = predpocet.run_recipe_engine_shadow(server=server)
        except Exception as exc:  # surfaced below without losing the other thread
            failures.append(exc)

    thread_a = threading.Thread(target=run, args=("A",), name="shadow-A")
    thread_a.start()
    assert a_started.wait(10)
    thread_b = threading.Thread(target=run, args=("B",), name="shadow-B")
    thread_b.start()
    thread_b.join(10)
    assert not thread_b.is_alive()
    release_a.set()
    thread_a.join(10)
    assert not thread_a.is_alive()
    assert failures == []

    with closing(server.db()) as con:
        row = dict(con.execute("SELECT * FROM recipe_engine_shadow").fetchone())
    assert row["offer_fingerprint"] == fingerprint_b
    assert row["complete"] == 1
    assert row["samples_success"] == 0
    assert results["B"]["complete"] is True
    assert results["A"] == {
        "week": current_monday(), "complete": False, "reason": "superseded"
    }


def test_activation_rejects_corrupt_inconsistent_or_non_finite_metrics(
    monkeypatch, tmp_path
):
    server = _server(monkeypatch, tmp_path)
    predpocet = server.predpocet
    monkeypatch.setattr(predpocet.time, "perf_counter", _clock())
    predpocet.run_recipe_engine_shadow(server=server)

    corruptions = (
        ("error_counts", "{"),
        ("error_counts", "[]"),
        ("success_rate", "NaN"),
        ("success_rate", "inf"),
        ("success_rate", -0.1),
        ("success_rate", 0.5),
        ("p95_ms", "NaN"),
        ("p95_ms", "inf"),
        ("p95_ms", -1),
        ("price_delta_eur_avg", "NaN"),
        ("price_delta_eur_avg", "inf"),
        ("price_delta_eur_avg", -0.01),
        ("matrix_size", 36.5),
        ("samples_total", -1),
        ("samples_success", 37),
        ("family_count", -1),
        ("method_count", -1),
        ("price_comparisons", 37),
        ("dietary_violations", -1),
        ("negative_quantities", -1),
        ("invalid_package_counts", -1),
        ("library_gate_pass", 2),
        ("complete", 2),
    )
    with closing(server.db()) as con:
        original = dict(con.execute("SELECT * FROM recipe_engine_shadow").fetchone())
        for column, value in corruptions:
            con.execute(
                f"UPDATE recipe_engine_shadow SET {column}=? WHERE tyzden=?",
                (value, original["tyzden"]),
            )
            con.commit()
            status = predpocet.shadow_activation_status(
                con, server=server, today=date.today()
            )
            assert status["eligible"] is False, (column, value, status)
            assert "invalid_metrics" in status["reasons"], (column, value, status)
            con.execute(
                f"UPDATE recipe_engine_shadow SET {column}=? WHERE tyzden=?",
                (original[column], original["tyzden"]),
            )
            con.commit()
