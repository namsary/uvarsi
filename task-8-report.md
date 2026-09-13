# Task 8 — ročné Premium podmienky, fix round 1 closeout

Dátum: 2026-09-13

Base HEAD: `bf86f9dd7851928fb520b02ca3e16e855c3c2002`

Scope: iba closeout šiestich review nálezov Task 8. Bez pushu, deployu, živého checkoutu, provider/network a AI volaní. Platobné rollout flagy zostali OFF.

## Uzavreté review nálezy

1. **Cenový handshake 39 → 49 €:** klient posiela nemenný identifikátor a sumu práve potvrdenej ponuky. Server cenu znovu určí v atómovej rezervácii; pri zmene odpovie `409 price_changed` bez checkout URL. Klient zobrazí nový 49 € súhrn, vymaže súhlasy a vyžiada nové potvrdenie.
2. **Potvrdenie podľa faktúry:** potvrdenie podania sa skladá z typu a sumy konkrétnej vlastnej faktúry. Rozlišuje zakladajúci prvý rok 39 €, bežný prvý rok 49 € a obnovu 49 €.
3. **Pravdivé zrušenie:** profil netvrdí, že samotné odoslanie formulára zrušilo obnovu. Zrušenie sa dokončuje cez Customer Portal alebo podporu a za ukončené sa považuje až po potvrdení.
4. **Konkrétna vlastná ročná faktúra:** profil zobrazuje bezpečný zoznam vlastných ročných faktúr; odstúpenie vyžaduje výber počiatočnej faktúry a reklamácia umožňuje výber ľubovoľnej vlastnej faktúry. Backend už žiadnu faktúru potichu nevyberá a vlastníctvo overuje serverovo.
5. **Lemon účtenka:** checkout payload obsahuje `receipt_link_url: https://uvar.si/app` spolu s textom tlačidla.
6. **Aktivačná veta:** checkout aj účtenka používajú prirodzenú vetu „Premium aktivujeme, keď nám Lemon Squeezy potvrdí platbu.“

## Testy

Všetky behy boli lokálne, bez siete a bez provider/AI volaní. Full suite nebola podľa zadania spustená.

| Rez | Výsledok |
| --- | ---: |
| Checkout, withdrawal, frontend, public, legal a payment-readiness | 369 passed |
| Platobné a subscription regresie | 329 passed |
| Auth regresie | 250 passed |
| Deploy a výkonové regresie | 136 passed |
| Finálny kombinovaný rez bez duplicitných súborov | 1 061 passed |

Samostatné rezy predstavovali 1 084 vykonaní a čiastočne sa prekrývali. Po aktualizácii reportu prešiel aj finálny kombinovaný rez 1 061 testov nad unikátnym zoznamom súborov. Zlyhania: 0. Focused, payment, auth aj finálny rez hlásili iba existujúce `anyio` deprecation warning.

## Gzip a integrita assetu

Merané cez gzip level 5; žiadny budget nebol zvýšený:

| Asset | Veľkosť | Limit | Rezerva |
| --- | ---: | ---: | ---: |
| `index.html` | 12 091 B | 12 100 B | 9 B |
| `app/static/app.html` | 37 425 B | 37 600 B | 175 B |
| `app/static/subscription-profile.19ddd6feb9d0.js` | 4 401 B | 4 500 B | 99 B |

- SHA-256: `19ddd6feb9d0b92e440a0e8db793cf75e0176389c80db5bb917c60f2c8652e5c`
- SRI: `sha384-DSgloEnJ5TtADKx65+HpPDanSeDa0yy5ZfEbk1jHD5i7eMnFr54ytHjqPkSno6Lt`
- Obsahový prefix `19ddd6feb9d0`, SRI, `app.html`, `nasad.ps1`, `hetzner/samopull.sh` a required-file referencie sú zosúladené.
- Starý `subscription-profile.6805efa9d12e.js` bol odstránený iba ako súčasť správneho obsahového rename; aktívnych odkazov naň zostalo 0.

## Zmenené fix súbory

- `app/customer_requests.py`
- `app/platby.py`
- `app/server.py`
- `app/static/app.html`
- `app/static/subscription-profile.6805efa9d12e.js` → `app/static/subscription-profile.19ddd6feb9d0.js`
- `hetzner/samopull.sh`
- `nasad.ps1`
- `tests/test_app_html_contract.py`
- `tests/test_checkout_consent.py`
- `tests/test_platby.py`
- `tests/test_premium_frontend_contract.py`
- `tests/test_subscription_checkout.py`
- `tests/test_subscription_withdrawal.py`
- `task-8-report.md`

## Zvyšné riziká

- Živý Lemon checkout ani skutočné e-mailové potvrdenie neboli podľa zadania volané; overený je lokálny payload a správanie aplikácie.
- Landing má 9 B a aplikácia 175 B gzip rezervu, preto ďalší rast vyžaduje zmenšenie obsahu, nie zvýšenie limitu.
- Platby zostávajú OFF a pred samostatným ostrým rolloutom je stále potrebný test-mode lifecycle smoke a výslovné schválenie vlastníkom.
