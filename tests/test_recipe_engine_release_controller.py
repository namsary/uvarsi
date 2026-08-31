import os
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
BASH = Path("C:/Program Files/Git/bin/bash.exe")
CONTROLLER = ROOT / "hetzner" / "recipe-engine-rollout.sh"


def bash_path(path: Path) -> str:
    return "/c" + path.resolve().as_posix()[2:]


def executable(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8", newline="\n")
    path.chmod(0o755)


@pytest.fixture
def rollout(tmp_path):
    if not BASH.exists():
        pytest.skip("Git Bash is required for the rollout controller behavior tests")

    live = tmp_path / "uvarsi"
    app = live / "app"
    recipes = app / "catalog" / "recipes"
    recipes.mkdir(parents=True)
    (live / "VERSION").write_text("test-release\n", encoding="utf-8")
    for name in (
        "config.py",
        "server.py",
        "deterministic_plan.py",
        "ingredient_catalog.py",
        "library_gate.py",
        "quantity_math.py",
        "recipe_catalog.py",
        "recipe_matcher.py",
        "recipe_renderer.py",
    ):
        (app / name).write_text("# present\n", encoding="utf-8")
    (app / "catalog" / "ingredients.json").write_text("{}\n", encoding="utf-8")
    (recipes / "manifest.json").write_text("{}\n", encoding="utf-8")
    (recipes / "recipe.json").write_text("{}\n", encoding="utf-8")

    target = live / "recipe-engine.target"
    target.write_text("on\n", encoding="utf-8")
    flag = live / "uvarsi-recipe-engine.env"
    flag.write_text("UVARSI_RECIPE_ENGINE=off\n", encoding="utf-8")
    state = tmp_path / "state"
    state.mkdir()
    calls = state / "calls.log"
    alerts = state / "alerts.log"

    fake_python = tmp_path / "python"
    executable(
        fake_python,
        "#!/bin/sh\n"
        "printf 'python %s\\n' \"$*\" >> \"$UVARSI_TEST_CALLS\"\n"
        "case \"$*\" in\n"
        "  *app.library_gate*) [ ! -f \"$UVARSI_TEST_STATE/fail-library\" ] ;;\n"
        "  *run_recipe_engine_shadow*) [ ! -f \"$UVARSI_TEST_STATE/fail-shadow\" ] ;;\n"
        "  *--recipe-engine-smoke*) [ ! -f \"$UVARSI_TEST_STATE/fail-smoke\" ] ;;\n"
        "  *platby_su_zapnute*) [ ! -f \"$UVARSI_TEST_STATE/payments-on\" ] ;;\n"
        "  *) exit 0 ;;\n"
        "esac\n",
    )
    fake_systemctl = tmp_path / "systemctl"
    executable(
        fake_systemctl,
        "#!/bin/sh\n"
        "printf 'systemctl %s\\n' \"$*\" >> \"$UVARSI_TEST_CALLS\"\n"
        "case \"$*\" in\n"
        "  *taktik*|*caddy*|*cron*) exit 88 ;;\n"
        "  *) exit 0 ;;\n"
        "esac\n",
    )
    fake_curl = tmp_path / "curl"
    executable(
        fake_curl,
        "#!/bin/sh\n"
        "case \"$*\" in\n"
        "  *notify.invalid*) printf 'alert\\n' >> \"$UVARSI_TEST_ALERTS\"; exit 0 ;;\n"
        "esac\n"
        "[ ! -f \"$UVARSI_TEST_STATE/malformed-health\" ] || { printf '{'; exit 0; }\n"
        "mode=$(sed -n 's/^[[:space:]]*\\(export[[:space:]][[:space:]]*\\)\\?UVARSI_RECIPE_ENGINE[[:space:]]*=[[:space:]]*//p' \"$UVARSI_RECIPE_FLAG_FILE\" | head -1)\n"
        "printf '{\"recipe_engine\":{\"mode\":\"%s\",\"ready\":true,\"blockers\":[],\"last_shadow\":{\"complete\":true,\"eligible\":true,\"success_rate\":0.99,\"p95_ms\":120,\"dietary_violations\":0,\"negative_quantities\":0,\"invalid_package_counts\":0}}}' \"$mode\"\n",
    )

    env = os.environ | {
        "UVARSI_DIR": bash_path(live),
        "UVARSI_PY": bash_path(fake_python),
        "UVARSI_HEALTH_PY": bash_path(Path(sys.executable)),
        "UVARSI_CURL": bash_path(fake_curl),
        "UVARSI_SYSTEMCTL": bash_path(fake_systemctl),
        "UVARSI_RECIPE_TARGET": bash_path(target),
        "UVARSI_RECIPE_FLAG_FILE": bash_path(flag),
        "UVARSI_RECIPE_SMOKE_STATE": bash_path(state / "smoke.json"),
        "UVARSI_RECIPE_ROLLOUT_LOCK": bash_path(state / "rollout.lock"),
        "UVARSI_ROLLOUT_LOCKED": "1",
        "UVARSI_NOTIFY_URL": "https://notify.invalid/uvarsi",
        "UVARSI_TEST_STATE": bash_path(state),
        "UVARSI_TEST_CALLS": bash_path(calls),
        "UVARSI_TEST_ALERTS": bash_path(alerts),
    }
    return {
        "live": live,
        "app": app,
        "target": target,
        "flag": flag,
        "state": state,
        "calls": calls,
        "alerts": alerts,
        "env": env,
    }


def run_controller(rollout):
    return subprocess.run(
        [str(BASH), bash_path(CONTROLLER)],
        cwd=ROOT,
        env=rollout["env"],
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
    )


def test_successfully_activates_both_stages_without_touching_unrelated_services(rollout):
    result = run_controller(rollout)

    assert result.returncode == 0, result.stdout + result.stderr
    assert rollout["flag"].read_text(encoding="utf-8") == "UVARSI_RECIPE_ENGINE=on\n"
    calls = rollout["calls"].read_text(encoding="utf-8")
    assert "run_recipe_engine_shadow" in calls
    assert "--recipe-engine-smoke" in calls
    assert "taktik" not in calls and "caddy" not in calls and "cron" not in calls
    assert set(
        line.removeprefix("systemctl ").split()[-1]
        for line in calls.splitlines()
        if line.startswith("systemctl ") and line.split()[-1].startswith("uvarsi")
    ) <= {"uvarsi", "uvarsi-plan-worker"}


def test_existing_export_syntax_is_accepted_and_canonicalized(rollout):
    rollout["flag"].write_text("export UVARSI_RECIPE_ENGINE=off\n", encoding="utf-8")

    result = run_controller(rollout)

    assert result.returncode == 0, result.stdout + result.stderr
    assert rollout["flag"].read_text(encoding="utf-8") == "UVARSI_RECIPE_ENGINE=on\n"


@pytest.mark.parametrize("failure", ["fail-shadow", "fail-smoke", "malformed-health", "payments-on"])
def test_any_gate_failure_rolls_back_to_off_and_emits_exactly_one_alert(rollout, failure):
    rollout["state"].joinpath(failure).write_text("1", encoding="ascii")

    result = run_controller(rollout)

    assert result.returncode != 0
    assert rollout["flag"].read_text(encoding="utf-8") == "UVARSI_RECIPE_ENGINE=off\n"
    assert rollout["alerts"].read_text(encoding="utf-8").splitlines() == ["alert"]


@pytest.mark.parametrize(
    "missing",
    ["server.py", "catalog/ingredients.json", "catalog/recipes/manifest.json"],
)
def test_incomplete_package_fails_closed_before_shadow(rollout, missing):
    rollout["app"].joinpath(missing).unlink()

    result = run_controller(rollout)

    assert result.returncode != 0
    assert "run_recipe_engine_shadow" not in rollout["calls"].read_text(encoding="utf-8")
    assert rollout["flag"].read_text(encoding="utf-8") == "UVARSI_RECIPE_ENGINE=off\n"


def test_library_gate_failure_never_reaches_shadow(rollout):
    rollout["state"].joinpath("fail-library").write_text("1", encoding="ascii")

    result = run_controller(rollout)

    assert result.returncode != 0
    calls = rollout["calls"].read_text(encoding="utf-8")
    assert "app.library_gate" in calls
    assert "run_recipe_engine_shadow" not in calls


def test_invalid_target_is_data_not_shell_and_cannot_execute_commands(rollout):
    marker = rollout["state"] / "injected"
    rollout["target"].write_text(
        f"on\n$(touch {bash_path(marker)})\n", encoding="utf-8"
    )

    result = run_controller(rollout)

    assert result.returncode != 0
    assert not marker.exists()
    assert rollout["flag"].read_text(encoding="utf-8") == "UVARSI_RECIPE_ENGINE=off\n"


def test_healthy_on_rerun_is_idempotent_and_does_not_restart(rollout):
    rollout["flag"].write_text("UVARSI_RECIPE_ENGINE=on\n", encoding="utf-8")

    result = run_controller(rollout)

    assert result.returncode == 0, result.stdout + result.stderr
    calls = rollout["calls"].read_text(encoding="utf-8")
    assert "systemctl" not in calls
    assert "run_recipe_engine_shadow" not in calls
    assert "--recipe-engine-smoke" not in calls
