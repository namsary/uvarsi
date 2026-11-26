# Druhá vlna overených receptov – návod na písanie

Cieľ: rozšíriť overenú knižnicu Uvar.si zo 104 na ~300 receptov.
Plán je v `docs/research/recipe-plan-wave2.txt` (dávka | id | názov | editorial lane | režimy).
Každý recept musí prejsť `tests/test_curated_recipe_wave2.py` a celou sadou testov.

## Čo vznikne pri každom recepte

1. `app/catalog/candidates/<id>.json`: recept a `source_record`, v rovnakom tvare ako `classic_pea_stew_egg.json`.
2. Riadok v `docs/research/recipe-targets-wave2.json`, pole `targets`:
   `{"id", "display_name", "editorial_lane", "expected_modes", "core": true, "references": [...], "selection_reason"}`.
   Polia `references` musia byť zhodné so `source_record.references`.
3. Povýšenie cez `promote_candidates(paths, reviewed_by="Claude (Uvar.si QA) podľa preferencií Martina", reviewed_on="<dnes>")`.
   Zapíše `<id>.review.json`, živý recept v `app/catalog/recipes/0*.json` a zdroj v `app/catalog/recipe_sources.json`.
   Ručne tieto súbory neupravuj.
4. Ak treba novú surovinu, pozri časť o surovinách nižšie.

Recepty z prvej vlny (`recipe-targets.json`, 104 kusov) nemeň.

## Zdroje

- Ku každému receptu daj aspoň dva skutočné slovenské alebo české recepty.
  Vhodné weby: varecha.pravda.sk, dobruchut.aktuality.sk, mojerecepty.sk, recepty.sk, receptyonline.cz, toprecepty.cz.
- URL over cez WebFetch alebo curl, musí vrátiť 200. `title` je skutočný názov stránky.
- `accessed_on` je dátum overenia (YYYY-MM-DD).
- Množstvá a časy ber zo zdrojov a zo zdravého kuchárskeho úsudku, nie odhadom naslepo.

## Preferencie Martina (záväzné)

- **Nič suché.**
  - Omáčky na cestoviny: smotanové okolo 80–100 ml na 80 g suchých cestovín, paradajkové 150–250 g paradajok alebo pretlaku.
  - Pri cestovinách s omáčkou daj na slot cestovín `water_ml_per_adult` (40–60) a krok:
    „Odober z hrnca {pasta.water} vody a potom cestoviny sceď cez sitko.“
    Pri spájaní s omáčkou: „Ak je omáčka hustá, prilievaj po troškách odloženú vodu z cestovín, kým bude krémová.“
  - Zapekané jedlá musia mať dosť tekutiny, aby príloha zmäkla.
  - Kuskus, bulgur a ryža musia mať správny pomer vody.
  - Šaláty potrebujú dresing: olej a kyselina.
- **Chudé mäso neprepekať.** Kuracie a morčacie prsia, bravčové karé a panenku opekaj krátko a dovar v omáčke. Dusené mäso pod pokrievkou. Pri veľkej dávke opekaj po častiach.
- **Prívarky** vždy s vajcom alebo fašírkou. Vajce je na hniličku s presným časom (6 minút od začiatku varu) alebo volské oko na masle. Vajcia vždy v celých kusoch.
- **Gulášu** patrí knedľa alebo chlieb. **Sekaná** sa pečie samostatne a podáva sa so zemiakovou kašou. Žiadne „všetko v jednom pekáči“, ak to tak Slováci nevaria.
- **Mrazená zelenina** (brokolica, šampiňóny, špenát, hrášok): kde dáva zmysel, pridaj vetu, ako ju použiť bez rozmrazenia.
- **Dochucovanie:** bylinky, ktoré k jedlu patria (majorán, tymián, petržlenová vňať, kôpor, pažítka, rasca, sladká paprika). Soľ až po ochutnaní, ak je v jedle slaný syr alebo slanina.
- **Mletá paprika** ide do hrnca až po odstavení z ohňa, inak horkne.
- **Cesnak** pridávaj až k hotovej cibuli alebo hubám, nie do studeného tuku.
- **Syr** sa sype až na tanier, keď sa jedlo skladuje na viac dní.
- **Postup** píš ako skúsený domáci kuchár: krátke, presné vety, čas a viditeľný znak hotovosti („kým zozlatne“, „kým zmäkne“).

## Technické pravidlá (vynucujú ich validátory)

- Kroky začínajú povoleným rozkazovacím slovesom (`_ALLOWED_IMPERATIVES` v `app/recipe_renderer.py`).
  Placeholdery sú len `{slot.amount}`, `{slot.name}`, `{slot.reference_name}`, `{slot.cut}` a `{slot.water}`.
  Každý `{slot.amount}` je v recepte najviac raz.
- Reťaz stavov:
  - Každý krok má `requires` a `produces` (`surovina:stav`, nádoba `hrniec:free` / `hrniec:occupied`).
  - Posledný krok servíruje všetky suroviny (`:served`) a uvoľní nádoby.
  - Vzor nájdeš v existujúcich kandidátoch.
- Za slovesami „Var/Opekaj/Dus…“ musí nasledovať surovina zo zoznamu v tvare, ktorý renderer pozná. Napríklad „Var papriku s cuketou a morčacím mäsom…“, nie „Var mäso…“, ak „mäso“ nie je tvar žiadnej suroviny.
- `child_factor` je z intervalu (0; 0,8]. Režim `vegan` nesmie mať vajce ani mliečne výrobky. Režim `high_protein` musí mať na porciu aspoň 30 g bielkovín a splniť `qualifies_high_protein`.
- Strukoviny: suché sa namáčajú a varia, konzervované sa scedia a nenamáčajú.
- `storage.refrigerated_days` nastav realisticky. Pri 3 dňoch musí recept dávať zmysel aj pre 12 porcií: jeden hrniec, delenie veľkej dávky, upozornenie na pekáč.

## Nové suroviny

- Najprv skús existujúce suroviny v `app/catalog/ingredients.json`. Pozor na rozdiel `chicken_thigh` (s kosťou) a `chicken_thigh_meat` (bez kosti).
- Nová surovina potrebuje výživové hodnoty z USDA SR Legacy. Vyhľadávanie:
  `python3 /tmp/claude-0/-home-user-taktik-mapa-app/6fdbcace-febf-5c8d-b2ef-0828d6724f50/scratchpad/usda/sr_lookup.py "cod" "atlantic" "raw"`.
  Do `source` zapíš FDC ID a názov ako pri existujúcich surovinách. Hodnoty sú pre surový stav.
- Doplň:
  - tvary v `app/recipe_renderer.py` (`_QUANTITY_NAMES` v genitíve, `_REFERENCE_NAMES` v akuzatíve, prípadne `_EXTRA_INGREDIENT_FORMS`);
  - bežné balenie v `app/regular_purchase.py`;
  - pri dvojznačných letákových názvoch zakázané prívlastky v `INGREDIENT_FORBIDDEN_OFFER_QUALIFIERS`;
  - test v `tests/test_offer_matcher.py` na typické letákové názvy, ktoré sa majú aj nemajú spárovať.

## Kontrola pred odovzdaním

```
python3 -c "from pathlib import Path; from app.ingredient_catalog import load_ingredient_catalog; from app.recipe_candidates import validate_candidate; print(validate_candidate(Path('app/catalog/candidates/<id>.json'), load_ingredient_catalog()).errors)"
python3 -m pytest -q -p no:cacheprovider tests/test_curated_recipe_wave2.py tests/test_curated_recipe_snapshots.py tests/test_recipe_catalog.py tests/test_offer_matcher.py tests/test_ingredient_catalog.py
```

Pred povýšením musí `validate_candidate` hlásiť `()`. Po povýšení hlási iba `duplicate_id:<id>`.
Celú dávku povýš jedným volaním `promote_candidates([...])`. Samo zvýši `library_version` o 1 a `catalog_revision` o 2, manifest ručne nemeň.

## Fotky a skúsenosti z 1. dávky

- Fotky sa robia zvlášť. Každé nové id pridaj na koniec `docs/research/fotky-caka.txt`, inak neprejde `tests/test_recipe_photos.py`.
- **Vyprážanie:** v slote oleja uveď iba množstvo, ktoré jedlo nasaje (15–25 ml na dospelého), lebo výživa ho počíta ako zjedené. V postupe povedz, že olej má byť v panvici aspoň pol centimetra a podľa potreby sa dolieva z fľaše.
- **Suroviny z 1. dávky** už existujú, nepridávaj ich znova: breadcrumbs, tomato_paste, pickled_cucumber, mustard, mayonnaise, tartar_sauce, poppy_seed, caraway, bay_leaf, allspice, celeriac, beef_round, chicken_whole.
- Testy, ktoré počítali recepty, už čítajú počet z `tests/recipe_waves.py`. Ďalší pevný počet nepridávaj.
