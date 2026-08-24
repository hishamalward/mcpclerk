from __future__ import annotations

from typing import Any

from mcpclerk.audit import AuditLog
from mcpclerk.policy import parse_policy
from mcpclerk.server import Proxy, build_server
from tests.fake_upstream import make_fake


def policy(tools: dict[str, Any], **defaults: Any):
    raw = {
        "version": 1,
        "defaults": {"unlisted": defaults.pop("unlisted", "deny"),
                     "approval_timeout_s": defaults.pop("approval_timeout_s", 120)},
        "upstreams": {"fake": {"transport": "stdio", "command": "unused", "tools": tools}},
    }
    if defaults.get("redaction"):
        raw["redaction"] = defaults["redaction"]
    return parse_policy(raw, sha256="test", path="<test>")


def make_proxy(tmp_path, tools: dict[str, Any], **kw: Any):
    pol = policy(tools, **{k: v for k, v in kw.items() if k in ("unlisted", "approval_timeout_s", "redaction")})
    log_path = tmp_path / "audit.jsonl"
    proxy = Proxy(pol, AuditLog(log_path), str(tmp_path / "approvals"),
                  approve_session=kw.get("approve_session", False), tty=kw.get("tty"),
                  in_memory={"fake": make_fake()})
    return proxy, build_server(proxy), log_path
