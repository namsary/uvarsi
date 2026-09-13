# Uvar.si — právny a produktový audit pred plateným spustením

Stav k 13. septembru 2026. Kontrolovaná právna verzia:
`2026-09-12-v5`. Dokument je interný pracovný audit, nie právne stanovisko
advokáta.

## Záver

Kód už zodpovedá ročnému Premium, ale produkčné platby ešte nie sú aktivované.
Pokladňa musí zostať zatvorená, kým neprejde skúšobný životný cyklus pre rovnaký
commit a vydanie a majiteľ nedá samostatný výslovný súhlas.

Pred aktiváciou zostávajú najmä tieto externé alebo prevádzkové blokátory:

1. zdokumentovaný právny titul pre komerčné používanie cenových faktov z Lidla,
   Kauflandu a Tesca;
2. úplný Lemon Squeezy test-mode lifecycle, rekonciliácia, Customer Portal a
   platný podpísaný marker;
3. kontrola finálnych dokumentov slovenským advokátom vrátane odstúpenia,
   reklamácií, automatickej obnovy a potvrdenia zmluvy na trvanlivom médiu;
4. overenie zmlúv, DPA, reálnej lokality hostingu, retencie a prenosov mimo EHP.

## Prevádzkovateľ

- PUMAR s. r. o.
- IČO 57 370 591
- Alexandra Dubčeka 4318/33, 075 01 Trebišov
- Mestský súd Košice, oddiel Sro, vložka č. 64515/V
- pumaragency@gmail.com
- +421 917 347 009

DIČ a IČ DPH sa nesmú domýšľať. Pred prvou platbou sa doplnia iba podľa
overeného stavu a podľa toho, či ich zákon pri tejto ponuke vyžaduje.

## Schválená ponuka

- Predáva sa iba ročné Premium bez skúšobného a bez mesačného plánu.
- Prvých 50 úspešných prvých platieb stojí 39 € za prvých 12 mesiacov.
- Zakladajúca cena vznikne jednorazovou zľavou 10 € z ročného variantu za 49 €.
- Zľava má `duration=once`, je obmedzená na ročný variant a najviac 50
  uplatnení.
- Po prvom roku sa zakladajúce predplatné obnoví za 49 € ročne.
- Ďalší zákazníci platia 49 € ročne od prvého obdobia.
- Predplatné sa automaticky obnovuje, kým ho zákazník nezruší.
- Zrušenie zastaví ďalšiu obnovu a prístup zostane do konca už zaplateného
  obdobia.
- Cena zahŕňa dane iba vtedy, ak to tak pre konkrétny nákup zobrazí a potvrdí
  Lemon Squeezy ako Merchant of Record.

Ponuka musí pred tlačidlom objednávky uviesť prvú cenu, cenu a frekvenciu
obnovy, automatickú obnovu, spôsob zrušenia, okamžitú aktiváciu a poučenie o
odstúpení. Tlačidlo musí jednoznačne vyjadriť povinnosť platby.

## Spotrebiteľské pravidlá

Spotrebiteľ môže od zmluvy uzavretej na diaľku odstúpiť do 14 dní. Ak výslovne
požiada o okamžité poskytovanie služby a dostane zákonné poučenie, pri
odstúpení môže vzniknúť povinnosť uhradiť pomernú časť ceny za už poskytnuté
obdobie. O konečnom výsledku nerozhoduje automat iba podľa tvrdenia klienta;
žiadosť preverí oprávnená obsluha alebo Merchant of Record.

Po 14 dňoch sa cena nevracia iba pre obyčajnú zmenu názoru. Zákazník môže
zrušiť budúcu obnovu a Premium používa do konca zaplateného obdobia. Tým nie sú
dotknuté práva pri vadnej alebo neposkytnutej službe, duplicitnej alebo
neoprávnenej platbe ani iné práva, ktoré nemožno zmluvne vylúčiť.

Lemon Squeezy má poslať pripomienku sedem dní pred ročnou obnovou. Pred go-live
sa musí overiť jej reálne doručenie aj potvrdenie objednávky, odstúpenia,
reklamácie a refundácie na trvanlivom médiu.

## Čo už vynucuje kód

| Oblasť | Stav a dôkaz |
| --- | --- |
| Identita a právna verzia | Centrálna identita PUMAR s. r. o. a nemenná verzia `2026-09-12-v5`; neplatný profil blokuje checkout. |
| Cenová integrita | Ročný variant 49 €, prvá zakladajúca platba 39 €, obnova 49 €, bez `custom_price`, bez trialu a najviac 50 úspešných zakladajúcich platieb. |
| Súhlas | Ukladá sa používateľ, ponuka, prvá a obnovovacia cena, mena, interval, automatická obnova, právna verzia, čas a identifikátor pokusu. |
| Predplatné | Server odvádza prístup iba z overeného lokálneho snapshotu; zrušenie ponechá prístup do `ends_at`, expirácia ho odoberie. |
| Webhooky | Podpis, obchod, variant, režim, ceny a faktúry sa kontrolujú; opakovanie udalosti je idempotentné a neznámy stav ide na kontrolu. |
| Refundácie | Odstúpenie, reklamácia, duplicitná a neoprávnená platba sa rozlišujú; čiastočná refundácia neodoberie iné obdobie. |
| Portál | Vlastník si vyžiada čerstvý Customer Portal odkaz; podpísaná URL sa neukladá ani neloguje. |
| Rekonciliácia | Výpadok poskytovateľa zachová posledný overený prístup; nové faktúry sa importujú iba raz. |
| Poistka platieb | Neúplná konfigurácia, zdroje, worker, bloček, právna verzia, otvorený prípad alebo neplatný lifecycle marker zatvoria iba nové checkouty. |

## Letáky, ceny a značky

Názov produktu, cena a obdobie platnosti sú fakty, no chránená môže byť
databáza, jej podstatná časť, fotografia, grafika, marketingový text, logo aj
zmluvný prístup k zdroju. Verejná dostupnosť preto sama osebe nepreukazuje
oprávnenie na automatizované komerčné preberanie.

Uvar.si zákazníkovi zobrazuje cenové fakty a platnosť, nie technické URL,
obrázky ani celé strany letákov. Interná databáza uchováva provenienciu a
odtlačok. Názvy reťazcov sa používajú iba identifikačne a služba nesmie
naznačovať partnerstvo. Pred platbami treba pre každý reťazec zdokumentovať
oficiálny feed alebo povolenie, licencovaný agregátor, prípadne právne overený
facts-only vstup.

VOP môžu vysvetliť obmedzenia presnosti, ale nenahradia oprávnenie na získanie
dát.

## Osobné údaje a dodávatelia

Spracúvané kategórie zahŕňajú e-mail a účet, hash hesla a relácie, voliteľný
Passkey, profil domácnosti, špajzu, plány, objednávkový súhlas, predplatné,
faktúry, zákaznícke žiadosti a nevyhnutné bezpečnostné logy.

Bežný plán je deterministický a neposiela Anthropic API e-mail, špajzu ani
osobný plán. AI sa používa pri dávkovom spracovaní letákov. Pred platením treba
overiť a zdokumentovať postavenie Hetzner, Resend, Lemon Squeezy, MailerLite a
Anthropic, príslušné DPA a mechanizmus prenosov mimo EHP.

Výmaz účtu nie je zrušenie predplatného ani žiadosť o refundáciu. Osobné
funkčné dáta sa odstránia alebo anonymizujú; nevyhnutné účtovné, platobné,
súhlasové a právne záznamy sa uchovajú iba po potrebnú dobu.

## Recepty a bezpečnosť potravín

Uvar.si nie je zdravotná ani individuálna výživová služba. Používateľ
kontroluje alergény, zloženie na obale, zdravotné obmedzenia, čerstvosť,
hygienu, bezpečnú tepelnú úpravu, cenu a dostupnosť. Kalórie, porcie a úspora sú
odhady. Receptová knižnica a osobný plán nevykonávajú živé AI volanie.

## Primárne právne zdroje na finálnu kontrolu

- [Zákon č. 108/2024 Z. z. o ochrane spotrebiteľa](https://www.slov-lex.sk/ezbierky/pravne-predpisy/SK/ZZ/2024/108)
- [Zákon č. 311/2025 Z. z. — zmeny účinné od 19. júna 2026](https://static.slov-lex.sk/pdf/SK/ZZ/2025/311/ZZ_2025_311.pdf)
- [Zákon č. 22/2004 Z. z. o elektronickom obchode](https://static.slov-lex.sk/static/SK/ZZ/2004/22/20250628.print.html)
- [Autorský zákon č. 185/2015 Z. z.](https://www.slov-lex.sk/ezbierky-fe/pravne-predpisy/SK/ZZ/2015/185/)
- [GDPR](https://eur-lex.europa.eu/legal-content/SK/TXT/?uri=CELEX%3A32016R0679)
- [Slovenská obchodná inšpekcia — internetové obchody](https://www.soi.sk/informacie-pre-verejnost/internetove-obchody)

