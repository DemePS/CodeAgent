"""The agent loop: stream a response, run the requested tools, repeat; one instruction at a time."""


import threading

import anthropic

from . import state
from .config import MAX_STEPS, MAX_TOKENS, MODEL
from .context import (
    clear_old_tool_results,
    compact,
    context_status,
    estimate_tokens,
    is_context_overflow,
    learn_window,
    manage_context,
    record_usage,
    reset_usage,
)
from .conversation import save_conversation
from .errors import describe as describe_error
from .memory import memory_snapshot, queue_memory_update
from .prompts import SYSTEM_PROMPT
from .schemas import TOOLS
from .skills import skills_catalog
from .tools import run_tool


def check_stop() -> None:
    """Stop the current instruction if the front end asked to (handled like Ctrl+C by send())."""
    if state.stop_requested:
        state.stop_requested = False
        raise KeyboardInterrupt


def active_tools() -> list[dict]:
    """The tool definitions sent to Claude: all of them, or the subset the front end enabled."""
    if state.tool_names is None:
        return TOOLS
    return [t for t in TOOLS if t["name"] in state.tool_names]


WAIT_NOTICE_SECONDS = 30  # say so while Claude has not started answering (the SDK retries timeouts quietly)


class _Call:
    """One model call running in a helper thread (see stream_response)."""

    def __init__(self) -> None:
        self.cancelled = False
        self.stream = None
        self.started = False  # the first streamed event arrived
        self.result = None
        self.error: BaseException | None = None
        self.done = threading.Event()


def stream_response(client: anthropic.Anthropic, messages: list, max_tokens: int = MAX_TOKENS):
    """Stream one model response to the UI and return the final message.

    The call runs in a helper thread while this one checks for Stop every 0.1 s: a Stop (session.stop)
    ends the wait at once, even while a large request is being sent or Claude thinks before its first
    word. The abandoned call's connection is closed and it shows nothing more."""
    call = _Call()

    def run() -> None:
        try:
            call.result = _stream(client, messages, max_tokens, call)
        except BaseException as error:  # handed to the waiting thread
            call.error = error
        finally:
            call.done.set()

    threading.Thread(target=run, name="claude-call", daemon=True).start()
    ticks = 0  # of 0.1 s
    try:
        while not call.done.wait(0.1):
            check_stop()
            ticks += 1
            if not call.started and ticks % (WAIT_NOTICE_SECONDS * 10) == 0:
                state.ui.message(f"[still waiting for Claude: {ticks // 10} s]")
    except BaseException:  # Stop, or Ctrl+C in the terminal
        call.cancelled = True
        if call.stream is not None:
            try:
                call.stream.close()
            except Exception:
                pass
        raise
    if call.error is not None:
        raise call.error
    return call.result


def _stream(client: anthropic.Anthropic, messages: list, max_tokens: int, call: _Call):
    ui = state.ui
    with client.messages.stream(
        cache_control={"type": "ephemeral"},  # cache the growing prefix: each loop step re-reads it cheaply
        model=MODEL,
        max_tokens=max_tokens,
        system=(state.system_prompt or SYSTEM_PROMPT).format(workspace=state.workspace),
        tools=active_tools(),
        thinking={"type": "adaptive"},
        messages=messages,
    ) as stream:
        call.stream = stream
        for event in stream:
            call.started = True
            if call.cancelled:  # stopped: the waiting thread has moved on
                return None
            if event.type == "content_block_start":
                block = event.content_block
                if block.type == "text":
                    ui.assistant_start()
                elif block.type == "thinking":
                    ui.thinking()
                elif block.type in ("tool_use", "server_tool_use"):
                    ui.tool_start(block.name)
                elif block.type == "web_search_tool_result":
                    results = block.content
                    if isinstance(results, list):
                        ui.tool_detail(f"\n   {len(results)} result(s)")
                    else:  # an error object, e.g. max_uses_exceeded or unavailable
                        ui.tool_detail(f"\n   web search error: {getattr(results, 'error_code', results)}")
            elif event.type == "text":
                ui.assistant_text(event.text)
            elif event.type == "content_block_stop" and event.content_block.type in ("tool_use", "server_tool_use"):
                # The full input is only known once the block ends -- show a short summary.
                args = ", ".join(
                    f"{k}={v!r}"[:80] for k, v in event.content_block.input.items()
                    if k not in ("content", "old_string", "new_string", "file_text", "old_str", "new_str", "insert_text")
                )
                ui.tool_detail(f"({args})")
        if call.cancelled:
            return None
        ui.assistant_end()
        return stream.get_final_message()


def call_model(client: anthropic.Anthropic, messages: list):
    """One model call with context management and a single compact-and-retry on overflow."""
    manage_context(client, messages)
    for attempt in (1, 2):
        # Leave room for the answer: never ask for more output than the window has left.
        room = state.context_window - estimate_tokens(messages) - 2000
        try:
            return stream_response(client, messages, max_tokens=max(4096, min(MAX_TOKENS, room)))
        except anthropic.APIStatusError as e:
            if attempt == 2 or not is_context_overflow(e):
                raise
            learn_window(e)
            clear_old_tool_results(messages)
            if not compact(client, messages, "the prompt was too long"):
                raise


def run_turn(client: anthropic.Anthropic, messages: list) -> None:
    """Call the model repeatedly until it stops asking for tools (at most MAX_STEPS calls)."""
    for _ in range(MAX_STEPS):
        check_stop()
        response = call_model(client, messages)
        tool_uses = [b for b in response.content if b.type == "tool_use"]

        if response.stop_reason == "max_tokens" and tool_uses:
            # A tool call cut off mid-input must not run; drop the turn to keep history valid.
            state.ui.message("[stopped: hit max_tokens in the middle of a tool call]")
            return

        # Append the full content (text, thinking, tool_use) -- not just the text.
        # Stored as plain dicts so the history can be saved to JSON and resumed later.
        messages.append({"role": "assistant", "content": [b.to_dict() for b in response.content]})
        record_usage(response, messages)

        if response.stop_reason == "tool_use":
            # Run every requested tool and return ALL results in one user message.
            results = [run_tool(b) for b in tool_uses]
            messages.append({"role": "user", "content": results})
            continue

        if response.stop_reason == "pause_turn":
            continue  # a long server-side web search paused; re-sending the history resumes it
        if response.stop_reason == "max_tokens":
            state.ui.message("[stopped: hit max_tokens]")
        elif response.stop_reason == "refusal":
            state.ui.message("[the model declined this request]")
        return
    state.ui.message(f"[stopped: reached AGENT_MAX_STEPS={MAX_STEPS} model calls for this instruction; "
          "send another instruction to continue]")


def send(client: anthropic.Anthropic, messages: list, text: str) -> bool:
    """Run one user instruction through the agent loop. Returns False if it failed."""
    checkpoint = len(messages)
    state.compacted_this_turn = False
    state.turn.update(instruction=text, excel_read=False)
    state.stop_requested = False
    # Memory and the skill list go with the first instruction (skills only when load_skill is enabled).
    skill_list = [skills_catalog()] if state.tool_enabled("load_skill") else []
    blocks = [] if state.memory_sent else [memory_snapshot(), *skill_list]
    blocks += state.pending_blocks  # e.g. the summary from /compact
    if state.skills_note and state.memory_sent and state.tool_enabled("load_skill"):
        blocks.append(state.skills_note)
    if state.mode_note:
        blocks.append(state.mode_note)
    if state.read_roots_note:
        blocks.append(state.read_roots_note)
    if blocks:
        messages.append({"role": "user", "content": [{"type": "text", "text": t} for t in [*blocks, text]]})
    else:
        messages.append({"role": "user", "content": text})
    try:
        run_turn(client, messages)
        save_conversation(messages)
        # After a compaction the turn's start is gone; the whole (small) history stands in for it.
        queue_memory_update(client, messages if state.compacted_this_turn else messages[checkpoint:])
        state.memory_sent = True
        state.mode_note = state.skills_note = state.read_roots_note = None
        state.pending_blocks.clear()
        state.ui.status(f"[context] {context_status(messages)}")
        return True
    except KeyboardInterrupt:
        state.ui.message("[interrupted]")
    except Exception as e:
        explanation = describe_error(e)
        if explanation is None and not isinstance(e, RuntimeError):  # RuntimeError: e.g. an empty summary
            raise
        state.ui.error(explanation or str(e))
    # Drop the unfinished turn so the history stays valid for the next request.
    if state.compacted_this_turn:
        # The history before this instruction was replaced by a summary: keep that summary for
        # the next instruction instead of the lost messages.
        summary = next((b["text"] for b in messages[0]["content"]
                        if b.get("type") == "text" and b["text"].startswith("<compacted_history>")), None)
        messages.clear()
        state.pending_blocks[:] = [summary] if summary else []
        state.memory_sent = False
        reset_usage()
    else:
        del messages[checkpoint:]
    save_conversation(messages)
    return False


def set_auto_mode(on: bool) -> None:
    """Switch autonomous mode and queue a note so Claude learns it with the next instruction."""
    state.auto_mode = on
    if on:
        state.mode_note = ("<mode>Autonomous mode is ON: your edits, new files and run_python calls are "
                      "applied without asking, and ask_human will not be answered. delete_file and "
                      "delete_folder still ask the user.</mode>")
        state.ui.warning("Autonomous mode ON -- edits and Python runs are applied without asking. "
                         "Ctrl+C stops the agent; /auto turns this off.")
    else:
        state.mode_note = "<mode>Autonomous mode is OFF: the user approves each change again.</mode>"
        state.ui.message("Autonomous mode OFF -- every edit and Python run needs your approval.")


