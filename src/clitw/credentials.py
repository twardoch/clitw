# this_file: src/clitw/credentials.py
"""Set up credentials for installed, reviewed API bindings without contacting providers."""

from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import os
import re
import stat
import sys
import warnings
from collections import defaultdict
from functools import lru_cache
from pathlib import Path

from dotenv import dotenv_values

from clitw.names import UnknownProvider, resolve_provider
from clitw.paths import home, install_lock, provider_path, registry_dir


def saved_values() -> dict[str, str]:
    """Read the private credential store, rejecting links and overly broad POSIX permissions."""
    path = home() / "credentials.json"
    if path.is_symlink():
        raise ValueError("The clitw credentials file must be a regular private file.")
    try:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        with os.fdopen(os.open(path, flags), encoding="utf-8") as f:
            info = os.fstat(f.fileno())
            if not stat.S_ISREG(info.st_mode) or (
                os.name == "posix" and (info.st_mode & 0o077 or info.st_uid != os.getuid())
            ):
                raise ValueError("The clitw credentials file must be private (chmod 600).")
            values = json.load(f)
    except FileNotFoundError:
        return {}
    except (UnicodeError, json.JSONDecodeError):
        raise ValueError("Invalid clitw credentials file.") from None
    if not isinstance(values, dict) or not all(isinstance(v, str) for v in values.values()):
        raise ValueError("Invalid clitw credentials file.")
    return values


def available_keys(file: Path | None = None) -> dict[str, str]:
    """Exported values override the selected dotenv, which overrides locally saved keys."""
    if file is None and (selected := os.getenv("CLITW_DOTENV")):
        file = Path(selected).expanduser()
    if file and not file.is_file():
        raise ValueError("The selected dotenv file does not exist.")
    values = saved_values()
    explicit = {
        **(dotenv_values(file, interpolate=False) if file else {}),
        **{k: v for k, v in os.environ.items() if v},
    }

    def resolve(key: str, seen: set[str]) -> str:
        if key in seen or len(seen) >= 20:
            return ""
        return re.sub(r"\$\{([A-Za-z_][A-Za-z_0-9]*)\}", lambda m: resolve(m[1], seen | {key}), explicit.get(key) or "")

    values.update({key: resolved for key in explicit if (resolved := resolve(key, set()))})
    return values


def save_key(name: str, value: str) -> None:
    from clitw.openapi.state import atomic

    if not value.strip() or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError("The credential must be a nonempty single-line value.")
    with install_lock():
        values = saved_values()
        values[name] = value
        atomic(home() / "credentials.json", (json.dumps(values) + "\n").encode(), 0o600)


def reviewed_openapi(name: str, entry: dict) -> bool:
    from clitw.openapi.catalogue import providers

    pair = providers().get(name)
    if not pair or entry.get("preset") is not True or entry.get("customized"):
        return False
    tool = pair[0]
    return bool(
        tool.base and entry.get("base_url", "").rstrip("/") == tool.base.rstrip("/") and entry.get("auth") == tool.auth
    )


def binding_variables(auth: str) -> set[str]:
    from clitw.openapi.state import HEADER

    result = set()
    for header in auth.split(","):
        match = HEADER.fullmatch(header)
        if match and match[1].lower() != "user-agent":
            result.update(key for key in match.groups()[2:] if key)
    return result


def oauth_client_variables(name: str, preset: dict) -> dict[str, str]:
    """Only collect client credentials named by both the Google preset and its exact OAuth template."""
    google_endpoints = {
        "mcx-gmail": "https://gmailmcp.googleapis.com/mcp/v1",
        "mcx-google-drive": "https://drivemcp.googleapis.com/mcp/v1",
    }
    expected = google_endpoints.get(name.removesuffix("-mcpo"))
    if not expected or preset.get("url") != expected:
        return {}
    options = preset.get("oauth")
    if not isinstance(options, dict):
        return {}
    return {
        field: variable
        for field, variable in (("client_id", "GOOGLE_MCP_CLIENT_ID"), ("client_secret", "GOOGLE_MCP_CLIENT_SECRET"))
        if options.get(field) == f"${{env:{variable}}}"
    }


@lru_cache(maxsize=1)
def bundled_variables() -> set[str]:
    """Names reserved for the published provider endpoints in the user-wide dotenv."""
    from clitw.mcp.config import presets
    from clitw.openapi.catalogue import providers

    names = set()
    for tool, probe in providers().values():
        names.update(binding_variables(tool.auth), binding_variables(probe.auth))
    for name, preset in presets().items():
        if variable := preset.get("auth_env"):
            names.add(variable)
        names.update(oauth_client_variables(name, preset).values())
    return names


def custom_mcp_value(name: str, conf: dict) -> str | None:
    """A user-pinned custom service may only implicitly load its own distinct dotenv variables."""
    value = os.getenv(name) or (conf.get("environment", {}).get("custom_vars") or {}).get(name)
    file = os.getenv("CLITW_DOTENV")
    if not value and file and name not in bundled_variables():
        return dotenv_values(Path(file).expanduser(), interpolate=False).get(name)
    return value


def installed_keys() -> dict[str, set[str]]:
    from clitw.mcp.config import load, presets
    from clitw.mcp.install import owned
    from clitw.openapi.catalogue import providers
    from clitw.openapi.state import registry

    result = {}
    for name, entry in registry().items():
        if not isinstance(entry, dict) or not reviewed_openapi(name, entry):
            continue
        launcher = provider_path(name)
        if launcher.is_symlink() or not launcher.is_file() or launcher.stat().st_size > 16384:
            continue
        if hashlib.sha256(launcher.read_bytes()).hexdigest() == entry.get("wrapper_sha256"):
            tool, probe = providers()[name]
            result[name] = binding_variables(tool.auth) | binding_variables(probe.auth)
    baked = registry_dir() / "baked.json"
    if baked.is_file():
        catalogue = presets()
        for name, entry in load(baked).items():
            if not isinstance(entry, dict) or not isinstance(entry.get("clitw_mcp"), dict):
                continue
            preset = catalogue.get(name, {})
            meta = entry.get("clitw_mcp") or {}
            variables = set(oauth_client_variables(name, preset).values())
            if preset.get("auth_mode") == "key" and (variable := preset.get("auth_env")):
                variables.add(variable)
            if (
                preset
                and meta.get("config") == "catalog"
                and not meta.get("hold")
                and meta.get("mode") == "direct"
                and entry.get("source") == preset["url"]
                and provider_path(name).is_file()
                and owned(name, entry, provider_path(name))
            ):
                result.setdefault(name, set()).update(variables)
    return result


def cli_keys(argv: list[str]) -> int:
    """List credential bindings or offer to save missing keys from a terminal."""
    parser = argparse.ArgumentParser(prog="tw auth keys", description=cli_keys.__doc__)
    parser.add_argument("names", nargs="*", metavar="NAME", help="restrict to named installed providers")
    parser.add_argument(
        "--list", action="store_true", help="show variable names and configuration status without prompting"
    )
    args = parser.parse_args(argv)
    try:
        installed = installed_keys()
        selected = set(installed)
        if args.names:
            from clitw.mcp.config import legacy_aliases as mcp_aliases
            from clitw.openapi.catalogue import legacy_aliases as openapi_aliases

            aliases = {**mcp_aliases(), **openapi_aliases()}
            selected = set()
            for name in args.names:
                try:
                    selected.add(resolve_provider(name, installed, aliases))
                except UnknownProvider:
                    continue
        by_key = defaultdict(list)
        for name in selected:
            for key in installed[name]:
                by_key[key].append(name)
        if not by_key:
            print("No reviewed API-key bindings found for the selected installed providers.")
            return 0
        values = available_keys()
        interactive = not args.list and sys.stdin.isatty() and sys.stderr.isatty()
        for key, names in sorted(by_key.items()):
            configured = bool(values.get(key))
            print(f"{key}\t{'configured' if configured else 'missing'}\t{', '.join(sorted(names))}", flush=True)
            if not configured and interactive:
                with warnings.catch_warnings():
                    warnings.simplefilter("error", getpass.GetPassWarning)
                    value = getpass.getpass(f"Save {key} locally (hidden; Enter skips): ")
                if value:
                    save_key(key, value)
                    print(f"Saved {key}.")
        return 0
    except (EOFError, KeyboardInterrupt):
        print("\nKey setup stopped.", file=sys.stderr)
        return 130
    except getpass.GetPassWarning:
        print("A terminal with hidden input is required to save a key.", file=sys.stderr)
        return 1
    except (ValueError, OSError) as exc:
        print(f"Key setup failed: {exc}", file=sys.stderr)
        return 1
