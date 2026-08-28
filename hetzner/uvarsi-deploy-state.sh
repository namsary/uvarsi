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
UVARSI_WEB_DIR="${UVARSI_WEB_DIR:-/var/www/uvarsi}"
UVARSI_WORKER_UNIT="$UVARSI_SYSTEMD_DIR/uvarsi-plan-worker.service"
UVARSI_APP_UNIT="$UVARSI_SYSTEMD_DIR/uvarsi.service"

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
    mv "$UVARSI_DIR/app" "$previous" || { rm -rf "$staged"; return 1; }
    had_previous=1
  fi
  if mv "$staged" "$UVARSI_DIR/app"; then
    if [ "$had_previous" -eq 1 ]; then
      rm -rf "$previous" || return 1
    fi
    return 0
  fi

  rm -rf "$staged"
  if [ "$had_previous" -eq 1 ]; then
    mv "$previous" "$UVARSI_DIR/app" || return 1
  fi
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
  for name in refresh_blocek.py recepty.py dozorca.sh zaloha.sh uvarsi-deploy-state.sh; do
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
  services_stopped=1
  [ -d "$snapshot/app" ] || return 1

  # No process may retain an old SQLite handle while rollback replaces the DB.
  if "$UVARSI_SYSTEMCTL" is-active --quiet uvarsi-plan-worker; then
    "$UVARSI_SYSTEMCTL" stop uvarsi-plan-worker >/dev/null 2>&1 || services_stopped=0
  fi
  if "$UVARSI_SYSTEMCTL" is-active --quiet uvarsi; then
    "$UVARSI_SYSTEMCTL" stop uvarsi >/dev/null 2>&1 || services_stopped=0
  fi
  if [ "$services_stopped" -eq 1 ]; then
    _uvarsi_restore_database "$snapshot" || ok=0
  else
    ok=0
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
  for name in refresh_blocek.py recepty.py dozorca.sh zaloha.sh uvarsi-deploy-state.sh; do
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

_uvarsi_apply_manual_targets() {
  release=$1
  [ -f "$release/index.html" ] || return 1
  [ -f "$release/sw.js" ] || return 1
  [ -f "$release/hetzner/uvarsi.service" ] || return 1
  for name in refresh_blocek.py recepty.py dozorca.sh zaloha.sh uvarsi-deploy-state.sh; do
    [ -f "$release/hetzner/$name" ] || return 1
  done
  "$UVARSI_CP" -a "$release/index.html" "$UVARSI_WEB_DIR/index.html" || return 1
  "$UVARSI_CP" -a "$release/sw.js" "$UVARSI_WEB_DIR/sw.js" || return 1
  for name in refresh_blocek.py recepty.py dozorca.sh zaloha.sh uvarsi-deploy-state.sh; do
    "$UVARSI_CP" -a "$release/hetzner/$name" "$UVARSI_DIR/$name" || return 1
  done
  chmod +x "$UVARSI_DIR/dozorca.sh" "$UVARSI_DIR/zaloha.sh" \
    "$UVARSI_DIR/uvarsi-deploy-state.sh" || return 1
  "$UVARSI_CP" -a "$release/hetzner/uvarsi.service" "$UVARSI_APP_UNIT" || return 1
  "$UVARSI_SYSTEMCTL" daemon-reload || return 1
}

uvarsi_install_manual_release() {
  release=$1
  snapshot=$2
  if _uvarsi_apply_core "$release" && _uvarsi_apply_manual_targets "$release"; then
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
