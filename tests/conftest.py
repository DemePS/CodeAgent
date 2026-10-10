"""Shared fixtures: a throwaway project folder and a scripted UI standing in for the person."""

from __future__ import annotations

import os

import pytest

from coding_agent import state
from coding_agent.config import OWN_FILES
from coding_agent.ui import UI

# The tests open projects all the time: the automatic clean-up must not touch the real ~/.coding-agent (tests of it switch it on).
os.environ["AGENT_CLEANUP"] = "off"


class ScriptedUI(UI):
    """Records everything the core shows and answers questions from a script.

    answers: consumed in order by confirm() and ask_text(); confirm() accepts "yes"/"no"/... .
    on_confirm: optional callback run just before a confirm() answer (e.g. edit a file meanwhile).
    """

    def __init__(self, answers=()):
        self.answers = list(answers)
        self.events: list[tuple] = []
        self.on_confirm = None

    def _record(self, kind, *args):
        self.events.append((kind, *args))

    def status(self, text): self._record("status", text)
    def success(self, text): self._record("success", text)
    def failure(self, text): self._record("failure", text)
    def warning(self, text): self._record("warning", text)
    def message(self, text): self._record("message", text)
    def panel(self, title, lines=(), tone="change"): self._record("panel", title, list(lines), tone)
    def diff(self, action, name, path, first_line, diff_lines): self._record("diff", action, name, first_line, diff_lines)
    def cell_changes(self, title, rows, more): self._record("cells", title, rows, more)
    def assistant_text(self, text): self._record("text", text)
    def tool_start(self, name): self._record("tool", name)

    def confirm(self, question, choices=("yes", "no")):
        self._record("confirm", question.strip(), choices)
        if self.on_confirm:
            self.on_confirm()
        answer = self.answers.pop(0) if self.answers else "no"
        assert answer in choices, (answer, choices)
        return answer

    def ask_text(self, prompt, multiline=False):
        self._record("ask_text", prompt)
        return self.answers.pop(0) if self.answers else ""

    def kinds(self):
        return [e[0] for e in self.events]

    def of(self, kind):
        return [e for e in self.events if e[0] == kind]


@pytest.fixture
def ui():
    return ScriptedUI()


@pytest.fixture
def workspace(tmp_path, ui, monkeypatch):
    """A fresh project folder as the agent's workspace, with default session state."""
    ws = tmp_path / "project"
    ws.mkdir()
    monkeypatch.setattr(state, "ui", ui, raising=False)
    state.set_workspace(ws.resolve(), "test-project", tmp_path / "memory-home")
    monkeypatch.setattr(state, "auto_mode", False)
    monkeypatch.setattr(state, "always_allow_python", False)
    monkeypatch.setattr(state, "pdf_read_as_text", False)
    monkeypatch.setattr(state, "tool_names", None)
    monkeypatch.setattr(state, "system_prompt", None)
    monkeypatch.setattr(state, "turn", {"instruction": "", "excel_read": False})
    monkeypatch.setattr(state, "context", {"tokens": 0, "chars": 0, "compactions": 0, "cleared": 0})
    monkeypatch.setattr(state, "pending_blocks", [])
    monkeypatch.setattr(state, "memory_sent", False)
    monkeypatch.setattr(state, "compacted_this_turn", False)
    assert state.protected_paths[: len(OWN_FILES)] == OWN_FILES
    return ws.resolve()
