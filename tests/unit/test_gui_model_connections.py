"""Prepared D3 coverage: isolated fixtures only; no network or real vault access."""

import io
import json
import os
import urllib.request
from pathlib import Path
from typing import Any

import pytest

from deskpal.config import LLMConfig, load_config
from deskpal.credentials import CredentialStoreError, WindowsCredentialStore
from deskpal.llm.errors import LLMError
from deskpal.llm.providers import OpenAILLMProvider, UnavailableLLMProvider
from deskpal.llm.url_policy import validate_connection_url
from deskpal.maintenance.errors import MaintenanceError
from deskpal.maintenance.service import MaintenanceService
from deskpal.maintenance.setup import SetupService


@pytest.fixture(autouse=True)
def isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    for key in tuple(os.environ):
        if key.startswith("DESKPAL_") or key == "OPENAI_API_KEY":
            monkeypatch.delenv(key, raising=False)
    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("Real transport and vault access are forbidden")
    monkeypatch.setattr(urllib.request.OpenerDirector, "open", forbidden)
    for operation in ("read", "write", "delete"):
        monkeypatch.setattr(WindowsCredentialStore, operation, forbidden)


def test_new_cloud_is_unconfigured_and_missing_path_preserves_root(tmp_path: Path) -> None:
    path = tmp_path / "new" / "config.toml"
    config = load_config(path, environ={}, allow_missing=True)
    assert config.llm.provider == "openai"
    assert config.llm.is_cloud and not config.llm.ready
    assert config.llm.credential_mode == "system_vault"
    assert config.configuration_revision == "missing"
    assert config.storage.data_dir == path.parent / "data"
    assert not path.exists()
    with pytest.raises(LLMError, match="尚未配置"):
        UnavailableLLMProvider().generate("hello")


def test_explicit_offline_remains_offline(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text('[llm]\nprovider="offline"\n', encoding="utf-8")
    config = load_config(path, environ={})
    assert config.llm.ready and not config.llm.is_cloud


def test_legacy_environment_wins_over_existing_reference(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "config.toml"
    path.write_text('[llm]\nprovider="openai"\nmodel="sample"\ncredential_ref="Deskpal/LLM/' + "a" * 32 + '"\n', encoding="utf-8")
    monkeypatch.setenv("OPENAI_API_KEY", "fixture-key")
    config = load_config(path)
    assert config.llm.ready and config.llm.credential_source == "environment"
    status = SetupService(path).inspect()
    assert status["values"]["credential_mode"] == "environment"
    assert status["credential_source"] == "environment"
    assert "fixture-key" not in repr(status)


def test_explicit_vault_does_not_fall_back_to_environment(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text('[llm]\nprovider="openai"\nmodel="sample"\ncredential_mode="system_vault"\n', encoding="utf-8")
    config = load_config(path, environ={"OPENAI_API_KEY": "fixture-key"})
    assert not config.llm.ready and config.llm.credential_source == "missing"


@pytest.mark.parametrize("endpoint", ["https://example.org/v1", "http://192.168.1.2/v1", "http://127.0.0.1.evil/v1"])
def test_local_rejects_remote_hosts(endpoint: str) -> None:
    with pytest.raises(LLMError):
        validate_connection_url(endpoint, "local")


def test_local_avoids_dns_proxy_authorization_and_redirect(monkeypatch: pytest.MonkeyPatch) -> None:
    assert validate_connection_url("http://localhost:1234/v1", "local") == "http://127.0.0.1:1234/v1"
    observed: list[Any] = []
    class Opener:
        def open(self, request: urllib.request.Request, *, timeout: float) -> Any:
            observed.append(request)
            assert request.get_header("Authorization") is None
            assert request.full_url == "http://127.0.0.1:1234/v1/responses"
            return io.BytesIO(json.dumps({"output": [{"type": "message", "content": [{"type": "output_text", "text": "OK"}]}]}).encode())
    def build(*handlers: Any) -> Opener:
        assert any(isinstance(handler, urllib.request.ProxyHandler) and handler.proxies == {} for handler in handlers)
        redirect = next(handler for handler in handlers if isinstance(handler, urllib.request.HTTPRedirectHandler))
        assert redirect.redirect_request(None, None, 302, "", {}, "https://example.org") is None
        return Opener()
    monkeypatch.setattr(urllib.request, "build_opener", build)
    provider = OpenAILLMProvider(LLMConfig(model="sample", base_url="http://localhost:1234/v1",
        execution_location="local", credential_mode="none"))
    assert provider.generate("public fixture") == "OK"
    assert len(observed) == 1


def test_credential_replace_keep_remove_and_cas_rollback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    values: dict[str, str] = {}
    monkeypatch.setattr(WindowsCredentialStore, "available", staticmethod(lambda: True))
    monkeypatch.setattr(WindowsCredentialStore, "write", lambda self, ref, secret: values.__setitem__(ref, secret))
    monkeypatch.setattr(WindowsCredentialStore, "read", lambda self, ref: values.get(ref))
    monkeypatch.setattr(WindowsCredentialStore, "delete", lambda self, ref: values.pop(ref, None))
    path = tmp_path / "config.toml"
    setup = SetupService(path)
    first = setup.save({"provider": "openai", "model": "sample", "credential_mode": "system_vault"},
        expected_revision="missing", credential_action="replace", credential_secret="fixture-secret")
    reference = first["values"]["credential_ref"]
    assert first["saved"] and first["requires_restart"] and first["active_revision"] is None
    assert "fixture-secret" not in repr(first) and "fixture-secret" not in path.read_text()
    original = path.read_bytes()
    with pytest.raises(MaintenanceError):
        setup.save({"model": "other"}, expected_revision="missing", credential_secret="unused-secret")
    assert path.read_bytes() == original and list(values.values()) == ["fixture-secret"]
    kept = setup.save({"model": "other"}, expected_revision=first["revision"], credential_action="keep")
    assert kept["values"]["credential_ref"] == reference
    with pytest.raises(MaintenanceError) as changed:
        setup.save({"base_url": "https://different.example/v1"}, expected_revision=kept["revision"])
    assert changed.value.code == "credential_target_changed"
    removed = setup.save({}, expected_revision=kept["revision"], credential_action="remove")
    assert removed["values"]["credential_ref"] == "" and reference not in values
    assert "fixture-secret" not in repr(removed)


def test_vault_write_failure_keeps_previous_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "config.toml"
    setup = SetupService(path)
    initial = setup.save({"provider": "offline"}, expected_revision="missing")
    before = path.read_bytes()
    monkeypatch.setattr(WindowsCredentialStore, "available", staticmethod(lambda: True))
    def fail(self: Any, reference: str, secret: str) -> None:
        raise CredentialStoreError()
    monkeypatch.setattr(WindowsCredentialStore, "write", fail)
    monkeypatch.setattr(WindowsCredentialStore, "delete", lambda self, reference: None)
    with pytest.raises(MaintenanceError):
        setup.save({"credential_mode": "system_vault"}, expected_revision=initial["revision"],
                   credential_action="replace", credential_secret="fixture-secret")
    assert path.read_bytes() == before


def test_connection_probe_requires_explicit_confirmation(tmp_path: Path) -> None:
    with pytest.raises(MaintenanceError) as error:
        MaintenanceService(tmp_path / "config.toml").execute("connection_test", {"expected_revision": "missing"})
    assert error.value.code == "confirmation_required"


def test_missing_connection_allows_local_browsing_without_false_readiness(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    setup = SetupService(path)
    setup.save({"model": ""}, expected_revision="missing")
    status = MaintenanceService(path).execute("setup_status", {})
    assert status["can_start"] is True
    assert status["llm_ready"] is False and status["configured"] is False
    assert status["effective"]["credential_mode"] == "system_vault"


def test_failed_credential_removal_retains_safe_cleanup_intent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(WindowsCredentialStore, "available", staticmethod(lambda: True))
    monkeypatch.setattr(WindowsCredentialStore, "write", lambda self, reference, secret: None)
    monkeypatch.setattr(WindowsCredentialStore, "read", lambda self, reference: "fixture-secret")
    path = tmp_path / "config.toml"
    setup = SetupService(path)
    first = setup.save({"model": "sample"}, expected_revision="missing", credential_secret="fixture-secret")
    reference = first["values"]["credential_ref"]
    def fail(self: Any, reference: str) -> None:
        raise CredentialStoreError()
    monkeypatch.setattr(WindowsCredentialStore, "delete", fail)
    result = setup.save({}, expected_revision=first["revision"], credential_action="remove")
    assert result["saved"] and result["credential_cleanup_pending"]
    assert result["credential_cleanup_refs"] == [reference]
    assert result["values"]["credential_ref"] == ""
    queue = path.with_name("." + path.name + ".credential-cleanup.json").read_text()
    assert reference in queue and "fixture-secret" not in queue
