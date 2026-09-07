import pytest
from pathlib import Path

from app.config import legal_version, public_base_url, release_id


def test_public_url_requires_explicit_value(monkeypatch):
    monkeypatch.delenv("UVARSI_URL", raising=False)

    with pytest.raises(RuntimeError, match="UVARSI_URL"):
        public_base_url()


def test_public_url_is_exact_canonical_https(monkeypatch):
    monkeypatch.setenv("UVARSI_URL", "https://uvar.si/")

    assert public_base_url() == "https://uvar.si"


def test_release_id_reads_version_file(tmp_path, monkeypatch):
    path = tmp_path / "VERSION"
    path.write_text("2026.08.18.1\n", encoding="utf-8")
    monkeypatch.setenv("UVARSI_VERSION_FILE", str(path))

    assert release_id() == "2026.08.18.1"


def test_legal_version_is_code_owned_not_environment_controlled(monkeypatch):
    monkeypatch.setenv("UVARSI_LEGAL_VERSION", "attacker-controlled")

    assert legal_version() == "2026-09-07-v1"


@pytest.mark.parametrize(
    "value",
    ["http://uvar.si", "https://example.com", "https://uvar.si/app"],
)
def test_public_url_rejects_any_noncanonical_variant(monkeypatch, value):
    monkeypatch.setenv("UVARSI_URL", value)

    with pytest.raises(RuntimeError, match="presne https://uvar.si"):
        public_base_url()


def test_backend_uses_required_public_url_configuration():
    source = Path("app/server.py").read_text(encoding="utf-8")

    assert "from config import public_base_url" in source
    assert "BASE_URL = public_base_url()" in source
