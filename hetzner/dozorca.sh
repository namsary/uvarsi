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
# DOČASNÁ vs. ŠTRUKTURÁLNA chyba: refresh_blocek.py končí kódom 1, keď má
# zmysel skúsiť to o hodinu znova (sieť, model, zamknutá DB), a kódom 3, keď
# je pád deterministický (napr. v DB nie je dosť overených ponúk). Kód 3 sa
# neopakuje, kým sa vstupné dáta nezmenia — inak by hodinové pokusy pálili
# kredit za výsledok, ktorý je vopred známy.
#
# TRETÍ prípad — NULOVÝ KREDIT (incident 24. 8. 2026): API odmieta každé
# volanie, kým majiteľ nedobije účet. Dáta s tým nemajú nič spoločné, takže
# blok viazaný na počet ponúk by sa uvoľnil pri prvej zmene v DB a pokusy by
# bežali ďalej. refresh_blocek to preto hlási značkou KREDIT_VYCERPANY a
# dozorca zapíše blok "KREDIT" a dovolí jeden overovací pokus za hodinu. Po
# dobití sa tak obnoví sám bez ručného resetu; bez kreditu probe nič neminie.
# Upozornenie na ntfy posiela naklady.py (práve raz za deň), dozorca ho
# zámerne NEZDVOJUJE.
#
# Inštalácia (cron):
#   0 5-21 * * * /opt/uvarsi/dozorca.sh >> /var/log/uvarsi.log 2>&1

set -u
DIR="${UVARSI_DIR:-/opt/uvarsi}"
LANDING_DATA="${UVARSI_LANDING_DATA:-/var/lib/uvarsi/landing_data.json}"
PY="${UVARSI_PY:-$DIR/venv/bin/python}"
HEALTH_PY="${UVARSI_HEALTH_PY:-$PY}"
CURL="${UVARSI_CURL:-curl}"
DATE="${UVARSI_DATE:-date}"
STATE="$DIR/.dozorca_state"          # "deň neúspechy blok [probe_epoch] [release_sha]"
PLAN_QUEUE_ALERT_STATE="$DIR/.plan_queue_alert_state"
RECIPE_ENGINE_ALERT_STATE="$DIR/.recipe_engine_alert_state"
RECIPE_SMOKE_ATTEMPT_STATE="$DIR/.recipe_engine_smoke_attempt"
COLLECTION_DIAGNOSTIC_STATE="$DIR/.collection_diagnostic_state"
RECIPE_SMOKE_STATE="${UVARSI_RECIPE_SMOKE_STATE:-/var/lib/uvarsi/recipe_engine_smoke.json}"
PLAN_QUEUE_HEALTH_URL="${UVARSI_PLAN_QUEUE_HEALTH_URL:-http://127.0.0.1:8090/api/health}"
RECIPE_SMOKE_MIN_INTERVAL_SECONDS="${UVARSI_RECIPE_SMOKE_MIN_INTERVAL_SECONDS:-900}"
CREDIT_RETRY_SECONDS="${UVARSI_CREDIT_RETRY_SECONDS:-3600}"
MAX_TRIES=6                          # max pokusov za jeden deň
NOTIFY_AT=2                          # po koľkých neúspechoch upozorniť
EXIT_STRUCTURAL=3                    # kód, ktorým refresh_blocek hlási "neopakuj"
MIN_OFFERS_PER_STORE=20              # malá vložka sa nesmie tváriť ako celý leták
NTFY_TOPIC="uvarsi-jarvis-8f3a2c"    # notifikácie: ntfy.sh/<topic>

# Cron aj ručne spustený samopull môžu dediť UTC z hostiteľa. Všetky Python
# procesy, ktoré dozorca spúšťa (zberač, rozpočtová poistka, bloček), však
# pracujú so slovenským obchodným dňom. Export drží ich kalendár zhodný s
# dátumom, podľa ktorého dozorca vybral aktuálne letáky.
export TZ=Europe/Bratislava

log(){ echo "[$(TZ=Europe/Bratislava "$DATE" '+%F %T')] DOZORCA: $*"; }
notify(){ "$CURL" -fsS --max-time 15 -H "Title: $1" -d "$2" "https://ntfy.sh/${NTFY_TOPIC}" >/dev/null 2>&1; }

upozorni_detail_zberu() {
  DATA_KEY="$1"
  WEEK="$2"
  LAST_KEY=""
  if [ -f "$COLLECTION_DIAGNOSTIC_STATE" ]; then
    read -r LAST_KEY < "$COLLECTION_DIAGNOSTIC_STATE" || LAST_KEY=""
  fi
  [ "$LAST_KEY" != "$DATA_KEY" ] || return 0

  DETAIL=$(sqlite3 "$DIR/uvarsi.db" \
    "SELECT group_concat(obchod || ': ' || COALESCE(NULLIF(detail, ''), stav), ' | ')
       FROM zber_stav
      WHERE tyzden='$WEEK' AND stav!='ok'" 2>/dev/null || true)
  DETAIL=$(printf '%s' "$DETAIL" | tr '\r\n' '  ' | head -c 900)
  [ -n "$DETAIL" ] || DETAIL="V databáze nie je uložený detail zlyhania zberu."
  if notify "Uvar.si: detail zlyhania zberu" "$DETAIL"; then
    printf '%s\n' "$DATA_KEY" > "$COLLECTION_DIAGNOSTIC_STATE"
  else
    log "diagnostiku zberu sa nepodarilo odoslať — ďalší beh to skúsi znova"
  fi
}

# Predpočet môže trvať dlhšie než hodinu. Druhý cron sa vtedy musí slušne
# skončiť, nie zaplatiť rovnaké modelové volania druhýkrát. FD 9 zostáva
# otvorený po celý beh a kernel ho pri každom ukončení procesu automaticky
# uvoľní. UVARSI_DOZORCA_LOCKED používajú iba kontraktové testy bez Linux flock.
if [ "${UVARSI_DOZORCA_LOCKED:-0}" != "1" ]; then
  if ! command -v flock >/dev/null 2>&1; then
    log "CHYBA — chýba flock, dozorca bez ochrany proti súbehu neštartuje."
    exit 1
  fi
  if ! exec 9>"$DIR/.dozorca.lock"; then
    log "CHYBA — zámok sa nedá vytvoriť; dozorca radšej neštartuje bez ochrany proti súbehu."
    exit 1
  fi
  if ! flock -n 9; then
    log "predchádzajúci beh ešte pracuje — tento hodinový pokus preskakujem."
    exit 0
  fi
fi

TODAY="${UVARSI_TODAY:-$(TZ=Europe/Bratislava "$DATE" +%F)}"
NOW_EPOCH="${UVARSI_NOW_EPOCH:-$(date +%s)}"
case "$NOW_EPOCH" in ''|*[!0-9]*) log "CHYBA — aktuálny epoch má neplatný formát"; exit 1 ;; esac
case "$CREDIT_RETRY_SECONDS" in ''|*[!0-9]*|0) log "CHYBA — interval kontroly kreditu má neplatný formát"; exit 1 ;; esac

skontroluj_frontu_planov() {
  # Health odpoveď je jediný zdroj pravdy: dozorca nesmie z počtu procesov
  # hádať, či worker reálne obnovuje lease.
  HEALTH=$("$CURL" -fsS --max-time 1 "$PLAN_QUEUE_HEALTH_URL" 2>/dev/null || true)
  [ -n "$HEALTH" ] || { log "UNKNOWN — frontu plánov sa nedá overiť cez health; značka upozornenia ostáva"; return; }
  STAV_FRONTY=$(printf '%s' "$HEALTH" | "$HEALTH_PY" -c '
import datetime as dt, json, sys
payload = json.load(
    sys.stdin,
    parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)),
)
q = payload.get("plan_queue")
if not isinstance(q, dict): raise SystemExit(2)
required = {"queued", "oldest_seconds", "worker_alive", "heartbeat_seconds", "heartbeat_at", "last_ready", "failed", "blocking_code"}
if not required.issubset(q): raise SystemExit(2)
integer = lambda v: isinstance(v, int) and not isinstance(v, bool) and v >= 0
if not integer(q["queued"]) or not integer(q["failed"]): raise SystemExit(2)
if q["oldest_seconds"] is not None and not integer(q["oldest_seconds"]): raise SystemExit(2)
if (q["queued"] == 0) != (q["oldest_seconds"] is None): raise SystemExit(2)
if not isinstance(q["worker_alive"], bool): raise SystemExit(2)
if q["heartbeat_seconds"] is not None and not integer(q["heartbeat_seconds"]): raise SystemExit(2)
if q["heartbeat_at"] is not None and not isinstance(q["heartbeat_at"], str): raise SystemExit(2)
if (q["heartbeat_seconds"] is None) != (q["heartbeat_at"] is None): raise SystemExit(2)
if q["worker_alive"] and q["heartbeat_at"] is None: raise SystemExit(2)
if q["last_ready"] is not None and not isinstance(q["last_ready"], str): raise SystemExit(2)
if q["blocking_code"] is not None and not isinstance(q["blocking_code"], str): raise SystemExit(2)
if q["heartbeat_at"] is not None:
    parsed = dt.datetime.fromisoformat(q["heartbeat_at"])
    if parsed.tzinfo is None: parsed = parsed.replace(tzinfo=dt.timezone.utc)
engine = payload.get("recipe_engine")
mode = engine.get("mode") if isinstance(engine, dict) else None
if mode == "on" and q["queued"] == 0: print("HEALTHY")
elif not q["worker_alive"] or q["heartbeat_seconds"] > 60: print("WORKER")
elif q["oldest_seconds"] is not None and q["oldest_seconds"] > 180: print("QUEUE")
else: print("HEALTHY")' 2>/dev/null) || {
    log "UNKNOWN — plan_queue health je neúplný alebo má nesprávne typy; značka upozornenia ostáva"
    return
  }
  REASON=""
  case "$STAV_FRONTY" in
    WORKER) REASON="worker heartbeat je starší než 60 s" ;;
    QUEUE) REASON="najstaršia úloha vo fronte čaká viac než 180 s" ;;
    HEALTHY) ;;
    *) log "UNKNOWN — plan_queue health má neznámy stav; značka upozornenia ostáva"; return ;;
  esac
  if [ -n "$REASON" ]; then
    if [ ! -f "$PLAN_QUEUE_ALERT_STATE" ]; then
      if notify "Uvar.si: fronta plánov" "$REASON. Health: $PLAN_QUEUE_HEALTH_URL"; then
        printf '%s\n' "$REASON" > "$PLAN_QUEUE_ALERT_STATE"
        log "UPOZORNENIE — $REASON"
      else
        log "UPOZORNENIE sa nepodarilo odoslať — ďalší beh ho skúsi znova"
      fi
    fi
    return
  fi
  if [ -f "$PLAN_QUEUE_ALERT_STATE" ]; then
    rm -f "$PLAN_QUEUE_ALERT_STATE"
    log "fronta plánov je znova zdravá — značka upozornenia zmazaná"
  fi
}

skontroluj_frontu_planov

recipe_engine_alert() {
  REASON="$1"
  if [ ! -f "$RECIPE_ENGINE_ALERT_STATE" ]; then
    if notify "Uvar.si: receptový engine" "$REASON"; then
      printf '%s\n' "$REASON" > "$RECIPE_ENGINE_ALERT_STATE"
    fi
  fi
  log "CHYBA — receptový engine: $REASON"
}

recipe_engine_health_state() {
  printf '%s' "$HEALTH" | "$HEALTH_PY" -c '
import json, math, sys
payload = json.load(
    sys.stdin,
    parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)),
)
engine = payload.get("recipe_engine")
if not isinstance(engine, dict): raise SystemExit(3)
required = {"mode","library_version","active_templates","coverage","last_shadow","p95_ms","ready","blockers"}
if not required.issubset(engine): raise SystemExit(2)
mode = engine["mode"]
if mode not in {"off","shadow","on"}: raise SystemExit(2)
integer = lambda value: isinstance(value, int) and not isinstance(value, bool)
number = lambda value: isinstance(value, (int,float)) and not isinstance(value, bool) and math.isfinite(value)
if engine["library_version"] is not None and not integer(engine["library_version"]): raise SystemExit(2)
if not integer(engine["active_templates"]) or engine["active_templates"] < 0: raise SystemExit(2)
coverage = engine["coverage"]
if not isinstance(coverage, dict) or set(coverage) != {"standard","high_protein","vegetarian","vegan"}: raise SystemExit(2)
if any(not integer(value) or value < 0 for value in coverage.values()): raise SystemExit(2)
if engine["last_shadow"] is not None and not isinstance(engine["last_shadow"], dict): raise SystemExit(2)
if engine["p95_ms"] is not None and (not number(engine["p95_ms"]) or engine["p95_ms"] < 0): raise SystemExit(2)
if not isinstance(engine["ready"], bool): raise SystemExit(2)
blockers = engine["blockers"]
if not isinstance(blockers, list) or any(not isinstance(value, str) or not value for value in blockers): raise SystemExit(2)
print(mode + "|" + ("1" if engine["ready"] else "0") + "|" + ",".join(blockers))' 2>/dev/null
}

skontroluj_recipe_engine() {
  [ -n "${HEALTH:-}" ] || {
    recipe_engine_alert "health nie je dostupný"
    return 1
  }
  STAV_ENGINE=$(recipe_engine_health_state)
  PARSE_RC=$?
  if [ "$PARSE_RC" -eq 3 ]; then
    recipe_engine_alert "health neobsahuje recipe_engine"
    return 1
  fi
  if [ "$PARSE_RC" -ne 0 ]; then
    recipe_engine_alert "health má neplatnú schému alebo typy"
    return 1
  fi
  MODE=${STAV_ENGINE%%|*}
  REST=${STAV_ENGINE#*|}
  READY=${REST%%|*}
  BLOCKERS=${REST#*|}

  if [ "$MODE" != "on" ]; then
    if [ ! -x "$DIR/recipe-engine-rollout.sh" ]; then
      if [ "$MODE" = "shadow" ] && [ "$READY" != "1" ]; then
        log "shadow ešte nie je pripravený na aktiváciu — $BLOCKERS"
      fi
      return 0
    fi

    log "aktuálne dáta sú pripravené — aktivujem receptový engine"
    if ! UVARSI_NOTIFY_URL="https://ntfy.sh/${NTFY_TOPIC}" \
      "$DIR/recipe-engine-rollout.sh"; then
      recipe_engine_alert "automatická aktivácia po obnove dát zlyhala"
      return 1
    fi

    HEALTH=$("$CURL" -fsS --max-time 1 "$PLAN_QUEUE_HEALTH_URL" 2>/dev/null || true)
    STAV_ENGINE=$(recipe_engine_health_state) || {
      recipe_engine_alert "health po automatickej aktivácii sa nedá overiť"
      return 1
    }
    MODE=${STAV_ENGINE%%|*}
    REST=${STAV_ENGINE#*|}
    READY=${REST%%|*}
    BLOCKERS=${REST#*|}
    if [ "$MODE" != "on" ] || [ "$READY" != "1" ]; then
      recipe_engine_alert "automatická aktivácia neskončila pripraveným režimom on: ${BLOCKERS:-unknown}"
      return 1
    fi

    rm -f "$RECIPE_ENGINE_ALERT_STATE"
    log "receptový engine je znova aktívny"
    return 0
  fi
  if [ "$READY" = "1" ]; then
    rm -f "$RECIPE_ENGINE_ALERT_STATE"
    return 0
  fi

  case "$BLOCKERS" in
    smoke_missing|smoke_stale|smoke_failed) ;;
    *) recipe_engine_alert "readiness blokuje: ${BLOCKERS:-unknown}"; return 1 ;;
  esac

  case "$RECIPE_SMOKE_MIN_INTERVAL_SECONDS" in
    ''|*[!0-9]*|0) recipe_engine_alert "interval smoke kontroly má neplatný formát"; return 1 ;;
  esac
  LAST_ATTEMPT=0
  if [ -f "$RECIPE_SMOKE_ATTEMPT_STATE" ]; then
    read -r LAST_ATTEMPT < "$RECIPE_SMOKE_ATTEMPT_STATE" || LAST_ATTEMPT=0
  fi
  case "$LAST_ATTEMPT" in *[!0-9]*|'') LAST_ATTEMPT=0 ;; esac
  if [ $((NOW_EPOCH - LAST_ATTEMPT)) -lt "$RECIPE_SMOKE_MIN_INTERVAL_SECONDS" ]; then
    recipe_engine_alert "syntetický smoke je po nedávnom pokuse stále neúspešný"
    return 1
  fi
  printf '%s\n' "$NOW_EPOCH" > "$RECIPE_SMOKE_ATTEMPT_STATE"

  if ! (
    cd "$DIR/app" || exit 1
    if [ -f "$DIR/uvarsi.env" ]; then set -a; . "$DIR/uvarsi.env"; set +a; fi
    UVARSI_URL=https://uvar.si \
    UVARSI_VERSION_FILE="$DIR/VERSION" \
    UVARSI_DB="$DIR/uvarsi.db" \
    UVARSI_RECIPE_SMOKE_STATE="$RECIPE_SMOKE_STATE" \
      "$PY" -m server --recipe-engine-smoke --state "$RECIPE_SMOKE_STATE"
  ) >/dev/null 2>&1; then
    recipe_engine_alert "lokálny syntetický smoke zlyhal"
    return 1
  fi

  HEALTH=$("$CURL" -fsS --max-time 1 "$PLAN_QUEUE_HEALTH_URL" 2>/dev/null || true)
  STAV_ENGINE=$(recipe_engine_health_state) || {
    recipe_engine_alert "health po smoke sa nedá overiť"
    return 1
  }
  MODE=${STAV_ENGINE%%|*}
  REST=${STAV_ENGINE#*|}
  READY=${REST%%|*}
  if [ "$MODE" != "on" ] || [ "$READY" != "1" ]; then
    recipe_engine_alert "health po smoke nie je v režime on a pripravený"
    return 1
  fi
  rm -f "$RECIPE_ENGINE_ALERT_STATE"
  log "receptový engine: syntetický smoke OK"
  return 0
}

MON_ISO=$("$PY" -c 'from datetime import date, timedelta; import sys; d=date.fromisoformat(sys.argv[1]); print((d-timedelta(days=d.weekday())).isoformat())' "$TODAY")

# Recepty sú lokálne a deterministické: ich release smoke nesmie čakať na
# externý Anthropic kredit potrebný iba na čítanie letákov. Po každom vydaní
# ich preto overíme ešte pred možným kreditovým skratom zberača.
skontroluj_recipe_engine || log "receptový smoke sa teraz nepodaril — pri dokončení zdravého behu ho overím znova"

# --- Stav z predošlých dnešných pokusov (formát: "deň neúspechy blok") ---
# Číta sa hneď na začiatku, aby sa kreditový blok stihol uplatniť EŠTE PRED
# zbieračom — inak by hodinový beh zbytočne búchal na API, ktoré odmieta všetko.
FAILS=0
BLOKNUTE_NA="-"                      # "-" = žiadny blok; "KREDIT" = došiel kredit;
                                     # inak počet ponúk pri štrukturálnom páde
LAST_CREDIT_PROBE=0
LAST_CREDIT_RELEASE="-"
if [ -f "$STATE" ]; then
  read -r SDATE SFAILS SBLOK SPROBE SRELEASE < "$STATE" || true
  if [ "${SDATE:-}" = "$TODAY" ]; then
    FAILS=${SFAILS:-0}
    BLOKNUTE_NA=${SBLOK:--}
    LAST_CREDIT_PROBE=${SPROBE:-0}
    LAST_CREDIT_RELEASE=${SRELEASE:--}
  fi
fi

CURRENT_RELEASE="-"
if [ -f "$DIR/.nasadene_sha" ]; then
  read -r CURRENT_RELEASE < "$DIR/.nasadene_sha" || CURRENT_RELEASE="-"
  CURRENT_RELEASE=${CURRENT_RELEASE%$'\r'}
  case "$CURRENT_RELEASE" in ''|*[!0-9a-f]*) CURRENT_RELEASE="-" ;; esac
fi
RELEASE_CHANGED=0

zapis_kreditovy_blok() {
  if [ "$CURRENT_RELEASE" = "-" ]; then
    echo "$TODAY $FAILS KREDIT $NOW_EPOCH" > "$STATE"
  else
    echo "$TODAY $FAILS KREDIT $NOW_EPOCH $CURRENT_RELEASE" > "$STATE"
  fi
}

# Nulový kredit sa nedá opraviť opakovaním, ale dobitie účtu nevidíme. Preto
# pustíme najviac jeden overovací pokus za hodinu. Starý trojpoľový stav nemá
# epoch a po nasadení dostane jeden okamžitý probe — bezpečný, odmietnutie stojí 0 €.
if [ "$BLOKNUTE_NA" = "KREDIT" ]; then
  case "$LAST_CREDIT_PROBE" in *[!0-9]*|'') LAST_CREDIT_PROBE=0 ;; esac
  if [ "$CURRENT_RELEASE" != "-" ] && [ "$CURRENT_RELEASE" != "$LAST_CREDIT_RELEASE" ]; then
    RELEASE_CHANGED=1
  fi
  if [ "$LAST_CREDIT_PROBE" -gt 0 ] && \
     [ "$RELEASE_CHANGED" -ne 1 ] && \
     { [ "$NOW_EPOCH" -lt "$LAST_CREDIT_PROBE" ] || \
       [ $((NOW_EPOCH - LAST_CREDIT_PROBE)) -lt "$CREDIT_RETRY_SECONDS" ]; }; then
    log "KREDIT VYČERPANÝ — ďalší automatický probe bude najskôr po hodinovej prestávke."
    exit "$EXIT_STRUCTURAL"
  fi
  if [ "$RELEASE_CHANGED" -eq 1 ]; then
    log "nové vydanie — overujem doplnený kredit bez čakania na hodinový interval…"
  fi
  log "overujem, či bol Anthropic kredit doplnený…"
  BLOKNUTE_NA="-"
fi

landing_data_is_current() {
  (cd "$DIR" && "$PY" -c 'from app.landing_data import landing_data_is_current; from datetime import date; import sys; raise SystemExit(0 if landing_data_is_current(sys.argv[1], date.fromisoformat(sys.argv[2]), required_offer_data_version=2) else 1)' "$LANDING_DATA" "$TODAY")
}

zahrej_plany() {
  # Predpočet iba zaradí idempotentné low-priority úlohy do trvalej fronty;
  # Anthropic volá až samostatný worker. Beží preto pri každom hodinovom
  # dohľade, nie iba raz po zbere. Ak zaradenie zlyhá, ďalší beh sa zotaví
  # ešte v ten istý deň bez zásahu majiteľa.
  # Nastavenie žije iba na serveri a nasadenie ho neprepisuje.
  (
    cd "$DIR/app" || exit 1
    if [ -f "$DIR/predpocet.env" ]; then set -a; . "$DIR/predpocet.env"; set +a; fi
    UVARSI_URL=https://uvar.si UVARSI_VERSION_FILE="$DIR/VERSION" \
      "$PY" -u predpocet.py --zahrej
  ) || log "predpočet sa nepodarilo zaradiť — ďalší hodinový beh to skúsi znova"
}

# --- 0. Databáza akcií pre appku: má aktuálny týždeň? ---
# (appka skladá osobné plány z tejto DB; bez nej ľuďom nič nevygeneruje)
POCET=$(sqlite3 "$DIR/uvarsi.db" \
        "SELECT COUNT(*) FROM akcie
         WHERE valid_from IS NOT NULL AND valid_to IS NOT NULL
           AND valid_from <= '$TODAY' AND '$TODAY' <= valid_to" \
        2>/dev/null || echo 0)

# Celkový počet nestačí: keď zlyhá JEDEN obchod, ostatné dva ľahko prekročia
# prah a chýbajúci reťazec sa už nikdy nedobehne — používateľ potom dostane
# plán bez Lidlu a nedozvie sa to. (21. 8. 2026: 431 akcií, ale bez Lidlu.)
CHYBA_ZBER=$(sqlite3 "$DIR/uvarsi.db" \
  "SELECT COUNT(*) FROM (SELECT 'Kaufland' o UNION SELECT 'Tesco' UNION SELECT 'Lidl') v
   WHERE NOT EXISTS (SELECT 1 FROM zber_stav s
                     JOIN akcie z ON z.obchod=s.obchod AND z.tyzden=s.tyzden
                     WHERE s.obchod=v.o AND s.stav='ok'
                       AND COALESCE(s.data_version, 0) >= 2
                       AND z.valid_from IS NOT NULL AND z.valid_to IS NOT NULL
                       AND z.valid_from <= '$TODAY' AND '$TODAY' <= z.valid_to)
      OR (SELECT COUNT(*) FROM akcie a
          WHERE a.obchod=v.o
            AND a.valid_from IS NOT NULL AND a.valid_to IS NOT NULL
            AND a.valid_from <= '$TODAY' AND '$TODAY' <= a.valid_to) < $MIN_OFFERS_PER_STORE" \
  2>/dev/null || echo 3)

if [ "${POCET:-0}" -lt 30 ] || [ "${CHYBA_ZBER:-3}" -gt 0 ]; then
  if [ "${CHYBA_ZBER:-3}" -gt 0 ]; then
    log "týždeň $MON_ISO: $CHYBA_ZBER obchod(ov) nemá úspešný zber — dobieham dáta…"
  else
    log "akcie pre týždeň $MON_ISO chýbajú ($POCET) — spúšťam zbierač…"
  fi
  # Opravujeme iba obchody, ktorým chýba zdravý a dnes platný leták. Opakovať
  # úspešné Vision čítanie by míňalo kredit a znižovalo šancu, že sa chybný
  # obchod zmestí do ochranného limitu behov. Pri nečitateľnej DB radšej
  # spustíme všetky tri — fail-closed stav sa tým nezamaskuje.
  NEUPLNE_OBCHODY=$(sqlite3 "$DIR/uvarsi.db" \
    "SELECT lower(v.o) FROM (SELECT 'Kaufland' o UNION SELECT 'Tesco' UNION SELECT 'Lidl') v
     WHERE NOT EXISTS (SELECT 1 FROM zber_stav s
                       JOIN akcie z ON z.obchod=s.obchod AND z.tyzden=s.tyzden
                       WHERE s.obchod=v.o AND s.stav='ok'
                         AND COALESCE(s.data_version, 0) >= 2
                         AND z.valid_from IS NOT NULL AND z.valid_to IS NOT NULL
                         AND z.valid_from <= '$TODAY' AND '$TODAY' <= z.valid_to)
        OR (SELECT COUNT(*) FROM akcie a
            WHERE a.obchod=v.o
              AND a.valid_from IS NOT NULL AND a.valid_to IS NOT NULL
              AND a.valid_from <= '$TODAY' AND '$TODAY' <= a.valid_to) < $MIN_OFFERS_PER_STORE" \
    2>/dev/null || true)
  ZBER_ARGS=()
  for OBCHOD in $NEUPLNE_OBCHODY; do
    case "$OBCHOD" in
      kaufland|tesco|lidl) ZBER_ARGS+=(--store "$OBCHOD") ;;
    esac
  done
  if [ "${#ZBER_ARGS[@]}" -eq 0 ]; then
    ZBER_ARGS=(--store kaufland --store tesco --store lidl)
  fi
  ZBER_VYSTUP=$(cd "$DIR/app" && UVARSI_DEPLOY_CREDIT_PROBE="$RELEASE_CHANGED" \
    "$PY" -u zbierac_akcii.py "${ZBER_ARGS[@]}" 2>&1)
  ZBER_RC=$?
  [ -n "$ZBER_VYSTUP" ] && printf '%s\n' "$ZBER_VYSTUP"
  case "$ZBER_VYSTUP" in
    *KREDIT_VYCERPANY*)
      zapis_kreditovy_blok
      log "KREDIT VYČERPANÝ — zberač bol odmietnutý ešte pred čítaním; o hodinu automaticky overím dobitie."
      exit "$EXIT_STRUCTURAL"
      ;;
  esac
  if [ "$ZBER_RC" -eq 0 ]; then
    log "zbierač OK"
  else
    log "zbierač zlyhal — appka zatiaľ nemá aktuálne dáta"
  fi
fi

# Zber mohol dáta doplniť. Stav čítame znova aj bez zberu: predpočet sa smie
# spustiť iba nad kompletnou trojicou obchodov a musí sa vedieť zotaviť pri
# každom ďalšom hodinovom behu dozorcu.
POCET=$(sqlite3 "$DIR/uvarsi.db" \
        "SELECT COUNT(*) FROM akcie
         WHERE valid_from IS NOT NULL AND valid_to IS NOT NULL
           AND valid_from <= '$TODAY' AND '$TODAY' <= valid_to" \
        2>/dev/null || echo 0)
CHYBA_ZBER=$(sqlite3 "$DIR/uvarsi.db" \
  "SELECT COUNT(*) FROM (SELECT 'Kaufland' o UNION SELECT 'Tesco' UNION SELECT 'Lidl') v
   WHERE NOT EXISTS (SELECT 1 FROM zber_stav s
                     JOIN akcie z ON z.obchod=s.obchod AND z.tyzden=s.tyzden
                     WHERE s.obchod=v.o AND s.stav='ok'
                       AND COALESCE(s.data_version, 0) >= 2
                       AND z.valid_from IS NOT NULL AND z.valid_to IS NOT NULL
                       AND z.valid_from <= '$TODAY' AND '$TODAY' <= z.valid_to)
      OR (SELECT COUNT(*) FROM akcie a
          WHERE a.obchod=v.o
            AND a.valid_from IS NOT NULL AND a.valid_to IS NOT NULL
            AND a.valid_from <= '$TODAY' AND '$TODAY' <= a.valid_to) < $MIN_OFFERS_PER_STORE" \
  2>/dev/null || echo 3)
ZBER_REV=$(sqlite3 "$DIR/uvarsi.db" \
  "SELECT COALESCE(MAX(strftime('%s', updated)), '0')
   FROM zber_stav s
   WHERE EXISTS (SELECT 1 FROM akcie a
                 WHERE a.obchod=s.obchod AND a.tyzden=s.tyzden
                   AND a.valid_from IS NOT NULL AND a.valid_to IS NOT NULL
                   AND a.valid_from <= '$TODAY' AND '$TODAY' <= a.valid_to)" \
  2>/dev/null || echo 0)
DATOVY_STAV="${POCET:-0}:${CHYBA_ZBER:-3}:${ZBER_REV:-0}"
# --- 1. Už je aktuálny landing JSON pripravený? ---
if landing_data_is_current; then
  if [ "${POCET:-0}" -ge 30 ] && [ "${CHYBA_ZBER:-3}" -eq 0 ]; then
    zahrej_plany
  fi
  skontroluj_recipe_engine || exit 1
  rm -f "$STATE"
  exit 0
fi

# --- 2. Uplatni dnešný štrukturálny blok ---
# Štrukturálny pád sa opakuje len vtedy, keď sa vstupné dáta odvtedy zmenili.
if [ "$BLOKNUTE_NA" != "-" ] && [ "$BLOKNUTE_NA" = "$DATOVY_STAV" ]; then
  upozorni_detail_zberu "$DATOVY_STAV" "$MON_ISO"
  log "ŠTRUKTURÁLNA chyba a stav zberu $DATOVY_STAV sa odvtedy nezmenil — nespúšťam ďalší pokus (šetrím kredit)."
  exit "$EXIT_STRUCTURAL"
fi

if [ "$FAILS" -ge "$MAX_TRIES" ]; then
  log "dnes už $FAILS neúspešných pokusov — pauza do zajtra (šetrím kredit)."
  exit 1
fi

log "landing JSON nie je aktuálny — pokus $((FAILS+1))/$MAX_TRIES…"

# --- 3. Skús obnoviť ---
# Výstup ide do premennej aj do logu: dozorca z neho musí prečítať, ČI bol pád
# o dátach alebo o účte. Bez toho by nulový kredit vyzeral ako hocijaká iná
# štrukturálna chyba a majiteľ by dostal hlášku, ktorá mu nepovie, čo urobiť.
VYSTUP=$(cd "$DIR" && "$PY" -u refresh_blocek.py "$LANDING_DATA" 2>&1)
RC=$?
[ -n "$VYSTUP" ] && printf '%s\n' "$VYSTUP"

# --- 3a. Došiel kredit: opakovanie nepomôže, kým ho majiteľ nedobije ---
# Notifikáciu posiela naklady.py práve raz za deň — dozorca ju NEZDVOJUJE
# (notify_kredit_preskoc), inak by majiteľ dostal to isté dvakrát za hodinu.
case "$VYSTUP" in
  *KREDIT_VYCERPANY*)
    zapis_kreditovy_blok
    log "KREDIT VYČERPANÝ — bloček bol odmietnutý; o hodinu automaticky overím dobitie."
    exit "$EXIT_STRUCTURAL"
    ;;
esac

if [ "$RC" -eq 0 ] && landing_data_is_current; then
  log "OK — landing JSON obnovený na týždeň $MON_ISO."
  if [ "$FAILS" -gt 0 ]; then
    notify "Uvar.si opravené" "Landing JSON sa obnovil na týždeň $MON_ISO (po $FAILS neúspešných pokusoch)."
  fi
  if [ "${POCET:-0}" -ge 30 ] && [ "${CHYBA_ZBER:-3}" -eq 0 ]; then
    zahrej_plany
  fi
  skontroluj_recipe_engine || exit 1
  rm -f "$STATE"
  exit 0
fi

# --- 4a. Štrukturálny pád: opakovanie nepomôže, kým sa dáta nezmenia ---
if [ "$RC" -eq "$EXIT_STRUCTURAL" ]; then
  echo "$TODAY $FAILS $DATOVY_STAV" > "$STATE"
  log "ŠTRUKTURÁLNA chyba (kód $RC) pri stave zberu $DATOVY_STAV — ďalšie pokusy nespúšťam, kým sa dáta nezmenia."
  TAIL=$(tail -12 /var/log/uvarsi.log 2>/dev/null | tr '\n' ' ' | tail -c 400)
  notify "Uvar.si: bloček sa nedá zostaviť" \
    "Týždeň $MON_ISO — refresh_blocek skončil štrukturálnou chybou pri ${POCET:-0} ponukách v DB. Opakovanie nepomôže, treba zásah. Log: $TAIL"
  upozorni_detail_zberu "$DATOVY_STAV" "$MON_ISO"
  exit "$EXIT_STRUCTURAL"
fi

# --- 4b. Dočasný neúspech: zapíš, upozorni ak treba, o hodinu skúsi znova ---
FAILS=$((FAILS+1))
echo "$TODAY $FAILS -" > "$STATE"
log "pokus $FAILS zlyhal (kód $RC) — skúsim znova o hodinu."

if [ "$FAILS" -eq "$NOTIFY_AT" ]; then
  TAIL=$(tail -12 /var/log/uvarsi.log 2>/dev/null | tr '\n' ' ' | tail -c 400)
  notify "Uvar.si: bloček sa neobnovuje" \
    "Týždeň $MON_ISO — $FAILS neúspešné pokusy, skúšam ďalej každú hodinu. Log: $TAIL"
fi
exit 1
