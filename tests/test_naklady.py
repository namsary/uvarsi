"""Strop na míňanie kreditu — modul app/naklady.py.

Kontext (incident 21. 8. 2026): bloček padal deterministicky, dozorca to bral
ako dočasnú chybu a opakoval až 6× denne. Každý pokus spustil PLNÝ vision beh
Opusom 5 nad ~36 stranami letáku (~0,37 €). Za dva dni to zožralo celý kredit
majiteľa (4,60 €). Detekcia deterministického pádu je opravená, ale strop na
míňanie neexistoval nikde — a práve ten tu testujeme.
"""
import datetime
import sqlite3
import types

import pytest

from app import naklady


PONDELOK = datetime.datetime(2026, 8, 17, 9, 0, 0)      # ISO týždeň 2026-08-17
UTOROK = datetime.datetime(2026, 8, 18, 9, 0, 0)
BUDUCI_TYZDEN = datetime.datetime(2026, 8, 24, 9, 0, 0)
BUDUCI_MESIAC = datetime.datetime(2026, 9, 1, 9, 0, 0)


@pytest.fixture
def con(tmp_path):
    spojenie = naklady.pripoj(tmp_path / "uvarsi.db")
    yield spojenie
    spojenie.close()


@pytest.fixture(autouse=True)
def ciste_prostredie(monkeypatch):
    """Testy nesmú závisieť od stropov nastavených v prostredí vývojára."""
    for nazov in naklady.PREMENNE_PROSTREDIA:
        monkeypatch.delenv(nazov, raising=False)


def usage(vstup=0, vystup=0, cache_write=0, cache_read=0):
    return types.SimpleNamespace(
        input_tokens=vstup,
        output_tokens=vystup,
        cache_creation_input_tokens=cache_write,
        cache_read_input_tokens=cache_read,
    )


# Vision beh nad letákom: ~80 000 vstupných tokenov Opusom = ~0,41 €.
VISION_USAGE = usage(vstup=80_000, vystup=2_000)


# ------------------------------------------------------------------ cenník
def test_cennik_pocita_v_eurach_podla_oficialneho_cennika():
    """Opus 5: $5/MTok vstup, $25/MTok výstup. EUR = USD × 0,92."""
    cena = naklady.cena_eur("claude-opus-5", vstup=1_000_000, vystup=0)
    assert cena == pytest.approx(5.0 * 0.92)

    cena = naklady.cena_eur("claude-opus-5", vstup=0, vystup=1_000_000)
    assert cena == pytest.approx(25.0 * 0.92)


@pytest.mark.parametrize(
    "model, vstup, vystup, cache_read, cache_write",
    [
        ("claude-opus-5", 5.0, 25.0, 0.50, 6.25),
        ("claude-sonnet-5", 2.0, 10.0, 0.20, 2.50),
        ("claude-haiku-4-5", 1.0, 5.0, 0.10, 1.25),
    ],
)
def test_vsetky_sadzby_sedia_s_cennikom(model, vstup, vystup, cache_read, cache_write):
    for pole, sadzba in (
        ("vstup", vstup), ("vystup", vystup),
        ("cache_read", cache_read), ("cache_write", cache_write),
    ):
        assert naklady.cena_eur(model, **{pole: 1_000_000}) == pytest.approx(sadzba * 0.92)


def test_model_s_datumovou_priponou_sa_ocenuje_rovnako():
    """Zbierač volá 'claude-haiku-4-5-20251001' — to je ten istý cenník."""
    assert naklady.cena_eur("claude-haiku-4-5-20251001", vstup=1_000_000) == pytest.approx(
        naklady.cena_eur("claude-haiku-4-5", vstup=1_000_000)
    )


def test_neznamy_model_sa_ucuje_najdrahsou_sadzbou():
    """Fail closed: radšej nadhodnotiť než ticho míňať mimo evidencie."""
    neznamy = naklady.cena_eur("claude-nieco-uplne-nove", vstup=1_000_000)
    assert neznamy == pytest.approx(naklady.cena_eur("claude-opus-5", vstup=1_000_000))


# ------------------------------------------------------------------ ledger
def test_zapis_uklada_skutocne_tokeny_z_odpovede(con):
    naklady.zapis(con, "zber_letakov", "claude-opus-5", VISION_USAGE, teraz=PONDELOK)

    riadok = con.execute("SELECT * FROM naklady").fetchone()
    assert riadok["ucel"] == "zber_letakov"
    assert riadok["model"] == "claude-opus-5"
    assert riadok["vstup"] == 80_000
    assert riadok["vystup"] == 2_000
    assert riadok["den"] == "2026-08-17"
    assert riadok["mesiac"] == "2026-08"
    assert riadok["odhad"] == 0, "skutočné čísla z usage nie sú odhad"
    assert riadok["eur"] == pytest.approx((80_000 * 5 + 2_000 * 25) / 1e6 * 0.92)


def test_zapis_zaznamena_aj_cache_tokeny(con):
    naklady.zapis(
        con, "plan", "claude-sonnet-5",
        usage(vstup=100, vystup=50, cache_write=1_000, cache_read=10_000),
        teraz=PONDELOK,
    )
    riadok = con.execute("SELECT * FROM naklady").fetchone()
    assert riadok["cache_write"] == 1_000
    assert riadok["cache_read"] == 10_000
    ocakavana = (100 * 2 + 50 * 10 + 1_000 * 2.50 + 10_000 * 0.20) / 1e6 * 0.92
    assert riadok["eur"] == pytest.approx(ocakavana)


def test_chybajuce_usage_sa_zapise_ako_konzervativny_odhad(con):
    """Bez usage sa nesmie zapísať nula — to by bola diera v evidencii."""
    naklady.zapis(con, "zber_letakov", "claude-opus-5", None, teraz=PONDELOK)

    riadok = con.execute("SELECT * FROM naklady").fetchone()
    assert riadok["odhad"] == 1
    assert riadok["eur"] > 0


@pytest.mark.parametrize("ucel", ("plan", "predpocet"))
def test_seven_meal_plan_uses_the_same_conservative_precheck_and_timeout_estimate(
        con, monkeypatch, ucel):
    """+40 % maximálnej odpovede znamená 0,03 € pred volaním aj po timeoute."""
    assert naklady.ODHAD_EUR[ucel] == pytest.approx(0.03)

    naklady.zapis(con, ucel, "claude-sonnet-5", None, teraz=PONDELOK)
    timeout = con.execute("SELECT odhad, eur FROM naklady WHERE ucel=?", (ucel,)).fetchone()
    assert timeout["odhad"] == 1 and timeout["eur"] == pytest.approx(0.03)

    monkeypatch.setenv("UVARSI_DENNY_STROP_EUR", "0.05")
    with pytest.raises(naklady.RozpocetVycerpany) as chyba:
        naklady.skontroluj(con, ucel, teraz=PONDELOK)
    assert chyba.value.kod == naklady.KOD_DENNY


# ------------------------------------------------------------------ stropy
def test_denny_strop_odmietne_volanie_este_pred_zaplatenim(con, monkeypatch):
    monkeypatch.setenv("UVARSI_DENNY_STROP_EUR", "0.40")
    naklady.zapis(con, "plan", "claude-opus-5", VISION_USAGE, teraz=PONDELOK)

    with pytest.raises(naklady.RozpocetVycerpany) as chyba:
        naklady.skontroluj(con, "plan", teraz=PONDELOK)
    assert chyba.value.kod == naklady.KOD_DENNY


def test_denny_strop_sa_zajtra_uvolni(con, monkeypatch):
    monkeypatch.setenv("UVARSI_DENNY_STROP_EUR", "0.40")
    naklady.zapis(con, "plan", "claude-opus-5", VISION_USAGE, teraz=PONDELOK)

    naklady.skontroluj(con, "plan", teraz=UTOROK)   # nesmie padnúť


def test_mesacny_strop_odmietne_aj_ked_dnesok_je_prazdny(con, monkeypatch):
    monkeypatch.setenv("UVARSI_MESACNY_STROP_EUR", "0.40")
    naklady.zapis(con, "plan", "claude-opus-5", VISION_USAGE, teraz=PONDELOK)

    with pytest.raises(naklady.RozpocetVycerpany) as chyba:
        naklady.skontroluj(con, "plan", teraz=UTOROK)
    assert chyba.value.kod == naklady.KOD_MESACNY

    naklady.skontroluj(con, "plan", teraz=BUDUCI_MESIAC)   # nový mesiac = nový rozpočet


def test_strop_zohladnuje_odhad_ceny_takze_neprekroci_sa_ani_raz(con, monkeypatch):
    """Kontrola musí byť PRED volaním, teda s odhadom ceny toho volania."""
    monkeypatch.setenv("UVARSI_DENNY_STROP_EUR", "0.50")
    naklady.zapis(con, "plan", "claude-sonnet-5", usage(vstup=1_000), teraz=PONDELOK)

    with pytest.raises(naklady.RozpocetVycerpany):
        naklady.skontroluj(con, "zber_letakov", odhad_eur=0.60, teraz=PONDELOK)


def test_sprava_o_vycerpanom_rozpocte_je_po_slovensky(con, monkeypatch):
    monkeypatch.setenv("UVARSI_DENNY_STROP_EUR", "0.01")
    with pytest.raises(naklady.RozpocetVycerpany) as chyba:
        naklady.skontroluj(con, "plan", odhad_eur=1.0, teraz=PONDELOK)
    assert "rozpočet" in str(chyba.value).lower()


# ------------------------------------------------------------------ fail closed
def test_necitatelny_ledger_odmietne_volanie(tmp_path, monkeypatch):
    """Keď sa evidencia nedá prečítať, NEMÍŇAME — nevieme, koľko už padlo."""
    con = naklady.pripoj(tmp_path / "uvarsi.db")
    con.execute("DROP TABLE naklady")
    con.commit()

    with pytest.raises(naklady.RozpocetVycerpany) as chyba:
        naklady.skontroluj(con, "plan", teraz=PONDELOK)
    assert chyba.value.kod == naklady.KOD_NECITATELNY
    con.close()


def test_pokazeny_strop_v_prostredi_odmietne_volanie(con, monkeypatch):
    monkeypatch.setenv("UVARSI_MESACNY_STROP_EUR", "nezmysel")
    with pytest.raises(naklady.RozpocetVycerpany) as chyba:
        naklady.skontroluj(con, "plan", teraz=PONDELOK)
    assert chyba.value.kod == naklady.KOD_NECITATELNY


def test_neznamy_ucel_sa_odmietne(con):
    with pytest.raises(naklady.RozpocetVycerpany):
        naklady.skontroluj(con, "vymysleny_ucel", teraz=PONDELOK)


# ------------------------------------------------------------------ pod-limit účelu
def test_vision_beh_ma_tyzdenny_strop_poctu_behov(con):
    limit = naklady.limit_behov("zber_letakov")
    assert limit <= 3, "vision beh sa má spúšťať len párkrát do týždňa"

    for _ in range(limit):
        naklady.rezervuj_beh(con, "zber_letakov", teraz=PONDELOK)

    with pytest.raises(naklady.RozpocetVycerpany) as chyba:
        naklady.rezervuj_beh(con, "zber_letakov", teraz=PONDELOK)
    assert chyba.value.kod == naklady.KOD_BEHY


def test_tyzdenny_strop_behov_sa_v_novom_tyzdni_uvolni(con):
    for _ in range(naklady.limit_behov("zber_letakov")):
        naklady.rezervuj_beh(con, "zber_letakov", teraz=PONDELOK)

    naklady.rezervuj_beh(con, "zber_letakov", teraz=BUDUCI_TYZDEN)


def test_pod_limit_uctu_zastavi_aj_ked_denny_strop_este_nie(con, monkeypatch):
    monkeypatch.setenv("UVARSI_DENNY_STROP_EUR", "100")
    monkeypatch.setenv("UVARSI_MESACNY_STROP_EUR", "100")
    monkeypatch.setenv("UVARSI_TYZDENNY_STROP_ZBER_EUR", "0.50")
    naklady.zapis(con, "zber_letakov", "claude-opus-5", VISION_USAGE, teraz=PONDELOK)

    with pytest.raises(naklady.RozpocetVycerpany) as chyba:
        naklady.skontroluj(con, "zber_letakov", teraz=UTOROK)
    assert chyba.value.kod == naklady.KOD_UCEL

    naklady.skontroluj(con, "plan", teraz=UTOROK)   # iný účel beží ďalej


# ------------------------------------------------------------------ INCIDENT
def test_rozbehnuta_slucka_drahych_volani_sa_zastavi_o_strop(con, monkeypatch):
    """Presná simulácia incidentu, ktorý vynuloval kredit.

    Dozorca opakoval refresh 6× denne dva dni po sebe. Každý pokus spustil plný
    vision beh (~0,41 €) a skončil tou istou deterministickou chybou. Bez stropu
    to bolo 12 × 0,41 € = celý kredit. So stropom sa míňanie MUSÍ zastaviť.
    """
    monkeypatch.setenv("UVARSI_DENNY_STROP_EUR", "1.00")
    monkeypatch.setenv("UVARSI_MESACNY_STROP_EUR", "5.00")
    volania = []

    def drahy_vision_beh(teraz):
        """Volanie, ktoré stojí peniaze a vždy skončí tou istou chybou."""
        naklady.skontroluj(con, "zber_letakov", odhad_eur=0.41, teraz=teraz)
        volania.append(teraz)
        naklady.zapis(con, "zber_letakov", "claude-opus-5", VISION_USAGE, teraz=teraz)
        raise ValueError("bloček sa nedá zostaviť — chýba prečiarknutá cena")

    odmietnute = 0
    for den in (PONDELOK, UTOROK):
        for hodina in range(6):          # MAX_TRIES z dozorcu
            teraz = den + datetime.timedelta(hours=hodina)
            try:
                drahy_vision_beh(teraz)
            except naklady.RozpocetVycerpany:
                odmietnute += 1
            except ValueError:
                pass

    assert len(volania) < 12, "strop musel časť pokusov zastaviť"
    assert odmietnute > 0, "zastavené pokusy majú byť typované odmietnutie"

    minute = con.execute("SELECT COALESCE(SUM(eur), 0) FROM naklady").fetchone()[0]
    assert minute <= 5.00, "mesačný strop sa nesmie prekročiť"
    assert minute < 4.60, "incident zožral 4,60 € — so stropom to musí byť menej"


def test_rozbehnuta_slucka_narazi_najprv_na_tyzdenny_limit_behov(con, monkeypatch):
    """Štrukturálna poistka: aj keby eurový strop bol vysoký, behov je málo."""
    monkeypatch.setenv("UVARSI_DENNY_STROP_EUR", "999")
    monkeypatch.setenv("UVARSI_MESACNY_STROP_EUR", "999")
    monkeypatch.setenv("UVARSI_TYZDENNY_STROP_ZBER_EUR", "999")
    behy = 0

    for hodina in range(12):
        try:
            naklady.rezervuj_beh(con, "zber_letakov", teraz=PONDELOK + datetime.timedelta(hours=hodina))
        except naklady.RozpocetVycerpany:
            continue
        behy += 1

    assert behy == naklady.limit_behov("zber_letakov") <= 3


def test_zber_ma_jeden_bezpecny_pokus_na_obnovu_po_dvoch_zlyhaniach(monkeypatch):
    monkeypatch.delenv("UVARSI_TYZDENNE_BEHY_ZBER", raising=False)
    assert naklady.limit_behov("zber_letakov") == 3


# ------------------------------------------------------------------ s_rozpoctom
def test_s_rozpoctom_nevola_ked_je_strop_vycerpany(con, monkeypatch):
    monkeypatch.setenv("UVARSI_DENNY_STROP_EUR", "0.01")
    volane = []

    with pytest.raises(naklady.RozpocetVycerpany):
        naklady.s_rozpoctom(
            con, "plan", "claude-sonnet-5",
            lambda: volane.append(1), odhad_eur=1.0, teraz=PONDELOK,
        )

    assert volane == [], "drahé volanie sa nesmie uskutočniť po odmietnutí"


def test_s_rozpoctom_zapise_skutocnu_spotrebu(con):
    odpoved = types.SimpleNamespace(usage=VISION_USAGE)

    vratene = naklady.s_rozpoctom(
        con, "zber_letakov", "claude-opus-5", lambda: odpoved, teraz=PONDELOK
    )

    assert vratene is odpoved
    assert con.execute("SELECT COUNT(*) FROM naklady").fetchone()[0] == 1
    assert con.execute("SELECT vstup FROM naklady").fetchone()[0] == 80_000


def test_s_rozpoctom_zauctuje_aj_neuspesne_volanie(con):
    """Spadnuté volanie mohlo tokeny minúť — nesmie sa tváriť ako zadarmo."""
    with pytest.raises(RuntimeError):
        naklady.s_rozpoctom(
            con, "plan", "claude-sonnet-5",
            lambda: (_ for _ in ()).throw(RuntimeError("timeout")), teraz=PONDELOK,
        )

    riadok = con.execute("SELECT * FROM naklady").fetchone()
    assert riadok["eur"] > 0 and riadok["odhad"] == 1


# ------------------------------------------------------------------ strážený klient
class FalosnyKlient:
    """Napodobenina anthropic.Anthropic — počíta, koľkokrát sa naozaj volalo."""

    def __init__(self, odpoved=None):
        self.volania = []
        self._odpoved = odpoved or types.SimpleNamespace(usage=VISION_USAGE)
        klient = self

        class Spravy:
            def create(self, **kw):
                klient.volania.append(kw)
                return klient._odpoved

        self.messages = Spravy()


def test_strazeny_klient_zauctuje_kazde_volanie(con):
    klient = FalosnyKlient()
    strazeny = naklady.strazeny_klient(con, klient, "zber_letakov", teraz=PONDELOK)

    strazeny.messages.create(model="claude-opus-5", max_tokens=100, messages=[])

    assert len(klient.volania) == 1
    riadok = con.execute("SELECT * FROM naklady").fetchone()
    assert riadok["ucel"] == "zber_letakov"
    assert riadok["vstup"] == 80_000


def test_strazeny_klient_nepustí_volanie_cez_vycerpany_strop(con, monkeypatch):
    monkeypatch.setenv("UVARSI_DENNY_STROP_EUR", "0.01")
    klient = FalosnyKlient()
    strazeny = naklady.strazeny_klient(con, klient, "zber_letakov", teraz=PONDELOK)

    with pytest.raises(naklady.RozpocetVycerpany):
        strazeny.messages.create(model="claude-opus-5", max_tokens=100, messages=[])

    assert klient.volania == [], "cez strážený klient sa nesmie prekĺznuť volanie"


def test_strazeny_klient_zastavi_slucku_volani(con, monkeypatch):
    """Ten istý incident, ale cez rozhranie, ktoré appka naozaj používa.

    Strop sa smie prekročiť nanajvýš o JEDNO volanie: kontrola pozná len odhad
    ceny, skutočná cena je známa až z odpovede. Podstatné je, že míňanie sa
    zastaví — nie že sa trafí presne na cent.
    """
    monkeypatch.setenv("UVARSI_DENNY_STROP_EUR", "1.00")
    monkeypatch.setenv("UVARSI_MESACNY_STROP_EUR", "5.00")
    klient = FalosnyKlient()
    strazeny = naklady.strazeny_klient(con, klient, "zber_letakov", teraz=PONDELOK)

    for _ in range(12):
        try:
            strazeny.messages.create(model="claude-opus-5", max_tokens=100, messages=[])
        except naklady.RozpocetVycerpany:
            break

    assert len(klient.volania) <= 3
    minute = con.execute("SELECT COALESCE(SUM(eur), 0) FROM naklady").fetchone()[0]
    cena_volania = naklady.cena_eur("claude-opus-5", vstup=80_000, vystup=2_000)
    assert minute <= 1.00 + cena_volania, "presah smie byť nanajvýš o jedno volanie"


# ------------------------------------------------------------------ prehľad
def test_stav_ukazuje_dnesok_mesiac_zostatok_a_posledne_operacie(con, monkeypatch):
    monkeypatch.setenv("UVARSI_DENNY_STROP_EUR", "1.00")
    monkeypatch.setenv("UVARSI_MESACNY_STROP_EUR", "5.00")
    naklady.zapis(con, "zber_letakov", "claude-opus-5", VISION_USAGE, teraz=PONDELOK)
    naklady.zapis(con, "plan", "claude-sonnet-5", usage(vstup=1_000, vystup=500), teraz=UTOROK)

    stav = naklady.stav(con, teraz=UTOROK)

    assert stav["den"] == "2026-08-18"
    assert stav["mesiac"] == "2026-08"
    assert stav["dnes_eur"] == pytest.approx((1_000 * 2 + 500 * 10) / 1e6 * 0.92, abs=1e-6)
    assert stav["mesiac_eur"] > stav["dnes_eur"]
    assert stav["denny_strop_eur"] == 1.00
    assert stav["mesacny_strop_eur"] == 5.00
    assert stav["zostatok_dnes_eur"] == pytest.approx(1.00 - stav["dnes_eur"], abs=1e-6)
    assert stav["zostatok_mesiac_eur"] == pytest.approx(5.00 - stav["mesiac_eur"], abs=1e-6)
    assert [p["ucel"] for p in stav["posledne"]] == ["plan", "zber_letakov"]
    assert stav["behy"]["zber_letakov"]["limit"] == naklady.limit_behov("zber_letakov")


def test_stav_neprezradi_ziadne_tajomstva(con):
    naklady.zapis(
        con, "plan", "claude-sonnet-5", usage(vstup=10),
        detail="ANTHROPIC_API_KEY=sk-ant-tajne", teraz=PONDELOK,
    )
    text = repr(naklady.stav(con, teraz=PONDELOK))
    for zakazane in ("API_KEY", "sk-ant", "tajne"):
        assert zakazane not in text


def test_stav_neprepadne_ked_je_evidencia_pokazena(tmp_path):
    """Prehľad je diagnostika — nesmie zhodiť /api/health."""
    con = naklady.pripoj(tmp_path / "uvarsi.db")
    con.execute("DROP TABLE naklady")
    con.commit()

    stav = naklady.stav(con, teraz=PONDELOK)
    assert stav["chyba"], "pokazená evidencia sa musí priznať, nie predstierať nulu"
    con.close()


# ------------------------------------------------------------------ upozornenia
def test_upozornenie_pri_50_a_80_percentach_mesacneho_stropu(con, monkeypatch):
    monkeypatch.setenv("UVARSI_MESACNY_STROP_EUR", "1.00")
    poslane = []

    naklady.zapis(con, "plan", "claude-opus-5", usage(vstup=120_000), teraz=PONDELOK,
                  notifikuj=poslane.append)          # ~0,55 € = nad 50 %
    assert len(poslane) == 1
    assert "50" in poslane[0]["titul"] + poslane[0]["sprava"]

    naklady.zapis(con, "plan", "claude-opus-5", usage(vstup=70_000), teraz=PONDELOK,
                  notifikuj=poslane.append)          # spolu ~0,87 € = nad 80 %
    assert len(poslane) == 2


def test_kazdy_prah_upozorni_len_raz_nie_pri_kazdom_volani(con, monkeypatch):
    monkeypatch.setenv("UVARSI_MESACNY_STROP_EUR", "1.00")
    poslane = []

    for _ in range(10):
        naklady.zapis(con, "plan", "claude-opus-5", usage(vstup=120_000),
                      teraz=PONDELOK, notifikuj=poslane.append)

    assert len(poslane) == len(naklady.PRAHY_UPOZORNENIA), "žiadna lavína notifikácií"


def test_novy_mesiac_upozorni_znova(con, monkeypatch):
    monkeypatch.setenv("UVARSI_MESACNY_STROP_EUR", "1.00")
    poslane = []
    naklady.zapis(con, "plan", "claude-opus-5", usage(vstup=200_000),
                  teraz=PONDELOK, notifikuj=poslane.append)
    prvy_mesiac = len(poslane)

    naklady.zapis(con, "plan", "claude-opus-5", usage(vstup=200_000),
                  teraz=BUDUCI_MESIAC, notifikuj=poslane.append)

    assert len(poslane) > prvy_mesiac


def test_upozornenie_ide_na_ntfy_topic_dozorcu():
    """Majiteľ už jeden ntfy kanál sleduje — nezakladáme druhý."""
    from pathlib import Path

    dozorca = Path("hetzner/dozorca.sh").read_text(encoding="utf-8")
    assert naklady.NTFY_TOPIC in dozorca


# ------------------------------------------------------------------ konfigurácia
def test_vychodzie_stropy_su_bezpecne():
    # Horné hranice prekalibrované 24. 8. 2026 spolu so stropmi samotnými.
    # Pôvodné (2,00 € / 10,00 €) zodpovedali dobe, keď fáza 2 čítala len vzorku
    # 12–14 strán z letáku. Po zrušení vzorky stojí poctivý beh ~2,50 €, takže
    # starý denný strop zber PRERUŠIL a vypadával posledný obchod v poradí.
    # Podrobne v app/naklady.py a v tests/test_stropy_pokryju_cely_zber.py.
    stropy = naklady.stropy()
    assert 0 < stropy.denny <= 5.00
    assert 0 < stropy.mesacny <= 40.00
    assert stropy.denny < stropy.mesacny


# Merané 24. 8. 2026 na reálnych letákoch (Kaufland 77 + Tesco 31 + Lidl 100
# strán, ~104 z nich potravinových): fáza 1 ≈ 0,03 €, fáza 2 ≈ 2,51 €.
POCTIVY_TYZDENNY_ZBER_EUR = 2.55


def test_vychodzie_stropy_prezijú_poctivu_prevadzku():
    """Strop, ktorý zastaví aj poctivý beh, je výpadok — nie ochrana."""
    limity = naklady.stropy()
    assert limity.denny > POCTIVY_TYZDENNY_ZBER_EUR, "riadny zber sa musí zmestiť do dňa"
    assert limity.tyzdenny_ucel["zber_letakov"] > POCTIVY_TYZDENNY_ZBER_EUR
    assert limity.mesacny > POCTIVY_TYZDENNY_ZBER_EUR * 4.5, "mesiac má 4–5 zberov"


def test_odhad_jedneho_volania_nezastavi_poctivy_beh():
    """Príliš vysoký odhad na volanie by strop spustil dávno pred útratou."""
    assert naklady.ODHAD_EUR["zber_letakov"] < naklady.stropy().denny / 5


def test_incident_by_s_vychodzimi_stropmi_nevynuloval_kredit(con):
    """Bez jediného prestavenia v prostredí musí incident skončiť lacno."""
    volania = 0
    for hodina in range(12):
        teraz = PONDELOK + datetime.timedelta(hours=hodina)
        try:
            naklady.skontroluj(con, "zber_letakov", teraz=teraz)
        except naklady.RozpocetVycerpany:
            continue
        naklady.zapis(con, "zber_letakov", "claude-opus-5", VISION_USAGE,
                      teraz=teraz, notifikuj=lambda sprava: None)
        volania += 1

    minute = con.execute("SELECT COALESCE(SUM(eur), 0) FROM naklady").fetchone()[0]
    assert minute < 4.60, f"incident stál 4,60 €; s východzími stropmi {minute:.2f} €"
    assert volania < 12


def test_stropy_sa_daju_prestavit_z_prostredia(monkeypatch):
    monkeypatch.setenv("UVARSI_DENNY_STROP_EUR", "0.25")
    monkeypatch.setenv("UVARSI_MESACNY_STROP_EUR", "7.5")
    stropy = naklady.stropy()
    assert stropy.denny == 0.25
    assert stropy.mesacny == 7.5
