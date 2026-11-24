# this_file: hatch_build.py
"""Expose a console script for each bundled integration without importing runtime dependencies."""

import json
from pathlib import Path

from hatchling.metadata.plugin.interface import MetadataHookInterface


class ProviderScripts(MetadataHookInterface):
    def update(self, metadata: dict) -> None:
        package = Path(self.root) / "src/clitwy"
        rows = (package / "openapi/catalogue.tsv").read_text(encoding="utf-8").splitlines()
        names = []
        for row in rows:
            if row.startswith("mcx-"):
                name, _, base, *_ = row.split("\t")
                if base:
                    names.append(name)
        names.extend(json.loads((package / "mcp/catalog.json").read_text()))
        scripts = {
            "tw": "clitwy.cli:main",
            "mcxadmin-build-mcp": "clitwy.builders:mcp_main",
            "mcxadmin-build-openapi": "clitwy.builders:openapi_main",
            "mcxadmin-auth": "clitwy.cli:auth",
            "mcxadmin-oauth": "clitwy.mcp.login:main",
        }
        for alias in [*names, "tw-groups", "tw-doctor"]:
            if alias in scripts:
                raise ValueError(f"Duplicate console script {alias}")
            scripts[alias] = "clitwy.cli:provider_main"
        metadata["scripts"] = scripts
