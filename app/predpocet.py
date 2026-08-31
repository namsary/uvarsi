#!/usr/bin/env python3
"""Uvar.si — PREDPOČET: najžiadanejšie jedálničky poskladané v noci dopredu.

PREČO. Zbierač letákov (`zbierac_akcii.py`, spúšťa ho `hetzner/dozorca.sh`)
dobehne v pondelok nadránom. Od tej chvíle sú ponuky týždňa dané a KAŽDÝ
zdieľaný plán sa dá poskladať — len o ňom ešte nikto nepožiadal. Prvý človek
s daným profilom preto doteraz čakal 60–120 sekúnd na volanie, ktoré mohlo
pokojne prebehnúť o tretej ráno, keď nikto nečaká. Predpočet to volanie
presunie do noci; ráno sa plán podá z `plany_zdielane` za milisekundy.

ČO SA ZAHRIEVA. Podpis zdieľaného plánu (`plan_data.plan_signature`) tvoria
verzia generátora, týždeň, sada obchodov, počet osôb, frekvencia varenia a
množina `offer_key`. Špajza v ňom (zámerne) nie je, takže sa profil dá
predpočítať bez čohokoľvek osobného. K podpisu patrí ešte VARIANT — tá istá
domácnosť dostane jeden z `server.PLAN_VARIANTS` smerov kuchyne podľa
`user_id % PLAN_VARIANTS`. Jednotkou zahrievania je preto jeden plán, teda
jedna dvojica (podpis, variant) = jedno volanie modelu = jeden riadok cache.
Zahriať profil „naozaj celý“ znamená zahriať všetky jeho varianty; pri
východzích troch variantoch teda `UVARSI_PREDPOCET_PROFILOV=9` znamená tri
úplne pokryté domácnosti.

PODĽA ČOHO SA VYBERÁ. Prvý týždeň niet histórie, tak sa ide podľa rozumného
odhadu (`VYCHODZIE_PROFILY` — východzí profil appky a jeho najbližší susedia).
Od druhého týždňa rozhoduje `dopyt_profilov`: čistý AGREGÁT toho, o čo ľudia
naozaj žiadali. Žiadne user_id, žiadny e-mail, žiadny čas jednotlivej
požiadavky — len počty na (týždeň, obchody, osoby, frekvencia, variant) a
`HISTORIA_TYZDNOV` týždňov dozadu. Zoznam sa tak učí sám a nepotrebuje, aby
majiteľ hádal, čo si ľudia vypýtajú.

ČO SA MÍŇA. Cena jedného zahriateho plánu je cena jedného volania Sonnetom
nad promptom, ktorého ~95 % tvorí katalóg ponúk. Ten je pre celý beh rovnaký a
ide s `cache_control`, takže prvé volanie cache zapíše (~0,027 €) a všetky
ďalšie ju len čítajú (~0,018 €). Účtuje sa ako účel „predpocet“, takže je v
/api/naklady vidieť zvlášť od plánov, ktoré si vypýtali ľudia.

TRI OHRANIČENIA, ktoré musia platiť súčasne (a fail closed cez `naklady`):
  1. POČET — `UVARSI_PREDPOCET_PROFILOV` (východzie 9), tvrdo zhora
     `MAX_POCET_PROFILOV`. Pokazená hodnota v prostredí nezhodí beh, len ho
     zúži: predpočet je zrýchlenie, nie povinnosť.
  2. REZERVA — predpočet zastane, keď by z denného stropu ostalo menej než
     `UVARSI_PREDPOCET_REZERVA_EUR`. V pondelok totiž ten istý deň zaplatil aj
     zber letákov (~1,11 €) a bolo by absurdné, keby nočné zahrievanie nechalo
     ranných používateľov bez rozpočtu na ich vlastný plán.
  3. BEHY a TÝŽDENNÝ STROP ÚČELU — `naklady.rezervuj_beh` a
     `stropy().tyzdenny_ucel["predpocet"]`. Rozbehnutý cron narazí na strop
     skôr, než stihne nekontrolovane míňať; viac povolených behov slúži iba
     na zotavenie po dočasnom výpadku, eurový strop sa tým nezvyšuje.

ZLYHANIE JE NEŠKODNÉ. Nič z tohto modulu nesmie zhodiť appku ani dozorcu.
Keď sa predpočet vypne, preskočí alebo padne, appka skladá plány naživo presne
ako doteraz — jediný rozdiel oproti dnešku je rýchlosť.

Ručne na serveri:
    cd /opt/uvarsi/app
    UVARSI_URL=https://uvar.si ../venv/bin/python predpocet.py --zahrej
    UVARSI_URL=https://uvar.si ../venv/bin/python predpocet.py --stav
"""
import datetime
import hashlib
import inspect
import json
import logging
import math
import os
import re
import secrets
import sqlite3
import time
from collections import Counter
from collections import namedtuple
from contextlib import closing
from decimal import Decimal, InvalidOperation

try:
    from . import naklady
    from . import plan_jobs
    from .plan_data import (
        build_personal_plan, measurable_offers, personal_plan_messages, plan_output_config,
    )
    from .plan_shortlist import select_offers
except ImportError:                      # beh priamo z adresára app/
    import naklady
    import plan_jobs
    from plan_data import (
        build_personal_plan, measurable_offers, personal_plan_messages, plan_output_config,
    )
    from plan_shortlist import select_offers


LOG = logging.getLogger("uvarsi.predpocet")

UCEL = "predpocet"

# Koľko plánov sa zahrieva. Jednotka je JEDEN plán (podpis + variant), teda
# jedno volanie modelu. Východzích 9 = tri najžiadanejšie domácnosti so
# všetkými troma variantmi, dokopy ~0,17 €.
VYCHODZI_POCET_PROFILOV = 9
# Tvrdý strop nad hodnotou z prostredia. Preklep (900 namiesto 9) sa nesmie
# premeniť na 18 € útraty; pri MAX × cene za profil ostane pod polovicou
# mesačného rozpočtu a zvyšné poistky (rezerva, strop účelu) platia ďalej.
MAX_POCET_PROFILOV = 60

# Koľko z denného stropu musí ostať živým používateľom. V pondelok už z neho
# zaplatil zber letákov, takže bez tejto rezervy by nočné zahrievanie dojedlo
# zvyšok a ráno by si nikto nevedel vypýtať vlastný plán.
VYCHODZIA_REZERVA_EUR = 0.20

# Odkedy dokedy sa pozerá na dopyt. Štyri týždne sú kompromis: dosť dlho na to,
# aby jeden pokojný týždeň zoznam nerozhodil, a dosť krátko na to, aby sa
# zoznam hýbal s tým, ako sa mení, kto appku používa.
HISTORIA_TYZDNOV = 4
# Dokedy sa dopyt vôbec drží. Je to agregát, nie osobný log, ale aj agregát sa
# má sám upratať — držať počty spred roka nemá komu a načo poslúžiť.
DRZANIE_DOPYTU_TYZDNOV = 12

# Musí sedieť so `server.PLAN_VARIANTS` (stráži to test). Keby sa rozišli,
# zahrialo by sa niečo, čo nikto nedostane.
POCET_VARIANTOV = 3

# Koľko môže stáť jeden zahriaty plán. Katalóg má v silnom týždni stovky ponúk
# a odpoveď až ~3 640 tokenov pre 7 jedál. Odhad 0,12 € kryje aj plný
# 10k-tokenový strop Sonnetu 5 vrátane vstupu/cache; skutočná úspešná odpoveď
# býva vďaka low effort podstatne lacnejšia a evidencia ju prepíše reálnym usage.
CENA_ZA_PROFIL_EUR = naklady.ODHAD_EUR[UCEL]

PREMENNE_PROSTREDIA = (
    "UVARSI_PREDPOCET",
    "UVARSI_PREDPOCET_PROFILOV",
    "UVARSI_PREDPOCET_REZERVA_EUR",
)

# Dôvody, prečo sa beh skončil. Sú to strojové kódy do /api/naklady; vysvetlenie
# pre človeka je v `VYSVETLENIE`.
DOVOD_HOTOVO = "hotovo"
DOVOD_ROZPOCET = "rozpocet"
DOVOD_BEHY = "behy"
DOVOD_VYPNUTE = "vypnute"
DOVOD_CHYBY = "chyby"
DOVOD_BLOKOVANE = "blokovane"

VYSVETLENIE = {
    DOVOD_HOTOVO: "Požadované profily sú zaradené alebo už boli pripravené.",
    DOVOD_ROZPOCET: "Zastavené pred stropom — zvyšok rozpočtu ostáva živým používateľom.",
    DOVOD_BEHY: "Tento týždeň už predpočet bežal dosť často; ďalší beh sa nespúšťa.",
    DOVOD_VYPNUTE: "Predpočet je vypnutý (UVARSI_PREDPOCET=0 alebo 0 profilov).",
    DOVOD_CHYBY: "Príliš veľa neúspešných pokusov po sebe — beh sa ukončil.",
    DOVOD_BLOKOVANE: "Niektoré profily sa zatiaľ nedajú zaradiť; dozorca to skúsi znova.",
}

# Koľko pádov po sebe sa toleruje, kým sa beh vzdá. Keď model odmieta alebo
# odpovedá nezmyslom, nemá zmysel prejsť celý zoznam a zaplatiť to.
MAX_ZLYHANI_PO_SEBE = 3

# Pevná anonymná matica pre shadow rollout. Nesmie sa odvodiť od účtov ani
# dopytu: každý týždeň meriame presne rovnakých 36 verejne nevystopovateľných
# kombinácií (4 režimy × 3 domácnosti × 3 rytmy).
SHADOW_MODES = ("standard", "high_protein", "vegetarian", "vegan")
SHADOW_HOUSEHOLDS = ((1, 0), (2, 2), (4, 0))
SHADOW_FREQUENCIES = (1, 2, 3)
SHADOW_MATRIX = tuple(
    (mode, adults, children, frequency)
    for mode in SHADOW_MODES
    for adults, children in SHADOW_HOUSEHOLDS
    for frequency in SHADOW_FREQUENCIES
)
SHADOW_SUCCESS_RATE_FLOOR = 0.98
SHADOW_P95_LIMIT_MS = 500.0
SHADOW_COUNTER_LIMIT = len(SHADOW_MATRIX) * 1000
SHADOW_ERROR_CODES = frozenset({
    "insufficient_offers", "diet_too_strict", "unmeasurable_packages",
    "internal_error",
})
_SHADOW_PACKAGE = re.compile(
    r"^[1-9]\d*(?:[,.]\d+)?\s+(?:g|kg|ml|l|ks)$", re.IGNORECASE
)


def build_deterministic_plan(**kwargs):
    """Late import keeps the legacy direct-script deployment importable."""
    from app.deterministic_plan import build_deterministic_plan as builder

    return builder(**kwargs)


def _shadow_catalogs():
    from app.ingredient_catalog import load_ingredient_catalog
    from app.library_gate import audit_library
    from app.recipe_catalog import load_recipe_catalog

    ingredients = load_ingredient_catalog()
    recipes = load_recipe_catalog(ingredients)
    return ingredients, recipes, audit_library(ingredients, recipes)


class Profil(namedtuple("ProfilZaklad", "obchody dospeli deti frekvencia variant")):
    """Profil predpočtu s kompatibilným celkovým počtom osôb.

    `osoby` zostáva odvodená vlastnosť, aby staršie volania a diagnostika
    fungovali počas prechodu na samostatných dospelých a deti.
    """

    __slots__ = ()

    @property
    def osoby(self):
        return self.dospeli + self.deti


# ---------------------------------------------------------------- schéma
SCHEMA = """
-- Čo si ľudia pýtali, v podobe počtov. ZÁMERNE bez user_id a bez e-mailu:
-- na zostavenie zoznamu na zahrievanie stačí, KOĽKO ráz taký profil niekto
-- chcel — nie kto to bol a kedy presne. Riadok je agregát, nie stopa po
-- človeku, a staršie týždne sa samy mažú.
CREATE TABLE IF NOT EXISTS dopyt_profilov (
  tyzden     TEXT NOT NULL,          -- ISO pondelok týždňa požiadavky
  obchody    TEXT NOT NULL,          -- normalizované: zoradené, čiarkou
  osoby      INTEGER NOT NULL,       -- kompatibilný súčet dospelí + deti
  dospeli    INTEGER NOT NULL,
  deti       INTEGER NOT NULL DEFAULT 0,
  frekvencia INTEGER NOT NULL,
  variant    INTEGER NOT NULL,
  pocet      INTEGER NOT NULL DEFAULT 0,
  posledny   TEXT,                   -- deň poslednej požiadavky (bez hodiny)
  PRIMARY KEY (tyzden, obchody, dospeli, deti, frekvencia, variant)
);

-- Jeden riadok na týždeň: ako sa predpočtu darilo. `zasahy` je to jediné
-- číslo, ktoré hovorí, či to malo zmysel — koľko živých generovaní sa vďaka
-- zahriatym plánom NEUSKUTOČNILO.
CREATE TABLE IF NOT EXISTS predpocet_behy (
  tyzden       TEXT NOT NULL PRIMARY KEY,
  zaciatok     TEXT,
  koniec       TEXT,
  behov        INTEGER NOT NULL DEFAULT 0,
  zahriatych   INTEGER NOT NULL DEFAULT 0,
  preskocenych INTEGER NOT NULL DEFAULT 0,
  zlyhanych    INTEGER NOT NULL DEFAULT 0,
  eur          REAL NOT NULL DEFAULT 0,
  zasahy       INTEGER NOT NULL DEFAULT 0,
  dovod        TEXT
);

-- Jediný trvalý výstup shadow porovnania. Neobsahuje plán, recept, špajzu,
-- user_id ani e-mail; iba súhrn pevnej anonymnej matice za jeden týždeň.
CREATE TABLE IF NOT EXISTS recipe_engine_shadow (
  tyzden                 TEXT NOT NULL PRIMARY KEY,
  run_token               TEXT,
  zaciatok                TEXT NOT NULL,
  koniec                  TEXT,
  complete                INTEGER NOT NULL DEFAULT 0,
  offer_fingerprint       TEXT NOT NULL,
  library_version         INTEGER,
  matrix_size             INTEGER NOT NULL,
  samples_total           INTEGER NOT NULL DEFAULT 0,
  samples_success         INTEGER NOT NULL DEFAULT 0,
  success_rate            REAL NOT NULL DEFAULT 0,
  p95_ms                  REAL,
  error_counts            TEXT NOT NULL DEFAULT '{}',
  family_count            INTEGER NOT NULL DEFAULT 0,
  method_count            INTEGER NOT NULL DEFAULT 0,
  price_comparisons       INTEGER NOT NULL DEFAULT 0,
  price_delta_eur_avg     REAL,
  dietary_violations      INTEGER NOT NULL DEFAULT 0,
  negative_quantities     INTEGER NOT NULL DEFAULT 0,
  invalid_package_counts  INTEGER NOT NULL DEFAULT 0,
  library_gate_pass       INTEGER NOT NULL DEFAULT 0
);
"""


def migrate_predpocet_schema(con) -> None:
    """Tabuľky predpočtu + príznak nad zdieľanými plánmi. Idempotentné.

    `predpocitany` odlišuje riadok poskladaný v noci od riadku, ktorý zaplatil
    živý používateľ. Bez neho by sa nedalo povedať, koľko čakania predpočet
    naozaj ušetril — a to je jediné číslo, podľa ktorého sa dá rozhodnúť, či
    má zmysel zahrievať viac alebo menej.
    """
    con.executescript(SCHEMA)
    dopyt_stlpce = {
        riadok[1] for riadok in con.execute("PRAGMA table_info(dopyt_profilov)")
    }
    if dopyt_stlpce and not {"dospeli", "deti"}.issubset(dopyt_stlpce):
        # SQLite nevie zmeniť zložený PRIMARY KEY cez ALTER COLUMN. Tabuľku
        # preto v jednej migrácii prebudujeme a staré `osoby` zachováme ako
        # dospelých; deti boli v pôvodnom modeli neznáme, teda bezpečne 0.
        zaloha = "dopyt_profilov_pred_deti"
        con.execute(f"ALTER TABLE dopyt_profilov RENAME TO {zaloha}")
        con.executescript(SCHEMA)
        con.execute(
            f"""INSERT INTO dopyt_profilov
                    (tyzden, obchody, osoby, dospeli, deti, frekvencia,
                     variant, pocet, posledny)
                SELECT tyzden, obchody, osoby, osoby, 0, frekvencia,
                       variant, SUM(pocet), MAX(posledny)
                  FROM {zaloha}
                 GROUP BY tyzden, obchody, osoby, frekvencia, variant"""
        )
        con.execute(f"DROP TABLE {zaloha}")
    stlpce = {riadok[1] for riadok in con.execute("PRAGMA table_info(plany_zdielane)")}
    if stlpce and "predpocitany" not in stlpce:
        con.execute(
            "ALTER TABLE plany_zdielane ADD COLUMN predpocitany INTEGER NOT NULL DEFAULT 0"
        )
    shadow_stlpce = {
        riadok[1] for riadok in con.execute("PRAGMA table_info(recipe_engine_shadow)")
    }
    if shadow_stlpce and "run_token" not in shadow_stlpce:
        con.execute("ALTER TABLE recipe_engine_shadow ADD COLUMN run_token TEXT")


# ---------------------------------------------------------------- konfigurácia
def _normalizuj_obchody(obchody) -> tuple:
    """Rovnaká sada obchodov musí dať rovnaký kľúč, nech príde v akomkoľvek poradí."""
    if isinstance(obchody, str):
        obchody = obchody.split(",")
    return tuple(sorted({str(obchod).strip() for obchod in obchody if str(obchod).strip()}))


def je_zapnuty() -> bool:
    """Vypnutie je jedno slovo v prostredí a nič viac sa nestane."""
    hodnota = os.environ.get("UVARSI_PREDPOCET")
    if hodnota is None or not hodnota.strip():
        return True
    return hodnota.strip().lower() not in ("0", "nie", "no", "off", "false")


def pocet_profilov() -> int:
    """Koľko plánov zahriať. Pokazená hodnota beh NEZHODÍ, len ho zúži.

    `naklady` fail closed vyhadzuje výnimku, lebo tam ide o platbu. Tu ide o
    zrýchlenie: keď je hodnota nezmyselná, správne je zahriať východzí počet
    (alebo nič), nie zhodiť nočný cron.
    """
    surove = os.environ.get("UVARSI_PREDPOCET_PROFILOV")
    if surove is None or not surove.strip():
        return VYCHODZI_POCET_PROFILOV
    try:
        hodnota = int(surove.strip())
    except (TypeError, ValueError):
        LOG.warning("UVARSI_PREDPOCET_PROFILOV nie je číslo (%r) — beriem východzích %d",
                    surove, VYCHODZI_POCET_PROFILOV)
        return VYCHODZI_POCET_PROFILOV
    return max(0, min(hodnota, MAX_POCET_PROFILOV))


def rezerva_eur() -> float:
    surove = os.environ.get("UVARSI_PREDPOCET_REZERVA_EUR")
    if surove is None or not surove.strip():
        return VYCHODZIA_REZERVA_EUR
    try:
        hodnota = float(surove.strip().replace(",", "."))
    except (TypeError, ValueError):
        return VYCHODZIA_REZERVA_EUR
    return hodnota if hodnota >= 0 else VYCHODZIA_REZERVA_EUR


# ---------------------------------------------------------------- výber profilov
# Východzí profil appky je Kaufland+Tesco+Lidl, 4 dospelí, variť raz za 2 dni —
# presne to má v `pouzivatelia` každý nový účet. Okolo neho sú najbližší
# susedia: menšia domácnosť a iná frekvencia. Poradie je poradie stávok, lebo
# `UVARSI_PREDPOCET_PROFILOV` zoznam odreže zhora.
VSETKY_OBCHODY = ("Kaufland", "Lidl", "Tesco")
VYCHODZIE_DOMACNOSTI = (
    (VSETKY_OBCHODY, 4, 0, 2),
    (VSETKY_OBCHODY, 2, 2, 2),
    (VSETKY_OBCHODY, 2, 0, 2),
    (VSETKY_OBCHODY, 2, 1, 2),
    (VSETKY_OBCHODY, 4, 0, 3),
    (VSETKY_OBCHODY, 2, 2, 3),
    (VSETKY_OBCHODY, 1, 0, 2),
    (VSETKY_OBCHODY, 3, 0, 2),
    (VSETKY_OBCHODY, 2, 0, 3),
    (VSETKY_OBCHODY, 4, 0, 1),
)


def vychodzie_profily() -> list:
    """Stávka na prvý týždeň, kým niet histórie. Domácnosť po domácnosti,
    každá so všetkými variantmi — inak by sa trafili len dve tretiny ľudí."""
    profily = []
    for obchody, dospeli, deti, frekvencia in VYCHODZIE_DOMACNOSTI:
        for variant in range(POCET_VARIANTOV):
            profily.append(Profil(
                _normalizuj_obchody(obchody), dospeli, deti, frekvencia, variant
            ))
    return profily


def zaznamenaj_dopyt(con, tyzden, obchody, *profil, osoby=None, dospeli=None,
                     deti=0, frekvencia=None, variant=None, dnes=None) -> None:
    """Pripočítaj jednotku k profilu, o ktorý niekto požiadal.

    Nikdy nesmie zhodiť požiadavku používateľa: evidencia dopytu je podklad na
    zahrievanie, nie súčasť odpovede. Keď sa zápis nepodarí, zoznam bude o
    kúsok menej presný a nič viac.
    """
    if profil:
        if any(hodnota is not None for hodnota in (osoby, dospeli, frekvencia, variant)):
            raise TypeError("profil zadaj pozične alebo pomenovane, nie oboma spôsobmi")
        if len(profil) == 3:              # staré: osoby, frekvencia, variant
            osoby, frekvencia, variant = profil
            dospeli, deti = osoby, 0
        elif len(profil) == 4:            # nové: dospelí, deti, frekvencia, variant
            dospeli, deti, frekvencia, variant = profil
        else:
            raise TypeError("profil musí obsahovať 3 staré alebo 4 nové hodnoty")
    elif dospeli is None:
        dospeli, deti = osoby, 0

    dnes = dnes or datetime.date.today()
    try:
        dospeli = int(dospeli)
        deti = int(deti)
        osoby = dospeli + deti
        con.execute(
            """INSERT INTO dopyt_profilov
                    (tyzden, obchody, osoby, dospeli, deti, frekvencia, variant,
                     pocet, posledny)
               VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?)
               ON CONFLICT(tyzden, obchody, dospeli, deti, frekvencia, variant)
               DO UPDATE SET
                     pocet = pocet + 1, posledny = excluded.posledny""",
            (str(tyzden), ",".join(_normalizuj_obchody(obchody)), osoby,
             dospeli, deti, int(frekvencia), int(variant), dnes.isoformat()),
        )
        hranica = (datetime.date.fromisoformat(str(tyzden))
                   - datetime.timedelta(weeks=DRZANIE_DOPYTU_TYZDNOV)).isoformat()
        con.execute("DELETE FROM dopyt_profilov WHERE tyzden < ?", (hranica,))
    except (sqlite3.Error, OSError, ValueError, TypeError):
        LOG.debug("dopyt profilu sa nezapísal", exc_info=True)


def oblubene_profily(con, tyzden, pocet, tyzdnov=HISTORIA_TYZDNOV) -> list:
    """Čo zahriať: najprv to, o čo ľudia žiadali, potom východzie stávky.

    Berie sa dopyt z PREDOŠLÝCH týždňov, nie z tohto: tento sa práve začal a
    jeho čísla by povedali nanajvýš to, kto vstal skoro. Keď história nestačí
    na `pocet` položiek, zvyšok doplnia východzie profily — zoznam teda nikdy
    nie je prázdny len preto, že appka je nová.
    """
    pocet = max(0, int(pocet))
    if not pocet:
        return []
    profily = historicke_profily(con, tyzden, tyzdnov=tyzdnov)
    for profil in vychodzie_profily():
        if profil not in profily:
            profily.append(profil)
    return profily[:pocet]


def historicke_profily(con, tyzden, tyzdnov=HISTORIA_TYZDNOV) -> list:
    """Vráť iba agregované profily z nedávnych týždňov, bez východzích stávok."""
    profily = []
    try:
        od = (datetime.date.fromisoformat(str(tyzden))
              - datetime.timedelta(weeks=tyzdnov)).isoformat()
        riadky = con.execute(
            """SELECT obchody, dospeli, deti, frekvencia, variant,
                      SUM(pocet) AS spolu
               FROM dopyt_profilov
               WHERE tyzden < ? AND tyzden >= ?
               GROUP BY obchody, dospeli, deti, frekvencia, variant
               ORDER BY spolu DESC, obchody, dospeli, deti, frekvencia, variant""",
            (str(tyzden), od),
        ).fetchall()
    except (sqlite3.Error, OSError, ValueError, TypeError):
        LOG.warning("dopyt profilov sa nedá prečítať — beriem východzie", exc_info=True)
        riadky = []
    for riadok in riadky:
        profily.append(Profil(
            _normalizuj_obchody(riadok["obchody"]), int(riadok["dospeli"]),
            int(riadok["deti"]),
            int(riadok["frekvencia"]), int(riadok["variant"]),
        ))
    return profily


# ---------------------------------------------------------------- rozpočet
def _je_miesto_v_rozpocte(con, teraz, rezerva) -> bool:
    """Ostane po tomto volaní ešte rezerva pre živých používateľov?

    Tvrdé stropy (denný, mesačný, týždenný účelový) drží `naklady.skontroluj`
    pri každom volaní. Tento test je nad ním: zastaví predpočet SKÔR, aby to,
    čo ostane, patrilo ľuďom, ktorí naozaj čakajú.
    """
    try:
        limity = naklady.stropy()
        den, mesiac, _ = naklady._obdobia(teraz)
        dnes_eur = naklady.spolu_za_den(con, den)
        mesiac_eur = naklady.spolu_za_mesiac(con, mesiac)
        rezervovane_eur = plan_jobs.active_reservations_eur(con)
    except (naklady.RozpocetVycerpany, sqlite3.Error, OSError):
        return False                     # nevieme → nemíňame
    odhad = CENA_ZA_PROFIL_EUR
    if dnes_eur + rezervovane_eur + odhad > limity.denny - rezerva:
        return False
    if mesiac_eur + rezervovane_eur + odhad > limity.mesacny - rezerva:
        return False
    return True


# ---------------------------------------------------------------- skladanie
class PlanNepouzitelny(Exception):
    """Model odpovedal tak, že sa z toho plán poskladať nedá."""


def _text_odpovede(msg) -> str:
    if getattr(msg, "stop_reason", None) == "max_tokens":
        raise PlanNepouzitelny("odpoveď sa nestihla dopísať do konca")
    text = "".join(blok.text for blok in msg.content
                   if getattr(blok, "type", None) == "text").strip()
    return re.sub(r"^```(?:json)?|```$", "", text, flags=re.M).strip()


def _pozna_clenov_domacnosti(funkcia) -> bool:
    """Či cieľ už používa nový kontrakt adults/children.

    Predpočet sa nasadzuje spolu so serverom, no lokálne testy a prípadný
    rozbehnutý starší proces môžu počas migrácie ešte vystavovať podpis s
    jediným `household_size`. Kontrola podpisu funkcie zachová kompatibilitu
    bez maskovania skutočného TypeError vo vnútri volanej funkcie.
    """
    try:
        parametre = inspect.signature(funkcia).parameters
    except (TypeError, ValueError):
        return False
    return "adults" in parametre and "children" in parametre


def _podpis_pre_profil(server, tyzden, profil, rows):
    """Podpis, prompt aj builder musia dostať tú istú skladbu domácnosti."""
    if _pozna_clenov_domacnosti(server.podpis_planu):
        parametre = inspect.signature(server.podpis_planu).parameters
        pozičné = [
            parameter for parameter in parametre.values()
            if parameter.kind in (
                inspect.Parameter.POSITIONAL_ONLY,
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
            )
        ]
        zaklad = [tyzden, list(profil.obchody)]
        # Serverový obal už môže mať nový čistý podpis bez household_size,
        # kým plan_data počas prechodu stále prijíma jeho voliteľné miesto.
        if "household_size" in parametre or len(pozičné) >= 6:
            zaklad.append(None)
        return server.podpis_planu(
            *zaklad, profil.frekvencia, rows, (),
            adults=profil.dospeli, children=profil.deti,
        )
    return server.podpis_planu(
        tyzden, list(profil.obchody), profil.osoby, profil.frekvencia, rows, ()
    )


def _poskladaj(con, server, rows, profil, klient=None):
    """Jedno platené volanie — cez ten istý strážený klient ako appka.

    Prompt, model aj strop tokenov sa berú zo `server`, nie z vlastných
    konštánt: keby sa rozišli, zahrialo by sa niečo s iným podpisom a nikto by
    sa do toho netrafil. Špajza sa sem nedostane nikdy — zdieľaný plán je z
    definície bez nej.
    """
    import anthropic

    strazeny = naklady.strazeny_klient(
        con,
        klient or anthropic.Anthropic(
            api_key=server.env("ANTHROPIC_API_KEY"),
            timeout=server.PLAN_TIMEOUT_SECONDS,
            max_retries=server.PLAN_MAX_RETRIES,
        ),
        UCEL,
        rezervovane_eur=lambda: plan_jobs.active_reservations_eur(con),
    )
    prompt_source = measurable_offers(rows)
    if not prompt_source:
        raise PlanNepouzitelny(
            "z aktuálnych akcií sa nedajú spoľahlivo vypočítať množstvá"
        )
    prompt_rows = select_offers(prompt_source, profil.obchody, limit=120)
    if _pozna_clenov_domacnosti(personal_plan_messages):
        blocks = personal_plan_messages(
            rows, profil.frekvencia, (), household_size=None,
            variant=profil.variant, pantry_driven=False,
            prompt_rows=prompt_rows,
            adults=profil.dospeli, children=profil.deti,
        )
    else:
        blocks = personal_plan_messages(
            rows, profil.frekvencia, (), household_size=profil.osoby,
            variant=profil.variant, pantry_driven=False,
            prompt_rows=prompt_rows,
        )
    zaklad = {
        "model": server.MODEL_PLAN,
        "max_tokens": server.PLAN_TOKENS,
        "messages": [{"role": "user", "content": blocks}],
    }
    nastavenie = {
        "output_config": plan_output_config(getattr(server, "PLAN_EFFORT", None)),
    }
    # Neopakovať pri TypeError: chyba môže vzniknúť až po odoslaní a druhý
    # pokus by mohol znamenať druhé platené volanie.
    msg = strazeny.messages.create(**zaklad, **nastavenie)
    try:
        model_output = json.loads(_text_odpovede(msg))
    except json.JSONDecodeError as chyba:
        raise PlanNepouzitelny("odpoveď nie je platný JSON") from chyba
    try:
        if _pozna_clenov_domacnosti(build_personal_plan):
            return build_personal_plan(
                con, model_output, list(profil.obchody), profil.frekvencia, None,
                adults=profil.dospeli, children=profil.deti,
            )
        return build_personal_plan(
            con, model_output, list(profil.obchody), profil.frekvencia, profil.osoby
        )
    except ValueError as chyba:
        raise PlanNepouzitelny(str(chyba)) from chyba


# ---------------------------------------------------------------- výber a fronta
def _profilovy_kluc(profil):
    return profil.obchody, profil.dospeli, profil.deti, profil.frekvencia, profil.variant


def aktivne_profily(con, server) -> list:
    """Aktuálne profily účtov, v poradí stabilnom pre opakovaný cron."""
    profily = []
    try:
        riadky = con.execute(
            "SELECT id, osoby, dospeli, deti, frekvencia, obchody "
            "FROM pouzivatelia ORDER BY id"
        ).fetchall()
    except (sqlite3.Error, OSError):
        LOG.warning("aktívne profily sa nedajú prečítať", exc_info=True)
        return profily
    for riadok in riadky:
        try:
            dospeli, deti = server.zlozenie_domacnosti(riadok)
            frekvencia = int(riadok["frekvencia"])
            if frekvencia not in (1, 2, 3):
                continue
            variant = server.plan_variant_for(riadok["id"], server.PLAN_VARIANTS)
            profily.append(Profil(
                _normalizuj_obchody(riadok["obchody"]), int(dospeli), int(deti),
                frekvencia, variant,
            ))
        except (KeyError, TypeError, ValueError):
            continue
    return profily


def _cielove_profily(con, server, tyzden, pocet) -> list:
    """Aktívne presné profily, potom dopyt, nakoniec bezpečné defaulty."""
    kandidati = []
    videne = set()
    fazy = (
        aktivne_profily(con, server),
        historicke_profily(con, tyzden),
        vychodzie_profily(),
    )
    for faza in fazy:
        for profil in faza:
            kluc = _profilovy_kluc(profil)
            if kluc in videne:
                continue
            videne.add(kluc)
            kandidati.append(profil)
            if len(kandidati) >= pocet:
                return kandidati
    return kandidati


def _novy_vysledok(tyzden=None, profilov=0):
    return {
        "tyzden": tyzden, "profilov": profilov,
        "queued": 0, "skipped": 0, "blocked": 0,
        # Staré kľúče ponechávame pre /api/naklady a staršie diagnostické čítače.
        "zahriatych": 0, "preskocenych": 0, "zlyhanych": 0,
        "eur": 0.0, "dovod": DOVOD_HOTOVO,
    }


def _ma_kompletny_povinny_zber(con, server, dnes) -> bool:
    """Fail closed, kým nie je overený aktuálny zber všetkých troch obchodov."""
    try:
        chybajuce = server.stores_missing_this_week(
            con, list(VSETKY_OBCHODY), dnes)
        rows = server.offers_for_current_week(
            con, list(VSETKY_OBCHODY), dnes)
    except (sqlite3.Error, OSError, ValueError):
        LOG.warning("úplnosť zberu pre predpočet sa nedá overiť", exc_info=True)
        return False
    zastupene = {row["obchod"] for row in rows}
    return (
        not chybajuce
        and len(rows) >= server.MIN_OFFERS_FOR_PLAN
        and set(VSETKY_OBCHODY).issubset(zastupene)
    )


# -------------------------------------------------------- deterministic shadow
def _shadow_offer_rows(con, server, today):
    rows = server.offers_for_current_week(
        con, list(VSETKY_OBCHODY), today
    )
    return tuple(server.measurable_offers(rows))


def _shadow_offer_fingerprint(rows) -> str:
    facts = [
        (
            str(row.get("offer_key") or ""),
            str(row.get("obchod") or ""),
            str(row.get("cena") or ""),
            str(row.get("povodna") or ""),
            str(row.get("jednotka") or ""),
            str(row.get("valid_from") or ""),
            str(row.get("valid_to") or ""),
        )
        for row in rows
    ]
    payload = json.dumps(sorted(facts), ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _shadow_decimal(value):
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float, Decimal)):
        raw = str(value)
    elif isinstance(value, str):
        match = re.search(r"-?\d+(?:[,.]\d+)?", value.replace(" ", ""))
        if match is None:
            return None
        raw = match.group(0).replace(",", ".")
    else:
        return None
    try:
        result = Decimal(raw)
    except (InvalidOperation, ValueError):
        return None
    return result if result.is_finite() else None


def _shadow_plan_quality(plan, mode, templates):
    """Return counters only; never return or persist recipe content."""
    families, methods = set(), set()
    dietary_violations = 0
    negative_quantities = 0
    invalid_package_counts = 0

    for field in ("nakup_spolu", "bezna_cena", "usetrene"):
        value = _shadow_decimal(plan.get(field))
        if value is not None and value < 0:
            negative_quantities += 1

    for meal in plan.get("jedla", ()):
        if not isinstance(meal, dict):
            dietary_violations += 1
            continue
        recipe = meal.get("recept") if isinstance(meal.get("recept"), dict) else {}
        template = templates.get(recipe.get("template_id"))
        if template is None or mode not in template.modes:
            dietary_violations += 1
        else:
            families.add(template.family)
            methods.add(template.method)
        if mode == "high_protein" and recipe.get("high_protein_claim") is not True:
            dietary_violations += 1
        for value in (meal.get("pokryva_dni"), recipe.get("porcie")):
            parsed = _shadow_decimal(value)
            if parsed is not None and parsed < 0:
                negative_quantities += 1
        nutrition = recipe.get("nutrition")
        if isinstance(nutrition, dict):
            for section in nutrition.values():
                if not isinstance(section, dict):
                    continue
                for value in section.values():
                    parsed = _shadow_decimal(value)
                    if parsed is not None and parsed < 0:
                        negative_quantities += 1
        for item in meal.get("suroviny", ()):
            if not isinstance(item, dict) or "offer_key" not in item:
                continue
            packages = item.get("mnozstvo")
            unit = str(item.get("jednotka") or "").strip()
            if type(packages) is not int or packages <= 0 or not _SHADOW_PACKAGE.fullmatch(unit):
                invalid_package_counts += 1

    for group in plan.get("nakupny_zoznam", ()):
        if not isinstance(group, dict):
            invalid_package_counts += 1
            continue
        for item in group.get("polozky", ()):
            if not isinstance(item, dict):
                invalid_package_counts += 1
                continue
            packages = item.get("mnozstvo")
            unit = str(item.get("jednotka") or "").strip()
            if type(packages) is not int or packages <= 0 or not _SHADOW_PACKAGE.fullmatch(unit):
                invalid_package_counts += 1
            for field in ("potrebne", "zostava"):
                parsed = _shadow_decimal(item.get(field))
                if parsed is not None and parsed < 0:
                    negative_quantities += 1

    return {
        "families": families,
        "methods": methods,
        "dietary_violations": dietary_violations,
        "negative_quantities": negative_quantities,
        "invalid_package_counts": invalid_package_counts,
    }


def _shadow_reference_price(con, server, week, rows, *, mode, adults, children,
                            frequency):
    if mode != "standard":
        return None
    try:
        signature = server.podpis_planu(
            week, list(VSETKY_OBCHODY), frequency, rows, (),
            adults=adults, children=children, stravovanie=mode,
        )
        previous = server.nacitaj_zdielany_plan(con, signature, 0)
        return _shadow_decimal(previous.get("nakup_spolu")) if previous else None
    except (AttributeError, KeyError, sqlite3.Error, TypeError, ValueError):
        return None


def _shadow_int(value, *, maximum=SHADOW_COUNTER_LIMIT):
    if type(value) is not int or value < 0 or value > maximum:
        raise ValueError("invalid shadow integer")
    return value


def _shadow_float(value, *, optional=False):
    if value is None and optional:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ValueError("invalid shadow float")
    result = float(value)
    if not math.isfinite(result) or result < 0:
        raise ValueError("invalid shadow float")
    return result


def _shadow_metrics(row):
    """Parse persisted aggregates without trusting SQLite affinity or JSON."""
    if row is None:
        raise ValueError("missing shadow metrics")
    complete = _shadow_int(row["complete"], maximum=1)
    library_gate = _shadow_int(row["library_gate_pass"], maximum=1)
    matrix_size = _shadow_int(row["matrix_size"])
    samples_total = _shadow_int(row["samples_total"], maximum=matrix_size)
    samples_success = _shadow_int(row["samples_success"], maximum=samples_total)
    family_count = _shadow_int(row["family_count"])
    method_count = _shadow_int(row["method_count"])
    library_version = _shadow_int(row["library_version"], maximum=2**63 - 1)
    price_comparisons = _shadow_int(
        row["price_comparisons"], maximum=samples_success
    )
    dietary_violations = _shadow_int(row["dietary_violations"])
    negative_quantities = _shadow_int(row["negative_quantities"])
    invalid_package_counts = _shadow_int(row["invalid_package_counts"])
    p95_ms = _shadow_float(row["p95_ms"], optional=True)
    price_delta = _shadow_float(row["price_delta_eur_avg"], optional=True)
    stored_rate = _shadow_float(row["success_rate"])
    if stored_rate > 1:
        raise ValueError("invalid shadow success rate")

    try:
        error_counts = json.loads(row["error_counts"])
    except (json.JSONDecodeError, TypeError):
        raise ValueError("invalid shadow errors") from None
    if not isinstance(error_counts, dict) or not set(error_counts) <= SHADOW_ERROR_CODES:
        raise ValueError("invalid shadow errors")
    parsed_errors = {
        key: _shadow_int(value, maximum=samples_total)
        for key, value in error_counts.items()
    }
    failures = samples_total - samples_success
    if sum(parsed_errors.values()) != failures:
        raise ValueError("inconsistent shadow errors")
    derived_rate = samples_success / samples_total if samples_total else 0.0
    if not math.isclose(stored_rate, derived_rate, rel_tol=0, abs_tol=1e-12):
        raise ValueError("inconsistent shadow success rate")
    if not isinstance(row["run_token"], str) or not row["run_token"]:
        raise ValueError("missing shadow run owner")

    return {
        "week": row["tyzden"],
        "complete": bool(complete),
        "matrix_size": matrix_size,
        "samples_total": samples_total,
        "samples_success": samples_success,
        "success_rate": derived_rate,
        "p95_ms": p95_ms,
        "error_counts": parsed_errors,
        "family_count": family_count,
        "method_count": method_count,
        "library_version": library_version,
        "price_comparisons": price_comparisons,
        "price_delta_eur_avg": price_delta,
        "dietary_violations": dietary_violations,
        "negative_quantities": negative_quantities,
        "invalid_package_counts": invalid_package_counts,
        "library_gate": "pass" if library_gate else "fail",
    }


def run_recipe_engine_shadow(*, server=None, now=None) -> dict:
    """Build the fixed matrix in scheduled work, never in a user request."""
    if server is None:
        import server as server_module

        server = server_module
    now = now or datetime.datetime.now()
    if isinstance(now, datetime.date) and not isinstance(now, datetime.datetime):
        now = datetime.datetime.combine(now, datetime.time())
    if server.recipe_engine_mode() != "shadow":
        return {"complete": False, "reason": "mode_not_shadow"}

    today = now.date()
    week = server.monday(today)
    started = now.isoformat(timespec="seconds")
    run_token = secrets.token_hex(16)
    with closing(server.db()) as con:
        migrate_predpocet_schema(con)
        rows = _shadow_offer_rows(con, server, today)
        fingerprint = _shadow_offer_fingerprint(rows)
        with con:
            con.execute(
                """INSERT INTO recipe_engine_shadow
                     (tyzden,run_token,zaciatok,koniec,complete,offer_fingerprint,
                      library_version,matrix_size,samples_total,samples_success,
                      success_rate,p95_ms,error_counts,family_count,method_count,
                      price_comparisons,price_delta_eur_avg,dietary_violations,
                      negative_quantities,invalid_package_counts,library_gate_pass)
                   VALUES (?,?,?,NULL,0,?,NULL,?,0,0,0,NULL,'{}',0,0,0,NULL,0,0,0,0)
                   ON CONFLICT(tyzden) DO UPDATE SET
                     run_token=excluded.run_token, zaciatok=excluded.zaciatok,
                     koniec=NULL, complete=0,
                     offer_fingerprint=excluded.offer_fingerprint,
                     library_version=NULL, matrix_size=excluded.matrix_size,
                     samples_total=0, samples_success=0, success_rate=0,
                     p95_ms=NULL, error_counts='{}', family_count=0,
                     method_count=0, price_comparisons=0,
                     price_delta_eur_avg=NULL, dietary_violations=0,
                     negative_quantities=0, invalid_package_counts=0,
                     library_gate_pass=0""",
                (week, run_token, started, fingerprint, len(SHADOW_MATRIX)),
            )
        if not _ma_kompletny_povinny_zber(con, server, today):
            return {
                "week": week, "complete": False,
                "reason": "incomplete_flyer_week",
            }

        try:
            ingredients, recipes, audit = _shadow_catalogs()
        except Exception:
            LOG.warning("shadow matrix: katalóg alebo library gate sa nedá načítať")
            return {"week": week, "complete": False, "reason": "library_gate_failed"}

        errors = Counter()
        durations = []
        families, methods = set(), set()
        price_deltas = []
        samples_success = 0
        dietary_violations = 0
        negative_quantities = 0
        invalid_package_counts = 0
        templates = {template.id: template for template in recipes.all()}

        for mode, adults, children, frequency in SHADOW_MATRIX:
            before = time.perf_counter()
            try:
                plan = build_deterministic_plan(
                    week=week,
                    rows=rows,
                    stores=VSETKY_OBCHODY,
                    adults=adults,
                    children=children,
                    frequency=frequency,
                    pantry=(),
                    pantry_driven=False,
                    mode=mode,
                    seed=f"shadow:{week}:{mode}:{adults}:{children}:{frequency}",
                    ingredient_catalog=ingredients,
                    recipe_catalog=recipes,
                )
                samples_success += 1
                quality = _shadow_plan_quality(plan, mode, templates)
                families.update(quality["families"])
                methods.update(quality["methods"])
                dietary_violations += quality["dietary_violations"]
                negative_quantities += quality["negative_quantities"]
                invalid_package_counts += quality["invalid_package_counts"]
                reference = _shadow_reference_price(
                    con, server, week, rows, mode=mode, adults=adults,
                    children=children, frequency=frequency,
                )
                current = _shadow_decimal(plan.get("nakup_spolu"))
                if reference is not None and current is not None:
                    price_deltas.append(abs(current - reference))
            except Exception as exc:
                code = getattr(exc, "code", None)
                if code not in {
                    "insufficient_offers", "diet_too_strict",
                    "unmeasurable_packages",
                }:
                    code = "internal_error"
                errors[code] += 1
            finally:
                durations.append(max(0.0, (time.perf_counter() - before) * 1000))

        ordered = sorted(durations)
        p95_index = max(0, math.ceil(0.95 * len(ordered)) - 1)
        p95_ms = round(ordered[p95_index], 3) if ordered else None
        total = len(SHADOW_MATRIX)
        success_rate = samples_success / total if total else 0.0
        price_delta = (
            sum(price_deltas, Decimal("0")) / len(price_deltas)
            if price_deltas else None
        )
        finished = datetime.datetime.now().isoformat(timespec="seconds")
        with con:
            updated = con.execute(
                """UPDATE recipe_engine_shadow SET
                     koniec=?, complete=1, library_version=?, samples_total=?,
                     samples_success=?, success_rate=?, p95_ms=?, error_counts=?,
                     family_count=?, method_count=?, price_comparisons=?,
                     price_delta_eur_avg=?, dietary_violations=?,
                     negative_quantities=?, invalid_package_counts=?,
                     library_gate_pass=?
                   WHERE tyzden=? AND run_token=?
                     AND offer_fingerprint=? AND complete=0""",
                (
                    finished, recipes.version, total, samples_success,
                    success_rate, p95_ms,
                    json.dumps(dict(sorted(errors.items())), separators=(",", ":")),
                    len(families), len(methods), len(price_deltas),
                    None if price_delta is None else float(price_delta),
                    dietary_violations, negative_quantities,
                    invalid_package_counts, int(not audit.errors), week,
                    run_token, fingerprint,
                ),
            )
        if updated.rowcount != 1:
            return {"week": week, "complete": False, "reason": "superseded"}
        row = con.execute(
            """SELECT * FROM recipe_engine_shadow
                 WHERE tyzden=? AND run_token=? AND offer_fingerprint=?""",
            (week, run_token, fingerprint),
        ).fetchone()

    try:
        result = _shadow_metrics(row)
    except (KeyError, TypeError, ValueError):
        return {"week": week, "complete": False, "reason": "invalid_metrics"}
    LOG.info(
        "shadow matrix week=%s samples=%s success=%s p95_ms=%s violations=%s",
        week, result["samples_total"], result["samples_success"],
        result["p95_ms"],
        result["dietary_violations"] + result["negative_quantities"]
        + result["invalid_package_counts"],
    )
    return result


def shadow_activation_status(con, *, server, today=None) -> dict:
    """Fail-closed activation evidence for the current complete flyer week."""
    today = today or datetime.date.today()
    week = server.monday(today)
    reasons = []
    migrate_predpocet_schema(con)
    complete_flyers = _ma_kompletny_povinny_zber(con, server, today)
    if not complete_flyers:
        reasons.append("incomplete_flyer_week")
    row = con.execute(
        "SELECT * FROM recipe_engine_shadow ORDER BY tyzden DESC LIMIT 1"
    ).fetchone()
    if row is None:
        if complete_flyers:
            reasons.append("missing_metrics")
        return {"eligible": False, "reasons": reasons, "week": week}

    try:
        result = _shadow_metrics(row)
    except (KeyError, TypeError, ValueError):
        reasons.append("invalid_metrics")
        return {
            "eligible": False,
            "reasons": list(dict.fromkeys(reasons)),
            "week": row["tyzden"] if row["tyzden"] else week,
        }
    result["eligible"] = False
    result["reasons"] = reasons
    metrics_complete = (
        result["complete"]
        and result["matrix_size"] == len(SHADOW_MATRIX)
        and result["samples_total"] == len(SHADOW_MATRIX)
    )
    if not metrics_complete:
        reasons.append("incomplete_metrics")

    try:
        rows = _shadow_offer_rows(con, server, today)
        _, recipes, current_audit = _shadow_catalogs()
        stale = (
            row["tyzden"] != week
            or row["offer_fingerprint"] != _shadow_offer_fingerprint(rows)
            or result["library_version"] != recipes.version
        )
        if current_audit.errors:
            reasons.append("library_gate_failed")
    except Exception:
        stale = True
        reasons.append("library_gate_failed")
    if stale:
        reasons.append("stale_metrics")
    if result["success_rate"] < SHADOW_SUCCESS_RATE_FLOOR:
        reasons.append("success_rate_below_floor")
    if result["p95_ms"] is None or result["p95_ms"] >= SHADOW_P95_LIMIT_MS:
        reasons.append("p95_too_slow")
    if result["dietary_violations"]:
        reasons.append("dietary_violations")
    if result["negative_quantities"]:
        reasons.append("negative_quantities")
    if result["invalid_package_counts"]:
        reasons.append("invalid_package_counts")
    if result["library_gate"] != "pass":
        reasons.append("library_gate_failed")

    result["reasons"] = list(dict.fromkeys(reasons))
    result["eligible"] = not result["reasons"]
    return result


def enqueue_popular_profiles(*, count=None, now=None) -> dict:
    """Zaraď cielené predpočty do trvalej fronty bez volania Anthropic."""
    now = now or datetime.datetime.now()
    if isinstance(now, datetime.date) and not isinstance(now, datetime.datetime):
        now = datetime.datetime.combine(now, datetime.time())
    count = pocet_profilov() if count is None else max(0, min(int(count), MAX_POCET_PROFILOV))
    vysledok = _novy_vysledok(profilov=count)
    if not je_zapnuty() or not count:
        vysledok["dovod"] = DOVOD_VYPNUTE
        return vysledok

    try:
        import server                    # neskoro, aby server smel importovať nás
    except Exception:
        LOG.warning("predpočet sa nespustil: appka sa nedá naimportovať", exc_info=True)
        vysledok["dovod"] = DOVOD_CHYBY
        return vysledok

    tyzden = server.monday(now.date())
    vysledok["tyzden"] = tyzden
    con = None
    beh_zarezany = False
    try:
        con = server.db()
        migrate_predpocet_schema(con)
        plan_jobs.migrate_plan_jobs_schema(con)
        profily = _cielove_profily(con, server, tyzden, count)
        vysledok["profilov"] = len(profily)
        if not _ma_kompletny_povinny_zber(con, server, now.date()):
            vysledok["blocked"] = len(profily)
            vysledok["dovod"] = DOVOD_BLOKOVANE
            _zapis_beh(con, tyzden, now, vysledok, zaciatok=False)
            return vysledok
        try:
            naklady.rezervuj_beh(con, UCEL, teraz=now)
            beh_zarezany = True
        except naklady.RozpocetVycerpany as odmietnutie:
            LOG.info("predpočet sa nespúšťa: %s", odmietnutie)
            vysledok["blocked"] = len(profily)
            vysledok["dovod"] = DOVOD_BEHY
            _zapis_beh(con, tyzden, now, vysledok, zaciatok=False)
            return vysledok

        rezerva = rezerva_eur()
        ponuky = {}
        for profil in profily:
            if profil.obchody not in ponuky:
                try:
                    ponuky[profil.obchody] = server.akcie_pre(list(profil.obchody))
                except Exception:
                    LOG.warning("ponuky pre %s sa nedajú načítať", profil.obchody, exc_info=True)
                    ponuky[profil.obchody] = []
            rows = ponuky[profil.obchody]
            if len(rows) < server.MIN_OFFERS_FOR_PLAN:
                vysledok["blocked"] += 1
                vysledok["dovod"] = DOVOD_BLOKOVANE
                continue

            podpis = _podpis_pre_profil(server, tyzden, profil, rows)
            active_job = con.execute(
                "SELECT 1 FROM plan_jobs WHERE signature=? AND variant=? "
                "AND week=? AND state IN ('queued', 'running') "
                "LIMIT 1",
                (podpis, profil.variant, tyzden),
            ).fetchone()
            if server.nacitaj_zdielany_plan(con, podpis, profil.variant) is not None or active_job:
                vysledok["skipped"] += 1
                continue
            if not _je_miesto_v_rozpocte(con, now, rezerva):
                vysledok["blocked"] += 1
                vysledok["dovod"] = DOVOD_ROZPOCET
                break

            request = plan_jobs.JobRequest(
                job_key=f"precompute:{podpis}:{profil.variant}",
                signature=podpis,
                variant=profil.variant,
                kind="precompute",
                user_id=None,
                week=tyzden,
                priority=20,
                payload={
                    "stores": list(profil.obchody),
                    "frequency": profil.frekvencia,
                    "adults": profil.dospeli,
                    "children": profil.deti,
                    "algo_version": server.PLAN_ALGO_VERSION,
                },
                reserved_eur=CENA_ZA_PROFIL_EUR,
            )
            try:
                result = plan_jobs.enqueue(con, request, now=now)
            except naklady.RozpocetVycerpany as odmietnutie:
                LOG.info("profil sa do predpočtu nezmestil: %s", odmietnutie)
                vysledok["blocked"] += 1
                vysledok["dovod"] = DOVOD_ROZPOCET
                break
            except (sqlite3.Error, OSError, ValueError) as chyba:
                LOG.warning("profil %s sa nedal zaradiť: %s", profil, chyba)
                vysledok["blocked"] += 1
                vysledok["dovod"] = DOVOD_BLOKOVANE
                continue
            if result.created:
                vysledok["queued"] += 1
            else:
                vysledok["skipped"] += 1

        vysledok["preskocenych"] = vysledok["skipped"]
        if vysledok["blocked"]:
            vysledok["dovod"] = vysledok["dovod"] if vysledok["dovod"] != DOVOD_HOTOVO else DOVOD_BLOKOVANE
        if beh_zarezany and not vysledok["queued"]:
            naklady.uvolni_beh(con, UCEL, teraz=now)
        _zapis_beh(con, tyzden, now, vysledok)
    except Exception:
        LOG.warning("predpočet skončil chybou", exc_info=True)
        vysledok["dovod"] = DOVOD_CHYBY
    finally:
        if con is not None:
            try:
                con.close()
            except sqlite3.Error:
                pass
    return vysledok


def zahrej(*, pocet=None, dnes=None, teraz=None, klient=None) -> dict:
    """Scheduled precompute; shadow work runs here, never in an HTTP request."""
    if teraz is None and dnes is not None:
        teraz = datetime.datetime.combine(dnes, datetime.time())
    vysledok = enqueue_popular_profiles(count=pocet, now=teraz)
    try:
        import server

        if server.recipe_engine_mode() == "shadow":
            vysledok["shadow"] = run_recipe_engine_shadow(server=server, now=teraz)
    except Exception:
        # Shadow je pozorovanie, nie používateľská cesta. Zlyhanie sa prizná
        # agregovaným kódom a nesmie zhodiť starý predpočet ani rannú appku.
        LOG.warning("scheduled shadow comparison failed")
        vysledok["shadow"] = {"complete": False, "reason": "internal_error"}
    return vysledok


def _minute_na_ucel(con, teraz) -> float:
    try:
        _, _, tyzden = naklady._obdobia(teraz)
        return naklady.spolu_za_ucel_tyzden(con, UCEL, tyzden)
    except (sqlite3.Error, OSError):
        return 0.0


def _zapis_beh(con, tyzden, teraz, vysledok, zaciatok=True) -> None:
    """Zapíš, ako beh dopadol. Súčty sa PRIPOČÍTAVAJÚ — týždeň môže mať viac
    behov a majiteľa zaujíma, čo sa za týždeň zahrialo dokopy."""
    cas = teraz.isoformat(timespec="seconds")
    try:
        with con:
            con.execute(
                """INSERT INTO predpocet_behy
                     (tyzden, zaciatok, koniec, behov, zahriatych, preskocenych,
                      zlyhanych, eur, zasahy, dovod)
                   VALUES (?, ?, ?, 1, ?, ?, ?, ?, 0, ?)
                   ON CONFLICT(tyzden) DO UPDATE SET
                     koniec = excluded.koniec,
                     behov = behov + 1,
                     zahriatych = zahriatych + excluded.zahriatych,
                     preskocenych = preskocenych + excluded.preskocenych,
                     zlyhanych = zlyhanych + excluded.zlyhanych,
                     eur = eur + excluded.eur,
                     dovod = excluded.dovod""",
                (tyzden, cas if zaciatok else None, cas, vysledok["zahriatych"],
                 vysledok["preskocenych"], vysledok["zlyhanych"], vysledok["eur"],
                 vysledok["dovod"]),
            )
            con.execute("DELETE FROM predpocet_behy WHERE tyzden < ?",
                        ((datetime.date.fromisoformat(tyzden)
                          - datetime.timedelta(weeks=DRZANIE_DOPYTU_TYZDNOV)).isoformat(),))
    except (sqlite3.Error, OSError, ValueError):
        LOG.debug("beh predpočtu sa nezapísal", exc_info=True)


def zapocitaj_zasah(con, podpis, variant, tyzden) -> None:
    """Zdieľaný plán sa podal — a bol predpočítaný, takže sa niečo ušetrilo.

    Pripočíta sa len vtedy, keď riadok naozaj vznikol v noci. Diagnostika
    nikdy nesmie zhodiť odpoveď používateľovi, tak sa každá chyba prehltne.
    """
    try:
        con.execute(
            """UPDATE predpocet_behy SET zasahy = zasahy + 1
               WHERE tyzden = ?
                 AND (SELECT predpocitany FROM plany_zdielane
                      WHERE podpis = ? AND variant = ?) = 1""",
            (str(tyzden), podpis, variant),
        )
    except (sqlite3.Error, OSError):
        LOG.debug("zásah do predpočítaného plánu sa nezapočítal", exc_info=True)


# ---------------------------------------------------------------- prehľad
def stav(con, teraz=None) -> dict:
    """Funguje to? Koľko to stálo? Koľko čakania to ušetrilo?

    Diagnostika — nesmie zhodiť /api/health ani /api/naklady, tak sa pokazená
    evidencia prizná v `chyba` namiesto toho, aby predstierala nulu.
    """
    teraz = teraz or datetime.datetime.now()
    tyzden = (teraz.date() - datetime.timedelta(days=teraz.date().weekday())).isoformat()
    prehlad = {
        "tyzden": tyzden,
        "zapnuty": je_zapnuty(),
        "profilov": pocet_profilov(),
        "cena_za_profil_eur": CENA_ZA_PROFIL_EUR,
        # Koľko by stál plný beh pri terajšom nastavení — číslo, podľa ktorého
        # sa majiteľ rozhoduje, či zahrievať viac alebo menej.
        "odhad_plneho_behu_eur": round(pocet_profilov() * CENA_ZA_PROFIL_EUR, 6),
        "zahriatych": 0,
        "preskocenych": 0,
        "zlyhanych": 0,
        "queued": 0,
        "skipped": 0,
        "blocked": 0,
        "eur": 0.0,
        "skutocna_cena_za_profil_eur": None,
        "usetrenych_generovani": 0,
        "hotovych_planov": 0,
        "posledny_beh": None,
        "dovod": None,
        "vysvetlenie": None,
        "chyba": None,
    }
    try:
        riadok = con.execute(
            "SELECT * FROM predpocet_behy WHERE tyzden = ?", (tyzden,)
        ).fetchone()
        if riadok is not None:
            prehlad.update({
                "zahriatych": int(riadok["zahriatych"]),
                "preskocenych": int(riadok["preskocenych"]),
                "zlyhanych": int(riadok["zlyhanych"]),
                "eur": round(float(riadok["eur"]), 6),
                "usetrenych_generovani": int(riadok["zasahy"]),
                "posledny_beh": riadok["koniec"],
                "dovod": riadok["dovod"],
                "vysvetlenie": VYSVETLENIE.get(riadok["dovod"]),
            })
            prehlad["skipped"] = prehlad["preskocenych"]
            if prehlad["zahriatych"]:
                # Nameraná cena, nie odhad — toto je to číslo, ktoré rozhoduje.
                prehlad["skutocna_cena_za_profil_eur"] = round(
                    prehlad["eur"] / prehlad["zahriatych"], 6)
        prehlad["hotovych_planov"] = int(con.execute(
            "SELECT COUNT(*) FROM plany_zdielane WHERE tyzden = ? AND predpocitany = 1",
            (tyzden,),
        ).fetchone()[0])
        try:
            prehlad["queued"] = int(con.execute(
                "SELECT COUNT(*) FROM plan_jobs "
                "WHERE week=? AND kind='precompute' AND state IN ('queued', 'running')",
                (tyzden,),
            ).fetchone()[0])
        except sqlite3.Error:
            pass
    except (sqlite3.Error, OSError) as chyba:
        prehlad["chyba"] = f"{type(chyba).__name__}: {chyba}"[:200]
    return prehlad


# ---------------------------------------------------------------- príkazový riadok
NAPOVEDA = """Predpočítanie zdieľaných jedálničkov (zahrievanie cache).

Použitie na serveri:
    cd /opt/uvarsi/app
    UVARSI_URL=https://uvar.si ../venv/bin/python predpocet.py --zahrej
    UVARSI_URL=https://uvar.si ../venv/bin/python predpocet.py --stav

Koľko sa zahrieva: UVARSI_PREDPOCET_PROFILOV (východzie %d, strop %d).
Vypnutie:          UVARSI_PREDPOCET=0
Rezerva pre ľudí:  UVARSI_PREDPOCET_REZERVA_EUR (východzie %.2f €)
""" % (VYCHODZI_POCET_PROFILOV, MAX_POCET_PROFILOV, VYCHODZIA_REZERVA_EUR)


def cli(argv=None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="predpocet.py", description=NAPOVEDA,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--zahrej", action="store_true",
                        help="poskladaj dopredu najžiadanejšie plány")
    parser.add_argument("--stav", action="store_true",
                        help="ako sa predpočtu tento týždeň darilo")
    parser.add_argument("--pocet", type=int, default=None,
                        help="koľko plánov zahriať (inak UVARSI_PREDPOCET_PROFILOV)")
    argumenty = parser.parse_args(argv)

    if argumenty.stav:
        import server
        with closing(server.db()) as con:
            migrate_predpocet_schema(con)
            prehlad = stav(con)
        for kluc, hodnota in prehlad.items():
            print(f"  {kluc}: {hodnota}")
        return 0

    if not argumenty.zahrej:
        parser.print_help()
        return 2

    vysledok = zahrej(pocet=argumenty.pocet)
    print(
        f"Predpočet {vysledok['tyzden']}: zaradených {vysledok['queued']}, "
        f"preskočených {vysledok['skipped']}, blokovaných {vysledok['blocked']} "
        f"— {VYSVETLENIE.get(vysledok['dovod'], vysledok['dovod'])}"
    )
    # Fronta je hotová aj vtedy, keď niektoré profily čakajú na ďalší beh.
    # Samotné generovanie patrí workeru, nie tomuto rýchlemu hodinovému príkazu.
    return 1 if vysledok["zlyhanych"] or vysledok["dovod"] == DOVOD_CHYBY else 0


if __name__ == "__main__":
    raise SystemExit(cli())
