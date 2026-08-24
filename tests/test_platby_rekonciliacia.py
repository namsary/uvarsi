"""Peniaze prišli — appka sa to musí dozvedieť aj vtedy, keď webhook nedorazí.

Audit pred spustením platieb našiel štyri diery, ktorými tichо odtekali peniaze:

1. Webhook, ktorý nedorazí, znamená natrvalo stratený nárok. Nikto sa to
   nedozvie — ani majiteľ, ani zákazník. Preto je tu rekonciliácia: pravidelne
   si vypýta objednávky z API poskytovateľa a doplní, čo chýba.
2. Webhook prijatý s vypnutými platbami sa nesmie zahodiť. Telo sa uloží tak,
   ako prišlo (aj s podpisom), a spracuje sa neskôr — podpis sa overuje až vtedy,
   takže sa neoslabuje nič.
3. 251. zákazník zaplatí a mlčky nedostane nič. Musí sa to dozvedieť majiteľ
   (ntfy) aj zákazník (v appke aj e-mailom).
4. Nespracovateľné udalosti musia dôjsť majiteľovi. Na ntfy sa ale nesmie
   dostať nič osobné — kanál je v repozitári a je verejne čitateľný.

Rekonciliácia zámerne nechodí okolo `spracuj_udalost`: skladá presne ten istý
tvar udalosti, aký chodí webhookom, takže platí tá istá idempotencia, tá istá
kontrola kapacity a tie isté UNIQUE obmedzenia.
"""
import datetime
import importlib
import io
import json
import sys
import urllib.error
from contextlib import closing
from pathlib import Path

import pytest

from test_platby import (
    CHECKOUT,
    TAJOMSTVO,
    aktivne,
    load_server,
    naroky,
    naplnit_miesta,
    objednavka,
    podpis,
    posli_webhook,
    prihlaseny,
    vytvor_pouzivatela,
    zapnute_platby,
)

from fastapi.testclient import TestClient


ROOT = Path(__file__).resolve().parents[1]


def platby_modul():
    return importlib.import_module("platby")


def rekonciliacia_modul():
    sys.modules.pop("rekonciliacia", None)
    return importlib.import_module("rekonciliacia")


def objednavka_z_api(order_id="ord-1", email="test@uvar.si", total=1900,
                     mena="EUR", stav="paid", variant_id=None, refunded=False,
                     custom=None):
    """Objednávka v tvare, v akom ju vracia LemonSqueezy API (v1/orders)."""
    attributes = {
        "identifier": f"id-{order_id}",
        "user_email": email,
        "user_name": "Test Testovic",
        "total": total,
        "currency": mena,
        "status": stav,
        "refunded": refunded,
        "created_at": "2026-08-24T10:00:00.000000Z",
        "first_order_item": {"variant_id": variant_id} if variant_id else {},
    }
    if custom is not None:
        attributes["custom_data"] = custom
    return {"type": "orders", "id": str(order_id), "attributes": attributes}


# ------------------------------------------------------------ 1. rekonciliácia
def test_zaplatena_objednavka_bez_webhooku_sa_dobehne_rekonciliaciou(monkeypatch, tmp_path):
    """Zákazník zaplatil, webhook nikdy neprišiel — nárok sa musí objaviť sám."""
    server = zapnute_platby(monkeypatch, tmp_path)
    rek = rekonciliacia_modul()
    vytvor_pouzivatela(server, user_id=5, email="clen@uvar.si")

    with closing(server.db()) as con:
        assert naroky(server) == [], "webhook naozaj neprišiel"
        suhrn = rek.rekonciluj(
            con, objednavky=[objednavka_z_api(order_id="ord-77", email="clen@uvar.si")],
            now=server.AUTH_CLOCK(),
        )

    assert suhrn["udelene"] == 1
    riadok = aktivne(server)[0]
    assert riadok["user_id"] == 5
    assert riadok["objednavka_id"] == "ord-77"
    assert riadok["poskytovatel"] == "lemonsqueezy"
    assert riadok["suma_centy"] == 1900 and riadok["mena"] == "EUR"


def test_rekonciliacia_dvakrat_udeli_prave_jeden_narok(monkeypatch, tmp_path):
    """Idempotencia: ten istý kľúč udalosti ako pri webhooku, teda žiadny duplikát."""
    server = zapnute_platby(monkeypatch, tmp_path)
    rek = rekonciliacia_modul()
    vytvor_pouzivatela(server, user_id=5, email="clen@uvar.si")
    objednavky = [objednavka_z_api(order_id="ord-77", email="clen@uvar.si")]

    with closing(server.db()) as con:
        prva = rek.rekonciluj(con, objednavky=objednavky, now=1000.0)
        druha = rek.rekonciluj(con, objednavky=objednavky, now=2000.0)

    assert prva["udelene"] == 1
    assert druha["udelene"] == 0 and druha["uz_spracovane"] == 1
    assert len(aktivne(server)) == 1
    assert len(naroky(server)) == 1


def test_rekonciliacia_nezdvoji_narok_ktory_uz_dorucil_webhook(monkeypatch, tmp_path):
    """Webhook aj rekonciliácia vidia tú istú objednávku — nárok smie byť jeden."""
    server = zapnute_platby(monkeypatch, tmp_path)
    rek = rekonciliacia_modul()
    vytvor_pouzivatela(server, user_id=1, email="test@uvar.si")
    client = TestClient(server.app, raise_server_exceptions=False)
    assert posli_webhook(client, objednavka(user_id=1, order_id="ord-42")).json()["akcia"] == "udelene"

    with closing(server.db()) as con:
        suhrn = rek.rekonciluj(
            con, objednavky=[objednavka_z_api(order_id="ord-42", email="test@uvar.si")],
            now=server.AUTH_CLOCK(),
        )

    assert suhrn["udelene"] == 0
    assert len(aktivne(server)) == 1


def test_rekonciliacia_prisiela_ucet_podla_id_z_custom_data_ked_ho_api_vrati(monkeypatch, tmp_path):
    server = zapnute_platby(monkeypatch, tmp_path)
    rek = rekonciliacia_modul()
    vytvor_pouzivatela(server, user_id=9, email="iny@uvar.si")

    with closing(server.db()) as con:
        rek.rekonciluj(
            con,
            objednavky=[objednavka_z_api(order_id="ord-9", email="nesedi@uvar.si",
                                         custom={"user_id": "9"})],
            now=server.AUTH_CLOCK(),
        )

    assert aktivne(server)[0]["user_id"] == 9


def test_objednavka_bez_priraditelneho_uctu_neudeli_nic_a_ohlasi_sa(monkeypatch, tmp_path):
    server = zapnute_platby(monkeypatch, tmp_path)
    rek = rekonciliacia_modul()
    vytvor_pouzivatela(server, user_id=1, email="test@uvar.si")

    with closing(server.db()) as con:
        suhrn = rek.rekonciluj(
            con, objednavky=[objednavka_z_api(order_id="ord-x", email="niekto-iny@inde.sk")],
            now=server.AUTH_CLOCK(),
        )

    assert naroky(server) == []
    assert suhrn["bez_uctu"] == 1


def test_rekonciliacia_respektuje_variant_a_cudzi_produkt_neudeli(monkeypatch, tmp_path):
    server = zapnute_platby(monkeypatch, tmp_path)
    rek = rekonciliacia_modul()
    vytvor_pouzivatela(server, user_id=1, email="test@uvar.si")

    with closing(server.db()) as con:
        suhrn = rek.rekonciluj(
            con,
            objednavky=[objednavka_z_api(order_id="ord-cudzi", email="test@uvar.si",
                                         variant_id=999)],
            now=server.AUTH_CLOCK(), variant_id="555",
        )

    assert naroky(server) == []
    assert suhrn["ignorovane"] == 1


def test_rekonciliacia_dobehne_aj_zameskane_vratenie(monkeypatch, tmp_path):
    """Aj webhook o vrátení sa môže stratiť — potom by Premium bežalo zadarmo."""
    server = zapnute_platby(monkeypatch, tmp_path)
    rek = rekonciliacia_modul()
    vytvor_pouzivatela(server, user_id=1, email="test@uvar.si")
    client = TestClient(server.app, raise_server_exceptions=False)
    posli_webhook(client, objednavka(user_id=1, order_id="ord-42"))

    with closing(server.db()) as con:
        suhrn = rek.rekonciluj(
            con,
            objednavky=[objednavka_z_api(order_id="ord-42", email="test@uvar.si",
                                         stav="refunded", refunded=True)],
            now=server.AUTH_CLOCK(),
        )

    assert suhrn["vratene"] == 1
    assert [row["stav"] for row in naroky(server)] == ["vrateny"]
    with closing(server.db()) as con:
        assert con.execute("SELECT platiaci FROM pouzivatelia WHERE id=1").fetchone()[0] == 0


def test_rekonciliacia_neudeluje_nezaplatene_objednavky(monkeypatch, tmp_path):
    server = zapnute_platby(monkeypatch, tmp_path)
    rek = rekonciliacia_modul()
    vytvor_pouzivatela(server, user_id=1, email="test@uvar.si")

    with closing(server.db()) as con:
        rek.rekonciluj(
            con,
            objednavky=[objednavka_z_api(order_id="ord-p", email="test@uvar.si", stav="pending")],
            now=server.AUTH_CLOCK(),
        )

    assert naroky(server) == []


def test_rekonciliacia_upozorni_majitela_ked_nieco_dobehla(monkeypatch, tmp_path):
    server = zapnute_platby(monkeypatch, tmp_path)
    rek = rekonciliacia_modul()
    vytvor_pouzivatela(server, user_id=5, email="clen@uvar.si")
    poslane = []

    with closing(server.db()) as con:
        rek.rekonciluj(
            con, objednavky=[objednavka_z_api(order_id="ord-77", email="clen@uvar.si")],
            now=server.AUTH_CLOCK(), notifikuj=poslane.append,
        )

    assert poslane, "dobehnutá platba je udalosť, o ktorej má majiteľ vedieť"
    text = " ".join(f"{s['titul']} {s['sprava']}" for s in poslane)
    assert "@" not in text, "na verejný ntfy kanál nepatrí e-mail zákazníka"


def test_rekonciliacia_nikdy_nesiaha_na_kluc_z_repozitara(monkeypatch, tmp_path):
    """Prístup k API poskytovateľa smie prísť len z env súboru, nikdy z kódu."""
    zdroj = (ROOT / "app" / "rekonciliacia.py").read_text(encoding="utf-8")
    assert "LEMON_API_KEY" in zdroj
    for zakazane in ("Bearer eyJ", "sk_live", "lsq_"):
        assert zakazane not in zdroj


class _Odpoved(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *chyba):
        return False


def test_stahovanie_prejde_strankovanie_a_filtruje_obchod(monkeypatch, tmp_path):
    rek = rekonciliacia_modul()
    adresy, hlavicky = [], []

    def otvor(ziadost, timeout=None):
        adresy.append(ziadost.full_url)
        hlavicky.append(dict(ziadost.headers))
        strana = len(adresy)
        return _Odpoved(json.dumps({
            "data": [{"type": "orders", "id": str(strana), "attributes": {}}],
            "links": {"next": strana < 3},
        }).encode())

    objednavky = rek.stiahni_objednavky("TAJNY-KLUC", store_id="42", otvor=otvor)

    assert [o["id"] for o in objednavky] == ["1", "2", "3"]
    assert "filter%5Bstore_id%5D=42" in adresy[0]
    assert hlavicky[0]["Authorization"] == "Bearer TAJNY-KLUC"


def test_stahovanie_ma_strop_poctu_stran(monkeypatch, tmp_path):
    """Hodinový beh nesmie donekonečna búchať do API poskytovateľa."""
    rek = rekonciliacia_modul()
    strany = []

    def otvor(ziadost, timeout=None):
        strany.append(ziadost.full_url)
        return _Odpoved(json.dumps({
            "data": [{"type": "orders", "id": str(len(strany)), "attributes": {}}],
            "links": {"next": True},
        }).encode())

    rek.stiahni_objednavky("KLUC", otvor=otvor)

    assert len(strany) == rek.MAX_STRAN


def test_bez_klucov_rekonciliacia_nic_neurobi(monkeypatch, tmp_path):
    """Vypínač strážia kľúče v uvarsi.env, nie premenná v kóde."""
    monkeypatch.setenv("UVARSI_DB", str(tmp_path / "uvarsi.db"))
    monkeypatch.setenv("UVARSI_ENV_FILE", str(tmp_path / "neexistuje.env"))
    for meno in ("LEMON_API_KEY", "LEMON_WEBHOOK_SECRET", "LEMON_VARIANT_ID"):
        monkeypatch.delenv(meno, raising=False)
    rek = rekonciliacia_modul()
    monkeypatch.setattr(rek, "DB", str(tmp_path / "uvarsi.db"))
    monkeypatch.setattr(rek, "ENV_FILE", str(tmp_path / "neexistuje.env"))

    def zakazane(*args, **kwargs):
        raise AssertionError("bez kľúča sa nesmie siahnuť na API poskytovateľa")

    monkeypatch.setattr(rek, "stiahni_objednavky", zakazane)

    assert rek.main() == 0
    assert not (tmp_path / "uvarsi.db").exists(), "bez kľúčov sa ani databáza neotvára"


def test_nedostupne_api_je_dovod_skusit_o_hodinu_znova(monkeypatch, tmp_path):
    """Výpadok siete nesmie vyzerať ako úspech — cron to musí vidieť."""
    monkeypatch.setenv("UVARSI_DB", str(tmp_path / "uvarsi.db"))
    env_subor = tmp_path / "uvarsi.env"
    env_subor.write_text("LEMON_API_KEY=abc\nLEMON_WEBHOOK_SECRET=def\n", encoding="utf-8")
    monkeypatch.setenv("UVARSI_ENV_FILE", str(env_subor))
    for meno in ("LEMON_API_KEY", "LEMON_WEBHOOK_SECRET", "LEMON_VARIANT_ID"):
        monkeypatch.delenv(meno, raising=False)
    rek = rekonciliacia_modul()
    monkeypatch.setattr(rek, "DB", str(tmp_path / "uvarsi.db"))
    monkeypatch.setattr(rek, "ENV_FILE", str(env_subor))

    def padne(*args, **kwargs):
        raise urllib.error.URLError("sieť nefunguje")

    monkeypatch.setattr(rek, "stiahni_objednavky", padne)

    assert rek.main() == 1


# --------------------------------------------- 2. webhook s vypnutými platbami
def test_webhook_s_vypnutymi_platbami_sa_odlozi_a_nestrati(monkeypatch, tmp_path):
    """503 by poskytovateľ po pár pokusoch vzdal a peniaze by zmizli."""
    server = load_server(monkeypatch, tmp_path, LEMON_WEBHOOK_SECRET=TAJOMSTVO)
    vytvor_pouzivatela(server)

    def zakazany_env(key, default=None):
        assert not key.startswith("LEMON"), f"vypnuté platby nesmú čítať {key}"
        return "" if key == "PLATBY_ZAPNUTE" else default

    monkeypatch.setattr(server, "env", zakazany_env)
    client = TestClient(server.app, raise_server_exceptions=False)
    response = posli_webhook(client, objednavka(order_id="ord-odlozena"))

    assert response.status_code == 200, "poskytovateľ musí dostať potvrdenie, inak to vzdá"
    assert response.json()["akcia"] == "odlozene"
    assert naroky(server) == [], "vypnuté platby stále nesmú udeliť nárok"
    with closing(server.db()) as con:
        odlozene = [dict(r) for r in con.execute("SELECT * FROM platobne_odlozene")]
    assert len(odlozene) == 1
    assert odlozene[0]["spracovane_o"] is None
    assert json.loads(odlozene[0]["telo"])["data"]["id"] == "ord-odlozena"


def test_odlozeny_webhook_sa_spracuje_az_ked_su_platby_zapnute(monkeypatch, tmp_path):
    server = load_server(monkeypatch, tmp_path, LEMON_WEBHOOK_SECRET=TAJOMSTVO)
    platby = platby_modul()
    vytvor_pouzivatela(server)
    client = TestClient(server.app, raise_server_exceptions=False)
    posli_webhook(client, objednavka(order_id="ord-odlozena"))
    assert naroky(server) == []

    with closing(server.db()) as con:
        suhrn = platby.spracuj_odlozene(con, tajomstvo=TAJOMSTVO, now=server.AUTH_CLOCK())

    assert suhrn["spracovane"] == 1 and suhrn["udelene"] == 1
    assert aktivne(server)[0]["objednavka_id"] == "ord-odlozena"


def test_odlozeny_webhook_s_falosnym_podpisom_neudeli_nic(monkeypatch, tmp_path):
    """Podpis sa overuje pri spracovaní — odloženie ho neobchádza."""
    server = load_server(monkeypatch, tmp_path)
    platby = platby_modul()
    vytvor_pouzivatela(server)
    client = TestClient(server.app, raise_server_exceptions=False)
    posli_webhook(client, objednavka(order_id="ord-falosna"), tajomstvo="cudzi-kluc")

    with closing(server.db()) as con:
        suhrn = platby.spracuj_odlozene(con, tajomstvo=TAJOMSTVO, now=server.AUTH_CLOCK())

    assert naroky(server) == []
    assert suhrn["neplatny_podpis"] == 1 and suhrn["udelene"] == 0


def test_odlozeny_webhook_sa_spracuje_prave_raz(monkeypatch, tmp_path):
    server = load_server(monkeypatch, tmp_path, LEMON_WEBHOOK_SECRET=TAJOMSTVO)
    platby = platby_modul()
    vytvor_pouzivatela(server)
    client = TestClient(server.app, raise_server_exceptions=False)
    posli_webhook(client, objednavka(order_id="ord-odlozena"))

    with closing(server.db()) as con:
        platby.spracuj_odlozene(con, tajomstvo=TAJOMSTVO, now=1000.0)
        druha = platby.spracuj_odlozene(con, tajomstvo=TAJOMSTVO, now=2000.0)

    assert druha["spracovane"] == 0
    assert len(aktivne(server)) == 1


def test_rovnake_telo_sa_neodlozi_dvakrat(monkeypatch, tmp_path):
    """Poskytovateľ doručenie opakuje — sklad sa tým nesmie zaplniť."""
    server = load_server(monkeypatch, tmp_path)
    vytvor_pouzivatela(server)
    client = TestClient(server.app, raise_server_exceptions=False)
    posli_webhook(client, objednavka(order_id="ord-1"))
    posli_webhook(client, objednavka(order_id="ord-1"))

    with closing(server.db()) as con:
        assert con.execute("SELECT COUNT(*) FROM platobne_odlozene").fetchone()[0] == 1


def test_telo_bez_hodnoverneho_podpisu_sa_neodklada(monkeypatch, tmp_path):
    """Sklad je pre peniaze, nie pre smeti z internetu."""
    server = load_server(monkeypatch, tmp_path)
    vytvor_pouzivatela(server)
    client = TestClient(server.app, raise_server_exceptions=False)

    response = posli_webhook(client, objednavka(), tajomstvo=None)

    assert response.status_code == 503
    with closing(server.db()) as con:
        assert con.execute("SELECT COUNT(*) FROM platobne_odlozene").fetchone()[0] == 0


def test_sklad_odlozenych_ma_strop(monkeypatch, tmp_path):
    server = load_server(monkeypatch, tmp_path)
    platby = platby_modul()

    with closing(server.db()) as con:
        for index in range(platby.MAX_ODLOZENYCH + 5):
            platby.odloz_webhook(con, telo=f'{{"n":{index}}}'.encode(),
                                 podpis="a" * 64, now=1.0)
        con.commit()
        assert con.execute("SELECT COUNT(*) FROM platobne_odlozene").fetchone()[0] == (
            platby.MAX_ODLOZENYCH
        )


# ---------------------------------------------------------- 3. nad kapacitu
def test_251_platba_upozorni_majitela(monkeypatch, tmp_path):
    server = zapnute_platby(monkeypatch, tmp_path)
    vytvor_pouzivatela(server)
    naplnit_miesta(server, 250)
    poslane = []
    monkeypatch.setattr(server, "posli_upozornenie_majitelovi", poslane.append)
    client = TestClient(server.app, raise_server_exceptions=False)

    response = posli_webhook(client, objednavka(order_id="ord-251"))

    assert response.json()["akcia"] == "nad_kapacitu"
    assert poslane, "peniaze bez protihodnoty sa nesmú stratiť v tichu"
    text = " ".join(f"{s['titul']} {s['sprava']}" for s in poslane)
    assert "ord-251" in text
    assert "@" not in text, "na verejný ntfy kanál nepatrí e-mail zákazníka"


def test_251_platba_sa_dozvie_aj_zakaznik_v_appke(monkeypatch, tmp_path):
    server = zapnute_platby(monkeypatch, tmp_path)
    vytvor_pouzivatela(server)
    naplnit_miesta(server, 250)
    client = prihlaseny(server)
    posli_webhook(client, objednavka(order_id="ord-251"))

    stav = client.get("/api/platba/stav").json()

    assert stav["ma_narok"] is False
    assert stav["platba_bez_miesta"] is True
    assert "vrát" in stav["sprava"].lower(), f"zákazník sa musí dozvedieť o vrátení: {stav['sprava']}"


def test_251_platba_sa_zakaznikovi_aj_posle_mailom(monkeypatch, tmp_path):
    server = zapnute_platby(monkeypatch, tmp_path)
    vytvor_pouzivatela(server, user_id=1, email="neskoro@uvar.si")
    naplnit_miesta(server, 250)
    maily = []
    monkeypatch.setattr(server, "posli_mail",
                        lambda komu, predmet, telo, html: maily.append((komu, predmet, telo)))
    client = TestClient(server.app, raise_server_exceptions=False)

    posli_webhook(client, objednavka(order_id="ord-251"))

    assert [m[0] for m in maily] == ["neskoro@uvar.si"]
    assert "vrát" in maily[0][2].lower()


def test_zlyhany_mail_zakaznikovi_nezhodi_webhook(monkeypatch, tmp_path):
    """Poskytovateľ nesmie dostať 500 len preto, že mailer nefunguje."""
    server = zapnute_platby(monkeypatch, tmp_path)
    vytvor_pouzivatela(server)
    naplnit_miesta(server, 250)

    def padne(*args, **kwargs):
        raise RuntimeError("mailer nefunguje")

    monkeypatch.setattr(server, "posli_mail", padne)
    client = TestClient(server.app, raise_server_exceptions=False)

    response = posli_webhook(client, objednavka(order_id="ord-251"))

    assert response.status_code == 200
    assert response.json()["akcia"] == "nad_kapacitu"


def test_druha_platba_toho_isteho_cloveka_zanecha_stopu_v_narokoch(monkeypatch, tmp_path):
    """Bez riadku by peniaze navyše neexistovali a nemal by ich kto vrátiť."""
    server = zapnute_platby(monkeypatch, tmp_path)
    vytvor_pouzivatela(server)
    client = TestClient(server.app, raise_server_exceptions=False)
    posli_webhook(client, objednavka(order_id="ord-1"))

    druha = posli_webhook(client, objednavka(order_id="ord-2"))

    assert druha.json()["akcia"] == "uz_udelene"
    riadky = naroky(server)
    assert len(riadky) == 2, "druhá platba musí byť dohľadateľná"
    navyse = [r for r in riadky if r["objednavka_id"] == "ord-2"][0]
    assert navyse["stav"] == "duplicitny"
    assert navyse["suma_centy"] == 1900
    assert len(aktivne(server)) == 1


def test_druha_platba_upozorni_majitela(monkeypatch, tmp_path):
    server = zapnute_platby(monkeypatch, tmp_path)
    vytvor_pouzivatela(server)
    poslane = []
    monkeypatch.setattr(server, "posli_upozornenie_majitelovi", poslane.append)
    client = TestClient(server.app, raise_server_exceptions=False)
    posli_webhook(client, objednavka(order_id="ord-1"))
    poslane.clear()

    posli_webhook(client, objednavka(order_id="ord-2"))

    assert poslane
    assert "ord-2" in " ".join(s["sprava"] for s in poslane)


def test_health_ukaze_ze_niekomu_dlzime_vratenie(monkeypatch, tmp_path):
    """Bez SSH sa majiteľ inak nedozvie, že drží peniaze bez protihodnoty."""
    server = zapnute_platby(monkeypatch, tmp_path)
    vytvor_pouzivatela(server)
    naplnit_miesta(server, 250)
    client = TestClient(server.app, raise_server_exceptions=False)
    assert client.get("/api/health").json()["platby"]["nevybavene_vratky"] == 0

    posli_webhook(client, objednavka(order_id="ord-251"))

    platby_stav = client.get("/api/health").json()["platby"]
    assert platby_stav["nevybavene_vratky"] == 1
    assert platby_stav["cakajucich_tiel"] == 0
    assert platby_stav["obsadene"] == 250


def test_health_ukaze_kolko_tiel_caka_s_vypnutymi_platbami(monkeypatch, tmp_path):
    server = load_server(monkeypatch, tmp_path)
    vytvor_pouzivatela(server)
    client = TestClient(server.app, raise_server_exceptions=False)

    posli_webhook(client, objednavka(order_id="ord-1"))

    assert client.get("/api/health").json()["platby"]["cakajucich_tiel"] == 1


# ------------------------------------------------- 4. upozornenia na udalosti
def test_ignorovana_udalost_sa_ohlasi_majitelovi(monkeypatch, tmp_path):
    server = zapnute_platby(monkeypatch, tmp_path)
    vytvor_pouzivatela(server)
    poslane = []
    monkeypatch.setattr(server, "posli_upozornenie_majitelovi", poslane.append)
    client = TestClient(server.app, raise_server_exceptions=False)

    posli_webhook(client, objednavka(udalost="order_created_test"))

    assert poslane, "nespracovateľná udalosť musí dôjsť majiteľovi"
    assert "@" not in " ".join(f"{s['titul']} {s['sprava']}" for s in poslane)


def test_nepouzitelna_udalost_sa_ohlasi_majitelovi(monkeypatch, tmp_path):
    server = zapnute_platby(monkeypatch, tmp_path)
    vytvor_pouzivatela(server)
    poslane = []
    monkeypatch.setattr(server, "posli_upozornenie_majitelovi", poslane.append)
    client = TestClient(server.app, raise_server_exceptions=False)

    response = posli_webhook(client, objednavka(user_id=4242))

    assert response.status_code == 400
    assert poslane


def test_ta_ista_ignorovana_udalost_neupozorni_dvakrat_za_den(monkeypatch, tmp_path):
    """Iný obchod v tom istom účte by inak zaplavil telefón majiteľa."""
    server = zapnute_platby(monkeypatch, tmp_path)
    vytvor_pouzivatela(server)
    poslane = []
    monkeypatch.setattr(server, "posli_upozornenie_majitelovi", poslane.append)
    client = TestClient(server.app, raise_server_exceptions=False)

    posli_webhook(client, objednavka(order_id="a", udalost="order_created_test"))
    posli_webhook(client, objednavka(order_id="b", udalost="order_created_test"))

    assert len(poslane) == 1


def test_upozornenia_nikdy_neobsahuju_osobne_udaje():
    """Ntfy kanál je v repozitári — čo tam pošleme, môže čítať ktokoľvek."""
    platby = platby_modul()
    for druh in platby.DRUHY_UPOZORNENI:
        sprava = platby.priprav_upozornenie(
            druh, den="2026-08-24", objednavka="ord-1", typ="order_created", pocet=3
        )
        cely = f"{sprava['titul']} {sprava['sprava']}"
        assert "@" not in cely, f"{druh}: e-mail nepatrí na verejný kanál"
        for zakazane in ("token", "Bearer", "secret", "heslo"):
            assert zakazane not in cely, f"{druh}: {zakazane} nepatrí na verejný kanál"


def test_modul_platby_stale_nic_nevypisuje():
    """Odosielanie zostáva na volajúcom — platby.py len skladá text."""
    zdroj = (ROOT / "app" / "platby.py").read_text(encoding="utf-8")
    for zakazane in ("print(", "logging", "logger", "sys.stderr", "sys.stdout", "requests"):
        assert zakazane not in zdroj, f"platby.py nesmie obsahovať {zakazane}"


# ------------------------------------------------------------ menšie diery
def test_vratenie_cudzej_objednavky_nesiahne_na_rucne_udeleny_narok(monkeypatch, tmp_path):
    """Vrátenie u poskytovateľa nesmie zobrať nárok, ktorý udelil majiteľ ručne."""
    server = load_server(monkeypatch, tmp_path)
    platby = platby_modul()
    vytvor_pouzivatela(server)
    with closing(server.db()) as con:
        platby.udel_narok_rucne(con, user_id=1, now=1.0)

        vysledok = platby.spracuj_udalost(
            con,
            payload=objednavka(user_id=1, order_id="ord-neznama", udalost="order_refunded"),
            now=2.0,
        )
        assert platby.ma_narok(con, 1) is True

    assert vysledok["akcia"] == "ignorovane"
    assert [r["stav"] for r in naroky(server)] == ["aktivny"]


def test_upratovanie_zmaze_len_stare_udalosti(monkeypatch, tmp_path):
    server = zapnute_platby(monkeypatch, tmp_path)
    platby = platby_modul()
    vytvor_pouzivatela(server)
    client = TestClient(server.app, raise_server_exceptions=False)
    posli_webhook(client, objednavka(order_id="ord-1"))

    with closing(server.db()) as con:
        teraz = server.AUTH_CLOCK()
        con.execute("UPDATE platobne_udalosti SET prijate_o=?",
                    (teraz - 400 * 24 * 3600,))
        con.commit()
        zmazane = platby.uprac_udalosti(con, now=teraz)
        zostatok = con.execute("SELECT COUNT(*) FROM platobne_udalosti").fetchone()[0]

    assert zmazane == 1 and zostatok == 0
    assert len(aktivne(server)) == 1, "upratovanie sa nikdy nedotkne nárokov"


def test_upratovanie_necha_cerstve_udalosti_na_pokoji(monkeypatch, tmp_path):
    server = zapnute_platby(monkeypatch, tmp_path)
    platby = platby_modul()
    vytvor_pouzivatela(server)
    client = TestClient(server.app, raise_server_exceptions=False)
    posli_webhook(client, objednavka(order_id="ord-1"))

    with closing(server.db()) as con:
        assert platby.uprac_udalosti(con, now=server.AUTH_CLOCK()) == 0


def test_cas_v_narokoch_je_vzdy_cislo_aj_ked_pride_datetime(monkeypatch, tmp_path):
    """REAL stĺpec + datetime fungoval len cez zastaraný adaptér — a mieša typy."""
    server = load_server(monkeypatch, tmp_path)
    platby = platby_modul()
    vytvor_pouzivatela(server)

    with closing(server.db()) as con:
        platby.udel_narok_rucne(con, user_id=1, now=datetime.datetime(2026, 8, 24, 12, 0))
        typy = con.execute(
            "SELECT typeof(ziskany_o), typeof(zmeneny_o) FROM naroky"
        ).fetchone()

    assert set(typy) == {"real"}


def test_migracia_prepise_stare_textove_casy_na_cisla(monkeypatch, tmp_path):
    server = load_server(monkeypatch, tmp_path)
    platby = platby_modul()
    vytvor_pouzivatela(server)
    with closing(server.db()) as con:
        con.execute(
            """INSERT INTO naroky (user_id, produkt, poskytovatel, objednavka_id,
                                   suma_centy, mena, stav, ziskany_o, zmeneny_o)
               VALUES (1, 'zakladajuci_clen', 'rucne', 'rucne-1-1', 0, NULL,
                       'aktivny', '2026-08-24 12:00:00', '2026-08-24 12:00:00')"""
        )
        con.commit()

        platby.migrate_platby_schema(con)
        typy = con.execute("SELECT typeof(ziskany_o), typeof(zmeneny_o) FROM naroky").fetchone()

    assert set(typy) == {"real"}


def test_premium_cli_neposiela_datetime_do_realoveho_stlpca():
    zdroj = (ROOT / "app" / "premium_cli.py").read_text(encoding="utf-8")
    assert "now=teraz" in zdroj
    assert "datetime.datetime.now()" not in zdroj, (
        "do REAL stĺpca patrí epocha, nie datetime cez zastaraný adaptér"
    )
