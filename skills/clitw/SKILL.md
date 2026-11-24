---
name: clitw
description: Discover and invoke installed OpenAPI operations and MCP tools through clitw. Use when the user asks to find a provider by capability or tag, compare documented prices or personal costs, inspect arguments, call a tool, or verify the result.
metadata:
  this_file: skills/clitw/SKILL.md
---

# Use a clitw provider

1. Match the user's task with `tw search WORD --json` or `tw search --tag TAG --json`. Inspect the selected name with `tw info NAME --json`. For a list limited to REST or MCP use `tw catalog openapi --json` or `tw catalog mcp --json`. Preserve the canonical name, including its authentication suffix.
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

## Answer a price or metadata question

Read the canonical-name-to-record JSON. Check `price`, `currency`, `price_unit` and `sources.price`. For a bundled public rate, give the documented tier, source link and date when available. Label personal invoice or subscription amounts as the user's own costs. Compare amounts only when the currencies, units and scope match. When only a pricing URL is available, report that link and the unknown amount.

For an existing local metadata file the user wants you to consult, pass its path with `--file`. Use `tw info --data-path` to locate the default. Report only the fields needed to answer the question.

## Troubleshooting

- If the launcher is missing, confirm the install and run use the same `CLITW_HOME`. `tw run` can locate a launcher without changing the shell's `PATH`.
- If MCP discovery succeeds but a call fails, preserve the tool name and exit status. Inspect the operation's help and the provider's authorization requirements.
- For a catalog-specific check, use `tw openapi try NAME` or `tw mcp try NAME`. Distinguish discovery-only results from live operation results. Keep tokens, authorization headers and private response bodies out of reports.
