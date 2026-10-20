"""Kontakt na podporu sa mení na jednom mieste: OPERATOR.support_email.

Python stránky ho berú priamo z `operator_profile.py`, JavaScript appky aj
profilového modulu z konštanty SUPPORT_EMAIL v app.html. Statické HTML bez
JavaScriptu (pätička appky, landing) musí mať tú istú adresu — test zlyhá a
vymenuje každé miesto, ktoré po zmene ostalo staré.
"""
import re
from pathlib import Path

from app.operator_profile import OPERATOR

ROOT = Path(__file__).resolve().parents[1]
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[a-z]{2,}")


PLACEHOLDERS = {"tvoj@email.sk"}  # vzor v políčku e-mailu, nie kontakt


def _emails(text):
    return set(EMAIL.findall(text)) - PLACEHOLDERS


def test_app_has_one_js_constant_equal_to_the_operator_profile():
    html = (ROOT / "app/static/app.html").read_text(encoding="utf-8")
    assert f"const SUPPORT_EMAIL = '{OPERATOR.support_email}';" in html
    assert html.count(OPERATOR.support_email) == 2, "konštanta + statická pätička"
    assert f'<a href="mailto:{OPERATOR.support_email}">Kontakt</a></footer>' in html
    assert _emails(html) == {OPERATOR.support_email}
    assert "pomoc@uvar.si" not in html


def test_profile_module_uses_the_app_constant_only():
    [module] = (ROOT / "app/static").glob("subscription-profile.*.js")
    source = module.read_text(encoding="utf-8")
    assert _emails(source) == set()
    assert "SUPPORT_EMAIL" in source


def test_static_landing_copies_match_the_operator_profile():
    stale = _emails((ROOT / "index.html").read_text(encoding="utf-8")) - {
        OPERATOR.support_email
    }
    assert not stale, f"index.html má inú adresu podpory: {sorted(stale)}"


def test_legal_pages_take_the_address_from_the_operator_profile():
    source = (ROOT / "app/legal_pages.py").read_text(encoding="utf-8")
    assert OPERATOR.support_email not in source
    assert "SUPPORT_EMAIL = OPERATOR.support_email" in source
