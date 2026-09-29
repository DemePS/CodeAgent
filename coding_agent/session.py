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
from collections.abc import Iterable
from pathlib import Path

from . import state
from .config import MEMORY_HOME, _get_client
from .conversation import load_conversation
from .loop import send as _send
from .loop import set_auto_mode
from .memory import finish_memory_updates
from .skills import discover_skills
from .ui import UI

messages: list = []  # the conversation of the open project


def project_id(workspace: Path) -> str:
    """One memory folder per project, e.g. ~/.coding-agent/memory/myapp-1a2b3c4d/."""
    return f"{workspace.name}-{hashlib.sha256(str(workspace).encode()).hexdigest()[:8]}"


def open_project(path: str | Path, ui: UI, tools: Iterable[str] | None = None,
                 system_prompt: str | None = None, resume: bool = False, auto: bool = False) -> Path:
    """Point the agent at a project folder. Returns the resolved workspace path."""
    workspace = Path(path).expanduser().resolve()
    if not workspace.is_dir():
        raise NotADirectoryError(f"Not a directory: {workspace}")
    state.ui = ui
    state.set_workspace(workspace, project_id(workspace), MEMORY_HOME)
    state.tool_names = set(tools) if tools is not None else None
    state.system_prompt = system_prompt
    state.skills = discover_skills()
    messages[:] = load_conversation() if resume else []
    if auto:
        set_auto_mode(True)
    return workspace


def send(text: str) -> bool:
    """Run one instruction to completion (tools, approvals, streaming). False if it failed."""
    return _send(_get_client(), messages, text)


def stop() -> None:
    """Ask the running instruction to stop (from another thread): it ends at the next model call
    or streamed chunk, and is rolled back like Ctrl+C. Pending questions must be answered by the UI."""
    state.stop_requested = True


def close() -> None:
    """Let a pending memory update finish (up to a minute)."""
    finish_memory_updates()
