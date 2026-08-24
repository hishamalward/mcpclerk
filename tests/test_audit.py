import json

from mcpclerk.audit import GENESIS, AuditLog, format_report, report, verify


def _write(tmp_path, n=3):
    log = AuditLog(tmp_path / "a.jsonl")
    log.append({"kind": "event", "event": "run-start", "run_id": "r1"})
    for i in range(n):
        log.append({"kind": "call", "run_id": "r1", "name": "fake.echo", "decision": "allowed", "outcome": "ok", "latency_ms": 1.0 + i})
    log.append({"kind": "event", "event": "run-end", "run_id": "r1", "entries": n + 2})
    log.close()
    return tmp_path / "a.jsonl"


def test_chain_and_verify_clean(tmp_path):
    path = _write(tmp_path)
    rows = [json.loads(l) for l in path.read_text().splitlines()]
    assert rows[0]["prev_hash"] == GENESIS and rows[0]["seq"] == 1
    assert all(rows[i]["prev_hash"] == rows[i - 1]["hash"] for i in range(1, len(rows)))
    r = verify(path)
    assert r.ok and r.entries == 5 and str(r).startswith("OK 5")


def test_verify_detects_edit(tmp_path):
    path = _write(tmp_path)
    lines = path.read_text().splitlines()
    lines[2] = lines[2].replace('"allowed"', '"approved"')
    path.write_text("\n".join(lines) + "\n")
    r = verify(path)
    assert not r.ok and r.line == 3 and "edited" in r.error


def test_verify_detects_deleted_line(tmp_path):
    path = _write(tmp_path)
    lines = path.read_text().splitlines()
    del lines[2]
    path.write_text("\n".join(lines) + "\n")
    r = verify(path)
    assert not r.ok and r.line == 3 and "seq" in r.error


def test_verify_detects_reorder(tmp_path):
    path = _write(tmp_path)
    lines = path.read_text().splitlines()
    lines[1], lines[2] = lines[2], lines[1]
    path.write_text("\n".join(lines) + "\n")
    assert not verify(path).ok


def test_verify_detects_tail_truncation_of_completed_run(tmp_path):
    path = _write(tmp_path)
    lines = path.read_text().splitlines()
    # drop a call but keep the run-end honest about seq by rewriting is impossible without the key,
    # so simulate the realistic attack: remove a middle line AND renumber is not attempted; instead
    # remove the run-end's predecessor by rebuilding a chain from a fresh log missing one call
    log = AuditLog(tmp_path / "b.jsonl")
    log.append({"kind": "event", "event": "run-start", "run_id": "r1"})
    log.append({"kind": "call", "run_id": "r1", "name": "fake.echo", "decision": "allowed", "outcome": "ok"})
    log.append({"kind": "event", "event": "run-end", "run_id": "r1", "entries": 4})  # claims one more than exists
    log.close()
    r = verify(tmp_path / "b.jsonl")
    assert not r.ok and "truncated" in r.error


def test_chain_continues_across_runs(tmp_path):
    path = _write(tmp_path, 1)
    log = AuditLog(path)
    assert log.seq == 3 and log.prev_hash != GENESIS
    log.append({"kind": "event", "event": "run-start", "run_id": "r2"})
    log.close()
    assert verify(path).ok and verify(path).entries == 4


def test_report(tmp_path):
    path = _write(tmp_path, 4)
    log = AuditLog(path)
    log.append({"kind": "call", "run_id": "r2", "name": "fake.write_thing", "decision": "approved", "approved_by": "file", "held_ms": 250.0, "outcome": "ok", "latency_ms": 3.0})
    log.append({"kind": "call", "run_id": "r2", "name": "fake.delete_thing", "decision": "refused-denied", "outcome": "not-forwarded"})
    log.close()
    r = report(path)
    assert r["calls"] == 6 and r["approved"]["count"] == 1 and r["approved"]["by"] == {"file": 1}
    assert r["refusals"]["by_decision"] == {"refused-denied": 1}
    assert r["per_tool"]["fake.echo"]["calls"] == 4 and r["per_tool"]["fake.echo"]["p50_ms"] == 2.5
    text = format_report(r)
    assert "fake.write_thing" in text and "refused  1" in text
