"""UX revízia pred spustením platieb: prístupnosť frekvencie, kontrast a dotyk."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP = (ROOT / "app" / "static" / "app.html").read_text(encoding="utf-8")
LANDING = (ROOT / "index.html").read_text(encoding="utf-8")


def _luminance(hex_color):
    channels = [int(hex_color[i:i + 2], 16) / 255 for i in (0, 2, 4)]
    linear = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
              for c in channels]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def _contrast(foreground, background):
    high, low = sorted((_luminance(foreground), _luminance(background)), reverse=True)
    return (high + 0.05) / (low + 0.05)


def _token(page, name):
    match = re.search(rf"--{name}:#([0-9A-Fa-f]{{6}})", page)
    assert match, name
    return match.group(1)


def test_success_green_has_at_least_4_5_to_1_on_every_light_background():
    for page in (APP, LANDING):
        green = _token(page, "green")
        for background in ("FFFFFF", _token(page, "paper"), "EAF3EC", "EFF7F1",
                           "EAF4ED", "F1F7F3", "FFFBEB"):
            assert _contrast(green, background) >= 4.5, (green, background)


def test_frequency_is_a_keyboard_operable_radio_group():
    start = APP.index("function viewOnboarding() ")
    onboarding = APP[start:APP.index("\n}\n", start)]
    assert 'id="c-frek" role="radiogroup" aria-labelledby="lbl-frek"' in onboarding
    assert '<label id="lbl-frek">' in onboarding
    assert '<button type="button" role="radio" class="chip' in onboarding
    assert "aria-checked=" in onboarding and "tabindex=" in onboarding
    assert '<div class="chip${' not in onboarding
    assert "onkeydown" in onboarding
    for key in ("ArrowRight", "ArrowLeft", "ArrowDown", "ArrowUp"):
        assert key in onboarding
    assert "setAttribute('aria-checked'" in onboarding


def test_savings_amount_never_wraps():
    assert re.search(r"\.save b\{[^}]*white-space:nowrap", APP)


def test_landing_footer_links_are_44px_tall_on_phones():
    mobile = LANDING.split("@media(max-width:560px){", 1)[1].split("}@media", 1)[0]
    assert ".foot-meta a{display:inline-flex;align-items:center;min-height:44px}" in mobile
