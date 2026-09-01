"""Oprava kódu musí zneplatniť staré uložené plány.

Regresia 21. 8. 2026: rozvrh dní varenia bol opravený a nasadený, ale majiteľ
ďalej videl starý (nesprávny) plán — zdieľaná cache ho servírovala, lebo podpis
obsahoval len týždeň, profil, špajzu a ponuky, nie verziu generátora.

`PLAN_ALGO_VERSION` sa preto musí zvýšiť pri každej zmene podoby plánu a musí
vstupovať do podpisu.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

import plan_data  # noqa: E402


ZAKLAD = dict(
    week="2026-08-17",
    stores=["Kaufland", "Tesco"],
    household_size=4,
    frequency=2,
    offer_keys=["a1", "b2", "c3"],
)


def test_algo_version_is_a_positive_integer():
    assert isinstance(plan_data.PLAN_ALGO_VERSION, int)
    assert not isinstance(plan_data.PLAN_ALGO_VERSION, bool)
    assert plan_data.PLAN_ALGO_VERSION == 21, (
        "oddelenie kuchynských základov od skutočnej špajze musí "
        "zneplatniť staršie plány"
    )


def test_portion_standard_has_an_independent_positive_version():
    assert plan_data.PORTION_STANDARD_VERSION == 1


def test_changing_the_generator_version_invalidates_every_cached_plan(monkeypatch):
    stary = plan_data.plan_signature(**ZAKLAD)
    monkeypatch.setattr(plan_data, "PLAN_ALGO_VERSION", plan_data.PLAN_ALGO_VERSION + 1)
    novy = plan_data.plan_signature(**ZAKLAD)
    assert stary != novy, (
        "po zvýšení verzie generátora sa musí zmeniť podpis, inak sa ďalej "
        "servírujú plány poskladané starým (chybným) kódom"
    )


def test_same_version_and_same_profile_still_shares_the_plan():
    """Zdieľanie musí ostať zachované — inak by každý platil vlastné generovanie."""
    assert plan_data.plan_signature(**ZAKLAD) == plan_data.plan_signature(**ZAKLAD)


def test_adults_and_children_each_change_the_shared_signature():
    base = dict(ZAKLAD, household_size=None, adults=2, children=2)
    assert plan_data.plan_signature(**base) != plan_data.plan_signature(
        **dict(base, adults=3, children=1)
    )
    assert plan_data.plan_signature(**base) != plan_data.plan_signature(
        **dict(base, adults=2, children=1)
    )


def test_changing_portion_standard_invalidates_the_shared_signature(monkeypatch):
    base = dict(ZAKLAD, household_size=None, adults=2, children=2)
    old = plan_data.plan_signature(**base)
    monkeypatch.setattr(
        plan_data, "PORTION_STANDARD_VERSION", plan_data.PORTION_STANDARD_VERSION + 1
    )
    assert plan_data.plan_signature(**base) != old


def test_diet_mode_changes_plan_signature():
    standard = plan_data.plan_signature(**ZAKLAD, diet_mode="standard")
    vegan = plan_data.plan_signature(**ZAKLAD, diet_mode="vegan")
    assert standard != vegan


def test_recipe_library_version_changes_plan_signature(monkeypatch):
    old = plan_data.plan_signature(**ZAKLAD)
    monkeypatch.setattr(
        plan_data, "RECIPE_LIBRARY_VERSION", plan_data.RECIPE_LIBRARY_VERSION + 1
    )
    assert plan_data.plan_signature(**ZAKLAD) != old


@pytest.mark.parametrize("mode", [None, "", "vegan ", "Vegan", "keto", 1, True])
def test_plan_signature_rejects_unknown_or_untyped_diet_mode(mode):
    with pytest.raises(ValueError, match="diet mode"):
        plan_data.plan_signature(**ZAKLAD, diet_mode=mode)


@pytest.mark.parametrize("pole,hodnota", [
    ("household_size", 2),
    ("frequency", 3),
    ("week", "2026-08-24"),
])
def test_profile_still_drives_the_signature(pole, hodnota):
    iny = dict(ZAKLAD, **{pole: hodnota})
    assert plan_data.plan_signature(**ZAKLAD) != plan_data.plan_signature(**iny)


def test_version_is_documented_next_to_its_constant():
    """Bez poznámky sa na zvýšenie zabudne — a chyba sa vráti."""
    zdroj = (Path(__file__).resolve().parent.parent / "app" / "plan_data.py").read_text(
        encoding="utf-8")
    i = zdroj.index("PLAN_ALGO_VERSION =")
    kontext = zdroj[max(0, i - 400):i]
    assert "Zvýš" in kontext or "zvýš" in kontext, (
        "pri konštante musí byť pokyn, že sa zvyšuje pri zmene podoby plánu"
    )
    assert "skontroluj_doma" in kontext and "špajz" in kontext, (
        "komentár musí pomenovať oddelenie kuchynských základov od špajze, "
        "kvôli ktorému cache neplatí"
    )
