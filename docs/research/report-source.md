# Canonical research source — Uvar.si recipe-quality audit

**Audience:** founder/product and engineering  
**Date:** 2026-08-29  
**Geography:** Slovakia, with international recipe-writing and structured-data standards  
**Scope:** recipe structure, quantities, rounding, packs vs consumption, pantry, seasonings, Slovak language, batch-cooking safety, Google Recipe eligibility  
**Assumptions:** Uvar.si primarily generates savoury household meals; baking requires separate precision rules. No code or production state was changed.

## Direct answer

Uvar.si should not publish model prose directly after schema validation. A release-grade recipe needs a deterministic editorial layer that: aggregates ingredient groups, subtracts pantry stock before package rounding, formats exact quantities into kitchen-natural units, exposes all seasonings, applies one Slovak voice and canonical product names, and assigns storage/reheating rules based on the dish. Public recipe pages should mirror the visible recipe in Recipe JSON-LD.

## Material findings

1. Google and Schema.org model ingredients, instructions, yield and time separately. `HowToStep` is preferred over an undifferentiated text block.
2. High-quality recipe sources list ingredients in order of use, include every used ingredient, separate recipe sections when needed, provide yield/time and describe doneness.
3. Slovak and international editorial recipes commonly use natural produce counts with size qualifiers, while using grams for ingredients where weight matters.
4. Exact calculations and display values serve different purposes. SI typography requires a space between value and unit; Slovak display should use a decimal comma.
5. Whole-package purchasing is a different problem from recipe consumption. Pantry stock must be subtracted from aggregate consumption before rounding the remainder to purchasable packs.
6. Recipe scaling is not entirely linear: official large-quantity recipe guidance warns that herbs, spices, leavening, thickeners and some liquids may need non-proportional adjustment.
7. Three-day batch planning needs food-specific storage logic. Cooked rice needs rapid cooling and short refrigerated storage; poultry and reheated leftovers require safe internal temperatures.

## Claim-to-source ledger

| Claim | Source | Publisher/date | URL | Access/notes |
|---|---|---|---|---|
| Recipe fields and preferred `HowToStep` structure | Recipe structured data | Google Search Central, updated 2025-12-10 | https://developers.google.com/search/docs/appearance/structured-data/recipe | Accessed 2026-08-29; primary technical documentation |
| Recipe vocabulary for ingredients, instructions and yield | Recipe | Schema.org, current | https://schema.org/Recipe | Accessed 2026-08-29; primary standard vocabulary |
| Ingredients ordered by use, complete quantities, prep/total time, doneness and recipe testing | How to Write a Recipe | Virginia Cooperative Extension / Virginia Tech, 2021 | https://www.pubs.ext.vt.edu/FST/FST-155/FST-155.html | Accessed 2026-08-29; university extension guidance |
| Numbered active-voice steps and complete ingredient list | Content Creator's Food Safety Style Guide | University of Maine Cooperative Extension, current | https://extension.umaine.edu/publications/4086e/ | Accessed 2026-08-29; specific to canning but relevant readability rules |
| Natural Slovak quantities and explicit seasonings | Plnená cuketa s kuracím mäsom a ryžou | Kuchyňa Lidla, recipe page | https://kuchynalidla.sk/recepty/plnena-cuketa-s-kuracim-masom-a-ryzou | Accessed 2026-08-29; first-party Slovak recipe example |
| Natural count/weight mix and seasoning „podľa chuti“ | Cuketové rizoto | Dobrúchuť.sk, current page | https://dobruchut.aktuality.sk/recept/79968/cuketove-rizoto-lahky-obed/ | Accessed 2026-08-29; Slovak editorial example |
| Servings, prep/cook time, natural produce counts and concise steps | Vegan courgette risotto | Good Food, current page | https://www.bbcgoodfood.com/recipes/summer-courgette-risotto | Accessed 2026-08-29; international editorial benchmark |
| Active, bake and total time; metric and volume measurements | Unofficially official recipe rules | King Arthur Baking, 2021-04-13 | https://www.kingarthurbaking.com/blog/2021/04/13/king-arthur-recipe-success | Accessed 2026-08-29; first-party test-kitchen guidance |
| Some ingredients do not scale proportionately | Standardized Recipes in a Nutshell | Wisconsin Department of Public Instruction / USDA-CICN, 2024 | https://dpi.wi.gov/sites/default/files/imce/school-nutrition/pdf/standardized_recipes.nutshell.pdf | Accessed 2026-08-29; official large-quantity recipe guidance |
| Space between number and unit; decimal marker by language context | SI Brochure, 9th ed. | BIPM, version 3.02 (2025) | https://www.bipm.org/documents/d/guest/si-brochure-9-en-pdf | Accessed 2026-08-29; primary SI reference |
| Space between numerical value and unit symbol | NIST Guide to the SI, chapter 7 | NIST, current | https://www.nist.gov/pml/special-publication-811/nist-guide-si-chapter-7-rules-and-style-conventions-expressing-values | Accessed 2026-08-29; primary metrology guidance |
| Correct headword and stem for `rezeň` | KSSJ / Pravidlá slovenského pravopisu entry | Slovnik.sk mirror of Slovak dictionaries, current | https://slovnik.aktuality.sk/pravopis/?q=reze%C5%88 | Accessed 2026-08-29; official dictionary content mirrored by publisher |
| Pantry staples should be removed from buy list | Pantry-aware shopping list | useLadle, current product page | https://www.useladle.com/ | Accessed 2026-08-29; first-party product-pattern evidence, not a standard |
| Cooked rice cooling, storage and reheating limits | Starchy foods and carbohydrates | NHS, current | https://www.nhs.uk/live-well/eat-well/food-types/starchy-foods-and-carbohydrates/ | Accessed 2026-08-29; government health guidance |
| Cooked rice should be cooled quickly, kept 24h and reheated once | Can you reheat rice? | UK Food Standards Agency, current | https://www.food.gov.uk/print/pdf/node/4286 | Accessed 2026-08-29; government food-safety guidance |
| Poultry and leftovers safe temperature 74 °C | Safe Minimum Internal Temperatures | FoodSafety.gov, reviewed 2024-11-21 | https://www.foodsafety.gov/food-safety-charts/safe-minimum-internal-temperatures | Accessed 2026-08-29; US government food-safety source |

## Local evidence reviewed

- `app/plan_data.py`: `STAPLES`, portion defaults, amount formatting, whole-package aggregation, prompt rules and pantry matching.
- `app/static/app.html`: current recipe, ingredient and shopping-list presentation contracts.
- Existing tests for recipe structure, shopping quantities and pantry behavior.

## Limitations and disagreements

- Recipe websites are editorial examples, not universal portion standards.
- Guidance on scaling salt and spices varies by recipe type and batch size. The official Wisconsin source supports the narrower claim that some ingredients may not scale proportionately; it does not prescribe one universal multiplier.
- `Ryžu sceď` is grammatically valid and appears on a Slovak professional recipe site. The audit criticizes missing technique/context, not the verb itself.
- Pantry entries without quantities are inherently ambiguous. The recommended „Skontroluj doma“ state preserves honesty without forcing a duplicate purchase.

## Research stopping rationale

The consequential claims have primary or official support: Google/Schema structure, SI formatting and food-safety guidance. Slovak and international editorial examples converge on natural units, explicit seasonings and structured steps. Additional recipe pages would be repetitive rather than decision-changing.
