"""Policy file: schema, validation, and per-tool resolution.

Resolution order for a tool under an upstream: exact name, then the longest matching glob,
then ``defaults.unlisted``. Deny by default: an unlisted tool is an unreviewed tool.
"""
from __future__ import annotations

import fnmatch
import hashlib
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

DECISIONS = ("allow", "deny", "approve")
UPSTREAM_NAME_RE = re.compile(r"^[a-z0-9_-]+$")
ENV_REF_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


class PolicyError(ValueError):
    """The policy file is invalid. The message says where and why."""


@dataclass(frozen=True)
class ToolRule:
    decision: str
    per_run: int | None
    per_minute: int | None
    approval_timeout_s: float
    source: str  # "exact", "glob:<pattern>", or "default"


@dataclass
class UpstreamConfig:
    name: str
    transport: str  # "stdio" | "http"
    command: str | None = None
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    cwd: str | None = None
    url: str | None = None
    headers: dict[str, str] = field(default_factory=dict)
    tools: dict[str, dict[str, Any]] = field(default_factory=dict)


@dataclass
class RedactionConfig:
    extend: list[str] = field(default_factory=list)
    extend_keys: list[str] = field(default_factory=list)
    replace_builtin: bool = False


@dataclass
class Policy:
    version: int
    namespace_separator: str
    unlisted: str
    approval_timeout_s: float
    default_per_run: int | None
    default_per_minute: int | None
    redaction: RedactionConfig
    upstreams: dict[str, UpstreamConfig]
    sha256: str
    path: str

    def resolve(self, upstream: str, tool: str) -> ToolRule:
        up = self.upstreams.get(upstream)
        if up is None:
            raise KeyError(upstream)
        entry: dict[str, Any] | None = None
        source = "default"
        if tool in up.tools:
            entry, source = up.tools[tool], "exact"
        else:
            best: str | None = None
            for pattern in up.tools:
                if any(ch in pattern for ch in "*?[") and fnmatch.fnmatchcase(tool, pattern):
                    if best is None or len(pattern) > len(best):
                        best = pattern
            if best is not None:
                entry, source = up.tools[best], f"glob:{best}"
        if entry is None:
            return ToolRule(self.unlisted, self.default_per_run, self.default_per_minute,
                            self.approval_timeout_s, source)
        quota = entry.get("quota") or {}
        return ToolRule(
            decision=entry["decision"],
            per_run=quota.get("per_run", self.default_per_run),
            per_minute=quota.get("per_minute", self.default_per_minute),
            approval_timeout_s=float(entry.get("approval_timeout_s", self.approval_timeout_s)),
            source=source,
        )

    def split(self, namespaced: str) -> tuple[str, str] | None:
        sep = self.namespace_separator
        if sep not in namespaced:
            return None
        upstream, tool = namespaced.split(sep, 1)
        return upstream, tool

    def join(self, upstream: str, tool: str) -> str:
        return f"{upstream}{self.namespace_separator}{tool}"


def _expand_env(value: str, where: str) -> str:
    def sub(m: re.Match[str]) -> str:
        name = m.group(1)
        if name not in os.environ:
            raise PolicyError(f"{where}: environment variable ${{{name}}} is not set")
        return os.environ[name]
    return ENV_REF_RE.sub(sub, value)


def _check_keys(obj: dict[str, Any], allowed: set[str], where: str) -> None:
    unknown = set(obj) - allowed
    if unknown:
        raise PolicyError(f"{where}: unknown key(s) {sorted(unknown)}")


def _quota(obj: Any, where: str) -> dict[str, int | None]:
    if obj is None:
        return {}
    if not isinstance(obj, dict):
        raise PolicyError(f"{where}: quota must be a mapping")
    _check_keys(obj, {"per_run", "per_minute"}, where)
    out: dict[str, int | None] = {}
    for k, v in obj.items():
        if v is not None and (not isinstance(v, int) or isinstance(v, bool) or v < 1):
            raise PolicyError(f"{where}.{k}: must be a positive integer or null")
        out[k] = v
    return out


def _tool_entry(raw: Any, where: str) -> dict[str, Any]:
    if isinstance(raw, str):
        raw = {"decision": raw}
    if not isinstance(raw, dict):
        raise PolicyError(f"{where}: must be 'allow' | 'deny' | 'approve' or a mapping")
    _check_keys(raw, {"decision", "quota", "approval_timeout_s"}, where)
    decision = raw.get("decision")
    if decision not in DECISIONS:
        raise PolicyError(f"{where}: decision must be one of {DECISIONS}, got {decision!r}")
    entry: dict[str, Any] = {"decision": decision, "quota": _quota(raw.get("quota"), where + ".quota")}
    if "approval_timeout_s" in raw:
        t = raw["approval_timeout_s"]
        if not isinstance(t, (int, float)) or isinstance(t, bool) or t <= 0:
            raise PolicyError(f"{where}.approval_timeout_s: must be a positive number")
        entry["approval_timeout_s"] = float(t)
    return entry


def load_policy(path: str | os.PathLike[str]) -> Policy:
    p = Path(path)
    try:
        text = p.read_text(encoding="utf-8")
    except OSError as e:
        raise PolicyError(f"cannot read policy {p}: {e}") from e
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise PolicyError(f"{p}: not valid YAML: {e}") from e
    return parse_policy(raw, sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(), path=str(p))


def parse_policy(raw: Any, *, sha256: str = "", path: str = "<inline>") -> Policy:
    if not isinstance(raw, dict):
        raise PolicyError(f"{path}: top level must be a mapping")
    _check_keys(raw, {"version", "namespace_separator", "defaults", "redaction", "upstreams"}, path)
    if raw.get("version") != 1:
        raise PolicyError(f"{path}: version must be 1")

    sep = raw.get("namespace_separator", ".")
    if not isinstance(sep, str) or not sep or sep.isalnum():
        raise PolicyError(f"{path}: namespace_separator must be a non-alphanumeric string such as '.' or '__'")

    defaults = raw.get("defaults") or {}
    _check_keys(defaults, {"unlisted", "approval_timeout_s", "quota"}, f"{path}: defaults")
    unlisted = defaults.get("unlisted", "deny")
    if unlisted not in DECISIONS:
        raise PolicyError(f"{path}: defaults.unlisted must be one of {DECISIONS}")
    timeout = defaults.get("approval_timeout_s", 120)
    if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or timeout <= 0:
        raise PolicyError(f"{path}: defaults.approval_timeout_s must be a positive number")
    dq = _quota(defaults.get("quota"), f"{path}: defaults.quota")

    red_raw = raw.get("redaction") or {}
    _check_keys(red_raw, {"extend", "extend_keys", "replace_builtin"}, f"{path}: redaction")
    redaction = RedactionConfig(
        extend=list(red_raw.get("extend") or []),
        extend_keys=list(red_raw.get("extend_keys") or []),
        replace_builtin=bool(red_raw.get("replace_builtin", False)),
    )
    for pat in redaction.extend:
        try:
            re.compile(pat)
        except re.error as e:
            raise PolicyError(f"{path}: redaction.extend pattern {pat!r} is not a valid regex: {e}") from e

    ups_raw = raw.get("upstreams")
    if not isinstance(ups_raw, dict) or not ups_raw:
        raise PolicyError(f"{path}: upstreams must be a non-empty mapping")
    upstreams: dict[str, UpstreamConfig] = {}
    for name, u in ups_raw.items():
        where = f"{path}: upstreams.{name}"
        if not isinstance(name, str) or not UPSTREAM_NAME_RE.match(name) or sep in name:
            raise PolicyError(f"{where}: upstream names must match [a-z0-9_-]+ and not contain {sep!r}")
        if not isinstance(u, dict):
            raise PolicyError(f"{where}: must be a mapping")
        _check_keys(u, {"transport", "command", "args", "env", "cwd", "url", "headers", "tools"}, where)
        transport = u.get("transport", "stdio")
        cfg = UpstreamConfig(name=name, transport=transport)
        if transport == "stdio":
            if not u.get("command"):
                raise PolicyError(f"{where}: stdio upstream needs 'command'")
            cfg.command = _expand_env(str(u["command"]), where + ".command")
            cfg.args = [_expand_env(str(a), where + ".args") for a in (u.get("args") or [])]
            cfg.env = {str(k): _expand_env(str(v), f"{where}.env.{k}") for k, v in (u.get("env") or {}).items()}
            cfg.cwd = _expand_env(str(u["cwd"]), where + ".cwd") if u.get("cwd") else None
        elif transport == "http":
            if not u.get("url"):
                raise PolicyError(f"{where}: http upstream needs 'url'")
            cfg.url = _expand_env(str(u["url"]), where + ".url")
            cfg.headers = {str(k): _expand_env(str(v), f"{where}.headers.{k}") for k, v in (u.get("headers") or {}).items()}
        else:
            raise PolicyError(f"{where}: transport must be 'stdio' or 'http'")
        tools_raw = u.get("tools") or {}
        if not isinstance(tools_raw, dict):
            raise PolicyError(f"{where}.tools: must be a mapping")
        cfg.tools = {str(t): _tool_entry(v, f"{where}.tools.{t}") for t, v in tools_raw.items()}
        upstreams[name] = cfg

    return Policy(
        version=1,
        namespace_separator=sep,
        unlisted=unlisted,
        approval_timeout_s=float(timeout),
        default_per_run=dq.get("per_run"),
        default_per_minute=dq.get("per_minute"),
        redaction=redaction,
        upstreams=upstreams,
        sha256=sha256,
        path=path,
    )
