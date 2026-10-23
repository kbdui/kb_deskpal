"""TOML configuration with environment overrides; loading has no I/O side effects."""

import math
import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import dotenv_values


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class LLMConfig:
    provider: str = "offline"
    model: str = ""
    base_url: str = "https://api.openai.com/v1"
    api_key_env: str = "OPENAI_API_KEY"
    timeout_seconds: float = 30.0
    api_key: str = field(default="", repr=False)
    expression_output_format: str = "prompt"
    credential_ref: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.expression_output_format, str) or self.expression_output_format not in ("prompt", "json_object"):
            raise ConfigError("llm.expression_output_format must be prompt or json_object")


@dataclass(frozen=True)
class SandboxConfig:
    workspace_dir: Path = Path("workspace")
    max_file_bytes: int = 1_048_576


@dataclass(frozen=True)
class StorageConfig:
    data_dir: Path = Path("data")


@dataclass(frozen=True)
class AgentConfig:
    max_tool_steps: int = 8


@dataclass(frozen=True)
class SchedulingConfig:
    poll_interval_seconds: float = 1.0


@dataclass(frozen=True)
class ScreenshotConfig:
    max_pixels: int = 16_000_000


@dataclass(frozen=True)
class MemoryConfig:
    mode: str = "explicit_only"
    embedding_provider: str = "disabled"
    model_cache_dir: Path = Path("data/models/fastembed")
    max_context_bytes: int = 8000
    max_candidates: int = 5
    semantic_threshold: float = 0.55
    evaluation_timeout_seconds: float = 15.0
    evaluation_max_output_tokens: int = 2048


@dataclass(frozen=True)
class WorkflowConfig:
    queue_capacity: int = 100
    request_max_bytes: int = 65536
    plan_max_bytes: int = 262144
    result_max_bytes: int = 262144
    shutdown_timeout_seconds: float = 5.0


@dataclass(frozen=True)
class ProactiveConfig:
    enabled: bool = False
    poll_interval_seconds: float = 1.0
    event_batch_size: int = 100
    max_proposals_per_poll: int = 10
    proposal_ttl_seconds: int = 86400
    quiet_start: str = "22:00"
    quiet_end: str = "08:00"
    timezone: str = "Asia/Shanghai"


@dataclass(frozen=True)
class AppConfig:
    llm: LLMConfig = field(default_factory=LLMConfig)
    sandbox: SandboxConfig = field(default_factory=SandboxConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)
    agent: AgentConfig = field(default_factory=AgentConfig)
    scheduling: SchedulingConfig = field(default_factory=SchedulingConfig)
    screenshot: ScreenshotConfig = field(default_factory=ScreenshotConfig)
    memory: MemoryConfig = field(default_factory=MemoryConfig)
    workflow: WorkflowConfig = field(default_factory=WorkflowConfig)
    proactive: ProactiveConfig = field(default_factory=ProactiveConfig)


def load_config(path: str | Path | None = None, *, environ: Mapping[str, str] | None = None,
                provider_override: str | None = None) -> AppConfig:
    """Priority: process environment > sibling .env > TOML > defaults.

    An explicit environ mapping is hermetic: no .env file is read. Environment
    files are parsed literally and never mutate the process environment.
    Explicit local management can override only provider selection, preserving
    storage/env settings while requiring no API credential or network.
    """
    raw: dict[str, Any] = {}
    root = Path.cwd()
    if path is not None:
        config_path = Path(path).expanduser().resolve()
        root = config_path.parent
        try:
            with config_path.open("rb") as stream:
                raw = tomllib.load(stream)
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise ConfigError("Cannot read valid TOML configuration") from exc

    env: Mapping[str, str]
    if environ is not None:
        env = environ
    else:
        env_file = root / ".env"
        file_values: dict[str, str] = {}
        try:
            if env_file.is_file():
                file_values = {
                    key: value for key, value in dotenv_values(
                        env_file, interpolate=False, encoding="utf-8"
                    ).items() if value is not None
                }
        except (OSError, UnicodeError):
            raise ConfigError("Cannot read local environment configuration") from None
        env = {**file_values, **os.environ}

    sections: dict[str, dict[str, Any]] = {}
    for name in ("llm", "sandbox", "storage", "agent", "scheduling", "screenshot", "memory",
                 "workflow", "proactive"):
        section = raw.get(name, {})
        if not isinstance(section, dict):
            raise ConfigError(f"Configuration section {name} must be a table")
        sections[name] = section
    for name, allowed in (
        ("workflow", WorkflowConfig.__dataclass_fields__),
        ("proactive", ProactiveConfig.__dataclass_fields__),
    ):
        if set(sections[name]) - set(allowed):
            raise ConfigError(f"Configuration section {name} contains unsupported settings")
    if any(key.lower() in {"api_key", "token", "secret", "authorization"} for key in sections["llm"]):
        raise ConfigError("API credentials are allowed only in environment variables")

    def value(section: str, key: str, default: Any, env_key: str) -> Any:
        return env.get(env_key, sections[section].get(key, default))

    def string(section: str, key: str, default: str, env_key: str) -> str:
        result = value(section, key, default, env_key)
        if not isinstance(result, str):
            raise ConfigError(f"{section}.{key} must be a string")
        return result.strip()

    def positive_number(section: str, key: str, default: Any, env_key: str, integer: bool = False) -> Any:
        raw_value = value(section, key, default, env_key)
        result: int | float
        try:
            if isinstance(raw_value, bool):
                raise ValueError
            if integer:
                if isinstance(raw_value, float):
                    raise ValueError
                result = int(raw_value)
            else:
                result = float(raw_value)
            if result <= 0 or not math.isfinite(result):
                raise ValueError
            return result
        except (TypeError, ValueError, OverflowError):
            raise ConfigError(f"{section}.{key} must be a positive {'integer' if integer else 'number'}") from None

    def directory(section: str, key: str, default: str, env_key: str) -> Path:
        text = string(section, key, default, env_key)
        if not text:
            raise ConfigError(f"{section}.{key} must not be empty")
        candidate = Path(text).expanduser()
        return (candidate if candidate.is_absolute() else root / candidate).resolve()

    def bounded_number(section: str, key: str, default: int | float,
                       maximum: int | float, *, integer: bool = False) -> Any:
        env_key = f"DESKPAL_{section.upper()}_{key.upper()}"
        candidate = value(section, key, default, env_key)
        # TOML values are typed; only environment variables may contain numbers as text.
        if env_key not in env and (isinstance(candidate, bool) or not isinstance(candidate, (int, float))):
            raise ConfigError(f"{section}.{key} must be a {'integer' if integer else 'number'}")
        result = positive_number(section, key, default, env_key, integer)
        if result > maximum:
            raise ConfigError(f"{section}.{key} must not exceed {maximum}")
        return result

    def proactive_boolean() -> bool:
        env_key = "DESKPAL_PROACTIVE_ENABLED"
        candidate = value("proactive", "enabled", False, env_key)
        if env_key in env:
            if isinstance(candidate, str) and candidate.strip().lower() in {"true", "false"}:
                return candidate.strip().lower() == "true"
        elif isinstance(candidate, bool):
            return candidate
        raise ConfigError("proactive.enabled must be a boolean (true or false)")

    workflow = WorkflowConfig(
        queue_capacity=bounded_number("workflow", "queue_capacity", 100, 10000, integer=True),
        request_max_bytes=bounded_number("workflow", "request_max_bytes", 65536, 16777216, integer=True),
        plan_max_bytes=bounded_number("workflow", "plan_max_bytes", 262144, 16777216, integer=True),
        result_max_bytes=bounded_number("workflow", "result_max_bytes", 262144, 16777216, integer=True),
        shutdown_timeout_seconds=bounded_number("workflow", "shutdown_timeout_seconds", 5.0, 60),
    )
    quiet_start = string("proactive", "quiet_start", "22:00", "DESKPAL_PROACTIVE_QUIET_START")
    quiet_end = string("proactive", "quiet_end", "08:00", "DESKPAL_PROACTIVE_QUIET_END")
    for key, clock in (("quiet_start", quiet_start), ("quiet_end", quiet_end)):
        if re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", clock) is None:
            raise ConfigError(f"proactive.{key} must use HH:MM in 24-hour time")
    timezone = string("proactive", "timezone", "Asia/Shanghai", "DESKPAL_PROACTIVE_TIMEZONE")
    try:
        ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError):
        raise ConfigError("proactive.timezone must name an available IANA timezone") from None
    proactive = ProactiveConfig(
        enabled=proactive_boolean(),
        poll_interval_seconds=bounded_number("proactive", "poll_interval_seconds", 1.0, 60),
        event_batch_size=bounded_number("proactive", "event_batch_size", 100, 100, integer=True),
        max_proposals_per_poll=bounded_number("proactive", "max_proposals_per_poll", 10, 10, integer=True),
        proposal_ttl_seconds=bounded_number("proactive", "proposal_ttl_seconds", 86400, 2592000, integer=True),
        quiet_start=quiet_start, quiet_end=quiet_end, timezone=timezone,
    )

    provider = string("llm", "provider", "offline", "DESKPAL_LLM_PROVIDER")
    if provider_override is not None:
        if not isinstance(provider_override, str) or provider_override not in {"offline", "openai"}:
            raise ConfigError("Provider override must be offline or openai")
        provider = provider_override
    model = string("llm", "model", "", "DESKPAL_LLM_MODEL")
    base_url = string("llm", "base_url", "https://api.openai.com/v1", "DESKPAL_LLM_BASE_URL").rstrip("/")
    api_key_env = string("llm", "api_key_env", "OPENAI_API_KEY", "DESKPAL_LLM_API_KEY_ENV")
    expression_output_format = string("llm", "expression_output_format", "prompt", "DESKPAL_LLM_EXPRESSION_OUTPUT_FORMAT")
    if expression_output_format not in ("prompt", "json_object"):
        raise ConfigError("llm.expression_output_format must be prompt or json_object")
    if provider not in {"offline", "openai"}:
        raise ConfigError("llm.provider must be offline or openai")
    if not api_key_env:
        raise ConfigError("llm.api_key_env must not be empty")
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", api_key_env) is None:
        raise ConfigError("llm.api_key_env must name a valid environment variable, not a credential")
    api_key = env.get(api_key_env, "") if provider == "openai" else ""
    credential_ref = string("llm", "credential_ref", "", "DESKPAL_LLM_CREDENTIAL_REF")
    if credential_ref:
        from deskpal.credentials import WindowsCredentialStore
        if not WindowsCredentialStore.valid_ref(credential_ref):
            raise ConfigError("llm.credential_ref must name a Deskpal credential")
        if provider == "openai" and not api_key.strip() and environ is None:
            from deskpal.credentials import CredentialStoreError
            try:
                api_key = WindowsCredentialStore().read(credential_ref) or ""
            except CredentialStoreError:
                raise ConfigError("System credential storage is unavailable") from None
    if provider == "openai" and (not model or not api_key.strip()):
        raise ConfigError("OpenAI mode requires an explicit model and an API key environment variable or system credential reference")
    if provider == "openai" and not base_url.startswith(("https://", "http://")):
        raise ConfigError("llm.base_url must be an HTTP(S) URL")
    poll_interval = positive_number(
        "scheduling", "poll_interval_seconds", 1.0, "DESKPAL_SCHEDULING_POLL_INTERVAL_SECONDS")
    if poll_interval > 60:
        raise ConfigError("scheduling.poll_interval_seconds must not exceed 60")
    memory_mode = string("memory", "mode", "explicit_only", "DESKPAL_MEMORY_MODE")
    embedding_provider = string("memory", "embedding_provider", "disabled", "DESKPAL_EMBEDDING_PROVIDER")
    if memory_mode not in {"explicit_only", "candidate_review", "paused"}:
        raise ConfigError("memory.mode must be explicit_only, candidate_review, or paused")
    if embedding_provider not in {"disabled", "fastembed"}:
        raise ConfigError("memory.embedding_provider must be disabled or fastembed")
    max_context_bytes = positive_number(
        "memory", "max_context_bytes", 8000, "DESKPAL_MEMORY_MAX_CONTEXT_BYTES", True)
    max_candidates = positive_number("memory", "max_candidates", 5, "DESKPAL_MEMORY_MAX_CANDIDATES", True)
    threshold = positive_number("memory", "semantic_threshold", 0.55, "DESKPAL_MEMORY_SEMANTIC_THRESHOLD")
    evaluation_timeout = positive_number("memory", "evaluation_timeout_seconds", 15.0,
                                         "DESKPAL_MEMORY_EVALUATION_TIMEOUT_SECONDS")
    evaluation_tokens = positive_number("memory", "evaluation_max_output_tokens", 2048,
                                        "DESKPAL_MEMORY_EVALUATION_MAX_OUTPUT_TOKENS", True)
    if max_context_bytes > 65536 or max_candidates > 20 or threshold > 1:
        raise ConfigError("Memory limits must be <=65536 bytes, <=20 candidates, and threshold <=1")
    if evaluation_timeout > 60 or evaluation_tokens > 8192:
        raise ConfigError("Memory evaluation must use <=60 seconds and <=8192 output tokens")
    return AppConfig(
        llm=LLMConfig(provider, model, base_url, api_key_env,
                      positive_number("llm", "timeout_seconds", 30.0, "DESKPAL_LLM_TIMEOUT_SECONDS"), api_key,
                      expression_output_format, credential_ref),
        sandbox=SandboxConfig(directory("sandbox", "workspace_dir", "workspace", "DESKPAL_WORKSPACE_DIR"),
                              positive_number("sandbox", "max_file_bytes", 1_048_576, "DESKPAL_MAX_FILE_BYTES", True)),
        storage=StorageConfig(directory("storage", "data_dir", "data", "DESKPAL_DATA_DIR")),
        agent=AgentConfig(positive_number("agent", "max_tool_steps", 8, "DESKPAL_MAX_TOOL_STEPS", True)),
        scheduling=SchedulingConfig(poll_interval),
        screenshot=ScreenshotConfig(positive_number(
            "screenshot", "max_pixels", 16_000_000, "DESKPAL_SCREENSHOT_MAX_PIXELS", True)),
        memory=MemoryConfig(memory_mode, embedding_provider,
                            directory("memory", "model_cache_dir", "data/models/fastembed", "DESKPAL_MODEL_CACHE_DIR"),
                            max_context_bytes, max_candidates, threshold, evaluation_timeout, evaluation_tokens),
        workflow=workflow,
        proactive=proactive,
    )


__all__ = ["AppConfig", "LLMConfig", "SandboxConfig", "StorageConfig", "AgentConfig",
           "SchedulingConfig", "ScreenshotConfig", "MemoryConfig", "WorkflowConfig", "ProactiveConfig",
           "ConfigError", "load_config"]

