"""Human approval for approve-class tools: a terminal prompt on /dev/tty and a file queue.

Both run in one polling loop so cancellation and timeout are trivial. The prompt never touches
stdout, which is the protocol channel when the proxy serves over stdio.
"""
from __future__ import annotations

import json
import os
import select
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, TextIO, Union

import anyio

POLL_S = 0.25
MAX_PROMPT_CHARS = 2000


@dataclass(frozen=True)
class ApprovalRequest:
    id: str
    upstream: str
    tool: str
    name: str
    args_redacted: Any
    timeout_s: float


@dataclass(frozen=True)
class ApprovalOutcome:
    decision: str  # "approved" | "refused" | "timeout"
    by: str | None  # "terminal" | "file" | None
    note: str = ""


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class Tty:
    """A terminal opened twice, once to read and once to write, because a single "r+" handle on a
    character device is refused by Python's buffered I/O (not seekable)."""

    def __init__(self, reader: TextIO, writer: TextIO):
        self._in = reader
        self._out = writer

    @classmethod
    def open(cls, path: str = "/dev/tty") -> "Tty":
        return cls(open(path, "r", buffering=1), open(path, "w", buffering=1))

    @classmethod
    def from_fd(cls, fd: int) -> "Tty":
        return cls(os.fdopen(os.dup(fd), "r", buffering=1), os.fdopen(os.dup(fd), "w", buffering=1))

    def fileno(self) -> int:
        return self._in.fileno()

    def readline(self) -> str:
        return self._in.readline()

    def write(self, text: str) -> None:
        self._out.write(text)

    def flush(self) -> None:
        self._out.flush()

    def close(self) -> None:
        for f in (self._in, self._out):
            try:
                f.close()
            except OSError:
                pass


def open_tty() -> Tty | None:
    """The controlling terminal, or None when there is none (headless, CI, Windows)."""
    if os.name != "posix":
        return None
    try:
        return Tty.open()
    except OSError:
        return None


def _write_prompt(tty: "Tty", req: ApprovalRequest, remaining: float) -> None:
    body = json.dumps(req.args_redacted, indent=2, ensure_ascii=False)
    if len(body) > MAX_PROMPT_CHARS:
        body = body[:MAX_PROMPT_CHARS] + f"\n... ({len(body) - MAX_PROMPT_CHARS} more chars in approvals/{req.id}.json)"
    tty.write(
        f"\n[mcpclerk] approval needed: {req.name}  (id {req.id}, {remaining:.0f}s left)\n"
        f"{body}\n"
        f"approve? [y/n] "
    )
    tty.flush()


def _tty_answer(tty: "Tty") -> str | None:
    """Non-blocking: returns 'y', 'n', or None if nothing readable yet."""
    try:
        ready, _, _ = select.select([tty], [], [], 0)
    except (ValueError, OSError):
        return None
    if not ready:
        return None
    line = tty.readline().strip().lower()
    if line in ("y", "yes"):
        return "y"
    if line in ("n", "no"):
        return "n"
    tty.write("please answer y or n: ")
    tty.flush()
    return None


async def request_approval(req: ApprovalRequest, approvals_dir: str | os.PathLike[str], *,
                           tty: "Tty | None") -> ApprovalOutcome:
    approvals = Path(approvals_dir)
    approvals.mkdir(parents=True, exist_ok=True)
    path = approvals / f"{req.id}.json"
    start = time.monotonic()
    now = datetime.now(timezone.utc)
    record: dict[str, Any] = {
        "id": req.id,
        "upstream": req.upstream,
        "tool": req.tool,
        "args_redacted": req.args_redacted,
        "requested_at": _iso(now),
        "expires_at": _iso(now + timedelta(seconds=req.timeout_s)),
        "approved": None,
        "note": "",
    }
    path.write_text(json.dumps(record, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    if tty is not None:
        _write_prompt(tty, req, req.timeout_s)

    outcome: ApprovalOutcome | None = None
    while outcome is None:
        elapsed = time.monotonic() - start
        if elapsed >= req.timeout_s:
            outcome = ApprovalOutcome("timeout", None)
            break
        if tty is not None:
            ans = _tty_answer(tty)
            if ans == "y":
                outcome = ApprovalOutcome("approved", "terminal")
            elif ans == "n":
                outcome = ApprovalOutcome("refused", "terminal")
        if outcome is None:
            try:
                current = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                current = None  # mid-edit; try again next tick
            if isinstance(current, dict) and current.get("approved") in (True, False):
                outcome = ApprovalOutcome(
                    "approved" if current["approved"] else "refused", "file", str(current.get("note", "")))
        if outcome is None:
            await anyio.sleep(POLL_S)

    record["approved"] = outcome.decision == "approved"
    record["resolved_at"] = _iso(datetime.now(timezone.utc))
    record["resolved_by"] = outcome.by or "timeout"
    if outcome.note:
        record["note"] = outcome.note
    path.write_text(json.dumps(record, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    if tty is not None:
        tty.write(f"[mcpclerk] {req.name}: {outcome.decision}" + (f" by {outcome.by}" if outcome.by else "") + "\n")
        tty.flush()
    return outcome


def say(msg: str, tty: "Tty | None") -> None:
    """Human-facing line: the tty if there is one, else stderr. Never stdout."""
    out = tty if tty is not None else sys.stderr
    out.write(msg.rstrip("\n") + "\n")
    out.flush()


# ---- the file queue as seen by a second terminal: `mcpclerk approve` --------------------------

def list_pending(approvals_dir: str | os.PathLike[str]) -> list[dict[str, Any]]:
    """Requests nobody has answered yet and that have not expired, oldest first."""
    d = Path(approvals_dir)
    if not d.exists():
        return []
    now = datetime.now(timezone.utc)
    out: list[dict[str, Any]] = []
    for f in sorted(d.glob("*.json")):
        try:
            rec = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(rec, dict) or rec.get("approved") is not None:
            continue
        try:
            expires = datetime.fromisoformat(str(rec.get("expires_at", "")).replace("Z", "+00:00"))
        except ValueError:
            continue
        if expires <= now:
            continue
        rec["_path"] = str(f)
        out.append(rec)
    return out


def answer(path: str | os.PathLike[str], approved: bool, note: str = "") -> None:
    """Write the human's answer into a pending request file (the proxy picks it up within 250 ms)."""
    f = Path(path)
    rec = json.loads(f.read_text(encoding="utf-8"))
    rec["approved"] = approved
    if note:
        rec["note"] = note
    f.write_text(json.dumps(rec, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def approve_loop(approvals_dir: str | os.PathLike[str], *, once: bool, wait_s: float,
                 stdin: TextIO = sys.stdin, stdout: TextIO = sys.stderr) -> int:
    """Prompt for every pending request. Returns the number answered. Blocking; not async on purpose,
    it is a human-facing loop in its own process."""
    answered = 0
    deadline = time.monotonic() + wait_s
    while True:
        pending = list_pending(approvals_dir)
        if not pending:
            if once and answered:
                return answered
            if time.monotonic() >= deadline:
                stdout.write(f"[mcpclerk] no pending approvals in {approvals_dir}\n")
                return answered
            time.sleep(POLL_S)
            continue
        for rec in pending:
            body = json.dumps(rec.get("args_redacted"), indent=2, ensure_ascii=False)
            if len(body) > MAX_PROMPT_CHARS:
                body = body[:MAX_PROMPT_CHARS] + "\n..."
            stdout.write(f"\n[mcpclerk] approval needed: {rec.get('upstream')}.{rec.get('tool')}  (id {rec.get('id')}, expires {rec.get('expires_at')})\n{body}\napprove? [y/n] ")
            stdout.flush()
            line = stdin.readline()
            if not line:
                return answered
            ans = line.strip().lower()
            if ans not in ("y", "yes", "n", "no"):
                stdout.write("please answer y or n\n")
                continue
            answer(rec["_path"], ans in ("y", "yes"))
            stdout.write(f"[mcpclerk] {rec.get('id')}: {'approved' if ans in ('y', 'yes') else 'refused'}\n")
            answered += 1
            if once:
                return answered
        deadline = time.monotonic() + wait_s
