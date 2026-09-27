"""Personal coding agent: Claude on Azure (Microsoft Foundry) with a manual tool-use loop.

Tools:
  - list_directory   : list the folders and files in a directory
  - change_directory : move the agent's current directory (never outside the workspace)
  - grep        : regex search across files in the workspace
  - read_file   : read a file (optionally a line range)
  - edit_file   : replace an exact snippet in a file -- shows a diff and asks permission first
  - write_file  : create/overwrite a file -- shows a diff and asks permission first
                  (edit_file and write_file never touch the agent's own source: this file)
  - ask_human   : lets the model ask you a question mid-task
  - run_python  : run a Python snippet, script or module (e.g. pytest) -- asks permission first
                  (uses `uv run` when uv is installed, so the project's own environment is used;
                  the code cannot start subprocesses -- see GUARD_SOURCE)
  - memory      : Anthropic's memory tool -- notes the agent keeps about each project across runs,
                  stored in ~/.coding_agent/memory/<project>/ (override with AGENT_MEMORY_DIR)
  - load_skill  : load a skill's full instructions when a task matches it
  - web_search  : Anthropic's server-side web search (runs on Anthropic's side; nothing executes
                  locally). AGENT_WEB_SEARCH=20250305 (default; the only version on Foundry
                  deployments hosted on Azure), 20260209 (better filtering; Anthropic-hosted
                  deployments), or off.

Skills are folders with a SKILL.md (a `name` / `description` header, then instructions), found in:
    skills/ next to this file            -- shipped with the agent
    ~/.coding_agent/skills/              -- personal, every project
    <project>/.agent/skills/             -- per project (can be committed)
A later location overrides an earlier one with the same skill name. Only names and descriptions
are sent up front; Claude loads a skill's instructions when it needs them.

Configuration (environment variables or a .env file):
    ANTHROPIC_FOUNDRY_ENDPOINT     https://<resource>.services.ai.azure.com/anthropic
    ANTHROPIC_FOUNDRY_API_KEY      API key; leave unset to sign in with Azure AD (azure-identity)
    ANTHROPIC_FOUNDRY_DEPLOYMENT   your Claude deployment name

Usage:
    python agent.py -d path/to/project "Add input validation to the CLI"
    python agent.py -d path/to/project "..." -i   # keep chatting after the task
    python agent.py -d path/to/project            # interactive mode only
    python agent.py -d path/to/project -r         # resume the last conversation in this project

`path:line` references in the output are clickable links that open the file at that line.
Set AGENT_EDITOR to vscode (default), cursor, file, or none.
"""

import argparse
import difflib
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from functools import lru_cache
from pathlib import Path

import anthropic
from anthropic import AnthropicFoundry
from anthropic.tools.memory import BetaLocalFilesystemMemoryTool
from dotenv import load_dotenv

load_dotenv()  # before reading any configuration below


@lru_cache(maxsize=1)
def _get_client() -> AnthropicFoundry:
    """AnthropicFoundry client: API key if ANTHROPIC_FOUNDRY_API_KEY is set, otherwise Azure AD."""
    api_key = os.environ.get("ANTHROPIC_FOUNDRY_API_KEY")
    if api_key:
        return AnthropicFoundry(
            api_key=api_key,
            base_url=os.environ["ANTHROPIC_FOUNDRY_ENDPOINT"],
            max_retries=2,
        )
    from azure.identity import DefaultAzureCredential, get_bearer_token_provider

    scope = os.environ.get("TOKEN_SCOPE", "https://ai.azure.com/.default")
    token_provider = get_bearer_token_provider(DefaultAzureCredential(), scope)
    return AnthropicFoundry(
        azure_ad_token_provider=token_provider,
        base_url=os.environ["ANTHROPIC_FOUNDRY_ENDPOINT"],
        max_retries=2,
    )


# On Foundry this is your *deployment name*; change it if yours differs.
MODEL = os.environ.get("ANTHROPIC_FOUNDRY_DEPLOYMENT", "claude-opus-5")
MAX_TOKENS = 64000  # safe with streaming (no HTTP timeout risk)
MAX_TOOL_OUTPUT_CHARS = 50_000
RUN_TIMEOUT_SECONDS = 120
UV = shutil.which("uv")  # None when uv is not installed
SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", ".mypy_cache", ".pytest_cache"}

WORKSPACE = Path(".").resolve()  # set from --dir in main()
CWD = WORKSPACE  # the agent's current directory inside the workspace; see change_directory
MAX_LISTING_ENTRIES = 500

MEMORY_HOME = Path(os.environ.get("AGENT_MEMORY_DIR", "~/.coding_agent/memory")).expanduser()
BUNDLED_SKILLS = Path(__file__).resolve().parent / "skills"
PERSONAL_SKILLS = Path("~/.coding_agent/skills").expanduser()
skills: dict[str, Path] = {}  # skill name -> its SKILL.md; filled in main()
memory_tool: BetaLocalFilesystemMemoryTool | None = None  # set per project in main()
conversation_file: Path | None = None  # set per project in main(); used by --resume

# Clickable `path:line` links in the terminal (OSC 8 hyperlinks).
EDITOR = os.environ.get("AGENT_EDITOR", "vscode").lower()
LINKS_ENABLED = EDITOR != "none" and sys.stdout.isatty()
FILE_REF = re.compile(r"((?:[A-Za-z]:[\\/])?[\w.\-/\\]+\.[A-Za-z0-9]+):(\d+)")

# Anthropic's web search tool version: 20250305 works everywhere on Foundry, 20260209 only on
# Anthropic-hosted deployments. "off" removes the tool (e.g. if your organization disabled it).
WEB_SEARCH = (os.environ.get("AGENT_WEB_SEARCH") or "20250305").strip().lower()
if WEB_SEARCH not in ("20250305", "20260209", "off"):
    raise SystemExit(f"AGENT_WEB_SEARCH must be 20250305, 20260209 or off (got {WEB_SEARCH!r})")
WEB_SEARCH_MAX_USES = 5  # searches allowed per model response

# The agent must never modify its own source code or its skills (project skills are added in main()).
PROTECTED_PATHS = [Path(__file__).resolve(), BUNDLED_SKILLS]

SYSTEM_PROMPT = """You are a coding agent working in the repository at {workspace}.
You have a current directory inside it, which starts at the repository root each session;
relative paths in every tool resolve against it. Use list_directory to explore and
change_directory to move around -- you can never leave the repository.

Use grep and read_file to understand the code before changing it. Read a file before
you change it. To change an existing file, use edit_file with an old_string copied exactly
from the file (without the line-number prefix) and enough surrounding lines to be unique.
Use write_file only to create a new file or to rewrite most of a file. The user sees a
diff and must approve every change. If the user rejects a change, read their feedback
and adjust rather than retrying the same edit. When a requirement is ambiguous or a
decision is genuinely the user's to make, use ask_human instead of guessing.
Never modify your own source code (the coding agent's files); those writes are refused.

You have a persistent memory directory, /memories, private to this project and kept between
sessions. Its current contents are given to you in a <memory> block with the first instruction
of each session, so do not view it again. Update memory at most once per task, at the very end,
and only if you learned something durable and new that a future session would need: build/test
commands, project conventions and structure, the user's preferences and corrections, decisions
with their reasons. Keep everything in /memories/notes.md, make small edits (str_replace or
insert) rather than rewriting it, and skip the update entirely when nothing new was learned.
Never store secrets such as API keys, passwords or tokens.

After changing code, verify it with run_python: run the tests (e.g. args ["-m", "pytest", "-q"]),
the script you changed, or a small snippet that exercises it. If it fails, read the error,
fix the code, and run it again. Code run this way cannot start subprocesses; if a test needs
one, say so instead of trying to work around the block. When an error comes from an installed
library, read that library's source in the project's .venv (grep with path=".venv" or
include_ignored, plus a glob such as "*.py", then read_file) instead of guessing how it works.

Skills: the first instruction of each session also carries a <skills> block listing expert
playbooks by name and description. When a task falls in a skill's area, call load_skill for it
before starting and follow it; load several when a task spans areas. Do not load skills that
are not relevant.

Web search (when available): use it for things the repository cannot tell you -- current
library or framework documentation and versions, error messages from third-party code, Azure
service behavior and limits. Prefer official documentation, check that what you find matches
the versions the project uses, and cite the URLs you relied on. Never put secrets, credentials
or proprietary code in a search query. Do not search for things you can find in the repository.

When you refer to a specific place in the code, write it as path:line (for example
src/app.py:42) with the path relative to the repository root -- the user can click it."""

TOOLS = [
    {"type": "memory_20250818", "name": "memory"},  # Anthropic-defined: no input_schema
    {
        "name": "load_skill",
        "description": (
            "Load the full instructions of a skill listed in the <skills> block, e.g. before an "
            "Azure architecture, backend or frontend task. Returns the skill's SKILL.md."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"name": {"type": "string", "description": "Skill name exactly as listed."}},
            "required": ["name"],
        },
    },
    {
        "name": "list_directory",
        "description": (
            "List the folders (ending in /) and files (with sizes) in a directory, one level deep. "
            "Hides .git, virtual environments, node_modules and caches inside the listed directory, "
            "but you can list them directly (e.g. path='.venv/lib')."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Directory to list, relative to the current directory. Defaults to '.'.",
                },
            },
        },
    },
    {
        "name": "change_directory",
        "description": (
            "Change the current directory. Later relative paths in all tools, and run_python, use it. "
            "Must stay inside the repository; '/' goes back to the repository root. Returns the new "
            "current directory relative to the root."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "Target directory, relative to the current one."}},
            "required": ["path"],
        },
    },
    {
        "name": "grep",
        "description": (
            "Search file contents in the workspace with a Python regular expression. "
            "Returns matching lines as 'path:line_number: text'. Use this to locate "
            "definitions, usages, or strings before reading files. Skips .git, virtual environments "
            "(.venv), node_modules and caches, unless path points inside one of them (e.g. "
            "'.venv/lib') or include_ignored is true -- useful for reading an installed library's "
            "source while debugging."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Python regex to search for."},
                "path": {
                    "type": "string",
                    "description": "File or directory to search, relative to the current directory. Defaults to '.'.",
                },
                "glob": {
                    "type": "string",
                    "description": "Only search files whose name matches this glob, e.g. '*.py'.",
                },
                "ignore_case": {"type": "boolean", "description": "Case-insensitive match."},
                "include_ignored": {
                    "type": "boolean",
                    "description": "Also search virtual environments, node_modules and caches (.git is always skipped).",
                },
            },
            "required": ["pattern"],
        },
    },
    {
        "name": "read_file",
        "description": (
            "Read a text file from the workspace. Output lines are prefixed with line numbers. "
            "Optionally pass start_line/end_line (1-indexed, inclusive) to read part of a large file."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path relative to the current directory."},
                "start_line": {"type": "integer", "minimum": 1},
                "end_line": {"type": "integer", "minimum": 1},
            },
            "required": ["path"],
        },
    },
    {
        "name": "edit_file",
        "description": (
            "Edit an existing file by replacing an exact snippet. old_string must match the file "
            "exactly (whitespace and indentation included, without read_file's line-number prefix) "
            "and must occur exactly once unless replace_all is true -- include surrounding lines "
            "to make it unique. The user is shown a unified diff and must approve before anything "
            "is written; if they decline, the result contains their feedback."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path relative to the current directory."},
                "old_string": {"type": "string", "description": "Exact text to replace."},
                "new_string": {"type": "string", "description": "Replacement text."},
                "replace_all": {
                    "type": "boolean",
                    "description": "Replace every occurrence instead of requiring a unique match.",
                },
            },
            "required": ["path", "old_string", "new_string"],
        },
    },
    {
        "name": "write_file",
        "description": (
            "Create a new file, or overwrite a file with the given full content. Prefer edit_file "
            "for changes to an existing file. The user is shown a "
            "unified diff and must approve before anything is written; if they decline, the "
            "result contains their feedback."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path relative to the current directory."},
                "content": {"type": "string", "description": "The complete new file content."},
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "ask_human",
        "description": (
            "Ask the user a question and wait for their answer. Use for clarifications, "
            "choosing between approaches, or information only the user has."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"question": {"type": "string"}},
            "required": ["question"],
        },
    },
    {
        "name": "run_python",
        "description": (
            "Run Python in the current directory to check code for bugs, and return the exit code, "
            "stdout and stderr. Pass either `code` (a snippet, run like `python -c`) or `args` "
            "(arguments after `python`: a script and its arguments, or -m and a module, e.g. "
            "[\"script.py\"], [\"-m\", \"pytest\", \"-q\"], [\"-m\", \"py_compile\", \"app.py\"]). "
            "The code cannot start subprocesses (subprocess, os.system, multiprocessing, ... raise "
            "PermissionError), so do not rely on them. The user must approve every run."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "code": {"type": "string", "description": "Python source to execute."},
                "args": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Arguments passed to the Python interpreter.",
                },
                "timeout": {
                    "type": "integer",
                    "minimum": 1,
                    "description": f"Seconds before the run is killed (default {RUN_TIMEOUT_SECONDS}).",
                },
            },
        },
    },
] + (
    [] if WEB_SEARCH == "off"
    else [{"type": f"web_search_{WEB_SEARCH}", "name": "web_search", "max_uses": WEB_SEARCH_MAX_USES}]
)


# Bootstrap that run_python executes instead of the code directly. It installs a CPython audit
# hook (PEP 578) that blocks every way of starting another process, then runs the snippet,
# script or module. Audit hooks cannot be removed from Python code once installed.
# Limits: this guards Python code, not native extensions that call the OS directly -- for
# real isolation run the agent in a container.
GUARD_SOURCE = r"""
import re, runpy, sys

BLOCKED_EVENTS = {
    "subprocess.Popen", "os.system", "os.exec", "os.spawn", "os.posix_spawn",
    "os.fork", "os.forkpty", "os.startfile", "_winapi.CreateProcess",
}
# Process-starting C functions reachable through ctypes (libc / kernel32 / shell32).
BLOCKED_SYMBOLS = re.compile(
    r"^_?(system|popen|exec\w*|fork\w*|vfork|clone\d?|posix_spawn\w*|spawn\w*|"
    r"CreateProcess\w*|WinExec|ShellExecute\w*)$"
)

def guard(event, args):
    if event in BLOCKED_EVENTS:
        raise PermissionError(f"Blocked by the coding agent: {event} (starting processes is not allowed)")
    if event == "ctypes.dlsym" and len(args) > 1 and isinstance(args[1], str) and BLOCKED_SYMBOLS.match(args[1]):
        raise PermissionError(f"Blocked by the coding agent: ctypes access to {args[1]!r}")

sys.addaudithook(guard)

try:  # the low-level helper behind subprocess is not audited itself -- disable it
    import _posixsubprocess, subprocess
    def _blocked(*a, **k):
        raise PermissionError("Blocked by the coding agent: _posixsubprocess.fork_exec")
    _posixsubprocess.fork_exec = _blocked
    subprocess._fork_exec = _blocked
except ImportError:
    pass

mode, target, *rest = sys.argv[1:]
if mode == "code":
    sys.argv = ["-c", *rest]
    sys.path[0] = ""
    exec(compile(target, "<string>", "exec"), {"__name__": "__main__", "__builtins__": __builtins__})
elif mode == "module":
    sys.argv = [target, *rest]
    sys.path[0] = ""
    runpy.run_module(target, run_name="__main__", alter_sys=True)
else:
    sys.argv = [target, *rest]
    sys.path[0] = __import__("os").path.dirname(__import__("os").path.abspath(target))
    runpy.run_path(target, run_name="__main__")
"""


class ToolError(Exception):
    """Raised by a tool to return an is_error tool_result to the model."""


# ---------------------------------------------------------------- helpers

def resolve(path: str) -> Path:
    """Resolve a path against the current directory and refuse anything outside the workspace."""
    p = (CWD / path).resolve()
    if p != WORKSPACE and WORKSPACE not in p.parents:
        raise ToolError(f"Path '{path}' is outside the workspace.")
    return p


def display(p: Path) -> str:
    """How a path is shown to the model: relative to the current directory."""
    return Path(os.path.relpath(p, CWD)).as_posix()


def is_protected(p: Path) -> bool:
    """True if p is the agent's own source code."""
    return any(p == prot or prot in p.parents for prot in PROTECTED_PATHS)


def truncate(text: str) -> str:
    if len(text) <= MAX_TOOL_OUTPUT_CHARS:
        return text
    return text[:MAX_TOOL_OUTPUT_CHARS] + f"\n... [truncated, {len(text) - MAX_TOOL_OUTPUT_CHARS} more chars]"


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
        for base in (WORKSPACE, CWD):  # references are usually root-relative, but accept either
            try:
                p = (base / m.group(1)).resolve()
            except (OSError, ValueError):
                continue
            if (p == WORKSPACE or WORKSPACE in p.parents) and p.is_file():
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


# ---------------------------------------------------------------- tools

def tool_list_directory(path: str = ".") -> str:
    root = resolve(path)
    if not root.is_dir():
        raise ToolError(f"Not a directory: {path}")
    try:
        entries = sorted(root.iterdir(), key=lambda e: (not e.is_dir(), e.name.lower()))
    except OSError as e:
        raise ToolError(f"Cannot list {path}: {e}")

    lines = [f"{display(root)}/"]
    for e in entries:
        if e.name in SKIP_DIRS:
            continue
        if len(lines) > MAX_LISTING_ENTRIES:
            lines.append(f"... [stopped after {MAX_LISTING_ENTRIES} entries]")
            break
        if e.is_dir():
            lines.append(f"  {e.name}/")
        else:
            try:
                lines.append(f"  {e.name}  ({e.stat().st_size:,} bytes)")
            except OSError:
                lines.append(f"  {e.name}")
    return "\n".join(lines) if len(lines) > 1 else f"{lines[0]}\n  (empty)"


def tool_change_directory(path: str) -> str:
    global CWD
    target = WORKSPACE if path.strip() in ("/", "\\") else resolve(path)
    if not target.is_dir():
        raise ToolError(f"Not a directory: {path}")
    CWD = target
    rel = CWD.relative_to(WORKSPACE).as_posix()
    print(f"\033[2m[cwd] {'(repository root)' if rel == '.' else rel}\033[0m")
    return f"Current directory is now: {'.' if rel == '.' else rel} (relative to the repository root)"


def tool_grep(
    pattern: str, path: str = ".", glob: str | None = None, ignore_case: bool = False, include_ignored: bool = False
) -> str:
    try:
        regex = re.compile(pattern, re.IGNORECASE if ignore_case else 0)
    except re.error as e:
        raise ToolError(f"Invalid regex: {e}")

    root = resolve(path)
    if not root.exists():
        raise ToolError(f"Path not found: {path}")

    skip = {".git"} if include_ignored else SKIP_DIRS
    files = [root] if root.is_file() else (
        Path(dirpath) / name
        for dirpath, dirnames, filenames in os.walk(root)
        if not dirnames.__setitem__(slice(None), [d for d in dirnames if d not in skip])
        for name in filenames
    )

    matches = []
    for f in files:
        if glob and not f.match(glob):
            continue
        try:
            with open(f, encoding="utf-8") as fh:
                for lineno, line in enumerate(fh, 1):
                    if regex.search(line):
                        matches.append(f"{display(f)}:{lineno}: {line.rstrip()}")
                        if len(matches) >= 500:
                            matches.append("... [stopped after 500 matches; narrow the search]")
                            return "\n".join(matches)
        except (UnicodeDecodeError, OSError):
            continue  # binary or unreadable file
    return "\n".join(matches) if matches else "No matches."


def tool_read_file(path: str, start_line: int | None = None, end_line: int | None = None) -> str:
    p = resolve(path)
    if not p.is_file():
        raise ToolError(f"File not found: {path}")
    try:
        lines = p.read_text(encoding="utf-8").splitlines()
    except UnicodeDecodeError:
        raise ToolError(f"{path} is not a UTF-8 text file.")
    start = start_line or 1
    end = min(end_line or len(lines), len(lines))
    body = "\n".join(f"{i:>5}\t{lines[i - 1]}" for i in range(start, end + 1))
    return truncate(body or "(empty file)")


def writable_path(path: str) -> Path:
    """Resolve a path the agent may write to, refusing its own source code."""
    p = resolve(path)
    if is_protected(p):
        raise ToolError(f"{path} is part of the coding agent's own source code and cannot be modified.")
    if p.is_dir():
        raise ToolError(f"{path} is a directory.")
    return p


def confirm_and_write(path: str, p: Path, old: str, new: str) -> str:
    """Show a diff of old -> new, ask the user, and write the file if approved."""
    existed = p.exists()
    diff = list(difflib.unified_diff(
        old.splitlines(),
        new.splitlines(),
        fromfile=f"a/{path}" if existed else "/dev/null",
        tofile=f"b/{path}",
        lineterm="",
    ))
    # Link the header to the first changed line: start at the first hunk's "+c" line number
    # ("@@ -a,b +c,d @@") and skip its unchanged context lines.
    first_line = 1
    for i, d in enumerate(diff):
        if d.startswith("@@"):
            first_line = max(int(re.match(r"@@ -\d+(?:,\d+)? \+(\d+)", d).group(1)), 1)
            for context in diff[i + 1:]:
                if not context.startswith(" "):
                    break
                first_line += 1
            break
    target = file_link(p, first_line, f"{path}:{first_line}") if existed else path
    print(f"\n\033[1;33m=== {'Modify' if existed else 'Create'} \033[0m{target}\033[1;33m ===\033[0m")
    print(colorize_diff(diff))

    answer = input("\nApply this change? [y]es / [n]o: ").strip().lower()
    if answer not in ("y", "yes"):
        feedback = input("Why not / what should change? (optional): ").strip()
        raise ToolError(
            "The user rejected this change; the file was NOT modified."
            + (f" User feedback: {feedback}" if feedback else "")
        )

    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(new, encoding="utf-8")
    return f"{'Modified' if existed else 'Created'} {path} ({len(new.splitlines())} lines)."


def tool_edit_file(path: str, old_string: str, new_string: str, replace_all: bool = False) -> str:
    p = writable_path(path)
    if not p.is_file():
        raise ToolError(f"File not found: {path}. Use write_file to create a new file.")
    try:
        old = p.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        raise ToolError(f"{path} is not a UTF-8 text file.")

    if not old_string:
        raise ToolError("old_string is empty. Use write_file to create or rewrite a whole file.")
    if old_string == new_string:
        raise ToolError("old_string and new_string are identical; nothing to change.")
    count = old.count(old_string)
    if count == 0:
        raise ToolError(
            f"old_string was not found in {path}. Re-read the file with read_file and copy the "
            "text exactly, including whitespace, without the line-number prefix."
        )
    if count > 1 and not replace_all:
        raise ToolError(
            f"old_string occurs {count} times in {path}. Add surrounding lines to make it unique, "
            "or set replace_all to true."
        )

    new = old.replace(old_string, new_string) if replace_all else old.replace(old_string, new_string, 1)
    result = confirm_and_write(path, p, old, new)
    return result + (f" Replaced {count} occurrences." if replace_all and count > 1 else "")


def tool_write_file(path: str, content: str) -> str:
    p = writable_path(path)
    old = p.read_text(encoding="utf-8") if p.exists() else ""
    if old == content:
        return "No changes: file already has this content."
    return confirm_and_write(path, p, old, content)


def tool_ask_human(question: str) -> str:
    print(f"\n\033[1;35m[agent asks]\033[0m {linkify(question)}")
    answer = input("Your answer: ").strip()
    return answer or "(the user gave no answer)"


_always_allow_python = False


def tool_run_python(code: str | None = None, args: list[str] | None = None, timeout: int = RUN_TIMEOUT_SECONDS) -> str:
    global _always_allow_python
    if bool(code) == bool(args):
        raise ToolError("Pass exactly one of `code` or `args`.")
    if code:
        guarded = ["code", code]
    elif args[0] == "-m" and len(args) > 1:
        guarded = ["module", *args[1:]]
    elif not args[0].startswith("-"):
        guarded = ["script", *args]
    else:
        raise ToolError("args must be a script path or -m <module>, optionally followed by arguments.")
    # With uv, `uv run` picks up the project's pyproject.toml / .venv and syncs its dependencies.
    python = [UV, "run", "--quiet", "python"] if UV else [sys.executable]
    cmd = [*python, "-c", GUARD_SOURCE, *guarded]

    print("\n\033[1;33m=== Run Python ===\033[0m")
    print(f"({'uv run python' if UV else sys.executable}, subprocesses blocked)")
    print(code if code else "python " + " ".join(args))
    if not _always_allow_python:
        answer = input("\nRun this? [y]es / [n]o / [a]lways for this session: ").strip().lower()
        if answer in ("a", "always"):
            _always_allow_python = True
        elif answer not in ("y", "yes"):
            feedback = input("Why not / what should change? (optional): ").strip()
            raise ToolError(
                "The user declined to run this."
                + (f" User feedback: {feedback}" if feedback else "")
            )

    try:
        proc = subprocess.run(cmd, cwd=CWD, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise ToolError(f"Timed out after {timeout}s.")

    print(f"\033[2m[exit code {proc.returncode}]\033[0m")
    return truncate(
        f"exit code: {proc.returncode}\n"
        f"--- stdout ---\n{proc.stdout or '(empty)'}\n"
        f"--- stderr ---\n{proc.stderr or '(empty)'}"
    )


def memory_snapshot() -> str:
    """All memory files, read locally, to hand Claude at the start of a session (no tool calls)."""
    files = sorted(p for p in memory_tool.memory_root.rglob("*") if p.is_file() and not p.name.startswith("."))
    if not files:
        return "<memory>\n(empty -- nothing saved for this project yet)\n</memory>"
    parts = []
    for p in files:
        try:
            text = p.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        parts.append(f'<file path="/memories/{p.relative_to(memory_tool.memory_root).as_posix()}">\n{text}\n</file>')
    return "<memory>\n" + truncate("\n".join(parts)) + "\n</memory>"


def read_skill_header(skill_md: Path) -> dict[str, str]:
    """Parse the `key: value` lines between the leading `---` markers of a SKILL.md."""
    lines = skill_md.read_text(encoding="utf-8").splitlines()
    header: dict[str, str] = {}
    if lines and lines[0].strip() == "---":
        for line in lines[1:]:
            if line.strip() == "---":
                break
            key, sep, value = line.partition(":")
            if sep:
                header[key.strip()] = value.strip()
    return header


def discover_skills() -> dict[str, Path]:
    """Find every SKILL.md; later locations override earlier ones with the same name."""
    found: dict[str, Path] = {}
    for root in (BUNDLED_SKILLS, PERSONAL_SKILLS, WORKSPACE / ".agent" / "skills"):
        for skill_md in sorted(root.glob("*/SKILL.md")) if root.is_dir() else []:
            try:
                name = read_skill_header(skill_md).get("name") or skill_md.parent.name
            except (OSError, UnicodeDecodeError):
                continue
            found[name] = skill_md
    return found


def skills_catalog() -> str:
    """Names and descriptions only -- the full instructions are loaded on demand."""
    if not skills:
        return "<skills>\n(none installed)\n</skills>"
    lines = [f"- {name}: {read_skill_header(p).get('description', '')}" for name, p in sorted(skills.items())]
    return "<skills>\n" + "\n".join(lines) + "\n</skills>"


def tool_load_skill(name: str) -> str:
    skill_md = skills.get(name)
    if skill_md is None:
        raise ToolError(f"Unknown skill '{name}'. Available: {', '.join(sorted(skills)) or 'none'}.")
    print(f"\033[2m[skill] {name}\033[0m")
    return skill_md.read_text(encoding="utf-8")


def tool_memory(**command) -> str:
    result = memory_tool.call(command)
    if command.get("command") != "view":
        target = command.get("path") or command.get("new_path", "")
        print(f"\033[2m[memory] {command.get('command')} {target}\033[0m")
    return result


TOOL_HANDLERS = {
    "list_directory": tool_list_directory,
    "change_directory": tool_change_directory,
    "grep": tool_grep,
    "read_file": tool_read_file,
    "edit_file": tool_edit_file,
    "write_file": tool_write_file,
    "ask_human": tool_ask_human,
    "run_python": tool_run_python,
    "memory": tool_memory,
    "load_skill": tool_load_skill,
}


def run_tool(block) -> dict:
    """Execute one tool_use block and build its tool_result."""
    handler = TOOL_HANDLERS.get(block.name)
    try:
        if handler is None:
            raise ToolError(f"Unknown tool: {block.name}")
        output = handler(**block.input)
        return {"type": "tool_result", "tool_use_id": block.id, "content": output}
    except ToolError as e:
        return {"type": "tool_result", "tool_use_id": block.id, "content": str(e), "is_error": True}
    except TypeError as e:  # bad/missing arguments from the model
        return {"type": "tool_result", "tool_use_id": block.id, "content": f"Invalid arguments: {e}", "is_error": True}
    except Exception as e:
        return {"type": "tool_result", "tool_use_id": block.id, "content": f"{type(e).__name__}: {e}", "is_error": True}


# ---------------------------------------------------------------- agent loop

def stream_response(client: anthropic.Anthropic, messages: list):
    """Stream one model response to the terminal and return the final message."""
    with client.messages.stream(
        cache_control={"type": "ephemeral"},  # cache the growing prefix: each loop step re-reads it cheaply
        model=MODEL,
        max_tokens=MAX_TOKENS,
        system=SYSTEM_PROMPT.format(workspace=WORKSPACE),
        tools=TOOLS,
        thinking={"type": "adaptive"},
        messages=messages,
    ) as stream:
        out = LinkedPrinter()
        for event in stream:
            if event.type in ("content_block_start", "content_block_stop"):
                out.flush()
            if event.type == "content_block_start":
                block = event.content_block
                if block.type == "text":
                    print("\n\033[1;34mClaude:\033[0m ", end="", flush=True)
                elif block.type == "thinking":
                    print("\n\033[2m(thinking...)\033[0m", end="", flush=True)
                elif block.type in ("tool_use", "server_tool_use"):
                    print(f"\n\033[2m-> {block.name}\033[0m", end="", flush=True)
                elif block.type == "web_search_tool_result":
                    results = block.content
                    if isinstance(results, list):
                        print(f"\n\033[2m   {len(results)} result(s)\033[0m", end="", flush=True)
                    else:  # an error object, e.g. max_uses_exceeded or unavailable
                        print(f"\n\033[2m   web search error: {getattr(results, 'error_code', results)}\033[0m", end="", flush=True)
            elif event.type == "text":
                out.write(event.text)
            elif event.type == "content_block_stop" and event.content_block.type in ("tool_use", "server_tool_use"):
                # The full input is only known once the block ends -- show a short summary.
                args = ", ".join(
                    f"{k}={v!r}"[:80] for k, v in event.content_block.input.items()
                    if k not in ("content", "old_string", "new_string", "file_text", "old_str", "new_str", "insert_text")
                )
                print(f"\033[2m({args})\033[0m", end="", flush=True)
        print()
        return stream.get_final_message()


def run_turn(client: anthropic.Anthropic, messages: list) -> None:
    """Call the model repeatedly until it stops asking for tools."""
    while True:
        response = stream_response(client, messages)
        tool_uses = [b for b in response.content if b.type == "tool_use"]

        if response.stop_reason == "max_tokens" and tool_uses:
            # A tool call cut off mid-input must not run; drop the turn to keep history valid.
            print("\n[stopped: hit max_tokens in the middle of a tool call]")
            return

        # Append the full content (text, thinking, tool_use) -- not just the text.
        # Stored as plain dicts so the history can be saved to JSON and resumed later.
        messages.append({"role": "assistant", "content": [b.to_dict() for b in response.content]})

        if response.stop_reason == "tool_use":
            # Run every requested tool and return ALL results in one user message.
            results = [run_tool(b) for b in tool_uses]
            messages.append({"role": "user", "content": results})
            continue

        if response.stop_reason == "pause_turn":
            continue  # a long server-side web search paused; re-sending the history resumes it
        if response.stop_reason == "max_tokens":
            print("\n[stopped: hit max_tokens]")
        elif response.stop_reason == "refusal":
            print("\n[the model declined this request]")
        return


def save_conversation(messages: list) -> None:
    """Write the history to disk (atomically) so --resume can pick it up."""
    tmp = conversation_file.with_suffix(".tmp")
    tmp.write_text(json.dumps(messages, ensure_ascii=False), encoding="utf-8")
    tmp.replace(conversation_file)


def load_conversation() -> list:
    """Load the saved history, or return [] if there is none."""
    if not conversation_file.is_file():
        print("No saved conversation for this project; starting a new one.")
        return []
    try:
        messages = json.loads(conversation_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        print(f"Could not read {conversation_file} ({e}); starting a new conversation.")
        return []

    def instruction(m: dict) -> str | None:
        """The user's typed text, or None for tool-result messages."""
        if isinstance(m["content"], str):
            return m["content"]
        texts = [b["text"] for b in m["content"] if b.get("type") == "text"]
        return texts[-1] if texts else None  # last text block; earlier ones may be memory and skills

    user_turns = [t for m in messages if m["role"] == "user" and (t := instruction(m)) is not None]
    print(f"Resumed conversation: {len(user_turns)} earlier instruction(s).")
    if user_turns:
        print(f"\033[2mLast instruction: {user_turns[-1][:200]}\033[0m")
    last_text = next(
        (b["text"] for m in reversed(messages) if m["role"] == "assistant"
         for b in reversed(m["content"]) if b.get("type") == "text"),
        None,
    )
    if last_text:
        print(f"\033[2mLast reply: {linkify(last_text[:300])}\033[0m")
    return messages


_memory_sent = False  # memory and the skill list go with the first instruction of each session


def send(client: anthropic.Anthropic, messages: list, text: str) -> bool:
    """Run one user instruction through the agent loop. Returns False if it failed."""
    global _memory_sent
    checkpoint = len(messages)
    if _memory_sent:
        messages.append({"role": "user", "content": text})
    else:
        messages.append({"role": "user", "content": [
            {"type": "text", "text": memory_snapshot()},
            {"type": "text", "text": skills_catalog()},
            {"type": "text", "text": text},
        ]})
    try:
        run_turn(client, messages)
        save_conversation(messages)
        _memory_sent = True
        return True
    except KeyboardInterrupt:
        print("\n[interrupted]")
    except anthropic.APIStatusError as e:
        print(f"\n[API error {e.status_code}] {e.message}")
    except anthropic.APIConnectionError:
        print("\n[network error -- check your Foundry endpoint]")
    # Drop the unfinished turn so the history stays valid for the next request.
    del messages[checkpoint:]
    save_conversation(messages)
    return False


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Personal coding agent (Claude on Azure).")
    parser.add_argument("instruction", nargs="?", help="Task for the agent. Omit to start in interactive mode.")
    parser.add_argument("-d", "--dir", default=".", help="Project directory the agent works in (default: current directory).")
    parser.add_argument("-i", "--interactive", action="store_true", help="Keep chatting after the instruction finishes.")
    parser.add_argument("-r", "--resume", action="store_true", help="Continue the last conversation in this project.")
    return parser.parse_args()


def main() -> None:
    global WORKSPACE, CWD, memory_tool, conversation_file, skills
    args = parse_args()
    WORKSPACE = CWD = Path(args.dir).expanduser().resolve()
    if not WORKSPACE.is_dir():
        raise SystemExit(f"Not a directory: {WORKSPACE}")

    # One memory folder per project, e.g. ~/.coding_agent/memory/myapp-1a2b3c4d/memories/
    project_id = f"{WORKSPACE.name}-{hashlib.sha256(str(WORKSPACE).encode()).hexdigest()[:8]}"
    memory_tool = BetaLocalFilesystemMemoryTool(base_path=str(MEMORY_HOME / project_id))
    conversation_file = MEMORY_HOME / project_id / "conversation.json"
    skills = discover_skills()
    PROTECTED_PATHS.append(WORKSPACE / ".agent" / "skills")

    client = _get_client()
    print(f"Workspace: {WORKSPACE}")
    print(f"Python runner: {'uv run (' + UV + ')' if UV else sys.executable + ' (uv not found)'}")
    print(f"Memory: {memory_tool.memory_root}")
    print(f"Skills: {', '.join(sorted(skills)) or '(none)'}")
    print(f"Web search: {'off' if WEB_SEARCH == 'off' else 'web_search_' + WEB_SEARCH}")
    messages = load_conversation() if args.resume else []

    if args.instruction:
        ok = send(client, messages, args.instruction)
        if not args.interactive:
            raise SystemExit(0 if ok else 1)

    print("Interactive mode. Type 'exit' to quit.")
    while True:
        try:
            user_input = input("\n\033[1mYou:\033[0m ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if user_input.lower() in ("exit", "quit"):
            break
        if user_input:
            send(client, messages, user_input)


if __name__ == "__main__":
    main()
