"""Explicit native host configuration changes; each write has a reversible receipt."""
import json
import os
from pathlib import Path
from urllib.parse import urlsplit

import tomlkit

from . import catalog, hosts, storage


def project_path(project):
    if not project or not Path(project).is_absolute() or not Path(project).is_dir():
        raise ValueError("an existing absolute --project directory is required")
    return Path(project).resolve()


def object_at(data, *keys):
    for key in keys:
        if key not in data:
            data[key] = {}
        data = data[key]
        if not isinstance(data, dict):
            raise ValueError("configuration section must be an object")
    return data


def strings_at(data, key):
    value = data.get(key, [])
    if not isinstance(value, list) or any(not isinstance(x, str) for x in value):
        raise ValueError("configuration permission list must contain strings")
    return list(value)


def parse(raw, path):
    if raw is None:
        return tomlkit.document() if path.suffix == ".toml" else {}
    try:
        value = tomlkit.parse(raw.decode()) if path.suffix == ".toml" else json.loads(raw)
    except (ValueError, UnicodeError):
        raise ValueError("configuration file is malformed; it was not changed") from None
    if not isinstance(value, dict):
        raise ValueError("configuration file must contain an object")
    return value


def save(path, original, data, environ):
    rendered = tomlkit.dumps(data) if path.suffix == ".toml" else json.dumps(data, indent=2) + "\n"
    return storage.write(path, original, rendered.encode(), environ)


def location(host, scope, project, environ, permissions=False):
    roots = hosts.roots(environ)
    if host == "codex":
        if scope == "local":
            raise ValueError("Codex supports user or project scope")
        return roots["codex_dir"] / "config.toml" if scope == "user" else project_path(project) / ".codex/config.toml"
    if permissions:
        if scope == "user":
            return roots["claude_dir"] / "settings.json"
        return project_path(project) / ".claude" / ("settings.local.json" if scope == "local" else "settings.json")
    return project_path(project) / ".mcp.json" if scope == "project" else roots["claude_registry"]


def server_map(data, host, scope, project, plugin=None):
    if host == "codex":
        return object_at(data, "plugins", plugin, "mcp_servers") if plugin else object_at(data, "mcp_servers")
    if plugin:
        raise ValueError("Claude plugin controls use the host server name, without --plugin")
    if scope == "local":
        return object_at(data, "projects", str(project_path(project)), "mcpServers")
    return object_at(data, "mcpServers")


def validate_entry(entry, host):
    if not isinstance(entry, dict) or bool(entry.get("command")) == bool(entry.get("url")):
        raise ValueError("server needs exactly one nonempty command or URL")
    if entry.get("command") and not isinstance(entry["command"], str):
        raise ValueError("server command must be a string")
    if entry.get("url"):
        try:
            url = urlsplit(entry["url"])
            valid = url.scheme in ("http", "https") and bool(url.hostname)
        except (ValueError, TypeError, AttributeError):
            valid = False
        if not valid:
            raise ValueError("server URL must be an HTTP or HTTPS endpoint")
    if "args" in entry:
        strings_at(entry, "args")
    for field in ("env", "headers" if host == "claude" else "http_headers", "env_http_headers"):
        if field in entry and (not isinstance(entry[field], dict) or
                              any(not isinstance(v, str) for v in entry[field].values())):
            raise ValueError("server environment and header values must be strings")
    if "enabled" in entry and type(entry["enabled"]) is not bool:
        raise ValueError("server enabled flag must be boolean")
    if host == "claude" and entry.get("type", "stdio" if entry.get("command") else "http") not in ("stdio", "http", "sse"):
        raise ValueError("unsupported Claude server transport")


def change(action, name, host, scope, project=None, entry=None, tool=None, mode=None,
           plugin=None, environ=os.environ):
    if host not in ("claude", "codex") or scope not in ("user", "project", "local"):
        raise ValueError("unsupported host or scope")
    if not isinstance(name, str) or not name or any(c in name for c in "\r\n\x00"):
        raise ValueError("a server name is required")
    if action not in ("add", "remove", "enable", "disable", "tool"):
        raise ValueError("unsupported configuration action")
    if action == "tool" and (not isinstance(tool, str) or not tool or any(c in tool for c in "\r\n\x00")
                             or mode not in ("allow", "ask", "deny", "clear")):
        raise ValueError("tool and allow/ask/deny/clear mode are required")
    if plugin and action in ("add", "remove"):
        raise ValueError("plugin server definitions belong to the plugin; use enable/disable or tool policy")
    if host == "claude" and action in ("enable", "disable"):
        if scope != "project" or plugin:
            raise ValueError("Claude server toggles require project scope and the host server name")
        project = str(project_path(project))
        shared_path = Path(project) / ".mcp.json"
        shared = parse(storage.read(shared_path), shared_path)
        if name in object_at(shared, "mcpServers"):
            path = location(host, "local", project, environ, permissions=True)
            original = storage.read(path)
            data = parse(original, path)
            for key in ("enabledMcpjsonServers", "disabledMcpjsonServers"):
                data[key] = [x for x in strings_at(data, key) if x != name]
            data["enabledMcpjsonServers" if action == "enable" else "disabledMcpjsonServers"].append(name)
        else:
            path = hosts.roots(environ)["claude_registry"]
            original = storage.read(path)
            data = parse(original, path)
            local = object_at(data, "projects", project)
            values = [x for x in strings_at(local, "disabledMcpServers") if x != name]
            local["disabledMcpServers"] = values + ([name] if action == "disable" else [])
        return save(path, original, data, environ)
    path = location(host, scope, project, environ, permissions=(action == "tool"))
    original = storage.read(path)
    data = parse(original, path)
    if host == "claude" and action == "tool":
        if plugin:
            raise ValueError("use the Claude host server name without --plugin")
        rule = f"mcp__{name}__{tool}"
        permissions = object_at(data, "permissions")
        for key in ("allow", "ask", "deny"):
            permissions[key] = [x for x in strings_at(permissions, key) if x != rule]
        if mode != "clear":
            permissions[mode].append(rule)
    else:
        servers = server_map(data, host, scope, project, plugin)
        if action == "add":
            if name in servers:
                raise ValueError("server already exists in this scope; remove it explicitly before replacing")
            validate_entry(entry, host)
            servers[name] = entry
        elif action == "remove":
            if name not in servers:
                raise ValueError("server is absent from the selected scope")
            del servers[name]
        else:
            target = object_at(servers, name)
            if action in ("enable", "disable"):
                target["enabled"] = action == "enable"
            else:
                denied = [x for x in strings_at(target, "disabled_tools") if x != tool]
                target["disabled_tools"] = denied + ([tool] if mode == "deny" else [])
                if "enabled_tools" in target:
                    enabled = [x for x in strings_at(target, "enabled_tools") if x != tool]
                    target["enabled_tools"] = enabled + ([tool] if mode in ("allow", "ask") else [])
                tools = object_at(target, "tools")
                policy = object_at(tools, tool)
                policy.pop("approval_mode", None)
                if mode in ("allow", "ask"):
                    policy["approval_mode"] = "auto" if mode == "allow" else "prompt"
                if not policy:
                    tools.pop(tool, None)
    return save(path, original, data, environ)


def set_catalog(data, scope, project=None, environ=os.environ):
    if scope not in ("user", "project"):
        raise ValueError("catalog scope must be user or project")
    catalog.validate_layer(data, project=(scope == "project"))
    home = Path(environ.get("MCPCLERK_HOME") or str(Path(environ.get("HOME", str(Path.home()))) / ".mcpclerk"))
    path = home / "mcp_catalog.json" if scope == "user" else project_path(project) / "mcpclerk.json"
    return save(path, storage.read(path), data, environ)


def migrate_keel(data, scope, project=None, environ=os.environ):
    if not isinstance(data, dict):
        raise ValueError("legacy configuration must contain an object")
    if scope == "project":
        converted = {"mcp": data.get("mcp", {})}
        guards = data.get("guards", {})
        if not isinstance(guards, dict):
            raise ValueError("legacy guards must contain an object")
        if "mcp_unknown" in guards:
            converted["unknown"] = guards["mcp_unknown"]
    else:
        converted = {k: v for k, v in data.items() if not k.startswith("_")}
    return set_catalog(converted, scope, project, environ)
