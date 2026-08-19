from pathlib import Path

import pytest

from hetzner.refresh_blocek import landing_data_output_path


def test_refresh_writes_validated_json_instead_of_landing_html():
    source = Path("hetzner/refresh_blocek.py").read_text(encoding="utf-8")

    assert "write_landing_data_atomic" in source
    assert "validate_landing_data" in source
    assert 're.sub(r"<!-- RCPT:START' not in source
    assert 'open(path, "w", encoding="utf-8").write(new)' not in source


def test_refresh_has_no_flyer_discovery_or_download_dependencies():
    source = Path("hetzner/refresh_blocek.py").read_text(encoding="utf-8")

    assert "requests" not in source
    assert "kupino" not in source
    assert "mletaky" not in source
    assert "PIL" not in source


def test_refresh_rejects_any_output_path_except_the_landing_json():
    assert landing_data_output_path([]) == Path("/var/lib/uvarsi/landing_data.json")
    assert landing_data_output_path(["/var/lib/uvarsi/landing_data.json"]) == Path("/var/lib/uvarsi/landing_data.json")

    with pytest.raises(SystemExit, match="landing_data.json"):
        landing_data_output_path(["/var/www/uvarsi/index.html"])


def test_index_hides_receipt_and_savings_claims_until_current_data_arrives():
    html = Path("index.html").read_text(encoding="utf-8")

    assert 'id="landing-data" aria-live="polite" hidden' in html
    assert 'id="landing-model" hidden' in html
    assert 'fetch("/api/public/landing")' in html
    assert "Reálnu úsporu vidíš priamo na bločku vyššie" not in html
    assert "Za rok to vie byť pokojne pár stoviek eur" not in html
