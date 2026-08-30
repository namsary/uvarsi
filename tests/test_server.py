import importlib
import hashlib
import json
import sqlite3
import sys
import threading
import time
import types
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.landing_data import write_landing_data_atomic
from app.offer_data import migrate_akcie_schema, offer_key_for
from app.plan_data import PORTION_STANDARD_VERSION, build_personal_plan
from app.weekly_data import current_monday


ROOT = Path(__file__).resolve().parents[1]


def test_daily_limit_uses_bratislava_day_after_local_midnight(monkeypatch, tmp_path):
    server = load_server(monkeypatch, tmp_path, [])
    instant = datetime(2026, 8, 28, 22, 30, tzinfo=timezone.utc)

    assert server.dnesok(instant) == "2026-08-29"


def load_server(monkeypatch, tmp_path, rows, landing_data=None):
    database = tmp_path / "uvarsi.db"
    con = sqlite3.connect(database)
    con.execute(
        """CREATE TABLE akcie (
            tyzden TEXT, nazov TEXT, obchod TEXT, cena REAL, povodna REAL,
            zlava TEXT, jednotka TEXT, kategoria TEXT, source_url TEXT,
            source_page INTEGER, valid_from TEXT, valid_to TEXT
        )"""
    )
    con.executemany(
        """INSERT INTO akcie
           (tyzden, nazov, obchod, cena, povodna, zlava, jednotka, kategoria,
            source_url, source_page, valid_from, valid_to)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        [tuple(row) + (None,) * (12 - len(row)) for row in rows],
    )
    migrate_akcie_schema(con)
    con.execute(
        """CREATE TABLE zber_stav (
            tyzden TEXT NOT NULL, obchod TEXT NOT NULL, stav TEXT NOT NULL,
            pocet INTEGER NOT NULL DEFAULT 0, detail TEXT, updated TEXT,
            PRIMARY KEY (tyzden, obchod)
        )"""
    )
    stores = sorted({row[2] for row in rows})
    con.executemany(
        "INSERT INTO zber_stav (tyzden, obchod, stav, pocet) VALUES (?, ?, 'ok', ?)",
        [
            (current_monday(), store, sum(1 for row in rows if row[2] == store))
            for store in stores
        ],
    )
    con.row_factory = sqlite3.Row
    for row in con.execute("SELECT rowid, * FROM akcie").fetchall():
        offer = dict(row)
        try:
            key = offer_key_for(offer["tyzden"], offer)
        except ValueError:
            continue
        con.execute("UPDATE akcie SET offer_key=? WHERE rowid=?", (key, row[0]))
    con.commit()
    con.close()

    monkeypatch.setenv("UVARSI_DB", str(database))
    monkeypatch.setenv("UVARSI_URL", "https://uvar.si")
    if landing_data is not None:
        landing_path = tmp_path / "landing_data.json"
        write_landing_data_atomic(landing_path, landing_data)
        monkeypatch.setenv("UVARSI_LANDING_DATA", str(landing_path))
    monkeypatch.syspath_prepend(str(ROOT / "app"))
    sys.modules.pop("server", None)
    return importlib.import_module("server")


def load_server_with_landing_path(monkeypatch, tmp_path, rows, landing_path):
    database = tmp_path / "uvarsi.db"
    con = sqlite3.connect(database)
    con.execute(
        """CREATE TABLE akcie (
            tyzden TEXT, nazov TEXT, obchod TEXT, cena REAL, povodna REAL,
            zlava TEXT, jednotka TEXT, kategoria TEXT, source_url TEXT,
            source_page INTEGER, valid_from TEXT, valid_to TEXT
        )"""
    )
    con.executemany(
        """INSERT INTO akcie
           (tyzden, nazov, obchod, cena, povodna, zlava, jednotka, kategoria,
            source_url, source_page, valid_from, valid_to)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        [tuple(row) + (None,) * (12 - len(row)) for row in rows],
    )
    migrate_akcie_schema(con)
    con.execute(
        """CREATE TABLE zber_stav (
            tyzden TEXT NOT NULL, obchod TEXT NOT NULL, stav TEXT NOT NULL,
            pocet INTEGER NOT NULL DEFAULT 0, detail TEXT, updated TEXT,
            PRIMARY KEY (tyzden, obchod)
        )"""
    )
    stores = sorted({row[2] for row in rows})
    con.executemany(
        "INSERT INTO zber_stav (tyzden, obchod, stav, pocet) VALUES (?, ?, 'ok', ?)",
        [
            (current_monday(), store, sum(1 for row in rows if row[2] == store))
            for store in stores
        ],
    )
    con.row_factory = sqlite3.Row
    for row in con.execute("SELECT rowid, * FROM akcie").fetchall():
        offer = dict(row)
        try:
            key = offer_key_for(offer["tyzden"], offer)
        except ValueError:
            continue
        con.execute("UPDATE akcie SET offer_key=? WHERE rowid=?", (key, row[0]))
    con.commit()
    con.close()

    monkeypatch.setenv("UVARSI_DB", str(database))
    monkeypatch.setenv("UVARSI_URL", "https://uvar.si")
    monkeypatch.setenv("UVARSI_LANDING_DATA", str(landing_path))
    monkeypatch.syspath_prepend(str(ROOT / "app"))
    sys.modules.pop("server", None)
    return importlib.import_module("server")


def test_auth_v3_public_capability_is_boolean_and_primary_routes_are_server_gated(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("UVARSI_AUTH_V3", "0")
    server = load_server(monkeypatch, tmp_path, [])
    client = TestClient(server.app, base_url="https://uvar.si")

    disabled_me = client.get("/api/me")
    disabled_routes = [
        client.post(
            path,
            headers={"Origin": "https://uvar.si"},
            content="{",
            follow_redirects=path != "/api/auth/login/",
        )
        for path in (
            "/api/auth/register",
            "/api/auth/confirm",
            "/api/auth/login",
            "/api/auth/login/",
            "/api/auth/password/request",
            "/api/auth/password/reset",
            "/api/auth/password/change",
            "/api/auth/sessions/logout-others",
            "/api/auth/passkey/login/options",
        )
    ]
    disabled_routes.extend(
        [
            client.get("/api/auth/sessions"),
            client.delete("/api/auth/sessions/not-a-session"),
            client.get("/api/auth/passkeys"),
        ]
    )

    assert disabled_me.json() == {"prihlaseny": False}
    assert [response.status_code for response in disabled_routes] == [404] * len(
        disabled_routes
    )

    monkeypatch.setenv("UVARSI_AUTH_V3", "1")
    enabled_me = client.get("/api/me")
    enabled_login = client.post(
        "/api/auth/login", headers={"Origin": "https://uvar.si"}, content="{"
    )
    enabled_passkey = client.post(
        "/api/auth/passkey/login/options",
        headers={"Origin": "https://uvar.si"},
        json={"email": "not-an-email"},
    )

    assert enabled_me.json() == {"prihlaseny": False, "auth_v3": True}
    assert all(type(value) is bool for value in enabled_me.json().values())
    assert enabled_login.status_code == 400
    assert enabled_passkey.status_code == 400


def landing_payload(week=None):
    today = date.today()
    return {
        "schema_version": 1,
        "generated_at": "2026-08-18T05:02:20+02:00",
        "week": week or current_monday(),
        "week_label": "17.–23. 8. 2026",
        "sources": [{
            "store": "Lidl", "url": "https://letak.test/lidl",
            "valid_from": current_monday(today),
            "valid_to": (today + timedelta(days=1)).isoformat(),
        }],
        "receipt": {
            "meals": [{"day": "PO", "name": "Test", "items": []}],
            "nakup_spolu": "1,00",
            "bezne": "2,00",
            "usetris": "1,00",
        },
    }


def current_plan_rows(count=16):
    return [tuple(plan_offer(index).get(field) for field in (
        "tyzden", "nazov", "obchod", "cena", "povodna", "zlava", "jednotka", "kategoria",
        "source_url", "source_page", "valid_from", "valid_to",
    )) for index in range(1, count + 1)]


def plan_offer(index):
    today = date.today()
    return {
        "tyzden": current_monday(today), "nazov": f"Ponuka {index}", "obchod": "Lidl",
        "cena": 1.0 + index / 100, "povodna": 2.0 + index / 100, "zlava": "-50 %",
        "jednotka": "1 kg", "kategoria": "trvanlive",
        "source_url": f"https://example.test/{index}.jpg", "source_page": index,
        "valid_from": (today - timedelta(days=1)).isoformat(),
        "valid_to": (today + timedelta(days=1)).isoformat(),
    }


def plan_key(index):
    offer = plan_offer(index)
    return offer_key_for(offer["tyzden"], offer)


# Plán prejde overením len s krokmi, podľa ktorých sa dá naozaj variť: tvar
# rezu, teplota, čas, ako to má vyzerať a na konci jedlo na tanieri.
COOKABLE_STEPS = [
    "Na 2 lyžiciach oleja opeč 2 cibule nakrájané na kocky 5 minút do sklovita.",
    "Prilej 200 ml vody, osoľ štipkou soli a duste 15 minút pod pokrievkou.",
    "Na miernom ohni prevar polievku ešte 3 minúty a rozdeľ na 4 hlboké taniere.",
]
COOKABLE_NAME = "Dusená cibuľová polievka"

DIVERSE_COOKABLE_RECIPES = [
    (
        "Kuracie s ryžou",
        [
            "V hrnci zohrej 2 lyžice oleja na strednom ohni 2 minúty, kým sa rozvonia.",
            "Pridaj 400 g kuracieho mäsa nakrájaného na kocky a var 8 minút, kým nezhnedne.",
            "Vsyp 300 g ryže, prilej 600 ml vody a var 15 minút na miernom ohni, kým ryža nezmäkne.",
            "Kuracie s ryžou rozdeľ na 4 taniere a podávaj horúce.",
        ],
    ),
    (
        "Bravčové s cestovinami",
        [
            "Na panvici zohrej 2 lyžice oleja na strednom ohni 2 minúty, kým sa rozvonia.",
            "Pridaj 400 g bravčového mäsa nakrájaného na pásiky a opekaj 10 minút do zlatista.",
            "Vmiešaj 300 g cestovín a 200 ml vody, prehrievaj 5 minút, kým omáčka nezhustne.",
            "Bravčové s cestovinami rozdeľ na 4 taniere a podávaj horúce.",
        ],
    ),
    (
        "Losos so zemiakmi",
        [
            "Rúru predhrej na 200 °C a plech potri 2 lyžicami oleja.",
            "Na plech polož 400 g lososa a 300 g zemiakov nakrájaných na kolieska.",
            "Peč 25 minút, kým losos nie je šťavnatý a zemiaky nie sú dozlata.",
            "Lososa so zemiakmi rozdeľ na 4 taniere a podávaj horúce.",
        ],
    ),
]


def model_plan(first_offer_key=None, pantry=None):
    """Odpoveď modelu. Bez špajze — bežný plán sa skladá len z ponúk.

    Špajza vstupuje do skladania výhradne pri výslovnom „navrhni jedlá z toho,
    čo mám doma"; na to slúži parameter `pantry`.
    """
    first_offer_key = first_offer_key or plan_key(1)
    days = ("PO", "ST", "PI", "NE")
    keys = (first_offer_key, plan_key(2), plan_key(3), plan_key(4))
    return {"meals": [
        {
            "day": day,
            "name": DIVERSE_COOKABLE_RECIPES[index % 3][0],
            "minutes": 30 + index * 5,
            "instructions": DIVERSE_COOKABLE_RECIPES[index % 3][1],
            "items": [{"offer_key": keys[index], "quantity": 2 if index == 0 else 1,
                       "amount_per_person": 150, "unit": "g"}],
            **({"pantry_ingredients": list(pantry or [])} if index == 0 else {}),
        }
        for index, day in enumerate(days)
    ]}


def repetitive_model_plan():
    plan = model_plan()
    for meal in plan["meals"]:
        meal["name"] = COOKABLE_NAME
        meal["instructions"] = COOKABLE_STEPS
    return plan


def fake_anthropic(model_output, constructors, message_calls=None, stop_reason="end_turn", usage=None):
    class Messages:
        def create(self, **kwargs):
            if message_calls is not None:
                message_calls.append(kwargs)
            return types.SimpleNamespace(
                content=[types.SimpleNamespace(type="text", text=json.dumps(model_output))],
                stop_reason=stop_reason,
                usage=usage,
            )

    class Anthropic:
        def __init__(self, **kwargs):
            constructors.append(kwargs)
            self.messages = Messages()

    return types.SimpleNamespace(Anthropic=Anthropic)


def prompt_text(call):
    """Every text block actually sent to the model, joined."""
    content = call["messages"][0]["content"]
    if isinstance(content, str):
        return content
    return "\n".join(block["text"] for block in content)


def insert_hashed_session(server, con, raw_token, user_id):
    now = server.AUTH_CLOCK()
    con.execute(
        """INSERT INTO sessions_v2 (token_hash, user_id, expires_at, created_at)
           VALUES (?, ?, ?, ?)""",
        (hashlib.sha256(raw_token.encode()).hexdigest(), user_id, now + 30 * 24 * 60 * 60, now),
    )


def grant_premium(server, user_id, order_id=None):
    """Aktívny nárok v `naroky` — jediný dôkaz o Premium, ktorý server uzná.

    Špajza aj vyšší denný strop prepočtov visia na tomto zázname, nie na
    stĺpci `platiaci` a už vôbec nie na tom, čo tvrdí klient.
    """
    with server.db() as con:
        con.execute(
            """INSERT INTO naroky (user_id, produkt, poskytovatel, objednavka_id,
                                   suma_centy, mena, stav, ziskany_o, zmeneny_o)
               VALUES (?, 'zakladajuci_clen', 'lemonsqueezy', ?, 1900, 'EUR', 'aktivny', 1, 1)""",
            (user_id, order_id or f"ord-{user_id}"),
        )
        con.commit()


def test_akcie_pre_never_returns_previous_week_prices(monkeypatch, tmp_path):
    server = load_server(
        monkeypatch,
        tmp_path,
        [("2026-08-10", "Mlieko", "Lidl", 1.0, 1.5, "-33 %", "1 l", "mliecne")],
    )
    monkeypatch.setattr(server, "monday", lambda: "2026-08-17")

    assert server.akcie_pre(["Lidl"]) == []


def test_akcie_pre_delegates_selection_to_current_week_helper(monkeypatch, tmp_path):
    server = load_server(monkeypatch, tmp_path, [])
    calls = []

    def current_week_only(connection, stores, today):
        calls.append((connection, stores, today))
        return []

    monkeypatch.setattr(server, "offers_for_current_week", current_week_only, raising=False)
    server.akcie_pre(["Lidl"])

    assert calls[0][1] == ["Lidl"]
    assert calls[0][2] == date.today()


def test_plan_is_503_when_only_previous_week_exists(monkeypatch, tmp_path):
    server = load_server(
        monkeypatch,
        tmp_path,
        [("2026-08-10", "Mlieko", "Lidl", 1.0, 1.5, "-33 %", "1 l", "mliecne")],
    )
    with server.db() as con:
        con.execute("INSERT INTO pouzivatelia (id, email, obchody) VALUES (1, 'test@uvar.si', 'Lidl')")
        insert_hashed_session(server, con, "session-token", 1)
        con.commit()

    constructors = []

    class ForbiddenAnthropic:
        def __init__(self, *args, **kwargs):
            constructors.append((args, kwargs))

    monkeypatch.setitem(sys.modules, "anthropic", types.SimpleNamespace(Anthropic=ForbiddenAnthropic))
    client = TestClient(server.app)
    client.cookies.set(server.COOKIE, "session-token")
    response = client.post("/api/plan/generuj?force=1")

    assert response.status_code == 503
    assert response.json()["detail"] == "Aktuálne letákové dáta sa obnovujú. Skús to o chvíľu."
    assert constructors == []


def test_plan_is_503_without_constructing_anthropic_when_current_week_offers_are_expired(monkeypatch, tmp_path):
    today = date.today()
    week = current_monday(today)
    server = load_server(
        monkeypatch,
        tmp_path,
        [(week, "Mlieko", "Lidl", 1.0, 1.5, "-33 %", "1 l", "mliecne",
          "https://example.test/lidl.jpg", 1, (today - timedelta(days=7)).isoformat(),
          (today - timedelta(days=1)).isoformat())],
    )
    with server.db() as con:
        con.execute("INSERT INTO pouzivatelia (id, email, obchody) VALUES (1, 'test@uvar.si', 'Lidl')")
        insert_hashed_session(server, con, "session-token", 1)
        con.commit()

    constructors = []

    class ForbiddenAnthropic:
        def __init__(self, *args, **kwargs):
            constructors.append((args, kwargs))

    monkeypatch.setitem(sys.modules, "anthropic", types.SimpleNamespace(Anthropic=ForbiddenAnthropic))
    client = TestClient(server.app)
    client.cookies.set(server.COOKIE, "session-token")

    response = client.post("/api/plan/generuj?force=1")

    assert response.status_code == 503
    assert constructors == []


@pytest.mark.parametrize("offer_kind", ["expired", "legacy"])
def test_cached_plan_is_503_when_current_week_has_no_verified_offers(monkeypatch, tmp_path, offer_kind):
    today = date.today()
    week = current_monday(today)
    if offer_kind == "expired":
        rows = [
            (week, "Mlieko", "Lidl", 1.0, 1.5, "-33 %", "1 l", "mliecne",
             "https://example.test/lidl.jpg", 1, (today - timedelta(days=7)).isoformat(),
             (today - timedelta(days=1)).isoformat()),
        ]
    else:
        rows = [(week, "Mlieko", "Lidl", 1.0, 1.5, "-33 %", "1 l", "mliecne")]

    server = load_server(monkeypatch, tmp_path, rows)
    with server.db() as con:
        con.execute("INSERT INTO pouzivatelia (id, email, obchody) VALUES (1, 'test@uvar.si', 'Lidl')")
        insert_hashed_session(server, con, "session-token", 1)
        con.execute(
            "INSERT INTO plany (user_id, tyzden, json) VALUES (?, ?, ?)",
            (1, week, '{"cached": true}'),
        )
        con.commit()

    constructors = []

    class ForbiddenAnthropic:
        def __init__(self, *args, **kwargs):
            constructors.append((args, kwargs))

    monkeypatch.setitem(sys.modules, "anthropic", types.SimpleNamespace(Anthropic=ForbiddenAnthropic))
    client = TestClient(server.app)
    client.cookies.set(server.COOKIE, "session-token")

    response = client.post("/api/plan/generuj")

    assert response.status_code == 503
    assert constructors == []


def test_offer_count_includes_only_current_verified_offers(monkeypatch, tmp_path):
    today = date.today()
    week = current_monday(today)
    rows = [
        (week, "Overené mlieko", "Lidl", 1.0, 1.5, "-33 %", "1 l", "mliecne",
         "https://example.test/current.jpg", 1, (today - timedelta(days=1)).isoformat(),
         (today + timedelta(days=1)).isoformat()),
        (week, "Expirované mlieko", "Lidl", 1.0, 1.5, "-33 %", "1 l", "mliecne",
         "https://example.test/expired.jpg", 2, (today - timedelta(days=8)).isoformat(),
         (today - timedelta(days=1)).isoformat()),
        (week, "Legacy mlieko", "Lidl", 1.0, 1.5, "-33 %", "1 l", "mliecne"),
    ]
    server = load_server(monkeypatch, tmp_path, rows)

    response = TestClient(server.app).get("/api/akcie/pocet")

    assert response.status_code == 200
    assert response.json() == {"tyzden": week, "pocet": 1}


def test_public_landing_serves_only_valid_current_data(monkeypatch, tmp_path):
    server = load_server(monkeypatch, tmp_path, [], landing_payload())

    response = TestClient(server.app).get("/api/public/landing")

    assert response.status_code == 200
    assert response.json()["week"] == current_monday()


@pytest.mark.parametrize(
    ("user_count", "expected_visible"),
    [(0, False), (9, False), (10, True), (251, True)],
)
def test_public_landing_reports_only_anonymous_real_account_count(
    monkeypatch, tmp_path, user_count, expected_visible
):
    server = load_server(monkeypatch, tmp_path, [], landing_payload())
    con = server.db()
    try:
        con.executemany(
            "INSERT INTO pouzivatelia (email) VALUES (?)",
            [(f"member-{index}@example.test",) for index in range(user_count)],
        )
        con.commit()
    finally:
        con.close()

    response = TestClient(server.app).get("/api/public/landing")

    assert response.status_code == 200
    community = response.json()["community"]
    assert community == {
        "accounts": user_count,
        "goal": 250,
        "visible": expected_visible,
    }
    assert type(community["accounts"]) is int
    assert type(community["goal"]) is int
    assert "member-" not in response.text


def test_public_landing_hides_community_when_count_query_fails(monkeypatch, tmp_path):
    server = load_server(monkeypatch, tmp_path, [], landing_payload())
    original_db = server.db

    class CountFailingConnection:
        def __init__(self, con):
            self.con = con

        def execute(self, sql):
            if sql == "SELECT COUNT(*) FROM pouzivatelia":
                raise sqlite3.Error("count unavailable")
            return self.con.execute(sql)

        def close(self):
            self.con.close()

    monkeypatch.setattr(server, "db", lambda: CountFailingConnection(original_db()))

    response = TestClient(server.app).get("/api/public/landing")

    assert response.status_code == 200
    assert response.json()["week"] == current_monday()
    assert response.json()["community"]["visible"] is False
    assert "accounts" not in response.json()["community"]


def test_public_landing_is_503_for_stale_data(monkeypatch, tmp_path):
    server = load_server(monkeypatch, tmp_path, [], landing_payload("2026-08-10"))

    response = TestClient(server.app).get("/api/public/landing")

    assert response.status_code == 503
    assert response.json()["detail"] == "Aktuálne letákové dáta sa obnovujú."


def test_weekly_public_page_serves_current_valid_html(monkeypatch, tmp_path):
    server = load_server(monkeypatch, tmp_path, [], landing_payload())

    response = TestClient(server.app).get("/co-varit-tento-tyzden")

    assert response.status_code == 200
    assert response.headers["content-type"] == "text/html; charset=utf-8"
    assert response.headers["cache-control"] == "public, max-age=300, must-revalidate"
    assert "Čo variť tento týždeň" in response.text


@pytest.mark.parametrize(
    "landing_data",
    [
        None,
        landing_payload("2026-08-10"),
        {
            **landing_payload(),
            "receipt": {"meals": "nie-je-zoznam", "nakup_spolu": "99,99", "bezne": "129,99", "usetris": "30,00"},
            "sources": [{
                "store": "Lidl",
                "url": "https://letak.test/tajny-zdroj",
                "valid_from": current_monday(),
                "valid_to": (date.today() + timedelta(days=1)).isoformat(),
            }],
        },
    ],
)
def test_weekly_public_page_fails_closed_without_leaking_stale_claims(
    monkeypatch, tmp_path, landing_data
):
    server = load_server(monkeypatch, tmp_path, [], landing_data)

    response = TestClient(server.app).get("/co-varit-tento-tyzden")

    assert response.status_code == 503
    assert response.headers["content-type"] == "text/html; charset=utf-8"
    assert response.headers["retry-after"] == "900"
    assert response.headers["cache-control"] == "no-store"
    assert "Týždenné ceny práve overujeme" in response.text
    assert "99,99" not in response.text
    assert "129,99" not in response.text
    assert "tajny-zdroj" not in response.text


@pytest.mark.parametrize("route", ["/lacny-jedalnicek", "/ako-varime-z-akcii"])
def test_evergreen_public_pages_are_cacheable_html(route, monkeypatch, tmp_path):
    server = load_server(monkeypatch, tmp_path, [])

    response = TestClient(server.app).get(route)

    assert response.status_code == 200
    assert response.headers["content-type"] == "text/html; charset=utf-8"
    assert response.headers["cache-control"] == "public, max-age=300, must-revalidate"


def test_robots_txt_is_plaintext_with_public_cache(monkeypatch, tmp_path):
    server = load_server(monkeypatch, tmp_path, [])

    response = TestClient(server.app).get("/robots.txt")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert response.headers["cache-control"] == "public, max-age=300, must-revalidate"
    assert "Sitemap: https://uvar.si/sitemap.xml" in response.text


@pytest.mark.parametrize(
    "landing_data, expected_lastmod",
    [
        (landing_payload(), "<lastmod>2026-08-18</lastmod>"),
        (landing_payload("2026-08-10"), None),
        (None, None),
    ],
)
def test_sitemap_reports_weekly_lastmod_only_for_valid_payload(
    monkeypatch, tmp_path, landing_data, expected_lastmod
):
    server = load_server(monkeypatch, tmp_path, [], landing_data)

    response = TestClient(server.app).get("/sitemap.xml")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/xml")
    assert response.headers["cache-control"] == "public, max-age=300, must-revalidate"
    assert "https://uvar.si/co-varit-tento-tyzden" in response.text
    if expected_lastmod is None:
        assert "<lastmod>" not in response.text
    else:
        assert expected_lastmod in response.text


def test_malformed_landing_file_recovers_weekly_page_instead_of_500(monkeypatch, tmp_path):
    landing_path = tmp_path / "landing_data.json"
    landing_path.write_text(
        '{"receipt":{"nakup_spolu":"99,99","bezne":"129,99"},"source_url":"https://letak.test/tajny-zdroj"',
        encoding="utf-8",
    )
    server = load_server_with_landing_path(monkeypatch, tmp_path, [], landing_path)

    response = TestClient(server.app).get("/co-varit-tento-tyzden")

    assert response.status_code == 503
    assert response.headers["content-type"] == "text/html; charset=utf-8"
    assert response.headers["retry-after"] == "900"
    assert response.headers["cache-control"] == "no-store"
    assert "Týždenné ceny práve overujeme" in response.text
    assert "99,99" not in response.text
    assert "129,99" not in response.text
    assert "tajny-zdroj" not in response.text


def test_malformed_landing_file_keeps_sitemap_truthful_and_parseable(monkeypatch, tmp_path):
    landing_path = tmp_path / "landing_data.json"
    landing_path.write_text(
        '{"receipt":{"nakup_spolu":"99,99","bezne":"129,99"},"source_url":"https://letak.test/tajny-zdroj"',
        encoding="utf-8",
    )
    server = load_server_with_landing_path(monkeypatch, tmp_path, [], landing_path)

    response = TestClient(server.app).get("/sitemap.xml")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/xml")
    assert response.headers["cache-control"] == "public, max-age=300, must-revalidate"
    root = ET.fromstring(response.text)
    namespace = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    weekly_url = root.find("sm:url[sm:loc='https://uvar.si/co-varit-tento-tyzden']", namespace)
    assert weekly_url is not None
    assert weekly_url.find("sm:lastmod", namespace) is None


def test_material_profile_change_invalidates_only_that_users_current_cached_plan(monkeypatch, tmp_path):
    server = load_server(monkeypatch, tmp_path, [])
    week = current_monday()
    with server.db() as con:
        con.executemany(
            "INSERT INTO pouzivatelia (id, email, osoby, frekvencia, obchody) VALUES (?, ?, ?, ?, ?)",
            [(1, "first@uvar.si", 4, 2, "Lidl"), (2, "second@uvar.si", 4, 2, "Lidl")],
        )
        insert_hashed_session(server, con, "first-session", 1)
        con.executemany("INSERT INTO plany (user_id, tyzden, json) VALUES (?, ?, ?)", [
            (1, week, '{"cached":"first"}'), (2, week, '{"cached":"second"}'),
        ])
        con.commit()
    client = TestClient(server.app)
    client.cookies.set(server.COOKIE, "first-session")

    response = client.post("/api/profil", json={"osoby": 5, "frekvencia": 2, "obchody": ["Lidl"]})

    assert response.status_code == 200
    with server.db() as con:
        rows = con.execute("SELECT user_id FROM plany WHERE tyzden=? ORDER BY user_id", (week,)).fetchall()
        assert [row[0] for row in rows] == [2]


def test_household_composition_change_invalidates_plan_even_when_total_is_same(monkeypatch, tmp_path):
    server = load_server(monkeypatch, tmp_path, [])
    week = current_monday()
    with server.db() as con:
        con.executemany(
            "INSERT INTO pouzivatelia "
            "(id, email, osoby, dospeli, deti, frekvencia, obchody) "
            "VALUES (?, ?, 4, 2, 2, 2, 'Lidl')",
            [(1, "first@uvar.si"), (2, "second@uvar.si")],
        )
        insert_hashed_session(server, con, "first-session", 1)
        con.executemany(
            "INSERT INTO plany (user_id, tyzden, json) VALUES (?, ?, ?)",
            [(1, week, '{"cached":"first"}'), (2, week, '{"cached":"second"}')],
        )
        con.commit()
    client = TestClient(server.app)
    client.cookies.set(server.COOKIE, "first-session")

    response = client.post("/api/profil", json={
        "adults": 3, "children": 1, "frekvencia": 2, "obchody": ["Lidl"],
    })

    assert response.status_code == 200
    with server.db() as con:
        rows = con.execute(
            "SELECT user_id FROM plany WHERE tyzden=? ORDER BY user_id", (week,)
        ).fetchall()
    assert [row[0] for row in rows] == [2]


def test_profile_api_persists_adults_children_and_total_compatibility(monkeypatch, tmp_path):
    server = load_server(monkeypatch, tmp_path, [])
    with server.db() as con:
        con.execute("INSERT INTO pouzivatelia (id, email) VALUES (1, 'family@uvar.si')")
        insert_hashed_session(server, con, "family-session", 1)
        con.commit()
    client = TestClient(server.app)
    client.cookies.set(server.COOKIE, "family-session")

    saved = client.post("/api/profil", json={
        "adults": 2, "children": 2, "frekvencia": 3, "obchody": ["Lidl"],
    })
    me = client.get("/api/me")

    assert saved.status_code == 200
    assert me.status_code == 200
    assert me.json()["adults"] == 2
    assert me.json()["children"] == 2
    assert me.json()["osoby"] == 4
    with server.db() as con:
        row = con.execute(
            "SELECT dospeli, deti, osoby FROM pouzivatelia WHERE id=1"
        ).fetchone()
    assert tuple(row) == (2, 2, 4)


@pytest.mark.parametrize("adults,children", [
    (0, 0), (-1, 2), (2, -1), (12, 1), (1.5, 1), (True, 1),
])
def test_profile_api_rejects_invalid_household_instead_of_clamping(
        monkeypatch, tmp_path, adults, children):
    server = load_server(monkeypatch, tmp_path, [])
    with server.db() as con:
        con.execute("INSERT INTO pouzivatelia (id, email) VALUES (1, 'family@uvar.si')")
        insert_hashed_session(server, con, "family-session", 1)
        con.commit()
    client = TestClient(server.app)
    client.cookies.set(server.COOKIE, "family-session")

    response = client.post("/api/profil", json={
        "adults": adults, "children": children,
        "frekvencia": 2, "obchody": ["Lidl"],
    })

    assert response.status_code == 422


def test_legacy_osoby_payload_is_treated_as_adults_only(monkeypatch, tmp_path):
    server = load_server(monkeypatch, tmp_path, [])
    with server.db() as con:
        con.execute("INSERT INTO pouzivatelia (id, email) VALUES (1, 'legacy@uvar.si')")
        insert_hashed_session(server, con, "legacy-session", 1)
        con.commit()
    client = TestClient(server.app)
    client.cookies.set(server.COOKIE, "legacy-session")

    response = client.post("/api/profil", json={
        "osoby": 4, "frekvencia": 2, "obchody": ["Lidl"],
    })

    assert response.status_code == 200
    profile = client.get("/api/me").json()
    assert (profile["adults"], profile["children"], profile["osoby"]) == (4, 0, 4)


def test_saving_the_pantry_keeps_the_current_plan_and_never_regenerates_it(monkeypatch, tmp_path):
    """Majiteľ: špajza je oddelený systém, nesmie mu prepísať jedálniček bez vyzvania."""
    server = load_server(monkeypatch, tmp_path, [])
    week = current_monday()
    with server.db() as con:
        con.executemany("INSERT INTO pouzivatelia (id, email) VALUES (?, ?)", [(1, "first@uvar.si"), (2, "second@uvar.si")])
        insert_hashed_session(server, con, "first-session", 1)
        con.executemany("INSERT INTO plany (user_id, tyzden, json) VALUES (?, ?, ?)", [
            (1, week, '{"cached":"first"}'), (2, week, '{"cached":"second"}'),
        ])
        con.commit()
    grant_premium(server, 1)

    constructors = []

    class ForbiddenAnthropic:
        def __init__(self, *args, **kwargs):
            constructors.append((args, kwargs))

    monkeypatch.setitem(sys.modules, "anthropic", types.SimpleNamespace(Anthropic=ForbiddenAnthropic))
    client = TestClient(server.app)
    client.cookies.set(server.COOKIE, "first-session")

    response = client.post("/api/spajza", json={"polozky": ["vajcia"]})

    assert response.status_code == 200
    assert constructors == [], "a pantry edit must never reach the paid model"
    with server.db() as con:
        rows = con.execute(
            "SELECT user_id, json FROM plany WHERE tyzden=? ORDER BY user_id", (week,)
        ).fetchall()
        assert [(row[0], row[1]) for row in rows] == [
            (1, '{"cached":"first"}'), (2, '{"cached":"second"}')
        ]
        saved = [row[0] for row in con.execute(
            "SELECT nazov FROM spajza WHERE user_id=1 ORDER BY id")]
        assert saved == ["vajcia"], "the pantry itself still saves instantly"


def test_the_served_plan_always_reports_the_readers_current_pantry(monkeypatch, tmp_path):
    """Špajza sa dopočítava pri každom čítaní, takže zmena je vidieť okamžite.

    Predtým si plán niesol odtlačok špajze, z ktorej vznikol — musel, lebo
    špajza bola v podpise a plán sa ňou skladal. Odkedy sa jedálniček skladá
    bez špajze, je jediná pravda tá aktuálna: nákupný zoznam sa ňou preznačí
    hneď a bez plateného prepočtu.
    """
    server = load_server(monkeypatch, tmp_path, current_plan_rows())
    with server.db() as con:
        con.execute("INSERT INTO pouzivatelia (id, email, obchody) VALUES (1, 'test@uvar.si', 'Lidl')")
        insert_hashed_session(server, con, "session-token", 1)
        con.execute("INSERT INTO spajza (user_id, nazov) VALUES (1, 'soľ')")
        con.commit()
    grant_premium(server, 1)
    constructors = []
    monkeypatch.setitem(sys.modules, "anthropic", fake_anthropic(model_plan(), constructors))
    client = TestClient(server.app)
    client.cookies.set(server.COOKIE, "session-token")

    generated = finish_plan_request(server, client, "/api/plan/generuj?force=1")

    assert generated.status_code == 200
    assert generated.json()["spajza"] == ["soľ"]

    client.post("/api/spajza", json={"polozky": ["soľ", "Ponuka 2"]})
    cached = client.get("/api/plan")

    assert cached.status_code == 200
    ponuka = next(
        item for group in cached.json()["nakupny_zoznam"] for item in group["polozky"]
        if item["nazov"] == "Ponuka 2"
    )
    assert ponuka["mnozstvo"] == 1
    assert cached.json()["spajza"] == ["soľ", "Ponuka 2"]
    assert [item["nazov"] for item in cached.json()["spajza_pokryte"]] == ["Ponuka 2"]
    assert len(constructors) == 1, "prepočítanie špajze nesmie stáť volanie modelu"
    assert cached.json()["jedla"] == generated.json()["jedla"], "menu ostáva nedotknuté"
    assert client.get("/api/me").json()["spajza"] == ["soľ", "Ponuka 2"]


def test_plan_route_persists_only_reconstructed_server_commerce(monkeypatch, tmp_path):
    server = load_server(monkeypatch, tmp_path, current_plan_rows())
    with server.db() as con:
        con.execute("INSERT INTO pouzivatelia (id, email, obchody) VALUES (1, 'test@uvar.si', 'Lidl')")
        insert_hashed_session(server, con, "session-token", 1)
        con.execute("INSERT INTO spajza (user_id, nazov) VALUES (1, 'soľ')")
        con.commit()
    grant_premium(server, 1)
    constructors = []
    monkeypatch.setitem(sys.modules, "anthropic", fake_anthropic(model_plan(), constructors))
    client = TestClient(server.app)
    client.cookies.set(server.COOKIE, "session-token")

    response = finish_plan_request(server, client, "/api/plan/generuj?force=1")

    assert response.status_code == 200
    payload = response.json()
    assert "_uvarsi_meta" not in payload
    offer = plan_offer(1)
    assert payload["jedla"][0]["suroviny"] == [
        {"offer_key": plan_key(1), "nazov": "Ponuka 1", "obchod": "Lidl", "jednotka": "1 kg",
         "mnozstvo": 1, "davka": "600 g", "cena": "1,01", "povodna": "2,01", "zlava": "-50 %",
         "source_url": offer["source_url"], "source_page": offer["source_page"],
         "valid_from": offer["valid_from"], "valid_to": offer["valid_to"]},
    ]
    assert payload["nakupny_zoznam"][0]["polozky"][0]["nazov"] == "Ponuka 1"
    assert constructors
    # Do `plany` sa ukladá plán BEZ špajze; pohľad so špajzou sa dopočíta pri
    # každom čítaní, aby nikdy nezostarol.
    with server.db() as con:
        ulozeny = json.loads(con.execute("SELECT json FROM plany WHERE user_id=1").fetchone()[0])
        shared = [json.loads(row[0]) for row in con.execute("SELECT json FROM plany_zdielane")]
    assert ulozeny.pop("_uvarsi_meta") == {
        "algo_version": server.PLAN_ALGO_VERSION,
        "portion_standard_version": PORTION_STANDARD_VERSION,
        "pantry_driven": False,
    }
    assert shared and all("_uvarsi_meta" not in plan for plan in shared)
    assert ulozeny == server.plan_without_pantry(payload)
    assert "spajza" not in ulozeny and "spajza_pokryte" not in ulozeny


def test_plan_generation_passes_household_composition_to_signature_prompt_and_builder(
        monkeypatch, tmp_path):
    server = load_server(monkeypatch, tmp_path, current_plan_rows())
    with server.db() as con:
        con.execute(
            "INSERT INTO pouzivatelia "
            "(id, email, osoby, dospeli, deti, obchody) "
            "VALUES (1, 'family@uvar.si', 4, 2, 2, 'Lidl')"
        )
        insert_hashed_session(server, con, "session-token", 1)
        con.commit()
    constructors = []
    message_calls = []
    captured = {}

    def capture_signature(week, stores, household_size, frequency, offer_keys, pantry=(),
                          pantry_driven=False, *, adults=None, children=0):
        captured["signature"] = (household_size, adults, children)
        return "family-signature"

    def capture_messages(rows, frequency, pantry, household_size, variant=0,
                         pantry_driven=False, *, prompt_rows=None, adults=None, children=0):
        captured["messages"] = (household_size, adults, children, prompt_rows)
        return [{"type": "text", "text": "test prompt"}]

    def capture_builder(con, model_output, stores, frequency, household_size, pantry=(),
                        today=None, *, adults=None, children=0):
        captured["builder"] = (household_size, adults, children)
        return {
            "jedla": [], "nakupny_zoznam": [],
            "nakup_spolu": "0,00", "bezne": "0,00", "usetris": "0,00",
        }

    def capture_demand(con, week, stores, *, dospeli, deti, frekvencia, variant):
        captured["demand"] = (dospeli, deti)

    monkeypatch.setattr(server, "plan_signature", capture_signature)
    monkeypatch.setattr(server, "personal_plan_messages", capture_messages)
    monkeypatch.setattr(server, "build_personal_plan", capture_builder)
    monkeypatch.setattr(server.predpocet, "zaznamenaj_dopyt", capture_demand)
    monkeypatch.setitem(
        sys.modules, "anthropic", fake_anthropic(model_plan(), constructors, message_calls)
    )
    client = TestClient(server.app, raise_server_exceptions=False)
    client.cookies.set(server.COOKIE, "session-token")

    response = finish_plan_request(server, client, "/api/plan/generuj?force=1")

    assert response.status_code == 200
    assert captured == {
        "signature": (None, 2, 2),
        "demand": (2, 2),
        "messages": (None, 2, 2, server.select_offers(server.akcie_pre(["Lidl"]), ["Lidl"])),
        "builder": (None, 2, 2),
    }


def test_invalid_model_plan_reports_failure_without_replacing_existing_valid_cache(
        monkeypatch, tmp_path, caplog):
    server = load_server(monkeypatch, tmp_path, current_plan_rows())
    with server.db() as con:
        con.execute("INSERT INTO pouzivatelia (id, email, obchody) VALUES (1, 'test@uvar.si', 'Lidl')")
        insert_hashed_session(server, con, "session-token", 1)
        con.execute("INSERT INTO spajza (user_id, nazov) VALUES (1, 'soľ')")
        current = build_personal_plan(con, model_plan(), ["Lidl"], 2, 4, pantry=["soľ"])
        current = server.osobny_plan_na_ulozenie(current)
        con.execute("INSERT INTO plany (user_id, tyzden, json) VALUES (?, ?, ?)", (1, current_monday(), json.dumps(current)))
        con.commit()
    grant_premium(server, 1)
    constructors = []
    monkeypatch.setitem(sys.modules, "anthropic", fake_anthropic(model_plan(first_offer_key="offer_unknown"), constructors))
    client = TestClient(server.app)
    client.cookies.set(server.COOKIE, "session-token")

    response = finish_plan_request(server, client, "/api/plan/generuj?force=1")

    assert response.status_code == 200
    assert response.json()["status"] == "failed"
    assert response.json()["code"] == "invalid_model_output"
    assert "modelový plán neprešiel bezpečnostnou kontrolou" in caplog.text
    assert "neznáme alebo neaktuálne offer_key" in caplog.text
    with server.db() as con:
        assert json.loads(con.execute("SELECT json FROM plany WHERE user_id=1").fetchone()[0]) == current
        failed = con.execute(
            "SELECT state, error_code FROM plan_jobs ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert tuple(failed) == ("failed", "invalid_model_output")


def test_diversity_validation_error_is_internal_and_never_leaks_to_users(
        monkeypatch, tmp_path, caplog):
    server, client = logged_in_plan_client(monkeypatch, tmp_path)
    calls = []
    monkeypatch.setitem(
        sys.modules, "anthropic", fake_anthropic(repetitive_model_plan(), [], calls)
    )

    response = client.post("/api/plan/generuj?force=1")

    payload = response.json()
    internal_error = "Týždenný plán nemá dosť rôznych spôsobov prípravy."
    assert response.status_code == 200
    assert payload["status"] == "failed"
    assert payload["code"] == "invalid_model_output"
    assert payload["message"] == server.SPRAVA_PLAN_ZLYHAL
    assert internal_error in caplog.text
    assert internal_error not in json.dumps(payload, ensure_ascii=False)
    assert len(calls) == server.MODEL_VALIDATION_ATTEMPTS


def timing_out_anthropic(constructors):
    class APITimeoutError(Exception):
        pass

    class Messages:
        def create(self, **kwargs):
            raise APITimeoutError("request timed out")

    class Anthropic:
        def __init__(self, **kwargs):
            constructors.append(kwargs)
            self.messages = Messages()

    return types.SimpleNamespace(Anthropic=Anthropic, APITimeoutError=APITimeoutError)


def logged_in_plan_client(monkeypatch, tmp_path):
    """Platiaci účet: špajza sa ráta a denný strop prepočtov je ten vyšší."""
    server = load_server(monkeypatch, tmp_path, current_plan_rows())
    with server.db() as con:
        con.execute("INSERT INTO pouzivatelia (id, email, obchody) VALUES (1, 'test@uvar.si', 'Lidl')")
        insert_hashed_session(server, con, "session-token", 1)
        con.execute("INSERT INTO spajza (user_id, nazov) VALUES (1, 'soľ')")
        con.commit()
    grant_premium(server, 1)
    return server, plan_client(server, 1, token="session-token")


def test_model_call_worst_case_wait_is_bounded_and_predictable(monkeypatch, tmp_path):
    """timeout=120 bez max_retries znamená v SDK aj vyše 6 minút na jednom zaseknutí."""
    server, client = logged_in_plan_client(monkeypatch, tmp_path)
    constructors = []
    monkeypatch.setitem(sys.modules, "anthropic", fake_anthropic(model_plan(), constructors))

    assert client.post("/api/plan/generuj?force=1").status_code == 200

    settings = constructors[0]
    assert "max_retries" in settings, "an unset max_retries silently multiplies the wait"
    assert settings["max_retries"] == server.PLAN_MAX_RETRIES
    assert settings["timeout"] == server.PLAN_TIMEOUT_SECONDS
    worst_case = server.PLAN_TIMEOUT_SECONDS * (server.PLAN_MAX_RETRIES + 1)
    assert worst_case == server.PLAN_WORST_CASE_SECONDS
    assert worst_case <= 150, "the user must never be able to wait more than ~2,5 minutes"


def test_a_model_call_that_times_out_answers_in_slovak_instead_of_crashing(monkeypatch, tmp_path):
    server, client = logged_in_plan_client(monkeypatch, tmp_path)
    constructors = []
    monkeypatch.setitem(sys.modules, "anthropic", timing_out_anthropic(constructors))

    response = client.post("/api/plan/generuj?force=1")

    assert response.status_code == 200
    assert response.json()["status"] == "failed"
    assert response.json()["code"] == "provider_timeout"
    assert response.json()["message"].endswith(".")
    with server.db() as con:
        assert con.execute("SELECT COUNT(*) FROM plany").fetchone()[0] == 0


# ------------------------------------------------- zdieľaný plán medzi profilmi
# Dvaja ľudia s tým istým profilom a tou istou špajzou dostanú ten istý plán.
# Doteraz sa počítal dvakrát — dvakrát 60–120 sekúnd čakania aj dvakrát zaplatené.
SHARED_VARIANT_USERS = (1, 1 + 3)  # rovnaká varianta, takže sa plán naozaj zdieľa


def shared_plan_server(monkeypatch, tmp_path, users=SHARED_VARIANT_USERS, pantry=None, premium=True):
    """Špajza je platená, takže testy o špajze bežia na platiacich účtoch."""
    server = load_server(monkeypatch, tmp_path, current_plan_rows())
    with server.db() as con:
        for user_id in users:
            con.execute(
                "INSERT INTO pouzivatelia (id, email, osoby, frekvencia, obchody)"
                " VALUES (?, ?, 4, 2, 'Lidl')", (user_id, f"user{user_id}@uvar.si"),
            )
            insert_hashed_session(server, con, f"session-{user_id}", user_id)
            for item in (pantry or {}).get(user_id, ["soľ"]):
                con.execute("INSERT INTO spajza (user_id, nazov) VALUES (?, ?)", (user_id, item))
        con.commit()
    if premium:
        for user_id in users:
            grant_premium(server, user_id)
    return server


def finish_plan_request(server, client, path, *args, **kwargs):
    response = client.post(path, *args, **kwargs)
    if response.status_code == 202 and path.split("?", 1)[0] in (
        "/api/plan/generuj", "/api/plan/zo-spajze",
    ):
        from app.plan_worker import process_one

        process_one()
        return client.get("/api/plan")
    return response


def plan_client(server, user_id, *, wait_for_worker=True, token=None):
    """Authenticated client for legacy plan assertions.

    Existing server tests exercise the completed plan and injected model fake.
    The production HTTP contract is still asynchronous: this test helper runs
    one local worker iteration only after observing a real 202 response.
    Async API contract tests opt out with ``wait_for_worker=False``.
    """
    client = TestClient(server.app)
    client.cookies.set(server.COOKIE, token or f"session-{user_id}")
    if wait_for_worker:
        post = client.post

        def post_and_finish(path, *args, **kwargs):
            client.post = post
            try:
                return finish_plan_request(server, client, path, *args, **kwargs)
            finally:
                client.post = post_and_finish

        client.post = post_and_finish
    return client


def test_second_user_with_the_same_profile_is_served_the_shared_plan_without_the_model(monkeypatch, tmp_path):
    server = shared_plan_server(monkeypatch, tmp_path)
    calls = []
    monkeypatch.setitem(sys.modules, "anthropic", fake_anthropic(model_plan(), [], calls))
    first, second = SHARED_VARIANT_USERS

    generated = plan_client(server, first).post("/api/plan/generuj")
    assert generated.status_code == 200
    assert len(calls) == 1

    shared = plan_client(server, second).post("/api/plan/generuj")

    assert shared.status_code == 200
    assert len(calls) == 1, "an identical profile must never pay for the model twice"
    assert shared.json()["jedla"] == generated.json()["jedla"]
    assert shared.json()["nakup_spolu"] == generated.json()["nakup_spolu"]
    # Plán musí pristáť aj v jeho vlastnom riadku, aby ho GET /api/plan vedel podať.
    with server.db() as con:
        stored = con.execute("SELECT json FROM plany WHERE user_id=?", (second,)).fetchone()
    assert json.loads(stored[0])["jedla"] == generated.json()["jedla"]


def test_shared_plan_still_carries_only_verified_prices_and_provenance(monkeypatch, tmp_path):
    """Zdieľaný plán nesmie oslabiť sľub o cenách — zdroj aj platnosť idú s ním."""
    server = shared_plan_server(monkeypatch, tmp_path)
    monkeypatch.setitem(sys.modules, "anthropic", fake_anthropic(model_plan(), []))
    first, second = SHARED_VARIANT_USERS
    plan_client(server, first).post("/api/plan/generuj")

    shared = plan_client(server, second).post("/api/plan/generuj").json()

    offer = plan_offer(1)
    bought = shared["jedla"][0]["suroviny"][0]
    assert bought["cena"] == "1,01" and bought["povodna"] == "2,01"
    assert bought["source_url"] == offer["source_url"]
    assert bought["source_page"] == offer["source_page"]
    assert (bought["valid_from"], bought["valid_to"]) == (offer["valid_from"], offer["valid_to"])


def test_shared_plan_is_never_reused_after_the_underlying_offers_changed(monkeypatch, tmp_path):
    """Žiadne staré dáta: iná ponuková sada = iný podpis = nový plán."""
    server = shared_plan_server(monkeypatch, tmp_path)
    calls = []
    monkeypatch.setitem(sys.modules, "anthropic", fake_anthropic(model_plan(), [], calls))
    first, second = SHARED_VARIANT_USERS
    plan_client(server, first).post("/api/plan/generuj")

    with server.db() as con:
        con.execute("DELETE FROM akcie WHERE nazov=?", ("Ponuka 16",))
        con.commit()
    response = plan_client(server, second).post("/api/plan/generuj")

    assert response.status_code == 200
    assert len(calls) == 2, "a changed offer set must not be answered from the shared cache"


def test_shared_plan_is_not_handed_to_a_different_household_or_frequency(monkeypatch, tmp_path):
    server = shared_plan_server(monkeypatch, tmp_path)
    calls = []
    monkeypatch.setitem(sys.modules, "anthropic", fake_anthropic(model_plan(), [], calls))
    first, second = SHARED_VARIANT_USERS
    plan_client(server, first).post("/api/plan/generuj")
    with server.db() as con:
        con.execute("UPDATE pouzivatelia SET osoby=6 WHERE id=?", (second,))
        con.commit()

    assert plan_client(server, second).post("/api/plan/generuj").status_code == 200
    assert len(calls) == 2, "a six-person household must not eat a four-person plan"


def test_a_different_pantry_no_longer_forces_its_own_generation(monkeypatch, tmp_path):
    """Práve toto bolo najdrahšie: platiaci nikdy netrafil zdieľanú cache.

    Špajza jedálniček neskladá, takže dve rovnaké domácnosti s rôznou špajzou
    dostanú ten istý plán — a líšia sa len tým, čo majú v nákupnom zozname
    odškrtnuté ako „máš doma".
    """
    first, second = SHARED_VARIANT_USERS
    server = shared_plan_server(monkeypatch, tmp_path, pantry={first: ["soľ"], second: ["soľ", "ryža"]})
    calls = []
    monkeypatch.setitem(sys.modules, "anthropic", fake_anthropic(model_plan(), [], calls))
    prvy = plan_client(server, first).post("/api/plan/generuj")

    druhy = plan_client(server, second).post("/api/plan/generuj")

    assert druhy.status_code == 200
    assert len(calls) == 1, "iná špajza už nesmie stáť druhé platené volanie"
    assert druhy.json()["jedla"] == prvy.json()["jedla"]
    assert druhy.json()["spajza"] == ["soľ", "ryža"]


def test_matching_pantries_still_share_and_each_user_sees_his_own_pantry(monkeypatch, tmp_path):
    first, second = SHARED_VARIANT_USERS
    server = shared_plan_server(monkeypatch, tmp_path, pantry={first: ["soľ"], second: ["SOĽ"]})
    calls = []
    monkeypatch.setitem(sys.modules, "anthropic", fake_anthropic(model_plan(), [], calls))
    plan_client(server, first).post("/api/plan/generuj")

    shared = plan_client(server, second).post("/api/plan/generuj")

    assert len(calls) == 1
    assert shared.json()["spajza"] == ["SOĽ"], "the plan must report the reader's own pantry"


def test_users_are_spread_over_plan_variants_so_plans_are_not_all_identical(monkeypatch, tmp_path):
    """Produktové rozhodnutie: susedia s rovnakým profilom nemajú mať ten istý jedálniček."""
    server = shared_plan_server(monkeypatch, tmp_path, users=(1, 2))
    calls = []
    monkeypatch.setitem(sys.modules, "anthropic", fake_anthropic(model_plan(), [], calls))

    plan_client(server, 1).post("/api/plan/generuj")
    plan_client(server, 2).post("/api/plan/generuj")

    assert server.PLAN_VARIANTS >= 2
    assert len(calls) == 2, "different variants must be generated separately"
    assert prompt_text(calls[0]) != prompt_text(calls[1])


def test_force_regenerate_never_answers_from_the_shared_cache(monkeypatch, tmp_path):
    server = shared_plan_server(monkeypatch, tmp_path)
    calls = []
    monkeypatch.setitem(sys.modules, "anthropic", fake_anthropic(model_plan(), [], calls))
    first, second = SHARED_VARIANT_USERS
    plan_client(server, first).post("/api/plan/generuj")

    assert plan_client(server, second).post("/api/plan/generuj?force=1").status_code == 200
    assert len(calls) == 2, "'generate me another one' must really call the model"


def test_shared_plans_from_other_weeks_do_not_pile_up(monkeypatch, tmp_path):
    server = shared_plan_server(monkeypatch, tmp_path)
    with server.db() as con:
        con.execute(
            "INSERT INTO plany_zdielane (podpis, variant, tyzden, json) VALUES ('stary', 0, ?, '{}')",
            ("2020-01-06",),
        )
        con.commit()
    monkeypatch.setitem(sys.modules, "anthropic", fake_anthropic(model_plan(), []))

    plan_client(server, SHARED_VARIANT_USERS[0]).post("/api/plan/generuj")

    with server.db() as con:
        weeks = {row[0] for row in con.execute("SELECT tyzden FROM plany_zdielane")}
    assert weeks == {current_monday()}, "last week's shared plans must be dropped"


def test_saving_the_profile_adopts_a_ready_shared_plan_without_calling_the_model(monkeypatch, tmp_path):
    """Zahriatie zadarmo: hotový plán pre nový profil sa prevezme hneď po onboardingu."""
    first, second = SHARED_VARIANT_USERS
    server = shared_plan_server(monkeypatch, tmp_path)
    calls = []
    monkeypatch.setitem(sys.modules, "anthropic", fake_anthropic(model_plan(), [], calls))
    plan_client(server, first).post("/api/plan/generuj")

    saved = plan_client(server, second).post(
        "/api/profil", json={"osoby": 4, "frekvencia": 2, "obchody": ["Lidl"]}
    )

    assert saved.status_code == 200
    assert len(calls) == 1, "onboarding must never trigger a paid model call on its own"
    ready = plan_client(server, second).get("/api/plan")
    assert ready.status_code == 200, "the plan is already waiting when the user looks"


# ------------------------------------------------- prompt, cache a orezaný JSON
def test_the_offer_catalogue_is_sent_as_a_cached_prefix(monkeypatch, tmp_path):
    server, client = logged_in_plan_client(monkeypatch, tmp_path)
    calls = []
    monkeypatch.setitem(sys.modules, "anthropic", fake_anthropic(model_plan(), [], calls))

    assert client.post("/api/plan/generuj?force=1").status_code == 200

    content = calls[0]["messages"][0]["content"]
    assert isinstance(content, list) and len(content) == 2
    assert content[0]["cache_control"] == {"type": "ephemeral"}
    assert plan_key(1) in content[0]["text"], "the offers are the block worth caching"
    assert "cache_control" not in content[1]
    assert len(content[0]["text"]) > len(content[1]["text"]), "the catalogue is the big part"


def test_prompt_cache_usage_is_reported_so_caching_can_be_verified(monkeypatch, tmp_path):
    """Cachovanie sa nesmie iba predpokladať — usage z odpovede to musí potvrdiť."""
    server, client = logged_in_plan_client(monkeypatch, tmp_path)
    usage = types.SimpleNamespace(
        input_tokens=120, output_tokens=900,
        cache_creation_input_tokens=5200, cache_read_input_tokens=0,
    )
    monkeypatch.setitem(sys.modules, "anthropic", fake_anthropic(model_plan(), [], usage=usage))

    assert client.post("/api/plan/generuj?force=1").status_code == 200

    assert server.pouzitie_modelu(usage) == {
        "input": 120, "output": 900, "cache_write": 5200, "cache_read": 0,
    }
    assert server.pouzitie_modelu(None) == {}


def test_output_cut_off_by_max_tokens_is_reported_in_slovak(monkeypatch, tmp_path):
    """max_tokens ohraničuje uvažovanie aj odpoveď — orezaný JSON už raz appku zhodil."""
    server, client = logged_in_plan_client(monkeypatch, tmp_path)
    monkeypatch.setitem(
        sys.modules, "anthropic", fake_anthropic(model_plan(), [], stop_reason="max_tokens")
    )

    response = client.post("/api/plan/generuj?force=1")

    assert response.status_code == 200
    assert response.json()["status"] == "failed"
    assert response.json()["code"] == "invalid_model_output"
    assert response.json()["message"].endswith(".")
    with server.db() as con:
        assert con.execute("SELECT COUNT(*) FROM plany").fetchone()[0] == 0
        assert con.execute("SELECT COUNT(*) FROM plany_zdielane").fetchone()[0] == 0


def test_reasoning_effort_reaches_the_model_and_stays_optional(monkeypatch, tmp_path):
    """Jediná páka na dĺžku volania, ktorá sa dá zmerať len naživo — nech je to jeden riadok."""
    server, client = logged_in_plan_client(monkeypatch, tmp_path)
    calls = []
    monkeypatch.setitem(sys.modules, "anthropic", fake_anthropic(model_plan(), [], calls))

    monkeypatch.setattr(server, "PLAN_EFFORT", None)
    assert client.post("/api/plan/generuj?force=1").status_code == 200
    assert "effort" not in calls[0]["output_config"]
    assert calls[0]["output_config"]["format"]["type"] == "json_schema"

    monkeypatch.setattr(server, "PLAN_EFFORT", "low")
    assert client.post("/api/plan/generuj?force=1").status_code == 200
    assert calls[1]["output_config"]["effort"] == "low"
    assert calls[1]["output_config"]["format"]["type"] == "json_schema"


def test_typeerror_during_model_call_is_never_retried_and_double_charged(monkeypatch, tmp_path):
    """Nejasný TypeError nesmie spustiť druhú platenú požiadavku."""
    server, client = logged_in_plan_client(monkeypatch, tmp_path)
    calls = []

    class Messages:
        def create(self, **kwargs):
            calls.append(kwargs)
            raise TypeError("chyba po odoslaní požiadavky")

    monkeypatch.setitem(sys.modules, "anthropic", types.SimpleNamespace(
        Anthropic=type("A", (), {"__init__": lambda self, **kw: setattr(self, "messages", Messages())})
    ))
    monkeypatch.setattr(server, "PLAN_EFFORT", "low")

    response = client.post("/api/plan/generuj?force=1")
    assert response.json()["status"] == "failed"
    assert response.json()["code"] == "generation_failed"
    assert len(calls) == 1


def test_plan_tokens_leave_room_for_the_longest_real_answer(monkeypatch, tmp_path):
    server = load_server(monkeypatch, tmp_path, [])

    assert server.PLAN_EFFORT == "low", (
        "produkčné meranie ukázalo opakované max_tokens; plánovač musí predvolene "
        "uprednostniť dokončený JSON pred zbytočne dlhým uvažovaním"
    )
    assert server.PLAN_TOKENS >= server.PLAN_ODPOVED_TOKENY * 2, (
        "max_tokens bounds thinking and the answer together; keep real headroom"
    )
    assert server.PLAN_TOKENS >= 10_000, (
        "sedemdňový plán potrebuje rezervu aj pri dočasne dlhšom uvažovaní modelu"
    )
    assert server.PLAN_ODPOVED_TOKENY >= 2600 * 7 / 5, (
        "7 detailných jedál je o 40 % dlhších než doterajšie maximum piatich"
    )
    source = (ROOT / "app" / "server.py").read_text(encoding="utf-8")
    offset = source.index("PLAN_ODPOVED_TOKENY =")
    assert "7 jedál" in source[max(0, offset - 500):offset], (
        "komentár pri rozpočte odpovede musí vysvetľovať sedemdňový maximálny plán"
    )


def category_rows():
    """Týždeň, v akom obchody naozaj zbierajú: mäsa je vždy najviac a je najlacnejšie."""
    today = date.today()
    rows = []
    for index, kategoria in enumerate(
        ["maso"] * 60 + ["zelenina"] * 30 + ["mliecne"] * 20
        + ["trvanlive"] * 20 + ["ovocie"] * 15 + ["pecivo"] * 15, start=1
    ):
        cena = 0.5 + index / 100 if kategoria == "maso" else 5.0 + index / 100
        rows.append((
            current_monday(today), f"{kategoria} {index}", "Lidl", round(cena, 2),
            round(cena * 2, 2), "-50 %", "1 kg", kategoria,
            f"https://example.test/{index}.jpg", index,
            (today - timedelta(days=1)).isoformat(), (today + timedelta(days=1)).isoformat(),
        ))
    return rows


def test_current_offer_catalogue_is_complete_before_prompt_shortlisting(monkeypatch, tmp_path):
    """Katalóg pre podpis a validáciu nesmie byť starý 140-položkový promptový výber."""
    server = load_server(monkeypatch, tmp_path, category_rows())

    selected = server.akcie_pre(["Lidl"])

    categories = {row["kategoria"] for row in selected}
    assert categories == {"maso", "zelenina", "mliecne", "trvanlive", "ovocie", "pecivo"}
    assert len(selected) == len(category_rows()) == 160
    counted = {name: sum(1 for row in selected if row["kategoria"] == name) for name in categories}
    assert counted == {"maso": 60, "zelenina": 30, "mliecne": 20, "trvanlive": 20, "ovocie": 15, "pecivo": 15}


def test_catalogue_without_purchasable_units_is_rejected_before_attempt_is_reserved(
        monkeypatch, tmp_path):
    rows = [tuple(list(row[:6]) + ["hrsť"] + list(row[7:])) for row in category_rows()[:20]]
    server = load_server(monkeypatch, tmp_path, rows)
    with server.db() as con:
        con.execute(
            "INSERT INTO pouzivatelia (id, email, obchody) VALUES (1, 'measure@uvar.si', 'Lidl')"
        )
        insert_hashed_session(server, con, "measure-session", 1)
        con.commit()
    client = TestClient(server.app)
    client.cookies.set(server.COOKIE, "measure-session")

    response = client.post("/api/plan/generuj")

    assert response.status_code == 503
    with server.db() as con:
        assert con.execute("SELECT COUNT(*) FROM plan_jobs").fetchone()[0] == 0


def test_complete_offer_catalogue_is_deterministic_so_the_shared_signature_is_stable(monkeypatch, tmp_path):
    server = load_server(monkeypatch, tmp_path, category_rows())

    assert [row["offer_key"] for row in server.akcie_pre(["Lidl"])] == [
        row["offer_key"] for row in server.akcie_pre(["Lidl"])
    ]


def test_a_meat_only_week_still_fills_the_whole_catalogue(monkeypatch, tmp_path):
    """Vyvažovanie kategórií nesmie zmenšiť ponuku, keď obchod nič iné nemá."""
    rows = [row for row in category_rows() if row[7] == "maso"]
    server = load_server(monkeypatch, tmp_path, rows)

    assert len(server.akcie_pre(["Lidl"])) == 60


def test_live_plan_shortlists_the_prompt_but_validates_an_offer_outside_it(monkeypatch, tmp_path):
    server = load_server(monkeypatch, tmp_path, current_plan_rows(130))
    with server.db() as con:
        con.execute("INSERT INTO pouzivatelia (id, email, obchody) VALUES (1, 'short@uvar.si', 'Lidl')")
        insert_hashed_session(server, con, "short-session", 1)
        con.commit()
    constructors = []
    message_calls = []
    outside_shortlist = plan_key(121)
    monkeypatch.setitem(
        sys.modules, "anthropic", fake_anthropic(
            model_plan(first_offer_key=outside_shortlist), constructors, message_calls
        ),
    )
    client = TestClient(server.app)
    client.cookies.set(server.COOKIE, "short-session")

    response = finish_plan_request(server, client, "/api/plan/generuj?force=1")

    assert response.status_code == 200
    prompt = prompt_text(message_calls[0])
    shown = [plan_key(index) for index in range(1, 131) if plan_key(index) in prompt]
    assert len(shown) <= 120
    assert outside_shortlist not in shown
    assert response.json()["jedla"][0]["suroviny"][0]["offer_key"] == outside_shortlist


@pytest.mark.parametrize("method, path", [("get", "/api/plan"), ("post", "/api/plan/generuj")])
def test_cached_plan_is_503_when_one_selected_offer_is_no_longer_current(monkeypatch, tmp_path, method, path):
    server = load_server(monkeypatch, tmp_path, current_plan_rows())
    with server.db() as con:
        con.execute("INSERT INTO pouzivatelia (id, email, obchody) VALUES (1, 'test@uvar.si', 'Lidl')")
        insert_hashed_session(server, con, "session-token", 1)
        con.execute("INSERT INTO spajza (user_id, nazov) VALUES (1, 'soľ')")
        cached = build_personal_plan(con, model_plan(), ["Lidl"], 2, 4, pantry=["soľ"])
        cached = server.osobny_plan_na_ulozenie(cached)
        con.execute("INSERT INTO plany (user_id, tyzden, json) VALUES (?, ?, ?)", (1, current_monday(), json.dumps(cached)))
        con.execute("DELETE FROM akcie WHERE rowid=1")
        con.commit()
    constructors = []
    monkeypatch.setitem(sys.modules, "anthropic", fake_anthropic(model_plan(), constructors))
    client = TestClient(server.app)
    client.cookies.set(server.COOKIE, "session-token")

    response = getattr(client, method)(path)

    assert response.status_code == 503
    assert constructors == []


def test_get_invalidates_legacy_personal_plan_without_portion_version_for_free(
        monkeypatch, tmp_path):
    server = load_server(monkeypatch, tmp_path, current_plan_rows())
    with server.db() as con:
        con.execute(
            "INSERT INTO pouzivatelia (id, email, obchody) "
            "VALUES (1, 'legacy-plan@uvar.si', 'Lidl')"
        )
        insert_hashed_session(server, con, "legacy-plan-session", 1)
        cached = build_personal_plan(con, model_plan(), ["Lidl"], 2, 4)
        cached = server.osobny_plan_na_ulozenie(cached)
        cached["_uvarsi_meta"].pop("portion_standard_version", None)
        con.execute(
            "INSERT INTO plany (user_id, tyzden, json) VALUES (1, ?, ?)",
            (current_monday(), json.dumps(cached)),
        )
        con.commit()
    constructors = []
    monkeypatch.setitem(sys.modules, "anthropic", fake_anthropic(model_plan(), constructors))
    client = TestClient(server.app)
    client.cookies.set(server.COOKIE, "legacy-plan-session")

    response = client.get("/api/plan")

    assert response.status_code == 200
    assert response.json() == {
        "prazdny": True,
        "vyzaduje_akciu": True,
        "dovod": "plan_zastaral",
        "obnovit_cez": "/api/plan/generuj",
    }
    assert constructors == []
    with server.db() as con:
        assert con.execute("SELECT COUNT(*) FROM plany WHERE user_id=1").fetchone()[0] == 0
        assert con.execute("SELECT COUNT(*) FROM prepocty WHERE user_id=1").fetchone()[0] == 0


def test_portion_standard_bump_requires_get_then_allows_explicit_post_regeneration(
        monkeypatch, tmp_path):
    server = load_server(monkeypatch, tmp_path, current_plan_rows())
    with server.db() as con:
        con.execute(
            "INSERT INTO pouzivatelia (id, email, obchody) "
            "VALUES (1, 'version-bump@uvar.si', 'Lidl')"
        )
        insert_hashed_session(server, con, "version-bump-session", 1)
        cached = build_personal_plan(con, model_plan(), ["Lidl"], 2, 4)
        cached = server.osobny_plan_na_ulozenie(cached)
        con.execute(
            "INSERT INTO plany (user_id, tyzden, json) VALUES (1, ?, ?)",
            (current_monday(), json.dumps(cached)),
        )
        con.commit()
    monkeypatch.setattr(
        server, "PORTION_STANDARD_VERSION", PORTION_STANDARD_VERSION + 1, raising=False
    )
    constructors = []
    monkeypatch.setitem(sys.modules, "anthropic", fake_anthropic(model_plan(), constructors))
    client = TestClient(server.app)
    client.cookies.set(server.COOKIE, "version-bump-session")

    stale = client.get("/api/plan")

    assert stale.status_code == 200
    assert stale.json()["dovod"] == "plan_zastaral"
    assert constructors == []
    with server.db() as con:
        assert con.execute("SELECT COUNT(*) FROM prepocty WHERE user_id=1").fetchone()[0] == 0

    regenerated = finish_plan_request(server, client, "/api/plan/generuj?force=1")

    assert regenerated.status_code == 200
    assert len(constructors) == 1
    assert "_uvarsi_meta" not in regenerated.json()
    with server.db() as con:
        stored = json.loads(
            con.execute("SELECT json FROM plany WHERE user_id=1").fetchone()[0]
        )
        assert stored["_uvarsi_meta"]["portion_standard_version"] == PORTION_STANDARD_VERSION + 1
        assert con.execute("SELECT pocet FROM prepocty WHERE user_id=1").fetchone()[0] == 1


# ------------------------------------------------- Premium: špajza a prepočty
# Rozhodnutie majiteľa: špajza je platená vlastnosť. Zadarmo dostane človek
# zdieľaný plán (skladá sa raz pre všetkých s rovnakým profilom), Premium
# dostane plán poskladaný z toho, čo má naozaj doma. Nárok sa vždy odvodzuje
# na serveri z tabuľky `naroky` — klient o svojom Premium nepovie nič.
def model_plan_without_pantry():
    """Odpoveď modelu pre človeka bez špajze — žiadne suroviny „z domu"."""
    plan = model_plan()
    plan["meals"][0].pop("pantry_ingredients")
    return plan


def premium_user_server(monkeypatch, tmp_path, pantry=(), premium=False, user_id=1):
    server = load_server(monkeypatch, tmp_path, current_plan_rows())
    with server.db() as con:
        con.execute(
            "INSERT INTO pouzivatelia (id, email, osoby, frekvencia, obchody)"
            " VALUES (?, ?, 4, 2, 'Lidl')", (user_id, f"user{user_id}@uvar.si"),
        )
        insert_hashed_session(server, con, f"session-{user_id}", user_id)
        for item in pantry:
            con.execute("INSERT INTO spajza (user_id, nazov) VALUES (?, ?)", (user_id, item))
        con.commit()
    if premium:
        grant_premium(server, user_id)
    return server


def test_a_free_account_stays_free_even_when_the_platiaci_column_says_otherwise(monkeypatch, tmp_path):
    """Ručne prepnutý stĺpec nie je dôkaz o platbe — nárok drží tabuľka naroky."""
    server = premium_user_server(monkeypatch, tmp_path)
    with server.db() as con:
        con.execute("UPDATE pouzivatelia SET platiaci=1 WHERE id=1")
        con.commit()

    profil = plan_client(server, 1).get("/api/me").json()

    assert profil["premium"] is False
    assert profil["platiaci"] is False
    assert profil["limit_prepoctov"] == server.LIMIT_PREPOCTOV_ZDARMA


def test_an_active_entitlement_is_what_makes_an_account_premium(monkeypatch, tmp_path):
    server = premium_user_server(monkeypatch, tmp_path, premium=True)

    profil = plan_client(server, 1).get("/api/me").json()

    assert profil["premium"] is True
    assert profil["limit_prepoctov"] == server.LIMIT_PREPOCTOV_PREMIUM
    assert server.LIMIT_PREPOCTOV_PREMIUM > server.LIMIT_PREPOCTOV_ZDARMA


def test_a_returned_entitlement_takes_premium_away_again(monkeypatch, tmp_path):
    server = premium_user_server(monkeypatch, tmp_path, premium=True)
    with server.db() as con:
        con.execute("UPDATE naroky SET stav='vrateny' WHERE user_id=1")
        con.commit()

    assert plan_client(server, 1).get("/api/me").json()["premium"] is False


def test_a_free_user_cannot_save_the_pantry_and_hears_why_in_slovak(monkeypatch, tmp_path):
    server = premium_user_server(monkeypatch, tmp_path)

    response = plan_client(server, 1).post("/api/spajza", json={"polozky": ["ryža", "vajcia"]})

    assert response.status_code == 403
    detail = response.json()["detail"]
    assert detail == server.SPRAVA_SPAJZA_PREMIUM
    assert "Premium" in detail and "špajza" in detail.casefold()
    with server.db() as con:
        assert con.execute("SELECT COUNT(*) FROM spajza").fetchone()[0] == 0, (
            "a refused pantry save must store nothing at all"
        )


def test_a_premium_user_saves_the_pantry_exactly_as_before(monkeypatch, tmp_path):
    server = premium_user_server(monkeypatch, tmp_path, premium=True)

    response = plan_client(server, 1).post("/api/spajza", json={"polozky": ["ryža", "vajcia"]})

    assert response.status_code == 200
    assert response.json() == {"ok": True, "pocet": 2}
    assert plan_client(server, 1).get("/api/me").json()["spajza"] == ["ryža", "vajcia"]


def test_me_never_hands_a_free_user_a_pantry_that_does_not_count(monkeypatch, tmp_path):
    """Zostatok po skončenom Premium sa nesmie tváriť, že plán stále ovplyvňuje."""
    server = premium_user_server(monkeypatch, tmp_path, pantry=["ryža"])

    profil = plan_client(server, 1).get("/api/me").json()

    assert profil["spajza"] == []
    assert profil["premium"] is False


def test_a_free_users_stored_pantry_never_reaches_the_model_or_the_plan(monkeypatch, tmp_path):
    server = premium_user_server(monkeypatch, tmp_path, pantry=["ryža", "vajcia", "cibuľa"])
    calls = []
    monkeypatch.setitem(
        sys.modules, "anthropic", fake_anthropic(model_plan_without_pantry(), [], calls)
    )

    response = plan_client(server, 1).post("/api/plan/generuj?force=1")

    assert response.status_code == 200
    assert response.json()["spajza"] == [], "a free plan is built from offers only"
    prompt = prompt_text(calls[0])
    for item in ("ryža", "vajcia", "cibuľa"):
        assert item not in prompt, "a free user's pantry must never be paid for"
    assert "Špajza používateľa" not in prompt, (
        "bežný plán je zdieľaný, takže sa v ňom o špajze vôbec nehovorí"
    )


def test_free_users_share_one_plan_no_matter_what_their_pantry_rows_say(monkeypatch, tmp_path):
    """Bez špajze v podpise ostáva plán zdieľaný — inak by každý platil vlastný."""
    first, second = SHARED_VARIANT_USERS
    server = premium_user_server(monkeypatch, tmp_path, pantry=["ryža"], user_id=first)
    with server.db() as con:
        con.execute(
            "INSERT INTO pouzivatelia (id, email, osoby, frekvencia, obchody)"
            " VALUES (?, ?, 4, 2, 'Lidl')", (second, "second@uvar.si"),
        )
        insert_hashed_session(server, con, f"session-{second}", second)
        con.execute("INSERT INTO spajza (user_id, nazov) VALUES (?, 'šošovica')", (second,))
        con.commit()
    calls = []
    monkeypatch.setitem(
        sys.modules, "anthropic", fake_anthropic(model_plan_without_pantry(), [], calls)
    )

    assert plan_client(server, first).post("/api/plan/generuj").status_code == 200
    assert plan_client(server, second).post("/api/plan/generuj").status_code == 200

    assert len(calls) == 1, "two free accounts must never pay for two plans"


def test_a_premium_pantry_shows_up_in_the_shopping_list_not_in_the_generation(monkeypatch, tmp_path):
    """Za čo Premium platí: nekúpiš druhýkrát to, čo doma máš — a nečakáš na to."""
    first, second = SHARED_VARIANT_USERS
    server = shared_plan_server(
        monkeypatch, tmp_path, pantry={first: ["soľ"], second: ["soľ", "Ponuka 2"]}
    )
    calls = []
    monkeypatch.setitem(sys.modules, "anthropic", fake_anthropic(model_plan(), [], calls))

    assert plan_client(server, first).post("/api/plan/generuj").status_code == 200
    assert plan_client(server, second).post("/api/plan/generuj").status_code == 200

    assert len(calls) == 1, "rovnaký profil sa má trafiť do zdieľanej cache aj s Premium"
    druhy = plan_client(server, second).get("/api/plan").json()
    ponuka = next(
        item for group in druhy["nakupny_zoznam"] for item in group["polozky"]
        if item["nazov"] == "Ponuka 2"
    )
    assert ponuka["mnozstvo"] == 1
    assert druhy["spajza"] == ["soľ", "Ponuka 2"]
    assert [item["nazov"] for item in druhy["spajza_pokryte"]] == ["Ponuka 2"]
    assert druhy["nakup_bez_spajze"] != druhy["nakup_spolu"]
    # A prvý používateľ vidí ten istý plán, ale bez cudzej špajze.
    prvy = plan_client(server, first).get("/api/plan").json()
    assert prvy["spajza_pokryte"] == [] and prvy["jedla"] == druhy["jedla"]


# ------------------------------------------------- denný strop prepočtov
def test_a_free_user_gets_one_plan_a_day_and_then_a_friendly_slovak_answer(monkeypatch, tmp_path):
    server = premium_user_server(monkeypatch, tmp_path)
    calls = []
    monkeypatch.setitem(
        sys.modules, "anthropic", fake_anthropic(model_plan_without_pantry(), [], calls)
    )
    client = plan_client(server, 1)

    assert client.post("/api/plan/generuj?force=1").status_code == 200

    response = client.post("/api/plan/generuj?force=1")

    assert response.status_code == 429
    assert len(calls) == 1, "an exhausted budget must never reach the paid model"
    detail = response.json()["detail"]
    assert "zajtra" in detail, "the answer must say when the next one is possible"
    assert "Traceback" not in detail and detail.endswith(".")
    assert client.get("/api/plan").status_code == 200, "the plan they already have stays"
    assert client.get("/api/me").json()["zostava_prepoctov"] == 0


def test_a_premium_user_gets_five_plans_a_day(monkeypatch, tmp_path):
    server = premium_user_server(monkeypatch, tmp_path, pantry=["soľ"], premium=True)
    calls = []
    monkeypatch.setitem(sys.modules, "anthropic", fake_anthropic(model_plan(), [], calls))
    client = plan_client(server, 1)

    for _ in range(server.LIMIT_PREPOCTOV_PREMIUM):
        assert client.post("/api/plan/generuj?force=1").status_code == 200

    assert client.post("/api/plan/generuj?force=1").status_code == 429
    assert len(calls) == server.LIMIT_PREPOCTOV_PREMIUM == 5


def test_the_daily_budget_is_counted_per_account(monkeypatch, tmp_path):
    first, second = SHARED_VARIANT_USERS
    server = shared_plan_server(monkeypatch, tmp_path, premium=False, pantry={first: [], second: []})
    calls = []
    monkeypatch.setitem(
        sys.modules, "anthropic", fake_anthropic(model_plan_without_pantry(), [], calls)
    )
    assert plan_client(server, first).post("/api/plan/generuj?force=1").status_code == 200
    assert plan_client(server, first).post("/api/plan/generuj?force=1").status_code == 429

    assert plan_client(server, second).post("/api/plan/generuj?force=1").status_code == 200


def test_yesterdays_regenerations_never_count_against_today(monkeypatch, tmp_path):
    server = premium_user_server(monkeypatch, tmp_path)
    with server.db() as con:
        con.execute(
            "INSERT INTO prepocty (user_id, den, pocet) VALUES (1, ?, 9)",
            ((date.today() - timedelta(days=1)).isoformat(),),
        )
        con.commit()
    monkeypatch.setitem(sys.modules, "anthropic", fake_anthropic(model_plan_without_pantry(), []))

    assert plan_client(server, 1).post("/api/plan/generuj?force=1").status_code == 200
    with server.db() as con:
        days = {row[0] for row in con.execute("SELECT den FROM prepocty")}
    assert days == {date.today().isoformat()}, "old counters must not pile up"


def test_a_plan_taken_from_the_shared_cache_costs_no_regeneration(monkeypatch, tmp_path):
    """Zdieľaný plán je zadarmo aj pre nás — nesmie teda míňať denný strop."""
    first, second = SHARED_VARIANT_USERS
    server = shared_plan_server(monkeypatch, tmp_path, premium=False, pantry={first: [], second: []})
    monkeypatch.setitem(sys.modules, "anthropic", fake_anthropic(model_plan_without_pantry(), []))
    plan_client(server, first).post("/api/plan/generuj")

    adopted = plan_client(server, second).post("/api/plan/generuj")

    assert adopted.status_code == 200
    profil = plan_client(server, second).get("/api/me").json()
    assert profil["zostava_prepoctov"] == profil["limit_prepoctov"] == server.LIMIT_PREPOCTOV_ZDARMA


def test_a_generation_failure_is_terminal_and_not_silently_retried(monkeypatch, tmp_path):
    """Neistý výsledok po odoslaní sa nesmie automaticky zaplatiť druhýkrát."""
    server = premium_user_server(monkeypatch, tmp_path)
    monkeypatch.setitem(sys.modules, "anthropic", timing_out_anthropic([]))
    client = plan_client(server, 1)

    failed = client.post("/api/plan/generuj?force=1")
    assert failed.status_code == 200
    assert failed.json()["status"] == "failed"
    assert failed.json()["code"] == "provider_timeout"

    monkeypatch.setitem(sys.modules, "anthropic", fake_anthropic(model_plan_without_pantry(), []))
    assert client.post("/api/plan/generuj?force=1").status_code == 429


def test_a_free_user_cannot_buy_extra_regenerations_by_shaping_the_request(monkeypatch, tmp_path):
    server = premium_user_server(monkeypatch, tmp_path)
    calls = []
    monkeypatch.setitem(
        sys.modules, "anthropic", fake_anthropic(model_plan_without_pantry(), [], calls)
    )
    client = plan_client(server, 1)
    assert client.post("/api/plan/generuj?force=1").status_code == 200

    for query in ("?force=1", "?force=99", "?force=-1", "", "?force=0"):
        response = client.post("/api/plan/generuj" + query)
        assert response.status_code in (200, 429), response.text
        if response.status_code == 200:
            assert response.json()["jedla"], "only a cached plan may be served for free"

    assert len(calls) == 1, "no query parameter may unlock a second paid generation"


def test_a_free_user_cannot_shape_a_request_into_pantry_personalisation(monkeypatch, tmp_path):
    server = premium_user_server(monkeypatch, tmp_path)
    client = plan_client(server, 1)

    for payload in (
        {"polozky": ["ryža"], "premium": True},
        {"polozky": ["ryža"], "platiaci": 1, "ma_narok": True},
        {"polozky": ["ryža"] * 80},
    ):
        assert client.post("/api/spajza", json=payload).status_code == 403

    saved = client.post("/api/profil", json={
        "osoby": 4, "frekvencia": 2, "obchody": ["Lidl"],
        "platiaci": 1, "premium": True, "spajza": ["ryža"],
    })

    assert saved.status_code == 200
    profil = client.get("/api/me").json()
    assert profil["premium"] is False and profil["spajza"] == []
    with server.db() as con:
        assert con.execute("SELECT COUNT(*) FROM spajza").fetchone()[0] == 0
        assert con.execute("SELECT COUNT(*) FROM naroky").fetchone()[0] == 0


def test_the_limit_message_names_the_limit_and_the_next_chance(monkeypatch, tmp_path):
    server = load_server(monkeypatch, tmp_path, [])
    dnes = date(2026, 8, 21)

    zadarmo = server.sprava_o_limite(server.LIMIT_PREPOCTOV_ZDARMA, False, dnes)
    platene = server.sprava_o_limite(server.LIMIT_PREPOCTOV_PREMIUM, True, dnes)

    assert "22. 8." in zadarmo and "zajtra" in zadarmo
    assert "5" in platene and "22. 8." in platene
    assert "Premium" in zadarmo, "the free answer may say plainly what the paid tier does"
    assert "Premium" not in platene, "a paying customer must never be sold to again"
    for sprava in (zadarmo, platene):
        assert sprava == sprava.strip() and sprava.endswith(".")


def test_two_tabs_clicking_at_the_same_moment_never_buy_two_plans(monkeypatch, tmp_path):
    """Strop drží server, nie appka — dve otvorené záložky ho neobídu."""
    server = premium_user_server(monkeypatch, tmp_path)
    calls = []
    pomaly = fake_anthropic(model_plan_without_pantry(), [], calls)
    povodne = pomaly.Anthropic

    class Pomaly(povodne):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            create = self.messages.create
            self.messages.create = lambda **kw: (time.sleep(0.4), create(**kw))[1]

    pomaly.Anthropic = Pomaly
    monkeypatch.setitem(sys.modules, "anthropic", pomaly)
    start = threading.Barrier(2)
    odpovede = []

    def klik():
        client = plan_client(server, 1)
        start.wait(timeout=5)
        odpovede.append(client.post("/api/plan/generuj?force=1").status_code)

    vlakna = [threading.Thread(target=klik) for _ in range(2)]
    for vlakno in vlakna:
        vlakno.start()
    for vlakno in vlakna:
        vlakno.join(timeout=30)

    assert sorted(odpovede) == [200, 202]
    assert len(calls) == 1, "súbežné kliknutia nesmú zaplatiť dva modely"


def test_premium_never_depends_on_the_payments_switch(monkeypatch, tmp_path):
    """Rozhodnutie: vypnuté platby neznamenajú Premium zadarmo ani zákaz testovať.

    Vypínač riadi len to, či sa dá zaplatiť. Kto nárok v `naroky` má (kúpou
    alebo ručne od majiteľa), ten Premium má; kto ho nemá, je bezplatný — a to
    aj vtedy, keď je vypínač vypnutý.
    """
    first, second = SHARED_VARIANT_USERS
    server = shared_plan_server(monkeypatch, tmp_path, premium=False)
    grant_premium(server, first)

    platiaci = plan_client(server, first).get("/api/me").json()
    bezplatny = plan_client(server, second).get("/api/me").json()

    assert platiaci["platby_zapnute"] is False and bezplatny["platby_zapnute"] is False
    assert platiaci["premium"] is True and platiaci["spajza_dostupna"] is True
    assert bezplatny["premium"] is False and bezplatny["spajza_dostupna"] is False


# ------------------------------------------------- značky pre appku
# Odmietnutie musí appka rozoznať bez čítania slovenskej vety: text je pre
# človeka, `kod` pre kód. Bez neho by sa obrazovka rozhodovala podľa reťazca,
# ktorý sa raz preformuluje — a zámok by prestal fungovať potichu.
def test_a_refused_pantry_save_carries_a_machine_readable_marker(monkeypatch, tmp_path):
    server = premium_user_server(monkeypatch, tmp_path)

    telo = plan_client(server, 1).post("/api/spajza", json={"polozky": ["ryža"]}).json()

    assert telo["kod"] == server.KOD_SPAJZA_PREMIUM == "spajza_premium"
    assert telo["detail"] == server.SPRAVA_SPAJZA_PREMIUM
    assert telo["premium"] is False and telo["spajza_dostupna"] is False


def test_a_refused_regeneration_carries_the_marker_and_the_numbers(monkeypatch, tmp_path):
    server = premium_user_server(monkeypatch, tmp_path)
    monkeypatch.setitem(sys.modules, "anthropic", fake_anthropic(model_plan_without_pantry(), []))
    client = plan_client(server, 1)
    assert client.post("/api/plan/generuj?force=1").status_code == 200

    telo = client.post("/api/plan/generuj?force=1").json()

    assert telo["kod"] == server.KOD_LIMIT_PREPOCTOV == "limit_prepoctov"
    assert telo["limit_prepoctov"] == server.LIMIT_PREPOCTOV_ZDARMA
    assert telo["zostava_prepoctov"] == 0
    assert telo["obnova"] == (date.today() + timedelta(days=1)).isoformat()
    assert telo["premium"] is False
    assert "zajtra" in telo["detail"]


def test_the_markers_are_stable_identifiers_not_sentences(monkeypatch, tmp_path):
    server = load_server(monkeypatch, tmp_path, [])

    kody = (server.KOD_SPAJZA_PREMIUM, server.KOD_LIMIT_PREPOCTOV)

    assert len(set(kody)) == 2
    for kod in kody:
        assert kod.isascii() and kod == kod.lower() and " " not in kod


# ------------------------------------------------- špajza, ktorá ostala z minulosti
# Majiteľ (a ktokoľvek z bety) má v tabuľke `spajza` riadky spred tohto
# rozhodnutia. Nemažú sa a nikto sa netvári, že tam nie sú: server povie, koľko
# ich je a že do jedálnička dočasne nevstupujú. Po Premium sa vrátia presne tak,
# ako boli.
def test_an_older_pantry_is_kept_even_when_the_account_is_free(monkeypatch, tmp_path):
    server = premium_user_server(monkeypatch, tmp_path, pantry=["ryža", "vajcia", "cibuľa"])
    monkeypatch.setitem(sys.modules, "anthropic", fake_anthropic(model_plan_without_pantry(), []))
    client = plan_client(server, 1)

    client.get("/api/me")
    client.post("/api/spajza", json={"polozky": []})
    client.post("/api/plan/generuj?force=1")

    with server.db() as con:
        ulozene = [row[0] for row in con.execute("SELECT nazov FROM spajza WHERE user_id=1 ORDER BY id")]
    assert ulozene == ["ryža", "vajcia", "cibuľa"], "nič sa nezmazalo potichu"


def test_a_free_account_is_told_its_pantry_is_only_sleeping(monkeypatch, tmp_path):
    server = premium_user_server(monkeypatch, tmp_path, pantry=["ryža", "vajcia", "cibuľa"])

    profil = plan_client(server, 1).get("/api/me").json()

    assert profil["spajza"] == [], "do plánu nevstupuje nič"
    assert profil["spajza_dostupna"] is False
    assert profil["spajza_ulozenych"] == 3
    assert profil["spajza_uspana"] is True
    sprava = profil["spajza_sprava"]
    assert "3" in sprava and "Premium" in sprava
    assert "nemažeme" in sprava.casefold(), "musí povedať, že dáta zostávajú"
    assert sprava == sprava.strip() and sprava.endswith(".")


def test_an_empty_pantry_says_nothing_at_all(monkeypatch, tmp_path):
    server = premium_user_server(monkeypatch, tmp_path)

    profil = plan_client(server, 1).get("/api/me").json()

    assert profil["spajza_ulozenych"] == 0
    assert profil["spajza_uspana"] is False
    assert profil["spajza_sprava"] is None


def test_a_premium_pantry_is_active_and_never_called_sleeping(monkeypatch, tmp_path):
    server = premium_user_server(monkeypatch, tmp_path, pantry=["ryža", "vajcia"], premium=True)

    profil = plan_client(server, 1).get("/api/me").json()

    assert profil["spajza"] == ["ryža", "vajcia"]
    assert profil["spajza_dostupna"] is True
    assert profil["spajza_ulozenych"] == 2
    assert profil["spajza_uspana"] is False and profil["spajza_sprava"] is None


def test_a_pantry_survives_the_end_of_premium_and_comes_back_untouched(monkeypatch, tmp_path):
    server = premium_user_server(monkeypatch, tmp_path, premium=True)
    client = plan_client(server, 1)
    client.post("/api/spajza", json={"polozky": ["ryža", "vajcia"]})

    with server.db() as con:
        con.execute("UPDATE naroky SET stav='vrateny' WHERE user_id=1")
        con.commit()
    uspana = client.get("/api/me").json()

    assert uspana["spajza"] == [] and uspana["spajza_ulozenych"] == 2
    grant_premium(server, 1, order_id="ord-1-znova")
    assert client.get("/api/me").json()["spajza"] == ["ryža", "vajcia"]


def test_the_plan_a_free_user_is_reading_is_never_taken_away_by_the_lock(monkeypatch, tmp_path):
    """Stará špajza plán nezhodí — plán zostáva, kým si človek sám nevyžiada nový."""
    server = premium_user_server(monkeypatch, tmp_path, pantry=["soľ"])
    with server.db() as con:
        plan = build_personal_plan(con, model_plan(), ["Lidl"], 2, 4, pantry=["soľ"])
        plan["spajza"] = ["soľ"]
        plan = server.osobny_plan_na_ulozenie(plan)
        con.execute("INSERT INTO plany (user_id, tyzden, json) VALUES (1, ?, ?)",
                    (current_monday(), json.dumps(plan)))
        con.commit()
    client = plan_client(server, 1)

    ulozeny = client.get("/api/plan")

    assert ulozeny.status_code == 200
    assert ulozeny.json()["jedla"], "jedálniček zostáva na obrazovke, nič sa nezhodí"
    # Uspatá špajza do zoznamu nevstupuje — a server to povie rovno, nezamlčí.
    assert ulozeny.json()["spajza"] == [] and ulozeny.json()["spajza_pokryte"] == []
    assert client.get("/api/me").json()["spajza_uspana"] is True, "a server to povie rovno"


@pytest.mark.parametrize("pocet, tvar", [(1, "položku"), (2, "položky"), (4, "položky"), (5, "položiek")])
def test_the_sleeping_pantry_message_speaks_natural_slovak(monkeypatch, tmp_path, pocet, tvar):
    server = load_server(monkeypatch, tmp_path, [])

    sprava = server.sprava_o_uspanej_spajze(pocet)

    assert f"{pocet} {tvar}" in sprava
    assert server.sprava_o_uspanej_spajze(0) is None


def test_the_pantry_endpoint_reads_the_entitlement_on_every_single_request(monkeypatch, tmp_path):
    """Nárok sa nesmie zapamätať zo sedenia — vrátená platba musí platiť hneď."""
    server = premium_user_server(monkeypatch, tmp_path, premium=True)
    client = plan_client(server, 1)
    assert client.post("/api/spajza", json={"polozky": ["ryža"]}).status_code == 200

    with server.db() as con:
        con.execute("UPDATE naroky SET stav='zruseny' WHERE user_id=1")
        con.commit()

    assert client.post("/api/spajza", json={"polozky": ["ryža", "vajcia"]}).status_code == 403
