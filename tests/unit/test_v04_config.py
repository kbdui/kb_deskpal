"""V0.4 typed limits and timezone validation without credentials or network."""

from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from deskpal.config import AppConfig, ConfigError, ProactiveConfig, WorkflowConfig, load_config


def test_defaults_are_independent_immutable_and_do_not_create_directories(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    first, second = AppConfig(), AppConfig()
    assert first.workflow == WorkflowConfig(100, 65536, 262144, 262144, 5.0)
    assert first.proactive == ProactiveConfig(False, 1.0, 100, 10, 86400, "22:00", "08:00", "Asia/Shanghai")
    assert first.workflow is not second.workflow
    assert first.proactive is not second.proactive
    with pytest.raises(FrozenInstanceError):
        first.proactive.enabled = True
    config = load_config(environ={})
    assert config.workflow == first.workflow
    assert config.proactive == first.proactive
    assert list(tmp_path.iterdir()) == []


def test_all_v04_toml_and_environment_overrides(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(
        '[workflow]\nqueue_capacity=8\nrequest_max_bytes=10\nplan_max_bytes=20\n'
        'result_max_bytes=30\nshutdown_timeout_seconds=2.5\n[proactive]\nenabled=true\n'
        'poll_interval_seconds=2\nevent_batch_size=30\nmax_proposals_per_poll=5\n'
        'proposal_ttl_seconds=3600\nquiet_start="21:30"\nquiet_end="07:30"\ntimezone="UTC"\n',
        encoding="utf-8",
    )
    config = load_config(path, environ={})
    assert config.workflow == WorkflowConfig(8, 10, 20, 30, 2.5)
    assert config.proactive == ProactiveConfig(True, 2.0, 30, 5, 3600, "21:30", "07:30", "UTC")
    overridden = load_config(path, environ={
        "DESKPAL_WORKFLOW_QUEUE_CAPACITY": "9", "DESKPAL_WORKFLOW_REQUEST_MAX_BYTES": "11",
        "DESKPAL_WORKFLOW_PLAN_MAX_BYTES": "21", "DESKPAL_WORKFLOW_RESULT_MAX_BYTES": "31",
        "DESKPAL_WORKFLOW_SHUTDOWN_TIMEOUT_SECONDS": "3.5", "DESKPAL_PROACTIVE_ENABLED": "false",
        "DESKPAL_PROACTIVE_POLL_INTERVAL_SECONDS": "3", "DESKPAL_PROACTIVE_EVENT_BATCH_SIZE": "31",
        "DESKPAL_PROACTIVE_MAX_PROPOSALS_PER_POLL": "6", "DESKPAL_PROACTIVE_PROPOSAL_TTL_SECONDS": "3601",
        "DESKPAL_PROACTIVE_QUIET_START": "20:30", "DESKPAL_PROACTIVE_QUIET_END": "06:30",
        "DESKPAL_PROACTIVE_TIMEZONE": "America/New_York",
    })
    assert overridden.workflow == WorkflowConfig(9, 11, 21, 31, 3.5)
    assert overridden.proactive == ProactiveConfig(False, 3.0, 31, 6, 3601, "20:30", "06:30", "America/New_York")


LIMITS = [
    ("workflow", "queue_capacity", 10000),
    ("workflow", "request_max_bytes", 16777216),
    ("workflow", "plan_max_bytes", 16777216),
    ("workflow", "result_max_bytes", 16777216),
    ("workflow", "shutdown_timeout_seconds", 60),
    ("proactive", "poll_interval_seconds", 60),
    ("proactive", "event_batch_size", 100),
    ("proactive", "max_proposals_per_poll", 10),
    ("proactive", "proposal_ttl_seconds", 2592000),
]


@pytest.mark.parametrize("section,key,maximum", LIMITS)
@pytest.mark.parametrize("invalid", ["0", "-1", "nan", "inf", "false", '"2"'])
def test_invalid_numeric_toml_types_and_values(tmp_path, section, key, maximum, invalid):
    path = tmp_path / "bad.toml"
    path.write_text(f"[{section}]\n{key}={invalid}\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(path, environ={})


@pytest.mark.parametrize("section,key,maximum", LIMITS)
def test_upper_bound_inclusive_and_overflow_rejected(section, key, maximum):
    variable = f"DESKPAL_{section.upper()}_{key.upper()}"
    config = load_config(environ={variable: str(maximum)})
    assert getattr(getattr(config, section), key) == maximum
    with pytest.raises(ConfigError):
        load_config(environ={variable: str(maximum + 1)})


@pytest.mark.parametrize("section,key,maximum", [item for item in LIMITS if not item[1].endswith("interval_seconds")
                                               and item[1] != "shutdown_timeout_seconds"])
def test_fractional_integer_settings_rejected(tmp_path, section, key, maximum):
    path = tmp_path / "bad.toml"
    path.write_text(f"[{section}]\n{key}=2.0\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(path, environ={})
    with pytest.raises(ConfigError):
        load_config(environ={f"DESKPAL_{section.upper()}_{key.upper()}": "2.0"})


@pytest.mark.parametrize("invalid", ['"true"', "1", "[]"])
def test_enabled_requires_toml_boolean(tmp_path, invalid):
    path = tmp_path / "bad.toml"
    path.write_text(f"[proactive]\nenabled={invalid}\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(path, environ={})


@pytest.mark.parametrize("invalid", ["1", "yes", "", "not-a-bool"])
def test_enabled_requires_explicit_environment_boolean(invalid):
    with pytest.raises(ConfigError):
        load_config(environ={"DESKPAL_PROACTIVE_ENABLED": invalid})


@pytest.mark.parametrize("key", ["quiet_start", "quiet_end"])
@pytest.mark.parametrize("invalid", ["24:00", "22:60", "8:00", "08:00:00", "", "noon"])
def test_quiet_clock_requires_valid_hh_mm(key, invalid):
    with pytest.raises(ConfigError):
        load_config(environ={f"DESKPAL_PROACTIVE_{key.upper()}": invalid})


@pytest.mark.parametrize("invalid", ["", "No_Such_Zone/Nowhere", "/absolute/path", "../UTC"])
def test_invalid_or_unavailable_timezone_is_rejected_without_echoing_value(invalid):
    with pytest.raises(ConfigError, match="IANA timezone"):
        load_config(environ={"DESKPAL_PROACTIVE_TIMEZONE": invalid})


def test_missing_timezone_data_is_an_explicit_configuration_failure(monkeypatch):
    from zoneinfo import ZoneInfoNotFoundError
    import deskpal.config as module

    def missing(key):
        raise ZoneInfoNotFoundError("synthetic private marker")

    monkeypatch.setattr(module, "ZoneInfo", missing)
    with pytest.raises(ConfigError) as error:
        load_config(environ={})
    assert "synthetic private marker" not in str(error.value)
    assert error.value.__cause__ is None


@pytest.mark.parametrize("text", ['workflow="bad"', 'proactive=[]', '[workflow]\nqueu_capacity=8',
                                  '[proactive]\nenable=true'])
def test_sections_and_unknown_new_keys_are_rejected(tmp_path, text):
    path = tmp_path / "bad.toml"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(path, environ={})


def test_example_contains_valid_v04_defaults():
    example = Path(__file__).resolve().parents[2] / "config.example.toml"
    config = load_config(example, environ={})
    assert config.workflow == WorkflowConfig()
    assert config.proactive == ProactiveConfig()
