# this_file: src/clitw/service_info.py
"""Read service descriptions, prices and local overrides without invoking a provider."""

from __future__ import annotations

import json
import math
import os
import re
from collections import defaultdict
from datetime import date
from importlib.resources import files
from pathlib import Path
from urllib.parse import unquote, urlsplit

import yaml
from platformdirs import user_data_path

from .mcp.config import legacy_aliases as mcp_aliases
from .mcp.config import presets
from .names import resolve_provider
from .openapi.catalogue import legacy_aliases as api_aliases
from .openapi.catalogue import providers
from .paths import bin_dir, registry_dir

TYPED = re.compile(r"mcx-[a-z0-9]+(?:-[a-z0-9]+)*-(api|mcp)([ko]?)")
NAME = re.compile(r"mcx-[a-z0-9]+(?:-[a-z0-9]+)*")
CLI = re.compile(r"(?<![a-zA-Z0-9_-])(?:mcx|tw)-[a-z0-9]+(?:-[a-z0-9]+)*(?![a-zA-Z0-9_-])")
FIELDS = {
    "info",
    "note",
    "tags",
    "url",
    "openapi",
    "docs",
    "price",
    "price_unit",
    "currency",
    "price_model",
    "price_note",
    "price_as_of",
    "pricing_url",
}
PRICING = {"price", "price_unit", "currency", "price_model", "price_note", "price_as_of", "pricing_url"}


def user_metadata_path() -> Path:
    """Location of the optional personal service descriptions, separate from installed provider state."""
    return user_data_path("com.twardoch.clitw", appauthor=False) / "data" / "envinfo.yaml"


def _text(value: object) -> str | None:
    if isinstance(value, str) and (text := value.strip()) and text != "-":
        return text
    return None


def _private_path(path: str) -> bool:
    """A URL from a private configuration can carry credentials in ordinary path components."""
    for _ in range(3):
        decoded = unquote(path)
        if decoded == path:
            break
        path = decoded
    if any(c in path for c in "?#\\") or re.search(r"%[a-f0-9]{2}|[\x00-\x1f\x7f]", path, re.IGNORECASE):
        return True
    for part in path.split("/"):
        if re.search(
            r"(?i)(?:^|[^a-z0-9])(?:sk[-_]|rk[-_]|fc[-_]|hf_|gh[pousr]_|github_pat_|xox[baprs]-|eyJ|AKIA|AIza)[A-Za-z0-9._=-]{8,}",
            part,
        ):
            return True
        if re.fullmatch(r"(?i)api[-_]?key|access[-_]?token|secret|token|credentials?", part):
            return True
        if re.fullmatch(r"[A-Za-z0-9_+=-]{24,}", part) and re.search(r"\d", part) and re.search(r"[A-Za-z]", part):
            return True
    return False


def _url(value: object, *, private: bool) -> str | None:
    if not (text := _text(value)) or any(c.isspace() or ord(c) < 32 or ord(c) == 127 or c in "{}\\$" for c in text):
        return None
    try:
        u = urlsplit(text)
        _ = u.port  # Includes urllib’s validation of explicit ports.
        if (
            u.scheme in {"https", "http"}
            and u.hostname
            and u.username is None
            and u.password is None
            and not u.query
            and not u.fragment
            and (not private or not _private_path(u.path))
        ):
            return text
    except ValueError:
        pass
    return None


def _fields(row: dict, *, private: bool = False) -> dict:
    result = {}
    if "pricing_model" in row and "price_model" not in row:
        row = dict(row, price_model=row["pricing_model"])
    for key in FIELDS & row.keys():
        value = row[key]
        if key in {"url", "openapi", "docs", "pricing_url"}:
            value = _url(value, private=private)
        elif key == "tags":
            if not isinstance(value, list):
                continue
            value = list(dict.fromkeys(t for v in value if (t := _text(v))))
        elif key == "price":
            if isinstance(value, bool) or not isinstance(value, (str, int, float)):
                continue
            try:
                number = float(value)
            except (ValueError, OverflowError):
                continue
            if not math.isfinite(number) or number < 0:
                continue
            value = number
        elif key == "price_as_of":
            try:
                value = date.fromisoformat(str(value)).isoformat()
            except ValueError:
                continue
        else:
            value = _text(value)
        if value is not None:
            result[key] = value
    return result


def _merge(record: dict, changes: dict, source: str) -> None:
    if not changes:
        return
    if source == "user" and ({"price", "price_unit", "currency", "price_model"} & changes.keys()):
        for key in PRICING:
            record.pop(key, None)
            record["sources"].pop(key, None)
    record.update(changes, origin=source)
    record["sources"].update(dict.fromkeys(changes, source))


def _record(name: str, availability: str, origin: str) -> dict:
    kind, auth = TYPED.fullmatch(name).groups()
    service = name[4:].rsplit("-", 1)[0].replace("-", " ")
    return {
        "name": name,
        "kind": kind,
        "auth": {"": "anonymous", "k": "key", "o": "oauth"}[auth],
        "availability": availability,
        "origin": origin,
        "info": f"{kind.upper()} commands for {service}.",
        "tags": [],
        "aliases": [],
        "sources": {"info": origin, "tags": origin},
    }


def _yaml(source, *, missing_ok: bool) -> dict:
    try:
        obj = yaml.safe_load(source.read_text(encoding="utf-8"))
    except FileNotFoundError:
        if missing_ok:
            return {}
        raise ValueError("Service metadata file does not exist.") from None
    except (OSError, TypeError, ValueError, yaml.YAMLError, RecursionError, OverflowError):
        raise ValueError("Cannot read service metadata. Use a YAML mapping of service names to fields.") from None
    if obj is None:
        return {}
    if not isinstance(obj, dict):
        raise ValueError("Service metadata must be a YAML mapping of service names to fields.")  # noqa: TRY004
    return obj


def _register_alias(record: dict, value: object) -> None:
    if isinstance(value, str) and NAME.fullmatch(value) and value != record["name"] and value not in record["aliases"]:
        record["aliases"].append(value)


def _installed(records: dict, variables: dict) -> None:
    for kind, filename in (("api", "openapi.json"), ("mcp", "baked.json")):
        try:
            registry = json.loads((registry_dir() / filename).read_bytes())
        except (FileNotFoundError, ValueError, OSError):
            continue
        if not isinstance(registry, dict):
            continue
        for name, entry in registry.items():
            match = TYPED.fullmatch(name)
            if not match or match[1] != kind or not isinstance(entry, dict):
                continue
            meta = entry if kind == "api" else entry.get("clitw_mcp")
            if not isinstance(meta, dict) or meta.get("hold"):
                continue
            custom = name not in records
            if not custom:
                if kind == "api" and entry.get("customized"):
                    custom = entry.get("base_url") != providers()[name][0].base or not entry.get("preset")
                elif kind == "mcp" and meta.get("config") != "catalog":
                    custom = entry.get("source") != presets()[name]["url"]
            if custom:
                old_aliases = records.get(name, {}).get("aliases", [])
                records[name] = dict(_record(name, "installed", "installed"), aliases=old_aliases)
                # A replacement endpoint does not inherit another service's credential-name matches.
                for matches in variables.values():
                    matches.discard(name)
            rec = records[name]
            rec["availability"] = "installed"
            _register_alias(rec, meta.get("legacy"))
            _register_alias(rec, meta.get("previous_name"))
            if kind == "api" and isinstance(entry.get("auth"), str):
                for var in re.findall(r"env:([A-Z_][A-Z0-9_]*)", entry["auth"]):
                    variables[var].add(name)
            if custom:
                # Private registries can store an opaque credential anywhere in a provider URL.
                description = _text(entry.get("description"))
                if description and "://" not in description:
                    _merge(rec, {"info": description}, "installed")
    folders = [bin_dir(), *(Path(p) for p in os.get_exec_path() if p)]
    for folder in dict.fromkeys(folders):
        try:
            for item in folder.glob("mcx-*"):
                name = item.name
                if os.name == "nt" and item.suffix.lower() in {".exe", ".cmd", ".bat", ".com"}:
                    name = item.stem.lower()
                if TYPED.fullmatch(name) and item.is_file() and os.access(item, os.X_OK):
                    records.setdefault(name, _record(name, "installed", "installed"))["availability"] = "installed"
        except OSError:
            continue


def resolve_service(value: str, catalogue: dict | None = None) -> dict:
    """Look up exact names and historical aliases, reporting all candidates for an ambiguous service."""
    rows = service_catalog() if catalogue is None else catalogue
    key = "mcx-" + value.removeprefix("mcx-").removeprefix("tw-")
    if key in rows:
        return rows[key]
    matches = sorted(name for name, row in rows.items() if key in row.get("aliases", ()))
    if len(matches) == 1:
        return rows[matches[0]]
    if matches:
        raise ValueError(f"Choose a provider for {value}: {', '.join(matches)}")
    return rows[resolve_provider(value, rows)]


def _user_metadata(records: dict, data: dict, variables: dict) -> None:
    edits = []
    rows = {
        outer: (row, _fields(row, private=True))
        for outer, row in data.items()
        if isinstance(outer, str) and isinstance(row, dict)
    }
    for name, (_, fields) in rows.items():
        if fields and TYPED.fullmatch(name):
            records.setdefault(name, _record(name, "user-defined", "user"))
    for outer, (row, fields) in rows.items():
        if not fields:
            continue
        if TYPED.fullmatch(outer):
            edits.append((3, outer, {outer}, fields))
            continue
        if NAME.fullmatch(outer):
            try:
                targets = {resolve_service(outer, records)["name"]}
            except ValueError:
                continue
            edits.append((2, outer, targets, fields))
            continue
        if not re.fullmatch(r"[A-Z_][A-Z0-9_]*", outer):
            continue
        commands = row.get("cli")
        texts = commands if isinstance(commands, list) else [commands]
        targets = set()
        for text in texts:
            for token in CLI.findall(text) if isinstance(text, str) else ():
                try:
                    targets.add(resolve_service(token, records)["name"])
                except ValueError:
                    continue
        if targets:
            edits.append((2, outer, targets, fields))
        elif len(variables.get(outer, ())) == 1:
            edits.append((1, outer, variables[outer], fields))
    for _, _, targets, fields in sorted(edits, key=lambda e: (e[0], e[1])):
        for name in targets:
            _merge(records[name], fields, "user")


def service_catalog(path: Path | None = None, *, include_user: bool = True, include_installed: bool = True) -> dict:
    """Assemble public definitions, optional installed bindings and user overrides entirely offline."""
    records = {}
    variables = defaultdict(set)
    for name, (tool, probe) in providers().items():
        rec = records[name] = _record(name, "bundled", "bundled")
        _merge(rec, _fields({"info": tool.description, "url": tool.base, "openapi": tool.source}), "bundled")
        for var in re.findall(r"env:([A-Z_][A-Z0-9_]*)", tool.auth + "," + probe.auth):
            variables[var].add(name)
    for name, row in presets().items():
        rec = records[name] = _record(name, "bundled", "bundled")
        _merge(rec, _fields({"info": row["description"], "url": row["url"], "docs": row.get("docs")}), "bundled")
        if row.get("auth_env"):
            variables[row["auth_env"]].add(name)
    for aliases in (api_aliases(), mcp_aliases()):
        for old, name in aliases.items():
            _register_alias(records[name], old)
    for name, row in _yaml(files("clitw").joinpath("services.yaml"), missing_ok=False).items():
        if name in records and isinstance(row, dict):
            _merge(records[name], _fields(row), "bundled")
    if include_installed:
        _installed(records, variables)
    if include_user:
        data = _yaml(Path(path) if path is not None else user_metadata_path(), missing_ok=path is None)
        _user_metadata(records, data, variables)
    return dict(sorted(records.items()))
