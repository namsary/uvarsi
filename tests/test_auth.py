import asyncio
import hashlib
import importlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import types
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from argon2 import PasswordHasher, Type
import httpx
import pytest
from fastapi.testclient import TestClient as FastAPITestClient


ROOT = Path(__file__).resolve().parents[1]
SUCCESS_MESSAGE = (
    "Poskytovateľ prijal žiadosť o prihlasovací e-mail. "
    "Odkaz bude platný 60 minút."
)
PROVIDER_FAILURE_MESSAGE = (
    "Prihlasovací e-mail sa teraz nepodarilo odovzdať poskytovateľovi. "
    "Skús to znova o chvíľu."
)
NODE = os.environ.get("UVARSI_NODE") or shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="node runtime is not available")
AUTH_ORIGIN = {"Origin": "https://uvar.si"}


def TestClient(app, *args, **kwargs):
    kwargs.setdefault("headers", AUTH_ORIGIN)
    return FastAPITestClient(app, *args, **kwargs)


def origin_client(server, **kwargs):
    headers = dict(kwargs.pop("headers", {}))
    headers.setdefault("Origin", "https://uvar.si")
    return TestClient(server.app, headers=headers, **kwargs)


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
    const rendered=await page(scenario);
    states.push({stored:[...shared.entries()],calls:[...calls],replaced,
      status:rendered.status});
  }
  process.stdout.write(JSON.stringify(states));
})().catch(error=>{console.error(error);process.exit(1)});
""",
        encoding="utf-8",
    )
    return subprocess.run(
        [NODE, str(script)], capture_output=True, text=True, encoding="utf-8"
    )


def run_account_action_page(
    tmp_path,
    html,
    *,
    page_name,
    fragment,
    button_id,
    values=None,
    status=200,
    body=None,
    network_error=False,
):
    script = tmp_path / f"{page_name}-account-action.js"
    script.write_text(
        "const pageScript=" + json.dumps(confirmation_script(html)) + ";\n"
        + "const spec="
        + json.dumps(
            {
                "fragment": fragment,
                "button": button_id,
                "values": values or {},
                "status": status,
                "body": body or {},
                "networkError": network_error,
            }
        )
        + ";\n"
        + r"""
const vm=require('vm');
const nodes=new Map();
const node=id=>nodes.get(id)||nodes.set(id,{
  disabled:false,textContent:'',value:'',type:'password',style:{},
  classList:{add(){},remove(){}},setAttribute(){},onclick:null
}).get(id);
for(const [id,value] of Object.entries(spec.values))node(id).value=value;
const calls=[];const storage=[];const historyValues=[];const replacements=[];
const storageApi={
  getItem(key){storage.push(['get',key]);return null},
  setItem(key,value){storage.push(['set',key,String(value)])},
  removeItem(key){storage.push(['remove',key])}
};
const context={
  URLSearchParams,
  location:{hash:spec.fragment,pathname:'/'+String(spec.button),replace(value){replacements.push(value)}},
  history:{replaceState(_state,_title,value){historyValues.push(value);context.location.hash=''}},
  localStorage:storageApi,sessionStorage:storageApi,
  document:{getElementById:node},
  fetch:async(url,options)=>{
    calls.push({url,body:JSON.parse(options.body)});
    if(spec.networkError)throw new Error('offline');
    return {ok:spec.status>=200&&spec.status<300,status:spec.status,
      json:async()=>spec.body};
  }
};
vm.createContext(context);vm.runInContext(pageScript,context);
(async()=>{
  const button=node(spec.button);
  const first=button.onclick();
  const second=button.onclick();
  await Promise.allSettled([first,second]);
  process.stdout.write(JSON.stringify({calls,storage,historyValues,replacements,
    status:node('status').textContent,disabled:button.disabled,text:button.textContent}));
})().catch(error=>{console.error(error);process.exit(1)});
""",
        encoding="utf-8",
    )
    return subprocess.run(
        [NODE, str(script)], capture_output=True, text=True, encoding="utf-8"
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
    monkeypatch.setenv("UVARSI_AUTH_V3", "0")
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


def ensure_legacy_account(server, email):
    try:
        normalized = server.normalize_email(email)
    except ValueError:
        return
    with closing(server.db()) as con:
        con.execute(
            "INSERT OR IGNORE INTO pouzivatelia (email) VALUES (?)", (normalized,)
        )
        con.commit()


def request_link(server, email="Cook@example.com"):
    ensure_legacy_account(server, email)
    return origin_client(server).post("/api/auth/request", json={"email": email})


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
    ensure_legacy_account(server, "cook@example.com")
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
    ensure_legacy_account(server, "cook@example.com")
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
        async with httpx.AsyncClient(
            transport=transport, base_url="https://testserver", headers=AUTH_ORIGIN
        ) as client:
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
    ensure_legacy_account(server, "cook@example.com")
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
        async with httpx.AsyncClient(
            transport=transport, base_url="https://testserver", headers=AUTH_ORIGIN
        ) as client:
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
    ensure_legacy_account(server, "cook@example.com")
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
        async with httpx.AsyncClient(
            transport=transport, base_url="https://testserver", headers=AUTH_ORIGIN
        ) as client:
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
    ensure_legacy_account(server, "cook@example.com")
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
    ensure_legacy_account(server, "cooldown@example.com")
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
    for index in range(5):
        ensure_legacy_account(server, f"cook{index}@example.com")
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
    assert "Pokračovať k heslu" in response.text
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


def test_migration_page_keeps_bearer_only_in_page_memory_and_scrubs_fragment(
    monkeypatch, tmp_path
):
    server, _ = load_auth_server(monkeypatch, tmp_path)
    html = TestClient(server.app).get("/prihlasenie").text
    script = confirmation_script(html)

    assert "sessionStorage" not in script
    assert "localStorage" not in script
    assert "location.search" not in script
    assert "history.pushState" not in script
    assert script.index("location.hash") < script.index("history.replaceState")
    assert "fetch('/api/auth/verify'" in script


@needs_node
def test_fresh_migration_fragment_confirms_by_post_without_persisting_bearer(
    monkeypatch, tmp_path
):
    server, _ = load_auth_server(monkeypatch, tmp_path)
    html = TestClient(server.app).get("/prihlasenie").text

    result = run_confirmation_flow(
        tmp_path,
        html,
        [
            {
                "hash": "#token=fresh-secret",
                "confirm": True,
                "status": 200,
                "body": {"redirect": "/heslo"},
            },
        ],
    )

    assert result.returncode == 0, result.stderr
    state = json.loads(result.stdout)[0]
    assert state["stored"] == []
    assert state["calls"] == [
        {"url": "/api/auth/verify", "body": {"token": "fresh-secret"}}
    ]
    assert state["replaced"] == "/heslo"


@needs_node
def test_reloaded_migration_page_has_no_bearer_and_requests_a_fresh_link(
    monkeypatch, tmp_path
):
    server, _ = load_auth_server(monkeypatch, tmp_path)
    html = TestClient(server.app).get("/prihlasenie").text

    result = run_confirmation_flow(
        tmp_path,
        html,
        [
            {"hash": "#token=page-closure-secret", "status": 200},
            {"hash": "", "status": 200},
        ],
    )

    assert result.returncode == 0, result.stderr
    states = json.loads(result.stdout)
    assert states[0]["stored"] == states[1]["stored"] == []
    assert states[0]["replaced"] == states[1]["replaced"] == "/prihlasenie"
    assert states[1]["calls"] == []
    assert "odkaz už nie je v tejto karte" in states[1]["status"].lower()
    assert "požiadaj o nový" in states[1]["status"].lower()


def test_account_confirmation_and_password_pages_have_safe_accessible_controls(
    monkeypatch, tmp_path
):
    server, _ = load_auth_server(monkeypatch, tmp_path)
    client = TestClient(server.app)
    confirmation = client.get("/potvrdenie")
    password = client.get("/heslo")

    for response in (confirmation, password):
        assert response.status_code == 200
        assert response.headers["cache-control"] == "private, no-store"
        assert response.headers["x-robots-tag"] == "noindex, nofollow, noarchive"
        assert "location.hash" in response.text
        assert "history.replaceState" in response.text
        assert "location.search" not in response.text
        assert "localStorage" not in response.text
        assert "Pracujem…" in response.text
        assert 'role="status"' in response.text
    assert "Potvrdiť účet" in confirmation.text
    assert "Účet vznikne až" in confirmation.text
    assert 'autocomplete="new-password"' in password.text
    assert "Zobraziť heslo" in password.text
    assert "10 až 128 znakov" in password.text
    assert "/api/auth/password/set" in password.text
    assert "/api/auth/password/reset" in password.text


@needs_node
def test_account_action_pages_scrub_fragment_and_block_double_submission(
    monkeypatch, tmp_path
):
    server, _ = load_auth_server(monkeypatch, tmp_path)
    client = TestClient(server.app)
    confirmation = run_account_action_page(
        tmp_path,
        client.get("/potvrdenie").text,
        page_name="confirmation",
        fragment="#token=confirm-secret",
        button_id="confirm",
        status=200,
        body={"redirect": "/app"},
    )
    reset = run_account_action_page(
        tmp_path,
        client.get("/heslo").text,
        page_name="reset",
        fragment="#token=reset-secret&purpose=reset",
        button_id="submit",
        values={"password": "nové bezpečné heslo"},
        status=200,
        body={"redirect": "/app"},
    )
    setup = run_account_action_page(
        tmp_path,
        client.get("/heslo").text,
        page_name="setup",
        fragment="",
        button_id="submit",
        values={"password": "migračné bezpečné heslo"},
        status=200,
        body={"ok": True},
    )

    for result in (confirmation, reset, setup):
        assert result.returncode == 0, result.stdout + result.stderr
    confirmed = json.loads(confirmation.stdout)
    reset_state = json.loads(reset.stdout)
    setup_state = json.loads(setup.stdout)
    assert confirmed["calls"] == [
        {"url": "/api/auth/confirm", "body": {"token": "confirm-secret"}}
    ]
    assert reset_state["calls"] == [
        {
            "url": "/api/auth/password/reset",
            "body": {
                "token": "reset-secret",
                "purpose": "reset",
                "password": "nové bezpečné heslo",
            },
        }
    ]
    assert setup_state["calls"] == [
        {
            "url": "/api/auth/password/set",
            "body": {"password": "migračné bezpečné heslo"},
        }
    ]
    for state, secret in (
        (confirmed, "confirm-secret"),
        (reset_state, "reset-secret"),
        (setup_state, "migračné bezpečné heslo"),
    ):
        assert state["storage"] == []
        assert secret not in json.dumps(state["historyValues"])
        assert secret not in json.dumps(state["replacements"])


@needs_node
def test_password_page_reports_network_failure_in_slovak_and_allows_retry(
    monkeypatch, tmp_path
):
    server, _ = load_auth_server(monkeypatch, tmp_path)
    result = run_account_action_page(
        tmp_path,
        TestClient(server.app).get("/heslo").text,
        page_name="reset-network-error",
        fragment="#token=retry-secret&purpose=reset",
        button_id="submit",
        values={"password": "nové bezpečné heslo"},
        network_error=True,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    state = json.loads(result.stdout)
    assert len(state["calls"]) == 1
    assert "pripoj" in state["status"].lower()
    assert state["disabled"] is False
    assert state["text"] == "Uložiť heslo"


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
                "auth_password_reset_outbox",
                "auth_legacy_setup_claims",
                "auth_setup_sessions",
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
            ("credential_changed_at", "REAL", 0, None, 0),
            ("expires_at", "REAL", 1, None, 0),
            ("created_at", "REAL", 1, None, 0),
        ],
        "auth_password_reset_outbox": [
            ("id", "INTEGER", 0, None, 1),
            ("email", "TEXT", 1, None, 0),
            ("credential_changed_at", "REAL", 0, None, 0),
            ("requested_at", "REAL", 1, None, 0),
            ("state", "TEXT", 1, None, 0),
            ("attempts", "INTEGER", 1, "0", 0),
            ("token_hash", "TEXT", 0, None, 0),
            ("token_seed", "TEXT", 0, None, 0),
            ("idempotency_key", "TEXT", 0, None, 0),
            ("next_attempt_at", "REAL", 0, None, 0),
            ("lease_owner", "TEXT", 0, None, 0),
            ("lease_expires_at", "REAL", 0, None, 0),
            ("created_at", "REAL", 1, None, 0),
            ("updated_at", "REAL", 1, None, 0),
            ("delivered_at", "REAL", 0, None, 0),
        ],
        "auth_legacy_setup_claims": [
            ("user_id", "INTEGER", 0, None, 1),
            ("claimed_at", "REAL", 1, None, 0),
        ],
        "auth_setup_sessions": [
            ("token_hash", "TEXT", 0, None, 1),
            ("user_id", "INTEGER", 1, None, 0),
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
        con.commit()

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


def test_post_redeems_once_into_a_hashed_one_hour_host_only_setup_capability(
    monkeypatch, tmp_path
):
    server, database = load_auth_server(monkeypatch, tmp_path)
    monkeypatch.setenv("RESEND_API_KEY", "test-only-key")
    monkeypatch.setattr(server, "AUTH_CLOCK", lambda: 1_800_000_000.0)
    token = issue_link(server, monkeypatch)
    client = TestClient(server.app, base_url="https://testserver")

    response = client.post("/api/auth/verify", json={"token": token})

    assert response.status_code == 200
    assert response.json() == {"ok": True, "redirect": "/heslo"}
    assert token not in response.text
    cookie = response.headers["set-cookie"]
    assert "uvarsi_setup=" in cookie
    assert "HttpOnly" in cookie
    assert "Secure" in cookie
    assert "SameSite=lax" in cookie
    assert "Max-Age=3600" in cookie
    assert "Domain=" not in cookie
    raw_session = client.cookies.get(server.SETUP_COOKIE)
    with sqlite3.connect(database) as con:
        assert con.execute("SELECT COUNT(*) FROM magic_tokens_v2").fetchone()[0] == 0
        sessions = con.execute(
            "SELECT token_hash, expires_at FROM auth_setup_sessions"
        ).fetchall()
    assert sessions == [
        (hashlib.sha256(raw_session.encode()).hexdigest(), 1_800_003_600.0)
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


def test_concurrent_redemption_creates_exactly_one_setup_capability(
    monkeypatch, tmp_path
):
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
        assert con.execute(
            "SELECT COUNT(*) FROM auth_setup_sessions"
        ).fetchone()[0] == 1
        assert con.execute("SELECT COUNT(*) FROM sessions_v2").fetchone()[0] == 0


def test_expired_session_is_rejected_and_deleted_server_side(monkeypatch, tmp_path):
    server, database = load_auth_server(monkeypatch, tmp_path)
    auth_data = sys.modules["auth_data"]
    now = [1_800_000_000.0]
    monkeypatch.setattr(server, "AUTH_CLOCK", lambda: now[0])
    with closing(server.db()) as con:
        user_id = con.execute(
            "INSERT INTO pouzivatelia (email) VALUES ('session-expiry@example.com')"
        ).lastrowid
        raw_session = auth_data.create_session(
            con, user_id=user_id, now=now[0], device_name="Expiry test"
        )
        con.commit()
    client = TestClient(server.app, base_url="https://testserver")
    client.cookies.set(server.COOKIE, raw_session)

    now[0] += 90 * 24 * 60 * 60 + 1
    response = client.get("/api/me")

    assert response.json() == {"prihlaseny": False}
    with sqlite3.connect(database) as con:
        assert con.execute("SELECT COUNT(*) FROM sessions_v2").fetchone()[0] == 0


def test_logout_deletes_only_the_current_hashed_session_and_cookie(monkeypatch, tmp_path):
    server, database = load_auth_server(monkeypatch, tmp_path)
    auth_data = sys.modules["auth_data"]
    password = "multi device password"
    seed_password_account(server, auth_data, "cook@example.com", password)
    now = [1_800_000_000.0]
    monkeypatch.setattr(server, "AUTH_CLOCK", lambda: now[0])
    first_client = TestClient(server.app, base_url="https://testserver")
    assert first_client.post(
        "/api/auth/login", json={"email": "cook@example.com", "password": password}
    ).status_code == 200

    now[0] += 60
    second_client = TestClient(server.app, base_url="https://testserver")
    assert second_client.post(
        "/api/auth/login", json={"email": "cook@example.com", "password": password}
    ).status_code == 200

    response = first_client.post("/api/auth/logout")

    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert "uvarsi_session=\"\"" in response.headers["set-cookie"]
    assert first_client.get("/api/me").json() == {
        "prihlaseny": False,
        "auth_v3": True,
    }
    assert second_client.get("/api/me").json()["prihlaseny"] is True
    with sqlite3.connect(database) as con:
        assert con.execute("SELECT COUNT(*) FROM sessions_v2").fetchone()[0] == 1


def test_two_password_logins_create_two_valid_sessions(monkeypatch, tmp_path):
    server, database = load_auth_server(monkeypatch, tmp_path)
    auth_data = sys.modules["auth_data"]
    password = "multi device password"
    seed_password_account(server, auth_data, "cook@example.com", password)
    now = [1_800_000_000.0]
    monkeypatch.setattr(server, "AUTH_CLOCK", lambda: now[0])
    first_client = TestClient(server.app, base_url="https://testserver")
    assert first_client.post(
        "/api/auth/login", json={"email": "cook@example.com", "password": password}
    ).status_code == 200
    first_session = first_client.cookies.get(server.COOKIE)

    now[0] += 60
    second_client = TestClient(server.app, base_url="https://testserver")
    assert second_client.post(
        "/api/auth/login", json={"email": "cook@example.com", "password": password}
    ).status_code == 200

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


def test_session_cookie_is_not_renewed_before_the_24_hour_touch(
    monkeypatch, tmp_path
):
    server, _ = load_auth_server(monkeypatch, tmp_path)
    auth_data = sys.modules["auth_data"]
    now = [1_800_000_000.0]
    monkeypatch.setattr(server, "AUTH_CLOCK", lambda: now[0])
    with closing(server.db()) as con:
        user_id = con.execute(
            "INSERT INTO pouzivatelia (email) VALUES ('session-expiry@example.com')"
        ).lastrowid
        raw_session = auth_data.create_session(
            con, user_id=user_id, now=now[0], device_name="Expiry test"
        )
        con.commit()
    client = TestClient(server.app, base_url="https://testserver")
    client.cookies.set(server.COOKIE, raw_session)

    now[0] += 24 * 60 * 60 - 1
    response = client.get("/api/me")

    assert response.status_code == 200
    assert response.json()["prihlaseny"] is True
    assert response.headers.get("set-cookie") is None


def test_session_touch_renews_the_secure_90_day_cookie_only_on_that_response(
    monkeypatch, tmp_path
):
    server, _ = load_auth_server(monkeypatch, tmp_path)
    auth_data = sys.modules["auth_data"]
    now = [1_800_000_000.0]
    monkeypatch.setattr(server, "AUTH_CLOCK", lambda: now[0])
    with closing(server.db()) as con:
        user_id = con.execute(
            "INSERT INTO pouzivatelia (email) VALUES ('touch-cookie@example.com')"
        ).lastrowid
        raw_session = auth_data.create_session(
            con, user_id=user_id, now=now[0], device_name="Touch test"
        )
        con.commit()
    client = TestClient(server.app, base_url="https://testserver")
    client.cookies.set(server.COOKIE, raw_session)

    now[0] += 24 * 60 * 60
    touched = client.get("/api/me")
    now[0] += 1
    immediate_repeat = client.get("/api/me")

    cookie = touched.headers["set-cookie"]
    assert f"uvarsi_session={raw_session}" in cookie
    assert "Max-Age=7776000" in cookie
    assert "HttpOnly" in cookie
    assert "Secure" in cookie
    assert "SameSite=lax" in cookie
    assert "Domain=" not in cookie
    assert immediate_repeat.headers.get("set-cookie") is None


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


def test_create_session_leaves_a_caller_transaction_open_for_rollback(
    monkeypatch, tmp_path
):
    server, database = load_auth_server(monkeypatch, tmp_path)
    auth_data = importlib.import_module("auth_data")
    with closing(server.db()) as con:
        user_id = con.execute(
            "INSERT INTO pouzivatelia (email) VALUES ('create-rollback@example.com')"
        ).lastrowid
        auth_data.create_session(
            con,
            user_id=user_id,
            now=1_800_000_000.0,
            device_name="Rollback",
        )

        assert con.in_transaction is True
        con.rollback()

    with sqlite3.connect(database) as con:
        assert con.execute(
            "SELECT COUNT(*) FROM pouzivatelia WHERE email='create-rollback@example.com'"
        ).fetchone() == (0,)
        assert con.execute("SELECT COUNT(*) FROM sessions_v2").fetchone() == (0,)


def test_create_session_leaves_a_caller_transaction_open_for_commit(
    monkeypatch, tmp_path
):
    server, database = load_auth_server(monkeypatch, tmp_path)
    auth_data = importlib.import_module("auth_data")
    with closing(server.db()) as con:
        user_id = con.execute(
            "INSERT INTO pouzivatelia (email) VALUES ('create-commit@example.com')"
        ).lastrowid
        raw_session = auth_data.create_session(
            con,
            user_id=user_id,
            now=1_800_000_000.0,
            device_name="Commit",
        )

        assert con.in_transaction is True
        con.commit()

    with sqlite3.connect(database) as con:
        assert con.execute(
            "SELECT email FROM pouzivatelia WHERE id=?", (user_id,)
        ).fetchone() == ("create-commit@example.com",)
        assert con.execute(
            "SELECT token_hash FROM sessions_v2 WHERE user_id=?", (user_id,)
        ).fetchone() == (hashlib.sha256(raw_session.encode()).hexdigest(),)


def test_create_session_persists_when_it_owns_the_transaction(monkeypatch, tmp_path):
    server, database = load_auth_server(monkeypatch, tmp_path)
    auth_data = importlib.import_module("auth_data")
    with closing(server.db()) as con:
        user_id = con.execute(
            "INSERT INTO pouzivatelia (email) VALUES ('create-owned@example.com')"
        ).lastrowid
        con.commit()
        raw_session = auth_data.create_session(
            con,
            user_id=user_id,
            now=1_800_000_000.0,
            device_name="Owned",
        )
        assert con.in_transaction is False

    with sqlite3.connect(database) as con:
        assert con.execute(
            "SELECT token_hash FROM sessions_v2 WHERE user_id=?", (user_id,)
        ).fetchone() == (hashlib.sha256(raw_session.encode()).hexdigest(),)


def _seed_session_revoke_test(server, auth_data, email):
    with closing(server.db()) as con:
        user_id = con.execute(
            "INSERT INTO pouzivatelia (email) VALUES (?)", (email,)
        ).lastrowid
        con.commit()
        current = auth_data.create_session(
            con,
            user_id=user_id,
            now=1_800_000_000.0,
            device_name="Current",
        )
        target = auth_data.create_session(
            con,
            user_id=user_id,
            now=1_800_000_000.0,
            device_name="Target",
        )
    return user_id, current, hashlib.sha256(target.encode()).hexdigest()


@pytest.mark.parametrize("operation", ["revoke_session", "revoke_other_sessions"])
def test_session_revokers_leave_a_caller_transaction_open_for_rollback(
    monkeypatch, tmp_path, operation
):
    server, database = load_auth_server(monkeypatch, tmp_path)
    auth_data = importlib.import_module("auth_data")
    original_email = f"{operation}-rollback@example.com"
    user_id, current, target_hash = _seed_session_revoke_test(
        server, auth_data, original_email
    )
    with closing(server.db()) as con:
        con.execute(
            "UPDATE pouzivatelia SET email='pending@example.com' WHERE id=?",
            (user_id,),
        )
        if operation == "revoke_session":
            assert auth_data.revoke_session(
                con, user_id=user_id, session_hash=target_hash
            ) is True
        else:
            auth_data.revoke_other_sessions(
                con, user_id=user_id, current_token=current
            )

        assert con.in_transaction is True
        con.rollback()

    with sqlite3.connect(database) as con:
        assert con.execute(
            "SELECT email FROM pouzivatelia WHERE id=?", (user_id,)
        ).fetchone() == (original_email,)
        assert con.execute(
            "SELECT COUNT(*) FROM sessions_v2 WHERE user_id=? AND revoked_at IS NOT NULL",
            (user_id,),
        ).fetchone() == (0,)


@pytest.mark.parametrize("operation", ["revoke_session", "revoke_other_sessions"])
def test_session_revokers_leave_a_caller_transaction_open_for_commit(
    monkeypatch, tmp_path, operation
):
    server, database = load_auth_server(monkeypatch, tmp_path)
    auth_data = importlib.import_module("auth_data")
    user_id, current, target_hash = _seed_session_revoke_test(
        server, auth_data, f"{operation}-commit@example.com"
    )
    with closing(server.db()) as con:
        con.execute(
            "UPDATE pouzivatelia SET email='committed@example.com' WHERE id=?",
            (user_id,),
        )
        if operation == "revoke_session":
            assert auth_data.revoke_session(
                con, user_id=user_id, session_hash=target_hash
            ) is True
        else:
            auth_data.revoke_other_sessions(
                con, user_id=user_id, current_token=current
            )

        assert con.in_transaction is True
        con.commit()

    with sqlite3.connect(database) as con:
        assert con.execute(
            "SELECT email FROM pouzivatelia WHERE id=?", (user_id,)
        ).fetchone() == ("committed@example.com",)
        assert con.execute(
            "SELECT revoked_at IS NOT NULL FROM sessions_v2 WHERE token_hash=?",
            (target_hash,),
        ).fetchone() == (1,)


@pytest.mark.parametrize("operation", ["revoke_session", "revoke_other_sessions"])
def test_session_revokers_persist_when_they_own_the_transaction(
    monkeypatch, tmp_path, operation
):
    server, database = load_auth_server(monkeypatch, tmp_path)
    auth_data = importlib.import_module("auth_data")
    user_id, current, target_hash = _seed_session_revoke_test(
        server, auth_data, f"{operation}-owned@example.com"
    )
    with closing(server.db()) as con:
        if operation == "revoke_session":
            assert auth_data.revoke_session(
                con, user_id=user_id, session_hash=target_hash
            ) is True
        else:
            auth_data.revoke_other_sessions(
                con, user_id=user_id, current_token=current
            )
        assert con.in_transaction is False

    with sqlite3.connect(database) as con:
        assert con.execute(
            "SELECT revoked_at IS NOT NULL FROM sessions_v2 WHERE token_hash=?",
            (target_hash,),
        ).fetchone() == (1,)


def _seed_single_session_test(server, auth_data, email):
    with closing(server.db()) as con:
        user_id = con.execute(
            "INSERT INTO pouzivatelia (email) VALUES (?)", (email,)
        ).lastrowid
        con.commit()
        raw_session = auth_data.create_session(
            con,
            user_id=user_id,
            now=1_800_000_000.0,
            device_name="Transaction test",
        )
    return user_id, raw_session


def test_session_touch_persists_when_it_owns_the_transaction(monkeypatch, tmp_path):
    server, database = load_auth_server(monkeypatch, tmp_path)
    auth_data = importlib.import_module("auth_data")
    user_id, raw_session = _seed_single_session_test(
        server, auth_data, "touch-owned@example.com"
    )
    with closing(server.db()) as con:
        assert auth_data.user_for_session(
            con, raw_session=raw_session, now=1_800_086_400.0
        )["id"] == user_id
        assert con.in_transaction is False

    with sqlite3.connect(database) as con:
        assert con.execute(
            "SELECT last_seen_at, expires_at FROM sessions_v2 WHERE user_id=?",
            (user_id,),
        ).fetchone() == (1_800_086_400.0, 1_807_862_400.0)


def test_session_touch_leaves_a_caller_transaction_open_for_rollback(
    monkeypatch, tmp_path
):
    server, database = load_auth_server(monkeypatch, tmp_path)
    auth_data = importlib.import_module("auth_data")
    original_email = "touch-rollback@example.com"
    user_id, raw_session = _seed_single_session_test(
        server, auth_data, original_email
    )
    with closing(server.db()) as con:
        con.execute(
            "UPDATE pouzivatelia SET email='pending@example.com' WHERE id=?",
            (user_id,),
        )
        assert auth_data.user_for_session(
            con, raw_session=raw_session, now=1_800_086_400.0
        )["id"] == user_id

        assert con.in_transaction is True
        assert tuple(
            con.execute(
                "SELECT last_seen_at, expires_at FROM sessions_v2 WHERE user_id=?",
                (user_id,),
            ).fetchone()
        ) == (1_800_086_400.0, 1_807_862_400.0)
        con.rollback()

    with sqlite3.connect(database) as con:
        assert con.execute(
            "SELECT email FROM pouzivatelia WHERE id=?", (user_id,)
        ).fetchone() == (original_email,)
        assert con.execute(
            "SELECT last_seen_at, expires_at FROM sessions_v2 WHERE user_id=?",
            (user_id,),
        ).fetchone() == (1_800_000_000.0, 1_807_776_000.0)


def test_expired_session_cleanup_persists_when_it_owns_the_transaction(
    monkeypatch, tmp_path
):
    server, database = load_auth_server(monkeypatch, tmp_path)
    auth_data = importlib.import_module("auth_data")
    user_id, raw_session = _seed_single_session_test(
        server, auth_data, "expiry-owned@example.com"
    )
    with closing(server.db()) as con:
        con.execute(
            "UPDATE sessions_v2 SET expires_at=? WHERE user_id=?",
            (1_800_000_000.0, user_id),
        )
        con.commit()
        assert auth_data.user_for_session(
            con, raw_session=raw_session, now=1_800_000_000.0
        ) is None
        assert con.in_transaction is False

    with sqlite3.connect(database) as con:
        assert con.execute(
            "SELECT COUNT(*) FROM sessions_v2 WHERE user_id=?", (user_id,)
        ).fetchone() == (0,)


def test_expired_session_cleanup_leaves_a_caller_transaction_open_for_rollback(
    monkeypatch, tmp_path
):
    server, database = load_auth_server(monkeypatch, tmp_path)
    auth_data = importlib.import_module("auth_data")
    original_email = "expiry-rollback@example.com"
    user_id, raw_session = _seed_single_session_test(
        server, auth_data, original_email
    )
    with closing(server.db()) as con:
        con.execute(
            "UPDATE sessions_v2 SET expires_at=? WHERE user_id=?",
            (1_800_000_000.0, user_id),
        )
        con.commit()
        con.execute(
            "UPDATE pouzivatelia SET email='pending@example.com' WHERE id=?",
            (user_id,),
        )
        assert auth_data.user_for_session(
            con, raw_session=raw_session, now=1_800_000_000.0
        ) is None

        assert con.in_transaction is True
        assert con.execute(
            "SELECT COUNT(*) FROM sessions_v2 WHERE user_id=?", (user_id,)
        ).fetchone()[0] == 0
        con.rollback()

    with sqlite3.connect(database) as con:
        assert con.execute(
            "SELECT email FROM pouzivatelia WHERE id=?", (user_id,)
        ).fetchone() == (original_email,)
        assert con.execute(
            "SELECT expires_at FROM sessions_v2 WHERE user_id=?", (user_id,)
        ).fetchone() == (1_800_000_000.0,)


def test_delete_session_persists_when_it_owns_the_transaction(monkeypatch, tmp_path):
    server, database = load_auth_server(monkeypatch, tmp_path)
    auth_data = importlib.import_module("auth_data")
    user_id, raw_session = _seed_single_session_test(
        server, auth_data, "delete-owned@example.com"
    )
    with closing(server.db()) as con:
        auth_data.delete_session(con, raw_session)
        assert con.in_transaction is False

    with sqlite3.connect(database) as con:
        assert con.execute(
            "SELECT COUNT(*) FROM sessions_v2 WHERE user_id=?", (user_id,)
        ).fetchone() == (0,)


def test_delete_session_leaves_a_caller_transaction_open_for_rollback(
    monkeypatch, tmp_path
):
    server, database = load_auth_server(monkeypatch, tmp_path)
    auth_data = importlib.import_module("auth_data")
    original_email = "delete-rollback@example.com"
    user_id, raw_session = _seed_single_session_test(
        server, auth_data, original_email
    )
    with closing(server.db()) as con:
        con.execute(
            "UPDATE pouzivatelia SET email='pending@example.com' WHERE id=?",
            (user_id,),
        )
        auth_data.delete_session(con, raw_session)

        assert con.in_transaction is True
        assert con.execute(
            "SELECT COUNT(*) FROM sessions_v2 WHERE user_id=?", (user_id,)
        ).fetchone()[0] == 0
        con.rollback()

    with sqlite3.connect(database) as con:
        assert con.execute(
            "SELECT email FROM pouzivatelia WHERE id=?", (user_id,)
        ).fetchone() == (original_email,)
        assert con.execute(
            "SELECT token_hash FROM sessions_v2 WHERE user_id=?", (user_id,)
        ).fetchone() == (hashlib.sha256(raw_session.encode()).hexdigest(),)


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


# ---------------------------------------------------------------- auth v3 routes
AUTH_V3_ORIGIN = AUTH_ORIGIN


def auth_v3_client(server, **kwargs):
    os.environ["UVARSI_AUTH_V3"] = "1"
    return FastAPITestClient(server.app, base_url="https://uvar.si", **kwargs)


def seed_password_account(server, auth_data, email, password="correct horse battery"):
    os.environ["UVARSI_AUTH_V3"] = "1"
    with closing(server.db()) as con:
        user_id = con.execute(
            "INSERT INTO pouzivatelia (email) VALUES (?)", (email,)
        ).lastrowid
        auth_data.set_password(
            con,
            user_id=user_id,
            password_hash=auth_data.hash_password(password),
            now=1_000.0,
        )
        con.commit()
    return user_id


def action_token_from_message(calls, page):
    text = calls[-1][1]["json"]["text"]
    match = re.search(
        rf"https://uvar\.si/{page}#token=([A-Za-z0-9_-]+)(?:&purpose=[a-z]+)?",
        text,
    )
    assert match, text
    return match.group(1)


def test_auth_v3_route_registration_waits_for_explicit_confirmation_post(
    monkeypatch, tmp_path
):
    server, database = load_auth_server(monkeypatch, tmp_path)
    monkeypatch.setenv("RESEND_API_KEY", "test-only-key")
    calls = []
    install_provider(monkeypatch, calls=calls)
    client = auth_v3_client(server)
    password = "  Žemľová polievka  "

    registered = client.post(
        "/api/auth/register",
        headers=AUTH_V3_ORIGIN,
        json={"email": " New.Cook@Example.com ", "password": password},
    )

    assert registered.status_code == 200
    assert "new.cook@example.com" not in registered.text.lower()
    assert password not in registered.text
    raw_token = action_token_from_message(calls, "potvrdenie")
    with sqlite3.connect(database) as con:
        assert con.execute(
            "SELECT COUNT(*) FROM pouzivatelia WHERE email='new.cook@example.com'"
        ).fetchone() == (0,)
        stored = con.execute(
            """SELECT token_hash, email, purpose, pending_password_hash
               FROM auth_action_tokens"""
        ).fetchone()
        assert stored[0] == hashlib.sha256(raw_token.encode()).hexdigest()
        assert stored[1:3] == ("new.cook@example.com", "confirm")
        assert stored[3].startswith("$argon2id$")
        assert password not in " ".join(str(value) for value in stored)

    scanner_get = client.get("/potvrdenie")
    assert scanner_get.status_code == 200
    assert 'type="button"' in scanner_get.text
    assert "Potvrdiť účet" in scanner_get.text
    assert "fetch('/api/auth/confirm'" in scanner_get.text
    with sqlite3.connect(database) as con:
        assert con.execute("SELECT COUNT(*) FROM auth_action_tokens").fetchone() == (1,)
        assert con.execute("SELECT COUNT(*) FROM pouzivatelia").fetchone() == (0,)

    confirmed = client.post(
        "/api/auth/confirm",
        headers=AUTH_V3_ORIGIN,
        json={"token": raw_token, "device_name": "Firefox na notebooku"},
    )

    assert confirmed.status_code == 200
    assert confirmed.json() == {"ok": True, "redirect": "/app"}
    assert client.cookies.get(server.COOKIE)
    with sqlite3.connect(database) as con:
        user = con.execute(
            "SELECT id FROM pouzivatelia WHERE email='new.cook@example.com'"
        ).fetchone()
        assert user is not None
        assert con.execute(
            "SELECT password_hash FROM auth_credentials WHERE user_id=?", user
        ).fetchone()[0].startswith("$argon2id$")
        assert con.execute("SELECT COUNT(*) FROM auth_action_tokens").fetchone() == (0,)
        assert con.execute("SELECT COUNT(*) FROM sessions_v2").fetchone() == (1,)

    replay = client.post(
        "/api/auth/confirm", headers=AUTH_V3_ORIGIN, json={"token": raw_token}
    )
    assert replay.status_code == 400
    existing_registration = client.post(
        "/api/auth/register",
        headers=AUTH_V3_ORIGIN,
        json={
            "email": "new.cook@example.com",
            "password": "different valid password",
        },
    )
    assert existing_registration.status_code == registered.status_code
    assert existing_registration.json() == registered.json()
    with sqlite3.connect(database) as con:
        assert con.execute("SELECT COUNT(*) FROM pouzivatelia").fetchone() == (1,)
        assert con.execute("SELECT COUNT(*) FROM sessions_v2").fetchone() == (1,)


def test_auth_v3_route_login_is_generic_and_keeps_both_devices_valid(
    monkeypatch, tmp_path
):
    server, database = load_auth_server(monkeypatch, tmp_path)
    auth_data = sys.modules["auth_data"]
    user_id = seed_password_account(server, auth_data, "cook@example.com")
    first = auth_v3_client(server)
    second = auth_v3_client(server)

    first_login = first.post(
        "/api/auth/login",
        headers=AUTH_V3_ORIGIN,
        json={"email": " Cook@Example.com ", "password": "correct horse battery"},
    )
    second_login = second.post(
        "/api/auth/login",
        headers=AUTH_V3_ORIGIN,
        json={"email": "cook@example.com", "password": "correct horse battery"},
    )
    wrong = auth_v3_client(server).post(
        "/api/auth/login",
        headers=AUTH_V3_ORIGIN,
        json={"email": "cook@example.com", "password": "wrong password"},
    )
    unknown = auth_v3_client(server).post(
        "/api/auth/login",
        headers=AUTH_V3_ORIGIN,
        json={"email": "missing@example.com", "password": "wrong password"},
    )

    assert first_login.status_code == second_login.status_code == 200
    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json() == unknown.json()
    assert "cook@example.com" not in wrong.text
    assert first.get("/api/me").json()["id"] == user_id
    assert second.get("/api/me").json()["id"] == user_id
    with sqlite3.connect(database) as con:
        assert con.execute(
            "SELECT COUNT(*) FROM sessions_v2 WHERE user_id=? AND revoked_at IS NULL",
            (user_id,),
        ).fetchone() == (2,)


def test_auth_v3_route_password_request_and_reset_are_generic_one_time_and_atomic(
    monkeypatch, tmp_path
):
    server, database = load_auth_server(monkeypatch, tmp_path)
    auth_data = sys.modules["auth_data"]
    user_id = seed_password_account(server, auth_data, "reset@example.com")
    with closing(server.db()) as con:
        old_first = auth_data.create_session(
            con, user_id=user_id, now=1_000.0, device_name="Mobil"
        )
        old_second = auth_data.create_session(
            con, user_id=user_id, now=1_000.0, device_name="Notebook"
        )
        con.commit()
    monkeypatch.setenv("RESEND_API_KEY", "test-only-key")
    calls = []
    delivered = threading.Event()

    def post(url, **kwargs):
        calls.append((url, kwargs))
        delivered.set()
        return ProviderResponse()

    monkeypatch.setitem(sys.modules, "requests", types.SimpleNamespace(post=post))

    with auth_v3_client(server) as client:
        known = client.post(
            "/api/auth/password/request",
            headers=AUTH_V3_ORIGIN,
            json={"email": " Reset@Example.com "},
        )
        unknown = client.post(
            "/api/auth/password/request",
            headers=AUTH_V3_ORIGIN,
            json={"email": "unknown@example.com"},
        )
        assert delivered.wait(1), "queued reset delivery did not reach the provider"

    assert known.status_code == unknown.status_code == 200
    assert known.json() == unknown.json()
    assert "reset@example.com" not in known.text.lower()
    raw_token = action_token_from_message(calls, "heslo")
    new_client = auth_v3_client(server)
    reset = new_client.post(
        "/api/auth/password/reset",
        headers=AUTH_V3_ORIGIN,
        json={"token": raw_token, "password": "nové bezpečné heslo"},
    )

    assert reset.status_code == 200
    new_session = new_client.cookies.get(server.COOKIE)
    assert new_session and new_session not in {old_first, old_second}
    with closing(server.db()) as con:
        assert auth_data.authenticate_password(
            con, email="reset@example.com", password="nové bezpečné heslo"
        ) == user_id
        assert auth_data.authenticate_password(
            con, email="reset@example.com", password="correct horse battery"
        ) is None
        sessions = con.execute(
            "SELECT token_hash, revoked_at FROM sessions_v2 WHERE user_id=?", (user_id,)
        ).fetchall()
        assert sum(row[1] is None for row in sessions) == 1
        assert sessions[-1][0] == hashlib.sha256(new_session.encode()).hexdigest()

    replay = new_client.post(
        "/api/auth/password/reset",
        headers=AUTH_V3_ORIGIN,
        json={"token": raw_token, "password": "ešte iné bezpečné"},
    )
    assert replay.status_code == 400


@pytest.mark.parametrize(
    "endpoint,purpose",
    [
        ("/api/auth/confirm", "confirm"),
        ("/api/auth/password/reset", "reset"),
    ],
)
def test_auth_v3_route_action_token_rolls_back_when_protected_mutation_fails(
    monkeypatch, tmp_path, endpoint, purpose
):
    server, database = load_auth_server(monkeypatch, tmp_path)
    auth_data = sys.modules["auth_data"]
    now = 5_000.0
    monkeypatch.setattr(server, "AUTH_CLOCK", lambda: now)
    pending_hash = auth_data.hash_password("new protected password")
    with closing(server.db()) as con:
        if purpose == "reset":
            user_id = con.execute(
                "INSERT INTO pouzivatelia (email) VALUES ('atomic@example.com')"
            ).lastrowid
            auth_data.set_password(
                con,
                user_id=user_id,
                password_hash=auth_data.hash_password("old protected password"),
                now=now - 1,
            )
        raw_token = auth_data.create_action_token(
            con,
            email="atomic@example.com",
            purpose=purpose,
            now=now,
            pending_password_hash=pending_hash if purpose == "confirm" else None,
        )
        sibling_token = None
        if purpose == "reset":
            sibling_token = auth_data.create_action_token(
                con,
                email="atomic@example.com",
                purpose="setup",
                now=now,
            )

    if purpose == "confirm":
        monkeypatch.setattr(
            server,
            "create_session",
            lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("boom")),
            raising=False,
        )
        payload = {"token": raw_token}
    else:
        monkeypatch.setattr(
            server,
            "revoke_other_sessions",
            lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("boom")),
            raising=False,
        )
        payload = {"token": raw_token, "password": "new protected password"}

    response = auth_v3_client(server, raise_server_exceptions=False).post(
        endpoint, headers=AUTH_V3_ORIGIN, json=payload
    )

    assert response.status_code == 500, response.text
    with closing(server.db()) as con:
        assert con.execute(
            "SELECT COUNT(*) FROM auth_action_tokens WHERE token_hash=?",
            (hashlib.sha256(raw_token.encode()).hexdigest(),),
        ).fetchone()[0] == 1
        if sibling_token is not None:
            assert con.execute(
                "SELECT COUNT(*) FROM auth_action_tokens WHERE token_hash=?",
                (hashlib.sha256(sibling_token.encode()).hexdigest(),),
            ).fetchone()[0] == 1
        if purpose == "confirm":
            assert con.execute(
                "SELECT COUNT(*) FROM pouzivatelia WHERE email='atomic@example.com'"
            ).fetchone()[0] == 0
        else:
            assert auth_data.authenticate_password(
                con, email="atomic@example.com", password="old protected password"
            ) is not None
            assert auth_data.authenticate_password(
                con, email="atomic@example.com", password="new protected password"
            ) is None


def test_auth_v3_route_authenticated_password_and_session_management(
    monkeypatch, tmp_path
):
    server, _ = load_auth_server(monkeypatch, tmp_path)
    auth_data = sys.modules["auth_data"]
    monkeypatch.setattr(server, "AUTH_CLOCK", lambda: 1_001.0)
    with closing(server.db()) as con:
        user_id = con.execute(
            "INSERT INTO pouzivatelia (email) VALUES ('legacy@example.com')"
        ).lastrowid
        current = auth_data.create_session(
            con, user_id=user_id, now=1_000.0, device_name="Aktuálny mobil"
        )
        old_other = auth_data.create_session(
            con, user_id=user_id, now=1_000.0, device_name="Starý notebook"
        )
        con.commit()
    client = auth_v3_client(server)
    client.cookies.set(server.COOKIE, current)

    password_set = client.post(
        "/api/auth/password/set",
        headers=AUTH_V3_ORIGIN,
        json={"password": "prvé bezpečné heslo"},
    )
    assert password_set.status_code == 200
    with closing(server.db()) as con:
        assert auth_data.authenticate_password(
            con, email="legacy@example.com", password="prvé bezpečné heslo"
        ) == user_id
        assert auth_data.user_for_session(con, raw_session=current, now=1_001.0)
        assert auth_data.user_for_session(con, raw_session=old_other, now=1_001.0) is None

    second = auth_v3_client(server)
    assert second.post(
        "/api/auth/login",
        headers=AUTH_V3_ORIGIN,
        json={
            "email": "legacy@example.com",
            "password": "prvé bezpečné heslo",
            "device_name": "PC",
        },
    ).status_code == 200
    second_raw = second.cookies.get(server.COOKIE)
    sessions = client.get("/api/auth/sessions")
    assert sessions.status_code == 200
    listed = sessions.json()["sessions"]
    assert len(listed) == 2
    assert all(
        re.fullmatch(r"[0-9a-f]{64}", item["session_hash"]) for item in listed
    )
    assert sum(item["current"] for item in listed) == 1
    assert current not in sessions.text
    assert second_raw not in sessions.text
    second_hash = hashlib.sha256(second_raw.encode()).hexdigest()

    credential_attempt = auth_v3_client(server)
    credential_attempt.cookies.set(server.COOKIE, second_hash)
    assert credential_attempt.get("/api/me").json() == {
        "prihlaseny": False,
        "auth_v3": True,
    }
    missing_origin = client.delete(f"/api/auth/sessions/{second_hash}")
    foreign_origin = client.delete(
        f"/api/auth/sessions/{second_hash}",
        headers={"Origin": "https://evil.example"},
    )
    assert missing_origin.status_code == foreign_origin.status_code == 403
    deleted = client.delete(
        f"/api/auth/sessions/{second_hash}", headers=AUTH_V3_ORIGIN
    )
    assert deleted.status_code == 200
    assert second.get("/api/me").json() == {
        "prihlaseny": False,
        "auth_v3": True,
    }

    third = auth_v3_client(server)
    assert third.post(
        "/api/auth/login",
        headers=AUTH_V3_ORIGIN,
        json={"email": "legacy@example.com", "password": "prvé bezpečné heslo"},
    ).status_code == 200
    logout_others = client.post(
        "/api/auth/sessions/logout-others", headers=AUTH_V3_ORIGIN, json={}
    )
    assert logout_others.status_code == 200
    assert client.get("/api/me").json()["id"] == user_id
    assert third.get("/api/me").json() == {
        "prihlaseny": False,
        "auth_v3": True,
    }

    wrong_change = client.post(
        "/api/auth/password/change",
        headers=AUTH_V3_ORIGIN,
        json={
            "current_password": "wrong password",
            "password": "druhé bezpečné heslo",
        },
    )
    assert wrong_change.status_code == 401
    changed = client.post(
        "/api/auth/password/change",
        headers=AUTH_V3_ORIGIN,
        json={
            "current_password": "prvé bezpečné heslo",
            "password": "druhé bezpečné heslo",
        },
    )
    assert changed.status_code == 200
    with closing(server.db()) as con:
        assert auth_data.authenticate_password(
            con, email="legacy@example.com", password="prvé bezpečné heslo"
        ) is None
        assert auth_data.authenticate_password(
            con, email="legacy@example.com", password="druhé bezpečné heslo"
        ) == user_id

    monkeypatch.setattr(server, "AUTH_CLOCK", lambda: 1_001.0 + 24 * 60 * 60)
    current_hash = hashlib.sha256(current.encode()).hexdigest()
    current_logout = client.delete(
        f"/api/auth/sessions/{current_hash}", headers=AUTH_V3_ORIGIN
    )
    assert current_logout.status_code == 200
    cookie_headers = current_logout.headers.get_list("set-cookie")
    assert any("Max-Age=0" in header for header in cookie_headers)
    assert all("Max-Age=7776000" not in header for header in cookie_headers)


@pytest.mark.parametrize(
    "endpoint,payload",
    [
        (
            "/api/auth/register",
            {"email": "a@example.com", "password": "long enough password"},
        ),
        ("/api/auth/request", {"email": "a@example.com"}),
        ("/api/auth/verify", {"token": "token"}),
        ("/api/auth/logout", {}),
        ("/api/auth/confirm", {"token": "token"}),
        (
            "/api/auth/login",
            {"email": "a@example.com", "password": "long enough password"},
        ),
        ("/api/auth/password/request", {"email": "a@example.com"}),
        (
            "/api/auth/password/reset",
            {"token": "token", "password": "long enough password"},
        ),
        ("/api/auth/password/set", {"password": "long enough password"}),
        (
            "/api/auth/password/change",
            {"current_password": "old password", "password": "new password"},
        ),
        ("/api/auth/sessions/logout-others", {}),
    ],
)
def test_auth_v3_route_state_changes_require_the_exact_origin(
    monkeypatch, tmp_path, endpoint, payload
):
    server, _ = load_auth_server(monkeypatch, tmp_path)
    client = auth_v3_client(server)
    missing = client.post(endpoint, json=payload)
    foreign = client.post(
        endpoint, headers={"Origin": "https://evil.example"}, json=payload
    )
    assert missing.status_code == foreign.status_code == 403


@pytest.mark.parametrize(
    "endpoint",
    [
        "/api/auth/register",
        "/api/auth/request",
        "/api/auth/verify",
        "/api/auth/confirm",
        "/api/auth/login",
        "/api/auth/password/request",
        "/api/auth/password/reset",
    ],
)
def test_auth_v3_route_public_json_rejects_malformed_or_non_object_bodies(
    monkeypatch, tmp_path, endpoint
):
    server, _ = load_auth_server(monkeypatch, tmp_path)
    client = auth_v3_client(server)
    malformed = client.post(
        endpoint,
        headers={**AUTH_V3_ORIGIN, "Content-Type": "application/json"},
        content="{",
    )
    array = client.post(endpoint, headers=AUTH_V3_ORIGIN, json=[])
    assert malformed.status_code == array.status_code == 400


def test_auth_v3_route_limits_ip_and_normalized_account_independently(
    monkeypatch, tmp_path
):
    server, _ = load_auth_server(monkeypatch, tmp_path)
    auth_data = sys.modules["auth_data"]
    monkeypatch.setenv("RESEND_API_KEY", "test-only-key")
    calls = []
    install_provider(monkeypatch, calls=calls)
    server.AUTH_V3_IP_LIMITER = server.ClientIpRateLimiter(max_requests=10)
    server.AUTH_V3_ACCOUNT_LIMITER = server.ClientIpRateLimiter(max_requests=1)
    client = auth_v3_client(server)

    first = client.post(
        "/api/auth/register",
        headers=AUTH_V3_ORIGIN,
        json={"email": "Rate@Example.com", "password": "long enough password"},
    )
    normalized_repeat = client.post(
        "/api/auth/register",
        headers=AUTH_V3_ORIGIN,
        json={"email": " rate@example.com ", "password": "long enough password"},
    )
    assert first.status_code == 200
    assert normalized_repeat.status_code == 429
    assert len(calls) == 1

    server.AUTH_V3_IP_LIMITER = server.ClientIpRateLimiter(max_requests=1)
    server.AUTH_V3_ACCOUNT_LIMITER = server.ClientIpRateLimiter(max_requests=10)
    first_ip = auth_v3_client(server).post(
        "/api/auth/login",
        headers=AUTH_V3_ORIGIN,
        json={"email": "one@example.com", "password": "long enough password"},
    )
    second_ip = auth_v3_client(server).post(
        "/api/auth/login",
        headers=AUTH_V3_ORIGIN,
        json={"email": "two@example.com", "password": "long enough password"},
    )
    assert first_ip.status_code == 401
    assert second_ip.status_code == 429

    server.AUTH_V3_IP_LIMITER = server.ClientIpRateLimiter(max_requests=1)
    server.AUTH_V3_ACCOUNT_LIMITER = server.ClientIpRateLimiter(max_requests=10)
    invalid_first = auth_v3_client(server).post(
        "/api/auth/login",
        headers=AUTH_V3_ORIGIN,
        json={"email": "not-an-email", "password": "long enough password"},
    )
    invalid_second = auth_v3_client(server).post(
        "/api/auth/login",
        headers=AUTH_V3_ORIGIN,
        json={"email": "still-not-an-email", "password": "long enough password"},
    )
    assert invalid_first.status_code == 401
    assert invalid_second.status_code == 429

    seed_password_account(server, auth_data, "limited-reset@example.com")
    now = server.AUTH_CLOCK()
    with closing(server.db()) as con:
        first_reset = auth_data.create_action_token(
            con, email="limited-reset@example.com", purpose="reset", now=now
        )
    server.AUTH_V3_IP_LIMITER = server.ClientIpRateLimiter(max_requests=10)
    server.AUTH_V3_ACCOUNT_LIMITER = server.ClientIpRateLimiter(max_requests=1)
    accepted_reset = auth_v3_client(server).post(
        "/api/auth/password/reset",
        headers=AUTH_V3_ORIGIN,
        json={"token": first_reset, "password": "first reset password"},
    )
    with closing(server.db()) as con:
        second_reset = auth_data.create_action_token(
            con, email="limited-reset@example.com", purpose="reset", now=now
        )
    limited_reset = auth_v3_client(server).post(
        "/api/auth/password/reset",
        headers=AUTH_V3_ORIGIN,
        json={"token": second_reset, "password": "second reset password"},
    )
    assert accepted_reset.status_code == 200
    assert limited_reset.status_code == 429


def test_auth_v3_route_provider_failure_never_exposes_registration_secrets(
    capsys, monkeypatch, tmp_path
):
    server, database = load_auth_server(monkeypatch, tmp_path)
    monkeypatch.setenv("RESEND_API_KEY", "test-only-key")
    calls = []
    install_provider(
        monkeypatch, error=TimeoutError("network unavailable"), calls=calls
    )
    password = "provider secret password"

    response = auth_v3_client(server).post(
        "/api/auth/register",
        headers=AUTH_V3_ORIGIN,
        json={"email": "provider@example.com", "password": password},
    )

    assert response.status_code == 503
    raw_token = action_token_from_message(calls, "potvrdenie")
    captured = capsys.readouterr()
    public = response.text + captured.out + captured.err
    assert password not in public
    assert raw_token not in public
    assert "provider@example.com" not in public
    with sqlite3.connect(database) as con:
        stored = con.execute(
            "SELECT token_hash, pending_password_hash FROM auth_action_tokens"
        ).fetchone()
        assert stored[0] != raw_token
        assert stored[1] != password


def test_auth_v3_route_legacy_magic_request_only_serves_existing_accounts(
    monkeypatch, tmp_path
):
    server, database = load_auth_server(monkeypatch, tmp_path)
    auth_data = sys.modules["auth_data"]
    monkeypatch.setenv("RESEND_API_KEY", "test-only-key")
    calls = []
    install_provider(monkeypatch, calls=calls)
    with closing(server.db()) as con:
        con.execute("INSERT INTO pouzivatelia (email) VALUES ('legacy@example.com')")
        con.commit()
    seed_password_account(server, auth_data, "configured@example.com")
    client = auth_v3_client(server)

    unknown = client.post(
        "/api/auth/request", headers=AUTH_V3_ORIGIN, json={"email": "new@example.com"}
    )
    configured = client.post(
        "/api/auth/request",
        headers=AUTH_V3_ORIGIN,
        json={"email": "configured@example.com"},
    )
    existing = client.post(
        "/api/auth/request",
        headers=AUTH_V3_ORIGIN,
        json={"email": "legacy@example.com"},
    )

    assert unknown.status_code == configured.status_code == existing.status_code == 200
    assert unknown.json() == configured.json() == existing.json()
    assert len(calls) == 1
    with sqlite3.connect(database) as con:
        assert con.execute("SELECT email FROM magic_tokens_v2").fetchall() == [
            ("legacy@example.com",)
        ]


def test_auth_v3_route_legacy_verify_rejects_unknown_or_now_configured_accounts(
    monkeypatch, tmp_path
):
    server, database = load_auth_server(monkeypatch, tmp_path)
    auth_data = sys.modules["auth_data"]
    monkeypatch.setenv("RESEND_API_KEY", "test-only-key")
    calls = []
    install_provider(monkeypatch, calls=calls)
    raw_token = issue_link(server, monkeypatch, "legacy@example.com")
    with closing(server.db()) as con:
        user_id = con.execute(
            "SELECT id FROM pouzivatelia WHERE email='legacy@example.com'"
        ).fetchone()[0]
        auth_data.set_password(
            con,
            user_id=user_id,
            password_hash=auth_data.hash_password("configured password"),
            now=1_000.0,
        )
        con.commit()
        unknown_token = "unknown-account-magic-token"
        con.execute(
            """INSERT INTO magic_tokens_v2
               (token_hash, email, expires_at, created_at) VALUES (?, ?, ?, ?)""",
            (
                hashlib.sha256(unknown_token.encode()).hexdigest(),
                "never-registered@example.com",
                server.AUTH_CLOCK() + 3_600,
                server.AUTH_CLOCK(),
            ),
        )
        con.commit()

    configured = auth_v3_client(server).post(
        "/api/auth/verify", headers=AUTH_V3_ORIGIN, json={"token": raw_token}
    )
    unknown = auth_v3_client(server).post(
        "/api/auth/verify", headers=AUTH_V3_ORIGIN, json={"token": unknown_token}
    )

    assert configured.status_code == unknown.status_code == 400
    with sqlite3.connect(database) as con:
        assert con.execute("SELECT COUNT(*) FROM sessions_v2").fetchone() == (0,)
        assert con.execute("SELECT COUNT(*) FROM magic_tokens_v2").fetchone() == (0,)


def test_auth_task5_fix1_legacy_mutations_reject_foreign_origin_before_body_or_cookie_use(
    monkeypatch, tmp_path
):
    server, database = load_auth_server(monkeypatch, tmp_path)
    auth_data = sys.modules["auth_data"]
    now = 1_800_000_000.0
    monkeypatch.setattr(server, "AUTH_CLOCK", lambda: now)
    monkeypatch.setenv("RESEND_API_KEY", "test-only-key")
    calls = []
    install_provider(monkeypatch, calls=calls)
    raw_magic = "foreign-origin-valid-magic-token"
    with closing(server.db()) as con:
        user_id = con.execute(
            "INSERT INTO pouzivatelia (email) VALUES ('legacy-origin@example.com')"
        ).lastrowid
        con.execute(
            """INSERT INTO magic_tokens_v2
               (token_hash, email, expires_at, created_at) VALUES (?, ?, ?, ?)""",
            (
                hashlib.sha256(raw_magic.encode()).hexdigest(),
                "legacy-origin@example.com",
                now + 3_600,
                now,
            ),
        )
        current = auth_data.create_session(
            con, user_id=user_id, now=now, device_name="Current"
        )
        con.commit()

    foreign_headers = {
        "Origin": "https://evil.example",
        "Content-Type": "text/plain",
    }
    request = auth_v3_client(server).post(
        "/api/auth/request",
        headers=foreign_headers,
        content=json.dumps({"email": "legacy-origin@example.com"}),
    )
    attacker = auth_v3_client(server)
    verify = attacker.post(
        "/api/auth/verify",
        headers=foreign_headers,
        content=json.dumps({"token": raw_magic}),
    )
    logged_in = auth_v3_client(server)
    logged_in.cookies.set(server.COOKIE, current)
    logout = logged_in.post(
        "/api/auth/logout", headers={"Origin": "https://evil.example"}
    )

    assert request.status_code == verify.status_code == logout.status_code == 403
    assert calls == []
    assert verify.headers.get_list("set-cookie") == []
    assert logout.headers.get_list("set-cookie") == []
    with sqlite3.connect(database) as con:
        assert con.execute("SELECT COUNT(*) FROM magic_tokens_v2").fetchone() == (1,)
        assert con.execute(
            "SELECT COUNT(*) FROM sessions_v2 WHERE token_hash=?",
            (hashlib.sha256(current.encode()).hexdigest(),),
        ).fetchone() == (1,)


def test_auth_task5_fix1_reset_atomically_invalidates_all_reset_and_setup_siblings(
    monkeypatch, tmp_path
):
    server, _ = load_auth_server(monkeypatch, tmp_path)
    auth_data = sys.modules["auth_data"]
    now = 1_800_000_000.0
    monkeypatch.setattr(server, "AUTH_CLOCK", lambda: now)
    user_id = seed_password_account(
        server, auth_data, "siblings@example.com", "original password"
    )
    with closing(server.db()) as con:
        older_reset = auth_data.create_action_token(
            con, email="siblings@example.com", purpose="reset", now=now - 2
        )
        setup_sibling = auth_data.create_action_token(
            con, email="siblings@example.com", purpose="setup", now=now - 1
        )
        chosen_reset = auth_data.create_action_token(
            con, email="siblings@example.com", purpose="reset", now=now
        )

    client = auth_v3_client(server)
    changed = client.post(
        "/api/auth/password/reset",
        headers=AUTH_V3_ORIGIN,
        json={"token": chosen_reset, "password": "first final password"},
    )
    old_retry = auth_v3_client(server).post(
        "/api/auth/password/reset",
        headers=AUTH_V3_ORIGIN,
        json={"token": older_reset, "password": "attacker reset password"},
    )
    setup_retry = auth_v3_client(server).post(
        "/api/auth/password/reset",
        headers=AUTH_V3_ORIGIN,
        json={
            "token": setup_sibling,
            "purpose": "setup",
            "password": "attacker setup password",
        },
    )

    assert changed.status_code == 200
    assert old_retry.status_code == setup_retry.status_code == 400
    with closing(server.db()) as con:
        assert con.execute(
            "SELECT COUNT(*) FROM auth_action_tokens WHERE email=?",
            ("siblings@example.com",),
        ).fetchone()[0] == 0
        assert auth_data.authenticate_password(
            con, email="siblings@example.com", password="first final password"
        ) == user_id
        assert auth_data.authenticate_password(
            con, email="siblings@example.com", password="attacker reset password"
        ) is None
        assert auth_data.authenticate_password(
            con, email="siblings@example.com", password="attacker setup password"
        ) is None


def test_auth_task5_fix1_password_request_returns_before_known_mailer_and_matches_unknown(
    monkeypatch, tmp_path
):
    server, _ = load_auth_server(monkeypatch, tmp_path)
    auth_data = sys.modules["auth_data"]
    seed_password_account(server, auth_data, "known-timing@example.com")
    monkeypatch.setenv("RESEND_API_KEY", "test-only-key")
    provider_entered = threading.Event()
    release_provider = threading.Event()

    def post(url, **kwargs):
        provider_entered.set()
        if not release_provider.wait(2):
            raise TimeoutError("test provider was not released")
        return ProviderResponse()

    monkeypatch.setitem(sys.modules, "requests", types.SimpleNamespace(post=post))

    async def scenario():
        transport = httpx.ASGITransport(app=server.app)
        async with httpx.AsyncClient(
            transport=transport, base_url="https://uvar.si"
        ) as client:
            known_done = asyncio.Event()

            async def request_known():
                response = await client.post(
                    "/api/auth/password/request",
                    headers=AUTH_V3_ORIGIN,
                    json={"email": "known-timing@example.com"},
                )
                known_done.set()
                return response

            known_task = asyncio.create_task(request_known())
            try:
                assert await asyncio.to_thread(provider_entered.wait, 1)
                returned_before_provider = True
                try:
                    await asyncio.wait_for(known_done.wait(), timeout=0.2)
                except TimeoutError:
                    returned_before_provider = False
                unknown = await client.post(
                    "/api/auth/password/request",
                    headers=AUTH_V3_ORIGIN,
                    json={"email": "unknown-timing@example.com"},
                )
            finally:
                release_provider.set()
            known = await known_task
            return returned_before_provider, known, unknown

    returned_before_provider, known, unknown = asyncio.run(scenario())

    assert returned_before_provider is True
    assert known.status_code == unknown.status_code == 200
    assert known.json() == unknown.json()


def test_auth_task5_fix1_password_request_provider_failure_never_changes_or_leaks_response(
    capsys, monkeypatch, tmp_path
):
    server, _ = load_auth_server(monkeypatch, tmp_path)
    auth_data = sys.modules["auth_data"]
    seed_password_account(server, auth_data, "provider-reset@example.com")
    monkeypatch.setenv("RESEND_API_KEY", "test-only-key")
    provider_finished = threading.Event()
    calls = []

    def post(url, **kwargs):
        calls.append((url, kwargs))
        provider_finished.set()
        raise TimeoutError("provider network secret")

    monkeypatch.setitem(sys.modules, "requests", types.SimpleNamespace(post=post))

    async def scenario():
        transport = httpx.ASGITransport(app=server.app)
        async with httpx.AsyncClient(
            transport=transport, base_url="https://uvar.si"
        ) as client:
            return await client.post(
                "/api/auth/password/request",
                headers=AUTH_V3_ORIGIN,
                json={"email": "provider-reset@example.com"},
            )

    response = asyncio.run(scenario())
    assert provider_finished.wait(1)
    captured = capsys.readouterr()
    raw_token = action_token_from_message(calls, "heslo")
    public = response.text + captured.out + captured.err
    assert response.status_code == 200
    assert "provider-reset@example.com" not in public
    assert raw_token not in public
    assert "provider network secret" not in public


def test_auth_task5_fix1_legacy_magic_is_a_single_setup_claim_not_repeat_login(
    monkeypatch, tmp_path
):
    server, database = load_auth_server(monkeypatch, tmp_path)
    auth_data = sys.modules["auth_data"]
    now = 1_800_000_000.0
    monkeypatch.setattr(server, "AUTH_CLOCK", lambda: now)
    monkeypatch.setenv("RESEND_API_KEY", "test-only-key")
    calls = []
    install_provider(monkeypatch, calls=calls)
    first_magic = "first-legacy-setup-token"
    sibling_magic = "second-legacy-setup-token"
    with closing(server.db()) as con:
        user_id = con.execute(
            "INSERT INTO pouzivatelia (email) VALUES ('one-setup@example.com')"
        ).lastrowid
        con.executemany(
            """INSERT INTO magic_tokens_v2
               (token_hash, email, expires_at, created_at) VALUES (?, ?, ?, ?)""",
            [
                (
                    hashlib.sha256(first_magic.encode()).hexdigest(),
                    "one-setup@example.com",
                    now + 3_600,
                    now,
                ),
                (
                    hashlib.sha256(sibling_magic.encode()).hexdigest(),
                    "one-setup@example.com",
                    now + 3_600,
                    now,
                ),
            ],
        )
        con.commit()

    setup_client = auth_v3_client(server)
    first = setup_client.post(
        "/api/auth/verify",
        headers=AUTH_V3_ORIGIN,
        json={"token": first_magic},
    )
    repeat = auth_v3_client(server).post(
        "/api/auth/verify",
        headers=AUTH_V3_ORIGIN,
        json={"token": sibling_magic},
    )

    assert first.status_code == 200
    assert repeat.status_code == 400
    repeated_request = auth_v3_client(server).post(
        "/api/auth/request",
        headers=AUTH_V3_ORIGIN,
        json={"email": "one-setup@example.com"},
    )
    assert repeated_request.status_code == 200
    assert len(calls) == 1
    assert setup_client.post(
        "/api/auth/password/set",
        headers=AUTH_V3_ORIGIN,
        json={"password": "migrated final password"},
    ).status_code == 200
    with sqlite3.connect(database) as con:
        assert con.execute(
            "SELECT COUNT(*) FROM sessions_v2 WHERE user_id=?", (user_id,)
        ).fetchone() == (1,)
        assert con.execute(
            "SELECT COUNT(*) FROM magic_tokens_v2 WHERE email=?",
            ("one-setup@example.com",),
        ).fetchone() == (0,)
        assert con.execute(
            "SELECT COUNT(*) FROM auth_legacy_setup_claims WHERE user_id=?",
            (user_id,),
        ).fetchone() == (1,)
    calls.clear()
    assert auth_v3_client(server).post(
        "/api/auth/request",
        headers=AUTH_V3_ORIGIN,
        json={"email": "one-setup@example.com"},
    ).status_code == 200
    assert calls == []


def test_auth_task5_fix2_queued_reset_rejects_credential_changed_before_generation(
    monkeypatch, tmp_path
):
    server, database = load_auth_server(monkeypatch, tmp_path)
    auth_data = sys.modules["auth_data"]
    seed_password_account(server, auth_data, "stale-queue@example.com")
    monkeypatch.setenv("RESEND_API_KEY", "test-only-key")
    now = [2_000.0]
    monkeypatch.setattr(server, "AUTH_CLOCK", lambda: now[0])
    worker_entered_db = threading.Event()
    release_worker_db = threading.Event()
    calls = []
    install_provider(monkeypatch, calls=calls)
    real_db = server.db
    request_thread = threading.current_thread()

    def controlled_db():
        if threading.current_thread() is not request_thread:
            worker_entered_db.set()
            if not release_worker_db.wait(2):
                raise TimeoutError("test did not release reset worker")
        return real_db()

    monkeypatch.setattr(server, "db", controlled_db)

    async def scenario():
        transport = httpx.ASGITransport(app=server.app)
        async with httpx.AsyncClient(
            transport=transport, base_url="https://uvar.si"
        ) as client:
            response = await client.post(
                "/api/auth/password/request",
                headers=AUTH_V3_ORIGIN,
                json={"email": "stale-queue@example.com"},
            )
            assert await asyncio.to_thread(worker_entered_db.wait, 1)
            now[0] += 1
            with closing(real_db()) as con:
                user_id = con.execute(
                    "SELECT id FROM pouzivatelia WHERE email='stale-queue@example.com'"
                ).fetchone()[0]
                auth_data.set_password(
                    con,
                    user_id=user_id,
                    password_hash=auth_data.hash_password("credential changed later"),
                    now=now[0],
                )
                con.commit()
            release_worker_db.set()

            async def worker_finished():
                while server.AUTH_BACKGROUND_TASKS:
                    await asyncio.sleep(0)

            await asyncio.wait_for(worker_finished(), timeout=2)
            return response

    try:
        response = asyncio.run(scenario())
    finally:
        release_worker_db.set()

    assert response.status_code == 200
    assert calls == []
    with sqlite3.connect(database) as con:
        assert con.execute(
            "SELECT COUNT(*) FROM auth_action_tokens WHERE email=?",
            ("stale-queue@example.com",),
        ).fetchone() == (0,)


def test_auth_task5_fix2_delivered_reset_rejects_credential_changed_after_mint(
    monkeypatch, tmp_path
):
    server, _ = load_auth_server(monkeypatch, tmp_path)
    auth_data = sys.modules["auth_data"]
    user_id = seed_password_account(server, auth_data, "stale-token@example.com")
    monkeypatch.setenv("RESEND_API_KEY", "test-only-key")
    now = [3_000.0]
    monkeypatch.setattr(server, "AUTH_CLOCK", lambda: now[0])
    provider_entered = threading.Event()
    release_provider = threading.Event()
    calls = []

    def post(url, **kwargs):
        calls.append((url, kwargs))
        provider_entered.set()
        if not release_provider.wait(2):
            raise TimeoutError("test did not release reset provider")
        return ProviderResponse()

    monkeypatch.setitem(sys.modules, "requests", types.SimpleNamespace(post=post))

    async def scenario():
        transport = httpx.ASGITransport(app=server.app)
        async with httpx.AsyncClient(
            transport=transport, base_url="https://uvar.si"
        ) as client:
            response = await client.post(
                "/api/auth/password/request",
                headers=AUTH_V3_ORIGIN,
                json={"email": "stale-token@example.com"},
            )
            assert await asyncio.to_thread(provider_entered.wait, 1)
            now[0] += 1
            with closing(server.db()) as con:
                auth_data.set_password(
                    con,
                    user_id=user_id,
                    password_hash=auth_data.hash_password("newer credential version"),
                    now=now[0],
                )
                con.commit()
            release_provider.set()

            async def worker_finished():
                while server.AUTH_BACKGROUND_TASKS:
                    await asyncio.sleep(0)

            await asyncio.wait_for(worker_finished(), timeout=2)
            return response

    try:
        response = asyncio.run(scenario())
    finally:
        release_provider.set()

    raw_token = action_token_from_message(calls, "heslo")
    replay = auth_v3_client(server).post(
        "/api/auth/password/reset",
        headers=AUTH_V3_ORIGIN,
        json={"token": raw_token, "password": "stale link takeover"},
    )
    assert response.status_code == 200
    assert replay.status_code == 400


def test_auth_task5_fix2_legacy_setup_is_recoverable_until_password_commit(
    monkeypatch, tmp_path
):
    server, database = load_auth_server(monkeypatch, tmp_path)
    auth_data = sys.modules["auth_data"]
    monkeypatch.setenv("RESEND_API_KEY", "test-only-key")
    calls = []
    install_provider(monkeypatch, calls=calls)
    now = 4_000.0
    monkeypatch.setattr(server, "AUTH_CLOCK", lambda: now)
    first_token = "recoverable-first-magic"
    with closing(server.db()) as con:
        user_id = con.execute(
            "INSERT INTO pouzivatelia (email) VALUES ('recoverable@example.com')"
        ).lastrowid
        con.execute(
            """INSERT INTO magic_tokens_v2
               (token_hash, email, expires_at, created_at) VALUES (?, ?, ?, ?)""",
            (
                hashlib.sha256(first_token.encode()).hexdigest(),
                "recoverable@example.com",
                now + 3_600,
                now,
            ),
        )
        con.commit()

    abandoned = auth_v3_client(server)
    assert abandoned.post(
        "/api/auth/verify",
        headers=AUTH_V3_ORIGIN,
        json={"token": first_token},
    ).status_code == 200
    assert abandoned.post("/api/auth/logout", headers=AUTH_V3_ORIGIN).status_code == 200
    with sqlite3.connect(database) as con:
        assert con.execute(
            "SELECT COUNT(*) FROM auth_legacy_setup_claims WHERE user_id=?", (user_id,)
        ).fetchone() == (0,)

    requested = auth_v3_client(server).post(
        "/api/auth/request",
        headers=AUTH_V3_ORIGIN,
        json={"email": "recoverable@example.com"},
    )
    assert requested.status_code == 200
    second_token = outbound_token(calls)
    recovered = auth_v3_client(server)
    assert recovered.post(
        "/api/auth/verify",
        headers=AUTH_V3_ORIGIN,
        json={"token": second_token},
    ).status_code == 200
    assert recovered.post(
        "/api/auth/password/set",
        headers=AUTH_V3_ORIGIN,
        json={"password": "final recoverable password"},
    ).status_code == 200

    with sqlite3.connect(database) as con:
        assert con.execute(
            "SELECT COUNT(*) FROM auth_legacy_setup_claims WHERE user_id=?", (user_id,)
        ).fetchone() == (1,)
    calls.clear()
    assert auth_v3_client(server).post(
        "/api/auth/request",
        headers=AUTH_V3_ORIGIN,
        json={"email": "recoverable@example.com"},
    ).status_code == 200
    assert calls == []


def test_auth_task5_fix2_migration_reopens_only_premature_legacy_claims(
    monkeypatch, tmp_path
):
    server, _ = load_auth_server(monkeypatch, tmp_path)
    auth_data = sys.modules["auth_data"]
    with sqlite3.connect(":memory:") as con:
        con.execute(
            "CREATE TABLE pouzivatelia (id INTEGER PRIMARY KEY, email TEXT NOT NULL)"
        )
        auth_data.migrate_auth_schema(con)
        incomplete_id = con.execute(
            "INSERT INTO pouzivatelia (email) VALUES ('incomplete@example.com')"
        ).lastrowid
        complete_id = con.execute(
            "INSERT INTO pouzivatelia (email) VALUES ('complete@example.com')"
        ).lastrowid
        auth_data.set_password(
            con,
            user_id=complete_id,
            password_hash=auth_data.hash_password("completed migration password"),
            now=7_000.0,
        )
        con.executemany(
            "INSERT INTO auth_legacy_setup_claims (user_id, claimed_at) VALUES (?, ?)",
            [(incomplete_id, 6_999.0), (complete_id, 7_000.0)],
        )
        con.commit()

        auth_data.migrate_auth_schema(con)

        assert con.execute(
            "SELECT user_id FROM auth_legacy_setup_claims ORDER BY user_id"
        ).fetchall() == [(complete_id,)]


def test_auth_task5_fix2_setup_token_cannot_replace_a_committed_password(
    monkeypatch, tmp_path
):
    server, _ = load_auth_server(monkeypatch, tmp_path)
    auth_data = sys.modules["auth_data"]
    now = 7_500.0
    monkeypatch.setattr(server, "AUTH_CLOCK", lambda: now)
    with closing(server.db()) as con:
        user_id = con.execute(
            "INSERT INTO pouzivatelia (email) VALUES ('setup-race@example.com')"
        ).lastrowid
        current = auth_data.create_session(
            con, user_id=user_id, now=now, device_name="Migration session"
        )
        setup_token = auth_data.create_action_token(
            con, email="setup-race@example.com", purpose="setup", now=now
        )

    setup_client = auth_v3_client(server)
    setup_client.cookies.set(server.COOKIE, current)
    assert setup_client.post(
        "/api/auth/password/set",
        headers=AUTH_V3_ORIGIN,
        json={"password": "committed setup password"},
    ).status_code == 200
    stale_setup = auth_v3_client(server).post(
        "/api/auth/password/reset",
        headers=AUTH_V3_ORIGIN,
        json={
            "token": setup_token,
            "purpose": "setup",
            "password": "stale setup takeover",
        },
    )

    assert stale_setup.status_code == 400
    with closing(server.db()) as con:
        assert auth_data.authenticate_password(
            con,
            email="setup-race@example.com",
            password="committed setup password",
        ) == user_id
        assert auth_data.authenticate_password(
            con, email="setup-race@example.com", password="stale setup takeover"
        ) is None


def test_auth_task5_fix2_reset_outbox_survives_restart_and_startup_delivers(
    monkeypatch, tmp_path
):
    server, database = load_auth_server(monkeypatch, tmp_path)
    auth_data = sys.modules["auth_data"]
    seed_password_account(server, auth_data, "restart-reset@example.com")
    with closing(server.db()) as con:
        auth_data.enqueue_password_reset_job(
            con, email="restart-reset@example.com", requested_at=5_000.0
        )

    monkeypatch.setenv("RESEND_API_KEY", "test-only-key")
    delivered = threading.Event()
    calls = []

    def post(url, **kwargs):
        calls.append((url, kwargs))
        delivered.set()
        return ProviderResponse()

    monkeypatch.setitem(sys.modules, "requests", types.SimpleNamespace(post=post))
    sys.modules.pop("server", None)
    sys.modules.pop("auth_data", None)
    restarted = importlib.import_module("server")
    restarted.ENV_FILE = str(tmp_path / "missing.env")
    restarted.AUTH_CLOCK = lambda: 5_001.0

    with FastAPITestClient(restarted.app, base_url="https://uvar.si"):
        assert delivered.wait(2), "startup did not drain the persisted reset job"

    raw_token = action_token_from_message(calls, "heslo")
    with sqlite3.connect(database) as con:
        assert con.execute(
            "SELECT state, attempts FROM auth_password_reset_outbox"
        ).fetchone() == ("sent", 1)
        assert con.execute(
            "SELECT token_hash FROM auth_action_tokens WHERE email=?",
            ("restart-reset@example.com",),
        ).fetchone() == (hashlib.sha256(raw_token.encode()).hexdigest(),)


def test_auth_task5_fix2_reset_outbox_retries_with_one_still_valid_token(
    monkeypatch, tmp_path
):
    server, database = load_auth_server(monkeypatch, tmp_path)
    auth_data = sys.modules["auth_data"]
    seed_password_account(server, auth_data, "retry-reset@example.com")
    with closing(server.db()) as con:
        auth_data.enqueue_password_reset_job(
            con, email="retry-reset@example.com", requested_at=6_000.0
        )
    monkeypatch.setenv("RESEND_API_KEY", "test-only-key")
    calls = []

    def post(url, **kwargs):
        calls.append((url, kwargs))
        if len(calls) == 1:
            raise TimeoutError("first delivery uncertain")
        return ProviderResponse()

    monkeypatch.setitem(sys.modules, "requests", types.SimpleNamespace(post=post))
    now = [6_001.0]
    monkeypatch.setattr(server, "AUTH_CLOCK", lambda: now[0])

    assert server.process_password_reset_outbox_batch("retry-worker", limit=1) == 1
    first_token = action_token_from_message(calls, "heslo")
    with sqlite3.connect(database) as con:
        state, attempts, next_attempt_at = con.execute(
            """SELECT state, attempts, next_attempt_at
               FROM auth_password_reset_outbox"""
        ).fetchone()
        assert (state, attempts) == ("queued", 1)
        assert next_attempt_at > now[0]
        assert con.execute("SELECT COUNT(*) FROM auth_action_tokens").fetchone() == (1,)

    assert server.process_password_reset_outbox_batch("retry-worker", limit=1) == 0
    now[0] = next_attempt_at
    assert server.process_password_reset_outbox_batch("retry-worker", limit=1) == 1
    second_token = action_token_from_message(calls, "heslo")
    assert first_token == second_token
    with sqlite3.connect(database) as con:
        assert con.execute(
            "SELECT state, attempts FROM auth_password_reset_outbox"
        ).fetchone() == ("sent", 2)
        assert con.execute("SELECT token_hash FROM auth_action_tokens").fetchone() == (
            hashlib.sha256(second_token.encode()).hexdigest(),
        )


def test_auth_task5_fix2_shutdown_drains_an_accepted_reset_delivery(
    monkeypatch, tmp_path
):
    server, database = load_auth_server(monkeypatch, tmp_path)
    auth_data = sys.modules["auth_data"]
    seed_password_account(server, auth_data, "shutdown-reset@example.com")
    monkeypatch.setenv("RESEND_API_KEY", "test-only-key")
    provider_entered = threading.Event()
    release_provider = threading.Event()
    request_done = threading.Event()
    allow_shutdown = threading.Event()
    shutdown_done = threading.Event()

    def post(url, **kwargs):
        provider_entered.set()
        if not release_provider.wait(3):
            raise TimeoutError("test did not release shutdown delivery")
        return ProviderResponse()

    monkeypatch.setitem(sys.modules, "requests", types.SimpleNamespace(post=post))

    def host_process():
        with FastAPITestClient(server.app, base_url="https://uvar.si") as client:
            response = client.post(
                "/api/auth/password/request",
                headers=AUTH_V3_ORIGIN,
                json={"email": "shutdown-reset@example.com"},
            )
            assert response.status_code == 200
            request_done.set()
            allow_shutdown.wait(2)
        shutdown_done.set()

    host = threading.Thread(target=host_process)
    host.start()
    try:
        assert request_done.wait(2)
        assert provider_entered.wait(2)
        allow_shutdown.set()
        assert not shutdown_done.wait(0.2)
        release_provider.set()
        assert shutdown_done.wait(2)
    finally:
        allow_shutdown.set()
        release_provider.set()
        host.join(timeout=3)

    with sqlite3.connect(database) as con:
        assert con.execute(
            "SELECT state FROM auth_password_reset_outbox"
        ).fetchone() == ("sent",)


@pytest.mark.parametrize(
    "purpose, configure_password",
    [("reset", True), ("setup", False)],
)
def test_auth_task5_fix3_null_generation_action_tokens_are_never_accepted(
    monkeypatch, tmp_path, purpose, configure_password
):
    server, _ = load_auth_server(monkeypatch, tmp_path)
    auth_data = sys.modules["auth_data"]
    now = 10_000.0
    raw_token = f"pre-upgrade-null-{purpose}-token"
    with closing(server.db()) as con:
        user_id = con.execute(
            "INSERT INTO pouzivatelia (email) VALUES ('null-token@example.com')"
        ).lastrowid
        if configure_password:
            auth_data.set_password(
                con,
                user_id=user_id,
                password_hash=auth_data.hash_password("newer account credential"),
                now=now - 1,
            )
        con.execute(
            """INSERT INTO auth_action_tokens
               (token_hash, email, purpose, pending_password_hash,
                credential_changed_at, expires_at, created_at)
               VALUES (?, ?, ?, NULL, NULL, ?, ?)""",
            (
                hashlib.sha256(raw_token.encode()).hexdigest(),
                "null-token@example.com",
                purpose,
                now + 3_600,
                now - 100,
            ),
        )
        con.commit()

        with pytest.raises(auth_data.ActionTokenInvalid):
            auth_data.consume_action_token(
                con, raw_token=raw_token, purpose=purpose, now=now
            )


def test_auth_task5_fix3_legacy_verify_grants_only_recoverable_password_setup(
    monkeypatch, tmp_path
):
    server, database = load_auth_server(monkeypatch, tmp_path)
    auth_data = sys.modules["auth_data"]
    now = 20_000.0
    monkeypatch.setattr(server, "AUTH_CLOCK", lambda: now)
    raw_magic = "restricted-legacy-migration-token"
    with closing(server.db()) as con:
        user_id = con.execute(
            "INSERT INTO pouzivatelia (email) VALUES ('restricted@example.com')"
        ).lastrowid
        con.execute(
            """INSERT INTO magic_tokens_v2
               (token_hash, email, expires_at, created_at) VALUES (?, ?, ?, ?)""",
            (
                hashlib.sha256(raw_magic.encode()).hexdigest(),
                "restricted@example.com",
                now + 3_600,
                now,
            ),
        )
        con.commit()

    client = auth_v3_client(server)
    verified = client.post(
        "/api/auth/verify",
        headers=AUTH_V3_ORIGIN,
        json={"token": raw_magic},
    )

    assert verified.status_code == 200
    assert verified.json() == {"ok": True, "redirect": "/heslo"}
    assert client.cookies.get(server.COOKIE) is None
    setup_cookie = client.cookies.get(server.SETUP_COOKIE)
    assert setup_cookie
    assert client.get("/api/me").json() == {
        "prihlaseny": False,
        "auth_v3": True,
    }
    assert client.get("/api/auth/sessions").status_code == 401
    password_page = client.get("/heslo")
    assert "token?'/api/auth/password/reset':'/api/auth/password/set'" in password_page.text
    with sqlite3.connect(database) as con:
        assert con.execute("SELECT COUNT(*) FROM sessions_v2").fetchone() == (0,)
        assert con.execute(
            "SELECT COUNT(*) FROM auth_setup_sessions WHERE user_id=?", (user_id,)
        ).fetchone() == (1,)

    completed = client.post(
        "/api/auth/password/set",
        headers=AUTH_V3_ORIGIN,
        json={"password": "recoverable migration password"},
    )
    assert completed.status_code == 200
    assert client.cookies.get(server.COOKIE)
    assert client.cookies.get(server.SETUP_COOKIE) is None
    assert client.get("/api/me").json()["id"] == user_id
    with sqlite3.connect(database) as con:
        assert con.execute(
            "SELECT COUNT(*) FROM auth_setup_sessions WHERE user_id=?", (user_id,)
        ).fetchone() == (0,)


def test_auth_task5_fix3_delayed_outbox_token_ttl_starts_when_job_is_claimed(
    monkeypatch, tmp_path
):
    server, database = load_auth_server(monkeypatch, tmp_path)
    auth_data = sys.modules["auth_data"]
    seed_password_account(server, auth_data, "delayed-reset@example.com")
    with closing(server.db()) as con:
        auth_data.enqueue_password_reset_job(
            con, email="delayed-reset@example.com", requested_at=1_000.0
        )
        delivery = auth_data.claim_password_reset_job(
            con,
            worker_id="delayed-worker",
            now=10_000.0,
            token_secret="test-only-key",
        )

    assert delivery is not None and delivery.raw_token
    with sqlite3.connect(database) as con:
        assert con.execute(
            "SELECT created_at, expires_at FROM auth_action_tokens WHERE token_hash=?",
            (delivery.token_hash,),
        ).fetchone() == (10_000.0, 13_600.0)


def test_auth_task5_fix3_uncertain_delivery_retries_same_valid_token_with_idempotency(
    monkeypatch, tmp_path
):
    server, database = load_auth_server(monkeypatch, tmp_path)
    auth_data = sys.modules["auth_data"]
    seed_password_account(server, auth_data, "uncertain-reset@example.com")
    with closing(server.db()) as con:
        auth_data.enqueue_password_reset_job(
            con, email="uncertain-reset@example.com", requested_at=30_000.0
        )
    monkeypatch.setenv("RESEND_API_KEY", "test-only-key")
    now = [30_001.0]
    monkeypatch.setattr(server, "AUTH_CLOCK", lambda: now[0])
    calls = []

    def post(url, **kwargs):
        calls.append((url, kwargs))
        if len(calls) <= 2:
            raise TimeoutError("provider accepted before connection dropped")
        return ProviderResponse()

    monkeypatch.setitem(sys.modules, "requests", types.SimpleNamespace(post=post))

    assert server.process_password_reset_outbox_batch("uncertain-worker", limit=1) == 1
    first_token = action_token_from_message(calls, "heslo")
    first_key = calls[0][1]["headers"]["Idempotency-Key"]
    with sqlite3.connect(database) as con:
        state, attempts, next_attempt_at, token_hash = con.execute(
            """SELECT state, attempts, next_attempt_at, token_hash
               FROM auth_password_reset_outbox"""
        ).fetchone()
        assert (state, attempts) == ("queued", 1)
        assert next_attempt_at == now[0] + 5
        assert token_hash == hashlib.sha256(first_token.encode()).hexdigest()
        persisted = con.execute(
            """SELECT token_seed, idempotency_key, token_hash
               FROM auth_password_reset_outbox"""
        ).fetchone()
        assert first_token not in repr(persisted)
        assert con.execute(
            "SELECT COUNT(*) FROM auth_action_tokens WHERE token_hash=?", (token_hash,)
        ).fetchone() == (1,)

    assert server.process_password_reset_outbox_batch("uncertain-worker", limit=1) == 0
    assert len(calls) == 1
    now[0] = next_attempt_at
    assert server.process_password_reset_outbox_batch("uncertain-worker", limit=1) == 1
    assert action_token_from_message(calls, "heslo") == first_token
    assert calls[1][1]["headers"]["Idempotency-Key"] == first_key
    with sqlite3.connect(database) as con:
        state, attempts, second_attempt_at = con.execute(
            """SELECT state, attempts, next_attempt_at
               FROM auth_password_reset_outbox"""
        ).fetchone()
        assert (state, attempts) == ("queued", 2)
        assert second_attempt_at == now[0] + 10

    assert server.process_password_reset_outbox_batch("uncertain-worker", limit=1) == 0
    now[0] = second_attempt_at
    assert server.process_password_reset_outbox_batch("uncertain-worker", limit=1) == 1
    assert action_token_from_message(calls, "heslo") == first_token
    assert calls[2][1]["headers"]["Idempotency-Key"] == first_key
    with sqlite3.connect(database) as con:
        assert con.execute(
            "SELECT state, attempts FROM auth_password_reset_outbox"
        ).fetchone() == ("sent", 3)


def test_auth_task5_fix3_lease_wake_and_shutdown_drain_are_bounded(
    monkeypatch, tmp_path
):
    server, database = load_auth_server(monkeypatch, tmp_path)
    auth_data = sys.modules["auth_data"]
    seed_password_account(server, auth_data, "lease-wake@example.com")
    with closing(server.db()) as con:
        auth_data.enqueue_password_reset_job(
            con, email="lease-wake@example.com", requested_at=40_000.0
        )
        delivery = auth_data.claim_password_reset_job(
            con,
            worker_id="crashed-worker",
            now=40_000.0,
            lease_seconds=30,
            token_secret="test-only-key",
        )
    assert delivery is not None

    monkeypatch.setattr(server, "AUTH_CLOCK", lambda: 40_000.0)
    scheduled = []

    def call_later(loop, delay, callback):
        scheduled.append((delay, callback))
        return types.SimpleNamespace(cancel=lambda: None, cancelled=lambda: False)

    monkeypatch.setattr(server, "AUTH_OUTBOX_CALL_LATER", call_later, raising=False)

    async def scenario():
        task = server.ensure_password_reset_worker()
        await task
        await asyncio.sleep(0)
        assert scheduled and scheduled[0][0] == pytest.approx(30.0)

        blocker = asyncio.create_task(asyncio.Event().wait())
        server.AUTH_BACKGROUND_TASKS.add(blocker)
        try:
            drained = await server.drain_password_reset_workers(deadline=0.01)
            assert drained is False
            assert not blocker.done()
        finally:
            blocker.cancel()
            await asyncio.gather(blocker, return_exceptions=True)

    asyncio.run(scenario())
    with sqlite3.connect(database) as con:
        assert con.execute(
            "SELECT state, lease_expires_at FROM auth_password_reset_outbox"
        ).fetchone() == ("running", 40_030.0)


def test_auth_task5_fix4_shutdown_stops_after_the_single_inflight_delivery(
    monkeypatch, tmp_path
):
    server, database = load_auth_server(monkeypatch, tmp_path)
    auth_data = sys.modules["auth_data"]
    email = "bounded-shutdown@example.com"
    seed_password_account(server, auth_data, email)
    now = 50_000.0
    with closing(server.db()) as con:
        for _ in range(8):
            auth_data.enqueue_password_reset_job(
                con, email=email, requested_at=now
            )

    monkeypatch.setenv("RESEND_API_KEY", "test-only-key")
    monkeypatch.setattr(server, "AUTH_CLOCK", lambda: now)
    provider_entered = threading.Event()
    release_provider = threading.Event()
    calls = []

    def post(url, **kwargs):
        calls.append((url, kwargs))
        if len(calls) == 1:
            provider_entered.set()
            if not release_provider.wait(5):
                raise TimeoutError("test did not release the in-flight delivery")
        return ProviderResponse()

    monkeypatch.setitem(sys.modules, "requests", types.SimpleNamespace(post=post))

    def state_counts():
        with sqlite3.connect(database) as con:
            return dict(
                con.execute(
                    "SELECT state, COUNT(*) FROM auth_password_reset_outbox GROUP BY state"
                )
            )

    async def scenario():
        server.AUTH_OUTBOX_SHUTTING_DOWN = False
        worker = server.ensure_password_reset_worker()
        for _ in range(200):
            if provider_entered.is_set():
                break
            await asyncio.sleep(0.01)
        assert provider_entered.is_set()

        server.AUTH_OUTBOX_SHUTTING_DOWN = True
        started = asyncio.get_running_loop().time()
        drained = await server.drain_password_reset_workers(deadline=1.0)
        elapsed = asyncio.get_running_loop().time() - started
        states_at_deadline = state_counts()
        calls_at_deadline = len(calls)

        release_provider.set()
        await asyncio.wait_for(worker, timeout=2)
        await asyncio.sleep(0)
        settled = await server.drain_password_reset_workers(deadline=0.1)
        return drained, elapsed, states_at_deadline, calls_at_deadline, settled

    try:
        (
            drained,
            elapsed,
            states_at_deadline,
            calls_at_deadline,
            settled,
        ) = asyncio.run(scenario())
    finally:
        release_provider.set()

    assert drained is False
    assert 0.8 <= elapsed < 1.5
    assert calls_at_deadline == 1
    assert states_at_deadline == {"queued": 7, "running": 1}
    assert settled is True
    assert len(calls) == 1
    assert 0 < calls[0][1]["timeout"] <= 30
    assert state_counts() == {"queued": 7, "sent": 1}

    server.AUTH_OUTBOX_SHUTTING_DOWN = False
    assert (
        server.process_password_reset_outbox_batch("recovery-worker", limit=1) == 1
    )
    assert state_counts() == {"queued": 6, "sent": 2}


def test_auth_v3_me_capabilities_preserve_passwordless_session_during_ui_rollout(
    monkeypatch, tmp_path
):
    server, _database = load_auth_server(monkeypatch, tmp_path)
    monkeypatch.setenv("UVARSI_AUTH_V3", "1")
    auth_data = sys.modules["auth_data"]
    now = 80_000.0
    monkeypatch.setattr(server, "AUTH_CLOCK", lambda: now)
    with closing(server.db()) as con:
        user_id = con.execute(
            "INSERT INTO pouzivatelia (email) VALUES ('rollout@example.com')"
        ).lastrowid
        session = auth_data.create_session(
            con, user_id=user_id, now=now, device_name="Existing session"
        )
        con.commit()
    client = auth_v3_client(server)
    client.cookies.set(server.COOKIE, session)

    passwordless = client.get("/api/me")
    public = auth_v3_client(server).get("/api/me")

    assert passwordless.status_code == 200
    assert passwordless.json()["prihlaseny"] is True
    assert passwordless.json()["auth_v3"] is True
    assert passwordless.json()["password_configured"] is False
    assert public.json() == {"prihlaseny": False, "auth_v3": True}

    with closing(server.db()) as con:
        auth_data.set_password(
            con,
            user_id=user_id,
            password_hash=auth_data.hash_password("rollout password"),
            now=now,
        )
    configured = client.get("/api/me")
    assert configured.json()["password_configured"] is True
    assert configured.json()["id"] == user_id

    monkeypatch.setenv("UVARSI_AUTH_V3", "0")
    disabled = client.get("/api/me")
    assert disabled.json()["prihlaseny"] is True
    assert disabled.json().get("auth_v3", False) is False
    with closing(server.db()) as con:
        assert con.execute(
            "SELECT revoked_at FROM sessions_v2 WHERE token_hash=?",
            (hashlib.sha256(session.encode()).hexdigest(),),
        ).fetchone()[0] is None


def test_auth_v3_flag_stages_primary_auth_without_breaking_session_or_setup_bridge(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("UVARSI_AUTH_V3", "0")
    server, database = load_auth_server(monkeypatch, tmp_path)
    auth_data = sys.modules["auth_data"]
    now = 90_000.0
    monkeypatch.setattr(server, "AUTH_CLOCK", lambda: now)
    raw_magic = "task-nine-legacy-setup"
    with closing(server.db()) as con:
        user_id = con.execute(
            "INSERT INTO pouzivatelia (email) VALUES ('stage@example.com')"
        ).lastrowid
        session = auth_data.create_session(
            con, user_id=user_id, now=now, device_name="Existing device"
        )
        con.execute(
            """INSERT INTO magic_tokens_v2
               (token_hash, email, expires_at, created_at) VALUES (?, ?, ?, ?)""",
            (
                hashlib.sha256(raw_magic.encode()).hexdigest(),
                "stage@example.com",
                now + 3_600,
                now,
            ),
        )
        con.commit()

    existing = FastAPITestClient(server.app, base_url="https://uvar.si")
    existing.cookies.set(server.COOKIE, session)
    me = existing.get("/api/me")
    hidden_login = existing.post(
        "/api/auth/login", headers=AUTH_V3_ORIGIN, content="{"
    )
    hidden_sessions = existing.get("/api/auth/sessions")
    hidden_passkey = existing.post(
        "/api/auth/passkey/login/options",
        headers=AUTH_V3_ORIGIN,
        json={"email": "stage@example.com"},
    )

    assert me.status_code == 200
    assert me.json()["id"] == user_id
    assert "auth_v3" not in me.json()
    assert "password_configured" not in me.json()
    assert hidden_login.status_code == 404
    assert hidden_sessions.status_code == 404
    assert hidden_passkey.status_code == 404

    setup = FastAPITestClient(server.app, base_url="https://uvar.si")
    verified = setup.post(
        "/api/auth/verify",
        headers=AUTH_V3_ORIGIN,
        json={"token": raw_magic},
    )
    configured = setup.post(
        "/api/auth/password/set",
        headers=AUTH_V3_ORIGIN,
        json={"password": "staged migration password"},
    )

    assert verified.status_code == 200
    assert verified.json()["redirect"] == "/heslo"
    assert configured.status_code == 200
    with sqlite3.connect(database) as con:
        assert con.execute(
            "SELECT COUNT(*) FROM auth_legacy_setup_claims WHERE user_id=?",
            (user_id,),
        ).fetchone() == (1,)

    monkeypatch.setenv("UVARSI_AUTH_V3", "1")
    password_login = FastAPITestClient(
        server.app, base_url="https://uvar.si"
    ).post(
        "/api/auth/login",
        headers=AUTH_V3_ORIGIN,
        json={
            "email": "stage@example.com",
            "password": "staged migration password",
        },
    )
    assert password_login.status_code == 200
    enabled = FastAPITestClient(server.app, base_url="https://uvar.si")
    enabled.cookies.set(server.COOKIE, password_login.cookies.get(server.COOKIE))
    assert enabled.get("/api/auth/sessions").status_code == 200
    assert enabled.get("/api/auth/passkeys").status_code == 200
