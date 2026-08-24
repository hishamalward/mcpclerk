# mcpclerk

[![ci](https://github.com/hishamalward/mcpclerk/actions/workflows/ci.yml/badge.svg)](https://github.com/hishamalward/mcpclerk/actions/workflows/ci.yml)
![python](https://img.shields.io/badge/python-3.10%2B-blue)
![license](https://img.shields.io/badge/license-MIT-green)

A governance proxy for MCP servers: sits in front of any MCP server, enforces a per-tool allowlist, holds write-class tools for human approval, applies per-tool quotas, redacts secret-looking arguments, and writes a hash-chained audit log of every call.

An AI agent on MCP servers can call any tool they expose, as often as it likes, with any arguments, and nothing records what it did in a form anyone can audit. In an enterprise the question is not "can the agent do the job" but **what is it allowed to do, who approved the dangerous parts, and what did it actually do?**

mcpclerk answers those three with code. It is itself an MCP server: the agent connects to it, it connects to the real servers and re-exposes their tools as `upstream.tool`. Every call goes through one pipeline: allowlist, quota, redaction, approval, forward, log. An unlisted tool is denied. A write-class tool waits for a human to answer `y`. Refusals come back as readable errors. The log is append-only JSON Lines, each entry hashed with the previous one, so an edit anywhere breaks the chain.

The demo wraps the official filesystem server: a read passes, a write is held and approved, a move is refused, the fourth search in a minute is refused on quota, and the log verifies. 49 tests prove each control against a fake upstream, including that the upstream always receives the unredacted arguments.

![demo](examples/demo.gif)

## Install

```bash
pip install mcpclerk          # Python 3.10+ (the MCP SDK requires it); pulls in mcp and pyyaml
mcpclerk --version
```

From source: `git clone https://github.com/hishamalward/mcpclerk && cd mcpclerk && pip install -e ".[dev]" && pytest`.

## Five minutes

1. Write a policy. This is the one from the demo (`examples/policy.filesystem.yaml`):

   ```yaml
   version: 1
   defaults:
     unlisted: deny              # a tool not named here is an unreviewed tool
     approval_timeout_s: 120     # a call nobody answers in time is refused, and logged as such
   upstreams:
     fs:
       transport: stdio
       command: npx
       args: ["-y", "@modelcontextprotocol/server-filesystem", "/tmp/mcpclerk-demo-sandbox"]
       tools:
         "read_*": allow
         list_directory: allow
         search_files: { decision: allow, quota: { per_minute: 3 } }
         write_file: approve
         edit_file: approve
         create_directory: approve
         move_file: deny         # the filesystem server has no delete; move is its destructive op
   ```

2. See what the upstream offers and what your policy does with it. The server's own annotations are shown next to your decision, which is how you notice you allowed a destructive tool:

   ```
   $ mcpclerk tools --policy examples/policy.filesystem.yaml
   tool                                 decision rule               read_only destructive quota
   fs.read_file                         allow    glob:read_*        True      None        -/-
   fs.write_file                        approve  exact              False     True        -/-
   fs.move_file                         deny     exact              False     True        -/-
   fs.search_files                      allow    exact              True      None        -/3
   ```

3. Register the proxy where your agent looks for MCP servers. For Claude Code, `examples/.mcp.json`:

   ```json
   { "mcpServers": { "fs-governed": {
       "command": "mcpclerk",
       "args": ["serve", "--policy", "examples/policy.filesystem.yaml"] } } }
   ```

4. In a second terminal, wait for approvals: `mcpclerk approve`. When the agent calls `fs.write_file`, you see the call with secrets already masked, and answer `y` or `n`.

5. Afterwards: `mcpclerk verify audit/mcpclerk.jsonl` and `mcpclerk report audit/mcpclerk.jsonl`.

## The five controls

| Control | What it does | What it prevents | What it cannot prevent | Proven by |
|---|---|---|---|---|
| **Allowlist** | `allow` / `deny` / `approve` per tool, exact name first, then longest glob, then `defaults.unlisted` (deny). Denied and unlisted tools are not even listed to the agent. | The agent using a tool nobody reviewed. | A bad decision in the policy itself. `mcpclerk tools` shows the upstream's read-only / destructive hints next to your decision to make that harder. | `test_policy.py`, `test_pipeline.py::test_denied_hidden_tool_called_by_name_is_refused` |
| **Approval** | `approve`-class calls are held. The request is written to `approvals/<id>.json` with redacted arguments; a human answers with `mcpclerk approve` (or by editing the file, or at a terminal prompt if the proxy has one). Timeout is a refusal. | An unsupervised write. | A human who approves without reading. `--approve-session` exists for that human and is logged on every affected entry. | `test_approval.py`, `test_pipeline.py::test_approve_via_file_then_forward`, `test_approval_refused_and_timed_out` |
| **Quotas** | `per_run` and `per_minute` (sliding window) per tool. Over-quota is refused with the limit and the seconds until the window frees. Refused calls do not consume quota; approved-then-refused-by-human calls do. | Runaway loops; a cheap tool becoming expensive by volume. | Distributing a loop across many tools, or across proxy restarts (`per_run` resets with the process). | `test_quota.py`, `test_pipeline.py::test_quota_exhaustion` |
| **Redaction** | Key rules (`api_key`, `token`, `password`, `authorization`, ...) replace the whole value; value rules (bearer headers, `sk-`/`AKIA`/`ghp_`/`xox` tokens, JWTs, PEM blocks, URL userinfo, `password=...`) replace the match. Applied to what is logged and shown to the human. **The upstream receives the original arguments.** | Secrets landing in the log or on an approver's screen. | A secret shaped like nothing on the list. Extend `redaction.extend` / `extend_keys` for your own shapes. | `test_redact.py`, `test_pipeline.py::test_upstream_receives_unredacted_args` |
| **Audit log** | One JSON Lines entry per call with timestamp, upstream, tool, redacted args, decision, who approved, outcome, latency, and `hash = sha256(prev_hash + canonical(entry))`. `verify` recomputes the chain; `report` summarizes it. | Quiet editing, deletion or reordering of entries after the fact; truncation of a completed run (`run-end` carries the count). | An attacker who rewrites the entire chain from genesis (this is a chain, not a signature; see below). Truncation of a run that was killed mid-way. | `test_audit.py` (edit, delete, reorder, truncate) |

Results are not logged, only their size and content types. The log is an audit of decisions, not a copy of the data; storing results would make it a second place for secrets to leak.

## How a call moves

```
agent ──tools/call fs.write_file──▶ mcpclerk ──▶ [namespace] ──▶ [allowlist] ──▶ [quota] ──▶ [redact for log]
                                                     │               │              │
                                                refused-unknown  refused-denied  refused-quota
                                                                                           │
                                                          ┌── decision = approve ──▶ [hold: approvals/<id>.json] ──▶ y ─┐
                                                          │                                │ n / timeout               │
                                                          │                       refused-by-human / refused-timeout   │
                                                          └── decision = allow ────────────────────────────────────────┤
                                                                                                                       ▼
                                                                          [forward with ORIGINAL args] ──▶ upstream ──▶ result
                                                                                                                       │
                                                                                          [append log entry, hash-chained]
```

Every path, including every refusal, ends in a log entry. Refusals return to the agent as a normal tool result with `is_error: true` and a one-line reason: `mcpclerk: refused-quota fs.search_files: 3/min exhausted; retry after 60s`.

## Approval, in detail

The proxy is usually started by the agent's MCP client, and the MCP SDK starts stdio servers in a new session, so the proxy normally has **no terminal of its own**. That is why the mechanism is a file queue and the terminal prompt is a client of it:

- `approvals/<id>.json` is written for every held call, with the redacted arguments, `requested_at`, `expires_at`, and `"approved": null`.
- `mcpclerk approve` (in any terminal, on the same machine) shows pending requests and writes your answer. `--once` answers one and exits; without it, it keeps watching.
- Editing the file by hand to `"approved": true` works too, which is what a headless job or a script does.
- If the proxy does happen to have a controlling terminal (you started it by hand), it also prompts there. Both paths race; the first answer wins.
- No answer within `approval_timeout_s` is a refusal, logged as `refused-timeout`. Silence on a write means no.
- `serve --approve-session` auto-approves every approve-class call for that process. It prints a warning at start, the `run-start` entry records it, every affected entry says `approved_by: session-flag`, and `report` shouts about it. It cannot be set in the policy file; it is a per-invocation act by whoever starts the process.

## The audit log

```json
{"kind":"call","ts":"2026-08-24T01:14:40.822Z","run_id":"20260824T011440Z-3e1c","id":"20260824T011440Z-0002",
 "name":"fs.write_file","upstream":"fs","tool":"write_file","rule":"exact",
 "args":{"content":"# notes\n[REDACTED:kv-secret]\n","path":"/tmp/mcpclerk-demo-sandbox/notes.md"},
 "decision":"approved","approved_by":"file","held_ms":253.7,"outcome":"ok","is_error":false,
 "latency_ms":7.7,"content_bytes":57,"content_types":["text"],
 "seq":4,"prev_hash":"5c0e…","hash":"b41a…"}
```

- `decision` is one of `allowed`, `approved`, `refused-denied`, `refused-unknown`, `refused-quota`, `refused-timeout`, `refused-by-human`.
- `latency_ms` is upstream time only; the human's thinking time is `held_ms`, so p95 latency in `report` means the tool, not the person.
- Event entries (`run-start` with the policy's SHA-256 and the flags, `discover` with exposed/hidden counts, `run-end` with the entry count) share the same chain.
- `verify` exits 0 with `OK n entries, chain intact` or 1 with `FAIL at line N: <what>`. Try it: `sed -i '' 's/allowed/approved/' examples/audit.demo.jsonl && mcpclerk verify examples/audit.demo.jsonl`.

The example log in `examples/audit.demo.jsonl` is the real output of the demo run. It is safe to publish by construction: the redaction tests are what prove it, and the demo writes a fake API key into a file precisely so the log can show `[REDACTED:kv-secret]` where it would have been.

## CLI

```
mcpclerk serve   --policy policy.yaml [--log audit/mcpclerk.jsonl] [--approvals approvals] [--approve-session] [--no-tty]
mcpclerk approve [--approvals approvals] [--once] [--wait 60]
mcpclerk tools   --policy policy.yaml [--json]
mcpclerk verify  audit/mcpclerk.jsonl
mcpclerk report  audit/mcpclerk.jsonl [--json]
```

Exit codes: 0 ok, 1 verify failed or policy invalid, 2 usage. The policy is validated at startup and any problem (unknown key, bad decision, unset `${ENV_VAR}`, a stdio upstream without `command`) stops the proxy before it serves anything.

## Policy reference

```yaml
version: 1
namespace_separator: "."         # "__" for clients that reject dots in tool names
defaults:
  unlisted: deny                 # allow | deny | approve
  approval_timeout_s: 120
  quota: { per_run: null, per_minute: null }
redaction:
  extend: ['(?i)my[-_ ]?internal[-_ ]?token\s*[:=]\s*\S+']   # value regexes, added to the built-ins
  extend_keys: [client_secret]                                # key names, added to the built-ins
  replace_builtin: false                                      # true: only your patterns (warned about)
upstreams:
  <name>:                        # [a-z0-9_-]+ ; becomes the prefix in <name>.<tool>
    transport: stdio | http
    command: ...   args: [...]   env: { KEY: "${FROM_PROXY_ENV}" }   cwd: ...     # stdio
    url: https://...                                                                # http
    tools:
      <tool or glob>: allow | deny | approve
      <tool>: { decision: approve, quota: { per_run: 10, per_minute: 3 }, approval_timeout_s: 60 }
```

## Prior art, and what this is instead

Gateways for MCP exist and do more than this: [Lasso Security's mcp-gateway](https://github.com/lasso-security/mcp-gateway), [IBM's mcp-context-forge](https://github.com/IBM/mcp-context-forge), and [Docker's MCP Gateway](https://github.com/docker/mcp-gateway) bring registries, multi-tenant auth, plugin pipelines and observability. mcpclerk claims no novelty. It claims smallness and verifiability: a single-purpose, readable, local proxy whose whole surface is the five controls above and a log you can check. It is about 1,000 lines of Python you can read in an afternoon, with one dependency beyond the MCP SDK (a YAML parser).

## What it does not do (yet)

- Identity and per-user policies. One operator is assumed; the log records *that* a human approved, not *which* human.
- A web UI, or remote approval channels (Slack, email). `mcpclerk approve` is a local terminal.
- Policy inheritance or templating across upstreams.
- Resources and prompts. v0.1 proxies tools only; `resources/list` and `prompts/list` are empty.
- HTTP upstreams that need request headers. The SDK's HTTP transport takes none in this version; a policy that sets `headers` fails loudly rather than silently sending nothing.
- Windows: the file queue and `mcpclerk approve` work; the in-process terminal prompt does not (no `/dev/tty`). CI runs Windows as best-effort.

## Threat model, honestly

What an attacker with the agent's seat would try first is to call a tool by name that is hidden from the list. That is refused and logged (`refused-unknown` or `refused-denied`). What this does not stop: a tool that is *allowed* being used for something harmful (the policy is your judgement, mcpclerk enforces it), an approver who rubber-stamps, and anyone with write access to the log file rewriting the whole chain from the first entry. The chain defends against quiet edits, which is the realistic threat; signatures or an external anchor (publishing the daily head hash somewhere you do not control) would be the next step, and are not in v0.1.

## Development

```bash
pip install -e ".[dev]"
pytest -q                                  # 49 tests, all in-process, no network, no subprocesses
python examples/demo_driver.py --approve-via-file   # the demo against the real filesystem server (needs npx)
vhs examples/demo.tape                     # re-record the GIF
```

Tests use the MCP SDK's in-memory transport on both sides: `Client(proxy) → proxy → Client(fake_upstream)`. The fake upstream (`tests/fake_upstream.py`) has a `secret_sink` tool that returns exactly what it received, which is how the suite proves the upstream sees unredacted arguments while the log does not.

Related: [toilscan](https://github.com/hishamalward/toilscan) (the same write-safety instinct applied to a developer tool), [agent-slots](https://github.com/hishamalward/agent-slots) (runtime isolation for parallel agents), and agentkeel (the process side: gates and blast radius for agent-written code; in progress).

For whoever owns this next: [docs/how-it-works.html](docs/how-it-works.html) is the tour (the code in call order, the controls, the interview answers); [docs/spec.md](docs/spec.md) is the contract.

## License

MIT.
