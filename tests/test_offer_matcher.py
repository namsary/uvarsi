from dataclasses import FrozenInstanceError, replace
from datetime import date
from decimal import Decimal

import pytest

from app.ingredient_catalog import IngredientCatalog, load_ingredient_catalog
from app.offer_matcher import MatchedOffer, match_offers
from app.quantity_math import PackageSize, Quantity


@pytest.fixture
def catalog():
    return load_ingredient_catalog()


def offer(**overrides):
    row = {
        "offer_key": "offer_123456789abc",
        "obchod": "Lidl",
        "nazov": "Basmati ryža Golden Sun 1 kg",
        "jednotka": "1 kg",
        "cena": 1.79,
        "povodna": 2.49,
        "valid_from": "2026-08-27",
        "valid_to": "2026-09-02",
        "source_url": "https://example.test/lidl-ryza.jpg",
    }
    row.update(overrides)
    return row


def test_maps_brand_product_to_canonical_ingredient_and_preserves_offer_facts(catalog):
    row = offer()

    matched = match_offers([row], catalog)

    assert matched == (
        MatchedOffer(
            offer_key=row["offer_key"],
            store=row["obchod"],
            product_name=row["nazov"],
            ingredient=catalog.by_id("rice"),
            package=PackageSize(Quantity(Decimal("1000"), "g")),
            sale_price=Decimal("1.79"),
            original_price=Decimal("2.49"),
            valid_from=date(2026, 8, 27),
            valid_to=date(2026, 9, 2),
            source_url=row["source_url"],
        ),
    )
    with pytest.raises(FrozenInstanceError):
        matched[0].store = "Tesco"


def test_unmapped_product_is_not_guessed(catalog):
    assert match_offers([offer(nazov="Rodinná dobrota")], catalog) == ()


def test_prefers_the_longest_matching_alias(catalog):
    short_match = replace(catalog.by_id("rice"), synonyms=())
    long_match = replace(
        catalog.by_id("tofu"),
        id="basmati_rice",
        name="basmati ryža",
        synonyms=(),
    )
    overlapping_catalog = IngredientCatalog((short_match, long_match))

    matched = match_offers([offer()], overlapping_catalog)

    assert matched[0].ingredient.id == "basmati_rice"


def test_rejects_equal_length_aliases_for_different_ingredients(catalog):
    ambiguous_catalog = IngredientCatalog(
        (
            replace(catalog.by_id("rice"), synonyms=()),
            replace(catalog.by_id("tofu"), synonyms=()),
        )
    )

    assert match_offers(
        [offer(nazov="Ryža alebo tofu 1 kg")], ambiguous_catalog
    ) == ()


def test_alias_must_be_a_complete_normalized_token_sequence(catalog):
    assert match_offers([offer(nazov="Superryža Golden Sun 1 kg")], catalog) == ()


@pytest.mark.parametrize("package", ["1 lb", "0 g"])
def test_rejects_packages_quantity_math_cannot_convert(catalog, package):
    assert match_offers([offer(jednotka=package)], catalog) == ()


@pytest.mark.parametrize("package", ["250 ml", "1 piece"])
def test_rejects_supported_dimensions_without_ingredient_conversion(catalog, package):
    assert match_offers([offer(jednotka=package)], catalog) == ()


@pytest.mark.parametrize(
    ("product_name", "package", "ingredient_id"),
    [
        ("Plnotučné mlieko Rajo 250 ml", "250 ml", "milk"),
        ("Vajcia M 6 piece", "6 piece", "egg"),
    ],
)
def test_accepts_dimensions_with_ingredient_conversion_metadata(
    catalog, product_name, package, ingredient_id
):
    matched = match_offers(
        [offer(nazov=product_name, jednotka=package)], catalog
    )

    assert matched[0].ingredient.id == ingredient_id
