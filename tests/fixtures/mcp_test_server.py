"""A tiny MCP server (stdio) used by tests/test_mcp.py: one ordinary tool, one that raises, one marked
read-only. Run as its own process (StdioServerParameters(command=sys.executable, args=[this file]))."""

import asyncio

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

mcp = MCPServer("test-server")


@mcp.tool()
def add(a: int, b: int) -> int:
    """Add two numbers."""
    return a + b


@mcp.tool()
def fail() -> str:
    """Always raises."""
    raise RuntimeError("boom")


@mcp.tool(annotations=ToolAnnotations(read_only_hint=True))
def whoami() -> str:
    """A read-only tool: never asks for confirmation."""
    return "test-server"


if __name__ == "__main__":
    asyncio.run(mcp.run_stdio_async())
