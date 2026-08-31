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
PY="${UVARSI_PY:-$DIR/venv/bin/python}"
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
RELEASE_ARCHIVE="$TMP/release.tar"
tar -C "$ZDROJ" --exclude='./app/catalog' -cf "$RELEASE_ARCHIVE" . || {
  log "archív vydania sa nepodarilo pripraviť"; exit 1; }
tar -C "$CIEL" -xf "$RELEASE_ARCHIVE" || {
  log "kópiu sa nepodarilo pripraviť"; exit 1; }
rm -f "$RELEASE_ARCHIVE" || {
  log "dočasný archív vydania sa nepodarilo odstrániť"; exit 1; }
mkdir -p "$CIEL/app/catalog/recipes" || {
  log "runtime katalóg sa nepodarilo pripraviť"; exit 1; }
for catalog_asset in ingredients.json recipes/manifest.json; do
  [ -f "$ZDROJ/app/catalog/$catalog_asset" ] && \
    [ -s "$ZDROJ/app/catalog/$catalog_asset" ] || {
      log "zdrojový katalóg nemá platný $catalog_asset"; exit 1; }
  cp -a "$ZDROJ/app/catalog/$catalog_asset" "$CIEL/app/catalog/$catalog_asset" || {
    log "runtime katalóg $catalog_asset sa nepodarilo pripraviť"; exit 1; }
done
for recept in "$ZDROJ"/app/catalog/recipes/*.json; do
  [ -e "$recept" ] || continue
  RECEPT_NAZOV="${recept##*/}"
  [ "$RECEPT_NAZOV" = "manifest.json" ] && continue
  [[ "$RECEPT_NAZOV" =~ ^[a-z0-9_-]+\.json$ ]] || {
    log "zdrojový receptový JSON má nebezpečný názov: $RECEPT_NAZOV"; exit 1; }
  [ -f "$recept" ] && [ -s "$recept" ] || {
    log "zdrojový receptový JSON nie je platný súbor: $RECEPT_NAZOV"; exit 1; }
  cp -a "$recept" "$CIEL/app/catalog/recipes/$RECEPT_NAZOV" || {
    log "receptový JSON $RECEPT_NAZOV sa nepodarilo pripraviť"; exit 1; }
done
# Windows môže do gitu uložiť CRLF; shell skripty musia mať LF, inak je shebang rozbitý
sed -i 's/\r//' "$CIEL"/hetzner/*.sh 2>/dev/null || true

# --- 2. overenie PRED prepnutím ---
# a) všetky moduly sa dajú naimportovať
if ! "$PY" -c "import argon2, webauthn" >/dev/null 2>&1; then
  log "auth závislosti chýbajú — vydanie NEPREPÍNAM"
  exit 1
fi
if ! (cd "$CIEL/app" && UVARSI_URL=https://uvar.si UVARSI_VERSION_FILE="$CIEL/VERSION" \
      "$PY" -c "import server" >/dev/null 2>"$TMP/import.err"); then
  log "import zlyhal — vydanie NEPREPÍNAM:"; sed 's/^/    /' "$TMP/import.err" | tail -5
  notify "Uvar.si: vydanie odmietnuté" "Nové vydanie sa nedá naimportovať, ostáva bežať staré."
  exit 1
fi
# b) povinné súbory
for f in app/server.py app/config.py app/auth_data.py app/public_pages.py app/plan_jobs.py app/plan_calendar.py app/plan_shortlist.py app/plan_worker.py app/predpocet.py app/deterministic_plan.py app/ingredient_catalog.py app/library_gate.py app/quantity_math.py app/recipe_catalog.py app/recipe_matcher.py app/recipe_renderer.py app/static/app.html app/catalog/ingredients.json app/catalog/recipes/manifest.json hetzner/uvarsi.service hetzner/uvarsi-plan-worker.service hetzner/uvarsi-deploy-state.sh hetzner/recipe-engine-rollout.sh hetzner/recipe-engine.target VERSION index.html sw.js; do
  [ -f "$CIEL/$f" ] && [ -s "$CIEL/$f" ] || { log "vo vydaní chýba platný $f — NEPREPÍNAM"; \
    notify "Uvar.si: neúplné vydanie" "Chýba $f."; exit 1; }
done
RECEPT_JSON_NAJDENY=0
for recept in "$CIEL"/app/catalog/recipes/*.json; do
  [ -e "$recept" ] || continue
  RECEPT_NAZOV="${recept##*/}"
  [ "$RECEPT_NAZOV" = "manifest.json" ] && continue
  [[ "$RECEPT_NAZOV" =~ ^[a-z0-9_-]+\.json$ ]] || {
    log "vo vydaní je receptový JSON s nebezpečným názvom: $RECEPT_NAZOV — NEPREPÍNAM"
    notify "Uvar.si: neúplné vydanie" "Neplatný názov receptového JSON."
    exit 1
  }
  if [ -f "$recept" ] && [ -s "$recept" ]; then
    RECEPT_JSON_NAJDENY=1
  fi
done
[ "$RECEPT_JSON_NAJDENY" -eq 1 ] || {
  log "vo vydaní chýba receptový JSON — NEPREPÍNAM"
  notify "Uvar.si: neúplné vydanie" "Chýba receptový JSON."
  exit 1
}
if ! (cd "$CIEL" && UVARSI_RECIPE_ENGINE=off "$PY" -m app.library_gate >/dev/null); then
  log "receptový library gate zlyhal — NEPREPÍNAM"
  notify "Uvar.si: vydanie odmietnuté" "Receptový katalóg neprešiel kontrolou."
  exit 1
fi
PREFLIGHT_SMOKE="$TMP/recipe-engine-preflight-smoke.json"
rm -f "$PREFLIGHT_SMOKE"
if ! (cd "$CIEL/app" && \
      UVARSI_URL=https://uvar.si UVARSI_VERSION_FILE="$CIEL/VERSION" \
      UVARSI_RECIPE_ENGINE=on PLATBY_ZAPNUTE=0 UVARSI_PAYMENTS_ENABLED=0 \
      UVARSI_RECIPE_SMOKE_STATE="$PREFLIGHT_SMOKE" \
      "$PY" -m server --recipe-engine-smoke --state "$PREFLIGHT_SMOKE" \
      >/dev/null 2>"$TMP/recipe-smoke.err"); then
  log "izolovaný deterministický smoke zlyhal — NEPREPÍNAM"
  notify "Uvar.si: vydanie odmietnuté" "Deterministický plán neprešiel izolovaným smoke testom."
  exit 1
fi
rm -f "$PREFLIGHT_SMOKE" "$TMP/recipe-smoke.err"

# --- 3. záloha aktuálneho stavu a prepnutie ---
PRED="$REL/predosle"
. "$CIEL/hetzner/uvarsi-deploy-state.sh" || { log "pomocný rollback skript sa nedá načítať — NEPREPÍNAM"; exit 1; }
uvarsi_snapshot "$PRED" || { log "záloha appky, worker stavu alebo heartbeat značky zlyhala — NEPREPÍNAM"; exit 1; }
cp -a "/var/www/uvarsi/index.html" "$PRED/index.html" || {
  log "záloha živého index.html zlyhala — NEPREPÍNAM"; exit 1; }
cp -a "/var/www/uvarsi/sw.js" "$PRED/sw.js" || {
  log "záloha živého sw.js zlyhala — NEPREPÍNAM"; exit 1; }
for f in refresh_blocek.py recepty.py dozorca.sh zaloha.sh recipe-engine-rollout.sh recipe-engine.target; do
  if [ -f "$DIR/$f" ]; then
    cp -a "$DIR/$f" "$PRED/$f" || { log "záloha $f zlyhala — NEPREPÍNAM"; exit 1; }
  else
    : > "$PRED/$f.absent" || { log "záloha neprítomnosti $f zlyhala — NEPREPÍNAM"; exit 1; }
  fi
done

nasad_z() {   # $1 = adresár s vydaním
  uvarsi_install_core "$1" "$PRED" || return $?
  # webový koreň — landing a service worker (bez toho sa stránka nikdy neobnoví)
  cp -a "$1/index.html" "/var/www/uvarsi/index.html" || return 1
  cp -a "$1/sw.js" "/var/www/uvarsi/sw.js" || return 1
  [ ! -f "$1/hetzner/refresh_blocek.py" ] || cp -a "$1/hetzner/refresh_blocek.py" "$DIR/refresh_blocek.py" || return 1
  [ ! -f "$1/hetzner/recepty.py" ] || cp -a "$1/hetzner/recepty.py" "$DIR/recepty.py" || return 1
  [ ! -f "$1/hetzner/dozorca.sh" ] || { cp -a "$1/hetzner/dozorca.sh" "$DIR/dozorca.sh" && chmod +x "$DIR/dozorca.sh"; } || return 1
  [ ! -f "$1/hetzner/zaloha.sh" ] || { cp -a "$1/hetzner/zaloha.sh" "$DIR/zaloha.sh" && chmod +x "$DIR/zaloha.sh"; } || return 1
  cp -a "$1/hetzner/recipe-engine-rollout.sh" "$DIR/recipe-engine-rollout.sh" || return 1
  chmod +x "$DIR/recipe-engine-rollout.sh" || return 1
  cp -a "$1/hetzner/recipe-engine.target" "$DIR/recipe-engine.target" || return 1
  if [ ! -f "$DIR/uvarsi-recipe-engine.env" ]; then
    (umask 077; printf 'UVARSI_RECIPE_ENGINE=off\n' > "$DIR/uvarsi-recipe-engine.env") || return 1
  fi
  [ ! -f "$1/hetzner/samopull.sh" ] || cp -a "$1/hetzner/samopull.sh" "$DIR/samopull.sh.novy" || return 1
  cp -a "$1/hetzner/uvarsi-deploy-state.sh" "$DIR/uvarsi-deploy-state.sh" || return 1
  systemctl enable uvarsi >/dev/null 2>&1 || return 1
  systemctl enable uvarsi-plan-worker >/dev/null 2>&1 || return 1
  systemctl restart uvarsi || return 1
}

zdravie() {   # čaká max 30 s na živú appku
  for _ in $(seq 1 30); do
    curl -fsS --max-time 5 localhost:8090/api/health >/dev/null 2>&1 && return 0
    sleep 1
  done
  return 1
}

spusti_worker() {
  systemctl restart uvarsi-plan-worker && systemctl is-active --quiet uvarsi-plan-worker
}

log "prepínam na $SHA"
LIVE_MUTATION=0
if nasad_z "$CIEL"; then
  LIVE_MUTATION=1
fi
PRED_HEARTBEAT=$(cat "$PRED/heartbeat.before" 2>/dev/null || true)
if [ "$LIVE_MUTATION" -eq 1 ] && zdravie && spusti_worker && uvarsi_wait_fresh_heartbeat "$PRED_HEARTBEAT"; then
  echo "$SHA" > "$STAV"
  # samopull sa aktualizuje až po úspechu, aby sa nezmenil pod vlastnými nohami
  [ -f "$DIR/samopull.sh.novy" ] && mv "$DIR/samopull.sh.novy" "$DIR/samopull.sh" && chmod +x "$DIR/samopull.sh"
  VER=$(cat "$DIR/VERSION" 2>/dev/null || echo "?")
  log "OK — nasadené vydanie $VER ($SHA)"
  # Nečakáme na najbližšiu celú hodinu. Dozorca má vlastný flock, takže sa
  # bezpečne ukončí, ak už práve beží iný zber.
  nohup "$DIR/dozorca.sh" >> /var/log/uvarsi.log 2>&1 &
  log "dozorca spustený na pozadí po nasadení"
  nohup "$DIR/recipe-engine-rollout.sh" >> /var/log/uvarsi-recipe-rollout.log 2>&1 &
  log "autonómny receptový rollout spustený na pozadí"
  notify "Uvar.si nasadené" "Vydanie $VER je živé. Appka odpovedá."
  exit 0
fi

# --- 4. neúspech → návrat ---
log "nasadenie alebo čerstvý heartbeat zlyhali — VRACIAM predošlú verziu"
NAVRAT_OK=1
rm -f "$DIR/samopull.sh.novy" || NAVRAT_OK=0
uvarsi_restore "$PRED" || { log "rollback appky alebo worker stavu zlyhal"; NAVRAT_OK=0; }
cp -a "$PRED/index.html" "/var/www/uvarsi/index.html" || {
  log "rollback index.html zlyhal"; NAVRAT_OK=0; }
cp -a "$PRED/sw.js" "/var/www/uvarsi/sw.js" || {
  log "rollback sw.js zlyhal"; NAVRAT_OK=0; }
for f in refresh_blocek.py recepty.py dozorca.sh zaloha.sh recipe-engine-rollout.sh recipe-engine.target; do
  if [ -f "$PRED/$f" ]; then
    cp -a "$PRED/$f" "$DIR/$f" || { log "rollback $f zlyhal"; NAVRAT_OK=0; }
  elif [ -f "$PRED/$f.absent" ]; then
    rm -f "$DIR/$f" || { log "rollback neprítomnosti $f zlyhal"; NAVRAT_OK=0; }
  else
    log "rollback značka pre $f chýba"; NAVRAT_OK=0
  fi
done
if [ "$NAVRAT_OK" -eq 1 ] && zdravie; then
  log "predošlá verzia beží"
  notify "Uvar.si: vydanie vrátené" "Nové vydanie neprešlo, beží predošlá verzia. Appka je v poriadku."
else
  log "ani predošlá verzia nenabehla — potrebný zásah"
  notify "Uvar.si: POTREBUJEM ŤA" "Appka nebeží ani po návrate na predošlú verziu. Treba zásah."
fi
exit 1
