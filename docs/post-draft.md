# Post draft (LinkedIn), from the demo

Maps to the "MCP governance" slot in the content pool. Owner edits before posting. Image: the demo GIF's frame where `mcpclerk approve` shows the held write with `[REDACTED:kv-secret]`, or the `verify` + `report` frame.

---

I gave an AI agent a filesystem, then put a clerk between them.

The clerk is an MCP server that sits in front of the real MCP server. The agent sees `fs.read_file`, `fs.write_file`, `fs.search_files`. It does not see `fs.move_file`, because the policy denies it, and a tool nobody reviewed is a tool the agent does not get.

What happened in the demo:

- `list_directory` passed straight through.
- `write_file` was held. In a second terminal I saw the call with the API key it was about to write already masked, typed `y`, and it went through.
- `move_file` was refused with a one-line reason the agent could read.
- The fourth `search_files` in a minute was refused on quota.
- `mcpclerk verify` walked the log: 10 entries, chain intact. I changed one character in line 4 and it failed at line 4.

Every entry in that log carries the hash of the entry before it. Every refusal is an entry. Results are not stored, only what was called, what was decided, and who said yes.

The enterprise question about agents is not "can it do the job". It is: what is it allowed to do, who approved the dangerous parts, and what did it actually do. This is a small, readable answer to those three, and nothing more. Gateways that do more exist; this one is 900 lines you can read in an afternoon and a log you can check with one command.

MIT, Python, the official MCP SDK. Link in the comments.

---

Notes for the edit: the numbers are from `examples/audit.demo.jsonl` and the GIF. No claim of novelty. Prior art is named in the README, not in the post.
