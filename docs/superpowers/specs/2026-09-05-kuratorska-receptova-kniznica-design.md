# Uvar.si — kurátorská receptová knižnica

**Dátum:** 5. september 2026

**Stav:** schválený návrh

**Nadväzuje na:** `2026-08-30-vlastny-receptovy-engine-design.md`

## Prečo meníme pôvodný návrh

Deterministický engine odstránil čakanie na živé generovanie cez jazykový model. Prvých 60 všeobecných šablón však ukázalo inú slabinu: príliš voľné kombinovanie surovín môže vytvoriť jedlo, ktoré je technicky úplné, ale kuchársky neprirodzené. Príkladom je pokyn nakrájať stehno s kosťou na kocky alebo postup, ktorý pripraví fazuľu, no už ju nepridá do jedla.

Uvar.si preto nebude stavať recepty z abstraktných kombinácií. Základom budú konkrétne, na Slovensku známe jedlá s overeným postupom. Engine ich prispôsobí akciám iba v medziach bezpečných náhrad.

## Rozhodnutie

Produkčná knižnica bude obsahovať 100 až 120 kurátorovaných receptových archetypov. Každý archetyp vychádza z reálne varenej rodiny jedál, napríklad kurací paprikáš, slovenské rizoto, francúzske zemiaky, šošovicový prívarok, bolonské cestoviny, zeleninové karí alebo pečené tofu so zeleninou.

Receptový engine:

1. vyberie archetyp kompatibilný s aktuálnymi akciami, profilom a špajzou,
2. použije iba náhrady povolené pre dané jedlo,
3. prepočíta dávku pre dospelých, deti a počet dní,
4. vytvorí nákup z celých balení,
5. zobrazí pôvodný, vopred skontrolovaný slovenský postup.

Používateľská požiadavka nebude volať generatívny model. AI zostane v dávkovom procese na čítanie letákov a pri vývoji môže pomôcť nájsť kandidátov na nové recepty.

## Skladba knižnice

Primárne redakčné zaradenie bude tvoriť:

- 40 % slovenské a stredoeurópske rodinné jedlá,
- 35 % moderné rýchle jedlá na bežný pracovný týždeň,
- 15 % ľahšie a bielkovinovo bohaté jedlá,
- 10 % samostatne navrhnuté vegetariánske a vegánske jedlá.

Stravovacie režimy sú samostatné značky a môžu sa prekrývať s redakčným zaradením. Knižnica musí obsahovať aspoň 24 receptov vhodných pre režim „Viac bielkovín“, 24 vegetariánskych a 16 vegánskych receptov. Jedlo sa do týchto skupín zaradí podľa surovín a výpočtu, nie podľa názvu.

Prvá verzia pokryje hlavné jedlá, sýte polievky a praktické šaláty. Raňajky, dezerty a sviatočné pečenie zostanú mimo tohto vydania.

## Zdroje a redakčný postup

Kandidátov budeme hľadať vo viacerých nezávislých zdrojoch:

- slovenské klasiky a rodinné jedlá: Varecha a Naničmama,
- súčasné bežné varenie: Kuchyňa Lidla a Tesco recepty,
- zdravé, bielkovinové, vegetariánske a vegánske jedlá: Aktin a Cvičte,
- zahraničné moderné archetypy: BBC Good Food a podobné redakčne spravované katalógy,
- používateľské diskusie: iba na overenie, ktoré jedlá ľudia naozaj varia a čo považujú za jednoduché.

Výskumný záznam kandidáta môže obsahovať názov jedla, zoznam surovín ako fakty, základnú techniku, čas, počet porcií, odkaz na zdroj a poznámky kontrolóra. Do produkcie sa nepreberie cudzí opis, fotografia, video ani doslovný postup.

Každý produkčný recept dostane vlastný slovenský text. Pri dôležitých jedlách porovnáme najmenej dva zdroje, aby sme zachytili bežný kuchynský postup a neprepisovali osobitú verziu jedného autora.

Nebudeme hromadne kopírovať celý katalóg jedného webu. Automatizovaný zber môže vytvárať iba neverejný zoznam kandidátov. Produkčnú knižnicu zostavíme výberom z viacerých zdrojov a každý recept prejde samostatnou kontrolou.

## Právna hranica

Zoznam surovín, základná technika a samotná myšlienka jedla majú prevažne faktický a funkčný charakter. Tvorivý opis, fotografie, videá a zostavená databáza však môžu byť chránené.

Pre Uvar.si preto platí:

- nekopírujeme cudzie vety ani odseky,
- nepoužívame cudzie fotografie alebo videá,
- nezverejňujeme zrkadlo cudzej receptovej databázy,
- pri výskume uchovávame zdroj a dátum overenia,
- produkčný postup píšeme nanovo a prispôsobujeme vlastnému dátovému modelu,
- rešpektujeme technické obmedzenia a podmienky konkrétneho webu.

Tento návrh vychádza z rozlíšenia medzi faktickým zoznamom surovín a tvorivým spracovaním receptu, ktoré uvádza U.S. Copyright Office, a z európskej ochrany databáz podľa smernice 96/9/ES.

## Dátový model archetypu

Každý recept bude obsahovať:

- stabilné `id`, verziu, stav schválenia a redakčnú skupinu,
- názov jedla a prirodzený variant názvu podľa použitej suroviny,
- povolené stravovacie režimy a alergény,
- počet porcií, čas, nádoby a spôsob prípravy,
- presný typ každej suroviny vrátane stavu `suchá`, `varená`, `konzervovaná`, `s kosťou` alebo `vykostená`,
- množstvo na dospelú porciu a detský koeficient,
- povolené náhrady s vlastnými množstvami a úpravou postupu,
- zoradené kroky a očakávaný stav surovín po každom kroku,
- výživový odhad, skladovanie a bezpečnú dobu uchovania,
- odkazy na výskumné zdroje a záznam redakčnej kontroly.

Náhrada nebude globálna. Recept na pečené kuracie stehná môže použiť stehno s kosťou, ale nebude ho krájať. Kuracie soté môže použiť prsia alebo vykostené stehenné mäso. Suchá fazuľa a fazuľa v konzerve budú dve odlišné suroviny s odlišným postupom.

## Kuchynský tok

Validátor bude pri každom recepte sledovať životný cyklus suroviny:

```text
nakúpená → pripravená → tepelne upravená → pridaná do jedla → podaná
```

Kontrola odmietne recept, ak:

- sa surovina použije pred očistením, nakrájaním, namočením alebo uvarením,
- sa pripravená povinná surovina do jedla nikdy nepridá,
- postup žiada krájať časť mäsa s kosťou ako vykostené mäso,
- suchá strukovina dostane postup pre konzervovanú alebo naopak,
- tekutiny nestačia na ryžu, cestoviny či suchú strukovinu,
- jediná nádoba je v čase ďalšieho kroku stále obsadená,
- počet hotových porcií nepokrýva naplánované dni.

## Jazyk receptov

Postup bude pôsobiť ako dobrý súčasný recept, nie ako technický manuál ani text pre dieťa.

- Množstvo uvedieme pri prvom použití alebo v kroku, kde je potrebné rozdeliť surovinu. Potom použijeme iba jej názov.
- Použijeme známe kuchárske spojenia: „opeč dosklovita“, „priveď do varu“, „var na miernom ohni“, „peč dozlatista“.
- Viditeľný výsledok doplníme tam, kde pomáha: „kým cibuľa nezmäkne“ alebo „kým mäso nebude v strede prepečené“.
- Kuchynský teplomer môže byť voliteľná poznámka, nie podmienka úspechu bežného receptu.
- Vynecháme samozrejmosti typu čakanie na kontrolku rúry.
- Zaokrúhlime zobrazené množstvá na prirodzené kuchynské hodnoty, pričom interný výpočet zostane presný.
- Koreniny a základné dochutenie budú patriť ku konkrétnemu jedlu; nevzniknú náhodnou univerzálnou kombináciou.

## Výber podľa akcií

Akcia neurčuje recept sama. Matcher najprv nájde kuchynsky platné archetypy a až potom ich zoradí podľa:

- počtu hlavných surovín v akcii,
- ceny celých balení a preukázateľnej úspory,
- využitia špajze,
- opakovania jedál a hlavných surovín,
- pestrosti techník a chutí,
- režimu používateľa.

Ak nie je dostupná bezpečná náhrada, recept sa nevytvorí. Engine nebude siliť akciové kuracie stehno s kosťou do soté len preto, že je lacné.

## Kontrola kvality

Každý nový archetyp prejde týmito bránami:

1. schéma a existencia všetkých surovín,
2. stravovacie režimy a alergény,
3. logický tok prípravy a obsadenie nádob,
4. porcie, tekutiny, celé nákupné balenia a špajza,
5. výživový výpočet,
6. prirodzená slovenčina,
7. porovnanie s reálnym kuchynským postupom,
8. úplný plán nad reálnymi akciami zo všetkých podporovaných obchodov.

Nový alebo upravený recept sa aktivuje až po automatických testoch a samostatnej obsahovej kontrole. Jedna chybná šablóna nesmie zablokovať celý plán.

## Rozdelenie vydania

### Vydanie A — stabilný základ

- dokončiť logické opravy existujúcich 60 šablón,
- zapnúť engine až po úplnom regresnom teste,
- ponechať platby vypnuté,
- zmerať rýchlosť a dôvody odmietnutých plánov.

### Vydanie B — kurátorská knižnica

- vytvoriť výskumný zoznam približne 160 kandidátov,
- vybrať 100 až 120 najsilnejších archetypov,
- napísať vlastné postupy a bezpečné náhrady,
- spustiť obsahové, jazykové a technické brány,
- najprv porovnávať výsledky bez zobrazenia používateľom,
- po úspešnom overení zvýšiť verziu knižnice a aktivovať nové recepty.

## Akceptačné kritériá

- Knižnica obsahuje 100 až 120 aktívnych, obsahovo odlišných archetypov.
- Každý recept má zdrojový záznam, vlastný slovenský text a úspešnú obsahovú kontrolu.
- Postup rozlišuje suché a konzervované strukoviny aj mäso s kosťou a bez kosti.
- Žiadna povinná surovina sa nestratí medzi prípravou a podávaním.
- Recept nepoužije obsadenú nádobu bez medzikroku, ktorý ju uvoľní.
- Jedálniček zachová správne porcie, špajzu, celé balenia a sedemdňový kalendár.
- Stravovacie režimy spĺňajú stanovené minimá a používateľ ich vidí iba vtedy, keď sú pre jeho profil uskutočniteľné.
- Používateľská požiadavka nevykoná žiadne volanie na generatívny model.
- Vytvorenie plánu má na produkčnom serveri p95 pod 500 ms.
- Platby zostanú vypnuté až do samostatne schváleného vydania.

## Zdroje návrhu

- [Varecha — slovenská kuchyňa podľa klikanosti](https://varecha.pravda.sk/najrecepty/narodne-kuchyne/slovenska-kuchyna)
- [Naničmama — klasické a tradičné recepty](https://nanicmama.sme.sk/varime-pecieme-zavarame/klasicke-a-tradicne-recepty-na-domacu-pohodu)
- [Tesco — receptové kategórie](https://www.tesco.sk/hello/recepty)
- [Aktin — zdravé a fit recepty](https://aktin.sk/recepty)
- [U.S. Copyright Office — copyright a recepty](https://www.copyright.gov/help/faq/faq-protect.html)
- [EUR-Lex — právna ochrana databáz](https://eur-lex.europa.eu/EN/legal-content/summary/legal-protection-databases.html)
