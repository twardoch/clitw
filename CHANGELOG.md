---
this_file: CHANGELOG.md
---

# Changelog

## 1.0.2

- Tagged web research providers so the documented `tw search --tag research` example returns matches in a clean installation.

## 1.0.1

- Added offline `tw info` and `tw search` for humans and coding agents. Service records include descriptions, tags, documentation and available provider pricing. Dated quotes identify the billing unit and source.
- Added per-user service metadata in the platform data directory. Existing envinfo inventories can supply notes, tags and personal costs. JSON output identifies the source of each field.
- Retired the discontinued LMNT speech-generation service.

- Corrected OAuth client authentication during both code exchange and refresh. A provider that uses HTTP Basic receives the client credentials once.
- Raised the event limit for Higgsfield discovery to 8 MiB. Guided login now reports a saved grant separately when discovery still needs verification.

- Named provider commands now identify the interface and authentication method. Offline migrations update owned launchers and their PATH exports.
- Added standalone builders for MCP and OpenAPI provider executables, guided MCP browser login, and an authentication command for the installed inventory. Run `tw auth --list` to inspect OAuth and API key status.

- OpenAPI setup now reports unusable bundled schemas as skipped and continues installing other providers. Bulk setup preserves existing user-configured bindings. Recipes needing a deployment URL require a named install with `--base-url`.
- Removed 12 unusable OpenAPI presets and their package launchers. The catalogue contains 235 defaults and 22 recipes for manually configured deployments.

- Added the `tw` CLI, the `clitw.catalog()` and `clitw.run()` Python interfaces, and named provider commands generated during package builds.
- Ported the MCP and OpenAPI installers and read-only try commands. Both write to a clitw-specific state directory. Bundled catalogues use public provider definitions. Custom servers and account configuration can be supplied at installation or invocation.
- Added the server-group and doctor utilities, the provider skill, and the ProperDocs/MaterialX site.
- Added source and wheel builds with tag-derived versions, a local tag preview command, and offline integration and packaging tests.
