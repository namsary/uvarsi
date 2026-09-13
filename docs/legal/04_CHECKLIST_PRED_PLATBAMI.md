# Uvar.si — checklist pred zapnutím platieb

Stav k 13. septembru 2026. Právna verzia `2026-09-12-v5`. Produkčné platby
možno zapnúť až po uzavretí každého bodu označeného **BLOKÁTOR**. Zaškrtnutie
znamená konkrétny technický alebo dokumentačný dôkaz, nie externé právne
stanovisko.

## Ponuka a prevádzkovateľ

- [x] Iba ročné Premium; bez trialu a bez mesačného plánu.
- [x] Prvých 50 úspešných prvých platieb: 39 € za prvý rok pomocou zľavy 10 €
  z ročného variantu 49 €.
- [x] Zľava je `duration=once`, viazaná iba na ročný variant a najviac 50
  uplatnení; obnova je 49 € ročne.
- [x] Neskorší zákazníci platia 49 € ročne od prvého obdobia.
- [x] Predplatné sa automaticky obnovuje; zrušenie zastaví ďalšiu obnovu a
  Premium zostane do konca zaplateného obdobia.
- [x] Prevádzkovateľ v kóde: PUMAR s. r. o., IČO 57 370 591, Alexandra Dubčeka
  4318/33, 075 01 Trebišov, Mestský súd Košice, oddiel Sro, vložka č. 64515/V,
  pumaragency@gmail.com, +421 917 347 009.
- [ ] **BLOKÁTOR:** overiť aktuálnosť firemných údajov v registri a DIČ/IČ DPH
  doplniť iba podľa overeného stavu a zákonnej povinnosti.
- [ ] Overiť postavenie mikropodniku pre prípadnú výnimku zo zákona o
  prístupnosti.

## Lemon Squeezy nastavenie

- [ ] **BLOKÁTOR:** živý aj testovací variant sú ročné, za 49 €, zverejnené,
  bez trialu a konečná cena s daňovým zobrazením bola skontrolovaná v pokladni.
- [ ] **BLOKÁTOR:** živá aj testovacia zakladajúca zľava je pevná 10 €,
  `duration=once`, obmedzená na jeden ročný variant a najviac 50 uplatnení.
- [ ] **BLOKÁTOR:** Customer Portal povoľuje zrušenie, obnovenie, zmenu karty a
  faktúry; sedemdňová pripomienka obnovy a payment recovery sú zapnuté.
- [ ] **BLOKÁTOR:** test/live store, API kľúče, webhook tajomstvá, varianty a
  zľavy sú oddelené a všetky požadované eventy sú prihlásené v správnom režime.
- [x] Žiadne tajomstvo, podpísaný checkout ani portálová URL nie sú v Gite,
  databáze ani logoch.

## Dokumenty a spotrebiteľský tok

- [x] VOP, ochrana údajov, cookies, odstúpenie a reklamácie sú verziované a
  dostupné v HTML aj textovej podobe.
- [x] Checkout ukladá prvú a obnovovaciu cenu, menu, ročný interval,
  automatickú obnovu, verziu `2026-09-12-v5`, čas a samostatné súhlasy.
- [x] Tlačidlo objednávky jednoznačne vyjadruje povinnosť platby.
- [x] Online odstúpenie a reklamácia sa ukladajú a servisne potvrdzujú.
- [x] Výmaz účtu je oddelený od zrušenia predplatného a refundácie.
- [ ] **BLOKÁTOR:** slovenský advokát skontroloval finálne VOP, automatickú
  obnovu, funkciu odstúpenia, reklamácie a potvrdenie na trvanlivom médiu.
- [ ] **BLOKÁTOR:** reálny e-mail po objednávke uvádza prvú cenu, obnovu 49 €
  ročne, spôsob zrušenia, právnu verziu a kontakty.

## Dáta, bezpečnosť a dodávatelia

- [x] Checkout, webhook, Portal a rekonciliácia neukladajú ani nelogujú
  tajomstvá a krátkodobé podpísané URL.
- [x] Databázové migrácie sú aditívne a chránia existujúce účty, relácie,
  Passkeys, špajzu, plány, ručné Premium a historické platby.
- [ ] **BLOKÁTOR:** overiť DPA a podmienky Hetzner, Resend, Anthropic,
  MailerLite a Lemon Squeezy a zdokumentovať prenosy mimo EHP.
- [ ] **BLOKÁTOR:** zaznamenať reálnu lokalitu servera, zálohy a retenčné
  lehoty logov, podpory, súhlasov a platobných záznamov.
- [ ] **BLOKÁTOR:** preveriť produkčnú databázu na historické použiteľné
  tajomstvá v otvorenom tvare.

## Letáky, ceny a recepty

- [x] Ponuky majú interne obchod, platnosť, zdroj a odtlačok; zákazník nevidí
  technické URL, stránky letáku ani cudzie obrázky.
- [x] Checkout vyžaduje aktuálne ponuky, verejný bloček a schválený zdroj.
- [ ] **BLOKÁTOR `price_source_not_approved`:** pre Lidl, Kaufland aj Tesco
  doložiť povolenie, licencovaný feed alebo právne overený facts-only vstup.
- [ ] **BLOKÁTOR:** externý právnik posúdil databázové právo, podmienky zdrojov,
  ochranné známky a takedown postup.
- [x] Plán je deterministický a bez živého AI volania; AI spracúva letáky
  dávkovo.

## Testovací životný cyklus

- [ ] **BLOKÁTOR:** testovacia prvá platba 39 € vytvorila presne jedno
  predplatné a jednu prvú faktúru; pravidelná prvá platba 49 € bola overená.
- [ ] **BLOKÁTOR:** úspešná obnova vytvorila samostatnú faktúru 49 € bez
  duplicitného nároku.
- [ ] **BLOKÁTOR:** zrušenie ponechalo prístup do `ends_at` a obnovenie pred
  expiráciou vrátilo automatické platenie, ak ho provider podporil.
- [ ] **BLOKÁTOR:** neúspešná platba, recovery, `unpaid` a expirácia mali presné
  prístupové správanie.
- [ ] **BLOKÁTOR:** úplná aj čiastočná refundácia zasiahli iba správnu faktúru.
- [ ] **BLOKÁTOR:** výpadok webhooku a rekonciliácia prešli bez straty prístupu,
  úniku údajov a duplicitnej faktúry.
- [ ] **BLOKÁTOR:** Customer Portal, podpísaný webhook, potvrdenie e-mailom,
  health/readiness a podpísaný lifecycle marker prešli na rovnakom vydaní.
- [ ] **BLOKÁTOR:** finálny plný test suite, statické kontroly, mobilný a
  desktopový smoke prešli na rovnakom commite.

## Produkčná aktivácia

- [x] Deploy aj bežiaci proces vyžadujú `PLATBY_ZAPNUTE=0` a
  `UVARSI_PAYMENTS_ENABLED=0`; nasadenie samo platby nezapne.
- [ ] **BLOKÁTOR:** majiteľ dostal ceny, právnu verziu, dôkaz zdrojov, testovací
  nákup, lifecycle marker, otvorené prípady a health blokátory.
- [ ] **BLOKÁTOR:** majiteľ následne samostatne a výslovne schválil zapnutie
  produkčných checkoutov.
