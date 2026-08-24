#!/usr/bin/env python3
"""Uvar.si — pridelenie Premium majiteľovi (len z príkazového riadku na serveri).

Zámerne NEMÁ webovú adresu: nedá sa zavolať zvonku. Nárok sa zapíše ako
poskytovateľ "rucne" so sumou 0, aby sa nikdy nepomiešal s reálnymi tržbami.

Použitie na serveri:
    cd /opt/uvarsi/app
    ../venv/bin/python premium_cli.py <email>              # zapni premium
    ../venv/bin/python premium_cli.py <email> --zrus       # vypni premium
    ../venv/bin/python premium_cli.py <email> --stav       # len zisti stav
Vždy zároveň zahodí uložený plán, aby sa vygeneroval nový podľa aktuálneho kódu.
"""
import argparse
import sqlite3
import sys
import time
from contextlib import closing

sys.path.insert(0, "/opt/uvarsi/app")

import platby  # noqa: E402

DB = "/opt/uvarsi/uvarsi.db"


def spoj():
    con = sqlite3.connect(DB, timeout=20)
    con.row_factory = sqlite3.Row
    return con


def najdi(con, email: str):
    r = con.execute("SELECT id, email FROM pouzivatelia WHERE lower(email)=lower(?)",
                    (email.strip(),)).fetchone()
    if not r:
        zoznam = [x["email"] for x in con.execute(
            "SELECT email FROM pouzivatelia ORDER BY id").fetchall()]
        raise SystemExit(f"Účet '{email}' neexistuje.\nÚčty v databáze: "
                         + (", ".join(zoznam) if zoznam else "(žiadne)"))
    return r


def stav(con, uid: int) -> str:
    r = con.execute("SELECT stav, poskytovatel, suma_centy FROM naroky "
                    "WHERE user_id=? ORDER BY id DESC LIMIT 1", (uid,)).fetchone()
    if not r:
        return "free (žiadny nárok)"
    zaplatene = "ručne (0 €)" if r["poskytovatel"] == "rucne" else f"{r['suma_centy']/100:.2f} €"
    return f"{r['stav']} · {zaplatene}"


def zahod_plan(con, uid: int) -> int:
    n = con.execute("DELETE FROM plany WHERE user_id=?", (uid,)).rowcount
    con.commit()
    return n


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("email")
    p.add_argument("--zrus", action="store_true", help="odobrať premium")
    p.add_argument("--stav", action="store_true", help="iba zobraziť stav")
    a = p.parse_args()

    with closing(spoj()) as con:
        u = najdi(con, a.email)
        uid, email = u["id"], u["email"]
        print(f"  účet:  {email} (id {uid})")
        print(f"  pred:  {stav(con, uid)}")

        if a.stav:
            return 0

        # Stĺpce ziskany_o/zmeneny_o su REAL — patri do nich epocha. `datetime`
        # tam doteraz preslo len cez zastaraly adapter (v novsom Pythone zmizne)
        # a ulozilo sa ako ISO text, takze v jednom stlpci boli dva typy naraz.
        teraz = time.time()
        if a.zrus:
            platby.zrus_narok_rucne(con, user_id=uid, now=teraz)
        else:
            platby.udel_narok_rucne(con, user_id=uid, now=teraz)
        con.commit()

        print(f"  po:    {stav(con, uid)}")
        n = zahod_plan(con, uid)
        print(f"  plán:  zahodených {n} uložených plánov — nový sa vygeneruje pri otvorení")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
