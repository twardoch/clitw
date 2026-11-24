---
this_file: CHANGELOG.md
---

# Changelog

## Unreleased

- Named provider commands now identify the interface and authentication method. Offline migrations update owned launchers and their PATH exports.
- Added standalone builders for MCP and OpenAPI provider executables, guided MCP browser login, and an authentication command for the installed inventory. Run `tw auth --list` to inspect OAuth and API key status.

- OpenAPI setup now reports unusable bundled schemas as skipped and continues installing other providers. Bulk setup preserves existing user-configured bindings. Recipes needing a deployment URL require a named install with `--base-url`.
- Removed 12 unusable OpenAPI presets and their package launchers. The catalogue contains 236 defaults and 22 recipes for manually configured deployments.

- Added the `tw` CLI, the `clitw.catalog()` and `clitw.run()` Python interfaces, and named provider commands generated during package builds.
- Ported the MCP and OpenAPI installers and read-only try commands. Both write to a clitw-specific state directory. Bundled catalogues use public provider definitions. Custom servers and account configuration can be supplied at installation or invocation.
- Added the server-group and doctor utilities, the provider skill, and the ProperDocs/MaterialX site.
- Added source and wheel builds with tag-derived versions, a local tag preview command, and offline integration and packaging tests.
