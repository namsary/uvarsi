from datetime import date
from decimal import Decimal

from app.ingredient_catalog import load_ingredient_catalog
from app.recipe_renderer import (
    _QUANTITY_NAMES,
    _REFERENCE_NAMES,
    _SlotWording,
    _render_template,
    _slovak_title_forms,
)


CLASSIC_REQUIRED_IDS = frozenset(
    {
        "apple",
        "apple_cider_vinegar",
        "beef_chuck",
        "bryndza",
        "butter",
        "cauliflower",
        "dry_peas",
        "dill",
        "ham",
        "lentils",
        "marjoram",
        "pork_loin",
        "pumpkin",
        "sauerkraut",
        "smoked_sausage",
        "sugar",
        "wheat_flour",
        "white_cabbage",
    }
)

CLASSIC_USDA_FDC_IDS = {
    "apple": "171688",
    "apple_cider_vinegar": "173469",
    "beef_chuck": "168660",
    "butter": "173410",
    "cauliflower": "169986",
    "dill": "172233",
    "dry_peas": "172428",
    "ham": "173864",
    "lentils": "172420",
    "red_lentils": "174284",
    "marjoram": "170928",
    "pork_loin": "167818",
    "pumpkin": "168448",
    "sauerkraut": "169279",
    "smoked_sausage": "174585",
    "sugar": "169655",
    "wheat_flour": "169761",
    "white_cabbage": "169975",
}

CLASSIC_RENDER_FORMS = {
    "apple": ("jabĺk", "jablká", "jabĺk", "jablkami"),
    "apple_cider_vinegar": (
        "jablčného octu",
        "jablčný ocot",
        "jablčného octu",
        "jablčným octom",
    ),
    "beef_chuck": (
        "hovädzieho predného bez kosti",
        "hovädzie predné bez kosti",
        "hovädzieho predného bez kosti",
        "hovädzím mäsom z predného bez kosti",
    ),
    "bryndza": ("bryndze", "bryndzu", "bryndze", "bryndzou"),
    "butter": ("masla", "maslo", "masla", "maslom"),
    "cauliflower": ("karfiolu", "karfiol", "karfiolu", "karfiolom"),
    "dill": ("kôpru", "kôpor", "kôpru", "kôprom"),
    "dry_peas": (
        "suchého poleného hrachu",
        "suchý polený hrach",
        "suchého poleného hrachu",
        "suchým poleným hrachom",
    ),
    "ham": ("varenej šunky", "varenú šunku", "varenej šunky", "varenou šunkou"),
    "lentils": ("šošovice", "šošovicu", "šošovice", "šošovicou"),
    "marjoram": ("majoránu", "majorán", "majoránu", "majoránom"),
    "pork_loin": (
        "bravčového karé bez kosti",
        "bravčové karé bez kosti",
        "bravčového karé bez kosti",
        "bravčovým karé bez kosti",
    ),
    "pumpkin": ("tekvice", "tekvicu", "tekvice", "tekvicou"),
    "sauerkraut": (
        "kyslej kapusty",
        "kyslú kapustu",
        "kyslej kapusty",
        "kyslou kapustou",
    ),
    "smoked_sausage": (
        "údenej klobásy",
        "údenú klobásu",
        "údenej klobásy",
        "údenou klobásou",
    ),
    "sugar": (
        "kryštálového cukru",
        "kryštálový cukor",
        "kryštálového cukru",
        "kryštálovým cukrom",
    ),
    "wheat_flour": (
        "hladkej pšeničnej múky",
        "hladkú pšeničnú múku",
        "hladkej pšeničnej múky",
        "hladkou pšeničnou múkou",
    ),
    "white_cabbage": (
        "bielej hlávkovej kapusty",
        "bielu hlávkovú kapustu",
        "bielej hlávkovej kapusty",
        "bielou hlávkovou kapustou",
    ),
}


def test_classic_ingredient_expansion_is_complete_and_sourced():
    catalog = load_ingredient_catalog()
    by_id = {item.id: item for item in catalog.all()}

    assert CLASSIC_REQUIRED_IDS <= by_id.keys()
    for ingredient_id in CLASSIC_REQUIRED_IDS:
        ingredient = by_id[ingredient_id]
        assert "https://" in ingredient.nutrition.source
        assert ingredient.nutrition.verified_on == date(2026, 9, 5)


def test_classic_usda_sources_use_stable_dataset_audit_links_and_exact_ids():
    catalog = load_ingredient_catalog()

    for ingredient_id, fdc_id in CLASSIC_USDA_FDC_IDS.items():
        source = catalog.by_id(ingredient_id).nutrition.source
        assert "USDA FoodData Central SR Legacy (April 2018)" in source
        assert f"FDC ID {fdc_id}," in source
        assert "https://fdc.nal.usda.gov/download-datasets/" in source
        assert "/fdc-app.html#/food-details/" not in source


def test_characteristic_slovak_ingredients_resolve_exactly():
    catalog = load_ingredient_catalog()

    assert catalog.resolve("kyslá kapusta").id == "sauerkraut"
    assert catalog.resolve("kapusta kvasená").id == "sauerkraut"
    assert catalog.resolve("biela hlávková kapusta").id == "white_cabbage"
    assert catalog.resolve("hladká múka").id == "wheat_flour"
    assert catalog.resolve("bryndza").id == "bryndza"
    assert catalog.resolve("hovädzie na guláš").id == "beef_chuck"
    assert catalog.resolve("bravčové karé").id == "pork_loin"
    assert catalog.resolve("suchý hrach").id == "dry_peas"
    assert catalog.resolve("šošovica červená").id == "red_lentils"
    assert catalog.resolve("kôpor").id == "dill"


def test_product_states_and_cuts_remain_distinct():
    catalog = load_ingredient_catalog()

    assert catalog.resolve("kyslá kapusta").id != catalog.resolve("biela kapusta").id
    assert catalog.resolve("šošovica").id != catalog.resolve("červená šošovica").id
    assert catalog.resolve("suchý hrach").id != catalog.resolve("zelený hrášok").id
    assert catalog.resolve("hovädzie na guláš").id != catalog.resolve("mleté hovädzie mäso").id
    assert catalog.resolve("bravčové karé").id != catalog.resolve("bravčové pliecko").id


def test_new_allergens_and_diet_tags_are_truthful():
    catalog = load_ingredient_catalog()

    assert set(catalog.by_id("wheat_flour").allergens) == {"gluten"}
    assert set(catalog.by_id("bryndza").allergens) == {"milk"}
    assert set(catalog.by_id("butter").allergens) == {"milk"}
    assert catalog.by_id("sugar").roles == {"seasoning"}
    assert {tag.value for tag in catalog.by_id("lentils").diet_tags} == {
        "vegetarian",
        "vegan",
    }
    assert {tag.value for tag in catalog.by_id("dry_peas").diet_tags} == {
        "vegetarian",
        "vegan",
    }


def test_whole_produce_uses_purchase_to_edible_yields():
    catalog = load_ingredient_catalog()

    assert catalog.by_id("apple").edible_ratio == Decimal("0.9")
    assert catalog.by_id("apple").grams_per_piece is None
    assert catalog.by_id("white_cabbage").edible_ratio == Decimal("0.8")
    assert catalog.by_id("cauliflower").edible_ratio == Decimal("0.39")
    assert catalog.by_id("pumpkin").edible_ratio == Decimal("0.7")


def test_whole_cauliflower_does_not_alias_pretrimmed_florets():
    catalog = load_ingredient_catalog()

    assert catalog.resolve("karfiolové ružičky") is None


def test_dill_nutrition_matches_sr_legacy_fdc_172233():
    dill = load_ingredient_catalog().by_id("dill")

    assert dill.nutrition.kcal == Decimal("43")
    assert dill.nutrition.protein_g == Decimal("3.46")
    assert dill.nutrition.fat_g == Decimal("1.12")
    assert dill.nutrition.carbs_g == Decimal("7.02")


def test_new_ingredients_render_literal_slovak_quantity_reference_and_title_forms():
    catalog = load_ingredient_catalog()
    title_forms = _slovak_title_forms()

    for ingredient_id, expected in CLASSIC_RENDER_FORMS.items():
        quantity, reference, genitive, instrumental = expected
        ingredient = catalog.by_id(ingredient_id)
        wording = _SlotWording(
            ingredient_id=ingredient_id,
            name=_QUANTITY_NAMES[ingredient_id],
            reference_name=_REFERENCE_NAMES[ingredient_id],
            amount="100 g",
            cut="",
            water=None,
        )
        slots = {"main": wording}

        assert _render_template(
            "{main.amount} {main.name}", slots, 1, label="teste množstva"
        ) == f"100 g {quantity}"
        assert _render_template(
            "Pridaj {main.name}.",
            slots,
            1,
            label="teste odkazu",
            omit_amounts=True,
        ) == f"Pridaj {reference}."
        assert _render_template(
            "Jedlo z {main.name}",
            slots,
            1,
            label="teste genitívu",
            prepositional_names=True,
        ) == f"Jedlo z {genitive}"
        assert _render_template(
            "Jedlo s {main.name}",
            slots,
            1,
            label="teste inštrumentálu",
            prepositional_names=True,
        ) == f"Jedlo s {instrumental}"
        assert title_forms[ingredient_id] == {
            "genitive": genitive,
            "instrumental": instrumental,
        }
