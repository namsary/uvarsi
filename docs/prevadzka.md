# Auth v3 rollout runbook

This is the canonical auth-v3 rollout procedure for Uvar.si. Task 10 creates
and reviews this local runbook and its smoke helper only. The production
benchmark, backup, release, flag change, and activation belong to the release
controller and require separate approval.

## Non-negotiable guardrails

- Payments stay OFF (`PLATBY_ZAPNUTE=0`) for the entire rollout.
- Do not edit, reload, restart, or stop Caddy, Taktik-mapa, timers, or any
  unrelated service. Read-only status and HTTP checks are allowed. The standard
  Uvar deployment is the only release step allowed to control both Uvar
  services and its own bounded supervisor cron row; it preserves all unrelated
  cron rows. Flag activation and rollback restart only uvarsi.
- Do not run `nasad.ps1`; it has a wider operational scope than this rollout.
- Do not print or capture e-mail addresses, passwords, cookies, tokens,
  response bodies from authenticated endpoints, environment values, or PII.
- Use a separate test account. Keep one existing old session open from before
  deployment, and do not use it for the mutating smoke.
- Every stage ends in a STOP GATE. Stop immediately on a failed or ambiguous
  check. Do not continue on partial evidence.

## Stage 1 — Preflight dependencies and baseline

The controller records the candidate commit and release directory without
publishing either. Confirm that `requirements-auth.txt` is from that exact
candidate. Install it into the existing venv before any release is pushed or
activated:

```bash
sudo /opt/uvarsi/venv/bin/python -m pip install --disable-pip-version-check \
  -r "$RELEASE/requirements-auth.txt"
/opt/uvarsi/venv/bin/python -c 'import argon2, webauthn; print("auth imports: ok")'
```

Perform these read-only baseline checks:

```bash
curl -fsS --max-time 10 https://uvar.si/api/health | \
  /opt/uvarsi/venv/bin/python -c 'import json,sys; d=json.load(sys.stdin); assert d.get("vydanie") and d.get("tyzden"); print("health shape: ok")'
curl -fsS --max-time 10 -o /dev/null https://uvar.si/co-varit-tento-tyzden
curl -fsS --max-time 10 -o /dev/null https://mapa.89.167.72.159.sslip.io/
grep -Eq '^[[:space:]]*(export[[:space:]]+)?PLATBY_ZAPNUTE=(0|false|off)[[:space:]]*$' \
  /opt/uvarsi/uvarsi.env
```

In a browser, prove the existing old session is authenticated before the
release. Record only pass/fail, browser class, UTC time, and candidate commit.
Do not record identity data or authenticated response bodies. Record a
read-only status snapshot for uvarsi, Caddy, Taktik-mapa, the plan worker, and
cron; do not issue a service-control command for any of them.

**STOP GATE 1:** imports succeed, payments are off, health and current-week
landing pass, the second hosted app passes, and the old session works. If not,
stop before benchmark, backup, push, or deployment.

## Stage 2 — Argon2 benchmark and memory pressure

Benchmark the exact `PasswordHasher(type=Type.ID)` defaults used by the
candidate. Use a synthetic benchmark-only secret. Never use a user password.

```bash
free -m
/usr/bin/time -v /opt/uvarsi/venv/bin/python - <<'PY'
import concurrent.futures
import statistics
import time
from argon2 import PasswordHasher, Type

hasher = PasswordHasher(type=Type.ID)
sample = "auth-v3-benchmark-only-value"

def one_hash(_):
    started = time.perf_counter()
    hasher.hash(sample)
    return (time.perf_counter() - started) * 1000

serial = [one_hash(i) for i in range(7)]
with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
    concurrent = list(pool.map(one_hash, range(4)))
print("serial_median_ms=%.1f" % statistics.median(serial))
print("concurrent_max_ms=%.1f" % max(concurrent))
print("argon2 m=%s,t=%s,p=%s" % (
    hasher.memory_cost, hasher.time_cost, hasher.parallelism))
PY
free -m
```

Acceptance target: the serial median is **150–350 ms**. For the four-way
memory pressure check, `Maximum resident set size` must remain below 25% of
available RAM, swap use must not grow, and kernel logs must show no OOM event.
Inspect the OOM signal read-only with the controller-approved journal query;
do not clear or rotate logs.

If the target is missed, STOP. Do not live-edit Python or tune production in
place. Prepare a separately reviewed code change for explicit Argon2
parameters, rerun local auth tests, and restart this runbook from Stage 1.

**STOP GATE 2:** retain only timing, Argon2 parameter numbers, peak RSS,
available-memory totals, swap delta, UTC time, and pass/fail. No secret or
input value enters evidence.

## Stage 3 — SQLite online backup, integrity, and counts

Create a SQLite online backup while the application remains available. The
script prints only `PRAGMA integrity_check` and counts only—no PII and no row
values. The four canonical count labels map to users (`pouzivatelia`),
entitlements (`naroky`), sessions (`sessions_v2`), and plans (`plany`).

```bash
BACKUP="/opt/uvarsi/backups/auth-v3-pre-$(date -u +%Y%m%dT%H%M%SZ).db"
sudo install -d -m 0700 /opt/uvarsi/backups
sudo /opt/uvarsi/venv/bin/python - "$BACKUP" <<'PY'
import os
import sqlite3
import sys

source = sqlite3.connect("file:/opt/uvarsi/uvarsi.db?mode=ro", uri=True)
target_path = sys.argv[1]
temporary_path = target_path + ".in-progress"
target = sqlite3.connect(temporary_path)
try:
    source.backup(target)
    integrity = target.execute("PRAGMA integrity_check").fetchone()[0]
    if integrity != "ok":
        raise SystemExit("backup integrity failed")
    print("integrity_check=ok")
    for label, table in (
        ("users", "pouzivatelia"),
        ("entitlements", "naroky"),
        ("sessions", "sessions_v2"),
        ("plans", "plany"),
    ):
        count = target.execute("SELECT COUNT(*) FROM " + table).fetchone()[0]
        print("%s_count=%d" % (label, count))
finally:
    target.close()
    source.close()
os.chmod(temporary_path, 0o600)
os.replace(temporary_path, target_path)
PY
sudo test -s "$BACKUP"
```

Record the backup path, size, SHA-256, integrity result, and the four aggregate
counts. Restrict the evidence file to the release controller. Never attach the
database itself to a ticket or chat.

**STOP GATE 3:** the online backup exists, is mode `0600`, has a recorded hash,
passes integrity, and has all four counts. If any check fails, stop. Do not
deploy and do not attempt a database repair.

## Stage 4 — Backend release with the flag OFF

Before the controller starts its separately approved push/deployment gate,
atomically set `UVARSI_AUTH_V3=0` in `/opt/uvarsi/uvarsi.env` without displaying
that file or any other environment value. Confirm only that the key exists once
and equals `0`. The release controller then deploys the reviewed backend
candidate. This document does not authorize push, upload, SSH, or deployment.

Use the existing reviewed **standard Uvar deployment** represented by the
installed `hetzner/samopull.sh` release path. It atomically installs one app
release, restarts both uvarsi and uvarsi-plan-worker, and rejects success until
it observes a fresh heartbeat. The worker binary comes from the same reviewed release as the server.
Do not substitute `nasad.ps1`: that wider maintenance
script can manage Caddy and cron and is outside this rollout.

The standard deployment must not modify Caddy, Taktik-mapa, timers, payment
configuration, the feature flag, or any unrelated cron row. It may atomically
replace only the Uvar.si supervisor row with its bounded wrapper. Once the
controller reports the coherent server-and-worker candidate deployed with the
flag off, verify:

1. `/api/health` has the expected candidate release and current week.
2. `/co-varit-tento-tyzden` and `/api/public/landing` return valid current-week
   content without recording their full bodies.
3. The plan worker reports a fresh heartbeat: capture
   `plan_queue.heartbeat_at` before deployment, then require a later timestamp
   and `plan_queue.worker_alive=true` after deployment. The timestamp must be
   newer than the pre-deploy value, proving the restarted worker is this release.
4. The existing old session still opens `/app` and remains authenticated.
5. The second hosted app at `mapa.89.167.72.159.sslip.io` still returns success.
6. Anonymous `/api/me` does not expose auth-v3 capability while the flag is off.
7. Payments stay off.

**STOP GATE 4:** all seven checks pass. If any fails, leave the flag off, stop
the rollout, and hand control back to the release controller. Do not activate.

## Stage 5 — Guarded activation

Only after STOP GATE 4 passes may the controller atomically set
`UVARSI_AUTH_V3=1`. Confirm the single key without printing the environment
file. Activate by restarting one unit only:

```bash
sudo systemctl restart uvarsi
sudo systemctl is-active --quiet uvarsi
```

Do not run a grouped service command. This flag-only activation does not
replace binaries: do not touch Caddy, Taktik-mapa, uvarsi-plan-worker, cron,
timers, or payment configuration.

Immediately repeat health, current-week landing, fresh heartbeat, existing old
session, second hosted app, and payments-off checks. Anonymous `/api/me` must
now expose `auth_v3: true` without identity data.

**STOP GATE 5:** any failed check triggers Stage 7 immediately. No additional
mutation, registration, or device smoke is allowed first.

## Stage 6 — Separate-account smoke: desktop, mobile, and PWA

Use a dedicated test account, never the existing old-session account. On a
Windows desktop, first run read-only mode:

```powershell
.\nastroje\over_auth_v3.ps1 -BaseUrl https://uvar.si -ExpectedOrigin https://uvar.si
```

Then use an interactive secure prompt and explicitly authorize session
mutation:

```powershell
$testCredential = Get-Credential
.\nastroje\over_auth_v3.ps1 -BaseUrl https://uvar.si -ExpectedOrigin https://uvar.si `
  -AllowMutation -Credential $testCredential
```

The helper verifies password login, two simultaneous sessions, `/api/me`
capability and identity shape, current-session logout, the other session
surviving, password fallback, logout-others from the current session, the
current session surviving, and the other session becoming anonymous. It keeps
independent web sessions in memory and verifies cleanup. It never prints
credentials, raw cookies/tokens, or response bodies. Every REST request refuses
redirects; a redirect is a failed phase, never a smoke success.

Registration is not part of the normal smoke. Only when the controller has an
approved disposable mailbox and explicitly accepts one test message may it add
`-AllowDisposableRegistrationProbe` and a separate
`-DisposableRegistrationCredential`. Verify only the generic registration
response shape; never record the body.

WebAuthn is a manual browser/PWA ceremony, not a REST smoke operation. The
PowerShell helper cannot verify authenticator prompts, RP/origin UI, or the
device's user-verification ceremony. Passkey on a supported phone is required
and must be completed manually in the mobile browser or installed PWA.

Repeat the user-visible flow on mobile browser and installed PWA with separate
sessions: password login, supported-phone Passkey registration and login,
password fallback, reset flow, logout-current, and logout-others. Logout-current and logout-others are required: prove current-device logout preserves the other
device, then prove logout-others preserves the initiating device and revokes the
other device. Record only device/browser class and pass/fail. Recheck the
original old session last.

**STOP GATE 6:** desktop, mobile, PWA, required supported-phone Passkey,
logout-current, logout-others, old-session preservation, and password fallback
all pass. Any failure triggers Stage 7.

## Stage 7 — Rollback

**ROLLBACK IS FLAG OFF ONLY.** Atomically restore `UVARSI_AUTH_V3=0`, verify the
single key without printing the file, then restart only uvarsi:

```bash
sudo systemctl restart uvarsi
sudo systemctl is-active --quiet uvarsi
```

Never roll back the database. Never restore the pre-rollout SQLite backup over
the live database: auth-v3 migrations are additive and may already contain
valid post-deploy state. Do not revert data, rewrite migrated tables, or delete
sessions. Keep payments off and preserve Caddy, Taktik-mapa, plan worker, cron,
timers, and every other service unchanged.

After flag-off restart, verify health, current-week landing, fresh worker
heartbeat, existing old session, and the second hosted app. Escalate the failed
phase and sanitized evidence to the controller.

**STOP GATE 7:** rollout remains stopped until a separately reviewed fix is
ready and the controller restarts this runbook at Stage 1.

## Evidence checklist — without secrets

- [ ] Candidate commit/release identifier and UTC timestamps.
- [ ] Dependency import pass/fail; no package credentials or environment values.
- [ ] Argon2 median, parameters, available memory, peak RSS, swap delta, OOM
      pass/fail; no benchmark input.
- [ ] Backup path, mode, size, SHA-256, integrity result, and users /
      entitlements / sessions / plans counts only; no PII or row values.
- [ ] Flag-off deploy gate result and flag-on activation gate result; never the
      contents of `uvarsi.env`.
- [ ] Health release/week shape, current-week landing pass/fail, and fresh
      heartbeat timestamps; no authenticated response body.
- [ ] Existing old session, second hosted app, desktop, mobile, PWA, required
      supported-phone Passkey ceremony, reset, logout-current, logout-others,
      both session postconditions, and password fallback pass/fail only.
- [ ] Payments stay off; Taktik-mapa and Caddy were preserved and not touched.
- [ ] If used, rollback phase, reason code, flag-off verification, and post-checks.
- [ ] Explicit statement that no e-mail, password, cookie, token, challenge,
      environment value, database row, or other secret was captured.

## Platobný release — zakladajúci člen 39 €

Táto časť je povinná pred prvou ostrou platbou. Nasadenie samo platby nikdy
nezapne. Počas deployu musí byť v `/opt/uvarsi/uvarsi.env` práve jeden explicitný
riadok `PLATBY_ZAPNUTE=0`; chýbajúca, duplicitná alebo nejednoznačná hodnota
release zastaví ešte pred zmenou živých súborov.

### Čo deploy chráni

- Pred prepnutím vytvorí online SQLite zálohu a overí jej integritu.
- Kandidátsky kód spustí svoje migrácie pred prvou health kontrolou. Každé
  vydanie, ktoré mení schému, musí osobitne preukázať kompatibilitu rollbacku;
  tento platobný release novú zmenu schémy nepridáva.
- Pri chybe vráti kód, statické súbory a Uvar.si systemd jednotky.
- Automatický rollback nikdy neobnoví starú databázu cez živú databázu. Nová
  relácia, plán, webhook, nárok ani zákaznícka požiadavka sa tým nestratia.
- Databázová záloha je iba pre samostatne schválenú manuálnu obnovu po skutočnej
  dátovej havárii, nie pre rollback vydania.
- Caddy, Taktik-mapa a iné služby zostávajú mimo samopullu.

### STOP GATE — konfigurácia a verejná pripravenosť

Testovacie a živé LemonSqueezy prostriedky musia byť oddelené. Bez vypisovania
hodnôt over prítomnosť `LEMON_TEST_API_KEY`, `LEMON_TEST_WEBHOOK_SECRET`,
`LEMON_TEST_STORE_ID`, `LEMON_TEST_VARIANT_ID` a samostatne plánovanú živú
konfiguráciu `LEMON_API_KEY`, `LEMON_WEBHOOK_SECRET`, `LEMON_STORE_ID`,
`LEMON_VARIANT_ID`, `LEMON_CHECKOUT_URL`. Lokálny marker podpisuje tretie,
nezávislé tajomstvo `UVARSI_PAYMENT_SMOKE_SIGNING_SECRET`.

`/api/health` musí mať správne vydanie, živého worker-a, prejdenú receptovú
bránu, schválené aktuálne cenové zdroje, funkčné spotrebiteľské workflow a
nulové nevyriešené platobné prípady. Deploy kontroluje nielen hodnotu v env
súbore, ale aj skutočný bežiaci proces: `recipe_engine.payments_enabled` musí
byť `false`. Pred smoke testom smie byť jediný blocker
`payment_smoke_missing`.

### Jeden testovací nákup a úplná refundácia

Na serveri spusti interaktívny nástroj pod účtom, ktorý smie čítať Uvar.si
konfiguráciu a zapisovať do `/var/lib/uvarsi`:

```bash
cd /opt/uvarsi
./venv/bin/python ./payment-smoke.py
```

Použi čistý testovací Uvar.si účet a LemonSqueezy **Test mode**. Nástroj:

1. overí verejnú pripravenosť a to, že platby ostali vypnuté;
2. prihlási testovací účet cez normálny heslový endpoint a cookie drží iba v
   pamäti;
3. ešte pred zobrazením odkazu cez API overí variant aj jeho nadradený produkt:
   oba musia byť v testovacom režime, zverejnené, jednorazový variant musí stáť
   39 € a produkt musí patriť presnému testovaciemu store;
4. vytvorí nový testovací checkout cez API poskytovateľa a otvorenie hosťovanej
   pokladne nechá na človeka — číslo platobnej karty ani bezpečnostný kód nikdy
   neprijíma;
5. cez testovacie API a presný podpísaný `order_created` webhook overí
   objednávku, presne jeden nárok a autentifikovaný `/api/platba/stav`;
   API rekonciliácia sa za dôkaz webhooku nepočíta;
6. po doručení testovacieho dokladu musí operátor napísať presne
   `POTVRDZUJEM`; potom nástroj vyžiada úplnú refundáciu cez dokumentované
   `POST /v1/orders/:id/refund` bez čiastkovej sumy;
7. cez presný podpísaný `order_refunded` webhook overí refundáciu, odobratie
   Premium a nulové otvorené prípady pre túto testovaciu objednávku; nesúvisiace
   riadky vo fronte nemení;
8. zapíše súbor `/var/lib/uvarsi/payment-smoke.json` s právami `0600`.

Marker neobsahuje e-mail, ID objednávky ani jeho hash. Obsahuje výsledky celého
životného cyklu, identifikátory testovacieho store a variantu a HMAC podpis
viazaný na presné vydanie aj odtlačok živej checkout URL, store a variantu.
Zmena vydania alebo živej platobnej konfigurácie marker automaticky zneplatní.
Hodinová produkčná rekonciliácia explicitné test-mode webhooky preskočí, aby ich
nemohla spotrebovať skôr než tento test s vlastným testovacím tajomstvom.

Zakladajúca ponuka musí byť v LemonSqueezy samostatný zverejnený **živý produkt
s jediným variantom**. `LEMON_CHECKOUT_URL` musí byť presne `buy_now_url`, ktorú
pre tento produkt vráti API poskytovateľa; ručne skopírovaný odkaz na iný
produkt, testovací režim alebo viacvýznamový produkt smoke test odmietne.
Produkčná rekonciliácia aj webhook prijmú iba udalosti s explicitným
`test_mode=false`; testovacie alebo neoznačené udalosti nesmú meniť živé nároky.

Referencie poskytovateľa: [Test mode](https://docs.lemonsqueezy.com/help/getting-started/test-mode),
[Testing and going live](https://docs.lemonsqueezy.com/guides/developer-guide/testing-going-live)
a [Issue a refund](https://docs.lemonsqueezy.com/api/orders/issue-refund).

### Posledná brána majiteľa

Po smoke teste znovu načítaj `/api/health`. `payment_readiness.ready` musí byť
`true` a `blockers` musí byť prázdne, pričom `PLATBY_ZAPNUTE` je stále `0`.
Zaznamenaj iba vydanie, čas a výsledky brán — nikdy kľúče, cookie, e-mail ani
objednávkový identifikátor.

`PLATBY_ZAPNUTE=1` sa smie nastaviť až po samostatnom výslovnom schválení
majiteľa. Po zapnutí okamžite over health, checkout summary a jednu bezpečnú
požiadavku bez dokončenia platby. Ak readiness nie je zelená, checkout ostane
serverom zablokovaný aj pri chybne zapnutom flage.

Pri platobnom incidente najprv nastav `PLATBY_ZAPNUTE=0` a reštartuj iba Uvar.si.
Databázu nevracaj. Rekonciliácia musí ďalej spracovať už prijaté podpísané
webhooky, refundácie a zákaznícke nároky.

## Tesco bridge — bezpečné nastavenie a release gate

Tesco bridge má dve rozdielne tajomstvá. `BRIDGE_SECRET` autentifikuje Hetzner
voči Workeru; rovnakú hodnotu server pozná ako
`UVARSI_TESCO_BRIDGE_SECRET`. `TOKEN_SECRET` podpisuje 24-hodinové media tokeny
a zostáva iba v Cloudflare. Release identita je 12- až 64-znakový malý
hexadecimálny commit SHA presne skontrolovaného Workeru. V správcovi hesiel
vytvor `BRIDGE_SECRET` v tvare `<release-sha>.<náhodný-base64url-reťazec>`;
náhodná časť musí mať aspoň 32 znakov. `TOKEN_SECRET` vytvor ako samostatný
náhodný base64url reťazec s najmenej 32 znakmi. Bridge secret rotuj pri každom
Worker release. Tajomstvá nevkladaj do príkazu, commitu, ticketu, chatu ani
release reportu.

### 1. Cloudflare Worker

V lokálnom adresári `cloudflare/tesco-bridge` spusti nasledujúce príkazy po
jednom. Wrangler si hodnotu vypýta interaktívne; vlož ju až do jeho promptu,
takže sa neobjaví v histórii shellu:

```text
npx wrangler secret put BRIDGE_SECRET
npx wrangler secret put TOKEN_SECRET
```

Po nastavení oboch tajomstiev nasaď z toho istého čistého checkoutu presne
skontrolovaný Worker. Neutajovaný release SHA si môžeš overiť cez
`git rev-parse HEAD`; musí sa zhodovať s prefixom uloženého `BRIDGE_SECRET`:

```text
npm run deploy
```

Do evidencie zapíš iba názov projektu `uvarsi-tesco-bridge`, tento release SHA,
presný pridelený `*.workers.dev` host, čas a pass/fail — nie výstup
autentifikovanej odpovede ani hodnotu tajomstva.

### 2. Hetzner bez vypísania hodnôt

Na serveri otvor konfiguráciu priamo v editore:

```text
sudoedit /opt/uvarsi/uvarsi.env
```

V editore nastav práve jeden riadok pre každý z týchto kľúčov:

```text
UVARSI_ENV=production
UVARSI_TESCO_BRIDGE_URL=https://uvarsi-tesco-bridge.<účet>.workers.dev
UVARSI_TESCO_BRIDGE_WORKER_HOST=uvarsi-tesco-bridge.<účet>.workers.dev
UVARSI_TESCO_BRIDGE_RELEASE=<release-sha>
UVARSI_TESCO_BRIDGE_SECRET=<rovnaký-BRIDGE_SECRET-ako-vo-Workeri>
PLATBY_ZAPNUTE=0
```

URL musí byť iba HTTPS origin bez cesty, portu, query, fragmentu alebo
prihlasovacích údajov. Jeho host sa musí presne zhodovať so zamknutým
`UVARSI_TESCO_BRIDGE_WORKER_HOST`, začínať `uvarsi-tesco-bridge.` a končiť
`.workers.dev`. `UVARSI_TESCO_BRIDGE_RELEASE` sa musí presne zhodovať s prefixom
release-bound bridge secretu. Súbor nečítaj cez `cat`, nekopíruj ho z PC a
nepridávaj ho do Gitu. Po uložení nastav práva a spusti tichý autentifikovaný
preflight:

```text
sudo chmod 600 /opt/uvarsi/uvarsi.env
sudo /opt/uvarsi/uvarsi-deploy-state.sh check-bridge
```

Preflight hneď vypne prípadný zdedený shell `xtrace`, potom nič nevypíše pri
úspechu. Pri chybe vráti nenulový kód bez tela odpovede, bearer hlavičky alebo
hodnoty kľúča. Okrem host/release zámku vyžaduje presný oficiálny Tesco
hypermarket `source_url`, slug zhodný s `valid_from`, aktuálnu platnosť a media
URL iba z toho istého Worker originu. Chybu rieš podľa všeobecného stavu
Workeru a DNS; do logu nekopíruj autentifikovanú odpoveď.

### 3. Štvorhodinová poistka dozorcu

`nasad.ps1` aj samopull tento riadok skutočne nainštalujú a overia. Odstránia
iba aktívny priamy Uvar.si riadok s `dozorca.sh` alebo starú kópiu wrappera;
ostatné záznamy vrátane Taktik-mapa zachovajú. Pred zmenou si odložia iba
dotknuté Uvar.si riadky a pri rollbacku ich obnovia bez prepisu zvyšku
crontabu:

```text
0 5-21 * * * /opt/uvarsi/uvarsi-deploy-state.sh run-supervisor >> /var/log/uvarsi.log 2>&1
```

Rovnaký vstup používa samopull ešte pred označením release za úspešný. Pred
spustením znovu overí platby OFF, bridge aj presný rozvrh. TERM odošle po
14 100 sekundách a po najviac 300 sekundách čakania pošle KILL, takže absolútny
strop procesu zberu a zostavenia bločku je 14 400 sekúnd, nie 14 700. Timeout
neobnovuje databázu zo zálohy: kandidát žije v stagingu, takže aktívne ceny a
`landing_data.json` zostanú poslednou overenou verziou a nové auth, zákaznícke,
špajzové, plánové či platobné riadky sa nestratia.

### 4. Povinný smoke a rollback

Pred živou zmenou musí prejsť `check-bridge`. Po reštarte deploy najprv so
stále vypnutými platbami synchronne spustí `run-supervisor` a až potom vyhodnotí
striktnú pripravenosť. Prvý rollout preto môže nahradiť staré alebo
agregátorové dáta oficiálnym stagingom namiesto toho, aby sa na nich zacyklil.
Po ohraničenom behu musí prejsť:

```text
sudo /opt/uvarsi/uvarsi-deploy-state.sh check-readiness
```

Brána je tichá a fail-closed. Overí platby OFF v súbore aj procese, dostupný
zamknutý bridge, aktívnu appku a worker s čerstvým heartbeat, presne jeden
bezpečný cron riadok a čerstvú značku úspešného ohraničeného behu. Ďalej
vyžaduje Kaufland/Tesco/Lidl po najmenej 20 aktuálnych ponúk z presne povolených
oficiálnych source kinds, reálne oficiálne URL v každom aktívnom aj staging
riadku, zhodné aktívne a staging dáta/fingerprinty a HTTP úspech Taktik-mapa.
Aktuálny trojjedlový bloček musí mať v každom jedle položku, kladné rozumné ceny
a súčty, presnú aritmetiku, aktuálne `valid_from`/`valid_to`, auditovateľné
source URL/strany a `offer_key` existujúce v aktívnych cenách. Nezmenený staging
fingerprint sa pri opakovaní znovu použije pred importom Anthropic klienta,
takže nevznikne ďalšie platené volanie ani rozpočtová rezervácia.

Ak niektorá brána zlyhá, release sa nesmie označiť za úspešný. Automatický
rollback vracia iba kód, statické Uvar.si súbory, jednotky a predchádzajúce
Uvar.si supervisor cron riadky. Nevracia `uvarsi.db`, `landing_data.json` ani
žiadne používateľské dáta a nedotýka sa Caddy, Taktik-mapa ani cudzích cron
riadkov. Ak zber alebo prísna brána zlyhá, release sa neoznačí za úspešný a
platby ostanú vypnuté.
