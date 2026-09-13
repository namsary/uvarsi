"""Release contract for the annual Premium subscription.

These tests deliberately cover the operator-facing runbook and both deployment
paths.  A release may contain correct billing code and still be unusable when
the server is missing one module or one mode-specific setting.
"""

import os
import subprocess
from pathlib import Path

import pytest


RUNBOOK = Path("docs/prevadzka.md")
LEGAL_FILES = (
    Path("docs/legal/00_PRAVNY_AUDIT_UVARSI.md"),
    Path("docs/legal/01_VOP_NAVRH.md"),
    Path("docs/legal/04_CHECKLIST_PRED_PLATBAMI.md"),
)
MANUAL_DEPLOY = Path("nasad.ps1")
AUTO_DEPLOY = Path("hetzner/samopull.sh")
DEPLOY_STATE = Path("hetzner/uvarsi-deploy-state.sh")
BASH = Path("C:/Program Files/Git/bin/bash.exe")

LIVE_KEYS = (
    "LEMON_API_KEY",
    "LEMON_WEBHOOK_SECRET",
    "LEMON_STORE_ID",
    "LEMON_SUBSCRIPTION_VARIANT_ID",
    "LEMON_FOUNDER_DISCOUNT_ID",
    "LEMON_FOUNDER_DISCOUNT_CODE",
)
TEST_KEYS = (
    "LEMON_TEST_API_KEY",
    "LEMON_TEST_WEBHOOK_SECRET",
    "LEMON_TEST_STORE_ID",
    "LEMON_TEST_SUBSCRIPTION_VARIANT_ID",
    "LEMON_TEST_FOUNDER_DISCOUNT_ID",
    "LEMON_TEST_FOUNDER_DISCOUNT_CODE",
)


def _bash_path(path: Path) -> str:
    return "/c" + path.resolve().as_posix()[2:]


def _run_payments_off_gate(tmp_path: Path, env_text: str):
    env_file = tmp_path / "uvarsi.env"
    mutation_marker = tmp_path / "live-mutation"
    env_file.write_text(env_text, encoding="utf-8", newline="\n")
    command = (
        f'. "{_bash_path(DEPLOY_STATE)}"\n'
        "uvarsi_require_payments_off || exit 42\n"
        ': > "$UVARSI_TEST_MUTATION_MARKER"\n'
    )
    result = subprocess.run(
        [str(BASH), "-c", command],
        cwd=Path.cwd(),
        env=os.environ
        | {
            "UVARSI_ENV_FILE": _bash_path(env_file),
            "UVARSI_TEST_MUTATION_MARKER": _bash_path(mutation_marker),
        },
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
    )
    return result, mutation_marker


@pytest.mark.parametrize("false_value", ("0", "false", "OFF"))
def test_payment_off_gate_accepts_one_explicit_false_for_both_flags(
    tmp_path, false_value
):
    result, mutation_marker = _run_payments_off_gate(
        tmp_path,
        f"PLATBY_ZAPNUTE={false_value}\n"
        f"UVARSI_PAYMENTS_ENABLED={false_value}\n",
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert mutation_marker.is_file()


@pytest.mark.parametrize(
    "env_text",
    (
        "UVARSI_PAYMENTS_ENABLED=0\n",
        "PLATBY_ZAPNUTE=0\n",
        "PLATBY_ZAPNUTE=0\nPLATBY_ZAPNUTE=off\nUVARSI_PAYMENTS_ENABLED=0\n",
        "PLATBY_ZAPNUTE=0\nUVARSI_PAYMENTS_ENABLED=0\nUVARSI_PAYMENTS_ENABLED=off\n",
        "PLATBY_ZAPNUTE=1\nUVARSI_PAYMENTS_ENABLED=0\n",
        "PLATBY_ZAPNUTE=0\nUVARSI_PAYMENTS_ENABLED=true\n",
        "PLATBY_ZAPNUTE=0\nUVARSI_PAYMENTS_ENABLED=\n",
        "PLATBY_ZAPNUTE=0\nUVARSI_PAYMENTS_ENABLED=no\n",
        "PLATBY_ZAPNUTE=0\nUVARSI_PAYMENTS_ENABLED off\n",
    ),
    ids=(
        "primary-missing",
        "secondary-missing",
        "primary-duplicate",
        "secondary-duplicate",
        "primary-true",
        "secondary-true",
        "secondary-empty",
        "secondary-noncontract-false",
        "secondary-malformed",
    ),
)
def test_payment_off_gate_stops_before_mutation_for_unsafe_flag_file(
    tmp_path, env_text
):
    result, mutation_marker = _run_payments_off_gate(tmp_path, env_text)

    assert result.returncode == 42
    assert not mutation_marker.exists()


def test_release_requires_separate_live_and_test_subscription_configuration():
    runbook = RUNBOOK.read_text(encoding="utf-8")
    for key in (*LIVE_KEYS, *TEST_KEYS, "UVARSI_PAYMENT_SMOKE_SIGNING_SECRET"):
        assert key in runbook


def test_runbook_records_the_approved_annual_dashboard_economics():
    runbook = RUNBOOK.read_text(encoding="utf-8")
    required = (
        "49 €",
        "10 €",
        "duration=once",
        "50",
        "bez skúšobného obdobia",
        "sedem dní pred obnovou",
        "Customer Portal",
        "2026-09-12-v5",
    )
    for statement in required:
        assert statement in runbook


def test_runbook_covers_every_required_test_subscription_lifecycle_event():
    runbook = RUNBOOK.read_text(encoding="utf-8")
    required_events = (
        "subscription_created",
        "subscription_payment_success",
        "subscription_cancelled",
        "subscription_resumed",
        "subscription_payment_failed",
        "subscription_payment_recovered",
        "subscription_expired",
        "subscription_payment_refunded",
        "order_refunded",
    )
    for event in required_events:
        assert event in runbook
    for operation in (
        "rekonciliácia",
        "portál",
        "podpísaný marker",
        "readiness",
        "rollback",
    ):
        assert operation in runbook.casefold()


def test_internal_legal_docs_match_the_current_annual_offer_and_operator():
    for path in LEGAL_FILES:
        text = path.read_text(encoding="utf-8")
        assert "2026-09-12-v5" in text
        assert "PUMAR s. r. o." in text
        assert "+421 917 347 009" in text
        assert "pumaragency@gmail.com" in text
        assert "39 €" in text
        assert "49 €" in text
        assert "automaticky" in text.casefold()
        stale_phrases = tuple(
            left + right
            for left, right in (
                ("39 € ", "jednorazovo"),
                ("bez automatickej ", "obnovy"),
                ("24 ", "mesiacov"),
            )
        )
        assert all(phrase not in text for phrase in stale_phrases)


def test_both_deploy_paths_require_all_subscription_runtime_artifacts():
    scripts = {
        "manual": MANUAL_DEPLOY.read_text(encoding="utf-8"),
        "automatic": AUTO_DEPLOY.read_text(encoding="utf-8"),
    }
    required = (
        "app/config.py",
        "app/platby.py",
        "app/predplatne.py",
        "app/rekonciliacia.py",
        "app/customer_requests.py",
        "app/payment_readiness.py",
        "app/payment_smoke_marker.py",
        "hetzner/payment-smoke.py",
        "app/static/subscription-profile.19ddd6feb9d0.js",
    )
    for deploy_name, script in scripts.items():
        normalized = script.replace("\\", "/")
        for artifact in required:
            if (
                deploy_name == "manual"
                and artifact.startswith("app/static/")
                and '$B/app/static/*' in normalized
            ):
                continue
            assert artifact in normalized, f"{deploy_name} deploy misses {artifact}"


def test_deploy_checks_name_every_subscription_setting_without_secret_values():
    for path in (MANUAL_DEPLOY, AUTO_DEPLOY):
        script = path.read_text(encoding="utf-8")
        for key in (*LIVE_KEYS, *TEST_KEYS, "UVARSI_PAYMENT_SMOKE_SIGNING_SECRET"):
            assert key in script, f"{path} does not check {key}"
        assert "sk_live_" not in script
        assert "sk_test_" not in script


def test_release_runbook_keeps_production_payments_off_until_owner_approval():
    runbook = RUNBOOK.read_text(encoding="utf-8")
    assert "PLATBY_ZAPNUTE=0" in runbook
    assert "UVARSI_PAYMENTS_ENABLED=0" in runbook
    assert "samostatnom výslovnom schválení majiteľa" in runbook
