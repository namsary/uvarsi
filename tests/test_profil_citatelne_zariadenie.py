"""Profil ukazuje „Chrome · Windows", nie surový user-agent."""
import importlib

import pytest
from fastapi.testclient import TestClient

from test_auth import load_auth_server


CHROME_WIN = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36")
SAFARI_IPHONE = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_6 like Mac OS X) "
                 "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.6 "
                 "Mobile/15E148 Safari/604.1")


@pytest.mark.parametrize(("user_agent", "label"), [
    (CHROME_WIN, "Chrome · Windows"),
    (SAFARI_IPHONE, "Safari · iPhone"),
    ("Mozilla/5.0 (Macintosh; Intel Mac OS X 14_6) AppleWebKit/605.1.15 "
     "(KHTML, like Gecko) Version/17.6 Safari/605.1.15", "Safari · Mac"),
    ("Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:131.0) Gecko/20100101 "
     "Firefox/131.0", "Firefox · Windows"),
    ("Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, "
     "like Gecko) Chrome/129.0.0.0 Mobile Safari/537.36", "Chrome · Android"),
    ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, "
     "like Gecko) Chrome/129.0.0.0 Safari/537.36 Edg/129.0.0.0", "Edge · Windows"),
    ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_6 like Mac OS X) AppleWebKit/605.1.15 "
     "(KHTML, like Gecko) CriOS/129.0 Mobile/15E148 Safari/604.1", "Chrome · iPhone"),
    # Staršie relácie majú user-agent orezaný na 80 znakov.
    (CHROME_WIN[:80], "Prehliadač · Windows"),
    (SAFARI_IPHONE[:80], "Safari · iPhone"),
    ("Firefox na notebooku", "Firefox na notebooku"),
    ("Zariadenie s Passkey", "Zariadenie s Passkey"),
])
def test_device_label_is_human(monkeypatch, tmp_path, user_agent, label):
    server, _ = load_auth_server(monkeypatch, tmp_path)
    assert server.citatelne_zariadenie(user_agent) == label


def test_new_and_legacy_sessions_are_listed_with_readable_names(monkeypatch, tmp_path):
    server, _ = load_auth_server(monkeypatch, tmp_path)
    monkeypatch.setenv("UVARSI_AUTH_V3", "1")
    auth_data = importlib.import_module("auth_data")
    now = server.AUTH_CLOCK()
    with server.db() as con:
        user_id = con.execute(
            "INSERT INTO pouzivatelia (email) VALUES ('device@example.com')"
        ).lastrowid
        current = auth_data.create_session(
            con, user_id=user_id, now=now, device_name=CHROME_WIN[:80]
        )
        con.commit()
    client = TestClient(server.app)
    client.cookies.set(server.COOKIE, current)

    response = client.get("/api/auth/sessions")

    assert response.status_code == 200
    assert [s["device_name"] for s in response.json()["sessions"]] == [
        "Prehliadač · Windows"
    ]

    class Req:
        headers = {"user-agent": SAFARI_IPHONE}

    assert server.auth_device_name(Req(), {}, "Prihlásené zariadenie") == "Safari · iPhone"
    assert server.auth_device_name(Req(), {"device_name": "Moje PC"}, "x") == "Moje PC"
