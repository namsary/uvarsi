import re
from pathlib import Path


APP = Path("app/static/app.html")


def app_html():
    return APP.read_text(encoding="utf-8")


def test_app_uses_a_modern_semantic_visual_system():
    html = app_html()

    for token in (
        "--canvas:",
        "--surface:",
        "--surface-soft:",
        "--border:",
        "--radius-card:",
        "--shadow-card:",
    ):
        assert token in html

    assert ".card{" in html
    card = re.search(r"\.card\{([^}]+)\}", html)
    assert card
    assert "border:1px solid var(--border)" in card.group(1)
    assert "border-radius:var(--radius-card)" in card.group(1)
    assert "box-shadow:3px 3px 0" not in html
    assert "-webkit-text-stroke" not in html


def test_primary_navigation_uses_accessible_svg_icons_not_emoji():
    html = app_html()
    nav = re.search(r'<nav id="nav".*?</nav>', html, re.S)
    assert nav

    markup = nav.group(0)
    assert 'aria-label="Hlavná navigácia"' in markup
    assert markup.count('<svg class="nav-icon" aria-hidden="true">') == 4
    assert markup.count("<use href=\"#i-") == 4
    assert not any(symbol in markup for symbol in ("🍽️", "🛒", "🥫", "⚙️"))
    assert "min-height:64px" in html
    assert "aria-current" in html


def test_loading_state_is_a_skeleton_and_respects_reduced_motion():
    html = app_html()

    assert 'class="plan-skeleton"' in html
    assert html.count('class="skeleton-line') >= 3
    assert "@media (prefers-reduced-motion:reduce)" in html
    assert ".meal-chevron" in html
    assert 'class="meal-chevron" aria-hidden="true"' in html


def test_mobile_shell_accounts_for_safe_areas_and_wider_screens():
    html = app_html()

    assert "env(safe-area-inset-bottom)" in html
    assert "env(safe-area-inset-top)" in html
    assert "@media (min-width:700px)" in html
    assert "touch-action:manipulation" in html
