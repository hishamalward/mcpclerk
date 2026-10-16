# Commands

MCPClerk reads and edits native MCP configuration for Claude Code and Codex. `list` does not start servers. It reports configured enablement and provenance, not effective host availability, project trust, managed policy, session flags, or live connections.

## Inspect and manage servers

```sh
mcpclerk list [--host claude|codex] [--project PATH]
mcpclerk add NAME --host claude|codex --scope SCOPE --file server.json [--project PATH]
mcpclerk remove NAME --host claude|codex --scope SCOPE [--project PATH]
mcpclerk enable NAME --host claude|codex --scope SCOPE [--project PATH] [--plugin CODEX_PLUGIN_ID]
mcpclerk disable NAME --host claude|codex --scope SCOPE [--project PATH] [--plugin CODEX_PLUGIN_ID]
```

Choose `SCOPE` for the host and command:

| Host | `add`, `remove`, `tool` | `enable`, `disable` |
| --- | --- | --- |
| Claude Code | `user`, `project`, `local` | `project` |
| Codex | `user`, `project` | `user`, `project` |

`local` is Claude-only; Codex rejects it. The `tool` command below uses the same scope table.

`server.json` contains one native server object with exactly one nonempty `command` or HTTP(S) `url`. The `project` and `local` scopes require an existing absolute `--project` directory. Claude `enable`/`disable` requires `project` scope and updates that project's enable/disable settings; approval of project `.mcp.json` entries remains a separate Claude Code setting. Claude `local` scope writes project-keyed server entries to the Claude registry or tool rules to `.claude/settings.local.json`, depending on the command. It does not edit installed plugin manifests. Codex plugin server definitions remain plugin-owned; `--plugin` selects a Codex plugin's enablement or tool policy. These commands change configuration files, not live server processes.

## Record local operation policy

```sh
mcpclerk tool NAME TOOL --mode allow|ask|deny|clear --host claude|codex \
  --scope SCOPE [--project PATH] [--plugin CODEX_PLUGIN_ID]
mcpclerk catalog --scope user|project --file catalog.json [--project PATH]
mcpclerk migrate-keel --scope user|project --file old-policy.json [--project PATH]
```

`tool` records native host allow/ask/deny settings; the host decides how those settings take effect. `catalog` replaces the selected MCPClerk user catalog or project `mcpclerk.json` layer, which the judge reads to classify operations and check targets. The two policies are separate. `migrate-keel` copies only MCP catalog policy when explicitly requested; it does not run automatically. Catalog data layers the shipped operation catalog, user catalog and project `mcpclerk.json`; project entries can add risk and narrow targets. Codex project trust is still required, and inherited managed denies or wildcard restrictions may narrow a local allow.

For Keel 0.10.0, migrate the user catalog and each project's `agentkeel.json` explicitly. Review the new catalog files, preserve the originals for rollback, then remove the legacy `mcp` and `guards.mcp_unknown` entries and the old user catalog from Keel's active locations. Keel refuses intercepted MCP calls while those legacy settings remain, so an omitted migration cannot silently weaken policy. MCPClerk does not delete the source files. The unknown-call log and `task.py mcp draft` convenience command are retired; catalog changes now have one explicit entry point.

`judge` reads one JSON object from standard input. It checks the tool operation against MCPClerk's catalog, declared task permissions, target policy and, for PostHog, the project pin in the selected host configuration. It does not inspect the live host tool policy or invoke the server:

```sh
printf '%s\n' '{"version":1,"tool":"mcp__revenuecat__list_apps","args":{},"host":"claude","cwd":".","project_dir":null,"task":{"permissions":[]}}' | mcpclerk judge
```

Read operations are allowed. Unknown operations are blocked. Remote writes, paid jobs, publishing and store submissions require the corresponding task permission (`remote-write`, `paid-job`, `publish`, or `store-submission`) and must satisfy project target policy. Unknown servers follow the configured `pass`, `warn`, or `block` mode; when unset, the mode is `pass`. The result includes a reason and classification, never the input arguments.

## Undo a configuration write

Server, tool, catalog and migration changes report a transaction ID for their private backup:

```sh
mcpclerk undo TRANSACTION
```

Undo restores the prior bytes only if the target still matches the content MCPClerk wrote. If a person or host has edited it since, MCPClerk preserves the current file and refuses the undo.

## State locations

- User operation catalog: `~/.mcpclerk/mcp_catalog.json` (or `$MCPCLERK_HOME/mcp_catalog.json`).
- Project operation catalog and unknown-server mode: `mcpclerk.json` in that project.
- Undo records: `~/.mcpclerk/backups/` (or the configured state home).
- Claude Code and Codex server definitions stay in their native host files.

MCPClerk keeps config inventory output free of commands, URLs, environment values and headers. Review native files directly when you need to inspect those details.
