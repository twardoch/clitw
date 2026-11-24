---
this_file: src_docs/md/operations.md
---

# MCP groups and diagnostics

## Inspect an MCP proxy group

The group commands operate on a local MCPProxy bridge configuration. Pass the directory that contains your own configuration. Use `list` to see each server’s current state and `tag` without arguments to inspect groups.

```sh
tw groups --bridge-dir ./bridge list
tw groups --bridge-dir ./bridge tag
```

Use `scan` to add newly discovered servers to the local tag file. To enable a named group, pass its tag to `on`, for example `tw groups --bridge-dir ./bridge on @research` for a group named `research`.

## Diagnose one installed provider

Use `--kind` and `--only` to narrow the check, and `--timeout` to bound how long it can wait on a provider.

```sh
tw doctor --kind openapi --only archive-wayback-api --limit 1 --timeout 30
```

If shell lookup fails for a `mcx-*` launcher, run `tw run NAME --list --json` using the same `CLITW_HOME` that you used to install it. If an installed command reports HTTP 401 or 403, check the provider's credential variable and account permissions. For a live read with a useful exit status, select the provider's `try` command from the [integration guide](integrations.md).
