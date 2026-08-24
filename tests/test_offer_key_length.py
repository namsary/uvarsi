"""Krátky `offer_key`: to isté overené fakty, o tri štvrtiny menší prompt.

Celý sha256 v hexa mal 64 znakov a v prompte stál 35 tokenov — 63 % riadku
ponuky bol nepriehľadný identifikátor, ktorý model iba zopakoval späť.
Skrátenie odtlačku je jediná zmena; kľúč musí ostať čistou funkciou tých istých
overených faktov, inak by zmenená cena prestala rušiť staré plány.

Za skrátenie sa platí rizikom kolízie. Cena kolízie je tu neúnosne vysoká —
reálna cena pripísaná k inému výrobku — takže sa nesmie prehltnúť: musí byť
odhalená tam, kde sa kľúče stavajú a ukladajú, a musí byť hlasná.
"""
import sqlite3

import pytest

from app.offer_data import (
    OFFER_KEY_DIGEST_CHARS,
    OFFER_KEY_PREFIX,
    OfferKeyCollision,
    canonical_offer_key,
    legacy_offer_key_for,
    migrate_akcie_schema,
    offer_key_for,
    offer_key_matches,
    replace_store_week,
)


TYZDEN = "2026-08-17"


def valid_offer(**overrides):
    offer = {
        "obchod": "Lidl",
        "nazov": "Plnotučné mlieko",
        "kategoria": "mliecne",
        "cena": 1.19,
        "povodna": 1.49,
        "zlava": "-20 %",
        "jednotka": "1 l",
        "source_url": "https://example.test/lidl-letak-2.jpg",
        "source_page": 2,
        "valid_from": "2026-08-17",
        "valid_to": "2026-08-23",
    }
    offer.update(overrides)
    return offer


def legacy_connection():
    con = sqlite3.connect(":memory:")
    con.execute(
        """CREATE TABLE akcie (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tyzden TEXT NOT NULL,
            obchod TEXT NOT NULL,
            nazov TEXT NOT NULL,
            kategoria TEXT,
            cena REAL,
            povodna REAL,
            zlava TEXT,
            jednotka TEXT
        )"""
    )
    migrate_akcie_schema(con)
    return con


# --------------------------------------------------------------------- dĺžka
def test_offer_key_is_short_enough_to_stop_dominating_the_prompt():
    kluc = offer_key_for(TYZDEN, valid_offer())

    assert kluc.startswith(OFFER_KEY_PREFIX)
    assert len(kluc) == len(OFFER_KEY_PREFIX) + OFFER_KEY_DIGEST_CHARS
    assert len(kluc) < 24, "kľúč musí byť rádovo kratší než pôvodných 70 znakov"


def test_digest_length_keeps_a_collision_astronomically_unlikely():
    """48 bitov proti pár tisíc ponukám: narodeninová pravdepodobnosť ~10⁻⁹."""
    assert OFFER_KEY_DIGEST_CHARS >= 12, (
        "kratší odtlačok ako 48 bitov už nie je bezpečný ani s hlasnou kontrolou"
    )


def test_short_key_is_the_prefix_of_the_old_full_length_key():
    """Vďaka tomu je starý dlhý kľúč rozpoznateľný a uložené plány neprasknú."""
    offer = valid_offer()

    assert legacy_offer_key_for(TYZDEN, offer).startswith(offer_key_for(TYZDEN, offer))
    assert len(legacy_offer_key_for(TYZDEN, offer)) == len(OFFER_KEY_PREFIX) + 64


# ------------------------------------------------------- stále tie isté fakty
@pytest.mark.parametrize("pole,hodnota", [
    ("nazov", "Polotučné mlieko"),
    ("cena", 1.20),
    ("povodna", 1.50),
    ("jednotka", "500 ml"),
    ("kategoria", "trvanlive"),
    ("zlava", "-19 %"),
    ("obchod", "Tesco"),
    ("source_url", "https://example.test/other.jpg"),
    ("source_page", 3),
    ("valid_from", "2026-08-16"),
    ("valid_to", "2026-08-24"),
])
def test_every_verified_fact_still_changes_the_short_key(pole, hodnota):
    """Zmenená cena musí zmeniť kľúč — to je jediné, čo ruší staré plány."""
    zaklad = offer_key_for(TYZDEN, valid_offer())

    assert offer_key_for(TYZDEN, valid_offer(**{pole: hodnota})) != zaklad


def test_week_still_changes_the_short_key():
    assert offer_key_for("2026-08-24", valid_offer()) != offer_key_for(TYZDEN, valid_offer())


def test_short_key_is_deterministic():
    assert offer_key_for(TYZDEN, valid_offer()) == offer_key_for(TYZDEN, valid_offer())


# --------------------------------------------------- staré riadky v `akcie`
def test_a_legacy_full_length_key_is_still_accepted_as_the_same_offer():
    """Riadky pozbierané pred skrátením sa nesmú zo dňa na deň stať neplatnými."""
    offer = valid_offer()

    assert offer_key_matches(legacy_offer_key_for(TYZDEN, offer), TYZDEN, offer)
    assert offer_key_matches(offer_key_for(TYZDEN, offer), TYZDEN, offer)


def test_a_tampered_row_is_rejected_in_both_key_formats():
    """Zhovievavosť voči starému formátu nesmie oslabiť kontrolu proti zásahu."""
    offer = valid_offer()
    zdrazeny = valid_offer(cena=1.99)

    assert not offer_key_matches(legacy_offer_key_for(TYZDEN, offer), TYZDEN, zdrazeny)
    assert not offer_key_matches(offer_key_for(TYZDEN, offer), TYZDEN, zdrazeny)
    assert not offer_key_matches("offer_nieco", TYZDEN, offer)
    assert not offer_key_matches(None, TYZDEN, offer)


def test_canonical_form_folds_a_legacy_key_onto_the_short_one():
    """Uložený plán so starým kľúčom sa musí trafiť na tú istú ponuku."""
    offer = valid_offer()
    kratky = offer_key_for(TYZDEN, offer)

    assert canonical_offer_key(legacy_offer_key_for(TYZDEN, offer)) == kratky
    assert canonical_offer_key(kratky) == kratky
    assert canonical_offer_key(None) is None
    assert canonical_offer_key("") == ""


# ------------------------------------------------------------------- kolízia
def kolizny_offer_key(week, offer, _kolizia=["offer_kolizia0000"]):
    """Dva rôzne výrobky, jeden kľúč — presne to, čo sa nesmie ticho podať."""
    return _kolizia[0]


def test_a_forced_collision_between_two_different_offers_is_caught_on_ingestion(monkeypatch):
    con = legacy_connection()
    monkeypatch.setattr("app.offer_data.offer_key_for", kolizny_offer_key)

    with pytest.raises(OfferKeyCollision):
        replace_store_week(con, TYZDEN, "Lidl", [
            valid_offer(nazov="Plnotučné mlieko", cena=1.19),
            valid_offer(nazov="Bravčové karé", cena=4.99, povodna=6.49, source_page=5),
        ])


def test_a_caught_collision_leaves_the_previous_store_week_untouched(monkeypatch):
    con = legacy_connection()
    replace_store_week(con, TYZDEN, "Lidl", [valid_offer(nazov="Staré mlieko")])
    monkeypatch.setattr("app.offer_data.offer_key_for", kolizny_offer_key)

    with pytest.raises(OfferKeyCollision):
        replace_store_week(con, TYZDEN, "Lidl", [
            valid_offer(nazov="Nové mlieko"),
            valid_offer(nazov="Bravčové karé", cena=4.99, povodna=6.49, source_page=5),
        ])

    assert [row[0] for row in con.execute("SELECT nazov FROM akcie")] == ["Staré mlieko"]


def test_a_collision_against_a_row_stored_earlier_is_caught_too(monkeypatch):
    """Kolízia nemusí byť v jednej dávke — druhý obchod ju musí odhaliť tiež."""
    con = legacy_connection()
    monkeypatch.setattr("app.offer_data.offer_key_for", kolizny_offer_key)
    replace_store_week(con, TYZDEN, "Lidl", [valid_offer(nazov="Plnotučné mlieko")])

    with pytest.raises(OfferKeyCollision):
        replace_store_week(
            con, TYZDEN, "Tesco",
            [valid_offer(obchod="Tesco", nazov="Bravčové karé", cena=4.99, povodna=6.49, source_page=5)],
        )


def test_two_identical_rows_are_a_duplicate_not_a_collision():
    """Rovnaké fakty = rovnaký kľúč. To je správne, nie porucha."""
    con = legacy_connection()

    replace_store_week(con, TYZDEN, "Lidl", [valid_offer(), valid_offer()])

    kluce = {row[0] for row in con.execute("SELECT offer_key FROM akcie")}
    assert len(kluce) == 1


def test_the_collision_error_is_loud_and_says_which_key_clashed(monkeypatch):
    con = legacy_connection()
    monkeypatch.setattr("app.offer_data.offer_key_for", kolizny_offer_key)

    with pytest.raises(OfferKeyCollision) as zachytene:
        replace_store_week(con, TYZDEN, "Lidl", [
            valid_offer(nazov="Plnotučné mlieko"),
            valid_offer(nazov="Bravčové karé", cena=4.99, povodna=6.49, source_page=5),
        ])

    assert "offer_kolizia0000" in str(zachytene.value)
    assert isinstance(zachytene.value, ValueError), "volajúci chytajú ValueError"


# ------------------------------------ starý riadok a starý plán po nasadení
def test_a_week_of_legacy_rows_still_reaches_the_plan_in_the_short_form():
    """`akcie` sa prepisujú každý týždeň, ale medzitým nesmie appka onemieť."""
    from datetime import date, timedelta

    from app.weekly_data import current_verified_offers

    dnes = date.today()
    con = legacy_connection()
    con.row_factory = sqlite3.Row
    stary = valid_offer(
        valid_from=(dnes - timedelta(days=1)).isoformat(),
        valid_to=(dnes + timedelta(days=1)).isoformat(),
    )
    tyzden = (dnes - timedelta(days=dnes.weekday())).isoformat()
    con.execute(
        """INSERT INTO akcie (tyzden, obchod, nazov, kategoria, cena, povodna, zlava,
                              jednotka, source_url, source_page, valid_from, valid_to, offer_key)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (tyzden, stary["obchod"], stary["nazov"], stary["kategoria"], stary["cena"],
         stary["povodna"], stary["zlava"], stary["jednotka"], stary["source_url"],
         stary["source_page"], stary["valid_from"], stary["valid_to"],
         legacy_offer_key_for(tyzden, stary)),
    )

    rows = current_verified_offers(con, ["Lidl"], dnes)

    assert [row["nazov"] for row in rows] == ["Plnotučné mlieko"]
    assert rows[0]["offer_key"] == offer_key_for(tyzden, stary), (
        "von ide vždy krátky tvar, aby prompt neplatil za dlhý kľúč"
    )


def test_a_plan_cached_with_long_keys_is_not_declared_stale_by_the_shortening():
    """Používateľ nesmie po nasadení vidieť „plán obsahuje neplatnú ponuku"."""
    from app.plan_data import cached_plan_is_current

    offer = valid_offer()
    plan = {"jedla": [{"suroviny": [{"offer_key": legacy_offer_key_for(TYZDEN, offer)}]}]}

    assert cached_plan_is_current(plan, [{"offer_key": offer_key_for(TYZDEN, offer)}])
    # A naopak: zmenená cena plán zneplatní aj cez starý kľúč.
    assert not cached_plan_is_current(
        plan, [{"offer_key": offer_key_for(TYZDEN, valid_offer(cena=1.29))}])
