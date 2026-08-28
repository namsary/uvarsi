# Uvar.si — hybridné načítanie jedálničkov

**Dátum:** 28. august 2026  
**Stav:** návrh na schválenie  
**Predpoklad vydania:** oprava `ace8ef7` je súčasťou toho istého release

## Cieľ

Používateľ nesmie dve minúty pozerať na blokovanú obrazovku. Uvar.si má hotový plán zobraziť do jednej sekundy, ak ho už pozná. Ak musí plán vytvoriť AI, aplikácia do jednej sekundy potvrdí prípravu, umožní používateľovi pokračovať inde a hotový plán sprístupní bez nového plateného volania.

Architektúra musí zároveň:

- zabrániť duplicitným AI volaniam pre rovnaký plán,
- zachovať kontrolu nákladov a limity prepočtov,
- prežiť reštart aplikácie alebo workeru,
- nepodať plán zo starých alebo neúplných akcií,
- zachovať oddelenú logiku bežného plánu a plánu zo špajze,
- fungovať na existujúcom Hetzneri bez Redis, Celery alebo ďalšieho plateného servera.

## Rozhodnutie

Použijeme hybrid troch vrstiev:

1. **Zdieľaná cache:** hotový plán sa podá okamžite všetkým profilom s rovnakým podpisom.
2. **Cielený predvýpočet:** po úspešnom zbere letákov sa vopred vytvoria plány pre aktívnych používateľov a najčastejšie kombinácie.
3. **Trvalá fronta:** chýbajúci plán sa zaradí do SQLite fronty. Samostatný systemd worker ho pripraví na pozadí.

Tento variant volíme namiesto predvýpočtu všetkých kombinácií, ktorý by zbytočne míňal kredit, a namiesto čisto čakajúceho API, pri ktorom by prvý používateľ vždy znášal celé AI volanie.

## Výkonnostné ciele

| Situácia | Odozva rozhrania | Cieľ dokončenia |
|---|---:|---:|
| Hotový alebo predpočítaný plán | p95 do 1 s | okamžite |
| Nová úloha vo fronte | p95 do 1 s | p95 do 60 s |
| Tvrdý limit jedného AI pokusu | — | 120 s |

Čas 20–60 sekúnd je cieľ, nie sľub. Po nasadení ho zmeriame na reálnych plánoch. Ak p95 presiahne 60 sekúnd, ďalšia optimalizácia musí znížiť vstupný výber akcií alebo upraviť modelové volanie; nesmie iba predĺžiť čakanie v prehliadači.

## Používateľský tok

### Otvorenie aplikácie

`GET /api/plan` vráti jeden z týchto stavov:

- `ready`: obsahuje hotový plán,
- `preparing`: plán sa pripravuje a odpoveď obsahuje bezpečný čas ďalšej kontroly,
- `empty`: plán ešte nikto nevyžiadal,
- `failed`: posledný pokus zlyhal; odpoveď vysvetlí stav a ponúkne opakovanie,
- `stale`: uložený plán už nezodpovedá týždňu, akciám, profilu alebo verzii algoritmu.

### Vytvorenie alebo prepočet

`POST /api/plan/generuj` a `POST /api/plan/zo-spajze`:

1. najprv hľadajú platný hotový plán,
2. potom hľadajú existujúcu aktívnu úlohu s rovnakým podpisom,
3. iba ak nič nenájdu, atomicky vytvoria novú úlohu,
4. vrátia `200` s plánom alebo `202` so stavom `preparing`.

Po odpovedi `202` zobrazí aplikácia: „Plán pripravujeme. Pokojne pokračuj inde.“ Používateľ môže meniť sekcie aplikácie alebo ju zavrieť. Kým je otvorená, klient kontroluje stav približne každé štyri sekundy. Po návrate načíta stav bežným `GET /api/plan`.

Rozhranie nezobrazuje falošné percentá. Ukáže iba pravdivý stav: zaradené, pripravuje sa, hotové alebo nepodarilo sa.

## Podpis plánu a deduplikácia

Jedinečný podpis musí obsahovať všetko, čo mení výsledok:

- týždeň a verziu aktuálnych akcií,
- vybrané obchody,
- počet dospelých a detí alebo aktuálny ekvivalent profilu,
- režim varenia,
- stravovacie obmedzenia,
- variant plánu,
- `PLAN_ALGO_VERSION`,
- pri pláne zo špajze aj normalizovaný obsah a verziu špajze.

Databáza vynúti, že pre jeden podpis a variant môže existovať iba jedna aktívna úloha. Súbežné kliknutia aj viacerí používatelia preto dostanú tú istú úlohu. Bežný plán zostáva zdieľateľný. Plán zo špajze sa zdieľa iba pri presne rovnakom anonymizovanom podpise vstupu.

## Trvalá fronta

Do existujúcej SQLite databázy pribudne tabuľka `plan_jobs` s minimálne týmito údajmi:

- identifikátor úlohy,
- podpis, variant, typ plánu a týždeň,
- stav `queued`, `running`, `ready`, `failed`,
- priorita a počet pokusov,
- čas vytvorenia, začatia, dokončenia a ďalšieho pokusu,
- lease vlastníka a čas vypršania lease,
- strojový kód poslednej chyby,
- väzba na hotový zdieľaný plán.

Samostatná systemd služba `uvarsi-plan-worker`:

- vyberá úlohu atomicky,
- spracúva najviac jednu AI úlohu naraz v prvej verzii,
- pravidelne obnovuje lease,
- po páde vráti úlohu s vypršaným lease do fronty,
- opakuje iba chyby, pri ktorých sa požiadavka preukázateľne neodoslala, najviac dvakrát,
- neopakuje validačné chyby bez zmeny vstupu alebo algoritmu,
- uloží výsledok najprv do zdieľanej cache a až potom označí úlohu ako hotovú.

SQLite v režime WAL a krátke transakcie postačia pre očakávaný počiatočný objem. Prechod na externú frontu sa zvažuje až vtedy, keď merania ukážu, že jeden worker nestíha dopyt.

## Náklady, limity a prepočty

Rozpočtová kontrola zostáva fail-closed. Nová úloha sa vytvorí iba vtedy, keď sa dá rezervovať jeden beh. Existujúca alebo hotová úloha ďalší beh nerezervuje.

- Rezervácia sa viaže na `job_id`, aby sa nedala započítať dvakrát.
- Úspešné AI volanie sa zaúčtuje podľa skutočného usage.
- Zlyhanie pred odoslaním požiadavky rezerváciu uvoľní.
- Neistý výsledok po odoslaní sa automaticky neopakuje naslepo.
- Technické zlyhanie pred odoslaním požiadavky používa tú istú úlohu a pevný limit pokusov. Timeout alebo prerušenie po odoslaní sa neopakuje automaticky, lebo by mohlo vzniknúť druhé platené volanie.
- Používateľský limit pre nútený prepočet sa spotrebuje iba pri vytvorení novej úlohy, nie pri načítaní cache alebo pripojení k existujúcej úlohe.
- Žiadny existujúci ledger ani limit sa pri nasadení nevynuluje.

Predvýpočet má samostatný účel `predpocet`, vlastný týždenný strop a rezervu pre živých používateľov. Keď strop predvýpočtu nestačí, systém vynechá menej dôležité profily; nesmie blokovať živú frontu.

## Cielený predvýpočet

Predvýpočet sa spustí až po úspešnom a overenom zbere Lidl, Kaufland a Tesco. Poradie profilov:

1. presné podpisy aktívnych používateľov pre nový týždeň,
2. podpisy, ktoré boli vyžiadané v posledných týždňoch,
3. obmedzený zoznam najčastejších predvolených profilov.

Systém nevytvára kartézsky súčin všetkých možností. Každý podpis pripraví iba raz a zastaví sa pred dosiahnutím rozpočtovej rezervy.

## Výber akcií pre AI

Model dnes nemusí dostať všetkých 582 položiek. Pred volaním vznikne deterministický shortlist približne 100–150 relevantných potravín. Výber musí zachovať:

- zastúpenie všetkých vybraných obchodov,
- hlavné kategórie jedla,
- použiteľné bielkoviny, prílohy, zeleninu a základné suroviny,
- cenovo výhodné položky,
- iba akcie platné pre daný týždeň a lokalitu.

Shortlist slúži iba na návrh. Finálna validácia kontroluje každú použitú položku voči celej aktuálnej databáze akcií. Tak sa zníži prompt a čas bez oslabenia pravdivosti cien.

## Špajza a pripravená oprava

Commit `ace8ef7` je povinnou súčasťou release. Bežný plán bez vstupu zo špajze ignoruje suroviny, ktoré si model svojvoľne označí ako zásoby. Plán vytvorený výslovne zo špajze zostáva prísny a odmietne neznámu alebo duplicitnú položku.

Fronta nesmie tieto dva typy plánu pomiešať. Zmena špajze zneplatní iba plán zo špajze, nie bežný zdieľaný plán.

## Chyby a obnova

- **AI timeout po odoslaní:** úloha zlyhá s možnosťou nového výslovného pokusu; systém ju automaticky neopakuje.
- **Chyba pred odoslaním:** worker použije krátky pevný backoff a najviac dva pokusy, ktoré nevytvoria platené AI volanie.
- **Neplatný výstup AI:** úloha zlyhá s používateľsky zrozumiteľným textom; automatické opakovanie bez zmeny vstupu sa nevykoná.
- **Vyčerpaný rozpočet alebo kredit:** úloha sa nevytvorí; existujúce plány zostanú dostupné.
- **Neúplné letáky:** nové plány sa negenerujú. Aplikácia nepoužije čiastočný týždeň ako úplný.
- **Pád workeru:** lease vyprší a úlohu bezpečne prevezme ďalší beh.
- **Reštart webovej aplikácie:** web a worker zostávajú oddelené; úlohy sú uložené v SQLite.
- **Zastaraný plán počas čakania:** stará úloha sa nedokončí ako aktuálna; klient dostane nový stav a môže zaradiť správny podpis.

Dozorca a `/api/health` musia ukázať aspoň: počet čakajúcich úloh, vek najstaršej, stav workeru, posledné dokončenie, počet zlyhaní a dôvod blokovania. Kritická neobslúžená fronta odošle existujúce ntfy upozornenie majiteľovi.

## Nasadenie

Release obsahuje databázovú migráciu, backend, frontend, worker, systemd jednotku, dozorcu a testy. Postup:

1. vytvoriť zálohu Uvar.si databázy a aplikácie,
2. nasadiť migráciu a kód,
3. spustiť a overiť `uvarsi-plan-worker`,
4. overiť health stav a kompatibilitu starej cache,
5. vykonať produkčný smoke test s jedným studeným a jedným cache hit profilom,
6. až potom považovať release za úspešný.

Nasadenie nesmie zasiahnuť inú aplikáciu na serveri. Platby zostávajú vypnuté.

## Testovací kontrakt

Implementácia musí mať automatické testy pre:

- okamžité `200` pri cache hit a `202` pri studenom pláne,
- jedinú úlohu pri dvojkliku, súbežných požiadavkách a rovnakom podpise dvoch používateľov,
- oddelené podpisy bežného a špajzového plánu,
- úspešné prevzatie plánu po zatvorení a opätovnom otvorení aplikácie,
- polling bez duplicitných POST požiadaviek,
- vypršanie lease a obnovu po páde workeru,
- bezpečný retry, konečné zlyhanie a používateľský text,
- správne rezervovanie, účtovanie a uvoľnenie nákladov,
- zachovanie denných, týždenných a mesačných ledgerov pri nasadení,
- zneplatnenie po zmene týždňa, akcií, profilu, algoritmu alebo špajze,
- úplnosť obchodov a kategórií v shortliste,
- finálnu validáciu cien voči plnej databáze,
- predvýpočet aktívnych profilov bez prekročenia rezervy,
- pripravenú opravu špajze z `ace8ef7`,
- funkčné health údaje a upozornenie pri zaseknutej fronte.

Pred produkciou musí prejsť celý existujúci testovací balík aj nový kontrakt. Produkčný smoke test nesmie zanechať testovaciu reláciu, plán ani zmenu limitu.

## Mimo rozsahu prvej verzie

- Redis, Celery alebo ďalší server,
- webové push notifikácie a e-mail o dokončení plánu,
- viac než jeden súbežný AI worker,
- predvýpočet všetkých možných profilov,
- resetovanie nákladových ledgerov,
- zapnutie platieb.

## Kritériá prijatia

Riešenie je pripravené na release, keď:

1. používateľ dostane plán alebo stav prípravy do jednej sekundy v p95,
2. jeden job nespôsobí viac než jedno platené AI volanie a súbežné požiadavky s rovnakým podpisom vytvoria iba jeden job,
3. studený plán sa v reálnom meraní dokončí do 60 sekúnd v p95 alebo vznikne zdokumentovaný optimalizačný krok pred plateným spustením,
4. plán prežije navigáciu, zatvorenie aplikácie a reštart služby,
5. chyby, limity a neúplné letáky nezobrazia nepravdivý alebo zastaraný plán,
6. celý testovací balík a produkčný smoke test prejdú,
7. platby zostanú vypnuté.
