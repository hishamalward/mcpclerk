"""One connection per upstream MCP server. stdio and Streamable HTTP differ only in construction."""
from __future__ import annotations

import inspect
import os
from contextlib import AsyncExitStack
from typing import Any

from mcp import types
from mcp.client import Client
from mcp.client.stdio import StdioServerParameters, stdio_client

from mcpclerk.policy import PolicyError, UpstreamConfig


def _http_transport(cfg: UpstreamConfig) -> Any:
    from mcp.client.streamable_http import StreamableHTTPTransport
    params = inspect.signature(StreamableHTTPTransport.__init__).parameters
    if cfg.headers:
        if "headers" not in params:
            raise PolicyError(f"upstreams.{cfg.name}.headers: this MCP SDK's HTTP transport takes no headers; "
                              "remove them or use an upstream that needs none (see README, not yet)")
        return StreamableHTTPTransport(cfg.url, headers=cfg.headers)
    return StreamableHTTPTransport(cfg.url)


class Upstream:
    def __init__(self, cfg: UpstreamConfig, *, in_memory: Any = None):
        self.cfg = cfg
        self.name = cfg.name
        self._in_memory = in_memory  # a Server/MCPServer instance, for tests
        self._stack = AsyncExitStack()
        self.client: Client | None = None
        self.tools: list[types.Tool] = []

    def _make_client(self) -> Client:
        if self._in_memory is not None:
            return Client(self._in_memory)
        if self.cfg.transport == "stdio":
            env = dict(os.environ)
            env.update(self.cfg.env)
            params = StdioServerParameters(command=self.cfg.command or "", args=list(self.cfg.args),
                                           env=env, cwd=self.cfg.cwd)
            return Client(stdio_client(params))
        return Client(_http_transport(self.cfg))

    async def open(self) -> None:
        await self._stack.__aenter__()
        self.client = await self._stack.enter_async_context(self._make_client())
        result = await self.client.list_tools()
        self.tools = list(result.tools)

    async def close(self) -> None:
        await self._stack.aclose()

    async def call(self, tool: str, arguments: dict[str, Any]) -> types.CallToolResult:
        assert self.client is not None, "upstream not open"
        return await self.client.call_tool(tool, arguments)
