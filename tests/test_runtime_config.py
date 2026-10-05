"""configure(): a key and model chosen while the program runs (a host application's Settings screen)."""

import ast
import queue
from pathlib import Path

import anthropic
import httpx
import pytest

import coding_agent
from coding_agent import config, diagnose, errors, memory

FOUNDRY = "https://x.services.ai.azure.com/anthropic"
KEY = "sk-ant-api03-abcdefghijklmnop"


@pytest.fixture
def env(monkeypatch):
    for name in ("ANTHROPIC_FOUNDRY_ENDPOINT", "ANTHROPIC_FOUNDRY_API_KEY", "ANTHROPIC_FOUNDRY_DEPLOYMENT",
                 "ANTHROPIC_API_KEY", "ANTHROPIC_MODEL", "AGENT_MEMORY_MODEL", "AGENT_COMPACT_MODEL"):
        monkeypatch.delenv(name, raising=False)
    config.clear()
    yield monkeypatch
    config.clear()


def test_exports():
    assert coding_agent.configure is config.configure
    assert coding_agent.active_provider is config.active_provider


def test_key_beats_foundry_and_clear_restores(env):
    env.setenv("ANTHROPIC_FOUNDRY_ENDPOINT", FOUNDRY)
    env.setenv("ANTHROPIC_FOUNDRY_API_KEY", "f")
    assert config.active_provider() == "foundry"
    config.configure(api_key=KEY)
    assert config.active_provider() == "anthropic"
    assert type(config._get_client()) is anthropic.Anthropic
    config.clear()
    assert config.active_provider() == "foundry"
    assert isinstance(config._get_client(), anthropic.AnthropicFoundry)


def test_neither_gives_none(env):
    assert config.active_provider() is None


def test_new_key_new_client_same_key_same_client(env):
    config.configure(api_key=KEY)
    first = config._get_client()
    assert config._get_client() is first
    config.configure(api_key=KEY + "2")
    assert config._get_client() is not first
    assert config._get_client().api_key == KEY + "2"


def test_model_order(env):
    assert config.get_model() == "claude-sonnet-5-5"
    env.setenv("ANTHROPIC_API_KEY", "k")
    env.setenv("ANTHROPIC_MODEL", "claude-haiku-4-5")
    assert config.get_model() == "claude-haiku-4-5"
    config.configure(model="claude-sonnet-5-5")
    assert config.get_model() == "claude-sonnet-5-5"
    assert config.get_memory_model() == config.get_compact_model() == "claude-sonnet-5-5"
    env.setenv("AGENT_MEMORY_MODEL", "m")
    assert config.get_memory_model() == "m"


def test_foundry_ignores_the_model_override(env):
    env.setenv("ANTHROPIC_FOUNDRY_ENDPOINT", FOUNDRY)
    env.setenv("ANTHROPIC_FOUNDRY_DEPLOYMENT", "dep")
    config.configure(model="claude-sonnet-5-5")
    assert config.get_model() == "dep"


def test_empty_removes_none_keeps(env):
    config.configure(api_key=KEY, model="claude-sonnet-5-5")
    config.configure(model="")
    assert config.current_api_key() == KEY and config.get_model() == "claude-sonnet-5-5"
    config.configure()
    assert config.current_api_key() == KEY
    config.configure(api_key=" ")
    assert config.current_api_key() is None


def test_key_never_goes_to_the_environment(env):
    import os
    config.configure(api_key=KEY)
    assert KEY not in os.environ.values()


def test_make_anthropic_client_options(env):
    client = config.make_anthropic_client(KEY, max_retries=0, timeout=15)
    assert client.max_retries == 0 and client.api_key == KEY
    assert config.make_anthropic_client(KEY).max_retries == 2


def test_legacy_names_read_at_access_time(env):
    config.configure(api_key=KEY, model="claude-sonnet-5-5")
    assert config.MODEL == "claude-sonnet-5-5"


def test_no_module_copies_a_model_constant():
    names = {"MODEL", "MEMORY_MODEL", "COMPACT_MODEL"}
    for path in Path(coding_agent.__file__).parent.rglob("*.py"):
        if path.name == "config.py":
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ImportFrom) and node.module and node.module.endswith("config"):
                assert not names & {a.name for a in node.names}, path


def status_error(code, message):
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    response = httpx.Response(code, request=request)
    return anthropic.APIStatusError(message, response=response, body=None)


def test_redact(env):
    config.configure(api_key="plainkey-without-prefix")
    text = errors.redact(f"bad {KEY} and plainkey-without-prefix")
    assert KEY not in text and "plainkey" not in text and "[api key hidden]" in text


def test_401_names_settings_with_an_override_and_hides_the_key(env):
    config.configure(api_key=KEY)
    message = errors.describe(status_error(401, f"invalid x-api-key {KEY}"))
    assert "Settings" in message and KEY not in message


def test_401_names_the_variable_without_one(env):
    env.setenv("ANTHROPIC_API_KEY", "k")
    assert "ANTHROPIC_API_KEY" in errors.describe(status_error(401, "no"))


def test_credit_and_overloaded(env):
    config.configure(api_key=KEY)
    assert "no credit" in errors.describe(status_error(400, "Your credit balance is too low"))
    assert "overloaded" in errors.describe(status_error(529, "Overloaded"))


def test_memory_worker_uses_the_client_of_its_item(env, monkeypatch):
    used = []
    monkeypatch.setattr(memory, "update_memory", lambda client, digest: used.append((client, digest)) and None)
    monkeypatch.setattr(memory, "_memory_queue", queue.Queue())
    memory._memory_queue.put(("one", "d1"))
    memory._memory_queue.put(("two", "d2"))
    memory._memory_queue.put(None)
    memory._memory_worker()
    assert used == [("one", "d1"), ("two", "d2")]


def test_sign_in_skipped_with_a_saved_key(env, monkeypatch):
    env.setenv("ANTHROPIC_FOUNDRY_ENDPOINT", FOUNDRY)
    config.configure(api_key=KEY)
    import coding_agent.signin as signin
    monkeypatch.setattr(signin, "SignIn", lambda: pytest.fail("sign-in must not start"))
    diagnose._sign_in()
