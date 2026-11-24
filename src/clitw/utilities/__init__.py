# this_file: src/clitw/utilities/__init__.py
"""CLI entry point for MCPProxy administration and provider diagnostics."""

import sys

import httpx
import yaml

from . import doctor, groups


def cli(argv: list[str]) -> int:
    families = ("groups", "doctor")
    if not argv or argv[0] in ("-h", "--help"):
        print("Usage: tw {" + ",".join(families) + "} [options]")
        return 0
    try:
        family, *args = argv
        if family == "groups":
            return groups.cli(args)
        if family == "doctor":
            return doctor.cli(args)
        raise ValueError("Unknown utility: " + family)
    except SystemExit as exc:
        return int(exc.code or 0)
    except (OSError, ValueError, TypeError, KeyError, httpx.HTTPError, yaml.YAMLError) as exc:
        # Provider errors can contain presigned URLs and account/session details.
        message = str(exc) if isinstance(exc, (ValueError, FileNotFoundError)) else type(exc).__name__
        print(f"tw {argv[0]}: {message}", file=sys.stderr)
        return 1
