"""Up arrow in the terminal: the questions you typed before, also from earlier runs.

Uses `readline` (Linux, macOS, WSL). Windows has no `readline` module, but its console already
recalls what was typed in the running session. Only the questions at the "You:" prompt are kept:
answers to approvals and the agent's own questions stay out of the history.
"""

from __future__ import annotations

import atexit
import re
import sys

from .config import AGENT_HOME

HISTORY_FILE = AGENT_HOME / "history"
HISTORY_LENGTH = 500
_readline = None


def enable() -> bool:
    """Turn on line editing and history for input(); False when this system has no readline."""
    global _readline
    if _readline is not None:
        return True
    if not sys.stdin.isatty():
        return False
    try:
        import readline
    except ImportError:
        return False
    readline.set_auto_history(False)  # only remember what remember() is given
    readline.set_history_length(HISTORY_LENGTH)
    try:
        readline.read_history_file(HISTORY_FILE)
    except (OSError, UnicodeDecodeError):
        pass  # no history yet, or an unreadable file: start empty
    _readline = readline
    atexit.register(save)
    return True


def remember(text: str) -> None:
    """Add a question (its first line, which is what the prompt can recall) to the history."""
    if _readline is None:
        return
    line = text.strip().splitlines()[0] if text.strip() else ""
    if not line or line.startswith('"""'):
        return
    length = _readline.get_current_history_length()
    if length and _readline.get_history_item(length) == line:
        return  # the same question twice in a row
    _readline.add_history(line)


def save() -> None:
    if _readline is None:
        return
    try:
        HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
        _readline.write_history_file(HISTORY_FILE)
    except OSError:
        pass


def prompt(text: str) -> str:
    """A prompt with colors: readline must be told the colour codes take no room on the line."""
    return re.sub(r"(\033\[[0-9;]*m)", "\001\\1\002", text) if _readline is not None else text
