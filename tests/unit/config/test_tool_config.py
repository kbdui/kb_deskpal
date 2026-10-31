"""New resource settings obey existing hermetic config behavior."""

import pytest

from deskpal.config import ConfigError, load_config


def test_v02_defaults_without_creating_resource_directories(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    config = load_config(environ={})
    assert config.scheduling.poll_interval_seconds == 1.0
    assert config.screenshot.max_pixels == 16_000_000
    assert not list(tmp_path.iterdir())


def test_v02_environment_overrides_toml_resource_settings(tmp_path):
    path = tmp_path / "fake.toml"
    path.write_text('[scheduling]\npoll_interval_seconds=2.5\n[screenshot]\nmax_pixels=100\n', encoding="utf-8")
    from_file = load_config(path, environ={})
    assert from_file.scheduling.poll_interval_seconds == 2.5
    assert from_file.screenshot.max_pixels == 100
    config = load_config(path, environ={"DESKPAL_SCHEDULING_POLL_INTERVAL_SECONDS": "60",
                                        "DESKPAL_SCREENSHOT_MAX_PIXELS": "200"})
    assert config.scheduling.poll_interval_seconds == 60
    assert config.screenshot.max_pixels == 200


@pytest.mark.parametrize("value", ["0", "-1", "61", "nan", "inf", "invalid"])
def test_invalid_poll_interval_is_rejected(value):
    with pytest.raises(ConfigError):
        load_config(environ={"DESKPAL_SCHEDULING_POLL_INTERVAL_SECONDS": value})


@pytest.mark.parametrize("value", ["0", "-1", "1.5", "nan", "inf", "invalid"])
def test_invalid_screenshot_limit_is_rejected(value):
    with pytest.raises(ConfigError):
        load_config(environ={"DESKPAL_SCREENSHOT_MAX_PIXELS": value})


@pytest.mark.parametrize("section,field,value", [("scheduling", "poll_interval_seconds", "true"),
                                               ("screenshot", "max_pixels", "false"),
                                               ("screenshot", "max_pixels", "1.5")])
def test_toml_bool_or_float_cannot_bypass_resource_types(tmp_path, section, field, value):
    path = tmp_path / "fake.toml"
    path.write_text(f"[{section}]\n{field}={value}\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(path, environ={})
