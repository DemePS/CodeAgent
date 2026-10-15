"""The session: what changes while the agent runs, and the session `seedagent.state` points at.

Everything that changes while the agent runs lives on a `Session` object (the workspace, the current
directory, autonomous mode, the conversation and its bookkeeping), so the terminal and the desktop app
drive the same core, and a host application can build its own (`Session(workspace=...)`).

The tools, the loop and the config read the *current* session through this module:
`state.workspace`, `state.cwd = ...`, `state.ui = ...`. The module is a facade over the current
`Session`, so the historical `seedagent.state` API keeps working unchanged and a tool never has to
receive its session by hand.

The current session is held in a `ContextVar`, not a plain module global: `activate(session)` makes
a session current for the duration of a `with` block, and each turn runs inside one (`session.send`).
Two threads (or async tasks) therefore see their own session -- `state.cwd`, `state.ui`, `state.tool_names`
-- so two sessions can run in parallel in one process, each with its own workspace, conversation and
approvals. `set_current` sets it without a block (the terminal's one session per process).

On top of that, `bind(session)` ties a session to a *thread*: `open_project` binds the session it opens to
the calling thread, so a front end that opens a project and later sends from that thread drives that
project -- another thread opening another project no longer steals it. A worker thread meant to run a
session opened elsewhere binds it itself (`bind`). `running()` is the session a turn is currently running
in (shared across threads), which is what a Stop button from any thread reaches.
"""

from __future__ import annotations

import atexit
import contextvars
import sys
import threading
import types
import weakref
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING

from .config import DEFAULT_CONTEXT_WINDOW, OWN_FILES

if TYPE_CHECKING:
    from .tools.web import _Browser
    from .ui import UI

# Every session alive in this process, weak references so a discarded session is not kept alive.
_sessions_registry: "weakref.WeakSet[Session]" = weakref.WeakSet()


class Session:
    """One agent session: the workspace it works in and everything that changes while it runs.

    A session is created with its workspace (`Session(workspace)`) and driven by a front end through
    seedagent.session (open_project / send). The attributes below are the defaults for a fresh session;
    a front end fills in the rest (its UI, the enabled tools, its own system prompt) after creating it.
    """

    def __init__(self, workspace: Path | str = ".", *, reach_outside: bool = False) -> None:
        path = Path(workspace).expanduser().resolve()
        _sessions_registry.add(self)
        # The UI is attached by the front end (session.open_project sets a LoggedUI); a Session can be built
        # before one exists (state.py builds a default Session at import, when ui.py may be mid-import), so
        # importing UI here would be a circular import. Everything that uses state.ui runs inside a turn.
        self.ui: "UI | None" = None
        # The session's headless browser (seedagent.tools.web._Browser), created on the first web_* call and
        # kept while the session runs. A browser per session: its own thread, its own Chromium and page, so
        # two sessions never share or close each other's browser. Imported only for the annotation: web.py
        # imports this module (a module-level import would be circular).
        self.browser: "_Browser | None" = None
        # The sites this session may browse without asking again. They live on the session, not on the browser
        # object, so web_close (which drops the browser) does not make the person approve the same site twice.
        self.web_approved: set[str] = set()  # hosts approved by answering "yes" to web_open
        self.web_all_sites = False  # the person answered "all sites for this session"
        self.workspace: Path = path  # the project the agent works in
        self.cwd: Path = path  # the agent's current directory inside the workspace; see change_directory
        self.auto_mode = False  # autonomous mode: no approval prompts (deletions and network still ask)
        self.memory_dir: Path | None = None  # this project's memory folder
        self.conversation_file: Path | None = None  # the saved history, used by --resume
        self.skills: dict[str, Path] = {}  # skill name -> its SKILL.md
        self.read_roots: list[Path] = []  # extra folders the agent may read but never write (added by the person)
        self.read_roots_note: str | None = None  # tells Claude about newly added read-only folders
        # The terminal agent asks the person before it reads a folder outside the project (and remembers the
        # answer); an application built on the package leaves this off: it only reads what it was given.
        self.ask_read_outside = reach_outside
        self.read_denied: set[Path] = set()  # folders the person refused this session: not asked again

        # What this session exposes to Claude. None means everything (the coding agent); an application
        # built on the package (e.g. an Excel filler) narrows the tools and brings its own instructions.
        self.tool_names: set[str] | None = None  # the enabled tools, or None for all
        # Workbook (resolved path) -> the only sheets edit_excel may change in it; set by an application
        # (e.g. the sheets the person ticked). Workbooks not listed: any sheet.
        self.excel_edit_sheets: dict[Path, set[str]] = {}
        self.excel_writable: set[Path] | None = None  # when set, edit_excel may change only these workbooks
        # Workbook (resolved path) -> {sheet: its number of columns}: edit_excel may not write beyond them.
        self.excel_max_columns: dict[Path, dict[str, int]] = {}
        # True: edit_excel never writes a formula, and never overwrites or clears one.
        self.excel_protect_formulas: bool = False
        # False: format_excel, add_chart, add_table and add_pivot_table are refused.
        self.excel_allow_format: bool = True
        # Require read_excel before read_pdf when the instruction mentions a spreadsheet.
        self.excel_first = False
        self.system_prompt: str | None = None  # replaces the coding agent's system prompt ({workspace} is filled in)

        # Paths the agent must never modify: its own files, plus the project's .agent/skills folder.
        self.protected_paths: list[Path] = list(OWN_FILES)

        self.mode_note: str | None = None  # tells Claude about a mode change with the next instruction
        self.skills_note: str | None = None  # an updated skill list after /skills, sent with the next instruction
        self.memory_sent = False  # memory and the skill list go with the first instruction of each session
        self.pending_blocks: list[str] = []  # e.g. a summary from /compact, sent with the next instruction
        self.compacted_this_turn = False  # a compaction replaced the history during the current instruction
        self.always_allow_python = False  # the user answered [a]lways to a run_python prompt
        self.always_allow_az = False  # the user answered [a]lways to a tool_az prompt
        self.always_allow_uv = False  # the user answered [a]lways to a run_uv prompt
        self.stop_requested = False  # set by session.stop() from another thread (e.g. a Stop button)
        self.pdf_read_as_text = False  # a PDF page read in text mode opens read_pdf's visual mode

        self.turn = {"instruction": "", "excel_read": False}  # the current instruction; reset for each one
        self.context_window: int = DEFAULT_CONTEXT_WINDOW  # tokens; lowered if the API reports a smaller window
        self.context = {"tokens": 0, "chars": 0, "compactions": 0, "cleared": 0}  # size at the last API call

        # The conversation of this session, in the shape the API sends (a list of message dicts). It lives
        # here, on the session, so two sessions in one process do not share one history. The front end holds
        # the same list object and mutates it in place (never rebinds it).
        self.messages: list = []

    def tool_enabled(self, name: str) -> bool:
        return self.tool_names is None or name in self.tool_names

    def reset_conversation(self) -> None:
        """Forget the bookkeeping of the previous conversation (a new project or a fresh start)."""
        self.memory_sent = self.compacted_this_turn = self.stop_requested = False
        self.always_allow_python = self.always_allow_az = self.always_allow_uv = False
        self.pdf_read_as_text = False
        self.mode_note = self.skills_note = None
        self.pending_blocks = []
        self.turn = {"instruction": "", "excel_read": False}
        self.excel_writable = None
        self.context = {"tokens": 0, "chars": 0, "compactions": 0, "cleared": 0}

    def set_workspace(self, path: Path, project_id: str, memory_home: Path) -> None:
        """Point the session at a project: its folders, memory and saved conversation."""
        self.workspace = self.cwd = path
        self.read_roots, self.read_roots_note = [], None
        self.read_denied = set()
        self.memory_dir = memory_home / project_id / "memories"
        self.conversation_file = memory_home / project_id / "conversation.json"
        self.protected_paths = [*OWN_FILES, path / ".agent" / "skills"]


_default_session = Session()  # so `state` always has a session, even before a front end makes one
_current: contextvars.ContextVar[Session] = contextvars.ContextVar("seedagent_session", default=_default_session)
_running_session: Session | None = None  # the session a turn is running in, shared across threads (see running())
# The session each thread opened with open_project (or bound with bind()). A front end that opens a
# project and later sends from another thread should bind that thread with bind(session); a front end
# that opens and sends in the same thread gets it for free. Kept apart from _current, which activate()
# changes and restores around a turn.
_bound: "weakref.WeakKeyDictionary[threading.Thread, Session]" = weakref.WeakKeyDictionary()


def bind(session: Session) -> Session:
    """Tie `session` to the calling thread: a later send() from this thread drives it.

    open_project calls this, so the thread that opened a project is the thread that runs its turns --
    another thread opening another project no longer steals the first one's session. A worker thread
    that will run a session opened elsewhere calls bind(session) itself."""
    _bound[threading.current_thread()] = session
    return session


def bound() -> Session | None:
    """The session the calling thread bound (open_project / bind), or None if it bound none."""
    return _bound.get(threading.current_thread())


def close_session(session: Session) -> None:
    """Drop a session's thread binding when it is done, so a worker thread does not hold it alive; close
    its browser if one is open (close() is idempotent). Doing so releases the browser slot (see
    AGENT_MAX_BROWSERS), so a front end that finished with a session should call this."""
    for thread, bound_session in list(_bound.items()):
        if bound_session is session:
            del _bound[thread]
    browser = session.browser
    if browser is not None:
        session.browser = None  # first: even if close() raises, the session no longer points at it
        try:
            browser.close()
        except Exception:
            pass  # a browser already gone (process ending) must not break closing a session


@atexit.register
def _close_browsers() -> None:
    """Close every session's browser when the process ends (replaces the old global atexit)."""
    for session in list(_sessions_registry):
        close_session(session)


def current() -> Session:
    """The session the tools and the loop read: what `seedagent.state` stands for, in this thread/task."""
    return _current.get()


def set_current(session: Session) -> None:
    """Make `session` the current one for this thread/task (the terminal's one session per process)."""
    _current.set(session)


@contextmanager
def activate(session: Session):
    """Make `session` current for the duration of the block, then restore the previous one.

    Each turn runs inside one (`session.send`), so a tool, an approval or a path check resolves the
    session that owns the turn -- two threads running two sessions never see each other's state.
    Also marks `session` as the running one, so stop() from any thread reaches it."""
    token = _current.set(session)
    previous = _running_session
    globals()["_running_session"] = session
    try:
        yield session
    finally:
        globals()["_running_session"] = previous
        _current.reset(token)


def running() -> Session:
    """The session a turn is currently running in, or the current one outside a turn.

    `stop()` (a Stop button, a Timer) may be called from any thread, whose context has no session:
    it must reach the session that is actually running, which is what this returns."""
    return _running_session if _running_session is not None else current()


def reset_conversation() -> None:
    """Forget the bookkeeping of the previous conversation (a new project or a fresh start)."""
    current().reset_conversation()


def set_workspace(path: Path, project_id: str, memory_home: Path) -> None:
    """Point the current session at a project: its folders, memory and saved conversation."""
    current().set_workspace(path, project_id, memory_home)


def tool_enabled(name: str) -> bool:
    return current().tool_enabled(name)


def for_messages(messages: list) -> Session:
    """The session that owns `messages`: the thread's bound session if it is the one, else the session whose
    conversation is `messages`, else the current session.

    Lets the loop resolve its session from the history it was given, so any caller (cli.py, session.send,
    a host app) drives the right session without passing it along."""
    session = bound()
    if session is not None and session.messages is messages:
        return session
    for session in (*_sessions_registry, current()):
        if session.messages is messages:
            return session
    return current()


class _StateModule(types.ModuleType):
    """`seedagent.state` is the current Session seen through the module: `state.workspace`,
    `state.cwd = ...`, `state.ui = ...` reach the session's attributes, so the historical API and the
    Session object stay one and the same state (no second copy to keep in sync). The current session is
    looked up at each access, so the same module serves two sessions running in parallel."""

    def __getattr__(self, name: str):
        if name.startswith("__"):
            raise AttributeError(name)
        return getattr(_current.get(), name)

    def __setattr__(self, name: str, value) -> None:
        if name.startswith("__"):
            object.__setattr__(self, name, value)
        else:
            setattr(_current.get(), name, value)

    def __dir__(self):
        return sorted(set(super().__dir__()) | set(vars(_current.get())))


sys.modules[__name__].__class__ = _StateModule
