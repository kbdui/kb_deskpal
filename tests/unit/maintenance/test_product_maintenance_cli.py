"""Prepared maintenance CLI tests; no backend, real vault or real config use."""

import getpass
import io
import json
import os

import pytest

from deskpal.maintenance import cli


PRIVATE = "synthetic-cli-secret-never-display-9032"


@pytest.fixture(autouse=True)
def isolated_environment(tmp_path, monkeypatch):
    for name in list(os.environ):
        if name.startswith("DESKPAL_") or name in {"OPENAI_API_KEY", "LOCALAPPDATA", "XDG_CONFIG_HOME"}:
            monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local-app-data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
    from deskpal.credentials import WindowsCredentialStore
    def forbidden(*args, **kwargs):
        raise AssertionError("test attempted to access a real vault")
    monkeypatch.setattr(WindowsCredentialStore, "read", forbidden)
    monkeypatch.setattr(WindowsCredentialStore, "write", forbidden)
    monkeypatch.setattr(WindowsCredentialStore, "delete", forbidden)


def output(capsys):
    captured = capsys.readouterr()
    assert captured.err == ""
    return json.loads(captured.out)


@pytest.mark.parametrize("argument", ["--maintenance", "--maintenance=diagnostics"])
def test_application_cli_routes_maintenance_before_config_and_backend(tmp_path, monkeypatch, capsys, argument):
    from deskpal.application import cli as application_cli
    calls = []
    class FakeService:
        def __init__(self, config_path):
            assert config_path == tmp_path / "isolated.toml"

        def execute(self, operation, parameters):
            calls.append((operation, parameters))
            return {"backend_started": False, "network_requested": False}
    def forbidden(*args, **kwargs):
        raise AssertionError("maintenance route reached production config/factory")
    monkeypatch.setattr(cli, "MaintenanceService", FakeService)
    monkeypatch.setattr(application_cli, "load_config", forbidden)
    monkeypatch.setattr(application_cli, "create_application", forbidden)
    args = ["--config", str(tmp_path / "isolated.toml"), argument]
    if argument == "--maintenance":
        args.append("diagnostics")
    assert application_cli.main(args) == 0
    result = output(capsys)
    assert result["ok"] is True
    assert result["data"]["backend_started"] is False
    assert calls == [("diagnostics", {})]


@pytest.mark.parametrize("raw", [
    '{"values":{},"values":{"model":"duplicate"}}',
    '{"values":{"model":"first","model":"second"}}',
    '{"values":{"timeout_seconds":NaN}}',
    '{"values":{"timeout_seconds":Infinity}}',
    '[]', '{"credential_secret":"synthetic-cli-secret-never-display-9032"}',
    '{"values":',
])
def test_invalid_json_and_argv_credentials_are_rejected_before_service(raw, monkeypatch, capsys):
    def forbidden(*args, **kwargs):
        raise AssertionError("invalid JSON or argv secret reached maintenance service")
    monkeypatch.setattr(cli, "MaintenanceService", forbidden)
    assert cli.main(["--maintenance", "setup_save", "--maintenance-parameters", raw]) == 1
    result = output(capsys)
    assert result["ok"] is False
    assert result["error"]["code"] == "invalid_config"
    assert PRIVATE not in repr(result)


def test_oversized_cli_json_is_rejected_before_service(monkeypatch, capsys):
    monkeypatch.setattr(cli, "MaintenanceService", lambda *args: pytest.fail("oversized input reached service"))
    raw = json.dumps({"values": {"model": "x" * 131073}})
    assert cli.main(["--maintenance", "setup_save", "--maintenance-parameters", raw]) == 1
    assert output(capsys)["error"]["code"] == "invalid_config"


def test_non_tty_store_key_refuses_without_prompting_or_creating_service(monkeypatch, capsys):
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO("ignored synthetic input"))
    def forbidden(*args, **kwargs):
        raise AssertionError("non-TTY flow prompted for a key or constructed service")
    monkeypatch.setattr(getpass, "getpass", forbidden)
    monkeypatch.setattr(cli, "MaintenanceService", forbidden)
    assert cli.main(["--maintenance", "setup_save", "--store-api-key",
                     "--maintenance-parameters", '{"values":{},"expected_revision":"missing"}']) == 1
    result = output(capsys)
    assert result["ok"] is False
    assert result["error"]["code"] == "confirmation_required"


def test_hidden_tty_key_is_forwarded_once_and_removed_from_retained_parameters(tmp_path, monkeypatch, capsys):
    class Terminal(io.StringIO):
        def isatty(self):
            return True
    monkeypatch.setattr(cli.sys, "stdin", Terminal())
    monkeypatch.setattr(getpass, "getpass", lambda prompt: PRIVATE)
    captured = {}
    class FakeService:
        def __init__(self, path):
            assert path == tmp_path / "config.toml"

        def execute(self, operation, parameters):
            captured["secret"] = parameters["credential_secret"]
            captured["reference"] = parameters
            return {"credential_present": True, "credential_source": "system_vault"}
    monkeypatch.setattr(cli, "MaintenanceService", FakeService)
    assert cli.main(["--config", str(tmp_path / "config.toml"), "--maintenance", "setup_save",
                     "--store-api-key", "--maintenance-parameters",
                     '{"values":{},"expected_revision":"missing"}']) == 0
    result = output(capsys)
    assert captured["secret"] == PRIVATE
    assert "credential_secret" not in captured["reference"]
    assert result["data"]["credential_present"] is True
    assert PRIVATE not in repr(result)


def test_arbitrary_adapter_exception_is_a_fixed_safe_json_failure(tmp_path, monkeypatch, capsys):
    class BrokenService:
        def __init__(self, path):
            pass

        def execute(self, operation, parameters):
            raise RuntimeError(f"sensitive argument={PRIVATE}")
    monkeypatch.setattr(cli, "MaintenanceService", BrokenService)
    assert cli.main(["--config", str(tmp_path / "config.toml"), "--maintenance", "diagnostics"]) == 1
    result = output(capsys)
    assert result["ok"] is False
    assert result["error"]["code"] == "io_error"
    assert PRIVATE not in repr(result)


def test_missing_config_status_returns_json_without_backend_or_key_prompt(tmp_path, monkeypatch, capsys):
    def forbidden(*args, **kwargs):
        raise AssertionError("status attempted to prompt for credentials")
    monkeypatch.setattr(getpass, "getpass", forbidden)
    path = tmp_path / "missing-config.toml"
    assert cli.main(["--config", str(path), "--maintenance", "setup_status", "--json"]) == 0
    result = output(capsys)
    assert result["ok"] is True
    assert result["data"]["config_exists"] is False
    assert result["data"]["can_start"] is False
    assert not path.exists()
