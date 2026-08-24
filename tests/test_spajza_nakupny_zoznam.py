"""Špajza je oddelený systém: mení nákupný zoznam, nie jedálniček.

Majiteľ to povedal presne: „špajza musí fungovať ako oddelený systém, nie že
tam pridám vajíčka a zrazu mi preskladá celý jedálniček bez vyzvania."

Z toho plynú tri veci, ktoré sa tu strážia:

  1. plán sa skladá BEZ špajze, takže ho zdieľajú aj platiaci účty,
  2. špajza sa dopočíta až nad hotovým nákupným zoznamom — lokálne, okamžite,
     bez volania modelu,
  3. párovanie voľného textu („10 vajec") na názvy z letáku je zámerne
     opatrné: radšej položku nechať v zozname, než poslať človeka do obchodu
     bez suroviny. Čo sa spárovalo, sa mu ukáže, aby to vedel zrušiť.
"""
from decimal import Decimal

import pytest

from app.plan_data import (
    apply_pantry_to_shopping_list,
    pantry_matches_offer,
    plan_without_pantry,
)


def plan():
    return {
        "tyzden": "2026-08-17",
        "jedla": [
            {
                "den": "PO", "nazov": "Rizoto",
                "recept": {"min": 30, "porcie": 8, "pre": "4 osoby × 2 dni",
                           "davky": ["Ryža guľatozrnná – 600 g", "soľ zo špajze"],
                           "kroky": ["krok"]},
                "suroviny": [
                    {"offer_key": "offer_aaa", "nazov": "Ryža guľatozrnná", "obchod": "Lidl"},
                    {"spajza": "soľ"},
                ],
            },
        ],
        "nakupny_zoznam": [
            {"obchod": "Lidl", "polozky": [
                {"offer_key": "offer_aaa", "nazov": "Ryža guľatozrnná", "jednotka": "1 kg",
                 "mnozstvo": 1, "cena": "1,49", "povodna": "1,99", "zlava": "-25 %"},
                {"offer_key": "offer_bbb", "nazov": "Kuracie stehná", "jednotka": "1 kg",
                 "mnozstvo": 2, "cena": "5,00", "povodna": "7,00", "zlava": "-28 %"},
            ]},
        ],
        "nakup_spolu": "6,49", "bezne": "8,99", "usetris": "2,50",
    }


# ------------------------------------------------------------- párovanie
@pytest.mark.parametrize("polozka_spajze, nazov_ponuky", [
    ("vajcia", "Vajcia M 10 ks"),
    ("vajíčka", "Vajcia M 10 ks"),
    ("10 vajec", "Vajcia M 10 ks"),
    ("VAJCIA", "Vajcia M 10 ks"),
    ("  vajcia  ", "Vajcia M 10 ks"),
    ("ryža", "Ryža guľatozrnná 1 kg"),
    ("ryže", "Ryža guľatozrnná 1 kg"),
    ("cibuľa", "Cibuľa žltá"),
    ("zemiaky", "Zemiaky konzumné neskoré"),
    ("zemiakov 2 kg", "Zemiaky konzumné neskoré"),
    ("mlieko", "Mlieko plnotučné 1,5 %"),
    ("maslo", "Maslo Rajo 250 g"),
    ("kuracie prsia", "Kuracie prsia bez kosti"),
])
def test_the_pantry_recognises_the_same_ingredient_written_by_a_human(polozka_spajze, nazov_ponuky):
    assert pantry_matches_offer(polozka_spajze, nazov_ponuky) is True


@pytest.mark.parametrize("polozka_spajze, nazov_ponuky", [
    # Toto je tá drahá chyba: človek príde do obchodu a stehná nemá.
    ("kuracie prsia", "Kuracie stehná"),
    ("bravčové karé", "Bravčové stehno"),
    ("mlieko", "Maslo Rajo 250 g"),
    ("maslo", "Maslový keks"),
    ("syr", "Syrokrém smotanový"),
    ("ryža", "Ryžový nápoj"),
    ("voda", "Vodka"),
    ("cesto", "Cesnak"),
    ("", "Vajcia M 10 ks"),
    ("   ", "Vajcia M 10 ks"),
    ("2 kg", "Vajcia M 10 ks"),
    ("vajcia", ""),
])
def test_the_pantry_would_rather_miss_a_match_than_invent_one(polozka_spajze, nazov_ponuky):
    assert pantry_matches_offer(polozka_spajze, nazov_ponuky) is False


# ------------------------------------------------- nákupný zoznam so špajzou
def test_owned_items_are_marked_and_the_remaining_total_drops():
    upraveny = apply_pantry_to_shopping_list(plan(), ["ryža"])

    polozky = upraveny["nakupny_zoznam"][0]["polozky"]
    assert polozky[0]["mas_doma"] is True
    assert polozky[0]["spajza"] == "ryža", "musí byť vidieť, ČO sa spárovalo"
    assert polozky[1]["mas_doma"] is False and polozky[1]["spajza"] is None
    assert upraveny["nakup_bez_spajze"] == "5,00"
    assert upraveny["spajza_usetri"] == "1,49"


def test_the_plan_itself_is_never_rewritten_by_the_pantry():
    """Špajza sa smie dotknúť len nákupného zoznamu — jedlá ostávajú, aké boli."""
    povodny = plan()

    upraveny = apply_pantry_to_shopping_list(povodny, ["ryža"])

    assert upraveny["jedla"] == povodny["jedla"]
    assert upraveny["nakup_spolu"] == povodny["nakup_spolu"] == "6,49"
    assert upraveny["bezne"] == povodny["bezne"]
    assert upraveny["usetris"] == povodny["usetris"]


def test_applying_the_pantry_does_not_mutate_the_shared_plan_it_was_given():
    """Zdieľaný plán drží v pamäti viac čitateľov naraz; nesmie sa prepísať."""
    povodny = plan()

    apply_pantry_to_shopping_list(povodny, ["ryža"])

    assert "mas_doma" not in povodny["nakupny_zoznam"][0]["polozky"][0]
    assert "spajza_usetri" not in povodny


def test_an_empty_pantry_still_produces_the_same_shape():
    """Obrazovka nesmie mať dva režimy — polia sú tam vždy."""
    upraveny = apply_pantry_to_shopping_list(plan(), [])

    assert upraveny["spajza_pokryte"] == []
    assert upraveny["nakup_bez_spajze"] == "6,49"
    assert upraveny["spajza_usetri"] == "0,00"
    assert all(not polozka["mas_doma"] for polozka in upraveny["nakupny_zoznam"][0]["polozky"])


def test_the_matched_items_are_listed_so_the_user_can_overrule_them():
    upraveny = apply_pantry_to_shopping_list(plan(), ["ryža", "kuracie stehná"])

    assert upraveny["spajza_pokryte"] == [
        {"offer_key": "offer_aaa", "nazov": "Ryža guľatozrnná", "spajza": "ryža", "cena": "1,49"},
        {"offer_key": "offer_bbb", "nazov": "Kuracie stehná", "spajza": "kuracie stehná",
         "cena": "5,00"},
    ]
    assert upraveny["nakup_bez_spajze"] == "0,00"


def test_one_shopping_item_is_claimed_by_at_most_one_pantry_entry():
    upraveny = apply_pantry_to_shopping_list(plan(), ["ryža guľatozrnná", "ryža"])

    assert [item["spajza"] for item in upraveny["spajza_pokryte"]] == ["ryža guľatozrnná"]


def test_a_pantry_full_of_junk_leaves_the_shopping_list_alone():
    upraveny = apply_pantry_to_shopping_list(plan(), ["", "   ", "2 kg", "niečo úplne iné"])

    assert upraveny["spajza_pokryte"] == []
    assert upraveny["nakup_bez_spajze"] == "6,49"


def test_the_result_is_computed_from_the_reader_pantry_every_single_time():
    """Žiadne uloženie: zmena špajze sa musí prejaviť okamžite, bez prepočtu."""
    zaklad = plan()

    prvy = apply_pantry_to_shopping_list(zaklad, ["ryža"])
    druhy = apply_pantry_to_shopping_list(zaklad, ["kuracie stehná"])

    assert prvy["spajza_usetri"] == "1,49"
    assert druhy["spajza_usetri"] == "5,00"


# ------------------------------------------------- čo sa nesmie uložiť zdieľane
def test_a_shared_plan_is_stripped_of_every_pantry_trace():
    """Podpis už špajzu neobsahuje, takže ju z uloženého plánu musí odstrániť kód."""
    osobny = apply_pantry_to_shopping_list(dict(plan(), spajza=["soľ", "ryža"]), ["ryža"])

    zdielany = plan_without_pantry(osobny)

    assert "spajza" not in zdielany
    assert "spajza_pokryte" not in zdielany
    assert "nakup_bez_spajze" not in zdielany and "spajza_usetri" not in zdielany
    assert zdielany["jedla"][0]["suroviny"] == [
        {"offer_key": "offer_aaa", "nazov": "Ryža guľatozrnná", "obchod": "Lidl"}
    ], "surovina zo špajze je cudzí osobný údaj a do zdieľaného riadku nepatrí"
    assert zdielany["jedla"][0]["recept"]["davky"] == ["Ryža guľatozrnná – 600 g"]
    for polozka in zdielany["nakupny_zoznam"][0]["polozky"]:
        assert "mas_doma" not in polozka and "spajza" not in polozka


def test_stripping_leaves_the_purchasable_facts_completely_intact():
    zdielany = plan_without_pantry(apply_pantry_to_shopping_list(plan(), ["ryža"]))

    assert zdielany["nakup_spolu"] == "6,49"
    assert zdielany["nakupny_zoznam"][0]["polozky"][0]["cena"] == "1,49"
    assert zdielany["tyzden"] == "2026-08-17"


def test_stripping_does_not_mutate_the_plan_it_was_given():
    osobny = apply_pantry_to_shopping_list(dict(plan(), spajza=["ryža"]), ["ryža"])

    plan_without_pantry(osobny)

    assert osobny["spajza"] == ["ryža"]
    assert osobny["nakupny_zoznam"][0]["polozky"][0]["mas_doma"] is True


def test_prices_stay_exact_cents_and_never_drift_through_floats():
    velky = plan()
    velky["nakupny_zoznam"][0]["polozky"][0]["cena"] = "0,10"
    velky["nakupny_zoznam"][0]["polozky"][1]["cena"] = "0,20"
    velky["nakup_spolu"] = "0,30"

    upraveny = apply_pantry_to_shopping_list(velky, ["ryža"])

    assert upraveny["spajza_usetri"] == "0,10"
    assert Decimal(upraveny["nakup_bez_spajze"].replace(",", ".")) == Decimal("0.20")
