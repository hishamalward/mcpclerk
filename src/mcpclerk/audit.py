"""Append-only, hash-chained JSON Lines audit log; verify and report.

hash = SHA-256(prev_hash + "\\n" + canonical(entry without "hash")). The first entry's prev_hash is
64 zeros. seq starts at 1 and increments by 1 across runs in the same file, so the chain spans runs.
"""
from __future__ import annotations

import hashlib
import json
import os
import statistics
from dataclasses import dataclass
from pathlib import Path
from typing import Any

GENESIS = "0" * 64

DECISIONS = (
    "allowed", "approved", "refused-denied", "refused-unknown", "refused-quota",
    "refused-timeout", "refused-by-human",
)


def canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def entry_hash(prev_hash: str, entry: dict[str, Any]) -> str:
    body = {k: v for k, v in entry.items() if k != "hash"}
    return hashlib.sha256((prev_hash + "\n" + canonical(body)).encode("utf-8")).hexdigest()


class AuditLog:
    """Appends entries to a file, continuing an existing chain if the file already has one."""

    def __init__(self, path: str | os.PathLike[str]):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.seq, self.prev_hash = self._tail()
        self._fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)

    def _tail(self) -> tuple[int, str]:
        if not self.path.exists() or self.path.stat().st_size == 0:
            return 0, GENESIS
        last: dict[str, Any] | None = None
        with self.path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    last = json.loads(line)
        if last is None:
            return 0, GENESIS
        return int(last["seq"]), str(last["hash"])

    def append(self, entry: dict[str, Any]) -> dict[str, Any]:
        self.seq += 1
        full = dict(entry)
        full["seq"] = self.seq
        full["prev_hash"] = self.prev_hash
        full["hash"] = entry_hash(self.prev_hash, full)
        os.write(self._fd, (canonical(full) + "\n").encode("utf-8"))
        os.fsync(self._fd)
        self.prev_hash = full["hash"]
        return full

    def close(self) -> None:
        try:
            os.close(self._fd)
        except OSError:
            pass


@dataclass(frozen=True)
class VerifyResult:
    ok: bool
    entries: int
    line: int | None = None
    error: str | None = None

    def __str__(self) -> str:
        if self.ok:
            return f"OK {self.entries} entries, chain intact"
        return f"FAIL at line {self.line}: {self.error}"


def read_entries(path: str | os.PathLike[str]) -> list[tuple[int, dict[str, Any]]]:
    out: list[tuple[int, dict[str, Any]]] = []
    with Path(path).open("r", encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, 1):
            if line.strip():
                out.append((lineno, json.loads(line)))
    return out


def verify(path: str | os.PathLike[str]) -> VerifyResult:
    try:
        rows = read_entries(path)
    except (OSError, ValueError) as e:
        return VerifyResult(False, 0, None, f"cannot read log: {e}")
    prev = GENESIS
    expected_seq = 1
    run_counts: dict[str, int] = {}
    for lineno, e in rows:
        if e.get("seq") != expected_seq:
            return VerifyResult(False, expected_seq - 1, lineno, f"seq {e.get('seq')} but expected {expected_seq} (missing or reordered entry)")
        if e.get("prev_hash") != prev:
            return VerifyResult(False, expected_seq - 1, lineno, "prev_hash does not match the previous entry")
        if entry_hash(prev, e) != e.get("hash"):
            return VerifyResult(False, expected_seq - 1, lineno, "hash does not match entry content (edited)")
        rid = e.get("run_id", "")
        run_counts[rid] = run_counts.get(rid, 0) + 1
        if e.get("kind") == "event" and e.get("event") == "run-end":
            if e.get("entries") != run_counts[rid]:
                return VerifyResult(False, expected_seq - 1, lineno, f"run-end claims {e.get('entries')} entries for run {rid}, found {run_counts[rid]} (truncated)")
        prev = e["hash"]
        expected_seq += 1
    return VerifyResult(True, expected_seq - 1)


def report(path: str | os.PathLike[str]) -> dict[str, Any]:
    rows = [e for _, e in read_entries(path)]
    calls = [e for e in rows if e.get("kind") == "call"]
    per_tool: dict[str, dict[str, Any]] = {}
    for e in calls:
        t = per_tool.setdefault(e["name"], {"calls": 0, "decisions": {}, "errors": 0, "latencies": []})
        t["calls"] += 1
        t["decisions"][e["decision"]] = t["decisions"].get(e["decision"], 0) + 1
        if e.get("outcome") == "error":
            t["errors"] += 1
        if e.get("latency_ms") is not None:
            t["latencies"].append(e["latency_ms"])
    for t in per_tool.values():
        lat = sorted(t.pop("latencies"))
        t["p50_ms"] = round(statistics.median(lat), 1) if lat else None
        t["p95_ms"] = round(lat[min(len(lat) - 1, int(round(0.95 * len(lat))) - 1 if len(lat) > 1 else 0)], 1) if lat else None
    approvals = [e for e in calls if e["decision"] == "approved"]
    by = {}
    for e in approvals:
        by[e.get("approved_by")] = by.get(e.get("approved_by"), 0) + 1
    refusals = [e for e in calls if e["decision"].startswith("refused")]
    return {
        "entries": len(rows),
        "runs": len({e.get("run_id") for e in rows}),
        "calls": len(calls),
        "approved": {"count": len(approvals), "by": by,
                     "held_ms_total": round(sum(e.get("held_ms") or 0 for e in approvals), 1)},
        "refusals": {"count": len(refusals),
                     "by_decision": {d: sum(1 for e in refusals if e["decision"] == d) for d in sorted({e["decision"] for e in refusals})}},
        "session_flag_runs": sum(1 for e in rows if e.get("kind") == "event" and e.get("event") == "run-start" and e.get("approve_session")),
        "per_tool": dict(sorted(per_tool.items())),
    }


def format_report(r: dict[str, Any]) -> str:
    lines = [
        f"entries {r['entries']}  runs {r['runs']}  calls {r['calls']}",
        f"approved {r['approved']['count']} (by: {r['approved']['by'] or '-'}; held {r['approved']['held_ms_total']} ms total)",
        f"refused  {r['refusals']['count']} {r['refusals']['by_decision'] or ''}".rstrip(),
    ]
    if r["session_flag_runs"]:
        lines.append(f"WARNING: {r['session_flag_runs']} run(s) used --approve-session")
    lines.append("")
    lines.append(f"{'tool':<32} {'calls':>5} {'errors':>6} {'p50 ms':>8} {'p95 ms':>8}  decisions")
    for name, t in r["per_tool"].items():
        p50 = "-" if t["p50_ms"] is None else t["p50_ms"]
        p95 = "-" if t["p95_ms"] is None else t["p95_ms"]
        dec = ", ".join(f"{k} {v}" for k, v in sorted(t["decisions"].items()))
        lines.append(f"{name:<32} {t['calls']:>5} {t['errors']:>6} {p50:>8} {p95:>8}  {dec}")
    return "\n".join(lines)
