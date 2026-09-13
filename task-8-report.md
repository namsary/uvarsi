# Task 8 — ročné Premium podmienky

Dátum: 2026-09-13

Base: `ffe0085689f3a93a38bb68d820532bd019ec4642`

Scope: iba Task 8; bez pushu, deployu, živého checkoutu, provider/network a AI volaní. Platobné rollout flagy zostali OFF.

## Výsledok

- Jednotný zákaznícky sľub: prvých najviac 50 úspešných initial zákazníkov 39 € za prvý rok, potom 49 € ročne; ďalší zákazníci 49 € ročne od začiatku.
- Predplatné sa automaticky obnovuje do zrušenia. Zrušenie je možné kedykoľvek a Premium zostáva aktívne do konca zaplateného obdobia.
- Bez trialu a mesačného plánu. Odstúpenie, primerané krátenie a samostatné nároky pri vade, duplicite alebo neoprávnenej platbe sú rozlíšené.
- Checkout pred CTA uvádza cenu, obnovu, spôsob zrušenia a okamžité plnenie. CTA je `Objednať Premium s povinnosťou platby`.
- Lemon checkout používa `product_options` pre návrat do aplikácie a text potvrdenia; nepoužíva statickú podpísanú URL.
- `LEGAL_VERSION` je `2026-09-12-v5` a údaje prevádzkovateľa sú PUMAR s. r. o., +421 917 347 009, pumaragency@gmail.com.
- Zákaznícke plochy už neobsahujú staré tvrdenia `39 € raz`, `24 mesiacov` ani `bez automatickej obnovy`.

## TDD a focused testy

- RED: cross-surface kontrakty najprv zlyhali na chýbajúcom ročnom sľube, starej právnej verzii a jednorazovej cenovej komunikácii.
- Public/legal/frontend/customer/payment-readiness rez: `343 passed`, 1 existujúce deprecation warning.
- Payment/auth/deploy regresný rez: `691 passed`, pričom 5 parametrizovaných prípadov odhalilo zastarané očakávanie testu pri novej povinnej checkout hláške.
- Opravená päťprípadová consent matica: `5 passed`.
- Finálny celý `tests/test_platby.py`: `111 passed`, 1 existujúce deprecation warning.
- Aktuálne failing testy: žiadne.
- `git diff --check`: PASS; iba informatívne LF/CRLF upozornenia Git na Windows.

## Gzip a integrita assetu

Merané cez gzip level 5, limity neboli zvýšené:

| Asset | Veľkosť | Limit | Rezerva |
| --- | ---: | ---: | ---: |
| `index.html` | 12 091 B | 12 100 B | 9 B |
| `app/static/app.html` | 37 083 B | 37 600 B | 517 B |
| `app/static/subscription-profile.6805efa9d12e.js` | 4 467 B | 4 500 B | 33 B |

- SHA-256: `6805efa9d12e98396ca36f64c108ad07f7f4a6b7d2fb3dc5e1cb2ed1b2586a02`
- SRI: `sha384-hXw53ZXb8fvlIUhqa1+5R//NRYPyIcXXaAi8BPcqecc7wsVO4Eg2R0lsukHe+CRd`
- Hashovaný názov je zosúladený v aplikácii, deploy skripte aj serverovom zozname povinných súborov.

## Zmenené súbory

- `app/operator_profile.py`
- `app/legal_pages.py`
- `app/payment_readiness.py`
- `app/platby.py`
- `app/server.py`
- `app/static/app.html`
- `app/static/subscription-profile.6805efa9d12e.js` (nahrádza `subscription-profile.50f7b3f42981.js`)
- `index.html`
- `nasad.ps1`
- `hetzner/samopull.sh`
- focused Task 8 kontrakty v `tests/`
- `task-8-report.md`

## Zvyšné riziká

- Živý Lemon checkout a jeho receipt/thank-you odpoveď neboli podľa zadania volané; platby zostávajú OFF do samostatného rollout kroku.
- Landing a lazy Profile asset majú malú gzip rezervu. Limity však zostali nezvýšené a oba kontrakty prešli.
- Full suite nebola podľa zadania spustená; relevantné public/legal/frontend/payment/auth/deploy rezy boli overené.
