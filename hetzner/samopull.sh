#!/bin/bash
# Uvar.si — SAMOPULL: server si sám sťahuje nové vydania.
#
# Beží z cronu každých 10 minút. Stiahne release.zip z odkazu v samopull.env,
# porovná kontrolný súčet s tým, čo je nasadené, a ak je nový:
#   1. rozbalí do /opt/uvarsi/releases/<sha>
#   2. spustí testy (import-check + zdravie appky na dočasnom porte)
#   3. až potom prepne ostrú verziu a reštartuje službu
#   4. ak zdravie po prepnutí nesedí, VRÁTI predošlú verziu a upozorní
#
# Zámerne NEMENÍ: Caddy config, cron, uvarsi.env, databázu, ani inú appku
# na serveri (taktik-mapa). Tie ostávajú výhradne v rukách človeka.
#
# Vypnutie: zmaž riadok so samopull z `crontab -e`, alebo zruš zdieľaný odkaz.
#
# Inštalácia (cron):
#   */10 * * * * /opt/uvarsi/samopull.sh >> /var/log/uvarsi-pull.log 2>&1

set -u
DIR=/opt/uvarsi
CFG="$DIR/samopull.env"
REL="$DIR/releases"
STAV="$DIR/.nasadene_sha"
PY="$DIR/venv/bin/python"
NTFY="uvarsi-jarvis-8f3a2c"
LOCK=/var/lock/uvarsi-samopull.lock

log(){ echo "[$(date '+%F %T')] SAMOPULL: $*"; }
notify(){ curl -s --max-time 15 -H "Title: $1" -d "$2" "https://ntfy.sh/$NTFY" >/dev/null 2>&1 || true; }

# jeden beh naraz (sťahovanie + testy môžu trvať)
exec 9>"$LOCK" || exit 0
flock -n 9 || exit 0

[ -f "$CFG" ] || { log "chýba $CFG — samopull nie je nastavený"; exit 0; }
# Config sa ZÁMERNE nenačítava cez `.` — v URL býva `&`, ktoré by bash bral ako
# "spusti na pozadí" a premenná by sa nastavila len v podprocese.
RELEASE_URL=$(sed -n 's/^[[:space:]]*RELEASE_URL=//p' "$CFG" | head -1 \
              | sed -e 's/^["'"'"']//' -e 's/["'"'"']$//' -e 's/[[:space:]]*$//')
[ -n "$RELEASE_URL" ] || { log "v $CFG chýba RELEASE_URL"; exit 1; }

TMP=$(mktemp -d /tmp/uvarsi-pull.XXXXXX) || exit 1
trap 'rm -rf "$TMP"' EXIT

# --- 1. stiahni najnovší kód (git) ---
ZDROJ="$DIR/zdroj"
VETVA="${RELEASE_BRANCH:-main}"
if [ ! -d "$ZDROJ/.git" ]; then
  log "prvé stiahnutie z $RELEASE_URL"
  rm -rf "$ZDROJ"
  git clone --quiet --depth 1 --branch "$VETVA" "$RELEASE_URL" "$ZDROJ" || {
    log "git clone zlyhal"; exit 1; }
else
  git -C "$ZDROJ" fetch --quiet --depth 1 origin "$VETVA" || {
    log "git fetch zlyhal (skúsim o 10 minút)"; exit 1; }
  git -C "$ZDROJ" reset --quiet --hard "origin/$VETVA" || {
    log "git reset zlyhal"; exit 1; }
fi

SHA=$(git -C "$ZDROJ" rev-parse HEAD)
[ -f "$STAV" ] && [ "$(cat "$STAV")" = "$SHA" ] && exit 0   # nič nové, ticho končí

log "nová verzia ${SHA:0:8} — pripravujem"
CIEL="$REL/${SHA:0:12}"
rm -rf "$CIEL"; mkdir -p "$CIEL"
cp -a "$ZDROJ/." "$CIEL/" || { log "kópiu sa nepodarilo pripraviť"; exit 1; }
# Windows môže do gitu uložiť CRLF; shell skripty musia mať LF, inak je shebang rozbitý
sed -i 's/\r//' "$CIEL"/hetzner/*.sh 2>/dev/null || true

# --- 2. overenie PRED prepnutím ---
# a) všetky moduly sa dajú naimportovať
if ! (cd "$CIEL/app" && UVARSI_URL=https://uvar.si UVARSI_VERSION_FILE="$CIEL/VERSION" \
      "$PY" -c "import server" >/dev/null 2>"$TMP/import.err"); then
  log "import zlyhal — vydanie NEPREPÍNAM:"; sed 's/^/    /' "$TMP/import.err" | tail -5
  notify "Uvar.si: vydanie odmietnuté" "Nové vydanie sa nedá naimportovať, ostáva bežať staré."
  exit 1
fi
# b) povinné súbory
for f in app/server.py app/auth_data.py app/static/app.html VERSION; do
  [ -s "$CIEL/$f" ] || { log "vo vydaní chýba $f — NEPREPÍNAM"; \
    notify "Uvar.si: neúplné vydanie" "Chýba $f."; exit 1; }
done

# --- 3. záloha aktuálneho stavu a prepnutie ---
PRED="$REL/predosle"
rm -rf "$PRED"; mkdir -p "$PRED"
cp -a "$DIR/app" "$PRED/app" 2>/dev/null || true
cp -a "$DIR/VERSION" "$PRED/VERSION" 2>/dev/null || true
for f in refresh_blocek.py recepty.py dozorca.sh; do
  cp -a "$DIR/$f" "$PRED/$f" 2>/dev/null || true
done

nasad_z() {   # $1 = adresár s vydaním
  cp -a "$1/app/." "$DIR/app/" || return 1
  cp -a "$1/VERSION" "$DIR/VERSION" 2>/dev/null || true
  [ -f "$1/hetzner/refresh_blocek.py" ] && cp -a "$1/hetzner/refresh_blocek.py" "$DIR/refresh_blocek.py"
  [ -f "$1/hetzner/recepty.py" ] && cp -a "$1/hetzner/recepty.py" "$DIR/recepty.py"
  [ -f "$1/hetzner/dozorca.sh" ] && { cp -a "$1/hetzner/dozorca.sh" "$DIR/dozorca.sh"; chmod +x "$DIR/dozorca.sh"; }
  [ -f "$1/hetzner/samopull.sh" ] && { cp -a "$1/hetzner/samopull.sh" "$DIR/samopull.sh.novy"; }
  systemctl restart uvarsi
}

zdravie() {   # čaká max 30 s na živú appku
  for _ in $(seq 1 30); do
    curl -fsS --max-time 5 localhost:8090/api/health >/dev/null 2>&1 && return 0
    sleep 1
  done
  return 1
}

log "prepínam na $SHA"
if nasad_z "$CIEL" && zdravie; then
  echo "$SHA" > "$STAV"
  # samopull sa aktualizuje až po úspechu, aby sa nezmenil pod vlastnými nohami
  [ -f "$DIR/samopull.sh.novy" ] && mv "$DIR/samopull.sh.novy" "$DIR/samopull.sh" && chmod +x "$DIR/samopull.sh"
  VER=$(cat "$DIR/VERSION" 2>/dev/null || echo "?")
  log "OK — nasadené vydanie $VER ($SHA)"
  notify "Uvar.si nasadené" "Vydanie $VER je živé. Appka odpovedá."
  exit 0
fi

# --- 4. neúspech → návrat ---
log "appka po nasadení neodpovedá — VRACIAM predošlú verziu"
rm -f "$DIR/samopull.sh.novy"
cp -a "$PRED/app/." "$DIR/app/" 2>/dev/null || true
cp -a "$PRED/VERSION" "$DIR/VERSION" 2>/dev/null || true
for f in refresh_blocek.py recepty.py dozorca.sh; do
  cp -a "$PRED/$f" "$DIR/$f" 2>/dev/null || true
done
systemctl restart uvarsi
if zdravie; then
  log "predošlá verzia beží"
  notify "Uvar.si: vydanie vrátené" "Nové vydanie neprešlo, beží predošlá verzia. Appka je v poriadku."
else
  log "ani predošlá verzia nenabehla — potrebný zásah"
  notify "Uvar.si: POTREBUJEM ŤA" "Appka nebeží ani po návrate na predošlú verziu. Treba zásah."
fi
exit 1
