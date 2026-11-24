---
this_file: src_docs/md/integrations.md
---

# Connect an OpenAPI service or an MCP server

Choose the integration by interface. An OpenAPI command gets its arguments from an API description. An MCP command discovers tools that the running MCP server exposes. A provider can publish both, with different operations and authentication requirements.

| Task | OpenAPI | MCP |
| --- | --- | --- |
| Find catalog names | `tw catalog openapi --json` | `tw catalog mcp --json` |
| See installation options | `tw openapi install --help` | `tw mcp install --help` |
| See probe options | `tw openapi try --help` | `tw mcp try --help` |

## Build a standalone command

```sh
mcxadmin-build-mcp mcx-deepwiki-github-search-mcp
mcx-deepwiki-github-search-mcp --list --compact
mcxadmin-build-openapi mcx-archive-wayback-api
mcx-archive-wayback-api --list --json
```

Provider names end in `-api` or `-mcp`. The final `k` selects key authentication and `o` selects OAuth. A bare suffix identifies a binding without service credentials. Pass `--bin-dir DIR` to place the executables in a directory of your choice. Keep it on your shell's PATH. When a different installation already owns a command of the same name, the builder preserves it. Use a separate directory to run both versions explicitly. Installing with `tw mcp install` or `tw openapi install` keeps the executables under `CLITW_HOME/bin`.

## Authorize installed providers

Run `tw auth --list` for a status check. Run `mcxadmin-auth` or `tw auth` in a terminal to sign in one provider at a time and configure API keys. Each browser login requires your confirmation. A successful tool listing can still leave account operations protected. Pika's authorization check also reads its app list.

```sh
mcxadmin-oauth --list
mcxadmin-oauth mcx-pika-mcpo mcx-runway-mcpo
tw auth keys --list
tw auth keys mcx-openai-apik
tw mcp try mcx-pika-mcpo mcx-runway-mcpo
```

`mcxadmin-oauth`, `tw auth oauth` and `tw mcp login` select the same guided OAuth flow. Use the same `CLITW_HOME` when building, authorizing and running a tool. A provider reporting HTTP 429 needs a later retry. A provider requiring an API key or an administrator's approval may need an action in that provider's account.

Set `CLITW_DOTENV` to the path of an existing dotenv file to reuse matching credentials. Exported environment variables take precedence. Keys entered through the wizard are stored in `CLITW_HOME/credentials.json`, with mode 0600 on POSIX. A configured key still needs verification against its provider. An OpenAPI launcher ending in `-apio` takes an OAuth token obtained from that provider. Use `tw auth keys NAME` to see the expected variable.

Gmail and Google Drive need a registered Google OAuth desktop app. Run `tw auth keys gmail google-drive` to enter the client ID and secret, then `mcxadmin-oauth gmail google-drive` to grant access. Each provider asks for consent separately.

To rename commands from an older installation using its existing schemas, run both offline migrations.

```sh
tw openapi install --migrate-names
tw mcp install --migrate-names
```

## Discover an OpenAPI operation

Install once, then inspect the operation list and its parameters. The Wayback schema ships with clitw. Other entries can fetch a public schema during installation.

```sh
tw openapi install mcx-archive-wayback-api
tw run archive-wayback-api --list --json
tw run archive-wayback-api get-wayback-v1-available --help
tw openapi try mcx-archive-wayback-api
```

A bulk `tw openapi install` reports a skip if a bundled schema cannot be downloaded or exposes no usable operations. It also skips services that need your own API base. For those, use `tw openapi install NAME --base-url URL` with your deployment URL and invoke the result with `tw run NAME`. A failed refresh keeps any previously working binding.

The last command in the example runs the catalog's reviewed check. Read the individual result to tell whether the check exercised the live API, inspected a local schema, or needed credentials. The provider's actual response to your own operation is available through `tw run`.

## Install your own OpenAPI description

Suppose you maintain a service with a status route. Save the following document as `status.json`.

```json
{
  "openapi": "3.0.3",
  "info": {"title": "Status service", "version": "1.0"},
  "paths": {
    "/status": {
      "get": {
        "operationId": "status",
        "responses": {"200": {"description": "Current service status"}}
      }
    }
  }
}
```

Install the local description and inspect it. These commands only need the file. To call `status` itself, you must also have the corresponding server running at the specified base URL.

```sh
tw openapi install --name local-status --spec ./status.json --base-url http://127.0.0.1:8042
tw run local-status --list --json
tw run local-status status --help
```

For a protected service, supply an environment reference at installation, for example `--auth-header 'Authorization:Bearer env:STATUS_API_TOKEN'`. Use `tw run` for a custom integration. The `mcx-*` package entry points cover providers with a configured default endpoint.

## Discover a remote MCP tool

DeepWiki is a remote server that answers questions about public GitHub repositories. Discovery contacts the server, so network availability can affect even a tool listing.

```sh
tw mcp install mcx-deepwiki-github-search-mcp
tw run deepwiki-github-search-mcp --list --json
tw run deepwiki-github-search-mcp read-wiki-structure --help
tw mcp try mcx-deepwiki-github-search-mcp --timeout 30 --jobs 1
```

## Use your own MCP configuration

Supply an HTTPS endpoint and choose a local name, or import your own configuration file. Replace the example URL before installing it.

```sh
tw mcp install --endpoint example https://example.org/mcp
tw mcp install --config ./mcp.json
```

A minimal `mcp.json` uses an `mcpServers` object whose keys name the services.

```json
{
  "mcpServers": {
    "example": {"url": "https://example.org/mcp"}
  }
}
```

Export credentials in the process that invokes the provider. For an MCP setup that reads a dotenv file, opt in explicitly with `CLITW_DOTENV`. Keep that file outside source control. For an OAuth-enabled MCP integration, run `mcxadmin-oauth NAME` to start the provider’s sign-in and verify access. Keep the same `CLITW_HOME` for subsequent commands so they can find the saved state.

Use `tw catalog` when you need the package's bundled inventory. Use `tw run NAME --list --json` when you need the commands available on a particular installed integration.
