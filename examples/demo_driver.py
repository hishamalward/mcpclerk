"""Drive the demo: an MCP client talking to mcpclerk over stdio, mcpclerk talking to the real
filesystem server. Every line printed is a real protocol round trip.

    python examples/demo_driver.py &               # then, in the same or another terminal:
    mcpclerk approve --once                        # answer the held write_file
    python examples/demo_driver.py --approve-via-file   # headless: this script answers the file queue itself
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from pathlib import Path

import anyio
from mcp.client import Client
from mcp.client.stdio import StdioServerParameters, stdio_client

ROOT = Path(__file__).resolve().parent.parent
SANDBOX = Path("/tmp/mcpclerk-demo-sandbox")  # no home directory in the example log
REL = str(SANDBOX)
OK, NO, HOLD = "✓", "✗", "⏸"


def show(mark: str, name: str, result) -> None:
    text = result.content[0].text if result.content else ""
    text = text.replace("\n", " ")
    print(f"  {mark} {name:<22} {text[:88]}")


async def approve_via_file(approvals: Path) -> None:
    """Stand in for the human: wait for the held call to appear, set approved: true."""
    for _ in range(600):
        files = sorted(approvals.glob("*.json")) if approvals.exists() else []
        for f in files:
            rec = json.loads(f.read_text())
            if rec.get("approved") is None:
                rec["approved"] = True
                rec["note"] = "approved by demo_driver --approve-via-file"
                f.write_text(json.dumps(rec, indent=2))
                print(f"  (file queue) wrote approved: true to {f.relative_to(ROOT)}")
                return
        await anyio.sleep(0.1)


async def main(a: argparse.Namespace) -> int:
    if SANDBOX.exists():
        shutil.rmtree(SANDBOX)
    SANDBOX.mkdir(parents=True)
    (SANDBOX / "hello.txt").write_text("hello from the demo sandbox\n")
    approvals = ROOT / a.approvals

    cmd = [sys.executable, "-m", "mcpclerk", "serve", "--policy", a.policy, "--log", a.log, "--approvals", a.approvals]
    if a.approve_via_file:
        cmd.append("--no-tty")
    params = StdioServerParameters(command=cmd[0], args=cmd[1:], cwd=str(ROOT), env=dict(os.environ))
    print(f"$ mcpclerk serve --policy {a.policy}")
    async with Client(stdio_client(params)) as c:
        tools = sorted(t.name for t in (await c.list_tools()).tools)
        print(f"  exposed {len(tools)} tools (move_file is not among them): {', '.join(tools[:4])}, ...")

        r = await c.call_tool("fs.list_directory", {"path": REL})
        show(OK if not r.is_error else NO, "fs.list_directory", r)

        print(f"  {HOLD} fs.write_file          held for approval ...")
        async with anyio.create_task_group() as tg:
            if a.approve_via_file:
                tg.start_soon(approve_via_file, approvals)
            r = await c.call_tool("fs.write_file", {"path": f"{REL}/notes.md",
                                                    "content": "# notes\napi_key=sk-demo1234567890abcdefghijklmnop\n"})
        show(OK if not r.is_error else NO, "fs.write_file", r)

        r = await c.call_tool("fs.move_file", {"source": f"{REL}/notes.md", "destination": f"{REL}/gone.md"})
        show(NO if r.is_error else OK, "fs.move_file", r)

        for i in range(4):
            r = await c.call_tool("fs.search_files", {"path": REL, "pattern": "*.md"})
            show(NO if r.is_error else OK, f"fs.search_files #{i + 1}", r)

    print(f"$ mcpclerk verify {a.log}")
    sys.stdout.flush()
    os.system(f"{sys.executable} -m mcpclerk verify {a.log}")
    return 0


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--policy", default="examples/policy.filesystem.yaml")
    p.add_argument("--log", default="audit/mcpclerk.jsonl")
    p.add_argument("--approvals", default="approvals")
    p.add_argument("--approve-via-file", action="store_true")
    sys.exit(anyio.run(main, p.parse_args()))
