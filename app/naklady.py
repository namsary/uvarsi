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

Druhý incident (24. 8. 2026) ukázal opačnú dieru: majiteľovi došiel kredit a
API začalo každé volanie odmietať HTTP chybou 400 ešte pred vykonaním práce.
`s_rozpoctom` to bral ako obyčajné spadnuté volanie a zaúčtoval konzervatívny
odhad — takže oba týždňové behy zbierača padli na volania, ktoré nikdy
nebežali, a /api/naklady hlásilo 0,66 € „minutých", ktoré nikto nezaplatil.
Odtiaľ štvrtá vrstva:
  4. `je_nedostatok_kreditu` — odmietnutie pre nulový kredit je VLASTNÝ druh
     zlyhania (`KreditVycerpany`). Neúčtuje sa, nezoberie miesto v týždennom
     počte behov, upozorní práve raz za deň a je vidieť na /api/health.
     Ostatné spadnuté volania sa účtujú ďalej — timeout tokeny minúť mohol.
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

# `predpocet` je ten istý model a tá istá práca ako `plan`, len ju nikto
# nečaká — v noci sa dopredu poskladajú najžiadanejšie zdieľané jedálničky.
# Vlastný účel má preto, aby bolo v /api/naklady vidieť zvlášť, koľko stálo
# zahrievanie a koľko plány, ktoré si vypýtali ľudia.
UCELY = ("zber_letakov", "blocek", "plan", "recepty", "predpocet")

# Koľko typicky stojí JEDNO volanie (nie celý beh). Používa sa na dve veci: ako
# odhad PRED volaním, aby sa strop nedal prekročiť ani o jedno volanie, a ako
# konzervatívny zápis, keď odpoveď `usage` neprinesie (spadnuté volanie).
# Nadhodnotiť sa neoplatí: príliš vysoký odhad by zastavil aj poctivý beh.
ODHAD_EUR = {
    "zber_letakov": 0.10,     # jedna vision dávka (~4 strany letáku Opusom)
    "blocek": 0.02,
    "plan": 0.03,            # 7 detailných jedál, konzervatívne aj pri timeoute
    "recepty": 0.02,
    "predpocet": 0.03,        # to isté volanie ako „plan", len v noci
}

# Ten istý kanál, ktorý už sleduje dozorca (hetzner/dozorca.sh).
NTFY_TOPIC = "uvarsi-jarvis-8f3a2c"
PRAHY_UPOZORNENIA = (50, 80)

# ---------------------------------------------------------------- východzie stropy
# PREKALIBROVANÉ 24. 8. 2026. Predošlé čísla (denný 1,50 €, mesačný 8,00 €,
# týždenný zber 2,50 €) pochádzali z čias, keď fáza 2 čítala len rovnomernú
# vzorku 12–14 strán z každého letáku. Tú vzorku sme neskôr ZRUŠILI — dnes sa
# číta každá potravinová strana, teda z 208 strán zhruba 104.
#
# Skutočný náklad jedného poctivého behu je preto ~2,50 €, nie 1,11 €:
#   • fáza 1 (Haiku, náhľady 320 px): 19 volaní ≈ 0,03 €
#   • fáza 2 (Opus 5, 1500 px): ~27 volaní × 0,093 € ≈ 2,51 €
#
# Starý denný strop 1,50 € teda beh PRERUŠIL niekde v polovici. Obchody sa
# spracúvajú v poradí Kaufland → Tesco → Lidl, takže vypadával ten posledný.
# 21. 8. 2026: 431 akcií v DB a chýbal presne Lidl. Strop nechránil, ale kazil
# dáta — a chyba vyzerala ako problém so zdrojom letáku.
#
# Nové čísla vychádzajú z meranej prevádzky, stále s rezervou:
#   denný 4,00 €  = celý zber (2,50 €) + plány a bloček toho dňa
#   týždenný zber 4,00 € = jeden celý beh + jeden opravný pokus
#   mesačný 25,00 € = 4 zbery (~10 €) + plány + predpočet, s rezervou;
#                     stále hlboko pod majiteľovým limitom 100 €/mesiac
#   2 behy zberu za týždeň ostáva — to bola správna poistka a zafungovala
#
# POZOR: keď sa zmení rozsah čítania (READ_PX, READ_BATCH_SIZE, podiel
# potravinových strán) alebo cena modelu, tieto čísla treba prepočítať znova.
# Strop pod skutočnou cenou poctivého behu nie je ochrana, je to tichý výpadok.
VYCHODZI_DENNY_STROP_EUR = 4.00
VYCHODZI_MESACNY_STROP_EUR = 25.00
VYCHODZI_TYZDENNY_STROP_ZBER_EUR = 4.00
VYCHODZI_TYZDENNY_STROP_PREDPOCET_EUR = 0.40
VYCHODZI_LIMIT_BEHOV = {"zber_letakov": 2, "predpocet": 2}

# Ktorý účel si strop počtu behov berie z ktorej premennej prostredia.
PREMENNA_BEHOV = {
    "zber_letakov": "UVARSI_TYZDENNE_BEHY_ZBER",
    "predpocet": "UVARSI_TYZDENNE_BEHY_PREDPOCET",
}

PREMENNE_PROSTREDIA = (
    "UVARSI_DENNY_STROP_EUR",
    "UVARSI_MESACNY_STROP_EUR",
    "UVARSI_TYZDENNY_STROP_ZBER_EUR",
    "UVARSI_TYZDENNE_BEHY_ZBER",
    "UVARSI_TYZDENNY_STROP_PREDPOCET_EUR",
    "UVARSI_TYZDENNE_BEHY_PREDPOCET",
)

KOD_DENNY = "rozpocet_denny"
KOD_MESACNY = "rozpocet_mesacny"
KOD_UCEL = "rozpocet_ucel"
KOD_BEHY = "rozpocet_behy"
KOD_NECITATELNY = "rozpocet_necitatelny"
KOD_KREDIT = "kredit_vycerpany"

SPRAVA_NECITATELNY = (
    "Rozpočet sa nedá overiť, tak sme platené volanie nespustili. "
    "Radšej nič než tiché míňanie."
)

# Text pre používateľa. Žiadne euro číslo — nič sa neminulo a číslo by klamalo.
SPRAVA_KREDIT = (
    "Nový jedálniček sa teraz nedá poskladať: prístup k AI je pozastavený, "
    "lebo na účte došiel kredit. Kým ho majiteľ nedobije, appka radšej nič "
    "nevygeneruje, než by si vymýšľala jedlá alebo ukazovala staré ceny."
)
# Prečo v appke nie sú akcie na tento týždeň. Sľub „skús to o chvíľu" platí len
# vtedy, keď sa zber naozaj obnovuje — pri nulovom kredite nemá ako dobehnúť.
SPRAVA_KREDIT_AKCIE = (
    "Akcie z letákov sa tento týždeň nenačítali: prístup k AI je pozastavený, "
    "lebo na účte došiel kredit. Staré ceny ti radšej neukazujeme ako dnešné. "
    "Ozveme sa, len čo bude appka opäť plne funkčná."
)
# Text pre majiteľa na ntfy — musí povedať, čo má urobiť.
TITUL_KREDIT = "Uvar.si: došiel kredit na Anthropic API"
SPRAVA_KREDIT_NTFY = (
    "API odmieta všetky volania — na účte je nulový kredit. Appka nevie "
    "generovať jedálničky ani landing bloček, kým kredit nedobiješ. "
    "Nič sa neúčtovalo (odmietnuté volania nespotrebovali ani token) a "
    "opakované pokusy sú zastavené, aby log nezaplavili."
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


class KreditVycerpany(RozpocetVycerpany):
    """API odmietlo volanie pre nulový kredit — NIČ sa nevykonalo.

    Tretí druh zlyhania, odlišný od oboch existujúcich:
      • nie je to dočasná chyba — opakovanie nepomôže, kým človek nedobije
        kredit, takže hodinové pokusy sú čistá strata (a zaplavia log);
      • nie je to ani normálne spadnuté volanie — to mohlo tokeny minúť
        a preto sa konzervatívne účtuje. Odmietnutie pre kredit príde skôr,
        než API čokoľvek prečíta, takže účtovať niet čo.
    Dedí z RozpocetVycerpany zámerne: každé miesto, ktoré už vie „platené
    volanie sa neuskutočnilo, degraduj pravdivo", sa zachová správne aj tu.
    """

    def __init__(self, sprava=SPRAVA_KREDIT, *, ucel=None):
        super().__init__(sprava, kod=KOD_KREDIT, ucel=ucel)


# Anthropic pre „došiel kredit" NEMÁ vlastný kód chyby: vráti HTTP 400 a v tele
# generický `error.type = "invalid_request_error"`, ktorý používa aj pre pokazené
# parametre (max_tokens, neznámy model…). Štruktúra teda stav zúži, ale sama ho
# neurčí. Preto:
#   1. povinne overíme, čo sa overiť dá — stavový kód 400 a typ chyby,
#   2. až potom hľadáme kanonickú anglickú vetu z API. Je to jediné rozlíšenie,
#      ktoré existuje. Zhoda je úmyselne úzka („credit balance is too low") a
#      hľadá sa v `error.message` z rozparsovaného tela, nie v texte výnimky.
# Vlastná lokalizácia sa tu nikdy neobjaví — je to odpoveď API, nie naša hláška.
FRAZY_KREDIT = ("credit balance is too low",)


def _stavovy_kod(chyba):
    for zdroj in (chyba, getattr(chyba, "response", None)):
        kod = getattr(zdroj, "status_code", None)
        if isinstance(kod, int) and not isinstance(kod, bool):
            return kod
    return None


def _telo_chyby(chyba):
    """Rozparsované telo odpovede z SDK: {'type':'error','error':{...}}."""
    telo = getattr(chyba, "body", None)
    if not isinstance(telo, dict):
        return None
    vnutro = telo.get("error")
    return vnutro if isinstance(vnutro, dict) else telo


def je_nedostatok_kreditu(chyba) -> bool:
    """Je to odmietnutie pre nulový kredit — teda volanie, ktoré nič nespotrebovalo?

    Fail closed opačným smerom než zvyšok modulu: keď si nie sme istí, vrátime
    False a volanie sa zaúčtuje ako každé iné spadnuté. Radšej zaúčtovať niečo,
    čo bolo zadarmo, než prestať účtovať skutočnú spotrebu.
    """
    if isinstance(chyba, KreditVycerpany):
        return True
    kod = _stavovy_kod(chyba)
    if kod is not None and kod != 400:
        return False
    vnutro = _telo_chyby(chyba)
    if vnutro is not None:
        typ = vnutro.get("type")
        if typ is not None and typ != "invalid_request_error":
            return False
        text = str(vnutro.get("message") or "")
    elif kod == 400:
        # Staršie SDK telo nerozparsuje; v texte výnimky je celý JSON od API.
        text = str(chyba)
    else:
        # Obyčajná výnimka bez akejkoľvek stopy po HTTP odpovedi — netipujeme.
        return False
    text = text.lower()
    return any(fraza in text for fraza in FRAZY_KREDIT)


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
            "predpocet": _euro_z_prostredia(
                "UVARSI_TYZDENNY_STROP_PREDPOCET_EUR", VYCHODZI_TYZDENNY_STROP_PREDPOCET_EUR
            ),
        },
    )


def limit_behov(ucel) -> int:
    """Koľkokrát za ISO týždeň sa smie drahá operácia vôbec spustiť."""
    premenna = PREMENNA_BEHOV.get(ucel)
    if premenna is not None:
        return _cele_z_prostredia(premenna, VYCHODZI_LIMIT_BEHOV[ucel])
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

-- Posledný známy stav „API odmieta pre nulový kredit". Slúži na dve veci:
-- /api/health a /api/naklady z nej hovoria pravdu namiesto falošného eura,
-- a primárny kľúč na DNI zaručuje jedno upozornenie za deň, nie za pokus.
-- Riadky sa mažú, keď volanie preukázateľne prejde — kredit teda zase je.
CREATE TABLE IF NOT EXISTS naklady_kredit (
  den     TEXT NOT NULL PRIMARY KEY,
  zistene TEXT,
  ucel    TEXT
);

-- Protizápisy: ktoré riadky evidencie už boli stornované ako „zaúčtované za
-- prácu, ktorá sa nevykonala". Primárny kľúč robí opravu idempotentnou.
CREATE TABLE IF NOT EXISTS naklady_storna (
  naklady_id INTEGER PRIMARY KEY,
  cas        TEXT,
  dovod      TEXT
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

    Presnú cenu volania vopred nikto nepozná (vie sa až z `usage` v odpovedi),
    takže strop sa smie prekročiť nanajvýš o rozdiel medzi odhadom a skutočnou
    cenou JEDNÉHO volania. Zaručené je zastavenie míňania, nie trafenie sa na
    cent — a presne to incidentu chýbalo.
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


def uvolni_beh(con, ucel, *, teraz=None):
    """Vráť miesto v týždennom počte behov — beh sa NEUSKUTOČNIL.

    Volá sa jedine vtedy, keď je preukázané, že sa nespotreboval ani token
    (API odmietlo volanie pre nulový kredit). Strop tým neslabne: miesto sa
    vracia len za prácu, ktorá sa nikdy nezačala. Nikdy nejde pod nulu, takže
    dvojité zavolanie počítadlo nerozbije — oprava smie bežať opakovane.
    """
    teraz = _teraz(teraz)
    _, _, tyzden = _obdobia(teraz)
    try:
        with con:
            con.execute(
                "UPDATE naklady_behy SET pocet = MAX(pocet - 1, 0), updated = ? "
                "WHERE tyzden = ? AND ucel = ?",
                (teraz.isoformat(timespec="seconds"), tyzden, ucel),
            )
            riadok = con.execute(
                "SELECT pocet FROM naklady_behy WHERE tyzden=? AND ucel=?", (tyzden, ucel)
            ).fetchone()
    except (sqlite3.Error, OSError):
        # Uvoľnenie je náprava, nie platba — nesmie prebiť pôvodnú chybu.
        return None
    return int(riadok["pocet"]) if riadok else 0


# ---------------------------------------------------------------- nulový kredit
def zapamataj_kredit(con, *, ucel=None, teraz=None) -> bool:
    """Zapíš, že API odmieta pre nulový kredit. True = je to novinka.

    Kľúčom je DEŇ. Hodinový dozorca aj desiatky pokusov používateľov tak
    spustia jedno jediné upozornenie namiesto lavíny — presne ako pri prahoch
    mesačného rozpočtu. Nový deň sa ozve znova, aby sa na to nezabudlo.
    """
    teraz = _teraz(teraz)
    den, _, _ = _obdobia(teraz)
    kurzor = con.execute(
        "INSERT OR IGNORE INTO naklady_kredit (den, zistene, ucel) VALUES (?, ?, ?)",
        (den, teraz.isoformat(timespec="seconds"), None if ucel is None else str(ucel)),
    )
    con.commit()
    return kurzor.rowcount == 1


def zabudni_kredit(con) -> None:
    """Volanie prešlo → kredit zjavne je. Príznak sa zmaže, appka mlčí."""
    try:
        con.execute("DELETE FROM naklady_kredit")
        con.commit()
    except (sqlite3.Error, OSError):
        pass


def kredit_stav(con) -> dict:
    """Čo o kredite povedať na /api/health a /api/naklady."""
    try:
        riadok = con.execute(
            "SELECT den, zistene FROM naklady_kredit ORDER BY den DESC LIMIT 1"
        ).fetchone()
    except (sqlite3.Error, OSError):
        return {"vycerpany": False, "od": None, "sprava": None}
    if riadok is None:
        return {"vycerpany": False, "od": None, "sprava": None}
    return {
        "vycerpany": True,
        "od": riadok["zistene"] or riadok["den"],
        "sprava": SPRAVA_KREDIT_NTFY,
    }


def _ohlas_kredit(con, ucel, teraz, notifikuj) -> KreditVycerpany:
    """Zaznamenaj stav, upozorni NAJVIAC RAZ za deň a vráť typované odmietnutie."""
    posli = posli_ntfy if notifikuj is None else notifikuj
    try:
        nove = zapamataj_kredit(con, ucel=ucel, teraz=teraz)
    except (sqlite3.Error, OSError):
        # Bez evidencie sa „práve raz" nedá zaručiť; lavína notifikácií by bola
        # horšia než ticho. Pokazená evidencia sa aj tak hlási cez /api/health.
        nove = False
    if nove:
        try:
            posli({"titul": TITUL_KREDIT, "sprava": SPRAVA_KREDIT_NTFY})
        except Exception:
            pass
    return KreditVycerpany(ucel=ucel)


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
        # Skutočná spotreba je dôkaz, že API zase účtuje — príznak „došiel
        # kredit" preto padá sám, bez zásahu a bez rizika trvalého zaseknutia.
        zabudni_kredit(con)

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
        if je_nedostatok_kreditu(chyba):
            # Jediná výnimka z pravidla „spadnuté volanie sa účtuje": API tu
            # request odmietlo EŠTE PRED akoukoľvek prácou, takže spotreba je
            # preukázateľne nulová. Zaúčtovať odhad by minulo týždenný rozpočet
            # za behy, ktoré nikdy nebežali — presne to sa 24. 8. 2026 stalo.
            raise _ohlas_kredit(con, ucel, teraz, notifikuj) from chyba
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
    # Príznak kreditu ide do základu: keď API odmieta, je to najdôležitejšia
    # informácia v celom prehľade a nesmie zmiznúť ani pri pokazenej evidencii.
    zaklad = {"den": den, "mesiac": mesiac, "tyzden": tyzden, "chyba": None,
              "kredit": kredit_stav(con)}
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


# ---------------------------------------------------------------- oprava evidencie
# Chyby, pri ktorých API request odmietlo EŠTE PRED prácou, takže spotreba je
# nulová. HTTP 400 je z definície odmietnutý request — model sa nespustil.
# Timeout (APITimeoutError) tu zámerne NIE JE: ten mohol tokeny minúť a jeho
# konzervatívne zaúčtovanie je správne.
CHYBY_BEZ_SPOTREBY = ("BadRequestError",)
DOVOD_STORNO = "storno: volanie odmietnuté pre nulový kredit (nič nespotrebovalo)"


def _kandidati_na_storno(con, tyzden):
    """Riadky zaúčtované odhadom za volanie, ktoré API odmietlo bez práce."""
    podmienky = " OR ".join(["detail LIKE ?"] * len(CHYBY_BEZ_SPOTREBY))
    parametre = [tyzden] + [f"zlyhalo: {nazov}%" for nazov in CHYBY_BEZ_SPOTREBY]
    return con.execute(
        "SELECT id, cas, den, mesiac, tyzden, ucel, model, eur, detail FROM naklady "
        f"WHERE tyzden = ? AND odhad = 1 AND ({podmienky}) AND eur > 0 "
        "  AND id NOT IN (SELECT naklady_id FROM naklady_storna) "
        "ORDER BY id",
        parametre,
    ).fetchall()


def oprav_kredit(con, *, teraz=None, tyzden=None, vykonaj=False) -> dict:
    """Naprav evidenciu po volaniach, ktoré API odmietlo pre nulový kredit.

    NIČ SA NEMAŽE. Pôvodné riadky ostávajú — sú to skutočné pokusy a patria do
    histórie incidentu. Ku každému sa dopíše protizápis so zápornou sumou a
    poznámkou prečo, takže súčet vyjde na nulu, ale stopa je čitateľná.

    Miesto v týždennom počte behov sa vráti len vtedy, keď po stornách v tom
    týždni na daný účel neostala ŽIADNA skutočná útrata — teda keď je dokázané,
    že ani jeden beh nič nespotreboval. Keď sa v týždni čokoľvek naozaj minulo,
    počítadlo sa nechá tak: strop sa opravou nikdy neuvoľní omylom.

    Idempotentné vďaka tabuľke `naklady_storna`: čo je raz stornované, sa
    druhýkrát preskočí. `vykonaj=False` je iba náhľad a nič nemení.
    """
    teraz = _teraz(teraz)
    _, _, tyz = _obdobia(teraz)
    tyz = tyzden or tyz
    kandidati = _kandidati_na_storno(con, tyz)
    suma = round(sum(float(r["eur"]) for r in kandidati), 6)
    vysledok = {
        "tyzden": tyz,
        "vykonane": bool(vykonaj),
        "stornovanych": len(kandidati),
        "vratene_eur": suma,
        "vratene_behy": {},
        "riadky": [
            {"id": r["id"], "cas": r["cas"], "ucel": r["ucel"],
             "eur": round(float(r["eur"]), 6), "detail": r["detail"]}
            for r in kandidati
        ],
    }
    if not kandidati or not vykonaj:
        return vysledok

    cas = teraz.isoformat(timespec="seconds")
    with con:
        for riadok in kandidati:
            con.execute(
                """INSERT INTO naklady
                   (cas, den, mesiac, tyzden, ucel, model,
                    vstup, vystup, cache_write, cache_read, eur, odhad, detail)
                   VALUES (?, ?, ?, ?, ?, ?, 0, 0, 0, 0, ?, 1, ?)""",
                (cas, riadok["den"], riadok["mesiac"], riadok["tyzden"], riadok["ucel"],
                 riadok["model"], -float(riadok["eur"]),
                 f"{DOVOD_STORNO} — pôvodný záznam #{riadok['id']}"),
            )
            con.execute(
                "INSERT OR IGNORE INTO naklady_storna (naklady_id, cas, dovod) VALUES (?, ?, ?)",
                (riadok["id"], cas, DOVOD_STORNO),
            )
        for ucel in sorted({r["ucel"] for r in kandidati}):
            if not limit_behov(ucel):
                continue
            zostatok = con.execute(
                "SELECT COALESCE(SUM(eur), 0) FROM naklady WHERE ucel = ? AND tyzden = ?",
                (ucel, tyz),
            ).fetchone()[0]
            if float(zostatok) > 1e-9:
                continue                     # v týždni sa naozaj míňalo — strop platí
            riadok = con.execute(
                "SELECT pocet FROM naklady_behy WHERE tyzden=? AND ucel=?", (tyz, ucel)
            ).fetchone()
            if riadok and int(riadok["pocet"]):
                vysledok["vratene_behy"][ucel] = int(riadok["pocet"])
                con.execute(
                    "UPDATE naklady_behy SET pocet = 0, updated = ? WHERE tyzden=? AND ucel=?",
                    (cas, tyz, ucel),
                )
    return vysledok


# ---------------------------------------------------------------- príkazový riadok
NAPOVEDA = """Oprava evidencie po výpadku kreditu na Anthropic API.

Použitie na serveri:
    cd /opt/uvarsi/app
    ../venv/bin/python naklady.py --oprav-kredit             # iba náhľad
    ../venv/bin/python naklady.py --oprav-kredit --vykonaj   # naozaj opraviť
"""


def cli(argv=None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="naklady.py", description=NAPOVEDA,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--oprav-kredit", action="store_true", dest="oprav",
                        help="storno volaní odmietnutých pre nulový kredit")
    parser.add_argument("--vykonaj", action="store_true",
                        help="bez tohto prepínača je to iba náhľad, nič sa nemení")
    parser.add_argument("--tyzden", default=None,
                        help="ISO pondelok, napr. 2026-08-17 (východzí: tento týždeň)")
    parser.add_argument("--db", default=None, help="cesta k uvarsi.db")
    argumenty = parser.parse_args(argv)

    if not argumenty.oprav:
        parser.print_help()
        return 2

    con = pripoj(argumenty.db)
    try:
        vysledok = oprav_kredit(con, tyzden=argumenty.tyzden, vykonaj=argumenty.vykonaj)
    finally:
        con.close()

    rezim = "OPRAVENÉ" if vysledok["vykonane"] else "NÁHĽAD (nič sa nezmenilo)"
    print(f"Týždeň {vysledok['tyzden']} — {rezim}")
    print(f"  volaní zaúčtovaných omylom: {vysledok['stornovanych']}")
    print(f"  vrátené do rozpočtu:        {vysledok['vratene_eur']:.2f} €")
    for riadok in vysledok["riadky"]:
        print(f"    #{riadok['id']}  {riadok['cas']}  {riadok['ucel']}  "
              f"{riadok['eur']:.4f} €  ({riadok['detail']})")
    if vysledok["vratene_behy"]:
        for ucel, pocet in vysledok["vratene_behy"].items():
            print(f"  vrátené behy „{ucel}“:      {pocet}× (počítadlo nastavené na 0)")
    elif not vysledok["stornovanych"]:
        print("  nič na opravu — evidencia je v poriadku.")
    elif vysledok["vykonane"]:
        print("  behy: nemenené (v týždni ostala skutočná útrata)")
    if not vysledok["vykonane"] and vysledok["stornovanych"]:
        print("\nSpusti znova s --vykonaj, keď to takto sedí.")
    return 0


if __name__ == "__main__":
    raise SystemExit(cli())
