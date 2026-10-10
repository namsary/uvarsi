import re

import pytest

from app.legal_pages import LEGAL_SLUGS, render_legal_page

# Review B1/T7: odkazy v hlavičke, kontakte a pätičke právnych stránok mali
# 15-17 px na výšku a 21-30 px na šírku; mobilný zákazník do nich netrafí.
CIELE = ("footer a", ".meta a", ".brand")


def _pravidla(html):
    css = re.search(r"<style>(.*?)</style>", html, re.S).group(1)
    return re.findall(r"([^{}]+)\{([^{}]*)\}", css)


def _velkost(pravidla, selektor, vlastnost):
    hodnoty = []
    for selektory, telo in pravidla:
        if selektor in [s.strip() for s in selektory.split(",")]:
            m = re.search(rf"(?:^|;)\s*{vlastnost}\s*:\s*(\d+)px", telo)
            if m:
                hodnoty.append(int(m.group(1)))
    return max(hodnoty, default=0)


@pytest.mark.parametrize("slug", sorted(LEGAL_SLUGS))
@pytest.mark.parametrize("selektor", CIELE)
def test_odkazy_pravnych_stranok_maju_dotykovu_plochu_44px(slug, selektor):
    pravidla = _pravidla(render_legal_page(slug))

    assert _velkost(pravidla, selektor, "min-height") >= 44
    assert _velkost(pravidla, selektor, "min-width") >= 44
