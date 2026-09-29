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


def resolve_readable(path: str) -> Path:
    """Resolve a path the agent may read: inside the workspace, or inside a read-only folder the
    person added (state.read_roots). Writing always goes through resolve(), workspace only."""
    p = (state.cwd / path).resolve()  # an absolute path ignores the current directory
    if in_workspace(p) or any(p == root or root in p.parents for root in state.read_roots):
        return p
    where = "the workspace and the read-only folders" if state.read_roots else "the workspace"
    raise ToolError(f"Path '{path}' is outside {where}.")


def in_workspace(p: Path) -> bool:
    return p == state.workspace or state.workspace in p.parents


def display(p: Path) -> str:
    """How a path is shown to the model: relative to the current directory, or absolute for a file
    in a read-only folder outside the workspace."""
    if not in_workspace(p):
        return p.as_posix()
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
    if not in_workspace(p):
        return p.as_posix()
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
