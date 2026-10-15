# MCPClerk

[![CI](https://github.com/hishamalward/mcpclerk/actions/workflows/ci.yml/badge.svg)](https://github.com/hishamalward/mcpclerk/actions/workflows/ci.yml)
![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue)
![MIT](https://img.shields.io/badge/license-MIT-green)

MCPClerk catalogs MCP operations and manages Claude Code and Codex server configuration in their native files. It can inspect configured sources, add or remove a server, change host enablement, maintain local MCP risk policy, and evaluate an operation against task permissions and target policy.

MCPClerk is a local configuration tool. It does not run an MCP proxy, launch servers to inspect them, or provide an approval, quota, or audit service. A listed configuration entry is not proof that a host has connected to or enabled that server.

**The 0.2.0 candidate is a breaking change from 0.1.0.** The proxy commands and YAML proxy policy format have been removed; there is no automatic conversion. `migrate-keel` is an explicit, reversible copy of MCP catalog policy only.

## Install

MCPClerk requires Python 3.10 or newer. From a checkout:

```sh
python -m pip install .
mcpclerk --help
```

For development, install `.[dev]` and run `python -m pytest`.

## Start here

`list` inspects native configuration without launching MCP processes. Its inventory omits commands, URLs, environment values, and headers:

```sh
mcpclerk list
mcpclerk list --host codex --project /path/to/project
```

Add one native server object from a JSON file to the selected host scope:

```sh
mcpclerk add analytics --host codex --scope user --file server.json
```

Use `mcpclerk tool NAME TOOL --mode allow|ask|deny|clear` to set the host's per-tool policy, and `mcpclerk judge` to evaluate a versioned JSON request from standard input. See the [command guide](docs/commands.md) for host scopes, catalog files, Keel migration, and undo.

Server, tool, catalog and migration changes print a private backup transaction ID and an update note. Keep the ID if you may need to undo the write. MCPClerk preserves native config formatting where supported, and `undo TRANSACTION` restores a prior file only while it still matches the version MCPClerk wrote.

Claude Code and Codex remain the runtime authorities for their native configuration and project trust. MCPClerk can report configured enablement and provenance, but it cannot establish effective availability: managed settings, project trust, session flags, and live connections remain host runtime decisions.

## License

MIT.
