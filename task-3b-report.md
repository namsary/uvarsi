# Task 3B — last-known-good bloček a Tesco display label

## Výsledok

- Aktuálny plne validný landing payload sa vracia bez zmeny obsahu a s `state=current`.
- Minulotýždňový payload, ktorý bol pri vzniku validný a matematicky konzistentný, sa vracia ako `state=historical_example`.
- Historická ukážka zobrazuje upozornenie „Ukážka z minulého týždňa – ceny už nemusia platiť.“ a nezobrazuje platnosť, zdroje, bežnú cenu, zľavu ani úsporu.
- Poškodený, matematicky neplatný alebo nekompatibilný payload zostáva nedostupný.
- Verejná týždenná stránka vracia pre historickú ukážku HTTP 200 s `noindex`; pri neplatných dátach zostáva HTTP 503.
- Aplikácia historický landing endpoint nepoužíva na tvorbu aktuálnych plánov ani ponúk.
- Tesco zostáva interne aj v databáze `Tesco`. Iba používateľský názov v surovinách a nákupných skupinách sa pri striktne overenej oficiálnej URL mení na `Tesco hypermarket` alebo `Tesco supermarket`.
- Atómový zápis landing dát zostal bez zmeny.
- Landing po zmene naďalej spĺňa existujúci gzip limit; odstránený bol nepoužívaný JavaScript a duplicitný fallback text.

## TDD dôkaz

- Historický payload: prvý test zlyhal na chýbajúcej verejnej klasifikácii, po implementácii prešiel.
- Verejný endpoint: historický snapshot najprv vracal 503; po implementácii vracia bezpečný sanitizovaný príklad.
- Landing UI: historický stav najprv spadol do recovery; po implementácii sa zobrazí bez aktuálnych claimov.
- Tesco display label: po oprave testovacej identity boli 2 oficiálne URL testy červené (`Tesco` namiesto požadovaného labelu) a 4 podvrhnuté URL už zostali bezpečne `Tesco`; po implementácii prešlo všetkých 6 prípadov.

## Overenie

- Spoločná focused sada: `436 passed, 36 skipped, 2 deselected`.
- Dodatočný starší refresh kontrakt + výkonový limit + historický loader: `3 passed`.
- Python syntax pre `landing_data.py`, `server.py`, `plan_data.py`, `public_pages.py`: bez chyby.
- `git diff --check` pre povolené Task 3B súbory: bez chyby.
- Testy nepoužili live sieť, AI ani serverovú mutáciu.

## Súbory Tasku 3B

- `app/landing_data.py`
- `app/server.py`
- `app/plan_data.py`
- `app/public_pages.py`
- `index.html`
- `tests/test_landing_data.py`
- `tests/test_server.py`
- `tests/test_public_pages.py`
- `tests/test_landing_html_contract.py`
- `tests/test_plan_data.py`
- `task-3b-report.md`

`app/static/app.html` nebolo potrebné meniť: nepoužíva verejný landing endpoint a aktuálne plánové dáta číta vlastnou oddelenou cestou.
