---
name: clitw
description: Discover and invoke installed OpenAPI operations and MCP tools through clitw. Use when the user asks to find a provider, inspect its arguments, call a tool, or verify the result of a provider command.
metadata:
  this_file: skills/clitw/SKILL.md
---

# Use a clitw provider

1. Run `tw --help` and choose an interface. Use `tw catalog openapi --json` for REST and `tw catalog mcp --json` for MCP. Parse the name-to-description mapping and match the requested service.
2. Install the selected record with `tw openapi install NAME` or `tw mcp install NAME`. Use the catalog key, including `mcx-`. For a disposable session, set `CLITW_HOME` to an empty temporary directory and keep that setting across commands.
3. Inspect `tw run NAME --list --json`. Find the operation that implements the requested action, then read `tw run NAME OPERATION --help` to confirm argument names and required values.
4. Execute exactly the requested operation. Pass each argument as a separate string when using Python or a process API. For a paid, destructive or externally visible action, establish the user's intent and relevant limits first.
5. Check the exit status and the result payload. Report the requested fields, such as the capture timestamp or job ID. State whether you verified discovery, a live read, or a completed job. If a job submission times out, check the saved ID before submitting again.

## Example: find a historical copy of a page

```sh
tw openapi install mcx-archive-wayback-api
tw run archive-wayback-api --list --json
tw run archive-wayback-api get-wayback-v1-available --help
tw run archive-wayback-api get-wayback-v1-available --url https://example.com/
```

Read `archived_snapshots.closest.url` and `timestamp` from the result. An empty `archived_snapshots` object means the lookup completed without finding a snapshot.

## Troubleshooting

- If the launcher is missing, confirm the install and run use the same `CLITW_HOME`. `tw run` can locate a launcher without changing the shell's `PATH`.
- If MCP discovery succeeds but a call fails, preserve the tool name and exit status. Inspect the operation's help and the provider's authorization requirements.
- For a catalog-specific check, use `tw openapi try NAME` or `tw mcp try NAME`. Distinguish discovery-only results from live operation results. Keep tokens, authorization headers and private response bodies out of reports.
