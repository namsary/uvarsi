"""Spätná kompatibilita rozdelenia domácnosti na dospelých a deti."""

from contextlib import closing

from tests.test_server import grant_premium, load_server


def test_existing_household_migrates_without_losing_account_plan_or_premium(monkeypatch, tmp_path):
    server = load_server(monkeypatch, tmp_path, [])
    with closing(server.db()) as con:
        con.execute(
            "INSERT INTO pouzivatelia (id, email, osoby) VALUES (1, 'legacy@uvar.si', 4)"
        )
        con.execute(
            "INSERT INTO plany (user_id, tyzden, json) VALUES (1, '2026-08-24', '{\"plan\":true}')"
        )
        con.commit()
    grant_premium(server, 1, "legacy-order")

    # Nasimuluj databázu z produkcie pred rozdelením domácnosti. Účet, plán aj
    # Premium už existujú a migrácia ich smie iba doplniť, nie prepisovať.
    with closing(server.db()) as con:
        con.execute("ALTER TABLE pouzivatelia DROP COLUMN deti")
        con.execute("ALTER TABLE pouzivatelia DROP COLUMN dospeli")
        con.commit()

    with closing(server.db()) as con:
        server.migruj_schemu(con)
        server.migruj_schemu(con)
        columns = {row[1] for row in con.execute("PRAGMA table_info(pouzivatelia)")}
        user = con.execute(
            "SELECT email, osoby, dospeli, deti FROM pouzivatelia WHERE id=1"
        ).fetchone()
        plan = con.execute("SELECT json FROM plany WHERE user_id=1").fetchone()[0]
        entitlement = con.execute(
            "SELECT stav FROM naroky WHERE user_id=1 AND objednavka_id='legacy-order'"
        ).fetchone()[0]

    assert {"dospeli", "deti"}.issubset(columns)
    assert tuple(user) == ("legacy@uvar.si", 4, 4, 0)
    assert plan == '{"plan":true}'
    assert entitlement == "aktivny"
