# Uvar.si — účet s heslom a voliteľným passkey

## Cieľ

Používateľ potvrdí e-mail iba pri registrácii, zmene e-mailu alebo obnove
hesla. Potom sa prihlási heslom alebo passkey. Mobil, počítač a nainštalovaná
PWA ostanú prihlásené súčasne.

## Dnešný problém

Súčasný systém používa magic link pri každom prihlásení. Relácia síce platí
30 dní, ale nové prihlásenie zmaže všetky predchádzajúce relácie používateľa.
Prihlásenie na mobile preto môže odhlásiť počítač. Relácia sa používaním
neobnovuje a používateľ nemá náhradný spôsob prihlásenia, keď e-mail mešká.

## Rozsah

Zmena zahŕňa registráciu, potvrdenie e-mailu, prihlásenie heslom, obnovu hesla,
passkey, viac zariadení, správu relácií a migráciu existujúceho účtu. Platby,
oprávnenia Premium, jedálničky a špajza sa nemenia.

## 1. Registrácia

1. Používateľ zadá e-mail a heslo na Uvar.si.
2. Server normalizuje e-mail, overí heslo a uloží iba Argon2id hash do
   časovo obmedzenej čakajúcej registrácie.
3. Server odošle jednorazový potvrdzovací odkaz platný 24 hodín.
4. Odkaz otvorí potvrdzovaciu stránku. Účet vytvorí až výslovné stlačenie
   tlačidla „Potvrdiť účet“, nie automatické načítanie odkazu e-mailovým
   skenerom.
5. Potvrdenie označí e-mail ako overený, vytvorí reláciu pre dané zariadenie a
   presmeruje používateľa do onboardingu. Opakované odoslanie nevytvorí druhý
   účet ani druhú identitu.

Verejná odpoveď nepovie, či e-mail už existuje. Ak má e-mail aktívny účet,
používateľ dostane správu s odkazom na prihlásenie alebo obnovu hesla.

## 2. Prihlásenie heslom

Prihlasovacia obrazovka obsahuje e-mail, heslo, tlačidlo „Prihlásiť sa“, odkaz
„Zabudol som heslo“ a voliteľné tlačidlo passkey. Server porovná heslo s
Argon2id hashom a pri úspechu pridá novú reláciu. Nezmaže relácie ostatných
zariadení.

Heslo má 10 až 128 znakov. Server prijme medzery a Unicode, heslo automaticky
neupravuje a nikdy ho nezapisuje do logu. Chybová odpoveď znie rovnako pri
neznámom e-maile aj nesprávnom hesle.

## 3. Relácie a zariadenia

Každé zariadenie dostane vlastný náhodný token. Databáza uloží iba jeho hash.
Web a PWA používajú cookie `Secure`, `HttpOnly` a `SameSite=Lax`.

- relácia sa používaním posúva na 90 dní;
- databáza obnoví expiráciu najviac raz za 24 hodín, aby zbytočne nezapisovala;
- odhlásenie zruší iba aktuálne zariadenie;
- profil zobrazí aktívne zariadenia, ich posledné použitie a tlačidlo na
  odobratie;
- používateľ môže jedným krokom odhlásiť všetky ostatné zariadenia;
- zmena alebo obnova hesla zruší všetky relácie okrem práve overenej relácie.

## 4. Obnova hesla

Používateľ zadá e-mail. Server vždy vráti rovnakú verejnú odpoveď. Ak účet
existuje, odošle jednorazový odkaz platný 60 minút. Odkaz otvorí formulár na
nové heslo. Po úspechu server zneplatní token, uloží nový Argon2id hash a zruší
ostatné relácie.

Token na potvrdenie registrácie nemožno použiť na obnovu hesla a naopak. Každý
token má účel, hash, expiráciu a jednorazové použitie.

## 5. Passkey, Face ID a odtlačok

Passkey je voliteľný. Používateľ ho pridá až po overení účtu. Rozhranie ho
pomenuje zrozumiteľne: „Prihlásiť odtlačkom, Face ID alebo PINom“.

Server používa WebAuthn s RP ID `uvar.si`, originom `https://uvar.si` a
požiadavkou `userVerification=required`. Uloží identifikátor poverenia,
verejný kľúč, počítadlo podpisov, transporty, názov zariadenia a čas použitia.
Biometrické údaje ostávajú v zariadení; Uvar.si ich nikdy neprijme.

Účet môže mať viac passkeys. Profil ich zobrazí a dovolí odobrať. Prihlásenie
heslom ostane vždy dostupné. Ak prehliadač WebAuthn nepodporuje, tlačidlo sa
nezobrazí a zvyšok prihlásenia funguje bez zmeny.

## 6. Migrácia existujúceho používateľa

Existujúci účet, plány, špajza a Premium zostanú nedotknuté. Aktívna relácia
ostane platná. Prihlásený používateľ uvidí v profile výzvu „Nastaviť heslo“ a
môže ho nastaviť bez ďalšieho e-mailu, pretože svoj e-mail už overil magic
linkom.

Odhlásený existujúci používateľ požiada o jeden posledný e-mail „Nastaviť
heslo“. Po jeho použití sa prihlasuje ako ostatní. Starý magic-link endpoint
zostane počas migrácie dostupný iba na nastavenie alebo obnovu hesla; nebude
hlavným prihlasovacím formulárom.

## 7. Dáta a rozhrania

Nové tabuľky:

- `auth_credentials`: používateľ, Argon2id hash, čas zmeny;
- `auth_action_tokens`: hash tokenu, účel, e-mail alebo používateľ, expirácia;
- `auth_passkeys`: credential ID, verejný kľúč, sign count, transporty a názov;
- `auth_webauthn_challenges`: jednorazová challenge, účel a krátka expirácia.

`sessions_v2` dostane údaje o vytvorení, poslednom použití, expirácii a
zrozumiteľnom názve zariadenia. Existujúce hashované session tokeny ostanú
platné.

Verejné endpointy:

- `POST /api/auth/register`
- `POST /api/auth/confirm`
- `POST /api/auth/login`
- `POST /api/auth/password/request`
- `POST /api/auth/password/reset`
- `POST /api/auth/passkey/login/options`
- `POST /api/auth/passkey/login/verify`

Prihlásené endpointy:

- nastavenie alebo zmena hesla;
- `POST /api/auth/passkey/register/options` a
  `POST /api/auth/passkey/register/verify`;
- zoznam a odobratie passkey;
- zoznam a odobratie relácií.

## 8. Bezpečnosť a prevádzka

- Argon2id parametre sa zvolia podľa času a pamäte dostupnej na Hetzneri a
  overia sa benchmarkom; hash musí obsahovať svoje parametre pre budúci rehash.
- Prihlásenie, registrácia, reset a WebAuthn challenge majú limity podľa IP aj
  účtu. Opakované zlyhania zavedú rastúce krátke oneskorenie, nie trvalý lock.
- Server overí `Origin` pri stavových auth požiadavkách a zachová ochranu
  cookie pred skriptmi.
- Tokeny, heslá, passkey challenges, cookies a e-mailové odkazy sa nelogujú.
- Všetky migračné kroky sú aditívne a idempotentné.
- Nasadenie používa prepínač. Nové API sa spustí skôr než nové rozhranie, aby
  neexistovalo okno, v ktorom sa používateľ nevie prihlásiť.

## 9. Chybové stavy

Výpadok e-mailu ponechá čakajúcu registráciu obnoviteľnú a zobrazí jasnú správu.
Prerušené potvrdenie možno bezpečne zopakovať. Chyba WebAuthn vráti používateľa
na heslo bez zrušenia relácie. Zlyhanie migrácie zachová starý spôsob obnovy a
nikdy nezmaže existujúce účty ani sessions.

## 10. Testy a prijatie

- registrácia vytvorí účet až po potvrdení e-mailu;
- heslo sa nikdy neuloží ani nevypíše v otvorenom tvare;
- nesprávne heslo a neznámy e-mail majú rovnakú odpoveď;
- druhé zariadenie nezruší prvé;
- relácia sa obnoví, ale databázu nezapisuje pri každej požiadavke;
- reset hesla zruší ostatné relácie a token nemožno použiť dvakrát;
- existujúci účet nastaví heslo bez straty dát;
- passkey registrácia a prihlásenie overia challenge, origin, RP ID a sign count;
- nepodporovaný WebAuthn ponechá funkčné heslo;
- prihlásenie funguje na mobile, desktope a v nainštalovanej PWA;
- celý regresný balík prejde pred zapnutím nového rozhrania.

Produkčný test použije samostatný testovací účet. Platby ostanú vypnuté.
