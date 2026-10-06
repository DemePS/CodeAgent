import logging

import pytest

from coding_agent import logs, session, state
from coding_agent.ui import UI


@pytest.fixture(autouse=True)
def detach(monkeypatch):
    yield
    monkeypatch.setenv("AGENT_LOG", "off")
    logs.attach(None)


class Recorder(UI):
    def __init__(self):
        self.calls = []
        self.extra = "kept"

    def status(self, text):
        self.calls.append(("status", text))

    def confirm(self, question, choices=("yes", "no")):
        return "yes"

    def ask_text(self, prompt, multiline=False):
        return "my secret answer"

    def tool_result(self, name, arguments, ok, summary):
        self.calls.append(("tool_result", name))


def open_in(tmp_path, monkeypatch, **env):
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(session, "MEMORY_HOME", tmp_path / "memory")
    monkeypatch.setenv("AGENT_CLEANUP", "off")
    ui = Recorder()
    (tmp_path / "proj").mkdir(exist_ok=True)
    session.open_project(tmp_path / "proj", ui=ui)
    return ui


def log_file():
    return state.conversation_file.parent / "agent.log"



def test_calls_are_passed_on_and_logged_next_to_the_memory(tmp_path, monkeypatch):
    ui = open_in(tmp_path, monkeypatch)
    state.ui.tool_start("read_file")
    state.ui.tool_result("read_file", "path='a.txt'", True, "12 characters")
    state.ui.tool_result("edit_file", "path='b'", False, "not found")
    assert state.ui.confirm("Apply?") == "yes"
    assert state.ui.ask_text("Why?") == "my secret answer"
    state.ui.status("[tokens: 1 in]")
    assert ("tool_result", "read_file") in ui.calls and ("status", "[tokens: 1 in]") in ui.calls
    assert state.ui.extra == "kept"                                                   # the application's own attributes
    text = log_file().read_text(encoding="utf-8")
    assert "tool: read_file" in text and "read_file(path='a.txt') -> ok: 12 characters" in text
    assert "WARNING edit_file(path='b') -> failed: not found" in text and "asked: Apply? -> yes" in text
    assert "asked for text: Why?" in text and "secret" not in text and "[tokens: 1 in]" in text


def test_the_model_text_is_only_logged_at_debug_and_the_file_can_be_switched_off(tmp_path, monkeypatch):
    open_in(tmp_path, monkeypatch)
    state.ui.assistant_start(); state.ui.assistant_text("hello "); state.ui.assistant_text("there"); state.ui.assistant_end()
    assert "assistant:" not in log_file().read_text(encoding="utf-8")
    open_in(tmp_path, monkeypatch, AGENT_LOG_LEVEL="DEBUG")
    state.ui.assistant_start(); state.ui.assistant_text("hello "); state.ui.assistant_text("there"); state.ui.assistant_end()
    assert "assistant: hello there" in log_file().read_text(encoding="utf-8")
    log_file().unlink()
    open_in(tmp_path, monkeypatch, AGENT_LOG="off")
    state.ui.status("x")
    assert not log_file().exists()
