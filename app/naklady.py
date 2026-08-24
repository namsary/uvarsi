#!/usr/bin/env python3
"""Uvar.si — NÁKLADY: evidencia a TVRDÝ strop na míňanie kreditu Anthropic.

Prečo existuje (incident 21. 8. 2026): landing bloček padal deterministicky,
dozorca to vyhodnotil ako dočasnú chybu a opakoval ho až 6× denne. Každý pokus
spustil PLNÝ vision beh Opusom 5 nad ~36 stranami letáku (~0,37 €) a nevyrobil
nič. Za dva dni to minulo celý kredit majiteľa (4,60 €) a dozvedel sa to až
vtedy, keď kredit došiel. Detekcia deterministického pádu je opravená — ale
strop na míňanie neexistoval NIKDE, takže ľubovoľná ďalšia chyba mala rovnaký
neobmedzený dosah.

Tri vrstvy ochrany, v tomto poradí:
  1. `rezervuj_beh` — koľkokrát za týždeň sa vôbec smie spustiť drahá operácia.
     Počíta sa BEH, nie euro, takže rozbehnutá slučka narazí na strop skôr,
     než stihne niečo minúť. Štrukturálna poistka, nie odhad.
  2. `skontroluj` — denný, mesačný a účelový strop v eurách. Volá sa VŽDY PRED
     platením a započítava aj odhad ceny volania, ktoré sa práve chystá.
  3. `zapis` — skutočná spotreba z `usage` odpovede (nie odhad), plus
     jednorazové upozornenie na ntfy pri 50 % a 80 % mesačného stropu.

Zásada: FAIL CLOSED. Keď sa evidencia nedá prečítať alebo strop určiť, drahé
volanie sa NEUSKUTOČNÍ. Radšej appka chvíľu nefunguje, než aby ticho míňala.
"""
import datetime
import math
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path


# ---------------------------------------------------------------- cenník
# Oficiálny cenník Anthropic v USD za milión tokenov. Eurá = USD × 0,92.
EUR_ZA_USD = 0.92
MILION = 1_000_000


@dataclass(frozen=True)
class Tarifa:
    """USD za milión tokenov."""
    vstup: float
    vystup: float
    cache_read: float
    cache_write: float          # 5-minútový zápis do cache


CENNIK_USD = {
    "claude-opus-5":   Tarifa(vstup=5.0, vystup=25.0, cache_read=0.50, cache_write=6.25),
    "claude-sonnet-5": Tarifa(vstup=2.0, vystup=10.0, cache_read=0.20, cache_write=2.50),
    "claude-haiku-4-5": Tarifa(vstup=1.0, vystup=5.0, cache_read=0.10, cache_write=1.25),
}
# Neznámy model sa účtuje najdrahšou sadzbou. Premenovaný model nesmie
# spôsobiť, že sa jeho spotreba ticho zaeviduje ako lacnejšia, než naozaj je.
NAJDRAHSIA_TARIFA = CENNIK_USD["claude-opus-5"]

UCELY = ("zber_letakov", "blocek", "plan", "recepty")

# Koľko typicky stojí JEDNO volanie (nie celý beh). Používa sa na dve veci: ako
# odhad PRED volaním, aby sa strop nedal prekročiť ani o jedno volanie, a ako
# konzervatívny zápis, keď odpoveď `usage` neprinesie (spadnuté volanie).
# Nadhodnotiť sa neoplatí: príliš vysoký odhad by zastavil aj poctivý beh.
ODHAD_EUR = {
    "zber_letakov": 0.10,     # jedna vision dávka (~4 strany letáku Opusom)
    "blocek": 0.02,
    "plan": 0.02,
    "recepty": 0.02,
}

# Ten istý kanál, ktorý už sleduje dozorca (hetzner/dozorca.sh).
NTFY_TOPIC = "uvarsi-jarvis-8f3a2c"
PRAHY_UPOZORNENIA = (50, 80)

# ---------------------------------------------------------------- východzie stropy
# Kredit, ktorý incident vynuloval, bol 4,60 €. Mesačný strop 5 € je teda
# „celý doterajší rozpočet za mesiac“ — nie svojvoľné číslo. Denný strop 1 €
# drží najhorší deň na pätine mesiaca: incident míňal ~2,20 €/deň, takže by ho
# bol zastavil hneď v prvý deň. Týždenné 2 behy zberu = jeden riadny beh plus
# jedno opakovanie, keď zdroj letákov vypadne.
VYCHODZI_DENNY_STROP_EUR = 1.00
VYCHODZI_MESACNY_STROP_EUR = 5.00
VYCHODZI_TYZDENNY_STROP_ZBER_EUR = 1.50
VYCHODZI_LIMIT_BEHOV = {"zber_letakov": 2}

PREMENNE_PROSTREDIA = (
    "UVARSI_DENNY_STROP_EUR",
    "UVARSI_MESACNY_STROP_EUR",
    "UVARSI_TYZDENNY_STROP_ZBER_EUR",
    "UVARSI_TYZDENNE_BEHY_ZBER",
)

KOD_DENNY = "rozpocet_denny"
KOD_MESACNY = "rozpocet_mesacny"
KOD_UCEL = "rozpocet_ucel"
KOD_BEHY = "rozpocet_behy"
KOD_NECITATELNY = "rozpocet_necitatelny"

SPRAVA_NECITATELNY = (
    "Rozpočet sa nedá overiť, tak sme platené volanie nespustili. "
    "Radšej nič než tiché míňanie."
)


class RozpocetVycerpany(Exception):
    """Typované odmietnutie: drahé volanie sa NEUSKUTOČNILO.

    `kod` je strojovo čitateľný dôvod, `str(chyba)` je veta pre človeka.
    Volajúci musí degradovať viditeľne a pravdivo — nikdy si nevymyslieť dáta.
    """

    def __init__(self, sprava, *, kod, ucel=None, minute_eur=None, strop_eur=None):
        super().__init__(sprava)
        self.kod = kod
        self.ucel = ucel
        self.minute_eur = minute_eur
        self.strop_eur = strop_eur


@dataclass(frozen=True)
class Stropy:
    denny: float
    mesacny: float
    tyzdenny_ucel: dict


# ---------------------------------------------------------------- cena
def normalizuj_model(model) -> str:
    """'claude-haiku-4-5-20251001' → 'claude-haiku-4-5' (dátumová prípona preč)."""
    nazov = str(model or "").strip().lower()
    if nazov in CENNIK_USD:
        return nazov
    zaklad, _, chvost = nazov.rpartition("-")
    if zaklad and len(chvost) == 8 and chvost.isdigit():
        return zaklad
    return nazov


def tarifa_pre(model) -> Tarifa:
    return CENNIK_USD.get(normalizuj_model(model), NAJDRAHSIA_TARIFA)


def cena_eur(model, vstup=0, vystup=0, cache_write=0, cache_read=0) -> float:
    """Cena jedného volania v eurách podľa skutočne spotrebovaných tokenov."""
    t = tarifa_pre(model)
    usd = (
        int(vstup or 0) * t.vstup
        + int(vystup or 0) * t.vystup
        + int(cache_write or 0) * t.cache_write
        + int(cache_read or 0) * t.cache_read
    ) / MILION
    return round(usd * EUR_ZA_USD, 6)


def usage_do_slovnika(usage) -> dict | None:
    """Skutočné čísla z odpovede API. None = odpoveď spotrebu neuviedla."""
    if usage is None:
        return None
    tokeny = {
        "vstup": getattr(usage, "input_tokens", None),
        "vystup": getattr(usage, "output_tokens", None),
        "cache_write": getattr(usage, "cache_creation_input_tokens", None),
        "cache_read": getattr(usage, "cache_read_input_tokens", None),
    }
    if all(hodnota is None for hodnota in tokeny.values()):
        return None
    return {kluc: int(hodnota or 0) for kluc, hodnota in tokeny.items()}


# ---------------------------------------------------------------- konfigurácia
def _euro_z_prostredia(nazov, vychodzie) -> float:
    surove = os.environ.get(nazov)
    if surove is None or not surove.strip():
        return float(vychodzie)
    try:
        hodnota = float(surove.strip().replace(",", "."))
    except (TypeError, ValueError) as chyba:
        raise RozpocetVycerpany(SPRAVA_NECITATELNY, kod=KOD_NECITATELNY) from chyba
    if not math.isfinite(hodnota) or hodnota < 0:
        raise RozpocetVycerpany(SPRAVA_NECITATELNY, kod=KOD_NECITATELNY)
    return hodnota


def _cele_z_prostredia(nazov, vychodzie) -> int:
    surove = os.environ.get(nazov)
    if surove is None or not surove.strip():
        return int(vychodzie)
    try:
        hodnota = int(surove.strip())
    except (TypeError, ValueError) as chyba:
        raise RozpocetVycerpany(SPRAVA_NECITATELNY, kod=KOD_NECITATELNY) from chyba
    if hodnota < 0:
        raise RozpocetVycerpany(SPRAVA_NECITATELNY, kod=KOD_NECITATELNY)
    return hodnota


def stropy() -> Stropy:
    """Aktuálne stropy. Pokazená hodnota v prostredí = odmietnutie, nie default."""
    return Stropy(
        denny=_euro_z_prostredia("UVARSI_DENNY_STROP_EUR", VYCHODZI_DENNY_STROP_EUR),
        mesacny=_euro_z_prostredia("UVARSI_MESACNY_STROP_EUR", VYCHODZI_MESACNY_STROP_EUR),
        tyzdenny_ucel={
            "zber_letakov": _euro_z_prostredia(
                "UVARSI_TYZDENNY_STROP_ZBER_EUR", VYCHODZI_TYZDENNY_STROP_ZBER_EUR
            ),
        },
    )


def limit_behov(ucel) -> int:
    """Koľkokrát za ISO týždeň sa smie drahá operácia vôbec spustiť."""
    if ucel == "zber_letakov":
        return _cele_z_prostredia("UVARSI_TYZDENNE_BEHY_ZBER", VYCHODZI_LIMIT_BEHOV[ucel])
    return VYCHODZI_LIMIT_BEHOV.get(ucel, 0)


# ---------------------------------------------------------------- databáza
SCHEMA = """
CREATE TABLE IF NOT EXISTS naklady (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  cas         TEXT NOT NULL,          -- ISO čas volania
  den         TEXT NOT NULL,          -- YYYY-MM-DD (denný strop)
  mesiac      TEXT NOT NULL,          -- YYYY-MM (mesačný strop)
  tyzden      TEXT NOT NULL,          -- ISO pondelok (týždenný strop účelu)
  ucel        TEXT NOT NULL,          -- zber_letakov|blocek|plan|recepty
  model       TEXT NOT NULL,
  vstup       INTEGER NOT NULL DEFAULT 0,
  vystup      INTEGER NOT NULL DEFAULT 0,
  cache_write INTEGER NOT NULL DEFAULT 0,
  cache_read  INTEGER NOT NULL DEFAULT 0,
  eur         REAL NOT NULL DEFAULT 0,
  odhad       INTEGER NOT NULL DEFAULT 0,   -- 1 = odpoveď spotrebu neuviedla
  detail      TEXT
);
CREATE INDEX IF NOT EXISTS idx_naklady_den ON naklady(den);
CREATE INDEX IF NOT EXISTS idx_naklady_mesiac ON naklady(mesiac);
CREATE INDEX IF NOT EXISTS idx_naklady_ucel ON naklady(ucel, tyzden);

-- Počítadlo BEHOV drahej operácie za týždeň. Toto je štrukturálna poistka
-- proti slučke: nezáleží na tom, koľko beh stál, ale koľkokrát sa spustil.
CREATE TABLE IF NOT EXISTS naklady_behy (
  tyzden  TEXT NOT NULL,
  ucel    TEXT NOT NULL,
  pocet   INTEGER NOT NULL DEFAULT 0,
  updated TEXT,
  PRIMARY KEY (tyzden, ucel)
);

-- Jedno upozornenie na (mesiac, prah). Primárny kľúč je tá záruka — bez neho
-- by pri každom volaní nad prahom odišla ďalšia notifikácia.
CREATE TABLE IF NOT EXISTS naklady_upozornenia (
  mesiac  TEXT NOT NULL,
  prah    INTEGER NOT NULL,
  poslane TEXT,
  PRIMARY KEY (mesiac, prah)
);
"""


def migrate_naklady_schema(con) -> None:
    con.executescript(SCHEMA)


def pripoj(cesta=None):
    """Spojenie na evidenciu nákladov. Východzie je tá istá uvarsi.db."""
    cesta = cesta or os.environ.get("UVARSI_DB", "/opt/uvarsi/uvarsi.db")
    Path(cesta).parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(cesta), timeout=20)
    con.row_factory = sqlite3.Row
    migrate_naklady_schema(con)
    return con


def _teraz(teraz=None) -> datetime.datetime:
    return teraz or datetime.datetime.now()


def _obdobia(teraz):
    den = teraz.date()
    pondelok = den - datetime.timedelta(days=den.weekday())
    return den.isoformat(), den.strftime("%Y-%m"), pondelok.isoformat()


def _suma(con, kde, parametre) -> float:
    riadok = con.execute(
        f"SELECT COALESCE(SUM(eur), 0) FROM naklady WHERE {kde}", parametre
    ).fetchone()
    return float(riadok[0] or 0.0)


def spolu_za_den(con, den) -> float:
    return _suma(con, "den = ?", (den,))


def spolu_za_mesiac(con, mesiac) -> float:
    return _suma(con, "mesiac = ?", (mesiac,))


def spolu_za_ucel_tyzden(con, ucel, tyzden) -> float:
    return _suma(con, "ucel = ? AND tyzden = ?", (ucel, tyzden))


# ---------------------------------------------------------------- strop PRED volaním
def skontroluj(con, ucel, *, odhad_eur=None, teraz=None):
    """Smie sa teraz minúť? Keď nie, vyhodí RozpocetVycerpany a NIČ sa nevolá.

    Započítava sa aj odhad ceny volania, ktoré sa práve chystá — inak by sa
    strop dal prekročiť vždy práve o jedno (a to najdrahšie) volanie.
    """
    if ucel not in UCELY:
        raise RozpocetVycerpany(
            f"Neznámy účel platby „{ucel}“ — volanie nespúšťam.", kod=KOD_NECITATELNY
        )
    teraz = _teraz(teraz)
    den, mesiac, tyzden = _obdobia(teraz)
    limity = stropy()                       # pokazené prostredie → RozpocetVycerpany
    odhad = ODHAD_EUR.get(ucel, 0.0) if odhad_eur is None else float(odhad_eur)
    odhad = max(odhad, 0.0)

    try:
        dnes_eur = spolu_za_den(con, den)
        mesiac_eur = spolu_za_mesiac(con, mesiac)
        ucel_eur = spolu_za_ucel_tyzden(con, ucel, tyzden)
    except (sqlite3.Error, OSError) as chyba:
        # Nevieme, koľko už padlo → nesmieme minúť ani cent.
        raise RozpocetVycerpany(SPRAVA_NECITATELNY, kod=KOD_NECITATELNY, ucel=ucel) from chyba

    if dnes_eur + odhad > limity.denny:
        raise RozpocetVycerpany(
            f"Dnešný rozpočet na AI je vyčerpaný ({dnes_eur:.2f} € z {limity.denny:.2f} €). "
            "Skús to zajtra.",
            kod=KOD_DENNY, ucel=ucel, minute_eur=dnes_eur, strop_eur=limity.denny,
        )
    if mesiac_eur + odhad > limity.mesacny:
        raise RozpocetVycerpany(
            f"Mesačný rozpočet na AI je vyčerpaný ({mesiac_eur:.2f} € z {limity.mesacny:.2f} €). "
            "Do nového mesiaca sa platené volania nespúšťajú.",
            kod=KOD_MESACNY, ucel=ucel, minute_eur=mesiac_eur, strop_eur=limity.mesacny,
        )
    strop_ucelu = limity.tyzdenny_ucel.get(ucel)
    if strop_ucelu is not None and ucel_eur + odhad > strop_ucelu:
        raise RozpocetVycerpany(
            f"Týždenný rozpočet na „{ucel}“ je vyčerpaný "
            f"({ucel_eur:.2f} € z {strop_ucelu:.2f} €).",
            kod=KOD_UCEL, ucel=ucel, minute_eur=ucel_eur, strop_eur=strop_ucelu,
        )
    return {"dnes_eur": dnes_eur, "mesiac_eur": mesiac_eur, "ucel_eur": ucel_eur}


def rezervuj_beh(con, ucel, *, teraz=None):
    """Zaber jedno miesto z týždenného počtu behov drahej operácie.

    Toto je poistka, ktorá robí rozbehnutú slučku ŠTRUKTURÁLNE nemožnou:
    dvanásť pokusov za dva dni si vypýta dvanásť rezervácií a od tretej
    dostane odmietnutie — bez ohľadu na to, koľko ktorý beh stál.
    """
    teraz = _teraz(teraz)
    _, _, tyzden = _obdobia(teraz)
    limit = limit_behov(ucel)
    try:
        with con:
            riadok = con.execute(
                "SELECT pocet FROM naklady_behy WHERE tyzden=? AND ucel=?", (tyzden, ucel)
            ).fetchone()
            pocet = int(riadok["pocet"]) if riadok else 0
            if pocet >= limit:
                raise RozpocetVycerpany(
                    f"Drahá operácia „{ucel}“ už tento týždeň bežala {pocet}× "
                    f"(strop {limit}×). Ďalší beh nespúšťam.",
                    kod=KOD_BEHY, ucel=ucel,
                )
            con.execute(
                """INSERT INTO naklady_behy (tyzden, ucel, pocet, updated)
                   VALUES (?, ?, 1, ?)
                   ON CONFLICT(tyzden, ucel) DO UPDATE SET
                     pocet = pocet + 1, updated = excluded.updated""",
                (tyzden, ucel, teraz.isoformat(timespec="seconds")),
            )
            con.execute("DELETE FROM naklady_behy WHERE tyzden < ?", (tyzden,))
    except RozpocetVycerpany:
        raise
    except (sqlite3.Error, OSError) as chyba:
        raise RozpocetVycerpany(SPRAVA_NECITATELNY, kod=KOD_NECITATELNY, ucel=ucel) from chyba
    return pocet + 1


# ---------------------------------------------------------------- zápis spotreby
def zapis(con, ucel, model, usage=None, *, detail=None, teraz=None,
          odhad_eur=None, notifikuj=None) -> float:
    """Zapíš skutočnú spotrebu volania a vráť jeho cenu v eurách.

    Keď odpoveď `usage` neprinesie (spadnuté volanie, staré SDK), zapíše sa
    konzervatívny odhad označený príznakom `odhad`. Nula sa nezapisuje nikdy —
    diera v evidencii je presne to, čo umožnilo incident.
    """
    teraz = _teraz(teraz)
    den, mesiac, tyzden = _obdobia(teraz)
    tokeny = usage_do_slovnika(usage)
    if tokeny is None:
        tokeny = {"vstup": 0, "vystup": 0, "cache_write": 0, "cache_read": 0}
        eur = float(ODHAD_EUR.get(ucel, 0.0) if odhad_eur is None else odhad_eur)
        je_odhad = 1
    else:
        eur = cena_eur(model, **tokeny)
        je_odhad = 0

    con.execute(
        """INSERT INTO naklady
           (cas, den, mesiac, tyzden, ucel, model,
            vstup, vystup, cache_write, cache_read, eur, odhad, detail)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (teraz.isoformat(timespec="seconds"), den, mesiac, tyzden, str(ucel),
         normalizuj_model(model), tokeny["vstup"], tokeny["vystup"],
         tokeny["cache_write"], tokeny["cache_read"], eur, je_odhad,
         None if detail is None else str(detail)[:300]),
    )
    con.commit()
    _upozorni_ak_treba(con, mesiac, notifikuj)
    return eur


def s_rozpoctom(con, ucel, model, volanie, *, odhad_eur=None, detail=None,
                teraz=None, notifikuj=None):
    """Skontroluj strop → zavolaj → zapíš skutočnú spotrebu.

    Jediný správny spôsob, ako v tejto appke zaplatiť za volanie modelu. Keď
    `volanie` spadne, zapíše sa konzervatívny odhad: spadnuté volanie mohlo
    tokeny minúť a tváriť sa, že bolo zadarmo, je presne tá chyba, ktorá
    dovolila incidentu bežať dva dni.
    """
    skontroluj(con, ucel, odhad_eur=odhad_eur, teraz=teraz)
    try:
        odpoved = volanie()
    except BaseException as chyba:
        zapis(con, ucel, model, None, teraz=teraz, odhad_eur=odhad_eur,
              detail=f"zlyhalo: {type(chyba).__name__}", notifikuj=notifikuj)
        raise
    zapis(con, ucel, model, getattr(odpoved, "usage", None), detail=detail,
          teraz=teraz, odhad_eur=odhad_eur, notifikuj=notifikuj)
    return odpoved


class _StrazeneSpravy:
    def __init__(self, strazeny):
        self._s = strazeny

    def create(self, **kw):
        return s_rozpoctom(
            self._s.con, self._s.ucel, kw.get("model"),
            lambda: self._s.klient.messages.create(**kw),
            odhad_eur=self._s.odhad_eur, teraz=self._s.teraz,
            notifikuj=self._s.notifikuj,
        )


class StrazenyKlient:
    """Klient Anthropic, cez ktorý sa NEDÁ zavolať model bez zaúčtovania.

    Obaľuje sa celé rozhranie, nie jednotlivé volania — vďaka tomu nemôže
    vzniknúť call site, ktorý na strop zabudne. Kto má klienta, má aj strop.
    """

    def __init__(self, con, klient, ucel, *, odhad_eur=None, teraz=None, notifikuj=None):
        self.con = con
        self.klient = klient
        self.ucel = ucel
        self.odhad_eur = odhad_eur
        self.teraz = teraz
        self.notifikuj = notifikuj
        self.messages = _StrazeneSpravy(self)


def strazeny_klient(con, klient, ucel, *, odhad_eur=None, teraz=None, notifikuj=None):
    return StrazenyKlient(con, klient, ucel, odhad_eur=odhad_eur, teraz=teraz,
                          notifikuj=notifikuj)


# ---------------------------------------------------------------- upozornenia
def posli_ntfy(sprava: dict) -> None:
    """Najlepšia snaha — notifikácia nikdy nesmie zhodiť platený beh."""
    try:
        import requests

        requests.post(
            f"https://ntfy.sh/{NTFY_TOPIC}",
            data=sprava["sprava"].encode("utf-8"),
            headers={"Title": sprava["titul"]},
            timeout=15,
        )
    except Exception:
        pass


def _upozorni_ak_treba(con, mesiac, notifikuj=None) -> None:
    posli = posli_ntfy if notifikuj is None else notifikuj
    try:
        limity = stropy()
        minute = spolu_za_mesiac(con, mesiac)
    except (RozpocetVycerpany, sqlite3.Error, OSError):
        return
    if limity.mesacny <= 0:
        return
    for prah in PRAHY_UPOZORNENIA:
        if minute < limity.mesacny * prah / 100.0:
            continue
        # Primárny kľúč (mesiac, prah) je záruka „práve raz“: druhý pokus
        # o ten istý prah sa ticho zahodí a notifikácia už neodíde.
        kurzor = con.execute(
            "INSERT OR IGNORE INTO naklady_upozornenia (mesiac, prah, poslane) VALUES (?, ?, ?)",
            (mesiac, prah, datetime.datetime.now().isoformat(timespec="seconds")),
        )
        con.commit()
        if kurzor.rowcount != 1:
            continue
        try:
            posli({
                "titul": f"Uvar.si: minuté {prah} % mesačného rozpočtu na AI",
                "sprava": (
                    f"Mesiac {mesiac}: minuté {minute:.2f} € z {limity.mesacny:.2f} € "
                    f"({prah} % stropu). Pri 100 % sa platené volania zastavia."
                ),
            })
        except Exception:
            pass


# ---------------------------------------------------------------- prehľad
def stav(con, teraz=None, limit_poslednych=5) -> dict:
    """Kam išli peniaze — bez SSH a bez tajomstiev.

    Diagnostika nesmie nikdy zhodiť /api/health, tak sa pokazená evidencia
    prizná v poli `chyba` namiesto toho, aby predstierala nulu.
    """
    teraz = _teraz(teraz)
    den, mesiac, tyzden = _obdobia(teraz)
    zaklad = {"den": den, "mesiac": mesiac, "tyzden": tyzden, "chyba": None}
    try:
        limity = stropy()
        dnes_eur = round(spolu_za_den(con, den), 6)
        mesiac_eur = round(spolu_za_mesiac(con, mesiac), 6)
        posledne = [
            {
                "cas": r["cas"], "ucel": r["ucel"], "model": r["model"],
                "eur": round(float(r["eur"]), 6), "odhad": bool(r["odhad"]),
            }
            for r in con.execute(
                "SELECT cas, ucel, model, eur, odhad FROM naklady "
                "ORDER BY id DESC LIMIT ?", (int(limit_poslednych),)
            )
        ]
        behy = {}
        for ucel in UCELY:
            limit = limit_behov(ucel)
            if not limit:
                continue
            riadok = con.execute(
                "SELECT pocet FROM naklady_behy WHERE tyzden=? AND ucel=?", (tyzden, ucel)
            ).fetchone()
            behy[ucel] = {
                "tyzden": tyzden,
                "pocet": int(riadok["pocet"]) if riadok else 0,
                "limit": limit,
            }
    except (RozpocetVycerpany, sqlite3.Error, OSError) as chyba:
        return {**zaklad, "chyba": f"{type(chyba).__name__}: {chyba}"[:200]}

    return {
        **zaklad,
        "dnes_eur": dnes_eur,
        "mesiac_eur": mesiac_eur,
        "denny_strop_eur": limity.denny,
        "mesacny_strop_eur": limity.mesacny,
        "zostatok_dnes_eur": round(max(limity.denny - dnes_eur, 0.0), 6),
        "zostatok_mesiac_eur": round(max(limity.mesacny - mesiac_eur, 0.0), 6),
        "behy": behy,
        "posledne": posledne,
    }
