import asyncio
import hashlib
import importlib
import json
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import types
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from argon2 import PasswordHasher, Type
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
NODE = shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="node runtime is not available")


def confirmation_script(html):
    scripts = re.findall(r"<script>(.*?)</script>", html, re.DOTALL)
    assert len(scripts) == 1
    return scripts[0]


def run_confirmation_flow(tmp_path, html, scenarios):
    script = tmp_path / "magic-login-flow.js"
    script.write_text(
        "const loginScript=" + json.dumps(confirmation_script(html)) + ";\n"
        + "const scenarios=" + json.dumps(scenarios) + ";\n"
        + r"""
const vm=require('vm');
const shared=new Map();
const calls=[];
let replaced='';
function storage(available){
  if(!available) return new Proxy({}, {get(){throw new Error('storage unavailable')}});
  return {
    getItem(key){return shared.has(key)?shared.get(key):null},
    setItem(key,value){shared.set(key,String(value))},
    removeItem(key){shared.delete(key)}
  };
}
async function page(spec){
  const nodes=new Map();
  const node=id=>nodes.get(id)||nodes.set(id,{style:{},classList:{add(){}},textContent:'',onclick:null}).get(id);
  const context={
    URLSearchParams,
    location:{hash:spec.hash||'',pathname:'/prihlasenie',replace(value){replaced=value}},
    history:{replaceState(_state,_title,value){context.location.hash='';replaced=value}},
    sessionStorage:storage(spec.storage!==false),
    document:{getElementById:node},
    fetch:async(url,options)=>{
      calls.push({url,body:JSON.parse(options.body)});
      if(spec.networkError) throw new Error('offline');
      return {ok:spec.status===200,status:spec.status,json:async()=>spec.body||{}};
    }
  };
  vm.createContext(context);
  vm.runInContext(loginScript,context);
  if(spec.confirm) await node('confirm').onclick();
  return {status:node('status').textContent};
}
(async()=>{
  const states=[];
  for(const scenario of scenarios){
    await page(scenario);
    states.push({stored:[...shared.entries()],calls:[...calls],replaced});
  }
  process.stdout.write(JSON.stringify(states));
})().catch(error=>{console.error(error);process.exit(1)});
""",
        encoding="utf-8",
    )
    return subprocess.run([NODE, str(script)], capture_output=True, text=True)


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


def load_auth_data(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "app"))
    return importlib.import_module("auth_data")


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


@pytest.mark.parametrize("path,status", [("/prihlasenie", 200), ("/prihlasenie?token=legacy", 400)])
def test_every_login_page_response_is_private_and_noindex(monkeypatch, tmp_path, path, status):
    server, _ = load_auth_server(monkeypatch, tmp_path)

    response = TestClient(server.app).get(path, follow_redirects=False)

    assert response.status_code == status
    assert response.headers["x-robots-tag"] == "noindex, nofollow, noarchive"
    assert response.headers["cache-control"] == "private, no-store"
    assert '<meta name="robots" content="noindex,nofollow,noarchive">' in response.text


def test_confirmation_page_contract_persists_fragment_ephemerally_before_hiding_it(monkeypatch, tmp_path):
    server, _ = load_auth_server(monkeypatch, tmp_path)
    html = TestClient(server.app).get("/prihlasenie").text
    script = confirmation_script(html)

    assert "sessionStorage" in script
    assert "localStorage" not in script
    assert script.index("sessionStorage") < script.index("history.replaceState")
    assert re.search(r"sessionStorage\s*\.\s*setItem", script)
    assert re.search(r"sessionStorage\s*\.\s*getItem", script)
    assert re.search(r"sessionStorage\s*\.\s*removeItem", script)
    assert "try" in script and "catch" in script


@needs_node
def test_fragment_token_survives_reload_then_confirms_once_from_session_storage(monkeypatch, tmp_path):
    server, _ = load_auth_server(monkeypatch, tmp_path)
    html = TestClient(server.app).get("/prihlasenie").text

    result = run_confirmation_flow(
        tmp_path,
        html,
        [
            {"hash": "#token=fresh-secret", "status": 200},
            {"hash": "", "confirm": True, "status": 200, "body": {"redirect": "/app"}},
        ],
    )

    assert result.returncode == 0, result.stderr
    states = json.loads(result.stdout)
    assert states[0]["stored"]
    assert states[0]["replaced"] == "/prihlasenie"
    assert states[1]["calls"] == [
        {"url": "/api/auth/verify", "body": {"token": "fresh-secret"}}
    ]
    assert states[1]["stored"] == []
    assert states[1]["replaced"] == "/app"


@needs_node
def test_fresh_fragment_overrides_older_namespaced_session_token(monkeypatch, tmp_path):
    server, _ = load_auth_server(monkeypatch, tmp_path)
    html = TestClient(server.app).get("/prihlasenie").text

    result = run_confirmation_flow(
        tmp_path,
        html,
        [
            {"hash": "#token=older-secret", "status": 200},
            {"hash": "#token=fresh-secret", "status": 200},
            {"hash": "", "confirm": True, "status": 200, "body": {"redirect": "/app"}},
        ],
    )

    assert result.returncode == 0, result.stderr
    states = json.loads(result.stdout)
    assert states[0]["stored"] == [["uvarsi.auth.magic-token.v1", "older-secret"]]
    assert states[1]["stored"] == [["uvarsi.auth.magic-token.v1", "fresh-secret"]]
    assert states[2]["calls"] == [
        {"url": "/api/auth/verify", "body": {"token": "fresh-secret"}}
    ]
    assert states[2]["stored"] == []


@needs_node
@pytest.mark.parametrize("status", [400, 410])
def test_definitively_invalid_or_expired_token_is_removed_from_session_storage(
    monkeypatch, tmp_path, status
):
    server, _ = load_auth_server(monkeypatch, tmp_path)
    html = TestClient(server.app).get("/prihlasenie").text

    result = run_confirmation_flow(
        tmp_path,
        html,
        [
            {"hash": "#token=bad-secret", "status": 200},
            {
                "hash": "",
                "confirm": True,
                "status": status,
                "body": {"detail": "Odkaz už nemožno použiť."},
            },
        ],
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)[-1]["stored"] == []


@needs_node
def test_transient_network_failure_keeps_token_for_a_safe_reload_retry(monkeypatch, tmp_path):
    server, _ = load_auth_server(monkeypatch, tmp_path)
    html = TestClient(server.app).get("/prihlasenie").text

    result = run_confirmation_flow(
        tmp_path,
        html,
        [
            {"hash": "#token=retry-secret", "status": 200},
            {"hash": "", "confirm": True, "networkError": True, "status": 0},
        ],
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)[-1]["stored"]


@needs_node
def test_server_error_keeps_token_for_a_safe_reload_retry(monkeypatch, tmp_path):
    server, _ = load_auth_server(monkeypatch, tmp_path)
    html = TestClient(server.app).get("/prihlasenie").text

    result = run_confirmation_flow(
        tmp_path,
        html,
        [
            {"hash": "#token=retry-after-5xx", "status": 200},
            {
                "hash": "",
                "confirm": True,
                "status": 503,
                "body": {"detail": "Overenie je dočasne nedostupné."},
            },
        ],
    )

    assert result.returncode == 0, result.stderr
    state = json.loads(result.stdout)[-1]
    assert state["calls"] == [
        {"url": "/api/auth/verify", "body": {"token": "retry-after-5xx"}}
    ]
    assert state["stored"] == [
        ["uvarsi.auth.magic-token.v1", "retry-after-5xx"]
    ]


@needs_node
def test_fresh_fragment_still_works_when_session_storage_is_unavailable(monkeypatch, tmp_path):
    server, _ = load_auth_server(monkeypatch, tmp_path)
    html = TestClient(server.app).get("/prihlasenie").text

    result = run_confirmation_flow(
        tmp_path,
        html,
        [
            {
                "hash": "#token=memory-only-secret",
                "storage": False,
                "confirm": True,
                "status": 200,
                "body": {"redirect": "/app"},
            }
        ],
    )

    assert result.returncode == 0, result.stderr
    state = json.loads(result.stdout)[0]
    assert state["calls"] == [
        {"url": "/api/auth/verify", "body": {"token": "memory-only-secret"}}
    ]
    assert state["replaced"] == "/app"


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


def test_app_shell_is_private_and_noindex(monkeypatch, tmp_path):
    monkeypatch.setenv("UVARSI_STATIC", str(ROOT / "app" / "static"))
    server, _ = load_auth_server(monkeypatch, tmp_path)

    response = TestClient(server.app).get("/app")

    assert response.status_code == 200
    assert response.headers["x-robots-tag"] == "noindex, nofollow, noarchive"
    assert response.headers["cache-control"] == "private, no-store"


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


def test_auth_v3_migration_is_idempotent_and_preserves_existing_rows(monkeypatch, tmp_path):
    server, _ = load_auth_server(monkeypatch, tmp_path)
    database = tmp_path / "populated-auth-v2.db"
    selected_columns = {
        "pouzivatelia": "id, email",
        "naroky": "id, user_id, stav",
        "plany": "id, user_id, tyzden, json",
        "magic_tokens_v2": "token_hash, email, expires_at, created_at",
        "sessions_v2": "token_hash, user_id, expires_at, created_at",
    }

    with sqlite3.connect(database) as con:
        con.executescript(
            """
            CREATE TABLE pouzivatelia (id INTEGER PRIMARY KEY, email TEXT NOT NULL);
            CREATE TABLE naroky (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, stav TEXT NOT NULL);
            CREATE TABLE plany (
              id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL,
              tyzden TEXT NOT NULL, json TEXT NOT NULL
            );
            CREATE TABLE magic_tokens_v2 (
              token_hash TEXT PRIMARY KEY, email TEXT NOT NULL,
              expires_at REAL NOT NULL, created_at REAL NOT NULL
            );
            CREATE TABLE sessions_v2 (
              token_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL,
              expires_at REAL NOT NULL, created_at REAL NOT NULL
            );
            INSERT INTO pouzivatelia VALUES (7, 'existing@example.com');
            INSERT INTO naroky VALUES (11, 7, 'aktivny');
            INSERT INTO plany VALUES (13, 7, '2026-08-24', '{"jedla":[]}');
            INSERT INTO magic_tokens_v2
              VALUES ('existing-magic-hash', 'existing@example.com', 1900003600.0, 1900000000.0);
            INSERT INTO sessions_v2
              VALUES ('existing-session-hash', 7, 1902592000.0, 1900000000.0);
            """
        )
        before = {
            table: con.execute(f"SELECT {columns} FROM {table}").fetchall()
            for table, columns in selected_columns.items()
        }

        server.migrate_auth_schema(con)
        server.migrate_auth_schema(con)

        after = {
            table: con.execute(f"SELECT {columns} FROM {table}").fetchall()
            for table, columns in selected_columns.items()
        }
        session_columns = {
            row[1] for row in con.execute("PRAGMA table_info(sessions_v2)")
        }

    assert after == before
    assert {"last_seen_at", "device_name", "revoked_at"} <= session_columns


def test_auth_v3_migration_adds_account_tables_and_session_metadata(monkeypatch, tmp_path):
    server, _ = load_auth_server(monkeypatch, tmp_path)

    with sqlite3.connect(":memory:") as con:
        server.migrate_auth_schema(con)
        schemas = {
            table: [tuple(row[1:6]) for row in con.execute(f"PRAGMA table_info({table})")]
            for table in (
                "auth_credentials",
                "auth_action_tokens",
                "auth_passkeys",
                "auth_webauthn_challenges",
            )
        }
        session_columns = {
            row[1]: tuple(row[2:5])
            for row in con.execute("PRAGMA table_info(sessions_v2)")
        }

    assert schemas == {
        "auth_credentials": [
            ("user_id", "INTEGER", 0, None, 1),
            ("password_hash", "TEXT", 1, None, 0),
            ("changed_at", "REAL", 1, None, 0),
        ],
        "auth_action_tokens": [
            ("token_hash", "TEXT", 0, None, 1),
            ("email", "TEXT", 1, None, 0),
            ("purpose", "TEXT", 1, None, 0),
            ("pending_password_hash", "TEXT", 0, None, 0),
            ("expires_at", "REAL", 1, None, 0),
            ("created_at", "REAL", 1, None, 0),
        ],
        "auth_passkeys": [
            ("credential_id", "TEXT", 0, None, 1),
            ("user_id", "INTEGER", 1, None, 0),
            ("public_key", "BLOB", 1, None, 0),
            ("sign_count", "INTEGER", 1, None, 0),
            ("transports", "TEXT", 1, "'[]'", 0),
            ("name", "TEXT", 1, None, 0),
            ("created_at", "REAL", 1, None, 0),
            ("last_used_at", "REAL", 0, None, 0),
        ],
        "auth_webauthn_challenges": [
            ("challenge_hash", "TEXT", 0, None, 1),
            ("user_id", "INTEGER", 0, None, 0),
            ("purpose", "TEXT", 1, None, 0),
            ("expires_at", "REAL", 1, None, 0),
            ("created_at", "REAL", 1, None, 0),
        ],
    }
    assert {
        name: session_columns[name]
        for name in ("last_seen_at", "device_name", "revoked_at")
    } == {
        "last_seen_at": ("REAL", 0, None),
        "device_name": ("TEXT", 0, None),
        "revoked_at": ("REAL", 0, None),
    }


@pytest.mark.parametrize("purpose", ["confirm", "reset", "setup"])
def test_auth_v3_action_token_purpose_accepts_every_permitted_value(
    monkeypatch, tmp_path, purpose
):
    server, _ = load_auth_server(monkeypatch, tmp_path)

    with sqlite3.connect(":memory:") as con:
        server.migrate_auth_schema(con)
        con.execute(
            """INSERT INTO auth_action_tokens
               (token_hash, email, purpose, pending_password_hash, expires_at, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (f"{purpose}-hash", "existing@example.com", purpose, None, 2.0, 1.0),
        )
        stored = con.execute(
            "SELECT purpose FROM auth_action_tokens WHERE token_hash=?",
            (f"{purpose}-hash",),
        ).fetchone()

    assert stored == (purpose,)


def test_auth_v3_action_token_purpose_rejects_an_invalid_value(monkeypatch, tmp_path):
    server, _ = load_auth_server(monkeypatch, tmp_path)

    with sqlite3.connect(":memory:") as con:
        server.migrate_auth_schema(con)
        with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
            con.execute(
                """INSERT INTO auth_action_tokens
                   (token_hash, email, purpose, expires_at, created_at)
                   VALUES (?, ?, ?, ?, ?)""",
                ("invalid-action-hash", "existing@example.com", "login", 2.0, 1.0),
            )


@pytest.mark.parametrize(
    "password",
    [
        "a" * 10,
        "a" * 128,
        "žluťoučký🐈",
    ],
)
def test_password_primitive_accepts_10_to_128_unicode_characters(
    monkeypatch, password
):
    auth_data = load_auth_data(monkeypatch)

    assert auth_data.validate_password(password) == password


@pytest.mark.parametrize("password", ["a" * 9, "a" * 129, None, 10, b"abcdefghij"])
def test_password_primitive_rejects_values_outside_the_typed_character_boundary(
    monkeypatch, password
):
    auth_data = load_auth_data(monkeypatch)

    with pytest.raises(ValueError, match="^invalid password$"):
        auth_data.validate_password(password)


def test_password_primitive_preserves_leading_and_trailing_whitespace(monkeypatch):
    auth_data = load_auth_data(monkeypatch)
    password = " 12345678 "

    assert auth_data.validate_password(password) == password


def test_password_primitive_hashes_and_verifies_only_argon2id(monkeypatch):
    auth_data = load_auth_data(monkeypatch)
    password = "päss word🐈"

    encoded = auth_data.hash_password(password)
    argon2i_encoded = PasswordHasher(type=Type.I).hash(password)

    assert encoded.startswith("$argon2id$")
    assert password not in encoded
    assert auth_data.verify_password(encoded, password) is True
    assert auth_data.verify_password(encoded, "wrong password") is False
    assert auth_data.verify_password("not-an-argon2-hash", password) is False
    assert auth_data.verify_password(argon2i_encoded, password) is False


def test_password_primitive_flags_old_argon2id_parameters_without_rejecting_them(
    monkeypatch,
):
    auth_data = load_auth_data(monkeypatch)
    password = "parameter test"
    current = auth_data.hash_password(password)
    old_parameters = PasswordHasher(
        time_cost=1,
        memory_cost=8 * 1024,
        parallelism=1,
        hash_len=16,
        salt_len=16,
        type=Type.ID,
    ).hash(password)

    assert auth_data.password_needs_rehash(current) is False
    assert auth_data.password_needs_rehash(old_parameters) is True
    assert auth_data.verify_password(old_parameters, password) is True
    assert auth_data.password_needs_rehash("not-an-argon2-hash") is True


def test_password_primitive_stores_only_argon2id_and_authenticates_generically(
    monkeypatch,
):
    auth_data = load_auth_data(monkeypatch)
    password = "correct horse"

    with sqlite3.connect(":memory:") as con:
        con.execute(
            "CREATE TABLE pouzivatelia (id INTEGER PRIMARY KEY, email TEXT NOT NULL)"
        )
        con.execute(
            "INSERT INTO pouzivatelia (id, email) VALUES (?, ?)",
            (7, "cook@example.com"),
        )
        auth_data.migrate_auth_schema(con)

        with pytest.raises(ValueError, match="^invalid password hash$"):
            auth_data.set_password(
                con, user_id=7, password_hash=password, now=1_900_000_000.0
            )
        assert con.execute("SELECT * FROM auth_credentials").fetchall() == []

        encoded = auth_data.hash_password(password)
        auth_data.set_password(
            con, user_id=7, password_hash=encoded, now=1_900_000_001.0
        )
        stored = con.execute(
            "SELECT user_id, password_hash, changed_at FROM auth_credentials"
        ).fetchall()

        assert stored == [(7, encoded, 1_900_000_001.0)]
        assert password not in repr(stored)
        assert (
            auth_data.authenticate_password(
                con, email="cook@example.com", password=password
            )
            == 7
        )
        assert (
            auth_data.authenticate_password(
                con, email="cook@example.com", password="wrong password"
            )
            is None
        )
        assert (
            auth_data.authenticate_password(
                con, email="unknown@example.com", password=password
            )
            is None
        )

        con.execute(
            "UPDATE auth_credentials SET password_hash='malformed' WHERE user_id=7"
        )
        assert (
            auth_data.authenticate_password(
                con, email="cook@example.com", password=password
            )
            is None
        )


def test_action_token_primitive_hashes_and_consumes_confirmation_once(monkeypatch):
    auth_data = load_auth_data(monkeypatch)
    now = 1_900_000_000.0
    pending_hash = auth_data.hash_password("pending pass")

    with sqlite3.connect(":memory:") as con:
        auth_data.migrate_auth_schema(con)
        raw_token = auth_data.create_action_token(
            con,
            email="cook@example.com",
            purpose="confirm",
            now=now,
            pending_password_hash=pending_hash,
        )
        stored = con.execute(
            """SELECT token_hash, email, purpose, pending_password_hash,
                      expires_at, created_at
               FROM auth_action_tokens"""
        ).fetchall()

        assert stored == [
            (
                hashlib.sha256(raw_token.encode()).hexdigest(),
                "cook@example.com",
                "confirm",
                pending_hash,
                now + 24 * 60 * 60,
                now,
            )
        ]
        assert raw_token not in repr(stored)
        assert auth_data.consume_action_token(
            con, raw_token=raw_token, purpose="confirm", now=now + 1
        ) == {
            "email": "cook@example.com",
            "purpose": "confirm",
            "pending_password_hash": pending_hash,
        }
        with pytest.raises(auth_data.ActionTokenInvalid, match="^invalid token$"):
            auth_data.consume_action_token(
                con, raw_token=raw_token, purpose="confirm", now=now + 2
            )


@pytest.mark.parametrize("purpose", ["reset", "setup"])
def test_action_token_primitive_reset_and_setup_expire_after_60_minutes(
    monkeypatch, purpose
):
    auth_data = load_auth_data(monkeypatch)
    now = 1_900_000_000.0

    with sqlite3.connect(":memory:") as con:
        auth_data.migrate_auth_schema(con)
        raw_token = auth_data.create_action_token(
            con, email="cook@example.com", purpose=purpose, now=now
        )
        stored = con.execute(
            "SELECT expires_at FROM auth_action_tokens WHERE token_hash=?",
            (hashlib.sha256(raw_token.encode()).hexdigest(),),
        ).fetchone()

    assert stored == (now + 60 * 60,)


def test_action_token_primitive_is_purpose_bound_and_expires_at_the_boundary(
    monkeypatch,
):
    auth_data = load_auth_data(monkeypatch)
    now = 1_900_000_000.0

    with sqlite3.connect(":memory:") as con:
        auth_data.migrate_auth_schema(con)
        reset_token = auth_data.create_action_token(
            con, email="cook@example.com", purpose="reset", now=now
        )
        with pytest.raises(auth_data.ActionTokenInvalid, match="^invalid token$"):
            auth_data.consume_action_token(
                con, raw_token=reset_token, purpose="setup", now=now + 1
            )
        assert con.in_transaction is False
        assert auth_data.consume_action_token(
            con, raw_token=reset_token, purpose="reset", now=now + 1
        )["email"] == "cook@example.com"

        setup_token = auth_data.create_action_token(
            con, email="cook@example.com", purpose="setup", now=now
        )
        with pytest.raises(auth_data.ActionTokenExpired, match="^expired token$"):
            auth_data.consume_action_token(
                con, raw_token=setup_token, purpose="setup", now=now + 60 * 60
            )
        assert con.in_transaction is False
        assert con.execute("SELECT * FROM auth_action_tokens").fetchall() == []


def test_action_token_primitive_rejects_invalid_purpose_and_pending_plaintext(
    monkeypatch,
):
    auth_data = load_auth_data(monkeypatch)

    with sqlite3.connect(":memory:") as con:
        auth_data.migrate_auth_schema(con)
        with pytest.raises(ValueError, match="^invalid action token purpose$"):
            auth_data.create_action_token(
                con, email="cook@example.com", purpose="login", now=1.0
            )
        with pytest.raises(ValueError, match="^invalid password hash$"):
            auth_data.create_action_token(
                con,
                email="cook@example.com",
                purpose="confirm",
                now=1.0,
                pending_password_hash="raw password",
            )
        assert con.execute("SELECT * FROM auth_action_tokens").fetchall() == []


def test_action_token_primitive_consumption_is_transactionally_one_time(
    monkeypatch, tmp_path
):
    auth_data = load_auth_data(monkeypatch)
    database = tmp_path / "action-token.db"
    now = 1_900_000_000.0
    with sqlite3.connect(database) as con:
        auth_data.migrate_auth_schema(con)
        raw_token = auth_data.create_action_token(
            con, email="cook@example.com", purpose="reset", now=now
        )

    def consume():
        with sqlite3.connect(database) as con:
            try:
                result = auth_data.consume_action_token(
                    con, raw_token=raw_token, purpose="reset", now=now + 1
                )
                con.commit()
                return result
            except auth_data.ActionTokenInvalid:
                return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: consume(), range(2)))

    assert sorted(result is None for result in results) == [False, True]
    with sqlite3.connect(database) as con:
        assert con.execute("SELECT COUNT(*) FROM auth_action_tokens").fetchone() == (0,)


def test_action_token_transaction_rollback_after_consume_restores_token(
    monkeypatch, tmp_path
):
    auth_data = load_auth_data(monkeypatch)
    database = tmp_path / "action-token-rollback.db"
    now = 1_900_000_000.0
    with sqlite3.connect(database) as con:
        auth_data.migrate_auth_schema(con)
        raw_token = auth_data.create_action_token(
            con, email="cook@example.com", purpose="reset", now=now
        )

    with sqlite3.connect(database) as con:
        consumed = auth_data.consume_action_token(
            con, raw_token=raw_token, purpose="reset", now=now + 1
        )

        assert consumed["email"] == "cook@example.com"
        assert con.in_transaction is True
        assert con.execute("SELECT COUNT(*) FROM auth_action_tokens").fetchone() == (0,)
        with sqlite3.connect(database) as observer:
            assert observer.execute(
                "SELECT COUNT(*) FROM auth_action_tokens"
            ).fetchone() == (1,)

        con.rollback()

        assert con.in_transaction is False
        assert con.execute("SELECT COUNT(*) FROM auth_action_tokens").fetchone() == (1,)
        auth_data.consume_action_token(
            con, raw_token=raw_token, purpose="reset", now=now + 2
        )
        con.commit()

    with sqlite3.connect(database) as con:
        assert con.execute("SELECT COUNT(*) FROM auth_action_tokens").fetchone() == (0,)


def test_action_token_transaction_commits_with_protected_user_and_credential_mutation(
    monkeypatch, tmp_path
):
    auth_data = load_auth_data(monkeypatch)
    database = tmp_path / "action-token-confirm.db"
    now = 1_900_000_000.0
    pending_hash = auth_data.hash_password("pending pass")
    with sqlite3.connect(database) as con:
        con.execute(
            "CREATE TABLE pouzivatelia (id INTEGER PRIMARY KEY, email TEXT NOT NULL)"
        )
        auth_data.migrate_auth_schema(con)
        raw_token = auth_data.create_action_token(
            con,
            email="cook@example.com",
            purpose="confirm",
            now=now,
            pending_password_hash=pending_hash,
        )

    with sqlite3.connect(database) as con:
        consumed = auth_data.consume_action_token(
            con, raw_token=raw_token, purpose="confirm", now=now + 1
        )
        user_id = con.execute(
            "INSERT INTO pouzivatelia (email) VALUES (?)", (consumed["email"],)
        ).lastrowid
        con.execute(
            """INSERT INTO auth_credentials (user_id, password_hash, changed_at)
               VALUES (?, ?, ?)""",
            (user_id, consumed["pending_password_hash"], now + 1),
        )

        assert con.in_transaction is True
        with sqlite3.connect(database) as observer:
            assert observer.execute(
                "SELECT COUNT(*) FROM auth_action_tokens"
            ).fetchone() == (1,)
            assert observer.execute("SELECT COUNT(*) FROM pouzivatelia").fetchone() == (
                0,
            )
            assert observer.execute(
                "SELECT COUNT(*) FROM auth_credentials"
            ).fetchone() == (0,)
        con.commit()

    with sqlite3.connect(database) as con:
        assert con.execute("SELECT COUNT(*) FROM auth_action_tokens").fetchone() == (0,)
        assert con.execute(
            """SELECT p.email, c.password_hash, c.changed_at
               FROM pouzivatelia p
               JOIN auth_credentials c ON c.user_id=p.id"""
        ).fetchone() == ("cook@example.com", pending_hash, now + 1)


def test_action_token_transaction_uses_existing_transaction_without_damaging_it(
    monkeypatch,
):
    auth_data = load_auth_data(monkeypatch)
    now = 1_900_000_000.0
    with sqlite3.connect(":memory:") as con:
        auth_data.migrate_auth_schema(con)
        con.execute("CREATE TABLE audit_event (value TEXT NOT NULL)")
        raw_token = auth_data.create_action_token(
            con, email="cook@example.com", purpose="reset", now=now
        )
        con.execute("BEGIN IMMEDIATE")
        con.execute("INSERT INTO audit_event (value) VALUES ('keep pending')")

        with pytest.raises(auth_data.ActionTokenInvalid, match="^invalid token$"):
            auth_data.consume_action_token(
                con, raw_token=raw_token, purpose="setup", now=now + 1
            )

        assert con.in_transaction is True
        assert con.execute("SELECT value FROM audit_event").fetchall() == [
            ("keep pending",)
        ]
        assert con.execute("SELECT COUNT(*) FROM auth_action_tokens").fetchone() == (1,)
        con.rollback()

        assert con.in_transaction is False
        assert con.execute("SELECT * FROM audit_event").fetchall() == []


def test_action_token_transaction_expiry_keeps_existing_transaction_safe(
    monkeypatch,
):
    auth_data = load_auth_data(monkeypatch)
    now = 1_900_000_000.0
    with sqlite3.connect(":memory:") as con:
        auth_data.migrate_auth_schema(con)
        con.execute("CREATE TABLE audit_event (value TEXT NOT NULL)")
        raw_token = auth_data.create_action_token(
            con, email="cook@example.com", purpose="setup", now=now
        )
        con.execute("BEGIN IMMEDIATE")
        con.execute("INSERT INTO audit_event (value) VALUES ('keep pending')")

        with pytest.raises(auth_data.ActionTokenExpired, match="^expired token$"):
            auth_data.consume_action_token(
                con, raw_token=raw_token, purpose="setup", now=now + 60 * 60
            )

        assert con.in_transaction is True
        assert con.execute("SELECT value FROM audit_event").fetchall() == [
            ("keep pending",)
        ]
        assert con.execute("SELECT COUNT(*) FROM auth_action_tokens").fetchone() == (0,)
        con.rollback()

        assert con.in_transaction is False
        assert con.execute("SELECT * FROM audit_event").fetchall() == []
        assert con.execute("SELECT COUNT(*) FROM auth_action_tokens").fetchone() == (1,)


@pytest.mark.parametrize("purpose", ["register", "login"])
def test_auth_v3_webauthn_challenge_purpose_accepts_every_permitted_value(
    monkeypatch, tmp_path, purpose
):
    server, _ = load_auth_server(monkeypatch, tmp_path)

    with sqlite3.connect(":memory:") as con:
        server.migrate_auth_schema(con)
        con.execute(
            """INSERT INTO auth_webauthn_challenges
               (challenge_hash, user_id, purpose, expires_at, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (f"{purpose}-hash", 7, purpose, 2.0, 1.0),
        )
        stored = con.execute(
            "SELECT purpose FROM auth_webauthn_challenges WHERE challenge_hash=?",
            (f"{purpose}-hash",),
        ).fetchone()

    assert stored == (purpose,)


def test_auth_v3_webauthn_challenge_purpose_rejects_an_invalid_value(
    monkeypatch, tmp_path
):
    server, _ = load_auth_server(monkeypatch, tmp_path)

    with sqlite3.connect(":memory:") as con:
        server.migrate_auth_schema(con)
        with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
            con.execute(
                """INSERT INTO auth_webauthn_challenges
                   (challenge_hash, user_id, purpose, expires_at, created_at)
                   VALUES (?, ?, ?, ?, ?)""",
                ("invalid-challenge-hash", 7, "reset", 2.0, 1.0),
            )


def test_post_redeems_once_into_a_hashed_90_day_host_only_session(monkeypatch, tmp_path):
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
    assert "Max-Age=7776000" in cookie
    assert "Domain=" not in cookie
    raw_session = client.cookies.get(server.COOKIE)
    with sqlite3.connect(database) as con:
        assert con.execute("SELECT COUNT(*) FROM magic_tokens_v2").fetchone()[0] == 0
        sessions = con.execute(
            "SELECT token_hash, expires_at FROM sessions_v2"
        ).fetchall()
    assert sessions == [
        (hashlib.sha256(raw_session.encode()).hexdigest(), 1_807_776_000.0)
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

    now[0] += 90 * 24 * 60 * 60 + 1
    response = client.get("/api/me")

    assert response.json() == {"prihlaseny": False}
    with sqlite3.connect(database) as con:
        assert con.execute("SELECT COUNT(*) FROM sessions_v2").fetchone()[0] == 0


def test_logout_deletes_only_the_current_hashed_session_and_cookie(monkeypatch, tmp_path):
    server, database = load_auth_server(monkeypatch, tmp_path)
    monkeypatch.setenv("RESEND_API_KEY", "test-only-key")
    now = [1_800_000_000.0]
    monkeypatch.setattr(server, "AUTH_CLOCK", lambda: now[0])
    first_client = TestClient(server.app, base_url="https://testserver")
    first_token = issue_link(server, monkeypatch)
    assert first_client.post(
        "/api/auth/verify", json={"token": first_token}
    ).status_code == 200

    now[0] += 60
    second_client = TestClient(server.app, base_url="https://testserver")
    second_token = issue_link(server, monkeypatch)
    assert second_client.post(
        "/api/auth/verify", json={"token": second_token}
    ).status_code == 200

    response = first_client.post("/api/auth/logout")

    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert "uvarsi_session=\"\"" in response.headers["set-cookie"]
    assert first_client.get("/api/me").json() == {"prihlaseny": False}
    assert second_client.get("/api/me").json()["prihlaseny"] is True
    with sqlite3.connect(database) as con:
        assert con.execute("SELECT COUNT(*) FROM sessions_v2").fetchone()[0] == 1


def test_two_magic_logins_create_two_valid_sessions(monkeypatch, tmp_path):
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

    assert first_client.get("/api/me").json()["prihlaseny"] is True
    assert second_client.get("/api/me").json()["prihlaseny"] is True
    with sqlite3.connect(database) as con:
        sessions = con.execute("SELECT token_hash FROM sessions_v2").fetchall()
    assert {row[0] for row in sessions} == {
        hashlib.sha256(first_session.encode()).hexdigest(),
        hashlib.sha256(second_client.cookies.get(server.COOKIE).encode()).hexdigest(),
    }


def test_active_session_slides_to_90_days_at_most_once_per_24_hours(
    monkeypatch, tmp_path
):
    server, _ = load_auth_server(monkeypatch, tmp_path)
    auth_data = importlib.import_module("auth_data")
    now = 1_800_000_000.0
    with server.db() as con:
        user_id = con.execute(
            "INSERT INTO pouzivatelia (email) VALUES ('sliding@example.com')"
        ).lastrowid
        raw_session = auth_data.create_session(
            con, user_id=user_id, now=now, device_name="Desktop"
        )

        assert auth_data.user_for_session(
            con,
            raw_session=raw_session,
            now=now + auth_data.SESSION_TOUCH_SECONDS - 1,
        )["id"] == user_id
        untouched = tuple(
            con.execute(
                "SELECT last_seen_at, expires_at FROM sessions_v2"
            ).fetchone()
        )
        assert untouched == (now, now + auth_data.SESSION_TTL_SECONDS)

        first_touch = now + auth_data.SESSION_TOUCH_SECONDS
        assert auth_data.user_for_session(
            con, raw_session=raw_session, now=first_touch
        )["id"] == user_id
        touched = tuple(
            con.execute(
                "SELECT last_seen_at, expires_at FROM sessions_v2"
            ).fetchone()
        )
        assert touched == (
            first_touch,
            first_touch + auth_data.SESSION_TTL_SECONDS,
        )

        assert auth_data.user_for_session(
            con,
            raw_session=raw_session,
            now=first_touch + auth_data.SESSION_TOUCH_SECONDS - 1,
        )["id"] == user_id
        assert tuple(
            con.execute(
                "SELECT last_seen_at, expires_at FROM sessions_v2"
            ).fetchone()
        ) == touched


def test_legacy_hashed_session_with_null_metadata_remains_valid_and_is_touched(
    monkeypatch, tmp_path
):
    server, _ = load_auth_server(monkeypatch, tmp_path)
    auth_data = importlib.import_module("auth_data")
    raw_session = "existing-hashed-session"
    now = 1_800_000_000.0
    with server.db() as con:
        user_id = con.execute(
            "INSERT INTO pouzivatelia (email) VALUES ('legacy-session@example.com')"
        ).lastrowid
        con.execute(
            """INSERT INTO sessions_v2
               (token_hash, user_id, expires_at, created_at)
               VALUES (?, ?, ?, ?)""",
            (
                hashlib.sha256(raw_session.encode()).hexdigest(),
                user_id,
                now + 60,
                now - 1_000,
            ),
        )
        con.commit()

        assert auth_data.user_for_session(
            con, raw_session=raw_session, now=now
        )["id"] == user_id
        assert tuple(
            con.execute(
                "SELECT last_seen_at, expires_at FROM sessions_v2"
            ).fetchone()
        ) == (now, now + auth_data.SESSION_TTL_SECONDS)


def test_revoke_other_sessions_preserves_only_the_current_raw_session(
    monkeypatch, tmp_path
):
    server, _ = load_auth_server(monkeypatch, tmp_path)
    auth_data = importlib.import_module("auth_data")
    now = 1_800_000_000.0
    with server.db() as con:
        user_id = con.execute(
            "INSERT INTO pouzivatelia (email) VALUES ('change@example.com')"
        ).lastrowid
        current = auth_data.create_session(
            con, user_id=user_id, now=now, device_name="Current"
        )
        other = auth_data.create_session(
            con, user_id=user_id, now=now, device_name="Other"
        )

        auth_data.revoke_other_sessions(
            con, user_id=user_id, current_token=current
        )

        assert auth_data.user_for_session(
            con, raw_session=current, now=now, touch=False
        )["id"] == user_id
        assert auth_data.user_for_session(
            con, raw_session=other, now=now, touch=False
        ) is None
        assert [
            tuple(row)
            for row in con.execute(
                """SELECT device_name, revoked_at IS NOT NULL
                   FROM sessions_v2 ORDER BY device_name"""
            )
        ] == [("Current", 0), ("Other", 1)]


def test_password_reset_without_current_session_revokes_every_user_session(
    monkeypatch, tmp_path
):
    server, _ = load_auth_server(monkeypatch, tmp_path)
    auth_data = importlib.import_module("auth_data")
    now = 1_800_000_000.0
    with server.db() as con:
        user_id = con.execute(
            "INSERT INTO pouzivatelia (email) VALUES ('reset@example.com')"
        ).lastrowid
        sessions = [
            auth_data.create_session(
                con, user_id=user_id, now=now, device_name=device
            )
            for device in ("Phone", "Laptop")
        ]

        auth_data.revoke_other_sessions(con, user_id=user_id, current_token=None)

        assert all(
            auth_data.user_for_session(
                con, raw_session=raw_session, now=now, touch=False
            )
            is None
            for raw_session in sessions
        )
        assert con.execute(
            """SELECT COUNT(*) FROM sessions_v2
               WHERE revoked_at IS NOT NULL"""
        ).fetchone()[0] == 2


def test_session_list_and_revoke_use_hash_identifiers_that_cannot_authenticate(
    monkeypatch, tmp_path
):
    server, _ = load_auth_server(monkeypatch, tmp_path)
    auth_data = importlib.import_module("auth_data")
    now = 1_800_000_000.0
    with server.db() as con:
        user_id = con.execute(
            "INSERT INTO pouzivatelia (email) VALUES ('sessions@example.com')"
        ).lastrowid
        other_user_id = con.execute(
            "INSERT INTO pouzivatelia (email) VALUES ('other@example.com')"
        ).lastrowid
        raw_session = auth_data.create_session(
            con, user_id=user_id, now=now, device_name="Firefox on Windows"
        )

        listed = auth_data.list_sessions(
            con, user_id=user_id, current_token=raw_session, now=now
        )

        assert listed == [
            {
                "session_hash": hashlib.sha256(raw_session.encode()).hexdigest(),
                "device_name": "Firefox on Windows",
                "created_at": now,
                "last_seen_at": now,
                "expires_at": now + auth_data.SESSION_TTL_SECONDS,
                "current": True,
            }
        ]
        session_hash = listed[0]["session_hash"]
        assert raw_session not in repr(listed)
        assert auth_data.user_for_session(
            con, raw_session=session_hash, now=now, touch=False
        ) is None
        assert auth_data.revoke_session(
            con, user_id=other_user_id, session_hash=session_hash
        ) is False
        assert auth_data.user_for_session(
            con, raw_session=raw_session, now=now, touch=False
        )["id"] == user_id
        assert auth_data.revoke_session(
            con, user_id=user_id, session_hash=session_hash
        ) is True
        assert auth_data.user_for_session(
            con, raw_session=raw_session, now=now, touch=False
        ) is None
        assert con.execute(
            """SELECT revoked_at IS NOT NULL FROM sessions_v2
               WHERE token_hash=?""",
            (session_hash,),
        ).fetchone()[0] == 1


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
