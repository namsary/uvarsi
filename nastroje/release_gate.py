#!/usr/bin/env python3
"""Uvar.si — RELEASE GATE (spustiteľná časť release procesu).

Nie je to kontrolný zoznam na čítanie. Spustí testy, overí živú produkciu
a vydá jeden z troch verdiktov:

    BLOCKED             niečo neprešlo — nesmie sa tvrdiť, že je hotovo
    LOCAL PASS          lokálne zelené, ale v produkcii to ešte nie je
    PRODUCTION VERIFIED  živý web dokázateľne beží na tejto verzii

Každý beh sa zapíše do RELEASE_LOG.md, aby sa nikdy nestalo
„opravené v kóde, ale nie na serveri".

Použitie:
    python3 nastroje/release_gate.py              # testy + produkcia
    python3 nastroje/release_gate.py --len-testy  # bez siete
"""
from __future__ import annotations

import argparse
import datetime
import json
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

KOREN = Path(__file__).resolve().parent.parent
LOG = KOREN / "RELEASE_LOG.md"
WEB = "https://uvar.si"

# Windows-only testy (volajú cscript.exe / Git bash) sa mimo Windows nedajú spustiť
MIMO_WINDOWS = ["tests/test_app_html_contract.py", "tests/test_dozorca_contract.py"]

MIN_PONUK = 30          # rovnaký prah ako hetzner/dozorca.sh


class Zistenie:
    def __init__(self, nazov: str, ok: bool, detail: str, blokuje: bool = True):
        self.nazov, self.ok, self.detail, self.blokuje = nazov, ok, detail, blokuje

    def __str__(self) -> str:
        znak = "OK  " if self.ok else ("!!  " if self.blokuje else "--  ")
        return f"  {znak}{self.nazov}: {self.detail}"


def pondelok(d: datetime.date | None = None) -> str:
    d = d or datetime.date.today()
    return (d - datetime.timedelta(days=d.weekday())).isoformat()


def _spusti(prikaz: list[str], timeout: int = 600) -> tuple[int, str]:
    try:
        v = subprocess.run(prikaz, cwd=KOREN, capture_output=True, text=True, timeout=timeout)
        return v.returncode, (v.stdout + v.stderr)
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        return 1, str(e)


# ----------------------------------------------------------------- lokálne
def brana_testy() -> list[Zistenie]:
    prikaz = [sys.executable, "-m", "pytest", "tests/", "-q"]
    for s in MIMO_WINDOWS:
        prikaz += ["--deselect", s]
    kod, vystup = _spusti(prikaz)
    m = re.search(r"(\d+) passed", vystup)
    preslo = int(m.group(1)) if m else 0
    zlyhalo = int(m.group(1)) if (m := re.search(r"(\d+) failed", vystup)) else 0
    return [Zistenie(
        "testy", kod == 0 and zlyhalo == 0,
        f"{preslo} presly, {zlyhalo} zlyhalo"
        + ("" if kod == 0 else f" (pytest kod {kod})"))]


def brana_git() -> list[Zistenie]:
    z = []
    kod, sha = _spusti(["git", "rev-parse", "HEAD"], timeout=30)
    sha = sha.strip()[:12] if kod == 0 else "?"
    z.append(Zistenie("git revizia", kod == 0, sha))

    kod, stav = _spusti(["git", "status", "--porcelain"], timeout=30)
    nezapisane = [r for r in stav.splitlines() if r.strip()]
    z.append(Zistenie("nezapisane zmeny", not nezapisane,
                      "ziadne" if not nezapisane
                      else f"{len(nezapisane)} suborov nie je commitnutych"))

    kod, vzdialene = _spusti(["git", "rev-parse", "origin/main"], timeout=30)
    if kod == 0:
        rovnake = vzdialene.strip()[:12] == sha
        z.append(Zistenie("push na origin/main", rovnake,
                          "lokalne == vzdialene" if rovnake
                          else f"vzdialene je {vzdialene.strip()[:12]} — treba pushnut"))
    return z


def brana_verzia() -> list[Zistenie]:
    v = KOREN / "VERSION"
    if not v.is_file():
        return [Zistenie("VERSION", False, "subor chyba")]
    return [Zistenie("VERSION", True, v.read_text(encoding="utf-8").strip())]


# ----------------------------------------------------------------- produkcia
def _ziskaj(cesta: str, timeout: int = 25):
    try:
        with urllib.request.urlopen(WEB + cesta, timeout=timeout) as o:
            return o.status, o.read(20000)
    except urllib.error.HTTPError as e:
        return e.code, b""
    except Exception as e:                                  # sieť/TLS
        return 0, str(e).encode()


def brana_produkcia(ocakavana_verzia: str) -> list[Zistenie]:
    z = []
    stav, telo = _ziskaj("/api/health")
    if stav != 200:
        z.append(Zistenie("/api/health", False, f"HTTP {stav} — appka neodpoveda"))
        return z
    try:
        h = json.loads(telo)
    except ValueError:
        z.append(Zistenie("/api/health", False, "odpoved nie je JSON"))
        return z

    z.append(Zistenie("/api/health", True, json.dumps(h, ensure_ascii=False)))

    ziva = str(h.get("vydanie", "?"))
    z.append(Zistenie("verzia na webe", ziva == ocakavana_verzia,
                      f"{ziva} (ocakavam {ocakavana_verzia})"))

    tyz = str(h.get("tyzden", ""))
    z.append(Zistenie("tyzden dat", tyz == pondelok(),
                      f"{tyz} (aktualny pondelok {pondelok()})"))

    pocet = int(h.get("pocet") or 0)
    z.append(Zistenie("pocet ponuk", pocet >= MIN_PONUK, f"{pocet} (prah {MIN_PONUK})"))

    for cesta, popis in (("/", "landing"), ("/app", "appka"),
                         ("/api/public/landing", "landing JSON"),
                         ("/prihlasenie", "prihlasovacia stranka")):
        stav, _ = _ziskaj(cesta)
        z.append(Zistenie(popis, stav == 200, f"HTTP {stav}"))

    # legitimnost: landing nesmie tvrdit uspory bez dat
    stav, telo = _ziskaj("/api/public/landing")
    if stav == 200:
        try:
            d = json.loads(telo)
            sedi = d.get("week") == pondelok()
            z.append(Zistenie("landing je na aktualny tyzden", sedi,
                              f"{d.get('week')} vs {pondelok()}"))
        except ValueError:
            z.append(Zistenie("landing JSON", False, "nie je platny JSON"))
    return z


# ----------------------------------------------------------------- verdikt
def zapis_log(verdikt: str, zistenia: list[Zistenie], verzia: str) -> None:
    riadky = [f"\n## {datetime.datetime.now():%Y-%m-%d %H:%M} — {verdikt} (vydanie {verzia})\n"]
    riadky += [str(x).rstrip() + "\n" for x in zistenia]
    with LOG.open("a", encoding="utf-8") as f:
        f.writelines(riadky)


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--len-testy", action="store_true", help="preskoci overenie produkcie")
    a = p.parse_args()

    zistenia = brana_verzia() + brana_testy() + brana_git()
    verzia = zistenia[0].detail

    if not a.len_testy:
        zistenia += brana_produkcia(verzia)

    blokery = [x for x in zistenia if not x.ok and x.blokuje]
    if blokery:
        verdikt = "BLOCKED"
    elif a.len_testy:
        verdikt = "LOCAL PASS"
    else:
        verdikt = "PRODUCTION VERIFIED"

    print(f"\n=== RELEASE GATE — {verdikt}\n")
    for x in zistenia:
        print(x)
    if blokery:
        print("\nBlokuje:")
        for x in blokery:
            print(f"  - {x.nazov}: {x.detail}")
    print()

    zapis_log(verdikt, zistenia, verzia)
    return 0 if verdikt == "PRODUCTION VERIFIED" else (0 if verdikt == "LOCAL PASS" else 1)


if __name__ == "__main__":
    raise SystemExit(main())
