import base64
import hashlib
import hmac
import json
import os
import shutil
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
NOW_EPOCH = "2000000000"
BRIDGE_HOST = "uvarsi-tesco-bridge.account.workers.dev"
BRIDGE_URL = f"https://{BRIDGE_HOST}"
BRIDGE_RELEASE = "a1b2c3d4e5f6"
BRIDGE_VERSION_ID = "11aa22bb-33cc-44dd-88ee-99ff00112233"
BRIDGE_SECRET = f"{BRIDGE_RELEASE}.unit-bridge-secret-0123456789abcdef"
SUPERVISOR_CRON = (
    "0 5-21 * * * /opt/uvarsi/uvarsi-deploy-state.sh run-supervisor "
    ">> /var/log/uvarsi.log 2>&1"
)
BACKUP_CRON = "30 3 * * * /opt/uvarsi/zaloha.sh >> /var/log/uvarsi-zaloha.log 2>&1"
PAYMENT_CRON = (
    "5 * * * * cd /opt/uvarsi/app && /opt/uvarsi/venv/bin/python "
    "rekonciliacia.py >> /var/log/uvarsi-platby.log 2>&1"
)
TAKTIK_CRON = "*/5 * * * * /opt/taktik-mapa/refresh.sh"


def bash_path(path):
    return "/c" + Path(path).as_posix()[2:]


def write_executable(path, text):
    path.write_text(text, encoding="utf-8", newline="\n")
    path.chmod(0o755)


def bridge_payload(*, release=BRIDGE_RELEASE, version_id=BRIDGE_VERSION_ID):
    leaflet = {
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
    statement = {
        "release": release,
        "version_id": version_id,
        "request_date": TODAY,
        "request_format": "HM",
        "leaflet": leaflet,
    }
    signature = hmac.new(
        BRIDGE_SECRET.encode(),
        json.dumps(statement, ensure_ascii=False, separators=(",", ":")).encode(),
        hashlib.sha256,
    ).digest()
    return {
        "bridge": {
            "release": release,
            "version_id": version_id,
            "attestation": base64.urlsafe_b64encode(signature).decode().rstrip("="),
        },
        "leaflet": leaflet,
    }


def official_source_url(store):
    return {
        "Kaufland": "https://predajne.kaufland.sk/aktualna-ponuka/prehlad.html",
        "Tesco": (
            "https://www.tesco.sk/akciove-ponuky/letaky-a-katalogy/"
            "hypermarkety/tesco-letak-2026-09-07/1"
        ),
        "Lidl": "https://www.lidl.sk/c/akcny-letak/s10023254",
    }[store]


def landing_payload():
    items = {
        "Kaufland": {
            "name": "Repkový olej Raciol", "unit": "1 l", "price": "1,55",
            "original_price": "2,99", "savings": "1,44", "off": "-48 %",
        },
        "Tesco": {
            "name": "Overená položka Tesco 1", "unit": "1 ks", "price": "1,99",
            "original_price": "2,49", "savings": "0,50", "off": "-20 %",
            "loyalty_price": "1,49", "loyalty_discount": "-40 %",
            "loyalty_program": "Clubcard", "loyalty_minimum_basket": None,
            "loyalty_condition": "iba s Clubcard",
        },
        "Lidl": {
            "name": "Overená položka Lidl 1", "unit": "1 ks", "price": "1,99",
            "original_price": "2,49", "savings": "0,50", "off": "-20 %",
        },
    }
    stores = tuple(items)
    return {
        "schema_version": 1,
        "offer_data_version": 2,
        "generated_at": "2026-09-11T05:10:00+02:00",
        "week": WEEK,
        "week_label": "7.–13. 9. 2026",
        "sources": [
            {
                "store": store,
                "url": official_source_url(store),
                "source_page": 1,
                "valid_from": WEEK,
                "valid_to": "2026-09-13",
            }
            for store in stores
        ],
        "receipt": {
            "meals": [
                {
                    "day": day,
                    "name": name,
                    "items": [{
                        "offer_key": f"{store.lower()}-offer-1",
                        "store": store,
                        "quantity": 1,
                        **items[store],
                    }],
                }
                for day, name, store in (
                    ("PO", "Zeleninové rizoto", "Kaufland"),
                    ("ST", "Kuracie so zemiakmi", "Tesco"),
                    ("PI", "Cestoviny s paradajkami", "Lidl"),
                )
            ],
            "nakup_spolu": "5,53",
            "bezne": "7,97",
            "usetris": "2,44",
            "polozky": 3,
            "polozky_s_beznou_cenou": 3,
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
                    tyzden TEXT, obchod TEXT, nazov TEXT, cena REAL,
                    povodna REAL, zlava TEXT, jednotka TEXT,
                    source_url TEXT, source_page INTEGER, offer_key TEXT,
                    valid_from TEXT, valid_to TEXT, cena_s_kartou REAL,
                    zlava_s_kartou TEXT, vernostny_program TEXT,
                    minimalny_nakup REAL, podmienka_s_kartou TEXT
                )"""
            )
        for index, (store, kind) in enumerate(kinds.items(), start=1):
            fingerprint = f"{index:064x}"
            row = (WEEK, store, "ok", 20, 2, kind, fingerprint, WEEK, "2026-09-13")
            for table in ("zber_stav", "zber_staging_stav"):
                con.execute(f"INSERT INTO {table} VALUES (?,?,?,?,?,?,?,?,?)", row)
            for table in ("akcie", "akcie_staging"):
                rows = []
                for offer in range(1, 21):
                    name = f"Overená položka {store} {offer}"
                    price, original, discount, unit = 1.99, 2.49, "-20 %", "1 ks"
                    card_price = card_discount = program = minimum = condition = None
                    if store == "Kaufland" and offer == 1:
                        name, price, original, discount, unit = (
                            "Repkový olej Raciol", 1.55, 2.99, "-48 %", "1 l"
                        )
                    if store == "Tesco" and offer == 1:
                        card_price, card_discount, program, condition = (
                            1.49, "-40 %", "Clubcard", "iba s Clubcard"
                        )
                    rows.append((
                        WEEK, store, name, price, original, discount, unit,
                        official_source_url(store), offer,
                        f"{store.lower()}-offer-{offer}", WEEK, "2026-09-13",
                        card_price, card_discount, program, minimum, condition,
                    ))
                con.executemany(
                    f"INSERT INTO {table} VALUES ({','.join('?' for _ in range(17))})",
                    rows,
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
    cron_state = state / "crontab"
    supervisor_success = live / ".supervisor_success_state"
    seed_collection_database(database)
    landing.write_text(json.dumps(landing_payload()), encoding="utf-8")
    bridge.write_text(json.dumps(bridge_payload()), encoding="utf-8")
    health.write_text(json.dumps(health_payload()), encoding="utf-8")
    env_file.write_text(
        "UVARSI_ENV=production\n"
        "PLATBY_ZAPNUTE=0\n"
        f"UVARSI_TESCO_BRIDGE_URL={BRIDGE_URL}\n"
        f"UVARSI_TESCO_BRIDGE_WORKER_HOST={BRIDGE_HOST}\n"
        f"UVARSI_TESCO_BRIDGE_RELEASE={BRIDGE_RELEASE}\n"
        f"UVARSI_TESCO_BRIDGE_VERSION_ID={BRIDGE_VERSION_ID}\n"
        f"UVARSI_TESCO_BRIDGE_SECRET={BRIDGE_SECRET}\n",
        encoding="utf-8",
    )
    cron_state.write_text(f"{TAKTIK_CRON}\n{SUPERVISOR_CRON}\n", encoding="utf-8")
    supervisor_success.write_text(
        f"{TODAY} {NOW_EPOCH}\n", encoding="utf-8", newline="\n"
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
    crontab = tmp_path / "crontab"
    write_executable(
        crontab,
        "#!/bin/sh\n"
        "if [ \"${1:-}\" = -l ]; then\n"
        "  [ ! -f \"$UVARSI_FAKE_STATE/fail-crontab-list\" ] || exit 9\n"
        "  if [ -f \"$UVARSI_FAKE_STATE/fail-crontab-list-after-write\" ] && "
        "[ -f \"$UVARSI_FAKE_STATE/crontab-written\" ]; then exit 9; fi\n"
        "  [ ! -f \"$UVARSI_FAKE_STATE/crontab\" ] || "
        "cat \"$UVARSI_FAKE_STATE/crontab\"\n"
        "  exit 0\n"
        "fi\n"
        "touch \"$UVARSI_FAKE_STATE/crontab-written\"\n"
        "cp \"$1\" \"$UVARSI_FAKE_STATE/crontab\"\n",
    )
    flock = tmp_path / "flock"
    write_executable(
        flock,
        "#!/bin/sh\n"
        "[ \"${1:-}\" = -x ] || exit 2\n"
        "[ \"${2:-}\" = 9 ] || exit 2\n"
        "touch \"$UVARSI_FAKE_STATE/cron-lock-acquired\"\n",
    )
    timeout = tmp_path / "timeout"
    write_executable(
        timeout,
        "#!/bin/sh\n"
        "printf '%s\\n' \"$@\" > \"$UVARSI_FAKE_STATE/timeout-args\"\n"
        "result=${UVARSI_TIMEOUT_RESULT:-124}\n"
        "if [ \"$result\" -eq 0 ] && [ \"${UVARSI_TIMEOUT_RUN_COMMAND:-0}\" = 1 ]; then\n"
        "  shift 3\n"
        "  \"$@\"\n"
        "  result=$?\n"
        "fi\n"
        "exit \"$result\"\n",
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
        "UVARSI_CRONTAB": bash_path(crontab),
        "UVARSI_FLOCK": bash_path(flock),
        "UVARSI_CRON_LOCK": bash_path(state / "crontab.lock"),
        "UVARSI_TIMEOUT": bash_path(timeout),
        "UVARSI_SUPERVISOR": bash_path(supervisor),
        "UVARSI_SUPERVISOR_SUCCESS_STATE": bash_path(supervisor_success),
        "UVARSI_HEALTH_PY": bash_path(Path(sys.executable)),
        "UVARSI_BRIDGE_PAYLOAD": bash_path(bridge),
        "UVARSI_HEALTH_FILE": bash_path(health),
        "UVARSI_FAKE_STATE": bash_path(state),
        "UVARSI_TODAY": TODAY,
        "UVARSI_NOW_EPOCH": NOW_EPOCH,
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
        "cron": cron_state,
        "supervisor_success": supervisor_success,
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
            f"UVARSI_TESCO_BRIDGE_WORKER_HOST={BRIDGE_HOST}\n"
            f"UVARSI_TESCO_BRIDGE_RELEASE={BRIDGE_RELEASE}\n"
            f"UVARSI_TESCO_BRIDGE_VERSION_ID={BRIDGE_VERSION_ID}\n"
        ),
        (
            "UVARSI_ENV=production\nPLATBY_ZAPNUTE=0\n"
            "UVARSI_TESCO_BRIDGE_URL=http://bridge.example\n"
            f"UVARSI_TESCO_BRIDGE_WORKER_HOST={BRIDGE_HOST}\n"
            f"UVARSI_TESCO_BRIDGE_RELEASE={BRIDGE_RELEASE}\n"
            f"UVARSI_TESCO_BRIDGE_VERSION_ID={BRIDGE_VERSION_ID}\n"
            f"UVARSI_TESCO_BRIDGE_SECRET={BRIDGE_SECRET}\n"
        ),
        (
            "UVARSI_ENV=production\nPLATBY_ZAPNUTE=0\n"
            f"UVARSI_TESCO_BRIDGE_URL={BRIDGE_URL}?debug=1\n"
            f"UVARSI_TESCO_BRIDGE_WORKER_HOST={BRIDGE_HOST}\n"
            f"UVARSI_TESCO_BRIDGE_RELEASE={BRIDGE_RELEASE}\n"
            f"UVARSI_TESCO_BRIDGE_VERSION_ID={BRIDGE_VERSION_ID}\n"
            f"UVARSI_TESCO_BRIDGE_SECRET={BRIDGE_SECRET}\n"
        ),
        (
            "UVARSI_ENV=production\nPLATBY_ZAPNUTE=0\n"
            f"UVARSI_TESCO_BRIDGE_URL={BRIDGE_URL}\n"
            f"UVARSI_TESCO_BRIDGE_WORKER_HOST={BRIDGE_HOST}\n"
            f"UVARSI_TESCO_BRIDGE_RELEASE={BRIDGE_RELEASE}\n"
            f"UVARSI_TESCO_BRIDGE_VERSION_ID={BRIDGE_VERSION_ID}\n"
            "UVARSI_TESCO_BRIDGE_SECRET=short\n"
        ),
        (
            "UVARSI_ENV=production\nPLATBY_ZAPNUTE=0\n"
            f"UVARSI_TESCO_BRIDGE_URL={BRIDGE_URL}\n"
            f"UVARSI_TESCO_BRIDGE_RELEASE={BRIDGE_RELEASE}\n"
            f"UVARSI_TESCO_BRIDGE_VERSION_ID={BRIDGE_VERSION_ID}\n"
            f"UVARSI_TESCO_BRIDGE_SECRET={BRIDGE_SECRET}\n"
        ),
        (
            "UVARSI_ENV=production\nPLATBY_ZAPNUTE=0\n"
            f"UVARSI_TESCO_BRIDGE_URL={BRIDGE_URL}\n"
            f"UVARSI_TESCO_BRIDGE_WORKER_HOST={BRIDGE_HOST}\n"
            f"UVARSI_TESCO_BRIDGE_VERSION_ID={BRIDGE_VERSION_ID}\n"
            f"UVARSI_TESCO_BRIDGE_SECRET={BRIDGE_SECRET}\n"
        ),
        (
            "UVARSI_ENV=production\nPLATBY_ZAPNUTE=0\n"
            f"UVARSI_TESCO_BRIDGE_URL={BRIDGE_URL}\n"
            f"UVARSI_TESCO_BRIDGE_WORKER_HOST={BRIDGE_HOST}\n"
            "UVARSI_TESCO_BRIDGE_RELEASE=ffffffffffff\n"
            f"UVARSI_TESCO_BRIDGE_VERSION_ID={BRIDGE_VERSION_ID}\n"
            f"UVARSI_TESCO_BRIDGE_SECRET={BRIDGE_SECRET}\n"
        ),
        (
            "UVARSI_ENV=production\nPLATBY_ZAPNUTE=0\n"
            f"UVARSI_TESCO_BRIDGE_URL={BRIDGE_URL}\n"
            f"UVARSI_TESCO_BRIDGE_WORKER_HOST={BRIDGE_HOST}\n"
            f"UVARSI_TESCO_BRIDGE_RELEASE={BRIDGE_RELEASE}\n"
            f"UVARSI_TESCO_BRIDGE_SECRET={BRIDGE_SECRET}\n"
        ),
    ],
    ids=(
        "both-missing", "secret-missing", "http-url", "query-url", "short-secret",
        "worker-host-missing", "release-missing", "release-secret-mismatch",
        "version-id-missing",
    ),
)
def test_bridge_preflight_fails_closed_for_missing_or_malformed_config(
        deployment, env_text):
    deployment["env_file"].write_text(env_text, encoding="utf-8")

    result = run_library(deployment, "_uvarsi_require_tesco_bridge_transport")

    assert result.returncode != 0
    assert not deployment["state"].joinpath("curl-args").exists()


def test_bridge_preflight_uses_stdin_auth_and_never_exposes_failure_body(deployment):
    deployment["state"].joinpath("fail-curl").touch()

    result = run_library(deployment, "_uvarsi_require_tesco_bridge_transport")

    assert result.returncode != 0
    output = result.stdout + result.stderr
    assert BRIDGE_SECRET not in output
    assert "Bearer" not in output
    assert "provider-response-body" not in output
    assert BRIDGE_SECRET not in deployment["state"].joinpath("curl-args").read_text()
    assert BRIDGE_SECRET in deployment["state"].joinpath("curl-config").read_text()


def test_bridge_preflight_disables_inherited_xtrace_before_reading_secrets(deployment):
    deployment["state"].joinpath("fail-curl").touch()

    result = run_library(deployment, "set -x\n_uvarsi_require_tesco_bridge_transport")

    output = result.stdout + result.stderr
    assert result.returncode != 0
    assert BRIDGE_SECRET not in output
    assert "Bearer" not in output
    assert "provider-response-body" not in output


def test_bridge_preflight_is_pinned_to_the_configured_worker_host_and_release(deployment):
    rogue_url = "https://uvarsi-tesco-bridge.attacker.workers.dev"
    env_text = deployment["env_file"].read_text().replace(BRIDGE_URL, rogue_url)
    deployment["env_file"].write_text(env_text, encoding="utf-8")
    payload = bridge_payload()
    for page in payload["leaflet"]["pages"]:
        page["thumbnail_url"] = page["thumbnail_url"].replace(BRIDGE_URL, rogue_url)
        page["image_url"] = page["image_url"].replace(BRIDGE_URL, rogue_url)
    deployment["bridge"].write_text(json.dumps(payload), encoding="utf-8")

    result = run_library(deployment, "_uvarsi_require_tesco_bridge_transport")

    assert result.returncode != 0
    assert not deployment["state"].joinpath("curl-args").exists()


@pytest.mark.parametrize("claim", ["release", "version_id", "attestation"])
def test_bridge_preflight_rejects_wrong_or_unbound_worker_identity(deployment, claim):
    if claim == "release":
        payload = bridge_payload(release="f" * 12)
    elif claim == "version_id":
        payload = bridge_payload(version_id="different-worker-version")
    else:
        payload = bridge_payload()
        payload["bridge"]["attestation"] = "A" * 43
    deployment["bridge"].write_text(json.dumps(payload), encoding="utf-8")

    result = run_library(deployment, "_uvarsi_require_tesco_bridge_transport")

    assert result.returncode != 0
    assert BRIDGE_SECRET not in result.stdout + result.stderr


@pytest.mark.parametrize("bad_source", [
    "https://www.kupino.sk/letak/tesco-letak-2026-09-07-2026-09-13",
    "https://www.tesco.sk/akciove-ponuky/letaky-a-katalogy/"
    "supermarkety/tesco-letak-2026-09-08/1",
])
def test_bridge_preflight_requires_exact_official_tesco_source_provenance(
        deployment, bad_source):
    payload = bridge_payload()
    payload["leaflet"]["source_url"] = bad_source
    deployment["bridge"].write_text(json.dumps(payload), encoding="utf-8")

    result = run_library(deployment, "_uvarsi_require_tesco_bridge_transport")

    assert result.returncode != 0


def test_bridge_preflight_rejects_an_invalid_success_body_without_printing_it(deployment):
    deployment["bridge"].write_text(
        json.dumps({"errors": [{"message": "provider-response-body"}]}),
        encoding="utf-8",
    )

    result = run_library(deployment, "_uvarsi_require_tesco_bridge_transport")

    assert result.returncode != 0
    assert "provider-response-body" not in result.stdout + result.stderr


def test_bridge_preflight_accepts_a_current_bounded_hm_manifest(deployment):
    result = run_library(deployment, "_uvarsi_require_tesco_bridge_transport")

    assert result.returncode == 0, result.stdout + result.stderr


def test_legacy_bridge_gate_allows_one_transition_with_verified_official_data(
        deployment):
    deployment["bridge"].write_text(
        json.dumps(bridge_payload(release="f" * 12)), encoding="utf-8"
    )

    result = run_library(deployment, "uvarsi_require_tesco_bridge")

    assert result.returncode == 0, result.stdout + result.stderr


def test_legacy_bridge_gate_reuses_current_active_data_after_failed_staging_run(
        deployment):
    """An abandoned staging candidate must not poison last-known-good data."""
    with sqlite3.connect(deployment["database"]) as con:
        con.execute(
            "UPDATE zber_staging_stav SET source_fingerprint=? WHERE obchod='Tesco'",
            ("f" * 64,),
        )
        con.execute(
            "DELETE FROM akcie_staging WHERE obchod='Lidl' AND rowid IN "
            "(SELECT rowid FROM akcie_staging WHERE obchod='Lidl' LIMIT 1)"
        )
    deployment["state"].joinpath("fail-curl").touch()

    result = run_library(deployment, "uvarsi_require_tesco_bridge")

    assert result.returncode == 0, result.stdout + result.stderr


def test_legacy_code_only_transition_still_fails_closed_when_payments_are_on(
        deployment):
    with sqlite3.connect(deployment["database"]) as con:
        con.execute(
            "UPDATE zber_stav SET collector_kind='kupino-aggregator' "
            "WHERE obchod='Tesco'"
        )
    deployment["bridge"].write_text(
        json.dumps(bridge_payload(release="f" * 12)), encoding="utf-8"
    )
    deployment["env_file"].write_text(
        deployment["env_file"].read_text().replace(
            "PLATBY_ZAPNUTE=0", "PLATBY_ZAPNUTE=1"
        ),
        encoding="utf-8",
    )

    result = run_library(deployment, "uvarsi_require_tesco_bridge")

    assert result.returncode != 0


def test_legacy_samopull_can_install_code_only_fix_with_payments_off(deployment):
    """The old caller gets one process-local escape; strict state stays strict."""
    with sqlite3.connect(deployment["database"]) as con:
        con.execute(
            "UPDATE zber_stav SET collector_kind='kupino-aggregator' "
            "WHERE obchod='Tesco'"
        )
    deployment["bridge"].write_text(
        json.dumps(bridge_payload(release="f" * 12)), encoding="utf-8"
    )
    deployment["env"]["UVARSI_TIMEOUT_RESULT"] = "0"
    deployment["env"]["UVARSI_HEARTBEAT_ATTEMPTS"] = "1"
    deployment["env"]["UVARSI_SLEEP"] = "true"

    transition = run_library(
        deployment,
        "uvarsi_require_tesco_bridge && "
        "test \"$UVARSI_LEGACY_CODE_DEPLOY\" = 1 && "
        "uvarsi_wait_fresh_heartbeat \"2026-09-11T03:09:55+00:00\" && "
        "uvarsi_run_supervisor_bounded && "
        "uvarsi_require_production_readiness",
    )
    strict_fresh_process = run_library(
        deployment, "uvarsi_require_production_readiness"
    )

    assert transition.returncode == 0, transition.stdout + transition.stderr
    assert strict_fresh_process.returncode != 0


def test_bridge_failure_exposes_only_a_stable_secret_safe_reason(deployment):
    deployment["state"].joinpath("fail-curl").touch()

    result = run_library(
        deployment,
        "_uvarsi_require_tesco_bridge_transport || "
        "printf '%s' \"$UVARSI_BRIDGE_FAILURE_REASON\"",
    )

    output = result.stdout + result.stderr
    assert result.returncode == 0
    assert result.stdout == "request_failed"
    assert BRIDGE_SECRET not in output
    assert "provider-response-body" not in output


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
    assert timeout_args[:3] == ["--signal=TERM", "--kill-after=300", "14100"]
    assert timeout_args[3] == bash_path(deployment["live"] / "dozorca.sh")
    assert deployment["database"].read_bytes() == database_before
    assert deployment["landing"].read_bytes() == landing_before


def test_successful_bounded_supervisor_records_positive_liveness(deployment):
    deployment["supervisor_success"].unlink()
    deployment["env"]["UVARSI_TIMEOUT_RESULT"] = "0"

    result = run_library(deployment, "uvarsi_run_supervisor_bounded")

    assert result.returncode == 0, result.stdout + result.stderr
    assert deployment["supervisor_success"].read_text().strip() == (
        f"{TODAY} {NOW_EPOCH}"
    )


def test_lock_busy_supervisor_does_not_record_false_success(deployment):
    deployment["supervisor_success"].write_text(
        f"{TODAY} {int(NOW_EPOCH) - 1}\n", encoding="utf-8", newline="\n"
    )
    write_executable(deployment["live"] / "dozorca.sh", "#!/bin/sh\nexit 75\n")
    deployment["env"].update(
        UVARSI_TIMEOUT_RESULT="0",
        UVARSI_TIMEOUT_RUN_COMMAND="1",
    )

    result = run_library(deployment, "uvarsi_run_supervisor_bounded")

    assert result.returncode == 75
    assert not deployment["supervisor_success"].exists()


def test_production_readiness_accepts_three_current_official_reusable_stores(deployment):
    result = run_library(deployment, "uvarsi_require_production_readiness")

    assert result.returncode == 0, result.stdout + result.stderr


def test_production_readiness_rejects_monthly_campaign_in_weekly_receipt(deployment):
    """A long thematic campaign must not make the weekly receipt deploy-ready."""
    monthly_key = "kaufland-monthly-protein-1"
    with sqlite3.connect(deployment["database"]) as con:
        con.execute(
            "INSERT INTO akcie VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                WEEK, "Kaufland", "Fínske chlieb 100 % ražné", 1.55, 2.99,
                "-48 %", "1 ks", official_source_url("Kaufland"), 80,
                monthly_key, "2026-09-01", "2026-09-30", None, None, None,
                None, None,
            ),
        )
        con.execute(
            "UPDATE zber_stav SET pocet=21 WHERE obchod='Kaufland'"
        )

    payload = landing_payload()
    payload["sources"][0].update({
        "source_page": 80,
        "valid_from": "2026-09-01",
        "valid_to": "2026-09-30",
    })
    payload["receipt"]["meals"][0]["items"][0].update({
        "offer_key": monthly_key,
        "name": "Fínske chlieb 100 % ražné",
        "unit": "1 ks",
    })
    deployment["landing"].write_text(json.dumps(payload), encoding="utf-8")

    result = run_library(deployment, "uvarsi_require_production_readiness")

    assert result.returncode != 0


def test_offer_readiness_counts_only_weekly_offers_toward_store_minimum(deployment):
    with sqlite3.connect(deployment["database"]) as con:
        con.execute(
            "UPDATE akcie SET valid_from='2026-09-01', valid_to='2026-09-30' "
            "WHERE obchod='Kaufland' AND offer_key='kaufland-offer-20'"
        )

    result = run_library(deployment, "_uvarsi_require_official_offer_data")

    assert result.returncode != 0


def test_production_readiness_serves_verified_current_data_when_bridge_is_unavailable(
        deployment):
    payload = bridge_payload(release="f" * 12)
    deployment["bridge"].write_text(json.dumps(payload), encoding="utf-8")

    result = run_library(deployment, "uvarsi_require_production_readiness")

    assert result.returncode == 0, result.stdout + result.stderr


def test_bounded_supervisor_skips_bridge_when_verified_current_data_are_ready(
        deployment):
    payload = bridge_payload(release="f" * 12)
    deployment["bridge"].write_text(json.dumps(payload), encoding="utf-8")
    deployment["env"]["UVARSI_TIMEOUT_RESULT"] = "0"

    result = run_library(deployment, "uvarsi_run_supervisor_bounded")

    assert result.returncode == 0, result.stdout + result.stderr


def test_bounded_supervisor_rebuilds_stale_receipt_from_current_offers_without_bridge(
        deployment):
    ready_landing = deployment["state"] / "ready-landing.json"
    shutil.copy2(deployment["landing"], ready_landing)
    stale = landing_payload()
    stale["week"] = "2026-08-31"
    deployment["landing"].write_text(json.dumps(stale), encoding="utf-8")
    with sqlite3.connect(deployment["database"]) as con:
        con.execute(
            "UPDATE zber_stav SET collector_kind='mletaky-aggregator' "
            "WHERE obchod='Tesco'"
        )
    deployment["bridge"].write_text(
        json.dumps(bridge_payload(release="f" * 12)), encoding="utf-8"
    )
    receipt = deployment["state"] / "refresh_receipt.py"
    receipt.write_text(
        "import os, shutil, sys\n"
        "if sys.argv[1] == '--verify-current': raise SystemExit(0)\n"
        "if sys.argv[1] != '--active-current-verified': raise SystemExit(9)\n"
        "def native(path):\n"
        "    return path[1].upper() + ':' + path[2:] if path.startswith('/c/') else path\n"
        "shutil.copy2(native(os.environ['UVARSI_READY_LANDING']), "
        "native(os.environ['UVARSI_LANDING_DATA']))\n",
        encoding="utf-8",
        newline="\n",
    )
    deployment["env"].update({
        "UVARSI_TIMEOUT_RESULT": "0",
        "UVARSI_TIMEOUT_RUN_COMMAND": "1",
        "UVARSI_READY_LANDING": bash_path(ready_landing),
        "UVARSI_RECEIPT_REFRESH": bash_path(receipt),
    })

    result = run_library(deployment, "uvarsi_run_supervisor_bounded")

    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(deployment["landing"].read_text(encoding="utf-8"))["week"] == WEEK
    assert deployment["supervisor_success"].is_file()


def test_bounded_supervisor_falls_back_to_known_current_rows_when_status_is_stale(
        deployment):
    """Free receipt availability must not depend on stale collector bookkeeping."""
    ready_landing = deployment["state"] / "ready-landing.json"
    shutil.copy2(deployment["landing"], ready_landing)
    stale = landing_payload()
    stale["week"] = "2026-08-31"
    deployment["landing"].write_text(json.dumps(stale), encoding="utf-8")
    with sqlite3.connect(deployment["database"]) as con:
        con.execute(
            "UPDATE zber_stav SET source_fingerprint=NULL WHERE obchod='Tesco'"
        )
    receipt_calls = deployment["state"] / "receipt-calls"
    receipt = deployment["state"] / "refresh_receipt.py"
    receipt.write_text(
        "import os, shutil, sys\n"
        "def native(path):\n"
        "    return path[1].upper() + ':' + path[2:] if path.startswith('/c/') else path\n"
        "with open(native(os.environ['UVARSI_RECEIPT_CALLS']), 'a', encoding='utf-8') as f:\n"
        "    f.write(' '.join(sys.argv[1:]) + '\\n')\n"
        "if sys.argv[1] == '--verify-current': raise SystemExit(0)\n"
        "if sys.argv[1] == '--active-current-verified': raise SystemExit(3)\n"
        "if sys.argv[1] != '--active-current': raise SystemExit(9)\n"
        "shutil.copy2(native(os.environ['UVARSI_READY_LANDING']), "
        "native(os.environ['UVARSI_LANDING_DATA']))\n",
        encoding="utf-8",
        newline="\n",
    )
    deployment["env"].update({
        "UVARSI_CODE_DEPLOY": "1",
        "UVARSI_TIMEOUT_RESULT": "0",
        "UVARSI_TIMEOUT_RUN_COMMAND": "1",
        "UVARSI_READY_LANDING": bash_path(ready_landing),
        "UVARSI_RECEIPT_CALLS": bash_path(receipt_calls),
        "UVARSI_RECEIPT_REFRESH": bash_path(receipt),
    })

    result = run_library(deployment, "uvarsi_run_supervisor_bounded")

    assert result.returncode == 0, result.stdout + result.stderr
    calls = receipt_calls.read_text(encoding="utf-8").splitlines()
    assert any(line.startswith("--active-current-verified ") for line in calls)
    assert any(line.startswith("--active-current ") for line in calls)
    assert not deployment["state"].joinpath("curl-args").exists()
    assert json.loads(deployment["landing"].read_text(encoding="utf-8"))["week"] == WEEK
    assert deployment["supervisor_success"].is_file()


def test_bounded_supervisor_rebuilds_receipt_that_references_monthly_campaign(
        deployment):
    ready_landing = deployment["state"] / "ready-landing.json"
    shutil.copy2(deployment["landing"], ready_landing)
    monthly_key = "kaufland-monthly-protein-1"
    with sqlite3.connect(deployment["database"]) as con:
        con.execute(
            "INSERT INTO akcie VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                WEEK, "Kaufland", "Fínske chlieb 100 % ražné", 1.55, 2.99,
                "-48 %", "1 ks", official_source_url("Kaufland"), 80,
                monthly_key, "2026-09-01", "2026-09-30", None, None, None,
                None, None,
            ),
        )
        con.execute("UPDATE zber_stav SET pocet=21 WHERE obchod='Kaufland'")
    stale = landing_payload()
    stale["sources"][0].update({
        "source_page": 80,
        "valid_from": "2026-09-01",
        "valid_to": "2026-09-30",
    })
    stale["receipt"]["meals"][0]["items"][0].update({
        "offer_key": monthly_key,
        "name": "Fínske chlieb 100 % ražné",
        "unit": "1 ks",
    })
    deployment["landing"].write_text(json.dumps(stale), encoding="utf-8")
    receipt = deployment["state"] / "refresh_receipt.py"
    receipt.write_text(
        "import os, shutil, sys\n"
        "if sys.argv[1] != '--active-current-verified': raise SystemExit(9)\n"
        "def native(path):\n"
        "    return path[1].upper() + ':' + path[2:] if path.startswith('/c/') else path\n"
        "shutil.copy2(native(os.environ['UVARSI_READY_LANDING']), "
        "native(os.environ['UVARSI_LANDING_DATA']))\n",
        encoding="utf-8",
        newline="\n",
    )
    deployment["env"].update({
        "UVARSI_TIMEOUT_RESULT": "0",
        "UVARSI_TIMEOUT_RUN_COMMAND": "1",
        "UVARSI_READY_LANDING": bash_path(ready_landing),
        "UVARSI_RECEIPT_REFRESH": bash_path(receipt),
    })

    result = run_library(deployment, "uvarsi_run_supervisor_bounded")

    assert result.returncode == 0, result.stdout + result.stderr
    refreshed = json.loads(deployment["landing"].read_text(encoding="utf-8"))
    assert refreshed["sources"][0]["valid_to"] == "2026-09-13"


def test_bounded_supervisor_checks_bridge_inside_cycle_before_collecting_missing_data(
        deployment):
    with sqlite3.connect(deployment["database"]) as con:
        con.execute(
            "UPDATE zber_stav SET collector_kind='kupino-aggregator' "
            "WHERE obchod='Tesco'"
        )
    payload = bridge_payload(release="f" * 12)
    deployment["bridge"].write_text(json.dumps(payload), encoding="utf-8")

    result = run_library(deployment, "uvarsi_run_supervisor_bounded")

    assert result.returncode != 0
    assert deployment["state"].joinpath("timeout-args").exists()


def test_production_readiness_accepts_multiple_auditable_sources_per_store(deployment):
    payload = landing_payload()
    payload["sources"].append({
        "store": "Kaufland",
        "url": official_source_url("Kaufland"),
        "source_page": 2,
        "valid_from": WEEK,
        "valid_to": "2026-09-13",
    })
    payload["receipt"]["meals"][0]["items"].append({
        "offer_key": "kaufland-offer-2",
        "name": "Overená položka Kaufland 2",
        "store": "Kaufland",
        "unit": "1 ks",
        "quantity": 1,
        "price": "1,99",
        "original_price": "2,49",
        "savings": "0,50",
        "off": "-20 %",
    })
    payload["receipt"].update(
        nakup_spolu="7,52",
        bezne="10,46",
        usetris="2,94",
        polozky=4,
        polozky_s_beznou_cenou=4,
    )
    deployment["landing"].write_text(json.dumps(payload), encoding="utf-8")

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


def test_production_readiness_rejects_claimed_official_kind_with_aggregator_urls(
        deployment):
    with sqlite3.connect(deployment["database"]) as con:
        for table in ("akcie", "akcie_staging"):
            con.execute(
                f"UPDATE {table} SET source_url=? WHERE obchod='Tesco'",
                ("https://www.kupino.sk/letak/tesco-current",),
            )

    result = run_library(deployment, "uvarsi_require_production_readiness")

    assert result.returncode != 0


def test_production_readiness_ignores_an_abandoned_staging_fingerprint(deployment):
    with sqlite3.connect(deployment["database"]) as con:
        con.execute(
            "UPDATE zber_staging_stav SET source_fingerprint=? WHERE obchod='Tesco'",
            ("f" * 64,),
        )

    result = run_library(deployment, "uvarsi_require_production_readiness")

    assert result.returncode == 0, result.stdout + result.stderr


def test_production_readiness_rejects_an_invalid_active_fingerprint(deployment):
    with sqlite3.connect(deployment["database"]) as con:
        con.execute(
            "UPDATE zber_stav SET source_fingerprint=? WHERE obchod='Tesco'",
            ("not-a-sha256",),
        )

    result = run_library(deployment, "uvarsi_require_production_readiness")

    assert result.returncode != 0


def test_production_readiness_requires_a_current_three_meal_receipt(deployment):
    payload = landing_payload()
    payload["sources"][0]["valid_to"] = "2026-09-10"
    deployment["landing"].write_text(json.dumps(payload), encoding="utf-8")

    result = run_library(deployment, "uvarsi_require_production_readiness")

    assert result.returncode != 0


def test_production_readiness_requires_current_valid_from_too(deployment):
    payload = landing_payload()
    payload["sources"][0]["valid_from"] = "2026-09-12"
    deployment["landing"].write_text(json.dumps(payload), encoding="utf-8")

    result = run_library(deployment, "uvarsi_require_production_readiness")

    assert result.returncode != 0


def test_production_readiness_rejects_empty_or_nonpositive_receipt(deployment):
    payload = landing_payload()
    payload["receipt"]["meals"][0]["items"] = []
    deployment["landing"].write_text(json.dumps(payload), encoding="utf-8")
    assert run_library(deployment, "uvarsi_require_production_readiness").returncode != 0

    payload = landing_payload()
    payload["receipt"]["meals"][0]["items"][0]["price"] = "0,00"
    deployment["landing"].write_text(json.dumps(payload), encoding="utf-8")
    assert run_library(deployment, "uvarsi_require_production_readiness").returncode != 0

    payload = landing_payload()
    payload["receipt"].update(nakup_spolu="0,00", bezne="0,00", usetris="0,00")
    deployment["landing"].write_text(json.dumps(payload), encoding="utf-8")
    assert run_library(deployment, "uvarsi_require_production_readiness").returncode != 0


def test_production_readiness_rejects_consistent_but_wrong_repkovy_olej_price(
        deployment):
    payload = landing_payload()
    oil = payload["receipt"]["meals"][0]["items"][0]
    oil.update(price="0,07", original_price="0,13", savings="0,06")
    payload["receipt"].update(
        nakup_spolu="4,05", bezne="5,11", usetris="1,06"
    )
    deployment["landing"].write_text(json.dumps(payload), encoding="utf-8")

    result = run_library(deployment, "uvarsi_require_production_readiness")

    assert result.returncode != 0


def weighted_receipt_payload(deployment):
    with sqlite3.connect(deployment["database"]) as con:
        for table in ("akcie", "akcie_staging"):
            con.execute(
                f"""UPDATE {table}
                    SET nazov=?, cena=?, povodna=?, zlava=?, jednotka=?,
                        cena_s_kartou=?, zlava_s_kartou=?, vernostny_program=?,
                        podmienka_s_kartou=?
                    WHERE obchod='Tesco' AND offer_key='tesco-offer-1'""",
                (
                    "Kuracie prsia na váhu", 5.15, 6.99, "-26 %", "kg",
                    4.75, "-32 %", "Clubcard", "iba s Clubcard",
                ),
            )

    payload = landing_payload()
    chicken = payload["receipt"]["meals"][1]["items"][0]
    chicken.update(
        name="Kuracie prsia na váhu",
        unit="kg",
        price="6,18",
        original_price="8,39",
        savings="2,21",
        off="-26 %",
        loyalty_price="5,70",
        loyalty_discount="-32 %",
        weight_multiplier="1.2",
    )
    payload["receipt"].update(
        nakup_spolu="9,72", bezne="13,87", usetris="4,15"
    )
    return payload


def test_production_readiness_accepts_exact_weighted_line_totals(deployment):
    deployment["landing"].write_text(
        json.dumps(weighted_receipt_payload(deployment)), encoding="utf-8"
    )

    result = run_library(deployment, "uvarsi_require_production_readiness")

    assert result.returncode == 0, result.stdout + result.stderr


def test_production_readiness_rejects_wrong_weighted_subtotal(deployment):
    payload = weighted_receipt_payload(deployment)
    chicken = payload["receipt"]["meals"][1]["items"][0]
    chicken.update(price="6,17", savings="2,22")
    payload["receipt"].update(nakup_spolu="9,71", usetris="4,16")
    deployment["landing"].write_text(json.dumps(payload), encoding="utf-8")

    result = run_library(deployment, "uvarsi_require_production_readiness")

    assert result.returncode != 0


def test_production_readiness_rejects_weighted_line_without_multiplier(deployment):
    payload = weighted_receipt_payload(deployment)
    del payload["receipt"]["meals"][1]["items"][0]["weight_multiplier"]
    deployment["landing"].write_text(json.dumps(payload), encoding="utf-8")

    result = run_library(deployment, "uvarsi_require_production_readiness")

    assert result.returncode != 0


def test_production_readiness_rejects_proportionally_tampered_weighted_prices(
        deployment):
    payload = weighted_receipt_payload(deployment)
    chicken = payload["receipt"]["meals"][1]["items"][0]
    chicken.update(
        price="6,70",
        original_price="9,09",
        savings="2,39",
        loyalty_price="6,18",
    )
    payload["receipt"].update(
        nakup_spolu="10,24", bezne="14,57", usetris="4,33"
    )
    deployment["landing"].write_text(json.dumps(payload), encoding="utf-8")

    result = run_library(deployment, "uvarsi_require_production_readiness")

    assert result.returncode != 0


@pytest.mark.parametrize(
    "mutation",
    ["original_price", "loyalty_price", "quantity", "unit", "validity"],
)
def test_production_readiness_rejects_wrong_weighted_offer_facts(
        deployment, mutation):
    payload = weighted_receipt_payload(deployment)
    chicken = payload["receipt"]["meals"][1]["items"][0]
    if mutation == "original_price":
        chicken.update(original_price="8,38", savings="2,20")
        payload["receipt"].update(bezne="13,86", usetris="4,14")
    elif mutation == "loyalty_price":
        chicken["loyalty_price"] = "5,69"
    elif mutation == "quantity":
        chicken["quantity"] = 2
    elif mutation == "unit":
        chicken["unit"] = "500 g"
    else:
        payload["sources"][1]["valid_to"] = "2026-09-14"
    deployment["landing"].write_text(json.dumps(payload), encoding="utf-8")

    result = run_library(deployment, "uvarsi_require_production_readiness")

    assert result.returncode != 0


@pytest.mark.parametrize(
    ("field", "wrong_value"),
    [
        ("name", "Iný výrobok"),
        ("unit", "500 ml"),
        ("off", "-99 %"),
        ("loyalty_condition", "bez Clubcard"),
        ("loyalty_price", "0,01"),
    ],
)
def test_production_readiness_rejects_customer_facts_not_matching_offer_row(
        deployment, field, wrong_value):
    payload = landing_payload()
    item = (
        payload["receipt"]["meals"][1]["items"][0]
        if field.startswith("loyalty_")
        else payload["receipt"]["meals"][0]["items"][0]
    )
    item[field] = wrong_value
    deployment["landing"].write_text(json.dumps(payload), encoding="utf-8")

    result = run_library(deployment, "uvarsi_require_production_readiness")

    assert result.returncode != 0


def test_production_readiness_binds_line_price_to_quantity(deployment):
    payload = landing_payload()
    payload["receipt"]["meals"][0]["items"][0]["quantity"] = 2
    deployment["landing"].write_text(json.dumps(payload), encoding="utf-8")

    result = run_library(deployment, "uvarsi_require_production_readiness")

    assert result.returncode != 0


@pytest.mark.parametrize("missing_field", ["offer_key", "source_page", "url"])
def test_production_readiness_requires_offer_and_source_references(
        deployment, missing_field):
    payload = landing_payload()
    if missing_field == "offer_key":
        del payload["receipt"]["meals"][0]["items"][0][missing_field]
    else:
        del payload["sources"][0][missing_field]
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


def test_production_readiness_requires_recent_supervisor_success_and_schedule(deployment):
    deployment["supervisor_success"].unlink()
    no_liveness = run_library(deployment, "uvarsi_require_production_readiness")
    assert no_liveness.returncode != 0

    deployment["supervisor_success"].write_text(
        f"{TODAY} {int(NOW_EPOCH) - 18001}\n", encoding="utf-8", newline="\n"
    )
    stale_liveness = run_library(deployment, "uvarsi_require_production_readiness")
    assert stale_liveness.returncode != 0

    deployment["supervisor_success"].write_text(
        f"{TODAY} {NOW_EPOCH}\n", encoding="utf-8", newline="\n"
    )
    deployment["cron"].write_text(
        f"{TAKTIK_CRON}\n0 5-21 * * * /opt/uvarsi/dozorca.sh\n",
        encoding="utf-8",
    )
    unsafe_schedule = run_library(deployment, "uvarsi_require_production_readiness")
    assert unsafe_schedule.returncode != 0


def test_supervisor_schedule_install_migrates_only_uvarsi_row_and_preserves_crontab(
        deployment):
    old_supervisor = "0 5-21 * * * /opt/uvarsi/dozorca.sh >> /var/log/uvarsi.log 2>&1"
    unrelated = "17 2 * * * /opt/other/report.sh"
    deployment["cron"].write_text(
        f"# keep comments\n{TAKTIK_CRON}\n{unrelated}\n{old_supervisor}\n",
        encoding="utf-8",
    )
    snapshot = deployment["state"] / "schedule-snapshot"
    snapshot.mkdir()

    installed = run_library(
        deployment,
        f'uvarsi_snapshot_supervisor_schedule "{bash_path(snapshot)}"\n'
        "uvarsi_install_supervisor_schedule",
    )
    assert installed.returncode == 0, installed.stdout + installed.stderr
    installed_lines = deployment["cron"].read_text().splitlines()
    assert "# keep comments" in installed_lines
    assert TAKTIK_CRON in installed_lines
    assert unrelated in installed_lines
    assert old_supervisor not in installed_lines
    assert installed_lines.count(SUPERVISOR_CRON) == 1
    assert deployment["state"].joinpath("crontab-written").exists()
    assert deployment["state"].joinpath("cron-lock-acquired").exists()

    canonical = f"{TAKTIK_CRON}\n{SUPERVISOR_CRON}\n"
    deployment["cron"].write_text(canonical, encoding="utf-8", newline="\n")
    deployment["state"].joinpath("crontab-written").unlink()
    verified = run_library(deployment, "uvarsi_install_supervisor_schedule")
    assert verified.returncode == 0, verified.stdout + verified.stderr
    assert deployment["cron"].read_text(encoding="utf-8") == canonical
    assert not deployment["state"].joinpath("crontab-written").exists()

    restored = run_library(
        deployment,
        f'uvarsi_restore_supervisor_schedule "{bash_path(snapshot)}"',
    )
    assert restored.returncode == 0, restored.stdout + restored.stderr
    restored_lines = deployment["cron"].read_text(encoding="utf-8").splitlines()
    assert TAKTIK_CRON in restored_lines
    assert old_supervisor in restored_lines
    assert SUPERVISOR_CRON not in restored_lines
    assert deployment["state"].joinpath("crontab-written").exists()


def test_crontab_rollback_restores_only_supervisor_and_preserves_concurrent_changes(
        deployment):
    removed_during_deploy = "17 2 * * * /opt/other/retired-report.sh"
    old_supervisor = "0 5-21 * * * /opt/uvarsi/dozorca.sh"
    old_backup = "12 3 * * * /opt/uvarsi/zaloha.sh"
    original = (
        "# Original crontab\n"
        f"{TAKTIK_CRON}\n"
        f"{removed_during_deploy}\n"
        f"{old_supervisor}\n"
        f"{old_backup}\n"
    )
    deployment["cron"].write_text(original, encoding="utf-8", newline="\n")
    snapshot = deployment["state"] / "complete-cron-snapshot"
    snapshot.mkdir()

    snap = run_library(
        deployment,
        f'uvarsi_snapshot_supervisor_schedule "{bash_path(snapshot)}"',
    )
    assert snap.returncode == 0, snap.stdout + snap.stderr
    concurrent_addition = "0 * * * * /opt/other/new-during-deploy.sh"
    current = (
        "# Current crontab\n"
        f"{TAKTIK_CRON}\n"
        f"{concurrent_addition}\n"
        f"{SUPERVISOR_CRON}\n"
        "30 3 * * * /opt/uvarsi/zaloha.sh\n"
        "5 * * * * cd /opt/uvarsi/app && "
        "/opt/uvarsi/venv/bin/python rekonciliacia.py\n"
    )
    deployment["cron"].write_text(
        current,
        encoding="utf-8",
        newline="\n",
    )

    restored = run_library(
        deployment,
        f'uvarsi_restore_supervisor_schedule "{bash_path(snapshot)}"',
    )

    assert restored.returncode == 0, restored.stdout + restored.stderr
    restored_lines = deployment["cron"].read_text(encoding="utf-8").splitlines()
    assert "# Current crontab" in restored_lines
    assert TAKTIK_CRON in restored_lines
    assert concurrent_addition in restored_lines
    assert removed_during_deploy not in restored_lines
    assert old_supervisor in restored_lines
    assert old_backup not in restored_lines
    assert SUPERVISOR_CRON not in restored_lines
    assert sum("rekonciliacia.py" in line for line in restored_lines) == 1
    assert deployment["state"].joinpath("crontab-written").exists()


def test_crontab_rollback_fails_closed_when_current_crontab_cannot_be_read(
        deployment):
    original = f"{TAKTIK_CRON}\n0 5-21 * * * /opt/uvarsi/dozorca.sh\n"
    deployment["cron"].write_text(original, encoding="utf-8", newline="\n")
    snapshot = deployment["state"] / "rollback-read-error-snapshot"
    snapshot.mkdir()
    snap = run_library(
        deployment,
        f'uvarsi_snapshot_supervisor_schedule "{bash_path(snapshot)}"',
    )
    assert snap.returncode == 0, snap.stdout + snap.stderr
    current = f"{TAKTIK_CRON}\n{SUPERVISOR_CRON}\n"
    deployment["cron"].write_text(current, encoding="utf-8", newline="\n")
    deployment["state"].joinpath("fail-crontab-list").touch()

    restored = run_library(
        deployment,
        f'uvarsi_restore_supervisor_schedule "{bash_path(snapshot)}"',
    )

    assert restored.returncode != 0
    assert deployment["cron"].read_text(encoding="utf-8") == current
    assert not deployment["state"].joinpath("crontab-written").exists()


def test_complete_schedule_install_fails_closed_on_crontab_read_error(deployment):
    original = f"{TAKTIK_CRON}\n17 2 * * * /opt/other/report.sh\n"
    deployment["cron"].write_text(original, encoding="utf-8", newline="\n")
    deployment["state"].joinpath("fail-crontab-list").touch()

    result = run_library(deployment, "uvarsi_install_production_schedule")

    assert result.returncode not in (0, 127)
    assert deployment["cron"].read_text(encoding="utf-8") == original


def test_complete_schedule_install_only_verifies_existing_schedule(deployment):
    unrelated = "17 2 * * * /opt/other/report.sh"
    deployment["cron"].write_text(
        f"# keep comments\n{TAKTIK_CRON}\n{unrelated}\n{SUPERVISOR_CRON}\n"
        f"{BACKUP_CRON}\n{PAYMENT_CRON}\n",
        encoding="utf-8",
        newline="\n",
    )

    result = run_library(deployment, "uvarsi_install_production_schedule")

    assert result.returncode == 0, result.stdout + result.stderr
    installed = deployment["cron"].read_text(encoding="utf-8").splitlines()
    assert "# keep comments" in installed
    assert TAKTIK_CRON in installed
    assert unrelated in installed
    assert installed.count(SUPERVISOR_CRON) == 1
    assert sum("/opt/uvarsi/zaloha.sh" in line for line in installed) == 1
    assert sum("rekonciliacia.py" in line for line in installed) == 1
    assert not deployment["state"].joinpath("crontab-written").exists()


def test_complete_schedule_rejects_noncanonical_duplicate_uvarsi_jobs(deployment):
    deployment["cron"].write_text(
        f"{TAKTIK_CRON}\n{SUPERVISOR_CRON}\n{BACKUP_CRON}\n{PAYMENT_CRON}\n"
        "45 2 * * * /opt/uvarsi/zaloha.sh >> /var/log/old-backup.log 2>&1\n"
        "10 * * * * cd /opt/uvarsi/app && "
        "/opt/uvarsi/venv/bin/python rekonciliacia.py >> /var/log/old-payments.log 2>&1\n",
        encoding="utf-8",
        newline="\n",
    )

    result = run_library(deployment, "uvarsi_install_production_schedule")

    assert result.returncode != 0
    assert not deployment["state"].joinpath("crontab-written").exists()


def test_supervisor_schedule_never_overwrites_crontab_after_a_read_error(deployment):
    original = f"{TAKTIK_CRON}\n0 5-21 * * * /opt/uvarsi/dozorca.sh\n"
    deployment["cron"].write_text(original, encoding="utf-8")
    deployment["state"].joinpath("fail-crontab-list").touch()

    result = run_library(deployment, "uvarsi_install_supervisor_schedule")

    assert result.returncode != 0
    assert deployment["cron"].read_text(encoding="utf-8") == original


def test_supervisor_schedule_restores_previous_uvarsi_row_when_verification_fails(
        deployment):
    old_supervisor = "0 5-21 * * * /opt/uvarsi/dozorca.sh"
    unrelated = "17 2 * * * /opt/other/report.sh"
    original = f"{TAKTIK_CRON}\n{unrelated}\n{old_supervisor}\n"
    deployment["cron"].write_text(original, encoding="utf-8", newline="\n")
    deployment["state"].joinpath("fail-crontab-list-after-write").touch()

    result = run_library(deployment, "uvarsi_install_supervisor_schedule")

    assert result.returncode != 0
    restored = deployment["cron"].read_text(encoding="utf-8").splitlines()
    assert TAKTIK_CRON in restored
    assert unrelated in restored
    assert old_supervisor in restored
    assert SUPERVISOR_CRON not in restored


@pytest.mark.parametrize("initial_state", ["stale-receipt", "aggregator-provenance"])
def test_bootstrap_runs_bounded_collector_before_strict_readiness(
        deployment, initial_state):
    ready_database = deployment["state"] / "ready.db"
    ready_landing = deployment["state"] / "ready-landing.json"
    shutil.copy2(deployment["database"], ready_database)
    shutil.copy2(deployment["landing"], ready_landing)
    if initial_state == "aggregator-provenance":
        with sqlite3.connect(deployment["database"]) as con:
            con.execute(
                "UPDATE zber_stav SET collector_kind='kupino-aggregator' "
                "WHERE obchod='Tesco'"
            )
    else:
        stale = landing_payload()
        stale["week"] = "2026-08-31"
        deployment["landing"].write_text(json.dumps(stale), encoding="utf-8")
    deployment["supervisor_success"].unlink()
    collector = deployment["state"] / "official_collector.py"
    collector.write_text(
        "import os, shutil\n"
        "def native(path):\n"
        "    return path[1].upper() + ':' + path[2:] if path.startswith('/c/') else path\n"
        "shutil.copy2(native(os.environ['UVARSI_READY_DB']), native(os.environ['UVARSI_DB']))\n"
        "shutil.copy2(native(os.environ['UVARSI_READY_LANDING']), "
        "native(os.environ['UVARSI_LANDING_DATA']))\n",
        encoding="utf-8",
        newline="\n",
    )
    receipt = deployment["state"] / "refresh_receipt.py"
    receipt.write_text(
        "import os, shutil, sys\n"
        "if sys.argv[1] == '--active-current-verified': raise SystemExit(9)\n"
        "def native(path):\n"
        "    return path[1].upper() + ':' + path[2:] if path.startswith('/c/') else path\n"
        "shutil.copy2(native(os.environ['UVARSI_READY_LANDING']), "
        "native(os.environ['UVARSI_LANDING_DATA']))\n",
        encoding="utf-8",
        newline="\n",
    )
    deployment["env"].update({
        "UVARSI_TIMEOUT_RESULT": "0",
        "UVARSI_TIMEOUT_RUN_COMMAND": "1",
        "UVARSI_READY_DB": bash_path(ready_database),
        "UVARSI_READY_LANDING": bash_path(ready_landing),
        "UVARSI_COLLECTOR": bash_path(collector),
        "UVARSI_RECEIPT_REFRESH": bash_path(receipt),
    })
    assert run_library(
        deployment, "uvarsi_require_production_readiness"
    ).returncode != 0

    result = run_library(deployment, "uvarsi_bootstrap_production_readiness")

    assert result.returncode == 0, result.stdout + result.stderr
    assert "PLATBY_ZAPNUTE=0" in deployment["env_file"].read_text()
    assert deployment["supervisor_success"].is_file()
    timeout_args = deployment["state"].joinpath("timeout-args").read_text().splitlines()
    assert timeout_args[:3] == ["--signal=TERM", "--kill-after=300", "14100"]
    assert timeout_args[-1] == "internal-supervisor-cycle"


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
