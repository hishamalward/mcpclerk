import os

import pytest

from mcpclerk.policy import PolicyError, load_policy, parse_policy
from tests.helpers import policy


def test_exact_glob_default_resolution():
    p = policy({"read_thing": "allow", "read_*": "approve", "re*": "deny", "write_thing": {"decision": "approve", "quota": {"per_minute": 3}}})
    assert p.resolve("fake", "read_thing").decision == "allow"           # exact beats glob
    assert p.resolve("fake", "read_other").decision == "approve"         # longest glob wins
    assert p.resolve("fake", "reboot").decision == "deny"                # shorter glob
    assert p.resolve("fake", "unlisted_tool").decision == "deny"         # default deny
    assert p.resolve("fake", "unlisted_tool").source == "default"
    r = p.resolve("fake", "write_thing")
    assert (r.decision, r.per_minute, r.per_run) == ("approve", 3, None)


def test_default_can_be_changed_but_defaults_to_deny():
    assert policy({}).unlisted == "deny"
    assert policy({}, unlisted="allow").resolve("fake", "x").decision == "allow"


@pytest.mark.parametrize("bad,msg", [
    ({"version": 2, "upstreams": {"a": {"command": "x"}}}, "version"),
    ({"version": 1, "upstreams": {}}, "non-empty"),
    ({"version": 1, "upstreams": {"a.b": {"command": "x"}}}, "upstream names"),
    ({"version": 1, "upstreams": {"a": {"transport": "stdio"}}}, "needs 'command'"),
    ({"version": 1, "upstreams": {"a": {"transport": "http"}}}, "needs 'url'"),
    ({"version": 1, "upstreams": {"a": {"command": "x", "tools": {"t": "maybe"}}}}, "decision must be"),
    ({"version": 1, "upstreams": {"a": {"command": "x", "tools": {"t": {"decision": "allow", "quota": {"per_hour": 1}}}}}}, "unknown key"),
    ({"version": 1, "upstreams": {"a": {"command": "x", "tools": {"t": {"decision": "allow", "quota": {"per_run": 0}}}}}}, "positive integer"),
    ({"version": 1, "defaults": {"unlisted": "yes"}, "upstreams": {"a": {"command": "x"}}}, "defaults.unlisted"),
    ({"version": 1, "typo": 1, "upstreams": {"a": {"command": "x"}}}, "unknown key"),
    ({"version": 1, "redaction": {"extend": ["("]}, "upstreams": {"a": {"command": "x"}}}, "not a valid regex"),
])
def test_validation_fails_loudly(bad, msg):
    with pytest.raises(PolicyError, match=msg):
        parse_policy(bad)


def test_env_expansion_and_missing_var(monkeypatch):
    monkeypatch.setenv("MCPCLERK_TEST_DIR", "/tmp/x")
    p = parse_policy({"version": 1, "upstreams": {"a": {"command": "npx", "args": ["${MCPCLERK_TEST_DIR}"], "env": {"K": "${MCPCLERK_TEST_DIR}"}}}})
    assert p.upstreams["a"].args == ["/tmp/x"] and p.upstreams["a"].env == {"K": "/tmp/x"}
    monkeypatch.delenv("MCPCLERK_TEST_DIR")
    with pytest.raises(PolicyError, match="not set"):
        parse_policy({"version": 1, "upstreams": {"a": {"command": "${MCPCLERK_TEST_DIR}"}}})


def test_load_policy_records_sha(tmp_path):
    f = tmp_path / "p.yaml"
    f.write_text("version: 1\nupstreams:\n  a:\n    command: x\n    tools:\n      t: allow\n")
    p = load_policy(f)
    assert len(p.sha256) == 64 and p.path == str(f)
    assert p.split("a.t") == ("a", "t") and p.split("nodot") is None and p.join("a", "t") == "a.t"


def test_separator_option():
    p = parse_policy({"version": 1, "namespace_separator": "__", "upstreams": {"a": {"command": "x"}}})
    assert p.join("a", "t") == "a__t" and p.split("a__t__x") == ("a", "t__x")
