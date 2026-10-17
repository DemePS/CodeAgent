"""MCP (Model Context Protocol) client support: config merging, connecting to a real stdio server,
tool discovery/registration, calling a tool, confirmation for a non-read-only tool, and errors.

The test server is started once for the module (spawning it per test would be slow and, under load,
flaky): one connection, reused by every test that calls a tool."""

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from seedagent import schemas, state
from seedagent.mcp_config import load_server_configs
from seedagent.tools import TOOL_HANDLERS, run_tool
from seedagent.tools import mcp_bridge
from tests.conftest import ScriptedUI

SERVER_SCRIPT = Path(__file__).parent / "fixtures" / "mcp_test_server.py"
CALC_CONFIG = {"calc": {"command": sys.executable, "args": [str(SERVER_SCRIPT)]}}


@pytest.fixture(scope="module", autouse=True)
def calc_server():
    before_tools, before_handlers = list(schemas.TOOLS), dict(TOOL_HANDLERS)
    failed = mcp_bridge.connect_configured_servers(CALC_CONFIG)
    assert failed == []
    yield
    mcp_bridge.close_all()
    schemas.TOOLS[:] = before_tools
    TOOL_HANDLERS.clear()
    TOOL_HANDLERS.update(before_handlers)


@pytest.fixture(autouse=True)
def _all_tools_enabled(monkeypatch):
    """Other test modules can leave state.tool_names restricted on whatever session ends up current
    (see conftest._restore_current_session); this module calls tools directly, without the `workspace`
    fixture that normally resets it, so it must reset it itself."""
    monkeypatch.setattr(state, "tool_names", None, raising=False)


def call(name, **arguments):
    return run_tool(SimpleNamespace(id="toolu_1", name=name, input=arguments))


def test_configs_merge_user_and_project_with_project_winning(tmp_path, monkeypatch):
    user_home = tmp_path / "home"
    user_home.mkdir()
    import seedagent.mcp_config as mcp_config
    monkeypatch.setattr(mcp_config, "USER_CONFIG", user_home / "mcp_servers.json")
    mcp_config.USER_CONFIG.write_text(json.dumps({"mcpServers": {
        "a": {"command": "user-a"}, "shared": {"command": "user-shared"},
    }}))
    project = tmp_path / "project"
    (project / ".agent").mkdir(parents=True)
    (project / ".agent" / "mcp_servers.json").write_text(json.dumps({"mcpServers": {
        "b": {"command": "project-b"}, "shared": {"command": "project-shared"},
    }}))

    configs = mcp_config.load_server_configs(project)
    assert configs["a"]["command"] == "user-a"
    assert configs["b"]["command"] == "project-b"
    assert configs["shared"]["command"] == "project-shared"  # the project's entry wins


def test_configs_without_a_workspace_are_user_only(tmp_path, monkeypatch):
    import seedagent.mcp_config as mcp_config
    monkeypatch.setattr(mcp_config, "USER_CONFIG", tmp_path / "mcp_servers.json")
    mcp_config.USER_CONFIG.write_text(json.dumps({"mcpServers": {"a": {"command": "x"}}}))
    assert mcp_config.load_server_configs() == {"a": {"command": "x"}}


def test_missing_or_malformed_config_files_are_ignored(tmp_path, monkeypatch):
    import seedagent.mcp_config as mcp_config
    monkeypatch.setattr(mcp_config, "USER_CONFIG", tmp_path / "does_not_exist.json")
    assert load_server_configs(tmp_path) == {}
    bad = tmp_path / ".agent"
    bad.mkdir()
    (bad / "mcp_servers.json").write_text("not json")
    assert load_server_configs(tmp_path) == {}


def test_entries_missing_command_or_url_are_dropped(tmp_path, monkeypatch):
    import seedagent.mcp_config as mcp_config
    monkeypatch.setattr(mcp_config, "USER_CONFIG", tmp_path / "mcp_servers.json")
    mcp_config.USER_CONFIG.write_text(json.dumps({"mcpServers": {
        "ok": {"command": "x"}, "also_ok": {"url": "https://example.com"}, "bad": {"nonsense": True}, "wrong_shape": "x",
    }}))
    assert set(mcp_config.load_server_configs()) == {"ok", "also_ok"}


def test_connecting_discovers_and_registers_the_servers_tools():
    names = {t["name"] for t in schemas.TOOLS}
    assert {"mcp__calc__add", "mcp__calc__fail", "mcp__calc__whoami"} <= names
    assert "mcp__calc__add" in TOOL_HANDLERS


def test_calling_a_tool_asks_for_confirmation_and_returns_its_result(monkeypatch):
    state.ui = ScriptedUI(answers=["yes"])
    monkeypatch.setattr(state, "auto_mode", False)
    result = call("mcp__calc__add", a=2, b=3)
    assert result["content"] == [{"type": "text", "text": "5"}]
    assert state.ui.of("confirm")  # not read-only: asked before running


def test_declining_the_confirmation_raises_a_tool_error(monkeypatch):
    state.ui = ScriptedUI(answers=["no"])
    monkeypatch.setattr(state, "auto_mode", False)
    result = call("mcp__calc__add", a=2, b=3)
    assert result["is_error"] and "declined" in result["content"]


def test_a_read_only_tool_never_asks(monkeypatch):
    state.ui = ScriptedUI(answers=[])  # would raise if confirm() were called with no answers queued
    monkeypatch.setattr(state, "auto_mode", False)
    result = call("mcp__calc__whoami")
    assert result["content"] == [{"type": "text", "text": "test-server"}]
    assert not state.ui.of("confirm")


def test_autonomous_mode_skips_the_confirmation(monkeypatch):
    state.ui = ScriptedUI(answers=[])
    monkeypatch.setattr(state, "auto_mode", True)
    result = call("mcp__calc__add", a=10, b=5)
    assert result["content"] == [{"type": "text", "text": "15"}]


def test_a_tool_that_raises_on_the_server_becomes_a_tool_error(monkeypatch):
    state.ui = ScriptedUI(answers=["yes"])
    monkeypatch.setattr(state, "auto_mode", False)
    result = call("mcp__calc__fail")
    assert result["is_error"]
    assert "calc.fail" in result["content"]


def test_connecting_again_is_a_no_op_and_an_unreachable_server_is_reported_not_raised():
    again = mcp_bridge.connect_configured_servers(CALC_CONFIG)  # already connected: nothing to do
    assert again == []
    failed = mcp_bridge.connect_configured_servers({"broken": {"command": "no-such-program-xyz"}})
    assert failed == ["broken"]
