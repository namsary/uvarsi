#!/bin/bash
# Autonomous, fail-closed deterministic recipe-engine activation.
set -u

DIR="${UVARSI_DIR:-/opt/uvarsi}"
PY="${UVARSI_PY:-$DIR/venv/bin/python}"
HEALTH_PY="${UVARSI_HEALTH_PY:-$PY}"
CURL="${UVARSI_CURL:-curl}"
SYSTEMCTL="${UVARSI_SYSTEMCTL:-systemctl}"
MV="${UVARSI_MV:-mv}"
TARGET="${UVARSI_RECIPE_TARGET:-$DIR/recipe-engine.target}"
TARGET_READER="${UVARSI_RECIPE_TARGET_READER:-cat}"
FLAG="${UVARSI_RECIPE_FLAG_FILE:-$DIR/uvarsi-recipe-engine.env}"
HEALTH_URL="${UVARSI_HEALTH_URL:-http://127.0.0.1:8090/api/health}"
LOCK="${UVARSI_RECIPE_ROLLOUT_LOCK:-/var/lock/uvarsi-recipe-rollout.lock}"
SMOKE_STATE="${UVARSI_RECIPE_SMOKE_STATE:-/var/lib/uvarsi/recipe_engine_smoke.json}"
NOTIFY_URL="${UVARSI_NOTIFY_URL:-}"
UVARSI_URL="${UVARSI_URL:-https://uvar.si}"
export UVARSI_URL

log(){ echo "[$(date '+%F %T')] RECIPE-ROLLOUT: $*"; }
smoke_detail(){
  [ -s "$SMOKE_STATE" ] || return 0
  "$HEALTH_PY" -c '
import json, math, re, sys

with open(sys.argv[1], encoding="utf-8") as handle:
    payload = json.load(
        handle,
        parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)),
    )
if not isinstance(payload, dict):
    raise SystemExit(0)

safe = {}
blockers = payload.get("blockers")
if isinstance(blockers, list):
    safe["blockers"] = [
        value
        for value in blockers[:8]
        if isinstance(value, str)
        and re.fullmatch(r"[a-z0-9_:-]{1,64}", value)
    ]
for key in ("latency_ms", "jobs_delta", "ai_costs_delta"):
    value = payload.get(key)
    if (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    ):
        safe[key] = value
for key, allowed in {
    "engine_mode": {"off", "shadow", "on"},
    "plan_engine": {"deterministic", "legacy"},
}.items():
    value = payload.get(key)
    if value in allowed:
        safe[key] = value
if safe:
    print(json.dumps(safe, ensure_ascii=True, separators=(",", ":")))
' "$SMOKE_STATE" 2>/dev/null
}
notify_failure(){
  STATUS="$1"
  GATE="${2:-unknown}"
  if [ -z "$NOTIFY_URL" ]; then
    log "notifikačný kanál nie je nakonfigurovaný"
    return 0
  fi
  if [ "$STATUS" = "complete" ]; then
    BODY="Rollback complete: recipe engine is off and both Uvar.si services are active. gate=$GATE"
  else
    BODY="Rollback incomplete: recipe-engine flag or Uvar.si service recovery failed. gate=$GATE"
  fi
  if [ "$GATE" = "on_smoke" ]; then
    DETAIL=$(smoke_detail || true)
    [ -z "$DETAIL" ] || BODY="$BODY detail=$DETAIL"
  fi
  "$CURL" -sS --max-time 15 -H "Title: Uvar.si: receptový rollout zlyhal" \
    -d "$BODY" "$NOTIFY_URL" \
    >/dev/null 2>&1 || true
}
notify_lock_held(){
  [ -n "$NOTIFY_URL" ] || return 0
  "$CURL" -sS --max-time 15 -H "Title: Uvar.si: receptový rollout čaká" \
    -d "Rollout not started: gate=lock_held; engine unchanged." "$NOTIFY_URL" \
    >/dev/null 2>&1 || true
}

if [ "${UVARSI_ROLLOUT_LOCKED:-0}" != "1" ]; then
  exec 8>"$LOCK" || exit 1
  flock -n 8 || { notify_lock_held; exit 0; }
fi

set_mode() {
  case "$1" in off|shadow|on) ;; *) return 1 ;; esac
  TMP_FLAG="$FLAG.tmp.$$"
  umask 077
  printf 'UVARSI_RECIPE_ENGINE=%s\n' "$1" > "$TMP_FLAG" || return 1
  if ! "$MV" -f "$TMP_FLAG" "$FLAG"; then
    rm -f "$TMP_FLAG"
    return 1
  fi
}

current_mode() {
  [ -f "$FLAG" ] || { printf 'invalid\n'; return; }
  {
    IFS= read -r LINE || { printf 'invalid\n'; return; }
    EXTRA_LINE=
    if IFS= read -r EXTRA_LINE || [ -n "$EXTRA_LINE" ]; then
      printf 'invalid\n'
      return
    fi
  } < "$FLAG"
  case "$LINE" in
    UVARSI_RECIPE_ENGINE=off|export\ UVARSI_RECIPE_ENGINE=off) printf 'off\n' ;;
    UVARSI_RECIPE_ENGINE=shadow|export\ UVARSI_RECIPE_ENGINE=shadow) printf 'shadow\n' ;;
    UVARSI_RECIPE_ENGINE=on|export\ UVARSI_RECIPE_ENGINE=on) printf 'on\n' ;;
    *) printf 'invalid\n' ;;
  esac
}

restart_uvarsi() {
  RESTART_OK=1
  "$SYSTEMCTL" restart uvarsi || RESTART_OK=0
  "$SYSTEMCTL" restart uvarsi-plan-worker || RESTART_OK=0
  "$SYSTEMCTL" is-active --quiet uvarsi || RESTART_OK=0
  "$SYSTEMCTL" is-active --quiet uvarsi-plan-worker || RESTART_OK=0
  [ "$RESTART_OK" -eq 1 ]
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
    if not isinstance(shadow, dict): raise SystemExit(2)
    if shadow.get("complete") is not True or shadow.get("eligible") is not True: raise SystemExit(2)
    def finite_number(value):
        return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
    success_rate = shadow.get("success_rate")
    p95_ms = shadow.get("p95_ms")
    if not finite_number(success_rate) or not (0.0 <= success_rate <= 1.0) or success_rate < 0.98: raise SystemExit(2)
    if not finite_number(p95_ms) or not (0.0 <= p95_ms < 500): raise SystemExit(2)
    for key in ("dietary_violations","negative_quantities","invalid_package_counts"):
        value = shadow.get(key)
        if not finite_number(value) or value != 0: raise SystemExit(2)
' "$EXPECTED"
}

payments_off() {
  (cd "$DIR/app" && UVARSI_RECIPE_ENGINE="${1:-off}" "$PY" -c '
import server
enabled = server.platby_su_zapnute() or server.platby_zapnute(server.env("UVARSI_PAYMENTS_ENABLED"))
raise SystemExit(1 if enabled else 0)
')
}

read_activation_target() {
  TARGET_COPY=$(mktemp "${FLAG}.target-read.XXXXXX") || return 1
  umask 077
  if ! "$TARGET_READER" "$TARGET" > "$TARGET_COPY"; then
    rm -f "$TARGET_COPY"
    return 1
  fi

  TARGET_VALID=1
  {
    IFS= read -r TARGET_VALUE || TARGET_VALID=0
    TARGET_VALUE=${TARGET_VALUE%$'\r'}
    [ "$TARGET_VALUE" = "on" ] || TARGET_VALID=0
    TARGET_EXTRA=
    if IFS= read -r TARGET_EXTRA || [ -n "$TARGET_EXTRA" ]; then TARGET_VALID=0; fi
  } < "$TARGET_COPY"
  rm -f "$TARGET_COPY" || return 1
  [ "$TARGET_VALID" -eq 1 ]
}

rollback_off() {
  log "gate zlyhal — vraciam iba receptový flag na off"
  ROLLBACK_OK=1
  set_mode off || ROLLBACK_OK=0
  restart_uvarsi || ROLLBACK_OK=0
  if [ "$ROLLBACK_OK" -eq 1 ]; then
    log "rollback complete — flag je off a obe Uvar.si služby sú aktívne"
    notify_failure complete "$ROLLOUT_GATE"
  else
    log "rollback incomplete — flag alebo služby vyžadujú zásah"
    notify_failure incomplete "$ROLLOUT_GATE"
  fi
  exit 1
}

ROLLOUT_GATE=target
[ -f "$TARGET" ] || { log "bez aktivačného cieľa — končím"; exit 0; }
read_activation_target || rollback_off

ROLLOUT_GATE=package
[ -d "$DIR/app" ] && [ -s "$DIR/VERSION" ] || rollback_off
for required in config.py server.py deterministic_plan.py ingredient_catalog.py \
  library_gate.py quantity_math.py recipe_catalog.py recipe_matcher.py recipe_renderer.py; do
  [ -s "$DIR/app/$required" ] || rollback_off
done
[ -s "$DIR/app/catalog/ingredients.json" ] || rollback_off
[ -s "$DIR/app/catalog/slovak_ingredient_forms.json" ] || rollback_off
[ -s "$DIR/app/catalog/recipes/manifest.json" ] || rollback_off
ROLLOUT_GATE=payments_off
payments_off off || rollback_off
ROLLOUT_GATE=library_gate
(cd "$DIR" && UVARSI_RECIPE_ENGINE=off \
  "$PY" -m app.library_gate >/dev/null) || rollback_off

ROLLOUT_GATE=flag
MODE=$(current_mode)
[ "$MODE" != "invalid" ] || rollback_off
if [ "$MODE" = "on" ]; then
  ROLLOUT_GATE=on_health
  payments_off on && health_gate on && { log "on je už zdravý — bez zmeny"; exit 0; }
  rollback_off
fi

ROLLOUT_GATE=shadow_restart
payments_off shadow || rollback_off
set_mode shadow || rollback_off
restart_uvarsi || rollback_off
payments_off shadow || rollback_off
ROLLOUT_GATE=shadow_matrix
(cd "$DIR/app" && UVARSI_RECIPE_ENGINE=shadow "$PY" -c '
import predpocet, server
result = predpocet.run_recipe_engine_shadow(server=server)
raise SystemExit(0 if result.get("complete") else 1)
') || rollback_off
ROLLOUT_GATE=shadow_health
health_gate shadow || rollback_off

ROLLOUT_GATE=on_restart
payments_off on || rollback_off
set_mode on || rollback_off
restart_uvarsi || rollback_off
payments_off on || rollback_off
ROLLOUT_GATE=on_smoke
rm -f "$SMOKE_STATE" || rollback_off
(cd "$DIR/app" && UVARSI_RECIPE_ENGINE=on UVARSI_RECIPE_SMOKE_STATE="$SMOKE_STATE" \
  "$PY" -m server --recipe-engine-smoke --state "$SMOKE_STATE" >/dev/null) || rollback_off
ROLLOUT_GATE=on_health
health_gate on || rollback_off

log "OK — deterministic recipe engine je on"
exit 0
