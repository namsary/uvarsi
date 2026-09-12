"""Account portability and erasure must be isolated, atomic, and auditable."""

import json
import re
import sqlite3

import pytest

from app import account_data, auth_data, customer_requests, plan_jobs, platby


@pytest.fixture
def database():
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.executescript(
        """
        CREATE TABLE pouzivatelia (
          id INTEGER PRIMARY KEY, email TEXT UNIQUE NOT NULL,
          vytvoreny TEXT DEFAULT CURRENT_TIMESTAMP, platiaci INTEGER DEFAULT 0,
          osoby INTEGER DEFAULT 4, dospeli INTEGER DEFAULT 4,
          deti INTEGER NOT NULL DEFAULT 0, frekvencia INTEGER DEFAULT 2,
          obchody TEXT DEFAULT 'Kaufland,Tesco,Lidl',
          stravovanie TEXT NOT NULL DEFAULT 'standard', onboarding INTEGER DEFAULT 0
        );
        CREATE TABLE spajza (
          id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, nazov TEXT NOT NULL,
          mnozstvo REAL, jednotka TEXT
        );
        CREATE TABLE plany (
          id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, tyzden TEXT NOT NULL,
          json TEXT NOT NULL, vytvoreny TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE prepocty (user_id INTEGER, den TEXT, pocet INTEGER);
        CREATE TABLE profilove_opravy (user_id INTEGER, den TEXT);
        CREATE TABLE sedenia (token TEXT PRIMARY KEY, user_id INTEGER, vytvorene TEXT);
        CREATE TABLE tokeny (token TEXT PRIMARY KEY, email TEXT, platny_do TEXT);
        """
    )
    auth_data.migrate_auth_schema(con)
    plan_jobs.migrate_plan_jobs_schema(con)
    platby.migrate_platby_schema(con)
    customer_requests.migrate_customer_requests_schema(con)
    account_data.migrate_account_data_schema(con)
    con.executemany(
        "INSERT INTO pouzivatelia (id,email,platiaci,osoby,dospeli,deti,onboarding) VALUES (?,?,?,?,?,?,1)",
        [
            (7, "seven@example.test", 1, 4, 2, 2),
            (8, "other@example.com", 0, 1, 1, 0),
        ],
    )
    con.executemany(
        "INSERT INTO spajza (id,user_id,nazov,mnozstvo,jednotka) VALUES (?,?,?,?,?)",
        [(1, 7, "ryža", 500, "g"), (2, 8, "tajná cibuľa", 2, "piece")],
    )
    con.executemany(
        "INSERT INTO plany (id,user_id,tyzden,json) VALUES (?,?,?,?)",
        [(1, 7, "2026-09-07", '{"jedla":["rizoto"]}'),
         (2, 8, "2026-09-07", '{"jedla":["cudzie jedlo"]}')],
    )
    auth_data.set_password(
        con, user_id=7,
        password_hash=auth_data.hash_password("bezpečné heslo účtu"), now=900,
    )
    auth_data.create_session(con, user_id=7, now=900, device_name="Edge")
    auth_data.store_passkey(
        con, credential_id="private-credential-id", user_id=7,
        public_key=b"private-public-key", sign_count=1,
        transports=["internal"], name="Martinov telefón", now=900,
    )
    con.execute("INSERT INTO sedenia VALUES ('legacy-secret',7,'2026-09-07')")
    con.execute("INSERT INTO prepocty VALUES (7,'2026-09-07',1)")
    con.execute("INSERT INTO profilove_opravy VALUES (7,'2026-09-07')")
    con.execute(
        """INSERT INTO naroky
           (user_id,produkt,poskytovatel,objednavka_id,suma_centy,mena,stav,ziskany_o,zmeneny_o)
           VALUES (7,?,?,?,?,?,?,?,?)""",
        (platby.PRODUKT_ZAKLADAJUCI, platby.POSKYTOVATEL, "order-7", 3900,
         "EUR", platby.STAV_AKTIVNY, 700.0, 700.0),
    )
    con.execute(
        """INSERT INTO checkout_attempts
           (public_id,user_id,product,amount_cents,currency,legal_version,
            privacy_version,accepted_at,expires_at,status,provider_order_id)
           VALUES ('checkout-7',7,'zakladajuci_clen',3900,'EUR','v1','v1',700,800,'paid','order-7')"""
    )
    con.execute(
        """INSERT INTO payment_cases
           (case_type,provider_order_id,user_id,status,created_at,updated_at)
           VALUES ('manual_review','order-7',7,'closed',700,800)"""
    )
    con.execute(
        """INSERT INTO consumer_requests
           (public_id,user_id,order_id,request_type,message,status,refund_scope,
             purchased_at,created_at,updated_at,legal_version,legal_snapshot,
             confirmation_idempotency_key)
           VALUES ('request-7',7,'order-7','complaint','Vybavený problém.',
                   'resolved',NULL,700,710,800,'v1','archívna právna snímka',
                   'consumer-request/request-7')"""
    )
    con.commit()
    yield con
    con.close()


def test_export_contains_only_authenticated_users_data_and_no_auth_secrets(database):
    exported = account_data.export_user_data(database, user_id=7)
    encoded = json.dumps(exported, ensure_ascii=False)

    assert exported["account"]["id"] == 7
    assert exported["account"]["email"] == "seven@example.test"
    assert exported["pantry"][0]["name"] == "ryža"
    assert exported["plans"][0]["week"] == "2026-09-07"
    assert exported["security"]["passkeys"][0]["name"] == "Martinov telefón"
    assert exported["payments"]["entitlements"][0]["amount_cents"] == 3900
    assert "other@example.com" not in encoded
    assert "tajná cibuľa" not in encoded
    assert "private-credential-id" not in encoded
    assert "private-public-key" not in encoded
    assert "legacy-secret" not in encoded
    assert "$argon2" not in encoded


def test_delete_removes_personal_rows_but_retains_minimal_payment_records(database):
    result = account_data.delete_user_account(database, user_id=7, now=2000)

    assert result["deleted"] is True
    assert database.execute("SELECT COUNT(*) FROM spajza WHERE user_id=7").fetchone()[0] == 0
    assert database.execute("SELECT COUNT(*) FROM plany WHERE user_id=7").fetchone()[0] == 0
    assert database.execute("SELECT COUNT(*) FROM prepocty WHERE user_id=7").fetchone()[0] == 0
    assert database.execute("SELECT COUNT(*) FROM profilove_opravy WHERE user_id=7").fetchone()[0] == 0
    assert database.execute("SELECT COUNT(*) FROM sessions_v2 WHERE user_id=7").fetchone()[0] == 0
    assert database.execute("SELECT COUNT(*) FROM auth_credentials WHERE user_id=7").fetchone()[0] == 0
    assert database.execute("SELECT COUNT(*) FROM auth_passkeys WHERE user_id=7").fetchone()[0] == 0
    assert database.execute("SELECT COUNT(*) FROM sedenia WHERE user_id=7").fetchone()[0] == 0
    assert database.execute("SELECT COUNT(*) FROM naroky WHERE user_id=7").fetchone()[0] == 1
    assert database.execute("SELECT COUNT(*) FROM checkout_attempts WHERE user_id=7").fetchone()[0] == 1
    assert database.execute("SELECT COUNT(*) FROM consumer_requests WHERE user_id=7").fetchone()[0] == 1
    assert database.execute("SELECT user_id FROM payment_cases WHERE provider_order_id='order-7'").fetchone()[0] is None
    account = database.execute(
        "SELECT email,platiaci,osoby,dospeli,deti,obchody,onboarding FROM pouzivatelia WHERE id=7"
    ).fetchone()
    assert re.fullmatch(r"deleted\+[A-Za-z0-9_-]+@deleted\.invalid", account[0])
    assert tuple(account[1:]) == (0, 1, 1, 0, "", 0)


def test_active_consumer_request_blocks_deletion_without_partial_changes(database):
    database.execute(
        "UPDATE consumer_requests SET status='processing' WHERE public_id='request-7'"
    )
    database.commit()

    with pytest.raises(account_data.AccountDeletionBlocked):
        account_data.delete_user_account(database, user_id=7, now=2000)

    assert database.execute("SELECT email FROM pouzivatelia WHERE id=7").fetchone()[0] == "seven@example.test"
    assert database.execute("SELECT COUNT(*) FROM spajza WHERE user_id=7").fetchone()[0] == 1
    assert database.execute("SELECT COUNT(*) FROM auth_credentials WHERE user_id=7").fetchone()[0] == 1


def test_delete_is_idempotent(database):
    first = account_data.delete_user_account(database, user_id=7, now=2000)
    email = database.execute("SELECT email FROM pouzivatelia WHERE id=7").fetchone()[0]
    second = account_data.delete_user_account(database, user_id=7, now=2001)

    assert first["deleted"] is True
    assert second == {"deleted": True, "already_deleted": True}
    assert database.execute("SELECT email FROM pouzivatelia WHERE id=7").fetchone()[0] == email


def test_reauthentication_tokens_are_hashed_short_lived_and_single_use(database):
    raw = account_data.create_reauth_token(database, user_id=7, now=1000)

    stored = database.execute("SELECT token_hash FROM account_reauth_tokens").fetchone()[0]
    assert raw not in stored
    assert account_data.consume_reauth_token(database, user_id=7, raw_token=raw, now=1001) is True
    assert account_data.consume_reauth_token(database, user_id=7, raw_token=raw, now=1002) is False

    expired = account_data.create_reauth_token(database, user_id=7, now=2000)
    assert account_data.consume_reauth_token(
        database, user_id=7, raw_token=expired,
        now=2000 + account_data.REAUTH_TTL_SECONDS + 1,
    ) is False


def test_delete_rolls_back_everything_on_database_failure(database):
    database.execute(
        """CREATE TRIGGER fail_plan_delete BEFORE DELETE ON plany
           WHEN OLD.user_id=7 BEGIN SELECT RAISE(ABORT,'simulated failure'); END"""
    )
    database.commit()

    with pytest.raises(sqlite3.DatabaseError):
        account_data.delete_user_account(database, user_id=7, now=2000)

    assert database.execute("SELECT email FROM pouzivatelia WHERE id=7").fetchone()[0] == "seven@example.test"
    assert database.execute("SELECT COUNT(*) FROM spajza WHERE user_id=7").fetchone()[0] == 1
    assert database.execute("SELECT COUNT(*) FROM plany WHERE user_id=7").fetchone()[0] == 1
