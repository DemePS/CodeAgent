"""Helpers shared by the tools: the error type, workspace-confined paths, output limits, approvals."""

import os
from pathlib import Path

from . import state
from .config import MAX_TOOL_OUTPUT_CHARS


class ToolError(Exception):
    """Raised by a tool to return an is_error tool_result to the model."""


def resolve(path: str) -> Path:
    """Resolve a path against the current directory and refuse anything outside the workspace."""
    p = (state.cwd / path).resolve()
    if p != state.workspace and state.workspace not in p.parents:
        raise ToolError(f"Path '{path}' is outside the workspace.")
    return p


def display(p: Path) -> str:
    """How a path is shown to the model: relative to the current directory."""
    return Path(os.path.relpath(p, state.cwd)).as_posix()


def is_protected(p: Path) -> bool:
    """True if p is the agent's own source code."""
    return any(p == prot or prot in p.parents for prot in state.protected_paths)


def truncate(text: str) -> str:
    if len(text) <= MAX_TOOL_OUTPUT_CHARS:
        return text
    return text[:MAX_TOOL_OUTPUT_CHARS] + f"\n... [truncated, {len(text) - MAX_TOOL_OUTPUT_CHARS} more chars]"


def rel_name(p: Path) -> str:
    """A path as the user sees it: relative to the repository root, never ambiguous."""
    return p.relative_to(state.workspace).as_posix() if p != state.workspace else "."


def approve(question: str) -> None:
    """Ask the user to approve an action (skipped in autonomous mode); raises ToolError on refusal."""
    if state.auto_mode:
        state.ui.status(f"(autonomous mode: approved without asking -- {question})")
        return
    if state.ui.confirm(question) != "yes":
        feedback = state.ui.ask_text("Why not / what should change? (optional): ")
        raise ToolError("The user rejected this; nothing was changed."
                        + (f" User feedback: {feedback}" if feedback else ""))


def writable_path(path: str) -> Path:
    """Resolve a path the agent may write to, refusing its own source code."""
    p = resolve(path)
    if is_protected(p):
        raise ToolError(f"{path} is part of the coding agent's own source code and cannot be modified.")
    if p.is_dir():
        raise ToolError(f"{path} is a directory.")
    return p
