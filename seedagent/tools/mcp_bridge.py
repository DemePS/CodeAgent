"""MCP (Model Context Protocol) client support: connects to the servers configured in mcp_config,
discovers their tools, and registers each one with the agent's own tool registry (register_tool).

The MCP SDK is async-only (anyio); the rest of the agent is synchronous. The bridge is the one place
that crosses that line: a single background thread runs a dedicated asyncio event loop that owns
every server connection for the life of the process, and the functions below hop onto it with
run_coroutine_threadsafe and block for the result -- nothing elsewhere in the codebase needs to know
MCP is async."""

import asyncio
import atexit
import contextlib
import logging
import threading

from mcp import ClientSession, StdioServerParameters, types
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import MCPError

from .. import state
from ..common import ToolError
from . import register_tool

CALL_TIMEOUT_SECONDS = 120
CONNECT_TIMEOUT_SECONDS = 30

_loop: asyncio.AbstractEventLoop | None = None
_loop_lock = threading.Lock()
_sessions: dict[str, ClientSession] = {}  # server name -> its live session, kept open for the process
_connected: set[str] = set()  # server names already connected and registered (connect_configured_servers is idempotent)


def _ensure_loop() -> asyncio.AbstractEventLoop:
    """The bridge's event loop, started lazily on first use and kept running for the process (closing
    MCP sessions one at a time, by hand, is simpler and safer than tearing the loop down and back up)."""
    global _loop
    with _loop_lock:
        if _loop is not None and not _loop.is_closed():
            return _loop
        ready = threading.Event()

        def run() -> None:
            global _loop
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            _loop = loop
            ready.set()
            loop.run_forever()

        threading.Thread(target=run, name="mcp-bridge", daemon=True).start()
        ready.wait()
        return _loop


def _run(coro, timeout: float):
    """Run `coro` on the bridge loop from any (sync) thread and return its result."""
    future = asyncio.run_coroutine_threadsafe(coro, _ensure_loop())
    return future.result(timeout=timeout)


async def _connect_one(name: str, cfg: dict, stack) -> ClientSession:
    """Open a server's transport and MCP session into `stack` (its AsyncExitStack), so the connection
    outlives this coroutine and is closed later, in one place, by close_all."""
    if "url" in cfg:
        read, write, *_ = await stack.enter_async_context(streamable_http_client(cfg["url"]))
    else:
        params = StdioServerParameters(command=cfg["command"], args=cfg.get("args", []), env=cfg.get("env"))
        read, write = await stack.enter_async_context(stdio_client(params))
    session = await stack.enter_async_context(ClientSession(read, write))
    await asyncio.wait_for(session.initialize(), timeout=CONNECT_TIMEOUT_SECONDS)
    return session


# One AsyncExitStack per server, so each can be closed on its own by close_all. A plain dict instead of
# a module-level stack: `connect_configured_servers` can be called more than once (e.g. a new project),
# and a server already connected is left alone (see _connected).
_stacks: dict[str, "contextlib.AsyncExitStack"] = {}


def _content_blocks(result: types.CallToolResult) -> list[dict]:
    """MCP content (TextContent, ImageContent; embedded resources are shown as their text/description,
    not fetched) converted to the content blocks a tool_result may carry."""
    blocks = []
    for item in result.content:
        if isinstance(item, types.TextContent):
            blocks.append({"type": "text", "text": item.text})
        elif isinstance(item, types.ImageContent):
            blocks.append({"type": "image", "source": {"type": "base64", "media_type": item.mime_type, "data": item.data}})
        else:  # an embedded resource or another content type: show what we can as text
            text = getattr(item, "text", None) or getattr(getattr(item, "resource", None), "text", None)
            blocks.append({"type": "text", "text": text or f"[{type(item).__name__}, not shown]"})
    return blocks or [{"type": "text", "text": "(no content)"}]


def _make_handler(server: str, tool: types.Tool):
    """The sync handler registered for one MCP tool: runs the call on the bridge loop, confirms first
    unless the server marked the tool read-only, and turns an MCP/tool error into a ToolError."""
    read_only = bool(tool.annotations and tool.annotations.read_only_hint)

    def handler(**kwargs):
        if not read_only:
            label = f"{server}.{tool.name}"
            if state.auto_mode:
                state.ui.status(f"(autonomous mode: calling {label} without asking)")
            elif state.ui.confirm(f"\nAllow the MCP tool {label} to run with {kwargs!r}?") != "yes":
                raise ToolError(f"The user declined to run {label}.")
        try:
            result = _run(_sessions[server].call_tool(tool.name, kwargs), timeout=CALL_TIMEOUT_SECONDS)
        except MCPError as e:
            raise ToolError(f"{server}.{tool.name}: {e}") from e
        except TimeoutError as e:
            raise ToolError(f"{server}.{tool.name} timed out after {CALL_TIMEOUT_SECONDS}s.") from e
        if result.is_error:
            raise ToolError(f"{server}.{tool.name}: {_content_blocks(result)[0].get('text', 'error')}")
        return _content_blocks(result)

    handler.__name__ = f"mcp_{server}_{tool.name}"
    return handler


def _qualified_schema(server: str, tool: types.Tool) -> dict:
    schema = dict(tool.input_schema) if tool.input_schema else {"type": "object", "properties": {}}
    schema.setdefault("type", "object")
    schema.setdefault("properties", {})
    description = tool.description or f"Tool {tool.name!r} from the MCP server {server!r}."
    return {"name": f"mcp__{server}__{tool.name}", "description": description, "input_schema": schema}


def connect_configured_servers(configs: dict[str, dict]) -> list[str]:
    """Connect to every configured MCP server not already connected, discover its tools, and register
    each with register_tool (mcp__<server>__<tool>). Returns the names of the servers that failed to
    connect (logged, never raised: one bad server must not stop the agent from starting)."""
    log = logging.getLogger("seedagent.mcp")
    failed = []
    for name, cfg in configs.items():
        if name in _connected:
            continue
        stack = contextlib.AsyncExitStack()
        try:
            session = _run(_connect_one(name, cfg, stack), timeout=CONNECT_TIMEOUT_SECONDS)
            tools = _run(session.list_tools(), timeout=CONNECT_TIMEOUT_SECONDS).tools
        except Exception as e:
            log.warning("MCP server %r: could not connect (%s: %s)", name, type(e).__name__, e)
            failed.append(name)
            _run(stack.aclose(), timeout=CONNECT_TIMEOUT_SECONDS)
            continue
        _sessions[name] = session
        _stacks[name] = stack
        _connected.add(name)
        for tool in tools:
            try:
                register_tool(_qualified_schema(name, tool), _make_handler(name, tool))
            except (ValueError, TypeError) as e:
                # e.g. a tool name register_tool's naming rules reject, or two servers offering the
                # same tool name: skip that one tool, the rest of this server's tools still work.
                log.warning("MCP server %r: tool %r not registered (%s)", name, tool.name, e)
    return failed


def close_all() -> None:
    """Close every MCP session and its transport (process exit; also used by tests)."""
    for name in list(_connected):
        stack = _stacks.pop(name, None)
        if stack is not None:
            try:
                _run(stack.aclose(), timeout=CONNECT_TIMEOUT_SECONDS)
            except Exception:
                pass  # the server's own process may already be gone
        _sessions.pop(name, None)
    _connected.clear()


atexit.register(close_all)
