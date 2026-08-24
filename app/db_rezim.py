"""Ako sa otvára SQLite — rovnako pre appku aj pre skripty.

Bez WAL beží SQLite v rollback-journal režime a tam jeden zapisovateľ blokuje
VŠETKÝCH čitateľov. Pri súbežnej prevádzke to znamená, že požiadavky čakajú
celý `timeout` a potom padnú na `database is locked` — a keďže tú istú
databázu používa aj prihlásenie, nepadne len generovanie plánu, ale celá appka.

WAL to obracia: čitatelia a jeden zapisovateľ si navzájom nezavadzajú.

Dve veci treba držať od seba:

  * `journal_mode=WAL` je vlastnosť DATABÁZY — nastaví sa raz a prežije reštart.
    Pragma sa aj tak posiela pri každom otvorení, lebo nová alebo obnovená
    databáza (napr. zo zálohy) o WAL nemusí vedieť a nikto iný ju nezapne.
  * `synchronous` je vlastnosť SPOJENIA — po každom otvorení sa vracia na
    default (FULL), takže sa musí nastaviť zakaždým. S WAL je NORMAL bezpečný
    voči pádu appky aj procesu; stratiť sa dá nanajvýš posledná transakcia pri
    výpadku prúdu, a to za cenu fsync na každom commite nekupujeme.

Pragmy sú zámerne „mäkké": keď ich databáza odmietne (súbeh, pamäťová databáza,
read-only mount), spojenie sa aj tak vráti použiteľné. Zlyhanie ladiacej pragmy
nesmie zhodiť prihlásenie.
"""
import sqlite3

# Rovnaký strop pre appku aj pre skripty. Zberač bežal s defaultom 5 s a vzdal
# sa skôr, než appka stihla dokončiť transakciu.
DB_TIMEOUT = 20.0
ZURNAL = "WAL"
SYNCHRONOUS = "NORMAL"


def nastav_rezim(con) -> None:
    """WAL + rozumný synchronous. Bezpečné volať pri každom otvorení."""
    for pragma in (f"PRAGMA journal_mode={ZURNAL}",
                   f"PRAGMA synchronous={SYNCHRONOUS}"):
        try:
            con.execute(pragma).fetchone()
        except sqlite3.DatabaseError:
            # Databáza pragmu neprijala (súbežný zápis, :memory:, read-only).
            # WAL je vlastnosť databázy, takže o nič trvalé neprichádzame.
            pass


def otvor(cesta, timeout=DB_TIMEOUT):
    """Spojenie pripravené na súbežnú prevádzku. Žiadne migrácie."""
    con = sqlite3.connect(cesta, timeout=timeout)
    con.row_factory = sqlite3.Row
    nastav_rezim(con)
    return con
