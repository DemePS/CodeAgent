"""The public API: open a project and run instructions, from any front end.

    from coding_agent import session
    from coding_agent.ui import TerminalUI

    session.open_project("path/to/project", ui=TerminalUI())
    session.send("Fill costs.xlsx from invoices/*.pdf")

An application built on the agent can narrow what Claude sees: `tools` enables a subset of the
tools (see coding_agent.schemas.TOOLS for the names) and `system_prompt` replaces the coding
agent's instructions ({workspace} in it is replaced by the project path).
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Iterable
from pathlib import Path

from . import cleanup, logs, state
from .common import grant_read_folder
from .config import MEMORY_HOME, _get_client, get_model
from .conversation import load_conversation
from .loop import send as _send
from .loop import set_auto_mode
from .memory import finish_memory_updates
from .skills import discover_skills
from .tools import register_tool  # noqa: F401  (re-exported: session.register_tool)
from .logs import LoggedUI
from .ui import UI

messages: list = []  # the conversation of the open project


def project_id(workspace: Path) -> str:
    """One memory folder per project, e.g. ~/.coding-agent/memory/myapp-1a2b3c4d/."""
    return f"{workspace.name}-{hashlib.sha256(str(workspace).encode()).hexdigest()[:8]}"


def had_text_read(history: list) -> bool:
    """True when the history holds a read_pdf call in text mode (the default) that was answered without an error:
    read_pdf's visual mode is then allowed, as it was when the conversation was saved."""
    text_reads = set()
    for message in history:
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use" and block.get("name") == "read_pdf" and block.get("id") is not None \
                    and (block.get("input") or {}).get("mode") in (None, "text"):
                text_reads.add(block["id"])
            elif block.get("type") == "tool_result" and block.get("tool_use_id") in text_reads and not block.get("is_error"):
                return True
    return False


def open_project(path: str | Path, ui: UI, tools: Iterable[str] | None = None,
                 system_prompt: str | None = None, resume: bool = False, auto: bool = False,
                 excel_first: bool = False) -> Path:
    """Point the agent at a project folder. Returns the resolved workspace path."""
    workspace = Path(path).expanduser().resolve()
    if not workspace.is_dir():
        raise NotADirectoryError(f"Not a directory: {workspace}")
    if (cleaned := cleanup.run_once(memory=MEMORY_HOME)) and cleaned != "Clean-up: nothing to delete":
        logging.getLogger("coding_agent.cleanup").info(cleaned)  # old backups, unused memories, old conversations (before resume)
    state.ui = LoggedUI(ui)
    state.set_workspace(workspace, project_id(workspace), MEMORY_HOME)
    logs.attach(state.conversation_file.parent / "agent.log")
    state.reset_conversation()
    state.tool_names = set(tools) if tools is not None else None
    state.system_prompt = system_prompt
    state.excel_first = excel_first
    state.skills = discover_skills()
    messages[:] = load_conversation() if resume else []
    state.pdf_read_as_text = had_text_read(messages)  # a resumed conversation that already read a PDF in text mode
    if auto:
        set_auto_mode(True)
    return workspace


def add_read_folder(path: str | Path) -> Path:
    """Let the agent read (never write) a folder outside the project, e.g. where the person keeps
    documents. Claude is told with the next instruction. Returns the resolved folder."""
    folder = Path(path).expanduser().resolve()
    if not folder.is_dir():
        raise NotADirectoryError(f"Not a directory: {folder}")
    if not (folder == state.workspace or state.workspace in folder.parents):
        grant_read_folder(folder)
    return folder


def send(text: str) -> bool:
    """Run one instruction to completion (tools, approvals, streaming). False if it failed."""
    return _send(_get_client(), messages, text)


def check_connection() -> tuple[bool, str]:
    """A tiny call to Claude: (True, summary) if it answers, else (False, why in plain words)."""
    from .errors import connection_summary, describe
    try:
        _get_client().messages.create(model=get_model(), max_tokens=1, messages=[{"role": "user", "content": "ping"}])
        return True, connection_summary()
    except Exception as e:
        explanation = describe(e)
        if explanation is None:
            explanation = f"{type(e).__name__}: {e}"
        return False, explanation


def stop() -> None:
    """Ask the running instruction to stop (from another thread): it ends at the next model call
    or streamed chunk, and is rolled back like Ctrl+C. Pending questions must be answered by the UI."""
    state.stop_requested = True


def close() -> None:
    """Let a pending memory update finish (up to a minute)."""
    finish_memory_updates()
