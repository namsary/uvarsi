"""Chýbajúci obchod sa musí dobehnúť, aj keď celkový počet akcií je vysoký.

Regresia 21. 8. 2026: zber Lidlu zlyhal, Kaufland a Tesco spolu dali 431 akcií.
Dozorca kontroloval len celkový počet (prah 30), ten bol splnený — a Lidl sa
už do konca týždňa nedobehol. Používateľ, ktorý má v nastavení všetky tri
obchody, dostal plán bez Lidlu a nedozvedel sa o tom.
"""
import re
import sqlite3
from pathlib import Path

import pytest


DOZORCA = Path(__file__).resolve().parent.parent / "hetzner" / "dozorca.sh"
OBCHODY = ("Kaufland", "Tesco", "Lidl")


@pytest.fixture(scope="module")
def skript() -> str:
    return DOZORCA.read_text(encoding="utf-8")


def _dotaz_na_chybajuce(skript: str) -> str:
    """Vytiahne zo skriptu SQL, ktorým zisťuje chýbajúce obchody.

    Berie posledný reťazec v úvodzovkách pred `2>/dev/null` — prvý je cesta
    k databáze, druhý je samotný dotaz.
    """
    m = re.search(r'CHYBA_OBCHOD=\$\(sqlite3\s+"[^"]*"\s*\\?\s*"(.*?)"\s*\\?\s*\n?\s*2>',
                  skript, re.S)
    assert m, "dozorca musí zisťovať chýbajúce obchody samostatným SQL dotazom"
    return m.group(1).replace("\n", " ")


def test_dozorca_checks_for_missing_stores_not_only_total_count(skript):
    assert "CHYBA_OBCHOD" in skript, (
        "dozorca musí kontrolovať aj to, či nechýba celý obchod"
    )
    assert re.search(r'CHYBA_OBCHOD[^\n]*-gt 0', skript), (
        "chýbajúci obchod musí spustiť zber rovnako ako nízky počet akcií"
    )


def test_all_three_stores_are_covered_by_the_check(skript):
    dotaz = _dotaz_na_chybajuce(skript)
    for obchod in OBCHODY:
        assert obchod in dotaz, f"kontrola musí zahŕňať {obchod}"


def test_query_finds_the_missing_store_on_real_data(tmp_path, skript):
    """Presne situácia z 21. 8.: 431 akcií, ale Lidl chýba."""
    db = tmp_path / "t.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE akcie (tyzden TEXT, obchod TEXT)")
    con.executemany("INSERT INTO akcie VALUES (?,?)",
                    [("2026-08-17", "Kaufland")] * 200
                    + [("2026-08-17", "Tesco")] * 231)
    con.commit()

    dotaz = _dotaz_na_chybajuce(skript).replace("$MON_ISO", "2026-08-17")
    chyba = con.execute(dotaz).fetchone()[0]
    assert chyba == 1, "musí nájsť práve jeden chýbajúci obchod (Lidl)"

    con.executemany("INSERT INTO akcie VALUES (?,?)", [("2026-08-17", "Lidl")] * 75)
    con.commit()
    assert con.execute(dotaz).fetchone()[0] == 0, (
        "po doplnení Lidlu už nesmie hlásiť nič chýbajúce"
    )
    con.close()


def test_previous_week_data_does_not_hide_a_missing_store(tmp_path, skript):
    """Minulotýždňový Lidl nesmie vyzerať ako splnená podmienka."""
    db = tmp_path / "t.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE akcie (tyzden TEXT, obchod TEXT)")
    con.executemany("INSERT INTO akcie VALUES (?,?)",
                    [("2026-08-17", "Kaufland")] * 100
                    + [("2026-08-17", "Tesco")] * 100
                    + [("2026-08-10", "Lidl")] * 100)
    con.commit()
    dotaz = _dotaz_na_chybajuce(skript).replace("$MON_ISO", "2026-08-17")
    assert con.execute(dotaz).fetchone()[0] == 1
    con.close()
