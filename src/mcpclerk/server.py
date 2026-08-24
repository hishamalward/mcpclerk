"""The proxy: a low-level MCP server whose handlers run the policy pipeline, in call order.

    discover -> namespace -> allowlist -> quota -> redact-for-log -> approval -> forward -> log
"""
from __future__ import annotations

import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, AsyncIterator

from mcp import types
from mcp.server.lowlevel import Server

from mcpclerk import __version__
from mcpclerk.approval import ApprovalRequest, Tty, request_approval, say
from mcpclerk.audit import AuditLog
from mcpclerk.policy import Policy, ToolRule
from mcpclerk.quota import QuotaBook
from mcpclerk.redact import Redactor
from mcpclerk.upstream import Upstream


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


@dataclass
class Exposed:
    upstream: str
    tool: str
    rule: ToolRule
    definition: types.Tool


@dataclass
class Proxy:
    policy: Policy
    log: AuditLog
    approvals_dir: str
    approve_session: bool = False
    tty: Tty | None = None
    in_memory: dict[str, Any] = field(default_factory=dict)  # upstream name -> server instance (tests)

    def __post_init__(self) -> None:
        self.run_id = f"{_stamp()}-{uuid.uuid4().hex[:4]}"
        self.redactor = Redactor(self.policy.redaction.extend, self.policy.redaction.extend_keys,
                                 self.policy.redaction.replace_builtin)
        self.quotas = QuotaBook()
        self.upstreams: dict[str, Upstream] = {}
        self.exposed: dict[str, Exposed] = {}
        self._call_counter = 0

    # ---- lifecycle -------------------------------------------------------------------------

    async def start(self) -> None:
        self._seq_at_start = self.log.seq
        self.log.append({
            "kind": "event", "event": "run-start", "ts": _now_iso(), "run_id": self.run_id,
            "version": __version__, "policy_path": self.policy.path, "policy_sha256": self.policy.sha256,
            "upstreams": sorted(self.policy.upstreams), "approve_session": self.approve_session,
            "approval_tty": self.tty is not None,
        })
        if self.approve_session:
            say("[mcpclerk] WARNING: --approve-session is set. Every approve-class call in this run "
                "is auto-approved and logged as approved_by=session-flag.", self.tty)
        for name, cfg in self.policy.upstreams.items():
            up = Upstream(cfg, in_memory=self.in_memory.get(name))
            await up.open()
            self.upstreams[name] = up
            exposed = hidden = 0
            for tool in up.tools:
                rule = self.policy.resolve(name, tool.name)
                if rule.decision == "deny":
                    hidden += 1
                    continue
                exposed += 1
                ns = self.policy.join(name, tool.name)
                self.exposed[ns] = Exposed(name, tool.name, rule, tool.model_copy(update={"name": ns}))
            self.log.append({
                "kind": "event", "event": "discover", "ts": _now_iso(), "run_id": self.run_id,
                "upstream": name, "exposed": exposed, "hidden": hidden,
            })

    async def stop(self) -> None:
        for up in self.upstreams.values():
            try:
                await up.close()
            except Exception:  # an upstream that dies on close must not lose the run-end entry
                pass
        self.log.append({
            "kind": "event", "event": "run-end", "ts": _now_iso(), "run_id": self.run_id,
            "entries": self.log.seq - self._seq_at_start + 1,  # includes this run-end entry
        })
        self.log.close()

    # ---- handlers --------------------------------------------------------------------------

    def list_tools(self) -> types.ListToolsResult:
        return types.ListToolsResult(tools=[e.definition for e in self.exposed.values()])

    async def call_tool(self, name: str, arguments: dict[str, Any] | None) -> types.CallToolResult:
        args = arguments or {}
        self._call_counter += 1
        call_id = f"{_stamp()}-{self._call_counter:04d}"
        entry: dict[str, Any] = {"kind": "call", "ts": _now_iso(), "run_id": self.run_id, "id": call_id, "name": name}

        # namespace
        parts = self.policy.split(name)
        if parts is None or parts[0] not in self.upstreams:
            entry.update(upstream=None, tool=None, args=self.redactor.redact(args),
                         decision="refused-unknown", outcome="not-forwarded")
            return self._refuse(entry, f"refused-unknown {name}: no such upstream")
        upstream, tool = parts
        entry.update(upstream=upstream, tool=tool, args=self.redactor.redact(args))
        known = any(t.name == tool for t in self.upstreams[upstream].tools)
        if not known:
            entry.update(decision="refused-unknown", outcome="not-forwarded")
            return self._refuse(entry, f"refused-unknown {name}: upstream has no such tool")

        # allowlist
        rule = self.policy.resolve(upstream, tool)
        entry["rule"] = rule.source
        if rule.decision == "deny":
            entry.update(decision="refused-denied", outcome="not-forwarded")
            return self._refuse(entry, f"refused-denied {name}: policy denies this tool ({rule.source})")

        # quota
        refusal = self.quotas.try_acquire(name, rule.per_run, rule.per_minute)
        if refusal is not None:
            entry.update(decision="refused-quota", outcome="not-forwarded", retry_after_s=refusal.retry_after_s)
            return self._refuse(entry, f"refused-quota {refusal.reason}")

        # approval (args already redacted for the log and the human; the upstream gets `args`)
        if rule.decision == "approve":
            if self.approve_session:
                entry.update(decision="approved", approved_by="session-flag", held_ms=0.0)
            else:
                held_start = time.perf_counter()
                outcome = await request_approval(
                    ApprovalRequest(call_id, upstream, tool, name, entry["args"], rule.approval_timeout_s),
                    self.approvals_dir, tty=self.tty)
                entry["held_ms"] = round((time.perf_counter() - held_start) * 1000, 1)
                if outcome.decision == "timeout":
                    entry.update(decision="refused-timeout", outcome="not-forwarded")
                    return self._refuse(entry, f"refused-timeout {name}: no approval within {rule.approval_timeout_s:.0f}s")
                if outcome.decision == "refused":
                    entry.update(decision="refused-by-human", outcome="not-forwarded", refused_by=outcome.by)
                    if outcome.note:
                        entry["note"] = outcome.note
                    return self._refuse(entry, f"refused-by-human {name}" + (f": {outcome.note}" if outcome.note else ""))
                entry.update(decision="approved", approved_by=outcome.by)
                if outcome.note:
                    entry["note"] = outcome.note
        else:
            entry["decision"] = "allowed"

        # forward, with the original arguments
        t0 = time.perf_counter()
        try:
            result = await self.upstreams[upstream].call(tool, args)
        except Exception as e:  # transport failure, not a tool error
            entry.update(outcome="error", is_error=True, latency_ms=round((time.perf_counter() - t0) * 1000, 1),
                         error=type(e).__name__)
            self.log.append(entry)
            return types.CallToolResult(
                content=[types.TextContent(type="text", text=f"mcpclerk: upstream {upstream} failed: {type(e).__name__}: {e}")],
                is_error=True)
        latency = round((time.perf_counter() - t0) * 1000, 1)
        content_bytes = sum(len(c.text.encode("utf-8")) for c in result.content if isinstance(c, types.TextContent))
        entry.update(outcome="error" if result.is_error else "ok", is_error=bool(result.is_error),
                     latency_ms=latency, content_bytes=content_bytes,
                     content_types=sorted({c.type for c in result.content}))
        self.log.append(entry)
        return result

    def _refuse(self, entry: dict[str, Any], message: str) -> types.CallToolResult:
        entry.setdefault("is_error", True)
        entry.setdefault("latency_ms", None)
        self.log.append(entry)
        return types.CallToolResult(content=[types.TextContent(type="text", text=f"mcpclerk: {message}")], is_error=True)


def build_server(proxy: Proxy) -> Server:
    """Wrap a Proxy in a low-level MCP server. Upstreams open in the lifespan, so discovery
    happens once per run and the exposed set is fixed before the first request."""

    @asynccontextmanager
    async def lifespan(_: Server) -> AsyncIterator[dict[str, Any]]:
        await proxy.start()
        try:
            yield {}
        finally:
            await proxy.stop()

    async def on_list_tools(ctx: Any, params: Any) -> types.ListToolsResult:
        return proxy.list_tools()

    async def on_call_tool(ctx: Any, params: types.CallToolRequestParams) -> types.CallToolResult:
        return await proxy.call_tool(params.name, params.arguments)

    return Server("mcpclerk", version=__version__,
                  instructions="Tools are namespaced upstream.tool. Some calls wait for a human approval; "
                               "refused calls return is_error with the control that refused them.",
                  lifespan=lifespan, on_list_tools=on_list_tools, on_call_tool=on_call_tool)
