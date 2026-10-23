"""Prepared offline setup coverage; uses only temporary configuration paths."""

import hashlib
import os
import tomllib
from pathlib import Path

import pytest

from deskpal.application.instance import InstanceLock
from deskpal.credentials import WindowsCredentialStore
from deskpal.maintenance.errors import MaintenanceError
from deskpal.maintenance.setup import SetupService


@pytest.fixture(autouse=True)
def isolated_setup_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for name in list(os.environ):
        if name.startswith("DESKPAL_") or name == "OPENAI_API_KEY":
            monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("setup tests must not access the system credential vault")

    for operation in ("read", "write", "delete"):
        monkeypatch.setattr(WindowsCredentialStore, operation, forbidden)


def test_first_save_cas_and_safe_inspection(tmp_path: Path) -> None:
    service = SetupService(tmp_path / "deskpal.toml")
    assert service.inspect()["revision"] == "missing"
    result = service.save({"provider": "offline"}, expected_revision="missing")
    assert result["config_valid"] is True
    assert result["values"]["provider"] == "offline"
    assert result["revision"] == hashlib.sha256((tmp_path / "deskpal.toml").read_bytes()).hexdigest()
    before = (tmp_path / "deskpal.toml").read_bytes()
    with pytest.raises(MaintenanceError, match="内容已变化"):
        service.save({"model": "stale"}, expected_revision="missing")
    assert (tmp_path / "deskpal.toml").read_bytes() == before


def test_update_preserves_unknown_text_and_original_backup(tmp_path: Path) -> None:
    path = tmp_path / "deskpal.toml"
    original = '# retained comment\n[llm]\nprovider="offline"\nextra = [1, 2]\n[custom]\nhello="world"\n'
    path.write_text(original, encoding="utf-8")
    original_bytes = path.read_bytes()
    service = SetupService(path)
    service.save({"timeout_seconds": 12.0}, expected_revision=service.inspect()["revision"])
    text = path.read_text(encoding="utf-8")
    assert '# retained comment\n' in text
    assert 'extra = [1, 2]\n' in text
    assert '[custom]\nhello="world"\n' in text
    expected = tomllib.loads(original)
    expected["llm"]["timeout_seconds"] = 12.0
    assert tomllib.loads(text) == expected
    assert next(tmp_path.glob("deskpal.toml.recovery-*")).read_bytes() == original_bytes


def test_bad_toml_requires_explicit_repair_and_keeps_backup(tmp_path: Path) -> None:
    path = tmp_path / "deskpal.toml"
    original = b'[llm\napi_key="private-do-not-echo"'
    path.write_bytes(original)
    service = SetupService(path)
    state = service.inspect()
    assert state["config_valid"] is False
    assert "private-do-not-echo" not in repr(state)
    with pytest.raises(MaintenanceError):
        service.save({"provider": "offline"}, expected_revision=state["revision"])
    assert path.read_bytes() == original
    with pytest.raises(MaintenanceError):
        service.repair({}, expected_revision=state["revision"], confirm=False)
    service.repair({"provider": "offline"}, expected_revision=state["revision"], confirm=True)
    assert next(tmp_path.glob("deskpal.toml.recovery-*")).read_bytes() == original


@pytest.mark.parametrize("values", [
    {"api_key": "private"}, {"api_key_env": "bad-key-value"},
    {"base_url": "https://name:password@example.com/v1"},
    {"base_url": "https://example.com/v1?api_key=private"},
    {"provider": "unsupported"}, {"timeout_seconds": True},
    {"data_dir": "workspace/private"}, {"model_cache_dir": "."},
])
def test_invalid_or_secret_fields_cannot_publish(tmp_path: Path, values: dict[str, object]) -> None:
    service = SetupService(tmp_path / "deskpal.toml")
    with pytest.raises(MaintenanceError) as failure:
        service.save(values, expected_revision="missing")
    assert "private" not in str(failure.value)
    assert not (tmp_path / "deskpal.toml").exists()


@pytest.mark.parametrize("timeout", [float("nan"), float("inf"), float("-inf"), -1, 0, 10 ** 400])
def test_non_positive_non_finite_or_overflowing_timeout_cannot_publish(tmp_path: Path, timeout: float | int) -> None:
    service = SetupService(tmp_path / "deskpal.toml")
    with pytest.raises(MaintenanceError) as failure:
        service.save({"provider": "offline", "timeout_seconds": timeout}, expected_revision="missing")
    assert failure.value.code == "invalid_config"
    assert not service.config_path.exists()
    assert not list(tmp_path.glob("deskpal.toml.recovery-*"))


def test_busy_original_data_dir_prevents_path_change(tmp_path: Path) -> None:
    service = SetupService(tmp_path / "deskpal.toml")
    state = service.save({"provider": "offline"}, expected_revision="missing")
    before = service.config_path.read_bytes()
    with InstanceLock(tmp_path / "data"):
        with pytest.raises(MaintenanceError) as failure:
            service.save({"data_dir": "new-data"}, expected_revision=state["revision"])
    assert failure.value.code == "busy"
    assert service.config_path.read_bytes() == before


def test_environment_source_is_visible_without_echoing_values(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DESKPAL_LLM_BASE_URL", "https://name:environment-private@example.com")
    service = SetupService(tmp_path / "deskpal.toml")
    state = service.inspect()
    assert state["sources"]["base_url"] == "environment"
    assert "environment-private" not in repr(state)


def test_inspection_redacts_invalid_file_url_and_env_name(tmp_path: Path) -> None:
    path = tmp_path / "deskpal.toml"
    path.write_text('[llm]\nbase_url="https://user:private@example.com"\napi_key_env="private-key-pasted"', encoding="utf-8")
    state = SetupService(path).inspect()
    assert "private" not in repr(state)
    assert state["config_valid"] is False


@pytest.mark.parametrize("source", ["environment", "dotenv"])
def test_effective_data_directory_instance_blocks_save(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, source: str) -> None:
    service = SetupService(tmp_path / "deskpal.toml")
    state = service.save({"provider": "offline"}, expected_revision="missing")
    effective = tmp_path / "effective-data"
    if source == "environment":
        monkeypatch.setenv("DESKPAL_DATA_DIR", str(effective))
    else:
        monkeypatch.delenv("DESKPAL_DATA_DIR", raising=False)
        (tmp_path / ".env").write_text("DESKPAL_DATA_DIR=effective-data\n", encoding="utf-8")
    before = service.config_path.read_bytes()
    with InstanceLock(effective):
        with pytest.raises(MaintenanceError) as failure:
            service.save({"data_dir": "new-data"}, expected_revision=state["revision"])
    assert failure.value.code == "busy"
    assert service.config_path.read_bytes() == before


def test_semantically_invalid_toml_can_be_explicitly_reset(tmp_path: Path) -> None:
    path = tmp_path / "deskpal.toml"
    original = b'[storage]\ndata_dir="old-data"\n[workflow]\nunsupported=true\n[llm]\napi_key="misplaced-private-key"\n'
    path.write_bytes(original)
    service = SetupService(path)
    state = service.inspect()
    assert not state["config_valid"]
    with pytest.raises(MaintenanceError):
        service.save({"provider": "offline"}, expected_revision=state["revision"])
    with InstanceLock(tmp_path / "old-data"):
        with pytest.raises(MaintenanceError) as failure:
            service.repair({"provider": "offline", "data_dir": "new-data"},
                           expected_revision=state["revision"], confirm=True)
    assert failure.value.code == "busy"
    result = service.repair({"provider": "offline", "data_dir": "new-data"},
                            expected_revision=state["revision"], confirm=True)
    assert result["config_valid"]
    assert result["configuration_reset"] is True
    assert result["local_recovery_copy"] is True
    assert next(tmp_path.glob("deskpal.toml.recovery-*")).read_bytes() == original
    assert "misplaced-private-key" not in repr(result)


@pytest.mark.parametrize("address", ["http://example.com/v1", "http://192.168.1.10/v1",
                                     "https://example.com:invalid/v1", "https://example.com:0/v1",
                                     "https://example.com:65536/v1"])
def test_setup_rejects_provider_invalid_urls_and_redacts_inspection(tmp_path: Path, address: str) -> None:
    path = tmp_path / "deskpal.toml"
    path.write_text('[llm]\nbase_url="' + address + '"\n', encoding="utf-8")
    service = SetupService(path)
    state = service.inspect()
    assert state["config_valid"] is False
    assert address not in repr(state)
    with pytest.raises(MaintenanceError) as failure:
        service.save({"base_url": address}, expected_revision=state["revision"])
    assert failure.value.code == "invalid_config"


@pytest.mark.parametrize("address", ["http://localhost:8080/v1", "http://127.0.0.1:8000/v1",
                                     "http://[::1]:8080/v1", "https://example.com:443/v1"])
def test_setup_accepts_same_loopback_and_https_policy_as_provider(tmp_path: Path, address: str) -> None:
    from deskpal.llm.providers import OpenAILLMProvider
    from deskpal.llm.url_policy import validate_base_url
    service = SetupService(tmp_path / "deskpal.toml")
    state = service.save({"provider": "offline", "base_url": address}, expected_revision="missing")
    assert state["config_valid"] is True
    assert validate_base_url(address + "/") == address
    assert OpenAILLMProvider._validate_url(address + "/") == address
