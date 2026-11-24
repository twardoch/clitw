# this_file: src/clitw/api.py
"""Public Python API shared by the console entry points."""

import importlib
import re
import subprocess
import sys
from collections.abc import Sequence
from contextlib import redirect_stdout
from types import ModuleType

from .names import UnknownProvider, resolve_provider
from .paths import bin_dir, provider_argv, provider_path

KINDS = ("openapi", "mcp")


def integration(kind: str) -> ModuleType:
    if kind not in KINDS:
        raise ValueError(f"Unknown integration kind {kind!r}. Choose openapi or mcp.")
    return importlib.import_module(f"clitw.{kind}")


def catalog(kind: str | None = None) -> dict[str, str]:
    """Return bundled integration names and descriptions without contacting providers."""
    entries = {}
    for group in (kind,) if kind else KINDS:
        entries.update(integration(group).catalog())
    return dict(sorted(entries.items()))


def run(name: str, args: Sequence[str] = ()) -> int:
    """Run a named integration, installing its bundled binding on first use. Return its exit code."""
    short = name[4:] if name.startswith("mcx-") else name.removeprefix("tw-")
    if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", short):
        print("Invalid provider name.", file=sys.stderr)
        return 2
    from .mcp.config import legacy_aliases as mcp_aliases
    from .openapi.catalogue import legacy_aliases as api_aliases

    installed = {p.stem for p in bin_dir().glob("mcx-*") if p.is_file()}
    try:
        canonical = resolve_provider(short, catalog().keys() | installed, {**api_aliases(), **mcp_aliases()})
    except (UnknownProvider, ValueError) as exc:
        print(exc, file=sys.stderr)
        return 127 if isinstance(exc, UnknownProvider) else 2
    executable = provider_path(canonical)
    if not executable.is_file():
        for kind in KINDS:
            module = integration(kind)
            if canonical in module.catalog():
                with redirect_stdout(sys.stderr):
                    status = module.cli_install([canonical])
                if status:
                    return status
                if not executable.is_file():
                    print(f"No executable was installed for {canonical}.", file=sys.stderr)
                    return 1
                break
    if not executable.is_file():
        print(f"No installed or bundled integration named {canonical}.", file=sys.stderr)
        return 127
    try:
        return subprocess.run([*provider_argv(canonical), *args], check=False).returncode
    except OSError as exc:
        print(f"Cannot start {canonical}: {exc.strerror}", file=sys.stderr)
        return 126
