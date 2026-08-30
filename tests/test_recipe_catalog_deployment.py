"""Recipe-engine catalog assets must travel atomically with runtime code."""

import os
import re
import shlex
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
MANUAL_DEPLOY = ROOT / "nasad.ps1"
SAMOPULL = ROOT / "hetzner" / "samopull.sh"
DEPLOY_STATE = ROOT / "hetzner" / "uvarsi-deploy-state.sh"
SWITCH_BOUNDARY = "# --- 3. záloha aktuálneho stavu a prepnutie ---"


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
        pytest.skip("Bash is unavailable; shell deployment behavior tests require Bash")
    return executable


def _powershell() -> str:
    executable = shutil.which("pwsh") or shutil.which("powershell.exe")
    if executable is None:
        pytest.skip("PowerShell is required for the manual deployment behavior test")
    return executable


def _prepare_manual_source(tmp_path: Path) -> Path:
    source = MANUAL_DEPLOY.read_text(encoding="utf-8")
    release = tmp_path / "manual source with spaces"
    release.mkdir()
    shutil.copyfile(MANUAL_DEPLOY, release / "nasad.ps1")

    for relative in re.findall(r'l\s*=\s*"\$B\\([^"]+)"', source):
        target = release / relative.replace("\\", "/")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("present\n", encoding="utf-8")

    static = release / "app" / "static" / "app.html"
    static.parent.mkdir(parents=True, exist_ok=True)
    static.write_text("present\n", encoding="utf-8")

    catalog = release / "app" / "catalog"
    recipes = catalog / "recipes"
    recipes.mkdir(parents=True)
    (catalog / "ingredients.json").write_text("{}\n", encoding="utf-8")
    (recipes / "manifest.json").write_text("{}\n", encoding="utf-8")
    (recipes / "smoke.json").write_text("{}\n", encoding="utf-8")
    for relative in (
        "candidates/draft.json",
        "development/root-draft.json",
        "recipes/candidates/nested-draft.json",
        "recipes/development/nested-draft.json",
    ):
        development_asset = catalog / relative
        development_asset.parent.mkdir(parents=True, exist_ok=True)
        development_asset.write_text("{}\n", encoding="utf-8")
    return release


def _run_manual_deploy_offline(
    tmp_path: Path,
    invalid_catalog_asset: tuple[str, str] | None = None,
    unsafe_recipe_name: str | None = None,
) -> tuple[subprocess.CompletedProcess, str]:
    release = _prepare_manual_source(tmp_path)
    if unsafe_recipe_name is not None:
        (release / "app/catalog/recipes" / unsafe_recipe_name).write_text(
            "{}\n", encoding="utf-8"
        )
    if invalid_catalog_asset is not None:
        asset, invalid_kind = invalid_catalog_asset
        target = {
            "ingredients": release / "app/catalog/ingredients.json",
            "manifest": release / "app/catalog/recipes/manifest.json",
            "recipe": release / "app/catalog/recipes/smoke.json",
        }[asset]
        target.unlink()
        if invalid_kind == "empty":
            target.write_bytes(b"")
        elif invalid_kind == "directory":
            target.mkdir()
            (target / "not-a-file").write_text("present\n", encoding="utf-8")
        else:
            raise AssertionError(f"unknown invalid kind: {invalid_kind}")
    calls = tmp_path / "remote-calls.txt"
    harness = tmp_path / "manual-deploy-harness.ps1"
    harness.write_text(
        "param([string]$Deploy, [string]$Calls)\n"
        "function global:ssh {\n"
        "  begin { $body = @(); $rest = @($args) }\n"
        "  process { if ($null -ne $_) { $body += $_ } }\n"
        "  end {\n"
        "    Add-Content -LiteralPath $Calls -Value (\"SSH \" + ($Rest -join ' '))\n"
        "    if ($body.Count) { Add-Content -LiteralPath $Calls -Value ($body -join \"`n\") }\n"
        "    $global:LASTEXITCODE = 0\n"
        "  }\n"
        "}\n"
        "function global:scp {\n"
        "  param([switch]$q, [switch]$r, "
        "[Parameter(ValueFromRemainingArguments=$true)][object[]]$Rest)\n"
        "  $switches = @(); if ($q) { $switches += '-q' }; if ($r) { $switches += '-r' }\n"
        "  Add-Content -LiteralPath $Calls -Value (\"SCP \" + (($switches + $Rest) -join ' '))\n"
        "  $global:LASTEXITCODE = 0\n"
        "}\n"
        ". $Deploy\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        [
            _powershell(),
            "-NoProfile",
            "-NonInteractive",
            "-File",
            str(harness),
            "-Deploy",
            str(release / "nasad.ps1"),
            "-Calls",
            str(calls),
        ],
        cwd=release,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
    )
    return result, calls.read_text(encoding="utf-8") if calls.exists() else ""


def test_manual_deploy_stages_catalog_before_switch_without_candidates_or_live_copy(tmp_path):
    """A recursive/live catalog copy would leak drafts or split code from data."""
    result, raw_calls = _run_manual_deploy_offline(tmp_path)

    assert result.returncode == 0, result.stdout + result.stderr
    calls = raw_calls.replace("\\", "/")
    staged = (
        "jarvis:/opt/uvarsi/releases/manual-stage/app/catalog/ingredients.json",
        "jarvis:/opt/uvarsi/releases/manual-stage/app/catalog/recipes/manifest.json",
        "jarvis:/opt/uvarsi/releases/manual-stage/app/catalog/recipes/smoke.json",
    )
    for target in staged:
        assert target in calls
        assert calls.index(target) < calls.index(
            "uvarsi_snapshot /opt/uvarsi/releases/manual-predosle"
        )
    assert "/app/catalog/candidates" not in calls
    assert "draft.json" not in calls
    assert "root-draft.json" not in calls
    assert "nested-draft.json" not in calls
    assert "jarvis:/opt/uvarsi/app/catalog/" not in calls


@pytest.mark.parametrize("asset", ["ingredients", "manifest", "recipe"])
@pytest.mark.parametrize("invalid_kind", ["empty", "directory"])
def test_manual_deploy_rejects_invalid_catalog_assets_before_snapshot(
        tmp_path, asset, invalid_kind):
    result, calls = _run_manual_deploy_offline(
        tmp_path, invalid_catalog_asset=(asset, invalid_kind)
    )

    assert "NASADENIE ZLYHALO" in result.stdout, result.stdout + result.stderr
    assert "uvarsi_snapshot /opt/uvarsi/releases/manual-predosle" not in calls


@pytest.mark.parametrize("unsafe_name", ["week night.json", "week;night.json"])
def test_manual_deploy_rejects_unsafe_recipe_filename_before_transfer_or_snapshot(
    tmp_path, unsafe_name
):
    result, calls = _run_manual_deploy_offline(
        tmp_path, unsafe_recipe_name=unsafe_name
    )

    assert "NASADENIE ZLYHALO" in result.stdout, result.stdout + result.stderr
    assert unsafe_name not in calls
    assert "uvarsi_snapshot /opt/uvarsi/releases/manual-predosle" not in calls


def _samopull_catalog_gate() -> str:
    script = SAMOPULL.read_text(encoding="utf-8")
    return script.split("# b) povinné súbory", 1)[1].split(SWITCH_BOUNDARY, 1)[0]


def _required_release_files(gate: str) -> list[str]:
    match = re.search(r"for f in ([^;\n]+); do", gate)
    assert match, "samopull required-files gate was not found"
    return shlex.split(match.group(1))


def _run_samopull_catalog_gate(
    tmp_path: Path,
    bash_executable: str,
    *,
    missing: str | None = None,
    candidate_only: bool = False,
    invalid_catalog_asset: tuple[str, str] | None = None,
    unsafe_recipe_name: str | None = None,
) -> tuple[subprocess.CompletedProcess, Path]:
    gate = _samopull_catalog_gate()
    release = tmp_path / "release with spaces"
    for relative in _required_release_files(gate):
        target = release / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("present\n", encoding="utf-8")

    ingredients = release / "app/catalog/ingredients.json"
    manifest = release / "app/catalog/recipes/manifest.json"
    recipe = release / "app/catalog/recipes/smoke.json"
    for target in (ingredients, manifest):
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("{}\n", encoding="utf-8")
    if not candidate_only:
        recipe.write_text("{}\n", encoding="utf-8")
    else:
        candidate = release / "app/catalog/candidates/draft.json"
        candidate.parent.mkdir(parents=True, exist_ok=True)
        candidate.write_text("{}\n", encoding="utf-8")
    if unsafe_recipe_name is not None:
        (recipe.parent / unsafe_recipe_name).write_text("{}\n", encoding="utf-8")

    missing_paths = {
        "ingredients": ingredients,
        "manifest": manifest,
        "recipe": recipe,
    }
    if missing is not None and missing_paths[missing].exists():
        missing_paths[missing].unlink()
    if invalid_catalog_asset is not None:
        asset, invalid_kind = invalid_catalog_asset
        target = missing_paths[asset]
        if target.exists():
            target.unlink()
        if invalid_kind == "empty":
            target.write_bytes(b"")
        elif invalid_kind == "directory":
            target.mkdir()
            (target / "not-a-file").write_text("present\n", encoding="utf-8")
        else:
            raise AssertionError(f"unknown invalid kind: {invalid_kind}")

    mutation_marker = tmp_path / "live-mutation-reached"
    command = (
        'PATH=/usr/bin:/bin:"$PATH"\n'
        "log() { :; }\n"
        "notify() { :; }\n"
        'CIEL="release with spaces"\n'
        f"{gate}\n"
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
    return result, mutation_marker


def test_samopull_catalog_gate_accepts_one_top_level_recipe_json(
        tmp_path, bash_executable):
    result, mutation_marker = _run_samopull_catalog_gate(tmp_path, bash_executable)

    assert result.returncode == 0, result.stdout + result.stderr
    assert mutation_marker.exists()


@pytest.mark.parametrize("missing", ["ingredients", "manifest", "recipe"])
def test_samopull_catalog_gate_rejects_incomplete_release_before_switch(
        tmp_path, bash_executable, missing):
    """Deleting any runtime catalog component must close the pre-switch gate."""
    result, mutation_marker = _run_samopull_catalog_gate(
        tmp_path, bash_executable, missing=missing
    )

    assert result.returncode != 0
    assert not mutation_marker.exists()


def test_samopull_does_not_count_candidate_json_as_a_runtime_recipe(
        tmp_path, bash_executable):
    result, mutation_marker = _run_samopull_catalog_gate(
        tmp_path, bash_executable, candidate_only=True
    )

    assert result.returncode != 0
    assert not mutation_marker.exists()


@pytest.mark.parametrize("asset", ["ingredients", "manifest", "recipe"])
@pytest.mark.parametrize("invalid_kind", ["empty", "directory"])
def test_samopull_catalog_gate_rejects_invalid_file_before_switch(
        tmp_path, bash_executable, asset, invalid_kind):
    result, mutation_marker = _run_samopull_catalog_gate(
        tmp_path,
        bash_executable,
        invalid_catalog_asset=(asset, invalid_kind),
    )

    assert result.returncode != 0
    assert not mutation_marker.exists()


@pytest.mark.parametrize("unsafe_name", ["week night.json", "week;night.json"])
def test_samopull_gate_rejects_unsafe_recipe_filename_before_switch(
    tmp_path, bash_executable, unsafe_name
):
    result, mutation_marker = _run_samopull_catalog_gate(
        tmp_path,
        bash_executable,
        unsafe_recipe_name=unsafe_name,
    )

    assert result.returncode != 0
    assert not mutation_marker.exists()


def _run_samopull_staging(
    tmp_path: Path,
    bash_executable: str,
    *,
    unsafe_recipe_name: str | None = None,
) -> tuple[subprocess.CompletedProcess, Path]:
    source = tmp_path / "source"
    catalog = source / "app/catalog"
    ingredient = catalog / "ingredients.json"
    ingredient.parent.mkdir(parents=True, exist_ok=True)
    ingredient.write_text("{}\n", encoding="utf-8")
    recipes = catalog / "recipes"
    recipes.mkdir()
    (recipes / "manifest.json").write_text("{}\n", encoding="utf-8")
    (recipes / "smoke.json").write_text("{}\n", encoding="utf-8")
    if unsafe_recipe_name is not None:
        (recipes / unsafe_recipe_name).write_text("{}\n", encoding="utf-8")
    for relative in (
        "candidates/draft.json",
        "development/root-draft.json",
        "recipes/candidates/nested-draft.json",
        "recipes/development/nested-draft.json",
    ):
        development_asset = catalog / relative
        development_asset.parent.mkdir(parents=True, exist_ok=True)
        development_asset.write_text("{}\n", encoding="utf-8")

    script = SAMOPULL.read_text(encoding="utf-8")
    staging = script[script.index('CIEL="$REL/${SHA:0:12}"'):].split(
        "# --- 2. overenie PRED prepnutím ---", 1
    )[0]
    releases = tmp_path / "releases"
    command = (
        'PATH=/usr/bin:/bin:"$PATH"\n'
        "log() { :; }\n"
        'ZDROJ="source"\n'
        'REL="releases"\n'
        'TMP="tmp"\n'
        'mkdir -p "$TMP"\n'
        'SHA="1234567890abcdef"\n'
        f"{staging}\n"
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
    return result, releases / "1234567890ab" / "app/catalog"


def test_samopull_staging_allowlists_only_runtime_catalog_assets(
        tmp_path, bash_executable):
    """Preparing the release must never stage candidate drafts."""
    result, staged = _run_samopull_staging(tmp_path, bash_executable)
    assert result.returncode == 0, result.stdout + result.stderr
    staged_files = sorted(
        path.relative_to(staged).as_posix()
        for path in staged.rglob("*")
        if path.is_file()
    )
    assert staged_files == [
        "ingredients.json",
        "recipes/manifest.json",
        "recipes/smoke.json",
    ]


@pytest.mark.parametrize("unsafe_name", ["week night.json", "week;night.json"])
def test_samopull_staging_rejects_unsafe_recipe_filename_before_copy(
    tmp_path, bash_executable, unsafe_name
):
    result, staged = _run_samopull_staging(
        tmp_path,
        bash_executable,
        unsafe_recipe_name=unsafe_name,
    )

    assert result.returncode != 0
    assert not (staged / "recipes" / unsafe_name).exists()


def test_directory_rollback_restores_code_and_catalog_together(
        tmp_path, bash_executable):
    """Directory rollback must not combine old code with a new catalog."""
    live = tmp_path / "live"
    live_catalog = live / "app/catalog"
    live_catalog.mkdir(parents=True)
    (live / "app/code.txt").write_text("new-code\n", encoding="utf-8")
    (live_catalog / "ingredients.json").write_text("new-catalog\n", encoding="utf-8")

    snapshot = tmp_path / "snapshot"
    snapshot_catalog = snapshot / "app/catalog"
    snapshot_catalog.mkdir(parents=True)
    (snapshot / "app/code.txt").write_text("old-code\n", encoding="utf-8")
    (snapshot_catalog / "ingredients.json").write_text("old-catalog\n", encoding="utf-8")

    library = tmp_path / "deploy-state.sh"
    shutil.copyfile(DEPLOY_STATE, library)
    result = subprocess.run(
        [
            bash_executable,
            "-c",
            'PATH=/usr/bin:/bin:"$PATH"\n. "./deploy-state.sh"\n'
            '_uvarsi_restore_app "snapshot"',
        ],
        cwd=tmp_path,
        env=os.environ
        | {
            "UVARSI_DIR": "live",
            "UVARSI_CP": "cp",
            "UVARSI_MV": "mv",
        },
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert (live / "app/code.txt").read_text(encoding="utf-8") == "old-code\n"
    assert (live_catalog / "ingredients.json").read_text(encoding="utf-8") == "old-catalog\n"
