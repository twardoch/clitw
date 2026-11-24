# this_file: src/clitwy/mcp/install.py
"""Install endpoint-pinned wrappers in clitwy's private registry and executable directory."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import sys
import tempfile
from pathlib import Path

from clitwy.names import UnknownProvider, provider_name
from clitwy.paths import bin_dir, home, install_lock, provider_path, registry_dir

from .config import (
    AUTH_CODES,
    PUBLIC_OAUTH_URLS,
    SAFE_PATH,
    binding_auth,
    connection_pin,
    endpoint,
    held,
    load,
    parts,
    read_config,
    resolve_name,
    server_slug,
)


def atomic(path: Path, body: bytes, mode: int) -> None:
    fd, temporary = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as out:
            out.write(body)
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def wrapper(name: str, source: str, pin: str, *, executable: str | None = None, package: str = "clitwy") -> bytes:
    args = [executable or sys.executable, "-m", f"{package}.mcp.runtime", str(home()), source, name, pin]
    if os.name == "nt":
        return (
            f"# managed by {package} mcp\nimport sys\nfrom {package}.mcp.runtime import main\n"
            + f"raise SystemExit(main({args[3:]!r} + sys.argv[1:]))\n"
        ).encode()
    return (f"#!/bin/sh\n# managed by {package} mcp\nexec " + shlex.join(args) + ' "$@"\n').encode()


def _renamed_runtime(name: str, entry: dict) -> tuple[dict, bytes] | None:
    """Rebind a receipt-verified clitw launcher and its private mcp2cli command to this installation."""
    meta = entry["clitw_mcp"]
    body = provider_path(name).read_bytes()
    if b"# managed by clitw mcp\n" not in body:
        return None
    previous = sys.executable
    if os.name != "nt":
        try:
            command = shlex.split(os.fsdecode(body.splitlines()[-1]))
        except ValueError:
            return None
        if len(command) < 2 or not Path(command[1]).is_absolute():
            return None
        previous = command[1]
    if body != wrapper(name, meta["config"], meta["pin"], executable=previous, package="clitw"):
        return None
    new = wrapper(name, meta["config"], meta["pin"])
    updated = dict(entry, clitw_mcp=dict(meta, wrapper_sha256=hashlib.sha256(new).hexdigest()))
    if entry.get("source_type") == "mcp_stdio":
        updated["source"] = shlex.join([sys.executable, "-m", "clitwy.mcp.runtime", "--private", name])
    return updated, new


def owned(name: str, row: dict, path: Path) -> bool:
    metadata = row.get("clitw_mcp") if isinstance(row, dict) else None
    if not metadata or path != provider_path(name) or row.get("wrapper_path") != str(path) or path.is_symlink():
        return False
    if not path.exists():
        return True
    return (
        path.is_file()
        and path.stat().st_size <= 16384
        and hashlib.sha256(path.read_bytes()).hexdigest() == metadata.get("wrapper_sha256")
    )


def registration(name: str, row: dict, conf: dict, source: str) -> tuple[dict, bytes]:
    mode, url, transport = endpoint(row, conf)
    pin = connection_pin(mode, url, transport)
    script = wrapper(name, source, pin)
    u = parts(url)
    private = (
        source != "catalog"
        and url not in PUBLIC_OAUTH_URLS
        and mode != "proxy"
        and (u.query or not SAFE_PATH.fullmatch(u.path))
    )
    if private:
        kind, public = "mcp_stdio", shlex.join([sys.executable, "-m", "clitwy.mcp.runtime", "--private", name])
    else:
        kind, public = "mcp", url
    meta = {
        "server": row["name"],
        "legacy": row["legacy"],
        "auth_mode": row["auth_mode"],
        "mode": mode,
        "pin": pin,
        "config": source,
        "wrapper_sha256": hashlib.sha256(script).hexdigest(),
    }
    return {
        "source_type": kind,
        "source": public,
        "transport": transport,
        "include": [server_slug(row) + "--*"] if mode == "proxy" else [],
        "wrapper_path": str(provider_path(name)),
        "description": row.get("description") if source == "catalog" else f"MCP server {name[4:]}",
        "clitw_mcp": meta,
    }, script


def commit(registry_path: Path, previous: dict, registry: dict, scripts: dict[str, bytes | None]) -> None:
    undo = {}
    try:
        for name, new in scripts.items():
            path = provider_path(name)
            before = path.read_bytes() if path.exists() else None
            if before == new:
                continue
            undo[path] = (before, path.stat().st_mode & 0o777 if before is not None else 0)
            if new is None:
                path.unlink()
            else:
                atomic(path, new, 0o755)
        if registry != previous:
            atomic(registry_path, (json.dumps(registry, indent=2) + "\n").encode(), 0o600)
    except OSError:
        for path, (data, mode) in reversed(undo.items()):
            if data is None:
                path.unlink(missing_ok=True)
            else:
                atomic(path, data, mode)
        raise


def migrate_names(names: list[str] | None = None, *, source: str | None = None, installed: set | None = None) -> dict:
    """Rename owned MCP registrations and exports offline, using the original pinned configuration."""
    registry_path = registry_dir() / "baked.json"
    if not registry_path.exists():
        if names and source is None:
            raise ValueError("no installed MCP registrations")
        return {}
    migrated = {}
    with install_lock():
        if registry_path.is_symlink():
            raise ValueError("registry is a symlink")
        original = load(registry_path)
        entries = {
            n: r
            for n, r in original.items()
            if isinstance(r, dict) and r.get("clitw_mcp") and re.fullmatch("mcx-[a-z0-9][a-z0-9-]*", n)
        }
        requested = set(names or [])
        selected = set()
        if requested:
            for value in requested:
                try:
                    selected.add(resolve_name(value, entries))
                    continue
                except UnknownProvider:
                    pass
                # Before conversion, a typed request may refer to a legacy registry entry.
                for old_name, entry in entries.items():
                    if entry["clitw_mcp"].get("auth_mode"):
                        continue
                    if any(
                        provider_name(entry["clitw_mcp"]["server"], "mcp", suffix)[4:] == value.removeprefix("mcx-")
                        for suffix in AUTH_CODES.values()
                    ):
                        selected.add(old_name)
                        break
                else:
                    if source is None:
                        raise ValueError(f"no installed MCP provider named {value}")
        else:
            selected = set(entries)
        updated, scripts = dict(original), {}
        ready = set()
        for old in sorted(selected):
            entry = entries[old]
            meta = entry["clitw_mcp"]
            if meta.get("auth_mode"):
                if prior := meta.get("previous_name"):
                    migrated[prior] = old
                if not owned(old, entry, provider_path(old)):
                    raise ValueError(f"{old}: existing executable has a different owner")
                if meta.get("hold"):
                    command = entry.get("source")
                    if entry.get("source_type") == "mcp_stdio" and isinstance(command, str):
                        try:
                            args = shlex.split(command)
                        except ValueError:
                            continue
                        if args[1:] == ["-m", "clitw.mcp.runtime", "--private", old] and Path(args[0]).is_absolute():
                            updated[old] = dict(
                                entry, source=shlex.join([sys.executable, "-m", "clitwy.mcp.runtime", "--private", old])
                            )
                    continue
                if provider_path(old).is_file():
                    if replacement := _renamed_runtime(old, entry):
                        updated[old], scripts[old] = replacement
                    ready.add(old)
                    continue
            cfg = meta["config"]
            if cfg.startswith(("http://", "https://")):
                # Untyped registries may have used a service name ending in literal '-mcp'.
                auth = meta.get("auth_mode") or binding_auth({}, cfg)
                direct = provider_name(meta["server"], "mcp", AUTH_CODES[auth])
                conf, rows = read_config(cfg, direct)
                matches = list(rows)
            else:
                conf, rows = read_config(cfg)
                matches = [key for key, row in rows.items() if row["name"] == meta["server"]]
            if len(matches) != 1:
                if source is not None and not matches:
                    continue
                raise ValueError(f"{old}: original MCP server is missing or ambiguous")
            new = matches[0]
            row = rows[new]
            acceptable = {new, old, meta.get("previous_name"), meta.get("legacy")}
            if requested and not any(
                "mcx-" + n.removeprefix("mcx-").removeprefix("tw-") in acceptable for n in requested
            ):
                if source is not None:
                    continue
                raise ValueError(f"{old}: current configuration resolves to {new}")
            if not owned(old, entry, provider_path(old)):
                raise ValueError(f"{old}: existing executable has a different owner")
            if new != old and (new in updated or os.path.lexists(provider_path(new))):
                raise ValueError(f"{old}: target {new} is occupied")
            if meta.get("hold") or held(row):
                data, script = held_registration(new, row, cfg, meta.get("hold") or held(row)), None
            else:
                data, script = registration(new, row, conf, cfg)
                if meta.get("pin") != data["clitw_mcp"]["pin"]:
                    raise ValueError(f"{old}: endpoint changed since registration")
                ready.add(new)
            if old != new:
                del updated[old]
                scripts[old] = None
                data["clitw_mcp"]["previous_name"] = old
                migrated[old] = new
            elif prior := meta.get("previous_name"):
                data["clitw_mcp"]["previous_name"] = prior
            updated[new], scripts[new] = data, script
        if scripts or updated != original:
            bin_dir().mkdir(parents=True, exist_ok=True)
            commit(registry_path, original, updated, scripts)
    if migrated:
        from clitwy.builders import migrate_exports

        migrate_exports("mcp", migrated)
    if installed is not None:
        installed.update(ready)
    return migrated


def held_registration(name: str, row: dict, source: str, reason: str) -> dict:
    return {
        "source_type": "mcp_stdio",
        "source": shlex.join([sys.executable, "-m", "clitwy.mcp.runtime", "--private", name]),
        "wrapper_path": str(provider_path(name)),
        "clitw_mcp": {
            "server": row["name"],
            "config": source,
            "mode": "held",
            "hold": reason,
            "legacy": row["legacy"],
            "auth_mode": row["auth_mode"],
        },
    }


def install(
    source: str,
    names: list[str],
    direct_name: str = "",
    *,
    auth_mode: str | None = None,
    installed: set[str] | None = None,
    discover=None,
) -> int:
    conf, rows = read_config(source, direct_name, auth_mode=auth_mode)
    chosen = {resolve_name(n, rows) for n in names}
    if rows:
        migrate_names(list(chosen or rows), source=source)
    if not rows and source == "catalog":
        raise ValueError("empty packaged catalogue")
    candidates = {n: rows[n] for n in (chosen or rows) if not held(rows[n])}
    accepted = discover(source, conf, candidates) if discover is not None else candidates
    home().mkdir(mode=0o700, parents=True, exist_ok=True)
    for path in (bin_dir(), registry_dir()):
        path.mkdir(parents=True, exist_ok=True)
    registry_path = registry_dir() / "baked.json"
    errors = 0
    with install_lock():
        if registry_path.is_symlink():
            raise ValueError("registry is a symlink")
        old = load(registry_path) if registry_path.exists() else {}
        registry = dict(old)
        scripts = {}
        messages = []
        for name in sorted(chosen or rows):
            row = rows[name]
            if not held(row) and name not in accepted:
                continue
            try:
                path = provider_path(name)
                before = old.get(name, {})
                prior = before.get("clitw_mcp") or {}
                prior_source = prior.get("config")
                if prior_source and prior_source not in ("catalog", source):
                    raise ValueError("already managed by a different configuration")
                if source == "catalog" and prior.get("hold"):
                    raise ValueError("server is held by an explicit configuration")
                if reason := held(row):
                    if name in old and not owned(name, before, path):
                        raise ValueError("registration or executable has a different owner")
                    if name not in old and os.path.lexists(path):
                        raise ValueError("executable already exists")
                    registry[name] = held_registration(name, row, source, reason)
                    if previous := prior.get("previous_name"):
                        registry[name]["clitw_mcp"]["previous_name"] = previous
                    scripts[name] = None
                    messages.append(f"Held {name} ({reason})")
                    continue
                data, script = registration(name, row, conf, source)
                identical = (
                    not path.is_symlink()
                    and path.is_file()
                    and path.stat().st_size == len(script)
                    and path.read_bytes() == script
                )
                if name in old and not owned(name, before, path) and not (prior_source == source and identical):
                    raise ValueError("registration or executable has a different owner")
                if name not in old and os.path.lexists(path) and not identical:
                    raise ValueError("executable already exists")
                if previous := prior.get("previous_name"):
                    data["clitw_mcp"]["previous_name"] = previous
                registry[name], scripts[name] = data, script
                messages.append(f"Installed {name}")
            except ValueError as exc:
                print(f"{name}: {exc}", file=sys.stderr)
                errors += 1
        # A direct endpoint invocation adds one binding. It has no list of removed servers.
        for name, reg in ({} if source.startswith(("http://", "https://")) else old).items():
            if (
                not isinstance(reg, dict)
                or (reg.get("clitw_mcp") or {}).get("config") != source
                or not re.fullmatch("mcx-[a-z0-9][a-z0-9-]*", name)
            ):
                continue
            if chosen and name not in chosen:
                continue
            if name in rows:
                continue
            if owned(name, reg, provider_path(name)):
                scripts[name] = None
                del registry[name]
                messages.append(f"Removed {name}")
        commit(registry_path, old, registry, scripts)
        if installed is not None:
            installed.update(n for n, body in scripts.items() if body is not None)
        for line in messages:
            print(line)
    return int(bool(errors))


def cli_install(
    argv: list[str], *, installed: set[str] | None = None, discover=None, parser: argparse.ArgumentParser | None = None
) -> int:
    """Install named presets, an explicitly supplied configuration, or a new HTTP endpoint."""
    parser = parser or argparse.ArgumentParser(prog="tw mcp install", description=__doc__)
    parser.add_argument(
        "names", nargs="*", metavar="NAME", help="Install selected servers; default: all from selected source"
    )
    choice = parser.add_mutually_exclusive_group()
    choice.add_argument("--config", type=Path, help="Read a private MCPProxy or MCP client JSON configuration")
    choice.add_argument("--endpoint", nargs=2, metavar=("NAME", "URL"), help="Install a public HTTP/SSE endpoint")
    choice.add_argument("--migrate-names", action="store_true", help="Rename installed MCP commands offline")
    parser.add_argument("--auth", choices=AUTH_CODES, help="Credential method for a direct --endpoint")
    opts = parser.parse_args(argv)
    if opts.auth and not opts.endpoint:
        parser.error("--auth requires --endpoint")
    try:
        if opts.migrate_names:
            for old, new in migrate_names(opts.names, installed=installed).items():
                print(f"{old} -> {new}")
            return 0
        if opts.endpoint:
            if opts.names:
                parser.error("--endpoint takes its own name")
            name, url = opts.endpoint
            if not url.startswith(("https://", "http://")):
                raise ValueError("expected a public HTTP(S) URL")
            return install(url, [], name, auth_mode=opts.auth, installed=installed, discover=discover)
        return install(
            str(opts.config.expanduser().resolve()) if opts.config else "catalog",
            opts.names,
            installed=installed,
            discover=discover,
        )
    except (OSError, KeyError, TypeError, ValueError) as exc:
        print(f"MCP installation failed: {exc}", file=sys.stderr)
        return 1
