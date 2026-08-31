import pytest

from app import config


@pytest.fixture(autouse=True)
def reset_recipe_engine_config_cache():
    reset = getattr(config, "reset_config_cache_for_tests", lambda: None)
    reset()
    yield
    reset()


@pytest.mark.parametrize("value", ["off", "shadow", "on"])
def test_accepts_only_exact_recipe_engine_modes(monkeypatch, value):
    monkeypatch.setenv("UVARSI_RECIPE_ENGINE", value)

    assert config.recipe_engine_mode() == value


def test_missing_recipe_engine_mode_defaults_to_off(monkeypatch):
    monkeypatch.delenv("UVARSI_RECIPE_ENGINE", raising=False)

    assert config.recipe_engine_mode() == "off"


@pytest.mark.parametrize(
    "value",
    ["", "yes", "true", "1", "ON", "Shadow", " off", "on ", "\tshadow\n"],
)
def test_invalid_recipe_engine_mode_fails_closed(monkeypatch, value):
    monkeypatch.setenv("UVARSI_RECIPE_ENGINE", value)

    with pytest.raises(RuntimeError, match="UVARSI_RECIPE_ENGINE"):
        config.recipe_engine_mode()


def test_recipe_engine_mode_is_cached_until_explicit_reset(monkeypatch):
    monkeypatch.setenv("UVARSI_RECIPE_ENGINE", "shadow")
    assert config.recipe_engine_mode() == "shadow"

    monkeypatch.setenv("UVARSI_RECIPE_ENGINE", "on")
    assert config.recipe_engine_mode() == "shadow"

    config.reset_config_cache_for_tests()
    assert config.recipe_engine_mode() == "on"
