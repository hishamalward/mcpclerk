"""mcpclerk serve | approve | tools | verify | report"""
from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from mcpclerk import __version__
from mcpclerk.policy import PolicyError, load_policy

DEFAULT_LOG = "audit/mcpclerk.jsonl"
DEFAULT_APPROVALS = "approvals"


def _serve(a: argparse.Namespace) -> int:
    import anyio
    from mcp.server.stdio import stdio_server

    from mcpclerk.approval import open_tty
    from mcpclerk.audit import AuditLog
    from mcpclerk.server import Proxy, build_server

    policy = load_policy(a.policy)
    tty = None if a.no_tty else open_tty()
    proxy = Proxy(policy, AuditLog(a.log), a.approvals, approve_session=a.approve_session, tty=tty)
    server = build_server(proxy)

    async def run() -> None:
        async with stdio_server() as (read, write):
            await server.run(read, write, server.create_initialization_options())

    anyio.run(run)
    return 0


def _tools(a: argparse.Namespace) -> int:
    import anyio

    from mcpclerk.upstream import Upstream

    policy = load_policy(a.policy)

    async def run() -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for name, cfg in policy.upstreams.items():
            up = Upstream(cfg)
            await up.open()
            try:
                for t in up.tools:
                    rule = policy.resolve(name, t.name)
                    ann = t.annotations.model_dump(exclude_none=True) if t.annotations else {}
                    rows.append({"name": policy.join(name, t.name), "decision": rule.decision, "rule": rule.source,
                                 "read_only": ann.get("read_only_hint"), "destructive": ann.get("destructive_hint"),
                                 "per_run": rule.per_run, "per_minute": rule.per_minute})
            finally:
                await up.close()
        return rows

    rows = anyio.run(run)
    if a.json:
        print(json.dumps(rows, indent=2))
        return 0
    print(f"{'tool':<36} {'decision':<8} {'rule':<18} {'read_only':<9} {'destructive':<11} quota")
    warn = 0
    for r in rows:
        q = "/".join(str(x) if x is not None else "-" for x in (r["per_run"], r["per_minute"]))
        flag = ""
        if r["destructive"] and r["decision"] == "allow":
            flag = "  <- destructive tool allowed without approval"
            warn += 1
        print(f"{r['name']:<36} {r['decision']:<8} {r['rule']:<18} {str(r['read_only']):<9} {str(r['destructive']):<11} {q}{flag}")
    if warn:
        print(f"\n{warn} destructive tool(s) are allowed without approval; consider 'approve'.", file=sys.stderr)
    return 0


def _approve(a: argparse.Namespace) -> int:
    from mcpclerk.approval import approve_loop
    approve_loop(a.approvals, once=a.once, wait_s=a.wait)
    return 0


def _verify(a: argparse.Namespace) -> int:
    from mcpclerk.audit import verify
    r = verify(a.log)
    print(r)
    return 0 if r.ok else 1


def _report(a: argparse.Namespace) -> int:
    from mcpclerk.audit import format_report, report
    r = report(a.log)
    print(json.dumps(r, indent=2) if a.json else format_report(r))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="mcpclerk", description="A governance proxy for MCP servers.")
    p.add_argument("--version", action="version", version=f"mcpclerk {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("serve", help="run the proxy over stdio")
    s.add_argument("--policy", required=True)
    s.add_argument("--log", default=DEFAULT_LOG)
    s.add_argument("--approvals", default=DEFAULT_APPROVALS)
    s.add_argument("--approve-session", action="store_true", help="auto-approve every approve-class call this run (logged loudly)")
    s.add_argument("--no-tty", action="store_true", help="skip the terminal prompt; the file queue is the only approval path")
    s.set_defaults(fn=_serve)

    t = sub.add_parser("tools", help="list upstream tools with their annotations and the policy's decision")
    t.add_argument("--policy", required=True)
    t.add_argument("--json", action="store_true")
    t.set_defaults(fn=_tools)

    ap = sub.add_parser("approve", help="answer pending approvals from a second terminal (the proxy itself usually has no terminal)")
    ap.add_argument("--approvals", default=DEFAULT_APPROVALS)
    ap.add_argument("--once", action="store_true", help="answer one request and exit")
    ap.add_argument("--wait", type=float, default=60.0, help="seconds to wait for a request to appear")
    ap.set_defaults(fn=_approve)

    v = sub.add_parser("verify", help="check the audit log's hash chain")
    v.add_argument("log")
    v.set_defaults(fn=_verify)

    r = sub.add_parser("report", help="summarize an audit log")
    r.add_argument("log")
    r.add_argument("--json", action="store_true")
    r.set_defaults(fn=_report)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.fn(args))
    except PolicyError as e:
        print(f"mcpclerk: policy error: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
