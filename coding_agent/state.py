"""Mutable state of the current agent session, shared by the core modules.

Everything that changes while the agent runs lives here (the workspace, the current directory,
autonomous mode, the conversation's bookkeeping), so the terminal and the desktop app drive the
same core. One agent session runs per process.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from .config import DEFAULT_CONTEXT_WINDOW, OWN_FILES

if TYPE_CHECKING:
    from .ui import UI

ui: "UI"  # how the core talks to the person; set by the front end (cli.py or the desktop app)

workspace: Path = Path(".").resolve()  # the project the agent works in
cwd: Path = workspace  # the agent's current directory inside the workspace; see change_directory
auto_mode = False  # autonomous mode: no approval prompts (deletions and network still ask)
memory_dir: Path | None = None  # this project's memory folder
conversation_file: Path | None = None  # the saved history, used by --resume
skills: dict[str, Path] = {}  # skill name -> its SKILL.md
read_roots: list[Path] = []  # extra folders the agent may read but never write (added by the person)
read_roots_note: str | None = None  # tells Claude about newly added read-only folders

# What this session exposes to Claude. None means everything (the coding agent); an application
# built on the package (e.g. an Excel filler) narrows the tools and brings its own instructions.
tool_names: set[str] | None = None  # the enabled tools, or None for all
# Workbook (resolved path) -> the only sheets edit_excel may change in it; set by an application
# (e.g. the sheets the person ticked). Workbooks not listed: any sheet.
excel_edit_sheets: dict[Path, set[str]] = {}
# Workbook (resolved path) -> {sheet: its number of columns}: edit_excel may not write beyond them (no
# new helper columns); set by an application, e.g. from the workbook as the job started.
excel_max_columns: dict[Path, dict[str, int]] = {}
# True: edit_excel never writes a formula, and never overwrites or clears one (for an application
# whose users fill in values, not formulas).
excel_protect_formulas: bool = False
# False: format_excel and excel_add are refused (for an application that fills existing workbooks,
# whose layout must never change).
excel_allow_format: bool = True
system_prompt: str | None = None  # replaces the coding agent's system prompt ({workspace} is filled in)


def tool_enabled(name: str) -> bool:
    return tool_names is None or name in tool_names

# Paths the agent must never modify: its own files, plus the project's .agent/skills folder.
protected_paths: list[Path] = list(OWN_FILES)

mode_note: str | None = None  # tells Claude about a mode change with the next instruction
skills_note: str | None = None  # an updated skill list after /skills, sent with the next instruction
memory_sent = False  # memory and the skill list go with the first instruction of each session
pending_blocks: list[str] = []  # e.g. a summary from /compact, sent with the next instruction
compacted_this_turn = False  # a compaction replaced the history during the current instruction
always_allow_python = False  # the user answered [a]lways to a run_python prompt
stop_requested = False  # set by session.stop() from another thread (e.g. a Stop button)

turn = {"instruction": "", "excel_read": False}  # the current instruction; reset for each one
context_window: int = DEFAULT_CONTEXT_WINDOW  # tokens; lowered if the API reports a smaller window
context = {"tokens": 0, "chars": 0, "compactions": 0, "cleared": 0}  # size at the last API call


def reset_conversation() -> None:
    """Forget the bookkeeping of the previous conversation (a new project or a fresh start)."""
    global memory_sent, mode_note, skills_note, compacted_this_turn, always_allow_python, stop_requested
    memory_sent = compacted_this_turn = always_allow_python = stop_requested = False
    mode_note = skills_note = None
    pending_blocks.clear()
    turn.update(instruction="", excel_read=False)
    context.update(tokens=0, chars=0, compactions=0, cleared=0)


def set_workspace(path: Path, project_id: str, memory_home: Path) -> None:
    """Point the session at a project: its folders, memory and saved conversation."""
    global workspace, cwd, memory_dir, conversation_file, protected_paths, read_roots, read_roots_note
    workspace = cwd = path
    read_roots, read_roots_note = [], None
    memory_dir = memory_home / project_id / "memories"
    conversation_file = memory_home / project_id / "conversation.json"
    protected_paths = [*OWN_FILES, path / ".agent" / "skills"]
