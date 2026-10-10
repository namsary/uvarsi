"""Formulár zakladajúcej ponuky nesmie hlásiť úspech, ktorý nenastal (N1)."""
import re
from pathlib import Path


def _submit_handler():
    page = Path("index.html").read_text(encoding="utf-8")
    start = page.index("form.addEventListener('submit'")
    return page[start:page.index("})();", start)]


def test_uspech_sa_neukazuje_casovacom_bez_ohladu_na_vysledok():
    handler = _submit_handler()

    assert "setTimeout" not in handler, "úspech nesmie prísť z pevného časovača"
    assert "preventDefault" in handler
    assert "fetch(" in handler


def test_uspech_sa_ukaze_az_po_odoslani_a_chyba_ma_viditelny_text():
    handler = _submit_handler()
    ok, fail = re.search(r"\.then\(function\(\)\{(.*?)\},function\(\)\{(.*?)\}\)", handler, re.S).groups()

    assert "successState.style.display='block'" in ok.replace(" ", "")
    assert "successState" not in fail
    page = Path("index.html").read_text(encoding="utf-8")
    err = re.search(r'<p[^>]+id="formErr"[^>]*>(.*?)</p>', page, re.S)
    assert err and 'role="alert"' in err.group(0)
    assert "nepodarilo" in err.group(1).lower()
    assert "formErr" in fail
