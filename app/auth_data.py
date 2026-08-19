"""Focused persistence and provider boundaries for passwordless authentication."""

from dataclasses import dataclass
import hashlib
import re
import secrets
import threading


MAGIC_TOKEN_TTL_SECONDS = 60 * 60
MAGIC_RESERVATION_TTL_SECONDS = 5 * 60
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


class EmailRequestInProgress(RuntimeError):
    """A normalized address already has an active provider request."""


class ReservationInvalid(RuntimeError):
    """A pending token reservation is missing, stale, or replaced."""


class MagicTokenInvalid(RuntimeError):
    """A token is unknown or was already consumed."""


class MagicTokenExpired(RuntimeError):
    """A known token passed its absolute expiry."""


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
    """Create additive auth-v2 tables without trusting legacy plaintext data."""
    con.executescript(AUTH_SCHEMA)


def token_hash(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


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
