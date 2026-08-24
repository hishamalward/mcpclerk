import json
import os

import anyio
import pytest

from mcpclerk.approval import ApprovalRequest, Tty, request_approval

pytestmark = pytest.mark.anyio


def _req(timeout=5.0):
    return ApprovalRequest("id-1", "fake", "write_thing", "fake.write_thing", {"path": "x", "content": "[REDACTED:kv-secret]"}, timeout)


async def test_file_queue_approve_with_note(tmp_path):
    async def human():
        await anyio.sleep(0.3)
        f = tmp_path / "id-1.json"
        rec = json.loads(f.read_text())
        assert rec["approved"] is None and rec["args_redacted"]["content"] == "[REDACTED:kv-secret]"
        rec["approved"] = True
        rec["note"] = "looks fine"
        f.write_text(json.dumps(rec))

    async with anyio.create_task_group() as tg:
        tg.start_soon(human)
        out = await request_approval(_req(), tmp_path, tty=None)
    assert (out.decision, out.by, out.note) == ("approved", "file", "looks fine")
    final = json.loads((tmp_path / "id-1.json").read_text())
    assert final["resolved_by"] == "file" and final["approved"] is True and "resolved_at" in final


async def test_file_queue_refuse(tmp_path):
    async def human():
        await anyio.sleep(0.3)
        f = tmp_path / "id-1.json"
        rec = json.loads(f.read_text())
        rec["approved"] = False
        f.write_text(json.dumps(rec))

    async with anyio.create_task_group() as tg:
        tg.start_soon(human)
        out = await request_approval(_req(), tmp_path, tty=None)
    assert (out.decision, out.by) == ("refused", "file")


async def test_timeout_is_a_refusal(tmp_path):
    out = await request_approval(_req(timeout=0.6), tmp_path, tty=None)
    assert out.decision == "timeout" and out.by is None
    final = json.loads((tmp_path / "id-1.json").read_text())
    assert final["resolved_by"] == "timeout" and final["approved"] is False


@pytest.mark.skipif(os.name != "posix", reason="pty")
async def test_terminal_prompt_wins(tmp_path):
    master, slave = os.openpty()
    tty = Tty.from_fd(slave)
    os.close(slave)
    try:
        async def human():
            await anyio.sleep(0.3)
            os.write(master, b"maybe\n")   # re-prompt
            await anyio.sleep(0.3)
            os.write(master, b"y\n")

        async with anyio.create_task_group() as tg:
            tg.start_soon(human)
            out = await request_approval(_req(), tmp_path, tty=tty)
        assert (out.decision, out.by) == ("approved", "terminal")
        shown = os.read(master, 4096).decode()
        assert "approval needed: fake.write_thing" in shown and "[REDACTED:kv-secret]" in shown
    finally:
        tty.close()
        os.close(master)


async def test_approve_command_answers_the_queue(tmp_path):
    """The proxy holds a call; `mcpclerk approve` in another process answers it."""
    import io
    import threading
    from mcpclerk.approval import approve_loop, list_pending

    assert list_pending(tmp_path) == []
    out = io.StringIO()
    result = {}

    def human():  # a second terminal
        result["n"] = approve_loop(tmp_path, once=True, wait_s=5.0, stdin=io.StringIO("what\ny\n"), stdout=out)

    t = threading.Thread(target=human)
    t.start()
    outcome = await request_approval(_req(), tmp_path, tty=None)
    t.join(5)
    assert (outcome.decision, outcome.by) == ("approved", "file")
    assert result["n"] == 1
    text = out.getvalue()
    assert "approval needed: fake.write_thing" in text and "[REDACTED:kv-secret]" in text and "please answer y or n" in text


def test_list_pending_skips_answered_and_expired(tmp_path):
    from mcpclerk.approval import list_pending
    (tmp_path / "a.json").write_text(json.dumps({"id": "a", "approved": None, "expires_at": "2999-01-01T00:00:00Z"}))
    (tmp_path / "b.json").write_text(json.dumps({"id": "b", "approved": True, "expires_at": "2999-01-01T00:00:00Z"}))
    (tmp_path / "c.json").write_text(json.dumps({"id": "c", "approved": None, "expires_at": "2000-01-01T00:00:00Z"}))
    (tmp_path / "d.json").write_text("not json")
    assert [r["id"] for r in list_pending(tmp_path)] == ["a"]
