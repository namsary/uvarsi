# Uvar.si — právny a produktový audit pred plateným spustením

Stav k 7. septembru 2026. Dokument je interný pracovný audit, nie právne
stanovisko advokáta.

## Záver

Jednorazová zakladajúca ponuka je technicky podstatne bližšie k spusteniu, ale
platby ešte nezapínať. Backend ich drží vypnuté a checkout sa uzavrie aj po
zapnutí prepínača, ak neprejde každá serverová kontrola.

Aktuálne hlavné blokátory sú:

1. `price_source_not_approved` — automatické zberače Lidl/Kupino/mLetaky sú v
   registri vedené ako `pending_permission`. Treba zdokumentovať právny titul
   alebo použiť schválený facts-only zdroj.
2. `payment_smoke_missing` — pre konkrétne vydanie ešte musí prebehnúť
   testovacia objednávka, podpísaný webhook, aktivácia nároku, úplná refundácia
   a odobratie nároku.
3. Externý právnik má skontrolovať finálne VOP, ochranu údajov, odstúpenie,
   reklamácie a najmä spôsob získavania cenových údajov.
4. Treba overiť zmluvy/DPA, reálnu lokalitu hostingu, retenčné lehoty a všetky
   firemné údaje, ktoré sa na PUMAR s. r. o. vzťahujú.

## Overený prevádzkovateľ v aplikácii

- PUMAR s. r. o.
- IČO 57 370 591
- Alexandra Dubčeka 4318/33, 075 01 Trebišov
- Mestský súd Košice, oddiel Sro, vložka č. 64515/V
- pumaragency@gmail.com

Tieto údaje sú centrálne v `app/operator_profile.py`. Telefón, DIČ a IČ DPH sa
nesmú domýšľať; doplnia sa iba po overení, ak sú pre konkrétny predaj povinné.

## Ponuka, ktorú možno spustiť ako prvú

- najviac 50 zakladajúcich členstiev,
- 39 € jednorazovo,
- bez automatickej obnovy,
- Premium natrvalo podľa zverejnených VOP,
- úplná 14-dňová refundácia bez krátenia,
- účet ani otvorenie pokladne miesto nerezervuje; rozhoduje úspešná a
  nerefundovaná platba.

Ročné Premium za 49 €/rok sa v tomto vydaní nesmie predávať. Pred jeho
spustením treba samostatne implementovať a otestovať obnovu, neúspešnú obnovu,
zrušenie ku koncu obdobia, expiráciu, refundáciu a rekonciliáciu.

## Čo už vynucuje kód

| Oblasť | Stav a dôkaz |
| --- | --- |
| Identita prevádzkovateľa | Centrálna validácia v `app/operator_profile.py`; chybný profil blokuje checkout kódom `operator_invalid`. |
| Právne dokumenty | Verejné, verziované HTML a textové stránky z `app/legal_pages.py`; testy `tests/test_legal_pages.py` a `tests/test_public_pages.py`. |
| Objednávkový súhlas | Cena 39 €, jednorazová platba, právna verzia a explicitné potvrdenie sa ukladajú do `checkout_attempts`; `tests/test_checkout_consent.py`. |
| Odstúpenie a reklamácia | Online formuláre, databázový stav a servisné potvrdenie; `app/customer_requests.py` a `tests/test_customer_requests.py`. |
| Export a výmaz účtu | Export iba vlastných údajov; výmaz po novom overení heslom alebo Passkey; `app/account_data.py` a `tests/test_account_data.py`. |
| Platobné upozornenia | Verejná notifikácia obsahuje iba agregovaný stav, identifikátory zostávajú v chránenej SQLite; `tests/test_payment_notification_privacy.py`. |
| Cenové zdroje | Proveniencia, platnosť a odtlačok sa ukladajú interne. Checkout vyžaduje všetky tri aktuálne a schválené zdroje; `app/source_policy.py` a `tests/test_source_policy.py`. |
| Verejné plány | Cena a platnosť zostávajú, technické URL, čísla strán a obrázkové podklady sa z odpovede odstraňujú; regresný test v `tests/test_server.py`. |
| Poistka platieb | `app/payment_readiness.py` vracia stabilné blokátory a zlyháva bezpečne; `tests/test_payment_readiness.py` a `tests/test_platby.py`. |

## Letáky, ceny a značky

Samotné fakty, ako názov produktu, cena a obdobie platnosti, nemusia byť
autorským dielom. Chránená však môže byť databáza, systematicky preberaná časť
databázy, fotografia, grafika, marketingový text, logo aj zmluvný prístup k
zdroju. Verejná dostupnosť preto sama osebe nestačí na záver, že automatizované
komerčné preberanie je dovolené.

Produkčný režim je nastavený konzervatívne:

- používateľ dostáva iba cenové fakty a konečnú platnosť, nie technické adresy,
  obrázky ani celé stránky letáku;
- interná databáza uchováva provenienciu a odtlačok zdroja na kontrolu chyby;
- názvy Lidl, Kaufland a Tesco sa používajú iba identifikačne a web nesmie
  naznačovať partnerstvo;
- aktuálne automatické zberače nevedia odomknúť platenie;
- schválenie musí byť rozhodnutie v serverovom registri po zdokumentovanej
  kontrole, nie parameter z prehliadača.

Pred prvou platbou treba vybrať a zdokumentovať jednu obhájiteľnú cestu:

1. oficiálny feed/API alebo písomné povolenie,
2. licencovaný agregátor s povoleným komerčným použitím,
3. kontrolovaný facts-only import z podkladov, na ktoré má PUMAR oprávnenie.

VOP môžu vysvetliť obmedzenia presnosti, ale nenahradia oprávnenie na získanie
dát.

## Spotrebiteľské povinnosti

Zákon č. 108/2024 Z. z. v znení účinnom od 19. júna 2026 vyžaduje pri zmluve
uzavretej online zreteľne a nepretržite dostupnú funkciu na odstúpenie počas
lehoty, druhý jednoznačný potvrdzovací krok a bezodkladné potvrdenie obsahu,
dátumu a času na trvanlivom médiu. Uvar.si má spotrebiteľský workflow, ale pred
go-live sa musí preveriť celý reálny tok vrátane doručenia e-mailu a refundácie.

Checkout musí bezprostredne pred tlačidlom ukázať hlavné vlastnosti, konečnú
cenu 39 €, jednorazovosť, nulovú automatickú obnovu, 14-dňovú plnú refundáciu a
odkazy na platné dokumenty. Tlačidlo musí jednoznačne vyjadrovať povinnosť
platby. Potvrdenie zmluvy a doklad musia prísť na trvanlivom médiu.

## Osobné údaje a dodávatelia

Reálne kategórie zahŕňajú e-mail a účet, hash hesla a relácie, voliteľný
Passkey, profil domácnosti, špajzu, osobné plány, platobný nárok, objednávkový
súhlas, odstúpenia, reklamácie, bezpečnostné časové údaje a nevyhnutné logy.

Bežný plán je deterministický a neposiela AI e-mail ani obsah špajze. AI sa
používa pri dávkovom spracovaní letákových podkladov. Pred platením treba
overiť a zdokumentovať postavenie a podmienky Hetzner, Resend, Lemon Squeezy,
MailerLite a Anthropic, prenosy mimo EHP a príslušné DPA.

Výmaz účtu odstráni alebo anonymizuje profil, špajzu, plány, úlohy, relácie,
heslo a Passkey. Minimálne účtovné, platobné, súhlasové a právne záznamy sa
uchovajú iba v rozsahu povinnosti alebo ochrany nárokov. Výmaz účtu nie je
refundácia a otvorené odstúpenie alebo reklamácia výmaz dočasne blokujú.

## Recepty a bezpečnosť potravín

Uvar.si nie je zdravotná ani individuálna výživová služba. Používateľ musí
kontrolovať alergény a zloženie na obale, osobitné zdravotné obmedzenia,
čerstvosť, hygienu, bezpečnú tepelnú úpravu, cenu a dostupnosť. Kalórie, porcie
a úspora sú odhady. Receptová knižnica a generátor musia zostať bez živého AI
volania; AI spracúva letáky dávkovo, nie používateľský plán.

## Primárne právne zdroje na finálnu kontrolu

- [Zákon č. 108/2024 Z. z. o ochrane spotrebiteľa](https://www.slov-lex.sk/ezbierky/pravne-predpisy/SK/ZZ/2024/108)
- [Zákon č. 311/2025 Z. z. — zmeny účinné od 19. júna 2026](https://static.slov-lex.sk/pdf/SK/ZZ/2025/311/ZZ_2025_311.pdf)
- [Zákon č. 22/2004 Z. z. o elektronickom obchode](https://static.slov-lex.sk/static/SK/ZZ/2004/22/20250628.print.html)
- [Autorský zákon č. 185/2015 Z. z.](https://www.slov-lex.sk/ezbierky-fe/pravne-predpisy/SK/ZZ/2015/185/)
- [GDPR](https://eur-lex.europa.eu/legal-content/SK/TXT/?uri=CELEX%3A32016R0679)
- [Slovenská obchodná inšpekcia — internetové obchody](https://www.soi.sk/informacie-pre-verejnost/internetove-obchody)

