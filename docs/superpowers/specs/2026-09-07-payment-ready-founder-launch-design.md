# Uvar.si — bezpečné spustenie zakladajúcich platieb

## Cieľ

Uvar.si prijme najviac 50 jednorazových platieb po 39 € za Zakladajúce
Premium. Platba sa smie zapnúť až vtedy, keď sú právne údaje, checkout,
webhooky, refundácie, používateľské práva a prevádzkový dohľad pripravené ako
jeden overený tok.

Ročné Premium za 49 € zostane dočasne iba budúcou ponukou. Nemá aktívny
checkout a nevytvára predplatné. Samostatný návrh neskôr pokryje obnovu,
neúspešnú platbu, zrušenie ku koncu obdobia a expiráciu.

## Schválené obchodné pravidlá

- Zakladajúce Premium stojí 39 € jednorazovo.
- Ponuka platí pre prvých 50 úspešne zaplatených a nerefundovaných členstiev.
- Vytvorenie účtu ani zápis na čakaciu listinu miesto nerezervuje.
- Premium sa aktivuje až po overenej udalosti `order_created` od
  LemonSqueezy.
- Úplná refundácia do 14 dní sa poskytne bez krátenia a bez uvedenia dôvodu.
- Úplná refundácia zruší Zakladajúce Premium po potvrdení udalosti
  `order_refunded`.
- Čiastočná refundácia sa nespracuje automaticky. Systém ju označí na ručné
  posúdenie a zákazníkovi neodoberie prístup potichu.
- Po vypredaní ponuky checkout nevznikne. Oneskorená 51. platba sa zaradí na
  vrátenie a nevytvorí ďalšie miesto.

## Prevádzkovateľ

Jediným zdrojom právnych údajov bude `app/operator_profile.py`. Verejné
stránky, checkout, servisné e-maily a potvrdenia z neho načítajú rovnaké údaje.

- obchodné meno: PUMAR s. r. o.
- sídlo: Alexandra Dubčeka 4318/33, 075 01 Trebišov
- IČO: 57 370 591
- register: Mestský súd Košice, oddiel Sro, vložka č. 64515/V
- kontaktný, reklamačný, privacy a security e-mail:
  `pumaragency@gmail.com`

Údaje vychádzajú z verejného záznamu Obchodného vestníka Ministerstva
spravodlivosti SR, R127155, deň zápisu 24. 12. 2025. Telefón sa nezverejní,
pretože na podporu sa nepoužíva. DIČ a IČ DPH sa zobrazia iba po ich overení;
ich absencia nesmie vytvoriť nepravdivé tvrdenie o registrácii k DPH.

Funkcia `payment_readiness()` vráti zoznam konkrétnych blokátorov. Server
odmietne zapnúť checkout, ak profil, právna verzia, LemonSqueezy konfigurácia
alebo bezpečnostné podmienky nie sú úplné.

## Verejné právne stránky

Server poskytne stabilné HTML stránky:

- `/vop` — všeobecné obchodné podmienky;
- `/ochrana-osobnych-udajov` — informácie podľa GDPR;
- `/cookies` — technické cookies a lokálne úložiská;
- `/odstupenie` — poučenie a online formulár;
- `/reklamacie` — reklamačný postup a kontaktný formulár.

Každá stránka uvedie verziu, dátum účinnosti, prevádzkovateľa a trvalý odkaz
na stiahnuteľnú textovú alebo PDF podobu. Pätička landingu aj aplikácie bude
obsahovať odkazy na všetky dokumenty a kontakt.

Texty budú zodpovedať účtu s heslom a voliteľným passkey. Magic link sa
spomenie iba pri potvrdení registrácie a obnove hesla. Dokumenty nesmú tvrdiť,
že AI vytvára recepty pre každého používateľa: produkčný plánovač používa
kurátorskú knižnicu a AI sa používa najmä pri spracovaní letákov a údržbe
knižnice.

## Predplatobná obrazovka

Prihlásený používateľ otvorí prehľad objednávky v aplikácii. Obrazovka tesne
pred presmerovaním uvedie:

- Zakladajúce Premium;
- cenu 39 € vrátane príslušných daní podľa checkoutu;
- jednorazovú platbu bez automatickej obnovy;
- hlavné funkcie Premium;
- limit 50 miest a aktuálnu dostupnosť;
- okamžité sprístupnenie po potvrdení platby;
- právo na úplnú refundáciu do 14 dní;
- odkazy na VOP, ochranu údajov a poučenie o odstúpení.

Používateľ samostatne potvrdí VOP a ochranu údajov. Nepovinný marketingový
súhlas sa nespojí s objednávkou. Tlačidlo bude znieť „Prejsť k objednávke s
povinnosťou platby“. LemonSqueezy checkout musí v testovacom režime jasne
zobraziť cenu a platobný charakter posledného tlačidla.

Server pri vytvorení checkoutu uloží auditný záznam: používateľa, produkt,
cenu, menu, verziu VOP, verziu privacy dokumentu, čas, anonymizovaný technický
kontext a náhodný pokus objednávky. Neukladá údaje o karte.

## Platobný stav a webhooky

LemonSqueezy zostáva Merchant of Record. Uvar.si neverí návratu prehliadača z
checkoutu; nárok udeľuje iba webhook s platným podpisom.

Tok musí byť idempotentný:

1. `POST /api/platba/start` overí prihlásenie, pripravenosť, súhlasy a voľné
   miesto. Vytvorí pokus a vráti checkout URL s interným ID používateľa a
   pokusu v `custom_data`.
2. `order_created` overí store, variant, menu, sumu, custom data a stav
   objednávky. Rovnaká udalosť sa môže spracovať opakovane bez druhého nároku.
3. Platný nákup aktivuje Zakladajúce Premium a započíta jedno miesto.
4. `order_refunded` uloží refundáciu a pri úplnom vrátení odoberie nárok.
5. Neúplná, neznáma alebo konfliktná udalosť ostane v karanténe na
   rekonciliáciu. Používateľovi sa nezmení nárok potichu.
6. Pravidelná rekonciliácia porovná lokálne platby so zdrojom LemonSqueezy a
   opraví chýbajúce webhooky bezpečným opakovaním rovnakých pravidiel.

Databáza bude uchovávať objednávku, sumu, menu, produkt, variant, stav,
LemonSqueezy identifikátor, čas, verziu súhlasov a históriu zmien. Citlivé
tajomstvá a celé webhookové telá sa nebudú zapisovať do bežného logu.

## Odstúpenie, refundácia a reklamácia

Používateľ otvorí formulár z profilu alebo verejnej stránky. Prihlásenému sa
objednávka predvyplní. Neprihlásený uvedie e-mail a číslo objednávky; server
nepotvrdí existenciu cudzej objednávky bez e-mailového overenia.

Odoslanie vytvorí časovo označenú žiadosť a bezodkladne pošle potvrdenie na
trvanlivom médiu. Žiadosť do 14 dní sa označí ako nárok na úplnú refundáciu.
Samotný formulár nepredstiera, že poskytovateľ platbu už vrátil. Stav v profile
rozlíši „prijaté“, „spracúva sa“, „vrátené“ a „vyžaduje kontrolu“.

Prvá verzia môže vykonať refundáciu cez LemonSqueezy administráciu. Systém
musí denne upozorniť na nevyriešenú žiadosť bez zverejnenia identity vo
verejnom kanáli a webhook následne uzavrie prípad. Automatická API refundácia
nie je podmienkou prvého predaja, ak tento ručný postup prejde skúškou.

Reklamácia má samostatný typ, opis a stav. Prvá verzia neprijíma súbory, čím
sa vyhne škodlivým prílohám a úniku snímok s osobnými údajmi. Potvrdenie uvedie
prijatie, kontaktný e-mail a možnosť doplniť potrebnú snímku odpoveďou na
servisný e-mail. Výsledok sa eviduje oddelene od marketingu.

## Export a výmaz účtu

Profil ponúkne:

- export používateľských údajov vo formáte JSON;
- zrušenie ostatných relácií;
- výmaz účtu po opätovnom potvrdení heslom alebo passkey.

Export obsahuje profil, špajzu, nastavenia, vlastné plány, nákupné stavy,
passkey metadáta bez verejných kľúčov a históriu platieb potrebnú pre
používateľa. Neobsahuje interné bezpečnostné logy ani údaje iných používateľov.

Výmaz ukončí relácie a odstráni alebo anonymizuje profil, špajzu, plány,
čakajúce úlohy a lokálne identifikátory. Účtovné a platobné záznamy, ktoré sa
musia uchovať, sa oddelia od aktívneho účtu a obmedzia na zákonný účel. Výmaz
účtu automaticky neznamená odstúpenie ani refundáciu; rozhranie to vysvetlí
pred potvrdením.

## Ochrana súkromia a upozornenia

Verejne čitateľný ntfy topic nesmie dostať e-mail, meno, ID používateľa,
číslo objednávky, LemonSqueezy ID ani obsah žiadosti. Upozornenie smie obsahovať
iba typ udalosti, počet nevyriešených prípadov a výzvu na otvorenie chránenej
administrácie.

MailerLite dostane marketingový kontakt iba po samostatnom súhlase. Registrácia
účtu, platba, servisné e-maily a odstúpenie nesmú používateľa automaticky
prihlásiť na marketing.

Produkcia nepoužíva analytické ani reklamné cookies bez predchádzajúceho
súhlasu. Technické úložiská majú pevné retenčné pravidlá a aplikácia ich pri
odhlásení alebo výmaze odstráni podľa rozsahu zariadenia.

## Ceny, letáky a recepty

Zákazník uvidí obchod, akciovú cenu, balenie, podmienku vernostnej karty,
platnosť a čas aktualizácie. Technický zberný odkaz ani kópia letáku sa
nezverejní. Uvar.si nepoužíva cudzie fotografie alebo grafické rozloženie
letákov.

VOP vysvetlia, že ceny a dostupnosť sa môžu líšiť podľa regiónu a predajne a
že údaj pri pokladnici je rozhodujúci. Recepty sú plánovací nástroj, nie
zdravotné odporúčanie; alergény a obal výrobku musí používateľ skontrolovať.

Pred ostrými platbami sa osobitne zdokumentuje právny základ alebo povolenie
produkčného zdroja cenových údajov. Technická kontrola môže povoliť iba zdroj,
ktorý je v internom registri označený ako schválený pre komerčné použitie.

## Prevádzková pripravenosť

`/api/health` poskytne bez tajomstiev samostatný stav `payment_readiness`.
Platby sa zapnú iba ak sú splnené tieto brány:

- úplný profil prevádzkovateľa;
- publikovaná a nemenná verzia právnych dokumentov;
- nakonfigurovaný checkout, store, variant a webhook secret;
- zakladajúci variant má cenu 39 € a nejde o predplatné;
- počítadlo a kapacita používajú iba úspešné reálne platby;
- privátne alebo anonymizované upozornenia;
- funkčný export, výmaz, odstúpenie a reklamácia;
- schválený zdroj cenových údajov;
- živý worker a úspešná skúška vytvorenia jedálnička;
- úspešný testovací nákup a refundácia zaznamenané pre aktuálne vydanie.

Ak sa kritická brána počas prevádzky pokazí, nové checkouty sa zastavia.
Existujúce Premium ostane dostupné. Výpadok webhooku sa rieši frontom a
rekonciliáciou, nie opakovaným účtovaním.

## Testovanie a nasadenie

Každá nová vlastnosť vznikne test-first. Minimálny testovací balík pokryje:

- neúplný profil a chybnú konfiguráciu blokujú checkout;
- správne právne údaje sa zobrazia na každej stránke;
- checkout vyžaduje verziu súhlasu a jednorazový produkt za 39 €;
- 50. platba uspeje a 51. sa bezpečne rieši bez ďalšieho nároku;
- duplicitný webhook nevytvorí duplicitný nárok;
- úplná refundácia odoberie nárok, čiastočná sa eskaluje;
- verejná notifikácia neobsahuje osobné ani objednávkové identifikátory;
- export neuniká cudzie údaje;
- výmaz odstráni používateľské dáta a zachová iba zákonné záznamy;
- marketingový súhlas je oddelený;
- staré účty, plány, špajza, heslá, passkeys a relácie ostanú funkčné;
- celý existujúci regresný balík prejde bez živého AI volania;
- testovací LemonSqueezy nákup na mobile aj desktope aktivuje Premium;
- refundácia v testovacom režime odoberie Premium a uzavrie žiadosť.

Nasadenie prebehne s `PLATBY_ZAPNUTE=0`. Po živom smoke teste sa prepínač
zapne osobitným rozhodnutím majiteľa. Platby sa nezapnú automaticky commitom,
deployom ani doplnením právnych údajov.

## Mimo rozsahu prvého spustenia

- ročné predplatné 49 €;
- automatická obnova a dunning;
- reklamné pixely a behaviorálna analytika;
- automatická refundácia bez ľudskej kontroly;
- partnerské alebo sponzorované poradie obchodov;
- natívna mobilná aplikácia v obchodoch s aplikáciami.
