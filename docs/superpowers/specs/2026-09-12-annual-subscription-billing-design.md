# Uvar.si — ročné Premium s automatickým obnovením

## Cieľ

Uvar.si bude predávať jedno ročné Premium predplatné cez Lemon Squeezy.
Prvých 50 platiacich zákazníkov zaplatí za prvý rok 39 €. Každé ďalšie
obdobie sa automaticky obnoví za 49 €. Ostatní zákazníci zaplatia 49 € od
prvého roka.

Platby zostanú vypnuté, kým testovací tok nepreukáže prvú platbu, aktiváciu
Premium, zrušenie, zachovanie prístupu do konca obdobia, úspešnú obnovu,
neúspešnú obnovu, expiráciu a refundáciu.

## Schválená ponuka

- Zakladajúce Premium stojí 39 € za prvých 12 mesiacov.
- Zakladajúca cena platí najviac pre prvých 50 úspešných prvých platieb.
- Po prvom roku sa Zakladajúce Premium automaticky obnoví za 49 €/rok.
- Bežné Premium stojí 49 €/rok od prvého obdobia.
- Predplatné sa automaticky obnovuje každý rok, kým ho zákazník zruší.
- Zákazník môže obnovovanie kedykoľvek zrušiť. Premium zostane aktívne do
  konca zaplateného obdobia.
- Ponuka nemá skúšobné obdobie.
- Cena zahŕňa dane iba vtedy, keď to tak zobrazí a potvrdí Lemon Squeezy ako
  Merchant of Record.

Zakladajúca cena vznikne ako jednorazová zľava 10 € z ročného variantu za
49 €. Zľava sa použije iba na prvú faktúru. Systém nesmie použiť vlastnú cenu
39 € na predplatné, pretože Lemon Squeezy by ju zachoval aj pri ďalších
obnoveniach. Zľava bude obmedzená na ročný variant a najviac 50 uplatnení.
Server si zároveň ponechá vlastnú transakčnú kontrolu kapacity, aby výsledok
nezávisel iba od počítadla poskytovateľa.

## Spotrebiteľské pravidlá

Uvar.si je priebežne poskytovaná digitálna služba. VOP preto zachovajú
zákonné právo spotrebiteľa odstúpiť od zmluvy v prvých 14 dňoch. Checkout si
samostatne vyžiada:

1. súhlas s VOP a ochranou osobných údajov;
2. výslovnú žiadosť o okamžitú aktiváciu Premium pred uplynutím 14 dní;
3. potvrdenie, že pri odstúpení môže spotrebiteľ uhradiť pomernú časť ceny za
   už poskytnuté obdobie, ak to pripúšťa platné právo a postup Merchant of
   Record.

Po uplynutí 14 dní sa cena nevracia iba pre zmenu názoru. Zrušenie zastaví
budúcu obnovu a ponechá prístup do konca zaplateného obdobia. Toto pravidlo
neobmedzí práva pri vadnej alebo neposkytnutej službe, duplicitnej či
neoprávnenej platbe ani iné práva, ktoré nemožno zmluvne vylúčiť.

Automatická obnova, ďalšia cena 49 €, dátum najbližšej platby a spôsob
zrušenia musia byť viditeľné pred odoslaním objednávky. Lemon Squeezy pošle
zákazníkovi pripomienku sedem dní pred obnovou. Uvar.si nebude posielať druhú
duplicitnú pripomienku, kým sa nepreukáže potreba vlastného záložného kanála.

## Checkout a cenová integrita

Jediným plateným produktom bude ročný predplatný variant Premium za 49 €.
Zakladajúci checkout použije poskytovateľom spravovanú jednorazovú zľavu 10 €.
Bežný checkout zľavu nepoužije.

Pred presmerovaním do checkoutu aplikácia zobrazí:

- cenu prvej platby;
- cenu a frekvenciu ďalších platieb;
- automatické obnovenie;
- spôsob zrušenia;
- okamžitú aktiváciu po potvrdení platby;
- spotrebiteľské poučenie a odkazy na právne dokumenty;
- text tlačidla, ktorý jednoznačne vyjadruje povinnosť platby.

Server uloží nemenný doklad súhlasu: používateľa, produkt, prvú cenu, cenu
obnovy, menu, interval, identifikátor zľavy, verzie právnych textov, čas a
náhodný identifikátor pokusu. Podpísaný webhook musí odkazovať na tento pokus.
Server overí obchod, variant, zľavu, sumu, menu, periodicitu a režim test/live.
Nezhoda neudelí Premium a vytvorí interný prípad na kontrolu.

## Údaje a migrácia

Migrácia bude iba aditívna. Zachová účty, relácie, passkeys, špajzu, plány,
existujúce ručné Premium a historické platobné záznamy.

Nový lokálny záznam predplatného bude obsahovať najmenej:

- používateľa a interný produkt;
- Lemon Squeezy customer, order a subscription ID;
- variant, menu a test/live režim;
- stav predplatného;
- začiatok a koniec aktuálneho obdobia;
- najbližší dátum obnovy a dátum ukončenia po zrušení;
- prvú cenu, štandardnú cenu obnovy a použitú zľavu;
- čas poslednej overenej udalosti.

Samostatná história platieb uloží každú prvú alebo obnovovaciu faktúru pod jej
vlastným jedinečným ID. Rovnaké predplatné tak môže mať viac úspešných platieb
bez toho, aby idempotencia druhú platbu omylom zahodila. Opakované doručenie
tej istej udalosti zostane bezpečne idempotentné.

## Stav predplatného a prístup

Lokálny stav kopíruje overený stav Lemon Squeezy. Pravidlá prístupu sú:

- `active`: Premium je aktívne;
- `cancelled`: Premium zostáva aktívne do `ends_at`, ďalšia platba nevznikne;
- `past_due`: Premium dočasne zostáva aktívne počas pokusov o záchranu platby;
- `unpaid`: Premium sa pozastaví, kým poskytovateľ platbu obnoví;
- `expired`: Premium sa odoberie;
- `paused`: Uvar.si tento stav zákazníkovi neponúka; ak príde, systém sa riadi
  overeným dátumom a označí stav na kontrolu;
- neznámy alebo neúplný stav: nové oprávnenie nevznikne a existujúce sa
  neodoberie bez rekonciliácie.

Aktívne Premium sa odvádza na serveri pri každej chránenej požiadavke. Klient
nesmie určovať stav predplatného ani dátum expirácie. Ručne udelené Premium
zostane samostatným, viditeľne neplateným nárokom.

## Webhooky

Webhook endpoint overí podpis, veľkosť tela, režim, obchod a variant. Telo
uloží do existujúceho bezpečného frontu pred spracovaním. Podporované udalosti:

- `order_created`: spáruje prvú objednávku, ale sám nevytvorí druhý nárok;
- `subscription_created`: vytvorí predplatné a aktivuje prvé obdobie;
- `subscription_updated`: synchronizuje stav, dátumy a zákaznícke odkazy;
- `subscription_payment_success`: uloží jedinečnú faktúru a predĺži obdobie;
- `subscription_payment_failed`: uloží zlyhanie bez duplicitného účtovania;
- `subscription_payment_recovered`: obnoví stav po úspešnej platbe;
- `subscription_cancelled`: zastaví budúcu obnovu, ale neodoberie prístup;
- `subscription_resumed`: obnoví automatické platenie počas ochrannej lehoty;
- `subscription_expired`: odoberie Premium;
- `subscription_payment_refunded` a `order_refunded`: zaevidujú úplnú alebo
  čiastočnú refundáciu bez neodôvodneného odobratia iného zaplateného obdobia.

Kľúč idempotencie bude používať jedinečné ID webhooku alebo faktúry. Nesmie
stáť iba na pôvodnom order ID, pretože každá ročná obnova patrí k tomu istému
predplatnému, ale predstavuje novú platbu.

## Zrušenie, obnovenie a platobná karta

Profil zobrazí stav Premium, cenu ďalšej obnovy, dátum ďalšej platby alebo
konca prístupu a tlačidlo „Spravovať predplatné“. Tlačidlo otvorí podpísaný
Lemon Squeezy Customer Portal, v ktorom zákazník môže:

- zrušiť alebo obnoviť predplatné;
- zmeniť platobnú kartu;
- zobraziť faktúry a históriu platieb.

Uvar.si nebude ukladať dlhodobo podpísané portálové URL. Server si pri
požiadavke vyžiada alebo vráti aktuálny bezpečný odkaz. Ak poskytovateľ nie je
dostupný, profil vysvetlí, že predplatné zostáva nezmenené, a ponúkne kontakt
na podporu.

## Neúspešná platba a dunning

Lemon Squeezy vykoná štyri pokusy o platbu približne počas dvoch týždňov a
pošle zákazníkovi pokyny na opravu karty. Uvar.si počas `past_due` ponechá
Premium aktívne. Pri `unpaid` ho pozastaví a pri `expired` odoberie. Úspešná
obnova platby prístup automaticky vráti.

Každá zmena sa synchronizuje webhookom a pravidelnou rekonciliáciou. Výpadok
webhooku nesmie vytvoriť druhú platbu, predĺžiť expirovaný prístup ani odobrať
zaplatené obdobie. Nezhoda medzi lokálnym stavom a poskytovateľom vytvorí
interné upozornenie bez osobných údajov vo verejnom kanáli.

## Odstúpenie, refundácia a reklamácia

Online funkcia na odstúpenie zostane dostupná počas zákonnej lehoty. Žiadosť
zaznamená čas, objednávku a aktuálne obdobie. Nevykoná refundáciu iba podľa
tvrdenia klienta.

Žiadosť do 14 dní sa označí na zákonné vybavenie. Ak zákazník výslovne požiadal
o okamžitú aktiváciu, systém pripraví pomerný výpočet využitého obdobia a
ponechá konečné vykonanie refundácie Merchant of Record alebo oprávnenej
obsluhe. Po potvrdení odstúpenia sa predplatné zruší, prístup skončí podľa
výsledku vybavenia a zákazník dostane potvrdenie na trvanlivom médiu.

Žiadosť po 14 dňoch sa pri obyčajnej zmene názoru vybaví ako zrušenie budúcej
obnovy bez refundácie. Reklamácia, duplicitná platba, neoprávnená platba a vada
služby zostanú samostatnými dôvodmi na nápravu. Čiastočná refundácia nikdy
automaticky neodoberie celé predplatné.

## Právne a zákaznícke texty

Nová nemenná právna verzia nahradí všetky tvrdenia o jednorazovej platbe,
24-mesačnej garancii a doživotnom pokračovaní. Rovnaké obchodné pravidlá budú
na landingu, v aplikácii, checkoute, VOP, poučení, FAQ, potvrdení objednávky a
servisných e-mailoch.

Kľúčové znenie ponuky:

> Prvý rok za 39 €. Potom 49 € ročne. Predplatné sa automaticky obnovuje,
> kým ho nezrušíš. Zrušiť ho môžeš kedykoľvek; Premium zostane aktívne do
> konca zaplateného obdobia.

Text nesmie tvrdiť, že refundácia nie je možná za žiadnych okolností. Musí
odlíšiť zmenu názoru od zákonného odstúpenia, reklamácie, duplicitnej platby a
neposkytnutej služby.

## Prevádzková pripravenosť

Platobná brána zostane fail-closed. Checkout sa otvorí iba vtedy, keď health
potvrdí:

- platný firemný profil a právnu verziu;
- ročný variant za 49 €;
- jednorazovú zakladajúcu zľavu 10 €, obmedzenú na prvú platbu a 50 použití;
- test/live obchod, variant, discount, API a webhook konfiguráciu;
- schválený cenový zdroj a aktuálny verejný bloček;
- živý plánovací worker a čerstvý úspešný receptový smoke test;
- funkčný Customer Portal;
- úspešný test všetkých platobných stavov pre aktuálne vydanie.

Ak kritická brána zlyhá, zastavia sa iba nové checkouty. Existujúce predplatné,
správa účtu, zrušenie a webhooky zostanú funkčné.

## Testovanie

Implementácia pôjde test-first. Automatické testy pokryjú najmenej:

- 39 € prvú platbu a 49 € každú obnovu;
- zľavu iba na prvú platbu a najviac 50 zakladajúcich miest;
- 51. zákazníka za 49 € bez zakladajúcej zľavy;
- zákaz vlastnej ceny 39 €, ktorá by znížila aj obnovy;
- presný súhlas s cenou, obnovou a okamžitou aktiváciou;
- idempotentnú prvú platbu aj viac samostatných ročných faktúr;
- zachovanie prístupu po zrušení do `ends_at`;
- obnovenie zrušeného predplatného pred expiráciou;
- `past_due`, `unpaid`, zotavenie platby a `expired`;
- úplnú a čiastočnú refundáciu bez odobratia nesprávneho obdobia;
- zlyhaný webhook a následnú rekonciliáciu;
- bezpečný Customer Portal bez úniku podpísaného URL;
- zhodu cien a podmienok vo všetkých verejných textoch;
- zachovanie účtov, ručných nárokov, plánov, špajze a prihlasovania;
- nulové živé AI volania počas platobných testov;
- úplný existujúci regresný balík.

Testovací Lemon Squeezy obchod musí prejsť prvou objednávkou, simulovanou
obnovou, zlyhaním platby, zotavením, zrušením, zachovaním prístupu, expiráciou
a refundáciou. Platby sa zapnú samostatným rozhodnutím majiteľa až po uložení
dôkazov pre rovnaký commit a vydanie.

## Nasadenie

1. Nasadí sa aditívna databázová migrácia a kód s platbami vypnutými.
2. Testovací obchod prejde celým lifecycle.
3. Verejné stránky a aplikácia sa skontrolujú na mobile aj desktope.
4. Produkčná konfigurácia sa overí bez vytvorenia platby.
5. Majiteľ samostatne schváli zapnutie produkčných checkoutov.
6. Prvé živé objednávky sa sledujú cez anonymné prevádzkové metriky a
   rekonciliáciu.

Nasadenie ani doplnenie tajomstiev nesmie samo zapnúť platby.

## Mimo rozsahu

- mesačné predplatné;
- skúšobné obdobie;
- natívna aplikácia alebo platby cez Google Play a App Store;
- zmena plánu počas roka;
- rodinné alebo tímové licencie;
- automatické rozhodovanie spornej refundácie bez ľudskej kontroly;
- reklamné a analytické cookies.

## Podklady

- Zákon č. 108/2024 Z. z., účinné znenie:
  <https://static.slov-lex.sk/static/SK/ZZ/2024/108/20260731.html>
- Lemon Squeezy subscription lifecycle:
  <https://docs.lemonsqueezy.com/help/products/subscriptions>
- Lemon Squeezy webhook events:
  <https://docs.lemonsqueezy.com/help/webhooks/event-types>
- Lemon Squeezy customer portal:
  <https://docs.lemonsqueezy.com/help/online-store/customer-portal>
- Lemon Squeezy jednorazová zľava:
  <https://docs.lemonsqueezy.com/api/discounts/create-discount>
- Lemon Squeezy recovery a dunning:
  <https://docs.lemonsqueezy.com/help/online-store/recovery-dunning>

Táto špecifikácia určuje produktové a technické správanie. Nenahrádza
individuálne právne alebo daňové stanovisko pre PUMAR s. r. o.
