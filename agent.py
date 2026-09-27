"""Personal coding agent: Claude on Azure (Microsoft Foundry) with a manual tool-use loop.

Tools:
  - grep        : regex search across files in the workspace
  - read_file   : read a file (optionally a line range)
  - write_file  : create/overwrite a file -- shows a diff and asks permission first
  - ask_human   : lets the model ask you a question mid-task

Usage:
    python agent.py -d path/to/project "Add input validation to the CLI"
    python agent.py -d path/to/project "..." -i   # keep chatting after the task
    python agent.py -d path/to/project            # interactive mode only
"""

import argparse
import difflib
import os
import re
from pathlib import Path

import anthropic

# Returns an AnthropicFoundry client (API key or Azure AD auth).
from client_factory import _get_client

# On Foundry this is your *deployment name*; change it if yours differs.
MODEL = os.environ.get("AGENT_MODEL", "claude-opus-5")
MAX_TOKENS = 16000
MAX_TOOL_OUTPUT_CHARS = 50_000
SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", ".mypy_cache", ".pytest_cache"}

WORKSPACE = Path(".").resolve()  # set from --dir in main()

SYSTEM_PROMPT = """You are a coding agent working in the repository at {workspace}.
All file paths are relative to that directory.

Use grep and read_file to understand the code before changing it. Read a file before
you overwrite it, and write the complete new content with write_file -- the user sees
a diff and must approve every write. If the user rejects a change, read their feedback
and adjust rather than retrying the same edit. When a requirement is ambiguous or a
decision is genuinely the user's to make, use ask_human instead of guessing."""

TOOLS = [
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
        "name": "write_file",
        "description": (
            "Create a file or overwrite it with the given full content. The user is shown a "
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


def tool_write_file(path: str, content: str) -> str:
    p = resolve(path)
    if p.is_dir():
        raise ToolError(f"{path} is a directory.")
    old = p.read_text(encoding="utf-8") if p.exists() else ""

    if old == content:
        return "No changes: file already has this content."

    diff = list(difflib.unified_diff(
        old.splitlines(),
        content.splitlines(),
        fromfile=f"a/{path}" if p.exists() else "/dev/null",
        tofile=f"b/{path}",
        lineterm="",
    ))
    action = "Modify" if p.exists() else "Create"
    print(f"\n\033[1;33m=== {action} {path} ===\033[0m")
    print(colorize_diff(diff))

    answer = input("\nApply this change? [y]es / [n]o: ").strip().lower()
    if answer not in ("y", "yes"):
        feedback = input("Why not / what should change? (optional): ").strip()
        raise ToolError(
            "The user rejected this write; the file was NOT modified."
            + (f" User feedback: {feedback}" if feedback else "")
        )

    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return f"{'Modified' if old else 'Created'} {path} ({len(content.splitlines())} lines)."


def tool_ask_human(question: str) -> str:
    print(f"\n\033[1;35m[agent asks]\033[0m {question}")
    answer = input("Your answer: ").strip()
    return answer or "(the user gave no answer)"


TOOL_HANDLERS = {
    "grep": tool_grep,
    "read_file": tool_read_file,
    "write_file": tool_write_file,
    "ask_human": tool_ask_human,
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

def run_turn(client: anthropic.Anthropic, messages: list) -> None:
    """Call the model repeatedly until it stops asking for tools."""
    while True:
        response = client.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            system=SYSTEM_PROMPT.format(workspace=WORKSPACE),
            tools=TOOLS,
            thinking={"type": "adaptive"},
            messages=messages,
        )
        # Append the full content (text, thinking, tool_use) -- not just the text.
        messages.append({"role": "assistant", "content": response.content})

        for block in response.content:
            if block.type == "text" and block.text.strip():
                print(f"\n\033[1;34mClaude:\033[0m {block.text}")
            elif block.type == "tool_use":
                args = ", ".join(f"{k}={v!r}"[:80] for k, v in block.input.items() if k != "content")
                print(f"\033[2m-> {block.name}({args})\033[0m")

        if response.stop_reason == "tool_use":
            # Run every requested tool and return ALL results in one user message.
            results = [run_tool(b) for b in response.content if b.type == "tool_use"]
            messages.append({"role": "user", "content": results})
            continue

        if response.stop_reason == "pause_turn":
            continue  # resume the paused turn
        if response.stop_reason == "max_tokens":
            print("\n[stopped: hit max_tokens]")
        elif response.stop_reason == "refusal":
            print("\n[the model declined this request]")
        return


def send(client: anthropic.Anthropic, messages: list, text: str) -> bool:
    """Run one user instruction through the agent loop. Returns False if it failed."""
    checkpoint = len(messages)
    messages.append({"role": "user", "content": text})
    try:
        run_turn(client, messages)
        return True
    except KeyboardInterrupt:
        print("\n[interrupted]")
    except anthropic.APIStatusError as e:
        print(f"\n[API error {e.status_code}] {e.message}")
    except anthropic.APIConnectionError:
        print("\n[network error -- check your Foundry endpoint]")
    # Drop the unfinished turn so the history stays valid for the next request.
    del messages[checkpoint:]
    return False


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Personal coding agent (Claude on Azure).")
    parser.add_argument("instruction", nargs="?", help="Task for the agent. Omit to start in interactive mode.")
    parser.add_argument("-d", "--dir", default=".", help="Project directory the agent works in (default: current directory).")
    parser.add_argument("-i", "--interactive", action="store_true", help="Keep chatting after the instruction finishes.")
    return parser.parse_args()


def main() -> None:
    global WORKSPACE
    args = parse_args()
    WORKSPACE = Path(args.dir).expanduser().resolve()
    if not WORKSPACE.is_dir():
        raise SystemExit(f"Not a directory: {WORKSPACE}")

    client = _get_client()
    messages: list = []
    print(f"Workspace: {WORKSPACE}")

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
