import base64
import hashlib
import importlib
import sqlite3
import sys
import types
from contextlib import closing
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


ROOT = Path(__file__).resolve().parents[1]
ORIGIN = {"Origin": "https://uvar.si"}


def b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def load_server(monkeypatch, tmp_path, *, enabled=True):
    database = tmp_path / "uvarsi.db"
    with sqlite3.connect(database) as con:
        con.execute(
            """CREATE TABLE akcie (
                tyzden TEXT, nazov TEXT, obchod TEXT, cena REAL, povodna REAL,
                zlava TEXT, jednotka TEXT, kategoria TEXT
            )"""
        )
    monkeypatch.setenv("UVARSI_DB", str(database))
    monkeypatch.setenv("UVARSI_URL", "https://uvar.si")
    monkeypatch.setenv("UVARSI_AUTH_V3", "1" if enabled else "0")
    monkeypatch.syspath_prepend(str(ROOT / "app"))
    sys.modules.pop("server", None)
    sys.modules.pop("auth_data", None)
    server = importlib.import_module("server")
    server.ENV_FILE = str(tmp_path / "missing.env")
    return server, sys.modules["auth_data"], database


def seed_account(server, auth_data, email, *, password=None, now=None):
    now = server.AUTH_CLOCK() if now is None else now
    with closing(server.db()) as con:
        user_id = con.execute(
            "INSERT INTO pouzivatelia (email) VALUES (?)", (email,)
        ).lastrowid
        if password is not None:
            auth_data.set_password(
                con,
                user_id=user_id,
                password_hash=auth_data.hash_password(password),
                now=now,
            )
        session = auth_data.create_session(
            con, user_id=user_id, now=now, device_name="Existing password device"
        )
        con.commit()
    return int(user_id), session


def authenticated_client(server, raw_session):
    client = TestClient(server.app, base_url="https://uvar.si")
    client.cookies.set(server.COOKIE, raw_session)
    return client


def insert_passkey(
    server,
    *,
    credential_id,
    user_id,
    public_key=b"public-key",
    sign_count=0,
    transports='["internal"]',
    name="Phone",
    created_at=1_000.0,
):
    with closing(server.db()) as con:
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
                transports,
                name,
                created_at,
            ),
        )
        con.commit()


def credential_payload(credential_id):
    return {
        "id": credential_id,
        "rawId": credential_id,
        "type": "public-key",
        "response": {
            "clientDataJSON": "e30",
            "attestationObject": "e30",
            "authenticatorData": "e30",
            "signature": "e30",
        },
    }


def test_registration_options_use_uvarsi_rp_required_uv_and_only_hash_challenge(
    monkeypatch, tmp_path
):
    server, auth_data, database = load_server(monkeypatch, tmp_path)
    user_id, session = seed_account(server, auth_data, "cook@example.com")
    existing_id = b64url(b"existing credential")
    insert_passkey(server, credential_id=existing_id, user_id=user_id)
    now = 10_000.0
    monkeypatch.setattr(server, "AUTH_CLOCK", lambda: now)
    client = authenticated_client(server, session)

    rejected_origin = client.post(
        "/api/auth/passkey/register/options",
        headers={"Origin": "https://uvar.si.evil.example"},
        json={},
    )
    response = client.post(
        "/api/auth/passkey/register/options", headers=ORIGIN, json={}
    )

    assert rejected_origin.status_code == 403
    assert response.status_code == 200
    options = response.json()
    assert options["rp"] == {"name": "Uvar.si", "id": "uvar.si"}
    assert options["authenticatorSelection"]["userVerification"] == "required"
    assert options["authenticatorSelection"]["residentKey"] == "required"
    assert options["authenticatorSelection"]["requireResidentKey"] is True
    assert options["timeout"] == 300_000
    assert options["excludeCredentials"] == [
        {"id": existing_id, "type": "public-key", "transports": ["internal"]}
    ]
    challenge = options["challenge"]
    assert len(challenge) >= 43
    with sqlite3.connect(database) as con:
        row = con.execute(
            """SELECT challenge_hash, user_id, purpose, expires_at, created_at
               FROM auth_webauthn_challenges"""
        ).fetchone()
    assert row == (
        hashlib.sha256(challenge.encode("utf-8")).hexdigest(),
        user_id,
        "register",
        now + 300,
        now,
    )
    assert challenge not in repr(row)


def test_registration_verify_is_atomic_stores_public_material_and_rejects_replay(
    monkeypatch, tmp_path
):
    server, auth_data, database = load_server(monkeypatch, tmp_path)
    user_id, session = seed_account(server, auth_data, "register@example.com")
    now = 20_000.0
    monkeypatch.setattr(server, "AUTH_CLOCK", lambda: now)
    client = authenticated_client(server, session)
    options_response = client.post(
        "/api/auth/passkey/register/options", headers=ORIGIN, json={}
    )
    assert options_response.status_code == 200
    options = options_response.json()
    credential_id_bytes = b"registered credential one"
    calls = []

    def verify_registration_response(**kwargs):
        calls.append(kwargs)
        return types.SimpleNamespace(
            credential_id=credential_id_bytes,
            credential_public_key=b"cose public key bytes",
            sign_count=7,
            user_verified=True,
        )

    monkeypatch.setattr(
        server, "verify_registration_response", verify_registration_response,
        raising=False,
    )
    payload = {
        "challenge": options["challenge"],
        "credential": credential_payload(b64url(credential_id_bytes)),
        "name": "  My phone  ",
        "transports": ["internal", "hybrid"],
    }

    verified = client.post(
        "/api/auth/passkey/register/verify", headers=ORIGIN, json=payload
    )
    replay = client.post(
        "/api/auth/passkey/register/verify", headers=ORIGIN, json=payload
    )

    assert verified.status_code == 200
    assert verified.json() == {"ok": True}
    assert replay.status_code == 400
    assert len(calls) == 1
    assert calls[0]["expected_rp_id"] == "uvar.si"
    assert calls[0]["expected_origin"] == "https://uvar.si"
    assert calls[0]["require_user_verification"] is True
    assert calls[0]["expected_challenge"] == base64.urlsafe_b64decode(
        options["challenge"] + "="
    )
    with sqlite3.connect(database) as con:
        assert con.execute(
            "SELECT COUNT(*) FROM auth_webauthn_challenges"
        ).fetchone() == (0,)
        stored = con.execute(
            """SELECT credential_id, user_id, public_key, sign_count, transports,
                      name, created_at, last_used_at
               FROM auth_passkeys"""
        ).fetchone()
    assert stored == (
        b64url(credential_id_bytes),
        user_id,
        b"cose public key bytes",
        7,
        '["internal","hybrid"]',
        "My phone",
        now,
        None,
    )
    assert "challenge" not in repr(stored).lower()


def test_registration_credential_failure_rolls_back_challenge_consumption(
    monkeypatch, tmp_path
):
    server, auth_data, database = load_server(monkeypatch, tmp_path)
    owner_id, owner_session = seed_account(server, auth_data, "owner@example.com")
    other_id, _ = seed_account(server, auth_data, "other@example.com")
    duplicate_id_bytes = b"globally owned credential"
    duplicate_id = b64url(duplicate_id_bytes)
    insert_passkey(server, credential_id=duplicate_id, user_id=other_id)
    client = authenticated_client(server, owner_session)
    options_response = client.post(
        "/api/auth/passkey/register/options", headers=ORIGIN, json={}
    )
    assert options_response.status_code == 200
    options = options_response.json()

    monkeypatch.setattr(
        server,
        "verify_registration_response",
        lambda **_kwargs: types.SimpleNamespace(
            credential_id=duplicate_id_bytes,
            credential_public_key=b"different public key",
            sign_count=0,
            user_verified=True,
        ),
        raising=False,
    )
    failed = client.post(
        "/api/auth/passkey/register/verify",
        headers=ORIGIN,
        json={
            "challenge": options["challenge"],
            "credential": credential_payload(duplicate_id),
            "name": "Duplicate",
        },
    )

    assert failed.status_code == 409
    with sqlite3.connect(database) as con:
        assert con.execute(
            "SELECT COUNT(*) FROM auth_webauthn_challenges"
        ).fetchone() == (1,)
        assert con.execute(
            "SELECT user_id, public_key FROM auth_passkeys WHERE credential_id=?",
            (duplicate_id,),
        ).fetchone() == (other_id, b"public-key")
        assert con.execute(
            "SELECT COUNT(*) FROM auth_passkeys WHERE user_id=?", (owner_id,)
        ).fetchone() == (0,)


@pytest.mark.parametrize(
    "failure_kind",
    [
        "missing-credential",
        "invalid-credential-base64",
        "unsupported-transports",
        "verifier-exception",
    ],
)
def test_registration_attempt_burns_identified_challenge_before_credential_validation(
    monkeypatch, tmp_path, failure_kind
):
    server, auth_data, database = load_server(monkeypatch, tmp_path)
    _user_id, session = seed_account(
        server, auth_data, f"register-{failure_kind}@example.com"
    )
    client = authenticated_client(server, session)
    options_response = client.post(
        "/api/auth/passkey/register/options", headers=ORIGIN, json={}
    )
    assert options_response.status_code == 200
    challenge = options_response.json()["challenge"]
    credential_id = b64url(b"attempted registration credential")
    payload = {
        "challenge": challenge,
        "credential": credential_payload(credential_id),
    }
    if failure_kind == "missing-credential":
        payload["credential"] = {}
    elif failure_kind == "invalid-credential-base64":
        payload["credential"] = credential_payload("not+base64url")
    elif failure_kind == "unsupported-transports":
        payload["transports"] = ["carrier-pigeon"]

    def reject_registration(**_kwargs):
        raise RuntimeError("injected verifier failure")

    monkeypatch.setattr(
        server, "verify_registration_response", reject_registration
    )

    rejected = client.post(
        "/api/auth/passkey/register/verify", headers=ORIGIN, json=payload
    )
    with sqlite3.connect(database) as con:
        challenges_after_attempt = con.execute(
            "SELECT COUNT(*) FROM auth_webauthn_challenges"
        ).fetchone()[0]
    replay = client.post(
        "/api/auth/passkey/register/verify", headers=ORIGIN, json=payload
    )

    assert rejected.status_code == 400
    assert challenges_after_attempt == 0
    assert replay.status_code == 400


def test_registration_malformed_attempt_cannot_burn_another_users_challenge(
    monkeypatch, tmp_path
):
    server, auth_data, database = load_server(monkeypatch, tmp_path)
    _owner_id, owner_session = seed_account(
        server, auth_data, "challenge-owner@example.com"
    )
    _other_id, other_session = seed_account(
        server, auth_data, "challenge-other@example.com"
    )
    owner = authenticated_client(server, owner_session)
    other = authenticated_client(server, other_session)
    options_response = owner.post(
        "/api/auth/passkey/register/options", headers=ORIGIN, json={}
    )
    challenge = options_response.json()["challenge"]
    malformed_payload = {"challenge": challenge, "credential": {}}

    foreign_attempt = other.post(
        "/api/auth/passkey/register/verify",
        headers=ORIGIN,
        json=malformed_payload,
    )
    with sqlite3.connect(database) as con:
        challenges_after_foreign_attempt = con.execute(
            "SELECT COUNT(*) FROM auth_webauthn_challenges"
        ).fetchone()[0]
    owner_attempt = owner.post(
        "/api/auth/passkey/register/verify",
        headers=ORIGIN,
        json=malformed_payload,
    )

    assert foreign_attempt.status_code == 400
    assert challenges_after_foreign_attempt == 1
    assert owner_attempt.status_code == 400
    with sqlite3.connect(database) as con:
        assert con.execute(
            "SELECT COUNT(*) FROM auth_webauthn_challenges"
        ).fetchone() == (0,)


def test_expired_registration_challenge_is_deleted_and_never_verified(
    monkeypatch, tmp_path
):
    server, auth_data, database = load_server(monkeypatch, tmp_path)
    _user_id, session = seed_account(server, auth_data, "expired@example.com")
    now = [30_000.0]
    monkeypatch.setattr(server, "AUTH_CLOCK", lambda: now[0])
    client = authenticated_client(server, session)
    options_response = client.post(
        "/api/auth/passkey/register/options", headers=ORIGIN, json={}
    )
    assert options_response.status_code == 200
    options = options_response.json()
    called = []
    monkeypatch.setattr(
        server,
        "verify_registration_response",
        lambda **kwargs: called.append(kwargs),
        raising=False,
    )

    now[0] += 300
    expired = client.post(
        "/api/auth/passkey/register/verify",
        headers=ORIGIN,
        json={
            "challenge": options["challenge"],
            "credential": credential_payload(b64url(b"late credential")),
        },
    )

    assert expired.status_code == 410
    assert called == []
    with sqlite3.connect(database) as con:
        assert con.execute(
            "SELECT COUNT(*) FROM auth_webauthn_challenges"
        ).fetchone() == (0,)


def test_login_options_and_verify_update_counter_create_one_session_and_reject_replay(
    monkeypatch, tmp_path
):
    server, auth_data, database = load_server(monkeypatch, tmp_path)
    user_id, password_session = seed_account(
        server,
        auth_data,
        "login@example.com",
        password="correct horse battery",
        now=40_000.0,
    )
    first_id_bytes = b"first login credential"
    second_id_bytes = b"second login credential"
    first_id = b64url(first_id_bytes)
    second_id = b64url(second_id_bytes)
    insert_passkey(
        server,
        credential_id=first_id,
        user_id=user_id,
        public_key=b"first cose key",
        sign_count=4,
        name="Phone",
        created_at=40_000.0,
    )
    insert_passkey(
        server,
        credential_id=second_id,
        user_id=user_id,
        public_key=b"second cose key",
        sign_count=0,
        transports='["usb"]',
        name="Security key",
        created_at=40_001.0,
    )
    now = 41_000.0
    monkeypatch.setattr(server, "AUTH_CLOCK", lambda: now)
    client = TestClient(server.app, base_url="https://uvar.si")
    options_response = client.post(
        "/api/auth/passkey/login/options",
        headers=ORIGIN,
        json={},
    )
    assert options_response.status_code == 200
    options = options_response.json()
    captured = []

    def verify_authentication_response(**kwargs):
        captured.append(kwargs)
        return types.SimpleNamespace(
            credential_id=first_id_bytes,
            new_sign_count=5,
            user_verified=True,
        )

    monkeypatch.setattr(
        server, "verify_authentication_response", verify_authentication_response,
        raising=False,
    )
    payload = {
        "challenge": options["challenge"],
        "credential": credential_payload(first_id),
        "device_name": "Passkey laptop",
    }
    verified = client.post(
        "/api/auth/passkey/login/verify", headers=ORIGIN, json=payload
    )
    replay = client.post(
        "/api/auth/passkey/login/verify", headers=ORIGIN, json=payload
    )

    assert options_response.status_code == 200
    assert options["rpId"] == "uvar.si"
    assert options["userVerification"] == "required"
    assert options.get("allowCredentials", []) == []
    assert verified.status_code == 200
    assert verified.json() == {"ok": True, "redirect": "/app"}
    cookie = verified.headers["set-cookie"]
    assert "Max-Age=7776000" in cookie
    assert "HttpOnly" in cookie
    assert "SameSite=lax" in cookie
    assert "Secure" in cookie
    assert replay.status_code == 400
    assert len(captured) == 1
    assert captured[0]["expected_rp_id"] == "uvar.si"
    assert captured[0]["expected_origin"] == "https://uvar.si"
    assert captured[0]["require_user_verification"] is True
    assert captured[0]["credential_public_key"] == b"first cose key"
    assert captured[0]["credential_current_sign_count"] == 4
    with sqlite3.connect(database) as con:
        assert con.execute(
            "SELECT user_id FROM auth_webauthn_challenges"
        ).fetchall() == []
        assert con.execute(
            "SELECT sign_count, last_used_at FROM auth_passkeys WHERE credential_id=?",
            (first_id,),
        ).fetchone() == (5, now)
        assert con.execute(
            "SELECT sign_count, last_used_at FROM auth_passkeys WHERE credential_id=?",
            (second_id,),
        ).fetchone() == (0, None)
        assert con.execute(
            "SELECT COUNT(*) FROM sessions_v2 WHERE user_id=? AND revoked_at IS NULL",
            (user_id,),
        ).fetchone() == (2,)
    password_client = authenticated_client(server, password_session)
    assert password_client.get("/api/me").json()["id"] == user_id
    assert client.get("/api/me").json()["id"] == user_id


def test_login_options_are_username_less_and_known_unknown_indistinguishable(
    monkeypatch, tmp_path
):
    server, auth_data, database = load_server(monkeypatch, tmp_path)
    user_id, _session = seed_account(server, auth_data, "known@example.com")
    credential_id = b64url(b"discoverable credential")
    insert_passkey(server, credential_id=credential_id, user_id=user_id)
    client = TestClient(server.app, base_url="https://uvar.si")

    known = client.post(
        "/api/auth/passkey/login/options",
        headers=ORIGIN,
        json={"email": "known@example.com"},
    )
    unknown = client.post(
        "/api/auth/passkey/login/options",
        headers=ORIGIN,
        json={"email": "unknown@example.com"},
    )

    assert known.status_code == unknown.status_code == 200
    known_options = known.json()
    unknown_options = unknown.json()
    assert known_options.pop("challenge") != unknown_options.pop("challenge")
    assert known_options == unknown_options
    assert known_options.get("allowCredentials", []) == []
    assert credential_id not in known.text
    assert credential_id not in unknown.text
    with sqlite3.connect(database) as con:
        assert con.execute(
            "SELECT purpose, user_id FROM auth_webauthn_challenges ORDER BY created_at"
        ).fetchall() == [("login", None), ("login", None)]


def test_login_rejects_non_monotonic_clone_signal_without_mutation_or_session(
    monkeypatch, tmp_path
):
    server, auth_data, database = load_server(monkeypatch, tmp_path)
    user_id, _session = seed_account(server, auth_data, "clone@example.com")
    credential_id_bytes = b"cloned credential"
    credential_id = b64url(credential_id_bytes)
    insert_passkey(
        server,
        credential_id=credential_id,
        user_id=user_id,
        sign_count=9,
    )
    now = 50_000.0
    monkeypatch.setattr(server, "AUTH_CLOCK", lambda: now)
    client = TestClient(server.app, base_url="https://uvar.si")
    options_response = client.post(
        "/api/auth/passkey/login/options",
        headers=ORIGIN,
        json={"email": "clone@example.com"},
    )
    assert options_response.status_code == 200
    options = options_response.json()
    monkeypatch.setattr(
        server,
        "verify_authentication_response",
        lambda **_kwargs: types.SimpleNamespace(
            credential_id=credential_id_bytes,
            new_sign_count=9,
            user_verified=True,
        ),
        raising=False,
    )
    with sqlite3.connect(database) as con:
        sessions_before = con.execute(
            "SELECT COUNT(*) FROM sessions_v2 WHERE user_id=?", (user_id,)
        ).fetchone()[0]

    rejected = client.post(
        "/api/auth/passkey/login/verify",
        headers=ORIGIN,
        json={
            "challenge": options["challenge"],
            "credential": credential_payload(credential_id),
        },
    )

    assert rejected.status_code == 401
    assert "count" not in rejected.text.lower()
    assert client.cookies.get(server.COOKIE) is None
    with sqlite3.connect(database) as con:
        assert con.execute(
            "SELECT sign_count, last_used_at FROM auth_passkeys WHERE credential_id=?",
            (credential_id,),
        ).fetchone() == (9, None)
        assert con.execute(
            "SELECT COUNT(*) FROM sessions_v2 WHERE user_id=?", (user_id,)
        ).fetchone() == (sessions_before,)
        assert con.execute(
            "SELECT COUNT(*) FROM auth_webauthn_challenges"
        ).fetchone() == (0,)


def test_login_session_failure_rolls_back_counter_and_challenge(
    monkeypatch, tmp_path
):
    server, auth_data, database = load_server(monkeypatch, tmp_path)
    user_id, _session = seed_account(server, auth_data, "atomic@example.com")
    credential_id_bytes = b"atomic credential"
    credential_id = b64url(credential_id_bytes)
    insert_passkey(
        server,
        credential_id=credential_id,
        user_id=user_id,
        sign_count=2,
    )
    client = TestClient(server.app, base_url="https://uvar.si")
    options_response = client.post(
        "/api/auth/passkey/login/options",
        headers=ORIGIN,
        json={"email": "atomic@example.com"},
    )
    assert options_response.status_code == 200
    options = options_response.json()
    monkeypatch.setattr(
        server,
        "verify_authentication_response",
        lambda **_kwargs: types.SimpleNamespace(
            credential_id=credential_id_bytes,
            new_sign_count=3,
            user_verified=True,
        ),
        raising=False,
    )
    monkeypatch.setattr(
        server,
        "create_session",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("injected")),
    )

    response = TestClient(
        server.app, base_url="https://uvar.si", raise_server_exceptions=False
    ).post(
        "/api/auth/passkey/login/verify",
        headers=ORIGIN,
        json={
            "challenge": options["challenge"],
            "credential": credential_payload(credential_id),
        },
    )

    assert response.status_code == 500
    with sqlite3.connect(database) as con:
        assert con.execute(
            "SELECT sign_count, last_used_at FROM auth_passkeys WHERE credential_id=?",
            (credential_id,),
        ).fetchone() == (2, None)
        assert con.execute(
            "SELECT COUNT(*) FROM auth_webauthn_challenges"
        ).fetchone() == (1,)


@pytest.mark.parametrize(
    "failure_kind",
    [
        "missing-credential",
        "invalid-credential-base64",
        "verifier-exception",
    ],
)
def test_login_attempt_burns_identified_challenge_before_credential_validation(
    monkeypatch, tmp_path, failure_kind
):
    server, auth_data, database = load_server(monkeypatch, tmp_path)
    user_id, _session = seed_account(
        server, auth_data, f"login-{failure_kind}@example.com"
    )
    credential_id = b64url(b"attempted login credential")
    insert_passkey(server, credential_id=credential_id, user_id=user_id)
    client = TestClient(server.app, base_url="https://uvar.si")
    options_response = client.post(
        "/api/auth/passkey/login/options",
        headers=ORIGIN,
        json={"email": f"login-{failure_kind}@example.com"},
    )
    assert options_response.status_code == 200
    challenge = options_response.json()["challenge"]
    payload = {
        "challenge": challenge,
        "credential": credential_payload(credential_id),
    }
    if failure_kind == "missing-credential":
        payload["credential"] = {}
    elif failure_kind == "invalid-credential-base64":
        payload["credential"] = credential_payload("not+base64url")

    def reject_authentication(**_kwargs):
        raise RuntimeError("injected verifier failure")

    monkeypatch.setattr(
        server, "verify_authentication_response", reject_authentication
    )

    rejected = client.post(
        "/api/auth/passkey/login/verify", headers=ORIGIN, json=payload
    )
    with sqlite3.connect(database) as con:
        challenges_after_attempt = con.execute(
            "SELECT COUNT(*) FROM auth_webauthn_challenges"
        ).fetchone()[0]
    replay = client.post(
        "/api/auth/passkey/login/verify", headers=ORIGIN, json=payload
    )

    assert rejected.status_code == 400
    assert challenges_after_attempt == 0
    assert replay.status_code == 400


def test_database_failure_before_challenge_consumption_allows_retry(
    monkeypatch, tmp_path
):
    server, auth_data, database = load_server(monkeypatch, tmp_path)
    _user_id, session = seed_account(server, auth_data, "retry@example.com")
    client = authenticated_client(server, session)
    options_response = client.post(
        "/api/auth/passkey/register/options", headers=ORIGIN, json={}
    )
    challenge = options_response.json()["challenge"]
    original_consume = server.consume_webauthn_challenge
    attempts = 0

    def fail_once_before_consumption(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise sqlite3.OperationalError("injected pre-consumption failure")
        return original_consume(*args, **kwargs)

    monkeypatch.setattr(
        server, "consume_webauthn_challenge", fail_once_before_consumption
    )
    payload = {"challenge": challenge, "credential": {}}
    failing_client = TestClient(
        server.app, base_url="https://uvar.si", raise_server_exceptions=False
    )
    failing_client.cookies.set(server.COOKIE, session)
    first = failing_client.post(
        "/api/auth/passkey/register/verify", headers=ORIGIN, json=payload
    )
    with sqlite3.connect(database) as con:
        challenges_after_database_failure = con.execute(
            "SELECT COUNT(*) FROM auth_webauthn_challenges"
        ).fetchone()[0]
    retry = client.post(
        "/api/auth/passkey/register/verify", headers=ORIGIN, json=payload
    )

    assert first.status_code == 500
    assert challenges_after_database_failure == 1
    assert retry.status_code == 400
    with sqlite3.connect(database) as con:
        assert con.execute(
            "SELECT COUNT(*) FROM auth_webauthn_challenges"
        ).fetchone() == (0,)


@pytest.mark.parametrize("ceremony", ["register", "login"])
def test_unidentifiable_challenge_returns_generic_error_without_consuming_issued_row(
    monkeypatch, tmp_path, ceremony
):
    server, auth_data, database = load_server(monkeypatch, tmp_path)
    user_id, session = seed_account(
        server, auth_data, f"unidentified-{ceremony}@example.com"
    )
    credential_id = b64url(b"unidentified challenge credential")
    insert_passkey(server, credential_id=credential_id, user_id=user_id)
    if ceremony == "register":
        client = authenticated_client(server, session)
        options_path = "/api/auth/passkey/register/options"
        verify_path = "/api/auth/passkey/register/verify"
        options_payload = {}
    else:
        client = TestClient(server.app, base_url="https://uvar.si")
        options_path = "/api/auth/passkey/login/options"
        verify_path = "/api/auth/passkey/login/verify"
        options_payload = {"email": f"unidentified-{ceremony}@example.com"}
    options_response = client.post(
        options_path, headers=ORIGIN, json=options_payload
    )
    assert options_response.status_code == 200

    rejected = client.post(
        verify_path,
        headers=ORIGIN,
        json={
            "challenge": None,
            "credential": credential_payload(credential_id),
        },
    )

    assert rejected.status_code == 400
    assert rejected.json() == {"detail": server.PASSKEY_FAILURE_MESSAGE}
    with sqlite3.connect(database) as con:
        assert con.execute(
            "SELECT COUNT(*) FROM auth_webauthn_challenges"
        ).fetchone() == (1,)


def test_passkey_list_delete_support_multiple_credentials_and_block_idor(
    monkeypatch, tmp_path
):
    server, auth_data, database = load_server(monkeypatch, tmp_path)
    owner_id, owner_session = seed_account(server, auth_data, "list@example.com")
    other_id, _ = seed_account(server, auth_data, "foreign@example.com")
    first_id = b64url(b"owner phone")
    second_id = b64url(b"owner laptop")
    foreign_id = b64url(b"foreign key")
    insert_passkey(
        server,
        credential_id=first_id,
        user_id=owner_id,
        name="Phone",
        created_at=60_000.0,
    )
    insert_passkey(
        server,
        credential_id=second_id,
        user_id=owner_id,
        transports='["hybrid","internal"]',
        name="Laptop",
        created_at=60_001.0,
    )
    insert_passkey(
        server,
        credential_id=foreign_id,
        user_id=other_id,
        name="Not mine",
    )
    client = authenticated_client(server, owner_session)

    listed = client.get("/api/auth/passkeys")
    foreign_delete = client.delete(
        f"/api/auth/passkeys/{foreign_id}", headers=ORIGIN
    )
    malformed_delete = client.delete(
        "/api/auth/passkeys/not%2Fa%2Fcredential", headers=ORIGIN
    )
    own_delete = client.delete(f"/api/auth/passkeys/{first_id}", headers=ORIGIN)

    assert listed.status_code == 200
    assert listed.json() == {
        "passkeys": [
            {
                "credential_id": second_id,
                "name": "Laptop",
                "transports": ["hybrid", "internal"],
                "created_at": 60_001.0,
                "last_used_at": None,
            },
            {
                "credential_id": first_id,
                "name": "Phone",
                "transports": ["internal"],
                "created_at": 60_000.0,
                "last_used_at": None,
            },
        ]
    }
    assert "public_key" not in listed.text
    assert "sign_count" not in listed.text
    assert foreign_delete.status_code == 404
    assert malformed_delete.status_code == 404
    assert own_delete.status_code == 200
    with sqlite3.connect(database) as con:
        assert con.execute(
            "SELECT credential_id FROM auth_passkeys ORDER BY credential_id"
        ).fetchall() == sorted([(second_id,), (foreign_id,)])


def test_malformed_or_unsupported_assertion_is_consumed_without_breaking_password_login(
    monkeypatch, tmp_path
):
    server, auth_data, database = load_server(monkeypatch, tmp_path)
    user_id, _session = seed_account(
        server,
        auth_data,
        "fallback@example.com",
        password="password fallback works",
        now=70_000.0,
    )
    credential_id = b64url(b"malformed credential")
    insert_passkey(server, credential_id=credential_id, user_id=user_id)
    client = TestClient(server.app, base_url="https://uvar.si")
    options_response = client.post(
        "/api/auth/passkey/login/options",
        headers=ORIGIN,
        json={"email": "fallback@example.com"},
    )
    assert options_response.status_code == 200
    options = options_response.json()
    unsupported = credential_payload(credential_id)
    unsupported["type"] = "password"

    malformed = client.post(
        "/api/auth/passkey/login/verify",
        headers=ORIGIN,
        json={"challenge": options["challenge"], "credential": unsupported},
    )
    missing = client.post(
        "/api/auth/passkey/login/verify", headers=ORIGIN, content="{"
    )
    password = client.post(
        "/api/auth/login",
        headers=ORIGIN,
        json={
            "email": "fallback@example.com",
            "password": "password fallback works",
        },
    )

    assert malformed.status_code == 400
    assert missing.status_code == 400
    assert password.status_code == 200
    assert password.cookies.get(server.COOKIE)
    with sqlite3.connect(database) as con:
        assert con.execute(
            "SELECT COUNT(*) FROM auth_webauthn_challenges"
        ).fetchone() == (0,)
        assert con.execute(
            "SELECT sign_count FROM auth_passkeys WHERE credential_id=?",
            (credential_id,),
        ).fetchone() == (0,)


def test_passkey_routes_are_hidden_when_auth_v3_feature_is_disabled(
    monkeypatch, tmp_path
):
    server, auth_data, _database = load_server(
        monkeypatch, tmp_path, enabled=False
    )
    _user_id, session = seed_account(
        server,
        auth_data,
        "flag@example.com",
        password="password remains available",
    )
    authenticated = authenticated_client(server, session)
    public = TestClient(server.app, base_url="https://uvar.si")

    assert authenticated.get("/api/auth/passkeys").status_code == 404
    assert authenticated.post(
        "/api/auth/passkey/register/options", headers=ORIGIN, json={}
    ).status_code == 404
    assert public.post(
        "/api/auth/passkey/login/options",
        headers=ORIGIN,
        json={"email": "flag@example.com"},
    ).status_code == 404
    assert public.post(
        "/api/auth/login",
        headers=ORIGIN,
        json={
            "email": "flag@example.com",
            "password": "password remains available",
        },
    ).status_code == 404
    monkeypatch.setenv("UVARSI_AUTH_V3", "1")
    assert public.post(
        "/api/auth/login",
        headers=ORIGIN,
        json={
            "email": "flag@example.com",
            "password": "password remains available",
        },
    ).status_code == 200
    assert authenticated.get("/api/auth/passkeys").status_code == 200


def test_challenge_creation_limits_account_for_registration_and_ip_for_login(
    monkeypatch, tmp_path
):
    server, auth_data, _database = load_server(monkeypatch, tmp_path)
    _user_id, session = seed_account(server, auth_data, "limit@example.com")
    authenticated = authenticated_client(server, session)
    server.AUTH_V3_IP_LIMITER = server.ClientIpRateLimiter(max_requests=10)
    server.AUTH_V3_ACCOUNT_LIMITER = server.ClientIpRateLimiter(max_requests=1)

    first_register = authenticated.post(
        "/api/auth/passkey/register/options", headers=ORIGIN, json={}
    )
    limited_register = authenticated.post(
        "/api/auth/passkey/register/options", headers=ORIGIN, json={}
    )

    assert first_register.status_code == 200
    assert limited_register.status_code == 429

    server.AUTH_V3_IP_LIMITER = server.ClientIpRateLimiter(max_requests=10)
    server.AUTH_V3_ACCOUNT_LIMITER = server.ClientIpRateLimiter(max_requests=1)
    public = TestClient(server.app, base_url="https://uvar.si")
    first_login = public.post(
        "/api/auth/passkey/login/options",
        headers=ORIGIN,
        json={"email": " LIMIT@example.com "},
    )
    second_username_less_login = public.post(
        "/api/auth/passkey/login/options",
        headers=ORIGIN,
        json={"email": "limit@example.com"},
    )

    assert first_login.status_code == 200
    assert second_username_less_login.status_code == 200

    server.AUTH_V3_IP_LIMITER = server.ClientIpRateLimiter(max_requests=1)
    server.AUTH_V3_ACCOUNT_LIMITER = server.ClientIpRateLimiter(max_requests=10)
    first_ip = public.post(
        "/api/auth/passkey/login/options",
        headers=ORIGIN,
        json={"email": "first-unknown@example.com"},
    )
    limited_ip = public.post(
        "/api/auth/passkey/login/options",
        headers=ORIGIN,
        json={"email": "second-unknown@example.com"},
    )

    assert first_ip.status_code == 200
    assert limited_ip.status_code == 429
