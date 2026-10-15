"""Resolve the PostHog project pin from the active host's own MCP configuration."""
from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import tomlkit

HEADER = "x-posthog-project-id"
PLUGIN_RE = re.compile(r"^plugin_([^_]+)_(.+)$")
_UNREADABLE = object()


def _json(path: Path):
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return _UNREADABLE
    return value if isinstance(value, dict) else _UNREADABLE


def _expand(value, env):
    pattern = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")
    value = str(value or "")
    if any(env.get(m.group(1)) is None for m in pattern.finditer(value)):
        return ""
    return pattern.sub(lambda m: env[m.group(1)], value)


def _one(values):
    values = set(values)
    return values.pop() if len(values) == 1 and all(values) else None


def _pin_of(url, headers, env):
    url = _expand(url, env)
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    host = (parts.hostname or "").lower()
    if host != "posthog.com" and not host.endswith(".posthog.com"):
        return None
    items = headers.items() if isinstance(headers, dict) else (headers or [])
    pins = [_expand(v, env).strip() for k, v in items if str(k).lower() == HEADER]
    pins += [v.strip() for v in parse_qs(parts.query, keep_blank_values=True).get("project_id", [])]
    return _one(pins)


def _claude_home(env):
    return Path(env.get("CLAUDE_CONFIG_DIR") or (Path(env.get("HOME") or Path.home()) / ".claude"))


def _claude_settings(env):
    config = _claude_home(env)
    path = config / ".claude.json" if env.get("CLAUDE_CONFIG_DIR") else config.parent / ".claude.json"
    return _json(path)


def _trusted_claude_project(settings, project_dir):
    if not project_dir or settings in (None, _UNREADABLE):
        return False
    projects = settings.get("projects") or {}
    project = projects.get(project_dir) or projects.get(os.path.realpath(project_dir)) or {}
    return project.get("hasTrustDialogAccepted") is True


def _tracked(project_dir, relative):
    try:
        return subprocess.run(["git", "-C", project_dir, "ls-files", "--error-unmatch", "--", relative],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False).returncode == 0
    except OSError:
        return True


def _plugin_pin(server, plugin, env):
    registry = _json(_claude_home(env) / "plugins" / "installed_plugins.json")
    if registry in (None, _UNREADABLE):
        return None
    pins, seen = [], False
    for plugin_id, installs in (registry.get("plugins") or {}).items():
        if str(plugin_id).split("@")[0] != plugin:
            continue
        for install in installs if isinstance(installs, list) else [installs]:
            path = Path(str((install or {}).get("installPath") or "")) / ".mcp.json"
            manifest = _json(path)
            if manifest is _UNREADABLE:
                return None
            entry = ((manifest or {}).get("mcpServers") or {}).get(server)
            if isinstance(entry, dict):
                seen = True
                pins.append(_pin_of(entry.get("url"), entry.get("headers") or {}, env) or "")
    return _one(pins) if seen and all(pins) else None


def _claude_pin(server, env):
    if _claude_disabled(server, env):
        return None
    plugin = PLUGIN_RE.match(server)
    if plugin:
        return _plugin_pin(plugin.group(2), plugin.group(1), env)
    settings = _claude_settings(env)
    if settings is _UNREADABLE:
        return None
    settings = settings or {}
    project_dir = env.get("CLAUDE_PROJECT_DIR")
    scopes = []
    if project_dir:
        projects = settings.get("projects") or {}
        project = projects.get(project_dir) or projects.get(os.path.realpath(project_dir)) or {}
        scopes.append(project.get("mcpServers") or {})
        shared = _json(Path(project_dir) / ".mcp.json")
        if shared is _UNREADABLE:
            return None
        user_settings = _json(_claude_home(env) / "settings.json")
        if user_settings is _UNREADABLE:
            return None
        managed_settings = _json(_claude_home(env) / "managed-settings.json")
        if managed_settings is _UNREADABLE:
            return None
        project_settings_paths = [Path(project_dir) / ".claude" / "settings.json",
                                  Path(project_dir) / ".claude" / "settings.local.json"]
        project_settings = [_json(p) for p in project_settings_paths]
        if any(item is _UNREADABLE for item in project_settings):
            return None
        approved = (project.get("enableAllProjectMcpServers") is True
                    or server in (project.get("enabledMcpjsonServers") or []))
        if isinstance(user_settings, dict):
            approved |= user_settings.get("enableAllProjectMcpServers") is True \
                or server in (user_settings.get("enabledMcpjsonServers") or [])
        if isinstance(managed_settings, dict):
            approved |= managed_settings.get("enableAllProjectMcpServers") is True \
                or server in (managed_settings.get("enabledMcpjsonServers") or [])
        trusted = _trusted_claude_project(settings, project_dir)
        shared_settings, local_settings = project_settings
        if trusted and isinstance(shared_settings, dict):
            approved |= shared_settings.get("enableAllProjectMcpServers") is True \
                or server in (shared_settings.get("enabledMcpjsonServers") or [])
        if trusted and isinstance(local_settings, dict) and not _tracked(project_dir, ".claude/settings.local.json"):
            approved |= local_settings.get("enableAllProjectMcpServers") is True \
                or server in (local_settings.get("enabledMcpjsonServers") or [])
        if approved and shared:
            scopes.append(shared.get("mcpServers") or {})
    scopes.append(settings.get("mcpServers") or {})
    for scope in scopes:
        entry = scope.get(server)
        if isinstance(entry, dict):
            return _pin_of(entry.get("url"), entry.get("headers") or {}, env)
    return None


def _claude_disabled(server, env):
    settings = _claude_settings(env)
    if settings is _UNREADABLE:
        return True
    project_dir = env.get("CLAUDE_PROJECT_DIR")
    project = {}
    if project_dir:
        projects = (settings or {}).get("projects") or {}
        project = projects.get(project_dir) or projects.get(os.path.realpath(project_dir)) or {}
    candidates = [project, _json(_claude_home(env) / "settings.json"),
                  _json(_claude_home(env) / "managed-settings.json")]
    if project_dir:
        candidates.extend((_json(Path(project_dir) / ".claude" / "settings.json"),
                           _json(Path(project_dir) / ".claude" / "settings.local.json")))
    for policy in candidates:
        if policy is _UNREADABLE:
            return True
        if not isinstance(policy, dict):
            continue
        if server in (policy.get("disabledMcpjsonServers") or []) \
                or server in (policy.get("disabledMcpServers") or []):
            return True
    return False


def _codex_pin(server, env, project_dir=None):
    codex_home = Path(env.get("CODEX_HOME") or (Path(env.get("HOME") or Path.home()) / ".codex"))
    path = codex_home / "config.toml"
    if not path.exists():
        return None
    try:
        data = tomlkit.parse(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    entry = (data.get("mcp_servers") or {}).get(server) or {}
    if not isinstance(entry, dict):
        return None
    project_entry = {}
    if project_dir:
        projects = data.get("projects") or {}
        trust = projects.get(project_dir) or projects.get(os.path.realpath(project_dir)) or {}
        project_path = Path(project_dir) / ".codex" / "config.toml"
        if trust.get("trust_level") == "trusted" and project_path.exists():
            try:
                project_data = tomlkit.parse(project_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return None
            project_entry = (project_data.get("mcp_servers") or {}).get(server) or {}
            if not isinstance(project_entry, dict):
                return None
    if not entry and not project_entry:
        return None
    # Project config overrides scalar fields. Header maps can layer, so conflicting pins fail closed.
    url = project_entry.get("url", entry.get("url"))
    headers = []
    for layer in (entry, project_entry):
        headers.extend((k, v) for k, v in (layer.get("http_headers") or {}).items())
        headers.extend((k, env.get(str(v)) or "") for k, v in (layer.get("env_http_headers") or {}).items())
    return _pin_of(url, headers, env)


def pinned_project(server: str, host: str, environ: dict[str, str] = os.environ,
                   project_dir: str | None = None) -> str | None:
    """Return the pinned project, or None if absent, ambiguous, or malformed."""
    if host == "codex":
        return _codex_pin(server, environ, project_dir)
    if project_dir:
        environ = {**environ, "CLAUDE_PROJECT_DIR": project_dir}
    return _claude_pin(server, environ)
