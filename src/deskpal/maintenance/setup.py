"""Non-secret first-run configuration, revision checks and explicit repair."""

import copy
import hashlib
import json
import math
import os
import re
import tempfile
import tomllib
import uuid
from contextlib import ExitStack
from pathlib import Path
from typing import Any, Mapping

from dotenv import dotenv_values

from deskpal.application.instance import InstanceLock, InstanceLockError
from deskpal.config import AppConfig, load_config
from deskpal.credentials import CredentialStoreError, WindowsCredentialStore
from deskpal.llm.errors import LLMError
from deskpal.llm.url_policy import validate_base_url
from deskpal.memory.cache_lock import CacheLease, CacheLeaseError

from .cache import _safe
from .errors import MaintenanceError, message_for

FIELDS = {
    "provider": ("llm", "provider", "offline", "DESKPAL_LLM_PROVIDER"),
    "model": ("llm", "model", "", "DESKPAL_LLM_MODEL"),
    "base_url": ("llm", "base_url", "https://api.openai.com/v1", "DESKPAL_LLM_BASE_URL"),
    "api_key_env": ("llm", "api_key_env", "OPENAI_API_KEY", "DESKPAL_LLM_API_KEY_ENV"),
    "credential_ref": ("llm", "credential_ref", "", "DESKPAL_LLM_CREDENTIAL_REF"),
    "timeout_seconds": ("llm", "timeout_seconds", 30.0, "DESKPAL_LLM_TIMEOUT_SECONDS"),
    "expression_output_format": ("llm", "expression_output_format", "prompt", "DESKPAL_LLM_EXPRESSION_OUTPUT_FORMAT"),
    "workspace_dir": ("sandbox", "workspace_dir", "workspace", "DESKPAL_WORKSPACE_DIR"),
    "data_dir": ("storage", "data_dir", "data", "DESKPAL_DATA_DIR"),
    "embedding_provider": ("memory", "embedding_provider", "disabled", "DESKPAL_EMBEDDING_PROVIDER"),
    "model_cache_dir": ("memory", "model_cache_dir", "data/models/fastembed", "DESKPAL_MODEL_CACHE_DIR"),
}


def _patch(text: str, values: Mapping[str, Any]) -> str:
    """Retain all unrelated source text, including unknown tables and comments.

    Complex quoted/dotted forms are accepted only if a final parsed semantic
    comparison proves the desired change and preservation of every other key.
    """
    original = tomllib.loads(text)
    desired = copy.deepcopy(original)
    for name, value in values.items():
        section, key, _, _ = FIELDS[name]
        desired.setdefault(section, {})[key] = value
    lines = text.splitlines(keepends=True)
    for name, value in values.items():
        section, key, _, _ = FIELDS[name]
        assignment = f"{key} = {json.dumps(value, ensure_ascii=False)}\n"
        begin = None
        end = len(lines)
        for index, line in enumerate(lines):
            match = re.fullmatch(r"\s*\[([A-Za-z_][A-Za-z_0-9]*)\]\s*(?:#.*)?\r?\n?", line)
            if match and match[1] == section:
                begin = index + 1
                end = next((j for j in range(begin, len(lines)) if lines[j].lstrip().startswith("[")), len(lines))
                break
        if begin is None:
            if lines and not lines[-1].endswith("\n"):
                lines[-1] += "\n"
            lines.extend([f"\n[{section}]\n", assignment])
            continue
        found = False
        for index in range(begin, end):
            if re.match(r"\s*" + re.escape(key) + r"\s*=", lines[index]):
                stop = index + 1
                while True:
                    try:
                        tomllib.loads("".join(lines[index:stop]))
                        break
                    except tomllib.TOMLDecodeError:
                        if stop >= end:
                            raise MaintenanceError("invalid_config") from None
                        stop += 1
                lines[index:stop] = [assignment]
                found = True
                break
        if not found:
            if end and not lines[end - 1].endswith("\n"):
                lines[end - 1] += "\n"
            lines.insert(end, assignment)
    result = "".join(lines)
    if tomllib.loads(result) != desired:
        raise MaintenanceError("invalid_config")
    return result


class SetupService:
    def __init__(self, config_path: Path) -> None:
        self.config_path = Path(config_path).expanduser().absolute()

    def _read(self) -> tuple[bytes, str]:
        _safe(self.config_path)
        if not self.config_path.exists():
            return b"", "missing"
        if not self.config_path.is_file() or self.config_path.stat().st_size > 1024**2:
            raise MaintenanceError("invalid_config")
        data = self.config_path.read_bytes()
        return data, hashlib.sha256(data).hexdigest()

    @staticmethod
    def _validate_values(values: Mapping[str, Any]) -> None:
        if not isinstance(values, Mapping) or set(values) - set(FIELDS):
            raise MaintenanceError("invalid_config")
        for name, value in values.items():
            if name == "timeout_seconds":
                if isinstance(value, bool) or not isinstance(value, (float, int)):
                    raise MaintenanceError("invalid_config")
                try:
                    if not math.isfinite(value) or value <= 0:
                        raise MaintenanceError("invalid_config")
                except (OverflowError, ValueError, TypeError):
                    raise MaintenanceError("invalid_config") from None
            elif not isinstance(value, str) or len(value) > 2048 or any(ord(c) < 32 for c in value):
                raise MaintenanceError("invalid_config")
        if "base_url" in values:
            try:
                validate_base_url(values["base_url"])
            except LLMError:
                raise MaintenanceError("invalid_config") from None
        if "api_key_env" in values and re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*", values["api_key_env"]) is None:
            raise MaintenanceError("invalid_config")
        if values.get("credential_ref") and not WindowsCredentialStore.valid_ref(values["credential_ref"]):
            raise MaintenanceError("invalid_config")

    def _validate_text(self, text: str) -> AppConfig:
        parsed = tomllib.loads(text)
        fields: dict[str, Any] = {}
        for name, (section, key, default, _) in FIELDS.items():
            table = parsed.get(section, {})
            if not isinstance(table, dict):
                raise MaintenanceError("invalid_config")
            fields[name] = table.get(key, default)
        self._validate_values(fields)
        self._directories(fields)
        env = {str(fields["api_key_env"]): "validation-placeholder"}
        descriptor, name = tempfile.mkstemp(prefix=".deskpal-config-check-", suffix=".toml", dir=self.config_path.parent)
        candidate = Path(name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
                stream.write(text)
            config = load_config(candidate, environ=env)
        finally:
            candidate.unlink(missing_ok=True)
        return config

    def _directories(self, fields: Mapping[str, Any]) -> tuple[Path, Path, Path]:
        paths: list[Path] = []
        for field in ("data_dir", "workspace_dir", "model_cache_dir"):
            value = fields[field]
            if not isinstance(value, str) or not value.strip():
                raise MaintenanceError("invalid_config")
            raw = Path(value).expanduser()
            raw = raw if raw.is_absolute() else self.config_path.parent / raw
            _safe(raw)
            paths.append(raw.resolve())
        data, workspace, cache = paths
        for path in (data, workspace, cache):
            _safe(path)
            if path.exists() and not path.is_dir():
                raise MaintenanceError("unsafe_path")
        if (data == workspace or data in workspace.parents or workspace in data.parents
                or cache == data or cache in data.parents or cache == workspace
                or cache in workspace.parents or workspace in cache.parents):
            raise MaintenanceError("unsafe_path")
        return data, workspace, cache

    def _environment(self) -> dict[str, Any]:
        env_file = self.config_path.parent / ".env"
        env: dict[str, Any] = {}
        if env_file.is_file():
            if env_file.stat().st_size > 1024**2:
                raise MaintenanceError("invalid_config")
            env.update({key: value for key, value in dotenv_values(env_file, interpolate=False).items()
                        if value is not None})
        env.update(os.environ)
        return env

    def inspect(self) -> dict[str, Any]:
        try:
            content, revision = self._read()
            issues: list[str] = []
            try:
                raw = tomllib.loads(content.decode("utf-8"))
            except (ValueError, UnicodeError):
                raw = {}
                issues.append("invalid_config")
            values: dict[str, Any] = {}
            for name, (section, key, default, _) in FIELDS.items():
                table = raw.get(section, {})
                value = table.get(key, default) if isinstance(table, dict) else default
                try:
                    self._validate_values({name: value})
                    values[name] = value
                except (MaintenanceError, ValueError):
                    values[name] = default
                    if "invalid_config" not in issues:
                        issues.append("invalid_config")
            # Read environment files only for presence/source; never return their values.
            env = self._environment()
            sources = {name: "environment" if env_key in os.environ else "dotenv" if env_key in env
                       else "file" if revision != "missing" else "default"
                       for name, (_, _, _, env_key) in FIELDS.items()}
            if any(source in {"environment", "dotenv"} for source in sources.values()):
                issues.append("environment_override")
            env_name = env.get("DESKPAL_LLM_API_KEY_ENV", values["api_key_env"])
            present = bool(isinstance(env_name, str) and str(env.get(env_name, "") or "").strip())
            credential_source = "environment" if present else "missing"
            reference = env.get("DESKPAL_LLM_CREDENTIAL_REF", values["credential_ref"])
            if not present and reference and WindowsCredentialStore.valid_ref(reference) and WindowsCredentialStore.available():
                try:
                    present = bool(WindowsCredentialStore().read(reference))
                    if present:
                        credential_source = "system_vault"
                except CredentialStoreError:
                    issues.append("credential_store_unavailable")
            if revision == "missing":
                issues.append("config_missing")
            config_valid = False
            if revision != "missing" and "invalid_config" not in issues:
                try:
                    load_config(self.config_path, environ={str(values["api_key_env"]): "validation-placeholder"})
                    self._directories(values)
                    config_valid = True
                except Exception:
                    issues.append("invalid_config")
            if values["provider"] == "openai" and not present:
                issues.append("credential_missing")
            return {"values": values, "revision": revision, "config_exists": revision != "missing",
                    "config_valid": config_valid, "credential_present": present, "credential_source": credential_source,
                    "sources": sources, "issues": [{"code": code, "message": message_for(code)} for code in issues]}
        except MaintenanceError:
            raise
        except Exception:
            raise MaintenanceError("io_error") from None

    def save(self, values: Mapping[str, Any], *, expected_revision: str | None,
             credential_secret: str | None = None) -> dict[str, Any]:
        return self._save(values, expected_revision=expected_revision, repair=False,
                          credential_secret=credential_secret)

    def repair(self, values: Mapping[str, Any], *, expected_revision: str | None,
               confirm: bool, credential_secret: str | None = None) -> dict[str, Any]:
        if confirm is not True:
            raise MaintenanceError("confirmation_required")
        return self._save(values, expected_revision=expected_revision, repair=True,
                          credential_secret=credential_secret)

    def _save(self, values: Mapping[str, Any], *, expected_revision: str | None,
              repair: bool, credential_secret: str | None) -> dict[str, Any]:
        new_ref = None
        stage: Path | None = None
        published = False
        vault_attempted = False
        try:
            self._validate_values(values)
            if expected_revision is None:
                raise MaintenanceError("revision_conflict")
            if credential_secret is not None:
                WindowsCredentialStore.validate_secret(credential_secret)
                if not WindowsCredentialStore.available():
                    raise MaintenanceError("credential_store_unavailable")
            self.config_path.parent.mkdir(parents=True, exist_ok=True)
            with CacheLease(self.config_path.parent / ("." + self.config_path.name + ".setup"), exclusive=True):
                original, revision = self._read()
                if revision != expected_revision:
                    raise MaintenanceError("revision_conflict")
                try:
                    old_text = original.decode("utf-8")
                    old_raw = tomllib.loads(old_text)
                    old_readable = revision != "missing"
                except (UnicodeError, ValueError):
                    if not repair:
                        raise MaintenanceError("invalid_config") from None
                    old_text = ""
                    old_raw = {}
                    old_readable = False
                edited = dict(values)
                if credential_secret is not None:
                    new_ref = "Deskpal/LLM/" + uuid.uuid4().hex
                    edited["credential_ref"] = new_ref
                text = _patch("" if repair else old_text, edited)
                config = self._validate_text(text)
                data_dirs = {config.storage.data_dir}
                if old_readable:
                    old_storage = old_raw.get("storage", {})
                    old_dir = old_storage.get("data_dir", "data") if isinstance(old_storage, dict) else None
                    if isinstance(old_dir, str) and old_dir.strip():
                        old_path = Path(old_dir).expanduser()
                        old_path = old_path if old_path.is_absolute() else self.config_path.parent / old_path
                        _safe(old_path)
                        data_dirs.add(old_path.resolve())
                    elif not repair:
                        raise MaintenanceError("invalid_config")
                # Environment and sibling dotenv settings have the same priority
                # for both files, because the configuration root does not move.
                environment = self._environment()
                effective = {name: environment.get(env_key, getattr(
                    config.storage if name == "data_dir" else config.sandbox if name == "workspace_dir" else config.memory,
                    name)) for name, (_, _, _, env_key) in FIELDS.items()
                    if name in {"data_dir", "workspace_dir", "model_cache_dir"}}
                effective = {name: str(value) if isinstance(value, Path) else value for name, value in effective.items()}
                data_dirs.add(self._directories(effective)[0])
                with ExitStack() as stack:
                    for directory in sorted(data_dirs, key=lambda path: os.path.normcase(str(path))):
                        stack.enter_context(InstanceLock(directory))
                    if self._read()[1] != revision:
                        raise MaintenanceError("revision_conflict")
                    if credential_secret is not None and new_ref is not None:
                        vault_attempted = True
                        WindowsCredentialStore().write(new_ref, credential_secret)
                    descriptor, name = tempfile.mkstemp(prefix=".deskpal-config-", dir=self.config_path.parent)
                    stage = Path(name)
                    with os.fdopen(descriptor, "wb") as stream:
                        stream.write(text.encode("utf-8"))
                        stream.flush()
                        os.fsync(stream.fileno())
                    if revision != "missing":
                        backup = self.config_path.with_name(self.config_path.name + ".recovery-" + uuid.uuid4().hex)
                        with backup.open("xb") as stream:
                            stream.write(original)
                            stream.flush()
                            os.fsync(stream.fileno())
                    if self._read()[1] != revision:
                        raise MaintenanceError("revision_conflict")
                    if revision == "missing":
                        os.link(stage, self.config_path)  # no-overwrite atomic publication
                        published = True
                        try:
                            stage.unlink()
                        except OSError:
                            pass
                    else:
                        os.replace(stage, self.config_path)
                    stage = None
                    published = True
            result = self.inspect()
            result.update(configuration_reset=repair, local_recovery_copy=revision != "missing")
            return result
        except MaintenanceError:
            raise
        except (InstanceLockError, CacheLeaseError):
            raise MaintenanceError("busy") from None
        except CredentialStoreError:
            raise MaintenanceError("credential_store_unavailable") from None
        except (ValueError, TypeError):
            raise MaintenanceError("invalid_config") from None
        except Exception:
            raise MaintenanceError("io_error") from None
        finally:
            if stage is not None:
                try:
                    stage.unlink(missing_ok=True)
                except OSError:
                    pass
            if new_ref is not None and vault_attempted and not published:
                try:
                    WindowsCredentialStore().delete(new_ref)
                except CredentialStoreError:
                    pass
