# Uvar.si — rôznorodejšie recepty, tri ceny a pravdivé počítadlo

## Cieľ

Zvýšiť vnímanú kvalitu jedálnička a konverziu cenovej sekcie bez vymysleného
social proofu. Zmena má tri prepojené časti: generovanie plánu, verejné
agregované údaje a landing.

## 1. Rôznorodosť týždenného plánu

Generátor dostane explicitný týždenný brief, nie iba všeobecný štýl. V jednom
pláne musí striedať hlavné suroviny, dominantné prílohy a spôsob prípravy.

- rovnaký hlavný proteín najviac dvakrát za týždeň;
- rovnaká dominantná príloha najviac dvakrát za týždeň;
- najmenej `min(3, počet jedál)` rozpoznateľných spôsobov prípravy;
- podobné jedlá nesmú ísť bezprostredne po sebe;
- pravidlo sa uplatní na 7-, 4- aj 3-jedlový režim;
- ak overené akcie objektívne neposkytujú dosť možností, plán nesmie spadnúť:
  validátor uvoľní iba nedosiahnuteľné pravidlo a ostatné zachová.

Server bude rôznorodosť kontrolovať z vybraných overených položiek a z krokov
receptu. Prompt bude obsahovať rovnaké pravidlá, aby model dostal šancu uspieť
na prvý pokus. Zmena zvýši `PLAN_ALGO_VERSION`, čím sa staré plány bezpečne
zneplatnia. Po nasadení sa spustí jeden kontrolovaný predpočet; ďalšie platené
behy ostanú pod existujúcimi dennými a mesačnými stropmi.

## 2. Cenová sekcia

Landing zobrazí tri porovnateľné karty:

1. **Free — 0 € navždy:** jeden obchod, trojdňový plán, nákupný zoznam.
2. **Zakladajúci — 39 € jednorazovo:** všetky obchody, celý týždeň, recepty,
   špajza a prístup natrvalo; najviac 250 skutočných nárokov.
3. **Premium — 49 € ročne:** štandardná ponuka po skončení zakladajúcej ceny,
   rovnaké jadro Premium funkcií a budúce aktualizácie.

Zakladajúci ostane vizuálne dominantný. Premium bude cenová kotva, nie ďalšie
aktívne tlačidlo na platbu: kým sú platby vypnuté, odkazuje na ten istý
nezáväzný e-mailový formulár a jasne povie „po spustení“.

## 3. Pravdivé počítadlo komunity

Verejný endpoint landingu doplní iba agregované údaje:

```json
{"community":{"accounts":12,"goal":250,"visible":true}}
```

`accounts` je `COUNT(*)` z tabuľky `pouzivatelia`; žiadne e-maily ani iné
osobné údaje sa neposielajú. `goal` je transparentne pomenovaný cieľ testovacej
komunity, nie počet predaných zakladajúcich miest. Landing zobrazí text
„Testovacia komunita: X z cieľa 250 účtov“ až od 10 skutočných účtov. Pod
hranicou zobrazí iba „Prvých 250 získa zakladajúcu cenu“, takže nízke číslo
neznižuje dôveru a zároveň nič nepredstiera.

Každý nový potvrdený účet sa zapíše do `pouzivatelia`, preto sa počítadlo zvýši
automaticky bez cron úlohy. Zakladajúca kapacita v platobnej logike ostáva
samostatná a naďalej počíta iba skutočne udelené nároky.

## Tok dát a zlyhania

- `/api/public/landing` najprv overí aktuálnosť letákových dát a potom doplní
  agregovaný počet účtov z tej istej SQLite databázy.
- Ak sa počet nedá načítať, landing sa vykreslí bez počítadla; bloček ani ceny
  nesmú spadnúť.
- Frontend nikdy nepripočíta lokálny základ ani náhodné číslo.
- Počítadlo a cenové karty musia fungovať bez horizontálneho pretekania od
  šírky 360 px a zachovať klávesnicové ovládanie.

## Testovanie a release

- test validátora s opakovaným proteínom, prílohou a spôsobom prípravy;
- test, že nedostatok vhodných akcií nespôsobí nemožný plán;
- integračný test verejného endpointu: agregovaný počet, hranica 10, žiadne PII;
- JS kontrakt: pod 10 sa číslo nezobrazí, od 10 sa zobrazí presná hodnota;
- vizuálny kontrakt troch kariet a cien 0/39/49;
- celý regresný balík, mobilný a desktopový produkčný test;
- commit na `main`, automatický Hetzner deploy a jeden kontrolovaný predpočet.

