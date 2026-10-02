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
                 "ANTHROPIC_API_KEY", "ANTHROPIC_MODEL"):
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
    assert model_with(ANTHROPIC_API_KEY="k") == "claude-opus-5"


def test_access_denied_names_the_right_key(env):
    env.setenv("ANTHROPIC_API_KEY", "anthropic-key")
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    response = httpx.Response(401, request=request)
    error = anthropic.AuthenticationError("invalid x-api-key", response=response, body=None)
    assert "ANTHROPIC_API_KEY" in errors.describe(error)
