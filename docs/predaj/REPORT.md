# Uvar.si – správa o príprave na predaj

Vetva `lh-predaj`, rozsah commitov `4b79408`..`8f6a362` (nad `56324b4`). Všetko je **lokálne**; nič sa nenasadilo, nič sa nepushlo, platby ostávajú vypnuté (`PLATBY_ZAPNUTE=0`) a platobný tok, zber letákov (`app/zbierac_akcii.py`) ani nákladové stropy (`app/naklady.py`) sa nemenili.

> **`LOCAL PASS` neznamená nasadené ani verejne funkčné.** Znamená iba, že cesta prešla na lokálnom serveri s dočasnou databázou a so zverejnenými shimmi (pozri časť 6). Produkciu nikto netestoval.

Zdroje faktov: git commity (hash), pracovné záznamy z rôznych kôl v scratchpade (`grading.md`, `gates.md`, `c1_legal.md`, `c3_contact.md`, `b_review.md`, logy `*_before.log` / `*_after.log` / `*suite.log`), `docs/prevadzka.md` a `agents/uvarsi-release-gatekeeper/SKILL.md`. Čo sa z nich nedá doložiť, je výslovne označené ako neoverené.

## 1. Čo sa zmenilo

| Commit | Zhrnutie |
|---|---|
| `4b79408` | Screenshoty pred zmenou (375 px a 1280 px) v `docs/predaj/screenshots/before/` |
| `a05264b` | Test end-to-end: návšteva → prihlásenie → plán → nákupný zoznam (`tests/test_customer_flow_e2e.py`) |
| `be2b1d5` | `/api/health` hlási `stav` a `problemy` (C4) |
| `898891c` | Prvý pokus o opravu E1; nahradený commitom `63b8cd0` |
| `63b8cd0` | Oprava E1: správa o nedostupnom režime pomenuje skutočnú príčinu |
| `53d65eb` | Oprava E2: Nákupný zoznam bez plánu ponúka ďalší krok |
| `ab22e7f` | Oprava V0: staré prihlásenie netvrdí, že e-mail určite odišiel |
| `37a1c24` | Oprava N1: formulár zakladajúcej ponuky nehlási úspech bez odoslania |
| `72a8c7c` | Oprava N1: formulár posiela MailerLite pôvodný formát (urlencoded) |
| `2c499ac` | Screenshoty po zmene (stavy zákazníckych ciest) |
| `bead3f9` | Právne stránky: dotykové plochy aspoň 44×44 px (T7) |
| `0ff0b09` | Snímky `/vop` po oprave T7 |
| `177219a` | Appka: dotykové plochy a čitateľné písmo (T8–T10, F7–F11, A3) |
| `13e9369` | Snímky po oprave `177219a` |
| `fa5a1e5` | Posledné dotykové plochy v appke aspoň 44 px |
| `e6a4944` | Snímky po oprave `fa5a1e5` |
| `c25525e` | Landing: kontakt na podporu v chybovom stave formulára (C3) |
| `8f6a362` | `docs/prevadzka.md`: zálohy, monitoring a postup obnovy (C5) |
| `71afe09` | Tento report |
| (nasledujúci commit) | Oprava reportu: úplný zoznam `[DOPLNIŤ]` z `docs/prevadzka.md`, upresnenie citácií; oprava čísla riadku v `docs/prevadzka.md` |

Právne stránky (C1) a ukladanie v prehliadači (C2) neviedli k zmene kódu (pozri časť 2). Pred a po screenshoty sú v `docs/predaj/screenshots/before/` a `docs/predaj/screenshots/after/` (s `README.md`).

Sada testov na konci: `4577 passed, 290 skipped` (log `scratchpad/c5/suite.log` pri `8f6a362`; východisko pred zmenami bolo `4531 passed, 290 skipped`). 290 preskočených testov som neskúmal.

## 2. Nájdené chyby, opravy a regresné testy

Ku každej oprave patrí regresný test, ktorý pred opravou padal (potvrdili to kontroly v jednotlivých kolách; samostatný `*_before.log` v scratchpade existuje len pri niektorých opravách), a po oprave prešla celá sada pred commitom.

| ID | Chyba | Oprava | Regresný test |
|---|---|---|---|
| C4 | `/api/health` nehlásil degradovaný stav, keď worker nebeží | pole `stav` (`ok`/`degradovane`) a `problemy` (`worker_nebezi`, `fronta_zaseknuta`, `chybaju_data_tyzdna`); HTTP stav zostáva 200 | `tests/test_health_degradovany_stav.py` (pred opravou `KeyError: 'stav'`) – `be2b1d5` |
| E1 | Pri uložení nastavení bez dát týždňa appka hlásila „zvolený jedálniček neposkladáme. Pridaj obchod…“ (zlá príčina a zlá rada) | `me()` vracia dôvod prázdnych režimov, appka ukáže správu podľa skutočnej príčiny – `63b8cd0` | `tests/test_onboarding_dovod_prazdnych_rezimov.py`; pri tejto oprave sa minimálne upravil `tests/test_onboarding_prazdne_data_sprava.py`, lebo kódoval nesprávne správanie |
| E2 | Záložka Nákup bez plánu: „Najprv si vygeneruj plán.“ bez ďalšieho kroku | karta „Zoznam vznikne, keď bude hotový jedálniček.“ + tlačidlo na jedálniček – `53d65eb` | `tests/test_zoznam_bez_planu_ma_cestu.py` |
| V0 | Pri `UVARSI_AUTH_V3=0` dostala neznáma adresa „Žiadosť je prijatá“, hoci sa nič neposlalo a účet nevznikol | pravdivý text karty – `ab22e7f` | `tests/test_prihlasenie_bez_uctu_pravdiva_sprava.py` |
| N1 | Formulár zakladajúcej ponuky ukázal „Zapísali sme ťa“ po pevných 400 ms bez ohľadu na výsledok | odoslanie cez `fetch`, úspech až po dokončení, viditeľná chyba pri zlyhaní (`37a1c24`); telo v pôvodnom formáte urlencoded (`72a8c7c`) | `tests/test_landing_formular_pravdivy_stav.py`, `tests/test_landing_formular_mailerlite_format.py` |
| T7 | Odkazy v hlavičke, kontakte a pätičke právnych stránok mali 15–17 px na výšku | min. 44×44 px – `bead3f9` | `tests/test_legal_pages_dotykove_plochy.py` |
| T8–T10, F7–F11, A3 | Odkazy prihlásenia a pätičky appky pod 44 px, písmo spodnej navigácie, štítkov Premium, množstiev a platnosti pod 11 px, pole E-mail na starom prihlásení bez labelu | `177219a` | `tests/test_app_dotykove_plochy_a_pismo.py` (pred opravou 4 failed) |
| (zvyšok T10) | Posledné prvky appky pod 44 px (VOP v pätičke, „Odhlásiť toto zariadenie“, odkaz podpory v Profile) | `fa5a1e5` | `tests/test_app_posledne_dotykove_plochy.py` |
| C3-1 | Chybová hláška formulára na landingu (v modálnom okne, ktoré robí pätičku nekliknuteľnou) nemala cestu k podpore | text s odkazom `mailto:pumaragency@gmail.com` – `c25525e` | `tests/test_landing_chyba_formulara_kontakt.py` (2 testy, pred opravou 2 failed) |

Test end-to-end hlavného toku (`a05264b`, `tests/test_customer_flow_e2e.py`, 4 testy) používa FastAPI `TestClient` a podvrhnutého poskytovateľa pošty, takže sa neposiela skutočný e-mail.

**Bez opravy (s dôvodom):**
- **C2 – úložisko pred súhlasom:** žiadna oprava netreba. `index.html` nepoužíva `localStorage`, `sessionStorage` ani `document.cookie`. V `app/static/app.html` sa kľúče píšu až po prihlásení alebo po akcii zákazníka (`uvarsi_profil` v `rememberProfile()` len po kontrole prihlásenia; `uvarsi_done:*`, `uvarsi_kupim:*` a `uvarsi.password-setup-dismissed.v1:<id>` po akcii) a pri odhlásení a zmazaní účtu sa čistia. Právne dôsledky pozri v časti 5.
- **Prekrytie spodnej navigácie na stránke plánu:** nereprodukuje sa. Meranie vykresleného rozloženia (`measure.js`) ukázalo, že obsah nie je prekrytý; navigácia v strede stránky na screenshotoch celej stránky je artefakt snímania. Safe-area inset sa nemeral, ale CSS ukazuje, že prekrytie nespôsobí.
- **C1 – právne stránky:** žiadna oprava netreba. Všetkých 5 ciest (`/vop`, `/ochrana-osobnych-udajov`, `/cookies`, `/odstupenie`, `/reklamacie`) vracia 200 aj v tvare `/pravne/*.txt` a každá pätička (landing, appka, právne stránky) má všetkých 5 odkazov aj `mailto`. Existujúce testy (`tests/test_legal_pages.py:31-32`, `:204`) zakazujú `[DOPLNIŤ]` na verejných stránkach, preto chýbajúce údaje idú len do tejto správy.
- **C5 – prevádzka:** `8f6a362` pridáva do `docs/prevadzka.md` časť o zálohách, monitoringu a obnove (iba dokumentácia, na serveri sa nič nespúšťalo; existujúci runbook auth-v3 zostal).

**Otvorené položky (nevyriešené):**
- **A1** – položky nákupného zoznamu sa nedajú odškrtnúť klávesnicou (`div.item` bez `tabindex`/`role`). Zdroj: `b_review.md`.
- **A2** – frekvencia varenia (`div.chip`) ide len myšou. Zdroj: `b_review.md`.
- **A5** – vypnuté Premium režimy stravovania nemajú vysvetlenie pre čítač ani klávesnicu. Zdroj: `b_review.md`.
- **A7** – správa pri HTTP 404 odkazuje na `pomoc@uvar.si`, ktorá sa inde v repozitári nevyskytuje (kontakt je všade inde `pumaragency@gmail.com`); adresu musí potvrdiť Martin.
- **Dôvod pre A1, A2, A5:** rozpočtový konflikt. Gzip rezerva je **0 B**: `app.html` má 37 600 B z limitu 37 600 B (`fa5a1e5`), úvodná stránka 12 100 B z praktického limitu 12 100 B (`c3_contact.md`). Merania v `b_review.md` odhadli A1 na +15 B a A2 na +17 B (len atribúty, bez klávesových handlerov), A5 na +30–60 B. Stropy a testy rozpočtu som nemenil ani nezvyšoval. Pre A7 je dôvodom neoverená adresa, nie rozpočet.
- **T4** – samostatné odkazy v pätičke landingu (15 px na výšku, vrátane `pumaragency@gmail.com`) a odkaz v chybovej hláške formulára (18 px, inline) sú pod 44 px. Oprava by potrebovala gzip rezervu, ktorú landing nemá (`c3_contact.md`).
- **Písmo pod 12 px:** na landingu ostáva, napr. `.hero-note` 11,52 px, `.step-tag` 10,72 px, `.proof-*` 10,88 px (`b_review.md`; po tomto meraní sa `index.html` menil len v N1 a C3, ktoré tieto štýly neriešili). V appke `177219a` zväčšil nav, štítky Premium, množstvá a platnosť (merané pri oprave na 12 px, resp. 11,5 px pri štítku Premium podľa `b_review.md`). Rozhodnutie, ktoré písmo pod 12 px je prijateľné, nepadlo.
- **Kontrast** (`b_review.md`): `.step-tag` na landingu 3,97:1 a ukážkové odškrtnuté položky zamknutej Špajze 2,89:1 (< 4,5:1) – neopravené (rezerva).
- **Desktop 1280×800:** tlačidlo „Vyskúšať Uvar.si“ je pod okrajom prvého výrezu (`b_review.md`); neopravené.
- **N2/N5, N4 a ďalšie nízke nálezy** zo `grading.md` (disabled Premium diéty bez vysvetlenia; neúspešná registrácia pri výpadku poskytovateľa nechá nepoužitý token; pri chýbajúcom `RESEND_API_KEY` hlási reset hesla „E-mail je na ceste“, hoci worker nič neposiela) – neopravené, len zdokumentované.
- **Neobjasnená anomália:** pri súbežnom behu sondy zostala jedna úloha obnovy hesla v stave `skipped` (attempts=1); sekvenčné opakovania ju nereprodukovali (`grading.md`).
- **Možná medzera zberu (nezistené behom):** `app/zbierac_akcii.py:108` má `MAX_PAGES = 200`; či jeho dosiahnutie zlyhá nahlas, sa neskúšalo (`grading.md`).

## 3. Zákaznícke cesty podľa gatekeepera

Verdikty podľa `agents/uvarsi-release-gatekeeper/SKILL.md`. Používajú sa len `BLOCKED` a `LOCAL PASS`. **`LOCAL PASS` ≠ nasadené ani verejne funkčné.**

| Gate | Verdikt | Dôvod (zdroj: `grading.md`, `gates.md`) |
|---|---|---|
| Flyer → database | **BLOCKED** | Skutočný zber proti živým letákom sa nesmie spúšťať (žiadne scrapovanie). Spustili sa len testy s fixtúrami (231 passed). Martin musí spustiť zber na serveri a overiť počty strán. |
| Current data | **BLOCKED** | Zdravý týždeň, zastaraný týždeň aj ponuky bez zdroja sa lokálne správajú pravdivo (bez plánu a bez cien z minulého týždňa; v kontrolovaných behoch nevznikol žiadny LLM klient – dôkaz neúplný, pozri časť 6). Verdikt zostáva `BLOCKED`: E1 a E2 sú overené len testom, prah „dostatok ponúk“ sa neskúšal a živé dáta nikto nevidel. |
| Landing receipt | **LOCAL PASS** | Bloček sa vykresľuje z `/api/public/landing`, bez dát sa zobrazí „Aktuálne ceny práve obnovujeme“ a žiadna úspora; testy dát, aritmetiky a atomického zápisu prešli (96 passed). Atomická výmena sa overila len testami, nie na skutočnom docroote. Použité hodnoty bločku boli testovacie, nie skutočné ceny. |
| Login | **BLOCKED** | Závisí od `UVARSI_AUTH_V3`. Pri `=1` sa lokálne overilo `LOCAL PASS` (presný hostiteľ `https://uvar.si`, jasná platnosť, použitie len raz, pravdivá chyba pri výpadku poskytovateľa). Pri `=0` (predvolená hodnota v kóde, `app/server.py:811-812`) nový zákazník nemá žiadnu cestu na registráciu. Produkčná hodnota je `[DOPLNIŤ]`. |
| Plan, recipes, pantry | **LOCAL PASS** | Free aj Premium používateľ pri 375 aj 1280 px: preferencie sa uložia a po obnovení ostanú, špajza (Premium) sa uloží, plán, detail receptu a nákupný zoznam podľa obchodov fungujú; pri zastaraných dátach plán odmietne. Špajza je pre Free zámerne zamknutá. |
| Paid plan | **BLOCKED** | Platby sú vypnuté (`PLATBY_ZAPNUTE=0`) a tok sa nesmie meniť, takže checkout, zrušenie ani vrátenie sa nedali preskúšať. Pozorované: `platby_zapnute:false`, „Platby zatiaľ nie sú spustené.“, VOP, odstúpenie a reklamácie dostupné, podpora v pätičke a v Profile. |

**Zákaznícke cesty zo zadania → gate:**

| Cesta | Gate | Výsledok |
|---|---|---|
| aktuálne dáta týždňa | Current data | `BLOCKED` |
| bloček na landing page | Landing receipt | `LOCAL PASS` |
| prihlásenie (magic link) | Login | `BLOCKED` (`=0`); pri `=1` lokálne `LOCAL PASS` |
| uloženie špajze/preferencií | Plan, recipes, pantry | `LOCAL PASS` (Free: len preferencie; Premium: aj špajza) |
| generovanie plánu | Plan, recipes, pantry + Current data | `LOCAL PASS` pri aktuálnych dátach; pri zastaraných dátach pravdivé odmietnutie |
| recepty | Plan, recipes, pantry | `LOCAL PASS` |
| nákupný zoznam | Plan, recipes, pantry | `LOCAL PASS`; bez plánu ponúka cestu na jedálniček (E2) |
| stav „dáta nie sú k dispozícii“ | Current data (+ Landing receipt) | `BLOCKED`; landing časť `LOCAL PASS`. Kontakt v tomto stave je len v pätičke (pozri časť 6) |

## 4. Zoznam `[DOPLNIŤ]`

**Z právnych podkladov (`c1_legal.md`):**
- DIČ – `docs/legal/00_PRAVNY_AUDIT_UVARSI.md:32-33`, `02_…NAVRH.md:13`, `04_CHECKLIST_PRED_PLATBAMI.md:23-24`; nezverejňuje sa zámerne.
- IČ DPH – tie isté riadky; závisí od overeného stavu platiteľa DPH.
- Stav mikropodniku (výnimka zo zákona o prístupnosti) – `04_…:25-26`.
- Riadok pre kľúč `uvarsi.password-setup-dismissed.v1:<id>` do tabuľky úložiska (v `app/legal_pages.py:405-412` ani v `03_…:20-24` sa nevyskytuje, pritom ho appka zapisuje – `app/static/app.html:1859-1874`).
- Zosúladenie `docs/legal/` s publikovaným znením (drafty 02 a 03 majú verziu `2026-09-07-v1`, publikované `2026-09-12-v5`; `02_…:13` tvrdí, že telefón sa nezverejňuje, hoci sa uvádza na právnych stránkach).
- Označené `[OVERIŤ]`: hosting v EHP a štandardné zmluvné doložky sú na verejnej stránke uvedené ako fakt (`app/legal_pages.py:316-323`), podklady ich vedú ako neoverené; DPA a postavenie MailerLite.

**Z prevádzky (`docs/prevadzka.md`, `8f6a362`):**
- príjemca upozornení ntfy a postup mimo pracovnej doby,
- obnova nárokov (entitlements) vytvorených po poslednej zálohe,
- RPO a RTO (cieľové hodnoty nie sú nikde určené) – `docs/prevadzka.md:773`,
- kópia zálohy mimo servera: kam, ako často, šifrovanie – `docs/prevadzka.md:765`, `:657`,
- doložený test obnovy zo zálohy a jeho frekvencia – `docs/prevadzka.md:766`, `:657-658`,
- zálohovanie `landing_data.json`, súborov prostredia a konfigurácie Caddy – `docs/prevadzka.md:767`, `:742`,
- či pri obnove treba zastaviť aj `uvarsi-plan-worker` – `docs/prevadzka.md:768`, `:728`,
- externý monitor `/api/health` (čítanie `stav`/`problemy`), jeho nástroj a interval – `docs/prevadzka.md:769-770`, `:696`.

**Konfigurácia:**
- Produkčná hodnota `UVARSI_AUTH_V3` (kód predvolene `0`; pri inom ako `1` nový zákazník nemá spôsob registrácie).

Vyplnené a zhodné vo všetkých zdrojoch: PUMAR s. r. o., IČO 57 370 591, sídlo, register (Mestský súd Košice, Sro, 64515/V), e-mail, telefón (`c1_legal.md`).

## 5. Čo musí urobiť človek (Martin)

1. **Nastaviť `UVARSI_AUTH_V3`** na produkcii a overiť. Pri `=0` nový zákazník nemá cestu na registráciu (test `a05264b`, `app/server.py:1617-1627`).
2. **MailerLite:**
   - Počas skúšania (`waitlist.js`, dvakrát) sa mohli odoslať falošné adresy `@example.com` do živého formulára MailerLite; z prostredia sa to nedalo overiť. Treba skontrolovať zoznam a vyčistiť ich.
   - Uviesť MailerLite ako sprostredkovateľa v dokumentoch o ochrane osobných údajov (zmluva o spracúvaní a postavenie sú neoverené) a skontrolovať double opt-in.
   - Oprava `72a8c7c` vracia pôvodný formát požiadavky, ale prijatie živým MailerLite nikto neskúšal (zakázané), a pri `no-cors` klient nevie, či žiadosť prijal.
3. **Rozhodnutie N3:** landing ukazuje ročný odhad „weekly úspora × 52“ (`be2b1d5:index.html:147,154`). Je označený ako príklad, ale je to extrapolácia; rozhodnúť, či je prijateľný pri pravidle „bez vymyslených čísel úspor“.
4. **Právna kontrola** (advokát):
   - `uvarsi.password-setup-dismissed.v1` chýba v tabuľkách úložiska,
   - či `uvarsi_profil` (cache na rýchlejšie vykreslenie, hodnota `{onboarding: bool}`) patrí medzi nevyhnutné,
   - texty odstúpenia a reklamácií, ktoré nemajú zdrojový draft,
   - tvrdenia o hostingu v EHP a zmluvných doložkách,
   - DIČ, IČ DPH, mikropodnik (časť 4).
5. **Napojiť monitoring na pole `stav`** v `/api/health` (zatiaľ ho nič nečíta) a doplniť údaje z časti 4 (ntfy, RPO/RTO, obnova nárokov).
6. **Potvrdiť kontaktnú adresu `pomoc@uvar.si`** (A7) alebo ju nahradiť overenou.
7. **Nasadenie a overenie na produkcii.** Nasadenie a overenie na produkcii nikto nerobil; všetky verdikty `LOCAL PASS` treba na produkcii zopakovať. Zber letákov (gate Flyer → database) a skutočná mailová cesta sa overia až tam.
8. **Rozhodnúť o otvorených položkách** A1, A2, A5, T4, písmo pod 12 px, kontrast, desktopová výzva – kvôli gzip rezerve 0 B treba buď úpravu limitov v testoch rozpočtu (rozhodnutie vlastníka), alebo nájsť úsporu.

## 6. Zverejnené obmedzenia

- **Gzip rezerva je 0 B** (`app.html` 37 600/37 600 B, úvodná stránka 12 100/12 100 B). Každá ďalšia zmena týchto súborov musí nájsť úsporu.
- **Dátum v teste:** `test_akcie_pre_delegates_selection_to_current_week_helper` padal medzi 22:00 a 24:00 UTC (porovnával `date.today()` s `bratislava_day()`); opravené v namsary/uvarsi#12.
- **Dev shimy pri lokálnych behoch** (`grading.md`): pošta zapisovaná do súboru namiesto odoslania, prepis hlavičky Origin na `https://uvar.si` (obchádza skutočnú kontrolu Origin), `/` a `/sw.js` z kópie adresára (náhrada nginx), `env -i` bez `.env`, dummy `RESEND_API_KEY` len pre worker obnovy hesla, Premium pridelené cez `platby.udel_narok_rucne` na dočasnej databáze, scenáre zastaraných dát prepísaním riadkov dočasnej DB. Tvrdenie „žiadny LLM nebol zavolaný“ vychádza zo špiónov, ktorých spustenie zvnútra bežiaceho servera sa nedokázalo; chýbajúci súbor je dôkaz, nie preukázaná skutočnosť.
- **E2 a V0 sa overili len testom**, nie v prehliadači.
- **`stav` v `/api/health`** nepokrýva čerstvosť supervízora, pole `platby.nevybavene_vratky` ani blokátory recept-enginu.
- **Kontakt v prázdnom stave („dáta nie sú k dispozícii“)** je len v pätičke (odkaz `Kontakt`, 66×44 px, 15 stlačení Tab); v samotnej hláške nie je (`c3_contact.md`). V karte V0 je „Kontakt“ len text, odkaz je v pätičke.
- **Hláška pri internej chybe** sa pri E1 stále môže zobraziť ako „neposkladáme“ a rada „Pridaj obchod“ nemusí pomôcť Free zákazníkovi.
- **`docs/prevadzka.md`** cituje `hetzner/uvarsi-plan-worker.service:28`, správny riadok je `:12` (ešte neopravené).
- **Nič z nasadenia, serverových príkazov ani živých požiadaviek** sa nespúšťalo. Skutočné doručenie e-mailu na `pumaragency@gmail.com` sa neoverovalo.
- **Neskúmané:** 290 preskočených testov v sade.
