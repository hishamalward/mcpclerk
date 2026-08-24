"""An in-process MCP server that stands in for any real upstream.

secret_sink returns exactly what it received, which is how the tests prove that the upstream sees
unredacted arguments while the log does not.
"""
from __future__ import annotations

import json
from typing import Any

from mcp.server.mcpserver import MCPServer


def make_fake() -> MCPServer:
    server = MCPServer("fake")
    state: dict[str, str] = {}

    @server.tool()
    def echo(text: str) -> str:
        """Return the text."""
        return text

    @server.tool()
    def read_thing(path: str) -> str:
        """Read a thing."""
        return state.get(path, "")

    @server.tool()
    def write_thing(path: str, content: str) -> str:
        """Write a thing."""
        state[path] = content
        return f"wrote {len(content)} chars to {path}"

    @server.tool()
    def delete_thing(path: str) -> str:
        """Delete a thing."""
        state.pop(path, None)
        return f"deleted {path}"

    @server.tool()
    def secret_sink(payload: dict[str, Any]) -> str:
        """Return the payload verbatim."""
        return json.dumps(payload, sort_keys=True)

    @server.tool()
    def boom(text: str) -> str:
        """Always fails."""
        raise ValueError(f"upstream failure: {text}")

    return server
