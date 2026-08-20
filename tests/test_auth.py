import asyncio
import hashlib
import importlib
import re
import sqlite3
import sys
import threading
import types
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient


ROOT = Path(__file__).resolve().parents[1]
SUCCESS_MESSAGE = (
    "Poskytovateľ prijal žiadosť o prihlasovací e-mail. "
    "Odkaz bude platný 60 minút."
)
PROVIDER_FAILURE_MESSAGE = (
    "Prihlasovací e-mail sa teraz nepodarilo odovzdať poskytovateľovi. "
    "Skús to znova o chvíľu."
)


def load_auth_server(monkeypatch, tmp_path):
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
    monkeypatch.syspath_prepend(str(ROOT / "app"))
    sys.modules.pop("server", None)
    sys.modules.pop("auth_data", None)
    server = importlib.import_module("server")
    server.ENV_FILE = str(tmp_path / "missing.env")
    return server, database


class ProviderResponse:
    def __init__(self, status_code=202, payload=None, text=""):
        self.status_code = status_code
        self._payload = {"id": "email_opaque_123"} if payload is None else payload
        self.text = text
        self.headers = {"x-request-id": "request_opaque_456"}

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


def install_provider(monkeypatch, *, response=None, error=None, calls=None):
    def post(url, **kwargs):
        if calls is not None:
            calls.append((url, kwargs))
        if error is not None:
            raise error
        return response or ProviderResponse()

    monkeypatch.setitem(sys.modules, "requests", types.SimpleNamespace(post=post))


def request_link(server, email="Cook@example.com"):
    return TestClient(server.app).post("/api/auth/request", json={"email": email})


def outbound_token(calls):
    text = calls[0][1]["json"]["text"]
    match = re.search(r"https://uvar\.si/prihlasenie#token=([A-Za-z0-9_-]+)", text)
    assert match, text
    return match.group(1)


def test_missing_resend_key_returns_truthful_503_without_leaking_message(capsys, monkeypatch, tmp_path):
    server, _ = load_auth_server(monkeypatch, tmp_path)
    monkeypatch.delenv("RESEND_API_KEY", raising=False)

    response = request_link(server)

    captured = capsys.readouterr()
    assert response.status_code == 503
    assert response.json()["detail"] == PROVIDER_FAILURE_MESSAGE
    assert "prihlasenie" not in captured.out + captured.err
    assert "cook@example.com" not in captured.out + captured.err


def test_resend_timeout_returns_truthful_503_without_leaking_message(capsys, monkeypatch, tmp_path):
    server, _ = load_auth_server(monkeypatch, tmp_path)
    monkeypatch.setenv("RESEND_API_KEY", "test-only-key")
    install_provider(monkeypatch, error=TimeoutError("transport timeout"))

    response = request_link(server)

    captured = capsys.readouterr()
    assert response.status_code == 503
    assert response.json()["detail"] == PROVIDER_FAILURE_MESSAGE
    assert "https://uvar.si/prihlasenie" not in captured.out + captured.err
    assert "cook@example.com" not in captured.out + captured.err


def test_resend_non_2xx_returns_truthful_503_without_printing_provider_body(capsys, monkeypatch, tmp_path):
    server, _ = load_auth_server(monkeypatch, tmp_path)
    monkeypatch.setenv("RESEND_API_KEY", "test-only-key")
    install_provider(
        monkeypatch,
        response=ProviderResponse(status_code=500, payload={"error": "no"}, text="PRIVATE PROVIDER BODY"),
    )

    response = request_link(server)

    captured = capsys.readouterr()
    assert response.status_code == 503
    assert response.json()["detail"] == PROVIDER_FAILURE_MESSAGE
    assert "PRIVATE PROVIDER BODY" not in captured.out + captured.err
    assert "https://uvar.si/prihlasenie" not in captured.out + captured.err


@pytest.mark.parametrize("redirect_status", [301, 302, 307, 308])
def test_resend_never_follows_redirect_with_token_bearing_body(
    capsys, monkeypatch, tmp_path, redirect_status
):
    server, _ = load_auth_server(monkeypatch, tmp_path)
    monkeypatch.setenv("RESEND_API_KEY", "test-only-key")
    forwarded_payloads = []

    def post(url, **kwargs):
        if kwargs.get("allow_redirects", True):
            forwarded_payloads.append(kwargs["json"])
            return ProviderResponse(status_code=202)
        return ProviderResponse(status_code=redirect_status)

    monkeypatch.setitem(sys.modules, "requests", types.SimpleNamespace(post=post))

    response = request_link(server)

    captured = capsys.readouterr()
    assert response.status_code == 503
    assert response.json()["detail"] == PROVIDER_FAILURE_MESSAGE
    assert forwarded_payloads == []
    assert "https://uvar.si/prihlasenie" not in captured.out + captured.err


def test_malformed_resend_success_response_returns_truthful_503(monkeypatch, tmp_path):
    server, _ = load_auth_server(monkeypatch, tmp_path)
    monkeypatch.setenv("RESEND_API_KEY", "test-only-key")
    install_provider(monkeypatch, response=ProviderResponse(payload={"unexpected": True}))

    response = request_link(server)

    assert response.status_code == 503
    assert response.json()["detail"] == PROVIDER_FAILURE_MESSAGE


def test_resend_response_missing_http_status_returns_truthful_503(monkeypatch, tmp_path):
    server, _ = load_auth_server(monkeypatch, tmp_path)
    monkeypatch.setenv("RESEND_API_KEY", "test-only-key")
    install_provider(monkeypatch, response=object())

    response = TestClient(server.app, raise_server_exceptions=False).post(
        "/api/auth/request", json={"email": "cook@example.com"}
    )

    assert response.status_code == 503
    assert response.json()["detail"] == PROVIDER_FAILURE_MESSAGE


def test_provider_acceptance_creates_one_hashed_60_minute_fragment_token(monkeypatch, tmp_path):
    server, database = load_auth_server(monkeypatch, tmp_path)
    monkeypatch.setenv("RESEND_API_KEY", "test-only-key")
    calls = []
    install_provider(monkeypatch, calls=calls)
    monkeypatch.setattr(server, "AUTH_CLOCK", lambda: 1_800_000_000.0, raising=False)

    response = request_link(server)

    assert response.status_code == 200
    assert response.json() == {"ok": True, "message": SUCCESS_MESSAGE}
    assert len(calls) == 1
    token = outbound_token(calls)
    outbound = calls[0][1]["json"]
    assert f"https://uvar.si/prihlasenie#token={token}" in outbound["html"]
    assert "/prihlasenie?token=" not in outbound["text"] + outbound["html"]
    assert "60 minút" in outbound["text"] + outbound["html"]

    with sqlite3.connect(database) as con:
        tables = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "magic_tokens_v2" in tables
        rows = con.execute(
            "SELECT token_hash, email, expires_at FROM magic_tokens_v2"
        ).fetchall()
    assert rows == [(hashlib.sha256(token.encode()).hexdigest(), "cook@example.com", 1_800_003_600.0)]
    assert token not in repr(rows)


@pytest.mark.parametrize(
    "email",
    [
        "missing-at.example.com",
        "two@@example.com",
        "space in@example.com",
        "user@example.com trailing",
        "user@example",
        ".leading@example.com",
        "double..dot@example.com",
        "user@-example.com",
    ],
)
def test_email_validation_uses_the_full_normalized_address(monkeypatch, tmp_path, email):
    server, _ = load_auth_server(monkeypatch, tmp_path)
    monkeypatch.setenv("RESEND_API_KEY", "test-only-key")
    calls = []
    install_provider(monkeypatch, calls=calls)

    response = request_link(server, email)

    assert response.status_code == 400
    assert response.json()["detail"] == "Zadaj platnú e-mailovú adresu."
    assert calls == []


def test_malformed_json_body_returns_the_same_safe_400(monkeypatch, tmp_path):
    server, _ = load_auth_server(monkeypatch, tmp_path)
    response = TestClient(server.app, raise_server_exceptions=False).post(
        "/api/auth/request",
        content="{",
        headers={"Content-Type": "application/json"},
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "Zadaj platnú e-mailovú adresu."


@pytest.mark.parametrize("body", [[], ["cook@example.com"], "cook@example.com", 7, None])
def test_non_object_json_body_returns_the_same_safe_400(monkeypatch, tmp_path, body):
    server, _ = load_auth_server(monkeypatch, tmp_path)
    response = TestClient(server.app, raise_server_exceptions=False).post(
        "/api/auth/request", json=body
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "Zadaj platnú e-mailovú adresu."


@pytest.mark.parametrize(
    "email, expected_status",
    [
        ("a" * 64 + "@example.com", 200),
        ("a" * 65 + "@example.com", 400),
        ("a" * 64 + "@" + "b" * 63 + "." + "c" * 63 + "." + "d" * 61, 200),
        ("a" * 64 + "@" + "b" * 63 + "." + "c" * 63 + "." + "d" * 62, 400),
        ("user@" + "a" * 63 + ".com", 200),
        ("user@" + "a" * 64 + ".com", 400),
        ("user@example..com", 400),
        ("tést@example.com", 400),
        ("test@exämple.com", 400),
    ],
)
def test_ascii_email_length_and_label_boundaries(monkeypatch, tmp_path, email, expected_status):
    server, _ = load_auth_server(monkeypatch, tmp_path)
    monkeypatch.setenv("RESEND_API_KEY", "test-only-key")
    calls = []
    install_provider(monkeypatch, calls=calls)

    response = request_link(server, email)

    assert response.status_code == expected_status
    assert len(calls) == (1 if expected_status == 200 else 0)


def test_failed_resend_preserves_the_older_unexpired_token(monkeypatch, tmp_path):
    server, database = load_auth_server(monkeypatch, tmp_path)
    monkeypatch.setenv("RESEND_API_KEY", "test-only-key")
    now = [1_800_000_000.0]
    monkeypatch.setattr(server, "AUTH_CLOCK", lambda: now[0])
    first_calls = []
    install_provider(monkeypatch, calls=first_calls)
    assert request_link(server).status_code == 200
    first_token = outbound_token(first_calls)

    now[0] += 61
    install_provider(monkeypatch, error=TimeoutError("transport timeout"))
    response = request_link(server, "cook@example.com")

    assert response.status_code == 503
    with sqlite3.connect(database) as con:
        rows = con.execute("SELECT token_hash, email FROM magic_tokens_v2").fetchall()
        reservations = con.execute("SELECT COUNT(*) FROM magic_token_reservations").fetchone()[0]
    assert rows == [(hashlib.sha256(first_token.encode()).hexdigest(), "cook@example.com")]
    assert reservations == 0


def test_provider_pause_keeps_reservation_short_and_does_not_block_unrelated_write(
    monkeypatch, tmp_path
):
    server, database = load_auth_server(monkeypatch, tmp_path)
    monkeypatch.setenv("RESEND_API_KEY", "test-only-key")
    monkeypatch.setattr(server, "AUTH_CLOCK", lambda: 1_800_000_000.0)
    entered = threading.Event()
    release = threading.Event()
    calls = []

    def post(url, **kwargs):
        calls.append((url, kwargs))
        entered.set()
        if not release.wait(2):
            raise TimeoutError("test provider was not released")
        return ProviderResponse()

    monkeypatch.setitem(sys.modules, "requests", types.SimpleNamespace(post=post))
    with ThreadPoolExecutor(max_workers=1) as pool:
        request = pool.submit(request_link, server)
        assert entered.wait(1), "provider was not reached"
        try:
            with sqlite3.connect(database, timeout=0.05) as con:
                con.execute("INSERT INTO pouzivatelia (email) VALUES ('unrelated@example.com')")
                con.commit()
                reservation = con.execute(
                    "SELECT email, token_hash FROM magic_token_reservations"
                ).fetchone()
            raw_token = outbound_token(calls)
            assert reservation == (
                "cook@example.com",
                hashlib.sha256(raw_token.encode()).hexdigest(),
            )
            assert raw_token not in repr(reservation)
        finally:
            release.set()
        response = request.result(timeout=2)

    assert response.status_code == 200


def test_paused_provider_does_not_delay_async_event_loop_heartbeat(monkeypatch, tmp_path):
    server, _ = load_auth_server(monkeypatch, tmp_path)
    monkeypatch.setenv("RESEND_API_KEY", "test-only-key")
    monkeypatch.setattr(server, "AUTH_CLOCK", lambda: 1_800_000_000.0)
    entered = threading.Event()
    release = threading.Event()
    provider_timed_out = threading.Event()

    def post(url, **kwargs):
        entered.set()
        if not release.wait(1):
            provider_timed_out.set()
        return ProviderResponse()

    monkeypatch.setitem(sys.modules, "requests", types.SimpleNamespace(post=post))

    async def scenario():
        transport = httpx.ASGITransport(app=server.app)
        async with httpx.AsyncClient(transport=transport, base_url="https://testserver") as client:
            request = asyncio.create_task(
                client.post("/api/auth/request", json={"email": "cook@example.com"})
            )
            assert await asyncio.to_thread(entered.wait, 2)
            heartbeat = asyncio.Event()
            asyncio.get_running_loop().call_soon(heartbeat.set)
            await asyncio.wait_for(heartbeat.wait(), timeout=0.2)
            assert not provider_timed_out.is_set()
            release.set()
            return await request

    try:
        response = asyncio.run(scenario())
    finally:
        release.set()

    assert response.status_code == 200


def test_concurrent_same_email_request_gets_in_progress_response_without_second_provider_call(
    monkeypatch, tmp_path
):
    server, _ = load_auth_server(monkeypatch, tmp_path)
    monkeypatch.setenv("RESEND_API_KEY", "test-only-key")
    monkeypatch.setattr(server, "AUTH_CLOCK", lambda: 1_800_000_000.0)
    entered = threading.Event()
    release = threading.Event()
    second_done = threading.Event()
    calls = []

    def post(url, **kwargs):
        calls.append((url, kwargs))
        entered.set()
        if not release.wait(2):
            raise TimeoutError("test provider was not released")
        return ProviderResponse()

    monkeypatch.setitem(sys.modules, "requests", types.SimpleNamespace(post=post))
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(request_link, server)
        assert entered.wait(1), "first provider call was not reached"
        second = pool.submit(request_link, server, "COOK@example.com")
        second.add_done_callback(lambda _: second_done.set())
        try:
            assert second_done.wait(1), "second request waited for provider I/O"
            second_response = second.result()
            assert second_response.status_code == 429
        finally:
            release.set()
        first_response = first.result(timeout=2)

    assert first_response.status_code == 200
    assert len(calls) == 1


def test_cancelled_request_keeps_exclusive_reservation_until_delivery_finalizes(
    monkeypatch, tmp_path
):
    server, database = load_auth_server(monkeypatch, tmp_path)
    monkeypatch.setenv("RESEND_API_KEY", "test-only-key")
    monkeypatch.setattr(server, "AUTH_CLOCK", lambda: 1_800_000_000.0)
    entered = threading.Event()
    release = threading.Event()
    retry_observed = threading.Event()
    calls = []

    def post(url, **kwargs):
        calls.append((url, kwargs))
        entered.set()
        if len(calls) > 1:
            retry_observed.set()
        if not release.wait(2):
            raise TimeoutError("test provider was not released")
        return ProviderResponse()

    monkeypatch.setitem(sys.modules, "requests", types.SimpleNamespace(post=post))

    async def scenario():
        transport = httpx.ASGITransport(app=server.app)
        async with httpx.AsyncClient(transport=transport, base_url="https://testserver") as client:
            request = asyncio.create_task(
                client.post("/api/auth/request", json={"email": "cook@example.com"})
            )
            assert await asyncio.to_thread(entered.wait, 2)
            request.cancel()
            retry = asyncio.create_task(
                client.post("/api/auth/request", json={"email": "COOK@example.com"})
            )
            retry.add_done_callback(lambda _: retry_observed.set())
            assert await asyncio.to_thread(retry_observed.wait, 2)
            calls_before_release = len(calls)
            with sqlite3.connect(database) as con:
                reserved_hash_before_release = con.execute(
                    "SELECT token_hash FROM magic_token_reservations WHERE email=?",
                    ("cook@example.com",),
                ).fetchone()
            request.cancel()
            release.set()
            request_result, retry_result = await asyncio.gather(
                request, retry, return_exceptions=True
            )
            return (
                request_result,
                getattr(retry_result, "status_code", None),
                calls_before_release,
                reserved_hash_before_release,
            )

    try:
        request_result, retry_status, calls_before_release, reserved_before = asyncio.run(
            scenario()
        )
    finally:
        release.set()

    raw_token = outbound_token(calls)
    raw_hash = hashlib.sha256(raw_token.encode()).hexdigest()
    assert isinstance(request_result, asyncio.CancelledError)
    assert retry_status == 429
    assert calls_before_release == len(calls) == 1
    assert reserved_before == (raw_hash,)
    with sqlite3.connect(database) as con:
        active = con.execute("SELECT token_hash, email FROM magic_tokens_v2").fetchall()
        reservations = con.execute("SELECT COUNT(*) FROM magic_token_reservations").fetchone()[0]
    assert active == [(raw_hash, "cook@example.com")]
    assert reservations == 0
    assert TestClient(server.app).post(
        "/api/auth/verify", json={"token": raw_token}
    ).status_code == 200


def test_provider_failure_after_cancellation_cleans_only_pending_reservation(
    monkeypatch, tmp_path
):
    server, database = load_auth_server(monkeypatch, tmp_path)
    monkeypatch.setenv("RESEND_API_KEY", "test-only-key")
    monkeypatch.setattr(server, "AUTH_CLOCK", lambda: 1_800_000_000.0)
    old_token = "older-still-valid-token"
    old_hash = hashlib.sha256(old_token.encode()).hexdigest()
    with server.db() as con:
        con.execute(
            """INSERT INTO magic_tokens_v2
               (token_hash, email, expires_at, created_at) VALUES (?, ?, ?, ?)""",
            (old_hash, "cook@example.com", 1_800_003_600.0, 1_799_999_000.0),
        )
        con.commit()
    entered = threading.Event()
    release = threading.Event()
    retry_observed = threading.Event()
    calls = []

    def post(url, **kwargs):
        calls.append((url, kwargs))
        entered.set()
        if len(calls) > 1:
            retry_observed.set()
        if not release.wait(2):
            raise TimeoutError("test provider was not released")
        raise TimeoutError("provider failed after cancellation")

    monkeypatch.setitem(sys.modules, "requests", types.SimpleNamespace(post=post))

    async def scenario():
        transport = httpx.ASGITransport(app=server.app)
        async with httpx.AsyncClient(transport=transport, base_url="https://testserver") as client:
            request = asyncio.create_task(
                client.post("/api/auth/request", json={"email": "cook@example.com"})
            )
            assert await asyncio.to_thread(entered.wait, 2)
            request.cancel()
            retry = asyncio.create_task(
                client.post("/api/auth/request", json={"email": "COOK@example.com"})
            )
            retry.add_done_callback(lambda _: retry_observed.set())
            assert await asyncio.to_thread(retry_observed.wait, 2)
            calls_before_release = len(calls)
            request.cancel()
            release.set()
            request_result, retry_result = await asyncio.gather(
                request, retry, return_exceptions=True
            )
            return request_result, getattr(retry_result, "status_code", None), calls_before_release

    try:
        request_result, retry_status, calls_before_release = asyncio.run(scenario())
    finally:
        release.set()

    assert isinstance(request_result, asyncio.CancelledError)
    assert retry_status == 429
    assert calls_before_release == len(calls) == 1
    with sqlite3.connect(database) as con:
        active = con.execute("SELECT token_hash, email FROM magic_tokens_v2").fetchall()
        reservations = con.execute("SELECT COUNT(*) FROM magic_token_reservations").fetchone()[0]
    assert active == [(old_hash, "cook@example.com")]
    assert reservations == 0
    assert TestClient(server.app).post(
        "/api/auth/verify", json={"token": old_token}
    ).status_code == 200


def test_provider_acceptance_finalize_failure_preserves_reservation_for_recovery(
    capsys, monkeypatch, tmp_path
):
    server, database = load_auth_server(monkeypatch, tmp_path)
    monkeypatch.setenv("RESEND_API_KEY", "test-only-key")
    monkeypatch.setattr(server, "AUTH_CLOCK", lambda: 1_800_000_000.0)
    calls = []
    install_provider(monkeypatch, calls=calls)

    def fail_finalize(*args, **kwargs):
        raise sqlite3.OperationalError("injected finalize failure")

    monkeypatch.setattr(server, "promote_magic_token", fail_finalize)

    response = TestClient(server.app, raise_server_exceptions=False).post(
        "/api/auth/request", json={"email": "cook@example.com"}
    )

    raw_token = outbound_token(calls)
    captured = capsys.readouterr()
    with sqlite3.connect(database) as con:
        reservations = con.execute(
            "SELECT email, token_hash FROM magic_token_reservations"
        ).fetchall()
        active_count = con.execute("SELECT COUNT(*) FROM magic_tokens_v2").fetchone()[0]
    assert response.status_code == 500
    assert reservations == [
        ("cook@example.com", hashlib.sha256(raw_token.encode()).hexdigest())
    ]
    assert active_count == 0
    assert raw_token not in captured.out + captured.err
    assert "https://uvar.si/prihlasenie" not in captured.out + captured.err


def test_successful_resend_invalidates_the_prior_token(monkeypatch, tmp_path):
    server, database = load_auth_server(monkeypatch, tmp_path)
    monkeypatch.setenv("RESEND_API_KEY", "test-only-key")
    now = [1_800_000_000.0]
    monkeypatch.setattr(server, "AUTH_CLOCK", lambda: now[0])
    first_calls = []
    install_provider(monkeypatch, calls=first_calls)
    assert request_link(server).status_code == 200
    first_token = outbound_token(first_calls)

    now[0] += 60
    second_calls = []
    install_provider(monkeypatch, calls=second_calls)
    response = request_link(server, "COOK@example.com")
    second_token = outbound_token(second_calls)

    assert response.status_code == 200
    assert second_token != first_token
    with sqlite3.connect(database) as con:
        rows = con.execute("SELECT token_hash, email FROM magic_tokens_v2").fetchall()
    assert rows == [(hashlib.sha256(second_token.encode()).hexdigest(), "cook@example.com")]


def test_normalized_email_has_a_db_backed_60_second_cooldown(monkeypatch, tmp_path):
    server, database = load_auth_server(monkeypatch, tmp_path)
    monkeypatch.setenv("RESEND_API_KEY", "test-only-key")
    now = [1_800_000_000.0]
    monkeypatch.setattr(server, "AUTH_CLOCK", lambda: now[0])
    calls = []
    install_provider(monkeypatch, calls=calls)
    assert request_link(server, " Cook@Example.com ").status_code == 200

    now[0] += 59
    limited = request_link(server, "cook@example.com")
    now[0] += 1
    accepted = request_link(server, "COOK@example.com")

    assert limited.status_code == 429
    assert limited.json()["detail"] == "Nový odkaz môžeš vyžiadať po 60 sekundách."
    assert accepted.status_code == 200
    assert len(calls) == 2
    with sqlite3.connect(database) as con:
        cooldown = con.execute(
            "SELECT email, sent_at FROM auth_email_cooldowns"
        ).fetchall()
    assert cooldown == [("cook@example.com", 1_800_000_060.0)]


def test_stale_reservations_are_pruned_even_when_current_email_is_on_cooldown(
    monkeypatch, tmp_path
):
    server, database = load_auth_server(monkeypatch, tmp_path)
    monkeypatch.setattr(server, "AUTH_CLOCK", lambda: 1_800_000_000.0)
    with server.db() as con:
        con.execute(
            """INSERT INTO magic_token_reservations
               (email, reservation_id, token_hash, expires_at, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            ("stale@example.com", "reservation_stale", "a" * 64, 1_799_999_999.0, 0),
        )
        con.execute(
            "INSERT INTO auth_email_cooldowns (email, sent_at) VALUES (?, ?)",
            ("cooldown@example.com", 1_800_000_000.0),
        )
        con.commit()

    response = request_link(server, "cooldown@example.com")

    assert response.status_code == 429
    with sqlite3.connect(database) as con:
        stale_count = con.execute(
            "SELECT COUNT(*) FROM magic_token_reservations WHERE email='stale@example.com'"
        ).fetchone()[0]
    assert stale_count == 0


def test_client_ip_is_limited_to_five_requests_per_ten_minutes(monkeypatch, tmp_path):
    server, _ = load_auth_server(monkeypatch, tmp_path)
    monkeypatch.setenv("RESEND_API_KEY", "test-only-key")
    monkeypatch.setattr(server, "AUTH_CLOCK", lambda: 1_800_000_000.0)
    calls = []
    install_provider(monkeypatch, calls=calls)
    client = TestClient(server.app, client=("198.51.100.20", 4000))

    accepted = [
        client.post("/api/auth/request", json={"email": f"cook{index}@example.com"})
        for index in range(5)
    ]
    limited = client.post(
        "/api/auth/request",
        json={"email": "sixth@example.com"},
        headers={"X-Forwarded-For": "203.0.113.99"},
    )

    assert [response.status_code for response in accepted] == [200] * 5
    assert limited.status_code == 429
    assert limited.json()["detail"] == "Priveľa pokusov. Skús to znova o 10 minút."
    assert len(calls) == 5


def test_ip_limiter_globally_prunes_expired_clients_before_applying_cardinality_bound(
    monkeypatch, tmp_path
):
    server, _ = load_auth_server(monkeypatch, tmp_path)
    limiter = server.ClientIpRateLimiter(max_requests=1, window_seconds=10, max_clients=2)

    assert limiter.allow("198.51.100.1", 0)
    assert limiter.allow("198.51.100.2", 0)
    assert not limiter.allow("198.51.100.3", 0)
    assert limiter.allow("198.51.100.3", 10)
    assert limiter.allow("198.51.100.1", 10)


def test_ip_limiter_rolls_each_client_window_at_exact_boundary(monkeypatch, tmp_path):
    server, _ = load_auth_server(monkeypatch, tmp_path)
    limiter = server.ClientIpRateLimiter(max_requests=2, window_seconds=600, max_clients=10)

    assert limiter.allow("198.51.100.1", 0)
    assert limiter.allow("198.51.100.1", 0)
    assert not limiter.allow("198.51.100.1", 599.999)
    assert limiter.allow("198.51.100.1", 600)


def test_auth_request_response_does_not_enumerate_existing_accounts(monkeypatch, tmp_path):
    server, _ = load_auth_server(monkeypatch, tmp_path)
    monkeypatch.setenv("RESEND_API_KEY", "test-only-key")
    now = [1_800_000_000.0]
    monkeypatch.setattr(server, "AUTH_CLOCK", lambda: now[0])
    install_provider(monkeypatch)
    with server.db() as con:
        con.execute("INSERT INTO pouzivatelia (email) VALUES ('known@example.com')")
        con.commit()
    known_client = TestClient(server.app, client=("198.51.100.21", 4000))
    unknown_client = TestClient(server.app, client=("198.51.100.22", 4000))

    known = known_client.post("/api/auth/request", json={"email": "known@example.com"})
    unknown = unknown_client.post("/api/auth/request", json={"email": "unknown@example.com"})

    assert known.status_code == unknown.status_code == 200
    assert known.json() == unknown.json() == {"ok": True, "message": SUCCESS_MESSAGE}


def issue_link(server, monkeypatch, email="cook@example.com"):
    calls = []
    install_provider(monkeypatch, calls=calls)
    response = request_link(server, email)
    assert response.status_code == 200
    return outbound_token(calls)


def test_fragment_get_is_a_branded_confirmation_and_does_not_consume(monkeypatch, tmp_path):
    server, database = load_auth_server(monkeypatch, tmp_path)
    monkeypatch.setenv("RESEND_API_KEY", "test-only-key")
    monkeypatch.setattr(server, "AUTH_CLOCK", lambda: 1_800_000_000.0)
    token = issue_link(server, monkeypatch)

    response = TestClient(server.app).get("/prihlasenie")

    assert response.status_code == 200
    assert "Uvar.si" in response.text
    assert "Potvrdiť prihlásenie" in response.text
    assert "location.hash" in response.text
    assert "Požiadať o nový odkaz" in response.text
    assert token not in response.text
    with sqlite3.connect(database) as con:
        assert con.execute("SELECT COUNT(*) FROM magic_tokens_v2").fetchone()[0] == 1


def test_legacy_query_token_get_shows_error_without_consuming(monkeypatch, tmp_path):
    server, database = load_auth_server(monkeypatch, tmp_path)
    monkeypatch.setenv("RESEND_API_KEY", "test-only-key")
    monkeypatch.setattr(server, "AUTH_CLOCK", lambda: 1_800_000_000.0)
    token = issue_link(server, monkeypatch)

    response = TestClient(server.app).get(
        f"/prihlasenie?token={token}", follow_redirects=False
    )

    assert response.status_code == 400
    assert "starý formát" in response.text
    assert "Požiadať o nový odkaz" in response.text
    assert token not in response.text
    with sqlite3.connect(database) as con:
        assert con.execute("SELECT COUNT(*) FROM magic_tokens_v2").fetchone()[0] == 1


def test_confirmation_page_turns_verification_network_failure_into_resend_ux(monkeypatch, tmp_path):
    server, _ = load_auth_server(monkeypatch, tmp_path)

    response = TestClient(server.app).get("/prihlasenie")

    assert "Overenie sa nepodarilo pripojiť. Požiadaj o nový odkaz." in response.text
    assert "catch" in response.text


def test_legacy_plaintext_magic_token_is_never_trusted(monkeypatch, tmp_path):
    server, database = load_auth_server(monkeypatch, tmp_path)
    monkeypatch.setattr(server, "AUTH_CLOCK", lambda: 1_800_000_000.0)
    with server.db() as con:
        con.execute(
            "INSERT INTO tokeny (token, email, platny_do) VALUES (?, ?, ?)",
            ("legacy-raw-token", "legacy@example.com", "2099-01-01T00:00:00"),
        )
        con.commit()

    response = TestClient(server.app).post(
        "/api/auth/verify", json={"token": "legacy-raw-token"}
    )

    assert response.status_code == 400
    with sqlite3.connect(database) as con:
        assert con.execute("SELECT COUNT(*) FROM sessions_v2").fetchone()[0] == 0


def test_auth_schema_migration_is_idempotent(monkeypatch, tmp_path):
    server, database = load_auth_server(monkeypatch, tmp_path)

    with server.db():
        pass
    with server.db():
        pass

    with sqlite3.connect(database) as con:
        tables = {
            row[0]
            for row in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE '%v2'"
            )
        }
    assert tables == {"magic_tokens_v2", "sessions_v2"}


def test_post_redeems_once_into_a_hashed_30_day_host_only_session(monkeypatch, tmp_path):
    server, database = load_auth_server(monkeypatch, tmp_path)
    monkeypatch.setenv("RESEND_API_KEY", "test-only-key")
    monkeypatch.setattr(server, "AUTH_CLOCK", lambda: 1_800_000_000.0)
    token = issue_link(server, monkeypatch)
    client = TestClient(server.app, base_url="https://testserver")

    response = client.post("/api/auth/verify", json={"token": token})

    assert response.status_code == 200
    assert response.json() == {"ok": True, "redirect": "/app"}
    assert token not in response.text
    cookie = response.headers["set-cookie"]
    assert "uvarsi_session=" in cookie
    assert "HttpOnly" in cookie
    assert "Secure" in cookie
    assert "SameSite=lax" in cookie
    assert "Max-Age=2592000" in cookie
    assert "Domain=" not in cookie
    raw_session = client.cookies.get(server.COOKIE)
    with sqlite3.connect(database) as con:
        assert con.execute("SELECT COUNT(*) FROM magic_tokens_v2").fetchone()[0] == 0
        sessions = con.execute(
            "SELECT token_hash, expires_at FROM sessions_v2"
        ).fetchall()
    assert sessions == [
        (hashlib.sha256(raw_session.encode()).hexdigest(), 1_802_592_000.0)
    ]
    assert raw_session not in repr(sessions)


def test_expired_magic_token_is_deleted_with_explicit_resend_error(monkeypatch, tmp_path):
    server, database = load_auth_server(monkeypatch, tmp_path)
    monkeypatch.setattr(server, "AUTH_CLOCK", lambda: 1_800_000_000.0)
    raw = "expired-raw-token"
    with server.db() as con:
        con.execute(
            """INSERT INTO magic_tokens_v2
               (token_hash, email, expires_at, created_at) VALUES (?, ?, ?, ?)""",
            (hashlib.sha256(raw.encode()).hexdigest(), "cook@example.com", 1_799_999_999.0, 0),
        )
        con.commit()

    response = TestClient(server.app).post("/api/auth/verify", json={"token": raw})

    assert response.status_code == 410
    assert response.json()["detail"] == (
        "Odkaz vypršal. Požiadaj o nový prihlasovací odkaz."
    )
    with sqlite3.connect(database) as con:
        assert con.execute("SELECT COUNT(*) FROM magic_tokens_v2").fetchone()[0] == 0


def test_used_and_invalid_magic_tokens_fail_explicitly_without_home_redirect(monkeypatch, tmp_path):
    server, _ = load_auth_server(monkeypatch, tmp_path)
    monkeypatch.setenv("RESEND_API_KEY", "test-only-key")
    monkeypatch.setattr(server, "AUTH_CLOCK", lambda: 1_800_000_000.0)
    token = issue_link(server, monkeypatch)
    client = TestClient(server.app, base_url="https://testserver")
    assert client.post("/api/auth/verify", json={"token": token}).status_code == 200

    reused = client.post("/api/auth/verify", json={"token": token})
    invalid = client.post("/api/auth/verify", json={"token": "not-a-real-token"})

    for response in (reused, invalid):
        assert response.status_code == 400
        assert response.json()["detail"] == (
            "Odkaz je neplatný alebo už bol použitý. Požiadaj o nový."
        )
        assert response.headers.get("location") is None


def test_concurrent_redemption_creates_exactly_one_session(monkeypatch, tmp_path):
    server, database = load_auth_server(monkeypatch, tmp_path)
    monkeypatch.setenv("RESEND_API_KEY", "test-only-key")
    monkeypatch.setattr(server, "AUTH_CLOCK", lambda: 1_800_000_000.0)
    token = issue_link(server, monkeypatch)

    def redeem():
        return TestClient(server.app).post("/api/auth/verify", json={"token": token})

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _: redeem(), range(2)))

    assert sorted(response.status_code for response in responses) == [200, 400]
    with sqlite3.connect(database) as con:
        assert con.execute("SELECT COUNT(*) FROM sessions_v2").fetchone()[0] == 1


def test_expired_session_is_rejected_and_deleted_server_side(monkeypatch, tmp_path):
    server, database = load_auth_server(monkeypatch, tmp_path)
    monkeypatch.setenv("RESEND_API_KEY", "test-only-key")
    now = [1_800_000_000.0]
    monkeypatch.setattr(server, "AUTH_CLOCK", lambda: now[0])
    token = issue_link(server, monkeypatch)
    client = TestClient(server.app, base_url="https://testserver")
    assert client.post("/api/auth/verify", json={"token": token}).status_code == 200

    now[0] += 30 * 24 * 60 * 60 + 1
    response = client.get("/api/me")

    assert response.json() == {"prihlaseny": False}
    with sqlite3.connect(database) as con:
        assert con.execute("SELECT COUNT(*) FROM sessions_v2").fetchone()[0] == 0


def test_logout_deletes_current_hashed_session_and_cookie(monkeypatch, tmp_path):
    server, database = load_auth_server(monkeypatch, tmp_path)
    monkeypatch.setenv("RESEND_API_KEY", "test-only-key")
    monkeypatch.setattr(server, "AUTH_CLOCK", lambda: 1_800_000_000.0)
    token = issue_link(server, monkeypatch)
    client = TestClient(server.app, base_url="https://testserver")
    assert client.post("/api/auth/verify", json={"token": token}).status_code == 200

    response = client.post("/api/auth/logout")

    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert "uvarsi_session=\"\"" in response.headers["set-cookie"]
    with sqlite3.connect(database) as con:
        assert con.execute("SELECT COUNT(*) FROM sessions_v2").fetchone()[0] == 0


def test_second_magic_login_rotates_the_users_session(monkeypatch, tmp_path):
    server, database = load_auth_server(monkeypatch, tmp_path)
    monkeypatch.setenv("RESEND_API_KEY", "test-only-key")
    now = [1_800_000_000.0]
    monkeypatch.setattr(server, "AUTH_CLOCK", lambda: now[0])
    first_token = issue_link(server, monkeypatch)
    first_client = TestClient(server.app, base_url="https://testserver")
    assert first_client.post("/api/auth/verify", json={"token": first_token}).status_code == 200
    first_session = first_client.cookies.get(server.COOKIE)

    now[0] += 60
    second_token = issue_link(server, monkeypatch)
    second_client = TestClient(server.app, base_url="https://testserver")
    assert second_client.post("/api/auth/verify", json={"token": second_token}).status_code == 200

    assert first_client.get("/api/me").json() == {"prihlaseny": False}
    with sqlite3.connect(database) as con:
        sessions = con.execute("SELECT token_hash FROM sessions_v2").fetchall()
    assert len(sessions) == 1
    assert sessions[0][0] != hashlib.sha256(first_session.encode()).hexdigest()


def test_legacy_plaintext_session_is_invalid_after_release(monkeypatch, tmp_path):
    server, _ = load_auth_server(monkeypatch, tmp_path)
    with server.db() as con:
        con.execute("INSERT INTO pouzivatelia (id, email) VALUES (1, 'legacy@example.com')")
        con.execute("INSERT INTO sedenia (token, user_id) VALUES ('legacy-plaintext', 1)")
        con.commit()
    client = TestClient(server.app)
    client.cookies.set(server.COOKIE, "legacy-plaintext")

    response = client.get("/api/me")

    assert response.json() == {"prihlaseny": False}
