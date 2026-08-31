from dataclasses import replace
from datetime import date
from decimal import Decimal

from app.ingredient_catalog import load_ingredient_catalog
from app.nutrition import estimate_recipe_nutrition
from app.offer_matcher import MatchedOffer
from app.quantity_math import PackageSize, PantryEntry, Quantity
from app.recipe_catalog import IngredientSlot
from app.recipe_matcher import SlotSelection
from app import recipe_renderer
from app.recipe_renderer import RenderedIngredient, RenderedMeal


FRONTEND_ITEM_KEYS = {
    "offer_key",
    "nazov",
    "obchod",
    "mnozstvo",
    "cena",
    "povodna",
    "potrebne",
    "potrebna_jednotka",
    "cena_za_balenie",
    "povodna_za_balenie",
    "zostava",
    "source_url",
}


def _rice_offer(*, package="1000", sale="1.49", original="1.99"):
    rice = load_ingredient_catalog().by_id("rice")
    return MatchedOffer(
        offer_key="offer_rice",
        store="Lidl",
        product_name="Ryža guľatozrnná",
        ingredient=rice,
        package=PackageSize(Quantity(Decimal(package), "g")),
        sale_price=Decimal(sale),
        original_price=Decimal(original) if original is not None else None,
        valid_from=date(2026, 8, 31),
        valid_to=date(2026, 9, 6),
        source_url="https://example.test/lidl-ryza",
    )


def _milk_offer():
    milk = load_ingredient_catalog().by_id("milk")
    return MatchedOffer(
        offer_key="offer_milk",
        store="Tesco",
        product_name="Plnotučné mlieko",
        ingredient=milk,
        package=PackageSize(Quantity(Decimal("1000"), "ml")),
        sale_price=Decimal("1.19"),
        original_price=Decimal("1.49"),
        valid_from=date(2026, 8, 31),
        valid_to=date(2026, 9, 6),
        source_url="https://example.test/tesco-mlieko",
    )


def _meal(amount, offer, *, unit="g", role="starch"):
    ingredient = offer.ingredient
    quantity = Quantity(Decimal(amount), unit)
    slot = IngredientSlot(
        key=role,
        role=role,
        candidates=(ingredient.id,),
        amount_per_adult=quantity.amount,
        unit=quantity.unit,
        child_factor=Decimal("0.5"),
        required=True,
        use="main",
        cut=None,
    )
    selection = SlotSelection(
        slot=slot,
        ingredient=ingredient,
        offer=offer,
        pantry=None,
    )
    rendered = RenderedIngredient(
        selection=selection,
        quantity=quantity,
        display_amount="9 999 kg",
        label="display text must not drive shopping arithmetic",
    )
    return RenderedMeal(
        template_id=f"{ingredient.id}-meal",
        candidate_key=f"{ingredient.id}-{amount}",
        name=f"Jedlo: {ingredient.name}",
        portions=1,
        covered_days=1,
        ingredients=(rendered,),
        pantry_basics=(),
        instructions=(),
        nutrition=estimate_recipe_nutrition(
            [
                (
                    ingredient,
                    quantity.amount
                    if quantity.unit == "g"
                    else quantity.amount * ingredient.density_g_per_ml,
                )
            ],
            adult_servings=Decimal("1"),
        ),
    )


def test_shopping_list_uses_frontend_contract_and_whole_package_price():
    assert hasattr(recipe_renderer, "build_shopping_list"), (
        "Task 4 must expose build_shopping_list"
    )

    result = recipe_renderer.build_shopping_list([_meal("300", _rice_offer())], [])

    assert len(result) == 1
    assert result[0]["obchod"] == "Lidl"
    rice = result[0]["polozky"][0]
    assert FRONTEND_ITEM_KEYS <= rice.keys()
    assert rice["potrebne"] == "300"
    assert rice["potrebna_jednotka"] == "g"
    assert rice["mnozstvo"] == 1
    assert rice["zostava"] == "700 g"
    assert rice["cena"] == rice["cena_za_balenie"] == "1,49"
    assert rice["povodna"] == rice["povodna_za_balenie"] == "1,99"


def test_groups_by_exact_offer_and_package_identity_before_rounding():
    one_kilo = _rice_offer()
    half_kilo = _rice_offer(package="500", sale="0.89", original="1.09")

    result = recipe_renderer.build_shopping_list(
        [
            _meal("600", one_kilo),
            _meal("600", one_kilo),
            _meal("200", half_kilo),
        ],
        [],
    )

    rows = result[0]["polozky"]
    assert len(rows) == 2
    assert rows[0]["potrebne"] == "1 200"
    assert rows[0]["mnozstvo"] == 2
    assert rows[0]["cena"] == "2,98"
    assert rows[0]["povodna"] == "3,98"
    assert rows[0]["zostava"] == "800 g"
    assert rows[1]["potrebne"] == "200"
    assert rows[1]["mnozstvo"] == 1
    assert rows[1]["cena"] == "0,89"
    assert rows[1]["povodna"] == "1,09"
    assert rows[1]["zostava"] == "300 g"


def test_subtracts_each_quantified_pantry_entry_once_after_weekly_aggregation():
    offer = _rice_offer(package="500", sale="0.89", original="1.09")
    pantry = [
        PantryEntry("rice", "ryža", Quantity(Decimal("250"), "g")),
        PantryEntry("rice", "ryža", Quantity(Decimal("150"), "g")),
    ]

    result = recipe_renderer.build_shopping_list(
        [_meal("300", offer), _meal("300", offer)],
        pantry,
    )

    rice = result[0]["polozky"][0]
    assert rice["potrebne"] == "600"
    assert rice["mnozstvo"] == 1
    assert rice["cena"] == "0,89"
    assert rice["povodna"] == "1,09"
    assert rice["zostava"] == "300 g"


def test_unknown_pantry_amount_marks_confirmation_without_hiding_purchase():
    offer = _rice_offer()

    result = recipe_renderer.build_shopping_list(
        [_meal("300", offer)],
        [
            PantryEntry("rice", "ryža", None),
            PantryEntry("pasta", "cestoviny", None),
        ],
    )

    rice = result[0]["polozky"][0]
    assert rice["mnozstvo_nezname"] is True
    assert rice["mnozstvo"] == 1
    assert rice["cena"] == "1,49"
    assert rice["zostava"] == "700 g"


def test_compatible_pantry_conversion_never_creates_negative_purchase_values():
    offer = _milk_offer()

    result = recipe_renderer.build_shopping_list(
        [_meal("300", offer, unit="ml", role="dairy")],
        [PantryEntry("milk", "mlieko", Quantity(Decimal("350"), "g"))],
    )

    milk = result[0]["polozky"][0]
    assert milk["potrebne"] == "300"
    assert milk["potrebna_jednotka"] == "ml"
    assert milk["mnozstvo"] == 0
    assert milk["cena"] == "0,00"
    assert milk["povodna"] == "0,00"
    assert milk["zostava"] == "0 ml"


def test_converts_compatible_package_to_the_exact_recipe_unit_before_buying():
    offer = replace(
        _milk_offer(),
        package=PackageSize(Quantity(Decimal("1000"), "g")),
    )

    result = recipe_renderer.build_shopping_list(
        [_meal("300", offer, unit="ml", role="dairy")],
        [],
    )

    milk = result[0]["polozky"][0]
    assert milk["mnozstvo"] == 1
    assert milk["potrebne"] == "300"
    assert milk["potrebna_jednotka"] == "ml"
    assert milk["cena"] == "1,19"
    assert milk["zostava"] == "700 ml"


def test_aggregates_compatible_recipe_units_before_pantry_and_package_rounding():
    offer = _milk_offer()

    result = recipe_renderer.build_shopping_list(
        [
            _meal("300", offer, unit="ml", role="dairy"),
            _meal("200", offer, unit="g", role="dairy"),
        ],
        [],
    )

    milk = result[0]["polozky"][0]
    assert milk["potrebne"] == "500"
    assert milk["potrebna_jednotka"] == "ml"
    assert milk["mnozstvo"] == 1
    assert milk["cena"] == "1,19"
    assert milk["povodna"] == "1,49"
    assert milk["zostava"] == "500 ml"


def test_one_pantry_pool_is_consumed_once_in_returned_row_order():
    first_lidl = replace(
        _rice_offer(package="500", sale="0.80", original="1.00"),
        offer_key="offer_rice_lidl_first",
        product_name="Ryža Lidl prvá",
    )
    tesco = replace(
        _rice_offer(package="500", sale="1.20", original="1.50"),
        offer_key="offer_rice_tesco",
        store="Tesco",
        product_name="Ryža Tesco",
    )
    second_lidl = replace(
        _rice_offer(package="500", sale="0.80", original="1.00"),
        offer_key="offer_rice_lidl_second",
        product_name="Ryža Lidl druhá",
    )

    result = recipe_renderer.build_shopping_list(
        [
            _meal("300", first_lidl),
            _meal("300", tesco),
            _meal("700", second_lidl),
        ],
        [PantryEntry("rice", "ryža", Quantity(Decimal("500"), "g"))],
    )

    rows = [item for group in result for item in group["polozky"]]
    assert [row["offer_key"] for row in rows] == [
        "offer_rice_lidl_first",
        "offer_rice_lidl_second",
        "offer_rice_tesco",
    ]
    assert [row["mnozstvo"] for row in rows] == [0, 1, 1]
    assert [row["cena"] for row in rows] == ["0,00", "0,80", "1,20"]
    assert [row["povodna"] for row in rows] == ["0,00", "1,00", "1,50"]
    assert [row["zostava"] for row in rows] == ["0 g", "0 g", "200 g"]
    assert sum(row["mnozstvo"] for row in rows) == 2
    assert sum(Decimal(row["cena"].replace(",", ".")) for row in rows) == Decimal("2.00")
    assert all(row["mnozstvo"] >= 0 for row in rows)
