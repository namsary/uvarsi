import datetime
import importlib
import json
import sqlite3
import sys
import threading
import time
import types
from pathlib import Path

import pytest

from app import plan_jobs
from app import plan_calendar
from app.offer_data import offer_key_for
from app.plan_jobs import JobRequest
from app import plan_worker
from app.plan_worker import ProcessResult, process_one


ROOT = Path(__file__).resolve().parents[1]
NOW = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None, microsecond=0)
STORES = ["Lidl", "Kaufland", "Tesco"]
DIVERSE_RECIPES = (
    ("Kuracie s ryžou", [
        "V hrnci zohrej 2 lyžice oleja na strednom ohni 2 minúty, kým sa rozvonia.",
        "Pridaj 400 g kuracieho mäsa nakrájaného na kocky a var 8 minút, kým nezhnedne.",
        "Vsyp 300 g ryže, prilej 600 ml vody a var 15 minút na miernom ohni, kým ryža nezmäkne.",
        "Kuracie s ryžou rozdeľ na 4 taniere a podávaj horúce.",
    ]),
    ("Bravčové s cestovinami", [
        "Na panvici zohrej 2 lyžice oleja na strednom ohni 2 minúty, kým sa rozvonia.",
        "Pridaj 400 g bravčového mäsa nakrájaného na pásiky a opekaj 10 minút do zlatista.",
        "Vmiešaj 300 g cestovín a 200 ml vody, prehrievaj 5 minút, kým omáčka nezhustne.",
        "Bravčové s cestovinami rozdeľ na 4 taniere a podávaj horúce.",
    ]),
    ("Losos so zemiakmi", [
        "Rúru predhrej na 200 °C a plech potri 2 lyžicami oleja.",
        "Na plech polož 400 g lososa a 300 g zemiakov nakrájaných na kolieska.",
        "Peč 25 minút, kým losos nie je šťavnatý a zemiaky nie sú dozlata.",
        "Lososa so zemiakmi rozdeľ na 4 taniere a podávaj horúce.",
    ]),
)


def _offer(index, store):
    today = NOW.date()
    return {
        "tyzden": (today - datetime.timedelta(days=today.weekday())).isoformat(),
        "obchod": store,
        "nazov": f"Ponuka {index}",
        "kategoria": "trvanlive",
        "cena": 1.0 + index / 100,
        "povodna": 2.0 + index / 100,
        "zlava": "-50 %",
        "jednotka": "1 kg",
        "source_url": f"https://example.test/{store}/{index}.jpg",
        "source_page": index,
        "valid_from": (today - datetime.timedelta(days=1)).isoformat(),
        "valid_to": (today + datetime.timedelta(days=1)).isoformat(),
    }


@pytest.fixture
def app_db(tmp_path, monkeypatch):
    database = tmp_path / "uvarsi.db"
    monkeypatch.setenv("UVARSI_DB", str(database))
    monkeypatch.setenv("UVARSI_URL", "https://uvar.si")
    monkeypatch.syspath_prepend(str(ROOT / "app"))
    sys.modules.pop("server", None)
    server = importlib.import_module("server")
    server.priprav_databazu()

    with server.db() as con:
        con.execute(
            """CREATE TABLE zber_stav (
                tyzden TEXT NOT NULL, obchod TEXT NOT NULL, stav TEXT NOT NULL,
                pocet INTEGER NOT NULL DEFAULT 0, detail TEXT, updated TEXT,
                PRIMARY KEY (tyzden, obchod)
            )"""
        )
        rows = [_offer(index, STORES[(index - 1) % len(STORES)]) for index in range(1, 25)]
        for row in rows:
            row["offer_key"] = offer_key_for(row["tyzden"], row)
            con.execute(
                """INSERT INTO akcie (
                    tyzden, obchod, nazov, kategoria, cena, povodna, zlava,
                    jednotka, source_url, source_page, valid_from, valid_to, offer_key
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                tuple(row[field] for field in (
                    "tyzden", "obchod", "nazov", "kategoria", "cena", "povodna",
                    "zlava", "jednotka", "source_url", "source_page", "valid_from",
                    "valid_to", "offer_key",
                )),
            )
        con.executemany(
            "INSERT INTO zber_stav (tyzden, obchod, stav, pocet) VALUES (?, ?, 'ok', 8)",
            [(server.monday(NOW.date()), store) for store in STORES],
        )
        con.execute(
            """INSERT INTO pouzivatelia
               (id, email, osoby, dospeli, deti, frekvencia, obchody)
               VALUES (1, 'worker@uvar.si', 4, 2, 2, 2, ?)""",
            (",".join(STORES),),
        )
        con.commit()
    return types.SimpleNamespace(server=server, path=database)


def _model_output(offer_keys):
    return {
        "meals": [
            {
                "day": day,
                "name": DIVERSE_RECIPES[(index - 1) % len(DIVERSE_RECIPES)][0],
                "minutes": 30,
                "instructions": DIVERSE_RECIPES[(index - 1) % len(DIVERSE_RECIPES)][1],
                "items": [{
                    "offer_key": offer_key,
                    "quantity": 1,
                    "amount_per_person": 150,
                    "unit": "g",
                }],
            }
            for index, (day, offer_key) in enumerate(
                zip(("PO", "ST", "PI", "NE"), offer_keys), start=1
            )
        ]
    }


class FakeModel:
    def __init__(self, output):
        self.output = output
        self.calls = 0
        self.messages = self

    def create(self, **_kwargs):
        self.calls += 1
        usage = types.SimpleNamespace(
            input_tokens=100, output_tokens=100,
            cache_creation_input_tokens=0, cache_read_input_tokens=0,
        )
        return types.SimpleNamespace(
            content=[types.SimpleNamespace(type="text", text=json.dumps(self.output))],
            stop_reason="end_turn",
            usage=usage,
        )


class SequenceModel(FakeModel):
    def __init__(self, outputs):
        super().__init__(None)
        self.outputs = list(outputs)
        self.requests = []

    def create(self, **kwargs):
        self.requests.append(kwargs)
        self.output = self.outputs[min(self.calls, len(self.outputs) - 1)]
        return super().create(**kwargs)


class TimeoutModel:
    def __init__(self):
        self.calls = 0
        self.messages = self

    def create(self, **_kwargs):
        self.calls += 1
        raise TimeoutError("request timed out after dispatch")


def _grant_premium(app_db, user_id=1):
    with app_db.server.db() as con:
        con.execute(
            """INSERT INTO naroky (
                user_id, produkt, poskytovatel, objednavka_id, suma_centy,
                mena, stav, ziskany_o, zmeneny_o
            ) VALUES (?, 'zakladajuci_clen', 'lemonsqueezy', ?, 1900,
                      'EUR', 'aktivny', 1, 1)""",
            (user_id, f"ord-{user_id}"),
        )
        con.commit()


def _queued_job(app_db, *, kind="regular", payload_changes=None, priority=100):
    server = app_db.server
    rows = server.akcie_pre(STORES)
    pantry = []
    if kind == "pantry":
        with server.db() as con:
            pantry = server.spajza_pouzivatela(con, 1, server.je_premium(con, 1))
    signature = server.podpis_planu(
        server.monday(NOW.date()), STORES, 2, rows, pantry,
        adults=2, children=2, zo_spajze=kind == "pantry",
    )
    payload = {
        "stores": STORES,
        "frequency": 2,
        "adults": 2,
        "children": 2,
        "algo_version": server.PLAN_ALGO_VERSION,
    }
    if kind == "pantry":
        payload["pantry_signature"] = server.podpis_spajze(pantry)
    if payload_changes:
        payload.update(payload_changes)
    with server.db() as con:
        return plan_jobs.enqueue(
            con,
            JobRequest(
                job_key=f"{kind}:{signature}:0",
                signature=signature,
                variant=0,
                kind=kind,
                user_id=None if kind == "precompute" else 1,
                week=server.monday(NOW.date()),
                priority=priority,
                payload=payload,
                regeneration_limit=None if kind == "precompute" else 5,
                regeneration_day=None if kind == "precompute" else NOW.date().isoformat(),
            ),
            now=NOW,
        ).job


def _queued_regular_job(app_db):
    return _queued_job(app_db)


def _job_row(app_db, job_id):
    con = sqlite3.connect(app_db.path)
    con.row_factory = sqlite3.Row
    try:
        return con.execute("SELECT * FROM plan_jobs WHERE id=?", (job_id,)).fetchone()
    finally:
        con.close()


def _shared_plan_count(app_db, job):
    with app_db.server.db() as con:
        return con.execute(
            "SELECT COUNT(*) FROM plany_zdielane WHERE podpis=? AND variant=?",
            (job.signature, job.variant),
        ).fetchone()[0]


def _mutable_job(app_db, mutation):
    kind = "pantry" if mutation == "pantry" else "regular"
    if kind == "pantry":
        _grant_premium(app_db)
        with app_db.server.db() as con:
            con.execute("INSERT INTO spajza (user_id, nazov) VALUES (1, 'soľ')")
            con.commit()
    job = _queued_job(app_db, kind=kind)
    rows = app_db.server.akcie_pre(STORES)
    output = _model_output([row["offer_key"] for row in rows[:4]])
    if kind == "pantry":
        output["meals"][0]["pantry_ingredients"] = ["soľ"]
    return job, output


def _mutate_current_input(app_db, mutation):
    with app_db.server.db() as con:
        if mutation == "collection":
            con.execute(
                "UPDATE zber_stav SET stav='fail' WHERE tyzden=? AND obchod='Tesco'",
                (app_db.server.monday(NOW.date()),),
            )
        elif mutation == "offers":
            row = con.execute("SELECT id, cena FROM akcie ORDER BY id LIMIT 1").fetchone()
            con.execute("UPDATE akcie SET cena=? WHERE id=?", (row["cena"] + 0.01, row["id"]))
        elif mutation == "pantry":
            con.execute("UPDATE spajza SET nazov='ryža' WHERE user_id=1")
        else:  # pragma: no cover - protects the test helper itself
            raise AssertionError(mutation)
        con.commit()


def test_valid_diverse_worker_plan_uses_exactly_one_model_call(app_db):
    job = _queued_regular_job(app_db)
    rows = app_db.server.akcie_pre(STORES)
    fake_model = FakeModel(_model_output([row["offer_key"] for row in rows[:4]]))

    result = process_one(client=fake_model, now=NOW)

    assert result.job_id == job.id
    assert _job_row(app_db, job.id)["state"] == "ready"
    with app_db.server.db() as con:
        assert con.execute(
            "SELECT 1 FROM plany_zdielane WHERE podpis=? AND variant=?",
            (job.signature, job.variant),
        ).fetchone()
        assert con.execute("SELECT COUNT(*) FROM plany").fetchone()[0] == 0
    assert fake_model.calls == 1, "platný rozmanitý plán nesmie spustiť opravné volanie"


def test_worker_repairs_one_semantically_invalid_model_plan_with_feedback(app_db):
    job = _queued_regular_job(app_db)
    rows = app_db.server.akcie_pre(STORES)
    valid = _model_output([row["offer_key"] for row in rows[:4]])
    invalid = json.loads(json.dumps(valid))
    invalid["meals"][0]["name"] = "Smotanové jedlo"
    model = SequenceModel([invalid, valid])

    result = process_one(client=model, now=NOW)

    assert result.status == "ready"
    assert model.calls == 2
    assert _job_row(app_db, job.id)["state"] == "ready"
    retry_messages = model.requests[1]["messages"]
    assert len(retry_messages) == 3
    assert "smotanové" in retry_messages[2]["content"].casefold()


def test_worker_stops_after_one_semantic_repair_attempt(app_db):
    job = _queued_regular_job(app_db)
    rows = app_db.server.akcie_pre(STORES)
    invalid = _model_output([row["offer_key"] for row in rows[:4]])
    invalid["meals"][0]["name"] = "Smotanové jedlo"
    model = SequenceModel([invalid, invalid, invalid])

    result = process_one(client=model, now=NOW)

    assert result.status == "failed"
    assert result.error_code == "invalid_model_output"
    assert model.calls == 2
    assert _job_row(app_db, job.id)["state"] == "failed"


def test_completed_store_with_no_purchasable_offer_does_not_block_other_stores(app_db):
    with app_db.server.db() as con:
        for stored in con.execute("SELECT * FROM akcie WHERE obchod='Lidl'").fetchall():
            changed = dict(stored)
            changed["jednotka"] = "hrsť"
            con.execute(
                "UPDATE akcie SET jednotka=?, offer_key=? WHERE id=?",
                (changed["jednotka"], offer_key_for(changed["tyzden"], changed), stored["id"]),
            )
        con.commit()
    job = _queued_regular_job(app_db)
    rows = app_db.server.akcie_pre(STORES)
    assert len(rows) >= app_db.server.MIN_OFFERS_FOR_PLAN
    assert {row["obchod"] for row in rows} == {"Kaufland", "Tesco"}
    fake_model = FakeModel(_model_output([row["offer_key"] for row in rows[:4]]))

    result = process_one(client=fake_model, now=NOW)

    assert result.status == "ready"
    assert fake_model.calls == 1
    assert _job_row(app_db, job.id)["state"] == "ready"


def test_builder_never_calls_model_when_no_offer_has_a_purchasable_unit(
        app_db, monkeypatch):
    rows = [dict(row, jednotka="hrsť") for row in app_db.server.akcie_pre(STORES)]
    job = types.SimpleNamespace(kind="regular", variant=0, reserved_eur=0, id=999)
    monkeypatch.setattr(
        app_db.server,
        "_job_context",
        lambda *_args: (STORES, 2, 2, 2, rows, [], None),
    )
    fake_model = FakeModel({"meals": []})

    with pytest.raises(Exception) as raised:
        app_db.server.build_and_store_job(job, client=fake_model)

    assert getattr(raised.value, "status_code", None) == 503
    assert fake_model.calls == 0


def test_worker_never_retries_after_dispatch_timeout(app_db):
    job = _queued_regular_job(app_db)
    timeout_model = TimeoutModel()

    process_one(client=timeout_model, now=NOW)
    process_one(client=timeout_model, now=NOW + datetime.timedelta(minutes=3))

    assert timeout_model.calls == 1
    row = _job_row(app_db, job.id)
    assert (row["state"], row["error_code"]) == ("failed", "provider_timeout")


def test_worker_recovers_a_dead_worker_before_dispatch(app_db):
    job = _queued_regular_job(app_db)
    with app_db.server.db() as con:
        claimed = plan_jobs.claim_next(con, "dead-worker", now=NOW, lease_seconds=1)
    assert claimed.id == job.id
    model = FakeModel(_model_output([
        row["offer_key"] for row in app_db.server.akcie_pre(STORES)[:4]
    ]))

    result = process_one(client=model, now=NOW + datetime.timedelta(seconds=2))

    assert result.status == "ready"
    assert model.calls == 1
    assert _job_row(app_db, job.id)["attempts"] == 2


def test_worker_death_after_dispatch_is_terminal_without_another_model_call(app_db):
    job = _queued_regular_job(app_db)
    with app_db.server.db() as con:
        plan_jobs.claim_next(con, "dead-worker", now=NOW, lease_seconds=1)
        assert plan_jobs.mark_dispatched(
            con, job.id, worker_id="dead-worker", now=NOW,
        )
    model = FakeModel(_model_output([
        row["offer_key"] for row in app_db.server.akcie_pre(STORES)[:4]
    ]))

    result = process_one(client=model, now=NOW + datetime.timedelta(seconds=2))

    assert result.status == "empty"
    assert model.calls == 0
    row = _job_row(app_db, job.id)
    assert (row["state"], row["error_code"]) == (
        "failed", "worker_lost_after_dispatch",
    )
    assert _shared_plan_count(app_db, job) == 0


def test_client_setup_failure_retries_only_twice_before_dispatch(app_db, monkeypatch):
    job = _queued_regular_job(app_db)
    attempts = []

    def fail_before_dispatch():
        attempts.append("setup")
        raise OSError("client setup failed")

    monkeypatch.setattr(app_db.server, "_new_plan_model_client", fail_before_dispatch)

    first = process_one(now=NOW)
    second = process_one(now=NOW + datetime.timedelta(seconds=1))
    third = process_one(now=NOW + datetime.timedelta(seconds=2))

    assert (first.status, second.status, third.status) == ("queued", "failed", "empty")
    assert attempts == ["setup", "setup"]
    row = _job_row(app_db, job.id)
    assert row["attempts"] == 2 and row["dispatched_at"] is None
    with app_db.server.db() as con:
        assert con.execute("SELECT COUNT(*) FROM naklady").fetchone()[0] == 0


def test_pantry_change_while_queued_fails_before_dispatch(app_db):
    _grant_premium(app_db)
    with app_db.server.db() as con:
        con.execute("INSERT INTO spajza (user_id, nazov) VALUES (1, 'soľ')")
        con.commit()
    job = _queued_job(app_db, kind="pantry")
    with app_db.server.db() as con:
        con.execute("UPDATE spajza SET nazov='ryža' WHERE user_id=1")
        con.commit()
    model = FakeModel(_model_output([
        row["offer_key"] for row in app_db.server.akcie_pre(STORES)[:4]
    ]))

    result = process_one(client=model, now=NOW)

    assert (result.status, result.error_code) == ("failed", "stale_pantry")
    assert model.calls == 0
    assert _job_row(app_db, job.id)["dispatched_at"] is None
    with app_db.server.db() as con:
        assert con.execute("SELECT COUNT(*) FROM naklady").fetchone()[0] == 0
    assert _shared_plan_count(app_db, job) == 0


def test_pantry_worker_writes_only_the_user_scoped_plan(app_db):
    _grant_premium(app_db)
    with app_db.server.db() as con:
        con.execute("INSERT INTO spajza (user_id, nazov) VALUES (1, 'soľ')")
        con.commit()
    job = _queued_job(app_db, kind="pantry")
    rows = app_db.server.akcie_pre(STORES)
    output = _model_output([row["offer_key"] for row in rows[:4]])
    output["meals"][0]["pantry_ingredients"] = ["soľ"]
    model = FakeModel(output)

    result = process_one(client=model, now=NOW)

    assert result.status == "ready"
    assert _shared_plan_count(app_db, job) == 0
    with app_db.server.db() as con:
        stored = json.loads(
            con.execute("SELECT json FROM plany WHERE user_id=1 AND tyzden=?", (job.week,)).fetchone()[0]
        )
    assert stored["_uvarsi_meta"]["pantry_signature"] == app_db.server.podpis_spajze(["soľ"])


def test_week_change_while_queued_fails_before_dispatch(app_db):
    job = _queued_regular_job(app_db)
    model = FakeModel({})

    result = process_one(client=model, now=NOW + datetime.timedelta(days=7))

    assert (result.status, result.error_code) == ("failed", "stale_week")
    assert model.calls == 0
    assert _job_row(app_db, job.id)["dispatched_at"] is None


def test_algorithm_change_while_queued_fails_before_dispatch(app_db, monkeypatch):
    job = _queued_regular_job(app_db)
    monkeypatch.setattr(app_db.server, "PLAN_ALGO_VERSION", app_db.server.PLAN_ALGO_VERSION + 1)
    model = FakeModel({})

    result = process_one(client=model, now=NOW)

    assert (result.status, result.error_code) == ("failed", "stale_algorithm")
    assert model.calls == 0
    assert _job_row(app_db, job.id)["dispatched_at"] is None


def test_invalid_persisted_profile_fails_before_dispatch(app_db):
    job = _queued_job(app_db, payload_changes={"frequency": 99})
    model = FakeModel({})

    result = process_one(client=model, now=NOW)

    assert (result.status, result.error_code) == ("failed", "invalid_profile")
    assert model.calls == 0
    assert _job_row(app_db, job.id)["dispatched_at"] is None


def test_one_missing_selected_store_fails_before_dispatch(app_db):
    job = _queued_regular_job(app_db)
    with app_db.server.db() as con:
        con.execute(
            "UPDATE zber_stav SET stav='fail' WHERE tyzden=? AND obchod='Tesco'",
            (job.week,),
        )
        con.commit()
    model = FakeModel({})

    result = process_one(client=model, now=NOW)

    assert (result.status, result.error_code) == ("failed", "incomplete_stores")
    assert model.calls == 0
    assert _job_row(app_db, job.id)["dispatched_at"] is None


@pytest.mark.parametrize(
    ("mutation", "error_code"),
    (("collection", "incomplete_stores"),
     ("offers", "stale_signature"),
     ("pantry", "stale_pantry")),
)
def test_mutable_input_change_after_context_build_blocks_dispatch(
        app_db, monkeypatch, mutation, error_code):
    job, output = _mutable_job(app_db, mutation)
    model = FakeModel(output)

    def mutate_during_client_setup():
        _mutate_current_input(app_db, mutation)
        return model

    monkeypatch.setattr(
        app_db.server, "_new_plan_model_client", mutate_during_client_setup,
    )

    result = process_one(now=NOW)

    assert (result.status, result.error_code) == ("failed", error_code)
    assert model.calls == 0
    row = _job_row(app_db, job.id)
    assert row["state"] == "failed" and row["dispatched_at"] is None
    assert _shared_plan_count(app_db, job) == 0
    with app_db.server.db() as con:
        assert con.execute("SELECT COUNT(*) FROM plany").fetchone()[0] == 0
        assert con.execute("SELECT COUNT(*) FROM naklady").fetchone()[0] == 0


@pytest.mark.parametrize(
    ("mutation", "error_code"),
    (("collection", "incomplete_stores"),
     ("offers", "stale_signature"),
     ("pantry", "stale_pantry")),
)
def test_mutable_input_change_after_model_return_blocks_publication(
        app_db, mutation, error_code):
    job, output = _mutable_job(app_db, mutation)

    class MutatingModel(FakeModel):
        def create(self, **kwargs):
            response = super().create(**kwargs)
            _mutate_current_input(app_db, mutation)
            return response

    model = MutatingModel(output)

    result = process_one(client=model, now=NOW)

    assert (result.status, result.error_code) == ("failed", error_code)
    assert model.calls == 1
    row = _job_row(app_db, job.id)
    assert row["state"] == "failed" and row["dispatched_at"] is not None
    assert _shared_plan_count(app_db, job) == 0
    with app_db.server.db() as con:
        assert con.execute("SELECT COUNT(*) FROM plany").fetchone()[0] == 0
        assert con.execute("SELECT COUNT(*) FROM naklady").fetchone()[0] == 1


def test_dispatch_cas_failure_never_calls_the_model(app_db, monkeypatch):
    job = _queued_regular_job(app_db)
    monkeypatch.setattr(plan_worker.plan_jobs, "mark_dispatched", lambda *args, **kwargs: False)
    model = FakeModel({})

    result = process_one(client=model, now=NOW)

    assert (result.status, result.error_code) == (
        "failed", "lease_lost_before_dispatch",
    )
    assert model.calls == 0
    assert _job_row(app_db, job.id)["dispatched_at"] is None
    with app_db.server.db() as con:
        assert con.execute("SELECT COUNT(*) FROM naklady").fetchone()[0] == 0


def test_heartbeat_runs_during_blocking_model_call_and_stops_in_finally(
        app_db, monkeypatch):
    job = _queued_regular_job(app_db)
    rows = app_db.server.akcie_pre(STORES)

    class BlockingModel(FakeModel):
        def create(self, **kwargs):
            deadline = time.monotonic() + 1.0
            while time.monotonic() < deadline:
                con = sqlite3.connect(app_db.path)
                try:
                    self.observed_worker = con.execute(
                        "SELECT worker_id, job_id FROM plan_worker_state"
                    ).fetchone()
                finally:
                    con.close()
                if self.observed_worker is not None:
                    break
                time.sleep(0.005)
            else:
                raise AssertionError("heartbeat thread did not report the running job")
            return super().create(**kwargs)

    monkeypatch.setattr(plan_worker, "HEARTBEAT_SECONDS", 0.01)
    model = BlockingModel(_model_output([row["offer_key"] for row in rows[:4]]))

    result = process_one(client=model, now=NOW)

    assert result.status == "ready"
    with app_db.server.db() as con:
        heartbeat = con.execute(
            "SELECT worker_id, job_id, heartbeat_at FROM plan_worker_state"
        ).fetchone()
    assert tuple(model.observed_worker) == (plan_worker.WORKER_ID, job.id)
    assert tuple(heartbeat[:2]) == (plan_worker.WORKER_ID, None)
    assert heartbeat["heartbeat_at"] is not None
    assert not any(
        thread.name == f"plan-heartbeat-{job.id}" for thread in threading.enumerate()
    )


def test_lease_lost_after_dispatch_never_publishes_or_retries(app_db):
    job = _queued_regular_job(app_db)
    rows = app_db.server.akcie_pre(STORES)

    class LeaseLosingModel(FakeModel):
        def create(self, **kwargs):
            response = super().create(**kwargs)
            con = sqlite3.connect(app_db.path)
            try:
                con.execute(
                    """UPDATE plan_jobs SET state='failed', finished_at=?, updated_at=?,
                              lease_owner=NULL, lease_expires_at=NULL,
                              error_code='worker_lost_after_dispatch'
                       WHERE id=?""",
                    (NOW.isoformat(timespec="seconds"), NOW.isoformat(timespec="seconds"), job.id),
                )
                con.commit()
            finally:
                con.close()
            return response

    model = LeaseLosingModel(_model_output([row["offer_key"] for row in rows[:4]]))

    first = process_one(client=model, now=NOW)
    second = process_one(client=model, now=NOW + datetime.timedelta(minutes=3))

    assert first.error_code == "worker_lost_after_dispatch"
    assert second.status == "empty"
    assert model.calls == 1
    assert _shared_plan_count(app_db, job) == 0


def test_two_expired_pre_dispatch_claims_become_terminal_before_a_third_call(app_db):
    job = _queued_regular_job(app_db)
    with app_db.server.db() as con:
        first = plan_jobs.claim_next(con, "crashed-worker-1", now=NOW, lease_seconds=1)
        second = plan_jobs.claim_next(
            con, "crashed-worker-2", now=NOW + datetime.timedelta(seconds=2), lease_seconds=1,
        )
    assert first.attempts == 1 and second.attempts == 2
    model = FakeModel(_model_output([
        row["offer_key"] for row in app_db.server.akcie_pre(STORES)[:4]
    ]))

    result = process_one(client=model, now=NOW + datetime.timedelta(seconds=4))

    assert result.status == "empty"
    assert model.calls == 0
    row = _job_row(app_db, job.id)
    assert (row["state"], row["attempts"], row["error_code"]) == (
        "failed", 2, "worker_lost_before_dispatch",
    )


def test_ready_cas_failure_rolls_back_the_plan_publish(app_db, monkeypatch):
    job = _queued_regular_job(app_db)
    rows = app_db.server.akcie_pre(STORES)
    model = FakeModel(_model_output([row["offer_key"] for row in rows[:4]]))
    monkeypatch.setattr(plan_worker.plan_jobs, "mark_ready", lambda *args, **kwargs: False)

    result = process_one(client=model, now=NOW)

    assert result.error_code == "worker_lost_after_dispatch"
    assert model.calls == 1
    assert _shared_plan_count(app_db, job) == 0


def test_precompute_writes_only_the_shared_cache(app_db):
    job = _queued_job(app_db, kind="precompute", priority=20)
    rows = app_db.server.akcie_pre(STORES)
    model = FakeModel(_model_output([row["offer_key"] for row in rows[:4]]))

    result = process_one(client=model, now=NOW)

    assert result.status == "ready"
    assert _shared_plan_count(app_db, job) == 1
    with app_db.server.db() as con:
        assert con.execute("SELECT COUNT(*) FROM plany").fetchone()[0] == 0
        assert con.execute(
            "SELECT predpocitany FROM plany_zdielane WHERE podpis=? AND variant=?",
            (job.signature, job.variant),
        ).fetchone()[0] == 1


def test_run_forever_waits_before_a_pre_dispatch_retry(monkeypatch):
    class StopLoop(Exception):
        pass

    calls = []

    def process_once():
        calls.append("process")
        if len(calls) > 1:
            raise AssertionError("worker retried without waiting")
        return ProcessResult(1, "queued", "client_setup")

    monkeypatch.setattr(
        plan_worker,
        "process_one",
        process_once,
    )
    monkeypatch.setattr(
        plan_worker.time,
        "sleep",
        lambda seconds: (_ for _ in ()).throw(StopLoop(seconds)),
    )

    with pytest.raises(StopLoop) as stopped:
        plan_worker.run_forever(poll_seconds=0.25)

    assert calls == ["process"]
    assert stopped.value.args == (0.25,)


def test_worker_separates_utc_queue_clock_from_bratislava_business_calendar():
    instant = datetime.datetime(2026, 8, 30, 22, 30, tzinfo=datetime.timezone.utc)
    guarded = plan_worker._LeaseAwareClient(
        server=None,
        job=types.SimpleNamespace(id=1),
        client=None,
        clock=lambda: instant,
        calendar_clock=lambda: plan_calendar.bratislava_day(instant),
    )

    assert guarded._clock() == instant
    assert guarded.job_now == datetime.date(2026, 8, 31)


def test_worker_utcnow_returns_an_aware_utc_instant():
    assert plan_worker.utcnow().tzinfo is datetime.timezone.utc
