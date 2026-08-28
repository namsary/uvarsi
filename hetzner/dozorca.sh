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
# dozorca zapíše blok "KREDIT" — dnes už nespustí nič, zajtra to skúsi znova.
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
STATE="$DIR/.dozorca_state"          # formát: "RRRR-MM-DD pocet_neuspechov blok"
PLAN_QUEUE_ALERT_STATE="$DIR/.plan_queue_alert_state"
PLAN_QUEUE_HEALTH_URL="${UVARSI_PLAN_QUEUE_HEALTH_URL:-http://127.0.0.1:8090/api/health}"
MAX_TRIES=6                          # max pokusov za jeden deň
NOTIFY_AT=2                          # po koľkých neúspechoch upozorniť
EXIT_STRUCTURAL=3                    # kód, ktorým refresh_blocek hlási "neopakuj"
NTFY_TOPIC="uvarsi-jarvis-8f3a2c"    # notifikácie: ntfy.sh/<topic>

log(){ echo "[$(date '+%F %T')] DOZORCA: $*"; }
notify(){ "$CURL" -s --max-time 15 -H "Title: $1" -d "$2" "https://ntfy.sh/${NTFY_TOPIC}" >/dev/null 2>&1; }

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

TODAY="${UVARSI_TODAY:-$(date +%F)}"

skontroluj_frontu_planov() {
  # Health odpoveď je jediný zdroj pravdy: dozorca nesmie z počtu procesov
  # hádať, či worker reálne obnovuje lease.
  HEALTH=$("$CURL" -fsS --max-time 1 "$PLAN_QUEUE_HEALTH_URL" 2>/dev/null || true)
  [ -n "$HEALTH" ] || { log "UNKNOWN — frontu plánov sa nedá overiť cez health; značka upozornenia ostáva"; return; }
  STAV_FRONTY=$(printf '%s' "$HEALTH" | "$HEALTH_PY" -c '
import datetime as dt, json, sys
q = json.load(sys.stdin).get("plan_queue")
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
if not q["worker_alive"] or q["heartbeat_seconds"] > 60: print("WORKER")
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

MON_ISO=$("$PY" -c 'from datetime import date, timedelta; import sys; d=date.fromisoformat(sys.argv[1]); print((d-timedelta(days=d.weekday())).isoformat())' "$TODAY")

# --- Stav z predošlých dnešných pokusov (formát: "deň neúspechy blok") ---
# Číta sa hneď na začiatku, aby sa kreditový blok stihol uplatniť EŠTE PRED
# zbieračom — inak by hodinový beh zbytočne búchal na API, ktoré odmieta všetko.
FAILS=0
BLOKNUTE_NA="-"                      # "-" = žiadny blok; "KREDIT" = došiel kredit;
                                     # inak počet ponúk pri štrukturálnom páde
if [ -f "$STATE" ]; then
  read -r SDATE SFAILS SBLOK < "$STATE" || true
  if [ "${SDATE:-}" = "$TODAY" ]; then
    FAILS=${SFAILS:-0}
    BLOKNUTE_NA=${SBLOK:--}
  fi
fi

# Nulový kredit sa dnes už zistil. Opakovať sa nedá „kým sa dáta nezmenia" —
# tu sa musí zmeniť účet. Upozornenie už odišlo z naklady.py (práve raz),
# dozorca teda mlčí a len nespúšťa ďalšie pokusy. Zajtra sa skúsi znova.
if [ "$BLOKNUTE_NA" = "KREDIT" ]; then
  log "KREDIT VYČERPANÝ — dnes už nič nespúšťam. Treba dobiť kredit na Anthropic API."
  exit "$EXIT_STRUCTURAL"
fi

landing_data_is_current() {
  (cd "$DIR" && "$PY" -c 'from app.landing_data import landing_data_is_current; from datetime import date; import sys; raise SystemExit(0 if landing_data_is_current(sys.argv[1], date.fromisoformat(sys.argv[2])) else 1)' "$LANDING_DATA" "$TODAY")
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
        "SELECT COUNT(*) FROM akcie WHERE tyzden='$MON_ISO'" 2>/dev/null || echo 0)

# Celkový počet nestačí: keď zlyhá JEDEN obchod, ostatné dva ľahko prekročia
# prah a chýbajúci reťazec sa už nikdy nedobehne — používateľ potom dostane
# plán bez Lidlu a nedozvie sa to. (21. 8. 2026: 431 akcií, ale bez Lidlu.)
CHYBA_ZBER=$(sqlite3 "$DIR/uvarsi.db" \
  "SELECT COUNT(*) FROM (SELECT 'Kaufland' o UNION SELECT 'Tesco' UNION SELECT 'Lidl') v
   WHERE NOT EXISTS (SELECT 1 FROM zber_stav s
                     WHERE s.tyzden='$MON_ISO' AND s.obchod=v.o AND s.stav='ok')" \
  2>/dev/null || echo 3)

if [ "${POCET:-0}" -lt 30 ] || [ "${CHYBA_ZBER:-3}" -gt 0 ]; then
  if [ "${CHYBA_ZBER:-3}" -gt 0 ]; then
    log "týždeň $MON_ISO: $CHYBA_ZBER obchod(ov) nemá úspešný zber — dobieham dáta…"
  else
    log "akcie pre týždeň $MON_ISO chýbajú ($POCET) — spúšťam zbierač…"
  fi
  if cd "$DIR/app" && "$PY" -u zbierac_akcii.py; then
    log "zbierač OK"
  else
    log "zbierač zlyhal — appka zatiaľ nemá aktuálne dáta"
  fi
fi

# Zber mohol dáta doplniť. Stav čítame znova aj bez zberu: predpočet sa smie
# spustiť iba nad kompletnou trojicou obchodov a musí sa vedieť zotaviť pri
# každom ďalšom hodinovom behu dozorcu.
POCET=$(sqlite3 "$DIR/uvarsi.db" \
        "SELECT COUNT(*) FROM akcie WHERE tyzden='$MON_ISO'" 2>/dev/null || echo 0)
CHYBA_ZBER=$(sqlite3 "$DIR/uvarsi.db" \
  "SELECT COUNT(*) FROM (SELECT 'Kaufland' o UNION SELECT 'Tesco' UNION SELECT 'Lidl') v
   WHERE NOT EXISTS (SELECT 1 FROM zber_stav s
                     WHERE s.tyzden='$MON_ISO' AND s.obchod=v.o AND s.stav='ok')" \
  2>/dev/null || echo 3)
# --- 1. Už je aktuálny landing JSON pripravený? ---
if landing_data_is_current; then
  if [ "${POCET:-0}" -ge 30 ] && [ "${CHYBA_ZBER:-3}" -eq 0 ]; then
    zahrej_plany
  fi
  rm -f "$STATE"
  exit 0
fi

# --- 2. Uplatni dnešný štrukturálny blok ---
# Štrukturálny pád sa opakuje len vtedy, keď sa vstupné dáta odvtedy zmenili.
if [ "$BLOKNUTE_NA" != "-" ] && [ "$BLOKNUTE_NA" = "${POCET:-0}" ]; then
  log "ŠTRUKTURÁLNA chyba pri ${POCET:-0} ponukách a dáta sa odvtedy nezmenili — nespúšťam ďalší pokus (šetrím kredit)."
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
    echo "$TODAY $FAILS KREDIT" > "$STATE"
    log "KREDIT VYČERPANÝ — refresh_blocek hlási, že Anthropic API odmieta volania pre nulový kredit. Ďalšie pokusy dnes nespúšťam, treba dobiť kredit."
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
  rm -f "$STATE"
  exit 0
fi

# --- 4a. Štrukturálny pád: opakovanie nepomôže, kým sa dáta nezmenia ---
if [ "$RC" -eq "$EXIT_STRUCTURAL" ]; then
  echo "$TODAY $FAILS ${POCET:-0}" > "$STATE"
  log "ŠTRUKTURÁLNA chyba (kód $RC) pri ${POCET:-0} ponukách — ďalšie pokusy nespúšťam, kým sa dáta nezmenia."
  TAIL=$(tail -12 /var/log/uvarsi.log 2>/dev/null | tr '\n' ' ' | tail -c 400)
  notify "Uvar.si: bloček sa nedá zostaviť" \
    "Týždeň $MON_ISO — refresh_blocek skončil štrukturálnou chybou pri ${POCET:-0} ponukách v DB. Opakovanie nepomôže, treba zásah. Log: $TAIL"
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
