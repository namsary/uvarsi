"""Private account export, fresh reauthentication, and safe account erasure."""

from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import secrets


REAUTH_TTL_SECONDS = 5 * 60
DELETED_EMAIL_SUFFIX = "@deleted.invalid"
OPEN_REQUEST_STATUSES = ("received", "processing", "requires_review")


SCHEMA = """
CREATE TABLE IF NOT EXISTS account_reauth_tokens (
  token_hash TEXT PRIMARY KEY,
  user_id INTEGER NOT NULL,
  expires_at REAL NOT NULL,
  created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS account_reauth_tokens_user_idx
  ON account_reauth_tokens(user_id, expires_at);
CREATE TABLE IF NOT EXISTS account_reauth_challenges (
  challenge_hash TEXT PRIMARY KEY,
  user_id INTEGER NOT NULL,
  expires_at REAL NOT NULL,
  created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS account_reauth_challenges_user_idx
  ON account_reauth_challenges(user_id, expires_at);
"""


class AccountNotFound(LookupError):
    """No account with the requested numeric ID exists."""


class AccountDeletionBlocked(RuntimeError):
    """An unresolved withdrawal or complaint must not be destroyed."""


def migrate_account_data_schema(con) -> None:
    con.executescript(SCHEMA)


def _hash(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _valid_user_id(user_id) -> int:
    if not isinstance(user_id, int) or isinstance(user_id, bool) or user_id <= 0:
        raise ValueError("invalid user id")
    return user_id


def _valid_token(raw_token) -> str | None:
    if not isinstance(raw_token, str) or not 20 <= len(raw_token) <= 512:
        return None
    return raw_token


@contextmanager
def _transaction(con, name: str):
    owns_transaction = not con.in_transaction
    savepoint = f"account_{name}"
    if owns_transaction:
        con.execute("BEGIN IMMEDIATE")
    else:
        con.execute(f"SAVEPOINT {savepoint}")
    try:
        yield
    except Exception:
        if owns_transaction:
            con.rollback()
        else:
            con.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
            con.execute(f"RELEASE SAVEPOINT {savepoint}")
        raise
    else:
        if owns_transaction:
            con.commit()
        else:
            con.execute(f"RELEASE SAVEPOINT {savepoint}")


def _table_exists(con, name: str) -> bool:
    return con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def _rows(con, query: str, parameters=()) -> list[dict]:
    cursor = con.execute(query, parameters)
    columns = [item[0] for item in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


def _first(con, query: str, parameters=()) -> dict | None:
    rows = _rows(con, query, parameters)
    return rows[0] if rows else None


def create_reauth_token(con, *, user_id: int, now: float) -> str:
    user_id = _valid_user_id(user_id)
    raw = secrets.token_urlsafe(32)
    with _transaction(con, "reauth_create"):
        con.execute("DELETE FROM account_reauth_tokens WHERE expires_at<=?", (float(now),))
        con.execute(
            "INSERT INTO account_reauth_tokens (token_hash,user_id,expires_at,created_at) VALUES (?,?,?,?)",
            (_hash(raw), user_id, float(now) + REAUTH_TTL_SECONDS, float(now)),
        )
    return raw


def consume_reauth_token(con, *, user_id: int, raw_token, now: float) -> bool:
    user_id = _valid_user_id(user_id)
    raw = _valid_token(raw_token)
    if raw is None:
        return False
    with _transaction(con, "reauth_consume"):
        con.execute("DELETE FROM account_reauth_tokens WHERE expires_at<=?", (float(now),))
        deleted = con.execute(
            "DELETE FROM account_reauth_tokens WHERE token_hash=? AND user_id=? AND expires_at>?",
            (_hash(raw), user_id, float(now)),
        ).rowcount
    return deleted == 1


def create_reauth_challenge(con, *, user_id: int, now: float) -> str:
    user_id = _valid_user_id(user_id)
    raw = secrets.token_urlsafe(32)
    with _transaction(con, "challenge_create"):
        con.execute("DELETE FROM account_reauth_challenges WHERE expires_at<=?", (float(now),))
        con.execute(
            "INSERT INTO account_reauth_challenges (challenge_hash,user_id,expires_at,created_at) VALUES (?,?,?,?)",
            (_hash(raw), user_id, float(now) + REAUTH_TTL_SECONDS, float(now)),
        )
    return raw


def consume_reauth_challenge(con, *, user_id: int, raw_challenge, now: float) -> bool:
    user_id = _valid_user_id(user_id)
    raw = _valid_token(raw_challenge)
    if raw is None:
        return False
    with _transaction(con, "challenge_consume"):
        con.execute("DELETE FROM account_reauth_challenges WHERE expires_at<=?", (float(now),))
        deleted = con.execute(
            "DELETE FROM account_reauth_challenges WHERE challenge_hash=? AND user_id=? AND expires_at>?",
            (_hash(raw), user_id, float(now)),
        ).rowcount
    return deleted == 1


def export_user_data(con, user_id: int) -> dict:
    """Return portable data belonging to one account, never auth secrets."""
    user_id = _valid_user_id(user_id)
    account = _first(
        con,
        """SELECT id,email,vytvoreny,platiaci,osoby,dospeli,deti,frekvencia,
                  obchody,stravovanie,onboarding
           FROM pouzivatelia WHERE id=?""",
        (user_id,),
    )
    if account is None:
        raise AccountNotFound("account not found")

    plans = _rows(
        con,
        """SELECT tyzden AS week,json,vytvoreny AS created_at
           FROM plany WHERE user_id=? ORDER BY tyzden""",
        (user_id,),
    ) if _table_exists(con, "plany") else []
    for plan in plans:
        try:
            plan["plan"] = json.loads(plan.pop("json"))
        except (TypeError, json.JSONDecodeError):
            plan["plan"] = None

    passkeys = _rows(
        con,
        """SELECT name,transports,created_at,last_used_at
           FROM auth_passkeys WHERE user_id=? ORDER BY created_at""",
        (user_id,),
    ) if _table_exists(con, "auth_passkeys") else []
    for passkey in passkeys:
        try:
            passkey["transports"] = json.loads(passkey["transports"])
        except (TypeError, json.JSONDecodeError):
            passkey["transports"] = []

    sessions = _rows(
        con,
        """SELECT device_name,created_at,last_seen_at,expires_at,revoked_at
           FROM sessions_v2 WHERE user_id=? ORDER BY created_at""",
        (user_id,),
    ) if _table_exists(con, "sessions_v2") else []
    entitlements = _rows(
        con,
        """SELECT produkt AS product,poskytovatel AS provider,
                  objednavka_id AS order_id,suma_centy AS amount_cents,
                  mena AS currency,stav AS status,ziskany_o AS purchased_at,
                  zmeneny_o AS changed_at
           FROM naroky WHERE user_id=? ORDER BY ziskany_o""",
        (user_id,),
    ) if _table_exists(con, "naroky") else []
    consents = _rows(
        con,
        """SELECT public_id,product,amount_cents,currency,legal_version,
                  privacy_version,accepted_at,status,provider_order_id
           FROM checkout_attempts WHERE user_id=? ORDER BY accepted_at""",
        (user_id,),
    ) if _table_exists(con, "checkout_attempts") else []
    requests = _rows(
        con,
        """SELECT public_id,order_id,request_type,message,status,refund_scope,
                  purchased_at,created_at,updated_at
           FROM consumer_requests WHERE user_id=? ORDER BY created_at""",
        (user_id,),
    ) if _table_exists(con, "consumer_requests") else []

    return {
        "format": "uvarsi-account-export-v1",
        "account": account,
        "pantry": _rows(
            con,
            """SELECT nazov AS name,mnozstvo AS quantity,jednotka AS unit
               FROM spajza WHERE user_id=? ORDER BY id""",
            (user_id,),
        ) if _table_exists(con, "spajza") else [],
        "plans": plans,
        "security": {"passkeys": passkeys, "sessions": sessions},
        "payments": {
            "entitlements": entitlements,
            "checkout_consents": consents,
            "requests": requests,
        },
    }


def delete_user_account(con, user_id: int, now: float) -> dict:
    """Anonymize one account and remove its non-retained data atomically."""
    user_id = _valid_user_id(user_id)
    with _transaction(con, "delete"):
        account = con.execute(
            "SELECT email FROM pouzivatelia WHERE id=?", (user_id,)
        ).fetchone()
        if account is None:
            raise AccountNotFound("account not found")
        old_email = str(account[0])
        if old_email.endswith(DELETED_EMAIL_SUFFIX):
            return {"deleted": True, "already_deleted": True}

        if _table_exists(con, "consumer_requests"):
            placeholders = ",".join("?" for _ in OPEN_REQUEST_STATUSES)
            active = con.execute(
                f"SELECT 1 FROM consumer_requests WHERE user_id=? AND status IN ({placeholders}) LIMIT 1",
                (user_id, *OPEN_REQUEST_STATUSES),
            ).fetchone()
            if active is not None:
                raise AccountDeletionBlocked(
                    "Najprv musíme dokončiť tvoju otvorenú reklamáciu alebo odstúpenie."
                )

        for table in (
            "spajza", "plany", "prepocty", "profilove_opravy", "plan_jobs",
            "auth_legacy_setup_claims", "auth_setup_sessions", "sessions_v2",
            "auth_credentials", "auth_passkeys", "auth_webauthn_challenges",
            "sedenia", "account_reauth_tokens", "account_reauth_challenges",
        ):
            if _table_exists(con, table):
                con.execute(f"DELETE FROM {table} WHERE user_id=?", (user_id,))

        for table in (
            "magic_tokens_v2", "magic_token_reservations", "auth_email_cooldowns",
            "auth_action_tokens", "auth_password_reset_outbox", "tokeny",
        ):
            if _table_exists(con, table):
                con.execute(f"DELETE FROM {table} WHERE email=?", (old_email,))

        if _table_exists(con, "payment_cases"):
            con.execute("UPDATE payment_cases SET user_id=NULL WHERE user_id=?", (user_id,))

        anonymous_email = f"deleted+{secrets.token_urlsafe(12)}{DELETED_EMAIL_SUFFIX}"
        con.execute(
            """UPDATE pouzivatelia
               SET email=?,platiaci=0,osoby=1,dospeli=1,deti=0,frekvencia=1,
                   obchody='',stravovanie='standard',onboarding=0
               WHERE id=?""",
            (anonymous_email, user_id),
        )
    return {"deleted": True, "already_deleted": False}
