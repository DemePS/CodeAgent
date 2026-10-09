"""Which Claude service the agent uses: Foundry whenever its endpoint is set, else Anthropic's own API."""

import os
import subprocess
import sys

import anthropic
import httpx
import pytest

from coding_agent import config, errors

FOUNDRY = "https://x.services.ai.azure.com/anthropic"


@pytest.fixture
def env(monkeypatch):
    for name in ("ANTHROPIC_FOUNDRY_ENDPOINT", "ANTHROPIC_FOUNDRY_API_KEY", "ANTHROPIC_FOUNDRY_DEPLOYMENT",
                 "ANTHROPIC_API_KEY", "ANTHROPIC_MODEL", "CODEAGENT_PROVIDER", "DEEPSEEK_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    config.clear()
    yield monkeypatch
    config.clear()


def model_with(**settings) -> str:
    """MODEL as a fresh process reads it at import (empty values count as unset, and block any .env)."""
    names = ("ANTHROPIC_FOUNDRY_ENDPOINT", "ANTHROPIC_FOUNDRY_DEPLOYMENT", "ANTHROPIC_API_KEY", "ANTHROPIC_MODEL")
    environment = {**os.environ, **{name: settings.get(name, "") for name in names}}
    return subprocess.run([sys.executable, "-c", "from coding_agent.config import get_model; print(get_model())"],
                          env=environment, capture_output=True, text=True, check=True).stdout.strip()


def test_foundry_with_key_stays_on_foundry_even_with_an_anthropic_key(env):
    env.setenv("ANTHROPIC_FOUNDRY_ENDPOINT", FOUNDRY)
    env.setenv("ANTHROPIC_FOUNDRY_API_KEY", "foundry-key")
    env.setenv("ANTHROPIC_API_KEY", "anthropic-key")  # set for some other tool
    assert not config.uses_anthropic_api()
    client = config._get_client()
    assert isinstance(client, anthropic.AnthropicFoundry)
    assert str(client.base_url).startswith(FOUNDRY)
    assert FOUNDRY in errors.connection_summary() and "deployment" in errors.connection_summary()


def test_anthropic_api_when_no_foundry_endpoint(env):
    env.setenv("ANTHROPIC_API_KEY", "anthropic-key")
    assert config.uses_anthropic_api()
    client = config._get_client()
    assert type(client) is anthropic.Anthropic
    assert client.api_key == "anthropic-key"
    assert "Anthropic API" in errors.connection_summary()


def test_neither_keeps_todays_missing_endpoint_error(env):
    assert not config.uses_anthropic_api()
    with pytest.raises(KeyError) as raised:
        config._get_client()
    message = errors.describe(raised.value)
    assert "ANTHROPIC_FOUNDRY_ENDPOINT is not set" in message and "ANTHROPIC_API_KEY" in message


def test_model_setting_follows_the_service():
    assert model_with(ANTHROPIC_FOUNDRY_ENDPOINT=FOUNDRY, ANTHROPIC_FOUNDRY_DEPLOYMENT="my-deployment",
                      ANTHROPIC_API_KEY="k", ANTHROPIC_MODEL="ignored-on-foundry") == "my-deployment"
    assert model_with(ANTHROPIC_API_KEY="k", ANTHROPIC_MODEL="claude-sonnet-5",
                      ANTHROPIC_FOUNDRY_DEPLOYMENT="ignored-without-foundry") == "claude-sonnet-5"
    assert model_with(ANTHROPIC_API_KEY="k") == "claude-sonnet-5"


def test_access_denied_names_the_right_key(env):
    env.setenv("ANTHROPIC_API_KEY", "anthropic-key")
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    response = httpx.Response(401, request=request)
    error = anthropic.AuthenticationError("invalid x-api-key", response=response, body=None)
    assert "ANTHROPIC_API_KEY" in errors.describe(error)


# --- DeepSeek: the Anthropic client on DeepSeek's base URL -------------------------------------------------------------------

@pytest.fixture
def deepseek_env(env):
    for name in ("DEEPSEEK_API_KEY", "CODEAGENT_PROVIDER", "ANTHROPIC_BASE_URL"):
        env.delenv(name, raising=False)
    return env


def test_a_deepseek_key_alone_selects_deepseek(deepseek_env):
    deepseek_env.setenv("DEEPSEEK_API_KEY", "ds-key")
    assert config.uses_deepseek() and config.uses_anthropic_api() and config.active_provider() == "deepseek"
    client = config._get_client()
    assert type(client) is anthropic.Anthropic and client.api_key == "ds-key"
    assert str(client.base_url).startswith(config.DEEPSEEK_BASE_URL)
    assert config.get_model() == "deepseek-flash"
    assert "DeepSeek" in errors.connection_summary() and "DEEPSEEK_API_KEY" in config.key_location()


def test_other_services_keep_their_priority_over_a_deepseek_key(deepseek_env):
    deepseek_env.setenv("DEEPSEEK_API_KEY", "ds-key")
    deepseek_env.setenv("ANTHROPIC_API_KEY", "anthropic-key")
    assert config.active_provider() == "anthropic" and not config.uses_deepseek()
    assert config._get_client().api_key == "anthropic-key" and config.get_model() == "claude-sonnet-5"
    config.clear()
    deepseek_env.delenv("ANTHROPIC_API_KEY")
    deepseek_env.setenv("ANTHROPIC_FOUNDRY_ENDPOINT", FOUNDRY)
    assert config.active_provider() == "foundry"


def test_the_provider_setting_chooses_deepseek_over_the_others(deepseek_env):
    deepseek_env.setenv("DEEPSEEK_API_KEY", "ds-key")
    deepseek_env.setenv("ANTHROPIC_API_KEY", "anthropic-key")
    deepseek_env.setenv("ANTHROPIC_FOUNDRY_ENDPOINT", FOUNDRY)
    deepseek_env.setenv("CODEAGENT_PROVIDER", "deepseek")
    assert config.active_provider() == "deepseek" and config.current_api_key() == "ds-key"


def test_a_key_given_to_configure_wins_over_deepseek(deepseek_env):
    deepseek_env.setenv("DEEPSEEK_API_KEY", "ds-key")
    config.configure(api_key="typed-key")
    assert config.active_provider() == "anthropic" and config.current_api_key() == "typed-key"


def test_a_base_url_on_deepseek_com_counts_as_deepseek(deepseek_env):
    deepseek_env.setenv("ANTHROPIC_API_KEY", "ds-key")
    deepseek_env.setenv("ANTHROPIC_BASE_URL", config.DEEPSEEK_BASE_URL)
    assert config.uses_deepseek() and config.active_provider() == "deepseek"
    assert str(config._get_client().base_url).startswith(config.DEEPSEEK_BASE_URL)
    assert config.get_model() == "claude-sonnet-5"                  # DeepSeek maps the name itself (to deepseek-flash)


def test_deepseek_gets_no_web_search_tool_and_no_cache_control(deepseek_env):
    from coding_agent import loop, state
    state.tool_names = None
    assert any(t["name"] == "web_search" for t in loop.active_tools()) or config.WEB_SEARCH == "off"
    assert loop.caching_options() == {"cache_control": {"type": "ephemeral"}}
    deepseek_env.setenv("DEEPSEEK_API_KEY", "ds-key")
    assert all(t["name"] != "web_search" for t in loop.active_tools()) and loop.caching_options() == {}


def test_the_cache_lifetime_setting(deepseek_env):
    from coding_agent import loop
    deepseek_env.delenv("AGENT_CACHE_TTL", raising=False)
    assert loop.caching_options() == {"cache_control": {"type": "ephemeral"}}
    deepseek_env.setenv("AGENT_CACHE_TTL", "1h")
    assert loop.caching_options() == {"cache_control": {"type": "ephemeral", "ttl": "1h"}}
    deepseek_env.setenv("AGENT_CACHE_TTL", "2h")
    with pytest.raises(config.SettingError, match="5m or 1h"):
        loop.caching_options()
