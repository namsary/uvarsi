from fastapi.testclient import TestClient

from tests.test_server import insert_hashed_session, load_server


def authenticated_client(server, *, email: str, token: str = "owner-session"):
    with server.db() as con:
        con.execute("INSERT INTO pouzivatelia (id, email) VALUES (1, ?)", (email,))
        insert_hashed_session(server, con, token, 1)
        con.commit()
    client = TestClient(server.app)
    client.cookies.set(server.COOKIE, token)
    return client


def test_detail_naklady_rejects_anonymous_client(monkeypatch, tmp_path):
    monkeypatch.setenv("UVARSI_ADMIN_EMAILS", "owner@example.test")
    server = load_server(monkeypatch, tmp_path, [])

    response = TestClient(server.app).get("/api/naklady")

    assert response.status_code == 401
    assert "owner@example.test" not in response.text
    assert "UVARSI_ADMIN_EMAILS" not in response.text


def test_detail_naklady_rejects_authenticated_non_owner_without_leaking_allowlist(monkeypatch, tmp_path):
    monkeypatch.setenv("UVARSI_ADMIN_EMAILS", "owner@example.test")
    server = load_server(monkeypatch, tmp_path, [])
    client = authenticated_client(server, email="visitor@example.test")

    response = client.get("/api/naklady")

    assert response.status_code == 403
    assert "owner@example.test" not in response.text
    assert "UVARSI_ADMIN_EMAILS" not in response.text


def test_detail_naklady_fails_closed_without_admin_configuration(monkeypatch, tmp_path):
    monkeypatch.delenv("UVARSI_ADMIN_EMAILS", raising=False)
    server = load_server(monkeypatch, tmp_path, [])
    client = authenticated_client(server, email="owner@example.test")

    response = client.get("/api/naklady")

    assert response.status_code == 403
    assert "owner@example.test" not in response.text
    assert "UVARSI_ADMIN_EMAILS" not in response.text


def test_detail_naklady_accepts_normalized_owner_from_comma_separated_allowlist(monkeypatch, tmp_path):
    monkeypatch.setenv(
        "UVARSI_ADMIN_EMAILS",
        "  OTHER@example.test, , OWNER@EXAMPLE.TEST  ,   ",
    )
    server = load_server(monkeypatch, tmp_path, [])
    client = authenticated_client(server, email="  owner@example.test  ")

    response = client.get("/api/naklady")

    assert response.status_code == 200
    body = response.json()
    assert {"behy", "posledne", "predpocet"} <= body.keys()


def test_health_stays_public_and_keeps_only_its_existing_summary(monkeypatch, tmp_path):
    monkeypatch.setenv("UVARSI_ADMIN_EMAILS", "owner@example.test")
    server = load_server(monkeypatch, tmp_path, [])

    response = TestClient(server.app).get("/api/health")

    assert response.status_code == 200
    assert "owner@example.test" not in response.text
    assert "UVARSI_ADMIN_EMAILS" not in response.text
