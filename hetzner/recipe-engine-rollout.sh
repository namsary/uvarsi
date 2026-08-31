#!/bin/bash
# Autonomous, fail-closed deterministic recipe-engine activation.
set -u

DIR="${UVARSI_DIR:-/opt/uvarsi}"
PY="${UVARSI_PY:-$DIR/venv/bin/python}"
HEALTH_PY="${UVARSI_HEALTH_PY:-$PY}"
CURL="${UVARSI_CURL:-curl}"
SYSTEMCTL="${UVARSI_SYSTEMCTL:-systemctl}"
TARGET="${UVARSI_RECIPE_TARGET:-$DIR/recipe-engine.target}"
FLAG="${UVARSI_RECIPE_FLAG_FILE:-$DIR/uvarsi-recipe-engine.env}"
HEALTH_URL="${UVARSI_HEALTH_URL:-http://127.0.0.1:8090/api/health}"
LOCK="${UVARSI_RECIPE_ROLLOUT_LOCK:-/var/lock/uvarsi-recipe-rollout.lock}"
SMOKE_STATE="${UVARSI_RECIPE_SMOKE_STATE:-/var/lib/uvarsi/recipe_engine_smoke.json}"
NOTIFY_URL="${UVARSI_NOTIFY_URL:-https://ntfy.sh/uvarsi-jarvis-8f3a2c}"

log(){ echo "[$(date '+%F %T')] RECIPE-ROLLOUT: $*"; }
notify_failure(){
  "$CURL" -sS --max-time 15 -H "Title: Uvar.si: receptový rollout zlyhal" \
    -d "Receptový engine bol bezpečne vrátený na off." "$NOTIFY_URL" \
    >/dev/null 2>&1 || true
}

if [ "${UVARSI_ROLLOUT_LOCKED:-0}" != "1" ]; then
  exec 8>"$LOCK" || exit 1
  flock -n 8 || exit 0
fi

[ -f "$TARGET" ] || { log "bez aktivačného cieľa — končím"; exit 0; }
TARGET_VALUE=$(cat "$TARGET" 2>/dev/null || true)
[ "$TARGET_VALUE" = "on" ] || { log "neplatný aktivačný cieľ"; exit 1; }

set_mode() {
  case "$1" in off|shadow|on) ;; *) return 1 ;; esac
  TMP_FLAG="$FLAG.tmp.$$"
  umask 077
  printf 'UVARSI_RECIPE_ENGINE=%s\n' "$1" > "$TMP_FLAG" || return 1
  mv -f "$TMP_FLAG" "$FLAG" || return 1
}

current_mode() {
  [ -f "$FLAG" ] || { printf 'off\n'; return; }
  VALUE=$(sed -n 's/^[[:space:]]*\(export[[:space:]][[:space:]]*\)\?UVARSI_RECIPE_ENGINE[[:space:]]*=[[:space:]]*//p' "$FLAG" | head -1)
  case "$VALUE" in off|shadow|on) printf '%s\n' "$VALUE" ;; *) printf 'invalid\n' ;; esac
}

restart_uvarsi() {
  "$SYSTEMCTL" restart uvarsi || return 1
  "$SYSTEMCTL" restart uvarsi-plan-worker || return 1
  "$SYSTEMCTL" is-active --quiet uvarsi || return 1
  "$SYSTEMCTL" is-active --quiet uvarsi-plan-worker || return 1
}

health_gate() {
  EXPECTED="$1"
  "$CURL" -fsS --max-time 5 "$HEALTH_URL" 2>/dev/null |
    "$HEALTH_PY" -c '
import json, math, sys
expected = sys.argv[1]
payload = json.load(sys.stdin, parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
engine = payload.get("recipe_engine")
if not isinstance(engine, dict): raise SystemExit(2)
if engine.get("mode") != expected or engine.get("ready") is not True: raise SystemExit(2)
if engine.get("blockers") != []: raise SystemExit(2)
if expected == "shadow":
    shadow = engine.get("last_shadow")
    if not isinstance(shadow, dict) or shadow.get("eligible") is not True: raise SystemExit(2)
    if float(shadow.get("success_rate", 0)) < 0.98: raise SystemExit(2)
    if float(shadow.get("p95_ms", 999999)) >= 500: raise SystemExit(2)
    for key in ("dietary_violations","negative_quantities","invalid_package_counts"):
        if shadow.get(key) != 0: raise SystemExit(2)
' "$EXPECTED"
}

payments_off() {
  (cd "$DIR/app" && UVARSI_RECIPE_ENGINE="${1:-off}" "$PY" -c \
    'import server; raise SystemExit(1 if server.platby_su_zapnute() else 0)')
}

rollback_off() {
  log "gate zlyhal — vraciam iba receptový flag na off"
  set_mode off || true
  restart_uvarsi || true
  notify_failure
  exit 1
}

[ -d "$DIR/app" ] && [ -s "$DIR/VERSION" ] || rollback_off
for required in config.py server.py deterministic_plan.py ingredient_catalog.py \
  library_gate.py quantity_math.py recipe_catalog.py recipe_matcher.py recipe_renderer.py; do
  [ -s "$DIR/app/$required" ] || rollback_off
done
[ -s "$DIR/app/catalog/ingredients.json" ] || rollback_off
[ -s "$DIR/app/catalog/recipes/manifest.json" ] || rollback_off
payments_off off || rollback_off
(cd "$DIR" && UVARSI_RECIPE_ENGINE=off "$PY" -m app.library_gate >/dev/null) || rollback_off

MODE=$(current_mode)
[ "$MODE" != "invalid" ] || rollback_off
if [ "$MODE" = "on" ]; then
  payments_off on && health_gate on && { log "on je už zdravý — bez zmeny"; exit 0; }
  rollback_off
fi

set_mode shadow || rollback_off
restart_uvarsi || rollback_off
payments_off shadow || rollback_off
(cd "$DIR/app" && UVARSI_RECIPE_ENGINE=shadow "$PY" -c '
import predpocet, server
result = predpocet.run_recipe_engine_shadow(server=server)
raise SystemExit(0 if result.get("complete") else 1)
') || rollback_off
health_gate shadow || rollback_off

set_mode on || rollback_off
restart_uvarsi || rollback_off
payments_off on || rollback_off
(cd "$DIR/app" && UVARSI_RECIPE_ENGINE=on UVARSI_RECIPE_SMOKE_STATE="$SMOKE_STATE" \
  "$PY" -m server --recipe-engine-smoke --state "$SMOKE_STATE" >/dev/null) || rollback_off
health_gate on || rollback_off

log "OK — deterministic recipe engine je on"
exit 0
