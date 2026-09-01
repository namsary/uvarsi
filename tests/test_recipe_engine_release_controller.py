import json
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
    (app / "catalog" / "slovak_ingredient_forms.json").write_text(
        "{}\n", encoding="utf-8"
    )
    (recipes / "manifest.json").write_text("{}\n", encoding="utf-8")
    (recipes / "recipe.json").write_text("{}\n", encoding="utf-8")

    target = live / "recipe-engine.target"
    target.write_text("on\n", encoding="utf-8")
    flag = live / "uvarsi-recipe-engine.env"
    flag.write_text("UVARSI_RECIPE_ENGINE=off\n", encoding="utf-8", newline="\n")
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
        "  *--recipe-engine-smoke*)\n"
        "    [ \"${UVARSI_VERSION_FILE:-}\" = \"$UVARSI_DIR/VERSION\" ] || exit 67\n"
        "    [ \"${UVARSI_DB:-}\" = \"$UVARSI_DIR/uvarsi.db\" ] || exit 68\n"
        "    [ ! -f \"$UVARSI_TEST_STATE/smoke-output.json\" ] || "
        "/usr/bin/cp \"$UVARSI_TEST_STATE/smoke-output.json\" \"$UVARSI_RECIPE_SMOKE_STATE\"\n"
        "    [ ! -f \"$UVARSI_TEST_STATE/fail-smoke\" ] ;;\n"
        "  *platby_su_zapnute*)\n"
        "    [ \"${UVARSI_URL:-}\" = 'https://uvar.si' ] || exit 66\n"
        "    [ ! -f \"$UVARSI_TEST_STATE/payments-on\" ] || exit 1\n"
        "    case \"${PLATBY_ZAPNUTE:-}\" in 1|true|TRUE|ano|áno|yes|on) exit 1 ;; esac\n"
        "    case \"${UVARSI_PAYMENTS_ENABLED:-}\" in 1|true|TRUE|ano|áno|yes|on) exit 1 ;; esac\n"
        "    exit 0 ;;\n"
        "  *) exit 0 ;;\n"
        "esac\n",
    )
    fake_systemctl = tmp_path / "systemctl"
    executable(
        fake_systemctl,
        "#!/bin/sh\n"
        "printf 'systemctl %s\\n' \"$*\" >> \"$UVARSI_TEST_CALLS\"\n"
        "cmd=$1; shift\n"
        "[ \"${1:-}\" != --quiet ] || shift\n"
        "service=${1:-}\n"
        "IFS= read -r mode_line < \"$UVARSI_RECIPE_FLAG_FILE\" || mode_line=\n"
        "mode=${mode_line#export }\n"
        "mode=${mode#UVARSI_RECIPE_ENGINE=}\n"
        "case \"$*\" in\n"
        "  *taktik*|*caddy*|*cron*) exit 88 ;;\n"
        "esac\n"
        "if [ \"$mode\" = off ] && [ -f \"$UVARSI_TEST_STATE/fail-rollback-$cmd-$service\" ]; then exit 73; fi\n"
        "exit 0\n",
    )
    fake_mv = tmp_path / "mv"
    executable(
        fake_mv,
        "#!/bin/sh\n"
        "source_path=\n"
        "for arg in \"$@\"; do\n"
        "  case \"$arg\" in -*) ;; *) source_path=$arg; break ;; esac\n"
        "done\n"
        "IFS= read -r source_line < \"$source_path\" || source_line=\n"
        "if [ -f \"$UVARSI_TEST_STATE/fail-off-write\" ] && "
        "[ \"$source_line\" = 'UVARSI_RECIPE_ENGINE=off' ]; then exit 74; fi\n"
        "exec /usr/bin/mv \"$@\"\n",
    )
    fake_curl = tmp_path / "curl"
    executable(
        fake_curl,
        "#!/bin/sh\n"
        "case \"$*\" in\n"
        "  *notify.invalid*) printf '%s\\n' \"$*\" >> \"$UVARSI_TEST_ALERTS\"; exit 0 ;;\n"
        "esac\n"
        "IFS= read -r mode_line < \"$UVARSI_RECIPE_FLAG_FILE\" || mode_line=\n"
        "mode=${mode_line#export }\n"
        "mode=${mode#UVARSI_RECIPE_ENGINE=}\n"
        "if [ \"$mode\" = on ] && [ -f \"$UVARSI_TEST_STATE/transient-health\" ]; then\n"
        "  attempts_file=\"$UVARSI_TEST_STATE/health-attempts\"\n"
        "  attempts=0; [ ! -f \"$attempts_file\" ] || IFS= read -r attempts < \"$attempts_file\"\n"
        "  attempts=$((attempts + 1)); printf '%s\\n' \"$attempts\" > \"$attempts_file\"\n"
        "  [ \"$attempts\" -gt 1 ] || exit 7\n"
        "fi\n"
        "[ ! -f \"$UVARSI_TEST_STATE/health.json\" ] || { cat \"$UVARSI_TEST_STATE/health.json\"; exit 0; }\n"
        "[ ! -f \"$UVARSI_TEST_STATE/malformed-health\" ] || { printf '{'; exit 0; }\n"
        "printf '{\"recipe_engine\":{\"mode\":\"%s\",\"ready\":true,\"blockers\":[],\"last_shadow\":{\"complete\":true,\"eligible\":true,\"success_rate\":0.99,\"p95_ms\":120,\"dietary_violations\":0,\"negative_quantities\":0,\"invalid_package_counts\":0}}}' \"$mode\"\n",
    )
    fake_sleep = tmp_path / "sleep"
    executable(
        fake_sleep,
        "#!/bin/sh\n"
        "printf 'sleep %s\\n' \"$*\" >> \"$UVARSI_TEST_CALLS\"\n",
    )

    env = os.environ | {
        "UVARSI_DIR": bash_path(live),
        "UVARSI_PY": bash_path(fake_python),
        "UVARSI_HEALTH_PY": bash_path(Path(sys.executable)),
        "UVARSI_CURL": bash_path(fake_curl),
        "UVARSI_SLEEP": bash_path(fake_sleep),
        "UVARSI_SYSTEMCTL": bash_path(fake_systemctl),
        "UVARSI_MV": bash_path(fake_mv),
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


def test_controller_source_does_not_publish_an_operational_notification_topic():
    source = CONTROLLER.read_text(encoding="utf-8")

    assert "ntfy.sh/" not in source
    assert 'NOTIFY_URL="${UVARSI_NOTIFY_URL:-}"' in source


def test_controller_supplies_public_url_to_cron_python_gates():
    source = CONTROLLER.read_text(encoding="utf-8")

    assert 'UVARSI_URL="${UVARSI_URL:-https://uvar.si}"' in source
    assert "export UVARSI_URL" in source


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
        if line.startswith("systemctl ")
    ) == {"uvarsi", "uvarsi-plan-worker"}


def test_transient_on_health_race_is_retried_before_rollback(rollout):
    rollout["state"].joinpath("transient-health").write_text("1", encoding="ascii")

    result = run_controller(rollout)

    assert result.returncode == 0, result.stdout + result.stderr
    assert rollout["flag"].read_text(encoding="utf-8") == "UVARSI_RECIPE_ENGINE=on\n"
    assert rollout["state"].joinpath("health-attempts").read_text(
        encoding="ascii"
    ).strip() == "2"
    assert "sleep 1" in rollout["calls"].read_text(encoding="utf-8")
    assert "Traceback" not in result.stderr
    assert not rollout["alerts"].exists()


def test_existing_export_syntax_is_accepted_and_canonicalized(rollout):
    rollout["flag"].write_text(
        "export UVARSI_RECIPE_ENGINE=off\n", encoding="utf-8", newline="\n"
    )

    result = run_controller(rollout)

    assert result.returncode == 0, result.stdout + result.stderr
    assert rollout["flag"].read_text(encoding="utf-8") == "UVARSI_RECIPE_ENGINE=on\n"


@pytest.mark.parametrize(
    ("failure", "gate"),
    [
        ("fail-shadow", "shadow_matrix"),
        ("fail-smoke", "on_smoke"),
        ("malformed-health", "shadow_health"),
        ("payments-on", "payments_off"),
    ],
)
def test_any_gate_failure_rolls_back_to_off_and_emits_exactly_one_alert(
    rollout, failure, gate
):
    rollout["state"].joinpath(failure).write_text("1", encoding="ascii")

    result = run_controller(rollout)

    assert result.returncode != 0
    assert rollout["flag"].read_text(encoding="utf-8") == "UVARSI_RECIPE_ENGINE=off\n"
    alerts = rollout["alerts"].read_text(encoding="utf-8").splitlines()
    assert len(alerts) == 1
    assert "rollback complete" in alerts[0].casefold()
    assert f"gate={gate}" in alerts[0]


def test_on_smoke_failure_appends_only_safe_aggregate_diagnostics(rollout):
    rollout["state"].joinpath("fail-smoke").write_text("1", encoding="ascii")
    rollout["state"].joinpath("smoke-output.json").write_text(
        json.dumps(
            {
                "ok": False,
                "blockers": ["too_slow"],
                "latency_ms": 6_250.0,
                "engine_mode": "on",
                "plan_engine": "deterministic",
                "jobs_delta": 0,
                "ai_costs_delta": 0,
                "email": "secret@example.test",
                "recipe": "private recipe text",
                "token": "private-token",
            }
        ),
        encoding="utf-8",
    )

    result = run_controller(rollout)

    assert result.returncode != 0
    alerts = rollout["alerts"].read_text(encoding="utf-8").splitlines()
    assert len(alerts) == 1
    alert = alerts[0]
    assert "gate=on_smoke" in alert
    assert "detail=" in alert
    for expected in (
        "too_slow",
        "latency_ms",
        "6250.0",
        "engine_mode",
        "deterministic",
        "jobs_delta",
        "ai_costs_delta",
    ):
        assert expected in alert
    assert "secret@example.test" not in alert
    assert "private recipe text" not in alert
    assert "private-token" not in alert


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
    assert len(rollout["alerts"].read_text(encoding="utf-8").splitlines()) == 1


def test_malformed_target_rolls_shadow_mode_back_and_alerts_once(rollout):
    rollout["flag"].write_text(
        "UVARSI_RECIPE_ENGINE=shadow\n", encoding="utf-8", newline="\n"
    )
    rollout["target"].write_text("on\nextra", encoding="utf-8", newline="\n")

    result = run_controller(rollout)

    assert result.returncode != 0
    assert rollout["flag"].read_text(encoding="utf-8") == "UVARSI_RECIPE_ENGINE=off\n"
    assert len(rollout["alerts"].read_text(encoding="utf-8").splitlines()) == 1


def test_missing_target_is_no_activation_without_mutation_or_alert(rollout):
    rollout["target"].unlink()
    rollout["flag"].write_text(
        "UVARSI_RECIPE_ENGINE=shadow\n", encoding="utf-8", newline="\n"
    )

    result = run_controller(rollout)

    assert result.returncode == 0, result.stdout + result.stderr
    assert rollout["flag"].read_text(encoding="utf-8") == "UVARSI_RECIPE_ENGINE=shadow\n"
    assert not rollout["calls"].exists()
    assert not rollout["alerts"].exists()


def test_target_open_failure_after_existence_check_rolls_back_and_alerts_once(rollout):
    rollout["flag"].write_text(
        "UVARSI_RECIPE_ENGINE=shadow\n", encoding="utf-8", newline="\n"
    )
    failing_reader = rollout["state"] / "target-reader"
    executable(
        failing_reader,
        "#!/bin/sh\n"
        "/usr/bin/rm -f \"$1\"\n"
        "/usr/bin/cat \"$1\"\n",
    )
    rollout["env"]["UVARSI_RECIPE_TARGET_READER"] = bash_path(failing_reader)

    result = run_controller(rollout)

    assert result.returncode != 0
    assert not rollout["target"].exists()
    assert rollout["flag"].read_text(encoding="utf-8") == "UVARSI_RECIPE_ENGINE=off\n"
    assert len(rollout["alerts"].read_text(encoding="utf-8").splitlines()) == 1


@pytest.mark.parametrize(
    ("field", "invalid"),
    [
        ("success_rate", "0.99"),
        ("success_rate", True),
        ("success_rate", float("nan")),
        ("success_rate", float("inf")),
        ("success_rate", 2.0),
        ("p95_ms", "120"),
        ("p95_ms", True),
        ("p95_ms", float("nan")),
        ("p95_ms", float("-inf")),
        ("p95_ms", -1),
        ("dietary_violations", False),
        ("negative_quantities", "0"),
        ("invalid_package_counts", float("nan")),
    ],
)
def test_shadow_health_rejects_non_numeric_or_non_finite_metrics(
    rollout, field, invalid
):
    payload = {
        "recipe_engine": {
            "mode": "shadow",
            "ready": True,
            "blockers": [],
            "last_shadow": {
                "complete": True,
                "eligible": True,
                "success_rate": 0.99,
                "p95_ms": 120,
                "dietary_violations": 0,
                "negative_quantities": 0,
                "invalid_package_counts": 0,
            },
        }
    }
    payload["recipe_engine"]["last_shadow"][field] = invalid
    rollout["state"].joinpath("health.json").write_text(
        json.dumps(payload, separators=(",", ":")), encoding="utf-8"
    )

    result = run_controller(rollout)

    assert result.returncode != 0
    assert "--recipe-engine-smoke" not in rollout["calls"].read_text(encoding="utf-8")
    assert rollout["flag"].read_text(encoding="utf-8") == "UVARSI_RECIPE_ENGINE=off\n"
    assert len(rollout["alerts"].read_text(encoding="utf-8").splitlines()) == 1


@pytest.mark.parametrize(
    ("payment_flag", "enabled_value"),
    [("PLATBY_ZAPNUTE", "1"), ("UVARSI_PAYMENTS_ENABLED", "true")],
)
def test_live_enabled_payment_flag_fails_closed_without_masking(
    rollout, payment_flag, enabled_value
):
    rollout["env"][payment_flag] = enabled_value

    result = run_controller(rollout)

    assert result.returncode != 0
    assert rollout["flag"].read_text(encoding="utf-8") == "UVARSI_RECIPE_ENGINE=off\n"
    assert len(rollout["alerts"].read_text(encoding="utf-8").splitlines()) == 1
    assert "run_recipe_engine_shadow" not in rollout["calls"].read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "contents",
    [
        "UVARSI_RECIPE_ENGINE=off\nUVARSI_RECIPE_ENGINE=shadow\n",
        "UVARSI_RECIPE_ENGINE=off\nUVARSI_RECIPE_ENGINE=shadow",
        "UVARSI_RECIPE_ENGINE=off\nOTHER_FLAG=1\n",
        "# comment\nUVARSI_RECIPE_ENGINE=off\n",
        "UVARSI_RECIPE_ENGINE='off'\n",
    ],
)
def test_flag_file_rejects_duplicate_or_extra_content_and_canonicalizes_off(
    rollout, contents
):
    rollout["flag"].write_text(contents, encoding="utf-8", newline="\n")

    result = run_controller(rollout)

    assert result.returncode != 0
    assert rollout["flag"].read_text(encoding="utf-8") == "UVARSI_RECIPE_ENGINE=off\n"
    assert len(rollout["alerts"].read_text(encoding="utf-8").splitlines()) == 1


def test_failed_rollback_flag_write_is_not_suppressed_and_alert_is_truthful(rollout):
    rollout["state"].joinpath("fail-shadow").write_text("1", encoding="ascii")
    rollout["state"].joinpath("fail-off-write").write_text("1", encoding="ascii")

    result = run_controller(rollout)

    assert result.returncode != 0
    assert rollout["flag"].read_text(encoding="utf-8") == (
        "UVARSI_RECIPE_ENGINE=shadow\n"
    )
    alerts = rollout["alerts"].read_text(encoding="utf-8").splitlines()
    assert len(alerts) == 1
    assert "rollback incomplete" in alerts[0].casefold()


@pytest.mark.parametrize("failure", ["restart-uvarsi", "is-active-uvarsi"])
def test_rollback_attempts_and_verifies_both_services_independently(rollout, failure):
    rollout["state"].joinpath("fail-shadow").write_text("1", encoding="ascii")
    rollout["state"].joinpath(f"fail-rollback-{failure}").write_text(
        "1", encoding="ascii"
    )

    result = run_controller(rollout)

    assert result.returncode != 0
    calls = rollout["calls"].read_text(encoding="utf-8").splitlines()
    assert calls.count("systemctl restart uvarsi") == 2
    assert calls.count("systemctl restart uvarsi-plan-worker") == 2
    assert calls.count("systemctl is-active --quiet uvarsi") == 2
    assert calls.count("systemctl is-active --quiet uvarsi-plan-worker") == 2
    alerts = rollout["alerts"].read_text(encoding="utf-8").splitlines()
    assert len(alerts) == 1
    assert "rollback incomplete" in alerts[0].casefold()


def test_held_rollout_lock_exits_without_mutation(rollout):
    env = rollout["env"].copy()
    env.pop("UVARSI_ROLLOUT_LOCKED")
    command = (
        f'exec 9>"{bash_path(rollout["state"] / "rollout.lock")}"\n'
        "flock -n 9\n"
        f'"{bash_path(CONTROLLER)}"'
    )

    result = subprocess.run(
        [str(BASH), "-c", command],
        cwd=ROOT,
        env=env,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert rollout["flag"].read_text(encoding="utf-8") == "UVARSI_RECIPE_ENGINE=off\n"
    assert not rollout["calls"].exists()
    alerts = rollout["alerts"].read_text(encoding="utf-8").splitlines()
    assert len(alerts) == 1
    assert "gate=lock_held" in alerts[0]
    assert "engine unchanged" in alerts[0].casefold()


def test_healthy_on_rerun_is_idempotent_and_does_not_restart(rollout):
    rollout["flag"].write_text(
        "UVARSI_RECIPE_ENGINE=on\n", encoding="utf-8", newline="\n"
    )

    result = run_controller(rollout)

    assert result.returncode == 0, result.stdout + result.stderr
    calls = rollout["calls"].read_text(encoding="utf-8")
    assert "systemctl" not in calls
    assert "run_recipe_engine_shadow" not in calls
    assert "--recipe-engine-smoke" not in calls
