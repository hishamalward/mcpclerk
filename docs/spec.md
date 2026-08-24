# mcpclerk — specification (v0.1, pre-code)

Status: **draft for approval. No code exists yet.** Written 2026-08-23 against the MCP Python SDK 2.0.0 (Python ≥ 3.10).

## 0. Name

Three candidates were considered on 2026-08-23:

| Candidate | Why it fit | Why not |
|---|---|---|
| `toolclerk` | A clerk checks the list, asks before anything risky, keeps the ledger — all three questions in one word. | Nothing says MCP; the ecosystem searches for "mcp". |
| `heldcall` | Names the signature behaviour: the call is held for a human. | Says nothing about the allowlist, quotas or the ledger. |
| **`mcpclerk`** | Same clerk metaphor with MCP in the name for discoverability. Free on PyPI; no GitHub repo carries the name. | The `mcp-` prefix usually means "an MCP server"; this is the layer in front of them. Accepted. |

**Chosen: `mcpclerk`.** CLI: `mcpclerk serve | tools | verify | report`. Package: `mcpclerk`.

## 1. Problem

An AI agent connected to MCP servers can call any tool those servers expose, as often as it likes, with whatever arguments it decides, and nothing records what it did in a form anyone can audit later. The enterprise question is not "can the agent do the job" but three others: **what is it allowed to do, who approved the dangerous parts, and what did it actually do.**

`mcpclerk` is a small MCP server that sits between the agent and the real MCP servers and answers those three questions with code.

Two sentences the repo must keep true:

- *A governance proxy for MCP: sits in front of any MCP server, enforces a per-tool allowlist, holds write-class tools for human approval, applies per-tool quotas, redacts secret-looking arguments, and writes a hash-chained audit log of every call.*
- *Demonstrated wrapping a real upstream server, with the proxy's own tests proving each control.*

## 2. Scope

### In (v0.1)

1. The proxy **is itself an MCP server** on stdio. It connects to one or more upstream MCP servers (stdio; Streamable HTTP too, because in SDK 2.0 an HTTP upstream is `Client("https://…")` — one line, so it is in) and re-exposes their tools under a namespaced name `upstream.tool`.
2. A YAML **policy file**: per-upstream, per-tool `allow` / `deny` / `approve`; a default for unlisted tools (deny); per-tool quotas; redaction patterns.
3. **Approval** for `approve`-class tools via a terminal prompt and a file-based queue, with a timeout; timeout is a refusal.
4. **Quotas** per tool, per run and per minute; over-quota is a refusal the agent can read.
5. **Redaction** of secret-looking argument values before anything is logged or shown to a human; the upstream always receives the real arguments.
6. An **append-only, hash-chained JSON Lines audit log**, with `verify` and `report` subcommands.
7. A **demo** wrapping the official filesystem MCP server, recorded as a GIF, with its log kept in `examples/`.
8. Hygiene as ToilScan: MIT, README first 200 words problem → approach → result, CI on macOS and Linux (Windows: see §11), tests, `.mcp.json` example.

### Not yet (goes in the README under that heading)

- Identity and per-user policies: a single operator is assumed.
- A web UI.
- Remote approval channels (Slack, email, PagerDuty).
- Policy inheritance or templating across upstreams.
- Anything that requires patching an upstream server.
- Proxying resources and prompts. v0.1 proxies **tools only**; `resources/list` and `prompts/list` return empty. Stated in the README because it is a real gap, not a footnote.

### Prior art (named in the README)

Lasso Security's `mcp-gateway`, IBM's `mcp-context-forge`, and Docker's MCP Gateway all sit in the same place in the stack and do more: multi-tenant registries, auth, plugin pipelines, observability. `mcpclerk` is not a smaller version of those; it is a **single-purpose, readable, local proxy whose whole surface is five controls and a verifiable log**. The claim is smallness and verifiability, not novelty.

## 3. Architecture

```
agent (Claude Code, any MCP client)
   │  stdio (JSON-RPC)
   ▼
mcpclerk serve  ── lowlevel mcp.server.Server(on_list_tools, on_call_tool)
   │   policy.yaml → Policy
   │   per call: allowlist → quota → redact-for-log → approval → forward → log
   │
   ├── Client(stdio_client(StdioServerParameters))   upstream "fs"
   └── Client("https://…/mcp")                        upstream "hosted" (optional)
```

- **Server side:** `mcp.server.lowlevel.Server` with `on_list_tools` and `on_call_tool` handlers, run over `mcp.server.stdio.stdio_server()`. The low-level server is chosen over `MCPServer` (the decorator API) because a proxy does not know its tools at import time; it discovers them.
- **Upstream side:** one `mcp.Client` per upstream, opened for the life of the run. `stdio_client(params)` already satisfies the SDK's `Transport` protocol, so stdio and HTTP upstreams differ only in how the `Client` is constructed.
- **Protocol stdout is sacred.** With stdio transport the proxy's stdout *is* the wire. Every human-facing line (approval prompt, banners, warnings) goes to `/dev/tty` when available, else to stderr. This is a correctness rule, not a style rule; one stray `print()` corrupts the session.

### Namespacing

Exposed name = `<upstream><sep><tool>`, `sep` defaults to `.` (as the brief asks). `policy.namespace_separator` may be set to `__` for clients that reject dots in tool names. The proxy rewrites `name` on the way out and splits on the first separator on the way in. Upstream names must match `^[a-z0-9_-]+$` and must not contain the separator.

### Tool discovery

`tools/list` is passed through with two changes: names are namespaced, and **tools whose decision is `deny` (explicit or by default) are not listed**. Reason: an agent cannot be tempted by a tool it cannot see, and a hidden tool called by name is still refused and logged (`refused-denied`), so hiding costs nothing in enforcement. Discovery is logged once per run as a `discover` event with the counts exposed / hidden per upstream.

## 4. Policy schema

```yaml
# policy.yaml — complete example for the demo
version: 1

namespace_separator: "."           # optional; default "."

defaults:
  unlisted: deny                   # allow | deny | approve — applies to tools not matched below
  approval_timeout_s: 120          # per-call approval wait; timeout is a refusal
  quota:                           # applied where a tool sets none; null = unlimited
    per_run: null
    per_minute: null

redaction:
  extend:                          # added to the built-in patterns (§8); regexes on VALUES
    - '(?i)questrade[-_ ]?refresh[-_ ]?token\s*[:=]\s*\S+'
  extend_keys:                     # added to the built-in KEY names that are always redacted
    - client_secret
  replace_builtin: false           # true = use only the patterns listed here (not recommended)

upstreams:
  fs:
    transport: stdio               # stdio | http
    command: npx
    args: ["-y", "@modelcontextprotocol/server-filesystem", "./demo-sandbox"]
    env: {}                        # values may reference ${VAR} from the proxy's environment; never logged
    tools:
      # exact name, string shorthand
      list_directory: allow
      read_text_file: allow
      get_file_info: allow
      # exact name, full form
      search_files:
        decision: allow
        quota: { per_minute: 30 }
      # write-class: held for a human
      write_file: approve
      edit_file:
        decision: approve
        approval_timeout_s: 60
      create_directory: approve
      # destructive: refused outright (the filesystem server has no delete tool; move is its destructive op)
      move_file: deny
      # glob: longest pattern wins over shorter globs; exact names win over any glob
      "read_*": allow

  hosted:                          # optional second upstream, HTTP
    transport: http
    url: https://example.internal/mcp
    headers:
      Authorization: "Bearer ${HOSTED_TOKEN}"
    tools:
      "*": deny
```

**Resolution order for a tool `t` under upstream `u`:** exact `u.tools[t]` → the longest matching glob in `u.tools` (fnmatch) → `defaults.unlisted`. Per-tool `quota` and `approval_timeout_s` fall back to `defaults`.

**Validation at startup** (fail fast, no serve): unknown keys; a decision outside `allow|deny|approve`; an upstream name containing the separator; a stdio upstream without `command`; an http upstream without `url`; `${VAR}` that is not set in the environment. The policy file's SHA-256 is recorded in the `run-start` log event so a log can be tied to the exact policy that produced it.

## 5. Request pipeline (one `tools/call`)

Order is fixed; each stage either passes the call on or terminates it with a decision that is logged.

| # | Stage | Terminates with | Notes |
|---|---|---|---|
| 1 | **discover** (once per run) | — | list upstream tools; build the exposed set |
| 2 | **namespace** | `refused-unknown` | split `u.t`; unknown upstream or tool → error result, logged |
| 3 | **allowlist** | `refused-denied` | resolve decision (§4) |
| 4 | **quota** | `refused-quota` | per-run counter and 60 s sliding window, checked *before* approval so a human is never asked to approve a call that would be refused anyway |
| 5 | **redact-for-log** | — | produce `args_redacted` (§8); the original `args` object is untouched |
| 6 | **approval** (only if `approve`) | `refused-timeout`, `refused-by-human` | shows `args_redacted` to the human; waits ≤ timeout |
| 7 | **forward** | outcome `error` | `client.call_tool(t, args)` with the **original** arguments |
| 8 | **log** | — | one entry per call, always, whatever happened above |

Every refusal is returned to the agent as a normal `CallToolResult` with `is_error: true` and a one-line text explaining which control refused and why (e.g. `mcpclerk: refused-quota fs.search_files 30/min exhausted; retry after 41s`). Refusals are never silent and never look like upstream errors.

Quota counters are incremented at stage 4 only when the call proceeds past it, so refused calls do not consume quota; a call that is later refused by a human or times out **does** consume quota (it was a real attempt).

## 6. Approval

Two mechanisms run concurrently for each held call; the first answer wins.

**Terminal prompt.** Written to and read from `/dev/tty` (never stdout). Shows: id, `u.t`, redacted args pretty-printed (truncated at 2 KB with a note), the seconds remaining. Keys: `y` approve, `n` refuse, anything else re-prompts. If `/dev/tty` cannot be opened (headless, CI, Windows), the prompt is skipped and the run logs `approval-tty: unavailable` once at start.

**File queue.** On each held call the proxy writes `approvals/<id>.json`:

```json
{
  "id": "20260823T235959Z-0007",
  "upstream": "fs", "tool": "write_file",
  "args_redacted": { "path": "demo-sandbox/notes.md", "content": "…" },
  "requested_at": "2026-08-23T23:59:59.120Z",
  "expires_at":   "2026-08-24T00:01:59.120Z",
  "approved": null,
  "note": ""
}
```

The human edits `approved` to `true` or `false` (and may add a `note`). The proxy polls the file every 500 ms. On resolution the file is rewritten with `resolved_at` and `resolved_by: "file"`; on timeout with `resolved_by: "timeout"`. Files are never deleted by the proxy (they are part of the evidence); the directory is git-ignored.

**Timeout.** Configurable per tool, default 120 s. A timed-out approval is a refusal, decision `refused-timeout`, and the agent gets the same shape of error as any other refusal. Why a refusal and not a retry: the proxy cannot know whether silence means "no" or "not yet", and the safe reading of silence on a write is "no".

**`--approve-session`.** Auto-approves every `approve`-class call for the lifetime of this process. When set: a banner on the tty/stderr at start, a `run-start` event with `approve_session: true`, and every affected entry carries `approved_by: "session-flag"`. There is deliberately no way to set this in the policy file; it is a per-invocation act by whoever starts the process. Per-call approval is the only default.

## 7. Quotas

- `per_run`: calls forwarded during one `serve` process.
- `per_minute`: calls in a sliding 60 s window (deque of timestamps).
- Both keyed on the namespaced tool. Both optional; unset means unlimited.
- Over-quota → `refused-quota`, not forwarded, logged, error text includes the limit and the seconds until the window frees (for per-minute).

## 8. Redaction

Applied to `args` recursively (dict keys and values, list items, nested). Produces `args_redacted`; the original object is never mutated and is what is forwarded. Two rule kinds:

- **Key rules** (case-insensitive key name matches → whole value replaced): `api[_-]?key`, `secret`, `token`, `password`, `passwd`, `authorization`, `bearer`, `cookie`, `private[_-]?key`, `credential`.
- **Value rules** (regex on string values, the match replaced): `Bearer\s+[A-Za-z0-9._~+/-]+=*`; `sk-[A-Za-z0-9]{20,}`; `AKIA[0-9A-Z]{16}`; `gh[pousr]_[A-Za-z0-9]{30,}`; `xox[baprs]-[A-Za-z0-9-]{10,}`; JWT `eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}`; `-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----`; `(?i)(password|passwd|secret|token)\s*[:=]\s*\S+` (catches secrets embedded in file contents and connection strings); URL userinfo `://[^/\s:]+:[^@\s]+@`.

Replacement text is `[REDACTED:<rule-name>]` so the log shows *that* something was hidden and *which rule* hid it. Users extend via `redaction.extend` / `extend_keys`; `replace_builtin: true` exists for people who know what they are doing and is warned about at startup.

**What is logged from results:** nothing of the content. The entry records `outcome`, `is_error`, `content_bytes`, `content_types`. The log is an audit of decisions, not a copy of the data; storing results would turn it into a second place secrets can leak. Stated in the README.

## 9. Audit log

Append-only JSON Lines at `--log` (default `audit/mcpclerk.jsonl`), opened with `O_APPEND`, one `fsync` per entry. Two entry classes share a chain: **events** (`run-start`, `discover`, `approval-tty`, `run-end`) and **calls**.

Call entry:

```json
{
  "seq": 12,
  "kind": "call",
  "ts": "2026-08-23T23:59:59.163Z",
  "run_id": "20260823T235930Z-4f1a",
  "id": "20260823T235959Z-0007",
  "upstream": "fs",
  "tool": "write_file",
  "name": "fs.write_file",
  "args": { "path": "demo-sandbox/notes.md", "content": "[REDACTED:kv-secret]" },
  "decision": "approved",
  "approved_by": "terminal",
  "outcome": "ok",
  "is_error": false,
  "content_bytes": 61,
  "latency_ms": 41.2,
  "prev_hash": "e3b0c442…",
  "hash": "9f86d081…"
}
```

- `decision` ∈ `allowed | approved | refused-denied | refused-unknown | refused-quota | refused-timeout | refused-by-human`.
- `approved_by` ∈ `terminal | file | session-flag` (present only when `decision = approved`).
- `outcome` ∈ `ok | error | not-forwarded`.
- `latency_ms` is forward time only (approval wait is reported separately as `held_ms`), so p95 latency in `report` means upstream latency, not how long the human took.

**Hash-chain rule.** `hash = SHA-256( prev_hash + "\n" + canonical(entry minus "hash") )`, where `canonical` is `json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)`. The first entry's `prev_hash` is 64 zeros. `seq` starts at 1 and increments by 1.

`verify` recomputes every hash and checks `seq` continuity; it exits 0 with `OK n entries`, or 1 with the first failing line number and which check failed. `run-end` carries `entries: n`, so truncation of a completed run's tail is detected too; truncation of a run that was killed mid-way is *not* detectable, and the README says so. The threat model is quiet editing by someone with write access to the file, not forgery by an outsider who can rewrite the whole chain — hence a chain, not signatures.

## 10. CLI

```
mcpclerk serve  --policy policy.yaml [--log PATH] [--approvals DIR] [--approve-session] [--no-tty]
mcpclerk tools  --policy policy.yaml            # connect to upstreams, list every tool with its annotations and the decision the policy gives it; helps write the policy
mcpclerk verify PATH                            # exit 0 = chain intact
mcpclerk report PATH [--json]                   # per-tool calls, decisions, refusals, approvals (by whom), p50/p95 latency, held time
```

`tools` is the one subcommand beyond the brief. It exists because the filesystem server (and any well-behaved upstream) publishes `read_only_hint` / `destructive_hint` annotations, and seeing them next to the policy's decision is how you notice you allowed a destructive tool. It reads; it never changes policy.

Exit codes: 0 ok; 1 verify failed / policy invalid; 2 usage.

## 11. Code layout, dependencies, CI

```
src/mcpclerk/
  __init__.py   __main__.py   cli.py
  policy.py     # schema, validation, resolution (exact > glob > default)
  redact.py     # pure functions; no I/O
  quota.py      # counters and sliding window; pure
  audit.py      # append, hash, verify, report; pure except the file
  approval.py   # tty prompt + file queue + timeout; returns a decision
  upstream.py   # Client construction for stdio / http; discovery; forward
  server.py     # lowlevel Server handlers; the pipeline lives here, in call order
tests/
  fake_upstream.py    # in-process MCPServer with echo / write_thing / delete_thing / secret_sink
  test_policy.py test_redact.py test_quota.py test_audit.py test_approval.py test_pipeline.py
examples/
  policy.filesystem.yaml   demo_driver.py   demo.gif   audit.demo.jsonl   .mcp.json
docs/
  spec.md   learning/how-it-works.md   post-draft.md
```

- **Dependencies:** `mcp>=2.0,<3`, `pyyaml>=6`. Standard library for everything else (`hashlib`, `json`, `re`, `fnmatch`, `collections.deque`, `os`, `time`). Dev: `pytest`, `anyio` (the SDK's own async layer; its pytest plugin runs the async tests — no extra framework).
- **Python:** ≥ 3.10, because the SDK requires it. Said in the README.
- **Testing without a real server:** the SDK's `Client(server_instance)` in-memory transport means the fake upstream and the proxy itself can both be exercised in-process: `Client(proxy_server)` → proxy → `Client(fake_server)`. Tests need no subprocesses and no network.
- **CI:** GitHub Actions on `ubuntu-latest` and `macos-latest`, Python 3.10 and 3.13; `windows-latest` runs the unit tests with `--no-tty` behaviour (there is no `/dev/tty`; the file queue is the approval path on Windows). If Windows is red for an SDK reason, it is marked `continue-on-error` and the README says Windows is best-effort.

## 12. Demo scenario

Upstream: `@modelcontextprotocol/server-filesystem` on `./demo-sandbox`, with `examples/policy.filesystem.yaml` (the example in §4). Driver: `examples/demo_driver.py` speaks to the proxy over stdio with the SDK `Client`, so the GIF shows real protocol traffic, not a mock.

1. `list_directory` → `allowed`, passes through.
2. `write_file notes.md` → held; terminal prompt appears; `y`; `approved (terminal)`; file exists.
3. `move_file notes.md → gone.md` → `refused-denied` (**the filesystem server has no delete tool**; `move_file` is its destructive operation and stands in for "delete" in the demo — flagged for approval below).
4. `search_files` in a loop → the 31st call in the minute → `refused-quota`.
5. `mcpclerk verify audit/mcpclerk.jsonl` → `OK`. Then one byte edited with `sed`, `verify` → `FAIL at line 4`.
6. `mcpclerk report` → the table.

Recorded as a ≤ 30 s GIF with `vhs` (installed locally); the `examples/demo.tape` script is committed so the recording is reproducible. The demo log is copied to `examples/audit.demo.jsonl` and is safe by construction: the redaction tests are what prove it.

A second, shorter demo wraps the `evalmine` MCP server (`run_suite` → `approve` because it spends money; `compare` / `last_report` → `allow`) if time allows; it is the second sentence of §1 made concrete with a server the owner wrote.

## 13. Blast radius

New repository. The proxy writes only under `--log` and `--approvals` (defaults `./audit/`, `./approvals/`); the demo writes under `./demo-sandbox/`. It never modifies the policy file, never touches the upstream's files itself, and never writes to stdout except protocol frames.

## 14. Success criteria (verifiable)

- Unit tests: allow / deny / default-deny; quota exhaustion (per-run and per-minute); approval approved / refused / timed out (terminal and file paths, timeout via a short configured limit); redaction patterns, **and** that the upstream receives unredacted args (fake upstream `secret_sink` returns what it got); hash chain `verify` passes on a clean log and fails on a tampered one (edited line, deleted line, reordered lines).
- Integration test with the fake upstream covering the full pipeline in-process, including the hidden-denied-tool-called-by-name case.
- The demo scenario runs against the real filesystem server and the GIF is recorded from that run.
- CI green on macOS and Linux (Windows best-effort, stated).

## 15. Decisions (ruled 2026-08-23: all five approved)

1. **"Delete refused" is `move_file` refused** in the real demo (the filesystem server has no delete tool); the fake test upstream has a literal `delete_file`, so the deny path is proven on a delete as well.
2. **Denied and unlisted tools are hidden from `tools/list`**; called by name they are still refused and logged.
3. **`mcpclerk tools` ships** as a read-only fourth subcommand.
4. **Namespace separator** defaults to `.`; `__` available via `namespace_separator`.
5. **Results are not logged**; only outcome, size and content types.
