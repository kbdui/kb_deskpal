"""TOML configuration with environment overrides; loading has no I/O side effects."""

import math
import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

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
class AppConfig:
    llm: LLMConfig = field(default_factory=LLMConfig)
    sandbox: SandboxConfig = field(default_factory=SandboxConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)
    agent: AgentConfig = field(default_factory=AgentConfig)
    scheduling: SchedulingConfig = field(default_factory=SchedulingConfig)
    screenshot: ScreenshotConfig = field(default_factory=ScreenshotConfig)


def load_config(path: str | Path | None = None, *, environ: Mapping[str, str] | None = None) -> AppConfig:
    """Priority: process environment > sibling .env > TOML > defaults.

    An explicit environ mapping is hermetic: no .env file is read. Environment
    files are parsed literally and never mutate the process environment.
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
    for name in ("llm", "sandbox", "storage", "agent", "scheduling", "screenshot"):
        section = raw.get(name, {})
        if not isinstance(section, dict):
            raise ConfigError(f"Configuration section {name} must be a table")
        sections[name] = section
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

    provider = string("llm", "provider", "offline", "DESKPAL_LLM_PROVIDER")
    model = string("llm", "model", "", "DESKPAL_LLM_MODEL")
    base_url = string("llm", "base_url", "https://api.openai.com/v1", "DESKPAL_LLM_BASE_URL").rstrip("/")
    api_key_env = string("llm", "api_key_env", "OPENAI_API_KEY", "DESKPAL_LLM_API_KEY_ENV")
    if provider not in {"offline", "openai"}:
        raise ConfigError("llm.provider must be offline or openai")
    if not api_key_env:
        raise ConfigError("llm.api_key_env must not be empty")
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", api_key_env) is None:
        raise ConfigError("llm.api_key_env must name a valid environment variable, not a credential")
    api_key = env.get(api_key_env, "") if provider == "openai" else ""
    if provider == "openai" and (not model or not api_key.strip()):
        raise ConfigError("OpenAI mode requires an explicit model and an API key environment variable")
    if provider == "openai" and not base_url.startswith(("https://", "http://")):
        raise ConfigError("llm.base_url must be an HTTP(S) URL")
    poll_interval = positive_number(
        "scheduling", "poll_interval_seconds", 1.0, "DESKPAL_SCHEDULING_POLL_INTERVAL_SECONDS")
    if poll_interval > 60:
        raise ConfigError("scheduling.poll_interval_seconds must not exceed 60")
    return AppConfig(
        llm=LLMConfig(provider, model, base_url, api_key_env,
                      positive_number("llm", "timeout_seconds", 30.0, "DESKPAL_LLM_TIMEOUT_SECONDS"), api_key),
        sandbox=SandboxConfig(directory("sandbox", "workspace_dir", "workspace", "DESKPAL_WORKSPACE_DIR"),
                              positive_number("sandbox", "max_file_bytes", 1_048_576, "DESKPAL_MAX_FILE_BYTES", True)),
        storage=StorageConfig(directory("storage", "data_dir", "data", "DESKPAL_DATA_DIR")),
        agent=AgentConfig(positive_number("agent", "max_tool_steps", 8, "DESKPAL_MAX_TOOL_STEPS", True)),
        scheduling=SchedulingConfig(poll_interval),
        screenshot=ScreenshotConfig(positive_number(
            "screenshot", "max_pixels", 16_000_000, "DESKPAL_SCREENSHOT_MAX_PIXELS", True)),
    )


__all__ = ["AppConfig", "LLMConfig", "SandboxConfig", "StorageConfig", "AgentConfig",
           "SchedulingConfig", "ScreenshotConfig", "ConfigError", "load_config"]

