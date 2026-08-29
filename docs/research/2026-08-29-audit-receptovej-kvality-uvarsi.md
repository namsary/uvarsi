# Uvar.si — read-only audit receptovej kvality

**Dátum:** 29. august 2026  
**Rozsah:** recepty, množstvá, balenia, špajza, koreniny, slovenčina, škálovanie a dátový štandard Recipe  
**Zásah do produktu:** žiadny; ide o audit a návrh pravidiel

## Verdikt

Uvar.si má dobrý technický základ: cenu a veľkosť balenia drží oddelene od textu modelu, nákupné balenia agreguje za celý týždeň a recept od modelu kontroluje. Receptová vrstva však ešte nie je pripravená na platené spustenie. Najväčšie riziká nie sú kozmetické:

1. **Špajza sa odpočítava príliš neskoro.** Najprv sa spočítajú celé nákupné balenia a až potom sa riadok označí ako „máš doma“. Pri viac než jednom vypočítanom balení sa položka zo špajze neodpočíta vôbec. To vysvetľuje nákup ryže, ktorú používateľ eviduje doma.
2. **Koreniny sú skrytý predpoklad.** Systém predpokladá soľ, korenie, olej a vodu, ale receptový zoznam ich neukazuje. Používateľ preto nevie, čo má skontrolovať doma, a postup môže spomenúť surovinu, ktorá v ingredienciách chýba.
3. **Množstvo sa počíta po položkách, nie za celý recept.** Každá „hlavná“ zelenina môže dostať samostatnú dávku 200 g na dospelú porciu. Dve či tri zeleniny preto ľahko vytvoria neprimerane veľkú dávku.
4. **Presná interná matematika sa zobrazuje ako kuchárska hodnota.** Výsledky typu 1 980 g alebo 0,5 g sú matematicky možné, ale v bežnom recepte pôsobia strojovo. Interný výpočet a text zobrazený kuchárovi musia byť dve rozdielne vrstvy.
5. **Batch cooking nemá bezpečnostnú logiku podľa typu jedla.** Ryža nie je vhodná na automatické rozdelenie na tri dni bez pokynu na rýchle schladenie a zmrazenie neskorších porcií.

## Čo ukazujú kvalitné receptové zdroje

- Google odporúča samostatné položky `recipeIngredient` a jednoznačné kroky `HowToStep`; porcie, časy, kategória a kuchyňa patria do vlastných polí, nie do textu postupu. V texte kroku nemá byť nadbytočné „Krok 1“. [Google Search Central — Recipe structured data](https://developers.google.com/search/docs/appearance/structured-data/recipe)
- Schema.org rozlišuje ingrediencie, postup, výťažnosť, časy a výživové údaje ako samostatné vlastnosti. [Schema.org — Recipe](https://schema.org/Recipe)
- Virginia Cooperative Extension odporúča uviesť porcie, čas prípravy a celkový čas; ingrediencie radiť podľa poradia použitia; uviesť všetky množstvá a stav suroviny; kroky písať v poradí, s výsledným znakom hotovosti. Recept má byť opakovane uvarený a upravený podľa výsledku. [Virginia Tech — How to Write a Recipe](https://www.pubs.ext.vt.edu/FST/FST-155/FST-155.html)
- Profesionálne stránky používajú prirodzenú kombináciu kusov a hmotnosti. Kuchyňa Lidla uvádza pre štyri porcie 4 menšie cukety, 400 g kuracích pŕs a 150 g ryže; soľ a dve koreniny sú v zozname. [Kuchyňa Lidla — Plnená cuketa s kuracím mäsom a ryžou](https://kuchynalidla.sk/recepty/plnena-cuketa-s-kuracim-masom-a-ryzou)
- Dobrúchuť pri dvoch porciách uvádza „1 väčšia cuketa“, 200 g ryže, 1,2 l vývaru a „podľa chuti“ soľ a mleté čierne korenie. [Dobrúchuť — Cuketové rizoto](https://dobruchut.aktuality.sk/recept/79968/cuketove-rizoto-lahky-obed/)
- Good Food pri štyroch porciách uvádza 3 cukety, 350 g ryže, 140 g hrášku, čas prípravy a varenia a tri ucelené kroky s časom aj znakom hotovosti. [Good Food — Vegan courgette risotto](https://www.bbcgoodfood.com/recipes/summer-courgette-risotto)
- King Arthur dôsledne oddeľuje aktívny čas, čas pečenia a celkový čas a pri presnom pečení uvádza hmotnosť aj objem. [King Arthur — Recipe rules](https://www.kingarthurbaking.com/blog/2021/04/13/king-arthur-recipe-success)

## Pravidlá pre Uvar.si

### 1. Oddeliť štyri rôzne čísla

Každá surovina má mať štyri samostatné hodnoty:

1. `recipe_required` — presná čistá spotreba receptu,
2. `pantry_available` — potvrdené množstvo doma,
3. `net_required` — čo po odpočítaní špajze chýba,
4. `purchase_packages` — celé reálne balenia potrebné na pokrytie chýbajúceho množstva.

Správne poradie je:

`súčet spotreby receptov − množstvo v špajzi = chýbajúce množstvo → zaokrúhlenie na celé balenia`

Nie:

`spotreba → celé balenia → pokus odpočítať špajzu`

Ak používateľ napíše iba „ryža“ bez množstva, produkt nemá tvrdiť ani „nič nekupuj“, ani automaticky ponechať plný nákup. Riadok sa má presunúť do stavu:

> **Skontroluj doma:** potrebuješ 300 g ryže. V špajzi ju eviduješ, množstvo však nepoznáme.

Taká položka nemá byť medzi primárnymi položkami „kúpiť“, kým ju používateľ nepotvrdí. Prvé verzie konkurenčných pantry-aware produktov používajú rovnaký princíp: položky označené ako zásoby sa z nákupného zoznamu odfiltrujú alebo presunú mimo nákupu. [useLadle — pantry-aware shopping list](https://www.useladle.com/)

### 2. Spotreba receptu nie je balenie v obchode

Recept:

> **Ryža:** 300 g

Nákupný zoznam:

> **Ryža, 1 kg:** kúp 1 balenie · použiješ 300 g · približne 700 g zostane

Ak je potrebných 1,3 kg a dostupné je iba 1 kg balenie:

> kúp 2 × 1 kg · použiješ 1,3 kg · približne 700 g zostane

Ak veľkosť balenia nie je overená, Uvar.si nesmie vypočítať počet balení. Má zobraziť „veľkosť balenia neoverená“. Google vo vlastnom príklade pripúšťa, že ingrediencia môže obsahovať súčasne množstvo aj balenie; v Uvar.si však musia byť spotreba a obchodný výrobok vizuálne oddelené, lebo cena sa viaže na balenie. [Google Search Central — `recipeIngredient`](https://developers.google.com/search/docs/appearance/structured-data/recipe#recipe-properties)

### 3. Prirodzené zaokrúhľovanie

Výpočet môže zostať presný. Používateľský text sa má zaokrúhliť podľa typu suroviny:

| Typ | Zobrazenie v slanom recepte |
|---|---|
| mäso, ryby, syr | do 500 g na 10 g; nad 500 g na 50 g |
| zelenina a ovocie | prirodzený počet kusov; ak počet nepoznáme, do 500 g na 25 g, nad 500 g na 50 g, nad 2 kg na 100 g |
| suchá ryža, cestoviny, strukoviny | na 10 g |
| tekutiny | do 100 ml na 5 ml; 100–500 ml na 10 ml; nad 500 ml na 50 ml |
| vajcia, pečivo, celé kusy | celé kusy; polovica iba pri prirodzene deliteľnej surovine, napr. citrón alebo cibuľa |
| soľ, korenie, sušené bylinky | ČL/PL alebo „podľa chuti“, nie desatiny gramu |
| pečenie | presná gramáž; nezaokrúhľovať podľa pravidiel pre slané jedlá |

Jednotky zapisovať s medzerou: `300 g`, `1,5 kg`, `200 ml`, `180 °C`. V slovenčine používať desatinnú čiarku. SI pravidlá vyžadujú medzeru medzi číslom a symbolom jednotky; desatinnou značkou môže byť čiarka podľa jazykového kontextu. [BIPM — SI Brochure](https://www.bipm.org/documents/d/guest/si-brochure-9-en-pdf), [NIST — Guide to the SI](https://www.nist.gov/pml/special-publication-811/nist-guide-si-chapter-7-rules-and-style-conventions-expressing-values)

Zaokrúhlenie nesmie maskovať chybnú porciu. `1 980 g cukety` sa môže zobraziť ako `približne 2 kg cukety`, ale predtým musí prejsť kontrolou celkovej skladby jedla.

### 4. Kontrolovať súčet skupiny, nie každú zeleninu samostatne

Súčasný prístup prideľuje dávku každej položke. Pre zmiešané jedlo treba najprv stanoviť celkovú dávku skupiny a až potom ju rozdeliť medzi položky:

- celková suchá príloha na dospelú porciu,
- celkové mäso/ryba/hlavný proteín,
- **celková** zelenina v jedle,
- tuk a omáčka,
- dochucovadlá.

Príklad: recept obsahuje cuketu, papriku a cibuľu. Nemajú dostať tri plné „zeleninové porcie“. Cuketa môže tvoriť 60 %, paprika 25 % a cibuľa 15 % z jednej spoločnej zeleninovej dávky. Pozorované pomery na Lidl, Dobrúchuť a Good Food ukazujú, že redakčné recepty kombinujú veľkosť kusov s hmotnosťou a množstvo posudzujú v kontexte celého jedla, nie izolovaného riadka.

### 5. Koreniny a základná špajza musia byť viditeľné

Voda môže byť tichý základ. Soľ, olej, korenie a bylinky nie. Každý recept má mať tri skupiny:

1. **Z akcie / kúpiť** — cenovo overené položky,
2. **Zo špajze** — konkrétne položky, ktoré používateľ eviduje doma,
3. **Skontroluj doma** — soľ, olej, čierne korenie a ďalšie neocenené dochucovadlá.

Každá surovina použitá v postupe musí byť v jednej z týchto skupín. Každá ingrediencia v zozname musí byť použitá v postupe. Virginia Tech aj University of Maine odporúčajú úplnosť a poradie ingrediencií podľa použitia; voliteľné prísady sa majú výslovne označiť. [Virginia Tech](https://www.pubs.ext.vt.edu/FST/FST-155/FST-155.html), [University of Maine Extension](https://extension.umaine.edu/publications/4086e/)

Namiesto všeobecného „korenie“ používať konkrétny názov:

- mleté čierne korenie,
- sladká mletá paprika,
- karí korenie,
- sušený tymian,
- čili vločky.

### 6. Jazykový štandard

Uvar.si má používateľa oslovovať jednotne v 2. osobe jednotného čísla a v rozkazovacom spôsobe:

> prepláchni · nakrájaj · rozohrej · opeč · pridaj · premiešaj · dochuť · podávaj

Nemá miešať `opeč`, `opečieme`, `opeká sa` a prekladové formulácie v jednom recepte.

Názvy produktov z letáka sa nemajú nechávať modelu na skloňovanie. Bezpečný používateľský formát je:

> **900 g · kuracie stehenné rezne**

V kroku:

> **Kuracie stehenné rezne (900 g) osuš, osoľ a okoreň.**

Takto sa systém vyhne chybnému tvaru „stehenných reziek“. Správny genitív množného čísla je **rezňov**; slovníkové heslo je `rezeň, -zňa`. [Krátky slovník slovenského jazyka / Pravidlá slovenského pravopisu](https://slovnik.aktuality.sk/pravopis/?q=reze%C5%88)

„Ryžu sceď“ je gramaticky možné a rovnakú formuláciu používa aj Kuchyňa Lidla. Problémom je neurčitá technika. Uvar.si má pomenovať jednu z dvoch metód:

- absorpčná: „Ryžu prepláchni, zalej 1,5-násobkom vody, prikry a var 12 minút na miernom ohni, kým sa voda nevsiakne. Odstav ju a nechaj 5 minút dôjsť.“
- vo veľkom množstve vody: „Ryžu uvar vo vriacej osolenej vode podľa času na obale, sceď ju a nechaj odkvapkať.“

### 7. Kroky receptu

Pevné pravidlo „v každom druhom kroku musí byť číslo“ produkuje falošnú presnosť. Lepší štandard:

- množstvo je povinné v zozname ingrediencií; v kroku sa opakuje iba pri rozdelení suroviny,
- čas alebo teplota sú povinné pri kritickej tepelnej úprave,
- jeden krok = jeden logický úsek práce,
- krok má uviesť zmenu stavu: do sklovita, dozlata, kým nezmäkne, kým omáčka nezhustne,
- počet krokov sa riadi zložitosťou; jednoduché jedlo môže mať 4, zložitejšie 8,
- voliteľne používať krátke názvy krokov, napr. „Priprav zeleninu“, „Uvar ryžu“, „Opeč mäso“, a samostatný text; to priamo zodpovedá `HowToStep.name` a `HowToStep.text`.

### 8. Bezpečnosť pri varení na dva či tri dni

Uvar.si nemôže všetky jedlá mechanicky označiť ako vhodné na tri dni. Recept musí niesť režim uchovania:

- `fridge_48h` — vhodné na dva dni v chladničke,
- `freeze_later_portions` — neskoršie porcie treba po vychladnutí zmraziť,
- `fresh_component` — časť jedla sa pripraví čerstvá v deň podávania,
- `not_for_leftovers` — recept sa do trojdňového režimu nezaradí.

Pri ryži je pravidlo prísnejšie: rýchlo schladiť, uložiť do chladničky do jednej hodiny, spotrebovať do 24 hodín a neohrievať viac než raz. Preto má trojdňový plán buď odporučiť čerstvú ryžu v deň jedenia, alebo neskoršie porcie ihneď zmraziť. [NHS — starchy foods and rice safety](https://www.nhs.uk/live-well/eat-well/food-types/starchy-foods-and-carbohydrates/), [Food Standards Agency — rice safety](https://www.food.gov.uk/print/pdf/node/4286)

Pri hydine sa nemá spoliehať iba na čas či farbu. Bezpečný cieľ je 74 °C v jadre; rovnakú teplotu uvádza FoodSafety.gov aj pre dôkladné zohriatie zvyškov. [FoodSafety.gov — safe minimum temperatures](https://www.foodsafety.gov/food-safety-charts/safe-minimum-internal-temperatures)

## Príklady pred/po

| Pred | Po |
|---|---|
| `1980g cukety` | `približne 2 kg cukety` — až po kontrole celkovej zeleninovej dávky |
| `0,5 g čierneho korenia` | `¼ ČL mletého čierneho korenia, potom podľa chuti` |
| `3,25 ks cibule` | `3 stredné cibule` alebo prepočet receptu tak, aby nevznikla štvrtina kusu |
| `900 g stehenných reziek` | `900 g · kuracie stehenné rezne` / `900 g kuracích stehenných rezňov` |
| `Ryžu uvar a sceď.` | `300 g ryže prepláchni, zalej 450 ml vody a var prikrytú 12 minút na miernom ohni, kým sa voda nevsiakne.` |
| postup spomenie olej, no ingrediencie nie | `Skontroluj doma: 2 PL oleja, soľ, mleté čierne korenie` |
| špajza: `ryža`; nákup: `2 × 1 kg ryže` | `Skontroluj doma: potrebuješ 1,3 kg; množstvo v špajzi nepoznáme` |
| recept: `300 g ryže`; nákup: `300 g ryže` | recept `použiješ 300 g`; nákup `1 × 1 kg, zostane približne 700 g` |

## Odporúčané akceptačné kritériá

Recept sa nesmie publikovať, ak:

- ingrediencia z postupu chýba v troch zoznamoch „kúpiť / zo špajze / skontrolovať doma“,
- ingrediencia zo zoznamu sa v postupe nepoužije,
- nákup sa zaokrúhľuje na balenia pred odpočítaním známej zásoby,
- používateľský text obsahuje desatiny gramu v slanom recepte,
- počet kusov nie je celý okrem prirodzene deliteľných surovín,
- súčet skupiny prekročí limit receptu iba preto, že každá zelenina dostala plnú porciu,
- postup mieša jazykové osoby alebo obsahuje neoverený skloňovaný názov produktu,
- jedlo na dva či tri dni nemá pokyn na uchovanie a ohrev,
- hydina nemá bezpečný znak hotovosti,
- verejná receptová stránka nemá zhodný viditeľný obsah a JSON-LD.

## Priority

1. **P0:** špajza pred baleniami; bezpečnosť ryže a zvyškov; úplný zoznam korenín.
2. **P1:** skupinové dávky a prirodzené zaokrúhľovanie; oddelenie „použiješ“ od „kúpiš“.
3. **P1:** jazykový register, kanonické názvy a blokovanie chybných tvarov.
4. **P2:** verejné receptové URL a kompletné Recipe JSON-LD pre SEO/GEO.

## Obmedzenia auditu

Pozorované gramáže zo slovenských a medzinárodných receptov nie sú univerzálne výživové normy. Slúžia ako redakčné príklady prirodzenej kuchárskej komunikácie. Presné dávky Uvar.si treba kalibrovať na otestovaných základných receptoch; generátor nemá vytvárať nový recept iba násobením každej položky izolovane.
