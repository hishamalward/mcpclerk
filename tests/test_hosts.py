from __future__ import annotations

import json
from pathlib import Path

import pytest
import tomlkit

from mcpclerk.hosts import inventory, read_document, roots


@pytest.fixture
def env(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    return {"HOME": str(home), "CLAUDE_CONFIG_DIR": str(home / ".claude"),
            "CODEX_HOME": str(home / ".codex")}


def test_roots_and_document_readers_keep_native_locations_and_toml_document(tmp_path):
    home = tmp_path / "home"
    custom = tmp_path / "claude-custom"
    values = roots({"HOME": str(home), "CLAUDE_CONFIG_DIR": str(custom), "CODEX_HOME": str(tmp_path / "codex")})
    assert values == {"claude_dir": custom, "codex_dir": tmp_path / "codex",
                      "claude_registry": custom / ".claude.json"}
    assert roots({"HOME": str(home)})["claude_registry"] == home / ".claude.json"

    toml_path = tmp_path / "config.toml"
    toml_path.write_text("# preserve this comment\n[mcp_servers.demo]\ncommand='run'\n")
    document = read_document(toml_path)
    assert isinstance(document, tomlkit.toml_document.TOMLDocument)
    assert "preserve this comment" in tomlkit.dumps(document)
    assert read_document(tmp_path / "missing.json") == {}


def test_inventory_lists_claude_scopes_codex_policy_and_redacts_config_values(tmp_path, env):
    claude_dir = Path(env["CLAUDE_CONFIG_DIR"])
    claude_dir.mkdir()
    (claude_dir / ".claude.json").write_text(json.dumps({
        "mcpServers": {"user": {"command": "/private/path", "args": ["secret-arg"]}},
        "disabledMcpServers": ["user"],
        "projects": {str(tmp_path): {"mcpServers": {"registry": {"url": "https://user:pass@example.test"}},
                                     "enabledMcpjsonServers": ["project"],
                                     "disabledMcpjsonServers": ["off"]}},
    }))
    (tmp_path / ".mcp.json").write_text(json.dumps({"mcpServers": {
        "project": {"type": "sse", "url": "https://private.example", "env": {"TOKEN": "do-not-show"}},
        "off": {"command": "private-command", "env": {"TOKEN": "still-secret"}},
    }}))
    (claude_dir / "settings.local.json").write_text(json.dumps({"permissions": {
        "allow": ["mcp__user__read"],
        "deny": ["mcp__user__delete"], "ask": ["mcp__user__prompt"],
    }}))
    project_settings = tmp_path / ".claude"
    project_settings.mkdir()
    (project_settings / "settings.local.json").write_text(json.dumps({"permissions": {
        "allow": ["mcp__project__write"],
    }}))

    codex_dir = Path(env["CODEX_HOME"])
    codex_dir.mkdir()
    (codex_dir / "config.toml").write_text("""[mcp_servers.codex]
command = "hidden-command"
args = ["hidden-arg"]
enabled = false
enabled_tools = ["read", "list"]
disabled_tools = ["delete"]
approval_mode = "ask"

[plugins.analytics.mcp_servers.extra]
url = "https://hidden.example"
enabled_tools = ["plugin-read"]
default_tools_approval_mode = "prompt"

[plugins.analytics.mcp_servers.extra.tools.inspect]
approval_mode = "deny"
""")
    (tmp_path / ".codex").mkdir()
    (tmp_path / ".codex" / "config.toml").write_text('''[mcp_servers.project_codex]
url="https://hidden.example"

[plugins.project-plugin.mcp_servers.local]
enabled_tools=["project-tool"]
''')
    cache_manifest = codex_dir / "plugins/cache/store/plugin-a/1.0/.mcp.json"
    cache_manifest.parent.mkdir(parents=True)
    cache_manifest.write_text(json.dumps({"mcpServers": {"cached": {"command": "cache-command", "enabled": True}}}))

    rows = inventory(str(tmp_path), env)
    indexed = {(r["host"], r["scope"], r["server"]): r for r in rows if r["server"]}
    assert indexed[("claude", "user", "user")]["transport"] == "stdio"
    assert indexed[("claude", "user", "user")]["configured_enabled"] is False
    assert indexed[("claude", "user", "user")]["policy"] == {
        "enabled_tools": ["read"], "disabled_tools": ["delete"],
        "approval_mode": {"default": None, "tools": {"prompt": "ask"}}}
    assert indexed[("claude", "project-registry", "registry")]["configured_enabled"] is None
    assert indexed[("claude", "project", "project")]["transport"] == "sse"
    assert indexed[("claude", "project", "project")]["configured_enabled"] is True
    assert indexed[("claude", "project", "project")]["policy"]["enabled_tools"] == ["write"]
    assert indexed[("claude", "project", "off")]["configured_enabled"] is False
    assert indexed[("codex", "user", "codex")]["configured_enabled"] is False
    assert indexed[("codex", "user", "codex")]["policy"] == {
        "enabled_tools": ["list", "read"], "disabled_tools": ["delete"], "approval_mode": "ask"}
    assert indexed[("codex", "plugin", "extra")]["plugin"] == "analytics"
    assert indexed[("codex", "plugin", "extra")]["policy"] == {
        "enabled_tools": ["plugin-read"], "disabled_tools": [],
        "approval_mode": {"default": "prompt", "tools": {"inspect": "deny"}}}
    assert indexed[("codex", "plugin", "local")]["plugin"] == "project-plugin"
    assert indexed[("codex", "plugin-cache", "cached")]["configured_enabled"] is None
    serialized = json.dumps(rows)
    for secret in ("private/path", "secret-arg", "user:pass", "do-not-show", "still-secret",
                   "hidden-command", "hidden-arg", "hidden.example", "cache-command"):
        assert secret not in serialized


def test_unreadable_config_is_reported_with_source_without_stopping_inventory(tmp_path, env):
    codex_dir = Path(env["CODEX_HOME"])
    codex_dir.mkdir()
    bad = codex_dir / "config.toml"
    bad.write_text("[mcp_servers.broken\nurl = 'secret'")
    rows = inventory(environ=env)
    row = next(row for row in rows if row["host"] == "codex" and row["scope"] == "user")
    assert row["server"] is None and row["status"] == "unreadable"
    assert row["source"] == str(bad)
    assert "secret" not in json.dumps(row)
    with pytest.raises(ValueError, match="malformed"):
        read_document(bad)


def test_claude_installed_plugin_manifest_and_unsafe_install_path(tmp_path, env):
    claude_dir = Path(env["CLAUDE_CONFIG_DIR"])
    plugin = claude_dir / "plugins/cache/market/plugin/1.0"
    plugin.mkdir(parents=True)
    (plugin / ".mcp.json").write_text(json.dumps({"mcpServers": {"installed": {"type": "http", "url": "https://private"}}}))
    outside = tmp_path / "outside"
    outside.mkdir()
    registry = claude_dir / "plugins/installed_plugins.json"
    registry.write_text(json.dumps({"plugins": {
        "market/plugin": [{"installPath": str(plugin)}],
        "bad/plugin": [{"installPath": str(outside)}],
    }}))
    rows = inventory(environ=env)
    installed = next(row for row in rows if row["server"] == "installed")
    assert installed["scope"] == "plugin" and installed["plugin"] == "market/plugin"
    assert installed["configured_enabled"] is None
    rejected = next(row for row in rows if row["scope"] == "plugin" and row["plugin"] == "bad/plugin")
    assert rejected["status"] == "unreadable" and rejected["server"] is None
    assert str(outside) not in json.dumps(rejected)
