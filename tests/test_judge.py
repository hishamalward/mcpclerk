import json

import pytest

from mcpclerk.judge import evaluate


def judge(data, environ=None):
    env = {"HOME": "/__mcpclerk_test_home__", "CLAUDE_CONFIG_DIR": "/__mcpclerk_test_home__/.claude",
           "MCPCLERK_HOME": "/__mcpclerk_test_home__/.mcpclerk"}
    env.update(environ or {})
    return evaluate(data, env)


def payload(tool, args=None, permissions=(), project=None, host="claude"): 
    return {"version": 1, "tool": tool, "args": args or {}, "host": host, "cwd": ".",
            "project_dir": project, "task": {"permissions": list(permissions)}}


def test_read_and_unknown_operation():
    assert judge(payload("mcp__revenuecat__list_apps"))["allowed"]
    result = judge(payload("mcp__revenuecat__invent_secret_operation"))
    assert (result["allowed"], result["classification"]) == (False, "unknown")


def test_malformed_input_fails_closed():
    with pytest.raises(ValueError):
        judge({"version": 1, "tool": "mcp__x__y", "args": {}, "host": "claude", "cwd": ".",
                  "project_dir": None, "task": {"permissions": [None]}})
    for name in ("mcp__", "mcp__server", "mcp__server__"):
        with pytest.raises(ValueError):
            judge({"version": 1, "tool": name, "args": {}, "host": "claude", "cwd": ".",
                   "project_dir": None, "task": {"permissions": []}})


def test_permissions_targets_and_risk_union(tmp_path):
    (tmp_path / "mcpclerk.json").write_text(json.dumps({"mcp": {"revenuecat": {
        "targets": ["good"], "read": ["create_product"],
    }}}))
    result = judge(payload("mcp__revenuecat__create_product", {"project_id": "bad", "secret": "noecho"},
                              ["remote-write"], str(tmp_path)))
    assert not result["allowed"] and result["classification"] == "remote-write"
    assert "bad" not in result["reason"] and "noecho" not in json.dumps(result)
    assert not judge(payload("mcp__revenuecat__create_product", {"project_id": "good"},
                                [], str(tmp_path)))["allowed"]
    assert judge(payload("mcp__revenuecat__create_product", {"project_id": "good"},
                             ["remote-write"], str(tmp_path)))["allowed"]


def test_unknown_server_mode_and_configured_generic_tool(tmp_path):
    (tmp_path / "mcpclerk.json").write_text(json.dumps({"unknown": "warn", "mcp": {
        "custom": {"servers": ["odd-name"], "write": ["change"], "target_arg": "workspace", "targets": ["w1"]}
    }}))
    unknown = judge(payload("mcp__other__tool", project=str(tmp_path)))
    assert unknown["allowed"] and "warning" in unknown["reason"]
    call = judge(payload("mcp__odd-name__change", {"workspace": "w1"}, ["remote-write"], str(tmp_path)))
    assert call["allowed"]


def test_posthog_target_pin_and_no_argument_echo(tmp_path):
    (tmp_path / "mcpclerk.json").write_text(json.dumps({"mcp": {"posthog": {"targets": ["111"]}}}))
    env = {"HOME": str(tmp_path), "CLAUDE_CONFIG_DIR": str(tmp_path / ".claude")}
    settings = tmp_path / ".claude" / ".claude.json"
    settings.parent.mkdir()
    settings.write_text(json.dumps({"mcpServers": {"posthog": {"type": "http", "url":
        "https://mcp.posthog.com/mcp", "headers": {"x-posthog-project-id": "111"}}}}))
    r = judge(payload("mcp__posthog__exec", {"command": 'call create-feature-flag {"key":"secret"}'},
                          ["remote-write"], str(tmp_path)), env)
    assert r["allowed"] and "secret" not in json.dumps(r)


def test_posthog_switch_and_explicit_project_targets(tmp_path):
    (tmp_path / "mcpclerk.json").write_text(json.dumps({"mcp": {"posthog": {"targets": ["111"]}}}))
    task = ["remote-write", "publish"]
    for command in ("switch 222", 'call switch-project {"projectId":"222"}',
                    'call switch-organization {"organizationId":"222"}',
                    'call project-settings-update {"id":"222","project_id":"111"}'):
        result = judge(payload("mcp__posthog__exec", {"command": command}, task, str(tmp_path)))
        assert not result["allowed"]
        assert "222" not in result["reason"]
    ok = judge(payload("mcp__posthog__exec", {"command": "switch 111"}, task, str(tmp_path)))
    assert ok["allowed"]


def test_posthog_flags_and_operation_class_use_catalog_names(tmp_path):
    project = tmp_path / "mcpclerk.json"
    project.write_text(json.dumps({"mcp": {"posthog": {"targets": ["*"]}}}))
    publish = judge(payload("mcp__plugin_posthog_posthog__exec", {
        "command": 'call --confirm --json workflows-publish {"id":"secret"}'}, ["publish"], str(tmp_path)))
    write = judge(payload("mcp__posthog__exec", {
        "command": 'call update-feature-flag {"id":"secret","active":true}'}, ["remote-write"], str(tmp_path)))
    assert publish["allowed"] and publish["classification"] == "publish"
    assert write["allowed"] and write["classification"] == "remote-write"


def test_sentry_wrapped_tool_and_issue_url_org_target(tmp_path):
    (tmp_path / "mcpclerk.json").write_text(json.dumps({"mcp": {"sentry": {"targets": ["my-org"]}}}))
    call = "mcp__plugin_sentry_sentry__execute_sentry_tool"
    bad = judge(payload(call, {"name": "analyze_issue_with_seer", "arguments": {
        "issueUrl": "https://outside-org.sentry.io/issues/123", "apiKey": "secret"}}, ["paid-job"], str(tmp_path)))
    assert not bad["allowed"] and bad["classification"] == "paid"
    assert "outside-org" not in bad["reason"] and "secret" not in json.dumps(bad)
    good = judge(payload(call, {"name": "get_issue_details", "arguments": {
        "issueUrl": "https://outside-org.sentry.io/issues/123"}}, [], str(tmp_path)))
    assert good["allowed"] and good["classification"] == "read"


def test_dataforseo_paid_and_unknown_routes():
    paid = judge(payload("mcp__dfs-mcp__api_request", {
        "method": "POST", "path": "/v3/serp/google/organic/live/advanced", "api_key": "secret"}, ["paid-job"]))
    denied = judge(payload("mcp__dfs-mcp__api_request", {
        "method": "POST", "path": "/v3/something/new"}, ["remote-write", "paid-job"]))
    assert paid["allowed"] and paid["classification"] == "paid"
    assert not denied["allowed"] and denied["classification"] == "unknown"
    assert "secret" not in json.dumps(paid)


def test_dataforseo_documentation_reads_have_no_target():
    from mcpclerk.judge import Call, _classify
    from mcpclerk.catalog import DEFAULT

    for tool in ("docs_index", "docs_list_sections", "docs_search"):
        assert _classify(Call("dataforseo", "dfs-mcp", tool, {}, DEFAULT["dataforseo"])) == ("read", None)


def test_catalog_risk_class_union_cannot_downgrade_write(tmp_path):
    (tmp_path / "mcpclerk.json").write_text(json.dumps({"mcp": {
        "revenuecat": {"read": ["create_product"], "targets": ["good"]}
    }}))
    r = judge(payload("mcp__revenuecat__create_product", {"project_id": "good"}, ["remote-write"], str(tmp_path)))
    assert r["allowed"] and r["classification"] == "remote-write"


def test_each_shipped_operation_belongs_to_only_one_class():
    from mcpclerk.catalog import DEFAULT, CLASS_KEYS

    for service in ("revenuecat", "posthog", "sentry"):
        entry = DEFAULT[service]
        buckets = [set(entry.get(key, [])) for key in CLASS_KEYS]
        assert all(not (a & b) for i, a in enumerate(buckets) for b in buckets[i + 1:]), service


def test_claude_connection_origin_is_separate_from_worktree_policy(tmp_path):
    origin, tree, home = (tmp_path / name for name in ("origin", "tree", "home"))
    for folder in (origin, tree, home):
        folder.mkdir()
    (tree / "mcpclerk.json").write_text(json.dumps({"mcp": {"posthog": {"targets": ["111"]}}}))
    (home / ".claude.json").write_text(json.dumps({"projects": {
        str(folder): {"mcpServers": {"posthog": {"url": "https://mcp.posthog.com/mcp?project_id=" + pin}}}
        for folder, pin in ((origin, "111"), (tree, "222"))
    }}))
    result = judge(payload("mcp__posthog__exec", {"command": 'call create-feature-flag {}'},
                           ["remote-write"], str(tree)),
                   {"CLAUDE_CONFIG_DIR": str(home), "CLAUDE_PROJECT_DIR": str(origin)})
    assert result["allowed"]
