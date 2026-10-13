"""Configuration behavior without using the machine's actual environment."""

import pytest
import traceback
import os
from types import SimpleNamespace

import deskpal.config as config_module

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


@pytest.mark.parametrize("source", ["toml", "environment"])
@pytest.mark.parametrize("bad_value", ["sk-fake-credential-must-stay-private", "ENV WITH SPACE",
                                       "9INVALID_NAME", "ENV.NAME", "密钥变量"])
def test_api_key_env_requires_identifier_and_error_does_not_expose_mistyped_value(tmp_path, source, bad_value):
    if source == "toml":
        path = tmp_path / "fake-only.toml"
        path.write_text(f'[llm]\napi_key_env="{bad_value}"\n', encoding="utf-8")
        env = {}
    else:
        path = None
        env = {"DESKPAL_LLM_API_KEY_ENV": bad_value}
    with pytest.raises(ConfigError) as error:
        load_config(path, environ=env)
    visible = str(error.value) + repr(error.value) + "".join(
        traceback.format_exception(error.type, error.value, error.tb))
    assert bad_value not in visible
    assert error.value.__cause__ is None


def test_valid_deepseek_environment_name_reads_only_fake_key_and_hides_repr():
    secret = "fake-deepseek-test-secret"
    config = load_config(environ={"DESKPAL_LLM_PROVIDER": "openai", "DESKPAL_LLM_MODEL": "fake-model",
                                  "DESKPAL_LLM_API_KEY_ENV": "DEEPSEEK_API_KEY", "DEEPSEEK_API_KEY": secret})
    assert config.llm.api_key_env == "DEEPSEEK_API_KEY"
    assert config.llm.api_key == secret
    assert secret not in repr(config)
    assert secret not in repr(config.llm)


@pytest.fixture
def isolated_process_environment(monkeypatch):
    # Patch only config's os reference, leaving pytest's real environment intact.
    # dotenv files and all credentials used in these tests are fake and temporary.
    process = {}
    monkeypatch.setattr(config_module, "os", SimpleNamespace(environ=process))
    return process


def test_dotenv_without_config_path_uses_current_directory(tmp_path, monkeypatch, isolated_process_environment):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text(
        'DESKPAL_LLM_PROVIDER=openai\nDESKPAL_LLM_MODEL="fake-中文-model"\n'
        'DESKPAL_LLM_API_KEY_ENV=TEST_DOTENV_KEY\nTEST_DOTENV_KEY=fake-dotenv-secret\n'
        'DESKPAL_DATA_DIR=数据\n', encoding="utf-8")
    config = load_config()
    assert config.llm.model == "fake-中文-model"
    assert config.llm.api_key == "fake-dotenv-secret"
    assert config.storage.data_dir == tmp_path / "数据"
    assert "fake-dotenv-secret" not in repr(config)
    assert not config.storage.data_dir.exists()


def test_dotenv_uses_config_parent_with_process_then_dotenv_then_toml_priority(
        tmp_path, monkeypatch, isolated_process_environment):
    settings = tmp_path / "settings"
    settings.mkdir()
    current = tmp_path / "other-cwd"
    current.mkdir()
    monkeypatch.chdir(current)
    (current / ".env").write_text("DESKPAL_LLM_MODEL=wrong-cwd-model\n", encoding="utf-8")
    path = settings / "config.toml"
    path.write_text('[llm]\nprovider="openai"\nmodel="toml-model"\napi_key_env="TEST_DOTENV_KEY"\n'
                    'timeout_seconds=3.0\n[sandbox]\nworkspace_dir="toml-files"\n'
                    '[storage]\ndata_dir="toml-data"\n[agent]\nmax_tool_steps=4\n', encoding="utf-8")
    (settings / ".env").write_text(
        'DESKPAL_LLM_MODEL=dotenv-model\nTEST_DOTENV_KEY=fake-dotenv-secret\n'
        'DESKPAL_LLM_TIMEOUT_SECONDS=4.5\nDESKPAL_WORKSPACE_DIR=dotenv-files\n'
        'DESKPAL_MAX_TOOL_STEPS=6\n', encoding="utf-8")
    isolated_process_environment.update({"DESKPAL_LLM_MODEL": "process-model", "DESKPAL_MAX_TOOL_STEPS": "7",
                                         "TEST_DOTENV_KEY": "fake-process-secret"})
    config = load_config(path)
    assert config.llm.model == "process-model"
    assert config.llm.api_key == "fake-process-secret"
    assert config.agent.max_tool_steps == 7
    assert config.llm.timeout_seconds == 4.5
    assert config.sandbox.workspace_dir == settings / "dotenv-files"
    assert config.storage.data_dir == settings / "toml-data"
    assert config.sandbox.max_file_bytes == 1_048_576


def test_dotenv_does_not_mutate_injected_or_global_environment(
        tmp_path, monkeypatch, isolated_process_environment):
    monkeypatch.chdir(tmp_path)
    existing, added = "DESKPAL_TEST_DOTENV_EXISTING", "DESKPAL_TEST_DOTENV_ADDED"
    monkeypatch.setenv(existing, "global-original")
    monkeypatch.setenv(added, "cleanup-placeholder")
    monkeypatch.delenv(added)
    isolated_process_environment["TEST_PROCESS_SENTINEL"] = "process-original"
    (tmp_path / ".env").write_text(f"{existing}=dotenv-value\n{added}=new-value\n"
                                   "TEST_PROCESS_SENTINEL=dotenv-value\n", encoding="utf-8")
    load_config()
    assert isolated_process_environment == {"TEST_PROCESS_SENTINEL": "process-original"}
    assert os.environ[existing] == "global-original"
    assert added not in os.environ


def test_dotenv_variable_syntax_is_literal_in_credentials(
        tmp_path, monkeypatch, isolated_process_environment):
    monkeypatch.chdir(tmp_path)
    isolated_process_environment["TEST_SOURCE_VALUE"] = "fake-process-secret"
    (tmp_path / ".env").write_text(
        'DESKPAL_LLM_PROVIDER=openai\nDESKPAL_LLM_MODEL=fake-model\n'
        'DESKPAL_LLM_API_KEY_ENV=TEST_DOTENV_KEY\nTEST_SOURCE_VALUE=fake-file-secret\n'
        'TEST_DOTENV_KEY=${TEST_SOURCE_VALUE}\n', encoding="utf-8")
    config = load_config()
    assert config.llm.api_key == "${TEST_SOURCE_VALUE}"
    assert isolated_process_environment["TEST_SOURCE_VALUE"] == "fake-process-secret"


@pytest.mark.parametrize("explicit", [{}, {"DESKPAL_LLM_MODEL": "explicit-model"}])
def test_explicit_environ_never_reads_dotenv_or_process(
        tmp_path, monkeypatch, isolated_process_environment, explicit):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_bytes(b"fake-secret-marker\xff")
    isolated_process_environment["DESKPAL_LLM_PROVIDER"] = "invalid-process-provider"

    def forbidden_read(*args, **kwargs):
        raise AssertionError("Explicit environ must not read .env")

    monkeypatch.setattr(config_module, "dotenv_values", forbidden_read)
    config = load_config(environ=explicit)
    assert config.llm.provider == "offline"
    assert config.llm.model == explicit.get("DESKPAL_LLM_MODEL", "")
    assert config.llm.api_key == ""


def test_dotenv_bare_entries_are_ignored_but_empty_values_remain_values(
        tmp_path, monkeypatch, isolated_process_environment):
    monkeypatch.chdir(tmp_path)
    env_file = tmp_path / ".env"
    env_file.write_text("DESKPAL_LLM_PROVIDER\nDESKPAL_MAX_TOOL_STEPS\nBARE_TEST_KEY\n", encoding="utf-8")
    config = load_config()
    assert config.llm.provider == "offline"
    assert config.agent.max_tool_steps == 8
    env_file.write_text("DESKPAL_LLM_PROVIDER=\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config()


def test_empty_dotenv_credential_is_not_treated_as_configured(
        tmp_path, monkeypatch, isolated_process_environment):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text(
        'DESKPAL_LLM_PROVIDER=openai\nDESKPAL_LLM_MODEL=fake-model\n'
        'DESKPAL_LLM_API_KEY_ENV=TEST_DOTENV_KEY\nTEST_DOTENV_KEY=\n', encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config()


@pytest.mark.parametrize("failure", ["encoding", "read-error"])
def test_dotenv_read_errors_are_sanitized_config_errors(
        tmp_path, monkeypatch, isolated_process_environment, failure):
    monkeypatch.chdir(tmp_path)
    marker = "fake-sensitive-dotenv-marker"
    (tmp_path / ".env").write_bytes(marker.encode("utf-8") + b"\xff")
    if failure == "read-error":
        def broken_read(*args, **kwargs):
            raise OSError(marker)
        monkeypatch.setattr(config_module, "dotenv_values", broken_read)
    with pytest.raises(ConfigError) as error:
        load_config()
    visible = str(error.value) + repr(error.value) + "".join(
        traceback.format_exception(error.type, error.value, error.tb))
    assert marker not in visible
    assert error.value.__cause__ is None
    assert error.value.__suppress_context__ is True


def test_missing_dotenv_uses_process_and_defaults_without_error(
        tmp_path, monkeypatch, isolated_process_environment):
    monkeypatch.chdir(tmp_path)
    isolated_process_environment["DESKPAL_LLM_MODEL"] = "process-model"
    config = load_config()
    assert config.llm.provider == "offline"
    assert config.llm.model == "process-model"
    assert config.storage.data_dir == tmp_path / "data"
    assert not (tmp_path / ".env").exists()
