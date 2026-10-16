"""Strict layered catalog for MCP operation risk classes."""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

OVERRIDE = ("targets", "target_arg", "note")
CLASS_KEYS = ("read", "write", "paid", "publish", "store", "unclassified")
UNKNOWN_MODES = ("pass", "warn", "block")
SERVICE_RE = re.compile(r"^[a-z0-9_-]+$")
USER_FILE = "mcp_catalog.json"


def _read(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as e:
        raise ValueError(f"invalid MCP catalog file {path.name}") from e
    if not isinstance(value, dict):
        raise ValueError(f"invalid MCP catalog file {path.name}: expected an object")
    return value


def validate_layer(data: dict[str, Any], project: bool = False) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise ValueError("MCP catalog must be an object")
    if project:
        if set(data) - {"mcp", "unknown"}:
            raise ValueError("project MCP config: unknown top-level field")
        layer = data.get("mcp", {})
        if not isinstance(layer, dict):
            raise ValueError("project MCP config: mcp must be an object")
    else:
        layer = {k: v for k, v in data.items() if k != "unknown"}
    where = "project config" if project else "MCP catalog"
    if any(not isinstance(k, str) or not isinstance(v, str) for k, v in data.items() if str(k).startswith("_")):
        raise ValueError(f"{where}: metadata fields must be strings")
    unknown = data.get("unknown", "pass")
    if unknown not in UNKNOWN_MODES:
        raise ValueError(f"{where}: unknown must be pass, warn, or block")
    services = {k: v for k, v in layer.items() if k != "unknown" and not str(k).startswith("_")}
    for service, entry in services.items():
        if not isinstance(service, str) or not SERVICE_RE.fullmatch(service) or not isinstance(entry, dict):
            raise ValueError(f"{where}: each service entry must be an object")
        allowed = set(CLASS_KEYS) | set(OVERRIDE) | {"servers", "verbs_read", "paid_segments", "read_segments"}
        extra = set(entry) - allowed
        if extra:
            raise ValueError(f"{where}: unknown catalog field")
        for key, value in entry.items():
            if key in CLASS_KEYS or key in ("servers", "verbs_read", "paid_segments", "read_segments", "targets"):
                if not isinstance(value, list) or any(not isinstance(x, str) for x in value):
                    raise ValueError(f"{where}: {service}.{key} must be a list")
            if key == "target_arg" and value is not None and not isinstance(value, str):
                raise ValueError(f"{where}: {service}.target_arg must be a string")
            if key == "note" and not isinstance(value, str):
                raise ValueError(f"{where}: {service}.note must be a string")
    return services


def merge(*layers: dict[str, Any]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for layer in layers:
        for service, entry in layer.items():
            into = out.setdefault(service, {})
            for key, value in entry.items():
                if key in OVERRIDE:
                    into[key] = value
                elif isinstance(value, list):
                    have = into.setdefault(key, [])
                    have.extend(x for x in value if x not in have)
                elif key not in into:
                    into[key] = value
    return out


def _plugin() -> dict[str, Any]:
    path = Path(__file__).with_name("mcp_catalog.json")
    if not path.is_file():
        raise ValueError("shipped MCP catalog is missing")
    entries = validate_layer(_read(path))
    if not entries:
        raise ValueError("shipped MCP catalog has no service entries")
    return entries


DEFAULT = _plugin()
SHIPPED = frozenset(DEFAULT)


def user_path(environ: dict[str, str] = os.environ) -> Path:
    return Path(environ.get("MCPCLERK_HOME") or (Path(environ.get("HOME") or Path.home()) / ".mcpclerk")) / USER_FILE


def load(project_dir: str | None = None, environ: dict[str, str] = os.environ) -> tuple[dict[str, dict[str, Any]], str]:
    user_raw = _read(user_path(environ))
    user = validate_layer(user_raw)
    project_services: dict[str, Any] = {}
    project_mode = None
    if project_dir:
        project_file = Path(project_dir) / "mcpclerk.json"
        raw = _read(project_file)
        project_services = validate_layer(raw, project=True)
        project_mode = raw.get("unknown")
    mode = project_mode or user_raw.get("unknown", "pass")
    if mode not in UNKNOWN_MODES:
        raise ValueError("MCP unknown mode must be pass, warn, or block")
    return merge(DEFAULT, user, project_services), mode
