"""Pre-render the public receipt into the static landing page.

The browser still refreshes the receipt from the public API.  This copy exists
for the first paint, no-JavaScript visitors and search/answer-engine crawlers.
It is rewritten from the same validated payload whenever weekly data changes.
"""

from __future__ import annotations

import html
import os
import re
import stat
from datetime import date
from pathlib import Path

try:
    from .landing_data import public_landing_payload
    from .offer_data import CURRENT_COLLECTION_DATA_VERSION
except ImportError:  # direct runtime imports from /opt/uvarsi/app
    from landing_data import public_landing_payload
    from offer_data import CURRENT_COLLECTION_DATA_VERSION


START = "<!-- RCPT:START -->"
END = "<!-- RCPT:END -->"
_LANDING_OPEN = re.compile(
    r'(<div class="rcpt-wrap" id="landing-data" aria-live="polite")'
    r'(?: hidden)?(>)'
)
_STATUS_OPEN = re.compile(
    r'(<p class="rcpt-proof" id="landing-status" aria-live="polite")'
    r'(?: hidden)?(>)'
)


def _safe(value: object) -> str:
    return html.escape(str(value), quote=True)


def _amount(value: object) -> str:
    return f"{_safe(value)} €"


def _receipt_markup(data: dict) -> str:
    historical = data["state"] == "historical_example"
    meals = data["receipt"]["meals"]
    heading = "Ukážka" if historical else "Tvoj týždeň"
    parts = [
        '<div class="rcpt" data-server-rendered="true">',
        '<div class="rcpt-head">',
        '<div class="rcpt-logo">Uvar.si</div>',
        f'<div class="rcpt-sub">{heading} · varíš {len(meals)}×</div>',
    ]
    if not historical:
        parts.append(f'<div class="rcpt-sub">{_safe(data["week_label"])}</div>')
    parts.extend(('</div>', '<hr class="rule-solid">'))

    for meal in meals:
        parts.append(
            '<div class="day">'
            f'<b>{_safe(meal["day"])}</b><span>{_safe(meal["name"])}</span>'
            '</div>'
        )
        for item in meal.get("items", ()):
            label = " · ".join(
                _safe(value) for value in (item.get("name"), item.get("store"))
                if value
            )
            details = ""
            if item.get("price") not in (None, ""):
                details += f'<b class="price">{_amount(item["price"])}</b>'
            if item.get("off"):
                details += f'<span class="off">{_safe(item["off"])}</span>'
            parts.append(
                '<div class="item">'
                f'<span>{label}</span><span class="item-r">{details}</span>'
                '</div>'
            )

    parts.append('<hr class="rule">')
    total_label = "Nákup v ukážke" if historical else "Nákup spolu"
    parts.append(
        f'<div class="row"><span>{total_label}</span>'
        f'<span>{_amount(data["receipt"]["nakup_spolu"])}</span></div>'
    )
    if not historical:
        parts.extend((
            '<div class="row"><span>Bežne by stál</span>'
            f'<span>{_amount(data["receipt"]["bezne"])}</span></div>',
            '<div class="save"><span>Ušetríš</span>'
            f'<span>{_amount(data["receipt"]["usetris"])}</span></div>',
        ))
    parts.extend(('<div class="rcpt-foot">Dobrú chuť</div>', '</div>'))

    if historical:
        proof = _safe(data["notice"])
        parts.append(
            '<p class="rcpt-proof"><span class="proof-current">'
            f'{proof}</span></p>'
        )
    else:
        parts.append(
            '<p class="rcpt-proof">'
            f'<span class="proof-current">Aktuálne letáky · {_safe(data["week_label"])}</span>'
            '<span class="proof-note">Cenu si pred nákupom over v predajni.</span>'
            '</p>'
        )
    return "".join(parts)


def _replace_snapshot(source: str, markup: str, visible: bool) -> str:
    start = source.find(START)
    end = source.find(END)
    if start < 0 or end < 0 or end < start:
        raise ValueError("Landing page nemá platné RCPT značky.")
    source = source[: start + len(START)] + markup + source[end:]

    landing_replacement = r"\1\2" if visible else r"\1 hidden\2"
    status_replacement = r"\1 hidden\2" if visible else r"\1\2"
    source, landing_count = _LANDING_OPEN.subn(landing_replacement, source, count=1)
    source, status_count = _STATUS_OPEN.subn(status_replacement, source, count=1)
    if landing_count != 1 or status_count != 1:
        raise ValueError("Landing page nemá očakávané kontajnery bločka.")
    return source


def publish_landing_html(
    index_path: str | Path,
    payload: object,
    *,
    today: date | None = None,
) -> str:
    """Atomically publish a truthful snapshot and return its public state."""
    index = Path(index_path)
    source = index.read_text(encoding="utf-8")
    try:
        data = public_landing_payload(
            payload,
            today or date.today(),
            required_offer_data_version=CURRENT_COLLECTION_DATA_VERSION,
        )
        markup = _receipt_markup(data)
        state = data["state"]
    except (KeyError, TypeError, ValueError):
        markup = ""
        state = "unavailable"

    rendered = _replace_snapshot(source, markup, visible=bool(markup))
    temporary = index.with_name(f".{index.name}.{os.getpid()}.tmp")
    mode = stat.S_IMODE(index.stat().st_mode)
    try:
        with temporary.open("w", encoding="utf-8", newline="") as handle:
            handle.write(rendered)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, index)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
    return state
