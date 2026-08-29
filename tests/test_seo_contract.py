"""Statický SEO kontrakt pre domovskú stránku Uvar.si."""
import json
from html.parser import HTMLParser
from pathlib import Path


TITLE = "Uvar.si — z letáka rovno na tanier"
DESCRIPTION = (
    "Uvar.si spojí aktuálne akcie z Lidla, Kauflandu a Tesca s tým, čo máš doma. "
    "Dostaneš jedálniček, recepty a nákupný zoznam na celý týždeň."
)
REQUIRED_LINKS = {
    "/co-varit-tento-tyzden": "Čo variť tento týždeň",
    "/lacny-jedalnicek": "Lacný jedálniček",
    "/ako-varime-z-akcii": "Ako varíme z akcií",
}


def read_homepage():
    return Path("index.html").read_text(encoding="utf-8")


def squash(text):
    return " ".join(text.split())


class HomeParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.canonicals = []
        self.meta = []
        self.h1_texts = []
        self.anchors = []
        self.jsonld_scripts = []
        self.title_parts = []
        self._in_title = False
        self._current_h1 = None
        self._current_anchor = None
        self._current_script = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "title":
            self._in_title = True
        elif tag == "meta":
            self.meta.append(attrs)
        elif tag == "link" and attrs.get("rel") == "canonical":
            self.canonicals.append(attrs.get("href", ""))
        elif tag == "h1":
            self._current_h1 = []
        elif tag == "a":
            self._current_anchor = {"href": attrs.get("href", ""), "text": []}
        elif tag == "script" and attrs.get("type") == "application/ld+json":
            self._current_script = []
        elif tag == "br":
            if self._current_h1 is not None:
                self._current_h1.append(" ")
            if self._current_anchor is not None:
                self._current_anchor["text"].append(" ")

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        elif tag == "h1" and self._current_h1 is not None:
            self.h1_texts.append(squash("".join(self._current_h1)))
            self._current_h1 = None
        elif tag == "a" and self._current_anchor is not None:
            self.anchors.append(
                (self._current_anchor["href"], squash("".join(self._current_anchor["text"])))
            )
            self._current_anchor = None
        elif tag == "script" and self._current_script is not None:
            self.jsonld_scripts.append("".join(self._current_script).strip())
            self._current_script = None

    def handle_data(self, data):
        if self._in_title:
            self.title_parts.append(data)
        if self._current_h1 is not None:
            self._current_h1.append(data)
        if self._current_anchor is not None:
            self._current_anchor["text"].append(data)
        if self._current_script is not None:
            self._current_script.append(data)


def parsed_homepage():
    parser = HomeParser()
    parser.feed(read_homepage())
    return parser


def meta_values(parser, key):
    return [
        tag.get("content", "")
        for tag in parser.meta
        if tag.get("name") == key or tag.get("property") == key
    ]


def graph_item(graph, wanted_type):
    matches = [item for item in graph if item.get("@type") == wanted_type]
    assert len(matches) == 1, f"expected exactly one {wanted_type} node"
    return matches[0]


def walk_keys(node):
    if isinstance(node, dict):
        for key, value in node.items():
            yield key
            yield from walk_keys(value)
    elif isinstance(node, list):
        for item in node:
            yield from walk_keys(item)


def test_homepage_keeps_one_canonical_one_h1_and_the_current_copy():
    parser = parsed_homepage()

    assert squash("".join(parser.title_parts)) == TITLE
    assert meta_values(parser, "description") == [DESCRIPTION]
    assert parser.canonicals == ["https://uvar.si/"]
    assert parser.h1_texts == [
        "Varenie na celý týždeň. Podľa akcií, ktoré práve platia."
    ]


def test_homepage_exposes_truthful_social_metadata_without_fake_image_claims():
    parser = parsed_homepage()

    expected = {
        "og:title": TITLE,
        "og:description": DESCRIPTION,
        "og:url": "https://uvar.si/",
        "og:type": "website",
        "og:locale": "sk_SK",
        "twitter:card": "summary",
    }
    for key, value in expected.items():
        assert meta_values(parser, key) == [value]

    assert meta_values(parser, "og:image") == []
    assert meta_values(parser, "twitter:image") == []


def test_homepage_jsonld_stays_inside_supported_website_and_app_claims():
    parser = parsed_homepage()

    assert len(parser.jsonld_scripts) == 1
    payload = json.loads(parser.jsonld_scripts[0])
    assert payload.get("@context") == "https://schema.org"
    graph = payload.get("@graph")
    assert isinstance(graph, list) and len(graph) == 2
    assert {item.get("@type") for item in graph} == {"WebSite", "SoftwareApplication"}

    website = graph_item(graph, "WebSite")
    assert website["name"] == "Uvar.si"
    assert website["url"] == "https://uvar.si/"
    assert website["inLanguage"] == "sk-SK"
    assert website["description"] == DESCRIPTION

    app = graph_item(graph, "SoftwareApplication")
    assert app["name"] == "Uvar.si"
    assert app["url"] == "https://uvar.si/app"
    assert app["operatingSystem"] == "Web"
    assert "meal" in app["applicationCategory"].lower()
    assert app["inLanguage"] == "sk-SK"

    forbidden_keys = {
        "offers",
        "price",
        "aggregaterating",
        "review",
        "author",
        "publisher",
    }
    assert forbidden_keys.isdisjoint({key.lower() for key in walk_keys(payload)})

    serialized = json.dumps(payload, ensure_ascii=False).lower()
    for forbidden_claim in ("ušetr", "garant", "používateľ", "users", "organization", "person"):
        assert forbidden_claim not in serialized


def test_homepage_links_to_the_three_public_seo_pages_with_real_anchors():
    parser = parsed_homepage()
    links = {href: text for href, text in parser.anchors}

    assert set(REQUIRED_LINKS).issubset(links)
    for href, label in REQUIRED_LINKS.items():
        assert links[href] == label
