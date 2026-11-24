---
this_file: DEPENDENCIES.md
---

# Dependencies

`mcp2cli` discovers commands and executes provider operations. clitw targets version 3.9.0 because the integration uses its parser, registry and OAuth APIs. The MCP SDK and HTTPX handle protocol and transport respectively. PyYAML reads schema and group configuration. python-dotenv supports explicit credential configuration. FileLock serializes installers sharing the same state directory. On Windows, distlib creates native executables for exported provider commands.

OpenAPI 3 inputs can be used directly. Swagger 2 inputs need the `swagger2openapi` converter or npm to fetch version 7.0.8. The development and documentation dependency groups in `pyproject.toml` provide pytest, Ruff, ProperDocs and MaterialX. Hatchling and hatch-vcs build the package, and the local tag helper calls gitnextver through uv.
