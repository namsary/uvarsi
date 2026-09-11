#!/bin/bash
# Shared, testable deployment state handling for the Uvar.si app and plan worker.
# This file deliberately knows nothing about Caddy or any other hosted app.

UVARSI_DIR="${UVARSI_DIR:-/opt/uvarsi}"
UVARSI_SYSTEMD_DIR="${UVARSI_SYSTEMD_DIR:-/etc/systemd/system}"
UVARSI_SYSTEMCTL="${UVARSI_SYSTEMCTL:-systemctl}"
UVARSI_CURL="${UVARSI_CURL:-curl}"
UVARSI_HEALTH_PY="${UVARSI_HEALTH_PY:-$UVARSI_DIR/venv/bin/python}"
UVARSI_SLEEP="${UVARSI_SLEEP:-sleep}"
UVARSI_CP="${UVARSI_CP:-cp}"
UVARSI_MV="${UVARSI_MV:-mv}"
UVARSI_ATOMIC_EXCHANGE="${UVARSI_ATOMIC_EXCHANGE:-}"
UVARSI_HEARTBEAT_ATTEMPTS="${UVARSI_HEARTBEAT_ATTEMPTS:-30}"
UVARSI_HEALTH_URL="${UVARSI_HEALTH_URL:-http://127.0.0.1:8090/api/health}"
UVARSI_DB="${UVARSI_DB:-$UVARSI_DIR/uvarsi.db}"
UVARSI_ENV_FILE="${UVARSI_ENV_FILE:-$UVARSI_DIR/uvarsi.env}"
UVARSI_WEB_DIR="${UVARSI_WEB_DIR:-/var/www/uvarsi}"
UVARSI_APP_DIR="${UVARSI_APP_DIR:-$UVARSI_DIR/app}"
UVARSI_LANDING_DATA="${UVARSI_LANDING_DATA:-/var/lib/uvarsi/landing_data.json}"
UVARSI_TIMEOUT="${UVARSI_TIMEOUT:-timeout}"
UVARSI_SUPERVISOR="${UVARSI_SUPERVISOR:-$UVARSI_DIR/dozorca.sh}"
UVARSI_SUPERVISOR_STATE="${UVARSI_SUPERVISOR_STATE:-$UVARSI_DIR/.dozorca_state}"
UVARSI_COLLECTION_FAILURE_STATE="${UVARSI_COLLECTION_FAILURE_STATE:-$UVARSI_DIR/.collection_failure_state}"
UVARSI_TAKTIK_URL="${UVARSI_TAKTIK_URL:-https://mapa.89.167.72.159.sslip.io/}"
UVARSI_MAX_COLLECTION_SECONDS="${UVARSI_MAX_COLLECTION_SECONDS:-14400}"
UVARSI_WORKER_UNIT="$UVARSI_SYSTEMD_DIR/uvarsi-plan-worker.service"
UVARSI_APP_UNIT="$UVARSI_SYSTEMD_DIR/uvarsi.service"

_uvarsi_today() {
  if [ -n "${UVARSI_TODAY:-}" ]; then
    printf '%s' "$UVARSI_TODAY"
  else
    TZ=Europe/Bratislava date +%F
  fi
}

_uvarsi_env_value() {
  key=$1
  [ -f "$UVARSI_ENV_FILE" ] || return 1
  "$UVARSI_HEALTH_PY" -c '
import re, sys
path, wanted = sys.argv[1:3]
matches = []
with open(path, encoding="utf-8") as handle:
    for raw in handle:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            continue
        name, value = line.split("=", 1)
        if name.strip() != wanted:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"\047":
            value = value[1:-1]
        matches.append(value)
if len(matches) != 1 or not matches[0] or re.search(r"[\x00-\x1f\x7f]", matches[0]):
    raise SystemExit(1)
print(matches[0], end="")
' "$UVARSI_ENV_FILE" "$key" 2>/dev/null
}

uvarsi_require_tesco_bridge() {
  # Values are read without sourcing or printing the env file. The bearer
  # header reaches curl over stdin config, so it is absent from argv and logs.
  environment=$(_uvarsi_env_value UVARSI_ENV) || return 1
  [ "$environment" = production ] || return 1
  bridge_url=$(_uvarsi_env_value UVARSI_TESCO_BRIDGE_URL) || return 1
  bridge_secret=$(_uvarsi_env_value UVARSI_TESCO_BRIDGE_SECRET) || return 1
  [ "${#bridge_secret}" -ge 32 ] || return 1
  case "$bridge_secret" in *[!A-Za-z0-9._~-]*) return 1 ;; esac

  "$UVARSI_HEALTH_PY" -c '
import sys
from urllib.parse import urlsplit
try:
    parsed = urlsplit(sys.argv[1])
    port = parsed.port
except ValueError:
    raise SystemExit(1)
valid = (
    parsed.scheme == "https" and bool(parsed.hostname)
    and parsed.username is None and parsed.password is None and port is None
    and parsed.path in ("", "/") and not parsed.query and not parsed.fragment
)
raise SystemExit(0 if valid else 1)
' "$bridge_url" >/dev/null 2>&1 || return 1
  bridge_url=${bridge_url%/}
  today=$(_uvarsi_today) || return 1
  response=$(mktemp "${TMPDIR:-/tmp}/uvarsi-bridge.XXXXXX") || return 1
  chmod 600 "$response" || { rm -f "$response"; return 1; }
  request=$(printf '{"date":"%s","format":"HM"}' "$today")
  if ! {
    printf 'header = "Accept: application/json"\n'
    printf 'header = "Content-Type: application/json"\n'
    printf 'header = "Authorization: Bearer %s"\n' "$bridge_secret"
  } | "$UVARSI_CURL" --disable --config - --silent --show-error --fail \
      --max-time 30 --request POST --data-binary "$request" \
      --output "$response" "$bridge_url/v1/tesco/leaflets" \
      >/dev/null 2>&1; then
    rm -f "$response"
    return 1
  fi
  if ! "$UVARSI_HEALTH_PY" -c '
import datetime as dt, json, sys
from urllib.parse import urlsplit
path, today_raw, bridge_url = sys.argv[1:4]
try:
    today = dt.date.fromisoformat(today_raw)
    with open(path, encoding="utf-8") as handle:
        payload = json.load(handle)
    leaflet = payload["leaflet"]
    start = dt.date.fromisoformat(leaflet["valid_from"])
    end = dt.date.fromisoformat(leaflet["valid_to"])
    pages = leaflet["pages"]
except (KeyError, TypeError, ValueError, json.JSONDecodeError, OSError):
    raise SystemExit(1)
if (
    not isinstance(leaflet, dict) or leaflet.get("country") != "sk"
    or leaflet.get("format") != "HM" or not start <= today <= end
    or not isinstance(pages, list) or not 8 <= len(pages) <= 120
    or leaflet.get("declared_pages") != len(pages)
):
    raise SystemExit(1)
origin = urlsplit(bridge_url)
for expected, page in enumerate(pages, start=1):
    if not isinstance(page, dict) or page.get("source_page") != expected:
        raise SystemExit(1)
    for field in ("thumbnail_url", "image_url"):
        value = page.get(field)
        if not isinstance(value, str):
            raise SystemExit(1)
        parsed = urlsplit(value)
        if (
            parsed.scheme != origin.scheme or parsed.hostname != origin.hostname
            or parsed.username is not None or parsed.password is not None
            or parsed.port is not None or parsed.query or parsed.fragment
            or not parsed.path.startswith("/v1/tesco/media/")
        ):
            raise SystemExit(1)
' "$response" "$today" "$bridge_url" >/dev/null 2>&1; then
    rm -f "$response"
    return 1
  fi
  rm -f "$response" || return 1
  UVARSI_TESCO_BRIDGE_URL=$bridge_url
  UVARSI_TESCO_BRIDGE_SECRET=$bridge_secret
  UVARSI_ENV=production
  export UVARSI_TESCO_BRIDGE_URL UVARSI_TESCO_BRIDGE_SECRET UVARSI_ENV
}

_uvarsi_require_collection_readiness() {
  today=$(_uvarsi_today) || return 1
  (
    cd "$UVARSI_APP_DIR" || exit 1
    "$UVARSI_HEALTH_PY" -c '
import datetime as dt, json, re, sqlite3, sys
from landing_data import validate_landing_data

database, landing_path, today_raw = sys.argv[1:4]
today = dt.date.fromisoformat(today_raw)
week = (today - dt.timedelta(days=today.weekday())).isoformat()
stores = {
    "Kaufland": "official-kaufland-offers",
    "Tesco": "official-tesco-viewer",
    "Lidl": "official-lidl-viewer",
}
fingerprint = re.compile(r"[0-9a-f]{64}")
with sqlite3.connect("file:" + database + "?mode=ro", uri=True) as con:
    required = {
        "tyzden", "obchod", "stav", "pocet", "data_version",
        "collector_kind", "source_fingerprint", "valid_from", "valid_to",
    }
    for table in ("zber_stav", "zber_staging_stav"):
        columns = {row[1] for row in con.execute("PRAGMA table_info(" + table + ")")}
        if not required <= columns:
            raise SystemExit(1)
    for store, expected_kind in stores.items():
        query = (
            "SELECT stav,pocet,data_version,collector_kind,source_fingerprint,"
            "valid_from,valid_to FROM {} WHERE tyzden=? AND obchod=?"
        )
        active = con.execute(query.format("zber_stav"), (week, store)).fetchone()
        staged = con.execute(query.format("zber_staging_stav"), (week, store)).fetchone()
        if active is None or staged is None or tuple(active) != tuple(staged):
            raise SystemExit(1)
        status, declared, version, kind, source_hash, start, end = active
        try:
            current = dt.date.fromisoformat(start) <= today <= dt.date.fromisoformat(end)
        except (TypeError, ValueError):
            raise SystemExit(1)
        if (
            status != "ok" or int(declared or 0) < 20 or int(version or 0) < 2
            or kind != expected_kind or not isinstance(source_hash, str)
            or fingerprint.fullmatch(source_hash) is None or not current
        ):
            raise SystemExit(1)
        for table in ("akcie", "akcie_staging"):
            count = con.execute(
                "SELECT COUNT(*) FROM " + table +
                " WHERE tyzden=? AND obchod=? AND valid_from<=? AND ?<=valid_to",
                (week, store, today_raw, today_raw),
            ).fetchone()[0]
            if int(count or 0) < 20 or int(count) != int(declared):
                raise SystemExit(1)

with open(landing_path, encoding="utf-8") as handle:
    landing = json.load(handle)
validate_landing_data(landing, today, required_offer_data_version=2)
meals = landing.get("receipt", {}).get("meals")
if not isinstance(meals, list) or len(meals) != 3:
    raise SystemExit(1)
source_stores = {
    source.get("store") for source in landing.get("sources", [])
    if isinstance(source, dict)
}
if source_stores != set(stores):
    raise SystemExit(1)
' "$UVARSI_DB" "$UVARSI_LANDING_DATA" "$today" >/dev/null 2>&1
  )
}

_uvarsi_require_runtime_health() {
  "$UVARSI_SYSTEMCTL" is-active --quiet uvarsi || return 1
  "$UVARSI_SYSTEMCTL" is-active --quiet uvarsi-plan-worker || return 1
  [ -x "$UVARSI_SUPERVISOR" ] || return 1
  today=$(_uvarsi_today) || return 1
  if [ -s "$UVARSI_SUPERVISOR_STATE" ]; then
    read -r failed_day _rest < "$UVARSI_SUPERVISOR_STATE" || return 1
    [ "$failed_day" != "$today" ] || return 1
  fi
  [ ! -s "$UVARSI_COLLECTION_FAILURE_STATE" ] || return 1

  health=$(mktemp "${TMPDIR:-/tmp}/uvarsi-health.XXXXXX") || return 1
  chmod 600 "$health" || { rm -f "$health"; return 1; }
  if ! "$UVARSI_CURL" --silent --show-error --fail --max-time 5 \
      --output "$health" "$UVARSI_HEALTH_URL" >/dev/null 2>&1; then
    rm -f "$health"
    return 1
  fi
  if ! "$UVARSI_HEALTH_PY" -c '
import json, sys
try:
    with open(sys.argv[1], encoding="utf-8") as handle:
        payload = json.load(handle)
    queue = payload["plan_queue"]
    engine = payload["recipe_engine"]
except (KeyError, TypeError, ValueError, OSError, json.JSONDecodeError):
    raise SystemExit(1)
heartbeat = queue.get("heartbeat_seconds")
if (
    queue.get("worker_alive") is not True
    or not isinstance(heartbeat, int) or isinstance(heartbeat, bool)
    or heartbeat < 0 or heartbeat > 60
    or engine.get("payments_enabled") is not False
):
    raise SystemExit(1)
' "$health" >/dev/null 2>&1; then
    rm -f "$health"
    return 1
  fi
  rm -f "$health" || return 1
  "$UVARSI_CURL" --silent --show-error --fail --max-time 10 \
    --output /dev/null "$UVARSI_TAKTIK_URL" >/dev/null 2>&1
}

uvarsi_require_production_readiness() {
  # Every call is quiet: callers report stable reason codes, never response
  # bodies, bearer headers or environment values.
  uvarsi_require_payments_off || return 1
  uvarsi_require_tesco_bridge || return 1
  uvarsi_require_runtime_payments_off || return 1
  _uvarsi_require_collection_readiness || return 1
  _uvarsi_require_runtime_health
}

uvarsi_run_supervisor_bounded() {
  # The collector and receipt writer stage their candidate state. Killing this
  # wrapper on timeout therefore leaves the live DB rows and landing JSON as-is.
  uvarsi_require_payments_off || return 1
  uvarsi_require_tesco_bridge || return 1
  case "$UVARSI_MAX_COLLECTION_SECONDS" in
    ''|*[!0-9]*) return 1 ;;
  esac
  [ "$UVARSI_MAX_COLLECTION_SECONDS" -gt 0 ] || return 1
  [ "$UVARSI_MAX_COLLECTION_SECONDS" -le 14400 ] || return 1
  [ -x "$UVARSI_SUPERVISOR" ] || return 1
  if "$UVARSI_TIMEOUT" --signal=TERM --kill-after=300 \
      "$UVARSI_MAX_COLLECTION_SECONDS" "$UVARSI_SUPERVISOR"; then
    result=0
  else
    result=$?
  fi
  uvarsi_require_payments_off || return 1
  return "$result"
}

_uvarsi_exchange_directories() {
  first=$1
  second=$2
  if [ -n "$UVARSI_ATOMIC_EXCHANGE" ]; then
    "$UVARSI_ATOMIC_EXCHANGE" "$first" "$second"
    return
  fi
  "$UVARSI_HEALTH_PY" -c '
import ctypes, os, sys
first, second = map(os.fsencode, sys.argv[1:3])
libc = ctypes.CDLL(None, use_errno=True)
renameat2 = getattr(libc, "renameat2", None)
if renameat2 is None:
    raise RuntimeError("renameat2 is unavailable")
renameat2.argtypes = (
    ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint,
)
renameat2.restype = ctypes.c_int
AT_FDCWD = -100
RENAME_EXCHANGE = 2
if renameat2(AT_FDCWD, first, AT_FDCWD, second, RENAME_EXCHANGE) != 0:
    error = ctypes.get_errno()
    raise OSError(error, os.strerror(error))
' "$first" "$second"
}

_uvarsi_snapshot_file() {
  source_path=$1
  snapshot=$2
  name=$3
  if [ -f "$source_path" ]; then
    "$UVARSI_CP" -a "$source_path" "$snapshot/$name" || return 1
  else
    : > "$snapshot/$name.absent" || return 1
  fi
}

_uvarsi_restore_file() {
  target_path=$1
  snapshot=$2
  name=$3
  if [ -f "$snapshot/$name" ]; then
    "$UVARSI_CP" -a "$snapshot/$name" "$target_path"
  elif [ -f "$snapshot/$name.absent" ]; then
    rm -f "$target_path"
  else
    return 1
  fi
}

_uvarsi_sqlite_backup() {
  source_database=$1
  target_database=$2
  "$UVARSI_HEALTH_PY" -c '
import os, sqlite3, sys
source_path, target_path = sys.argv[1:3]
temporary_path = target_path + ".backup-in-progress"
for path in (temporary_path, temporary_path + "-wal", temporary_path + "-shm"):
    try: os.unlink(path)
    except FileNotFoundError: pass
source = sqlite3.connect(source_path)
target = sqlite3.connect(temporary_path)
try:
    source.backup(target)
    if target.execute("PRAGMA integrity_check").fetchone() != ("ok",):
        raise RuntimeError("SQLite backup failed integrity_check")
finally:
    target.close()
    source.close()
os.replace(temporary_path, target_path)
' "$source_database" "$target_database"
}

_uvarsi_snapshot_database() {
  snapshot=$1
  if [ -f "$UVARSI_DB" ]; then
    _uvarsi_sqlite_backup "$UVARSI_DB" "$snapshot/uvarsi.db"
  else
    : > "$snapshot/uvarsi.db.absent"
  fi
}

uvarsi_require_payments_off() {
  # Never source or print the env file.  Exactly one explicit false value is
  # required before a release may mutate live Uvar.si files.
  [ -f "$UVARSI_ENV_FILE" ] || return 1
  count=$(grep -Eic '^[[:space:]]*(export[[:space:]]+)?PLATBY_ZAPNUTE[[:space:]]*=' "$UVARSI_ENV_FILE")
  [ "$count" -eq 1 ] || return 1
  grep -Eqi "^[[:space:]]*(export[[:space:]]+)?PLATBY_ZAPNUTE[[:space:]]*=[[:space:]]*['\"]?(0|false|off)['\"]?[[:space:]]*$" "$UVARSI_ENV_FILE"
}

uvarsi_require_runtime_payments_off() {
  # The env file is not the final authority: a systemd Environment= override
  # wins over it.  Ask the running process what it actually loaded.
  health=$(
    "$UVARSI_CURL" -fsS --max-time 5 "$UVARSI_HEALTH_URL" 2>/dev/null
  ) || return 1
  printf '%s' "$health" | "$UVARSI_HEALTH_PY" -c '
import json, sys
try:
    payload = json.load(sys.stdin)
    enabled = payload["recipe_engine"]["payments_enabled"]
except (KeyError, TypeError, ValueError, json.JSONDecodeError):
    raise SystemExit(1)
raise SystemExit(0 if enabled is False else 1)
'
}

uvarsi_migrate_release() {
  # Candidate migrations run before any health check. A release that changes
  # schema must prove old-code compatibility separately; automated rollback
  # must never overwrite newer customer/payment rows with the online backup.
  release=$1
  uvarsi_require_payments_off || return 1
  [ -d "$release/app" ] || return 1
  [ -f "$release/VERSION" ] || return 1
  (
    cd "$release/app" || exit 1
    UVARSI_URL=https://uvar.si \
      UVARSI_DB="$UVARSI_DB" \
      UVARSI_VERSION_FILE="$release/VERSION" \
      PLATBY_ZAPNUTE=0 UVARSI_PAYMENTS_ENABLED=0 \
      "$UVARSI_HEALTH_PY" -c 'import server; server.priprav_databazu()'
  ) || return 1
  uvarsi_require_payments_off
}

_uvarsi_sqlite_restore() {
  source_database=$1
  target_database=$2
  "$UVARSI_HEALTH_PY" -c '
import sqlite3, sys
source = sqlite3.connect(sys.argv[1])
target = sqlite3.connect(sys.argv[2])
try:
    source.backup(target)
    if target.execute("PRAGMA integrity_check").fetchone() != ("ok",):
        raise RuntimeError("restored SQLite database failed integrity_check")
finally:
    target.close()
    source.close()
' "$source_database" "$target_database"
}

_uvarsi_restore_database() {
  snapshot=$1
  if [ -f "$snapshot/uvarsi.db" ]; then
    _uvarsi_sqlite_restore "$snapshot/uvarsi.db" "$UVARSI_DB"
  elif [ -f "$snapshot/uvarsi.db.absent" ]; then
    rm -f "$UVARSI_DB" "$UVARSI_DB-wal" "$UVARSI_DB-shm"
  else
    return 1
  fi
}

_uvarsi_restore_app() {
  snapshot=$1
  staged="$UVARSI_DIR/.app-restore-staged.$$"
  previous="$UVARSI_DIR/.app-restore-previous.$$"
  rm -rf "$staged" "$previous" || return 1
  "$UVARSI_CP" -a "$snapshot/app" "$staged" || {
    rm -rf "$staged"
    return 1
  }
  [ -d "$staged" ] || { rm -rf "$staged"; return 1; }

  had_previous=0
  if [ -e "$UVARSI_DIR/app" ]; then
    "$UVARSI_MV" "$UVARSI_DIR/app" "$previous" || {
      rm -rf "$staged"
      return 1
    }
    had_previous=1
  fi
  if "$UVARSI_MV" "$staged" "$UVARSI_DIR/app"; then
    [ -d "$UVARSI_DIR/app" ] || {
      echo "uvarsi_restore: app promotion returned success but $UVARSI_DIR/app is unavailable; previous=$previous staged=$staged" >&2
      return 1
    }
    if [ "$had_previous" -eq 1 ]; then
      rm -rf "$previous" || return 1
    fi
    return 0
  fi

  echo "uvarsi_restore: app promotion failed; previous=$previous staged=$staged" >&2
  if [ -d "$UVARSI_DIR/app" ]; then
    echo "uvarsi_restore: promotion failed but live app path still exists; recovery copies preserved" >&2
    return 1
  fi
  if [ "$had_previous" -eq 1 ]; then
    if "$UVARSI_MV" "$previous" "$UVARSI_DIR/app" && [ -d "$UVARSI_DIR/app" ]; then
      rm -rf "$staged" || true
      echo "uvarsi_restore: restored previous app after promotion failure" >&2
      return 1
    fi
    if [ -d "$UVARSI_DIR/app" ]; then
      echo "uvarsi_restore: recovery move returned failure but restored the live app; staged=$staged" >&2
      return 1
    fi
    if "$UVARSI_CP" -a "$previous" "$UVARSI_DIR/app" && [ -d "$UVARSI_DIR/app" ]; then
      echo "uvarsi_restore: recovered app path by copying $previous; recovery copies preserved" >&2
      return 1
    fi
  elif "$UVARSI_CP" -a "$staged" "$UVARSI_DIR/app" && [ -d "$UVARSI_DIR/app" ]; then
    echo "uvarsi_restore: recovered app path by copying $staged; staged recovery copy preserved" >&2
    return 1
  fi

  echo "uvarsi_restore: manual recovery required; app=$UVARSI_DIR/app previous=$previous staged=$staged" >&2
  return 1
}

_uvarsi_health_marker() {
  # A deployment health response is valid only when the whole queue contract is
  # present and typed. Print its persisted heartbeat marker (possibly blank).
  "$UVARSI_CURL" -fsS --max-time 5 "$UVARSI_HEALTH_URL" 2>/dev/null |
    "$UVARSI_HEALTH_PY" -c '
import datetime as dt, json, sys
q = json.load(sys.stdin).get("plan_queue")
if not isinstance(q, dict): raise SystemExit(2)
required = {"queued", "oldest_seconds", "worker_alive", "heartbeat_seconds", "heartbeat_at", "last_ready", "failed", "blocking_code"}
if not required.issubset(q): raise SystemExit(2)
integer = lambda v: isinstance(v, int) and not isinstance(v, bool) and v >= 0
if not integer(q["queued"]) or not integer(q["failed"]): raise SystemExit(2)
if q["oldest_seconds"] is not None and not integer(q["oldest_seconds"]): raise SystemExit(2)
if q["queued"] == 0 and q["oldest_seconds"] is not None: raise SystemExit(2)
if q["queued"] > 0 and q["oldest_seconds"] is None: raise SystemExit(2)
if not isinstance(q["worker_alive"], bool): raise SystemExit(2)
if q["heartbeat_seconds"] is not None and not integer(q["heartbeat_seconds"]): raise SystemExit(2)
if q["heartbeat_at"] is not None and not isinstance(q["heartbeat_at"], str): raise SystemExit(2)
if (q["heartbeat_seconds"] is None) != (q["heartbeat_at"] is None): raise SystemExit(2)
if q["worker_alive"] and q["heartbeat_at"] is None: raise SystemExit(2)
if q["last_ready"] is not None and not isinstance(q["last_ready"], str): raise SystemExit(2)
if q["blocking_code"] is not None and not isinstance(q["blocking_code"], str): raise SystemExit(2)
if q["heartbeat_at"] is not None:
    value = dt.datetime.fromisoformat(q["heartbeat_at"])
    if value.tzinfo is None: value = value.replace(tzinfo=dt.timezone.utc)
print(q["heartbeat_at"] or "")'
}

_uvarsi_persisted_heartbeat() {
  if [ -f "$UVARSI_DB" ]; then
    "$UVARSI_HEALTH_PY" -c '
import datetime as dt, sqlite3, sys
with sqlite3.connect(sys.argv[1]) as con:
    row = con.execute("SELECT heartbeat_at FROM plan_worker_state WHERE singleton=1").fetchone()
value = row[0] if row else None
if value:
    parsed = dt.datetime.fromisoformat(value)
    if parsed.tzinfo is None: parsed = parsed.replace(tzinfo=dt.timezone.utc)
print(value or "")' "$UVARSI_DB"
  else
    _uvarsi_health_marker
  fi
}

uvarsi_snapshot() {
  snapshot=$1
  rm -rf "$snapshot" || return 1
  mkdir -p "$snapshot" || return 1
  [ -d "$UVARSI_DIR/app" ] || return 1
  "$UVARSI_CP" -a "$UVARSI_DIR/app" "$snapshot/app" || return 1
  _uvarsi_snapshot_database "$snapshot" || return 1
  if [ -f "$UVARSI_DIR/VERSION" ]; then
    "$UVARSI_CP" -a "$UVARSI_DIR/VERSION" "$snapshot/VERSION" || return 1
  else
    : > "$snapshot/VERSION.absent" || return 1
  fi
  _uvarsi_snapshot_file "$UVARSI_WEB_DIR/index.html" "$snapshot" index.html || return 1
  _uvarsi_snapshot_file "$UVARSI_WEB_DIR/sw.js" "$snapshot" sw.js || return 1
  for name in refresh_blocek.py recepty.py dozorca.sh zaloha.sh payment-smoke.py uvarsi-deploy-state.sh recipe-engine-rollout.sh recipe-engine.target; do
    _uvarsi_snapshot_file "$UVARSI_DIR/$name" "$snapshot" "$name" || return 1
  done
  if [ -f "$UVARSI_WORKER_UNIT" ]; then
    "$UVARSI_CP" -a "$UVARSI_WORKER_UNIT" "$snapshot/uvarsi-plan-worker.service" || return 1
    if "$UVARSI_SYSTEMCTL" is-enabled --quiet uvarsi-plan-worker; then
      : > "$snapshot/worker.enabled" || return 1
    else
      : > "$snapshot/worker.disabled" || return 1
    fi
    if "$UVARSI_SYSTEMCTL" is-active --quiet uvarsi-plan-worker; then
      : > "$snapshot/worker.active" || return 1
    else
      : > "$snapshot/worker.inactive" || return 1
    fi
  else
    : > "$snapshot/uvarsi-plan-worker.service.absent" || return 1
    : > "$snapshot/worker.disabled" || return 1
    : > "$snapshot/worker.inactive" || return 1
  fi
  if [ -f "$UVARSI_APP_UNIT" ]; then
    "$UVARSI_CP" -a "$UVARSI_APP_UNIT" "$snapshot/uvarsi.service" || return 1
    if "$UVARSI_SYSTEMCTL" is-enabled --quiet uvarsi; then
      : > "$snapshot/app.enabled" || return 1
    else
      : > "$snapshot/app.disabled" || return 1
    fi
    if "$UVARSI_SYSTEMCTL" is-active --quiet uvarsi; then
      : > "$snapshot/app.active" || return 1
    else
      : > "$snapshot/app.inactive" || return 1
    fi
  else
    : > "$snapshot/uvarsi.service.absent" || return 1
    : > "$snapshot/app.disabled" || return 1
    : > "$snapshot/app.inactive" || return 1
  fi
  _uvarsi_persisted_heartbeat > "$snapshot/heartbeat.before" || return 1
}

uvarsi_restore() {
  snapshot=$1
  ok=1
  [ -d "$snapshot/app" ] || return 1

  # Roll back code, static files and Uvar.si unit state only.  Never restore the
  # database here: a checkout webhook, session or plan may have been written
  # after the snapshot, and replacing SQLite would silently lose that data.
  if "$UVARSI_SYSTEMCTL" is-active --quiet uvarsi-plan-worker; then
    "$UVARSI_SYSTEMCTL" stop uvarsi-plan-worker >/dev/null 2>&1 || ok=0
  fi
  if "$UVARSI_SYSTEMCTL" is-active --quiet uvarsi; then
    "$UVARSI_SYSTEMCTL" stop uvarsi >/dev/null 2>&1 || ok=0
  fi

  _uvarsi_restore_app "$snapshot" || ok=0
  if [ -f "$snapshot/VERSION" ]; then
    "$UVARSI_CP" -a "$snapshot/VERSION" "$UVARSI_DIR/VERSION" || ok=0
  elif [ -f "$snapshot/VERSION.absent" ]; then
    rm -f "$UVARSI_DIR/VERSION" || ok=0
  else
    ok=0
  fi
  _uvarsi_restore_file "$UVARSI_WEB_DIR/index.html" "$snapshot" index.html || ok=0
  _uvarsi_restore_file "$UVARSI_WEB_DIR/sw.js" "$snapshot" sw.js || ok=0
  for name in refresh_blocek.py recepty.py dozorca.sh zaloha.sh payment-smoke.py uvarsi-deploy-state.sh recipe-engine-rollout.sh recipe-engine.target; do
    _uvarsi_restore_file "$UVARSI_DIR/$name" "$snapshot" "$name" || ok=0
  done

  if [ -f "$snapshot/uvarsi-plan-worker.service" ]; then
    "$UVARSI_CP" -a "$snapshot/uvarsi-plan-worker.service" "$UVARSI_WORKER_UNIT" || ok=0
  elif [ -f "$snapshot/uvarsi-plan-worker.service.absent" ]; then
    if "$UVARSI_SYSTEMCTL" is-active --quiet uvarsi-plan-worker; then
      "$UVARSI_SYSTEMCTL" stop uvarsi-plan-worker >/dev/null 2>&1 || ok=0
    fi
    if "$UVARSI_SYSTEMCTL" is-enabled --quiet uvarsi-plan-worker; then
      "$UVARSI_SYSTEMCTL" disable uvarsi-plan-worker >/dev/null 2>&1 || ok=0
    fi
    rm -f "$UVARSI_WORKER_UNIT" || ok=0
  else
    ok=0
  fi
  if [ -f "$snapshot/uvarsi.service" ]; then
    "$UVARSI_CP" -a "$snapshot/uvarsi.service" "$UVARSI_APP_UNIT" || ok=0
  elif [ -f "$snapshot/uvarsi.service.absent" ]; then
    if "$UVARSI_SYSTEMCTL" is-active --quiet uvarsi; then
      "$UVARSI_SYSTEMCTL" stop uvarsi >/dev/null 2>&1 || ok=0
    fi
    if "$UVARSI_SYSTEMCTL" is-enabled --quiet uvarsi; then
      "$UVARSI_SYSTEMCTL" disable uvarsi >/dev/null 2>&1 || ok=0
    fi
    rm -f "$UVARSI_APP_UNIT" || ok=0
  else
    ok=0
  fi
  "$UVARSI_SYSTEMCTL" daemon-reload || ok=0

  if [ -f "$snapshot/uvarsi-plan-worker.service" ]; then
    if [ -f "$snapshot/worker.enabled" ]; then
      "$UVARSI_SYSTEMCTL" enable uvarsi-plan-worker >/dev/null 2>&1 || ok=0
      "$UVARSI_SYSTEMCTL" is-enabled --quiet uvarsi-plan-worker || ok=0
    elif [ -f "$snapshot/worker.disabled" ]; then
      "$UVARSI_SYSTEMCTL" disable uvarsi-plan-worker >/dev/null 2>&1 || ok=0
      if "$UVARSI_SYSTEMCTL" is-enabled --quiet uvarsi-plan-worker; then ok=0; fi
    else
      ok=0
    fi
    if [ -f "$snapshot/worker.active" ]; then
      "$UVARSI_SYSTEMCTL" restart uvarsi-plan-worker || ok=0
      "$UVARSI_SYSTEMCTL" is-active --quiet uvarsi-plan-worker || ok=0
    elif [ -f "$snapshot/worker.inactive" ]; then
      "$UVARSI_SYSTEMCTL" stop uvarsi-plan-worker || ok=0
      if "$UVARSI_SYSTEMCTL" is-active --quiet uvarsi-plan-worker; then ok=0; fi
    else
      ok=0
    fi
  else
    if "$UVARSI_SYSTEMCTL" is-enabled --quiet uvarsi-plan-worker; then ok=0; fi
    if "$UVARSI_SYSTEMCTL" is-active --quiet uvarsi-plan-worker; then ok=0; fi
    [ ! -e "$UVARSI_WORKER_UNIT" ] || ok=0
  fi
  if [ -f "$snapshot/uvarsi.service" ]; then
    if [ -f "$snapshot/app.enabled" ]; then
      "$UVARSI_SYSTEMCTL" enable uvarsi >/dev/null 2>&1 || ok=0
      "$UVARSI_SYSTEMCTL" is-enabled --quiet uvarsi || ok=0
    elif [ -f "$snapshot/app.disabled" ]; then
      "$UVARSI_SYSTEMCTL" disable uvarsi >/dev/null 2>&1 || ok=0
      if "$UVARSI_SYSTEMCTL" is-enabled --quiet uvarsi; then ok=0; fi
    else
      ok=0
    fi
    if [ -f "$snapshot/app.active" ]; then
      "$UVARSI_SYSTEMCTL" restart uvarsi || ok=0
      "$UVARSI_SYSTEMCTL" is-active --quiet uvarsi || ok=0
    elif [ -f "$snapshot/app.inactive" ]; then
      "$UVARSI_SYSTEMCTL" stop uvarsi || ok=0
      if "$UVARSI_SYSTEMCTL" is-active --quiet uvarsi; then ok=0; fi
    else
      ok=0
    fi
  else
    if "$UVARSI_SYSTEMCTL" is-enabled --quiet uvarsi; then ok=0; fi
    if "$UVARSI_SYSTEMCTL" is-active --quiet uvarsi; then ok=0; fi
    [ ! -e "$UVARSI_APP_UNIT" ] || ok=0
  fi
  [ "$ok" -eq 1 ]
}

_uvarsi_apply_core() {
  release=$1
  [ -d "$release/app" ] || return 1
  [ -f "$release/VERSION" ] || return 1
  [ -f "$release/hetzner/uvarsi-plan-worker.service" ] || return 1
  [ -f "$release/hetzner/uvarsi.service" ] || return 1
  staged="$UVARSI_DIR/.app-install-staged.$$"
  rm -rf "$staged" || return 1
  "$UVARSI_CP" -a "$release/app" "$staged" || {
    rm -rf "$staged"
    return 1
  }
  [ -d "$staged" ] || { rm -rf "$staged"; return 1; }
  if [ -e "$UVARSI_DIR/app" ]; then
    _uvarsi_exchange_directories "$UVARSI_DIR/app" "$staged" || {
      rm -rf "$staged"
      return 1
    }
    [ -d "$UVARSI_DIR/app" ] || {
      _uvarsi_exchange_directories "$UVARSI_DIR/app" "$staged" || true
      return 1
    }
    rm -rf "$staged" || return 1
  else
    "$UVARSI_MV" "$staged" "$UVARSI_DIR/app" || return 1
  fi
  "$UVARSI_CP" -a "$release/VERSION" "$UVARSI_DIR/VERSION" || return 1
  "$UVARSI_CP" -a "$release/hetzner/uvarsi-plan-worker.service" "$UVARSI_WORKER_UNIT" || return 1
  "$UVARSI_CP" -a "$release/hetzner/uvarsi.service" "$UVARSI_APP_UNIT" || return 1
  "$UVARSI_SYSTEMCTL" daemon-reload || return 1
}

uvarsi_install_core() {
  release=$1
  snapshot=$2
  if _uvarsi_apply_core "$release"; then
    return 0
  fi
  uvarsi_restore "$snapshot" || return 2
  return 1
}

_uvarsi_apply_manual_targets() {
  release=$1
  [ -f "$release/index.html" ] || return 1
  [ -f "$release/sw.js" ] || return 1
  [ -f "$release/hetzner/uvarsi.service" ] || return 1
  for name in refresh_blocek.py recepty.py dozorca.sh zaloha.sh payment-smoke.py uvarsi-deploy-state.sh recipe-engine-rollout.sh recipe-engine.target; do
    [ -f "$release/hetzner/$name" ] || return 1
  done
  "$UVARSI_CP" -a "$release/index.html" "$UVARSI_WEB_DIR/index.html" || return 1
  "$UVARSI_CP" -a "$release/sw.js" "$UVARSI_WEB_DIR/sw.js" || return 1
  for name in refresh_blocek.py recepty.py dozorca.sh zaloha.sh payment-smoke.py uvarsi-deploy-state.sh recipe-engine-rollout.sh recipe-engine.target; do
    "$UVARSI_CP" -a "$release/hetzner/$name" "$UVARSI_DIR/$name" || return 1
  done
  chmod +x "$UVARSI_DIR/dozorca.sh" "$UVARSI_DIR/zaloha.sh" \
    "$UVARSI_DIR/payment-smoke.py" "$UVARSI_DIR/uvarsi-deploy-state.sh" \
    "$UVARSI_DIR/recipe-engine-rollout.sh" || return 1
  "$UVARSI_CP" -a "$release/hetzner/uvarsi.service" "$UVARSI_APP_UNIT" || return 1
  "$UVARSI_SYSTEMCTL" daemon-reload || return 1
}

uvarsi_install_manual_release() {
  release=$1
  snapshot=$2
  uvarsi_require_payments_off || return 1
  if _uvarsi_apply_core "$release" && \
      _uvarsi_apply_manual_targets "$release" && \
      uvarsi_migrate_release "$release"; then
    return 0
  fi
  uvarsi_restore "$snapshot" || return 2
  return 1
}

uvarsi_wait_fresh_heartbeat() {
  before=$1
  attempt=1
  while [ "$attempt" -le "$UVARSI_HEARTBEAT_ATTEMPTS" ]; do
    marker=$(_uvarsi_health_marker 2>/dev/null || true)
    if [ -n "$marker" ] && "$UVARSI_CURL" -fsS --max-time 5 "$UVARSI_HEALTH_URL" 2>/dev/null |
      "$UVARSI_HEALTH_PY" -c '
import datetime as dt, json, sys
before, current = sys.argv[1:3]
q = json.load(sys.stdin).get("plan_queue", {})
if q.get("worker_alive") is not True: raise SystemExit(1)
def instant(value):
    parsed = dt.datetime.fromisoformat(value)
    if parsed.tzinfo is None: parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)
raise SystemExit(0 if (not before or instant(current) > instant(before)) else 1)' "$before" "$marker"; then
      return 0
    fi
    "$UVARSI_SLEEP" 1
    attempt=$((attempt + 1))
  done
  return 1
}

if [ "${BASH_SOURCE[0]}" = "$0" ]; then
  case "${1:-}" in
    check-bridge) uvarsi_require_tesco_bridge ;;
    check-readiness) uvarsi_require_production_readiness ;;
    run-supervisor) uvarsi_run_supervisor_bounded ;;
    *) exit 64 ;;
  esac
fi
