# Incident: bloček sa v týždni od 14. 9. 2026 neobnovil

## Používateľský prejav

Na úvodnej stránke zostal historický bloček a oznam, že ceny sa obnovujú. Verejný
`/api/health` pritom ukazoval pre aktuálny týždeň nula ponúk, nula pokusov zberu
a nulové náklady. Server aj plánovací worker boli živé a kredit nebol vyčerpaný.

## Príčina

Hodinový cron volal `uvarsi-deploy-state.sh run-supervisor`. Pri neúplnom zbere
vedel jeho prísny bootstrap skončiť ešte pred spustením `dozorca.sh`, napríklad
na kontrole Tesco bridge. Účtovanie pokusov, bezplatná obnova Kauflandu aj ntfy
hlásenie chyby však žijú až v `dozorca.sh`. Výsledkom bolo tiché zlyhanie: bez
ponúk, bez evidovaného pokusu a bez upozornenia.

Druhá medzera bola v kontrole plánovača. Nasadenie overovalo správny riadok v
crontabe, ale nie to, či systémová služba `cron` skutočne beží.

## Oprava

- Bežný hodinový beh vždy vstúpi do `dozorca.sh`; prísny bootstrap zostáva iba
  pre nasadenie.
- Inštalácia a kontrola harmonogramu overí službu `cron` a v prípade potreby ju
  zapne.
- `/api/health` zverejňuje iba bezpečný stav posledného úspešného behu dozorcu:
  čas, vek záznamu a príznak čerstvosti. Nezverejňuje cestu, log ani tajomstvá.
- Platobné prepínače zostávajú vypnuté.

## Overenie po nasadení

1. `dozorca.fresh` musí byť `true` a `age_seconds` sa musí po hodinovom behu
   znovu znížiť bez zásahu z notebooku.
2. `pocet` a `ponuky_podla_obchodu` musia obsahovať aktuálne ponuky alebo musí
   dozorca vytvoriť konkrétne upozornenie o zlyhanom zdroji.
3. Landing môže ponechať posledný overený bloček, ale nesmie predstierať, že je
   aktuálny, ak aktuálny zber nie je kompletný.
4. `payment_readiness.payments_enabled` aj oba serverové platobné prepínače
   zostávajú vypnuté.

## Prevádzkové stopy

- Verejný stav: `https://uvar.si/api/health`
- Zber a dozorca: `/var/log/uvarsi.log`
- Automatické nasadenie: `/var/log/uvarsi-pull.log`
- Úspešný beh dozorcu: `/opt/uvarsi/.supervisor_success_state`

Do dokumentácie ani verejného health endpointu sa nesmú kopírovať API kľúče,
heslá, bridge secret ani obsah serverového env súboru.
