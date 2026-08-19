"""Focused persistence and provider boundaries for passwordless authentication."""

from dataclasses import dataclass
import hashlib
import re
import secrets
import threading


MAGIC_TOKEN_TTL_SECONDS = 60 * 60
EMAIL_COOLDOWN_SECONDS = 60
SESSION_TTL_SECONDS = 30 * 24 * 60 * 60

_LOCAL_PART = r"[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+(?:\.[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+)*"
_DOMAIN_LABEL = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
_EMAIL = re.compile(rf"{_LOCAL_PART}@(?:{_DOMAIN_LABEL}\.)+[A-Za-z]{{2,63}}")
_OPAQUE_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")

AUTH_SCHEMA = """
CREATE TABLE IF NOT EXISTS magic_tokens_v2 (
  token_hash TEXT PRIMARY KEY,
  email TEXT NOT NULL,
  expires_at REAL NOT NULL,
  created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS magic_tokens_v2_email_idx
  ON magic_tokens_v2(email);
CREATE TABLE IF NOT EXISTS auth_email_cooldowns (
  email TEXT PRIMARY KEY,
  sent_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions_v2 (
  token_hash TEXT PRIMARY KEY,
  user_id INTEGER NOT NULL,
  expires_at REAL NOT NULL,
  created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS sessions_v2_user_idx
  ON sessions_v2(user_id);
"""


class DeliveryError(RuntimeError):
    """The provider did not verifiably accept a message."""


class EmailCooldown(RuntimeError):
    """A normalized address received an accepted message too recently."""


class MagicTokenInvalid(RuntimeError):
    """A token is unknown or was already consumed."""


class MagicTokenExpired(RuntimeError):
    """A known token passed its absolute expiry."""


@dataclass(frozen=True)
class DeliveryAccepted:
    provider_id: str
    request_id: str | None = None


class ClientIpRateLimiter:
    """Bound one-worker request bursts; proxy-edge rate limiting is still recommended."""

    def __init__(self, max_requests=5, window_seconds=10 * 60):
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self._requests = {}
        self._lock = threading.Lock()

    def allow(self, client_ip: str, now: float) -> bool:
        cutoff = now - self.window_seconds
        with self._lock:
            recent = [stamp for stamp in self._requests.get(client_ip, ()) if stamp > cutoff]
            if len(recent) >= self.max_requests:
                self._requests[client_ip] = recent
                return False
            recent.append(now)
            self._requests[client_ip] = recent
            return True


def migrate_auth_schema(con) -> None:
    """Create additive auth-v2 tables without trusting legacy plaintext data."""
    con.executescript(AUTH_SCHEMA)


def token_hash(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def normalize_email(value) -> str:
    if not isinstance(value, str):
        raise ValueError("invalid email")
    normalized = value.strip().lower()
    if len(normalized) > 254 or _EMAIL.fullmatch(normalized) is None:
        raise ValueError("invalid email")
    return normalized


def _safe_opaque_id(value) -> str | None:
    return value if isinstance(value, str) and _OPAQUE_ID.fullmatch(value) else None


def send_resend_message(*, api_key, sender, recipient, subject, text, html):
    """Return typed acceptance or raise without logging message/provider bodies."""
    if not api_key:
        raise DeliveryError("provider unavailable")
    try:
        import requests

        response = requests.post(
            "https://api.resend.com/emails",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "from": sender,
                "to": [recipient],
                "subject": subject,
                "text": text,
                "html": html,
            },
            timeout=20,
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


def issue_magic_token(con, *, email: str, now: float, deliver):
    """Publish one hashed token only after the provider accepts its raw link."""
    con.execute("DELETE FROM magic_tokens_v2 WHERE expires_at <= ?", (now,))
    con.commit()
    con.execute("BEGIN IMMEDIATE")
    try:
        cooldown = con.execute(
            "SELECT sent_at FROM auth_email_cooldowns WHERE email=?", (email,)
        ).fetchone()
        if cooldown is not None and float(cooldown[0]) > now - EMAIL_COOLDOWN_SECONDS:
            raise EmailCooldown("email cooldown active")
        raw_token = secrets.token_urlsafe(32)
        accepted = deliver(raw_token)
        if not isinstance(accepted, DeliveryAccepted):
            raise DeliveryError("provider acceptance was not typed")
        con.execute("DELETE FROM magic_tokens_v2 WHERE email=?", (email,))
        con.execute(
            """INSERT INTO magic_tokens_v2
               (token_hash, email, expires_at, created_at) VALUES (?, ?, ?, ?)""",
            (token_hash(raw_token), email, now + MAGIC_TOKEN_TTL_SECONDS, now),
        )
        con.execute(
            """INSERT INTO auth_email_cooldowns (email, sent_at) VALUES (?, ?)
               ON CONFLICT(email) DO UPDATE SET sent_at=excluded.sent_at""",
            (email, now),
        )
        con.commit()
    except Exception:
        con.rollback()
        raise
    return accepted


def consume_magic_token(con, *, raw_token: str, now: float) -> str:
    """Atomically consume one token, rotate the user's session, and return its raw cookie."""
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
        con.execute("DELETE FROM magic_tokens_v2 WHERE token_hash=?", (digest,))
        user = con.execute("SELECT id FROM pouzivatelia WHERE email=?", (email,)).fetchone()
        if user is None:
            user_id = con.execute(
                "INSERT INTO pouzivatelia (email) VALUES (?)", (email,)
            ).lastrowid
        else:
            user_id = user[0]
        con.execute("DELETE FROM sessions_v2 WHERE user_id=?", (user_id,))
        raw_session = secrets.token_urlsafe(32)
        con.execute(
            """INSERT INTO sessions_v2
               (token_hash, user_id, expires_at, created_at) VALUES (?, ?, ?, ?)""",
            (token_hash(raw_session), user_id, now + SESSION_TTL_SECONDS, now),
        )
        con.commit()
        return raw_session
    except (MagicTokenInvalid, MagicTokenExpired):
        if con.in_transaction:
            con.rollback()
        raise
    except Exception:
        con.rollback()
        raise


def user_for_session(con, *, raw_session: str, now: float):
    if not isinstance(raw_session, str) or not raw_session:
        return None
    digest = token_hash(raw_session)
    row = con.execute(
        """SELECT p.*, s.expires_at
           FROM sessions_v2 s JOIN pouzivatelia p ON p.id=s.user_id
           WHERE s.token_hash=?""",
        (digest,),
    ).fetchone()
    if row is None:
        return None
    if float(row["expires_at"]) <= now:
        con.execute("DELETE FROM sessions_v2 WHERE token_hash=?", (digest,))
        con.commit()
        return None
    return {key: row[key] for key in row.keys() if key != "expires_at"}


def delete_session(con, raw_session: str) -> None:
    if raw_session:
        con.execute("DELETE FROM sessions_v2 WHERE token_hash=?", (token_hash(raw_session),))
        con.commit()
