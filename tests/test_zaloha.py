"""Nočná záloha databázy — jediný záznam o tom, kto zaplatil.

Audit 24. 8. 2026: tabuľka `naroky` je jediný dôkaz o platbách a NIČ ju
nezálohovalo. `samopull.sh` databázu zámerne necháva na pokoji a adresár
`predosle` drží iba kód. Strata disku = strata zoznamu platiacich.

Zálohovať SQLite obyčajným `cp` sa nesmie: pri súbežnom zápise skopíruje
rozpísanú stránku a vo WAL režime navyše nechá `-wal` bokom, takže kópia je
nepoužiteľná presne vtedy, keď ju treba. Preto `VACUUM INTO` a overenie.

Na serveri beží aj druhá appka (taktik-mapa) — záloha sa jej nesmie dotknúť.
"""
import os
import re
import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
ZALOHA = ROOT / "hetzner" / "zaloha.sh"
DEPLOY = ROOT / "nasad.ps1"


@pytest.fixture(scope="module")
def script() -> str:
    assert ZALOHA.exists(), (
        "chýba hetzner/zaloha.sh — databáza s tabuľkou `naroky` nemá zálohu"
    )
    return ZALOHA.read_text(encoding="utf-8")


# --------------------------------------------------------------- tvar skriptu
def test_backup_script_is_lf_only():
    """CRLF v shebangu = `cannot execute: required file not found`."""
    assert b"\r\n" not in ZALOHA.read_bytes(), (
        "shell skripty musia mať LF konce riadkov"
    )


def test_backup_script_has_a_bash_shebang(script):
    assert script.startswith("#!/bin/bash")


def test_backup_uses_sqlite_backup_not_a_file_copy(script):
    assert "VACUUM INTO" in script or ".backup" in script, (
        "SQLite sa zálohuje cez VACUUM INTO / .backup — tie vedia o WAL a o "
        "súbežných zapisovateľoch"
    )
    telo = "\n".join(
        line for line in script.splitlines() if not line.strip().startswith("#")
    )
    assert not re.search(r"\bcp\b[^\n]*\$DB|\bcp\b[^\n]*\.db\b", telo), (
        "obyčajný `cp` databázy skopíruje rozpísanú stránku — kópia je "
        "poškodená práve vtedy, keď ju treba"
    )


def test_backup_verifies_what_it_wrote(script):
    assert "integrity_check" in script, (
        "nepreverená záloha je iba nádej; kópia sa musí otvoriť a overiť"
    )
    assert "naroky" in script, (
        "overenie musí siahnuť na tabuľku `naroky` — kvôli nej záloha existuje"
    )


def test_backup_rotates_old_copies(script):
    assert re.search(r"DRZAT|POCET_ZALOH|-mtime|RETENCIA", script), (
        "bez rotácie zálohy zaplnia disk a zhodia appku, ktorú mali chrániť"
    )


def test_backup_never_touches_the_other_app(script):
    """Komentár smie taktik-mapu spomenúť; ŽIADNY príkaz sa jej nesmie dotknúť."""
    prikazy = "\n".join(
        line for line in script.splitlines() if not line.strip().startswith("#")
    )
    for cudzie in ("taktik-mapa", "mapa.89"):
        assert cudzie not in prikazy, (
            f"na serveri beží aj taktik-mapa — záloha Uvar.si sa jej nesmie "
            f"dotknúť, ale skript pracuje s {cudzie}"
        )
    for cesta in re.findall(r"(/(?:opt|var|etc)/[A-Za-z0-9_./-]+)", script):
        assert cesta.startswith(("/opt/uvarsi", "/var/backups/uvarsi",
                                 "/var/log/uvarsi", "/var/lib/uvarsi")), (
            f"záloha siaha mimo Uvar.si: {cesta}"
        )


def test_backup_documents_its_cron_line(script):
    assert re.search(
        r"^#\s*\S+\s+\S+\s+\S+\s+\S+\s+\S+\s+/opt/uvarsi/zaloha\.sh",
        script, flags=re.M,
    ), "hlavička musí dokumentovať cron riadok, ako to robí dozorca.sh"


def _documented_cron_line() -> str:
    match = re.search(
        r"^#\s*(\S+\s+\S+\s+\S+\s+\S+\s+\S+\s+/opt/uvarsi/zaloha\.sh.*)$",
        ZALOHA.read_text(encoding="utf-8"), flags=re.M,
    )
    assert match, "hlavička zaloha.sh musí dokumentovať cron riadok"
    return match.group(1).strip()


def test_backup_runs_nightly():
    riadok = _documented_cron_line()
    hodina = riadok.split()[1]
    assert hodina.isdigit() and 0 <= int(hodina) <= 5, (
        f"záloha má bežať v noci, nie o {hodina}:00 — rozvrh: {riadok}"
    )


# ----------------------------------------------------------------- nasadenie
@pytest.fixture(scope="module")
def deploy() -> str:
    return DEPLOY.read_text(encoding="utf-8")


def test_deploy_transfers_the_backup_script(deploy):
    assert '"$B\\hetzner\\zaloha.sh"' in deploy, (
        "zaloha.sh sa musí nasadzovať, inak na serveri nikdy nevznikne"
    )
    assert 'r = "/opt/uvarsi/zaloha.sh"' in deploy


def test_deploy_installs_the_documented_backup_cron_line(deploy):
    riadok = _documented_cron_line()
    assert riadok in deploy, (
        "rozvrh zálohy ostal iba komentárom — na serveri sa nikdy nespustí. "
        f"Očakávam v nasad.ps1 presne:\n  {riadok}"
    )


def test_deploy_installs_exactly_one_backup_cron_line(deploy):
    bloky = [
        blok for blok in re.findall(r"@'\n(.*?)\n'@", deploy, flags=re.S)
        if "crontab" in blok and "zaloha.sh" in blok
    ]
    assert bloky, "očakávam bash blok, ktorý inštaluje cron pre zálohu"
    blok = bloky[0]
    assert "grep -v" in blok, (
        "opakované nasadenie by inak pridalo druhý rovnaký riadok"
    )
    assert re.search(r"zaloha\.sh'\s*\|\|\s*true|-c 'zaloha\.sh'|grep -c", blok), (
        "nasadenie musí overiť, že v crontabe je práve jeden riadok so zálohou"
    )


def test_deploy_cron_keeps_the_other_apps_entries(deploy):
    """`crontab -l | grep -v ...` musí ostatné riadky zachovať, nie prepísať."""
    bloky = [
        blok for blok in re.findall(r"@'\n(.*?)\n'@", deploy, flags=re.S)
        if "crontab" in blok
    ]
    for blok in bloky:
        assert "crontab -l" in blok, (
            "cron sa nesmie prepísať naslepo — taktik-mapa má vlastné riadky"
        )
        assert not re.search(r"crontab\s+-r", blok), (
            "`crontab -r` zmaže aj cron druhej appky"
        )


# ------------------------------------------------------- naozaj to zálohuje?
pytestmark_bash = pytest.mark.skipif(
    shutil.which("bash") is None, reason="bash nie je k dispozícii"
)


def _priprav_databazu(cesta: Path) -> None:
    con = sqlite3.connect(cesta)
    con.executescript(
        """CREATE TABLE naroky (user_id INTEGER, stav TEXT);
           INSERT INTO naroky VALUES (7, 'aktivny');"""
    )
    con.commit()
    con.close()


@pytestmark_bash
def test_backup_produces_a_readable_copy(tmp_path):
    database = tmp_path / "uvarsi.db"
    _priprav_databazu(database)
    kam = tmp_path / "zalohy"

    beh = subprocess.run(
        ["bash", str(ZALOHA)],
        env={**os.environ, "UVARSI_DB": str(database), "UVARSI_ZALOHY": str(kam),
             "UVARSI_PY": "python3", "UVARSI_TICHO": "1"},
        capture_output=True, text=True,
    )

    assert beh.returncode == 0, f"záloha zlyhala:\n{beh.stdout}\n{beh.stderr}"
    kopie = sorted(kam.glob("*.db"))
    assert len(kopie) == 1, f"očakávam jednu zálohu, mám {kopie}"
    con = sqlite3.connect(kopie[0])
    assert con.execute("SELECT user_id FROM naroky").fetchone()[0] == 7, (
        "záloha musí obsahovať `naroky` — kvôli nim celá existuje"
    )
    con.close()


@pytestmark_bash
def test_backup_survives_an_open_writer(tmp_path):
    """Presne stav, v ktorom `cp` vyrobí poškodenú kópiu."""
    database = tmp_path / "uvarsi.db"
    _priprav_databazu(database)
    kam = tmp_path / "zalohy"

    zapisovatel = sqlite3.connect(database, timeout=20)
    zapisovatel.execute("PRAGMA journal_mode=WAL")
    zapisovatel.execute("INSERT INTO naroky VALUES (8, 'aktivny')")
    zapisovatel.commit()

    beh = subprocess.run(
        ["bash", str(ZALOHA)],
        env={**os.environ, "UVARSI_DB": str(database), "UVARSI_ZALOHY": str(kam),
             "UVARSI_PY": "python3", "UVARSI_TICHO": "1"},
        capture_output=True, text=True,
    )
    zapisovatel.close()

    assert beh.returncode == 0, f"záloha zlyhala:\n{beh.stdout}\n{beh.stderr}"
    kopia = sorted(kam.glob("*.db"))[0]
    con = sqlite3.connect(kopia)
    assert con.execute("SELECT COUNT(*) FROM naroky").fetchone()[0] == 2, (
        "commitnutý riadok musí byť v zálohe aj vtedy, keď dáta ležia vo -wal"
    )
    assert con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    con.close()


@pytestmark_bash
def test_backup_rotation_keeps_a_bounded_number_of_copies(tmp_path):
    database = tmp_path / "uvarsi.db"
    _priprav_databazu(database)
    kam = tmp_path / "zalohy"
    kam.mkdir()
    for index in range(40):
        (kam / f"uvarsi-2026-01-{index:02d}.db").write_bytes(b"stara")

    beh = subprocess.run(
        ["bash", str(ZALOHA)],
        env={**os.environ, "UVARSI_DB": str(database), "UVARSI_ZALOHY": str(kam),
             "UVARSI_PY": "python3", "UVARSI_TICHO": "1"},
        capture_output=True, text=True,
    )

    assert beh.returncode == 0, f"záloha zlyhala:\n{beh.stdout}\n{beh.stderr}"
    zostalo = list(kam.glob("*.db"))
    assert len(zostalo) <= 20, (
        f"rotácia nechala {len(zostalo)} súborov — disk sa zaplní a zhodí appku"
    )


@pytestmark_bash
def test_backup_fails_loudly_on_a_missing_database(tmp_path):
    beh = subprocess.run(
        ["bash", str(ZALOHA)],
        env={**os.environ, "UVARSI_DB": str(tmp_path / "niet.db"),
             "UVARSI_ZALOHY": str(tmp_path / "zalohy"),
             "UVARSI_PY": "python3", "UVARSI_TICHO": "1"},
        capture_output=True, text=True,
    )
    assert beh.returncode != 0, (
        "chýbajúca databáza musí zálohu zhodiť, nie ticho prejsť ako úspech"
    )
