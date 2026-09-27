"""Personal coding agent: Claude on Azure (Microsoft Foundry) with a manual tool-use loop.

Tools:
  - grep        : regex search across files in the workspace
  - read_file   : read a file (optionally a line range)
  - edit_file   : replace an exact snippet in a file -- shows a diff and asks permission first
  - write_file  : create/overwrite a file -- shows a diff and asks permission first
                  (edit_file and write_file never touch the agent's own source: this file and the auth package)
  - ask_human   : lets the model ask you a question mid-task
  - run_python  : run a Python snippet, script or module (e.g. pytest) -- asks permission first
                  (uses `uv run` when uv is installed, so the project's own environment is used)
  - memory      : Anthropic's memory tool -- notes the agent keeps about each project across runs,
                  stored in ~/.coding_agent/memory/<project>/ (override with AGENT_MEMORY_DIR)

Usage:
    python agent.py -d path/to/project "Add input validation to the CLI"
    python agent.py -d path/to/project "..." -i   # keep chatting after the task
    python agent.py -d path/to/project            # interactive mode only
    python agent.py -d path/to/project -r         # resume the last conversation in this project
"""

import argparse
import difflib
import hashlib
import inspect
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import anthropic
from anthropic.tools.memory import BetaLocalFilesystemMemoryTool

# Returns an AnthropicFoundry client (API key or Azure AD auth).
from auth.anthropic import _get_client

# On Foundry this is your *deployment name*; change it if yours differs.
MODEL = os.environ.get("ANTHROPIC_FOUNDRY_DEPLOYMENT", "claude-opus-5")
MAX_TOKENS = 64000  # safe with streaming (no HTTP timeout risk)
MAX_TOOL_OUTPUT_CHARS = 50_000
RUN_TIMEOUT_SECONDS = 120
UV = shutil.which("uv")  # None when uv is not installed
SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", ".mypy_cache", ".pytest_cache"}

WORKSPACE = Path(".").resolve()  # set from --dir in main()

MEMORY_HOME = Path(os.environ.get("AGENT_MEMORY_DIR", "~/.coding_agent/memory")).expanduser()
memory_tool: BetaLocalFilesystemMemoryTool | None = None  # set per project in main()
conversation_file: Path | None = None  # set per project in main(); used by --resume

# The agent must never modify its own source code.
PROTECTED_PATHS = [Path(__file__).resolve(), Path(inspect.getfile(_get_client)).resolve().parent]

SYSTEM_PROMPT = """You are a coding agent working in the repository at {workspace}.
All file paths are relative to that directory.

Use grep and read_file to understand the code before changing it. Read a file before
you change it. To change an existing file, use edit_file with an old_string copied exactly
from the file (without the line-number prefix) and enough surrounding lines to be unique.
Use write_file only to create a new file or to rewrite most of a file. The user sees a
diff and must approve every change. If the user rejects a change, read their feedback
and adjust rather than retrying the same edit. When a requirement is ambiguous or a
decision is genuinely the user's to make, use ask_human instead of guessing.
Never modify your own source code (the coding agent's files); those writes are refused.

You have a persistent memory directory, /memories, private to this project and kept between
sessions. At the start of every task, view /memories and read what is relevant before doing
anything else. As you work, record what would help a future session: build/test commands,
project conventions and structure, the user's preferences and corrections, and decisions
with their reasons. Keep it short and organized (update or delete stale notes rather than
appending duplicates). Never store secrets such as API keys, passwords or tokens.

After changing code, verify it with run_python: run the tests (e.g. args ["-m", "pytest", "-q"]),
the script you changed, or a small snippet that exercises it. If it fails, read the error,
fix the code, and run it again."""

TOOLS = [
    {"type": "memory_20250818", "name": "memory"},  # Anthropic-defined: no input_schema
    {
        "name": "grep",
        "description": (
            "Search file contents in the workspace with a Python regular expression. "
            "Returns matching lines as 'path:line_number: text'. Use this to locate "
            "definitions, usages, or strings before reading files."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Python regex to search for."},
                "path": {
                    "type": "string",
                    "description": "File or directory to search, relative to the workspace. Defaults to '.'.",
                },
                "glob": {
                    "type": "string",
                    "description": "Only search files whose name matches this glob, e.g. '*.py'.",
                },
                "ignore_case": {"type": "boolean", "description": "Case-insensitive match."},
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
                "path": {"type": "string", "description": "File path relative to the workspace."},
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
                "path": {"type": "string", "description": "File path relative to the workspace."},
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
                "path": {"type": "string", "description": "File path relative to the workspace."},
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
            "Run Python in the workspace directory to check code for bugs, and return the exit code, "
            "stdout and stderr. Pass either `code` (a snippet, run like `python -c`) or `args` "
            "(arguments after `python`, e.g. [\"script.py\"], [\"-m\", \"pytest\", \"-q\"], "
            "[\"-m\", \"py_compile\", \"app.py\"]). The user must approve every run."
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
]


class ToolError(Exception):
    """Raised by a tool to return an is_error tool_result to the model."""


# ---------------------------------------------------------------- helpers

def resolve(path: str) -> Path:
    """Resolve a workspace-relative path and refuse anything outside the workspace."""
    p = (WORKSPACE / path).resolve()
    if p != WORKSPACE and WORKSPACE not in p.parents:
        raise ToolError(f"Path '{path}' is outside the workspace.")
    return p


def is_protected(p: Path) -> bool:
    """True if p is the agent's own source (this file or anything in the auth package)."""
    return any(p == prot or prot in p.parents for prot in PROTECTED_PATHS)


def truncate(text: str) -> str:
    if len(text) <= MAX_TOOL_OUTPUT_CHARS:
        return text
    return text[:MAX_TOOL_OUTPUT_CHARS] + f"\n... [truncated, {len(text) - MAX_TOOL_OUTPUT_CHARS} more chars]"


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

def tool_grep(pattern: str, path: str = ".", glob: str | None = None, ignore_case: bool = False) -> str:
    try:
        regex = re.compile(pattern, re.IGNORECASE if ignore_case else 0)
    except re.error as e:
        raise ToolError(f"Invalid regex: {e}")

    root = resolve(path)
    if not root.exists():
        raise ToolError(f"Path not found: {path}")

    files = [root] if root.is_file() else (
        Path(dirpath) / name
        for dirpath, dirnames, filenames in os.walk(root)
        if not dirnames.__setitem__(slice(None), [d for d in dirnames if d not in SKIP_DIRS])
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
                        matches.append(f"{f.relative_to(WORKSPACE)}:{lineno}: {line.rstrip()}")
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
    print(f"\n\033[1;33m=== {'Modify' if existed else 'Create'} {path} ===\033[0m")
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
    print(f"\n\033[1;35m[agent asks]\033[0m {question}")
    answer = input("Your answer: ").strip()
    return answer or "(the user gave no answer)"


_always_allow_python = False


def tool_run_python(code: str | None = None, args: list[str] | None = None, timeout: int = RUN_TIMEOUT_SECONDS) -> str:
    global _always_allow_python
    if bool(code) == bool(args):
        raise ToolError("Pass exactly one of `code` or `args`.")
    # With uv, `uv run` picks up the project's pyproject.toml / .venv and syncs its dependencies.
    python = [UV, "run", "--quiet", "python"] if UV else [sys.executable]
    cmd = [*python, "-c", code] if code else [*python, *args]

    print("\n\033[1;33m=== Run Python ===\033[0m")
    print(f"({'uv run python' if UV else sys.executable})")
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
        proc = subprocess.run(cmd, cwd=WORKSPACE, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise ToolError(f"Timed out after {timeout}s.")

    print(f"\033[2m[exit code {proc.returncode}]\033[0m")
    return truncate(
        f"exit code: {proc.returncode}\n"
        f"--- stdout ---\n{proc.stdout or '(empty)'}\n"
        f"--- stderr ---\n{proc.stderr or '(empty)'}"
    )


def tool_memory(**command) -> str:
    result = memory_tool.call(command)
    if command.get("command") != "view":
        target = command.get("path") or command.get("new_path", "")
        print(f"\033[2m[memory] {command.get('command')} {target}\033[0m")
    return result


TOOL_HANDLERS = {
    "grep": tool_grep,
    "read_file": tool_read_file,
    "edit_file": tool_edit_file,
    "write_file": tool_write_file,
    "ask_human": tool_ask_human,
    "run_python": tool_run_python,
    "memory": tool_memory,
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
        model=MODEL,
        max_tokens=MAX_TOKENS,
        system=SYSTEM_PROMPT.format(workspace=WORKSPACE),
        tools=TOOLS,
        thinking={"type": "adaptive"},
        messages=messages,
    ) as stream:
        for event in stream:
            if event.type == "content_block_start":
                block = event.content_block
                if block.type == "text":
                    print("\n\033[1;34mClaude:\033[0m ", end="", flush=True)
                elif block.type == "thinking":
                    print("\n\033[2m(thinking...)\033[0m", end="", flush=True)
                elif block.type == "tool_use":
                    print(f"\n\033[2m-> {block.name}\033[0m", end="", flush=True)
            elif event.type == "text":
                print(event.text, end="", flush=True)
            elif event.type == "content_block_stop" and event.content_block.type == "tool_use":
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
            continue  # resume the paused turn
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

    user_turns = [m for m in messages if m["role"] == "user" and isinstance(m["content"], str)]
    print(f"Resumed conversation: {len(user_turns)} earlier instruction(s).")
    if user_turns:
        print(f"\033[2mLast instruction: {user_turns[-1]['content'][:200]}\033[0m")
    last_text = next(
        (b["text"] for m in reversed(messages) if m["role"] == "assistant"
         for b in reversed(m["content"]) if b.get("type") == "text"),
        None,
    )
    if last_text:
        print(f"\033[2mLast reply: {last_text[:300]}\033[0m")
    return messages


def send(client: anthropic.Anthropic, messages: list, text: str) -> bool:
    """Run one user instruction through the agent loop. Returns False if it failed."""
    checkpoint = len(messages)
    messages.append({"role": "user", "content": text})
    try:
        run_turn(client, messages)
        save_conversation(messages)
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
    global WORKSPACE, memory_tool, conversation_file
    args = parse_args()
    WORKSPACE = Path(args.dir).expanduser().resolve()
    if not WORKSPACE.is_dir():
        raise SystemExit(f"Not a directory: {WORKSPACE}")

    # One memory folder per project, e.g. ~/.coding_agent/memory/myapp-1a2b3c4d/memories/
    project_id = f"{WORKSPACE.name}-{hashlib.sha256(str(WORKSPACE).encode()).hexdigest()[:8]}"
    memory_tool = BetaLocalFilesystemMemoryTool(base_path=str(MEMORY_HOME / project_id))
    conversation_file = MEMORY_HOME / project_id / "conversation.json"

    client = _get_client()
    print(f"Workspace: {WORKSPACE}")
    print(f"Python runner: {'uv run (' + UV + ')' if UV else sys.executable + ' (uv not found)'}")
    print(f"Memory: {memory_tool.memory_root}")
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
