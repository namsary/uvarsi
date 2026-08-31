"""Deploy musí preniesť KAŽDÝ runtime modul, nielen tie zo zoznamu.

Regresia 20. 8. 2026: `auth_data.py` chýbal v manifeste, deploy prešiel „zeleno“
a služba spadla pri importe (ModuleNotFoundError: No module named 'auth_data').
Ručne udržiavaný zoznam súborov je krehký — tento test enumeruje adresár, takže
každý nový modul musí byť v deploy manifeste, inak testy zčervenajú.
"""
import os
from pathlib import Path
import shutil
import subprocess

import pytest


APP_DIR = Path("app")
DEPLOY = Path("nasad.ps1")
SAMOPULL = Path("hetzner/samopull.sh")

# moduly, ktoré sa zámerne nenasadzujú (nie sú súčasťou runtime)
NEDEPLOYOVANE: set[str] = {"recipe_candidates.py"}


def _runtime_modules() -> list[str]:
    return sorted(
        path.name
        for path in APP_DIR.glob("*.py")
        if not path.name.startswith("_") and path.name not in NEDEPLOYOVANE
    )


def test_there_are_runtime_modules_to_check():
    """Poistka proti tichému prázdnemu testu, keby sa zmenila štruktúra."""
    assert _runtime_modules(), "očakávam aspoň jeden modul v app/"


def test_every_app_module_is_in_deploy_manifest():
    script = DEPLOY.read_text(encoding="utf-8")
    chybajuce = [
        name for name in _runtime_modules()
        if f'"$B\\app\\{name}"' not in script
    ]
    assert not chybajuce, (
        "tieto moduly sa nenasadzujú a služba na nich spadne pri importe: "
        + ", ".join(chybajuce)
    )


def test_server_imports_are_all_deployed():
    """Čo server.py importuje lokálne, to musí byť na serveri."""
    server = (APP_DIR / "server.py").read_text(encoding="utf-8")
    script = DEPLOY.read_text(encoding="utf-8")
    lokalne = {
        path.stem for path in APP_DIR.glob("*.py")
    }
    chybajuce = []
    for modul in sorted(lokalne):
        importovany = (
            f"from {modul} import" in server or f"import {modul}\n" in server
        )
        if importovany and f'"$B\\app\\{modul}.py"' not in script:
            chybajuce.append(f"{modul}.py")
    assert not chybajuce, (
        "server.py ich importuje, ale deploy ich neprenáša: " + ", ".join(chybajuce)
    )


def test_offline_candidate_workflow_is_not_a_runtime_dependency():
    """Karanténa smie zostať mimo manuálneho deployu iba kým ju runtime neimportuje."""
    server = (APP_DIR / "server.py").read_text(encoding="utf-8")
    worker = (APP_DIR / "plan_worker.py").read_text(encoding="utf-8")

    assert "recipe_candidates" not in server
    assert "recipe_candidates" not in worker


def test_samopull_preflight_rejects_incomplete_release_without_public_pages():
    script = SAMOPULL.read_text(encoding="utf-8")
    assert "app/public_pages.py" in script, (
        "samopull kopíruje celé app/, ale pred prepnutím musí odmietnuť vydanie "
        "bez public_pages.py"
    )


def _discover_bash() -> str | None:
    for name in ("bash", "bash.exe"):
        executable = shutil.which(name)
        if executable:
            return executable
    if os.name == "nt":
        git = shutil.which("git")
        if git:
            git_root = Path(git).resolve().parent.parent
            for candidate in (git_root / "bin/bash.exe", git_root / "usr/bin/bash.exe"):
                if candidate.is_file():
                    return str(candidate)
    return None


@pytest.fixture(scope="module")
def bash_executable() -> str:
    executable = _discover_bash()
    if executable is None:
        pytest.skip("Bash is unavailable; samopull behavior tests require Bash")
    return executable


def test_samopull_missing_plan_calendar_aborts_before_live_mutation(
        tmp_path, bash_executable):
    """Removing plan_calendar.py must stop the real required-files gate."""
    release = tmp_path / "release with spaces"
    required = (
        "app/server.py",
        "app/auth_data.py",
        "app/public_pages.py",
        "app/plan_jobs.py",
        "app/plan_calendar.py",
        "app/plan_shortlist.py",
        "app/plan_worker.py",
        "app/predpocet.py",
        "app/static/app.html",
        "app/catalog/ingredients.json",
        "app/catalog/recipes/manifest.json",
        "app/catalog/recipes/smoke.json",
        "hetzner/uvarsi-plan-worker.service",
        "hetzner/uvarsi-deploy-state.sh",
        "VERSION",
        "index.html",
        "sw.js",
    )
    for relative in required:
        if relative == "app/plan_calendar.py":
            continue
        target = release / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("present\n", encoding="utf-8")

    script = SAMOPULL.read_text(encoding="utf-8")
    required_files_gate = script.split("# b) povinné súbory", 1)[1].split(
        "# --- 3. záloha aktuálneho stavu a prepnutie ---", 1
    )[0]
    mutation_marker = tmp_path / "live-mutation-reached"
    command = (
        "log() { :; }\n"
        "notify() { :; }\n"
        'CIEL="release with spaces"\n'
        f"{required_files_gate}\n"
        'printf reached > "live-mutation-reached"\n'
    )

    result = subprocess.run(
        [bash_executable, "-c", command],
        cwd=tmp_path,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert not mutation_marker.exists()
