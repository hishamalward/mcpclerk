import json

from mcpclerk.connection import pinned_project


def test_claude_user_and_project_config_pins(tmp_path):
    config = tmp_path / ".claude"
    config.mkdir()
    (config / ".claude.json").write_text(json.dumps({"mcpServers": {"posthog": {
        "url": "https://mcp.posthog.com/mcp?project_id=one"}}}))
    env = {"HOME": str(tmp_path), "CLAUDE_CONFIG_DIR": str(config)}
    assert pinned_project("posthog", "claude", env) == "one"
    project = tmp_path / "project"
    project.mkdir()
    (project / ".mcp.json").write_text(json.dumps({"mcpServers": {"posthog": {
        "url": "https://mcp.posthog.com/mcp", "headers": {"X-PostHog-Project-Id": "two"}}}}))
    (config / ".claude.json").write_text(json.dumps({"projects": {str(project): {
        "enabledMcpjsonServers": ["posthog"]}}}))
    env["CLAUDE_PROJECT_DIR"] = str(project)
    assert pinned_project("posthog", "claude", env) == "two"


def test_disagreeing_pins_and_malformed_config_are_unpinned(tmp_path):
    config = tmp_path / ".claude"
    config.mkdir()
    (config / ".claude.json").write_text(json.dumps({"mcpServers": {"posthog": {
        "url": "https://mcp.posthog.com/mcp?project_id=one",
        "headers": {"x-posthog-project-id": "two"}}}}))
    env = {"HOME": str(tmp_path), "CLAUDE_CONFIG_DIR": str(config)}
    assert pinned_project("posthog", "claude", env) is None
    (config / ".claude.json").write_text("{")
    assert pinned_project("posthog", "claude", env) is None


def test_codex_native_config_and_env_headers(tmp_path):
    codex = tmp_path / ".codex"
    codex.mkdir()
    path = codex / "config.toml"
    path.write_text('[mcp_servers.posthog]\nurl="https://mcp.posthog.com/mcp"\nenv_http_headers={"x-posthog-project-id"="PIN"}\n')
    env = {"HOME": str(tmp_path), "CODEX_HOME": str(codex), "PIN": "three"}
    assert pinned_project("posthog", "codex", env) == "three"
    assert pinned_project("posthog", "codex", {"HOME": str(tmp_path), "CODEX_HOME": str(codex)}) is None


def test_claude_user_approval_and_any_scope_disable(tmp_path):
    config = tmp_path / ".claude"
    config.mkdir()
    project = tmp_path / "project"
    (project / ".claude").mkdir(parents=True)
    (project / ".mcp.json").write_text(json.dumps({"mcpServers": {"posthog": {
        "url": "https://mcp.posthog.com/mcp", "headers": {"x-posthog-project-id": "four"}}}}))
    (config / ".claude.json").write_text(json.dumps({"projects": {str(project): {}}}))
    (config / "settings.json").write_text(json.dumps({"enabledMcpjsonServers": ["posthog"]}))
    env = {"HOME": str(tmp_path), "CLAUDE_CONFIG_DIR": str(config), "CLAUDE_PROJECT_DIR": str(project)}
    assert pinned_project("posthog", "claude", env) == "four"
    (project / ".claude" / "settings.json").write_text(json.dumps({"disabledMcpjsonServers": ["posthog"]}))
    assert pinned_project("posthog", "claude", env) is None


def test_tracked_project_settings_cannot_approve_without_verified_trust(tmp_path):
    import subprocess

    project = tmp_path / "project"
    (project / ".claude").mkdir(parents=True)
    (project / ".mcp.json").write_text(json.dumps({"mcpServers": {"posthog": {
        "url": "https://mcp.posthog.com/mcp?project_id=five"}}}))
    subprocess.run(["git", "init", str(project)], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    (project / ".claude" / "settings.json").write_text(json.dumps({"enabledMcpjsonServers": ["posthog"]}))
    subprocess.run(["git", "-C", str(project), "add", ".claude/settings.json"], check=True)
    config = tmp_path / ".claude"
    config.mkdir()
    path = config / ".claude.json"
    path.write_text(json.dumps({"projects": {str(project): {}}}))
    env = {"HOME": str(tmp_path), "CLAUDE_CONFIG_DIR": str(config), "CLAUDE_PROJECT_DIR": str(project)}
    assert pinned_project("posthog", "claude", env) is None
    path.write_text(json.dumps({"projects": {str(project): {"hasTrustDialogAccepted": True}}}))
    assert pinned_project("posthog", "claude", env) == "five"


def test_codex_trusted_project_override_and_pin_conflicts(tmp_path):
    codex = tmp_path / ".codex"
    project = tmp_path / "project"
    (codex).mkdir()
    (project / ".codex").mkdir(parents=True)
    (codex / "config.toml").write_text(f'[projects."{project}"]\ntrust_level="trusted"\n'
        '[mcp_servers.posthog]\nurl="https://mcp.posthog.com/mcp"\nhttp_headers={"x-posthog-project-id"="user"}\n')
    (project / ".codex" / "config.toml").write_text('[mcp_servers.posthog]\n'
        'url="https://mcp.posthog.com/mcp"\nhttp_headers={"x-posthog-project-id"="project"}\n')
    env = {"HOME": str(tmp_path), "CODEX_HOME": str(codex)}
    assert pinned_project("posthog", "codex", env, str(project)) is None
    (project / ".codex" / "config.toml").write_text('[mcp_servers.posthog]\n'
        'url="https://mcp.posthog.com/mcp"\nhttp_headers={"x-posthog-project-id"="user"}\n')
    assert pinned_project("posthog", "codex", env, str(project)) == "user"
