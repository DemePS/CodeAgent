"""An application adds its own tool with coding_agent.register_tool, without changing the package."""

from types import SimpleNamespace

import pytest

import coding_agent
from coding_agent import schemas, state, tools
from coding_agent.common import ToolError
from coding_agent.loop import active_tools
from coding_agent.tools import TOOL_HANDLERS, register_tool, run_tool
from tests.conftest import ScriptedUI

SCHEMA = {"name": "say_hello", "description": "Greet someone.",
          "input_schema": {"type": "object", "properties": {"who": {"type": "string"}, "loud": {"type": "boolean"}}, "required": ["who"]}}


@pytest.fixture(autouse=True)
def clean_registry():
    before_tools, before_handlers = list(schemas.TOOLS), dict(TOOL_HANDLERS)
    yield
    schemas.TOOLS[:] = before_tools
    TOOL_HANDLERS.clear()
    TOOL_HANDLERS.update(before_handlers)


def call(name, **arguments):
    return run_tool(SimpleNamespace(id="toolu_1", name=name, input=arguments))


def greet(who, loud=False):
    return f"Hello {who}{'!' if loud else ''}"


def test_a_registered_tool_is_offered_to_claude_and_called_with_its_arguments(monkeypatch):
    state.ui = ScriptedUI()
    monkeypatch.setattr(state, "tool_names", None)                      # all tools
    register_tool(SCHEMA, greet)
    assert "say_hello" in [t["name"] for t in active_tools()]
    assert call("say_hello", who="Awa", loud=True) == {"type": "tool_result", "tool_use_id": "toolu_1", "content": "Hello Awa!"}


def test_a_session_offers_it_only_when_it_lists_the_tool(monkeypatch):
    state.ui = ScriptedUI()
    register_tool(SCHEMA, greet)
    monkeypatch.setattr(state, "tool_names", {"read_file"})
    assert "say_hello" not in [t["name"] for t in active_tools()]
    assert call("say_hello", who="Awa")["is_error"] and "Unknown tool" in call("say_hello", who="Awa")["content"]
    monkeypatch.setattr(state, "tool_names", {"read_file", "say_hello"})
    assert "say_hello" in [t["name"] for t in active_tools()] and call("say_hello", who="Awa")["content"] == "Hello Awa"


def test_the_handlers_errors_reach_claude_and_do_not_stop_the_agent(monkeypatch):
    state.ui = ScriptedUI()
    monkeypatch.setattr(state, "tool_names", None)

    def refuse(who):
        raise ToolError("not allowed")

    def broken(who):
        raise RuntimeError("boom")
    register_tool({**SCHEMA, "name": "refuse"}, refuse)
    register_tool({**SCHEMA, "name": "broken"}, broken)
    assert call("refuse", who="x") == {"type": "tool_result", "tool_use_id": "toolu_1", "content": "not allowed", "is_error": True}
    assert call("broken", who="x")["content"] == "RuntimeError: boom"
    register_tool(SCHEMA, greet)
    assert call("say_hello", nobody="x")["content"].startswith("Invalid arguments")       # a missing argument


def test_a_handler_can_return_content_blocks(monkeypatch):
    state.ui = ScriptedUI()
    monkeypatch.setattr(state, "tool_names", None)
    blocks = [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]
    register_tool(SCHEMA, lambda who, loud=False: blocks)
    assert call("say_hello", who="x")["content"] == blocks


def test_registering_twice_is_harmless_and_names_cannot_clash_or_replace_built_ins():
    register_tool(SCHEMA, greet)
    register_tool(SCHEMA, greet)                                          # the same handler: nothing happens
    assert [t["name"] for t in schemas.TOOLS].count("say_hello") == 1
    with pytest.raises(ValueError, match="already registered"):
        register_tool(SCHEMA, lambda who: "other")
    for built_in in ("read_file", "ask_human", "run_python"):
        with pytest.raises(ValueError, match="built-in"):
            register_tool({**SCHEMA, "name": built_in}, greet)
    assert TOOL_HANDLERS["read_file"] is not greet


def test_a_bad_definition_is_refused_at_once():
    bad = [({**SCHEMA, "name": "has space"}, "name"), ({**SCHEMA, "name": ""}, "name"), ({**SCHEMA, "description": " "}, "description"),
           ({"name": "x", "description": "d", "input_schema": {"type": "string"}}, "object"),
           ({"name": "x", "description": "d"}, "object")]
    for schema, word in bad:
        with pytest.raises(ValueError, match=word):
            register_tool(schema, greet)
    with pytest.raises(TypeError, match="callable"):
        register_tool(SCHEMA, "not a function")
    assert "say_hello" not in TOOL_HANDLERS


def test_it_is_reachable_from_the_package_and_the_session():
    from coding_agent import session
    assert coding_agent.register_tool is tools.register_tool is session.register_tool
