"""Priepustnosť: WAL, migrácie raz pri štarte a strop na súbežné skladanie.

Audit 24. 8. 2026 našiel tri chyby, ktoré sa prejavia až pod záťažou — a vtedy
zhodia CELÚ appku, nielen generovanie plánu:

  1. SQLite bežalo v rollback-journal režime. Tam jeden zapisovateľ blokuje
     VŠETKÝCH čitateľov, takže súbežné požiadavky čakali celých 20 sekúnd
     timeoutu a potom padli na `database is locked` — vrátane prihlásenia.
  2. `db()` na KAŽDEJ požiadavke prehnal celú schému (`executescript`) plus
     štyri migračné funkcie, a `generuj_plan` volá `db()` päť- až šeťkrát.
  3. `generuj_plan` je synchronné `def`, teda drží vlákno z anyio poolu (default
     40) až 120 sekúnd. 41. návštevník čakal — a ten istý pool obsluhuje aj
     `/api/me` a prihlásenie, takže zamrzlo všetko.

Tieto testy držia opravu. Každý z nich na pôvodnom kóde padne.
"""
import asyncio
import importlib
import sqlite3
import sys
import threading
from contextlib import closing
from pathlib import Path

import anyio
import pytest
from fastapi.testclient import TestClient

from test_server import (
    current_plan_rows,
    fake_anthropic,
    grant_premium,
    insert_hashed_session,
    load_server,
    model_plan,
    model_plan_without_pantry,
    plan_client,
)


ROOT = Path(__file__).resolve().parents[1]


def plan_server(monkeypatch, tmp_path, user_id=1, premium=False):
    """Server s jedným účtom, ktorý si smie dať poskladať jedálniček."""
    server = load_server(monkeypatch, tmp_path, current_plan_rows())
    with closing(server.db()) as con:
        con.execute(
            "INSERT INTO pouzivatelia (id, email, osoby, frekvencia, obchody)"
            " VALUES (?, ?, 4, 2, 'Lidl')", (user_id, f"user{user_id}@uvar.si"),
        )
        insert_hashed_session(server, con, f"session-{user_id}", user_id)
        con.commit()
    if premium:
        grant_premium(server, user_id)
    return server


def load_zbierac(monkeypatch, tmp_path):
    monkeypatch.setenv("UVARSI_DB", str(tmp_path / "zbierac.db"))
    monkeypatch.syspath_prepend(str(ROOT / "app"))
    sys.modules.pop("zbierac_akcii", None)
    return importlib.import_module("zbierac_akcii")


def rezim(con) -> str:
    return con.execute("PRAGMA journal_mode").fetchone()[0].lower()


# ------------------------------------------------------------------- 1. WAL
def test_db_connection_runs_in_wal_mode(monkeypatch, tmp_path):
    """Bez WAL zapisovateľ blokuje čitateľov a pod záťažou padne aj login."""
    server = plan_server(monkeypatch, tmp_path)
    with closing(server.db()) as con:
        assert rezim(con) == "wal", (
            "SQLite musí bežať vo WAL; v rollback-journal režime jeden zapisovateľ "
            "blokuje všetkých čitateľov a požiadavky padajú na `database is locked`"
        )


def test_wal_persists_for_a_connection_that_never_issued_the_pragma(monkeypatch, tmp_path):
    """WAL je vlastnosť databázy — musí platiť aj pre cudzie spojenie."""
    server = plan_server(monkeypatch, tmp_path)
    with closing(server.db()):
        pass
    with closing(sqlite3.connect(server.DB)) as cudzie:
        assert rezim(cudzie) == "wal"


def test_db_connection_uses_normal_synchronous(monkeypatch, tmp_path):
    """S WAL je synchronous=NORMAL bezpečný a ušetrí fsync na každom commite."""
    server = plan_server(monkeypatch, tmp_path)
    with closing(server.db()) as con:
        assert con.execute("PRAGMA synchronous").fetchone()[0] == 1


def test_a_reader_is_not_blocked_by_an_open_writer(monkeypatch, tmp_path):
    """Presne tá situácia, ktorá appku zhodila: zápis beží, čítanie musí prejsť."""
    server = plan_server(monkeypatch, tmp_path)
    with closing(server.db()) as zapisovatel:
        zapisovatel.execute("BEGIN IMMEDIATE")
        zapisovatel.execute(
            "INSERT INTO spajza (user_id, nazov) VALUES (1, 'soľ')")
        with closing(server.db()) as citatel:
            citatel.execute("PRAGMA busy_timeout=200")
            assert citatel.execute(
                "SELECT COUNT(*) FROM pouzivatelia").fetchone()[0] == 1
        zapisovatel.rollback()


def test_collector_connection_runs_in_wal_with_a_long_timeout(monkeypatch, tmp_path):
    """zbierac_akcii.db() otváralo spojenie bez timeoutu — teda default 5 s."""
    zbierac = load_zbierac(monkeypatch, tmp_path)
    zachytene = {}
    povodny = sqlite3.connect

    def spy(*args, **kwargs):
        zachytene.update(kwargs)
        return povodny(*args, **kwargs)

    monkeypatch.setattr(zbierac.sqlite3, "connect", spy)
    with closing(zbierac.db()) as con:
        assert rezim(con) == "wal"
    assert zachytene.get("timeout", 5) >= 20, (
        "zberač otváral databázu bez timeoutu (default 5 s) — pri súbežnom "
        "zápise sa vzdá skôr, než appka stihne dokončiť transakciu"
    )


# ------------------------------------------- 2. migrácie raz, nie na požiadavku
def test_startup_migrates_the_schema_exactly_once(monkeypatch, tmp_path):
    server = plan_server(monkeypatch, tmp_path)
    behy = []
    povodne = server.migruj_schemu
    monkeypatch.setattr(
        server, "migruj_schemu",
        lambda con: (behy.append(1), povodne(con))[1],
    )
    server._SCHEMA_HOTOVA.clear()

    with TestClient(server.app) as client:
        assert behy == [1], "štart appky musí schému zmigrovať práve raz"
        for _ in range(5):
            assert client.get("/api/akcie/pocet").status_code == 200

    assert behy == [1], (
        f"schéma sa prehnala {len(behy)}× — migrácie nesmú bežať na každej "
        "požiadavke, `db()` má byť lacné pripojenie"
    )


def test_db_does_not_touch_migrations_after_startup(monkeypatch, tmp_path):
    """`db()` sa volá 5–6× za jedno generovanie plánu; musí byť lacné."""
    server = plan_server(monkeypatch, tmp_path)
    with TestClient(server.app):
        pass

    def vybuchni(con):
        raise AssertionError("db() po štarte nesmie spúšťať migrácie")

    monkeypatch.setattr(server, "migruj_schemu", vybuchni)
    for _ in range(10):
        with closing(server.db()) as con:
            con.execute("SELECT 1").fetchone()


def test_helpers_still_work_standalone_without_the_app(monkeypatch, tmp_path):
    """CLI a zberače volajú db() bez toho, aby ktokoľvek spustil lifespan."""
    database = tmp_path / "samostatna.db"
    monkeypatch.setenv("UVARSI_DB", str(database))
    monkeypatch.setenv("UVARSI_URL", "https://uvar.si")
    monkeypatch.setenv("UVARSI_VERSION_FILE", str(ROOT / "VERSION"))
    monkeypatch.syspath_prepend(str(ROOT / "app"))
    sys.modules.pop("server", None)
    server = importlib.import_module("server")

    with closing(server.db()) as con:
        tabulky = {
            row[0] for row in
            con.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    assert {"pouzivatelia", "akcie", "naroky"} <= tabulky, (
        "bez bežiacej appky musí prvé db() schému dotvoriť samo, inak sa "
        "premium_cli.py ani zberač nespustia"
    )


# --------------------------------------------- 3. strop na súbežné skladanie
def test_plan_concurrency_bound_is_below_the_thread_pool(monkeypatch, tmp_path):
    server = plan_server(monkeypatch, tmp_path)
    assert server.PLAN_SUBEZNE_MAX < server.PLAN_VLAKNA_STROP, (
        "strop na skladanie plánov musí byť nižší než počet vlákien, inak "
        "plány zjedia celý pool a zamrzne aj prihlásenie"
    )
    assert server.PLAN_VLAKNA_STROP > 40, (
        "anyio default je 40 vlákien; s 120-sekundovým plánom to nestačí"
    )


def test_thread_pool_limit_is_actually_raised(monkeypatch, tmp_path):
    server = plan_server(monkeypatch, tmp_path)

    async def zmeraj():
        server.zvys_strop_vlakien()
        return anyio.to_thread.current_default_thread_limiter().total_tokens

    assert asyncio.run(zmeraj()) >= server.PLAN_VLAKNA_STROP


def test_startup_raises_the_thread_pool_limit(monkeypatch, tmp_path):
    server = plan_server(monkeypatch, tmp_path)
    volane = []
    monkeypatch.setattr(server, "zvys_strop_vlakien", lambda: volane.append(1))
    with TestClient(server.app):
        pass
    assert volane == [1], "lifespan musí strop vlákien nastaviť pri štarte"


def test_busy_server_turns_the_plan_away_in_slovak(monkeypatch, tmp_path):
    server = plan_server(monkeypatch, tmp_path)
    volania = []
    monkeypatch.setitem(sys.modules, "anthropic",
                        fake_anthropic(model_plan(), [], volania))
    plno = threading.BoundedSemaphore(1)
    assert plno.acquire(blocking=False)
    monkeypatch.setattr(server, "PLAN_MIESTA", plno)

    odpoved = plan_client(server, 1).post("/api/plan/generuj")

    assert odpoved.status_code == 503, (
        "keď je plno, appka to musí povedať hneď — nie ticho visieť na vlákne"
    )
    telo = odpoved.json()
    assert "jedálničkov" in telo["detail"] and "minút" in telo["detail"], (
        f"hláška musí byť po slovensky a konkrétna: {telo['detail']!r}"
    )
    assert telo["kod"] == server.KOD_PLAN_ZANEPRAZDNENY, (
        "appka sa nesmie rozhodovať podľa textu, ktorý raz preformulujeme"
    )
    assert volania == [], "odmietnutý plán nesmie zavolať (a zaplatiť) model"


def test_turned_away_user_keeps_the_daily_quota(monkeypatch, tmp_path):
    """Kto plán nedostal, nesmie prísť o dnešný prepočet."""
    server = plan_server(monkeypatch, tmp_path)
    monkeypatch.setitem(sys.modules, "anthropic",
                        fake_anthropic(model_plan_without_pantry(), [], []))
    plno = threading.BoundedSemaphore(1)
    assert plno.acquire(blocking=False)
    monkeypatch.setattr(server, "PLAN_MIESTA", plno)

    assert plan_client(server, 1).post("/api/plan/generuj").status_code == 503

    with closing(server.db()) as con:
        assert server.pouzite_prepocty(con, 1, server.dnesok()) == 0, (
            "odmietnutie kvôli záťaži nie je pokus používateľa — strop sa "
            "nesmie znížiť"
        )

    plno.release()
    monkeypatch.setattr(server, "PLAN_MIESTA", threading.BoundedSemaphore(2))
    assert plan_client(server, 1).post("/api/plan/generuj").status_code == 200, (
        "prepočet musel ostať nedotknutý, takže hneď ďalší pokus prejde"
    )


def test_plan_slot_is_released_after_a_successful_generation(monkeypatch, tmp_path):
    server = plan_server(monkeypatch, tmp_path)
    monkeypatch.setitem(sys.modules, "anthropic",
                        fake_anthropic(model_plan_without_pantry(), [], []))
    miesta = threading.BoundedSemaphore(2)
    monkeypatch.setattr(server, "PLAN_MIESTA", miesta)

    assert plan_client(server, 1).post("/api/plan/generuj").status_code == 200

    for _ in range(2):
        assert miesta.acquire(blocking=False), (
            "úspešné skladanie musí miesto vrátiť, inak sa strop postupne "
            "vyčerpá a appka sa zamkne sama"
        )


def test_plan_slot_is_released_when_generation_fails(monkeypatch, tmp_path):
    server = plan_server(monkeypatch, tmp_path)
    miesta = threading.BoundedSemaphore(1)
    monkeypatch.setattr(server, "PLAN_MIESTA", miesta)

    def vybuchni(*args, **kwargs):
        raise RuntimeError("model spadol")

    monkeypatch.setattr(server, "poskladaj_novy_plan", vybuchni)
    client = TestClient(server.app, raise_server_exceptions=False)
    client.cookies.set(server.COOKIE, "session-1")

    assert client.post("/api/plan/generuj").status_code == 500

    assert miesta.acquire(blocking=False), (
        "aj neúspešné skladanie musí miesto vrátiť"
    )
