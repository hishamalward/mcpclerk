"""Full pipeline through the in-memory MCP transport: Client(proxy) -> proxy -> Client(fake)."""
import json

import anyio
import pytest
from mcp.client import Client

from mcpclerk.audit import verify
from tests.helpers import make_proxy

pytestmark = pytest.mark.anyio

BASE = {"echo": "allow", "read_thing": "allow", "write_thing": "approve", "delete_thing": "deny",
        "secret_sink": "allow", "boom": "allow"}


def _entries(path):
    return [json.loads(l) for l in path.read_text().splitlines()]


async def test_discovery_hides_denied_and_unlisted(tmp_path):
    proxy, server, log = make_proxy(tmp_path, {"echo": "allow", "write_thing": "approve", "delete_thing": "deny"})
    async with Client(server) as c:
        names = sorted(t.name for t in (await c.list_tools()).tools)
    assert names == ["fake.echo", "fake.write_thing"]  # delete (deny) and the rest (unlisted) hidden
    ev = [e for e in _entries(log) if e["kind"] == "event"]
    assert ev[0]["event"] == "run-start" and ev[0]["approve_session"] is False
    assert ev[1] == {**ev[1], "event": "discover", "upstream": "fake", "exposed": 2, "hidden": 4}
    assert ev[-1]["event"] == "run-end" and verify(log).ok


async def test_allow_passes_through_and_is_logged(tmp_path):
    proxy, server, log = make_proxy(tmp_path, BASE)
    async with Client(server) as c:
        r = await c.call_tool("fake.echo", {"text": "hi"})
    assert not r.is_error and r.content[0].text == "hi"
    call = [e for e in _entries(log) if e["kind"] == "call"][0]
    assert call["decision"] == "allowed" and call["outcome"] == "ok" and call["latency_ms"] >= 0
    assert call["content_bytes"] == 2 and "content" not in call  # results are not logged


async def test_denied_hidden_tool_called_by_name_is_refused(tmp_path):
    proxy, server, log = make_proxy(tmp_path, BASE)
    async with Client(server) as c:
        r = await c.call_tool("fake.delete_thing", {"path": "x"})
        u = await c.call_tool("fake.unlisted", {})
        nope = await c.call_tool("other.echo", {})
    assert r.is_error and "refused-denied" in r.content[0].text
    assert u.is_error and "refused-unknown" in u.content[0].text
    assert nope.is_error and "no such upstream" in nope.content[0].text
    decisions = [e["decision"] for e in _entries(log) if e["kind"] == "call"]
    assert decisions == ["refused-denied", "refused-unknown", "refused-unknown"]


async def test_default_deny_can_be_relaxed(tmp_path):
    proxy, server, log = make_proxy(tmp_path, {}, unlisted="allow")
    async with Client(server) as c:
        r = await c.call_tool("fake.echo", {"text": "x"})
    assert not r.is_error


async def test_quota_exhaustion(tmp_path):
    proxy, server, log = make_proxy(tmp_path, {"echo": {"decision": "allow", "quota": {"per_run": 2}}})
    async with Client(server) as c:
        a = await c.call_tool("fake.echo", {"text": "1"})
        b = await c.call_tool("fake.echo", {"text": "2"})
        d = await c.call_tool("fake.echo", {"text": "3"})
    assert not a.is_error and not b.is_error and d.is_error
    assert "refused-quota" in d.content[0].text and "per-run quota of 2" in d.content[0].text
    assert [e["decision"] for e in _entries(log) if e["kind"] == "call"] == ["allowed", "allowed", "refused-quota"]


async def test_approve_via_file_then_forward(tmp_path):
    proxy, server, log = make_proxy(tmp_path, BASE)
    approvals = tmp_path / "approvals"

    async def human():
        while not list(approvals.glob("*.json")) if approvals.exists() else True:
            await anyio.sleep(0.05)
        f = next(approvals.glob("*.json"))
        rec = json.loads(f.read_text())
        assert rec["args_redacted"]["content"] == "[REDACTED:kv-secret]"
        rec["approved"] = True
        f.write_text(json.dumps(rec))

    async with Client(server) as c:
        async with anyio.create_task_group() as tg:
            tg.start_soon(human)
            r = await c.call_tool("fake.write_thing", {"path": "n.md", "content": "password=hunter2"})
        back = await c.call_tool("fake.read_thing", {"path": "n.md"})
    assert not r.is_error and "wrote 16 chars" in r.content[0].text
    assert back.content[0].text == "password=hunter2"  # upstream got the real content
    call = [e for e in _entries(log) if e["kind"] == "call"][0]
    assert call["decision"] == "approved" and call["approved_by"] == "file" and call["held_ms"] > 0
    assert call["args"]["content"] == "[REDACTED:kv-secret]"
    assert "hunter2" not in log.read_text()


async def test_approval_refused_and_timed_out(tmp_path):
    proxy, server, log = make_proxy(tmp_path, {"write_thing": {"decision": "approve", "approval_timeout_s": 0.5}})
    approvals = tmp_path / "approvals"

    async def human_refuses():
        while not (approvals.exists() and list(approvals.glob("*.json"))):
            await anyio.sleep(0.05)
        f = next(approvals.glob("*.json"))
        rec = json.loads(f.read_text())
        rec["approved"] = False
        rec["note"] = "not today"
        f.write_text(json.dumps(rec))

    async with Client(server) as c:
        async with anyio.create_task_group() as tg:
            tg.start_soon(human_refuses)
            r1 = await c.call_tool("fake.write_thing", {"path": "a", "content": "b"})
        r2 = await c.call_tool("fake.write_thing", {"path": "a", "content": "b"})  # nobody answers
    assert r1.is_error and "refused-by-human" in r1.content[0].text and "not today" in r1.content[0].text
    assert r2.is_error and "refused-timeout" in r2.content[0].text
    calls = [e for e in _entries(log) if e["kind"] == "call"]
    assert [c["decision"] for c in calls] == ["refused-by-human", "refused-timeout"]
    assert calls[0]["outcome"] == "not-forwarded" and calls[1]["outcome"] == "not-forwarded"


async def test_approve_session_flag_is_loud(tmp_path):
    proxy, server, log = make_proxy(tmp_path, BASE, approve_session=True)
    async with Client(server) as c:
        r = await c.call_tool("fake.write_thing", {"path": "a", "content": "b"})
    assert not r.is_error
    rows = _entries(log)
    assert rows[0]["approve_session"] is True
    call = [e for e in rows if e["kind"] == "call"][0]
    assert call["approved_by"] == "session-flag" and not (tmp_path / "approvals").exists()


async def test_upstream_receives_unredacted_args(tmp_path):
    proxy, server, log = make_proxy(tmp_path, BASE)
    payload = {"api_key": "sk-abcdefghijklmnopqrstuvwxyz", "note": "Bearer abc.def", "n": 1}
    async with Client(server) as c:
        r = await c.call_tool("fake.secret_sink", {"payload": payload})
    assert json.loads(r.content[0].text) == payload          # upstream saw everything
    call = [e for e in _entries(log) if e["kind"] == "call"][0]
    assert call["args"]["payload"]["api_key"].startswith("[REDACTED:key-")
    assert "[REDACTED:bearer]" in call["args"]["payload"]["note"]
    text = log.read_text()
    assert "sk-abcdefghijklmnopqrstuvwxyz" not in text and "abc.def" not in text


async def test_upstream_error_is_logged_as_error_outcome(tmp_path):
    proxy, server, log = make_proxy(tmp_path, BASE)
    async with Client(server) as c:
        r = await c.call_tool("fake.boom", {"text": "x"})
    assert r.is_error
    call = [e for e in _entries(log) if e["kind"] == "call"][0]
    assert call["decision"] == "allowed" and call["outcome"] == "error" and call["is_error"] is True


async def test_log_verifies_after_a_full_run(tmp_path):
    proxy, server, log = make_proxy(tmp_path, BASE)
    async with Client(server) as c:
        await c.call_tool("fake.echo", {"text": "a"})
        await c.call_tool("fake.delete_thing", {"path": "x"})
    r = verify(log)
    assert r.ok and r.entries == 5  # run-start, discover, 2 calls, run-end
    assert _entries(log)[-1]["entries"] == 5
