"""Versioned public legal documents rendered from one structured source."""

from __future__ import annotations

from dataclasses import dataclass
from html import escape

try:
    from .operator_profile import (
        LEGAL_EFFECTIVE_DATE,
        LEGAL_VERSION,
        OPERATOR,
        public_operator_dict,
    )
except ImportError:
    from operator_profile import (
        LEGAL_EFFECTIVE_DATE,
        LEGAL_VERSION,
        OPERATOR,
        public_operator_dict,
    )


BASE_URL = "https://uvar.si"
FOUNDER_PROMISE = (
    "39 € raz. Premium bez predplatného počas prevádzky služby Uvar.si."
)


@dataclass(frozen=True)
class LegalSection:
    heading: str
    paragraphs: tuple[str, ...] = ()
    bullets: tuple[str, ...] = ()


@dataclass(frozen=True)
class LegalDocument:
    title: str
    summary: str
    sections: tuple[LegalSection, ...]


_COMMON_OPERATOR = LegalSection(
    "Prevádzkovateľ a kontakt",
    (
        "Službu Uvar.si prevádzkuje PUMAR s. r. o., Alexandra Dubčeka "
        "4318/33, 075 01 Trebišov, IČO 57 370 591, zapísaná v Obchodnom "
        "registri Mestského súdu Košice, oddiel Sro, vložka č. 64515/V.",
        "Kontaktný e-mail pre podporu, ochranu osobných údajov, odstúpenie "
        "aj reklamácie je pumaragency@gmail.com.",
    ),
)


_DOCUMENTS: dict[str, LegalDocument] = {
    "vop": LegalDocument(
        "Všeobecné obchodné podmienky Uvar.si",
        "Pravidlá používania digitálnej služby Uvar.si a jednorazovej ponuky Zakladajúce Premium.",
        (
            _COMMON_OPERATOR,
            LegalSection(
                "Služba Uvar.si",
                (
                    "Uvar.si skladá týždenný jedálniček, zrozumiteľné recepty "
                    "a nákupný zoznam z aktuálne spracovaných akciových ponúk "
                    "podporovaných obchodov a z nastavení domácnosti.",
                    "Plánovač používa kurátorskú knižnicu receptov a "
                    "deterministické výpočty. AI pomáha pri spracovaní letákov "
                    "a údržbe knižnice; nevytvára každý zákaznícky plán naživo.",
                    "Uvar.si nie je prepojené s Lidlom, Kauflandom ani Tescom, "
                    "ak výslovne neuvedieme overené partnerstvo.",
                ),
            ),
            LegalSection(
                "Účet a prístup",
                (
                    "Na používanie aplikácie je potrebný účet s platnou "
                    "e-mailovou adresou. Registráciu potvrdíte e-mailom a potom "
                    "sa prihlasujete heslom; podporované zariadenie si môže "
                    "voliteľne uložiť passkey.",
                    "Prihlasovacie údaje chráňte a podozrenie na zneužitie účtu "
                    "nám bezodkladne oznámte. Služba je určená na osobné "
                    "plánovanie domácnosti, nie na ďalší predaj dát alebo "
                    "automatizované hromadné získavanie obsahu.",
                ),
            ),
            LegalSection(
                "Zakladajúce Premium a uzavretie zmluvy",
                (
                    FOUNDER_PROMISE,
                    "Ide o platbu bez automatickej obnovy, nejde o predplatné a "
                    "nevznikne žiadny ďalší pravidelný poplatok.",
                    "Ponuka je určená najviac pre prvých 50 úspešne zaplatených "
                    "a nerefundovaných členstiev. Samotné vytvorenie účtu ani "
                    "otvorenie checkoutu miesto nerezervuje.",
                    "Pred odoslaním objednávky uvidíte súhrn produktu, konečnú "
                    "cenu, tieto podmienky a poučenie o odstúpení. Lemon Squeezy "
                    "vystupuje v checkoute ako Merchant of Record a predávajúci "
                    "pre platobnú transakciu. Zmluva o Zakladajúcom Premium sa "
                    "uzavrie potvrdením objednávky spoločnosťou Lemon Squeezy.",
                    "PUMAR s. r. o. je prevádzkovateľ služby Uvar.si, zabezpečuje "
                    "prístup k produktu a je kontaktným miestom pre produktovú "
                    "podporu, reklamácie a odstúpenie.",
                ),
            ),
            LegalSection(
                "Sprístupnenie, trvanie a ukončenie služby",
                (
                    "Premium aktivujeme bez zbytočného odkladu po prijatí "
                    "overeného potvrdenia platby. Ak platba prebehne, ale Premium "
                    "sa nesprístupní, po spárovaní objednávky ho ručne aktivujeme "
                    "alebo zabezpečíme úplné vrátenie platby. Nemusíte zaplatiť znova.",
                    "Zakladajúce Premium trvá počas prevádzky služby Uvar.si; "
                    "nesľubuje prevádzku na neurčitý ani neobmedzený čas. Ak sa "
                    "PUMAR s. r. o. rozhodne ukončiť prevádzku, oznámi dátum "
                    "ukončenia v predstihu primeranom okolnostiam na trvanlivom "
                    "médiu, ak je to vzhľadom na dôvod ukončenia možné.",
                    "Ukončením prevádzky nezanikajú už vzniknuté zákonné práva "
                    "spotrebiteľa ani nároky, ktoré nemožno zmluvne vylúčiť, "
                    "vrátane práv z vád, reklamácie, odstúpenia, primeranej zľavy, "
                    "vrátenia platby alebo náhrady škody, ak ich priznáva zákon.",
                    "Dostupnosť môže krátko obmedziť "
                    "údržba, obnova letákov, bezpečnostná udalosť alebo výpadok "
                    "dodávateľa.",
                ),
            ),
            LegalSection(
                "Zmeny digitálnej služby",
                (
                    "Funkcie môžeme primerane meniť kvôli bezpečnosti, zákonu, "
                    "oprave alebo zlepšeniu. Zmena nesmie vytvoriť nový poplatok "
                    "bez vašej samostatnej objednávky.",
                    "O podstatnej nepriaznivej zmene, ktorá viac než zanedbateľne "
                    "ovplyvní prístup k službe alebo jej používanie, vás jasne "
                    "informujeme vopred na trvanlivom médiu. Oznámenie uvedie "
                    "povahu a čas zmeny aj spôsob, ako možno bezplatne ukončiť zmluvu.",
                    "Môžete bezplatne ukončiť zmluvu do 30 dní od doručenia "
                    "oznámenia alebo od vykonania zmeny, podľa toho, čo nastane "
                    "neskôr. Toto právo nevznikne, ak si môžete bez dodatočných "
                    "nákladov ponechať nezmenenú verziu služby a tá zostane v súlade "
                    "so zmluvou.",
                ),
            ),
            LegalSection(
                "Odstúpenie a vrátenie platby",
                (
                    "Od zmluvy uzavretej na diaľku môžete odstúpiť bez uvedenia "
                    "dôvodu do 14 dní od jej uzavretia. Pri Zakladajúcom Premium "
                    "poskytujeme v tejto lehote úplné vrátenie platby bez "
                    "krátenia aj vtedy, keď ste službu už začali používať.",
                    "Odstúpenie odošlete cez funkciu v účte na stránke Uvar.si "
                    "alebo e-mailom na pumaragency@gmail.com. Prijatie "
                    "elektronického oznámenia potvrdíme na trvanlivom médiu. "
                    "Platbu vrátime rovnakým spôsobom, akým bola prijatá, "
                    "najneskôr v zákonnej lehote. Po potvrdení úplnej refundácie "
                    "sa Zakladajúce Premium skončí.",
                ),
            ),
            LegalSection(
                "Ceny obchodov a výpočet úspory",
                (
                    "Akciové ceny, balenia, podmienky vernostných programov a "
                    "platnosť sa automaticky spracúvajú z verejne komunikovaných "
                    "ponúk. Ponuka môže byť regionálna, viazaná na kartu, "
                    "obmedzená zásobami alebo zmenená obchodom.",
                    "Pred nákupom si cenu, balenie, podmienky a dostupnosť overte "
                    "v konkrétnej predajni. Cena pri pokladnici je rozhodujúca. "
                    "Výpočet úspory je modelový a nie je zárukou osobnej úspory "
                    "ani najnižšej ceny na trhu.",
                ),
            ),
            LegalSection(
                "Recepty, porcie a bezpečnosť",
                (
                    "Recepty, množstvá, kalórie a porcie sú praktický odhad na "
                    "plánovanie domácnosti, nie zdravotné ani individuálne "
                    "výživové odporúčanie. Skutočná spotreba sa líši podľa veku, "
                    "apetítu, výrobku a zvoleného balenia.",
                ),
                (
                    "Skontrolujte zloženie a alergény na obale.",
                    "Overte čerstvosť, skladovanie a bezpečnú tepelnú úpravu.",
                    "Pri alergii, ochorení, tehotenstve alebo osobitnej diéte sa riaďte zdravotníkom.",
                    "Nebezpečný alebo nelogický postup nepoužite a oznámte nám ho.",
                ),
            ),
            LegalSection(
                "Reklamácie a zodpovednosť",
                (
                    "PUMAR s. r. o. zodpovedá za vady digitálnej služby, ktoré "
                    "sa prejavia počas celej dohodnutej doby poskytovania služby.",
                    "Vadu služby môžete vytknúť cez funkciu v účte alebo na "
                    "pumaragency@gmail.com. Prijatie reklamácie písomne potvrdíme "
                    "a vybavíme ju bezplatne v primeranej lehote; ak zákon "
                    "neurčuje inak, oznámená lehota neprekročí 30 dní bez "
                    "objektívneho dôvodu, ktorý nevieme ovplyvniť.",
                    "Ak reklamáciu zamietneme, oznámime vám výsledok aj písomné "
                    "dôvody zamietnutia na trvanlivom médiu.",
                    "Nič v týchto podmienkach neobmedzuje zákonné práva "
                    "spotrebiteľa ani zodpovednosť, ktorú nemožno zmluvne "
                    "vylúčiť. Nezodpovedáme za samostatnú kúpnu zmluvu medzi "
                    "vami a obchodom ani za vypredanie jeho zásob.",
                ),
            ),
            LegalSection(
                "Účet, osobné údaje a duševné vlastníctvo",
                (
                    "Účet môžete exportovať a po čerstvom overení totožnosti "
                    "vymazať. Výmaz účtu nie je odstúpenie ani žiadosť o "
                    "refundáciu. Údaje potrebné pre účtovníctvo, reklamácie a "
                    "právne nároky uchováme iba v nevyhnutnom rozsahu.",
                    "Softvér, dizajn a pôvodný obsah Uvar.si sú chránené. Svoj "
                    "jedálniček a nákupný zoznam môžete používať a vytlačiť pre "
                    "osobnú potrebu.",
                ),
            ),
            LegalSection(
                "Alternatívne riešenie sporov a rozhodné právo",
                (
                    "Ak nie ste spokojný s vybavením žiadosti, pošlite žiadosť "
                    "o nápravu na pumaragency@gmail.com. Ak ju zamietneme alebo "
                    "na ňu neodpovieme do 30 dní, môžete podať návrh na "
                    "alternatívne riešenie sporu príslušnému subjektu, najmä "
                    "Slovenskej obchodnej inšpekcii; pravidlá sú na soi.sk.",
                    "Zmluva sa riadi právom Slovenskej republiky. Spotrebiteľovi "
                    "s obvyklým pobytom v inom štáte EÚ zostáva ochrana "
                    "ustanovení práva, ktoré nemožno zmluvou vylúčiť.",
                ),
            ),
        ),
    ),
    "ochrana-osobnych-udajov": LegalDocument(
        "Ochrana osobných údajov Uvar.si",
        "Ako PUMAR s. r. o. spracúva údaje používateľov služby Uvar.si.",
        (
            _COMMON_OPERATOR,
            LegalSection(
                "Aké údaje spracúvame",
                (),
                (
                    "Účet a bezpečnosť: e-mail, interné ID, hash hesla, hash relácie, passkey a bezpečnostné časové údaje.",
                    "Profil služby: počet dospelých a detí, rytmus varenia, vybrané obchody a režim stravovania.",
                    "Obsah účtu: špajza, jedálničky, nákupné zoznamy a ich technické verzie.",
                    "Platby a nároky: objednávka, produkt, suma, mena, stav, súhlas s právnou verziou a refundácia.",
                    "Podpora: obsah odstúpenia, reklamácie alebo inej správy a priebeh vybavenia.",
                    "Prevádzka: IP adresa, čas, technické identifikátory a bezpečnostné logy v nevyhnutnom rozsahu.",
                ),
            ),
            LegalSection(
                "Účely a právne základy",
                (
                    "Účet, jedálniček, Premium, servisnú komunikáciu a kroky "
                    "pred objednávkou spracúvame na plnenie zmluvy alebo na "
                    "žiadosť pred jej uzavretím. Platobné a účtovné záznamy "
                    "spracúvame aj pre zákonné povinnosti. Bezpečnostné logy a "
                    "obranu právnych nárokov spracúvame na oprávnený záujem.",
                    "Čakaciu listinu alebo marketing spracúvame iba na základe "
                    "samostatného súhlasu. Súhlas možno kedykoľvek odvolať bez "
                    "vplyvu na zákonnosť predchádzajúceho spracúvania.",
                ),
            ),
            LegalSection(
                "Povinné údaje a následky ich neposkytnutia",
                (
                    "Na vytvorenie a zabezpečenie účtu sú povinné údaje e-mail "
                    "a heslo alebo iný zvolený prihlasovací prostriedok. Na "
                    "zostavenie plánu potrebujeme počet dospelých a detí, rytmus "
                    "varenia, vybrané obchody a režim stravovania. Bez e-mailu "
                    "a prihlasovacieho prostriedku nemožno vytvoriť účet; bez "
                    "potrebných nastavení nemožno zostaviť osobný plán.",
                    "Na kúpu Premium sú potrebné údaje účtu, identifikácia "
                    "objednávky, produkt, cena, mena a potvrdenie aktuálnych "
                    "zmluvných podmienok. Platobné údaje zadávate spoločnosti "
                    "Lemon Squeezy. Bez údajov potrebných pre objednávku Premium "
                    "nemožno kúpiť ani aktivovať.",
                    "Údaje špajze, passkey a odpoveď na nepovinnú otázku nie sú "
                    "podmienkou vytvorenia účtu ani kúpy Premium, ak pri konkrétnej "
                    "funkcii výslovne neuvedieme inak.",
                ),
            ),
            LegalSection(
                "AI a automatizácia",
                (
                    "AI používame najmä pri spracovaní letákov a pri kurátorskej "
                    "údržbe receptovej knižnice. Produkčný plánovač recepty "
                    "negeneruje naživo pre jednotlivého používateľa a pri "
                    "vytvorení bežného plánu neposiela AI e-mail, heslo ani "
                    "obsah špajze.",
                    "Uvar.si nevykonáva automatizované rozhodovanie, ktoré by "
                    "malo voči používateľovi právne alebo obdobne významné účinky.",
                ),
            ),
            LegalSection(
                "Príjemcovia a dodávatelia",
                (
                    "V nevyhnutnom rozsahu môžu údaje spracúvať Hetzner pri "
                    "hostingu, Resend pri servisných e-mailoch, Lemon Squeezy "
                    "pri platbách a refundáciách a MailerLite pri samostatnej "
                    "čakacej listine alebo marketingovom súhlase. Anthropic sa "
                    "používa na AI spracovanie letákových podkladov, nie na "
                    "spracovanie osobného profilu pri každom pláne.",
                    "Údaje môžu dostať aj účtovník, právny poradca alebo orgán "
                    "verejnej moci, ak je to potrebné alebo vyžadované zákonom. "
                    "Osobné údaje nepredávame obchodným reťazcom ani reklamným sieťam.",
                ),
            ),
            LegalSection(
                "Prenosy mimo EHP",
                (
                    "Hetzner poskytuje hosting v Európskom hospodárskom priestore. "
                    "Ak Anthropic, Resend alebo MailerLite prenášajú osobné údaje "
                    "mimo EHP, ich aktuálne zmluvy o spracúvaní údajov zahŕňajú "
                    "štandardné zmluvné doložky Európskej komisie. Uplatnia sa aj "
                    "primerané doplnkové bezpečnostné opatrenia podľa rizika.",
                    "Lemon Squeezy vystupuje pri platbe ako Merchant of Record a "
                    "samostatný predávajúci. Platobné údaje spracúva podľa vlastných "
                    "podmienok a informácií o ochrane súkromia, ktoré sú dostupné "
                    "pred odoslaním objednávky; PUMAR s. r. o. dostane iba údaje "
                    "potrebné na spárovanie objednávky, sprístupnenie služby a podporu.",
                ),
            ),
            LegalSection(
                "Ako dlho údaje uchovávame",
                (
                    "Profil a používateľský obsah uchovávame počas existencie "
                    "účtu. Bežná prihlásená relácia môže trvať najviac 90 dní a "
                    "dočasná relácia na nastavenie hesla najviac jednu hodinu. "
                    "Bezpečnostné záznamy držíme iba po dobu potrebnú na ochranu "
                    "služby alebo riešenie incidentu.",
                    "Účtovné a platobné záznamy uchovávame počas zákonnej lehoty. "
                    "Reklamácie, odstúpenia a dôkazy súhlasu počas vybavenia a "
                    "následne iba po dobu potrebnú na zákonnú povinnosť alebo "
                    "ochranu právnych nárokov.",
                    "Kontakt na čakacej listine uchovávame do odvolania súhlasu, "
                    "najviac 12 mesiacov od prihlásenia alebo najviac 30 dní po "
                    "poslednej požadovanej správe o spustení Uvar.si či "
                    "zakladajúcej ponuke, podľa toho, čo nastane skôr. Potom ho "
                    "z MailerLite vymažeme alebo zaradíme "
                    "na blokovací zoznam iba v minimálnom rozsahu potrebnom na "
                    "rešpektovanie odhlásenia.",
                ),
            ),
            LegalSection(
                "Čakacia listina",
                (
                    "Súhlas s čakacou listinou sa vzťahuje iba na požadované "
                    "informácie o spustení Uvar.si, dostupnosti a skončení "
                    "zakladajúcej ponuky. Nie je to súhlas na neobmedzený marketing.",
                    "Z každej správy sa môžete odhlásiť odkazom na odhlásenie "
                    "alebo môžete odvolať súhlas e-mailom na "
                    "pumaragency@gmail.com. Odvolanie nemá vplyv na zákonnosť "
                    "spracúvania pred jeho doručením.",
                ),
            ),
            LegalSection(
                "Vaše práva",
                (
                    "Máte právo na prístup, opravu, výmaz, obmedzenie, "
                    "prenosnosť, námietku a odvolanie súhlasu podľa podmienok "
                    "GDPR. Údaje z účtu môžete exportovať a účet vymazať cez "
                    "profil po čerstvom overení totožnosti alebo môžete napísať "
                    "na pumaragency@gmail.com. Odpovieme bez zbytočného odkladu, "
                    "spravidla do jedného mesiaca.",
                    "Ak máte podozrenie na porušenie práv, môžete podať návrh na "
                    "začatie konania Úradu na ochranu osobných údajov Slovenskej "
                    "republiky; aktuálny postup je na dataprotection.gov.sk.",
                ),
            ),
            LegalSection(
                "Výmaz a bezpečnosť",
                (
                    "Po výmaze odstránime alebo anonymizujeme profil, špajzu, "
                    "osobné plány, prihlasovacie údaje a relácie. Minimálne "
                    "platobné a právne záznamy zostanú oddelené, ak ich musíme "
                    "uchovať. Výmaz účtu sám osebe nie je žiadosť o refundáciu.",
                    "Používame hashované heslá a tokeny, zabezpečené cookies, "
                    "šifrovaný prenos, obmedzovanie pokusov a zálohy. Citlivé "
                    "údaje o zdraví nevkladajte do názvov položiek špajze.",
                ),
            ),
        ),
    ),
    "cookies": LegalDocument(
        "Cookies a lokálne úložiská Uvar.si",
        "Prehľad technických údajov, ktoré Uvar.si ukladá do prehliadača.",
        (
            _COMMON_OPERATOR,
            LegalSection(
                "Nevyhnutné cookies",
                (),
                (
                    "uvarsi_session: náhodný prihlasovací token v zabezpečenej HttpOnly cookie; server uchováva iba jeho hash; najviac 90 dní alebo do odhlásenia.",
                    "uvarsi_setup: dočasný token na bezpečné potvrdenie účtu alebo nastavenie hesla; najviac jednu hodinu alebo do dokončenia kroku.",
                ),
            ),
            LegalSection(
                "Lokálne úložisko",
                (),
                (
                    "uvarsi_profil: minimálny údaj o dokončení úvodného nastavenia, aby sa aplikácia zobrazila rýchlejšie; uchováva sa do odhlásenia, vymazania účtu alebo vymazania údajov stránky v prehliadači.",
                    "uvarsi_done:*: zaškrtnuté položky nákupného zoznamu pre konkrétny účet a týždeň; automaticky neexpirujú, pri inom pláne sa nepoužijú a zostanú do vymazania účtu alebo údajov stránky v prehliadači.",
                    "uvarsi_kupim:*: voľba kúpiť položku, hoci je evidovaná v špajzi, pre konkrétny účet a plán; automaticky neexpiruje a zostane do vymazania účtu alebo údajov stránky v prehliadači.",
                ),
            ),
            LegalSection(
                "Prečo sa ukladajú",
                (
                    "Tieto technológie sú potrebné na prihlásenie, bezpečnosť a "
                    "funkcie, ktoré si používateľ výslovne vyžiada. Preto sa "
                    "nepodmieňujú marketingovým súhlasom. Uvar.si momentálne "
                    "nepoužíva analytické ani reklamné cookies.",
                ),
            ),
            LegalSection(
                "Ovládanie a výmaz",
                (
                    "Odhlásenie odstráni prihlasovaciu cookie a známy profil. "
                    "Lokálny stav môžete vymazať v nastavení prehliadača; môže "
                    "to spôsobiť odhlásenie alebo stratu zaškrtnutých položiek, "
                    "nie automatický výmaz serverového účtu.",
                    "Pri výmaze účtu aplikácia odstráni Uvar.si cookies, lokálne "
                    "údaje a cache v technicky možnom rozsahu. Ak niekedy "
                    "pridáme analytiku alebo reklamu, nenutné technológie zostanú "
                    "vypnuté, kým používateľ neudelí platný súhlas.",
                ),
            ),
        ),
    ),
    "odstupenie": LegalDocument(
        "Odstúpenie od zmluvy Uvar.si",
        "Ako uplatniť právo na odstúpenie od zmluvy a úplné vrátenie jednorazovej platby za Zakladajúce Premium.",
        (
            _COMMON_OPERATOR,
            LegalSection(
                "Právo na odstúpenie",
                (
                    "Od zmluvy uzavretej na diaľku môžete odstúpiť bez uvedenia "
                    "dôvodu do 14 dní od uzavretia zmluvy. Uvar.si poskytne pri "
                    "Zakladajúcom Premium úplné vrátenie jednorazovej platby "
                    "39 € bez krátenia aj po začatí používania služby.",
                    "Lehota je zachovaná, ak oznámenie o odstúpení odošlete pred "
                    "uplynutím 14-dňovej lehoty. Rozhoduje včasné odoslanie "
                    "oznámenia, nie deň jeho vybavenia ani deň vrátenia platby.",
                ),
            ),
            LegalSection(
                "Ako odstúpenie odoslať",
                (
                    "Prihláste sa do Uvar.si, otvorte Profil a použite funkciu "
                    "Odstúpiť od zmluvy. Počas 14-dňovej lehoty je dostupná "
                    "nepretržite. Oznámenie môžete poslať aj na "
                    "pumaragency@gmail.com.",
                ),
                (
                    "Uveďte e-mail svojho účtu.",
                    "Uveďte číslo objednávky, ak ho máte k dispozícii.",
                    "Jednoznačne napíšte, že odstupujete od zmluvy Uvar.si.",
                    "Dôvod uvádzať nemusíte.",
                ),
            ),
            LegalSection(
                "Potvrdenie a refundácia",
                (
                    "Elektronické odstúpenie bezodkladne potvrdíme e-mailom "
                    "spolu s dátumom a časom prijatia. Úplnú platbu vrátime "
                    "rovnakým spôsobom, akým bola prijatá, najneskôr do 14 dní "
                    "od doručenia oznámenia. Na inom spôsobe vrátenia sa môžeme "
                    "dohodnúť iba výslovne a bez dodatočných poplatkov pre vás.",
                    "Po potvrdení refundácie poskytovateľom platby sa Zakladajúce "
                    "Premium ukončí. Výmaz účtu je samostatný úkon a refundáciu "
                    "nespustí automaticky.",
                ),
            ),
            LegalSection(
                "Vzor oznámenia",
                (
                    "Použitie vzoru nie je povinné. Stačí akékoľvek jednoznačné "
                    "vyhlásenie, z ktorého vyplýva, že odstupujete od zmluvy.",
                ),
                (
                    "Adresát: PUMAR s. r. o., Alexandra Dubčeka 4318/33, 075 01 Trebišov, pumaragency@gmail.com.",
                    "Oznamujem, že odstupujem od zmluvy o digitálnej službe Uvar.si.",
                    "Meno a priezvisko spotrebiteľa.",
                    "Adresa spotrebiteľa.",
                    "E-mail účtu a číslo objednávky, ak je k dispozícii.",
                    "Dátum objednávky.",
                    "Dátum odoslania.",
                    "Podpis spotrebiteľa (iba ak sa vzor posiela v listinnej podobe).",
                ),
            ),
        ),
    ),
    "reklamacie": LegalDocument(
        "Reklamácie a podpora Uvar.si",
        "Ako oznámiť vadu digitálnej služby a ako PUMAR s. r. o. reklamáciu vybaví.",
        (
            _COMMON_OPERATOR,
            LegalSection(
                "Ako oznámiť problém",
                (
                    "Reklamáciu odošlite cez funkciu v Profile alebo na "
                    "pumaragency@gmail.com. Napíšte, čo nefunguje, kedy sa chyba "
                    "prejavila a aký výsledok ste očakávali. Heslo, passkey ani "
                    "prihlasovací token neposielajte.",
                    "Online formulár prijíma iba text. Ak je snímka obrazovky "
                    "potrebná, odpovedzte ňou na potvrdzovací e-mail.",
                ),
            ),
            LegalSection(
                "Potvrdenie a vybavenie",
                (
                    "PUMAR s. r. o. zodpovedá za vady digitálnej služby, ktoré "
                    "sa prejavia počas celej dohodnutej doby poskytovania služby.",
                    "Prijatie bezodkladne písomne potvrdíme. Vadu odstránime "
                    "bezplatne, v primeranej lehote a bez závažných ťažkostí. "
                    "Oznámená lehota spravidla neprekročí 30 dní; dlhšiu lehotu "
                    "použijeme iba z objektívneho dôvodu, ktorý nevieme ovplyvniť, "
                    "a dôvod vám oznámime.",
                    "Ak vadu neodstránime, opakuje sa, je závažná alebo je "
                    "zrejmé, že ju neodstránime, môžete mať podľa zákona právo "
                    "na primeranú zľavu alebo odstúpenie od zmluvy.",
                    "Ak reklamáciu zamietneme, doručíme vám výsledok aj písomné "
                    "dôvody zamietnutia na trvanlivom médiu.",
                ),
            ),
            LegalSection(
                "Čo nie je reklamácia služby",
                (
                    "Uvar.si nepredáva potraviny. Vypredaný výrobok, stav tovaru "
                    "alebo cena účtovaná predajňou riešte s príslušným obchodom. "
                    "Ak však Uvar.si zobrazí nesprávne priradenú cenu, chybný "
                    "výpočet alebo nepoužiteľný recept, oznámte to nám.",
                ),
            ),
            LegalSection(
                "Žiadosť o nápravu a ARS",
                (
                    "Ak nie ste spokojný s vybavením, pošlite žiadosť o nápravu "
                    "na pumaragency@gmail.com. Po zamietavej odpovedi alebo ak "
                    "neodpovieme do 30 dní môžete podať návrh na alternatívne "
                    "riešenie sporu príslušnému subjektu, najmä Slovenskej "
                    "obchodnej inšpekcii. Pravidlá sú na soi.sk. Právo obrátiť "
                    "sa na súd zostáva zachované.",
                ),
            ),
        ),
    ),
}

LEGAL_SLUGS = tuple(_DOCUMENTS)


def _document(slug: str) -> LegalDocument:
    try:
        return _DOCUMENTS[slug]
    except KeyError as error:
        raise KeyError(slug) from error


def _date_label() -> str:
    value = LEGAL_EFFECTIVE_DATE
    return f"{value.day}. {value.month}. {value.year}"


def legal_text(slug: str) -> str:
    document = _document(slug)
    operator = public_operator_dict()
    lines = [
        document.title,
        "=" * len(document.title),
        "",
        document.summary,
        "",
        f"Verzia: {LEGAL_VERSION}",
        f"Účinnosť: {_date_label()}",
        f"Prevádzkovateľ: {operator['business_name']}",
        f"IČO: {operator['company_id']}",
        f"Sídlo: {operator['registered_office']}",
        f"Register: {operator['register']}",
        f"Kontakt: {operator['support_email']}",
    ]
    if operator.get("support_phone"):
        lines.append(f"Telefón: {operator['support_phone']}")
    lines.append("")
    for section in document.sections:
        lines.extend((section.heading, "-" * len(section.heading)))
        for paragraph in section.paragraphs:
            lines.extend((paragraph, ""))
        for item in section.bullets:
            lines.append(f"- {item}")
        if section.bullets:
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def render_legal_page(slug: str) -> str:
    document = _document(slug)
    operator = public_operator_dict()
    phone = (
        '<p><a href="tel:'
        + escape(operator["support_phone"].replace(" ", ""), quote=True)
        + '">'
        + escape(operator["support_phone"])
        + "</a></p>"
        if operator.get("support_phone")
        else ""
    )
    canonical = f"{BASE_URL}/{slug}"
    body: list[str] = []
    for section in document.sections:
        content = "".join(f"<p>{escape(value)}</p>" for value in section.paragraphs)
        if section.bullets:
            content += "<ul>" + "".join(
                f"<li>{escape(value)}</li>" for value in section.bullets
            ) + "</ul>"
        body.append(
            f'<section class="legal-section"><h2>{escape(section.heading)}</h2>{content}</section>'
        )
    return f"""<!DOCTYPE html>
<html lang="sk">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{escape(document.title)} | Uvar.si</title>
  <meta name="description" content="{escape(document.summary, quote=True)}">
  <meta name="robots" content="index,follow">
  <link rel="canonical" href="{canonical}">
  <style>
    :root{{--paper:#f4f7f2;--surface:#fff;--ink:#12392e;--muted:#586b64;--line:#dbe4dc;--accent:#a52f25;--green:#103c31}}
    *{{box-sizing:border-box}} body{{margin:0;background:var(--paper);color:var(--ink);font-family:Arial,sans-serif;line-height:1.65}}
    main{{width:min(860px,calc(100% - 32px));margin:0 auto;padding:28px 0 64px}} a{{color:var(--accent);text-underline-offset:3px}}
    .brand{{display:inline-block;margin-bottom:34px;color:var(--green);font-size:1.4rem;font-weight:900;text-decoration:none;letter-spacing:.03em;text-transform:uppercase}}
    .brand span{{color:var(--accent)}} h1{{font-size:clamp(2rem,6vw,3.5rem);line-height:1.05;letter-spacing:-.04em;margin:0 0 14px}}
    .lead{{font-size:1.08rem;color:var(--muted);max-width:68ch}} .meta,.legal-section{{background:var(--surface);border:1px solid var(--line);border-radius:16px;padding:20px;margin:18px 0}}
    .meta p{{margin:4px 0}} h2{{font-size:1.25rem;margin:0 0 10px}} p{{margin:0 0 12px}} p:last-child{{margin-bottom:0}} li{{margin:7px 0}}
    .actions{{display:flex;gap:12px;flex-wrap:wrap;margin:22px 0}} .actions a{{display:inline-flex;min-height:44px;align-items:center;padding:10px 14px;border:1px solid var(--line);border-radius:999px;background:var(--surface);font-weight:700;text-decoration:none}}
    footer{{margin-top:30px;padding-top:20px;border-top:1px solid var(--line);color:var(--muted);font-size:.9rem}}
  </style>
</head>
<body><main>
  <a class="brand" href="/">Uvar<span>.si</span></a>
  <h1>{escape(document.title)}</h1>
  <p class="lead">{escape(document.summary)}</p>
  <div class="meta">
    <p><strong>Verzia:</strong> {escape(LEGAL_VERSION)} · účinnosť {_date_label()}</p>
    <p><strong>{escape(operator['business_name'])}</strong> · IČO {escape(operator['company_id'])}</p>
    <p>{escape(operator['registered_office'])} · {escape(operator['register'])}</p>
    <p><a href="mailto:{escape(operator['support_email'], quote=True)}">{escape(operator['support_email'])}</a></p>
    {phone}
  </div>
  <div class="actions"><a href="/pravne/{slug}.txt" download>Stiahnuť textovú verziu</a><a href="/app">Otvoriť Uvar.si</a></div>
  {''.join(body)}
  <footer><a href="/vop">VOP</a> · <a href="/ochrana-osobnych-udajov">Ochrana údajov</a> · <a href="/cookies">Cookies</a> · <a href="/odstupenie">Odstúpenie</a> · <a href="/reklamacie">Reklamácie</a></footer>
</main></body>
</html>"""
