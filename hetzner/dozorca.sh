#!/bin/bash
# Uvar.si — DOZORCA (beží na jarvise, plne autonómne, nezávislé od PC).
#
# Beží KAŽDÚ HODINU od 05:00 do 21:00. Pozrie sa, či bloček sedí na aktuálny
# týždeň:
#   • sedí  → okamžite skončí (žiadne API volanie, nula nákladov)
#   • nesedí → hneď skúsi obnoviť; keď to spadne, skúsi ZNOVA O HODINU
#             (nie zajtra) — takže výpadok v noci je vyriešený skôr, než
#             ľudia ráno nakupujú.
# Ochrana proti míňaniu kreditu: max 6 pokusov za deň. Po 2 neúspechoch
# pošle upozornenie (vieš o tom do ~2 h, nie o 3 dni).
#
# Inštalácia (cron):
#   0 5-21 * * * /opt/uvarsi/dozorca.sh >> /var/log/uvarsi.log 2>&1

set -u
DIR="${UVARSI_DIR:-/opt/uvarsi}"
LANDING_DATA="${UVARSI_LANDING_DATA:-/var/lib/uvarsi/landing_data.json}"
PY="${UVARSI_PY:-$DIR/venv/bin/python}"
STATE="$DIR/.dozorca_state"          # formát: "RRRR-MM-DD pocet_neuspechov"
MAX_TRIES=6                          # max pokusov za jeden deň
NOTIFY_AT=2                          # po koľkých neúspechoch upozorniť
NTFY_TOPIC="uvarsi-jarvis-8f3a2c"    # notifikácie: ntfy.sh/<topic>

log(){ echo "[$(date '+%F %T')] DOZORCA: $*"; }
notify(){ curl -s --max-time 15 -H "Title: $1" -d "$2" "https://ntfy.sh/${NTFY_TOPIC}" >/dev/null 2>&1 || true; }

TODAY="${UVARSI_TODAY:-$(date +%F)}"

MON_ISO=$("$PY" -c 'from datetime import date, timedelta; import sys; d=date.fromisoformat(sys.argv[1]); print((d-timedelta(days=d.weekday())).isoformat())' "$TODAY")

landing_data_is_current() {
  (cd "$DIR" && "$PY" -c 'from app.landing_data import landing_data_is_current; from datetime import date; import sys; raise SystemExit(0 if landing_data_is_current(sys.argv[1], date.fromisoformat(sys.argv[2])) else 1)' "$LANDING_DATA" "$TODAY")
}

# --- 0. Databáza akcií pre appku: má aktuálny týždeň? ---
# (appka skladá osobné plány z tejto DB; bez nej ľuďom nič nevygeneruje)
POCET=$(sqlite3 "$DIR/uvarsi.db" \
        "SELECT COUNT(*) FROM akcie WHERE tyzden='$MON_ISO'" 2>/dev/null || echo 0)
if [ "${POCET:-0}" -lt 30 ]; then
  log "akcie pre týždeň $MON_ISO chýbajú ($POCET) — spúšťam zbierač…"
  if cd "$DIR/app" && "$PY" -u zbierac_akcii.py; then
    log "zbierač OK"
  else
    log "zbierač zlyhal — appka zatiaľ nemá aktuálne dáta"
  fi
fi

# --- 1. Už je aktuálny landing JSON pripravený? ---
if landing_data_is_current; then
  rm -f "$STATE"
  exit 0
fi

# --- 2. Načítaj dnešný počet neúspechov ---
FAILS=0
if [ -f "$STATE" ]; then
  read -r SDATE SFAILS < "$STATE" || true
  [ "${SDATE:-}" = "$TODAY" ] && FAILS=${SFAILS:-0}
fi

if [ "$FAILS" -ge "$MAX_TRIES" ]; then
  log "dnes už $FAILS neúspešných pokusov — pauza do zajtra (šetrím kredit)."
  exit 1
fi

log "landing JSON nie je aktuálny — pokus $((FAILS+1))/$MAX_TRIES…"

# --- 3. Skús obnoviť ---
if cd "$DIR" && "$PY" -u refresh_blocek.py "$LANDING_DATA" && landing_data_is_current; then
  log "OK — landing JSON obnovený na týždeň $MON_ISO."
  if [ "$FAILS" -gt 0 ]; then
    notify "Uvar.si opravené" "Landing JSON sa obnovil na týždeň $MON_ISO (po $FAILS neúspešných pokusoch)."
  fi
  rm -f "$STATE"
  exit 0
fi

# --- 4. Neúspech: zapíš, upozorni ak treba, o hodinu skúsi znova ---
FAILS=$((FAILS+1))
echo "$TODAY $FAILS" > "$STATE"
log "pokus $FAILS zlyhal — skúsim znova o hodinu."

if [ "$FAILS" -eq "$NOTIFY_AT" ]; then
  TAIL=$(tail -12 /var/log/uvarsi.log 2>/dev/null | tr '\n' ' ' | tail -c 400)
  notify "Uvar.si: bloček sa neobnovuje" \
    "Týždeň $MON_ISO — $FAILS neúspešné pokusy, skúšam ďalej každú hodinu. Log: $TAIL"
fi
exit 1
