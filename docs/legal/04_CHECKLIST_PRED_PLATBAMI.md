# Uvar.si — checklist pred zapnutím platieb

Stav k 7. septembru 2026. Platby možno zapnúť až po uzavretí každého bodu
označeného **BLOKÁTOR**. Zaškrtnutie znamená, že existuje konkrétny technický
alebo dokumentačný dôkaz; neznamená externé právne stanovisko.

## Ponuka a prevádzkovateľ

- [x] Najviac 50 zakladajúcich členstiev za 39 € jednorazovo.
- [x] Bez automatickej obnovy; Zakladajúce Premium sa nepredáva ako predplatné.
- [x] Úplná 14-dňová refundácia bez krátenia.
- [x] Prevádzkovateľ v kóde: PUMAR s. r. o., IČO 57 370 591, Alexandra Dubčeka
  4318/33, 075 01 Trebišov, Mestský súd Košice, oddiel Sro, vložka č. 64515/V,
  pumaragency@gmail.com (`app/operator_profile.py`).
- [ ] **BLOKÁTOR:** overiť aktuálnosť firemných údajov v registri a doplniť
  telefón, DIČ alebo IČ DPH iba vtedy, ak sa na túto ponuku vzťahujú.
- [ ] Overiť postavenie mikropodniku pre prípadnú výnimku zo zákona o
  prístupnosti.
- [ ] **BLOKÁTOR PRE ROČNÉ PREMIUM:** pred predajom 49 €/rok dokončiť obnovu,
  zrušenie ku koncu obdobia, neúspešnú obnovu, expiráciu a refundáciu.

## Dokumenty a spotrebiteľský tok

- [x] VOP, ochrana údajov, cookies, odstúpenie a reklamácie sú verziované a
  dostupné v HTML aj textovej podobe (`app/legal_pages.py`,
  `tests/test_legal_pages.py`, `tests/test_public_pages.py`).
- [x] Checkout ukladá presnú ponuku, cenu 3 900 centov, menu, právnu verziu,
  čas a explicitný súhlas (`tests/test_checkout_consent.py`).
- [x] Tlačidlo checkoutu jednoznačne vyjadruje povinnosť platby.
- [x] Online odstúpenie a reklamácia sa ukladajú a servisne potvrdzujú
  (`app/customer_requests.py`, `tests/test_customer_requests.py`).
- [x] Účet má export a bezpečný výmaz po čerstvom overení; výmaz nie je
  refundácia (`app/account_data.py`, `tests/test_account_data.py`).
- [ ] **BLOKÁTOR:** slovenský advokát skontroloval finálne znenia, funkciu
  odstúpenia od zmluvy podľa § 20a zákona č. 108/2024 Z. z., reklamačný tok a
  potvrdenie zmluvy na trvanlivom médiu.
- [ ] **BLOKÁTOR:** reálny e-mail po objednávke obsahuje alebo prikladá cenu,
  jednorazovosť, VOP, odstúpenie a reklamačný kontakt.

## Dáta, bezpečnosť a dodávatelia

- [x] Verejné platobné upozornenia neobsahujú objednávku, e-mail, používateľské
  ID ani provider ID; identifikátory zostávajú v chránenej SQLite
  (`tests/test_payment_notification_privacy.py`).
- [x] Výmaz odstraňuje profil, špajzu, osobné plány, úlohy, relácie, heslo a
  Passkey a zachováva iba minimálne právne/účtovné záznamy
  (`tests/test_account_data.py`).
- [ ] **BLOKÁTOR:** overiť DPA a zmluvné podmienky Hetzner, Resend, Anthropic,
  MailerLite a Lemon Squeezy a zdokumentovať mechanizmus prenosov mimo EHP.
- [ ] **BLOKÁTOR:** zaznamenať reálnu lokalitu Hetzner servera a zodpovednosť
  za zálohy.
- [ ] **BLOKÁTOR:** skontrolovať produkčnú databázu, že historické tabuľky
  `tokeny` a `sedenia` neobsahujú použiteľné tajomstvá v otvorenom tvare.
- [ ] Nastaviť a zdokumentovať retenciu logov, bezpečnostných udalostí,
  podpory, súhlasov a platobných záznamov.

## Letáky, ceny a recepty

- [x] Každý úspešný zber ukladá interne obchod, počet faktov, typ zberača,
  platnosť a SHA-256 odtlačok zdroja (`app/zbierac_akcii.py`).
- [x] Checkout vyžaduje všetky tri obchody, aktuálnu platnosť, aspoň 20 faktov
  na obchod a schválený serverový zdroj (`app/source_policy.py`,
  `tests/test_source_policy.py`).
- [x] Používateľ nevidí technické URL, čísla strán ani cudzie obrazové podklady;
  cena a platnosť zostávajú (`tests/test_server.py`).
- [ ] **BLOKÁTOR `price_source_not_approved`:** získať písomné povolenie,
  licencovaný feed alebo právne overený facts-only vstup pre Lidl, Kaufland aj
  Tesco a zaznamenať rozhodnutie v serverovom registri.
- [ ] **BLOKÁTOR:** externý právnik posúdil databázové právo, podmienky zdrojov,
  ochranné známky a navrhovaný takedown postup.
- [x] Aplikácia nevyvoláva dojem partnerstva s reťazcami a rozhodujúca je cena
  pri pokladnici (verziované VOP).
- [x] Bežný plán je deterministický a bez živého AI volania; AI spracúva
  letákové podklady dávkovo.

## Marketing a analytika

- [ ] MailerLite formulár má samostatný dobrovoľný marketingový súhlas, účel,
  privacy odkaz a odhlásenie.
- [x] Kapacita 50 a verejný počet používajú iba úspešne zaplatené a
  nerefundované zakladajúce objednávky.
- [ ] Claimy o úspore majú konkrétny výpočet, obdobie a dátum; nejde o záruku.
- [ ] Ak pribudne analytika alebo reklamný pixel, pred aktiváciou sa zavedie
  súhlas a aktualizuje cookies dokument.

## Technický go-live dôkaz

- [x] Fail-closed brána zverejňuje iba bezpečné kódy a pri jedinom chýbajúcom
  predpoklade checkout nepustí (`app/payment_readiness.py`).
- [ ] **BLOKÁTOR `payment_smoke_missing`:** testovacia objednávka na mobile aj
  desktope pre konkrétne vydanie.
- [ ] **BLOKÁTOR:** podpísaný webhook vytvoril presne jeden nárok a potvrdenie
  objednávky bolo doručené.
- [ ] **BLOKÁTOR:** testovacie odstúpenie a úplná refundácia zrušili nárok a
  poslali potvrdenie.
- [ ] **BLOKÁTOR:** výpadok webhooku a následná rekonciliácia prešli bez úniku
  osobných údajov a bez duplicitného nároku.
- [ ] **BLOKÁTOR:** finálny plný test suite, statická kontrola a produkčný smoke
  prešli na rovnakom commite a vydaní.
- [ ] **BLOKÁTOR:** majiteľ dal samostatný výslovný súhlas so zapnutím
  `PLATBY_ZAPNUTE=1` až po predložení všetkých dôkazov.

