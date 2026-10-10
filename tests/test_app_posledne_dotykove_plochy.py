import re
from pathlib import Path

APP = Path(__file__).resolve().parents[1] / "app" / "static" / "app.html"
HTML = APP.read_text(encoding="utf-8")
CSS = re.search(r"<style>(.*?)</style>", HTML, re.S).group(1)
PRAVIDLA = re.findall(r"([^{}]+)\{([^{}]*)\}", CSS)

# Posledné dotykové plochy pod 44 px v appke: odkaz VOP v pätičke (25 px
# široký), tlačidlo „Odhlásiť toto zariadenie“ (42 px vysoké) a odkaz na
# podporu v Profile (19 px vysoký, text v profilovom module).


def _telo(selektor):
    return ";".join(
        telo for selektory, telo in PRAVIDLA
        if selektor in [s.strip() for s in selektory.split(",")]
    )


def _px(selektor, vlastnost):
    m = re.search(rf"(?:^|;){vlastnost}\s*:\s*(\d+)px", _telo(selektor))
    return int(m.group(1)) if m else 0


def test_odkazy_v_paticke_su_aj_siroke_aspon_44px():
    # „VOP“ má ~25 px textu; vodorovný padding ho dorovná nad 44 px.
    m = re.search(r"(?:^|;)padding\s*:\s*0\s+(\d+)px", _telo(".legal-footer a"))
    assert m and 25 + 2 * int(m.group(1)) >= 44


def test_tlacidlo_odhlasenia_zariadenia_je_vysoke_aspon_44px():
    # Bez vlastného min-height platí základných 48 px z `.btn`.
    vlastne = _px(".security-row .btn", "min-height")
    assert vlastne == 0 or vlastne >= 44
    assert _px(".btn", "min-height") >= 44


def test_odkaz_na_podporu_v_profile_ma_dotykovu_plochu_44px():
    telo = _telo(".service-card a")
    assert re.search(r"display\s*:\s*inline-(block|flex)", telo)
    assert _px(".service-card a", "line-height") >= 44
