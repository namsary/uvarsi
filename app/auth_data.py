"""Focused persistence and provider boundaries for passwordless authentication."""

from contextlib import contextmanager
from dataclasses import dataclass
import base64
import hashlib
import hmac
import json
import re
import secrets
import threading

from argon2 import PasswordHasher, Type, extract_parameters
from argon2.exceptions import InvalidHashError, VerificationError


MAGIC_TOKEN_TTL_SECONDS = 60 * 60
MAGIC_RESERVATION_TTL_SECONDS = 5 * 60
EMAIL_COOLDOWN_SECONDS = 60
SESSION_TTL_SECONDS = 90 * 24 * 60 * 60
SESSION_TOUCH_SECONDS = 24 * 60 * 60
CONFIRM_ACTION_TOKEN_TTL_SECONDS = 24 * 60 * 60
PASSWORD_ACTION_TOKEN_TTL_SECONDS = 60 * 60
PASSWORD_RESET_OUTBOX_LEASE_SECONDS = 30
PASSWORD_RESET_OUTBOX_MAX_ATTEMPTS = 3
PASSWORD_RESET_OUTBOX_RETRY_BASE_SECONDS = 5
PASSWORD_RESET_OUTBOX_RETRY_MAX_SECONDS = 60
WEBAUTHN_CHALLENGE_TTL_SECONDS = 5 * 60
PASSWORDLESS_CREDENTIAL_VERSION = 0.0

_ACTION_TOKEN_TTLS = {
    "confirm": CONFIRM_ACTION_TOKEN_TTL_SECONDS,
    "reset": PASSWORD_ACTION_TOKEN_TTL_SECONDS,
    "setup": PASSWORD_ACTION_TOKEN_TTL_SECONDS,
}
_PASSWORD_HASHER = PasswordHasher(type=Type.ID)
_DUMMY_PASSWORD_HASH = _PASSWORD_HASHER.hash("uvarsi generic authentication check")

_LOCAL_PART = r"[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+(?:\.[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+)*"
_DOMAIN_LABEL = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
_EMAIL = re.compile(rf"{_LOCAL_PART}@(?:{_DOMAIN_LABEL}\.)+[A-Za-z]{{2,63}}")
_OPAQUE_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")
_PASSKEY_ID = re.compile(r"[A-Za-z0-9_-]{1,2048}")

AUTH_SCHEMA = """
CREATE TABLE IF NOT EXISTS magic_tokens_v2 (
  token_hash TEXT PRIMARY KEY,
  email TEXT NOT NULL,
  expires_at REAL NOT NULL,
  created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS magic_tokens_v2_email_idx
  ON magic_tokens_v2(email);
CREATE TABLE IF NOT EXISTS magic_token_reservations (
  email TEXT PRIMARY KEY,
  reservation_id TEXT UNIQUE NOT NULL,
  token_hash TEXT UNIQUE NOT NULL,
  expires_at REAL NOT NULL,
  created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS auth_email_cooldowns (
  email TEXT PRIMARY KEY,
  sent_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS auth_legacy_setup_claims (
  user_id INTEGER PRIMARY KEY,
  claimed_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS auth_setup_sessions (
  token_hash TEXT PRIMARY KEY,
  user_id INTEGER NOT NULL,
  expires_at REAL NOT NULL,
  created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS auth_setup_sessions_user_idx
  ON auth_setup_sessions(user_id);
CREATE TABLE IF NOT EXISTS sessions_v2 (
  token_hash TEXT PRIMARY KEY,
  user_id INTEGER NOT NULL,
  expires_at REAL NOT NULL,
  created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS sessions_v2_user_idx
  ON sessions_v2(user_id);
CREATE TABLE IF NOT EXISTS auth_credentials (
  user_id INTEGER PRIMARY KEY,
  password_hash TEXT NOT NULL,
  changed_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS auth_action_tokens (
  token_hash TEXT PRIMARY KEY,
  email TEXT NOT NULL,
  purpose TEXT NOT NULL CHECK(purpose IN ('confirm','reset','setup')),
  pending_password_hash TEXT,
  credential_changed_at REAL,
  expires_at REAL NOT NULL,
  created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS auth_password_reset_outbox (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  email TEXT NOT NULL,
  credential_changed_at REAL,
  requested_at REAL NOT NULL,
  state TEXT NOT NULL CHECK(state IN ('queued','running','sent','skipped','failed')),
  attempts INTEGER NOT NULL DEFAULT 0,
  token_hash TEXT,
  token_seed TEXT,
  idempotency_key TEXT,
  next_attempt_at REAL,
  lease_owner TEXT,
  lease_expires_at REAL,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL,
  delivered_at REAL
);
CREATE INDEX IF NOT EXISTS auth_password_reset_outbox_next
  ON auth_password_reset_outbox(state, created_at, id);
CREATE TABLE IF NOT EXISTS auth_passkeys (
  credential_id TEXT PRIMARY KEY,
  user_id INTEGER NOT NULL,
  public_key BLOB NOT NULL,
  sign_count INTEGER NOT NULL,
  transports TEXT NOT NULL DEFAULT '[]',
  name TEXT NOT NULL,
  created_at REAL NOT NULL,
  last_used_at REAL
);
CREATE TABLE IF NOT EXISTS auth_webauthn_challenges (
  challenge_hash TEXT PRIMARY KEY,
  user_id INTEGER,
  purpose TEXT NOT NULL CHECK(purpose IN ('register','login')),
  expires_at REAL NOT NULL,
  created_at REAL NOT NULL
);
"""


class DeliveryError(RuntimeError):
    """The provider did not verifiably accept a message."""


class EmailCooldown(RuntimeError):
    """A normalized address received an accepted message too recently."""


class EmailRequestInProgress(RuntimeError):
    """A normalized address already has an active provider request."""


class ReservationInvalid(RuntimeError):
    """A pending token reservation is missing, stale, or replaced."""


class MagicTokenInvalid(RuntimeError):
    """A token is unknown or was already consumed."""


class MagicTokenExpired(RuntimeError):
    """A known token passed its absolute expiry."""


class ActionTokenInvalid(RuntimeError):
    """An action token is unknown, purpose-mismatched, or already consumed."""


class ActionTokenExpired(RuntimeError):
    """A known action token passed its absolute expiry."""


class WebAuthnChallengeInvalid(RuntimeError):
    """A WebAuthn challenge is unknown, mismatched, or already consumed."""


class WebAuthnChallengeExpired(RuntimeError):
    """A known WebAuthn challenge passed its five-minute expiry."""


class PasskeyCloneDetected(RuntimeError):
    """A credential returned a non-monotonic signature counter."""


@dataclass(frozen=True)
class PasswordResetDelivery:
    job_id: int
    email: str
    worker_id: str
    raw_token: str | None
    token_hash: str | None
    idempotency_key: str | None


@dataclass(frozen=True)
class DeliveryAccepted:
    provider_id: str
    request_id: str | None = None


@dataclass(frozen=True)
class MagicTokenReservation:
    email: str
    reservation_id: str
    token_hash: str
    raw_token: str


class ClientIpRateLimiter:
    """Bound one-worker beta memory/requests; shared proxy-edge limiting is still required."""

    def __init__(self, max_requests=5, window_seconds=10 * 60, max_clients=10_000):
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self.max_clients = max_clients
        self._requests = {}
        self._lock = threading.Lock()

    def allow(self, client_ip: str, now: float) -> bool:
        cutoff = now - self.window_seconds
        with self._lock:
            for tracked_ip, stamps in tuple(self._requests.items()):
                active = [stamp for stamp in stamps if stamp > cutoff]
                if active:
                    self._requests[tracked_ip] = active
                else:
                    del self._requests[tracked_ip]
            if client_ip not in self._requests and len(self._requests) >= self.max_clients:
                return False
            recent = self._requests.get(client_ip, [])
            if len(recent) >= self.max_requests:
                return False
            recent.append(now)
            self._requests[client_ip] = recent
            return True


def migrate_auth_schema(con) -> None:
    """Create additive auth-v3 tables without trusting legacy plaintext data."""
    con.executescript(AUTH_SCHEMA)
    session_columns = {
        row[1] for row in con.execute("PRAGMA table_info(sessions_v2)")
    }
    for name, column_type in (
        ("last_seen_at", "REAL"),
        ("device_name", "TEXT"),
        ("revoked_at", "REAL"),
    ):
        if name not in session_columns:
            con.execute(f"ALTER TABLE sessions_v2 ADD COLUMN {name} {column_type}")
    action_columns = {
        row[1] for row in con.execute("PRAGMA table_info(auth_action_tokens)")
    }
    if "credential_changed_at" not in action_columns:
        con.execute(
            "ALTER TABLE auth_action_tokens ADD COLUMN credential_changed_at REAL"
        )
    outbox_columns = {
        row[1] for row in con.execute("PRAGMA table_info(auth_password_reset_outbox)")
    }
    for name, column_type in (
        ("token_seed", "TEXT"),
        ("idempotency_key", "TEXT"),
        ("next_attempt_at", "REAL"),
    ):
        if name not in outbox_columns:
            con.execute(
                f"ALTER TABLE auth_password_reset_outbox ADD COLUMN {name} {column_type}"
            )
    con.execute(
        """UPDATE auth_password_reset_outbox
           SET next_attempt_at=COALESCE(next_attempt_at, requested_at)
           WHERE next_attempt_at IS NULL"""
    )
    for (job_id,) in con.execute(
        """SELECT id FROM auth_password_reset_outbox
           WHERE idempotency_key IS NULL"""
    ).fetchall():
        con.execute(
            "UPDATE auth_password_reset_outbox SET idempotency_key=? WHERE id=?",
            (f"password-reset/{secrets.token_urlsafe(24)}", job_id),
        )
    con.execute(
        """CREATE INDEX IF NOT EXISTS auth_password_reset_outbox_due
           ON auth_password_reset_outbox(state, next_attempt_at, created_at, id)"""
    )
    # Tokens created before credential generation was recorded cannot prove
    # which password state authorized them. Fail closed during migration and
    # retain the consume-time check for databases upgraded while a process runs.
    con.execute(
        """DELETE FROM auth_action_tokens
           WHERE purpose IN ('reset', 'setup') AND credential_changed_at IS NULL"""
    )
    if con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='pouzivatelia'"
    ).fetchone():
        # Fix-round 1 could persist a claim before password setup. Such rows
        # represent interrupted migration, not completion, and must be reopened.
        con.execute(
            """DELETE FROM auth_legacy_setup_claims
               WHERE NOT EXISTS (
                 SELECT 1 FROM auth_credentials c
                 WHERE c.user_id=auth_legacy_setup_claims.user_id
               )"""
        )


def token_hash(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


@contextmanager
def _session_mutation(con, savepoint: str):
    owns_transaction = not con.in_transaction
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


def create_webauthn_challenge(
    con, *, purpose: str, now: float, user_id: int | None
) -> str:
    """Persist only a digest of a fresh five-minute ceremony challenge."""
    if purpose not in {"register", "login"}:
        raise ValueError("invalid WebAuthn challenge purpose")
    raw_challenge = secrets.token_urlsafe(32)
    with _session_mutation(con, "webauthn_challenge_create"):
        con.execute(
            """INSERT INTO auth_webauthn_challenges
               (challenge_hash, user_id, purpose, expires_at, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (
                token_hash(raw_challenge),
                user_id,
                purpose,
                now + WEBAUTHN_CHALLENGE_TTL_SECONDS,
                now,
            ),
        )
    return raw_challenge


def consume_webauthn_challenge(
    con,
    *,
    raw_challenge: object,
    purpose: str,
    now: float,
    expected_user_id: int | None = None,
) -> dict:
    """Delete one challenge and leave its successful transaction to the caller.

    The caller must commit only after the verified credential mutation or
    session creation succeeds, and roll back operational failures.
    """
    if (
        not isinstance(raw_challenge, str)
        or not raw_challenge
        or len(raw_challenge) > 512
        or purpose not in {"register", "login"}
    ):
        raise WebAuthnChallengeInvalid("invalid challenge")
    if con.in_transaction:
        raise RuntimeError("WebAuthn challenge consumption needs a clean connection")
    digest = token_hash(raw_challenge)
    con.execute("BEGIN IMMEDIATE")
    try:
        row = con.execute(
            """SELECT user_id, purpose, expires_at
               FROM auth_webauthn_challenges WHERE challenge_hash=?""",
            (digest,),
        ).fetchone()
        if row is None or row[1] != purpose:
            con.rollback()
            raise WebAuthnChallengeInvalid("invalid challenge")
        if expected_user_id is not None and row[0] != expected_user_id:
            con.rollback()
            raise WebAuthnChallengeInvalid("challenge owner mismatch")
        if float(row[2]) <= now:
            con.execute(
                "DELETE FROM auth_webauthn_challenges WHERE challenge_hash=?",
                (digest,),
            )
            con.commit()
            raise WebAuthnChallengeExpired("expired challenge")
        con.execute(
            "DELETE FROM auth_webauthn_challenges WHERE challenge_hash=?",
            (digest,),
        )
        return {
            "user_id": int(row[0]) if row[0] is not None else None,
            "purpose": row[1],
        }
    except (WebAuthnChallengeInvalid, WebAuthnChallengeExpired):
        raise
    except Exception:
        if con.in_transaction:
            con.rollback()
        raise


def _validated_passkey_id(credential_id: object) -> str:
    if (
        not isinstance(credential_id, str)
        or _PASSKEY_ID.fullmatch(credential_id) is None
    ):
        raise ValueError("invalid passkey credential id")
    return credential_id


def store_passkey(
    con,
    *,
    credential_id: str,
    user_id: int,
    public_key: bytes,
    sign_count: int,
    transports: list[str],
    name: str,
    now: float,
) -> None:
    credential_id = _validated_passkey_id(credential_id)
    if not isinstance(public_key, bytes) or not public_key:
        raise ValueError("invalid passkey public key")
    if (
        not isinstance(sign_count, int)
        or isinstance(sign_count, bool)
        or sign_count < 0
    ):
        raise ValueError("invalid passkey sign count")
    if not isinstance(name, str) or not name:
        raise ValueError("invalid passkey name")
    with _session_mutation(con, "passkey_store"):
        con.execute(
            """INSERT INTO auth_passkeys
               (credential_id, user_id, public_key, sign_count, transports,
                name, created_at, last_used_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, NULL)""",
            (
                credential_id,
                user_id,
                public_key,
                sign_count,
                json.dumps(transports, separators=(",", ":")),
                name,
                now,
            ),
        )


def passkey_for_credential(con, *, credential_id: object) -> dict | None:
    try:
        credential_id = _validated_passkey_id(credential_id)
    except ValueError:
        return None
    row = con.execute(
        """SELECT credential_id, user_id, public_key, sign_count, transports,
                  name, created_at, last_used_at
           FROM auth_passkeys WHERE credential_id=?""",
        (credential_id,),
    ).fetchone()
    if row is None:
        return None
    return {
        "credential_id": row["credential_id"],
        "user_id": int(row["user_id"]),
        "public_key": bytes(row["public_key"]),
        "sign_count": int(row["sign_count"]),
        "transports": json.loads(row["transports"]),
        "name": row["name"],
        "created_at": row["created_at"],
        "last_used_at": row["last_used_at"],
    }


def list_passkeys(con, *, user_id: int) -> list[dict]:
    rows = con.execute(
        """SELECT credential_id, transports, name, created_at, last_used_at
           FROM auth_passkeys WHERE user_id=?
           ORDER BY created_at DESC, credential_id""",
        (user_id,),
    ).fetchall()
    return [
        {
            "credential_id": row["credential_id"],
            "name": row["name"],
            "transports": json.loads(row["transports"]),
            "created_at": row["created_at"],
            "last_used_at": row["last_used_at"],
        }
        for row in rows
    ]


def update_passkey_use(
    con, *, credential_id: str, user_id: int, new_sign_count: int, now: float
) -> None:
    credential_id = _validated_passkey_id(credential_id)
    if (
        not isinstance(new_sign_count, int)
        or isinstance(new_sign_count, bool)
        or new_sign_count < 0
    ):
        raise PasskeyCloneDetected("invalid sign count")
    row = con.execute(
        """SELECT sign_count FROM auth_passkeys
           WHERE credential_id=? AND user_id=?""",
        (credential_id, user_id),
    ).fetchone()
    if row is None:
        raise ValueError("passkey not found")
    current_sign_count = int(row[0])
    if (current_sign_count > 0 or new_sign_count > 0) and (
        new_sign_count <= current_sign_count
    ):
        raise PasskeyCloneDetected("non-monotonic sign count")
    with _session_mutation(con, "passkey_use"):
        con.execute(
            """UPDATE auth_passkeys SET sign_count=?, last_used_at=?
               WHERE credential_id=? AND user_id=?""",
            (new_sign_count, now, credential_id, user_id),
        )


def delete_passkey(con, *, user_id: int, credential_id: object) -> bool:
    try:
        credential_id = _validated_passkey_id(credential_id)
    except ValueError:
        return False
    with _session_mutation(con, "passkey_delete"):
        deleted = con.execute(
            "DELETE FROM auth_passkeys WHERE user_id=? AND credential_id=?",
            (user_id, credential_id),
        ).rowcount
    return deleted == 1


def validate_password(value: object) -> str:
    if not isinstance(value, str) or not 10 <= len(value) <= 128:
        raise ValueError("invalid password")
    return value


def _is_argon2id_hash(encoded: object) -> bool:
    if not isinstance(encoded, str) or not encoded.startswith("$argon2id$"):
        return False
    try:
        return extract_parameters(encoded).type is Type.ID
    except InvalidHashError:
        return False


def hash_password(password: str) -> str:
    return _PASSWORD_HASHER.hash(validate_password(password))


def verify_password(encoded: str, password: str) -> bool:
    if not _is_argon2id_hash(encoded) or not isinstance(password, str):
        return False
    try:
        return _PASSWORD_HASHER.verify(encoded, password)
    except (InvalidHashError, VerificationError):
        return False


def password_needs_rehash(encoded: str) -> bool:
    if not _is_argon2id_hash(encoded):
        return True
    try:
        return _PASSWORD_HASHER.check_needs_rehash(encoded)
    except InvalidHashError:
        return True


def _require_argon2id_hash(encoded: object) -> str:
    if not _is_argon2id_hash(encoded):
        raise ValueError("invalid password hash")
    return encoded


def _insert_action_token(
    con,
    *,
    email: str,
    purpose: str,
    now: float,
    pending_password_hash: str | None = None,
    credential_changed_at: float | None = None,
    raw_token: str | None = None,
) -> str:
    ttl = _ACTION_TOKEN_TTLS.get(purpose)
    if ttl is None:
        raise ValueError("invalid action token purpose")
    normalized_email = normalize_email(email)
    if pending_password_hash is not None:
        pending_password_hash = _require_argon2id_hash(pending_password_hash)

    account_table_exists = con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='pouzivatelia'"
    ).fetchone() is not None
    if purpose in {"reset", "setup"} and credential_changed_at is None and account_table_exists:
        credential = con.execute(
            """SELECT c.changed_at
               FROM pouzivatelia p
               LEFT JOIN auth_credentials c ON c.user_id=p.id
               WHERE p.email=?""",
            (normalized_email,),
        ).fetchone()
        if credential is not None:
            if purpose == "reset" and credential[0] is not None:
                credential_changed_at = float(credential[0])
            elif purpose == "setup" and credential[0] is None:
                credential_changed_at = PASSWORDLESS_CREDENTIAL_VERSION

    if credential_changed_at is not None:
        credential_changed_at = float(credential_changed_at)

    raw_token = raw_token or secrets.token_urlsafe(32)
    con.execute("DELETE FROM auth_action_tokens WHERE expires_at <= ?", (now,))
    con.execute(
        """INSERT INTO auth_action_tokens
           (token_hash, email, purpose, pending_password_hash,
            credential_changed_at, expires_at, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            token_hash(raw_token),
            normalized_email,
            purpose,
            pending_password_hash,
            credential_changed_at,
            now + ttl,
            now,
        ),
    )
    return raw_token


def create_action_token(
    con,
    *,
    email: str,
    purpose: str,
    now: float,
    pending_password_hash: str | None = None,
    credential_changed_at: float | None = None,
) -> str:
    raw_token = _insert_action_token(
        con,
        email=email,
        purpose=purpose,
        now=now,
        pending_password_hash=pending_password_hash,
        credential_changed_at=credential_changed_at,
    )
    con.commit()
    return raw_token


def consume_action_token(
    con, *, raw_token: str, purpose: str, now: float
) -> dict:
    if purpose not in _ACTION_TOKEN_TTLS:
        raise ValueError("invalid action token purpose")
    if not isinstance(raw_token, str) or not raw_token or len(raw_token) > 512:
        raise ActionTokenInvalid("invalid token")

    digest = token_hash(raw_token)
    owns_transaction = not con.in_transaction
    savepoint = "action_token_consume"
    if owns_transaction:
        con.execute("BEGIN IMMEDIATE")
    else:
        con.execute(f"SAVEPOINT {savepoint}")
    try:
        row = con.execute(
            """SELECT email, purpose, pending_password_hash, expires_at,
                      credential_changed_at
               FROM auth_action_tokens
               WHERE token_hash=? AND purpose=?""",
            (digest, purpose),
        ).fetchone()
        if row is None:
            raise ActionTokenInvalid("invalid token")
        if float(row[3]) <= now:
            con.execute("DELETE FROM auth_action_tokens WHERE token_hash=?", (digest,))
            if owns_transaction:
                con.commit()
            else:
                con.execute(f"RELEASE SAVEPOINT {savepoint}")
            raise ActionTokenExpired("expired token")
        account_table_exists = con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='pouzivatelia'"
        ).fetchone() is not None
        if purpose in {"reset", "setup"} and account_table_exists and row[4] is None:
            raise ActionTokenInvalid("token has no credential generation")
        if purpose == "reset" and row[4] is not None:
            credential = con.execute(
                """SELECT c.changed_at
                   FROM pouzivatelia p
                   JOIN auth_credentials c ON c.user_id=p.id
                   WHERE p.email=?""",
                (row[0],),
            ).fetchone()
            if credential is None or float(credential[0]) != float(row[4]):
                raise ActionTokenInvalid("credential changed after token generation")
        if purpose == "setup" and account_table_exists:
            if float(row[4]) != PASSWORDLESS_CREDENTIAL_VERSION:
                raise ActionTokenInvalid("invalid setup credential generation")
            if con.execute(
                """SELECT 1 FROM pouzivatelia p
                   JOIN auth_credentials c ON c.user_id=p.id
                   WHERE p.email=?""",
                (row[0],),
            ).fetchone():
                raise ActionTokenInvalid("password setup was already completed")

        if purpose in {"reset", "setup"}:
            con.execute(
                """DELETE FROM auth_action_tokens
                   WHERE email=? AND purpose IN ('reset', 'setup')""",
                (row[0],),
            )
        else:
            con.execute("DELETE FROM auth_action_tokens WHERE token_hash=?", (digest,))
        if not owns_transaction:
            con.execute(f"RELEASE SAVEPOINT {savepoint}")
        return {
            "email": row[0],
            "purpose": row[1],
            "pending_password_hash": row[2],
        }
    except ActionTokenExpired:
        raise
    except ActionTokenInvalid:
        if owns_transaction:
            con.rollback()
        else:
            con.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
            con.execute(f"RELEASE SAVEPOINT {savepoint}")
        raise
    except Exception:
        if owns_transaction:
            con.rollback()
        else:
            con.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
            con.execute(f"RELEASE SAVEPOINT {savepoint}")
        raise


def enqueue_password_reset_job(
    con, *, email: str, requested_at: float
) -> int:
    """Persist one constant-shaped reset request with its credential version."""
    normalized_email = normalize_email(email)
    with _session_mutation(con, "password_reset_enqueue"):
        credential = con.execute(
            """SELECT c.changed_at
               FROM pouzivatelia p
               JOIN auth_credentials c ON c.user_id=p.id
               WHERE p.email=?""",
            (normalized_email,),
        ).fetchone()
        changed_at = float(credential[0]) if credential is not None else None
        cursor = con.execute(
            """INSERT INTO auth_password_reset_outbox
               (email, credential_changed_at, requested_at, state,
                token_seed, idempotency_key, next_attempt_at,
                created_at, updated_at)
               VALUES (?, ?, ?, 'queued', ?, ?, ?, ?, ?)""",
            (
                normalized_email,
                changed_at,
                float(requested_at),
                secrets.token_urlsafe(32),
                f"password-reset/{secrets.token_urlsafe(24)}",
                float(requested_at),
                float(requested_at),
                float(requested_at),
            ),
        )
        job_id = int(cursor.lastrowid)
    return job_id


def _recover_password_reset_leases(con, *, now: float) -> None:
    expired = con.execute(
        """SELECT token_hash FROM auth_password_reset_outbox
           WHERE state='running' AND lease_expires_at <= ?
             AND attempts >= ? AND token_hash IS NOT NULL""",
        (now, PASSWORD_RESET_OUTBOX_MAX_ATTEMPTS),
    ).fetchall()
    if expired:
        con.executemany(
            "DELETE FROM auth_action_tokens WHERE token_hash=?", expired
        )
    con.execute(
        """UPDATE auth_password_reset_outbox
           SET state=CASE WHEN attempts < ? THEN 'queued' ELSE 'failed' END,
               token_hash=CASE WHEN attempts < ? THEN token_hash ELSE NULL END,
               next_attempt_at=CASE WHEN attempts < ? THEN ? ELSE next_attempt_at END,
               lease_owner=NULL, lease_expires_at=NULL, updated_at=?
           WHERE state='running' AND lease_expires_at <= ?""",
        (
            PASSWORD_RESET_OUTBOX_MAX_ATTEMPTS,
            PASSWORD_RESET_OUTBOX_MAX_ATTEMPTS,
            PASSWORD_RESET_OUTBOX_MAX_ATTEMPTS,
            now,
            now,
            now,
        ),
    )


def _password_reset_raw_token(*, token_secret: str, token_seed: str) -> str:
    if not isinstance(token_secret, str) or not token_secret:
        raise ValueError("password reset token secret is required")
    digest = hmac.new(
        token_secret.encode("utf-8"),
        f"uvarsi-password-reset-v1:{token_seed}".encode("utf-8"),
        hashlib.sha256,
    ).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def claim_password_reset_job(
    con,
    *,
    worker_id: str,
    now: float,
    token_secret: str | None,
    lease_seconds: int = PASSWORD_RESET_OUTBOX_LEASE_SECONDS,
) -> PasswordResetDelivery | None:
    """Lease one job and mint its version-bound token in one transaction."""
    con.execute("BEGIN IMMEDIATE")
    try:
        _recover_password_reset_leases(con, now=now)
        if not token_secret:
            con.commit()
            return None
        row = con.execute(
            """SELECT id, email, credential_changed_at, token_hash,
                      token_seed, idempotency_key
               FROM auth_password_reset_outbox
               WHERE state='queued' AND attempts < ? AND next_attempt_at <= ?
               ORDER BY created_at, id LIMIT 1""",
            (PASSWORD_RESET_OUTBOX_MAX_ATTEMPTS, now),
        ).fetchone()
        if row is None:
            con.commit()
            return None
        job_id, email, expected_changed_at, stored_token_hash, token_seed, idempotency_key = row
        con.execute(
            """UPDATE auth_password_reset_outbox
               SET state='running', attempts=attempts+1, lease_owner=?,
                   lease_expires_at=?, updated_at=?
               WHERE id=?""",
            (worker_id, now + lease_seconds, now, job_id),
        )
        credential = con.execute(
            """SELECT c.changed_at
               FROM pouzivatelia p
               JOIN auth_credentials c ON c.user_id=p.id
               WHERE p.email=?""",
            (email,),
        ).fetchone()
        current_changed_at = float(credential[0]) if credential is not None else None
        if (
            expected_changed_at is None
            or current_changed_at is None
            or float(expected_changed_at) != current_changed_at
        ):
            con.execute(
                """UPDATE auth_password_reset_outbox
                   SET state='skipped', lease_owner=NULL, lease_expires_at=NULL,
                       updated_at=? WHERE id=?""",
                (now, job_id),
            )
            con.commit()
            return PasswordResetDelivery(
                job_id=int(job_id),
                email=email,
                worker_id=worker_id,
                raw_token=None,
                token_hash=None,
                idempotency_key=None,
            )

        if not isinstance(token_seed, str) or not token_seed:
            token_seed = secrets.token_urlsafe(32)
            con.execute(
                "UPDATE auth_password_reset_outbox SET token_seed=? WHERE id=?",
                (token_seed, job_id),
            )
        if not isinstance(idempotency_key, str) or not idempotency_key:
            idempotency_key = f"password-reset/{secrets.token_urlsafe(24)}"
            con.execute(
                "UPDATE auth_password_reset_outbox SET idempotency_key=? WHERE id=?",
                (idempotency_key, job_id),
            )
        raw_token = _password_reset_raw_token(
            token_secret=token_secret, token_seed=token_seed
        )
        digest = token_hash(raw_token)
        if stored_token_hash is not None and stored_token_hash != digest:
            # A provider/token key changed during an uncertain delivery. Keep
            # the already-delivered link valid and stop retrying a changed body
            # under the same provider idempotency key.
            con.execute(
                """UPDATE auth_password_reset_outbox
                   SET state='failed', lease_owner=NULL, lease_expires_at=NULL,
                       updated_at=? WHERE id=?""",
                (now, job_id),
            )
            con.commit()
            return PasswordResetDelivery(
                job_id=int(job_id),
                email=email,
                worker_id=worker_id,
                raw_token=None,
                token_hash=None,
                idempotency_key=None,
            )
        token_row = con.execute(
            "SELECT expires_at FROM auth_action_tokens WHERE token_hash=?",
            (digest,),
        ).fetchone()
        if token_row is None or float(token_row[0]) <= now:
            con.execute("DELETE FROM auth_action_tokens WHERE token_hash=?", (digest,))
            _insert_action_token(
                con,
                email=email,
                purpose="reset",
                now=now,
                credential_changed_at=float(expected_changed_at),
                raw_token=raw_token,
            )
        con.execute(
            "UPDATE auth_password_reset_outbox SET token_hash=? WHERE id=?",
            (digest, job_id),
        )
        con.commit()
        return PasswordResetDelivery(
            job_id=int(job_id),
            email=email,
            worker_id=worker_id,
            raw_token=raw_token,
            token_hash=digest,
            idempotency_key=idempotency_key,
        )
    except Exception:
        if con.in_transaction:
            con.rollback()
        raise


def finish_password_reset_job(
    con,
    delivery: PasswordResetDelivery,
    *,
    accepted: bool,
    now: float,
) -> bool:
    """Finish or safely requeue one leased delivery without retaining raw tokens."""
    if delivery.raw_token is None or delivery.token_hash is None:
        return False
    con.execute("BEGIN IMMEDIATE")
    try:
        row = con.execute(
            """SELECT attempts FROM auth_password_reset_outbox
               WHERE id=? AND state='running' AND lease_owner=? AND token_hash=?""",
            (delivery.job_id, delivery.worker_id, delivery.token_hash),
        ).fetchone()
        if row is None:
            con.rollback()
            return False
        if accepted:
            con.execute(
                """UPDATE auth_password_reset_outbox
                   SET state='sent', lease_owner=NULL, lease_expires_at=NULL,
                       delivered_at=?, updated_at=? WHERE id=?""",
                (now, now, delivery.job_id),
            )
        else:
            next_state = (
                "queued"
                if int(row[0]) < PASSWORD_RESET_OUTBOX_MAX_ATTEMPTS
                else "failed"
            )
            if next_state == "failed":
                con.execute(
                    "DELETE FROM auth_action_tokens WHERE token_hash=?",
                    (delivery.token_hash,),
                )
            delay = min(
                PASSWORD_RESET_OUTBOX_RETRY_BASE_SECONDS * (2 ** (int(row[0]) - 1)),
                PASSWORD_RESET_OUTBOX_RETRY_MAX_SECONDS,
            )
            con.execute(
                """UPDATE auth_password_reset_outbox
                   SET state=?, token_hash=CASE WHEN ?='queued' THEN token_hash ELSE NULL END,
                       next_attempt_at=CASE WHEN ?='queued' THEN ? ELSE next_attempt_at END,
                       lease_owner=NULL, lease_expires_at=NULL, updated_at=? WHERE id=?""",
                (
                    next_state,
                    next_state,
                    next_state,
                    now + delay,
                    now,
                    delivery.job_id,
                ),
            )
        con.commit()
        return True
    except Exception:
        if con.in_transaction:
            con.rollback()
        raise


def password_reset_outbox_next_wake(con, *, now: float) -> float | None:
    """Return the next durable retry or abandoned-lease recovery time."""
    row = con.execute(
        """SELECT MIN(wake_at) FROM (
             SELECT next_attempt_at AS wake_at
             FROM auth_password_reset_outbox
             WHERE state='queued' AND attempts < ?
             UNION ALL
             SELECT lease_expires_at AS wake_at
             FROM auth_password_reset_outbox
             WHERE state='running'
           ) WHERE wake_at IS NOT NULL""",
        (PASSWORD_RESET_OUTBOX_MAX_ATTEMPTS,),
    ).fetchone()
    if row is None or row[0] is None:
        return None
    return max(float(now), float(row[0]))


def set_password(
    con, *, user_id: int, password_hash: str, now: float
) -> None:
    encoded = _require_argon2id_hash(password_hash)
    with _session_mutation(con, "password_set"):
        con.execute(
            """INSERT INTO auth_credentials (user_id, password_hash, changed_at)
               VALUES (?, ?, ?)
               ON CONFLICT(user_id) DO UPDATE SET
                 password_hash=excluded.password_hash,
                 changed_at=excluded.changed_at""",
            (user_id, encoded, now),
        )


def authenticate_password(con, *, email: str, password: str) -> int | None:
    if not isinstance(email, str) or not isinstance(password, str):
        return None
    row = con.execute(
        """SELECT p.id, c.password_hash
           FROM pouzivatelia p
           JOIN auth_credentials c ON c.user_id=p.id
           WHERE p.email=?""",
        (email,),
    ).fetchone()
    encoded = row[1] if row is not None else _DUMMY_PASSWORD_HASH
    verified = verify_password(encoded, password)
    if row is None or not verified:
        return None
    return int(row[0])


def normalize_email(value) -> str:
    if not isinstance(value, str):
        raise ValueError("invalid email")
    normalized = value.strip().lower()
    if not normalized.isascii() or len(normalized) > 254 or normalized.count("@") != 1:
        raise ValueError("invalid email")
    local_part, domain = normalized.split("@")
    labels = domain.split(".")
    if (
        not local_part
        or len(local_part) > 64
        or not domain
        or len(domain) > 253
        or any(not label or len(label) > 63 for label in labels)
        or _EMAIL.fullmatch(normalized) is None
    ):
        raise ValueError("invalid email")
    return normalized


def _safe_opaque_id(value) -> str | None:
    return value if isinstance(value, str) and _OPAQUE_ID.fullmatch(value) else None


def send_resend_message(
    *, api_key, sender, recipient, subject, text, html, idempotency_key=None
):
    """Return typed acceptance or raise without logging message/provider bodies."""
    if not api_key:
        raise DeliveryError("provider unavailable")
    try:
        import requests

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        if idempotency_key is not None:
            safe_key = _safe_opaque_id(idempotency_key.replace("/", "_"))
            if safe_key is None:
                raise DeliveryError("invalid provider idempotency key")
            headers["Idempotency-Key"] = idempotency_key
        response = requests.post(
            "https://api.resend.com/emails",
            headers=headers,
            json={
                "from": sender,
                "to": [recipient],
                "subject": subject,
                "text": text,
                "html": html,
            },
            timeout=20,
            allow_redirects=False,
        )
    except Exception as exc:
        raise DeliveryError("provider unavailable") from exc

    status_code = getattr(response, "status_code", None)
    if not isinstance(status_code, int) or not 200 <= status_code < 300:
        raise DeliveryError("provider rejected message")
    try:
        payload = response.json()
    except Exception as exc:
        raise DeliveryError("provider returned malformed acceptance") from exc
    provider_id = _safe_opaque_id(payload.get("id") if isinstance(payload, dict) else None)
    if provider_id is None:
        raise DeliveryError("provider returned malformed acceptance")
    headers = getattr(response, "headers", {})
    request_id = _safe_opaque_id(headers.get("x-request-id") if hasattr(headers, "get") else None)
    return DeliveryAccepted(provider_id=provider_id, request_id=request_id)


def reserve_magic_token(con, *, email: str, now: float) -> MagicTokenReservation:
    """Commit a short-lived hashed reservation without touching an older active token."""
    raw_token = secrets.token_urlsafe(32)
    reservation = MagicTokenReservation(
        email=email,
        reservation_id=secrets.token_urlsafe(24),
        token_hash=token_hash(raw_token),
        raw_token=raw_token,
    )
    con.execute("DELETE FROM magic_tokens_v2 WHERE expires_at <= ?", (now,))
    con.execute("DELETE FROM magic_token_reservations WHERE expires_at <= ?", (now,))
    con.commit()
    con.execute("BEGIN IMMEDIATE")
    try:
        cooldown = con.execute(
            "SELECT sent_at FROM auth_email_cooldowns WHERE email=?", (email,)
        ).fetchone()
        if cooldown is not None and float(cooldown[0]) > now - EMAIL_COOLDOWN_SECONDS:
            raise EmailCooldown("email cooldown active")
        if con.execute(
            "SELECT 1 FROM magic_token_reservations WHERE email=?", (email,)
        ).fetchone():
            raise EmailRequestInProgress("email request in progress")
        con.execute(
            """INSERT INTO magic_token_reservations
               (email, reservation_id, token_hash, expires_at, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (
                email,
                reservation.reservation_id,
                reservation.token_hash,
                now + MAGIC_RESERVATION_TTL_SECONDS,
                now,
            ),
        )
        con.commit()
        return reservation
    except Exception:
        con.rollback()
        raise


def promote_magic_token(con, *, reservation: MagicTokenReservation, now: float, accepted):
    """Atomically replace prior links only for the still-current accepted reservation."""
    if not isinstance(accepted, DeliveryAccepted):
        raise DeliveryError("provider acceptance was not typed")
    con.execute("BEGIN IMMEDIATE")
    try:
        pending = con.execute(
            """SELECT 1 FROM magic_token_reservations
               WHERE email=? AND reservation_id=? AND token_hash=? AND expires_at>?""",
            (
                reservation.email,
                reservation.reservation_id,
                reservation.token_hash,
                now,
            ),
        ).fetchone()
        if pending is None:
            con.execute(
                "DELETE FROM magic_token_reservations WHERE reservation_id=?",
                (reservation.reservation_id,),
            )
            con.commit()
            raise ReservationInvalid("reservation missing or stale")
        con.execute("DELETE FROM magic_tokens_v2 WHERE email=?", (reservation.email,))
        con.execute(
            """INSERT INTO magic_tokens_v2
               (token_hash, email, expires_at, created_at) VALUES (?, ?, ?, ?)""",
            (
                reservation.token_hash,
                reservation.email,
                now + MAGIC_TOKEN_TTL_SECONDS,
                now,
            ),
        )
        con.execute(
            """INSERT INTO auth_email_cooldowns (email, sent_at) VALUES (?, ?)
               ON CONFLICT(email) DO UPDATE SET sent_at=excluded.sent_at""",
            (reservation.email, now),
        )
        con.execute(
            "DELETE FROM magic_token_reservations WHERE reservation_id=?",
            (reservation.reservation_id,),
        )
        con.commit()
    except Exception:
        if con.in_transaction:
            con.rollback()
        raise


def cancel_magic_token_reservation(con, reservation: MagicTokenReservation) -> None:
    """Remove only the matching pending request; active links are never touched."""
    con.execute(
        """DELETE FROM magic_token_reservations
           WHERE email=? AND reservation_id=? AND token_hash=?""",
        (reservation.email, reservation.reservation_id, reservation.token_hash),
    )
    con.commit()


def create_session(
    con, *, user_id: int, now: float, device_name: str
) -> str:
    raw_session = secrets.token_urlsafe(32)
    with _session_mutation(con, "session_create"):
        con.execute(
            """INSERT INTO sessions_v2
               (token_hash, user_id, expires_at, created_at, last_seen_at,
                device_name, revoked_at)
               VALUES (?, ?, ?, ?, ?, ?, NULL)""",
            (
                token_hash(raw_session),
                user_id,
                now + SESSION_TTL_SECONDS,
                now,
                now,
                device_name,
            ),
        )
    return raw_session


def consume_magic_token(con, *, raw_token: str, now: float) -> str:
    """Atomically consume one token and return a restricted setup capability."""
    if not isinstance(raw_token, str) or not raw_token or len(raw_token) > 512:
        raise MagicTokenInvalid("invalid token")
    digest = token_hash(raw_token)
    con.execute("BEGIN IMMEDIATE")
    try:
        row = con.execute(
            "SELECT email, expires_at FROM magic_tokens_v2 WHERE token_hash=?", (digest,)
        ).fetchone()
        if row is None:
            raise MagicTokenInvalid("invalid token")
        if float(row[1]) <= now:
            con.execute("DELETE FROM magic_tokens_v2 WHERE token_hash=?", (digest,))
            con.commit()
            raise MagicTokenExpired("expired token")

        email = row[0]
        # Every issued link for this address belongs to the same one-time
        # migration opportunity. Consuming any one retires all siblings.
        con.execute("DELETE FROM magic_tokens_v2 WHERE email=?", (email,))
        user = con.execute(
            """SELECT p.id, c.user_id
               FROM pouzivatelia p
               LEFT JOIN auth_credentials c ON c.user_id=p.id
               WHERE p.email=?""",
            (email,),
        ).fetchone()
        if user is None or user[1] is not None:
            # The migration bridge must never create an identity or become a
            # password-account login path. Consume stale links definitively.
            con.commit()
            raise MagicTokenInvalid("account is not eligible for migration")
        user_id = user[0]
        raw_session = create_setup_session(con, user_id=user_id, now=now)
        con.commit()
        return raw_session
    except (MagicTokenInvalid, MagicTokenExpired):
        if con.in_transaction:
            con.rollback()
        raise
    except Exception:
        con.rollback()
        raise


def create_setup_session(con, *, user_id: int, now: float) -> str:
    raw_session = secrets.token_urlsafe(32)
    with _session_mutation(con, "setup_session_create"):
        con.execute(
            """INSERT INTO auth_setup_sessions
               (token_hash, user_id, expires_at, created_at)
               VALUES (?, ?, ?, ?)""",
            (
                token_hash(raw_session),
                user_id,
                now + PASSWORD_ACTION_TOKEN_TTL_SECONDS,
                now,
            ),
        )
    return raw_session


def user_for_setup_session(con, *, raw_session: str, now: float):
    if not isinstance(raw_session, str) or not raw_session:
        return None
    digest = token_hash(raw_session)
    row = con.execute(
        """SELECT p.id, p.email, s.expires_at
           FROM auth_setup_sessions s
           JOIN pouzivatelia p ON p.id=s.user_id
           LEFT JOIN auth_credentials c ON c.user_id=p.id
           WHERE s.token_hash=? AND c.user_id IS NULL""",
        (digest,),
    ).fetchone()
    if row is None:
        return None
    if float(row[2]) <= now:
        with _session_mutation(con, "setup_session_expire"):
            con.execute(
                "DELETE FROM auth_setup_sessions WHERE token_hash=?", (digest,)
            )
        return None
    return {"id": int(row[0]), "email": row[1]}


def delete_setup_session(con, raw_session: str) -> None:
    if not isinstance(raw_session, str) or not raw_session:
        return
    with _session_mutation(con, "setup_session_delete"):
        con.execute(
            "DELETE FROM auth_setup_sessions WHERE token_hash=?",
            (token_hash(raw_session),),
        )


def user_for_session(
    con, *, raw_session: str, now: float, touch: bool = True
):
    if not isinstance(raw_session, str) or not raw_session:
        return None
    digest = token_hash(raw_session)
    row = con.execute(
        """SELECT p.*,
                  s.expires_at AS session_expires_at,
                  s.last_seen_at AS session_last_seen_at,
                  s.revoked_at AS session_revoked_at
           FROM sessions_v2 s JOIN pouzivatelia p ON p.id=s.user_id
           WHERE s.token_hash=?""",
        (digest,),
    ).fetchone()
    if row is None:
        return None
    if row["session_revoked_at"] is not None:
        return None
    if float(row["session_expires_at"]) <= now:
        with _session_mutation(con, "session_expire"):
            con.execute("DELETE FROM sessions_v2 WHERE token_hash=?", (digest,))
        return None
    last_seen_at = row["session_last_seen_at"]
    if touch and (
        last_seen_at is None
        or float(last_seen_at) + SESSION_TOUCH_SECONDS <= now
    ):
        with _session_mutation(con, "session_touch"):
            con.execute(
                """UPDATE sessions_v2
                   SET last_seen_at=?, expires_at=?
                   WHERE token_hash=? AND revoked_at IS NULL
                     AND (last_seen_at IS NULL OR last_seen_at <= ?)""",
                (
                    now,
                    now + SESSION_TTL_SECONDS,
                    digest,
                    now - SESSION_TOUCH_SECONDS,
                ),
            )
    session_fields = {
        "session_expires_at",
        "session_last_seen_at",
        "session_revoked_at",
    }
    return {key: row[key] for key in row.keys() if key not in session_fields}


def list_sessions(
    con, *, user_id: int, current_token: str, now: float
) -> list[dict]:
    current_hash = (
        token_hash(current_token)
        if isinstance(current_token, str) and current_token
        else None
    )
    rows = con.execute(
        """SELECT token_hash, device_name, created_at, last_seen_at, expires_at
           FROM sessions_v2
           WHERE user_id=? AND revoked_at IS NULL AND expires_at > ?
           ORDER BY created_at DESC, token_hash""",
        (user_id, now),
    ).fetchall()
    return [
        {
            "session_hash": row["token_hash"],
            "device_name": row["device_name"],
            "created_at": row["created_at"],
            "last_seen_at": row["last_seen_at"],
            "expires_at": row["expires_at"],
            "current": current_hash is not None
            and secrets.compare_digest(row["token_hash"], current_hash),
        }
        for row in rows
    ]


def revoke_session(con, *, user_id: int, session_hash: str) -> bool:
    if not isinstance(session_hash, str) or not session_hash:
        return False
    with _session_mutation(con, "session_revoke"):
        revoked = con.execute(
            """UPDATE sessions_v2
               SET revoked_at=CAST(strftime('%s', 'now') AS REAL)
               WHERE user_id=? AND token_hash=? AND revoked_at IS NULL""",
            (user_id, session_hash),
        ).rowcount
    return revoked == 1


def revoke_other_sessions(
    con, *, user_id: int, current_token: str | None
) -> None:
    with _session_mutation(con, "sessions_revoke_other"):
        if current_token:
            con.execute(
                """UPDATE sessions_v2
                   SET revoked_at=CAST(strftime('%s', 'now') AS REAL)
                   WHERE user_id=? AND token_hash<>? AND revoked_at IS NULL""",
                (user_id, token_hash(current_token)),
            )
        else:
            con.execute(
                """UPDATE sessions_v2
                   SET revoked_at=CAST(strftime('%s', 'now') AS REAL)
                   WHERE user_id=? AND revoked_at IS NULL""",
                (user_id,),
            )


def delete_session(con, raw_session: str) -> None:
    if raw_session:
        with _session_mutation(con, "session_delete"):
            con.execute(
                "DELETE FROM sessions_v2 WHERE token_hash=?",
                (token_hash(raw_session),),
            )
