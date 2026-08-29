"""Schválený moderný vizuálny a konverzný kontrakt landingu."""
import re
from pathlib import Path


def html():
    return Path("index.html").read_text(encoding="utf-8")


def test_primary_action_opens_the_working_app_and_old_waitlist_story_is_gone():
    page = html()

    assert re.search(r'<a[^>]+class="[^"]*btn-primary[^"]*"[^>]+href="/app"', page)
    for stale in ("Ešte nie sme live", "Spúšťame po mestách", "Ktorý plán by si si vybral?"):
        assert stale not in page
    assert "Bez karty" in page
    assert "Premium platby sú zatiaľ vypnuté" in page


def test_receipt_is_the_single_signature_animation_and_respects_reduced_motion():
    page = html()

    animation = re.search(r"@keyframes\s+receipt-enter\s*\{(.*?)\}", page, re.S)
    assert animation
    assert "transform" in animation.group(1)
    assert "opacity" in animation.group(1)
    assert "prefers-reduced-motion:reduce" in page.replace(" ", "")
    reduced = page.split("@media(prefers-reduced-motion:reduce)", 1)[1]
    assert "animation:none" in reduced.replace(" ", "")


def test_design_uses_modern_surface_tokens_and_large_touch_targets():
    page = html()

    for token in ("--surface:", "--radius-lg:", "--shadow-soft:"):
        assert token in page
    button_rule = re.search(r"\.btn\s*\{(.*?)\}", page, re.S)
    assert button_rule
    assert re.search(r"min-height:\s*(4[4-9]|[5-9]\d)px", button_rule.group(1))
    assert "border-radius:999px" in page


def test_secondary_touch_targets_and_reduced_motion_are_accessible():
    page = html()

    for selector in (".nav-link", ".modal-close", ".faq summary"):
        rule = re.search(re.escape(selector) + r"\s*\{(.*?)\}", page, re.S)
        assert rule, selector
        assert re.search(r"min-height:\s*44px", rule.group(1)), selector
    reduced = page.split("@media(prefers-reduced-motion:reduce)", 1)[1]
    compact = reduced.replace(" ", "").replace("\n", "")
    assert "scroll-behavior:auto" in compact
    assert ".btn:hover{transform:none}" in compact


def test_waitlist_modal_traps_and_restores_keyboard_focus():
    page = html()

    assert "lastTrigger=trigger" in page.replace(" ", "")
    assert "focusableElements" in page
    assert "e.key==='Tab'" in page
    assert "lastTrigger.focus()" in page.replace(" ", "")
    assert 'successState.setAttribute(\'tabindex\',\'-1\')' in page
    assert "successState.focus()" in page


def test_mobile_hero_has_no_old_receipt_rotation():
    page = html()

    assert "@media(max-width:900px)" in page.replace(" ", "")
    mobile = page.split("@media(max-width:900px)", 1)[1]
    assert ".hero" in mobile
    assert "grid-template-columns:1fr" in mobile.replace(" ", "")
    assert "rotate(-1.6deg)" not in page


def test_founding_offer_matches_the_approved_value_story():
    page = html()

    assert "39 €" in page
    assert "jednorazovo" in page.lower()
    assert "Prvých 250" in page
