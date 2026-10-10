"""Clean-up of what the agent keeps on the machine, so nothing grows forever.

Runs by itself, once per process: at the start of the coding-agent command, and the first time an
application opens a project (session.open_project). AGENT_CLEANUP=off switches this automatic run
off; an application that prefers to decide when (a server running for weeks might call it daily)
calls cleanup.run() itself.

- Backups (BACKUP_HOME, ~/.coding-agent/backups): the copy of a workbook taken before each change
  is deleted after AGENT_BACKUP_DAYS days (default 3): long enough to undo a change noticed when
  the workbook is checked, without keeping copies of confidential workbooks on the machine.
- Saved conversations (MEMORY_HOME/<project>/conversation.json, used by --resume): deleted after
  AGENT_CONVERSATION_DAYS days (default 30).
- Memory (MEMORY_HOME/<project>/): the whole memory of a project folder not used for
  AGENT_MEMORY_DAYS days (default 90) is deleted. The notes of a project in use are kept; the
  memory curator already keeps them under a size limit.
Nothing here ever touches the projects themselves. A file that cannot be deleted (open elsewhere,
no permission) is left for next time.
"""

from __future__ import annotations

import os
import shutil
import time
from pathlib import Path

BACKUP_DAYS = 3
CONVERSATION_DAYS = 30
MEMORY_DAYS = 90
DAY = 86_400
_ran = False  # the clean-up has run in this process (by run(), automatically or by the application)


def days(variable: str, default: int) -> int:
    """A number of days from the environment (at least 1), or the default."""
    try:
        return max(1, int(os.environ.get(variable) or default))
    except ValueError:
        return default


def older_than(path: Path, days_old: float, now: float) -> bool:
    return now - path.stat().st_mtime > days_old * DAY


def clean_backups(backups: Path, days_old: float, now: float) -> int:
    """Backups older than `days_old`. Returns how many were deleted."""
    if not backups.is_dir():
        return 0
    deleted = 0
    for file in [f for f in backups.rglob("*") if f.is_file()]:
        if older_than(file, days_old, now):
            try:
                file.unlink()
                deleted += 1
            except OSError:
                pass
    for folder in sorted((p for p in backups.rglob("*") if p.is_dir()), key=lambda p: len(p.parts), reverse=True):
        if not any(folder.iterdir()):
            folder.rmdir()
    return deleted


def clean_memory(memory: Path, days_unused: float, now: float) -> int:
    """The whole memory of each project not used for `days_unused` days (its newest file is older).
    Returns how many projects' memories were deleted."""
    if not memory.is_dir():
        return 0
    deleted = 0
    for project in memory.iterdir():
        if not project.is_dir():
            continue
        newest = max((f.stat().st_mtime for f in project.rglob("*") if f.is_file()), default=project.stat().st_mtime)
        if now - newest > days_unused * DAY:
            shutil.rmtree(project, ignore_errors=True)
            deleted += not project.exists()
    return deleted


def clean_conversations(memory: Path, days_old: float, now: float) -> int:
    """Saved conversations older than `days_old`. Returns how many were deleted."""
    deleted = 0
    for file in memory.glob("*/conversation.json") if memory.is_dir() else []:
        if older_than(file, days_old, now):
            try:
                file.unlink()
                deleted += 1
            except OSError:
                pass
    return deleted


def enabled() -> bool:
    """AGENT_CLEANUP=off (or 0, false, no) switches the automatic clean-up off."""
    return (os.environ.get("AGENT_CLEANUP") or "on").strip().lower() not in ("off", "0", "false", "no")


def run_once(backups: Path | None = None, memory: Path | None = None) -> str | None:
    """The clean-up, unless it already ran in this process or AGENT_CLEANUP=off: the summary, or None when it did not run."""
    if _ran or not enabled():
        return None
    return run(backups, memory)


def run(backups: Path | None = None, memory: Path | None = None, now: float | None = None) -> str:
    """The whole clean-up; a one-line summary. Never raises. Given both folders, the agent's settings
    are not loaded (an application may configure them later, before its first call to Claude)."""
    global _ran
    _ran = True
    if backups is None or memory is None:
        from .config import BACKUP_HOME, MEMORY_HOME

        backups = BACKUP_HOME if backups is None else backups
        memory = MEMORY_HOME if memory is None else memory
    now = time.time() if now is None else now
    done = []
    for label, step in [
        ("old backup(s)", lambda: clean_backups(backups, days("AGENT_BACKUP_DAYS", BACKUP_DAYS), now)),
        ("unused project memories", lambda: clean_memory(memory, days("AGENT_MEMORY_DAYS", MEMORY_DAYS), now)),
        ("old saved conversation(s)", lambda: clean_conversations(memory, days("AGENT_CONVERSATION_DAYS",
                                                                                 CONVERSATION_DAYS), now)),
    ]:
        try:
            if count := step():
                done.append(f"{count} {label}")
        except OSError as e:
            done.append(f"{label}: not cleaned ({e})")
    return "Clean-up: " + (", ".join(done) if done else "nothing to delete")
