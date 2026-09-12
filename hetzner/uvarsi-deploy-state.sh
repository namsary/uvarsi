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
UVARSI_BASH="${UVARSI_BASH:-/bin/bash}"
UVARSI_CRONTAB="${UVARSI_CRONTAB:-crontab}"
UVARSI_FLOCK="${UVARSI_FLOCK:-flock}"
UVARSI_CRON_LOCK="${UVARSI_CRON_LOCK:-$UVARSI_DIR/.crontab.lock}"
UVARSI_SUPERVISOR="${UVARSI_SUPERVISOR:-$UVARSI_DIR/dozorca.sh}"
UVARSI_COLLECTOR="${UVARSI_COLLECTOR:-$UVARSI_APP_DIR/zbierac_akcii.py}"
UVARSI_RECEIPT_REFRESH="${UVARSI_RECEIPT_REFRESH:-$UVARSI_DIR/refresh_blocek.py}"
UVARSI_DEPLOY_STATE_SCRIPT="${UVARSI_DEPLOY_STATE_SCRIPT:-${BASH_SOURCE[0]}}"
UVARSI_SUPERVISOR_STATE="${UVARSI_SUPERVISOR_STATE:-$UVARSI_DIR/.dozorca_state}"
UVARSI_SUPERVISOR_SUCCESS_STATE="${UVARSI_SUPERVISOR_SUCCESS_STATE:-$UVARSI_DIR/.supervisor_success_state}"
UVARSI_SUPERVISOR_SUCCESS_MAX_AGE_SECONDS="${UVARSI_SUPERVISOR_SUCCESS_MAX_AGE_SECONDS:-18000}"
UVARSI_COLLECTION_FAILURE_STATE="${UVARSI_COLLECTION_FAILURE_STATE:-$UVARSI_DIR/.collection_failure_state}"
UVARSI_TAKTIK_URL="${UVARSI_TAKTIK_URL:-https://mapa.89.167.72.159.sslip.io/}"
UVARSI_MAX_COLLECTION_SECONDS="${UVARSI_MAX_COLLECTION_SECONDS:-14400}"
UVARSI_TERMINATION_GRACE_SECONDS="${UVARSI_TERMINATION_GRACE_SECONDS:-300}"
UVARSI_WORKER_UNIT="$UVARSI_SYSTEMD_DIR/uvarsi-plan-worker.service"
UVARSI_APP_UNIT="$UVARSI_SYSTEMD_DIR/uvarsi.service"
UVARSI_PROC_ROOT="${UVARSI_PROC_ROOT:-/proc}"
UVARSI_BRIDGE_FAILURE_REASON="not_checked"
# Reset on every source.  Only the compatibility function below may enable
# this process-local escape for the already-installed pre-decoupling samopull.
UVARSI_LEGACY_CODE_DEPLOY=0

_uvarsi_bridge_fail() {
  # Stable operator-facing enum only.  Never include a URL, response body,
  # bearer value or environment content in this state.
  UVARSI_BRIDGE_FAILURE_REASON=$1
  return 1
}

_uvarsi_release_trace() {
  # Temporary-safe deployment breadcrumb for an installed legacy samopull.
  # Only fixed enum values may leave the host; no runtime value is interpolated.
  stage=$1
  case "$stage" in
    runtime_payments_ok|runtime_payments_failed|install_core_ok|install_core_failed|\
    migration_ok|migration_failed|heartbeat_ok|heartbeat_compat|\
    schedule_ok|schedule_failed|supervisor_ok|supervisor_failed|\
    production_ready|production_failed) ;;
    *) stage=unknown ;;
  esac
  in_samopull=0
  for source_file in "${BASH_SOURCE[@]}"; do
    case "$source_file" in */samopull.sh) in_samopull=1 ;; esac
  done
  if [ "$in_samopull" -eq 1 ] && declare -F notify >/dev/null 2>&1; then
    notify "Uvar.si deploy diagnostika" "stage=$stage"
  fi
  return 0
}

_uvarsi_called_from_samopull() {
  for source_file in "${BASH_SOURCE[@]}"; do
    case "$source_file" in */samopull.sh) return 0 ;; esac
  done
  return 1
}

_uvarsi_require_process_payments_off() {
  # Health may be temporarily blocked by a long SQLite operation.  The legacy
  # deployer may then inspect only the two payment flags of the running process.
  # The env file remains the primary explicit OFF gate and no environment value
  # is printed, logged or copied.
  uvarsi_require_payments_off || return 1
  "$UVARSI_SYSTEMCTL" is-active --quiet uvarsi || return 1
  pid=$("$UVARSI_SYSTEMCTL" show --property=MainPID --value uvarsi 2>/dev/null) || return 1
  case "$pid" in ''|*[!0-9]*|0) return 1 ;; esac
  process_environment="$UVARSI_PROC_ROOT/$pid/environ"
  [ -r "$process_environment" ] || return 1
  "$UVARSI_HEALTH_PY" -c '
import sys

path = sys.argv[1]
allowed_false = {"", "0", "false", "off", "no", "nie"}
watched = {"PLATBY_ZAPNUTE", "UVARSI_PAYMENTS_ENABLED"}
seen = {}
with open(path, "rb") as handle:
    entries = handle.read().split(b"\0")
for entry in entries:
    if b"=" not in entry:
        continue
    raw_key, raw_value = entry.split(b"=", 1)
    try:
        key = raw_key.decode("ascii")
    except UnicodeDecodeError:
        continue
    if key not in watched:
        continue
    if key in seen:
        raise SystemExit(1)
    try:
        value = raw_value.decode("utf-8").strip().casefold()
    except UnicodeDecodeError:
        raise SystemExit(1)
    if value not in allowed_false:
        raise SystemExit(1)
    seen[key] = value
' "$process_environment" >/dev/null 2>&1
}

_uvarsi_today() {
  if [ -n "${UVARSI_TODAY:-}" ]; then
    printf '%s' "$UVARSI_TODAY"
  else
    TZ=Europe/Bratislava date +%F
  fi
}

_uvarsi_now_epoch() {
  if [ -n "${UVARSI_NOW_EPOCH:-}" ]; then
    printf '%s' "$UVARSI_NOW_EPOCH"
  else
    date +%s
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

_uvarsi_require_tesco_bridge_transport() {
  # A parent shell may have enabled xtrace. Disable it before command
  # substitutions can place an env value (especially the secret) in stderr.
  case $- in *x*) set +x ;; esac
  # Values are read without sourcing or printing the env file. The bearer
  # header reaches curl over stdin config, so it is absent from argv and logs.
  UVARSI_BRIDGE_FAILURE_REASON="config_invalid"
  environment=$(_uvarsi_env_value UVARSI_ENV) || return 1
  [ "$environment" = production ] || return 1
  bridge_url=$(_uvarsi_env_value UVARSI_TESCO_BRIDGE_URL) || return 1
  bridge_worker_host=$(_uvarsi_env_value UVARSI_TESCO_BRIDGE_WORKER_HOST) || return 1
  bridge_release=$(_uvarsi_env_value UVARSI_TESCO_BRIDGE_RELEASE) || return 1
  bridge_version_id=$(_uvarsi_env_value UVARSI_TESCO_BRIDGE_VERSION_ID) || return 1
  bridge_secret=$(_uvarsi_env_value UVARSI_TESCO_BRIDGE_SECRET) || return 1
  case "$bridge_release" in
    *[!0-9a-f]*|'') return 1 ;;
  esac
  [ "${#bridge_release}" -ge 12 ] && [ "${#bridge_release}" -le 64 ] || return 1
  case "$bridge_version_id" in
    *[!A-Za-z0-9._-]*|'') return 1 ;;
  esac
  [ "${#bridge_version_id}" -ge 8 ] && [ "${#bridge_version_id}" -le 128 ] || return 1
  case "$bridge_secret" in "$bridge_release".*) ;; *) return 1 ;; esac
  bridge_secret_random=${bridge_secret#*.}
  [ "${#bridge_secret_random}" -ge 32 ] || return 1
  case "$bridge_secret" in *[!A-Za-z0-9._~-]*) return 1 ;; esac

  "$UVARSI_HEALTH_PY" -c '
import sys
from urllib.parse import urlsplit
try:
    parsed = urlsplit(sys.argv[1])
    port = parsed.port
except ValueError:
    raise SystemExit(1)
expected_host = sys.argv[2]
valid = (
    parsed.scheme == "https" and bool(parsed.hostname)
    and expected_host == expected_host.lower()
    and expected_host.startswith("uvarsi-tesco-bridge.")
    and expected_host.endswith(".workers.dev")
    and parsed.hostname == expected_host
    and parsed.username is None and parsed.password is None and port is None
    and parsed.path in ("", "/") and not parsed.query and not parsed.fragment
)
raise SystemExit(0 if valid else 1)
  ' "$bridge_url" "$bridge_worker_host" >/dev/null 2>&1 || return 1
  bridge_url=${bridge_url%/}
  today=$(_uvarsi_today) || { _uvarsi_bridge_fail local_error; return 1; }
  response=$(mktemp "${TMPDIR:-/tmp}/uvarsi-bridge.XXXXXX") || {
    _uvarsi_bridge_fail local_error; return 1; }
  chmod 600 "$response" || {
    rm -f "$response"; _uvarsi_bridge_fail local_error; return 1; }
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
    _uvarsi_bridge_fail request_failed
    return 1
  fi
  if ! UVARSI_BRIDGE_VERIFY_SECRET=$bridge_secret "$UVARSI_HEALTH_PY" -c '
import base64, datetime as dt, hashlib, hmac, json, os, sys
from urllib.parse import urlsplit
path, today_raw, bridge_url, expected_release, expected_version = sys.argv[1:6]
try:
    today = dt.date.fromisoformat(today_raw)
    with open(path, encoding="utf-8") as handle:
        payload = json.load(handle)
    bridge = payload["bridge"]
    leaflet = payload["leaflet"]
    start = dt.date.fromisoformat(leaflet["valid_from"])
    end = dt.date.fromisoformat(leaflet["valid_to"])
    pages = leaflet["pages"]
    source = urlsplit(leaflet["source_url"])
except (KeyError, TypeError, ValueError, json.JSONDecodeError, OSError):
    raise SystemExit(1)
if not isinstance(bridge, dict) or set(bridge) != {"release", "version_id", "attestation"}:
    raise SystemExit(1)
if bridge.get("release") != expected_release or bridge.get("version_id") != expected_version:
    raise SystemExit(1)
attestation = bridge.get("attestation")
secret = os.environ.get("UVARSI_BRIDGE_VERIFY_SECRET")
if (
    not isinstance(attestation, str) or len(attestation) != 43
    or any(char not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_" for char in attestation)
    or not isinstance(secret, str) or not secret
):
    raise SystemExit(1)
statement = {
    "release": expected_release,
    "version_id": expected_version,
    "request_date": today_raw,
    "request_format": "HM",
    "leaflet": leaflet,
}
expected_attestation = base64.urlsafe_b64encode(hmac.new(
    secret.encode(),
    json.dumps(statement, ensure_ascii=False, separators=(",", ":")).encode(),
    hashlib.sha256,
).digest()).decode().rstrip("=")
if not hmac.compare_digest(attestation, expected_attestation):
    raise SystemExit(1)
slug = leaflet.get("slug")
expected_slug = "tesco-letak-" + start.isoformat()
expected_source_path = (
    "/akciove-ponuky/letaky-a-katalogy/hypermarkety/" + expected_slug + "/1"
)
if (
    not isinstance(leaflet, dict) or leaflet.get("country") != "sk"
    or leaflet.get("format") != "HM" or not start <= today <= end
    or slug != expected_slug
    or source.scheme != "https" or source.hostname != "www.tesco.sk"
    or source.username is not None or source.password is not None
    or source.port is not None or source.path != expected_source_path
    or source.query or source.fragment
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
  ' "$response" "$today" "$bridge_url" "$bridge_release" "$bridge_version_id" >/dev/null 2>&1; then
    rm -f "$response"
    _uvarsi_bridge_fail response_invalid
    return 1
  fi
  rm -f "$response" || { _uvarsi_bridge_fail local_error; return 1; }
  UVARSI_TESCO_BRIDGE_URL=$bridge_url
  UVARSI_TESCO_BRIDGE_SECRET=$bridge_secret
  UVARSI_ENV=production
  export UVARSI_TESCO_BRIDGE_URL UVARSI_TESCO_BRIDGE_SECRET UVARSI_ENV
  UVARSI_BRIDGE_FAILURE_REASON="ok"
}

_uvarsi_require_collection_readiness() {
  readiness_scope=${1:-full}
  case "$readiness_scope" in
    full|offers) ;;
    *) return 1 ;;
  esac
  today=$(_uvarsi_today) || return 1
  (
    cd "$UVARSI_APP_DIR" || exit 1
    "$UVARSI_HEALTH_PY" -c '
import datetime as dt, json, re, sqlite3, sys
from decimal import Decimal, InvalidOperation
from landing_data import CURRENT_LANDING_STATE, validate_publishable_landing_data
from offer_data import MAX_FLYER_VALIDITY_DAYS
from source_policy import MIN_FACTS_PER_STORE, collector_kind_for_url

database, landing_path, today_raw, readiness_scope = sys.argv[1:5]
if readiness_scope not in {"full", "offers"}:
    raise SystemExit(1)
today = dt.date.fromisoformat(today_raw)
week = (today - dt.timedelta(days=today.weekday())).isoformat()
stores = {
    "Kaufland": "official-kaufland-offers",
    "Tesco": "official-tesco-viewer",
    "Lidl": "official-lidl-viewer",
}
fingerprint = re.compile(r"[0-9a-f]{64}")

def money(value):
    try:
        amount = Decimal(str(value).strip().replace(",", "."))
    except (InvalidOperation, ValueError):
        raise SystemExit(1)
    if (
        not amount.is_finite() or amount <= 0 or amount > Decimal("10000")
        or amount != amount.quantize(Decimal("0.01"))
    ):
        raise SystemExit(1)
    return amount

def optional_money(value):
    return None if value is None else money(value)

def money_text(value):
    return format(value.quantize(Decimal("0.01")), "f").replace(".", ",")

def weight_multiplier(value):
    try:
        amount = Decimal(str(value).strip().replace(",", "."))
    except (InvalidOperation, ValueError):
        raise SystemExit(1)
    if not amount.is_finite() or amount <= 0 or amount > Decimal("100"):
        raise SystemExit(1)
    return amount

active_offer_refs = set()
active_offer_sources = {}
active_offers = {}
active_source_refs = set()
with sqlite3.connect("file:" + database + "?mode=ro", uri=True) as con:
    required_status = {
        "tyzden", "obchod", "stav", "pocet", "data_version",
        "collector_kind", "source_fingerprint", "valid_from", "valid_to",
    }
    # Runtime readiness is about the atomically promoted, public dataset.  A
    # failed future collection may legitimately leave staging incomplete or
    # different; that scratch state must never poison the last-known-good rows.
    for table in ("zber_stav",):
        columns = {row[1] for row in con.execute("PRAGMA table_info(" + table + ")")}
        if not required_status <= columns:
            raise SystemExit(1)
    required_offer = {
        "tyzden", "obchod", "nazov", "cena", "povodna", "zlava", "jednotka",
        "source_url", "source_page", "offer_key", "valid_from", "valid_to",
        "cena_s_kartou", "zlava_s_kartou", "vernostny_program",
        "minimalny_nakup", "podmienka_s_kartou",
    }
    for table in ("akcie",):
        columns = {row[1] for row in con.execute("PRAGMA table_info(" + table + ")")}
        if not required_offer <= columns:
            raise SystemExit(1)
    for store, expected_kind in stores.items():
        query = (
            "SELECT stav,pocet,data_version,collector_kind,source_fingerprint,"
            "valid_from,valid_to FROM {} WHERE tyzden=? AND obchod=?"
        )
        active = con.execute(query.format("zber_stav"), (week, store)).fetchone()
        if active is None:
            raise SystemExit(1)
        status, declared, version, kind, source_hash, start, end = active
        try:
            current = dt.date.fromisoformat(start) <= today <= dt.date.fromisoformat(end)
        except (TypeError, ValueError):
            raise SystemExit(1)
        if (
            status != "ok" or int(declared or 0) < MIN_FACTS_PER_STORE
            or int(version or 0) < 2
            or kind != expected_kind or not isinstance(source_hash, str)
            or fingerprint.fullmatch(source_hash) is None or not current
        ):
            raise SystemExit(1)
        for table in ("akcie",):
            rows = con.execute(
                "SELECT offer_key,nazov,cena,povodna,zlava,jednotka,"
                "source_url,source_page,valid_from,valid_to,cena_s_kartou,"
                "zlava_s_kartou,vernostny_program,minimalny_nakup,podmienka_s_kartou "
                "FROM " + table + " WHERE tyzden=? AND obchod=?",
                (week, store),
            ).fetchall()
            keys = {row[0] for row in rows if isinstance(row[0], str) and row[0].strip()}
            if (
                len(rows) < MIN_FACTS_PER_STORE
                or len(rows) != int(declared) or len(keys) != len(rows)
            ):
                raise SystemExit(1)
            facts = set()
            for row in rows:
                (
                    offer_key, name, price_raw, original_raw, discount, unit,
                    source_url, source_page, start_raw, end_raw, loyalty_raw,
                    loyalty_discount, loyalty_program, minimum_raw,
                    loyalty_condition,
                ) = row
                try:
                    offer_start = dt.date.fromisoformat(start_raw)
                    offer_end = dt.date.fromisoformat(end_raw)
                except (TypeError, ValueError):
                    raise SystemExit(1)
                price = money(price_raw)
                original = optional_money(original_raw)
                loyalty = optional_money(loyalty_raw)
                minimum = optional_money(minimum_raw)
                if (
                    not isinstance(name, str) or not name.strip()
                    or not isinstance(unit, str) or not unit.strip()
                    or original is not None and original < price
                    or discount is not None and not isinstance(discount, str)
                    or isinstance(source_page, bool) or not isinstance(source_page, int)
                    or not 1 <= source_page <= 500
                    or collector_kind_for_url(source_url) != expected_kind
                    or not offer_start <= today <= offer_end
                ):
                    raise SystemExit(1)
                loyalty_metadata = (
                    loyalty_discount, loyalty_program, minimum_raw, loyalty_condition,
                )
                if loyalty is None:
                    if any(value not in (None, "") for value in loyalty_metadata):
                        raise SystemExit(1)
                elif (
                    loyalty >= price
                    or loyalty_discount is not None and (
                        not isinstance(loyalty_discount, str) or not loyalty_discount.strip()
                    )
                    or not isinstance(loyalty_program, str) or not loyalty_program.strip()
                    or loyalty_condition is not None and (
                        not isinstance(loyalty_condition, str) or not loyalty_condition.strip()
                    )
                ):
                    raise SystemExit(1)
                # Tematické mesačné kampane sú legitímne ponuky obchodu,
                # ale nie sú súčasťou týždenného letáka Uvar.si. Brána
                # nasadenia musí použiť rovnaké okno ako appka a dozorca;
                # inak by vedela označiť starý bloček za zdravý, hoci ho
                # backend pre platby správne odmieta.
                if (offer_end - offer_start).days + 1 > MAX_FLYER_VALIDITY_DAYS:
                    continue
                fact = (
                    offer_key, name, str(price), None if original is None else str(original),
                    discount, unit, source_url, source_page, start_raw, end_raw,
                    None if loyalty is None else str(loyalty), loyalty_discount,
                    loyalty_program, None if minimum is None else str(minimum),
                    loyalty_condition,
                )
                facts.add(fact)
                if table == "akcie":
                    active_offer_refs.add((store, offer_key))
                    source_ref = (store, source_url, source_page, start_raw, end_raw)
                    active_offer_sources[(store, offer_key)] = source_ref
                    active_offers[(store, offer_key)] = {
                        "name": name,
                        "unit": unit,
                        "price": price,
                        "original": original,
                        "discount": discount or "",
                        "loyalty": loyalty,
                        "loyalty_discount": loyalty_discount or None,
                        "loyalty_program": loyalty_program,
                        "minimum": minimum,
                        "loyalty_condition": loyalty_condition,
                    }
                    active_source_refs.add(source_ref)
            if len(facts) < MIN_FACTS_PER_STORE:
                raise SystemExit(1)

if readiness_scope == "offers":
    raise SystemExit(0)

with open(landing_path, encoding="utf-8") as handle:
    landing = json.load(handle)
state, reference_day = validate_publishable_landing_data(
    landing, today, required_offer_data_version=2
)
if state != CURRENT_LANDING_STATE or reference_day != today:
    raise SystemExit(1)
meals = landing.get("receipt", {}).get("meals")
if not isinstance(meals, list) or len(meals) != 3:
    raise SystemExit(1)
sources = landing.get("sources")
if not isinstance(sources, list) or len(sources) < 3:
    raise SystemExit(1)
source_stores = set()
landing_source_refs = set()
for source in sources:
    try:
        store = source["store"]
        source_url = source["url"]
        source_page = source["source_page"]
        start_raw = source["valid_from"]
        end_raw = source["valid_to"]
        start = dt.date.fromisoformat(start_raw)
        end = dt.date.fromisoformat(end_raw)
    except (KeyError, TypeError, ValueError):
        raise SystemExit(1)
    source_ref = (store, source_url, source_page, start_raw, end_raw)
    if (
        store not in stores
        or collector_kind_for_url(source_url) != stores[store]
        or isinstance(source_page, bool) or not isinstance(source_page, int)
        or not 1 <= source_page <= 500 or not start <= today <= end
        or source_ref not in active_source_refs
        or source_ref in landing_source_refs
    ):
        raise SystemExit(1)
    source_stores.add(store)
    landing_source_refs.add(source_ref)
if source_stores != set(stores):
    raise SystemExit(1)

receipt = landing["receipt"]
total = money(receipt.get("nakup_spolu"))
regular = money(receipt.get("bezne"))
try:
    savings = Decimal(str(receipt.get("usetris")).strip().replace(",", "."))
except (InvalidOperation, ValueError):
    raise SystemExit(1)
if (
    not savings.is_finite() or savings < 0 or savings > Decimal("10000")
    or regular < total
    or (regular - total).quantize(Decimal("0.01"))
    != savings.quantize(Decimal("0.01"))
):
    raise SystemExit(1)
item_count = 0
item_total = Decimal("0")
item_regular_total = Decimal("0")
substantiated_count = 0
seen_offer_refs = set()
for meal in meals:
    items = meal.get("items") if isinstance(meal, dict) else None
    if not isinstance(items, list) or not items:
        raise SystemExit(1)
    for item in items:
        if not isinstance(item, dict):
            raise SystemExit(1)
        base_item_fields = {
            "offer_key", "name", "store", "unit", "quantity", "price",
            "original_price", "savings", "off",
        }
        loyalty_item_fields = {
            "loyalty_price", "loyalty_discount", "loyalty_program",
            "loyalty_minimum_basket", "loyalty_condition",
        }
        store = item.get("store")
        offer_key = item.get("offer_key")
        quantity = item.get("quantity")
        if (
            store not in stores
            or not isinstance(offer_key, str) or not offer_key.strip()
            or (store, offer_key) not in active_offer_refs
            or active_offer_sources.get((store, offer_key)) not in landing_source_refs
            or (store, offer_key) in seen_offer_refs
            or isinstance(quantity, bool) or not isinstance(quantity, int)
            or quantity <= 0 or quantity > 100
        ):
            raise SystemExit(1)
        offer = active_offers[(store, offer_key)]
        weighted = offer["unit"].strip().casefold() == "kg"
        expected_fields = base_item_fields | ({"weight_multiplier"} if weighted else set())
        if offer["loyalty"] is not None:
            expected_fields |= loyalty_item_fields
        if (
            set(item) != expected_fields
            or item.get("name") != offer["name"]
            or item.get("unit") != offer["unit"]
            or item.get("off") != offer["discount"]
        ):
            raise SystemExit(1)
        line_price = money(item.get("price"))
        line_original = optional_money(item.get("original_price"))
        line_loyalty = optional_money(item.get("loyalty_price"))
        if (offer["original"] is None) != (line_original is None):
            raise SystemExit(1)
        if weighted:
            multiplier = weight_multiplier(item.get("weight_multiplier"))
            if quantity != 1 or (
                offer["original"] is None and offer["loyalty"] is None
            ):
                raise SystemExit(1)
            expected_price = (offer["price"] * multiplier).quantize(Decimal("0.01"))
            expected_original = (
                None if offer["original"] is None
                else (offer["original"] * multiplier).quantize(Decimal("0.01"))
            )
            expected_loyalty = (
                None if offer["loyalty"] is None
                else (offer["loyalty"] * multiplier).quantize(Decimal("0.01"))
            )
            if line_original is not None:
                if line_original < line_price:
                    raise SystemExit(1)
            if line_loyalty is not None:
                if line_loyalty >= line_price:
                    raise SystemExit(1)
            if (
                line_price != expected_price
                or line_original != expected_original
                or line_loyalty != expected_loyalty
            ):
                raise SystemExit(1)
        else:
            expected_price = offer["price"] * quantity
            expected_original = (
                None if offer["original"] is None else offer["original"] * quantity
            )
            expected_loyalty = (
                None if offer["loyalty"] is None else offer["loyalty"] * quantity
            )
            if (
                line_price != expected_price
                or line_original != expected_original
                or line_loyalty != expected_loyalty
            ):
                raise SystemExit(1)
        expected_savings = (
            None if expected_original is None else expected_original - expected_price
        )
        if item.get("savings") != (
            None if expected_savings is None else money_text(expected_savings)
        ):
            raise SystemExit(1)
        if offer["loyalty"] is not None and (
            line_loyalty is None
            or item.get("loyalty_discount") != offer["loyalty_discount"]
            or item.get("loyalty_program") != offer["loyalty_program"]
            or item.get("loyalty_minimum_basket") != (
                None if offer["minimum"] is None else money_text(offer["minimum"])
            )
            or item.get("loyalty_condition") != offer["loyalty_condition"]
        ):
            raise SystemExit(1)
        seen_offer_refs.add((store, offer_key))
        item_total += expected_price
        item_regular_total += (
            expected_price if expected_original is None else expected_original
        )
        substantiated_count += expected_original is not None
        item_count += 1
if (
    item_count < 3
    or item_total.quantize(Decimal("0.01")) != total.quantize(Decimal("0.01"))
    or item_regular_total.quantize(Decimal("0.01")) != regular.quantize(Decimal("0.01"))
):
    raise SystemExit(1)
if (
    receipt.get("polozky") != item_count
    or isinstance(receipt.get("polozky_s_beznou_cenou"), bool)
    or not isinstance(receipt.get("polozky_s_beznou_cenou"), int)
    or receipt["polozky_s_beznou_cenou"] != substantiated_count
):
    raise SystemExit(1)
' "$UVARSI_DB" "$UVARSI_LANDING_DATA" "$today" "$readiness_scope" >/dev/null 2>&1
  )
}

_uvarsi_require_official_offer_data() {
  _uvarsi_require_collection_readiness offers
}

# Kompatibilita iba pre jednu prechodovú verziu: starý samopull volal túto
# bránu pri každom deployi. Nový release sa tak môže nasadiť z už overených
# aktuálnych ponúk aj počas výpadku transportu. Všetky nové zberové cesty volajú
# priamo prísnu transportnú kontrolu vyššie.
uvarsi_require_tesco_bridge() {
  if _uvarsi_require_official_offer_data; then
    return 0
  fi
  if _uvarsi_require_tesco_bridge_transport; then
    return 0
  fi
  # One-release compatibility path: old samopull incorrectly made an external
  # collector transport a prerequisite for installing application code.  With
  # payments explicitly off it may install the reviewed code, but this flag is
  # not exported and cannot make a fresh process or payment gate data-ready.
  if uvarsi_require_payments_off; then
    UVARSI_LEGACY_CODE_DEPLOY=1
    return 0
  fi
  return 1
}

_uvarsi_supervisor_cron_line() {
  printf '%s' '0 5-21 * * * /opt/uvarsi/uvarsi-deploy-state.sh run-supervisor >> /var/log/uvarsi.log 2>&1'
}

_uvarsi_backup_cron_line() {
  printf '%s' '30 3 * * * /opt/uvarsi/zaloha.sh >> /var/log/uvarsi-zaloha.log 2>&1'
}

_uvarsi_payment_cron_line() {
  printf '%s' '5 * * * * cd /opt/uvarsi/app && /opt/uvarsi/venv/bin/python rekonciliacia.py >> /var/log/uvarsi-platby.log 2>&1'
}

_uvarsi_read_crontab() {
  target=$1
  error_file=$(mktemp "${TMPDIR:-/tmp}/uvarsi-cron-error.XXXXXX") || return 1
  chmod 600 "$error_file" || { rm -f "$error_file"; return 1; }
  if LC_ALL=C "$UVARSI_CRONTAB" -l > "$target" 2>"$error_file"; then
    rm -f "$error_file"
    return 0
  fi
  if grep -Eqi '^no crontab for ' "$error_file"; then
    : > "$target"
    rm -f "$error_file"
    return 0
  fi
  rm -f "$error_file"
  return 1
}

_uvarsi_transform_supervisor_cron() {
  source_file=$1
  target_file=$2
  replacement_file=$3
  "$UVARSI_HEALTH_PY" -c '
import re, sys
source, target, replacement = sys.argv[1:4]
direct = re.compile(r"(?:^|\s)/opt/uvarsi/dozorca\.sh(?:\s|$)")
wrapped = re.compile(
    r"(?:^|\s)/opt/uvarsi/uvarsi-deploy-state\.sh\s+run-supervisor(?:\s|$)"
)

def is_target(line):
    stripped = line.strip()
    return bool(stripped and not stripped.startswith("#") and (
        direct.search(stripped) or wrapped.search(stripped)
    ))

with open(source, encoding="utf-8") as handle:
    kept = [line.rstrip("\n") for line in handle if not is_target(line)]
with open(replacement, encoding="utf-8") as handle:
    additions = [line.strip() for line in handle if line.strip()]
if any(line.startswith("#") or not is_target(line) for line in additions):
    raise SystemExit(1)
with open(target, "w", encoding="utf-8", newline="\n") as handle:
    for line in kept + additions:
        handle.write(line + "\n")
' "$source_file" "$target_file" "$replacement_file" >/dev/null 2>&1
}

_uvarsi_extract_supervisor_cron() {
  source_file=$1
  target_file=$2
  "$UVARSI_HEALTH_PY" -c '
import re, sys
source, target = sys.argv[1:3]
direct = re.compile(r"(?:^|\s)/opt/uvarsi/dozorca\.sh(?:\s|$)")
wrapped = re.compile(
    r"(?:^|\s)/opt/uvarsi/uvarsi-deploy-state\.sh\s+run-supervisor(?:\s|$)"
)
with open(source, encoding="utf-8") as handle, open(
    target, "w", encoding="utf-8", newline="\n"
) as output:
    for line in handle:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and (
            direct.search(stripped) or wrapped.search(stripped)
        ):
            output.write(stripped + "\n")
' "$source_file" "$target_file" >/dev/null 2>&1
}

uvarsi_snapshot_supervisor_schedule() {
  snapshot=$1
  [ -d "$snapshot" ] || return 1
  current=$(mktemp "${TMPDIR:-/tmp}/uvarsi-cron-current.XXXXXX") || return 1
  extracted=$(mktemp "${TMPDIR:-/tmp}/uvarsi-cron-snapshot.XXXXXX") || {
    rm -f "$current"
    return 1
  }
  chmod 600 "$current" "$extracted" || {
    rm -f "$current" "$extracted"
    return 1
  }
  if ! _uvarsi_read_crontab "$current" || ! "$UVARSI_HEALTH_PY" -c '
import re, sys
direct = re.compile(r"(?:^|\s)/opt/uvarsi/dozorca\.sh(?:\s|$)")
wrapped = re.compile(
    r"(?:^|\s)/opt/uvarsi/uvarsi-deploy-state\.sh\s+run-supervisor(?:\s|$)"
)
with open(sys.argv[1], encoding="utf-8") as source, open(
    sys.argv[2], "w", encoding="utf-8", newline="\n"
) as target:
    for line in source:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and (
            direct.search(stripped) or wrapped.search(stripped)
        ):
            target.write(stripped + "\n")
' "$current" "$extracted" >/dev/null 2>&1; then
    rm -f "$current" "$extracted"
    return 1
  fi
  "$UVARSI_CP" -a "$extracted" "$snapshot/supervisor.cron" || {
    rm -f "$current" "$extracted"
    return 1
  }
  chmod 600 "$snapshot/supervisor.cron" || {
    rm -f "$current" "$extracted"
    return 1
  }
  "$UVARSI_CP" -a "$current" "$snapshot/crontab.full" || {
    rm -f "$current" "$extracted"
    return 1
  }
  chmod 600 "$snapshot/crontab.full" || {
    rm -f "$current" "$extracted"
    return 1
  }
  rm -f "$current" "$extracted"
}

uvarsi_require_supervisor_schedule() {
  current=$(mktemp "${TMPDIR:-/tmp}/uvarsi-cron-check.XXXXXX") || return 1
  chmod 600 "$current" || { rm -f "$current"; return 1; }
  canonical=$(_uvarsi_supervisor_cron_line) || { rm -f "$current"; return 1; }
  if ! _uvarsi_read_crontab "$current" || ! "$UVARSI_HEALTH_PY" -c '
import re, sys
path, canonical = sys.argv[1:3]
direct = re.compile(r"(?:^|\s)/opt/uvarsi/dozorca\.sh(?:\s|$)")
wrapped = re.compile(
    r"(?:^|\s)/opt/uvarsi/uvarsi-deploy-state\.sh\s+run-supervisor(?:\s|$)"
)
with open(path, encoding="utf-8") as handle:
    active = [line.strip() for line in handle if line.strip() and not line.lstrip().startswith("#")]
targets = [line for line in active if direct.search(line) or wrapped.search(line)]
raise SystemExit(0 if targets == [canonical] else 1)
' "$current" "$canonical" >/dev/null 2>&1; then
    rm -f "$current"
    return 1
  fi
  rm -f "$current"
}

_uvarsi_supervisor_schedule_matches() (
  uvarsi_expected_file=$1
  uvarsi_verify_current=$(mktemp "${TMPDIR:-/tmp}/uvarsi-cron-verify.XXXXXX") || return 1
  chmod 600 "$uvarsi_verify_current" || { rm -f "$uvarsi_verify_current"; return 1; }
  if ! _uvarsi_read_crontab "$uvarsi_verify_current" || ! "$UVARSI_HEALTH_PY" -c '
import re, sys
current_path, expected_path = sys.argv[1:3]
direct = re.compile(r"(?:^|\s)/opt/uvarsi/dozorca\.sh(?:\s|$)")
wrapped = re.compile(
    r"(?:^|\s)/opt/uvarsi/uvarsi-deploy-state\.sh\s+run-supervisor(?:\s|$)"
)

def targets(path):
    with open(path, encoding="utf-8") as handle:
        return [
            line.strip() for line in handle
            if line.strip() and not line.lstrip().startswith("#")
            and (direct.search(line) or wrapped.search(line))
        ]

raise SystemExit(0 if targets(current_path) == targets(expected_path) else 1)
' "$uvarsi_verify_current" "$uvarsi_expected_file" >/dev/null 2>&1; then
    rm -f "$uvarsi_verify_current"
    return 1
  fi
  rm -f "$uvarsi_verify_current"
)

_uvarsi_apply_supervisor_schedule() (
  uvarsi_replacement=$1
  [ -f "$uvarsi_replacement" ] || return 1
  uvarsi_before=$(mktemp "${TMPDIR:-/tmp}/uvarsi-cron-before.XXXXXX") || return 1
  uvarsi_candidate=$(mktemp "${TMPDIR:-/tmp}/uvarsi-cron-candidate.XXXXXX") || {
    rm -f "$uvarsi_before"
    return 1
  }
  uvarsi_rollback_source=$(mktemp "${TMPDIR:-/tmp}/uvarsi-cron-rollback-source.XXXXXX") || {
    rm -f "$uvarsi_before" "$uvarsi_candidate"
    return 1
  }
  uvarsi_rollback_candidate=$(mktemp "${TMPDIR:-/tmp}/uvarsi-cron-rollback.XXXXXX") || {
    rm -f "$uvarsi_before" "$uvarsi_candidate" "$uvarsi_rollback_source"
    return 1
  }
  uvarsi_previous=$(mktemp "${TMPDIR:-/tmp}/uvarsi-cron-previous.XXXXXX") || {
    rm -f "$uvarsi_before" "$uvarsi_candidate" \
      "$uvarsi_rollback_source" "$uvarsi_rollback_candidate"
    return 1
  }
  chmod 600 "$uvarsi_before" "$uvarsi_candidate" \
      "$uvarsi_rollback_source" "$uvarsi_rollback_candidate" "$uvarsi_previous" || {
    rm -f "$uvarsi_before" "$uvarsi_candidate" \
      "$uvarsi_rollback_source" "$uvarsi_rollback_candidate" "$uvarsi_previous"
    return 1
  }

  # Serialize every Uvar.si cron writer. The transform below removes and adds
  # only the supervisor row; all unrelated root jobs are copied unchanged.
  exec 9>"$UVARSI_CRON_LOCK" || {
    rm -f "$uvarsi_before" "$uvarsi_candidate" \
      "$uvarsi_rollback_source" "$uvarsi_rollback_candidate" "$uvarsi_previous"
    return 1
  }
  "$UVARSI_FLOCK" -x 9 >/dev/null 2>&1 || {
    rm -f "$uvarsi_before" "$uvarsi_candidate" \
      "$uvarsi_rollback_source" "$uvarsi_rollback_candidate" "$uvarsi_previous"
    return 1
  }
  if ! _uvarsi_read_crontab "$uvarsi_before" || \
      ! _uvarsi_extract_supervisor_cron "$uvarsi_before" "$uvarsi_previous" || \
      ! _uvarsi_transform_supervisor_cron \
        "$uvarsi_before" "$uvarsi_candidate" "$uvarsi_replacement"; then
    rm -f "$uvarsi_before" "$uvarsi_candidate" \
      "$uvarsi_rollback_source" "$uvarsi_rollback_candidate" "$uvarsi_previous"
    return 1
  fi
  if ! "$UVARSI_CRONTAB" "$uvarsi_candidate" >/dev/null 2>&1 || \
      ! _uvarsi_supervisor_schedule_matches "$uvarsi_replacement"; then
    # Prefer a targeted rollback based on the newest readable crontab, so even
    # a concurrent unrelated edit survives. If the crontab cannot be read,
    # restore the exact pre-write snapshot rather than leave a half-verified row.
    if _uvarsi_read_crontab "$uvarsi_rollback_source" && \
        _uvarsi_transform_supervisor_cron \
          "$uvarsi_rollback_source" "$uvarsi_rollback_candidate" "$uvarsi_previous"; then
      "$UVARSI_CRONTAB" "$uvarsi_rollback_candidate" >/dev/null 2>&1 || true
    else
      "$UVARSI_CRONTAB" "$uvarsi_before" >/dev/null 2>&1 || true
    fi
    rm -f "$uvarsi_before" "$uvarsi_candidate" \
      "$uvarsi_rollback_source" "$uvarsi_rollback_candidate" "$uvarsi_previous"
    return 1
  fi
  rm -f "$uvarsi_before" "$uvarsi_candidate" \
    "$uvarsi_rollback_source" "$uvarsi_rollback_candidate" "$uvarsi_previous"
)

uvarsi_install_supervisor_schedule() {
  if uvarsi_require_supervisor_schedule; then
    _uvarsi_release_trace schedule_ok
    return 0
  fi
  replacement=$(mktemp "${TMPDIR:-/tmp}/uvarsi-cron-replacement.XXXXXX") || {
    _uvarsi_release_trace schedule_failed
    return 1
  }
  chmod 600 "$replacement" || {
    rm -f "$replacement"
    _uvarsi_release_trace schedule_failed
    return 1
  }
  canonical=$(_uvarsi_supervisor_cron_line) || {
    rm -f "$replacement"
    _uvarsi_release_trace schedule_failed
    return 1
  }
  printf '%s\n' "$canonical" > "$replacement" || {
    rm -f "$replacement"
    _uvarsi_release_trace schedule_failed
    return 1
  }
  if _uvarsi_apply_supervisor_schedule "$replacement" && \
      uvarsi_require_supervisor_schedule; then
    rm -f "$replacement"
    _uvarsi_release_trace schedule_ok
    return 0
  fi
  rm -f "$replacement"
  _uvarsi_release_trace schedule_failed
  return 1
}

uvarsi_restore_supervisor_schedule() {
  snapshot=$1
  previous="$snapshot/supervisor.cron"
  [ -f "$previous" ] || return 1
  # Restore only Uvar.si's supervisor row. Any unrelated job added or changed
  # during the release (including the co-hosted Taktik app) stays untouched.
  _uvarsi_apply_supervisor_schedule "$previous"
}

_uvarsi_transform_production_cron() {
  source_file=$1
  target_file=$2
  supervisor=$(_uvarsi_supervisor_cron_line) || return 1
  backup=$(_uvarsi_backup_cron_line) || return 1
  payment=$(_uvarsi_payment_cron_line) || return 1
  "$UVARSI_HEALTH_PY" -c '
import re, sys
source, target, supervisor, backup, payment = sys.argv[1:6]
patterns = (
    re.compile(r"(?:^|\s)/opt/uvarsi/dozorca\.sh(?:\s|$)"),
    re.compile(r"(?:^|\s)/opt/uvarsi/uvarsi-deploy-state\.sh\s+run-supervisor(?:\s|$)"),
    re.compile(r"(?:^|\s)/opt/uvarsi/zaloha\.sh(?:\s|$)"),
    re.compile(r"(?:^|\s)/opt/uvarsi/venv/bin/python\s+rekonciliacia\.py(?:\s|$)"),
)
with open(source, encoding="utf-8") as handle:
    lines = [line.rstrip("\n") for line in handle]
kept = [
    line for line in lines
    if line.lstrip().startswith("#") or not any(pattern.search(line) for pattern in patterns)
]
with open(target, "w", encoding="utf-8", newline="\n") as handle:
    for line in kept + [supervisor, backup, payment]:
        handle.write(line + "\n")
' "$source_file" "$target_file" "$supervisor" "$backup" "$payment" >/dev/null 2>&1
}

uvarsi_require_production_schedule() {
  current=$(mktemp "${TMPDIR:-/tmp}/uvarsi-cron-check.XXXXXX") || return 1
  chmod 600 "$current" || { rm -f "$current"; return 1; }
  supervisor=$(_uvarsi_supervisor_cron_line) || { rm -f "$current"; return 1; }
  backup=$(_uvarsi_backup_cron_line) || { rm -f "$current"; return 1; }
  payment=$(_uvarsi_payment_cron_line) || { rm -f "$current"; return 1; }
  if ! _uvarsi_read_crontab "$current" || ! "$UVARSI_HEALTH_PY" -c '
import re, sys
path, supervisor, backup, payment = sys.argv[1:5]
with open(path, encoding="utf-8") as handle:
    active = [line.strip() for line in handle if line.strip() and not line.lstrip().startswith("#")]
expected = (supervisor, backup, payment)
patterns = (
    re.compile(r"(?:^|\s)/opt/uvarsi/(?:dozorca\.sh|uvarsi-deploy-state\.sh\s+run-supervisor)(?:\s|$)"),
    re.compile(r"(?:^|\s)/opt/uvarsi/zaloha\.sh(?:\s|$)"),
    re.compile(r"(?:^|\s)/opt/uvarsi/venv/bin/python\s+rekonciliacia\.py(?:\s|$)"),
)
unique = all(active.count(line) == 1 for line in expected)
no_variants = all(
    [line for line in active if pattern.search(line)] == [canonical]
    for pattern, canonical in zip(patterns, expected)
)
raise SystemExit(0 if unique and no_variants else 1)
' "$current" "$supervisor" "$backup" "$payment" >/dev/null 2>&1; then
    rm -f "$current"
    return 1
  fi
  rm -f "$current"
  uvarsi_require_supervisor_schedule
}

uvarsi_install_production_schedule() {
  # Verify-only for the same reason as the supervisor schedule above.
  uvarsi_require_production_schedule
}

_uvarsi_record_supervisor_success() {
  today=$(_uvarsi_today) || return 1
  now=$(_uvarsi_now_epoch) || return 1
  case "$now" in ''|*[!0-9]*) return 1 ;; esac
  temporary="${UVARSI_SUPERVISOR_SUCCESS_STATE}.tmp.$$"
  (umask 077; printf '%s %s\n' "$today" "$now" > "$temporary") || return 1
  "$UVARSI_MV" "$temporary" "$UVARSI_SUPERVISOR_SUCCESS_STATE" || {
    rm -f "$temporary"
    return 1
  }
}

_uvarsi_require_supervisor_liveness() {
  uvarsi_require_supervisor_schedule || return 1
  [ -s "$UVARSI_SUPERVISOR_SUCCESS_STATE" ] || return 1
  read -r success_day success_epoch success_extra < "$UVARSI_SUPERVISOR_SUCCESS_STATE" || return 1
  [ -z "${success_extra:-}" ] || return 1
  today=$(_uvarsi_today) || return 1
  now=$(_uvarsi_now_epoch) || return 1
  case "$success_epoch" in ''|*[!0-9]*) return 1 ;; esac
  case "$now" in ''|*[!0-9]*) return 1 ;; esac
  case "$UVARSI_SUPERVISOR_SUCCESS_MAX_AGE_SECONDS" in
    ''|*[!0-9]*) return 1 ;;
  esac
  [ "$success_day" = "$today" ] || return 1
  [ "$success_epoch" -le "$now" ] || return 1
  [ $((now - success_epoch)) -le "$UVARSI_SUPERVISOR_SUCCESS_MAX_AGE_SECONDS" ]
}

_uvarsi_require_runtime_services() {
  "$UVARSI_SYSTEMCTL" is-active --quiet uvarsi || return 1
  "$UVARSI_SYSTEMCTL" is-active --quiet uvarsi-plan-worker || return 1
  [ -x "$UVARSI_SUPERVISOR" ] || return 1

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

_uvarsi_require_runtime_health() {
  _uvarsi_require_runtime_services || return 1
  today=$(_uvarsi_today) || return 1
  if [ -s "$UVARSI_SUPERVISOR_STATE" ]; then
    read -r failed_day _rest < "$UVARSI_SUPERVISOR_STATE" || return 1
    [ "$failed_day" != "$today" ] || return 1
  fi
  [ ! -s "$UVARSI_COLLECTION_FAILURE_STATE" ] || return 1
  _uvarsi_require_supervisor_liveness
}

uvarsi_require_production_readiness() {
  # Every call is quiet: callers report stable reason codes, never response
  # bodies, bearer headers or environment values.
  if [ "$UVARSI_LEGACY_CODE_DEPLOY" = 1 ]; then
    if uvarsi_require_code_deploy_readiness; then
      _uvarsi_release_trace production_ready
      return 0
    fi
    _uvarsi_release_trace production_failed
    return 1
  fi
  if uvarsi_require_payments_off && \
      uvarsi_require_runtime_payments_off && \
      _uvarsi_require_collection_readiness && \
      _uvarsi_require_runtime_health; then
    _uvarsi_release_trace production_ready
    return 0
  fi
  _uvarsi_release_trace production_failed
  return 1
}

uvarsi_require_code_deploy_readiness() {
  # Application releases and external weekly collection have separate health
  # boundaries.  This gate never claims that prices or a receipt are current.
  uvarsi_require_payments_off || return 1
  uvarsi_require_runtime_payments_off || return 1
  uvarsi_require_supervisor_schedule || return 1
  _uvarsi_require_runtime_services
}

_uvarsi_supervisor_cycle() {
  [ "${UVARSI_BOUNDED_CYCLE:-0}" = 1 ] || return 1
  uvarsi_require_payments_off || return 1
  if ! _uvarsi_require_collection_readiness; then
    if ! _uvarsi_require_official_offer_data; then
      _uvarsi_require_tesco_bridge_transport || return 1
      (
        cd "$UVARSI_APP_DIR" || exit 1
        "$UVARSI_HEALTH_PY" -u "$UVARSI_COLLECTOR"
      ) || return 1
    fi
    "$UVARSI_HEALTH_PY" -u "$UVARSI_RECEIPT_REFRESH" \
      "$UVARSI_LANDING_DATA" || return 1
    _uvarsi_require_collection_readiness || return 1
  fi
  "$UVARSI_SUPERVISOR"
}

_uvarsi_run_supervisor_bounded() {
  # The collector and receipt writer stage their candidate state. Killing this
  # wrapper on timeout therefore leaves the live DB rows and landing JSON as-is.
  uvarsi_require_payments_off || return 1
  if ! _uvarsi_require_official_offer_data; then
    if [ "$UVARSI_LEGACY_CODE_DEPLOY" != 1 ] && \
       [ "${UVARSI_CODE_DEPLOY:-0}" != 1 ]; then
      _uvarsi_require_tesco_bridge_transport || return 1
    fi
  fi
  case "$UVARSI_MAX_COLLECTION_SECONDS" in
    ''|*[!0-9]*) return 1 ;;
  esac
  [ "$UVARSI_MAX_COLLECTION_SECONDS" -gt 0 ] || return 1
  [ "$UVARSI_MAX_COLLECTION_SECONDS" -le 14400 ] || return 1
  case "$UVARSI_TERMINATION_GRACE_SECONDS" in
    ''|*[!0-9]*) return 1 ;;
  esac
  [ "$UVARSI_TERMINATION_GRACE_SECONDS" -gt 0 ] || return 1
  [ "$UVARSI_TERMINATION_GRACE_SECONDS" -lt "$UVARSI_MAX_COLLECTION_SECONDS" ] || return 1
  [ -x "$UVARSI_SUPERVISOR" ] || return 1
  uvarsi_require_supervisor_schedule || return 1
  rm -f "$UVARSI_SUPERVISOR_SUCCESS_STATE" || return 1
  terminate_after=$((UVARSI_MAX_COLLECTION_SECONDS - UVARSI_TERMINATION_GRACE_SECONDS))
  if _uvarsi_require_collection_readiness; then
    bounded_command=$UVARSI_SUPERVISOR
    bounded_argument=
  else
    bounded_command=$UVARSI_BASH
    bounded_argument=$UVARSI_DEPLOY_STATE_SCRIPT
  fi
  if [ -n "$bounded_argument" ]; then
    if UVARSI_BOUNDED_CYCLE=1 "$UVARSI_TIMEOUT" --signal=TERM \
        --kill-after="$UVARSI_TERMINATION_GRACE_SECONDS" "$terminate_after" \
        "$bounded_command" "$bounded_argument" internal-supervisor-cycle; then
      result=0
    else
      result=$?
    fi
  elif "$UVARSI_TIMEOUT" --signal=TERM \
      --kill-after="$UVARSI_TERMINATION_GRACE_SECONDS" "$terminate_after" \
      "$bounded_command"; then
    result=0
  else
    result=$?
  fi
  uvarsi_require_payments_off || return 1
  if [ "$result" -eq 0 ]; then
    if _uvarsi_require_collection_readiness; then
      _uvarsi_record_supervisor_success || return 1
    elif [ "$UVARSI_LEGACY_CODE_DEPLOY" != 1 ] && \
         [ "${UVARSI_CODE_DEPLOY:-0}" != 1 ]; then
      return 1
    fi
  elif [ "$UVARSI_LEGACY_CODE_DEPLOY" = 1 ]; then
    # The old caller may finish installing code after a fail-closed collection
    # attempt.  Do not record a false supervisor success or touch data state.
    return 0
  fi
  return "$result"
}

uvarsi_run_supervisor_bounded() {
  if _uvarsi_run_supervisor_bounded; then
    _uvarsi_release_trace supervisor_ok
    return 0
  else
    result=$?
    _uvarsi_release_trace supervisor_failed
    return "$result"
  fi
}

uvarsi_bootstrap_production_readiness() {
  # This is intentionally ordered so a first official rollout may replace
  # stale/aggregator staging before the strict current-data gate evaluates it.
  # Payments still fail closed first. The authenticated bridge is required only
  # when current verified official offer data are not already reusable.
  uvarsi_require_payments_off || return 1
  uvarsi_require_runtime_payments_off || return 1
  uvarsi_require_supervisor_schedule || return 1
  uvarsi_run_supervisor_bounded || return 1
  uvarsi_require_production_readiness
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
  if health=$(
      "$UVARSI_CURL" -fsS --max-time 5 "$UVARSI_HEALTH_URL" 2>/dev/null
    ); then
    printf '%s' "$health" | "$UVARSI_HEALTH_PY" -c '
import json, sys
try:
    payload = json.load(sys.stdin)
    enabled = payload["recipe_engine"]["payments_enabled"]
except (KeyError, TypeError, ValueError, json.JSONDecodeError):
    raise SystemExit(1)
if enabled is False:
    raise SystemExit(0)
raise SystemExit(2 if enabled is True else 1)
'
    health_status=$?
    if [ "$health_status" -eq 0 ]; then
      _uvarsi_release_trace runtime_payments_ok
      return 0
    fi
    # A valid live ON signal must never be overridden by the process fallback.
    _uvarsi_release_trace runtime_payments_failed
    return 1
  fi
  if _uvarsi_called_from_samopull && _uvarsi_require_process_payments_off; then
    _uvarsi_release_trace runtime_payments_ok
    return 0
  fi
  _uvarsi_release_trace runtime_payments_failed
  return 1
}

uvarsi_migrate_release() {
  # Candidate migrations run before any health check. A release that changes
  # schema must prove old-code compatibility separately; automated rollback
  # must never overwrite newer customer/payment rows with the online backup.
  release=$1
  uvarsi_require_payments_off || {
    _uvarsi_release_trace migration_failed; return 1; }
  [ -d "$release/app" ] || {
    _uvarsi_release_trace migration_failed; return 1; }
  [ -f "$release/VERSION" ] || {
    _uvarsi_release_trace migration_failed; return 1; }
  (
    cd "$release/app" || exit 1
    UVARSI_URL=https://uvar.si \
      UVARSI_DB="$UVARSI_DB" \
      UVARSI_VERSION_FILE="$release/VERSION" \
      PLATBY_ZAPNUTE=0 UVARSI_PAYMENTS_ENABLED=0 \
      "$UVARSI_HEALTH_PY" -c 'import server; server.priprav_databazu()'
  ) || { _uvarsi_release_trace migration_failed; return 1; }
  if uvarsi_require_payments_off; then
    _uvarsi_release_trace migration_ok
    return 0
  fi
  _uvarsi_release_trace migration_failed
  return 1
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
    _uvarsi_release_trace install_core_ok
    return 0
  fi
  _uvarsi_release_trace install_core_failed
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
      _uvarsi_release_trace heartbeat_ok
      return 0
    fi
    "$UVARSI_SLEEP" 1
    attempt=$((attempt + 1))
  done
  if [ "$UVARSI_LEGACY_CODE_DEPLOY" = 1 ]; then
    # The legacy deployer compares timestamps before the final runtime gate.
    # A same-second restart may not advance that marker even though the worker
    # is healthy.  The final code gate still requires worker_alive=true and a
    # heartbeat at most 60 seconds old, so only the brittle comparison is
    # relaxed during this one process-local transition.
    _uvarsi_release_trace heartbeat_compat
    return 0
  fi
  return 1
}

if [ "${BASH_SOURCE[0]}" = "$0" ]; then
  case "${1:-}" in
    check-bridge) _uvarsi_require_tesco_bridge_transport ;;
    check-readiness) uvarsi_require_production_readiness ;;
    run-supervisor)
      # Prechodové vydanie nás môže zavolať cez `bash subor` ešte
      # predtým, než nový samopull nastaví execute bit pre priamy cron.
      chmod +x "$UVARSI_DEPLOY_STATE_SCRIPT" || exit 1
      uvarsi_run_supervisor_bounded
      ;;
    internal-supervisor-cycle) _uvarsi_supervisor_cycle ;;
    *) exit 64 ;;
  esac
fi
