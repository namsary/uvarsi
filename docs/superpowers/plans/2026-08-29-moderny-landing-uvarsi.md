# Moderný landing Uvar.si — implementačný plán

**Cieľ:** Prebudovať landing na modernú, mobilnú a konverznú vstupnú stránku bez zmeny zdroja letákových dát alebo fungovania appky.

## Súbory

- `index.html`: vizuálny systém, obsah, CTA, bloček, modelový príklad, cenník, FAQ a animácia.
- `tests/test_landing_html_contract.py`: verejný dôkaz aktuálnosti bez nefunkčných externých odkazov.
- `tests/test_landing_model_section.py`: transparentná týždenná a ročná projekcia.
- `tests/test_landing_visual_contract.py`: nové dizajnové, mobilné, prístupnostné a obsahové kontrakty.
- `tests/test_frontend_speed_contract.py`: výkonový strop zvýšiť iba vtedy, ak ho nový landing preukázateľne potrebuje.

## Postup

1. Napísať kontraktové testy, ktoré vyžadujú:
   - hlavné CTA do `/app`,
   - odstránenie starých textov „ešte nie sme live“ a „spúšťame po mestách“,
   - žiadne klikateľné odkazy na externé agregátory letákov,
   - aktuálny týždeň a obchody ako dôkaz pri bločku,
   - ročný prepočet ako podmienenú projekciu s vysvetlením `týždenná úspora × 52`,
   - jednu výkonnú animáciu cez `transform`/`opacity` a statickú verziu pri `prefers-reduced-motion`,
   - dotykové ciele aspoň 44 px a responzívny hero.
2. Spustiť nové testy a potvrdiť, že zlyhajú na starom landingu.
3. Prepracovať `index.html`:
   - nový svieži grocery-tech systém farieb a povrchov,
   - Manrope pre obsah, Anton iba pre logo a vybrané akcenty, IBM Plex Mono pre ceny a dátumy,
   - hero s jasnou hodnotou, CTA a animovaným bločkom,
   - stručný trojkrok, aktuálny modelový príklad, cenník a zredukované FAQ,
   - odstrániť opakovaný problémový a feature balast,
   - zachovať všetky dátové poistky a dynamické vykreslenie aktuálneho bločku.
4. Spustiť cielené testy, potom celý test suite.
5. Otestovať živý vzhľad lokálne na 390 px, 768 px a 1440 px; skontrolovať overflow, focus, CTA, modal a reduced motion.
6. Commit, push na `origin/main`, počkať na automatické nasadenie a overiť produkciu na desktop aj mobile.

## Remote na mobile

Samostatný diagnostický prúd. Najprv potvrdiť stav Codex remote-control websocketu a poslednú chybu. Uvar.si ani Hetzner sa kvôli tomu nemenia. Reštart Codex Desktop spraviť až po uložení a odovzdaní tejto úlohy, pretože reštart by prerušil aktívnu lokálnu reláciu.
