"""Token usage of this process, as counted by the API.

Tokens are reported by the API with every model call (`response.usage`), not by the tools, so the three places that call the
model record them here: the agent loop ("agent"), context compaction ("compact") and the memory curator ("memory").
Tool results are measured separately, as an estimate (characters / CHARS_PER_TOKEN), per tool name: that is how much each
tool adds to the conversation.

    mark = usage.snapshot()
    session.send("...")
    usage.since(mark)    # {"agent": {"input": ..., "output": ..., "cache_read": ..., "cache_write": ..., "calls": ...}, ...}
"""

from __future__ import annotations

import copy
import threading

from .config import CHARS_PER_TOKEN

_lock = threading.Lock()
_FIELDS = ("input", "output", "cache_read", "cache_write", "calls")
_calls: dict[str, dict[str, int]] = {}  # kind -> totals
_tools: dict[str, dict[str, int]] = {}  # tool name -> {"calls", "result_tokens"} (estimated)


def record(response, kind: str) -> None:
    """Add one model call's usage (kind: agent, compact or memory)."""
    u = getattr(response, "usage", None)
    if u is None:
        return
    with _lock:
        total = _calls.setdefault(kind, dict.fromkeys(_FIELDS, 0))
        total["input"] += u.input_tokens or 0
        total["output"] += u.output_tokens or 0
        total["cache_read"] += getattr(u, "cache_read_input_tokens", 0) or 0
        total["cache_write"] += getattr(u, "cache_creation_input_tokens", 0) or 0
        total["calls"] += 1


def record_tool(name: str, content) -> None:
    """Add one tool result to the estimate of what that tool put into the conversation."""
    chars = len(content) if isinstance(content, str) else len(str(content))
    with _lock:
        total = _tools.setdefault(name, {"calls": 0, "result_tokens": 0})
        total["calls"] += 1
        total["result_tokens"] += int(chars / CHARS_PER_TOKEN)


def snapshot() -> dict:
    with _lock:
        return {"calls": copy.deepcopy(_calls), "tools": copy.deepcopy(_tools)}


def since(mark: dict) -> dict:
    """What was used after `mark` (a snapshot): the same shape as snapshot()."""
    now = snapshot()
    out = {"calls": {}, "tools": {}}
    for part in out:
        for key, values in now[part].items():
            before = mark[part].get(key, {})
            diff = {k: v - before.get(k, 0) for k, v in values.items()}
            if any(diff.values()):
                out[part][key] = diff
    return out


def line(used: dict) -> str:
    """One line for what `since()` returned: "tokens: 1,234 in (800 cached), 56 out, 3 calls" (empty if nothing was used)."""
    total = dict.fromkeys(_FIELDS, 0)
    for values in used["calls"].values():
        for key in _FIELDS:
            total[key] += values[key]
    if not total["calls"]:
        return ""
    return (f"tokens: {total['input'] + total['cache_read'] + total['cache_write']:,} in ({total['cache_read']:,} cached), "
            f"{total['output']:,} out, {total['calls']} call(s)")


def reset() -> None:
    with _lock:
        _calls.clear()
        _tools.clear()
