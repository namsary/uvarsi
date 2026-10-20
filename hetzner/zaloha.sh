#!/bin/bash
# Konce riadkov tohto skriptu musia zostať LF (pozri .gitattributes).
# Uvar.si — ZÁLOHA DATABÁZY (beží na jarvise každú noc, plne autonómne).
#
# Tabuľka `naroky` je JEDINÝ záznam o tom, kto zaplatil. Nič ju doteraz
# nezálohovalo: samopull.sh databázu zámerne nechá na pokoji a adresár
# `predosle` drží iba kód. Strata disku = strata zoznamu platiacich.
#
# PREČO NIE `cp`: SQLite sa nesmie zálohovať obyčajnou kópiou súboru. Pri
# súbežnom zápise skopíruje rozpísanú stránku a vo WAL režime nechá `-wal`
# bokom — výsledok je poškodená databáza presne vtedy, keď ju treba. Preto
# `VACUUM INTO`: SQLite si sám vezme konzistentný snímok vrátane WAL a zapíše
# ho ako hotovú, skomprimovanú databázu. Zálohovať sa dá aj počas prevádzky.
#
# NEPREVERENÁ ZÁLOHA JE IBA NÁDEJ. Každá kópia sa hneď otvorí, prejde
# `PRAGMA integrity_check` a prečíta sa z nej `naroky`. Kým to neprejde,
# súbor sa ani nepremenuje na finálny názov — v adresári teda nikdy neleží
# záloha, o ktorej by sme nevedeli, že sa dá otvoriť.
#
# Na serveri beží aj druhá appka (taktik-mapa). Tento skript sa jej NEDOTKNE:
# siaha výhradne do /opt/uvarsi a /var/backups/uvarsi.
#
# Obnova zo zálohy (ručne, s vypnutou službou):
#   systemctl stop uvarsi
#   cp /var/backups/uvarsi/uvarsi-RRRR-MM-DD.db /opt/uvarsi/uvarsi.db
#   rm -f /opt/uvarsi/uvarsi.db-wal /opt/uvarsi/uvarsi.db-shm
#   systemctl start uvarsi
#
# Inštalácia (cron):
#   30 3 * * * /opt/uvarsi/zaloha.sh >> /var/log/uvarsi-zaloha.log 2>&1

set -u
DIR="${UVARSI_DIR:-/opt/uvarsi}"
DB="${UVARSI_DB:-$DIR/uvarsi.db}"
KAM="${UVARSI_ZALOHY:-/var/backups/uvarsi}"
PY="${UVARSI_PY:-$DIR/venv/bin/python}"
DRZAT="${UVARSI_DRZAT:-14}"          # koľko nočných záloh si necháme
# Súkromná téma upozornení žije iba na serveri v súbore ntfy_topic (jeden
# riadok, nie v gite). Verejná predvolená téma je len núdzová záloha.
NTFY_TOPIC_SUBOR=$( [ -r "$DIR/ntfy_topic" ] && head -n1 "$DIR/ntfy_topic" | tr -cd 'A-Za-z0-9_-' )
NTFY_TOPIC="${UVARSI_NTFY_TOPIC:-${NTFY_TOPIC_SUBOR:-uvarsi-jarvis-8f3a2c}}"    # notifikácie: ntfy.sh/<topic>

log(){ echo "[$(date '+%F %T')] ZALOHA: $*"; }
notify(){
  [ -n "${UVARSI_TICHO:-}" ] && return 0
  curl -s --max-time 15 -H "Title: $1" -d "$2" "https://ntfy.sh/$NTFY_TOPIC" >/dev/null 2>&1 || true
}

# Chýbajúca databáza nie je „nič na zálohovanie" — je to poplach.
if [ ! -f "$DB" ]; then
  log "databáza $DB neexistuje — niet čo zálohovať"
  notify "Uvar.si: záloha zlyhala" "Databáza $DB neexistuje."
  exit 1
fi

mkdir -p "$KAM" || { log "adresár $KAM sa nepodarilo vytvoriť"; exit 1; }

DNES=$(date +%F)
CIEL="$KAM/uvarsi-$DNES.db"
ROZPISANY="$CIEL.rozpisany"
rm -f "$ROZPISANY"

# --- 1. konzistentný snímok (funguje aj počas zápisu) ---
if ! "$PY" - "$DB" "$ROZPISANY" <<'PY'
import sqlite3, sys

zdroj, ciel = sys.argv[1], sys.argv[2]
con = sqlite3.connect(zdroj, timeout=60.0)
try:
    # VACUUM INTO číta cez bežné spojenie, takže vidí aj to, čo leží vo -wal.
    con.execute("VACUUM INTO ?", (ciel,))
finally:
    con.close()
PY
then
  log "snímok databázy zlyhal"
  rm -f "$ROZPISANY"
  notify "Uvar.si: záloha zlyhala" "VACUUM INTO neprešiel. Tabuľka naroky je bez zálohy."
  exit 1
fi

# --- 2. overenie: dá sa kópia vôbec otvoriť a je v nej to, kvôli čomu vznikla? ---
if ! "$PY" - "$ROZPISANY" <<'PY'
import sqlite3, sys

con = sqlite3.connect(sys.argv[1])
try:
    stav = con.execute("PRAGMA integrity_check").fetchone()[0]
    if stav != "ok":
        sys.exit("integrity_check: %s" % stav)
    tabulky = {r[0] for r in
               con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "naroky" in tabulky:
        pocet = con.execute("SELECT COUNT(*) FROM naroky").fetchone()[0]
        print("overene: integrity_check ok, naroky=%d" % pocet)
    else:
        # Čerstvý server pred prvým štartom appky: tabuľka ešte nevznikla.
        # Nie je to poškodená záloha, tak sa kvôli tomu nebudí majiteľ.
        print("overene: integrity_check ok, naroky zatial neexistuju")
finally:
    con.close()
PY
then
  log "záloha neprešla overením — zahadzujem ju"
  rm -f "$ROZPISANY"
  notify "Uvar.si: záloha je nepoužiteľná" "Kópia neprešla integrity_check. Treba zásah."
  exit 1
fi

mv -f "$ROZPISANY" "$CIEL" || { log "presun zálohy zlyhal"; rm -f "$ROZPISANY"; exit 1; }
log "hotovo: $CIEL ($(du -h "$CIEL" 2>/dev/null | cut -f1))"

# --- 3. rotácia: názvy sú RRRR-MM-DD, takže abecedné poradie = časové ---
# Maže sa VÝHRADNE vzor uvarsi-*.db vo vlastnom adresári, nič iné na serveri.
STARE=$(ls -1 "$KAM"/uvarsi-*.db 2>/dev/null | sort | head -n -"$DRZAT")
if [ -n "$STARE" ]; then
  echo "$STARE" | while IFS= read -r subor; do
    [ -n "$subor" ] && rm -f "$subor" && log "rotácia: zmazané $subor"
  done
fi

POCET=$(ls -1 "$KAM"/uvarsi-*.db 2>/dev/null | wc -l)
log "v adresári $KAM je $POCET záloh (držíme $DRZAT)"
exit 0

# --- 4. kópia mimo servera ---
# Záloha na tom istom disku nechráni pred stratou servera. Cieľ (napr. Hetzner
# Storage Box) žije iba na serveri v súbore zaloha.env, nie v gite:
#   ZALOHA_VZDIALENA=u123456@u123456.your-storagebox.de:uvarsi/
#   ZALOHA_SSH_PORT=23
#   ZALOHA_SSH_KLUC=/root/.ssh/uvarsi_zaloha
# Kým nie je nastavený, raz týždenne (v nedeľu) príde pripomienka.
ZALOHA_ENV="${UVARSI_ZALOHA_ENV:-$DIR/zaloha.env}"
ZALOHA_VZDIALENA=""
ZALOHA_SSH_PORT="22"
ZALOHA_SSH_KLUC=""
if [ -r "$ZALOHA_ENV" ]; then
  ZALOHA_VZDIALENA=$(sed -n 's/^[[:space:]]*ZALOHA_VZDIALENA=//p' "$ZALOHA_ENV" | head -1 | tr -d '"'"'"'')
  ZALOHA_SSH_PORT=$(sed -n 's/^[[:space:]]*ZALOHA_SSH_PORT=//p' "$ZALOHA_ENV" | head -1 | tr -cd '0-9')
  ZALOHA_SSH_KLUC=$(sed -n 's/^[[:space:]]*ZALOHA_SSH_KLUC=//p' "$ZALOHA_ENV" | head -1 | tr -d '"'"'"'')
  ZALOHA_SSH_PORT=${ZALOHA_SSH_PORT:-22}
fi
if [ -z "$ZALOHA_VZDIALENA" ]; then
  log "kópia mimo servera nie je nastavená ($ZALOHA_ENV)"
  if [ "$(date +%u)" = "7" ]; then
    notify "Uvar.si: záloha iba na serveri" "Záloha databázy je len na tom istom disku. Nastav kópiu mimo servera (zaloha.env)."
  fi
  exit 0
fi
SSH_PRIKAZ="ssh -p $ZALOHA_SSH_PORT -o BatchMode=yes -o StrictHostKeyChecking=accept-new"
[ -n "$ZALOHA_SSH_KLUC" ] && SSH_PRIKAZ="$SSH_PRIKAZ -i $ZALOHA_SSH_KLUC"
if rsync -a --timeout=300 -e "$SSH_PRIKAZ" "$CIEL" "$ZALOHA_VZDIALENA"; then
  log "kópia mimo servera: hotovo ($ZALOHA_VZDIALENA)"
else
  log "kópia mimo servera ZLYHALA"
  notify "Uvar.si: záloha mimo servera zlyhala" "Nočná záloha je na serveri, ale kópiu mimo servera sa nepodarilo odoslať. Skúsim zajtra."
  exit 1
fi
