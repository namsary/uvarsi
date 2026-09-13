# Task 7 — subscription reconciliation, dunning a privacy-safe alerts

Dátum: 2026-09-13

Base: `34200ce7444de72074959045ad4d050d57a2096f`

Scope: iba Task 7; bez pushu, deployu, full suite a živých sieťových/API/Anthropic volaní.

## Výsledok

- Pridané `reconcile_subscriptions(con, *, provider_rows, invoice_rows, now, expected) -> dict`.
- Provider subscription snapshoty aj faktúry prechádzajú cez `predplatne.process_subscription_event()`, teda cez rovnakú validačnú, idempotentnú a karanténnu cestu ako webhooky.
- Oba provider zoznamy sa musia úspešne načítať ešte pred prvou databázovou zmenou. Výpadok providera preto zachová posledný lokálne overený prístup.
- Zmeškaná zaplatená obnova sa importuje práve raz; opakovaný beh ju rozpozná ako duplikát.
- `past_due` zachová prístup, potvrdené `unpaid` ho pozastaví, zaplatená obnova ho vráti a `expired` ho odoberie.
- Staršia provider revízia neprepíše novší overený stav.
- Health obsahuje iba agregáty `subscription_drift`, `past_due`, `unpaid`, `expired` a `queued_webhooks`.
- Alerty povoľujú iba agregované počty, allowlist bezpečných kódov a striktne validovaný ISO dátum. Provider payload, email, URL, token ani ID zákazníka sa do verejného alertu nedostanú.
- Bežné načítanie stránky ani tvorba plánu providera nevolajú. Reconciliation ostáva samostatný serverový job.
- Legacy jednorazové platby a rollout flagy ostali nezmenené; payment flagy boli pri overení nenastavené.

## RED dôkazy

1. Prvý Task 7 kontrakt: `10 failed` — chýbalo rozhranie, provider failure semantika, health počítadlá a bezpečný alert.
2. All-or-nothing provider fetch: `1 failed` — chýbala API orchestration vrstva.
3. Hardening proti reálnemu Lemon API tvaru a úniku údajov: `5 failed` — obnova bez vymyslených invoice polí, pending dunning, recovery counter, drift pri neplatnom riadku a nevalidovaný text dátumu.

Každý RED rez zlyhal na očakávanom chýbajúcom správaní pred produkčnou úpravou.

## GREEN dôkazy

- Hardening rez: `5 passed`.
- Focused Task 7 + legacy reconciliation + notification privacy: `74 passed`.
- Relevantný payment/subscription regresný rez: `396 passed`.
- Relevantný auth/deploy/privacy regresný rez: `333 passed`.
- Python syntax kontrola piatich relevantných modulov/testu: PASS.
- `git diff --check`: PASS; iba informatívne LF/CRLF upozornenia Git pre Windows.

Spolu bolo vo finálnych balíkoch vykonaných `803` úspešných testov. Jeden privacy súbor je zámerne zahrnutý vo focused aj auth/deploy reze; číslo preto vyjadruje počet testovacích vykonaní, nie počet unikátnych testov.

## Bezpečný predpoklad pri neúplných provider dátach

Lemon subscription invoice neopakuje všetky údaje subscription objektu. Reconciliation preto spája iba overiteľné fakty z faktúry, zodpovedajúceho subscription snapshotu a posledného lokálne overeného stavu. Ročné obdobie zaplatenej obnovy smie vzniknúť len ako súvislé pokračovanie od `paid_through` po overené `renews_at`. Ak hranicu, identitu, sumu alebo revíziu nemožno bezpečne dokázať, udalosť ide do review a prístup sa nemení.

Overený verejný tvar API:

- https://docs.lemonsqueezy.com/api/subscriptions/the-subscription-object
- https://docs.lemonsqueezy.com/api/subscription-invoices/the-subscription-invoice-object

## Zmenené súbory

- `app/rekonciliacia.py`
- `app/predplatne.py`
- `app/platby.py`
- `tests/test_platby_rekonciliacia.py`
- `tests/test_subscription_reconciliation.py`
- `task-7-report.md`

`app/server.py` nevyžadoval zmenu: existujúci health endpoint už volá `platby.stav_dozoru()`, do ktorého sa nové agregované počítadlá doplnili.

## Zvyšné riziká

- Živý Lemon API kontrakt a sieťové chyby neboli zámerne skúšané; testy používajú iba lokálne provider fixtures.
- Ak provider nevráti zodpovedajúci subscription snapshot alebo dôveryhodný renewal boundary, obnova sa fail-closed odloží na kontrolu a nárok sa automaticky nezmení.
- Ntfy odoslanie je best-effort. Autoritatívny stav zostáva v lokálnych health počítadlách a review udalostiach.
- Testy hlásia jednu existujúcu Starlette/AnyIO deprecation warning; nejde o Task 7 regresiu.
- Full suite nebola spustená podľa zadania.

## Fix round 1 — 2026-09-13

### Uzavreté pripomienky review

1. Celý reconcile je atómový cez vlastný SQLite savepoint. Chyba uprostred dávky vráti všetky predchádzajúce zmeny a navonok vytvorí iba bezpečný agregovaný blocker `reconciliation_batch_failed` bez obsahu výnimky a bez PII.
2. Neplatná prvá alebo neskoršia stránka, neúplná stránka, chýbajúce či nekonzistentné pagination metadata a slučka stránok končia ako `ProviderUnavailable`. Pred úspešným stiahnutím celej dávky nevznikne žiadna databázová mutácia ani zdravá nula.
3. Pred normalizáciou sa vyžaduje presný typ `subscriptions`, respektíve `subscription-invoices`. Konfliktný typ je fail-closed a nemení nárok.
4. Health a drift počítadlá sú oddelené podľa `expected.test_mode`; produkčný health nepočíta testovacie subscription dáta.
5. Iba presne chýbajúce migračné tabuľky majú schema fallback. Iná SQLite `OperationalError` nastaví `subscription_health_available=false`, bezpečný kód `subscription_health_db_error` a blocker bez textu databázovej chyby.
6. Päťstranový/500-záznamový limit bol odstránený. Downloader používa dokumentovanú Lemon stránkovú navigáciu `page[number]`/`page[size]`, overí úplnosť a konzistenciu každej stránky a pokračuje až po deklarovanú poslednú stranu. Ochranný limit 10 000 strán nikdy nevráti skrátený úspech — pri jeho prekročení zlyhá uzavreto.

Lemon dokumentuje stránkovanie, odkazy `first`/`last`/`next`/`prev` a `meta.page`; nedokumentuje bezpečný updated cursor pre tieto list endpointy, preto implementácia nevymýšľa neexistujúci kontrakt:

- https://docs.lemonsqueezy.com/api/getting-started/requests
- https://docs.lemonsqueezy.com/api/subscriptions/list-all-subscriptions
- https://docs.lemonsqueezy.com/api/subscription-invoices/list-all-subscription-invoices

### RED dôkazy fix round 1

- Atómový rollback: `1 failed` — druhá mutácia vyhodila chybu a prvá zostala zapísaná.
- Pagination hardening a pokrytie nad 500 záznamov: `7 failed` — pôvodný limit skracoval výsledok a neplatné stránky neboli odmietnuté.
- Presné provider resource typy: `2 failed` — konfliktné riadky sa pred opravou dali pretypovať.
- Oddelenie test/live health: `1 failed` — health nemal parameter režimu.
- Nedostupný subscription health: `3 failed` — databázová chyba sa mohla tváriť ako zdravé nulové počítadlá.

### GREEN dôkazy fix round 1

- Focused reconciliation, pagination a notification privacy: `86 passed`.
- Payment/subscription regresný rez: `396 passed`.
- Auth/deploy/privacy regresný rez: `333 passed`.
- Python syntax kontrola relevantných modulov a testov: PASS.
- `git diff --check`: PASS; iba informatívne LF/CRLF upozornenia na Windows.
- `PLATBY_ZAPNUTE`: UNSET; `UVARSI_SUBSCRIPTION_CHECKOUTS`: UNSET.

Finálne bolo vykonaných `815` úspešných testov. Privacy test je zámerne v dvoch rezoch, takže ide o počet vykonaní, nie unikátnych testov.

### Zvyšné riziká po fix round 1

- Bez živých Lemon volaní podľa zadania; reálny provider kontrakt je overený lokálnymi fixtures podľa oficiálnej dokumentácie.
- Úplný provider scan má cenu úmernú počtu záznamov. Nekonzistentná alebo extrémna pagination zlyhá uzavreto a zachová posledný overený nárok.
- Surovú odloženú webhook požiadavku nemožno pred overením podpisu bezpečne zaradiť do test/live režimu. Zostáva preto iba v samostatnom agregáte `cakajucich_tiel`, nie v režimovom `queued_webhooks`.
- Ntfy ostáva best-effort; autoritatívny blocker je lokálny health stav.
- Jedna existujúca Starlette/AnyIO deprecation warning nie je regresia Task 7.
- Full suite nebola spustená podľa zadania.
