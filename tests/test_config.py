import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import tomlkit

from mcpclerk import config, hosts, storage


@pytest.fixture
def setup(tmp_path):
    home = tmp_path / "home"
    project = tmp_path / "project"
    home.mkdir()
    project.mkdir()
    env = {"HOME": str(home), "MCPCLERK_HOME": str(home / ".mcpclerk")}
    return home, project, env


def test_codex_add_policy_and_undo_preserve_unrelated_toml(setup):
    home, project, env = setup
    path = home / ".codex/config.toml"
    path.parent.mkdir()
    original = b'# Keep this comment\nmodel = "fixture"\n[mcp_servers.existing]\ncommand = "keep"\n'
    path.write_bytes(original)
    path.chmod(0o640)
    transaction = config.change("add", "sample", "codex", "user", entry={"command": "node", "args": ["server.js"], "env": {"TOKEN": "secret"}}, environ=env)
    assert '# Keep this comment' in path.read_text()
    assert tomlkit.parse(path.read_text())["mcp_servers"]["existing"]["command"] == "keep"
    storage.undo(transaction, env)
    assert path.read_bytes() == original
    assert path.stat().st_mode & 0o777 == 0o640


def test_project_plugin_tool_policy_and_scope_removal(setup):
    home, project, env = setup
    config.change("tool", "docs", "codex", "project", str(project), plugin="docs@market", tool="search", mode="deny", environ=env)
    path = project / ".codex/config.toml"
    data = tomlkit.parse(path.read_text())
    assert data["plugins"]["docs@market"]["mcp_servers"]["docs"]["disabled_tools"] == ["search"]
    config.change("tool", "docs", "codex", "project", str(project), plugin="docs@market", tool="search", mode="ask", environ=env)
    entry = tomlkit.parse(path.read_text())["plugins"]["docs@market"]["mcp_servers"]["docs"]
    assert entry["disabled_tools"] == []
    assert entry["tools"]["search"]["approval_mode"] == "prompt"
    with pytest.raises(ValueError, match="definitions belong"):
        config.change("remove", "docs", "codex", "project", str(project), plugin="docs@market", environ=env)


def test_claude_scopes_are_independent_and_permissions_do_not_remove_wildcard_denies(setup):
    home, project, env = setup
    registry = home / ".claude.json"
    registry.write_text(json.dumps({"mcpServers": {"shared": {"command": "keep"}}, "unrelated": True}))
    config.change("add", "local", "claude", "local", str(project), entry={"command": "local"}, environ=env)
    data = json.loads(registry.read_text())
    assert data["unrelated"] is True and data["mcpServers"]["shared"]["command"] == "keep"
    assert data["projects"][str(project)]["mcpServers"]["local"]["command"] == "local"
    settings = project / ".claude/settings.json"
    settings.parent.mkdir()
    settings.write_text(json.dumps({"permissions": {"deny": ["mcp__shared__*", "Read(secret)"]}, "theme": "dark"}))
    config.change("tool", "shared", "claude", "project", str(project), tool="read", mode="allow", environ=env)
    updated = json.loads(settings.read_text())
    assert updated["permissions"] == {"deny": ["mcp__shared__*", "Read(secret)"], "ask": [], "allow": ["mcp__shared__read"]}
    assert updated["theme"] == "dark"
    config.change("disable", "shared", "claude", "project", str(project), environ=env)
    assert json.loads(registry.read_text())["projects"][str(project)]["disabledMcpServers"] == ["shared"]
    with pytest.raises(ValueError, match="project scope"):
        config.change("disable", "shared", "claude", "user", environ=env)


def test_shared_project_enable_uses_native_local_approvals(setup):
    home, project, env = setup
    config.change("add", "sample", "claude", "project", str(project), entry={"type": "http", "url": "https://example.invalid/mcp"}, environ=env)
    config.change("enable", "sample", "claude", "project", str(project), environ=env)
    path = project / ".claude/settings.local.json"
    data = json.loads(path.read_text())
    assert data["enabledMcpjsonServers"] == ["sample"]
    config.change("disable", "sample", "claude", "project", str(project), environ=env)
    assert json.loads(path.read_text())["disabledMcpjsonServers"] == ["sample"]


def test_malformed_existing_config_is_not_overwritten_and_migration_is_explicit(setup):
    home, project, env = setup
    path = project / ".mcp.json"
    path.write_text('{"secret":broken')
    with pytest.raises(ValueError, match="malformed"):
        config.change("add", "sample", "claude", "project", str(project), entry={"command": "node"}, environ=env)
    assert path.read_text() == '{"secret":broken'
    transaction = config.migrate_keel({"mcp": {"posthog": {"targets": ["123"]}}, "guards": {"mcp_unknown": "warn"}, "writable": ["unrelated"]}, "project", str(project), env)
    migrated = project / "mcpclerk.json"
    assert json.loads(migrated.read_text()) == {"mcp": {"posthog": {"targets": ["123"]}}, "unknown": "warn"}
    storage.undo(transaction, env)
    assert not migrated.exists()


def test_cli_mutation_list_judge_and_undo_do_not_print_secrets(setup):
    home, project, env = setup
    server = project / "server.json"
    server.write_text(json.dumps({"url": "https://example.invalid/mcp?token=private-value", "headers": {"Authorization": "Bearer secret-value"}}))
    process_env = dict(os.environ, **env, PYTHONPATH=str(Path(__file__).parents[1] / "src"))
    def run(*args, payload=None):
        return subprocess.run([sys.executable, "-m", "mcpclerk", *args], env=process_env, text=True, input=payload, capture_output=True)
    added = run("add", "sample", "--host", "claude", "--scope", "project", "--project", str(project), "--file", str(server))
    assert added.returncode == 0, added.stderr
    receipt = json.loads(added.stdout)["transaction"]
    listed = run("list", "--project", str(project))
    assert listed.returncode == 0, listed.stderr
    assert "sample" in listed.stdout and "private-value" not in listed.stdout and "secret-value" not in listed.stdout
    result = run("judge", payload=json.dumps({"version": 1, "tool": "mcp__revenuecat__create_product", "args": {"secret": "private-value"}, "host": "claude", "cwd": str(project), "project_dir": str(project), "task": {"permissions": []}}))
    assert result.returncode == 0 and json.loads(result.stdout)["allowed"] is False
    assert "private-value" not in result.stdout
    assert run("judge", payload='{"secret":private-value}').returncode == 1
    assert run("undo", receipt).returncode == 0
    assert not (project / ".mcp.json").exists()
