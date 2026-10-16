"""Passive inventory of MCP entries in Claude Code and Codex configuration."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Mapping

import tomlkit


def roots(environ: Mapping[str, str] = os.environ) -> dict[str, Path]:
    home = Path(environ.get("HOME") or Path.home()).expanduser().absolute()
    claude_dir = Path(environ.get("CLAUDE_CONFIG_DIR") or home / ".claude").expanduser().absolute()
    codex_dir = Path(environ.get("CODEX_HOME") or home / ".codex").expanduser().absolute()
    claude_registry = (claude_dir / ".claude.json" if environ.get("CLAUDE_CONFIG_DIR")
                       else home / ".claude.json")
    return {"claude_dir": claude_dir, "codex_dir": codex_dir,
            "claude_registry": claude_registry}


def read_document(path: str | os.PathLike[str]) -> dict[str, Any]:
    """Read JSON or TOML, preserving tomlkit's document for formatting-safe edits."""
    file = Path(path)
    if not file.exists():
        return {}
    try:
        text = file.read_text(encoding="utf-8")
        value = tomlkit.parse(text) if file.suffix.lower() == ".toml" else json.loads(text)
        if not isinstance(value, dict):
            raise ValueError
        return value
    except (OSError, UnicodeError, ValueError) as error:
        raise ValueError("configuration document is unreadable or malformed") from error


def _safe_plugin_path(path: Path, root: Path) -> bool:
    if not path.is_absolute() or not root.is_absolute():
        return False
    path = Path(os.path.abspath(path))
    root = Path(os.path.abspath(root))
    try:
        path.relative_to(root)
    except ValueError:
        return False
    try:
        for candidate in (root, path):
            current = Path(candidate.anchor)
            for part in candidate.parts[1:]:
                current = current / part
                if current.is_symlink():
                    return False
        return True
    except (OSError, ValueError):
        return False


def _names(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    return sorted({item for item in value if isinstance(item, str) and item})


def _combine_controls(*values: Mapping[str, list[str]]) -> dict[str, list[str]]:
    merged: dict[str, set[str]] = {}
    for value in values:
        for rule, tools in value.items():
            merged.setdefault(rule, set()).update(tools)
    return {rule: sorted(tools) for rule, tools in merged.items()}


def _policy(entry: Mapping[str, Any], controls: Mapping[str, list[str]] | None = None) -> dict[str, Any]:
    controls = controls or {}
    enabled = set(_names(entry.get("enabled_tools"))) | set(controls.get("allow", []))
    disabled = set(_names(entry.get("disabled_tools"))) | set(controls.get("deny", []))
    mode = entry.get("default_tools_approval_mode", entry.get("approval_mode"))
    tools = entry.get("tools")
    tool_modes = {name: tool.get("approval_mode") for name, tool in tools.items()
                  if isinstance(name, str) and isinstance(tool, dict)
                  and isinstance(tool.get("approval_mode"), str)} if isinstance(tools, dict) else {}
    if controls.get("ask"):
        tool_modes.update({name: "ask" for name in controls["ask"]})
    approval = ({"default": mode if isinstance(mode, str) else None, "tools": dict(sorted(tool_modes.items()))}
                if tool_modes else mode if isinstance(mode, str) else None)
    return {"enabled_tools": sorted(enabled), "disabled_tools": sorted(disabled), "approval_mode": approval}


def _transport(entry: Mapping[str, Any]) -> str:
    kind = str(entry.get("type") or "").lower()
    if kind in ("http", "sse", "stdio"):
        return kind
    if isinstance(entry.get("command"), str) and entry["command"]:
        return "stdio"
    if isinstance(entry.get("url"), str) and entry["url"]:
        return "http"
    return "unknown"


def _row(host: str, scope: str, server: str | None, source: Path, *,
         entry: Mapping[str, Any] | None = None, enabled: bool | None = None,
         status: str = "configured", plugin: str | None = None,
         controls: Mapping[str, list[str]] | None = None) -> dict[str, Any]:
    entry = entry or {}
    return {"host": host, "scope": scope, "server": server, "source": str(source),
            "transport": _transport(entry), "configured_enabled": enabled,
            "policy": _policy(entry, controls), "status": status, "plugin": plugin}


def _unreadable(host: str, scope: str, source: Path, plugin: str | None = None) -> dict[str, Any]:
    return _row(host, scope, None, source, status="unreadable", plugin=plugin)


def _document(path: Path, host: str, scope: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    try:
        return read_document(path), []
    except ValueError:
        return {}, [_unreadable(host, scope, path)]


def _entries(rows: list[dict[str, Any]], host: str, scope: str, source: Path,
             values: Any, *, enabled: bool | None = None, plugin: str | None = None,
             infer_enabled: bool = True, enabled_names: Any = None, disabled: Any = None,
             controls: Mapping[str, Mapping[str, list[str]]] | None = None) -> None:
    if not isinstance(values, dict):
        return
    for name, entry in values.items():
        if isinstance(name, str) and isinstance(entry, dict):
            configured = entry.get("enabled") if infer_enabled and isinstance(entry.get("enabled"), bool) else enabled
            if isinstance(disabled, list) and name in disabled:
                configured = False
            elif isinstance(enabled_names, list) and name in enabled_names:
                configured = True
            per_server = controls.get(name, {}) if controls else {}
            rows.append(_row(host, scope, name, source, entry=entry, enabled=configured,
                             plugin=plugin, controls=per_server))


def _claude_controls(files: list[tuple[Path, str]]) -> tuple[dict[str, dict[str, list[str]]], list[dict[str, Any]]]:
    controls: dict[str, dict[str, list[str]]] = {}
    errors: list[dict[str, Any]] = []
    for path, scope in files:
        settings, unreadable = _document(path, "claude", scope)
        errors.extend(unreadable)
        permissions = settings.get("permissions")
        if not isinstance(permissions, dict):
            continue
        for rule in ("allow", "deny", "ask"):
            for pattern in _names(permissions.get(rule)):
                if not pattern.startswith("mcp__") or "*" in pattern or "?" in pattern:
                    continue
                # Try every separator so server names containing underscores remain intact.
                parts = pattern.split("__")
                if len(parts) < 3:
                    continue
                server, tool = "__".join(parts[1:-1]), parts[-1]
                if not server or not tool:
                    continue
                values = controls.setdefault(server, {}).setdefault(rule, [])
                if tool not in values:
                    values.append(tool)
    return controls, errors


def _claude_plugins(rows: list[dict[str, Any]], base: Path) -> None:
    registry = base / "plugins" / "installed_plugins.json"
    data, errors = _document(registry, "claude", "plugin")
    rows.extend(errors)
    plugins = data.get("plugins")
    if not isinstance(plugins, dict):
        return
    install_root = base / "plugins"
    for plugin_id, installs in plugins.items():
        if not isinstance(plugin_id, str):
            continue
        for install in installs if isinstance(installs, list) else [installs]:
            install_path = Path(str(install.get("installPath") or "")).expanduser() if isinstance(install, dict) else Path("")
            if not str(install_path) or not _safe_plugin_path(install_path, install_root):
                rows.append(_unreadable("claude", "plugin", registry, plugin_id))
                continue
            manifest = install_path / ".mcp.json"
            if not _safe_plugin_path(manifest, install_root):
                rows.append(_unreadable("claude", "plugin", registry, plugin_id))
                continue
            if not manifest.exists():
                continue
            data, errors = _document(manifest, "claude", "plugin")
            if errors:
                rows.extend(_unreadable("claude", "plugin", manifest, plugin_id) for _ in errors)
                continue
            servers = data.get("mcpServers")
            _entries(rows, "claude", "plugin", manifest, servers, plugin=plugin_id, infer_enabled=False)


def _codex_plugins(rows: list[dict[str, Any]], codex_dir: Path) -> None:
    cache = codex_dir / "plugins" / "cache"
    if not cache.is_dir() or cache.is_symlink():
        return
    for manifest in sorted(cache.glob("*/*/*/.mcp.json")):
        if not _safe_plugin_path(manifest, cache):
            continue
        data, errors = _document(manifest, "codex", "plugin-cache")
        rows.extend(errors)
        _entries(rows, "codex", "plugin-cache", manifest,
                 data.get("mcp_servers") if isinstance(data.get("mcp_servers"), dict) else data.get("mcpServers"),
                 plugin=manifest.parents[1].name, infer_enabled=False)


def inventory(project: str | None = None, environ: Mapping[str, str] = os.environ) -> list[dict[str, Any]]:
    """List configured sources and provenance without revealing launch credentials."""
    paths = roots(environ)
    rows: list[dict[str, Any]] = []

    claude, errors = _document(paths["claude_registry"], "claude", "user")
    rows.extend(errors)
    user_control, control_errors = _claude_controls([
        (paths["claude_dir"] / "settings.json", "user-settings"),
        (paths["claude_dir"] / "settings.local.json", "user-settings"),
    ])
    rows.extend(control_errors)
    _entries(rows, "claude", "user", paths["claude_registry"], claude.get("mcpServers"),
             enabled_names=claude.get("enabledMcpServers"),
             disabled=claude.get("disabledMcpServers"), controls=user_control)
    if project:
        project_path = Path(project).expanduser()
        project_key = str(project_path)
        try:
            project_key = str(project_path.resolve())
        except OSError:
            pass
        projects = claude.get("projects")
        project_data = projects.get(project_key) if isinstance(projects, dict) else None
        if project_data is None and isinstance(projects, dict):
            project_data = projects.get(str(project_path))
        project_data = project_data if isinstance(project_data, dict) else {}
        enabled_list = project_data.get("enabledMcpjsonServers")
        disabled_list = project_data.get("disabledMcpjsonServers")
        shared_enabled = project_data.get("enableAllProjectMcpServers") is True
        project_control, control_errors = _claude_controls([
            (project_path / ".claude" / "settings.json", "project-settings"),
            (project_path / ".claude" / "settings.local.json", "project-settings"),
        ])
        rows.extend(control_errors)
        _entries(rows, "claude", "project-registry", paths["claude_registry"],
                 project_data.get("mcpServers"), enabled_names=project_data.get("enabledMcpServers"),
                 disabled=project_data.get("disabledMcpServers"),
                 controls=project_control)
        project_manifest = project_path / ".mcp.json"
        manifest, errors = _document(project_manifest, "claude", "project")
        rows.extend(errors)
        servers = manifest.get("mcpServers")
        if isinstance(servers, dict):
            for name, entry in servers.items():
                enabled = (False if isinstance(disabled_list, list) and name in disabled_list else
                           False if isinstance(project_data.get("disabledMcpServers"), list) and name in project_data["disabledMcpServers"] else
                           False if isinstance(claude.get("disabledMcpServers"), list) and name in claude["disabledMcpServers"] else
                           True if shared_enabled or isinstance(enabled_list, list) and name in enabled_list else None)
                if isinstance(name, str) and isinstance(entry, dict):
                    if enabled is None and isinstance(entry.get("enabled"), bool):
                        enabled = entry["enabled"]
                    rows.append(_row("claude", "project", name, project_manifest,
                                     entry=entry, enabled=enabled,
                                     controls=_combine_controls(user_control.get(name, {}), project_control.get(name, {}))))

    _claude_plugins(rows, paths["claude_dir"])

    codex_user = paths["codex_dir"] / "config.toml"
    codex, errors = _document(codex_user, "codex", "user")
    rows.extend(errors)
    _entries(rows, "codex", "user", codex_user, codex.get("mcp_servers"))
    if project:
        project_config = Path(project).expanduser() / ".codex" / "config.toml"
        project_doc, errors = _document(project_config, "codex", "project")
        rows.extend(errors)
        _entries(rows, "codex", "project", project_config, project_doc.get("mcp_servers"))
        project_plugins = project_doc.get("plugins")
        if isinstance(project_plugins, dict):
            for plugin_id, plugin in project_plugins.items():
                if isinstance(plugin_id, str) and isinstance(plugin, dict):
                    _entries(rows, "codex", "plugin", project_config, plugin.get("mcp_servers"), plugin=plugin_id)
    plugins = codex.get("plugins")
    if isinstance(plugins, dict):
        for plugin_id, plugin in plugins.items():
            if isinstance(plugin_id, str) and isinstance(plugin, dict):
                _entries(rows, "codex", "plugin", codex_user, plugin.get("mcp_servers"), plugin=plugin_id)
    _codex_plugins(rows, paths["codex_dir"])
    rows.sort(key=lambda row: (row["host"], row["scope"], row["server"] or "", row["source"], row["plugin"] or ""))
    return rows
