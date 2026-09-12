
## 2026-09-12 — Verejný kontakt a 24-mesačná garancia (vydanie 2026.09.12.22)

- Verejný kontakt PUMAR s. r. o. obsahuje potvrdené telefónne číslo +421 917 347 009.
- Zakladajúce Premium za 39 € je garantované na 24 mesiacov od uzavretia zmluvy; potom pokračuje bez ďalšieho poplatku počas ďalšej prevádzky Uvar.si.
- Ak PUMAR s. r. o. z vlastného rozhodnutia ukončí službu skôr, vráti pomernú časť ceny za nevyužité kalendárne dni garantovaného obdobia.
- Rovnaké znenie ponuky je použité na úvodnej stránke, v aplikácii, objednávke aj platobnej bráne. Platby zostávajú vypnuté do úspešného skúšobného nákupu a refundácie.

## 2026-09-12 — Právne spresnenia a bloček bez JavaScriptu (vydanie 2026.09.12.21)

- Lehota na vrátenie platby je pevne oddelená od prípadnej dohody o inom spôsobe vrátenia; nepovinné technológie zostávajú vypnuté, kým používateľ neudelí platný súhlas.
- Aktuálny bloček sa atomicky vloží aj priamo do HTML úvodnej stránky. Je preto viditeľný pri prvom načítaní, bez JavaScriptu aj pre vyhľadávače a odpovedné AI systémy.
- Statická kópia vzniká výhradne z rovnakého overeného JSON-u ako verejné API, obnovuje ju každý úspešný týždenný refresh, hodinový dozorca aj nasadenie a nevolá Anthropic API.
- Po skončení platnosti sa starý bloček automaticky označí ako historická ukážka a prestane tvrdiť úsporu; pri neplatných dátach sa skryje.
- Platby zostávajú vypnuté, kým nebude doplnený a overený telefón, obchodné rozhodnutie o minimálnej dobe služby a platobný smoke test.

## 2026-09-12 — Available receipt from current weekly facts (vydanie 2026.09.12.20)

- Ak starší technický záznam zberača nesedí, bezplatný bloček sa môže obnoviť z už publikovaných ponúk, ktoré majú platný kľúč, dnešnú platnosť, známy zdroj a aspoň 20 položiek z každého obchodu.
- Tematické kampane dlhšie než 21 dní sú aj v tejto ceste vyradené; napríklad mesačné Fínske pečivo sa do týždenného bločka nedostane.
- Dostupnostná cesta neoznačí zber za právne schválený a nikdy neodomkne platby. Prísna platobná brána zostáva nezmenená.

## 2026-09-12 — Collector-count compatible recovery (vydanie 2026.09.12.19)

- Overenie používa rovnakú definíciu počtu ako zberač: unikátne ponuky, nie fyzické databázové riadky. Opakovaná rovnaká akcia na viacerých miestach preto nevyvolá falošný nový zber.
- Pri prekrývajúcich sa týždňoch sa každý blok stále overí samostatne, ale minimálny počet sa posudzuje za celý obchod; platný menší zvyšok staršieho letáka tak nezablokuje bezpečnú obnovu.

## 2026-09-12 — Atomic registered-source recovery (vydanie 2026.09.12.18)

- Overenie aktívnych ponúk a prestavba bločka prebiehajú v jednom databázovom snímku; medzi kontrolou a použitím sa už nemôže vymeniť dátová sada.
- Stav zberu sa musí presne zhodovať s celým surovým blokom podľa obchodu, týždňa, zdroja, počtu a rozsahu platnosti. Dlhšie tematické kampane ostanú auditovateľné v databáze, ale do týždenného bločka nevstúpia.
- Úspešná obnova zo schváleného záložného zdroja obnoví aj heartbeat dozorcu, no prísna brána pre budúce platby ostáva nezávisle zatvorená.

## 2026-09-12 — Immediate free-receipt repair (vydanie 2026.09.12.17)

- Nasadenie rozlišuje použiteľnosť registrovaných dát pre bezplatný bloček od prísnejšieho právneho schválenia potrebného pre platby.
- Ak aktívne týždenné ponuky sú kompletné a zhodujú sa s podpísaným stavom svojho registrovaného zberača, bloček sa prestavia okamžite počas deployu bez AI a bez čakania na hodinový cron.
- Nezhoda zdroja, odtlačku, platnosti alebo počtu naďalej zlyhá bezpečne a spustí pôvodnú cestu doplnenia dát; platby zostávajú vypnuté.

## 2026-09-12 — Registered-source receipt recovery (vydanie 2026.09.12.16)

- Bezplatný ukážkový bloček možno bezpečne prestavať aj zo stále platných ponúk registrovaného záložného čítača, keď sú ceny, platnosť, strana a pôvod každej položky overiteľné.
- Neznámy alebo nepovolený zdroj je naďalej odmietnutý a nemôže prepísať posledný dobrý bloček.
- Toto uvoľnenie neplatí pre platený predaj: právna brána zdrojov a všetky platby zostávajú vypnuté až do samostatného schválenia.

## 2026-09-12 — Recovery-state cleanup (vydanie 2026.09.12.15)

- Úspešná obnova bločka z kompletnej publikovanej databázy odstráni starý príznak neúspešného staging zberu, aby už vyriešený incident neblokoval finálnu kontrolu nasadenia.
- Príznak sa odstraňuje až po zápise a opätovnom overení aktuálneho bločka; neúspešná alebo neúplná obnova ho zachová.
- Platby zostávajú vypnuté a zmena nespúšťa zber ani AI.

## 2026-09-12 — Active weekly receipt recovery (vydanie 2026.09.12.14)

- Neaktuálny verejný bloček sa obnoví priamo z už publikovaných ponúk, keď sú všetky tri obchody kompletné a ich údaje pochádzajú z oficiálnych týždenných zdrojov.
- Oprava bločka nemení rozpracovaný staging, nespúšťa zberač a nepoužíva Anthropic API; z už načítaného týždňa preto nevznikne ďalší kreditový náklad.
- Mesačné tematické kampane sa nezapočítajú ani do bločka, ani do rozhodovania hodinového dozorcu. Položky typu „Fínsky chlieb“ mimo týždenného letáka sa tak nemôžu vrátiť cez vedľajšiu cestu.
- Ak aktívne ponuky nie sú úplné, ostáva zachovaný pôvodný bezpečný postup: dokončiť staging, overiť ho a až potom ho atomicky publikovať.
- Overenie: 134 testov obnovy bločka, dozorcu a nasadzovania prešlo; platby zostávajú vypnuté a Taktik-mapa je mimo nasadenia.

## 2026-09-12 — Per-store offer diagnostics (vydanie 2026.09.12.13)

- Verejný health pridáva iba bezpečné súhrnné počty aktuálnych týždenných ponúk pre Kaufland, Tesco a Lidl; neobsahuje osobné údaje, tajomstvá ani zdrojové odpovede.
- Diagnostika bez SSH ukáže, ktorý konkrétny reťazec po odfiltrovaní mesačných kampaní nespĺňa produkčný prah.

## 2026-09-12 — Same-release deployment gates (vydanie 2026.09.12.12)

- Automatické nasadenie po prepnutí spúšťa dozor aj finálnu kontrolu z práve nasadeného vydania, nie z funkcií predošlej verzie načítaných pri štarte samonasadzovača.
- Zmeny pravidiel akcií sa preto uplatnia na bloček okamžite v tom istom deployi; oprava už nie je o jeden desaťminútový cyklus alebo jedno vydanie pozadu.
- Nasadzovač aj prvý prechodový dozor zabezpečia spustiteľnosť bezpečného cron vstupného bodu, takže hodinová autonómna obnova nezlyhá na právach súboru.
- Finálna kontrola zostáva ohraničená timeoutom, vyžaduje vypnuté platby a nemení Taktik-mapu.

## 2026-09-12 — Weekly receipt readiness alignment (vydanie 2026.09.12.11)

- Produkčná brána používa rovnaké maximálne 21-dňové letákové okno ako aplikácia, dozorca a platobná kontrola.
- Mesačná tematická kampaň Kauflandu už nemôže spôsobiť falošné potvrdenie zdravého týždenného bločka.
- Ak sa pravidlá ponúk po vydaní zmenia, ohraničený dozorca bloček prestavia hneď počas nasadenia z existujúcej databázy, bez nového AI čítania letákov a bez čakania na ďalšiu hodinu.
- Regresný test pokrýva presne incident s mesačným „Fínskym chlebom“ aj autonómnu obnovu bločka bez dostupnosti externého Tesco bridge.
- Platby ostávajú vypnuté a Taktik-mapa je mimo nasadenia.

## 2026-09-12 — Code/data failure isolation (vydanie 2026.09.12.10)

- Nasadenie aplikácie už nie je blokované dočasným výpadkom externého zdroja letáka, pokiaľ sú platby vypnuté a aplikácia, worker aj dozorca sú funkčné.
- Čerstvosť ponúk a bločka ostáva samostatnou prísnou bránou: neúspešný zber sa nikdy nevydáva za úspešný a nemôže povoliť platby.
- Starý nasadzovač dostal jednorazovú procesnú kompatibilitu, aby vedel nainštalovať túto opravu; nový proces už používa oddelené brány natrvalo.
- Pri odloženom zbere ostávajú posledné overené dáta nedotknuté a dozorca pokračuje v autonómnych pokusoch.
- Pokračovanie bez rollbacku je povolené iba pri vopred potvrdenom výpadku externého transportu; interná chyba dozorcu, refreshu alebo runtime ostáva blokujúca.
- Jednorazový prechod neblokuje krehké porovnanie rovnakosekundových heartbeat značiek; finálna brána naďalej vyžaduje živý worker a heartbeat mladší než 60 sekúnd.
- Prechod zo starého produkčného nasadzovača zapisuje do schváleného anonymného kanála iba allowlistovanú fázu deployu, aby sa chyba štartu dala lokalizovať bez SSH a bez úniku runtime hodnôt.
- Ak starý ťažký health endpoint dočasne visí, samopull smie potvrdiť vypnuté platby iba dvojitou kontrolou serverového OFF súboru a dvoch allowlistovaných príznakov priamo v prostredí živého procesu; platný ON signál sa nikdy neprebíja fallbackom.
- Diagnostická stopa rozlišuje aj posledné tri post-deploy brány: bezpečný rozvrh dozorcu, ohraničený beh dozorcu a finálnu produkčnú readiness.
- Starý cron dozorcu sa pri prechode nahradí automaticky a transakčne; mení sa iba presne rozpoznaný riadok Uvar.si, zvyšok zdieľaného crontabu vrátane Taktik mapy zostáva zachovaný.
- Osobné plány používajú iba krátke letákové okná; mesačné tematické kampane z online prehľadu (napríklad proteínový sortiment mimo týždenného letáka) sa vyradia aj zo starej databázy bez nového AI zberu.
- Overenie delty: 266 kritických testov zberu, bločka, dozorcu a nasadzovania prešlo bez chyby; predchádzajúci integrovaný beh mal 3 866 úspešných testov.
- Platby ostávajú vypnuté a Taktik-mapa je mimo nasadenia.

## 2026-09-12 — Published-data recovery (vydanie 2026.09.12.3)

- Oprava koreňa: bezpečnostná brána posudzuje atomicky publikované ponuky, nie nedokončenú staging kópiu po zlyhanom zbere.
- Dôsledok: 849 aktuálnych oficiálnych ponúk sa dá znovu použiť na obnovu bločka bez ďalšieho čítania letákov a bez ďalšieho AI nákladu.
- Dozorca: neúplný staging už nespúšťa zber, keď sú aktívne ponuky všetkých troch obchodov aktuálne; opraví iba bloček z publikovanej DB.
- Diagnostika: zlyhanie Tesco bridge používa iba bezpečné dôvody `config_invalid`, `request_failed`, `response_invalid` alebo `local_error`; kľúče, URL a odpovede sa nezapisujú.
- Bezpečnosť: zber naďalej publikuje nové dáta až po všetkých kontrolách; platby ostávajú vypnuté a Taktik-mapa je mimo nasadenia.
- Testy: 159 testov deploy/dozorca a 409 testov celého dátového okruhu prešlo bez chyby.

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

## 2026-09-03 — Pravdivé rozlíšenie Free a Premium (vydanie 2026.09.03.4)

- Free vytvorí plnohodnotný jedálniček na celý týždeň z jedného obchodu podľa výberu; Premium môže porovnávať viac podporovaných obchodov.
- Rozsah oprávnenia vynucuje server pri profile, generovaní, načítaní aj predvýpočte plánu; nemožno ho obísť upravenou požiadavkou z prehliadača.
- Voľba obchodov používa prístupné tlačidlá: vo Free sa správa ako výber jedného obchodu, v Premium ako výber viacerých.
- Lidl sa načítava priamo z oficiálneho aktuálneho letáka: endpoint poskytne presnú platnosť a úplný manifest všetkých strán; Kupino a mLetáky zostávajú iba ako zálohy.
- Platený AI predvýpočet sa pri lokálnom generátore vôbec nezaraďuje a worker pred prípadným volaním znovu overí aktuálny profil aj oprávnenie.
- Overenie: celý balík 2904 testov prešiel; 10 testov bolo podmienene preskočených.
- Platby zostávajú vypnuté.

## 2026-09-03 — Schémou chránený verejný bloček (vydanie 2026.09.03.5)

- Anthropic musí pri skladaní ukážkového bločka vrátiť kladný celý počet balení; desatinná hodnota sa už nemôže dostať z modelu do validačnej vrstvy.
- Pri chybe naďalej ostáva posledný platný bloček nedotknutý.
- Overenie: 48 dotknutých testov prešlo. Platby zostávajú vypnuté.

## 2026-09-03 — Kompatibilná schéma bločka (vydanie 2026.09.03.6)

- Výstupná schéma používa Anthropicom podporovaný typ `integer`; kladnosť hodnoty naďalej povinne kontroluje server pred publikovaním.
- Neplatný výstup nemôže prepísať posledný platný bloček. Platby zostávajú vypnuté.

## 2026-09-03 — Poistka proti neúplnému letáku (vydanie 2026.09.03.7)

- Zber ani dozorca už neoznačia obchod s 14 akciami za kompletný; minimum je 20 overených potravinových ponúk na obchod.
- Aktuálny oficiálny Lidl leták poskytol 105 strán, z ktorých bolo 67 potravinových, a zber uložil 362 akcií.
- Platby zostávajú vypnuté.
