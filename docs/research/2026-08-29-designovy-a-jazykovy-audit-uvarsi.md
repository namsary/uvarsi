# Uvar.si — dizajnový a jazykový audit

**Dátum:** 29. august 2026  
**Rozsah:** mobilná aplikácia, recept, nákup, špajza, chybové stavy a slovenský text  
**Cieľ:** platený produkt, nie interný prototyp

## Verdikt

Uvar.si má zapamätateľnú identitu, ale priveľa prvkov používa naraz rovnaký
vizuálny dôraz: hrubý čierny rám, žltú plochu, tvrdý tieň, verzálky a monospace.
Výsledok pripomína prototyp alebo retro web. Produkt netreba prerobiť na
generický SaaS; treba zachovať svet papierových cenoviek a zaviesť disciplínu.

Receptová slovenčina je dnes väčšie reputačné riziko než farby. Jedna formulácia
typu „stehenných reziek“ alebo tabuľková dávka `1 980 g` stačí, aby používateľ
prestal veriť aj správnym cenám.

## P0 pred plateným spustením

1. Interaktívne prvky musia byť skutočné tlačidlá a checkboxy, s viditeľným
   focusom, stavom `aria-expanded` a dotykovou plochou najmenej 44 × 44 px.
2. Pri dlhom skladaní plánu treba ukázať fázu práce, ponechať posledný platný
   plán a chybu zobraziť v kompaktnom paneli. Prázdna chybová stena pôsobí ako
   pokazená aplikácia.
3. Recept musí vizuálne oddeliť:
   - množstvo, ktoré sa použije,
   - základ a koreniny, ktoré treba skontrolovať doma,
   - produkt z akcie a jeho pôvod,
   - počet a veľkosť balení na nákup.
4. „Máš doma“, „čiastočne pokryté zo špajze“ a „už kúpené“ musia mať rozdielne
   symboly aj texty. Prečiarknutie nesmie znamenať tri rôzne veci.
5. Malá červená typografia potrebuje tmavší odtieň s kontrastom aspoň 4,5 : 1.

## Jazykový štandard

- Jednotne 2. osoba jednotného čísla: `prepláchni`, `nakrájaj`, `opeč`,
  `premiešaj`, `dochuť`, `podávaj`.
- Kanonický názov produktu sa v zozname neskloňuje. Bezpečný formát je
  `900 g · kuracie stehenné rezne`; správny genitív je `stehenných rezňov`.
- Gramy a mililitre sa zobrazujú v prirodzených kuchynských krokoch. Zlomky sú
  vhodné pri ČL/PL, nie pri desatinách gramu.
- Každá surovina z postupu patrí do jednej zo skupín `z akcie`, `zo špajze`
  alebo `skontroluj doma`.
- Ryža má pomenovanú metódu, množstvo vody, čas a stav hotovosti. Uvar.si používa
  absorpčnú metódu; neurčité `uvar a sceď` sa nepublikuje.
- Jedno číslo nehrá dve úlohy: `použiješ 300 g` nie je `kúp 300 g`.

## Odporúčaný smer: moderná trhová cenovka

- papier `#FFF9ED`, tmavá lesná `#17342A`, paradajková `#C93427`, horčicová
  `#F3C928`, jemná linka `#DED5C3`;
- Anton iba pre logo a hlavný titul obrazovky;
- Manrope pre text, tlačidlá a formuláre;
- IBM Plex Mono iba pre ceny, dátumy, obchody a platnosť;
- bežné karty biele, 1 px hranica, jemný polomer, bez tvrdého tieňa;
- hrubý rám a žltá len pre jeden podpisový prvok alebo primárnu akciu;
- jednotná SVG ikonografia namiesto zariadením menených emoji;
- aktívna záložka má ikonu, text aj tvarový indikátor, nie iba inú farbu.

## Poradie implementácie

1. funkčné a prístupné interakcie;
2. receptová hierarchia a tri druhy množstiev;
3. jednotné načítavacie a chybové stavy;
4. vizuálne tokeny a ikonografia;
5. jemné animácie, história navigácie, tablet a PWA detaily.

Plný vizuálny redizajn má nasledovať až po overení P0 logiky. Inak by nový
povrch iba zakryl staré chyby.
