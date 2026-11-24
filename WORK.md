---
this_file: WORK.md
---

# Implementation and verification

## 2026-11-24 — clitwy package rename

Updated installation instructions, Python examples and the agent skill for the `clitwy` package. Existing shell examples use `tw`. Service metadata stays under `com.twardoch.clitw`, and the repository and documentation URLs retain the `clitw` path. Verified `clitwy.catalog`, `clitwy.run` and the bundled pricing example through the renamed source package. The strict ProperDocs build passed. Checked the five generated pages, 68 local asset and navigation links, search index, compressed sitemap and links to the renamed skill.

## 2026-11-24 — Service information and search

Added offline lookup for 295 service bindings across MCP and OpenAPI. Human-readable and JSON output share the same data, including tags, billing units, dated provider rates and per-field provenance. The bundled inventory includes 176 first-party pricing links and 22 numeric rates with billing units, dates and sources. Users can supply their own YAML in the platform data directory or select a file per invocation. Removed the retired LMNT default.

Verified the commands from a wheel built from the source distribution. An isolated install exposed 273 provider executables and 22 additional recipes. Tests exercised zero and unknown prices, ambiguous names, tag and text search, legacy inventories, local quote overrides and discovery of installed executables without running them. The strict documentation build passed. A clean installation check of the documented research filter led to 11 tagged research providers and a regression test.

## 2026-11-24 — Standalone providers and authorization

Provider commands encode the interface and configured authentication. The public inventory includes 38 MCP bindings, comprising 20 OAuth, 14 key and four anonymous bindings. The OpenAPI inventory includes 236 ready-to-use presets and 22 additional configurable recipes. The installers can migrate owned executables and their exported names offline.

Verified standalone MCP and OpenAPI commands from an installed wheel on a restricted PATH. An isolated OAuth resource server and identity provider exercised native browser consent, PKCE, the loopback callback and authenticated discovery from a fresh process. A second resource on the same origin received no credential. The built site, Git inputs and distribution archives were checked for local paths, private source files and known credential values. Key setup was exercised in a pseudo-terminal, including hidden input, origin scoping and owner-only storage.

## 2026-11-24 — Skip unusable APIs

Removed 12 presets whose service had closed, whose API hostname no longer resolved, or whose schema identified a different provider. The default wheel now offers 236 OpenAPI provider launchers and 38 MCP provider launchers. The catalogue also retains 24 recipes that require a deployment URL. Bulk setup skips those unless the user explicitly configures them. A failure to refresh a previously working schema preserves its existing binding.

A live bulk install from an empty state directory completed with exit code 0. It created 236 wrappers and explicitly skipped all 24 recipes requiring a base URL. Verified mixed valid and malformed inputs in an isolated installation. The valid API installed and continued to provide command discovery after an unauthenticated request returned HTTP 401. The malformed schema left no wrapper, registry entry or staged snapshot. The wheel contains no retired or tenant-only aliases.

## 2026-11-24 — Initial implementation

Implemented `tw`, its Python API, custom MCP and OpenAPI installations, provider-specific launchers, read-only probes and the four auxiliary commands. The default state directory is separate from existing MCX and mcp2cli installations. The public package contains reviewed service definitions. Private server configurations and credentials are supplied locally.

| Check | Observed result |
|---|---|
| Clean Python 3.12 installation | A wheel built from the source distribution installed successfully. It defines 315 console scripts, including `tw`, four utilities and 310 provider launchers. |
| Public MCP catalogue | All 38 presets installed in an isolated state directory. A live DeepWiki discovery returned three tools and its selected read passed. |
| Public OpenAPI catalogue | 271 of 272 installed after correcting the Agno spec and the DocuSign resolver. Those installations exposed 28,359 commands. Thirty need an API base for live operations. The remaining IMDbapi.dev preset has an unavailable public schema and accepts a user-supplied file. |
| Live REST | The package returned successful reads from crates.io, the Internet Archive, Cerebras and DocuSign. First use of the crates.io launcher produced parseable JSON containing 53 operations. |
| Offline tests | `uv run --offline pytest -q` passed all 34 tests. Ruff lint and format checks passed. |
| Documentation | The strict offline ProperDocs build succeeded. Browser navigation and search worked, and all 94 local references across five generated pages resolved. |
| Packaging and privacy | The wheel and source distribution contain the project license. The wheel includes the provider skill. Checks found no private checkout directories, local home paths or known credential values in the audited public artifacts. |
| Versioning | hatch-vcs produced a development version from Git. `scripts/auto-gittag-semver` previewed `v0.1.0` using gitnextver. |

Installation and discovery counts describe generated commands. The live checks exercised selected read-only operations. Provider writes and media generation were outside these checks. The install and live results above came from macOS. Windows launchers were not exercised on a Windows host.
