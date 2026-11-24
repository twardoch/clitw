# this_file: src/clitw/openapi/install.py
"""Install selected catalogue or user-defined API wrappers under CLITW_HOME."""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import subprocess
import sys
import tempfile
from dataclasses import replace
from pathlib import Path
from urllib.error import URLError

from clitw.names import provider_name, resolve_provider
from clitw.paths import home, install_lock, provider_path, registry_dir

from .catalogue import Tool, auth_kind, legacy_aliases, providers
from .schema import UnusableSchema, prepare
from .state import NAME, atomic, header_args, registry, valid_url, wrapper


def canonical(name: str, entries: dict | None = None) -> str:
    available = registry() if entries is None else entries
    aliases = dict(legacy_aliases())
    aliases.update({entry["previous_name"]: key for key, entry in available.items() if entry.get("previous_name")})
    return resolve_provider(name, set(providers()) | available.keys(), aliases)


def customized(entry: dict | None, tool: Tool) -> bool:
    """Recognize a user-configured binding, including entries written by older versions."""
    return bool(
        entry
        and (
            not entry.get("preset")
            or entry.get("customized")
            or entry.get("base_url") != tool.base
            or entry.get("auth") != tool.auth
        )
    )


def publish(tool: Tool, staged: Path, commands: int, preset: bool, bulk: bool) -> bool:
    """Replace only files whose contents match our preceding successful publication."""
    with install_lock():
        entries = registry()
        dest = provider_path(tool.name)
        previous = entries.get(tool.name)
        if bulk and customized(previous, tool):
            return False
        baked = registry_dir() / "baked.json"
        if baked.is_file() and tool.name in json.loads(baked.read_bytes()):
            raise ValueError("name already belongs to an MCP integration")
        if dest.is_symlink() or (dest.exists() and not dest.is_file()):
            raise ValueError("wrapper destination is occupied")
        existing = dest.read_bytes() if dest.exists() else None
        if existing is not None and (
            not previous or hashlib.sha256(existing).hexdigest() != previous.get("wrapper_sha256")
        ):
            raise ValueError("wrapper destination is occupied or modified")
        snapshot = home() / "openapi" / tool.name / staged.name
        body = staged.read_bytes()
        if snapshot.is_symlink() or (snapshot.exists() and snapshot.read_bytes() != body):
            raise ValueError("existing snapshot failed content verification")
        if not snapshot.exists():
            atomic(snapshot, body)
        script = wrapper(tool.name)
        defaults = providers().get(tool.name, (None,))[0]
        entries[tool.name] = {
            "source": str(snapshot),
            "base_url": tool.base,
            "auth": tool.auth,
            "description": tool.description,
            "command_count": commands,
            "preset": preset,
            "customized": not preset or defaults is None or (tool.base, tool.auth) != (defaults.base, defaults.auth),
            "wrapper_sha256": hashlib.sha256(script).hexdigest(),
        }
        if previous and previous.get("previous_name"):
            entries[tool.name]["previous_name"] = previous["previous_name"]
        atomic(dest, script, 0o755)
        try:
            atomic(registry_dir() / "openapi.json", (json.dumps(entries, indent=2) + "\n").encode())
        except OSError:
            if existing is None:
                dest.unlink(missing_ok=True)
            else:
                atomic(dest, existing, 0o755)
            raise
        return True


def install_one(tool: Tool, preset: bool, bulk: bool = False) -> tuple[str, bool]:
    if not NAME.fullmatch(tool.name) or (tool.base and not valid_url(tool.base, base=True)):
        raise ValueError("invalid wrapper name or API base")
    if tool.auth and not tool.base:
        raise ValueError("a credential binding requires an explicit API destination")
    header_args(tool.auth, {})
    if bulk and customized(registry().get(tool.name), tool):
        return f"{tool.name}: skipped (existing user configuration retained)", False
    if preset and not tool.base:
        return f"{tool.name}: skipped (requires --base-url)", False
    with tempfile.TemporaryDirectory(prefix="clitw-openapi-stage-") as work:
        try:
            staged, count, _, _ = prepare(tool, Path(work))
        except UnusableSchema as exc:
            if not preset:
                raise
            return f"{tool.name}: skipped ({exc})", False
        if not publish(tool, staged, count, preset, bulk):
            return f"{tool.name}: skipped (existing user configuration retained)", False
    return f"{tool.name}: {count} commands" + (
        "" if tool.base else " (local discovery only; configure --base-url)"
    ), True


def main(argv: list[str], *, installed: set[str] | None = None, parser: argparse.ArgumentParser | None = None) -> int:
    parser = parser or argparse.ArgumentParser(prog="tw openapi install", description=__doc__)
    parser.add_argument("names", nargs="*", help="named providers (default: entire catalogue)")
    parser.add_argument("--migrate-names", action="store_true", help="rename owned installed OpenAPI wrappers offline")
    parser.add_argument("--name", help="name for a user-defined wrapper")
    parser.add_argument("--spec", help="OpenAPI file or public HTTPS URL")
    parser.add_argument("--base-url", help="API origin and path prefix for one wrapper")
    parser.add_argument("--auth-header", action="append", default=None, metavar="HEADER:[SCHEME ]env:VARIABLE")
    parser.add_argument("--description", default="Custom OpenAPI service")
    parser.add_argument("--jobs", type=int, default=3)
    opts = parser.parse_args(argv)
    names = opts.names
    catalogue = providers()
    if opts.jobs < 1:
        parser.error("--jobs must be positive")
    from .upgrade import migrate_for_cli

    if opts.migrate_names:
        if opts.spec or opts.name or opts.base_url or opts.auth_header:
            parser.error("--migrate-names only accepts optional provider selectors")
        changed = migrate_for_cli(names)
        if changed is None:
            return 1
        for old, new in changed.items():
            print(f"{old} -> {new}")
        if installed is not None:
            installed.update(changed.values())
        return 0
    if opts.spec:
        if opts.name and names or len(names) > 1 or not (opts.name or names):
            parser.error("provide one name with a custom --spec")
        selected = [
            Tool(
                provider_name(opts.name or names[0], "api", auth_kind(",".join(opts.auth_header or []))),
                opts.spec,
                opts.base_url or "",
                ",".join(opts.auth_header or []),
                opts.description,
            )
        ]
        preset = False
    else:
        if opts.name:
            parser.error("--name is for a user-defined --spec")
        try:
            names = [resolve_provider(n, catalogue, legacy_aliases()) for n in names]
        except ValueError as error:
            parser.error(str(error))
        selected = [catalogue[n][0] for n in dict.fromkeys(names or catalogue)]
        if (opts.base_url or opts.auth_header is not None) and len(selected) != 1:
            parser.error("select one wrapper to configure its origin or credentials")
        if opts.base_url or opts.auth_header is not None:
            tool = selected[0]
            # Automatic provider credentials always remain scoped to the published destination.
            same = not opts.base_url or opts.base_url.rstrip("/") == tool.base.rstrip("/")
            auth = (tool.auth if same else "") if opts.auth_header is None else ",".join(opts.auth_header)
            selected = [
                replace(
                    tool,
                    name=provider_name(tool.name, "api", auth_kind(auth)),
                    base=opts.base_url or tool.base,
                    auth=auth,
                )
            ]
        preset = True
    if migrate_for_cli([t.name for t in selected] if opts.spec else names) is None:
        return 1
    if not names and preset:
        from .upgrade import reconcile

        try:
            for old in reconcile():
                print(f"{old}: removed obsolete default wrapper", flush=True)
        except (OSError, ValueError, TypeError) as exc:
            print(f"OpenAPI registry reconciliation failed ({type(exc).__name__})", file=sys.stderr)
            return 1
    status = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=opts.jobs) as executor:
        pending = {executor.submit(install_one, tool, preset, not names and preset): tool.name for tool in selected}
        for done in concurrent.futures.as_completed(pending):
            try:
                message, published = done.result()
                print(message, flush=True)
                if published and installed is not None:
                    installed.add(pending[done])
            except (ValueError, OSError, URLError, subprocess.SubprocessError, TimeoutError) as exc:
                reason = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
                print(f"{pending[done]}: {reason}", file=sys.stderr, flush=True)
                status = 1
    return status
