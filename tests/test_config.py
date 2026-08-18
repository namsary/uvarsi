import pytest

from app.config import public_base_url, release_id


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
