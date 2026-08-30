# Uvar.si — vlastný deterministický receptový engine

**Dátum:** 30. august 2026  
**Stav:** návrh na schválenie  
**Rozsah:** tvorba jedálničkov, recepty, porcie, špajza, nákupné balenia a stravovacie režimy

## Kontext

Súčasný systém necháva jazykový model vytvoriť celý recept pri požiadavke používateľa. Plán preto trvá dlho, stojí peniaze pri každom novom variante a môže zlyhať na nepresnom recepte alebo na následnej bezpečnostnej kontrole. Posledný produkčný pokus zlyhal dvakrát na príliš všeobecnom postupe, hoci infraštruktúra, prihlásenie aj fronta fungovali.

Pre platený produkt je takáto neistota neprijateľná. Používateľský plán preto nebude vznikať živým volaním Claude ani iného LLM. Umelá inteligencia zostane v dávkovom procese na čítanie letákov. Recepty, množstvá a nákupný zoznam bude skladať náš vlastný testovateľný engine.

## Ciele

- Zobraziť nový jedálniček bez čakania na modelové volanie.
- Vytvárať opakovateľné, variteľné a jazykovo správne recepty.
- Počítať porcie, spotrebu zo špajze, celé nákupné balenia, cenu nákupu a zvyšok balenia na ďalšie použitie.
- Zachovať správny rytmus varenia: každý deň, raz za dva dni alebo raz za tri dni.
- Podporiť režimy Pro: viac bielkovín, vegetariánsky a vegánsky.
- Oddeliť týždenné náklady na čítanie letákov od bezplatného skladania plánov.
- Umožniť bezpečne rozširovať knižnicu bez zmeny aplikačného kódu pri každom novom recepte.

## Čo tento návrh nerieši

- Medicínske, redukčné ani športové výživové plány.
- Individuálne kalorické alebo makro ciele používateľa.
- Spustenie platieb. Platby zostávajú vypnuté do samostatného schváleného release.
- Nahradenie súčasného zberu a OCR letákov.
- Automatické publikovanie receptov vytvorených AI bez kontroly.

## Rozhodnutie

Uvar.si použije verziovanú knižnicu približne 60 flexibilných receptových šablón. Šablóna neurčuje jednu konkrétnu značku ani jedinú surovinu. Definuje kulinársku konštrukciu jedla, napríklad pečené mäso alebo tofu, prílohu, zeleninu, omáčku a povolené náhrady.

Pri vytváraní plánu engine:

1. načíta overené a stále platné akcie,
2. zohľadní profil, režim varenia, obchody, špajzu a stravovací režim,
3. vyberie kompatibilné šablóny a konkrétne akciové suroviny,
4. vypočíta porcie, množstvá, výživové odhady a celé balenia,
5. zostaví recepty, kalendár a nákupný zoznam bez sieťového volania na LLM.

AI bude môcť mimo používateľskej požiadavky navrhnúť kandidáta na nový recept. Kandidát sa dostane do produkcie až po automatických testoch a ľudskej kontrole.

## Tok dát

```text
Letáky → OCR/AI extrakcia → normalizované akcie → týždenná databáza akcií
                                                ↓
Profil + špajza → receptový matcher → porcie a balenia → hotový plán
                         ↑
             verziovaná knižnica receptov
             + katalóg surovín a výživy
```

Používateľská požiadavka nesmie volať Anthropic, OpenAI ani iný generatívny model. Akcie musia byť pripravené týždenným dávkovým procesom ešte pred vytvorením plánu.

## Dátový model surovín

Každá surovina dostane stabilný interný identifikátor a tieto údaje:

- slovenský názov, povolené synonymá a gramatické tvary,
- kategóriu a kulinársku rolu,
- jednotku spotreby a bežné nákupné balenia,
- jedlý podiel a prípadnú stratu pri spracovaní,
- stravovacie značky: mäso, ryba, mlieko, vajce, vegetariánske, vegánske,
- alergény a prípadné upozornenia,
- energiu, bielkoviny, tuky a sacharidy na 100 g alebo 100 ml,
- zdroj a dátum overenia výživových údajov.

Ponuka z letáka sa mapuje na túto kanonickú surovinu. Značka a presný obchodný názov zostanú pri ponuke, aby nákupný zoznam ukázal reálny produkt, ale recept pracuje s kanonickou surovinou.

## Receptová šablóna

Každá šablóna bude obsahovať minimálne:

- `id`, verziu, názov a rodinu jedál,
- stravovacie režimy a zakázané skupiny surovín,
- spôsob prípravy, odhadovaný čas a potrebné vybavenie,
- surovinové pozície, ich kulinársku rolu a povolené náhrady,
- množstvo každej pozície na jednu dospelú porciu,
- koeficient detskej porcie,
- povinné suroviny a voliteľné dochutenie,
- parametrizované kroky s konkrétnymi množstvami a názvami,
- výživový výpočet a značku, či sú hodnoty iba odhadom,
- pravidlá pre zväčšenie dávky bez nezmyselných gramáží,
- testovacie príklady a stav schválenia.

Kroky nesmú byť všeobecné pokyny typu „priprav podľa chuti“. Musia uvádzať činnosť, surovinu, čas alebo jednoznačný výsledný stav. Zároveň nebudeme nútiť recept do umelo dlhého postupu, ak je jednoduchý pokyn sám osebe dostatočný.

## Porcie a kalendár

Jedna dospelá porcia znamená jednu hlavnú porciu jedla pre jedného dospelého na jedno jedlo. Detská porcia sa vypočíta samostatným koeficientom podľa typu suroviny; nebude iba slepou polovicou všetkého.

Počet pripravených porcií vychádza z počtu stravníkov a dní, ktoré má várka pokryť:

- každý deň: sedem varení po jednej dennej dávke,
- raz za dva dni: štyri varenia pokrývajúce `2 + 2 + 2 + 1` deň,
- raz za tri dni: tri varenia pokrývajúce `3 + 3 + 1` deň.

Pre štyroch dospelých pri varení raz za tri dni sú dávky 12, 12 a 4 dospelé porcie. Engine nesmie vytvoriť prázdny deň ani prebytočné porcie do ďalšieho týždňa bez výslovnej voľby používateľa.

## Špajza, spotreba a nákupné balenia

Špajza eviduje množstvo, nie iba názov. Používateľ môže mať napríklad 450 g ryže. Engine odpočíta najviac evidované množstvo a nikdy nevytvorí záporný stav.

Nákupný zoznam rozlišuje:

- množstvo potrebné do receptu,
- množstvo už dostupné doma,
- chýbajúce množstvo,
- počet a veľkosť celých balení na kúpu,
- očakávaný zvyšok po varení.

Ak recept potrebuje 300 g ryže, doma nie je žiadna a dostupné je iba 1 kg balenie, zoznam ukáže kúpu jedného 1 kg balenia, použitie 300 g a zvyšok 700 g. Zvyšok sa do špajze nepripíše automaticky. Používateľ ho potvrdí po nákupe alebo varení.

Cena nákupu sa počíta z celých kupovaných balení, nie z pomernej ceny spotrebovaných gramov. Úspora sa počíta iba vtedy, keď máme porovnateľnú pôvodnú a akciovú cenu toho istého balenia.

## Výber a hodnotenie receptov

Matcher najprv vyradí všetky nekompatibilné šablóny. Zvyšné ohodnotí podľa:

- pokrytia aktuálnymi akciami,
- reálnej úspory a ceny celého nákupu,
- využitia zásob zo špajze,
- množstva zvyškov z balení,
- preferovaných obchodov,
- pestrosti bielkovín, príloh, zeleniny a spôsobov prípravy,
- opakovania jedál z predchádzajúcich týždňov,
- zvoleného stravovacieho režimu.

Pri rovnakom skóre použije stabilné poradie odvodené z týždňa, profilu a verzie enginu. Rovnaký vstup preto vytvorí rovnaký plán, no nový týždeň môže priniesť inú kombináciu.

Plán nesmie mať dve takmer rovnaké jedlá len preto, že boli lacné. Pre každý režim musia byť v týždni dostupné aspoň tri rôzne spôsoby prípravy a rodiny jedál.

## Stravovacie režimy

Bezplatný plán používa režim „bez obmedzenia“. Režimy Pro sú:

### Viac bielkovín

- cieľ najmenej 30 g bielkovín na dospelú porciu hlavného jedla,
- aplikácia zobrazí „odhad X g bielkovín na dospelú porciu“,
- cieľ sa dosahuje kombináciou surovín, nie automaticky väčšou porciou mäsa,
- detské porcie nemajú automaticky dospelý bielkovinový cieľ.

Formuláciu „vysoký obsah bielkovín“ smieme použiť iba vtedy, keď najmenej 20 % energetickej hodnoty jedla pochádza z bielkovín. Ide o hranicu podľa nariadenia (ES) č. 1924/2006. Na výpočet energie použijeme zákonné konverzné faktory vrátane 4 kcal na gram bielkovín a 9 kcal na gram tuku. Ak hranicu nevieme overiť, použijeme iba neutrálny názov „Viac bielkovín“ a hodnotu označíme ako odhad.

### Vegetariánsky

Šablóna ani vybrané náhrady nesmú obsahovať mäso, ryby, morské plody ani zložky z nich. Vajcia a mliečne výrobky sú povolené, ak používateľ nemá ďalšie obmedzenie.

### Vegánsky

Šablóna ani náhrady nesmú obsahovať živočíšne suroviny. Kontrola prebieha cez kanonické značky surovín, nie iba cez názov receptu.

Zakladajúci plán zdedí rovnaké oprávnenia ako súčasné Pro mapovanie. Platby sa týmto návrhom nezapínajú.

## Výživové údaje

Výživové hodnoty sa vypočítajú zo súčtu kanonických hodnôt surovín a vydelia reálnym počtom porcií. V aplikácii budú označené ako odhad, kým nevychádzajú z presne overených značkových údajov.

Uvar.si nebude tvrdiť, že jedálniček lieči ochorenie, zabezpečuje chudnutie alebo nahrádza odborné poradenstvo. Pri alergiách musí rozhranie upozorniť, že zloženie konkrétneho výrobku treba skontrolovať na obale.

## Minimálny obsah knižnice

Prvý produkčný prechod vyžaduje aspoň 60 aktívnych flexibilných šablón. Jedna šablóna môže patriť do viacerých skupín, ale po aplikovaní obmedzení musí zostať minimálne:

| Režim | Minimálny počet použiteľných šablón |
|---|---:|
| Bez obmedzenia | 50 |
| Viac bielkovín | 24 |
| Vegetariánsky | 20 |
| Vegánsky | 12 |

V každom režime musia byť dostupné aspoň tri rodiny jedál a tri spôsoby prípravy. Release sa nesmie oprieť o veľký počet šablón, ktoré sú iba premenovanou verziou toho istého receptu.

## Výkon a chybové stavy

- Vytvorenie plánu po načítaní akcií má mať p95 pod 500 ms na existujúcom Hetzneri.
- Používateľská požiadavka nevytvára úlohu vo fronte a nečaká na model.
- Hotový plán sa môže uložiť do verziovanej cache, ale cache nie je podmienkou rýchlej odozvy.
- Ak nie je dosť kompatibilných akcií, systém nevymyslí cenu ani produkt. Vysvetlí problém a navrhne pridať obchod alebo zmeniť režim.
- Chyba jednej šablóny nesmie znefunkčniť celý týždeň; neplatná šablóna sa vyradí ešte pri štarte alebo release kontrole.

## Bezpečné rozširovanie receptov

Nový recept prejde týmto procesom:

1. človek alebo AI vytvorí kandidáta mimo produkčnej knižnice,
2. validátor skontroluje schému, suroviny, jednotky a stravovacie značky,
3. testy overia porcie, postup, výživu, balenia, alergény a slovenský jazyk,
4. recept prejde ľudskou kulinárskou a jazykovou kontrolou,
5. až potom dostane stav `active` a zvýši sa verzia knižnice.

Zmena verzie knižnice zneplatní plány, ktorých výsledok by už nebol aktuálny. AI kandidát sa nikdy nezverejní len preto, že prešiel syntaktickou validáciou.

## Testovacia stratégia

### Jednotkové testy

- mapovanie ponúk na kanonické suroviny,
- vegetariánske, vegánske a bielkovinové filtre,
- výpočet energie a bielkovín,
- dospelé a detské porcie,
- rytmy `7`, `4` a `3` varenia,
- spotreba zo špajze,
- celé balenia a zvyšky,
- ceny a úspora,
- deterministické poradie.

### Invarianty

- žiadne záporné množstvo,
- žiadne zlomkové nákupné balenie,
- každá surovina v postupe existuje v zozname surovín,
- každá povinná surovina je pokrytá špajzou alebo nákupom,
- vegánsky plán neobsahuje živočíšnu surovinu,
- súčet dní pokrytých várkami je presne sedem,
- cena nákupu zodpovedá celým baleniam.

### Integračné a regresné testy

- celý týždeň nad reálnou vzorkou akcií z Lidl, Kaufland, Tesco a Fresh,
- samostatné scenáre pre každý režim a rytmus varenia,
- ryža: potreba 300 g, balenie 1 kg, správny nákup a zvyšok,
- špajza: evidovaná ryža sa nekupuje druhýkrát,
- jednoduchý, ale úplný postup nepadne na slovnej heuristike,
- kvalita slovenčiny, skloňovanie množstiev a zákaz výrazov typu „ryžu sceďok“ alebo „stehenných rezíkov“,
- p95 výkonu na existujúcom serveri.

## Pozorovanie prevádzky a náklady

Budeme merať počet vytvorených plánov, p50/p95 čas, dôvody nemožného zostavenia, použitie režimov a počet vyradených šablón. Logy nesmú obsahovať e-mail, obsah špajze viazaný na identitu ani iné nepotrebné osobné údaje.

Náklady na AI sa budú týkať iba dávkového čítania letákov a prípadného vývojového návrhu nových receptov. Metrika používateľských plánov preto nebude obsahovať cenu za modelové volanie.

## Nasadenie

1. Vytvoriť katalóg surovín, výživy a dátovú schému receptov.
2. Implementovať engine a testy za vypnutým feature flagom.
3. Naplniť a skontrolovať minimálne 60 šablón.
4. Porovnávať nový výsledok so súčasným systémom bez zobrazenia používateľovi.
5. Prejsť funkčnými, jazykovými, výživovými a výkonnostnými bránami.
6. Zapnúť nový engine pre interný účet a následne pre všetkých používateľov.
7. Vypnúť živé generovanie receptov cez Claude; OCR letákov zostane aktívne.
8. Ponechať starý kód iba počas krátkeho rollback okna a potom ho odstrániť.

Release musí zostať kompatibilný s existujúcim samopull nasadením a nesmie zapnúť platby.

## Akceptačné kritériá

- Endpoint vytvárania plánu nevykoná žiadne volanie na Anthropic, OpenAI ani iný LLM.
- Nový plán vznikne s p95 pod 500 ms po načítaní akcií.
- Knižnica spĺňa minimálne počty a pestrosť vo všetkých režimoch.
- Plán vždy pokryje presne sedem dní podľa zvoleného rytmu.
- Špajza, celé balenia, ceny a zvyšky prejdú integračnými testami.
- Bielkovinové údaje sú vypočítané a primerane označené ako odhad.
- Vegetariánske a vegánske obmedzenia sú kontrolované na úrovni surovín.
- Neplatná alebo neschválená šablóna sa nemôže dostať do produkčného plánu.
- Produkčný smoke test prejde cez skutočný účet pri vypnutých platbách.

## Odmietnuté alternatívy

### Živé generovanie cez OpenAI namiesto Claude

Zmenilo by dodávateľa, nie problém. Ostalo by čakanie, cena, nedeterministický výstup a riziko zlyhania.

### Stovky úplne pevných receptov

Obsah by sa ťažko udržiaval a zle by reagoval na meniace sa akcie. Flexibilné šablóny poskytujú viac kombinácií pri menšom a kontrolovateľnom jadre.

### Živý AI fallback pri nedostatku receptov

Obnovil by presne tú nespoľahlivosť, ktorú návrh odstraňuje. Pri nedostatku dát aplikácia radšej pravdivo vysvetlí problém a ponúkne bezpečnú zmenu vstupu.

## Zdroje pre výživové tvrdenia

- [Nariadenie (ES) č. 1924/2006 o výživových a zdravotných tvrdeniach](https://eur-lex.europa.eu/legal-content/en/TXT/?uri=CELEX%3A32006R1924)
- [Nariadenie (EÚ) č. 1169/2011 o poskytovaní informácií o potravinách spotrebiteľom](https://eur-lex.europa.eu/eli/reg/2011/1169/2025-04-01/eng)
