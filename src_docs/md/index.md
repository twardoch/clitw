---
this_file: src_docs/md/index.md
---

# Get started with clitwy

Use `tw` to turn an OpenAPI operation or an MCP tool into a shell command. This example finds an archived snapshot of a public web page. It uses a bundled OpenAPI schema and needs no account.

## Install clitwy

Install the `clitwy` package in a dedicated tool environment. To install from a checkout, run `uv tool install .` in the repository root.

```sh
uv tool install clitwy
tw --help
```

If uv reports a command conflict and your old `clitw` tool came from this project, uninstall it before installing `clitwy`. Run both offline migrations with your previous `CLITW_HOME` setting to update existing provider launchers.

```sh
uv tool uninstall clitw
uv tool install clitwy
tw openapi install --migrate-names
tw mcp install --migrate-names
```

Update Python imports to `clitwy`. Continue using `tw` in shell commands.

For an isolated first run, set the state directory before installing a provider. Keep the same shell open for the following commands.

```sh
export CLITW_HOME="$(mktemp -d)"
tw catalog openapi
tw openapi install mcx-archive-wayback-api
tw run archive-wayback-api --list --json
```

The package installs a shell command for each ready-to-use catalog name, including `mcx-archive-wayback-api`. It shares the installed binding with `tw run archive-wayback-api`. Both forms can install a bundled provider on first use. The explicit install above keeps setup output separate from the JSON command listing.

## Find a provider by task

```sh
tw search archive
tw info mcx-archive-wayback-api
```

Search the service descriptions when you know the task but not the command name. Inspect a match for its tags, service links and available pricing. [Service metadata and personal overlays](integrations.md#service-metadata)

## Look up an archived snapshot

Show the operation's arguments, then ask the Internet Archive about `https://example.com/`. The second command contacts the service.

```sh
tw run archive-wayback-api get-wayback-v1-available --help
tw run archive-wayback-api get-wayback-v1-available --url https://example.com/
```

Look for `archived_snapshots.closest` in the response. When a snapshot exists, `url` is the archived copy and `timestamp` is the capture time. An empty `archived_snapshots` object means the service returned no matching capture.

The same package installation that provides `tw` also puts the provider launcher on your path.

```sh
mcx-archive-wayback-api --list --json
```

[Configure OpenAPI and MCP integrations](integrations.md) · [Use clitwy from Python or an agent](automation.md) · [Manage MCP groups](operations.md)
