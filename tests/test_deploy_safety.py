"""Nasadenie MUSÍ vedieť zlyhať.

Audit 20. 8. 2026: `nasad.ps1` hlásil úspech bez ohľadu na to, čo sa pokazilo —
tri výpadky za týždeň. Konkrétne:

  * $ErrorActionPreference = "Continue" a nikde sa nekontroloval $LASTEXITCODE,
  * Caddyfile sa zapísal na disk PRED validáciou a exit kód validácie zjedla
    rúra (`caddy validate ... | tail -3 && systemctl reload`), takže reload
    prebehol aj nad rozbitým configom — a na serveri beží aj druhá appka
    (taktik-mapa, site `mapa.89.167.72.159.sslip.io`), ktorú rozbitý Caddyfile
    zhodí pri najbližšom reštarte,
  * `shutil.copy(p, p + ".zaloha")` prepísal jedinú zálohu pri každom behu,
  * venv sa nikdy nevytvoril, cron pre dozorcu sa nikdy nenainštaloval,
  * uvarsi.env sa neoveroval a kontrola po nasadení netvrdila prakticky nič.

Testy držia text `nasad.ps1` — rovnako ako ostatné tests/test_deploy_*.py.
"""
import re
from pathlib import Path

import pytest


DEPLOY = Path("nasad.ps1")
DOZORCA = Path("hetzner/dozorca.sh")
MAPA_SITE = "mapa.89.167.72.159.sslip.io"
CADDYFILE = "/etc/caddy/Caddyfile"


@pytest.fixture(scope="module")
def script() -> str:
    return DEPLOY.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def lines(script) -> list[str]:
    return script.splitlines()


def _powershell_lines(lines: list[str]) -> list[tuple[int, str]]:
    """Riadky mimo here-stringov (@' ... '@), teda skutočný PowerShell kód."""
    out, v_heredoku = [], False
    for index, line in enumerate(lines):
        if not v_heredoku and re.search(r"@'\s*$", line):
            v_heredoku = True
            continue
        if v_heredoku:
            if line.startswith("'@"):
                v_heredoku = False
            continue
        out.append((index, line))
    return out


def _heredoc_blocks(lines: list[str]) -> list[str]:
    """Telá here-stringov — bash/python, ktoré sa posielajú na server."""
    bloky, aktualny = [], None
    for line in lines:
        if aktualny is None:
            if re.search(r"@'\s*$", line):
                aktualny = []
            continue
        if line.startswith("'@"):
            bloky.append("\n".join(aktualny))
            aktualny = None
            continue
        aktualny.append(line)
    return bloky


# ------------------------------------------------------- 3. nič nesmie prejsť ticho
def test_error_action_preference_stops(script):
    assert '$ErrorActionPreference = "Stop"' in script, (
        'nasadenie bežalo s "Continue" — chyby sa ignorovali a skript dobehol do konca'
    )
    assert '$ErrorActionPreference = "Continue"' not in script


def test_deploy_has_a_failure_exit_path(script):
    assert re.search(r"^\s*exit 1\s*$", script, flags=re.M), (
        "skript nemal jedinú vetvu, ktorá by skončila nenulovým kódom"
    )


def test_every_remote_call_checks_its_exit_code(lines):
    """scp/ssh bez kontroly $LASTEXITCODE = tichý výpadok."""
    kod = _powershell_lines(lines)
    volania = [
        (index, line) for index, line in kod
        if re.search(r"(^|\|\s*|\s)(ssh|scp)\s", line) and not line.strip().startswith("#")
    ]
    assert volania, "očakávam aspoň jedno vzdialené volanie v nasad.ps1"

    poradie = [index for index, _ in kod]
    nekontrolovane = []
    for index, line in volania:
        pozicia = poradie.index(index)
        nasledujuce = [text for _, text in kod[pozicia + 1:pozicia + 4]]
        if not any(
            "Vyzaduj" in text or "$LASTEXITCODE" in text for text in nasledujuce
        ):
            nekontrolovane.append(line.strip())
    assert not nekontrolovane, (
        "tieto vzdialené volania nekontrolujú návratový kód:\n  "
        + "\n  ".join(nekontrolovane)
    )


def test_exit_code_helper_actually_exits(script):
    assert re.search(
        r"function Vyzaduj.*\$LASTEXITCODE -ne 0", script, flags=re.S
    ), "musí existovať pomocník, ktorý po nenulovom exit kóde nasadenie zastaví"


def test_missing_local_file_fails_the_deploy(script):
    """Chýbajúci lokálny súbor sa predtým iba vypísal načerveno a išlo sa ďalej."""
    assert re.search(r"Test-Path[^\n]*\n[^\n]*Zlyhaj", script) or re.search(
        r"if \(-not \(Test-Path[^\n]*\)\) \{ Zlyhaj", script
    ), "chýbajúci súbor v manifeste musí nasadenie zastaviť, nie ho len oznámiť"


# ------------------------------------------------------------------ 2. Caddy
def test_caddyfile_is_never_written_in_place(script):
    assert 'open(p, "w")' not in script and 'open(p,"w")' not in script, (
        f"{CADDYFILE} sa nesmie prepísať pred validáciou — zapisuj do dočasného "
        "súboru a až overený ho presuň"
    )


def test_caddy_proposal_goes_to_a_temp_path_first(script):
    assert re.search(r"Caddyfile\.(nove|novy|tmp)", script), (
        "návrh configu musí vzniknúť na dočasnej ceste vedľa ostrého Caddyfile"
    )


def test_caddy_validate_runs_on_the_temp_file_as_its_own_command(script):
    validacie = [
        line for line in script.splitlines()
        if "caddy validate" in line and not line.strip().startswith("#")
    ]
    assert validacie, "nasadenie musí Caddyfile validovať"
    for line in validacie:
        assert "|" not in line, (
            "exit kód `caddy validate` nesmie zjesť rúra (`| tail`) — v rúre je "
            f"návratový kód posledného článku, teda vždy 0:\n  {line}"
        )
        assert "&&" not in line and "||" not in line, (
            f"validácia musí byť samostatný príkaz, nie článok reťaze:\n  {line}"
        )
        assert re.search(r"Caddyfile\.(nove|novy|tmp)", line), (
            f"validovať sa má návrh, nie ostrý Caddyfile:\n  {line}"
        )


def test_caddy_reload_is_not_chained_to_a_pipeline(script):
    assert "| tail -3 && systemctl reload caddy" not in script
    assert "CADDY_CHYBA" not in script, (
        "`|| echo CADDY_CHYBA` iba vypíše text a vráti 0 — zlyhanie musí "
        "zhodiť nasadenie"
    )


def test_caddy_step_aborts_on_the_first_error(script):
    bash = [blok for blok in _heredoc_blocks(script.splitlines()) if "caddy validate" in blok]
    assert bash, "očakávam bash blok, ktorý validuje a nasadzuje Caddy config"
    for blok in bash:
        assert re.search(r"^set -e", blok, flags=re.M), (
            "bash blok okolo caddy validate musí bežať so `set -e`, inak sa "
            "pokračuje aj po zlyhaní"
        )


def test_caddy_backup_is_timestamped(script):
    assert 'shutil.copy(p, p + ".zaloha")' not in script, (
        "pevný názov zálohy prepíše poslednú funkčnú kópiu už pri druhom nasadení"
    )
    zalohy = [line for line in script.splitlines() if "zaloha" in line]
    assert zalohy, "nasadenie musí Caddyfile zálohovať"
    assert any("date +" in line for line in zalohy), (
        "záloha musí mať v názve časovú pečiatku, aby sa neprepisovala"
    )


def test_other_app_site_block_is_guarded_before_anything_is_written(script):
    assert MAPA_SITE in script, (
        f"na serveri beží aj taktik-mapa ({MAPA_SITE}); nasadenie musí overiť, "
        "že jej site blok v configu ostal"
    )
    python_bloky = [
        blok for blok in _heredoc_blocks(script.splitlines()) if "Caddyfile" in blok and "def " in blok
    ]
    assert python_bloky, "očakávam python blok, ktorý upravuje Caddyfile"
    for blok in python_bloky:
        assert MAPA_SITE in blok, f"python blok musí poznať site {MAPA_SITE}"
        # Stráž smie site pomenovať priamo alebo cez konštantu (MAPA = "...").
        strazca = re.search(rf'(\w+)\s*=\s*"{re.escape(MAPA_SITE)}"', blok)
        mena = [MAPA_SITE] + ([strazca.group(1)] if strazca else [])
        guardy = [
            line for line in blok.splitlines()
            if " not in " in line and any(meno in line for meno in mena)
        ]
        assert len(guardy) >= 2, (
            "kontrola musí byť dvakrát: pred úpravou (blok tam vôbec je) aj po "
            "úprave (nezmizol), a až potom sa smie čokoľvek zapísať; našiel som "
            f"{len(guardy)}"
        )
        zapis = min(
            (blok.index(kus) for kus in ("open(tmp", "open(NOVY", "open(novy") if kus in blok),
            default=None,
        )
        assert zapis is not None, "python blok musí zapisovať do dočasného súboru"
        assert blok.rindex(guardy[-1]) < zapis, (
            "obe kontroly site bloku mapa.* musia prebehnúť PRED akýmkoľvek zápisom"
        )


def test_deploy_never_touches_the_other_apps_files(script):
    assert "taktik-mapa" not in script or not re.search(
        r"(rm|mv|scp|cp)[^\n]*taktik-mapa", script
    ), "nasadenie nesmie siahať na súbory druhej appky"


# --------------------------------------------------------------------- 4. venv
def test_venv_is_created_when_missing(script):
    assert "python3 -m venv /opt/uvarsi/venv" in script, (
        "na čerstvom serveri /opt/uvarsi/venv neexistuje; pip line zlyhá a systemd "
        "sa točí v 203/EXEC"
    )


def test_venv_is_created_before_pip_installs_into_it(script):
    venv = script.index("python3 -m venv /opt/uvarsi/venv")
    pip = script.index("/opt/uvarsi/venv/bin/pip")
    assert venv < pip, "venv musí vzniknúť skôr, než doň pip inštaluje"


def test_dependency_step_fails_when_pip_fails(script):
    assert "pip -q install fastapi uvicorn anthropic pillow requests 2>&1 | tail -1" not in script, (
        "`| tail -1` zahodí návratový kód pipu"
    )


# ---------------------------------------------------------------------- 5. cron
def _documented_cron_line() -> str:
    text = DOZORCA.read_text(encoding="utf-8")
    match = re.search(r"^#\s*(\S+\s+\S+\s+\S+\s+\S+\s+\S+\s+/opt/uvarsi/dozorca\.sh.*)$",
                      text, flags=re.M)
    assert match, "hlavička dozorca.sh musí dokumentovať cron riadok"
    return match.group(1).strip()


def test_dozorca_schedule_is_documented_in_the_watcher():
    assert _documented_cron_line().startswith("0 5-21 * * *")


def test_deploy_installs_the_documented_cron_line(script):
    riadok = _documented_cron_line()
    assert riadok in script, (
        "dozorca.sh sa nasadzuje, ale rozvrh ostal iba komentárom — na čerstvom "
        f"serveri sa nikdy nespustí. Očakávam v nasad.ps1 presne:\n  {riadok}"
    )
    assert "crontab" in script


def test_deploy_installs_exactly_one_dozorca_cron_line(script):
    bash = [blok for blok in _heredoc_blocks(script.splitlines()) if "crontab" in blok]
    assert bash, "očakávam bash blok, ktorý inštaluje cron"
    blok = bash[0]
    assert "grep -v" in blok, (
        "opakované nasadenie by inak pridalo druhý rovnaký riadok"
    )
    assert re.search(r"-(eq|ne) 1\b", blok), (
        "nasadenie musí overiť, že v crontabe je práve jeden riadok s dozorcom"
    )


# ------------------------------------------------------------------ 6. uvarsi.env
def test_deploy_verifies_the_env_file(script):
    assert "/opt/uvarsi/uvarsi.env" in script, (
        "bez uvarsi.env zlyhá prihlasovací e-mail aj generovanie plánu"
    )
    for kluc in ("ANTHROPIC_API_KEY", "RESEND_API_KEY"):
        assert kluc in script, f"nasadenie musí overiť prítomnosť {kluc}"


def test_env_check_never_prints_secret_values(script):
    bloky = [blok for blok in _heredoc_blocks(script.splitlines()) if "uvarsi.env" in blok]
    assert bloky, "očakávam bash blok, ktorý overuje uvarsi.env"
    for blok in bloky:
        assert not re.search(r"\bcat\b[^\n]*uvarsi\.env", blok), (
            "nikdy nevypisuj obsah uvarsi.env"
        )
        for line in blok.splitlines():
            if "grep" in line and ("KEY" in line or "$k" in line or "${k}" in line):
                assert re.search(r"grep\s+-[A-Za-z]*q", line), (
                    f"grep nad uvarsi.env musí byť tichý (-q):\n  {line}"
                )


def test_env_file_is_never_uploaded(script):
    assert not re.search(r"(scp|Test-Path)[^\n]*uvarsi\.env", script), (
        "uvarsi.env je tajomstvo servera — nikdy ho nenahrávaj z PC"
    )
    assert 'r = "/opt/uvarsi/uvarsi.env"' not in script


def test_missing_env_file_fails_the_deploy(script):
    bloky = [blok for blok in _heredoc_blocks(script.splitlines()) if "uvarsi.env" in blok]
    for blok in bloky:
        assert re.search(r"exit 1|exit \$", blok), (
            "chýbajúci alebo neúplný uvarsi.env musí nasadenie zhodiť"
        )


# ----------------------------------------------------------- 7. kontrola po nasadení
def _dozorca_threshold() -> str:
    text = DOZORCA.read_text(encoding="utf-8")
    match = re.search(r'"\$\{POCET:-0\}"\s*-lt\s*(\d+)', text)
    assert match, "dozorca.sh musí mať číselný prah počtu akcií"
    return match.group(1)


def test_postdeploy_check_fails_on_any_non_200(script):
    bloky = [blok for blok in _heredoc_blocks(script.splitlines()) if "http_code" in blok]
    assert bloky, "očakávam kontrolný bash blok s curl -w %{http_code}"
    blok = bloky[0]
    for cesta in ("/app", "/api/public/landing", "/api/health"):
        assert cesta in blok, f"kontrola musí testovať {cesta}"
    assert '"200"' in blok or "= 200" in blok, (
        "kontrola musí trvať na kóde 200; 500 a 502 predtým prešli ako úspech"
    )
    assert re.search(r"exit \$", blok), (
        "kontrolný skript musí vrátiť nenulový kód, keď niečo nesedí"
    )


def test_postdeploy_check_uses_the_dozorca_offer_threshold(script):
    prah = _dozorca_threshold()
    bloky = [blok for blok in _heredoc_blocks(script.splitlines()) if "PRAH" in blok]
    assert bloky, "kontrola musí mať prah počtu akcií"
    assert re.search(rf"PRAH=\s*{prah}\b", bloky[0]), (
        f"prah musí byť rovnaký ako v dozorca.sh ({prah}), inak nasadenie prejde "
        "so stránkou, ktorá hlási „obnovujeme“"
    )


def test_postdeploy_result_stops_the_deploy(lines):
    kod = _powershell_lines(lines)
    kontrola = [i for i, (_, line) in enumerate(kod) if "check.sh" in line]
    assert kontrola, "očakávam spustenie kontrolného skriptu"
    okolie = " ".join(line for _, line in kod[kontrola[0]:kontrola[0] + 8])
    assert "Vyzaduj" in okolie or "$LASTEXITCODE" in okolie, (
        "výsledok kontroly sa musí premietnuť do návratového kódu nasadenia"
    )


def test_service_state_is_still_checked(script):
    assert "sluzba" in script and "is-active" in script


# ---------------------------------------------------------------- 8. /api/health
def test_deploy_compares_live_release_id_with_local_version(script):
    assert "/api/health" in script, (
        "/api/health odhalí čiastočne prenesený scp — presne trieda chyby, ktorá "
        "spôsobila výpadok auth_data.py"
    )
    assert re.search(r"Get-Content[^\n]*VERSION", script), (
        "nasadenie musí načítať lokálny VERSION a porovnať ho so živým vydaním"
    )
    bloky = [blok for blok in _heredoc_blocks(script.splitlines()) if "vydanie" in blok]
    assert bloky, "kontrolný skript musí čítať pole `vydanie` z /api/health"
    assert any("OCAKAVANE" in blok or "ocakavane" in blok for blok in bloky), (
        "živé vydanie sa musí porovnať s očakávaným, nie len vypísať"
    )
