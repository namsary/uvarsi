# Uvar.si SEO/GEO Design

**Dátum:** 2026-08-25  
**Rozsah:** slovenský trh a slovenský jazyk počas najbližších 80 dní  
**Stav:** schválený smer — technický základ a malý dôveryhodný obsahový klaster

## Cieľ

Zvýšiť organickú objaviteľnosť Uvar.si v klasickom vyhľadávaní aj v odpovediach generatívnych vyhľadávačov bez masovej výroby tenkého AI obsahu. Verejné stránky majú privádzať ľudí s konkrétnym problémom „čo variť lacno tento týždeň“ do existujúcej aplikácie. Platby ani entitlement logika sa týmto vydaním nemenia.

## Východiskový stav

- Landing je ľahký (približne 13 kB po gzip) a má titulok, description, canonical a jeden H1.
- Chýbajú `robots.txt`, `sitemap.xml`, Open Graph/Twitter metadáta a JSON-LD.
- `/app` a `/prihlasenie` nemajú `noindex` a môžu sa dostať do indexu.
- `www.uvar.si` vracia duplicitu namiesto trvalého presmerovania na `uvar.si`.
- Verejný web nemá indexovateľné interné obsahové stránky.
- Google už pozná staršiu verziu landing page, preto treba po nasadení požiadať o nové prehľadanie.
- Caddy má deklarovanú dlhú cache fontov, ale živá odpoveď ju nevracala; release musí hlavičku overiť na produkcii.

## Zásady

1. Indexujeme len verejný, užitočný a pravdivý obsah. Aplikácia, prihlásenie a API nie sú akvizičné stránky.
2. Aktuálne ceny a jedlá sa zobrazia iba po úspešnej validácii `landing_data.json`; pri starých alebo chybných dátach stránka nesmie tvrdiť, že ide o aktuálny týždeň.
3. Štruktúrované dáta musia presne zodpovedať viditeľnému obsahu. Bez vymyslených hodnotení, úspor, recenzií alebo cien.
4. GEO nie je samostatný trik. Použijeme priamu odpoveď, konkrétne dátumy, zdroje, metodiku, aktualizáciu a jasne pomenovanú úlohu AI.
5. Nevytvárame stovky programatických receptových alebo produktových stránok. Najprv tri kvalitné stránky a meranie výsledku.
6. Žiadny nový ťažký JavaScript, chat widget ani externý font. Obsah musí byť dostupný v počiatočnom HTML.

## Verejná informačná architektúra

### `/`

Domovská a konverzná stránka. Získa navigáciu na obsahové stránky, Open Graph/Twitter metadáta a JSON-LD typu `WebSite` + `SoftwareApplication`. `SoftwareApplication` nebude obsahovať cenu, kým nie je finálna verejná platená ponuka aktívna a viditeľná.

### `/co-varit-tento-tyzden`

Hlavná týždenná akvizičná stránka. Server ju vykreslí do kompletného HTML z už validovaného `landing_data.json`.

Viditeľné minimum:

- priama odpoveď v prvom odseku,
- presný rozsah platnosti,
- jedlá a ich overené akciové položky,
- obchod, akciová cena, pôvodná cena iba ak je validná, jednotka,
- odkazy na zdroje a strany letákov,
- čas poslednej aktualizácie,
- vysvetlenie, že AI navrhuje kombináciu/recept, ale ceny pochádzajú z uvedených zdrojov,
- CTA do aplikácie.

Ak dáta nie sú aktuálne, stránka vráti dočasný stav HTTP 503 s `Retry-After`, zobrazí neutrálny stav „aktuálny výber obnovujeme“ a nevypíše staré ceny ako dnešné. Tým nepošle crawleru falošný úspech ani pokyn na trvalé vyradenie URL.

### `/lacny-jedalnicek`

Evergreen stránka vysvetľujúca plánovanie lacného týždňa pre slovenskú domácnosť, dávkové varenie, počet porcií a prácu so špajzou. Bez konkrétnych časovo nestabilných cien. CTA smeruje na aktuálnu týždennú stránku a do aplikácie.

### `/ako-varime-z-akcii`

Metodika a dôvera: ktoré reťazce aktuálne pokrývame, ako kontrolujeme dátumy a ceny, čo robí AI, čo overuje program a čo sa stane pri chýbajúcich dátach. Stránka nesmie sľubovať absolútnu úplnosť všetkých ponúk.

## Crawl a indexácia

- `robots.txt` povolí verejné stránky pre všeobecné crawlery aj `OAI-SearchBot`.
- Robots zablokuje `/api/` pre crawl, ale nebude blokovať `/app` ani `/prihlasenie`, pretože crawler musí vedieť prečítať ich `noindex`.
- `/app` a `/prihlasenie` vrátia `X-Robots-Tag: noindex, nofollow, noarchive` a rovnaký meta robots fallback.
- `sitemap.xml` obsahuje iba `/`, `/co-varit-tento-tyzden`, `/lacny-jedalnicek` a `/ako-varime-z-akcii` s kanonickými HTTPS adresami. Týždenná stránka dostane `lastmod` podľa validovaných dát.
- `www.uvar.si`, `uvarsi.sk`, `www.uvarsi.sk` a pomocná sslip adresa sa trvalo presmerujú na zodpovedajúcu cestu na `https://uvar.si`.

## Štruktúrované dáta

- Domovská stránka: `WebSite` a `SoftwareApplication`/`WebApplication`.
- Obsahové stránky: `WebPage` alebo `Article` a `BreadcrumbList`.
- Týždenná stránka uvedie `dateModified` len z reálneho dátumu dát.
- `Recipe` sa nepoužije, kým stránka nemá plný viditeľný recept a povinné dôveryhodné polia.
- FAQ schema nie je priorita; viditeľné FAQ ostáva užitočné, ale Google jeho rich results pre bežné komerčné weby spravidla nezobrazuje.

## Výkon a cache

- Zachovať inline kritické CSS; pri dnešnej veľkosti by delenie pridalo ďalší request bez istého prínosu.
- Hashované fonty a statické PWA aktíva: `Cache-Control: public, max-age=31536000, immutable`.
- HTML a dynamické SEO stránky: krátka cache alebo revalidácia, aby sa týždenné dáta nezasekli.
- API, app a prihlasovanie: `no-store` alebo existujúce bezpečné správanie; žiadna shared cache používateľských odpovedí.
- Zachovať gzip a nepridávať render-blocking externé závislosti.
- Koreňový service worker musí `/co-varit-tento-tyzden`, `robots.txt` a `sitemap.xml` vždy pustiť priamo do siete. Stale-while-revalidate by pri týždennom obsahu mohol vrátiť staré akcie.

## Meranie a prevádzka

- Release gate overí HTTP 200, canonical, robots, sitemapu, `noindex` na súkromných obrazovkách, cache fontu a prítomnosť aktuálneho týždňa na verejnej stránke.
- Oba deploy mechanizmy (`nasad.ps1` aj autonómny `hetzner/samopull.sh`) musia preniesť nový serverový modul a aktuálny koreňový service worker.
- Po nasadení sa doména pridá/overí v Google Search Console a Bing Webmaster Tools, odošle sa sitemap a vyžiada sa nové prehľadanie `/` a `/co-varit-tento-tyzden`.
- Analytics rozlíši organické registrácie a referral návštevy z Google, Bing a ChatGPT. Implementácia analytického providera nie je súčasťou tohto vydania, kým nie je zvolený privacy režim a consent riešenie.

## Čo sme prevzali z článku Postoja

Článok správne zdôrazňuje, že AI môže auditovať technické parametre webu a že tie môžu byť dôležitejšie než samotné množstvo AI textu. Praktický preklad pre Uvar.si je tento audit, testované technické opravy a kvalitné aktuálne stránky. Neberieme z neho žiadny neoverený ranking claim; implementácia sa riadi oficiálnymi pravidlami Google a OpenAI.

## Akceptačné kritériá

- Všetky existujúce testy a nové SEO/GEO testy prejdú.
- Platby zostanú vypnuté a platobný kód sa nezmení.
- Žiadna verejná stránka nezobrazí staré ceny ako aktuálne.
- `/app` a `/prihlasenie` sú `noindex` v HTML aj hlavičke.
- `robots.txt` a `sitemap.xml` sú dostupné bez prihlásenia.
- Všetky štyri indexovateľné URL majú unikátny title, description, canonical a jeden H1.
- JSON-LD je validný JSON a zodpovedá viditeľnému obsahu.
- Alternatívne hostnames sa presmerujú na `https://uvar.si` bez straty cesty.
- Hashovaný font má na produkcii immutable cache hlavičku.
