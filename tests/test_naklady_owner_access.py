from fastapi.testclient import TestClient
import pytest

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


@pytest.mark.parametrize("configured_value", ["", "   ", " , \t, , "])
def test_detail_naklady_fails_closed_for_effectively_empty_admin_configuration(
    monkeypatch, tmp_path, configured_value,
):
    monkeypatch.setenv("UVARSI_ADMIN_EMAILS", configured_value)
    server = load_server(monkeypatch, tmp_path, [])
    client = authenticated_client(server, email="owner@example.test")

    response = client.get("/api/naklady")

    assert response.status_code == 403


def test_detail_naklady_reads_allowlist_from_server_env_file(monkeypatch, tmp_path):
    monkeypatch.delenv("UVARSI_ADMIN_EMAILS", raising=False)
    server = load_server(monkeypatch, tmp_path, [])
    env_file = tmp_path / "uvarsi.env"
    env_file.write_text("UVARSI_ADMIN_EMAILS=owner@example.test\n", encoding="utf-8")
    monkeypatch.setattr(server, "ENV_FILE", str(env_file))
    client = authenticated_client(server, email="owner@example.test")

    response = client.get("/api/naklady")

    assert response.status_code == 200


def test_detail_naklady_does_not_treat_prefixed_env_name_as_allowlist(monkeypatch, tmp_path):
    monkeypatch.delenv("UVARSI_ADMIN_EMAILS", raising=False)
    server = load_server(monkeypatch, tmp_path, [])
    env_file = tmp_path / "uvarsi.env"
    env_file.write_text("UVARSI_ADMIN_EMAILS_OLD=attacker@example.test\n", encoding="utf-8")
    monkeypatch.setattr(server, "ENV_FILE", str(env_file))
    client = authenticated_client(server, email="attacker@example.test")

    response = client.get("/api/naklady")

    assert response.status_code == 403


def test_detail_naklady_reads_exported_allowlist_from_server_env_file(monkeypatch, tmp_path):
    monkeypatch.delenv("UVARSI_ADMIN_EMAILS", raising=False)
    server = load_server(monkeypatch, tmp_path, [])
    env_file = tmp_path / "uvarsi.env"
    env_file.write_text("export UVARSI_ADMIN_EMAILS=owner@example.test\n", encoding="utf-8")
    monkeypatch.setattr(server, "ENV_FILE", str(env_file))
    client = authenticated_client(server, email="owner@example.test")

    response = client.get("/api/naklady")

    assert response.status_code == 200


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
