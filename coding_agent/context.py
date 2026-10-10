"""Context window management: usage tracking, clearing old tool outputs, compaction."""

import json
import re

import anthropic

from . import state, usage
from .config import (
    CHARS_PER_TOKEN,
    CLEAR_AT,
    CLEAR_MIN_FREE,
    CLEARED_NOTE,
    COMPACT_AT,
    get_compact_model,
    IMAGE_TOKENS,
    KEEP_RECENT_RESULTS,
    PDF_PAGE_TOKENS,
)
from .conversation import save_conversation
from .memory import memory_snapshot, turn_digest
from .skills import skills_catalog

# --- Context management ----------------------------------------------------------------------------
# The history is append-only, so a long session would eventually overflow the context window.
# Before every model call: past CLEAR_AT, old tool outputs are replaced by a note (cheap, keeps the
# structure); past COMPACT_AT, the earlier conversation is summarized into one message. The size is
# measured by the API's usage numbers from the last call plus an estimate for what was added since.



def history_chars(messages: list) -> int:
    """Size of the history in characters, counting each image as its token cost, not its base64."""
    images = 0.0

    def strip(value):
        nonlocal images
        if isinstance(value, dict):
            if value.get("type") == "image":
                images += 1
                return None
            if value.get("type") == "document":  # a PDF: count its pages
                match = re.match(r"pages: (\d+)", str(value.get("context", "")))
                images += (int(match.group(1)) if match else 5) * PDF_PAGE_TOKENS / IMAGE_TOKENS
                return None
            return {k: strip(v) for k, v in value.items()}
        if isinstance(value, list):
            return [strip(v) for v in value]
        return value
    return len(json.dumps(strip(messages), ensure_ascii=False)) + int(images * IMAGE_TOKENS * CHARS_PER_TOKEN)


def estimate_tokens(messages: list) -> int:
    """Tokens the next request will use: last measured size + an estimate for the new messages."""
    chars = history_chars(messages)
    if not state.context["tokens"]:  # nothing measured yet (new session, after compaction or /clear)
        return int(chars / CHARS_PER_TOKEN) + 10_000  # + system prompt and tool definitions
    return max(0, state.context["tokens"] + int((chars - state.context["chars"]) / CHARS_PER_TOKEN))


def record_usage(response, messages: list) -> None:
    """Remember the real size of the conversation, as counted by the API (history + this reply)."""
    u = response.usage
    state.context["tokens"] = (u.input_tokens + (u.cache_read_input_tokens or 0)
                          + (u.cache_creation_input_tokens or 0) + u.output_tokens)
    state.context["chars"] = history_chars(messages)


def reset_usage() -> None:
    state.context["tokens"] = state.context["chars"] = 0


def context_status(messages: list) -> str:
    used = estimate_tokens(messages) if messages else 0
    return f"{used / 1000:.0f}k / {state.context_window / 1000:.0f}k tokens ({100 * used / state.context_window:.0f}%)"


def is_tool_results(message: dict) -> bool:
    return (message["role"] == "user" and isinstance(message["content"], list)
            and any(b.get("type") == "tool_result" for b in message["content"]))


def _clearable(messages: list) -> list[dict]:
    """The tool_result blocks clear_old_tool_results would replace: large or with an image, not among the latest ones."""
    result_messages = [m for m in messages if is_tool_results(m)]
    blocks = []
    for m in result_messages[:-KEEP_RECENT_RESULTS]:
        for b in m["content"]:
            content = b.get("content")
            if b.get("type") != "tool_result":
                continue
            has_image = isinstance(content, list) and any(c.get("type") in ("image", "document") for c in content)
            if has_image or (isinstance(content, str) and len(content) > 300):
                blocks.append(b)
    return blocks


def clearable_tokens(messages: list) -> int:
    """About how many tokens clear_old_tool_results would free now."""
    return int(sum(history_chars([b]) for b in _clearable(messages)) / CHARS_PER_TOKEN)


PAGE_READ_TOOLS = ("read_pdf",)


def forget_page_reads(messages: list, start: int = 0) -> int:
    """At the end of a turn, replace the pages read VISUALLY in it (page images, a PDF document block) with a reference: which file and
    which pages. Images are about 1,600 tokens a page, slow to process, and would be sent again with every later model call. The text of a
    page is cached (pdf_page_texts, the OCR cache, the index), so reading it again is quick. Text reads stay as they are: the provider's
    prompt cache makes carrying them cheap.
    New message objects are made: the lists that other code holds (the memory curator reads the turn) are not changed.
    Returns how many results were replaced."""
    calls = {}
    for message in messages[start:]:
        if message["role"] == "assistant" and isinstance(message["content"], list):
            for block in message["content"]:
                if block.get("type") == "tool_use":
                    calls[block["id"]] = block
    replaced = 0
    for i in range(start, len(messages)):
        message = messages[i]
        if message["role"] != "user" or not isinstance(message["content"], list):
            continue
        blocks, changed = [], False
        for block in message["content"]:
            call = calls.get(block.get("tool_use_id")) if block.get("type") == "tool_result" else None
            content = block.get("content")
            visual = isinstance(content, list) and any(isinstance(part, dict) and part.get("type") in ("image", "document") for part in content)
            if call and call["name"] in PAGE_READ_TOOLS and visual and not block.get("is_error"):
                shown = call["input"]
                block = {**block, "content": (f"[{call['name']} {shown.get('path')}, pages {shown.get('pages') or 'all'}, read as images: the images are "
                                              f"not kept in the conversation; read the pages again (mode 'text' if they have text) if you need them]")}
                changed = True
                replaced += 1
            blocks.append(block)
        if changed:
            messages[i] = {**message, "content": blocks}
    return replaced


def clear_old_tool_results(messages: list) -> int:
    """Replace the output of older tool calls with a short note. Returns how many were cleared."""
    cleared = 0
    for b in _clearable(messages):
        b["content"] = CLEARED_NOTE
        cleared += 1
    state.context["cleared"] += cleared
    return cleared


COMPACT_PROMPT = """You compact the conversation history of a coding agent so it can continue its
work with a much smaller context. You get a transcript of the earlier conversation (the user's
instructions, the tools the agent called with their results, and the agent's replies).

Write a brief that lets the agent continue seamlessly, with these sections:
- Goal: what the user asked for, in their words where it matters, and every instruction still in effect.
- Decisions and preferences: what was decided and why; changes the user rejected and their feedback.
- Files: files created, modified or deleted (path and what changed), and key places (path:line).
- Findings: facts learned that are still needed -- commands that work, errors seen, test results.
- State: what is done and what is in progress right now.
- Next steps: what remains, in order.
Be specific and factual; keep paths, names, commands and error messages exact. Leave out
anything that no longer matters. Never include secrets. Answer only with the brief inside
<summary>...</summary>."""


def summarize_history(client: anthropic.Anthropic, head: list) -> str:
    """One model call that turns the older messages into a brief."""
    budget = state.context_window * 2  # characters, well inside the window even for dense text
    for result_chars in (3000, 1000, 300, 0):  # shrink tool outputs until the transcript fits
        transcript = turn_digest(head, result_chars=result_chars, limit=False)
        if len(transcript) <= budget:
            break
    else:
        transcript = "[the earliest part of the conversation was omitted]\n" + transcript[-budget:]
    response = client.messages.create(
        model=get_compact_model(),
        max_tokens=8000,
        system=COMPACT_PROMPT,
        messages=[{"role": "user", "content": f"<transcript>\n{transcript}\n</transcript>"}],
    )
    usage.record(response, "compact")
    text = "".join(b.text for b in response.content if b.type == "text")
    match = re.search(r"<summary>\s*(.*?)\s*(?:</summary>|$)", text, re.DOTALL)
    summary = (match.group(1) if match else text).strip()
    if not summary:
        raise RuntimeError(f"empty summary (stop reason: {response.stop_reason})")
    return summary


def compacted_block(summary: str) -> str:
    return ("<compacted_history>\nThe earlier part of this conversation was compacted to save context. "
            f"Summary:\n{summary}\n</compacted_history>")


def session_blocks() -> list[str]:
    """What a fresh context needs besides the summary: memory, skills and the current mode."""
    blocks = [memory_snapshot(), *([skills_catalog()] if state.tool_enabled("load_skill") else [])]
    if state.auto_mode:
        blocks.append("<mode>Autonomous mode is ON: your edits, new files and run_python calls are applied "
                      "without asking, and ask_human will not be answered. delete_file and delete_folder still ask the user.</mode>")
    return blocks


def compact(client: anthropic.Anthropic, messages: list, reason: str) -> bool:
    """During an instruction: summarize everything before the latest step, keep that step verbatim.

    The history must end with a user message: either the instruction itself (then the summary goes
    in front of it) or tool results (then their assistant message is kept too, so the tool_use /
    tool_result pairs stay valid).
    """
    tail = messages[-2:] if is_tool_results(messages[-1]) else messages[-1:]
    head = messages[:-len(tail)]
    if not head:
        return False
    state.ui.status(f"[context] {reason}: compacting {len(head)} earlier messages...")
    summary = summarize_history(client, head)
    blocks = [*session_blocks(), compacted_block(summary)]
    first = [{"type": "text", "text": t} for t in blocks]
    if len(tail) == 1:  # the instruction: keep what the user typed, drop its old memory/skills blocks
        content = tail[0]["content"]
        content = [{"type": "text", "text": content}] if isinstance(content, str) else [
            b for b in content if not (b.get("type") == "text" and b["text"].startswith(
                ("<memory>", "<skills>", "<compacted_history>", "<mode>")))]
        messages[:] = [{"role": "user", "content": first + content}]
    else:
        messages[:] = [{"role": "user", "content": first}, *tail]
    state.context["compactions"] += 1
    state.compacted_this_turn = True
    reset_usage()
    state.ui.status(f"[context] compacted -> about {context_status(messages)}")
    return True


def compact_between_instructions(client: anthropic.Anthropic, messages: list) -> None:
    """/compact: summarize the whole conversation; the summary goes with the next instruction."""
    if not messages:
        state.ui.message("Nothing to compact.")
        return
    state.ui.status(f"[context] compacting {len(messages)} messages...")
    summary = summarize_history(client, messages)
    messages.clear()
    state.pending_blocks[:] = [compacted_block(summary)]
    state.memory_sent = False  # memory and skills go again with the next instruction
    state.context["compactions"] += 1
    reset_usage()
    save_conversation(messages)
    state.ui.status("[context] done; the summary is sent with your next instruction.")


def manage_context(client: anthropic.Anthropic, messages: list) -> None:
    """Before a model call: clear old tool outputs, then compact, when the history gets large.

    Clearing edits earlier messages, so the API's cache of the conversation is lost from the first cleared output on and
    the next call pays for the whole history again. So it happens only when it frees a good share of the window
    (CLEAR_MIN_FREE), all at once: then the history stays well under the threshold for many calls, each one a cache hit."""
    if (estimate_tokens(messages) > CLEAR_AT * state.context_window
            and clearable_tokens(messages) >= CLEAR_MIN_FREE * state.context_window):
        cleared = clear_old_tool_results(messages)
        if cleared:
            state.ui.status(f"[context] cleared {cleared} old tool output(s) -> about "
                            f"{context_status(messages)}")
    if estimate_tokens(messages) > COMPACT_AT * state.context_window:
        compact(client, messages, f"over {COMPACT_AT:.0%} of the context window")


def is_context_overflow(error: anthropic.APIStatusError) -> bool:
    return error.status_code in (400, 413) and bool(
        re.search(r"too long|exceed.*context|context.*(limit|window|length)", str(error.message), re.I))


def learn_window(error: anthropic.APIStatusError) -> None:
    """'prompt is too long: 210000 tokens > 200000 maximum' tells us the real window."""
    match = re.search(r"(\d+) tokens? > (\d+)", str(error.message))
    if match and int(match.group(2)) < state.context_window:
        state.context_window = int(match.group(2))
        state.ui.status(f"[context] this deployment's window is {state.context_window:,} tokens; "
                        "set AGENT_CONTEXT_WINDOW to that value")


