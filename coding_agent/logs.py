"""Logs of what the agent does, next to the project's memory.

`LoggedUI` wraps the UI of a session: every call is passed on unchanged and also written to the logger `coding_agent.ui`
(tool calls and their results, changes, warnings, errors, questions asked, token usage). What the person types as an answer,
and the contents of files, are never logged: only the short summaries the UI already shows.
The log file is MEMORY_HOME/<project>/agent.log (with the saved conversation), kept to about 2 MB; AGENT_LOG_LEVEL
(default INFO; DEBUG also logs the model's text) and AGENT_LOG=off to write no file. Programs that configure `logging` themselves
(a server) receive the same records.
"""

from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .ui import UI

log = logging.getLogger("coding_agent.ui")
_handler: RotatingFileHandler | None = None


def attach(file: Path) -> None:
    """Write the log to `file` (replacing the file of the previously open project)."""
    global _handler
    root = logging.getLogger("coding_agent")
    if _handler is not None:
        root.removeHandler(_handler)
        _handler.close()
        _handler = None
    if file is None or os.environ.get("AGENT_LOG", "on").strip().lower() in ("0", "off", "false", "no"):
        return
    level = getattr(logging, os.environ.get("AGENT_LOG_LEVEL", "INFO").upper(), logging.INFO)
    try:
        file.parent.mkdir(parents=True, exist_ok=True)
        _handler = RotatingFileHandler(file, maxBytes=1_000_000, backupCount=1, encoding="utf-8")
    except OSError:
        return  # no log file is better than no agent
    _handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    root.addHandler(_handler)
    root.setLevel(level)


class LoggedUI(UI):
    """Passes every call to `ui` and logs it."""

    def __init__(self, ui: UI) -> None:
        self.ui = ui
        self._text: list[str] = []

    def __getattr__(self, name):  # anything else the application's UI offers
        return getattr(self.ui, name)

    def status(self, text):
        log.info("%s", text)
        self.ui.status(text)

    def success(self, text):
        log.info("%s", text)
        self.ui.success(text)

    def failure(self, text):
        log.warning("%s", text)
        self.ui.failure(text)

    def warning(self, text):
        log.warning("%s", text)
        self.ui.warning(text)

    def message(self, text):
        log.info("%s", text)
        self.ui.message(text)

    def error(self, text):
        log.error("%s", text)
        self.ui.error(text)

    def panel(self, title, lines=(), tone="change"):
        log.info("%s: %s", tone, title)
        self.ui.panel(title, lines, tone)

    def diff(self, action, name, path, first_line, diff_lines):
        log.info("%s %s (%d diff lines)", action, path, len(diff_lines))
        self.ui.diff(action, name, path, first_line, diff_lines)

    def cell_changes(self, title, rows, more):
        log.info("%s: %d cell(s) shown, %d more", title, len(rows), more)
        self.ui.cell_changes(title, rows, more)

    def confirm(self, question, choices=("yes", "no")):
        answer = self.ui.confirm(question, choices)
        log.info("asked: %s -> %s", question, answer)
        return answer

    def ask_text(self, prompt, multiline=False):
        log.info("asked for text: %s", prompt)  # the answer is not logged
        return self.ui.ask_text(prompt, multiline)

    def assistant_start(self):
        self._text = []
        self.ui.assistant_start()

    def assistant_text(self, text):
        self._text.append(text)
        self.ui.assistant_text(text)

    def thinking(self):
        log.debug("thinking")
        self.ui.thinking()

    def tool_start(self, name):
        log.info("tool: %s", name)
        self.ui.tool_start(name)

    def tool_detail(self, text):
        log.debug("tool detail: %s", text.strip())
        self.ui.tool_detail(text)

    def assistant_end(self):
        if self._text:
            log.debug("assistant: %s", "".join(self._text))
        self.ui.assistant_end()

    def tool_result(self, name, arguments, ok, summary):
        log.log(logging.INFO if ok else logging.WARNING, "%s(%s) -> %s: %s", name, arguments, "ok" if ok else "failed", summary)
        self.ui.tool_result(name, arguments, ok, summary)
