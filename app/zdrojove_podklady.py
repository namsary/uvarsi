"""Uvar.si — presné podklady od obchodu k jednotlivým stranám letáku.

PREČO. Haiku číta ceny z obrázka strany. Obchody však ku každému letáku
zverejňujú aj presnejšie podklady:

  * Lidl (API endpoints.leaflets.schwarz): popis a kľúčové slová každej strany
    a celý leták ako PDF s textovou vrstvou (názvy, gramáže, ceny, zľavy).
  * Tesco (GraphQL leaflets-be): zoznam produktov na každej strane (názov,
    merná jednotka) — ceny tam nie sú.

Tieto podklady sa pridajú k obrázku strany ako pomocný text. Model tak nemusí
hádať názvy a gramáže a cenu si overí v texte. Obrázok ostáva rozhodujúci.

ZLYHANIE JE NEŠKODNÉ. Každá chyba tu (sieť, PDF, zmena API) vráti prázdne
podklady a zber pokračuje presne ako doteraz. Podklady preto nikdy nesmú
zastaviť zber ani zmeniť identitu letáku.
"""
import os
import re
import shutil
import subprocess
import tempfile
import unicodedata
from urllib.parse import quote, unquote, urlparse

import requests

LIDL_API_URL = "https://endpoints.leaflets.schwarz/v4/flyer"
TESCO_GRAPHQL_URL = "https://api.prod.retail.tesco.com/marketing/leaflets-be/graphql"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)
MAX_PDF_BYTES = 300 * 1024 * 1024
MAX_HINT_CHARS = 3500
# Text PDF strany sa použije, len keď sedí s kľúčovými slovami tej istej strany
# z API. Tak sa nikdy nepriradí text inej strany (PDF môže mať o stranu menej).
MIN_KEYWORD_OVERLAP = 0.4


def zapnute():
    """Testy podklady vypínajú, aby nikdy nesiahli na sieť."""
    hodnota = os.environ.get("UVARSI_SOURCE_HINTS", "1").strip().lower()
    return hodnota not in ("0", "nie", "no", "off", "false")


def _ascii_tokens(text):
    plain = unicodedata.normalize("NFKD", str(text or "")).encode("ascii", "ignore").decode()
    return {token for token in re.findall(r"[a-z]{4,}", plain.lower())}


def keyword_overlap(keywords, page_text):
    wanted = _ascii_tokens(keywords)
    if not wanted:
        return 0.0
    return len(wanted & _ascii_tokens(page_text)) / len(wanted)


def _clean_pdf_text(text):
    lines = []
    for line in str(text or "").splitlines():
        line = re.sub(r"[ \t]+", " ", line).strip()
        if line:
            lines.append(line)
    return "\n".join(lines)


def pdf_page_texts(path):
    """Text všetkých strán PDF (index 0 = strana 1); bez pdftotext prázdny zoznam."""
    binary = shutil.which("pdftotext")
    if not binary:
        return []
    result = subprocess.run(
        [binary, "-layout", "-enc", "UTF-8", str(path), "-"],
        capture_output=True, timeout=180, check=False,
    )
    if result.returncode != 0:
        return []
    pages = result.stdout.decode("utf-8", "replace").split("\f")
    if pages and not pages[-1].strip():
        pages.pop()
    return [_clean_pdf_text(page) for page in pages]


def _lidl_slug(source_url):
    match = re.search(r"/l/sk/letak/([^/?#]+)/", unquote(str(source_url or "")))
    if not match or not re.fullmatch(r"[A-Za-z0-9-]{1,200}", match.group(1)):
        return None
    return match.group(1)


def _safe_pdf_url(value):
    if not isinstance(value, str):
        return None
    parsed = urlparse(value)
    if parsed.scheme != "https" or parsed.hostname != "assets.leaflets.schwarz":
        return None
    if not parsed.path.lower().endswith(".pdf") or parsed.username or parsed.password:
        return None
    return value


def _download_pdf(url, directory):
    path = os.path.join(directory, "letak.pdf")
    size = 0
    with requests.get(url, headers={"User-Agent": UA}, timeout=120, stream=True) as response:
        if response.status_code != 200:
            return None
        with open(path, "wb") as handle:
            for chunk in response.iter_content(1024 * 1024):
                size += len(chunk)
                if size > MAX_PDF_BYTES:
                    return None
                handle.write(chunk)
    with open(path, "rb") as handle:
        if handle.read(5) != b"%PDF-":
            return None
    return path


def lidl_page_hints(manifest, log=print):
    """{strana: text} z popisu strany a overeného textu PDF tej istej strany."""
    slug = _lidl_slug(manifest.get("source_url"))
    if not slug:
        return {}
    payload = requests.get(
        f"{LIDL_API_URL}?flyer_identifier={quote(slug, safe='')}",
        headers={"User-Agent": UA}, timeout=30,
    ).json()
    flyer = payload.get("flyer") if isinstance(payload, dict) else None
    if not isinstance(flyer, dict):
        return {}
    expected_id = str(manifest.get("source_identity") or "").removeprefix("lidl-flyer:")
    if not expected_id or flyer.get("id") != expected_id:
        # Iný leták než ten, ktorý zber číta — podklady by patrili inej ponuke.
        return {}
    pages = {}
    for page in flyer.get("pages") or []:
        if isinstance(page, dict) and isinstance(page.get("number"), int):
            pages[page["number"]] = page

    pdf_texts = []
    pdf_url = _safe_pdf_url(flyer.get("pdfUrl"))
    if pdf_url:
        with tempfile.TemporaryDirectory(prefix="uvarsi-lidl-pdf-") as directory:
            path = _download_pdf(pdf_url, directory)
            if path:
                pdf_texts = pdf_page_texts(path)

    hints = {}
    verified = 0
    for number, page in pages.items():
        parts = []
        alt = str(page.get("altText") or "").strip()
        if alt:
            parts.append(f"Popis strany od Lidla: {alt}")
        if 0 < number <= len(pdf_texts):
            text = pdf_texts[number - 1]
            if text and keyword_overlap(page.get("keyWords"), text) >= MIN_KEYWORD_OVERLAP:
                parts.append("Text z oficiálneho PDF tejto strany:\n" + text)
                verified += 1
        if parts:
            hints[number] = "\n".join(parts)[:MAX_HINT_CHARS]
    log(f"[INFO] lidl: podklady pre {len(hints)} strán, overený text PDF pre {verified}")
    return hints


_TESCO_QUERY = """query UvarsiLeafletProducts {
  leaflets(options: { filter: {
    country: { eq: sk }
    type: { eq: %s }
    validTo: { after: "%sT00:00:00.000Z" }
  } }) {
    items {
      id validFrom validTo
      pages { positions { products { product { productName measure } } } }
    }
  }
}"""


def tesco_page_hints(manifest, log=print):
    """{strana: text} so zoznamom produktov, ktoré Tesco uvádza na strane."""
    leaflet_format = manifest.get("leaflet_format")
    valid_from = str(manifest.get("valid_from") or "")
    valid_to = str(manifest.get("valid_to") or "")
    if leaflet_format not in ("HM", "SM") or not valid_from or not valid_to:
        return {}
    response = requests.post(
        TESCO_GRAPHQL_URL,
        json={"query": _TESCO_QUERY % (leaflet_format, valid_from)},
        headers={"User-Agent": UA, "Content-Type": "application/json"},
        timeout=60,
    )
    data = response.json().get("data") or {}
    items = (data.get("leaflets") or {}).get("items") or []
    declared = manifest.get("declared_pages")
    match = None
    for item in items:
        pages = item.get("pages") or []
        if (
            str(item.get("validFrom") or "")[:10] == valid_from
            and str(item.get("validTo") or "")[:10] == valid_to
            and len(pages) == declared
        ):
            if match is not None:
                # Dva rovnaké letáky — nevieme, ktorý zber číta; radšej nič.
                return {}
            match = item
    if match is None:
        return {}
    hints = {}
    for index, page in enumerate(match.get("pages") or [], start=1):
        names = []
        for position in page.get("positions") or []:
            for entry in position.get("products") or []:
                product = (entry or {}).get("product") or {}
                name = re.sub(r"\s+", " ", str(product.get("productName") or "")).strip()
                if name and name not in names:
                    names.append(name)
        if names:
            hints[index] = (
                "Produkty, ktoré Tesco uvádza na tejto strane (bez cien): "
                + "; ".join(names[:80])
            )[:MAX_HINT_CHARS]
    log(f"[INFO] tesco: zoznam produktov pre {len(hints)} strán")
    return hints


def page_hints(store, manifest, log=print):
    """Podklady pre zber; akákoľvek chyba = žiadne podklady, nie zlyhanie zberu."""
    if not zapnute() or not isinstance(manifest, dict):
        return {}
    try:
        if store == "lidl" and manifest.get("collector_kind") == "official-lidl-viewer":
            return lidl_page_hints(manifest, log=log)
        if store == "tesco" and manifest.get("collector_kind") == "official-tesco-viewer":
            return tesco_page_hints(manifest, log=log)
    except Exception as exc:  # podklady sú len pomoc, nikdy nie podmienka
        log(f"[WARN] {store}: podklady od obchodu nie sú dostupné ({type(exc).__name__})")
    return {}
