"""Task authorization for native MCP calls, independent of host approval settings."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from . import catalog
from .connection import pinned_project

READ, WRITE, PAID, PUBLISH, STORE, UNKNOWN = "read", "remote-write", "paid", "publish", "store", "unknown"
NO_TARGET = object()
NEEDS = {WRITE: "remote-write", PAID: "paid-job", PUBLISH: "publish", STORE: "store-submission"}
SERVERS = {"revenuecat": {"revenuecat"}, "posthog": {"posthog", "plugin_posthog_posthog"},
           "sentry": {"sentry", "plugin_sentry_sentry"},
           "dataforseo": {"dataforseo", "dfs-mcp", "dfs_mcp"}}


@dataclass
class Call:
    service: str
    server: str
    tool: str
    args: dict[str, Any]
    entry: dict[str, Any]


def match(tool_name: str, entries: dict[str, dict[str, Any]]) -> Call | None:
    if not tool_name.startswith("mcp__"):
        return None
    rest = tool_name[5:]
    names = []
    for service, entry in entries.items():
        own = SERVERS.get(service) or ({service} if not entry.get("servers") else set())
        for server in own | set(entry.get("servers") or []):
            names.append((-len(server), service not in SERVERS, server, service))
    for _length, _custom, server, service in sorted(names):
        if rest.startswith(server + "__"):
            return Call(service, server, rest[len(server) + 2:], {}, entries[service])
    return None


def _listed(name: str, entry: dict[str, Any]) -> str:
    for cls, key in ((STORE, "store"), (PUBLISH, "publish"), (PAID, "paid"), (WRITE, "write"),
                     (WRITE, "unclassified"), (READ, "read")):
        if name in (entry.get(key) or []):
            return cls
    return UNKNOWN


def _target(entry: dict[str, Any], args: dict[str, Any], name: str):
    if not entry.get("target_arg"):
        sorted_name = any(name in (entry.get(k) or []) for k in catalog.CLASS_KEYS if k != "unclassified")
        return None if name in (entry.get("unclassified") or []) and not sorted_name else NO_TARGET
    value = args.get(entry["target_arg"])
    return None if value is None or value == "" else str(value)


def _classify(call: Call) -> tuple[str, Any]:
    args, service, entry = call.args, call.service, call.entry
    if service == "revenuecat":
        return _listed(call.tool, entry), args.get("project_id")
    if service == "sentry":
        name = call.tool
        if name == "execute_sentry_tool":
            name = str(args.get("name") or "")
            args = {**args, **(args.get("arguments") if isinstance(args.get("arguments"), dict) else {})}
        target = args.get("organizationSlug")
        if not target:
            m = re.match(r"^https://([a-z0-9-]+)\.sentry\.io/", str(args.get("issueUrl") or ""))
            target = m.group(1) if m else None
        return _listed(name, entry), target
    if service == "posthog":
        if call.tool != "exec":
            return UNKNOWN, None
        words = str(args.get("command") or "").split()
        verb = words[0] if words else ""
        if verb in (entry.get("verbs_read") or []):
            return READ, None
        if verb == "switch":
            return WRITE, words[1] if len(words) > 1 else None
        if verb != "call":
            return UNKNOWN, None
        rest = [w for w in words[1:] if w not in ("--json", "--confirm")]
        if not rest or rest[0].startswith("-"):
            return UNKNOWN, None
        operation = rest[0]
        m = re.search(r"\{.*\}\s*$", str(args.get("command") or ""), re.S)
        try:
            body = json.loads(m.group()) if m else {}
        except ValueError:
            body = {}
        if not isinstance(body, dict):
            body = {}
        target = None
        if operation == "project-settings-update":
            target = body.get("id")
        elif operation in ("switch-project", "switch-organization"):
            target = next((body[k] for k in ("projectId", "project_id", "id", "organizationId", "organization_id") if body.get(k) is not None), None)
        return _listed(operation, entry), None if target is None else str(target)
    if service == "dataforseo":
        if call.tool != "api_request" and _listed(call.tool, entry) == READ:
            return READ, None
        if call.tool == "api_request":
            path = str(args.get("path") or args.get("url") or "")
            method = str(args.get("method") or "GET").upper()
            segments = set(re.split(r"[/?#]", path))
            if segments & set(entry.get("paid_segments") or []):
                return PAID, NO_TARGET
            if method == "GET" and segments & set(entry.get("read_segments") or []):
                return READ, None
            return UNKNOWN, None
        return _listed(call.tool, entry), _target(entry, args, call.tool)
    return _listed(call.tool, entry), _target(entry, args, call.tool)


def _reason(allowed: bool, message: str, classification: str) -> dict[str, Any]:
    return {"allowed": allowed, "reason": message, "classification": classification}


def evaluate(payload: dict[str, Any], environ=None) -> dict[str, Any]:
    """Judge {version, tool, args, host, cwd, project_dir, task.permissions} without logging values."""
    import os
    env = os.environ if environ is None else environ
    if not isinstance(payload, dict) or payload.get("version") != 1:
        raise ValueError("judge input must be a version 1 object")
    tool_name = payload.get("tool")
    args = payload.get("args", {})
    names = tool_name[5:].split("__", 1) if isinstance(tool_name, str) and tool_name.startswith("mcp__") else []
    if (not isinstance(tool_name, str) or not tool_name.startswith("mcp__") or not isinstance(args, dict)
            or len(names) != 2 or not names[0] or not names[1]
            or payload.get("host") not in ("claude", "codex")
            or not isinstance(payload.get("cwd"), str)
            or (payload.get("project_dir") is not None and not isinstance(payload.get("project_dir"), str))):
        raise ValueError("judge input requires an MCP tool, object args, host, cwd, and project_dir")
    task = payload.get("task")
    if not isinstance(task, dict) or not isinstance(task.get("permissions"), list) \
            or any(not isinstance(p, str) for p in task["permissions"]):
        raise ValueError("task.permissions must be a list")
    project_dir = payload.get("project_dir")
    entries, unknown_mode = catalog.load(project_dir, env)
    call = match(tool_name, entries)
    if call is None:
        server, _, op = tool_name[5:].partition("__")
        mode = unknown_mode
        allowed = mode != "block"
        reason = ("unknown MCP server; warning policy allows it" if mode == "warn" else
                  "unknown MCP server; pass policy allows it" if allowed else
                  "unknown MCP server is blocked by policy")
        return _reason(allowed, reason, UNKNOWN)
    call.args = args
    classification, target = _classify(call)
    if classification == READ:
        return _reason(True, "read operation is allowed", classification)
    if classification == UNKNOWN:
        return _reason(False, "operation is not listed in the MCP catalog", classification)
    permissions = set(task["permissions"])
    required = NEEDS[classification]
    if required not in permissions:
        return _reason(False, f"operation requires the {required} permission", classification)
    if target is NO_TARGET:
        return _reason(True, "permission and target policy allow the operation", classification)
    allowed_targets = [str(x) for x in (entries.get(call.service) or {}).get("targets") or []]
    if "*" in allowed_targets:
        return _reason(True, "permission and target policy allow the operation", classification)
    if call.service == "posthog":
        pinned = pinned_project(call.server, str(payload["host"]), env, project_dir)
        if pinned is not None and target is not None and str(target) != str(pinned):
            return _reason(False, "operation target disagrees with the host connection pin", classification)
        if target is None:
            if pinned is not None and str(pinned) in allowed_targets:
                return _reason(True, "permission and target policy allow the pinned project", classification)
            return _reason(False, "operation acts on the active project, which is not pinned to an allowed target", classification)
    if target is None:
        return _reason(False, "operation has no target; policy must allow all targets", classification)
    if str(target) not in allowed_targets:
        return _reason(False, "operation target is not listed in project MCP policy", classification)
    return _reason(True, "permission and target policy allow the operation", classification)
