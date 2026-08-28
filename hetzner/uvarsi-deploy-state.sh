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
UVARSI_HEARTBEAT_ATTEMPTS="${UVARSI_HEARTBEAT_ATTEMPTS:-30}"
UVARSI_HEALTH_URL="${UVARSI_HEALTH_URL:-http://127.0.0.1:8090/api/health}"
UVARSI_DB="${UVARSI_DB:-$UVARSI_DIR/uvarsi.db}"
UVARSI_WORKER_UNIT="$UVARSI_SYSTEMD_DIR/uvarsi-plan-worker.service"

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
  if [ -f "$UVARSI_DIR/VERSION" ]; then
    "$UVARSI_CP" -a "$UVARSI_DIR/VERSION" "$snapshot/VERSION" || return 1
  else
    : > "$snapshot/VERSION.absent" || return 1
  fi
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
  _uvarsi_persisted_heartbeat > "$snapshot/heartbeat.before" || return 1
}

uvarsi_restore() {
  snapshot=$1
  ok=1
  [ -d "$snapshot/app" ] || return 1

  rm -rf "$UVARSI_DIR/app" || ok=0
  [ "$ok" -eq 0 ] || "$UVARSI_CP" -a "$snapshot/app" "$UVARSI_DIR/app" || ok=0
  if [ -f "$snapshot/VERSION" ]; then
    "$UVARSI_CP" -a "$snapshot/VERSION" "$UVARSI_DIR/VERSION" || ok=0
  elif [ -f "$snapshot/VERSION.absent" ]; then
    rm -f "$UVARSI_DIR/VERSION" || ok=0
  else
    ok=0
  fi

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

  "$UVARSI_SYSTEMCTL" restart uvarsi || ok=0
  "$UVARSI_SYSTEMCTL" is-active --quiet uvarsi || ok=0
  [ "$ok" -eq 1 ]
}

_uvarsi_apply_core() {
  release=$1
  [ -d "$release/app" ] || return 1
  [ -f "$release/VERSION" ] || return 1
  [ -f "$release/hetzner/uvarsi-plan-worker.service" ] || return 1
  rm -rf "$UVARSI_DIR/app" || return 1
  "$UVARSI_CP" -a "$release/app" "$UVARSI_DIR/app" || return 1
  "$UVARSI_CP" -a "$release/VERSION" "$UVARSI_DIR/VERSION" || return 1
  "$UVARSI_CP" -a "$release/hetzner/uvarsi-plan-worker.service" "$UVARSI_WORKER_UNIT" || return 1
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
