"""Bounded, hermetic V0.3 memory configuration; no local .env/config reads."""

import pytest

from deskpal.config import ConfigError, load_config


def test_memory_defaults_are_opt_in_and_loading_has_no_storage_side_effect(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    config = load_config(environ={})
    assert config.memory.mode == "explicit_only"
    assert config.memory.embedding_provider == "disabled"
    assert config.memory.model_cache_dir == tmp_path / "data" / "models" / "fastembed"
    assert (config.memory.max_context_bytes, config.memory.max_candidates) == (8000, 5)
    assert config.memory.semantic_threshold == .55
    assert (config.memory.evaluation_timeout_seconds, config.memory.evaluation_max_output_tokens) == (15, 2048)
    assert not list(tmp_path.iterdir())


def test_memory_environment_overrides_toml_with_relative_model_cache(tmp_path):
    config_file = tmp_path / "settings.toml"
    config_file.write_text('[memory]\nmode="paused"\nembedding_provider="disabled"\n'
                           'max_context_bytes=99\nmax_candidates=1\nsemantic_threshold=0.4\n'
                           'evaluation_timeout_seconds=2\nevaluation_max_output_tokens=128\n', encoding="utf-8")
    config = load_config(config_file, environ={
        "DESKPAL_MEMORY_MODE": "candidate_review", "DESKPAL_EMBEDDING_PROVIDER": "fastembed",
        "DESKPAL_MODEL_CACHE_DIR": "cached-models", "DESKPAL_MEMORY_MAX_CONTEXT_BYTES": "65536",
        "DESKPAL_MEMORY_MAX_CANDIDATES": "20", "DESKPAL_MEMORY_SEMANTIC_THRESHOLD": "1",
        "DESKPAL_MEMORY_EVALUATION_TIMEOUT_SECONDS": "60", "DESKPAL_MEMORY_EVALUATION_MAX_OUTPUT_TOKENS": "8192",
    })
    memory = config.memory
    assert (memory.mode, memory.embedding_provider) == ("candidate_review", "fastembed")
    assert memory.model_cache_dir == tmp_path / "cached-models"
    assert (memory.max_context_bytes, memory.max_candidates, memory.semantic_threshold) == (65536, 20, 1)
    assert (memory.evaluation_timeout_seconds, memory.evaluation_max_output_tokens) == (60, 8192)
    assert not memory.model_cache_dir.exists()


@pytest.mark.parametrize("name,value", [
    ("DESKPAL_MEMORY_MODE", "automatic"), ("DESKPAL_MEMORY_MODE", ""),
    ("DESKPAL_EMBEDDING_PROVIDER", "cloud"), ("DESKPAL_MODEL_CACHE_DIR", " "),
    ("DESKPAL_MEMORY_MAX_CONTEXT_BYTES", "65537"), ("DESKPAL_MEMORY_MAX_CONTEXT_BYTES", "0"),
    ("DESKPAL_MEMORY_MAX_CANDIDATES", "21"), ("DESKPAL_MEMORY_MAX_CANDIDATES", "1.5"),
    ("DESKPAL_MEMORY_SEMANTIC_THRESHOLD", "1.001"), ("DESKPAL_MEMORY_SEMANTIC_THRESHOLD", "nan"),
    ("DESKPAL_MEMORY_SEMANTIC_THRESHOLD", "0"), ("DESKPAL_MEMORY_SEMANTIC_THRESHOLD", "-0.1"),
    ("DESKPAL_MEMORY_EVALUATION_TIMEOUT_SECONDS", "60.001"), ("DESKPAL_MEMORY_EVALUATION_TIMEOUT_SECONDS", "inf"),
    ("DESKPAL_MEMORY_EVALUATION_MAX_OUTPUT_TOKENS", "8193"), ("DESKPAL_MEMORY_EVALUATION_MAX_OUTPUT_TOKENS", "0"),
])
def test_invalid_memory_settings_are_rejected(name, value):
    with pytest.raises(ConfigError):
        load_config(environ={name: value})


@pytest.mark.parametrize("key", ["max_context_bytes", "max_candidates", "semantic_threshold",
                                "evaluation_timeout_seconds", "evaluation_max_output_tokens"])
def test_toml_boolean_is_never_a_numeric_memory_limit(tmp_path, key):
    config_file = tmp_path / "settings.toml"
    config_file.write_text(f"[memory]\n{key}=true\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(config_file, environ={})
