import re
from pathlib import Path


LANDING = Path("index.html")
APP = Path("app/static/app.html")


def _html(path):
    return path.read_text(encoding="utf-8")


def _rule(html, selector):
    match = re.search(rf"{re.escape(selector)}\s*\{{([^}}]+)\}}", html)
    assert match, f"missing CSS rule: {selector}"
    return match.group(1)


def _tokens(html):
    declarations = _rule(html, ":root")
    return dict(
        re.findall(r"(--[a-z0-9-]+)\s*:\s*([^;]+)", declarations, re.I)
    )


def test_app_uses_the_landing_brand_tokens_and_type_hierarchy():
    """The authenticated product must still look unmistakably like Uvar.si."""
    landing = _html(LANDING)
    app = _html(APP)
    canonical = _tokens(landing)
    product = _tokens(app)

    shared_tokens = (
        "--paper",
        "--paper-2",
        "--surface",
        "--ink",
        "--ink-soft",
        "--yellow",
        "--red",
        "--green",
        "--green-deep",
        "--line",
        "--radius-sm",
        "--radius-lg",
        "--shadow-soft",
        "--shadow-card",
    )
    assert {name: product.get(name) for name in shared_tokens} == {
        name: canonical[name] for name in shared_tokens
    }
    assert product["--border"] == "var(--line)"
    assert product["--radius-card"] == "var(--radius-sm)"

    display = _rule(app, ".display")
    assert 'font-family:"Manrope",system-ui,sans-serif' in display
    assert "font-weight:800" in display
    assert 'font-family:"Anton"' not in display
    assert "text-transform:uppercase" not in display

    for data_accent in (".brand", ".meal-d", ".save b"):
        assert 'font-family:"Anton",sans-serif' in _rule(app, data_accent)


def test_app_primary_controls_and_surfaces_follow_the_landing_geometry():
    app = _html(APP)

    button = _rule(app, ".btn")
    assert "border-radius:999px" in button
    assert "background:var(--green-deep)" in button
    assert "color:#fff" in button
    assert "box-shadow:0 3px 0" not in button

    card = _rule(app, ".card")
    assert "background:var(--surface)" in card
    assert "border:1px solid var(--border)" in card
    assert "border-radius:var(--radius-card)" in card
    assert "box-shadow:var(--shadow-card)" in card

    assert "background:var(--paper)" in _rule(app, "body")
    assert "background:var(--yellow)" in _rule(app, ".save")
    assert "background:var(--yellow)" in _rule(app, ".chip.on")
    assert "transform:none" in _rule(app, ".btn:disabled:hover")


def test_app_keeps_its_task_shell_and_accessibility_contracts():
    app = _html(APP)

    wrap = _rule(app, ".wrap")
    assert "max-width:680px" in wrap
    assert "env(safe-area-inset-top)" in app
    assert "env(safe-area-inset-bottom)" in app
    assert "min-height:64px" in _rule(app, "nav button")
    assert "outline:3px solid var(--accent)" in _rule(app, ":focus-visible")
    assert ".btn:disabled" in app
    assert ".plan-skeleton" in app
    assert "@media (prefers-reduced-motion:reduce)" in app


def test_plan_failures_keep_a_semantic_danger_eyebrow():
    app = _html(APP)
    failures = re.findall(
        r'<span class="([^"]*eyebrow[^"]*)">Plán sa nepodaril</span>', app
    )

    assert failures
    assert all("eyebrow--danger" in classes for classes in failures)
    assert "color:var(--red)" in _rule(app, ".eyebrow--danger")
