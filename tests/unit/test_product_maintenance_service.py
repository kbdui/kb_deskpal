"""Prepared service-boundary checks with isolated files and a fake vault only."""

import builtins
import hashlib
import importlib
import json
import math
import os
from pathlib import Path

import pytest

from deskpal.application.instance import InstanceLockError
from deskpal.config import ConfigError
from deskpal.credentials import CredentialStoreError
from deskpal.maintenance import service
from deskpal.maintenance.errors import MaintenanceError


PRIVATE = "synthetic-secret-never-display-4821"
REFERENCE = "Deskpal/LLM/" + "a" * 32


@pytest.fixture(autouse=True)
def isolated_environment(tmp_path, monkeypatch):
    for name in list(os.environ):
        if name.startswith("DESKPAL_") or name in {"OPENAI_API_KEY", "LOCALAPPDATA", "XDG_CONFIG_HOME"}:
            monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local-app-data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
    import deskpal.credentials as credentials
    import deskpal.maintenance.setup as setup
    class FakeVault:
        @staticmethod
        def available():
            return True

        @staticmethod
        def valid_ref(value):
            return value == REFERENCE

        def read(self, reference):
            assert reference == REFERENCE
            return PRIVATE
    monkeypatch.setattr(credentials, "WindowsCredentialStore", FakeVault)
    monkeypatch.setattr(setup, "WindowsCredentialStore", FakeVault)


@pytest.fixture
def config_path(tmp_path):
    path = tmp_path / "synthetic.toml"
    path.write_text('[llm]\nprovider="offline"\n[storage]\ndata_dir="data"\n'
                    '[sandbox]\nworkspace_dir="workspace"\n'
                    '[memory]\nmodel_cache_dir="model-cache"\n', encoding="utf-8")
    return path


def test_maintenance_import_and_queries_are_qt_free_and_never_construct_application(config_path, monkeypatch):
    import deskpal.application as application
    import deskpal.application.factory as factory
    original_import = builtins.__import__
    def guarded_import(name, *args, **kwargs):
        if name == "PySide6" or name.startswith(("PySide6.", "deskpal.gui")):
            raise AssertionError("maintenance attempted a GUI import")
        return original_import(name, *args, **kwargs)
    def forbidden(*args, **kwargs):
        raise AssertionError("maintenance must not construct Core or Application")
    monkeypatch.setattr(builtins, "__import__", guarded_import)
    for module in (application, factory):
        monkeypatch.setattr(module, "create_application", forbidden)
        monkeypatch.setattr(module, "create_agent", forbidden)
    monkeypatch.setattr(factory, "_create_core", forbidden)
    importlib.reload(service)
    local = service.MaintenanceService(config_path)
    assert local.execute("setup_status", {})["can_start"] is True
    result = local.execute("diagnostics", {})
    assert result["backend_started"] is False
    assert result["network_requested"] is False


@pytest.mark.parametrize("operation,parameters", [
    ("unknown", {}), ("setup_status", {"unexpected": True}),
    ("setup_status", []),
    ("setup_save", {"values": {"timeout_seconds": float("nan")}}),
    ("setup_save", {"values": {"timeout_seconds": float("inf")}}),
    ("setup_save", {"values": object()}),
    ("setup_save", {"values": {"model": "x" * 131073}}),
])
def test_invalid_parameters_never_dispatch(tmp_path, monkeypatch, operation, parameters):
    local = service.MaintenanceService(tmp_path / "unused.toml")
    def forbidden(*args, **kwargs):
        raise AssertionError("invalid parameters reached an adapter")
    monkeypatch.setattr(local, "_execute", forbidden)
    with pytest.raises(MaintenanceError) as caught:
        local.execute(operation, parameters)
    assert caught.value.code == "invalid_config"


def test_cyclic_parameters_are_rejected_before_dispatch(tmp_path, monkeypatch):
    local = service.MaintenanceService(tmp_path / "unused.toml")
    cycle = {}
    cycle["nested"] = cycle
    monkeypatch.setattr(local, "_execute", lambda *args: pytest.fail("cycle reached adapter"))
    with pytest.raises(MaintenanceError) as caught:
        local.execute("setup_save", {"values": cycle})
    assert caught.value.code == "invalid_config"


@pytest.mark.parametrize("key", ["api_key", "credential_secret", "secret", "token", "authorization", "API_KEY"])
def test_secret_fields_in_nested_adapter_dto_are_rejected_whole(tmp_path, monkeypatch, key):
    local = service.MaintenanceService(tmp_path / "unused.toml")
    monkeypatch.setattr(local, "_execute", lambda *args: {"items": [{key: PRIVATE}]})
    with pytest.raises(MaintenanceError) as caught:
        local.execute("diagnostics", {})
    assert caught.value.code == "io_error"
    assert PRIVATE not in str(caught.value)


@pytest.mark.parametrize("payload", [{"body": "x" * (4 * 1024 * 1024)}, {"value": float("nan")}, {"value": object()}])
def test_unbounded_or_non_json_adapter_results_are_rejected(tmp_path, monkeypatch, payload):
    local = service.MaintenanceService(tmp_path / "unused.toml")
    monkeypatch.setattr(local, "_execute", lambda *args: payload)
    with pytest.raises(MaintenanceError) as caught:
        local.execute("diagnostics", {})
    assert caught.value.code == "io_error"


def test_success_dto_is_a_detached_json_copy(tmp_path, monkeypatch):
    local = service.MaintenanceService(tmp_path / "unused.toml")
    original = {"items": [{"name": "synthetic"}], "credential_ref": REFERENCE}
    monkeypatch.setattr(local, "_execute", lambda *args: original)
    result = local.execute("diagnostics", {})
    result["items"][0]["name"] = "changed"
    assert original["items"][0]["name"] == "synthetic"


@pytest.mark.parametrize("kind,code", [
    (RuntimeError, "io_error"), (OSError, "io_error"),
    (ValueError, "invalid_config"), (KeyError, "invalid_config"),
    (ConfigError, "invalid_config"), (ImportError, "optional_dependency_missing"),
    (InstanceLockError, "busy"),
])
def test_safe_error_never_uses_untrusted_exception_body(kind, code):
    result = service.safe_error(kind(PRIVATE))
    assert result["code"] == code
    assert PRIVATE not in json.dumps(result, ensure_ascii=False)
    assert result["message"] and result["next_steps"]


def test_safe_error_uses_fixed_messages_for_maintenance_and_vault_errors():
    for error, code in ((MaintenanceError("restore_pending"), "restore_pending"),
                        (CredentialStoreError(), "credential_store_unavailable")):
        error.args = (PRIVATE,)
        result = service.safe_error(error)
        assert result["code"] == code
        assert PRIVATE not in repr(result)


def test_valid_environment_override_is_warning_and_effective_config_can_start(config_path, tmp_path, monkeypatch):
    monkeypatch.setenv("DESKPAL_LLM_MODEL", "synthetic-environment-model")
    monkeypatch.setenv("DESKPAL_DATA_DIR", str(tmp_path / "environment-data"))
    result = service.MaintenanceService(config_path).execute("setup_status", {})
    assert result["config_valid"] is True
    assert result["can_start"] is True
    assert result["sources"]["model"] == "environment"
    assert result["values"]["model"] == ""
    assert result["effective"]["model"] == "synthetic-environment-model"
    assert result["effective"]["data_dir"] == str((tmp_path / "environment-data").resolve())
    assert "environment_override" in {issue["code"] for issue in result["issues"]}


def test_offline_startup_override_needs_no_credential_and_preserves_cloud_file(config_path, monkeypatch):
    import deskpal.credentials as credentials

    def forbidden(*args, **kwargs):
        raise AssertionError("synthetic startup status must not read a credential vault")

    monkeypatch.setattr(credentials.WindowsCredentialStore, "read", forbidden)
    config_path.write_text(config_path.read_text(encoding="utf-8").replace(
        'provider="offline"', 'provider="openai"\nmodel="synthetic-cloud-model"'), encoding="utf-8")
    original = config_path.read_bytes()
    ordinary = service.MaintenanceService(config_path).execute("setup_status", {})
    assert ordinary["config_valid"] is True
    assert ordinary["can_start"] is False
    assert ordinary["credential_present"] is False
    assert "credential_missing" in {issue["code"] for issue in ordinary["issues"]}
    overridden = service.MaintenanceService(config_path, provider_override="offline").execute("setup_status", {})
    assert overridden["config_valid"] is True
    assert overridden["can_start"] is True
    assert overridden["effective"]["provider"] == "offline"
    assert overridden["values"]["provider"] == "openai"
    assert overridden["effective"]["model"] == "synthetic-cloud-model"
    assert config_path.read_bytes() == original


def test_save_with_offline_startup_override_keeps_selected_cloud_provider(config_path):
    import tomllib

    local = service.MaintenanceService(config_path, provider_override="offline")
    before = local.execute("setup_status", {})
    saved = local.execute("setup_save", {
        "values": {"provider": "openai", "model": "synthetic-cloud-model"},
        "expected_revision": before["revision"],
    })
    assert saved["can_start"] is True
    assert saved["effective"]["provider"] == "offline"
    assert saved["values"]["provider"] == "openai"
    assert tomllib.loads(config_path.read_text(encoding="utf-8"))["llm"]["provider"] == "openai"


@pytest.mark.parametrize("override", ["openai", "", "unexpected"])
def test_non_offline_startup_override_is_rejected(config_path, override):
    with pytest.raises(MaintenanceError) as caught:
        service.MaintenanceService(config_path, provider_override=override)
    assert caught.value.code == "invalid_config"


@pytest.mark.parametrize("literal", ["nan", "inf", "-inf", "-1", "0", "1" + "0" * 400])
def test_invalid_file_timeout_returns_safe_revision_and_can_be_explicitly_repaired(config_path, literal):
    config_path.write_text(config_path.read_text(encoding="utf-8").replace(
        'provider="offline"', 'provider="offline"\ntimeout_seconds=' + literal), encoding="utf-8")
    original = config_path.read_bytes()
    local = service.MaintenanceService(config_path)
    state = local.execute("setup_status", {})
    assert state["revision"] == hashlib.sha256(original).hexdigest()
    assert state["config_valid"] is False
    assert state["can_start"] is False
    assert state["values"]["timeout_seconds"] == 30.0
    assert math.isfinite(state["values"]["timeout_seconds"])
    assert "invalid_config" in {issue["code"] for issue in state["issues"]}
    json.dumps(state, allow_nan=False)
    assert config_path.read_bytes() == original
    repaired = local.execute("setup_repair", {
        "values": {"provider": "offline", "timeout_seconds": 30.0},
        "expected_revision": state["revision"], "confirm": True,
    })
    assert repaired["config_valid"] is True
    assert repaired["can_start"] is True
    assert repaired["values"]["timeout_seconds"] == 30.0
    assert repaired["configuration_reset"] is True
    assert next(config_path.parent.glob(config_path.name + ".recovery-*")).read_bytes() == original


def test_restore_marker_blocks_effective_startup_without_invalidating_config(config_path, tmp_path):
    directory = tmp_path / "data"
    directory.mkdir()
    (directory / ".restore-in-progress.json").write_text("malformed synthetic marker", encoding="ascii")
    result = service.MaintenanceService(config_path).execute("setup_status", {})
    assert result["config_valid"] is True
    assert result["can_start"] is False
    assert "restore_pending" in {issue["code"] for issue in result["issues"]}


def test_environment_userinfo_url_does_not_escape_into_status(config_path, monkeypatch):
    monkeypatch.setenv("DESKPAL_LLM_BASE_URL", f"https://user:{PRIVATE}@example.invalid/v1")
    result = service.MaintenanceService(config_path).execute("setup_status", {})
    assert result["config_valid"] is True
    assert result["can_start"] is False
    assert result["sources"]["base_url"] == "environment"
    assert PRIVATE not in json.dumps(result, ensure_ascii=False)
    assert "effective" not in result


def test_vault_presence_exposes_only_boolean_and_source(config_path):
    # Place the reference inside the existing llm table without writing a secret.
    text = config_path.read_text(encoding="utf-8").replace('provider="offline"', f'provider="offline"\ncredential_ref="{REFERENCE}"')
    config_path.write_text(text, encoding="utf-8")
    result = service.MaintenanceService(config_path).execute("setup_status", {})
    assert result["credential_present"] is True
    assert result["credential_source"] == "system_vault"
    assert PRIVATE not in repr(result)


@pytest.mark.parametrize("platform,variable,folder", [("win32", "LOCALAPPDATA", "Deskpal"), ("linux", "XDG_CONFIG_HOME", "deskpal")])
def test_default_config_path_uses_isolated_platform_directory(tmp_path, monkeypatch, platform, variable, folder):
    monkeypatch.setattr(service.sys, "platform", platform)
    assert service.default_config_path() == Path(os.environ[variable]) / folder / "config.toml"
    assert not service.default_config_path().exists()


def test_default_config_path_prefers_existing_cwd_config(tmp_path):
    project = tmp_path / "config.toml"
    project.write_text('[llm]\nprovider="offline"\n', encoding="utf-8")
    assert service.default_config_path() == project.resolve()
