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
DIR=/opt/uvarsi
PAGE=/var/www/uvarsi/index.html
PY="$DIR/venv/bin/python"
STATE="$DIR/.dozorca_state"          # formát: "RRRR-MM-DD pocet_neuspechov"
MAX_TRIES=6                          # max pokusov za jeden deň
NOTIFY_AT=2                          # po koľkých neúspechoch upozorniť
NTFY_TOPIC="uvarsi-jarvis-8f3a2c"    # notifikácie: ntfy.sh/<topic>

log(){ echo "[$(date '+%F %T')] DOZORCA: $*"; }
notify(){ curl -s --max-time 15 -H "Title: $1" -d "$2" "https://ntfy.sh/${NTFY_TOPIC}" >/dev/null 2>&1 || true; }

TODAY=$(date +%F)

# --- aký týždeň má byť na bločku (pondelok–nedeľa, presne ako v Pythone) ---
DOW=$(date +%u)                                   # 1=pondelok … 7=nedeľa
MON_D=$(date -d "-$((DOW-1)) days" +%-d)
SUN_D=$(date -d "+$((7-DOW)) days" +%-d)
SUN_M=$(date -d "+$((7-DOW)) days" +%-m)
SUN_Y=$(date -d "+$((7-DOW)) days" +%Y)
WANT="${MON_D}.–${SUN_D}. ${SUN_M}. ${SUN_Y}"

# --- 0. Databáza akcií pre appku: má aktuálny týždeň? ---
# (appka skladá osobné plány z tejto DB; bez nej ľuďom nič nevygeneruje)
MON_ISO=$(date -d "-$((DOW-1)) days" +%F)
POCET=$(sqlite3 "$DIR/uvarsi.db" \
        "SELECT COUNT(*) FROM akcie WHERE tyzden='$MON_ISO'" 2>/dev/null || echo 0)
if [ "${POCET:-0}" -lt 30 ]; then
  log "akcie pre týždeň $MON_ISO chýbajú ($POCET) — spúšťam zbierač…"
  if cd "$DIR/app" && "$PY" -u zbierac_akcii.py; then
    log "zbierač OK"
  else
    log "zbierač zlyhal — appka zatiaľ použije minulý týždeň"
  fi
fi

# --- 1. Už je bloček na landingu čerstvý? ---
if grep -qF "$WANT" "$PAGE" 2>/dev/null; then
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

log "bloček nesedí na '$WANT' — pokus $((FAILS+1))/$MAX_TRIES…"

# --- 3. Skús obnoviť ---
if cd "$DIR" && "$PY" -u refresh_blocek.py "$PAGE" && grep -qF "$WANT" "$PAGE" 2>/dev/null; then
  log "OK — bloček obnovený na '$WANT'."
  if [ "$FAILS" -gt 0 ]; then
    notify "Uvar.si opravené" "Bloček sa obnovil na týždeň $WANT (po $FAILS neúspešných pokusoch)."
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
    "Týždeň $WANT — $FAILS neúspešné pokusy, skúšam ďalej každú hodinu. Log: $TAIL"
fi
exit 1
