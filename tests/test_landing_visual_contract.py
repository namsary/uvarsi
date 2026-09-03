"""Schválený moderný vizuálny a konverzný kontrakt landingu."""
import re
from pathlib import Path


def html():
    return Path("index.html").read_text(encoding="utf-8")


def legal_terms():
    return Path("docs/legal/01_VOP_NAVRH.md").read_text(encoding="utf-8")


def pricing_cards(page):
    section = page.split('<section class="plans-band"', 1)[1].split("</section>", 1)[0]
    starts = list(re.finditer(r'<div class="plan(?: plan--hot)?">', section))
    return [
        section[match.start():starts[index + 1].start() if index + 1 < len(starts) else None]
        for index, match in enumerate(starts)
    ]


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


def test_pricing_shows_exactly_free_founding_and_annual_premium():
    cards = pricing_cards(html())

    assert len(cards) == 3
    free, founding, premium = cards
    assert '<div class="plan-name">Free</div>' in free
    assert '<div class="plan-price">0 €</div>' in free
    assert '<div class="plan-per">navždy</div>' in free
    assert '<div class="plan-name">Zakladajúci</div>' in founding
    assert '<div class="plan-price">39 €</div>' in founding
    assert "jednorazovo" in founding
    assert "cena natrvalo" in founding
    assert "Prvých 250" in founding
    assert '<div class="plan-name">Premium</div>' in premium
    assert '<div class="plan-price">49 €</div>' in premium
    assert "/ rok" in premium
    assert "po skončení zakladajúcej ponuky" in premium


def test_free_and_premium_store_promises_match_the_product_entitlements():
    free, founding, premium = pricing_cards(html())

    assert "1 obchod podľa výberu" in free
    assert "Jedálniček na celý týždeň" in free
    assert "Jedálniček na 3 dni" not in free
    for paid in (founding, premium):
        assert "Všetky podporované obchody" in paid


def test_founding_is_the_only_highlighted_pricing_card():
    cards = pricing_cards(html())

    assert sum('class="plan plan--hot"' in card for card in cards) == 1
    assert 'class="plan plan--hot"' in cards[1]
    assert '<div class="plan-name">Zakladajúci</div>' in cards[1]


def test_pricing_grid_is_three_desktop_two_tablet_and_one_mobile_column():
    page = html().replace(" ", "").replace("\n", "")

    assert ".plans{display:grid;grid-template-columns:repeat(3,minmax(0,1fr))" in page
    assert "@media(max-width:1050px){.plans{grid-template-columns:repeat(2,minmax(0,1fr))}" in page
    assert "@media(max-width:560px)" in page
    mobile = page.split("@media(max-width:560px)", 1)[1]
    assert ".plans{grid-template-columns:1fr}" in mobile
    assert ".plans,.plan{min-width:0}" in page


def test_founding_offer_has_a_progressive_accessible_counter_slot():
    page = html()

    assert re.search(
        r'<div[^>]+id="community-counter"[^>]*>\s*Prvých 250 získa zakladajúcu cenu\s*</div>',
        page,
    )
    assert "renderCommunity(data.community)" in page
    assert page.count('fetch("/api/public/landing")') == 1
    bar_rule = re.search(r"\.pc-bar\s*\{(.*?)\}", page, re.S)
    assert bar_rule
    assert "overflow:hidden" in bar_rule.group(1).replace(" ", "")


def test_landing_and_legal_draft_share_the_approved_prices_without_stale_offers():
    page = html()
    legal = legal_terms()

    assert "39 €" in page and "39 €" in legal
    assert "49 €" in page and "49 €" in legal
    assert "39 € jednorazovo" in page and "39 € jednorazovo" in legal
    assert "49 € ročne" in legal
    for stale_price in ("19 €", "29 €"):
        assert stale_price not in page
        assert stale_price not in legal


def test_interest_email_is_nonbinding_and_only_a_later_purchase_creates_entitlement():
    page = html()
    legal = legal_terms()

    assert "Platby sú vypnuté" in page
    assert "nezáväzný záujem" in page
    assert "nevytvára objednávku" in page
    assert "úspešný neskorší nákup" in page
    assert "odstúpenia a vrátenia platby" in page
    assert "konečnom checkoute" in page
    assert "Platby za Premium sú momentálne vypnuté" in legal
    assert "nezáväzné prejavenie záujmu" in legal
    assert "nevzniká objednávka" in legal
    assert "úspešnom nákupe" in legal
    assert "odstúpenia a vrátenia platby" in legal
    assert "konečnom checkoute" in legal
    for live_payment_claim in (
        "Kúpiť Premium teraz",
        "Zaplať teraz",
        "Platbu technicky a zmluvne spracúva",
    ):
        assert live_payment_claim not in page
        assert live_payment_claim not in legal


def test_account_counter_is_informational_and_never_presented_as_buyer_popularity():
    page = html()
    legal = legal_terms()

    assert "Počet účtov je informatívny" in page
    assert "nejde o počet kupujúcich" in page
    assert "Počet vytvorených účtov" in legal
    assert "nie je počtom kupujúcich" in legal
    for fake_claim in ("najobľúbenejší", "najpopulárnejší", "najpredávanejší"):
        assert fake_claim not in page.lower()
        assert fake_claim not in legal.lower()


def test_annual_savings_is_a_model_example_not_a_guarantee():
    page = html()
    legal = legal_terms()

    assert "Modelový príklad" in page
    assert "nie je zárukou úspory" in page
    assert "modelového nákupného zoznamu" in legal
    assert "Nie je zárukou osobnej úspory" in legal
