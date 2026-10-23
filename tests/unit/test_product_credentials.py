"""Vault integration contracts with a fake store; never touch system credentials."""

import os
from pathlib import Path

import pytest

from deskpal.config import ConfigError, load_config
from deskpal.credentials import CredentialStoreError, WindowsCredentialStore
from deskpal.maintenance.errors import MaintenanceError
from deskpal.maintenance.setup import SetupService

REF = "Deskpal/LLM/" + "a" * 32


@pytest.fixture(autouse=True)
def isolated_credential_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for name in list(os.environ):
        if name.startswith("DESKPAL_") or name == "OPENAI_API_KEY":
            monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("credential tests require an explicit fake vault adapter")

    for operation in ("read", "write", "delete"):
        monkeypatch.setattr(WindowsCredentialStore, operation, forbidden)


@pytest.mark.parametrize("reference", ["other-app/secret", "Deskpal/LLM/../../secret", "Deskpal/LLM/", ""])
def test_vault_reference_scope(reference: str) -> None:
    assert WindowsCredentialStore.valid_ref(reference) is False


@pytest.mark.parametrize("secret", ["", "\nsecret", "a\x00b", "a" * 1281])
def test_credential_validation_is_fixed_and_safe(secret: str) -> None:
    with pytest.raises(CredentialStoreError) as failure:
        WindowsCredentialStore.validate_secret(secret)
    assert str(failure.value) == "系统凭据库不可用，请使用外部环境变量配置凭据。"


def test_hermetic_config_and_offline_override_never_read_vault(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "deskpal.toml"
    path.write_text(f'[llm]\nprovider="openai"\nmodel="example"\ncredential_ref="{REF}"\n', encoding="utf-8")
    def forbidden(*args: object) -> str:
        raise AssertionError("vault must not be read")
    monkeypatch.setattr(WindowsCredentialStore, "read", forbidden)
    with pytest.raises(ConfigError):
        load_config(path, environ={})
    result = load_config(path, environ={}, provider_override="offline")
    assert result.llm.api_key == ""
    assert load_config(path, environ={"OPENAI_API_KEY": "fake-env"}).llm.api_key == "fake-env"


def test_environment_precedes_vault_and_repr_hides_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "deskpal.toml"
    path.write_text(f'[llm]\nprovider="openai"\nmodel="example"\ncredential_ref="{REF}"\n', encoding="utf-8")
    calls: list[str] = []
    def read(self: WindowsCredentialStore, reference: str) -> str:
        calls.append(reference)
        return "fake-vault-secret"
    monkeypatch.setattr(WindowsCredentialStore, "read", read)
    monkeypatch.setenv("OPENAI_API_KEY", "fake-environment-secret")
    assert load_config(path).llm.api_key == "fake-environment-secret"
    assert not calls
    monkeypatch.delenv("OPENAI_API_KEY")
    config = load_config(path)
    assert config.llm.api_key == "fake-vault-secret"
    assert calls == [REF]
    assert "fake-vault-secret" not in repr(config)


def test_setup_secret_stored_only_in_fake_vault_and_new_ref_rolled_back(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    stored: dict[str, str] = {}
    monkeypatch.setattr(WindowsCredentialStore, "available", staticmethod(lambda: True))
    monkeypatch.setattr(WindowsCredentialStore, "write", lambda self, ref, secret: stored.__setitem__(ref, secret))
    monkeypatch.setattr(WindowsCredentialStore, "read", lambda self, ref: stored.get(ref))
    monkeypatch.setattr(WindowsCredentialStore, "delete", lambda self, ref: stored.pop(ref, None))
    service = SetupService(tmp_path / "deskpal.toml")
    state = service.save({"provider": "openai", "model": "example"}, expected_revision="missing",
                         credential_secret="fake-vault-only")
    old_ref = state["values"]["credential_ref"]
    assert stored[old_ref] == "fake-vault-only"
    assert "fake-vault-only" not in repr(state)
    assert "fake-vault-only" not in service.config_path.read_text(encoding="utf-8")
    assert not (tmp_path / ".env").exists()
    def fail_replace(*args: object) -> None:
        raise OSError("unsafe-private-error")
    monkeypatch.setattr("deskpal.maintenance.setup.os.replace", fail_replace)
    with pytest.raises(MaintenanceError) as failure:
        service.save({}, expected_revision=state["revision"], credential_secret="new-fake-secret")
    assert "unsafe-private-error" not in str(failure.value)
    assert stored == {old_ref: "fake-vault-only"}
    assert old_ref in service.config_path.read_text(encoding="utf-8")
