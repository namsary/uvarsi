from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime
from html import escape
from urllib.parse import urlsplit

try:
    from .landing_data import validate_landing_data
except ImportError:
    from landing_data import validate_landing_data


BASE_URL = "https://uvar.si"
WEEKLY_URL = f"{BASE_URL}/co-varit-tento-tyzden"
EVERGREEN_URLS = {
    "lacny-jedalnicek": f"{BASE_URL}/lacny-jedalnicek",
    "ako-varime-z-akcii": f"{BASE_URL}/ako-varime-z-akcii",
}

ROBOTS_TXT = """User-agent: *
Allow: /
Disallow: /api/

User-agent: OAI-SearchBot
Allow: /
Disallow: /api/

Sitemap: https://uvar.si/sitemap.xml
"""


@dataclass(frozen=True)
class RenderedPage:
    html: str
    indexable: bool
    last_modified: date | None = None


def _safe_text(value: object) -> str:
    return escape("" if value is None else str(value), quote=True)


def _safe_url(value: object) -> str:
    text = str(value or "").strip()
    parsed = urlsplit(text)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ""
    return escape(text, quote=True)


def _iso_to_date(stamp: str | None) -> date | None:
    if not stamp:
        return None
    return datetime.fromisoformat(stamp).date()


def _format_date(value: str | date) -> str:
    parsed = value if isinstance(value, date) else date.fromisoformat(value)
    return f"{parsed.day}. {parsed.month}. {parsed.year}"


def _format_datetime(iso_value: str) -> str:
    parsed = datetime.fromisoformat(iso_value)
    return f"{parsed.day}. {parsed.month}. {parsed.year} {parsed:%H:%M}"


def _json_ld(payload: list[dict]) -> str:
    return json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")


def _nav() -> str:
    return (
        '<nav aria-label="Hlavná navigácia"><ul>'
        f'<li><a href="{BASE_URL}/">Uvar.si</a></li>'
        f'<li><a href="{WEEKLY_URL}">Čo variť tento týždeň</a></li>'
        f'<li><a href="{EVERGREEN_URLS["lacny-jedalnicek"]}">Lacný jedálniček</a></li>'
        f'<li><a href="{EVERGREEN_URLS["ako-varime-z-akcii"]}">Ako varíme z akcií</a></li>'
        "</ul></nav>"
    )


def _shell(
    *,
    title: str,
    description: str,
    canonical: str,
    h1: str,
    body: str,
    json_ld_payload: list[dict],
    indexable: bool,
) -> str:
    robots = "index,follow" if indexable else "noindex,follow"
    safe_title = _safe_text(title)
    safe_description = _safe_text(description)
    safe_h1 = _safe_text(h1)
    safe_canonical = _safe_text(canonical)
    return f"""<!DOCTYPE html>
<html lang="sk">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{safe_title}</title>
  <meta name="description" content="{safe_description}">
  <meta name="robots" content="{robots}">
  <link rel="canonical" href="{safe_canonical}">
  <meta property="og:type" content="article">
  <meta property="og:title" content="{safe_title}">
  <meta property="og:description" content="{safe_description}">
  <meta property="og:url" content="{safe_canonical}">
  <meta property="og:locale" content="sk_SK">
  <meta name="twitter:card" content="summary">
  <meta name="twitter:title" content="{safe_title}">
  <meta name="twitter:description" content="{safe_description}">
  <style>
    :root {{
      color-scheme: light;
      --bg: #fffaf0;
      --ink: #1f1a17;
      --muted: #5e524a;
      --line: #dfd1c4;
      --card: #fff;
      --accent: #1f6a50;
    }}
    * {{ box-sizing: border-box; }}
    body {{ margin: 0; font-family: Georgia, "Times New Roman", serif; background: linear-gradient(180deg, #fff8ec 0%, #fffdf8 55%, #f7f2eb 100%); color: var(--ink); }}
    main {{ max-width: 900px; margin: 0 auto; padding: 32px 20px 56px; }}
    nav ul {{ display: flex; flex-wrap: wrap; gap: 14px; list-style: none; padding: 0; margin: 0 0 24px; }}
    nav a, a.cta {{ color: var(--accent); font-weight: 700; text-decoration-thickness: 2px; text-underline-offset: 3px; }}
    h1, h2, h3 {{ line-height: 1.15; }}
    h1 {{ margin: 0 0 12px; font-size: clamp(2rem, 4vw, 3rem); }}
    p, li {{ line-height: 1.6; }}
    .lede {{ font-size: 1.1rem; color: var(--muted); }}
    .card {{ background: var(--card); border: 1px solid var(--line); border-radius: 18px; padding: 20px; margin: 18px 0; box-shadow: 0 10px 30px rgba(31, 26, 23, 0.06); }}
    .eyebrow {{ text-transform: uppercase; letter-spacing: 0.08em; font-size: 0.82rem; color: var(--muted); }}
    .price {{ font-weight: 700; }}
    .meta {{ color: var(--muted); }}
  </style>
  <script type="application/ld+json">{_json_ld(json_ld_payload)}</script>
</head>
<body>
  <main>
    {_nav()}
    <h1>{safe_h1}</h1>
    {body}
  </main>
</body>
</html>"""


def _breadcrumbs(current_name: str, current_url: str) -> dict:
    return {
        "@context": "https://schema.org",
        "@type": "BreadcrumbList",
        "itemListElement": [
            {"@type": "ListItem", "position": 1, "name": "Uvar.si", "item": f"{BASE_URL}/"},
            {"@type": "ListItem", "position": 2, "name": current_name, "item": current_url},
        ],
    }


def _article(*, title: str, description: str, url: str, date_modified: str | None = None) -> dict:
    article = {
        "@context": "https://schema.org",
        "@type": "Article",
        "headline": title,
        "description": description,
        "inLanguage": "sk-SK",
        "mainEntityOfPage": url,
    }
    if date_modified:
        article["dateModified"] = date_modified
    return article


def _required_text_field(record: dict, field: str) -> str:
    value = record.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Chýba {field}.")
    return value


def _required_iso_date(record: dict, field: str) -> date:
    try:
        return date.fromisoformat(_required_text_field(record, field))
    except ValueError as error:
        raise ValueError(f"Chýba alebo nesedí {field}.") from error


def _shared_validity_markup(sources: list[dict]) -> tuple[str, list[str]]:
    shared_from: date | None = None
    shared_to: date | None = None
    source_markup: list[str] = []

    for source in sources:
        store = _required_text_field(source, "store")
        valid_from = _required_iso_date(source, "valid_from")
        valid_to = _required_iso_date(source, "valid_to")
        if valid_from > valid_to:
            raise ValueError("Zdroj má obrátené dátumy platnosti.")

        if shared_from is None or valid_from > shared_from:
            shared_from = valid_from
        if shared_to is None or valid_to < shared_to:
            shared_to = valid_to

        label = f"{_safe_text(store)}: {_format_date(valid_from)} - {_format_date(valid_to)}"
        if source.get("source_page") not in (None, ""):
            label += f", strana {_safe_text(source['source_page'])}"
        href = _safe_url(source.get("url"))
        if href:
            label += f' (<a href="{href}">zdroj</a>)'
        source_markup.append(f"<li>{label}</li>")

    if shared_from is None or shared_to is None:
        raise ValueError("Chýbajú zdroje s platnosťou.")

    if shared_from <= shared_to:
        return (
            f'<p class="meta">Platnosť cien: {_format_date(shared_from)} - {_format_date(shared_to)}</p>',
            source_markup,
        )

    return (
        '<p class="meta">Platnosť cien sa líši podľa obchodu. Presné termíny nájdeš pri zdrojoch nižšie.</p>',
        source_markup,
    )


def _weekly_body(payload: dict) -> str:
    receipt = payload["receipt"]
    sources = payload["sources"]
    validity_markup, source_markup = _shared_validity_markup(sources)
    meals_markup: list[str] = []
    for meal in receipt["meals"]:
        items_markup: list[str] = []
        for item in meal["items"]:
            unit = _required_text_field(item, "unit")
            if item.get("price") in (None, ""):
                raise ValueError("Chýba price.")
            original = ""
            if item.get("original_price"):
                original = (
                    f'<p class="meta">Pôvodná cena: {_safe_text(item["original_price"])} €</p>'
                )
            items_markup.append(
                "<li>"
                f"<strong>{_safe_text(item['name'])}</strong> "
                f"({_safe_text(item['store'])}, {_safe_text(unit)}) "
                f'<span class="price">{_safe_text(item["price"])} €</span>'
                f"{original}"
                "</li>"
            )
        meals_markup.append(
            '<section class="card">'
            f'<p class="eyebrow">{_safe_text(meal["day"])}</p>'
            f'<h2>{_safe_text(meal["name"])}</h2>'
            "<ul>"
            + "".join(items_markup)
            + "</ul></section>"
        )
    return (
        f'<p class="lede">Aktuálny týždenný jedálniček pre { _safe_text(payload["week_label"]) } '
        "stojí na aktuálne platných letákoch a priamo odpovedá, čo sa oplatí variť tento týždeň.</p>"
        f'<div class="card"><p><strong>Priama odpoveď:</strong> Tento týždeň má zmysel postaviť varenie okolo '
        f'{_safe_text(receipt["meals"][0]["name"])} a ďalších overených akciových položiek.</p>'
        f"{validity_markup}"
        f'<p class="meta">Aktualizované: {_format_datetime(payload["generated_at"])}</p></div>'
        + "".join(meals_markup)
        + '<section class="card"><h2>Zdroje a strany letákov</h2><ul>'
        + "".join(source_markup)
        + "</ul></section>"
        + '<section class="card"><h2>Ako pracujeme s AI a dátami</h2>'
        "<p>AI skladá návrh jedál iba z overených položiek. Ceny, obchody, jednotky aj zdrojové strany berieme výhradne z aktuálnych letákových dát a pri neplatných údajoch stránku radšej stiahneme z indexu.</p>"
        f'<p><a class="cta" href="{BASE_URL}/app">Otvor aplikáciu Uvar.si</a> a priprav si celý nákupný plán.</p>'
        "</section>"
    )


def _weekly_recovery(today: date | None) -> RenderedPage:
    title = "Týždenné ceny práve overujeme | Uvar.si"
    description = (
        "Týždenný prehľad akciových cien sa zobrazí až po overení nových letákových dát."
    )
    body = (
        '<p class="lede">Týždenné ceny práve overujeme. Keď dorazia čerstvé a overené letákové dáta, '
        "sem sa vráti konkrétny jedálniček bez vymyslených čísiel.</p>"
        '<section class="card"><h2>Čo môžeš robiť medzitým</h2>'
        f'<p>Pozri si <a href="{EVERGREEN_URLS["lacny-jedalnicek"]}">lacný jedálniček</a> '
        f'alebo náš postup <a href="{EVERGREEN_URLS["ako-varime-z-akcii"]}">ako varíme z akcií</a>.</p>'
        "</section>"
    )
    structured = [
        _article(title=title, description=description, url=WEEKLY_URL),
        _breadcrumbs("Čo variť tento týždeň", WEEKLY_URL),
    ]
    return RenderedPage(
        html=_shell(
            title=title,
            description=description,
            canonical=WEEKLY_URL,
            h1="Čo variť tento týždeň",
            body=body,
            json_ld_payload=structured,
            indexable=False,
        ),
        indexable=False,
        last_modified=None,
    )


def render_weekly_page(payload: dict | None, today: date | None = None) -> RenderedPage:
    today = today or date.today()
    if not isinstance(payload, dict):
        return _weekly_recovery(today)
    try:
        validated = validate_landing_data(payload, today)
    except ValueError:
        return _weekly_recovery(today)

    title = "Čo variť tento týždeň z akcií | Uvar.si"
    description = (
        "Aktuálny týždenný jedálniček pre "
        f'{validated["week_label"]} vrátane jedla {validated["receipt"]["meals"][0]["name"]} '
        "z overených akciových cien a platných letákov."
    )
    modified = validated["generated_at"]
    structured = [
        _article(
            title=title,
            description=description,
            url=WEEKLY_URL,
            date_modified=modified,
        ),
        _breadcrumbs("Čo variť tento týždeň", WEEKLY_URL),
    ]
    try:
        body = _weekly_body(validated)
    except (KeyError, TypeError, ValueError):
        return _weekly_recovery(today)
    return RenderedPage(
        html=_shell(
            title=title,
            description=description,
            canonical=WEEKLY_URL,
            h1="Čo variť tento týždeň",
            body=body,
            json_ld_payload=structured,
            indexable=True,
        ),
        indexable=True,
        last_modified=_iso_to_date(modified),
    )


def render_evergreen_page(slug: str) -> RenderedPage:
    pages = {
        "lacny-jedalnicek": {
            "title": "Lacný jedálniček bez vymyslených zliav | Uvar.si",
            "description": "Praktický návod, ako skladať lacný jedálniček bez nestabilných cenových tvrdení.",
            "h1": "Lacný jedálniček",
            "lead": "Lacný jedálniček funguje vtedy, keď si držíš jednoduchý rytmus, suroviny sa prekrývajú a nakupuješ až z overených podkladov.",
            "steps": [
                "Začni dvoma až troma opakovateľnými jedlami na týždeň.",
                "Stavaj jedlá na surovinách, ktoré vieš prehodiť medzi obedom a večerou.",
                "Aktuálne akcie si over až v týždennom prehľade, nie v evergreen texte.",
            ],
        },
        "ako-varime-z-akcii": {
            "title": "Ako varíme z akcií bez klamlivých tvrdení | Uvar.si",
            "description": "Ako Uvar.si skladá jedlá z akcií tak, aby nevznikali vymyslené cenové sľuby.",
            "h1": "Ako varíme z akcií",
            "lead": "Z akcií sa oplatí variť iba vtedy, keď sú ceny aktuálne, zdroj pomenovaný a stránka vie priznať, keď dáta práve nie sú pripravené.",
            "steps": [
                "Najprv zbierame len overiteľné ponuky s presným zdrojom.",
                "Potom AI skladá jedlá z týchto položiek, nie z vymyslených úspor.",
                "Keď dáta zostarnú, týždenná stránka sa prepne do bezpečného režimu bez cien.",
            ],
        },
    }
    if slug not in pages:
        raise KeyError(slug)

    page = pages[slug]
    steps = "".join(f"<li>{_safe_text(step)}</li>" for step in page["steps"])
    body = (
        f'<p class="lede">{_safe_text(page["lead"])}</p>'
        '<section class="card"><h2>Priamy záver</h2>'
        f"<p>{_safe_text(page['lead'])}</p></section>"
        '<section class="card"><h2>Praktický postup</h2><ol>'
        f"{steps}</ol></section>"
        '<section class="card"><h2>Súvisiace stránky</h2>'
        f'<p><a href="{WEEKLY_URL}">Čo variť tento týždeň</a> ti dá aktuálny týždenný kontext, '
        f'kým <a href="{EVERGREEN_URLS["lacny-jedalnicek"]}">lacný jedálniček</a> a '
        f'<a href="{EVERGREEN_URLS["ako-varime-z-akcii"]}">ako varíme z akcií</a> vysvetľujú stabilný postup.</p>'
        "</section>"
    )
    canonical = EVERGREEN_URLS[slug]
    structured = [
        _article(
            title=page["title"],
            description=page["description"],
            url=canonical,
        ),
        _breadcrumbs(page["h1"], canonical),
    ]
    return RenderedPage(
        html=_shell(
            title=page["title"],
            description=page["description"],
            canonical=canonical,
            h1=page["h1"],
            body=body,
            json_ld_payload=structured,
            indexable=True,
        ),
        indexable=True,
        last_modified=None,
    )


def render_sitemap(today: date, weekly_modified: date | None) -> str:
    _ = today
    entries = [
        f"  <url><loc>{BASE_URL}/</loc></url>",
        (
            f"  <url><loc>{WEEKLY_URL}</loc>"
            + (f"<lastmod>{weekly_modified.isoformat()}</lastmod>" if weekly_modified else "")
            + "</url>"
        ),
        f"  <url><loc>{EVERGREEN_URLS['lacny-jedalnicek']}</loc></url>",
        f"  <url><loc>{EVERGREEN_URLS['ako-varime-z-akcii']}</loc></url>",
    ]
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        + "\n".join(entries)
        + "\n</urlset>"
    )
