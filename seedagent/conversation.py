"""The saved conversation of a project, so --resume can continue it."""

import json

from . import state


def save_conversation(messages: list) -> None:
    """Write the history to disk (atomically) so --resume can pick it up."""
    state.conversation_file.parent.mkdir(parents=True, exist_ok=True)
    tmp = state.conversation_file.with_suffix(".tmp")
    tmp.write_text(json.dumps(messages, ensure_ascii=False), encoding="utf-8")
    tmp.replace(state.conversation_file)


def load_conversation() -> list:
    """Load the saved history, or return [] if there is none."""
    if not state.conversation_file.is_file():
        state.ui.message("No saved conversation for this project; starting a new one.")
        return []
    try:
        messages = json.loads(state.conversation_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        state.ui.message(f"Could not read {state.conversation_file} ({e}); starting a new conversation.")
        return []

    def instruction(m: dict) -> str | None:
        """The user's typed text, or None for tool-result messages."""
        if isinstance(m["content"], str):
            return m["content"]
        texts = [b["text"] for b in m["content"] if b.get("type") == "text"]
        if not texts or texts[-1].startswith("<compacted_history>"):
            return None
        return texts[-1]  # last text block; earlier ones may be memory and skills

    user_turns = [t for m in messages if m["role"] == "user" and (t := instruction(m)) is not None]
    compacted = any(isinstance(m["content"], list) and any(
        b.get("type") == "text" and b["text"].startswith("<compacted_history>") for b in m["content"])
        for m in messages[:1])
    state.ui.message(f"Resumed conversation: {len(user_turns)} earlier instruction(s)"
                     + (" after a summary of the older ones (compacted)." if compacted else "."))
    if user_turns:
        state.ui.status(f"Last instruction: {user_turns[-1][:200]}")
    last_text = next(
        (b["text"] for m in reversed(messages) if m["role"] == "assistant"
         for b in reversed(m["content"]) if b.get("type") == "text"),
        None,
    )
    if last_text:
        state.ui.status(f"Last reply: {last_text[:300]}")
    return messages
