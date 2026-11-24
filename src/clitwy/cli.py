# this_file: src/clitwy/cli.py
"""Route CLI arguments without interpreting flags belonging to providers."""

import argparse
import json
import sys
from pathlib import Path

from .__version__ import __version__
from .api import KINDS, catalog, integration, run

UTILITIES = ("groups", "doctor")


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(prog="tw", description="Install and run MCP and OpenAPI command-line tools.")
    parser.add_argument("--version", action="version", version=__version__)
    commands = parser.add_subparsers(dest="command")
    commands.add_parser("info", help="describe providers, authentication and pricing")
    commands.add_parser("search", help="find providers by purpose, text or tag")
    listing = commands.add_parser("catalog", help="list bundled integrations offline")
    listing.add_argument("kind", nargs="?", choices=KINDS)
    listing.add_argument("--json", action="store_true", help="write a name-to-description JSON object")
    for kind in KINDS:
        group = commands.add_parser(kind, help=f"install or check {kind} integrations")
        group.add_argument("action", choices=("install", "try", "login") if kind == "mcp" else ("install", "try"))
    commands.add_parser("auth", help="authorize installed providers with browser OAuth or API keys")
    runner = commands.add_parser("run", help="invoke a provider with its original arguments")
    runner.add_argument("name", metavar="NAME")
    for name in UTILITIES:
        commands.add_parser(name, help=f"run the {name} utility")
    if args[:1] == ["info"]:
        from .service_cli import info

        return info(args[1:])
    if args[:1] == ["search"]:
        from .service_cli import search

        return search(args[1:])
    if args[:1] == ["auth"]:
        return auth(args[1:])
    if args[:2] == ["mcp", "login"]:
        from .mcp.login import cli_login

        return cli_login(args[2:])
    if args[:1] and args[0] in KINDS and args[1:2] and args[1] in ("install", "try"):
        module = integration(args[0])
        handler = module.cli_install if args[1] == "install" else module.cli_try
        return handler(args[2:])
    if args[:1] and args[0] in UTILITIES:
        from .utilities import cli

        return cli(args)
    if args[:1] == ["run"] and len(args) > 1 and args[1] not in ("-h", "--help"):
        return run(args[1], args[2:])
    options = parser.parse_args(args)
    if options.command == "catalog":
        result = catalog(options.kind)
        if options.json:
            print(json.dumps(result, indent=2))
        else:
            for name, description in result.items():
                print(f"{name}\t{description}")
        return 0
    parser.print_help()
    return 0


def auth(args: list[str] | None = None) -> int:
    """Guide OAuth and API key setup for the current installation."""
    from .credentials import cli_keys
    from .mcp.login import cli_login

    args = sys.argv[1:] if args is None else args
    if args[:1] == ["keys"]:
        return cli_keys(args[1:])
    if args[:1] == ["oauth"]:
        return cli_login(args[1:])

    parser = argparse.ArgumentParser(
        prog=Path(sys.argv[0]).stem if Path(sys.argv[0]).stem == "mcxadmin-auth" else "tw auth",
        description=auth.__doc__,
        epilog="Use 'tw auth oauth' or 'tw auth keys' to select a method.",
    )
    parser.add_argument("--list", action="store_true", help="show OAuth and API key status without prompting")
    parser.add_argument("names", nargs="*", metavar="NAME", help="select installed providers (default: all)")
    opts = parser.parse_args(args)
    flags = ["--list"] if opts.list else []
    if opts.names:
        from .credentials import installed_keys
        from .mcp.config import legacy_aliases as mcp_aliases
        from .mcp.login import packaged_bindings
        from .names import resolve_provider
        from .openapi.catalogue import legacy_aliases as api_aliases

        key_names = installed_keys()
        oauth_names = {b.name for b in packaged_bindings()}
        try:
            names = [
                resolve_provider(n, key_names.keys() | oauth_names, {**api_aliases(), **mcp_aliases()})
                for n in opts.names
            ]
        except ValueError as exc:
            parser.error(str(exc))
        keys = [n for n in names if key_names.get(n)]
        oauth = [n for n in names if n in oauth_names and n.endswith("-mcpo")]
        if keys and (status := cli_keys([*flags, *keys])):
            return status
        if oauth:
            return cli_login([*flags, *oauth])
        if not keys:
            print("Selected providers have no configured sign-in method.")
        return 0
    print("MCP browser authorization", flush=True)
    oauth_status = cli_login(flags, quit_code=130)
    if oauth_status in (2, 130):
        return oauth_status
    print("API key configuration", flush=True)
    key_status = cli_keys(flags)
    return oauth_status or key_status


def provider_main() -> int:
    name = Path(sys.argv[0]).stem.removeprefix("tw-")
    if name in UTILITIES:
        from .utilities import cli

        return cli([name, *sys.argv[1:]])
    return run(name, sys.argv[1:])
