"""Configuration behavior without using the machine's actual environment."""

import pytest

from deskpal.config import ConfigError, load_config


def test_defaults_use_current_directory_without_creating_storage(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    config = load_config(environ={})
    assert config.llm.provider == "offline"
    assert config.llm.api_key == ""
    assert config.sandbox.workspace_dir == tmp_path / "workspace"
    assert config.storage.data_dir == tmp_path / "data"
    assert list(tmp_path.iterdir()) == []


def test_environment_overrides_toml_for_all_public_settings(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(
        '[llm]\nprovider="offline"\nmodel="toml-model"\n'
        'base_url="https://toml.invalid/v1"\napi_key_env="TOML_KEY"\ntimeout_seconds=2\n'
        '[sandbox]\nworkspace_dir="toml-workspace"\nmax_file_bytes=5\n'
        '[storage]\ndata_dir="toml-data"\n[agent]\nmax_tool_steps=2\n',
        encoding="utf-8",
    )
    config = load_config(path, environ={
        "DESKPAL_LLM_PROVIDER": "openai", "DESKPAL_LLM_MODEL": "env-model",
        "DESKPAL_LLM_BASE_URL": "https://env.invalid/v1/", "DESKPAL_LLM_API_KEY_ENV": "TEST_KEY",
        "DESKPAL_LLM_TIMEOUT_SECONDS": "3.5", "DESKPAL_WORKSPACE_DIR": "env-workspace",
        "DESKPAL_MAX_FILE_BYTES": "15", "DESKPAL_DATA_DIR": "env-data",
        "DESKPAL_MAX_TOOL_STEPS": "4", "TEST_KEY": "fake-unit-test-key",
    })
    assert config.llm.provider == "openai"
    assert config.llm.model == "env-model"
    assert config.llm.base_url == "https://env.invalid/v1"
    assert config.llm.api_key_env == "TEST_KEY"
    assert config.llm.api_key == "fake-unit-test-key"
    assert config.llm.timeout_seconds == 3.5
    assert config.sandbox.workspace_dir == tmp_path / "env-workspace"
    assert config.sandbox.max_file_bytes == 15
    assert config.storage.data_dir == tmp_path / "env-data"
    assert config.agent.max_tool_steps == 4
    assert "fake-unit-test-key" not in repr(config)
    assert "fake-unit-test-key" not in repr(config.llm)
    assert not config.storage.data_dir.exists()
    assert not config.sandbox.workspace_dir.exists()


def test_toml_values_override_defaults_and_paths_use_config_parent(tmp_path, monkeypatch):
    settings = tmp_path / "settings"
    settings.mkdir()
    path = settings / "config.toml"
    path.write_text('[sandbox]\nworkspace_dir="../files"\nmax_file_bytes=9\n'
                    '[storage]\ndata_dir="db"\n[agent]\nmax_tool_steps=3\n', encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    config = load_config(path, environ={})
    assert config.sandbox.workspace_dir == tmp_path / "files"
    assert config.storage.data_dir == settings / "db"
    assert config.sandbox.max_file_bytes == 9
    assert config.agent.max_tool_steps == 3


def test_offline_does_not_read_key_variable():
    class KeyReadForbidden(dict):
        def get(self, key, default=None):
            if key == "OPENAI_API_KEY":
                raise AssertionError("Offline mode must not look up the API key")
            return super().get(key, default)

    assert load_config(environ=KeyReadForbidden()).llm.api_key == ""


@pytest.mark.parametrize("env", [
    {"DESKPAL_LLM_PROVIDER": "other"},
    {"DESKPAL_LLM_PROVIDER": "openai", "OPENAI_API_KEY": "fake-test-key"},
    {"DESKPAL_LLM_PROVIDER": "openai", "DESKPAL_LLM_MODEL": "model"},
    {"DESKPAL_LLM_PROVIDER": "openai", "DESKPAL_LLM_MODEL": "model", "OPENAI_API_KEY": "  "},
    {"DESKPAL_LLM_PROVIDER": "openai", "DESKPAL_LLM_MODEL": "model",
     "OPENAI_API_KEY": "fake-test-key", "DESKPAL_LLM_BASE_URL": "file:///unsafe"},
    {"DESKPAL_LLM_API_KEY_ENV": " "}, {"DESKPAL_WORKSPACE_DIR": " "}, {"DESKPAL_DATA_DIR": ""},
])
def test_invalid_required_values_are_rejected_without_exposing_key(env):
    with pytest.raises(ConfigError) as error:
        load_config(environ=env)
    assert "fake-test-key" not in str(error.value)


@pytest.mark.parametrize("variable", ["DESKPAL_MAX_TOOL_STEPS", "DESKPAL_MAX_FILE_BYTES"])
@pytest.mark.parametrize("value", ["0", "-1", "nan", "inf", "1.5", "invalid"])
def test_invalid_integer_environment_values(variable, value):
    with pytest.raises(ConfigError):
        load_config(environ={variable: value})


@pytest.mark.parametrize("value", ["0", "-0.1", "nan", "inf", "-inf", "invalid"])
def test_invalid_timeout_environment_values(value):
    with pytest.raises(ConfigError):
        load_config(environ={"DESKPAL_LLM_TIMEOUT_SECONDS": value})


@pytest.mark.parametrize("section,field,value", [
    ("agent", "max_tool_steps", "true"), ("agent", "max_tool_steps", "2.0"),
    ("sandbox", "max_file_bytes", "false"), ("sandbox", "max_file_bytes", "2.5"),
    ("llm", "timeout_seconds", "true"), ("llm", "provider", "123"),
    ("storage", "data_dir", "[]"),
])
def test_toml_wrong_types_fail(tmp_path, section, field, value):
    path = tmp_path / "config.toml"
    path.write_text(f"[{section}]\n{field}={value}\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(path, environ={})


@pytest.mark.parametrize("field", ["api_key", "token", "secret", "authorization", "API_KEY"])
def test_credentials_forbidden_in_toml_even_with_environment_override(tmp_path, field):
    path = tmp_path / "config.toml"
    path.write_text(f'[llm]\n{field}="fake-forbidden-key"\n', encoding="utf-8")
    with pytest.raises(ConfigError) as error:
        load_config(path, environ={"DESKPAL_LLM_PROVIDER": "offline"})
    assert "fake-forbidden-key" not in str(error.value)


@pytest.mark.parametrize("content", ["[broken", 'llm="not-a-table"', 'agent=[]'])
def test_invalid_configuration_document(tmp_path, content):
    path = tmp_path / "config.toml"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(path, environ={})


def test_missing_configuration_file_is_reported(tmp_path):
    with pytest.raises(ConfigError):
        load_config(tmp_path / "missing.toml", environ={})
