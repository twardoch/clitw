# this_file: src/clitwy/openapi/catalogue.py
"""Read the shipped provider bindings without accessing the network or local configuration."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path


@dataclass(frozen=True)
class Tool:
    name: str
    source: str
    base: str
    auth: str
    description: str


@dataclass(frozen=True)
class Probe:
    method: str
    route: str
    arguments: list[str] | str
    source: str = ""
    base: str = ""
    auth: str = "="


# These provider-specific bearer values are OAuth grants even when supplied by a caller.
_OAUTH_VARIABLES = {"DROPBOX_ACCESS_TOKEN", "EBAY_TOKEN", "RINGCENTRAL_TOKEN", "SLACK_BOT_TOKEN", "ZOOM_ACCESS_TOKEN"}


def auth_kind(binding: str) -> str:
    variables = re.findall(r"env:([A-Z][A-Z0-9_]*)", binding)
    if any("OAUTH" in variable.split("_") or variable in _OAUTH_VARIABLES for variable in variables):
        return "o"
    return "k" if variables else ""


@lru_cache(maxsize=1)
def _rows() -> tuple[list[str], ...]:
    lines = Path(__file__).with_name("catalogue.tsv").read_text(encoding="utf-8").splitlines()
    return tuple(row.split("\t") for row in lines if row and not row.startswith("#"))


@lru_cache(maxsize=1)
def legacy_aliases() -> dict[str, str]:
    return {old: row[0] for row in _rows() for old in row[-1].split()}


@lru_cache(maxsize=1)
def providers() -> dict[str, tuple[Tool, Probe]]:
    result = {}
    for name, src, base, auth, desc, method, path, args, source, target, probe_auth, _ in _rows():
        result[name] = (
            Tool(name, src, base, auth, desc),
            Probe(method, path, json.loads(args), source, target, probe_auth),
        )
    return result


def catalog() -> dict[str, str]:
    """Return the descriptions of the public OpenAPI CLI presets."""
    return {name: tool.description for name, (tool, _) in providers().items()}
