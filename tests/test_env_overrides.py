"""Tests for TRADINGAGENTS_* env-var overlay onto DEFAULT_CONFIG."""

from __future__ import annotations

import os

import pytest

import tradingagents.default_config as default_config_module


def _config_with_env(monkeypatch, **overrides):
    """The defaults as the environment given here would set them."""
    for key in list(default_config_module._ENV_OVERRIDES):
        monkeypatch.delenv(key, raising=False)
    for key, val in overrides.items():
        monkeypatch.setenv(key, val)
    return default_config_module.build_default_config()


def test_no_env_uses_built_in_defaults(monkeypatch):
    config = _config_with_env(monkeypatch)
    assert config["llm_provider"] == "openai"
    assert config["deep_think_llm"] == "gpt-6-sol"
    assert config["quick_think_llm"] == "gpt-6-luna"
    assert config["backend_url"] is None
    assert config["max_debate_rounds"] == 1
    assert config["checkpoint_enabled"] is False
    assert config["save_report"] is True


def test_string_overrides(monkeypatch):
    config = _config_with_env(
        monkeypatch,
        TRADINGAGENTS_LLM_PROVIDER="google",
        TRADINGAGENTS_DEEP_THINK_LLM="gemini-3-pro-preview",
        TRADINGAGENTS_QUICK_THINK_LLM="gemini-3-flash-preview",
        TRADINGAGENTS_LLM_BACKEND_URL="https://example.invalid/v1",
        TRADINGAGENTS_OUTPUT_LANGUAGE="Chinese",
    )
    assert config["llm_provider"] == "google"
    assert config["deep_think_llm"] == "gemini-3-pro-preview"
    assert config["quick_think_llm"] == "gemini-3-flash-preview"
    assert config["backend_url"] == "https://example.invalid/v1"
    assert config["output_language"] == "Chinese"


def test_int_coercion(monkeypatch):
    config = _config_with_env(
        monkeypatch,
        TRADINGAGENTS_MAX_DEBATE_ROUNDS="3",
        TRADINGAGENTS_MAX_RISK_ROUNDS="2",
    )
    assert config["max_debate_rounds"] == 3
    assert isinstance(config["max_debate_rounds"], int)
    assert config["max_risk_discuss_rounds"] == 2
    assert isinstance(config["max_risk_discuss_rounds"], int)


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("true", True), ("True", True), ("1", True), ("yes", True), ("on", True),
        ("false", False), ("False", False), ("0", False), ("no", False), ("off", False),
    ],
)
@pytest.mark.parametrize("env_var,key", [
    ("TRADINGAGENTS_CHECKPOINT_ENABLED", "checkpoint_enabled"),
    ("TRADINGAGENTS_SAVE_REPORT", "save_report"),
])
def test_bool_coercion(monkeypatch, raw, expected, env_var, key):
    config = _config_with_env(monkeypatch, **{env_var: raw})
    assert config[key] is expected


def test_reasoning_thinking_overrides(monkeypatch):
    """The provider reasoning/thinking knobs are env-configurable (non-interactive runs)."""
    config = _config_with_env(
        monkeypatch,
        TRADINGAGENTS_OPENAI_REASONING_EFFORT="high",
        TRADINGAGENTS_GOOGLE_THINKING_LEVEL="minimal",
        TRADINGAGENTS_ANTHROPIC_EFFORT="low",
    )
    assert config["openai_reasoning_effort"] == "high"
    assert config["google_thinking_level"] == "minimal"
    assert config["anthropic_effort"] == "low"


def test_reasoning_effort_defaults_to_none(monkeypatch):
    """Unset reasoning/thinking knobs stay None so each provider uses its own default."""
    config = _config_with_env(monkeypatch)
    assert config["openai_reasoning_effort"] is None
    assert config["google_thinking_level"] is None
    assert config["anthropic_effort"] is None


def test_empty_env_value_is_passthrough(monkeypatch):
    """Empty TRADINGAGENTS_* values must not clobber the built-in default."""
    config = _config_with_env(
        monkeypatch,
        TRADINGAGENTS_LLM_PROVIDER="",
        TRADINGAGENTS_MAX_DEBATE_ROUNDS="",
        TRADINGAGENTS_SAVE_REPORT="",
    )
    assert config["llm_provider"] == "openai"
    assert config["max_debate_rounds"] == 1
    assert config["save_report"] is True


def test_empty_path_value_keeps_the_default_path(monkeypatch):
    """.env.example lists the path variables blank; uncommenting one made the
    path empty, and the graph failed creating its directories."""
    config = _config_with_env(
        monkeypatch,
        TRADINGAGENTS_RESULTS_DIR="",
        TRADINGAGENTS_CACHE_DIR="",
        TRADINGAGENTS_MEMORY_LOG_PATH="",
    )
    home = default_config_module._TRADINGAGENTS_HOME
    assert config["results_dir"] == os.path.join(home, "logs")
    assert config["data_cache_dir"] == os.path.join(home, "cache")
    assert config["memory_log_path"] == os.path.join(home, "memory", "trading_memory.md")


def test_invalid_int_raises(monkeypatch):
    """Garbage int values should surface a ValueError at import, not silently misconfigure."""
    monkeypatch.setenv("TRADINGAGENTS_MAX_DEBATE_ROUNDS", "not-a-number")
    with pytest.raises(ValueError, match="TRADINGAGENTS_MAX_DEBATE_ROUNDS"):
        default_config_module.build_default_config()


@pytest.mark.parametrize("bad", ["treu", "flase", "maybe", "2", "enabled"])
@pytest.mark.parametrize("env_var", ["TRADINGAGENTS_CHECKPOINT_ENABLED", "TRADINGAGENTS_SAVE_REPORT"])
def test_invalid_bool_raises(monkeypatch, bad, env_var):
    """A misspelled boolean must fail loudly (like ints) instead of silently False."""
    monkeypatch.setenv(env_var, bad)
    with pytest.raises(ValueError, match=env_var):
        default_config_module.build_default_config()


def test_unknown_env_var_is_ignored(monkeypatch):
    """Env vars outside _ENV_OVERRIDES must not bleed into DEFAULT_CONFIG."""
    config = _config_with_env(
        monkeypatch,
        TRADINGAGENTS_NONEXISTENT_KEY="oops",
    )
    assert "nonexistent_key" not in config


def test_export_and_search_defaults(monkeypatch):
    config = _config_with_env(monkeypatch)
    assert config["save_report"] is True
    assert config["duckduckgo_news_max_queries"] == 2
    assert config["duckduckgo_news_min_interval"] == 3.0
    assert config["duckduckgo_news_allowed_domains"] == []


def test_search_env_types_and_export_boolean(monkeypatch):
    config = _config_with_env(
        monkeypatch,
        TRADINGAGENTS_SAVE_REPORT="false",
        TRADINGAGENTS_DUCKDUCKGO_NEWS_MAX_QUERIES="3",
        TRADINGAGENTS_DUCKDUCKGO_NEWS_MAX_RESULTS="8",
        TRADINGAGENTS_DUCKDUCKGO_NEWS_TIMEOUT="12.5",
        TRADINGAGENTS_DUCKDUCKGO_NEWS_TOTAL_TIMEOUT="40.5",
        TRADINGAGENTS_DUCKDUCKGO_NEWS_MIN_INTERVAL="2.5",
        TRADINGAGENTS_DUCKDUCKGO_NEWS_CACHE_TTL="120",
        TRADINGAGENTS_DUCKDUCKGO_NEWS_REGION="cn-zh",
        TRADINGAGENTS_DUCKDUCKGO_NEWS_ALLOWED_DOMAINS=" Reuters.com, apnews.com,reuters.com ",
    )
    assert config["save_report"] is False
    for key, value in {"max_queries": 3, "max_results": 8, "cache_ttl": 120}.items():
        assert config[f"duckduckgo_news_{key}"] == value
        assert isinstance(config[f"duckduckgo_news_{key}"], int)
    for key, value in {"timeout": 12.5, "total_timeout": 40.5, "min_interval": 2.5}.items():
        assert config[f"duckduckgo_news_{key}"] == value
        assert isinstance(config[f"duckduckgo_news_{key}"], float)
    assert config["duckduckgo_news_region"] == "cn-zh"
    assert config["duckduckgo_news_allowed_domains"] == ["reuters.com", "apnews.com"]


@pytest.mark.parametrize("suffix,bad", [
    ("MAX_QUERIES", "2.5"), ("MAX_RESULTS", "no"), ("CACHE_TTL", "1.5"),
    ("TIMEOUT", "slow"), ("TOTAL_TIMEOUT", "nan"), ("MIN_INTERVAL", "inf"),
    ("ALLOWED_DOMAINS", "reuters.com,"), ("ALLOWED_DOMAINS", "reuters.com,,apnews.com"),
    ("ALLOWED_DOMAINS", "https://reuters.com"), ("ALLOWED_DOMAINS", "*.reuters.com"),
    ("ALLOWED_DOMAINS", '["reuters.com"]'), ("ALLOWED_DOMAINS", " "),
    ("ALLOWED_DOMAINS", ","), ("ALLOWED_DOMAINS", ",".join(["reuters.com"] * 41)),
])
def test_invalid_search_env_fails_loudly(monkeypatch, suffix, bad):
    name = f"TRADINGAGENTS_DUCKDUCKGO_NEWS_{suffix}"
    with pytest.raises(ValueError, match=name):
        _config_with_env(monkeypatch, **{name: bad})


def test_blank_search_list_preserves_default(monkeypatch):
    config = _config_with_env(monkeypatch, TRADINGAGENTS_DUCKDUCKGO_NEWS_ALLOWED_DOMAINS="")
    assert config["duckduckgo_news_allowed_domains"] == []


@pytest.mark.parametrize("suffix,bad", [
    ("MAX_QUERIES", "0"), ("MAX_QUERIES", "9"), ("MAX_RESULTS", "31"),
    ("TIMEOUT", "16"), ("TOTAL_TIMEOUT", "121"), ("CACHE_TTL", "3601"),
    ("MIN_INTERVAL", "-1"), ("MIN_INTERVAL", "31"), ("REGION", "anywhere"),
    ("ALLOWED_DOMAINS", "com.cn"),
])
def test_search_env_still_obeys_adapter_bounds(monkeypatch, suffix, bad):
    from tradingagents.extensions.duckduckgo_news import _settings

    config = _config_with_env(monkeypatch, **{f"TRADINGAGENTS_DUCKDUCKGO_NEWS_{suffix}": bad})
    with pytest.raises(ValueError):
        _settings(config)
