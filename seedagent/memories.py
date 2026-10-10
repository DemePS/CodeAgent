"""The agent's memory of each project, for developers: list it, show it, delete it.

    coding-agent --memories                 every project's memory: name, last used, size, notes
    coding-agent --memories NAME            one project's notes, in full
    coding-agent --forget NAME              delete one project's memory (asks first)
    coding-agent --forget all               delete every project's memory (asks first)
    coding-agent --forget . -d path         the memory of the project folder given with -d

NAME is a memory folder's name (e.g. myapp-1a2b3c4d), or the start of it (e.g. myapp) when only
one matches. The memory folder is MEMORY_HOME (~/.coding-agent/memory, or AGENT_MEMORY_DIR); it is
shared by every application built on the agent (e.g. the Excel filler).
"""

from __future__ import annotations

import datetime
import shutil
from pathlib import Path


def entries(memory_home: Path) -> list[dict]:
    """Each project's memory, most recently used first: name, path, last used, size, notes, and
    whether a conversation is saved."""
    found = []
    for folder in memory_home.iterdir() if memory_home.is_dir() else []:
        if not folder.is_dir():
            continue
        files = [f for f in folder.rglob("*") if f.is_file()]
        notes = folder / "memories" / "notes.md"
        found.append({
            "name": folder.name,
            "path": folder,
            "last_used": datetime.datetime.fromtimestamp(max((f.stat().st_mtime for f in files),
                                                             default=folder.stat().st_mtime)),
            "size": sum(f.stat().st_size for f in files),
            "notes": notes.read_text(encoding="utf-8", errors="replace") if notes.is_file() else "",
            "conversation": (folder / "conversation.json").is_file(),
        })
    return sorted(found, key=lambda e: e["last_used"], reverse=True)


def find(memory_home: Path, name: str) -> list[dict]:
    """The memories matching NAME: the exact folder name, else the folders starting with it."""
    all_ = entries(memory_home)
    exact = [e for e in all_ if e["name"] == name]
    return exact or [e for e in all_ if e["name"].lower().startswith(name.lower())]


def listing(memory_home: Path, current: str | None = None) -> str:
    """Every project's memory as a table; `current` (a folder name) is marked."""
    found = entries(memory_home)
    if not found:
        return f"No memory yet in {memory_home}."
    lines = [f"Memory folder: {memory_home}", ""]
    width = max(len(e["name"]) for e in found)
    for e in found:
        first = next((line.strip("# ").strip() for line in e["notes"].splitlines() if line.strip()), "")
        lines.append(f"{'*' if e['name'] == current else ' '} {e['name']:<{width}}  {e['last_used']:%Y-%m-%d %H:%M}  "
                     f"{e['size'] / 1024:6.1f} KB  {'notes' if e['notes'] else 'no notes':8}"
                     f"{'  conversation' if e['conversation'] else ''}"
                     + (f"  -- {first[:60]}" if first else ""))
    if any(e["name"] == current for e in found):
        lines.append("\n* the project given with -d")
    lines.append("\nShow one: coding-agent --memories NAME   Delete: coding-agent --forget NAME (or all)")
    return "\n".join(lines)


def show(memory_home: Path, name: str) -> str:
    found = find(memory_home, name)
    if len(found) != 1:
        return ambiguous(name, found)
    e = found[0]
    return (f"{e['name']}  ({e['path']})\nlast used {e['last_used']:%Y-%m-%d %H:%M}, "
            f"{'a saved conversation' if e['conversation'] else 'no saved conversation'}\n\n"
            + (e["notes"].strip() or "(no notes)"))


def ambiguous(name: str, found: list[dict]) -> str:
    if not found:
        return f"No memory named {name!r}. List them with: coding-agent --memories"
    return f"{name!r} matches several memories: {', '.join(e['name'] for e in found)}. Give the full name."


def forget(memory_home: Path, name: str, confirm=input) -> str:
    """Delete one project's memory, or all ("all"), after confirmation. Returns what was done."""
    targets = entries(memory_home) if name == "all" else find(memory_home, name)
    if name != "all" and len(targets) != 1:
        return ambiguous(name, targets)
    if not targets:
        return f"No memory to delete in {memory_home}."
    names = ", ".join(e["name"] for e in targets)
    answer = confirm(f"Delete the memory of {names} ({len(targets)} project(s))? The agent forgets what it "
                     "learned there; this cannot be undone. [y/N] ")
    if answer.strip().lower() not in ("y", "yes", "o", "oui"):
        return "Nothing deleted."
    for e in targets:
        shutil.rmtree(e["path"], ignore_errors=True)
    return f"Deleted the memory of {names}."
