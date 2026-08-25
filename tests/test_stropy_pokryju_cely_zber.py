"""Strop musí byť nad cenou poctivého behu, inak z neho je tichý výpadok.

Regresia 21. 8. 2026: v DB bolo 431 akcií a chýbal presne Lidl. Vyzeralo to
ako problém so zdrojom letáku. V skutočnosti denný strop 1,50 € prerušil zber
uprostred — obchody idú v poradí Kaufland → Tesco → Lidl a posledný nedostal
nič. Strop bol kalibrovaný na dobu, keď sa čítala len vzorka 12–14 strán;
po zrušení vzorky stojí beh ~2,50 €.

Tieto testy strážia, aby sa to nezopakovalo: keď niekto zmení rozsah čítania
alebo stropy, musí prejsť tadeto.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

import naklady  # noqa: E402
import zbierac_akcii  # noqa: E402


# Merané 24. 8. 2026 na reálnych letákoch (Kaufland 77, Tesco 31, Lidl 100 strán):
#   fáza 1 (Haiku, 320 px):  19 volaní ≈ 0,03 €
#   fáza 2 (Opus 5, 1500 px): ~27 volaní × 0,093 € ≈ 2,51 €
CENA_POCTIVEHO_ZBERU_EUR = 2.55


def test_denny_strop_pokryje_cely_zber():
    """Bez tohto sa beh preruší v polovici a vypadne posledný obchod."""
    assert naklady.VYCHODZI_DENNY_STROP_EUR > CENA_POCTIVEHO_ZBERU_EUR, (
        f"denný strop {naklady.VYCHODZI_DENNY_STROP_EUR} € nepokryje jeden "
        f"poctivý zber (~{CENA_POCTIVEHO_ZBERU_EUR} €) — zber sa preruší "
        f"uprostred a vypadne posledný obchod v poradí ({zbierac_akcii.STORES[-1]})"
    )


def test_denny_strop_nechá_rezervu_aj_na_plany():
    """V deň zberu musia ísť aj plány a bloček, inak appka ľuďom nič nevygeneruje."""
    zvysok = naklady.VYCHODZI_DENNY_STROP_EUR - CENA_POCTIVEHO_ZBERU_EUR
    assert zvysok >= 1.00, (
        f"po zbere ostáva na deň len {zvysok:.2f} € — v pondelok ráno, keď "
        f"ľudia chcú plán, je to málo"
    )


def test_tyzdenny_strop_zberu_pokryje_beh_aj_opravny_pokus():
    assert naklady.VYCHODZI_TYZDENNY_STROP_ZBER_EUR >= CENA_POCTIVEHO_ZBERU_EUR, (
        "týždenný strop zberu musí pokryť aspoň jeden celý beh"
    )


def test_mesacny_strop_pokryje_stiri_tyzdenne_zbery():
    """Mesiac má 4–5 týždňov; posledný zber nesmie naraziť na mesačný strop."""
    potreba = 4 * CENA_POCTIVEHO_ZBERU_EUR
    assert naklady.VYCHODZI_MESACNY_STROP_EUR > potreba, (
        f"mesačný strop {naklady.VYCHODZI_MESACNY_STROP_EUR} € nepokryje ani "
        f"4 zbery ({potreba:.2f} €) — posledný týždeň v mesiaci ostane bez dát"
    )


def test_mesacny_strop_ostava_v_majitelovom_rozpocte():
    """Poistka opačným smerom — strop nesmie ticho vyrásť do neúnosna."""
    assert naklady.VYCHODZI_MESACNY_STROP_EUR <= 40.00, (
        "majiteľ má na celý projekt ≤100 €/mesiac vrátane marketingu; "
        "mesačný strop nad 40 € treba prebrať s ním, nie ho len zdvihnúť"
    )


def test_stropy_su_navzajom_konzistentne():
    assert (
        naklady.VYCHODZI_DENNY_STROP_EUR <= naklady.VYCHODZI_MESACNY_STROP_EUR
    ), "denný strop nemôže byť vyšší než mesačný"
    assert (
        naklady.VYCHODZI_TYZDENNY_STROP_ZBER_EUR <= naklady.VYCHODZI_MESACNY_STROP_EUR
    ), "týždenný strop zberu nemôže byť vyšší než mesačný"


@pytest.mark.parametrize("premenna", [
    "READ_PX", "READ_BATCH_SIZE", "SCAN_PX", "SCAN_BATCH_SIZE",
])
def test_rozsah_citania_sa_nezmenil_bez_prepoctu_stropov(premenna):
    """Keď sa zmení rozsah čítania, cena behu sa zmení a stropy treba prepočítať.

    Tieto hodnoty sú vstupom do výpočtu CENA_POCTIVEHO_ZBERU_EUR vyššie.
    Ak tento test padne, NEUPRAVUJ len číslo — prepočítaj cenu behu aj stropy.
    """
    ocakavane = {
        "READ_PX": 1500,
        "READ_BATCH_SIZE": 4,
        "SCAN_PX": 320,
        "SCAN_BATCH_SIZE": 12,
    }
    assert getattr(zbierac_akcii, premenna) == ocakavane[premenna], (
        f"{premenna} sa zmenilo — prepočítaj cenu poctivého zberu aj stropy "
        f"v app/naklady.py, inak sa vráti tichý výpadok posledného obchodu"
    )


def test_pocet_behov_zberu_ostava_obmedzeny():
    """Toto bola správna poistka a zafungovala — nesmie sa stratiť."""
    assert naklady.VYCHODZI_LIMIT_BEHOV.get("zber_letakov", 0) == 2
