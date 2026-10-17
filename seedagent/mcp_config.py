"""MCP (Model Context Protocol) server configuration: which servers to connect to and how.

Two JSON files are merged, same shape as Claude Desktop's config:

    {"mcpServers": {"name": {"command": "...", "args": [...], "env": {...}}}}  # stdio server
    {"mcpServers": {"name": {"url": "https://..."}}}                           # streamable-HTTP server

- ~/.coding-agent/mcp_servers.json -- user-wide servers, available in every project.
- <project>/.agent/mcp_servers.json -- project-specific servers, next to .agent/skills/.

A server name defined in both is the project's version (the project file wins)."""

import json
from pathlib import Path

from .config import AGENT_HOME

USER_CONFIG = AGENT_HOME / "mcp_servers.json"


def _read(path: Path) -> dict[str, dict]:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    servers = data.get("mcpServers")
    return servers if isinstance(servers, dict) else {}


def load_server_configs(workspace: Path | None = None) -> dict[str, dict]:
    """The merged server configs: user-wide, then the project's (project entries win on a name clash).
    Each value is {"command": str, "args": [str, ...], "env": {...}} for a stdio server, or {"url": str}
    for a streamable-HTTP one. Entries that are not a dict, or that give neither "command" nor "url",
    are skipped."""
    servers = _read(USER_CONFIG)
    if workspace is not None:
        servers = {**servers, **_read(Path(workspace) / ".agent" / "mcp_servers.json")}
    return {
        name: cfg for name, cfg in servers.items()
        if isinstance(cfg, dict) and isinstance(name, str) and ("command" in cfg or "url" in cfg)
    }
