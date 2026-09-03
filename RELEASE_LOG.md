
## 2026-08-21 00:12 — BLOCKED (vydanie 2026.08.18.1)
  OK  VERSION: 2026.08.18.1
  OK  testy: 260 presly, 0 zlyhalo
  !!  git revizia: ?
  !!  nezapisane zmeny: 1 suborov nie je commitnutych
  OK  /api/health: {"vydanie": "2026.08.18.1", "tyzden": "2026-08-17", "pocet": 431}
  OK  verzia na webe: 2026.08.18.1 (ocakavam 2026.08.18.1)
  OK  tyzden dat: 2026-08-17 (aktualny pondelok 2026-08-17)
  OK  pocet ponuk: 431 (prah 30)
  OK  landing: HTTP 200
  OK  appka: HTTP 200
  !!  landing JSON: HTTP 503
  OK  prihlasovacia stranka: HTTP 200

## 2026-08-26 — SEO GEO release gate update (vydanie 2026.08.25.2)

- Scope: release gate now blocks on robots, sitemap, public SEO pages, weekly freshness signal, private-route `noindex`, immutable font cache, `www` canonical redirect, and homepage canonical/JSON-LD/internal-link regressions.
- Test evidence: `C:\Users\Ucet\AppData\Local\Programs\Python\Python312\python.exe -m pytest tests/test_release_gate_seo.py -q` -> `5 passed in 0.27s`; `C:\Users\Ucet\AppData\Local\Programs\Python\Python312\python.exe -m pytest -q` -> `1002 passed, 47 skipped in 94.95s (0:01:34)`.
- Payment isolation: release-gate work only; `app/platby.py` and payment runtime behavior stay untouched.
- Rollback note: revert commit `chore: gate SEO GEO release`, restore the prior `VERSION` and rerun the local suite before any future deploy attempt.
- Production status: live deploy and production verification are still pending explicit authorization.

## 2026-08-27 — Final integrated SEO release gap closure

- Scope: close the full-size homepage gate, evergreen content, publishable-evidence boundary, samopull root-asset rollback, and all alternate-host redirect gaps.
- Focused evidence: public pages/routes/auth `201 passed, 24 skipped in 29.94s`; samopull/deploy contracts `74 passed, 6 skipped in 1.46s`; release gate `14 passed in 0.53s`.
- Full-suite evidence: `C:\Users\Ucet\AppData\Local\Programs\Python\Python312\python.exe -m pytest -q` -> `1024 passed, 47 skipped in 78.75s (0:01:18)`.
- Safety: no deploy, SSH, push, merge, payment change, Caddy change, cron change, environment change, or other-app change was performed in this wave.
- Rollback note: revert commit `fix: close integrated SEO release gaps` and rerun the complete local suite before any future deploy attempt.
- Production status: deployment and live production verification remain pending explicit authorization.

## 2026-08-27 — Autonomous plan-cache recovery (vydanie 2026.08.27.1)

- Scope: retry plan precomputation on every hourly supervisor run after complete Kaufland, Tesco and Lidl data; refresh stale public receipt before warming plans.
- Cost safety: weekly precompute budget remains 0.40 EUR; six run slots allow recovery after transient failures without increasing the euro ceiling.
- Concurrency safety: a process-wide `flock` prevents overlapping supervisors from paying for the same missing cache twice; deployment verifies the dependency.
- Runtime coverage: current data, low offer count, missing store, refresh-before-warm ordering, occupied lock, hot-cache idempotence and success-failure-recovery.
- Full-suite evidence: `1033 passed, 47 skipped in 73.90s (0:01:13)`.
- Payment isolation: payment enablement and payment runtime behavior remain untouched.
- Production status: integration, deployment and live verification are pending the guarded release steps.

## 2026-08-27 14:15 — BLOCKED (vydanie 2026.08.27.1)
  OK  VERSION: 2026.08.27.1
  OK  testy: 999 presly, 0 zlyhalo
  !!  git revizia: ?
  !!  nezapisane zmeny: 7 suborov nie je commitnutych
  OK  /api/health: {"vydanie": "2026.08.25.1", "tyzden": "2026-08-24", "pocet": 0, "naklady": {"den": "2026-08-27", "mesiac": "2026-08", "tyzden": "2026-08-24", "chyba": null, "kredit": {"vycerpany": true, "od": "2026-08-26T05:00:04", "sprava": "API odmieta všetky volania — na účte je nulový kredit. Appka nevie generovať jedálničky ani landing bloček, kým kredit nedobiješ. Nič sa neúčtovalo (odmietnuté volania nespotrebovali ani token) a opakované pokusy sú zastavené, aby log nezaplavili."}, "dnes_eur": 0.0, "mesiac_eur": 0.66, "denny_strop_eur": 4.0, "mesacny_strop_eur": 25.0, "zostatok_dnes_eur": 4.0, "zostatok_mesiac_eur": 24.34, "behy": {"zber_letakov": {"tyzden": "2026-08-24", "pocet": 2, "limit": 2}, "predpocet": {"tyzden": "2026-08-24", "pocet": 0, "limit": 2}}, "posledne": [{"cas": "2026-08-24T10:02:55", "ucel": "blocek", "model": "claude-sonnet-5", "eur": 0.02, "odhad": true}, {"cas": "2026-08-24T10:02:53", "ucel": "zber_letakov", "model": "claude-haiku-4-5", "eur": 0.1, "odhad": true}, {"cas": "2026-08-24T10:01:35", "ucel": "zber_letakov", "model": "claude-haiku-4-5", "eur": 0.1, "odhad": true}, {"cas": "2026-08-24T10:01:09", "ucel": "zber_letakov", "model": "claude-haiku-4-5", "eur": 0.1, "odhad": true}, {"cas": "2026-08-24T09:01:41", "ucel": "blocek", "model": "claude-sonnet-5", "eur": 0.02, "odhad": true}]}, "predpocet": {"tyzden": "2026-08-24", "zapnuty": true, "profilov": 9, "cena_za_profil_eur": 0.03, "odhad_plneho_behu_eur": 0.27, "zahriatych": 0, "preskocenych": 0, "zlyhanych": 0, "eur": 0.0, "skutocna_cena_za_profil_eur": null, "usetrenych_generovani": 0, "hotovych_planov": 0, "posledny_beh": null, "dovod": null, "vysvetlenie": null, "chyba": null}, "platby": {"obsadene": 0, "kapacita": 250, "cakajucich_tiel": 0, "nevybavene_vratky": 0}}
  !!  verzia na webe: 2026.08.25.1 (ocakavam 2026.08.27.1)
  OK  tyzden dat: 2026-08-24 (aktualny pondelok 2026-08-24)
  !!  pocet ponuk: 0 (prah 30)
  OK  landing: HTTP 200
  OK  appka: HTTP 200
  !!  landing JSON: HTTP 503
  OK  prihlasovacia stranka: HTTP 200
  !!  robots.txt: HTTP 404
  !!  robots.txt OAI-SearchBot: HTTP 404
  !!  robots.txt blokuje /api/: HTTP 404
  !!  robots.txt sitemap: HTTP 404
  !!  sitemap.xml: HTTP 404
  !!  sitemap.xml XML: HTTP 404
  !!  sitemap obsahuje https://uvar.si/: HTTP 404
  !!  sitemap obsahuje https://uvar.si/co-varit-tento-tyzden: HTTP 404
  !!  sitemap obsahuje https://uvar.si/lacny-jedalnicek: HTTP 404
  !!  sitemap obsahuje https://uvar.si/ako-varime-z-akcii: HTTP 404
  !!  SEO /co-varit-tento-tyzden: HTTP 404
  !!  SEO /lacny-jedalnicek: HTTP 404
  !!  SEO /ako-varime-z-akcii: HTTP 404
  !!  týždenný SEO signál: HTTP 404
  !!  /app noindex: hlavička X-Robots-Tag chýba
  !!  /prihlasenie noindex: hlavička X-Robots-Tag chýba
  !!  font immutable cache: Cache-Control chýba
  !!  www.uvar.si weekly redirect: HTTP 404, Location ?
  !!  uvarsi.sk weekly redirect: HTTP 404, Location ?
  !!  www.uvarsi.sk weekly redirect: HTTP 404, Location ?
  !!  uvarsi.89.167.72.159.sslip.io weekly redirect: HTTP 404, Location ?
  OK  landing canonical: https://uvar.si/
  !!  landing JSON-LD: script type=application/ld+json chýba
  !!  landing interné odkazy: chýbajú /co-varit-tento-tyzden, /lacny-jedalnicek, /ako-varime-z-akcii
## 2026-08-29 — Moderný landing Uvar.si (vydanie 2026.08.29.16)

- Landing dostal nový responzívny grocery-tech vizuálny systém, jasné CTA do fungujúcej appky a ľahkú animáciu bločka.
- Nefunkčné verejné odkazy na agregátory letákov boli odstránené; interná kontrola zdrojov a platnosti dát zostala zachovaná.
- Týždenná úspora aj ročná projekcia pochádzajú z aktuálneho bločka. Landing zobrazuje výpočet týždenná úspora × 52 a upozornenie, že nejde o garanciu.
- Cenník rozlišuje fungujúci Free plán a zakladajúcu ponuku 39 € jednorazovo; platby zostávajú vypnuté.
- Overenie: 1248 testov prešlo, 44 bolo podmienene preskočených; mobilný, tabletový a desktopový viewport bez horizontálneho pretekania.
## 2026-09-03 — Štvrtkový eurový cyklus zberu (vydanie 2026.09.03.3)

- Aj eurový strop zberu letákov sa obnovuje vo štvrtok; spotreba predošlého letáka už neblokuje nový leták v tom istom ISO týždni.
- Denný a mesačný ochranný strop ostávajú nezmenené a platia naďalej.
- Overenie: 82 testov nákladov a kreditných poistiek prešlo. Platby zostávajú vypnuté.

## 2026-09-03 — Spoľahlivý JSON z AI čítania (vydanie 2026.09.03.2)

- Sken strán aj extrakcia akcií používajú schémou vynútený JSON výstup Anthropic API.
- Starší SDK fallback prijme platný JSON aj v Markdown obale; neúplný alebo vecne chybný obsah naďalej odmietne.
- Bezpečný detail chyby sa uloží k stavu obchodu, aby ďalší výpadok nebol anonymný.
- Overenie: 91 dotknutých testov prešlo. Platby zostávajú vypnuté.

## 2026-09-03 — Obnova štvrtkových letákov (vydanie 2026.09.03.1)

- Zberný limit sa obnovuje so štvrtkovým cyklom letákov, nie až v pondelok.
- Dozorca posudzuje dnešnú platnosť ponúk a opakuje iba obchod, ktorému chýba použiteľný leták.
- Menej než 10 overených ponúk z jedného obchodu sa nepovažuje za úspešný zber a neprepíše zdravé dáta.
- Overenie: 233 dotknutých testov a celý balík 2882 testov prešli; 10 testov bolo podmienene preskočených.
- Platby zostávajú vypnuté; nasadenie nemení Caddy ani aplikáciu Taktik.
