"""Project memory: notes read at session start, updated in the background after each instruction."""

import difflib
import json
import queue
import re
import threading

import anthropic

from . import state
from .common import truncate
from .config import (
    MEMORY_EXIT_WAIT_SECONDS,
    MEMORY_MAX_CHARS,
    MEMORY_MODEL,
    MEMORY_UPDATES,
)


def memory_snapshot() -> str:
    """All memory files, read locally, to hand Claude at the start of a session (no tool calls)."""
    files = sorted(p for p in state.memory_dir.rglob("*") if p.is_file() and not p.name.startswith(".")) if state.memory_dir.is_dir() else []
    if not files:
        return "<memory>\n(empty -- nothing saved for this project yet)\n</memory>"
    parts = []
    for p in files:
        try:
            text = p.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        parts.append(f'<file path="/memories/{p.relative_to(state.memory_dir).as_posix()}">\n{text}\n</file>')
    return "<memory>\n" + truncate("\n".join(parts)) + "\n</memory>"


# --- Background memory ---------------------------------------------------------------------------
# Claude does not write memory during a task (that used to add slow tool calls at the end of each
# instruction). Instead, after each instruction, a digest of what happened goes to a worker thread
# that asks a separate "curator" call to update notes.md. Updates run one at a time, in order.

MEMORY_CURATOR_PROMPT = f"""You maintain the long-term memory notes of a coding agent for one
software project. You are given the current notes and a digest of the latest session turn
(the user's instruction, the tools the agent used, the user's answers and rejections, and the
agent's final reply).

Keep only durable facts that will help a future session on this project:
- how to build, run, lint and test it (exact commands that worked), and its structure;
- project conventions and the libraries and versions it relies on;
- the user's preferences and corrections (anything they rejected, and why);
- decisions and their reasons, and known pitfalls or open problems;
- anything the user explicitly asked to remember.
Do not keep: one-off task details, progress logs, things obvious from the code, speculation,
or secrets (API keys, passwords, tokens, connection strings) -- remove any you find.

Keep the notes concise Markdown grouped under short headings, merge duplicates, update facts
that changed, and stay under {MEMORY_MAX_CHARS} characters.

If nothing durable was learned, answer exactly NO_CHANGE. Otherwise answer with the complete
updated notes inside <notes>...</notes> and nothing else."""

_memory_queue: "queue.Queue[str | None]" = queue.Queue()
_memory_status: list[str] = []  # messages from the worker, printed before the next prompt
_memory_status_lock = threading.Lock()
_memory_thread: threading.Thread | None = None


def _memory_report(message: str) -> None:
    with _memory_status_lock:
        _memory_status.append(message)


def print_memory_status() -> None:
    """Print what the memory worker reported (called before prompts, never while streaming)."""
    with _memory_status_lock:
        pending, _memory_status[:] = list(_memory_status), []
    for message in pending:
        state.ui.status(f"[memory] {message}")


def turn_digest(new_messages: list, result_chars: int = 0, limit: bool = True) -> str:
    """A compact account of messages: what the memory curator needs, not the file contents.

    With result_chars, every tool result is included (cut to that many characters) -- used to
    summarize the conversation when compacting. limit=False returns it without the size cap.
    """
    lines: list[str] = []
    names: dict[str, str] = {}  # tool_use_id -> tool name
    results: dict[str, str] = {}  # tool_use_id -> the result line, shown under its call

    def short(value, limit: int = 300) -> str:
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        return text if len(text) <= limit else text[:limit] + " ...[cut]"

    for m in new_messages:
        content = m["content"]
        if isinstance(content, str):
            lines.append(f"USER: {content}")
            continue
        for b in content:
            kind = b.get("type")
            if m["role"] == "user" and kind == "text":
                if b["text"].startswith("<compacted_history>"):  # an earlier summary: keep it whole
                    lines.append(b["text"])
                elif not b["text"].startswith(("<memory>", "<skills>")):  # skip what the curator already has
                    lines.append(f"USER: {short(b['text'], 4000)}")
            elif kind == "tool_result":
                name = names.get(b.get("tool_use_id"), "")
                result = b.get("content")
                if isinstance(result, list):  # text and image blocks: keep the text, not the base64
                    result = "\n".join(c.get("text", "") if c.get("type") == "text" else f"[{c.get('type')}]"
                                        for c in result)
                result = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)
                if b.get("is_error"):
                    results[b["tool_use_id"]] = f"  -> FAILED: {short(result, max(600, result_chars))}"
                elif result_chars:
                    results[b["tool_use_id"]] = f"  -> {short(result, result_chars)}"
                elif name == "ask_human":
                    results[b["tool_use_id"]] = f"  -> user answered: {short(result, 1000)}"
                elif name == "run_python":
                    results[b["tool_use_id"]] = f"  -> {short(result, 600)}"
            elif kind == "text":
                lines.append(f"AGENT: {short(b['text'], 3000)}")
            elif kind in ("tool_use", "server_tool_use"):
                names[b["id"]] = b["name"]
                args = {k: v for k, v in b.get("input", {}).items()
                        if k not in ("content", "old_string", "new_string")}
                lines.append(f"AGENT used {b['name']}({short(args)})")
                lines.append(b["id"])  # placeholder, replaced by the result line (if any)
    text = "\n".join(results.get(line, line) for line in lines if line not in names or line in results)
    return truncate(text) if limit else text


def update_memory(client: anthropic.Anthropic, digest: str) -> str:
    """One curator call. Returns a status line; writes notes.md only when something changed."""
    notes_file = state.memory_dir / "notes.md"
    current = notes_file.read_text(encoding="utf-8") if notes_file.is_file() else ""
    response = client.messages.create(
        model=MEMORY_MODEL,
        max_tokens=8000,
        system=MEMORY_CURATOR_PROMPT,
        messages=[{"role": "user", "content":
                   f"<current_notes>\n{current or '(empty)'}\n</current_notes>\n\n"
                   f"<session_turn>\n{digest}\n</session_turn>"}],
    )
    if response.stop_reason not in ("end_turn", "stop_sequence"):
        return f"update skipped (stop reason: {response.stop_reason})"
    text = "".join(b.text for b in response.content if b.type == "text").strip()
    if text == "NO_CHANGE" or not text:
        return ""  # nothing to say
    match = re.search(r"<notes>\s*(.*?)\s*</notes>", text, re.DOTALL)
    if not match:
        return "update skipped (unexpected answer from the memory model)"
    notes = match.group(1).strip() + "\n"
    if notes == current:
        return ""
    state.memory_dir.mkdir(parents=True, exist_ok=True)
    tmp = notes_file.with_suffix(".tmp")
    tmp.write_text(notes, encoding="utf-8")
    tmp.replace(notes_file)  # atomic: a crash never leaves half-written notes
    old_lines, new_lines = current.splitlines(), notes.splitlines()
    diff = list(difflib.unified_diff(old_lines, new_lines, lineterm="", n=0))
    added = sum(1 for d in diff if d.startswith("+") and not d.startswith("+++"))
    removed = sum(1 for d in diff if d.startswith("-") and not d.startswith("---"))
    return f"notes updated (+{added}/-{removed} lines): {notes_file}"


def _memory_worker(client: anthropic.Anthropic) -> None:
    while True:
        digest = _memory_queue.get()
        try:
            if digest is None:
                return
            status = update_memory(client, digest)
            if status:
                _memory_report(status)
        except Exception as e:  # never let a memory problem reach the agent
            _memory_report(f"update failed: {type(e).__name__}: {str(e)[:200]}")
        finally:
            _memory_queue.task_done()


def queue_memory_update(client: anthropic.Anthropic, new_messages: list) -> None:
    """Hand one finished turn to the background worker (started on first use)."""
    global _memory_thread
    if not MEMORY_UPDATES:
        return
    if _memory_thread is None:
        _memory_thread = threading.Thread(target=_memory_worker, args=(client,), name="memory", daemon=True)
        _memory_thread.start()
    _memory_queue.put(turn_digest(new_messages))


def finish_memory_updates() -> None:
    """On exit: let pending updates finish (up to MEMORY_EXIT_WAIT_SECONDS; Ctrl+C skips)."""
    if _memory_thread is None:
        return
    _memory_queue.put(None)
    if _memory_thread.is_alive() and _memory_queue.unfinished_tasks > 1:
        state.ui.status("[memory] saving notes... (Ctrl+C to skip)")
    try:
        _memory_thread.join(MEMORY_EXIT_WAIT_SECONDS)
    except KeyboardInterrupt:
        pass
    if _memory_thread.is_alive():
        _memory_report("not saved: the update was still running when the agent exited")
    print_memory_status()


