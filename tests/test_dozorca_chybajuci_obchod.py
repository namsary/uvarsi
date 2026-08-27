"""Neúplný zber sa musí dobehnúť, aj keď celkový počet akcií je vysoký.

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


def _dotaz_na_neuplny_zber(skript: str) -> str:
    """Vytiahne zo skriptu SQL, ktorým zisťuje chýbajúce obchody.

    Berie posledný reťazec v úvodzovkách pred `2>/dev/null` — prvý je cesta
    k databáze, druhý je samotný dotaz.
    """
    m = re.search(r'CHYBA_ZBER=\$\(sqlite3\s+"[^"]*"\s*\\?\s*"(.*?)"\s*\\?\s*\n?\s*2>',
                  skript, re.S)
    assert m, "dozorca musí zisťovať neúspešné zbery samostatným SQL dotazom"
    return m.group(1).replace("\n", " ")


def test_dozorca_checks_for_missing_stores_not_only_total_count(skript):
    assert "CHYBA_ZBER" in skript, (
        "dozorca musí kontrolovať stav zberu, nie iba prítomnosť jedného riadka"
    )
    assert re.search(r'CHYBA_ZBER[^\n]*-gt 0', skript), (
        "neúspešný alebo chýbajúci zber musí spustiť zber rovnako ako nízky počet"
    )


def test_all_three_stores_are_covered_by_the_check(skript):
    dotaz = _dotaz_na_neuplny_zber(skript)
    for obchod in OBCHODY:
        assert obchod in dotaz, f"kontrola musí zahŕňať {obchod}"
    assert "zber_stav" in dotaz and "stav='ok'" in dotaz.replace(" ", ""), (
        "jeden náhodný riadok akcie nestačí — každý obchod musí mať úspešný stav zberu"
    )


def test_query_finds_failed_store_even_when_each_store_has_some_rows(tmp_path, skript):
    """Aj 28+1+1 riadkov je neúplný zber, keď Lidl skončil stavom fail."""
    db = tmp_path / "t.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE zber_stav (tyzden TEXT, obchod TEXT, stav TEXT, pocet INTEGER)")
    con.executemany("INSERT INTO zber_stav VALUES (?,?,?,?)", [
        ("2026-08-17", "Kaufland", "ok", 28),
        ("2026-08-17", "Tesco", "ok", 1),
        ("2026-08-17", "Lidl", "fail", 1),
    ])
    con.commit()

    dotaz = _dotaz_na_neuplny_zber(skript).replace("$MON_ISO", "2026-08-17")
    chyba = con.execute(dotaz).fetchone()[0]
    assert chyba == 1, "musí nájsť práve jeden neúspešný obchod (Lidl)"

    con.execute("UPDATE zber_stav SET stav='ok', pocet=75 WHERE obchod='Lidl'")
    con.commit()
    assert con.execute(dotaz).fetchone()[0] == 0, (
        "po doplnení Lidlu už nesmie hlásiť nič chýbajúce"
    )
    con.close()


def test_previous_week_data_does_not_hide_a_missing_store(tmp_path, skript):
    """Minulotýždňový Lidl nesmie vyzerať ako splnená podmienka."""
    db = tmp_path / "t.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE zber_stav (tyzden TEXT, obchod TEXT, stav TEXT, pocet INTEGER)")
    con.executemany("INSERT INTO zber_stav VALUES (?,?,?,?)", [
        ("2026-08-17", "Kaufland", "ok", 100),
        ("2026-08-17", "Tesco", "ok", 100),
        ("2026-08-10", "Lidl", "ok", 100),
    ])
    con.commit()
    dotaz = _dotaz_na_neuplny_zber(skript).replace("$MON_ISO", "2026-08-17")
    assert con.execute(dotaz).fetchone()[0] == 1
    con.close()
