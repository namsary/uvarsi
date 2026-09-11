import json
import os
import sqlite3
import subprocess
import sys
import types
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
BASH = Path("C:/Program Files/Git/bin/bash.exe")
LIBRARY = ROOT / "hetzner" / "uvarsi-deploy-state.sh"
TODAY = "2026-09-11"
WEEK = "2026-09-07"
BRIDGE_URL = "https://tesco-bridge.example"
BRIDGE_SECRET = "unit-bridge-secret-0123456789abcdef"


def bash_path(path):
    return "/c" + Path(path).as_posix()[2:]


def write_executable(path, text):
    path.write_text(text, encoding="utf-8", newline="\n")
    path.chmod(0o755)


def bridge_payload():
    return {
        "leaflet": {
            "country": "sk",
            "format": "HM",
            "slug": "tesco-letak-2026-09-07",
            "valid_from": WEEK,
            "valid_to": "2026-09-13",
            "source_url": (
                "https://www.tesco.sk/akciove-ponuky/letaky-a-katalogy/"
                "hypermarkety/tesco-letak-2026-09-07/1"
            ),
            "declared_pages": 8,
            "pages": [
                {
                    "source_page": page,
                    "thumbnail_url": f"{BRIDGE_URL}/v1/tesco/media/token-{page}",
                    "image_url": f"{BRIDGE_URL}/v1/tesco/media/token-{page}",
                }
                for page in range(1, 9)
            ],
        }
    }


def landing_payload():
    return {
        "schema_version": 1,
        "offer_data_version": 2,
        "generated_at": "2026-09-11T05:10:00+02:00",
        "week": WEEK,
        "week_label": "7.–13. 9. 2026",
        "sources": [
            {
                "store": store,
                "url": f"https://source.example/{store.lower()}",
                "valid_from": WEEK,
                "valid_to": "2026-09-13",
            }
            for store in ("Kaufland", "Tesco", "Lidl")
        ],
        "receipt": {
            "meals": [
                {"day": day, "name": name, "items": []}
                for day, name in (
                    ("PO", "Zeleninové rizoto"),
                    ("ST", "Kuracie so zemiakmi"),
                    ("PI", "Cestoviny s paradajkami"),
                )
            ],
            "nakup_spolu": "0,00",
            "bezne": "0,00",
            "usetris": "0,00",
        },
    }


def health_payload():
    return {
        "pocet": 60,
        "plan_queue": {
            "queued": 0,
            "oldest_seconds": None,
            "worker_alive": True,
            "heartbeat_seconds": 5,
            "heartbeat_at": "2026-09-11T03:09:55+00:00",
            "last_ready": "2026-09-11T03:09:55+00:00",
            "failed": 0,
            "blocking_code": None,
        },
        "recipe_engine": {"payments_enabled": False},
    }


def seed_collection_database(path):
    kinds = {
        "Kaufland": "official-kaufland-offers",
        "Tesco": "official-tesco-viewer",
        "Lidl": "official-lidl-viewer",
    }
    with sqlite3.connect(path) as con:
        for table in ("zber_stav", "zber_staging_stav"):
            con.execute(
                f"""CREATE TABLE {table} (
                    tyzden TEXT, obchod TEXT, stav TEXT, pocet INTEGER,
                    data_version INTEGER, collector_kind TEXT,
                    source_fingerprint TEXT, valid_from TEXT, valid_to TEXT,
                    PRIMARY KEY (tyzden, obchod)
                )"""
            )
        for table in ("akcie", "akcie_staging"):
            con.execute(
                f"""CREATE TABLE {table} (
                    tyzden TEXT, obchod TEXT, valid_from TEXT, valid_to TEXT
                )"""
            )
        for index, (store, kind) in enumerate(kinds.items(), start=1):
            fingerprint = f"{index:064x}"
            row = (WEEK, store, "ok", 20, 2, kind, fingerprint, WEEK, "2026-09-13")
            for table in ("zber_stav", "zber_staging_stav"):
                con.execute(f"INSERT INTO {table} VALUES (?,?,?,?,?,?,?,?,?)", row)
            for table in ("akcie", "akcie_staging"):
                con.executemany(
                    f"INSERT INTO {table} VALUES (?,?,?,?)",
                    [(WEEK, store, WEEK, "2026-09-13")] * 20,
                )


@pytest.fixture
def deployment(tmp_path):
    live = tmp_path / "live"
    app = ROOT / "app"
    state = tmp_path / "state"
    live.mkdir()
    state.mkdir()
    database = live / "uvarsi.db"
    landing = live / "landing_data.json"
    env_file = live / "uvarsi.env"
    bridge = state / "bridge.json"
    health = state / "health.json"
    seed_collection_database(database)
    landing.write_text(json.dumps(landing_payload()), encoding="utf-8")
    bridge.write_text(json.dumps(bridge_payload()), encoding="utf-8")
    health.write_text(json.dumps(health_payload()), encoding="utf-8")
    env_file.write_text(
        "UVARSI_ENV=production\n"
        "PLATBY_ZAPNUTE=0\n"
        f"UVARSI_TESCO_BRIDGE_URL={BRIDGE_URL}\n"
        f"UVARSI_TESCO_BRIDGE_SECRET={BRIDGE_SECRET}\n",
        encoding="utf-8",
    )

    curl = tmp_path / "curl"
    write_executable(
        curl,
        "#!/bin/sh\n"
        "set -u\n"
        "printf '%s\\n' \"$@\" > \"$UVARSI_FAKE_STATE/curl-args\"\n"
        "cat > \"$UVARSI_FAKE_STATE/curl-config\"\n"
        "output=/dev/stdout\n"
        "previous=\n"
        "url=\n"
        "for argument in \"$@\"; do\n"
        "  if [ \"$previous\" = output ]; then output=$argument; previous=; continue; fi\n"
        "  case \"$argument\" in\n"
        "    --output|-o) previous=output ;;\n"
        "    http://*|https://*) url=$argument ;;\n"
        "  esac\n"
        "done\n"
        "if [ -f \"$UVARSI_FAKE_STATE/fail-curl\" ]; then\n"
        "  echo \"Bearer $UVARSI_TEST_SECRET provider-response-body\" >&2\n"
        "  exit 22\n"
        "fi\n"
        "case \"$url\" in\n"
        "  */v1/tesco/leaflets) cp \"$UVARSI_BRIDGE_PAYLOAD\" \"$output\" ;;\n"
        "  */api/health) cp \"$UVARSI_HEALTH_FILE\" \"$output\" ;;\n"
        "  *) : > \"$output\" ;;\n"
        "esac\n",
    )
    systemctl = tmp_path / "systemctl"
    write_executable(systemctl, "#!/bin/sh\n[ \"$1\" = is-active ]\n")
    timeout = tmp_path / "timeout"
    write_executable(
        timeout,
        "#!/bin/sh\n"
        "printf '%s\\n' \"$@\" > \"$UVARSI_FAKE_STATE/timeout-args\"\n"
        "exit \"${UVARSI_TIMEOUT_RESULT:-124}\"\n",
    )
    supervisor = live / "dozorca.sh"
    write_executable(supervisor, "#!/bin/sh\nexit 0\n")

    env = os.environ | {
        "UVARSI_DIR": bash_path(live),
        "UVARSI_APP_DIR": bash_path(app),
        "UVARSI_DB": bash_path(database),
        "UVARSI_ENV_FILE": bash_path(env_file),
        "UVARSI_LANDING_DATA": bash_path(landing),
        "UVARSI_CURL": bash_path(curl),
        "UVARSI_SYSTEMCTL": bash_path(systemctl),
        "UVARSI_TIMEOUT": bash_path(timeout),
        "UVARSI_SUPERVISOR": bash_path(supervisor),
        "UVARSI_HEALTH_PY": bash_path(Path(sys.executable)),
        "UVARSI_BRIDGE_PAYLOAD": bash_path(bridge),
        "UVARSI_HEALTH_FILE": bash_path(health),
        "UVARSI_FAKE_STATE": bash_path(state),
        "UVARSI_TODAY": TODAY,
        "UVARSI_TEST_SECRET": BRIDGE_SECRET,
        "UVARSI_TAKTIK_URL": "https://mapa.example/",
    }
    return {
        "live": live,
        "state": state,
        "database": database,
        "landing": landing,
        "env_file": env_file,
        "bridge": bridge,
        "health": health,
        "env": env,
    }


def run_library(deployment, command):
    return subprocess.run(
        [str(BASH), "-c", f'. "{bash_path(LIBRARY)}"\n{command}'],
        cwd=ROOT,
        env=deployment["env"],
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
    )


@pytest.mark.parametrize(
    "env_text",
    [
        "UVARSI_ENV=production\nPLATBY_ZAPNUTE=0\n",
        (
            "UVARSI_ENV=production\nPLATBY_ZAPNUTE=0\n"
            f"UVARSI_TESCO_BRIDGE_URL={BRIDGE_URL}\n"
        ),
        (
            "UVARSI_ENV=production\nPLATBY_ZAPNUTE=0\n"
            "UVARSI_TESCO_BRIDGE_URL=http://bridge.example\n"
            f"UVARSI_TESCO_BRIDGE_SECRET={BRIDGE_SECRET}\n"
        ),
        (
            "UVARSI_ENV=production\nPLATBY_ZAPNUTE=0\n"
            f"UVARSI_TESCO_BRIDGE_URL={BRIDGE_URL}?debug=1\n"
            f"UVARSI_TESCO_BRIDGE_SECRET={BRIDGE_SECRET}\n"
        ),
        (
            "UVARSI_ENV=production\nPLATBY_ZAPNUTE=0\n"
            f"UVARSI_TESCO_BRIDGE_URL={BRIDGE_URL}\n"
            "UVARSI_TESCO_BRIDGE_SECRET=short\n"
        ),
    ],
    ids=("both-missing", "secret-missing", "http-url", "query-url", "short-secret"),
)
def test_bridge_preflight_fails_closed_for_missing_or_malformed_config(
        deployment, env_text):
    deployment["env_file"].write_text(env_text, encoding="utf-8")

    result = run_library(deployment, "uvarsi_require_tesco_bridge")

    assert result.returncode != 0
    assert not deployment["state"].joinpath("curl-args").exists()


def test_bridge_preflight_uses_stdin_auth_and_never_exposes_failure_body(deployment):
    deployment["state"].joinpath("fail-curl").touch()

    result = run_library(deployment, "uvarsi_require_tesco_bridge")

    assert result.returncode != 0
    output = result.stdout + result.stderr
    assert BRIDGE_SECRET not in output
    assert "Bearer" not in output
    assert "provider-response-body" not in output
    assert BRIDGE_SECRET not in deployment["state"].joinpath("curl-args").read_text()
    assert BRIDGE_SECRET in deployment["state"].joinpath("curl-config").read_text()


def test_bridge_preflight_rejects_an_invalid_success_body_without_printing_it(deployment):
    deployment["bridge"].write_text(
        json.dumps({"errors": [{"message": "provider-response-body"}]}),
        encoding="utf-8",
    )

    result = run_library(deployment, "uvarsi_require_tesco_bridge")

    assert result.returncode != 0
    assert "provider-response-body" not in result.stdout + result.stderr


def test_bridge_preflight_accepts_a_current_bounded_hm_manifest(deployment):
    result = run_library(deployment, "uvarsi_require_tesco_bridge")

    assert result.returncode == 0, result.stdout + result.stderr


def test_guarded_supervisor_refuses_payments_on_before_bridge_or_collection(deployment):
    deployment["env_file"].write_text(
        deployment["env_file"].read_text().replace("PLATBY_ZAPNUTE=0", "PLATBY_ZAPNUTE=1"),
        encoding="utf-8",
    )

    result = run_library(deployment, "uvarsi_run_supervisor_bounded")

    assert result.returncode != 0
    assert not deployment["state"].joinpath("curl-args").exists()
    assert not deployment["state"].joinpath("timeout-args").exists()


def test_guarded_supervisor_has_a_four_hour_hard_cap_and_preserves_live_data(deployment):
    database_before = deployment["database"].read_bytes()
    landing_before = deployment["landing"].read_bytes()

    result = run_library(deployment, "uvarsi_run_supervisor_bounded")

    assert result.returncode == 124
    timeout_args = deployment["state"].joinpath("timeout-args").read_text().splitlines()
    assert timeout_args[:3] == ["--signal=TERM", "--kill-after=300", "14400"]
    assert timeout_args[3] == bash_path(deployment["live"] / "dozorca.sh")
    assert deployment["database"].read_bytes() == database_before
    assert deployment["landing"].read_bytes() == landing_before


def test_production_readiness_accepts_three_current_official_reusable_stores(deployment):
    result = run_library(deployment, "uvarsi_require_production_readiness")

    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("store", ["Kaufland", "Tesco", "Lidl"])
def test_production_readiness_requires_each_store_minimum(deployment, store):
    with sqlite3.connect(deployment["database"]) as con:
        con.execute("DELETE FROM akcie WHERE obchod=? AND rowid IN (SELECT rowid FROM akcie WHERE obchod=? LIMIT 1)", (store, store))

    result = run_library(deployment, "uvarsi_require_production_readiness")

    assert result.returncode != 0


def test_production_readiness_rejects_unapproved_source_kind(deployment):
    with sqlite3.connect(deployment["database"]) as con:
        con.execute(
            "UPDATE zber_stav SET collector_kind='kupino-aggregator' WHERE obchod='Tesco'"
        )

    result = run_library(deployment, "uvarsi_require_production_readiness")

    assert result.returncode != 0


def test_production_readiness_requires_matching_reusable_fingerprints(deployment):
    with sqlite3.connect(deployment["database"]) as con:
        con.execute(
            "UPDATE zber_staging_stav SET source_fingerprint=? WHERE obchod='Tesco'",
            ("f" * 64,),
        )

    result = run_library(deployment, "uvarsi_require_production_readiness")

    assert result.returncode != 0


def test_production_readiness_requires_a_current_three_meal_receipt(deployment):
    payload = landing_payload()
    payload["sources"][0]["valid_to"] = "2026-09-10"
    deployment["landing"].write_text(json.dumps(payload), encoding="utf-8")

    result = run_library(deployment, "uvarsi_require_production_readiness")

    assert result.returncode != 0


def test_production_readiness_requires_worker_and_supervisor_health(deployment):
    payload = health_payload()
    payload["plan_queue"]["worker_alive"] = False
    deployment["health"].write_text(json.dumps(payload), encoding="utf-8")
    worker = run_library(deployment, "uvarsi_require_production_readiness")
    assert worker.returncode != 0

    deployment["health"].write_text(json.dumps(health_payload()), encoding="utf-8")
    deployment["live"].joinpath(".dozorca_state").write_text(
        f"{TODAY} 1 -\n", encoding="utf-8"
    )
    supervisor = run_library(deployment, "uvarsi_require_production_readiness")
    assert supervisor.returncode != 0


def test_repeated_verified_fingerprints_use_zero_ai_and_no_extra_budget(
        monkeypatch, tmp_path):
    sys.path.insert(0, str(ROOT / "app"))
    sys.path.insert(0, str(ROOT / "tests"))
    from app import zbierac_akcii as collector
    from test_zbierac_akcii import run_main_over_stores

    database = run_main_over_stores(
        monkeypatch,
        tmp_path,
        {"kaufland": True, "tesco": True, "lidl": True},
    )
    collector.main()
    with sqlite3.connect(database) as con:
        before = (
            con.execute("SELECT COALESCE(SUM(pocet), 0) FROM naklady_behy").fetchone()[0],
            con.execute("SELECT COUNT(*) FROM naklady").fetchone()[0],
            con.execute(
                "SELECT obchod,source_fingerprint FROM zber_staging_stav ORDER BY obchod"
            ).fetchall(),
        )

    calls = []
    monkeypatch.setitem(
        sys.modules,
        "anthropic",
        types.SimpleNamespace(
            Anthropic=lambda **_kwargs: calls.append("anthropic")
        ),
    )
    monkeypatch.setattr(
        collector,
        "zbieraj",
        lambda *_args: pytest.fail("unchanged fingerprint must reuse stored extraction"),
    )

    collector.main()

    with sqlite3.connect(database) as con:
        after = (
            con.execute("SELECT COALESCE(SUM(pocet), 0) FROM naklady_behy").fetchone()[0],
            con.execute("SELECT COUNT(*) FROM naklady").fetchone()[0],
            con.execute(
                "SELECT obchod,source_fingerprint FROM zber_staging_stav ORDER BY obchod"
            ).fetchall(),
        )
    assert calls == []
    assert after == before


def test_cost_details_redact_bridge_anthropic_and_bearer_secrets(monkeypatch, tmp_path):
    sys.path.insert(0, str(ROOT / "app"))
    from app import naklady

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-unit-secret")
    monkeypatch.setenv("UVARSI_TESCO_BRIDGE_SECRET", BRIDGE_SECRET)
    con = naklady.pripoj(tmp_path / "costs.db")
    naklady.zapis(
        con,
        "plan",
        "claude-sonnet-5",
        None,
        detail=(
            f"Authorization: Bearer {BRIDGE_SECRET}; sk-ant-unit-secret; "
            "provider response body"
        ),
        notifikuj=lambda _message: None,
    )

    stored = con.execute("SELECT detail FROM naklady").fetchone()[0]
    con.close()
    assert BRIDGE_SECRET not in stored
    assert "sk-ant-unit-secret" not in stored
    assert "Bearer" not in stored
    assert "provider response body" not in stored
    typical_error = (
        "BadRequestError: Error code: 400 - "
        "{'error': {'message': 'provider-detail-should-not-leak'}}"
    )
    assert "provider-detail-should-not-leak" not in naklady.bezpecny_detail(typical_error)
