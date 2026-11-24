# this_file: src/clitwy/builders.py
"""Build independent mcx-NAME programs and publish them in a user-selected executable directory."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import math
import os
import re
import sys
import sysconfig
import tempfile
from functools import lru_cache
from importlib.metadata import PackageNotFoundError, distribution
from pathlib import Path

from .openapi.state import NAME, atomic
from .paths import bin_dir, install_lock, provider_path, registry_dir


def _default_bin() -> Path:
    if os.name == "nt":
        return Path(sysconfig.get_path("scripts", scheme="nt_user"))
    return Path(os.getenv("XDG_BIN_HOME") or Path.home() / ".local/bin")


def _windows_exe(name: str, target: Path, work: Path) -> bytes:
    """Build a native console launcher with the same argv semantics as pip's console scripts."""
    from distlib.scripts import ScriptMaker

    source, out = work / "source", work / "built"
    source.mkdir()
    out.mkdir()
    filename = name + ".py"
    (source / filename).write_text(
        f"#!python\nimport runpy, sys\nsys.argv[0] = {str(target)!r}\nrunpy.run_path(sys.argv[0], run_name='__main__')\n",
        encoding="utf-8",
    )
    maker = ScriptMaker(str(source), str(out))
    maker.make(filename)
    return (out / (name + ".exe")).read_bytes()


def _outfile(directory: Path, name: str) -> Path:
    return directory / (name + ".exe" if os.name == "nt" else name)


def _identity(path: Path, source: Path) -> str | None:
    if path.is_symlink():
        return "link" if os.readlink(path) == str(source) else None
    if os.name == "nt" and path.is_file() and path.stat().st_size < 8_000_000:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    return None


def _create(path: Path, source: Path, data: bytes | None = None) -> str:
    if os.name != "nt":
        path.symlink_to(source)
        return "link"
    # Hard-link the completed file into place; an unrelated command created concurrently wins.
    with tempfile.TemporaryDirectory(dir=path.parent, prefix=".mcx-") as tmp:
        work = Path(tmp)
        payload = data if data is not None else _windows_exe(source.stem, source, work)
        stage = work / "launcher.exe"
        stage.write_bytes(payload)
        os.link(stage, path)
        return hashlib.sha256(payload).hexdigest()


@lru_cache(maxsize=1)
def _packaged_launchers() -> dict[str, tuple[Path, str]]:
    """Read installed console-script receipts once when the destination is already occupied."""
    try:
        dist = distribution("clitwy")
    except PackageNotFoundError:
        return {}
    names = {
        e.name for e in dist.entry_points if e.group == "console_scripts" and e.value == "clitwy.cli:provider_main"
    }
    return {
        Path(f.name).stem: (Path(f.locate()), f.hash.value)
        for f in dist.files or []
        if Path(f.name).stem in names and f.name.startswith("mcx-") and f.hash and f.hash.mode == "sha256"
    }


def _packaged(name: str, destination: Path) -> bool:
    receipt = _packaged_launchers().get(name)
    if not receipt or not destination.is_file():
        return False
    recorded, digest = receipt
    if not recorded.is_file() or recorded.is_symlink() or not destination.samefile(recorded):
        return False
    actual = base64.urlsafe_b64encode(hashlib.sha256(recorded.read_bytes()).digest()).decode().rstrip("=")
    return actual == digest


def _intact(source: Path, row: dict, kind: str) -> bool:
    metadata = row.get("clitw_mcp", {}) if kind == "mcp" else row
    return (
        not source.is_symlink()
        and source.is_file()
        and hashlib.sha256(source.read_bytes()).hexdigest() == metadata.get("wrapper_sha256")
    )


def _records(path: Path) -> dict:
    if path.is_symlink():
        raise ValueError("export registry is a symlink")
    records = json.loads(path.read_bytes()) if path.exists() else {}
    if not isinstance(records, dict):
        raise TypeError("invalid export registry")
    return records


def migrate_exports(kind: str, renamed: dict[str, str]) -> None:
    """Move launcher exports previously created by this installation to their new provider names."""
    if _export(kind, set(), None, renamed):
        raise ValueError("one or more provider exports are occupied or modified")


def _export(kind: str, installed: set[str], directory: Path | None, renamed: dict[str, str] | None = None) -> int:
    """Publish this run's successes; reconcile only recorded exports whose native binding disappeared."""
    ledger = registry_dir() / (kind + "-exports.json")
    if not installed and not ledger.exists():
        return 0
    directory = directory.expanduser().resolve() if directory is not None else None
    errors = 0
    with install_lock():
        path = registry_dir() / ("baked.json" if kind == "mcp" else "openapi.json")
        registry = _records(path)
        previous = _records(ledger)
        exports = {k: dict(v) for k, v in previous.items()}
        undo = []
        targets = {(directory, n) for n in installed if directory is not None}
        renamed = dict(renamed or {})
        for canonical, entry in registry.items():
            meta = entry.get("clitw_mcp", {}) if kind == "mcp" else entry
            if old := meta.get("previous_name"):
                renamed.setdefault(old, canonical)
        try:
            for folder, entries in list(exports.items()):
                for name, identity in list(entries.items()):
                    if not (
                        NAME.fullmatch(name)
                        and Path(folder).is_absolute()
                        and isinstance(identity, str)
                        and (identity == "link" if os.name != "nt" else re.fullmatch(r"[a-f0-9]{64}", identity))
                    ):
                        raise ValueError("invalid export receipt")
                    entry = registry.get(name)
                    source, dest = provider_path(name), _outfile(Path(folder), name)
                    if (
                        source.exists()
                        and entry is not None
                        and (kind != "mcp" or entry.get("clitw_mcp", {}).get("mode") != "held")
                    ):
                        continue
                    if (
                        (new_name := renamed.get(name))
                        and new_name in registry
                        and new_name != name
                        and (kind != "mcp" or registry[new_name].get("clitw_mcp", {}).get("mode") != "held")
                    ):
                        if not NAME.fullmatch(new_name):
                            raise ValueError("invalid renamed provider")
                        if not _intact(provider_path(new_name), registry[new_name], kind):
                            print(f"{new_name}: private wrapper changed before export", file=sys.stderr)
                            errors += 1
                            continue
                        new_path = _outfile(Path(folder), new_name)
                        if os.path.lexists(new_path) and not (
                            (
                                entries.get(new_name)
                                and _identity(new_path, provider_path(new_name)) == entries[new_name]
                            )
                            or _packaged(new_name, new_path)
                        ):
                            print(f"{new_name}: existing PATH command retained", file=sys.stderr)
                            errors += 1
                            continue
                        if Path(folder).is_dir():
                            targets.add((Path(folder), new_name))
                    if _identity(dest, source) == identity:
                        data = dest.read_bytes() if os.name == "nt" else None
                        dest.unlink()
                        undo.append(("removed", dest, source, identity, data))
                    del entries[name]
                if not entries:
                    del exports[folder]
            for folder_path, name in sorted(targets):
                source, dest = provider_path(name), _outfile(folder_path, name)
                entry = registry.get(name, {})
                if not _intact(source, entry, kind):
                    print(f"{name}: private wrapper changed before export", file=sys.stderr)
                    errors += 1
                    continue
                if os.name != "nt" and folder_path == bin_dir().resolve():
                    continue
                known = exports.get(str(folder_path), {}).get(name)
                if os.path.lexists(dest):
                    if (known and _identity(dest, source) == known) or _packaged(name, dest):
                        continue
                    print(f"{name}: PATH destination is occupied; existing file retained", file=sys.stderr)
                    errors += 1
                    continue
                folder_path.mkdir(parents=True, exist_ok=True)
                identity = _create(dest, source)
                undo.append(("created", dest, source, identity, None))
                exports.setdefault(str(folder_path), {})[name] = identity
            if exports != previous:
                atomic(ledger, (json.dumps(exports, sort_keys=True, indent=2) + "\n").encode())
        except (ValueError, OSError):
            for action, dest, source, identity, data in reversed(undo):
                if action == "created" and _identity(dest, source) == identity:
                    dest.unlink()
                elif action == "removed" and not os.path.lexists(dest):
                    _create(dest, source, data)
            raise
    count = len(installed) - errors
    if count > 0 and directory is not None:
        print(f"{count} mcx command(s) available in {directory}")
    return int(bool(errors))


def _run(kind: str, argv: list[str] | None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    options = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
    options.add_argument(
        "--bin-dir", type=Path, default=_default_bin(), help="directory for generated mcx-NAME executables"
    )
    if kind == "mcp":
        options.add_argument("--jobs", type=int, default=6, help="maximum concurrent schema checks")
        options.add_argument("--timeout", type=float, default=18, help="seconds per MCP schema check")
    args, _ = options.parse_known_args(argv)
    parser = argparse.ArgumentParser(
        prog="mcxadmin-build-" + kind, parents=[options], allow_abbrev=False, description=__doc__
    )
    installed = set()
    if kind == "mcp":
        from .build_discovery import discover
        from .mcp.install import cli_install

        if not 1 <= args.jobs <= 32 or not math.isfinite(args.timeout) or args.timeout <= 0:
            parser.error("use 1–32 jobs and a positive finite timeout")
        result = cli_install(
            argv,
            parser=parser,
            installed=installed,
            discover=lambda s, c, r: discover(s, c, r, args.jobs, args.timeout),
        )
    else:
        from .openapi.install import main

        result = main(argv, parser=parser, installed=installed)
    try:
        return max(result, _export(kind, installed, args.bin_dir))
    except (OSError, ValueError, TypeError) as exc:
        print(
            f"Could not export {kind} executables ({type(exc).__name__}): check the destination and registry",
            file=sys.stderr,
        )
        return 1


def mcp_main(argv: list[str] | None = None) -> int:
    """Console entry point for mcxadmin-build-mcp."""
    return _run("mcp", argv)


def openapi_main(argv: list[str] | None = None) -> int:
    """Console entry point for mcxadmin-build-openapi."""
    return _run("openapi", argv)
