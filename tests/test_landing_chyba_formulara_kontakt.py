"""Chyba odoslania čakacej listiny ukáže zákazníkovi aj cestu ďalej (kontakt).

Chybová hláška je vo vnútri modálneho okna, ktoré prekrýva pätičku s kontaktom.
Zákazník, ktorému odoslanie zlyhá, preto nemal v okne žiadnu cestu k podpore.
"""
import re
from pathlib import Path

KONTAKT = "pumaragency@gmail.com"


def _chyba():
    page = Path("index.html").read_text(encoding="utf-8")
    m = re.search(r'<p id="formErr"[^>]*>(.*?)</p>', page, re.S)
    assert m, "chýba #formErr"
    return m.group(1)


def test_chybova_hlaska_formulara_odkazuje_na_kontakt_mailtom():
    chyba = _chyba()

    assert f'href="mailto:{KONTAKT}"' in chyba
    assert "skús znova" in chyba.lower()


def test_chybova_hlaska_nema_vymyslene_udaje():
    text = re.sub(r"<[^>]+>", "", _chyba())

    assert KONTAKT in text
    assert not re.search(r"\d{3}\s?\d{3}\s?\d{3}", text)
