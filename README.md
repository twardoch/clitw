---
this_file: README.md
---

# clitwy

`tw` turns MCP servers and OpenAPI descriptions into named command-line tools. Browse the bundled integrations offline, install the ones you use, and pass provider arguments through to mcp2cli.

```bash
uv tool install clitwy
tw catalog openapi
tw catalog mcp --json
tw openapi install mcx-crates-io-apik
tw run crates-io-apik --list --compact
mcx-crates-io-apik get-site-metadata
```

The `mcx-*` console scripts and `tw run` use the same bindings. A bundled provider installs on first use. For a single live check, run `tw openapi try mcx-crates-io-apik` or `tw mcp try NAME` after installation. Use `--help` on either installer or try command for destinations, credentials, report files and custom integrations.

```python
from clitwy import catalog, run

print(catalog("mcp"))
status = run("crates-io-apik", ["--list", "--compact"])
```

Set `CLITW_HOME` to select a separate installation directory. The default is `$XDG_DATA_HOME/clitw` or `~/.local/share/clitw`. Credentials belong in your environment or a configuration file you control. The distributed catalogues contain public service definitions. Bulk OpenAPI installation skips unavailable or invalid schemas and recipes that need a tenant-specific API base. To enable one of those recipes, supply its verified `--base-url` on a named install, then use `tw run NAME`. Swagger 2 schemas use the `swagger2openapi` converter, supplied on PATH or through npm.

The auxiliary commands `tw groups` and `tw doctor` have their own help. Try commands use reviewed reads or discovery.

## Upgrade from the earlier clitw tool

If you installed this project with uv under its former name `clitw` and uv reports a command conflict, remove that tool before installing `clitwy`. Use the same `CLITW_HOME` as before so both offline migrations can update your existing provider launchers.

```sh
uv tool uninstall clitw
uv tool install clitwy
tw openapi install --migrate-names
tw mcp install --migrate-names
```

Change Python imports from `clitw` to `clitwy`. Shell scripts can keep calling `tw`.

## Find a service

`tw info` shows service descriptions, links, tags and available pricing. `tw search` narrows the list before you install anything. Add `--json` for records keyed by canonical launcher name.

```sh
tw info mcx-openai-apik
tw search archive
tw info mcx-openai-apik --json
tw info --data-path
```

The last command prints the location of your optional personal YAML file. Use it to add notes, tags or costs from your own account. For a price comparison, check the currency, billing unit and source of each amount. [Service metadata and personal overlays](src_docs/md/integrations.md#service-metadata)

## Standalone commands and authentication

`mcxadmin-build-mcp` and `mcxadmin-build-openapi` put individual provider commands on your PATH. Provider names end in `-api` or `-mcp`. The final `k` selects key authentication and `o` selects OAuth. A bare suffix identifies a binding without service credentials. Pass one or more catalog names to build a selection, or omit names for the full inventory. On macOS and Linux the default destination is `~/.local/bin` unless you set `XDG_BIN_HOME`. Use `--bin-dir DIR` to choose another directory. Existing commands from another installation are preserved.

```bash
mcxadmin-build-mcp mcx-deepwiki-github-search-mcp
mcx-deepwiki-github-search-mcp --list --compact
mcxadmin-build-openapi mcx-archive-wayback-api
mcx-archive-wayback-api get-wayback-v1-available --url https://example.com/
```

Run `mcxadmin-auth` or `tw auth` in a terminal to authorize installed providers. It offers browser sign-in for eligible MCP servers and prompts privately for missing API keys. A provider still controls which accounts, subscriptions and permissions it accepts.

```bash
tw auth --list               # Check OAuth and key status without prompting
mcxadmin-oauth                    # Browser sign-ins only, one provider at a time
mcxadmin-oauth mcx-pika-mcpo mcx-runway-mcpo        # Select installed MCP providers
tw auth keys mcx-openai-apik  # Configure a single provider's API key
tw mcp try mcx-pika-mcpo mcx-runway-mcpo       # Verify the connection after signing in
```

## Development

```bash
uv run tw --help
uv run pytest
uv build
uv run --group docs properdocs build --strict -f src_docs/mkdocs.yaml
```

Agent instructions live in `skills/`. Documentation sources live in `src_docs/md/` and the built site in `docs/`. The package uses hatch-vcs for tag-derived versions. `scripts/auto-gittag-semver` previews the next tag. Its `--apply` option creates that tag locally on a clean commit. clitwy uses [mcp2cli](https://github.com/knowsuchagency/mcp2cli) and follows the [CLI-Anything](https://github.com/HKUDS/CLI-Anything) practice of verifying a tool through its real entry point.
