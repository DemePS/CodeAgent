"""How the agent talks to the person: the UI interface, and its terminal implementation.

The core (loop, tools, safety rules) never prints or reads input itself; it calls `state.ui`.
TerminalUI renders to a terminal (colors, clickable `path:line` links, paste-safe prompts); the
desktop app provides another implementation that sends the same calls to its window.
"""

from __future__ import annotations

import os
import re
import select
import sys
import time
from pathlib import Path

from . import state
from .config import EDITOR, FILE_REF, LINKS_ENABLED


class UI:
    """Everything the core shows or asks. Subclasses render it; questions block until answered."""

    # --- one-line notices
    def status(self, text: str) -> None:
        """Progress of a tool or of the agent itself, e.g. "[git] status"."""

    def success(self, text: str) -> None:
        """A change was applied, e.g. "Modified app.py"."""

    def failure(self, text: str) -> None:
        """A change was not applied, e.g. "app.py was not modified"."""

    def warning(self, text: str) -> None:
        """Something the person must notice before approving."""

    def message(self, text: str) -> None:
        """Plain information for the person (setup, resume summary, stop reasons)."""

    def error(self, text: str) -> None:
        """Something failed and the instruction could not complete (e.g. Claude is unreachable)."""
        self.message(f"[error] {text}")

    # --- what a tool is about to do
    def panel(self, title: str, lines: list[str] | tuple = (), tone: str = "change") -> None:
        """A titled block: tone is change, danger (deletions), network, run or question."""

    def diff(self, action: str, name: str, path: Path, first_line: int, diff_lines: list[str]) -> None:
        """A unified diff of a text file about to be created ("Create") or modified ("Modify")."""

    def cell_changes(self, title: str, rows: list[tuple[str, str, str, str | None]], more: int) -> None:
        """Workbook cells about to change: (cell, old, new, number format), plus how many are not shown."""

    # --- questions (block until the person answers)
    def confirm(self, question: str, choices: tuple[str, ...] = ("yes", "no")) -> str:
        """Ask to choose one of choices; returns the chosen one."""
        raise NotImplementedError

    def ask_text(self, prompt: str, multiline: bool = False) -> str:
        """Ask for free text (feedback after a refusal, an answer to Claude's question)."""
        raise NotImplementedError

    # --- the model's streamed reply
    def assistant_start(self) -> None:
        """Claude starts writing text."""

    def assistant_text(self, text: str) -> None:
        """A chunk of Claude's text."""

    def thinking(self) -> None:
        """Claude is thinking (the content is not shown)."""

    def tool_start(self, name: str) -> None:
        """Claude calls a tool (arguments follow in tool_detail)."""

    def tool_detail(self, text: str) -> None:
        """A short summary of a tool call's arguments or of a server tool's result."""

    def assistant_end(self) -> None:
        """The response is complete."""


# ------------------------------------------------------------------------------------ terminal

def file_link(p: Path, line: int, label: str) -> str:
    """Wrap label in a terminal hyperlink that opens p at the given line."""
    if not LINKS_ENABLED:
        return label
    posix = p.as_posix()
    if EDITOR in ("vscode", "cursor"):
        url = f"{EDITOR}://file{'' if posix.startswith('/') else '/'}{posix}:{line}"
    else:
        url = p.as_uri()  # plain file:// links cannot carry a line number
    return f"\033]8;;{url}\033\\{label}\033]8;;\033\\"


def linkify(text: str) -> str:
    """Turn every `path:line` that names an existing workspace file into a clickable link."""
    if not LINKS_ENABLED:
        return text

    def replace(m: re.Match) -> str:
        for base in (state.workspace, state.cwd):  # usually root-relative, but accept either
            try:
                p = (base / m.group(1)).resolve()
            except (OSError, ValueError):
                continue
            if (p == state.workspace or state.workspace in p.parents) and p.is_file():
                return file_link(p, int(m.group(2)), m.group(0))
        return m.group(0)

    return FILE_REF.sub(replace, text)


class LinkedPrinter:
    """Prints streamed text word by word, so a `path:line` split across chunks still gets linked."""

    def __init__(self) -> None:
        self.pending = ""

    def write(self, text: str) -> None:
        self.pending += text
        cut = max(self.pending.rfind(c) for c in " \n\t")
        if cut >= 0:
            print(linkify(self.pending[:cut + 1]), end="", flush=True)
            self.pending = self.pending[cut + 1:]

    def flush(self) -> None:
        if self.pending:
            print(linkify(self.pending), end="", flush=True)
            self.pending = ""


def colorize_diff(diff_lines: list[str]) -> str:
    out = []
    for line in diff_lines:
        if line.startswith(("+++", "---")):
            out.append(f"\033[1m{line}\033[0m")
        elif line.startswith("+"):
            out.append(f"\033[32m{line}\033[0m")
        elif line.startswith("-"):
            out.append(f"\033[31m{line}\033[0m")
        elif line.startswith("@@"):
            out.append(f"\033[36m{line}\033[0m")
        else:
            out.append(line)
    return "\n".join(out)


def pending_input() -> bool:
    """True when more typed or pasted input is already waiting (a multi-line paste arrives at once)."""
    if not sys.stdin.isatty():
        return False
    try:
        if os.name == "nt":
            import msvcrt
            time.sleep(0.05)  # let the console receive the rest of the paste
            return msvcrt.kbhit()
        return bool(select.select([sys.stdin], [], [], 0.05)[0])
    except (OSError, ValueError):
        return False


def discard_pending_input() -> None:
    """Drop anything typed or pasted ahead, so it cannot answer the prompt that follows."""
    if not sys.stdin.isatty():
        return
    try:
        if os.name == "nt":
            import msvcrt
            while msvcrt.kbhit():
                msvcrt.getwch()
        else:
            import termios
            termios.tcflush(sys.stdin, termios.TCIFLUSH)
    except (OSError, ValueError, ImportError):
        pass


def read_text(prompt: str) -> str:
    """Read one message, which may be several lines: a paste, or a block between \"\"\" lines."""
    first = input(prompt)
    if first.strip() == '"""':
        lines = []
        while (line := input()).strip() != '"""':
            lines.append(line)
        return "\n".join(lines)
    lines = [first]
    while pending_input():  # the rest of a multi-line paste
        lines.append(input())
    return "\n".join(lines)


TONES = {"change": "1;33", "danger": "1;31", "network": "1;35", "run": "1;33", "question": "1;35"}


class TerminalUI(UI):
    def __init__(self) -> None:
        self._out: LinkedPrinter | None = None

    def status(self, text: str) -> None:
        print(f"\033[2m{text}\033[0m", flush=True)

    def success(self, text: str) -> None:
        print(f"\033[32m✔ {text}\033[0m")

    def failure(self, text: str) -> None:
        print(f"\033[33m✘ {text}\033[0m")

    def warning(self, text: str) -> None:
        print(f"\033[1;31m  WARNING: {text}\033[0m")

    def message(self, text: str) -> None:
        print(linkify(text))

    def error(self, text: str) -> None:
        print(f"\033[1;31m[error]\033[0m {text}")

    def panel(self, title: str, lines=(), tone: str = "change") -> None:
        if tone == "question":
            print(f"\n\033[{TONES[tone]}m[agent asks]\033[0m {linkify(title)}")
            return
        print(f"\n\033[{TONES.get(tone, '1;33')}m=== {title} ===\033[0m")
        for line in lines:
            print(line if tone == "run" else f"  {line}")

    def diff(self, action: str, name: str, path: Path, first_line: int, diff_lines: list[str]) -> None:
        target = file_link(path, first_line, f"{name}:{first_line}") if action == "Modify" else name
        print(f"\n\033[1;33m=== {action} \033[0m{target}\033[1;33m ===\033[0m")
        print(colorize_diff(diff_lines))

    def cell_changes(self, title: str, rows, more: int) -> None:
        print(f"\n\033[1;33m=== {title} ===\033[0m")
        for cell_ref, old, new, fmt in rows:
            old_part = f"\033[31m{old}\033[0m" if old else "\033[2m(empty)\033[0m"
            new_part = f"\033[32m{new}\033[0m" if new else "\033[2m(empty)\033[0m"
            print(f"  {cell_ref:>16}  {old_part} -> {new_part}" + (f"  \033[2m[{fmt}]\033[0m" if fmt else ""))
        if more:
            print(f"  ... and {more} more cell(s)")

    def confirm(self, question: str, choices: tuple[str, ...] = ("yes", "no")) -> str:
        """[y]es / [n]o style prompt; anything typed or pasted before it appeared is ignored."""
        options = " / ".join(f"[{c[0]}]{c[1:]}" for c in choices)
        discard_pending_input()
        answer = input(f"{question} {options}: ").strip().lower()
        for choice in choices:
            if answer in (choice, choice[0]):
                return choice
        return "no" if "no" in choices else choices[-1]

    def ask_text(self, prompt: str, multiline: bool = False) -> str:
        discard_pending_input()
        return (read_text(prompt) if multiline else input(prompt)).strip()

    # --- streaming
    def assistant_start(self) -> None:
        self._out = LinkedPrinter()
        print("\n\033[1;34mClaude:\033[0m ", end="", flush=True)

    def assistant_text(self, text: str) -> None:
        if self._out is None:
            self._out = LinkedPrinter()
        self._out.write(text)

    def _flush(self) -> None:
        if self._out:
            self._out.flush()

    def thinking(self) -> None:
        self._flush()
        print("\n\033[2m(thinking...)\033[0m", end="", flush=True)

    def tool_start(self, name: str) -> None:
        self._flush()
        print(f"\n\033[2m-> {name}\033[0m", end="", flush=True)

    def tool_detail(self, text: str) -> None:
        print(f"\033[2m{text}\033[0m", end="", flush=True)

    def assistant_end(self) -> None:
        self._flush()
        self._out = None
        print()


class HeadlessUI(UI):
    """No person attached (tests, scripts): shows nothing and refuses every approval."""

    def confirm(self, question: str, choices: tuple[str, ...] = ("yes", "no")) -> str:
        return "no" if "no" in choices else choices[-1]

    def ask_text(self, prompt: str, multiline: bool = False) -> str:
        return ""
