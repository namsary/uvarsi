"""Formulár zakladajúcej ponuky posiela MailerLite rovnakú požiadavku ako pred opravou N1.

Pôvodný formulár (commit ab22e7f) posielal klasický HTML POST s typom
application/x-www-form-urlencoded. Oprava N1 ho nahradila fetchom; ten musí
na drôte poslať to isté (URL, metódu, typ obsahu aj polia), inak MailerLite
môže požiadavky odmietať a zákazník by o tom nevedel (odpoveď je pri no-cors
nečitateľná).
"""
import re
from pathlib import Path

ML_URL = "https://assets.mailerlite.com/jsonp/2513849/forms/193159378287724436/subscribe"
POLIA = ["fields[plan]", "fields[email]", "ml-submit", "anticsrf"]


def _page():
    return Path("index.html").read_text(encoding="utf-8")


def _handler():
    page = _page()
    start = page.index("form.addEventListener('submit'")
    return page[start:page.index("})();", start)]


def test_formular_ma_nezmeneny_cielovy_endpoint_metodu_a_polia():
    form = re.search(r'<form[^>]+id="wf"[^>]*>(.*?)</form>', _page(), re.S)
    assert form
    tag = form.group(0)[: form.group(0).index(">") + 1]
    assert f'action="{ML_URL}"' in tag
    assert 'method="post"' in tag
    names = re.findall(r'<input[^>]+name="([^"]+)"', form.group(1))
    assert names == POLIA
    assert re.search(r'name="ml-submit" value="1"', form.group(1))
    assert re.search(r'name="anticsrf" value="true"', form.group(1))


def test_fetch_posiela_urlencoded_telo_nie_multipart():
    handler = _handler()
    call = re.search(r"fetch\(([^;]*?)\)\.then", handler, re.S).group(1)

    assert "form.action" in call and "method:'POST'" in call
    assert "new FormData(form)" not in call.replace("new URLSearchParams(new FormData(form))", ""), (
        "FormData ako telo fetchu posiela multipart/form-data, nie urlencoded"
    )
    assert "new URLSearchParams(new FormData(form))" in call
    # URLSearchParams samo pridá ";charset=UTF-8"; pôvodný formulár ho nemal.
    # form.enctype je pri tomto formulári presne application/x-www-form-urlencoded.
    assert "headers:{'Content-Type':form.enctype}" in call
    assert "enctype" not in re.search(r'<form[^>]+id="wf"[^>]*>', _page()).group(0)


def test_uspech_netvrdi_viac_nez_klient_vie():
    """Pri no-cors je odpoveď nečitateľná; klient vie len to, že žiadosť odišla."""
    page = _page()
    success = re.search(r'id="successState">(.*?)</div></div>', page, re.S).group(1)

    assert "Zapísali sme" not in success
    assert "odoslali" in success
    assert "potvrden" in success.lower()
