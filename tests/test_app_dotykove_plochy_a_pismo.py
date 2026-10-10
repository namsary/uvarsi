import re
from pathlib import Path

APP = Path(__file__).resolve().parents[1] / "app" / "static" / "app.html"
HTML = APP.read_text(encoding="utf-8")
CSS = re.search(r"<style>(.*?)</style>", HTML, re.S).group(1)
PRAVIDLA = re.findall(r"([^{}]+)\{([^{}]*)\}", CSS)

# Review B1/B2 (T8, T9, T10, F7, F8, F9, F10, F11, A3): malé ovládacie prvky a
# písmo v appke. Každý test meria pravidlo, ktoré prehliadač naozaj použije.


def _telo(selektor):
    return ";".join(
        telo for selektory, telo in PRAVIDLA
        if selektor in [s.strip() for s in selektory.split(",")]
    )


def _rem(selektor, vlastnost="font-size"):
    telo = _telo(selektor)
    m = re.search(rf"(?:^|;){vlastnost}\s*:\s*(?:[\w-]+\s+)?(\d*\.?\d+)rem", telo)
    if not m and vlastnost == "font-size":
        m = re.search(r"(?:^|;)font\s*:[^;]*?(\d*\.?\d+)rem", telo)
    return float(m.group(1)) if m else None


def test_odkazy_prihlasenia_maju_dotykovu_plochu_44px():
    m = re.search(r"min-height\s*:\s*(\d+)px", _telo(".auth-link"))
    assert m and int(m.group(1)) >= 44


def test_paticka_appky_ma_dotykove_odkazy_a_citatelne_pismo():
    telo = _telo(".legal-footer a")
    m = re.search(r"(?:min-height|line-height)\s*:\s*(\d+)px", telo)
    assert m and int(m.group(1)) >= 44
    assert re.search(r"display\s*:\s*inline-(block|flex)", telo)
    assert _rem(".legal-footer") >= 0.8


def test_hlavne_ovladanie_a_udaje_maju_aspon_12px():
    assert _rem("nav button", "font") >= 0.75
    assert _rem(".diet-premium", "font") >= 0.72
    assert _rem(".tag") >= 0.75
    assert _rem(".prov", "font") >= 0.75


def test_email_vo_vstupe_starej_prihlasovacej_obrazovky_ma_label():
    assert '<label for="em">E-mail</label><input id="em"' in HTML
