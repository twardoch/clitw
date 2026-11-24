# this_file: src/clitw/openapi/upgrade.py
"""Reconcile unchanged default wrappers from earlier releases during a bulk installation."""

from __future__ import annotations

import errno
import hashlib
import json
import os
import shlex
import sys
from pathlib import Path

from clitw.names import provider_name
from clitw.paths import home, install_lock, provider_path, registry_dir

from .catalogue import auth_kind, legacy_aliases, providers
from .state import NAME, atomic, registry, wrapper

# LMNT announced its shutdown at https://www.lmnt.com/. Both stock names used this exact snapshot.
LMNT_NAMES = {"mcx-lmnt", "mcx-lmnt-apik"}
LMNT_SNAPSHOT = "0bf07de5e45cda86364d086ae7366342bc165fe6f03895d5095fe1c450ba47dc"

# Historical signatures only. These origins are never used for discovery or requests.
RETIRED = dict.fromkeys(
    (
        "mcx-imdb-apidev",
        "mcx-msft-customimagesearch",
        "mcx-msft-imagesearch",
        "mcx-msft-visualsearch",
        "mcx-msft-websearch",
        "mcx-proxycurl",
        "mcx-sn19",
    ),
    ("", ""),
)
RETIRED.update(
    {
        "mcx-aisearch": ("https://api.aisearchapi.io", "Authorization:Bearer env:AISEARCH_API_KEY"),
        "mcx-g-analytics": (
            "https://analytics.googleapis.com/analytics/v3",
            "Authorization:Bearer env:GOOGLE_OAUTH_ACCESS_TOKEN",
        ),
        "mcx-gemelo-api": ("https://api.gemelo.ai/", "X-API-Key:env:GEMELO_API_KEY,X-Client-Key:env:GEMELO_CLIENT_KEY"),
        "mcx-goog-io": ("https://api.goog.io", ""),
        **dict.fromkeys(LMNT_NAMES, ("https://api.lmnt.com", "x-api-key:env:LMNT_API_KEY")),
        "mcx-searchly": ("https://searchly.asuarez.dev/api/v1", ""),
    }
)


def former_default(name: str, entry: dict) -> bool:
    if name in LMNT_NAMES and (
        Path(entry.get("source", "")).name != LMNT_SNAPSHOT + ".json"
        or entry.get("description") != "LMNT — low-latency speech synthesis"
    ):
        return False
    previous = RETIRED.get(name)
    recipe = legacy_aliases().get(name, name)
    if previous is None and recipe in providers():
        tool = providers()[recipe][0]
        if not tool.base:
            previous = tool.base, tool.auth
    return bool(
        previous is not None
        and entry.get("preset") is True
        and not entry.get("customized")
        and (entry.get("base_url"), entry.get("auth")) == previous
    )


def intact(name: str, entry: dict) -> bool:
    """Require the exact generated launcher, registry receipt and content-addressed snapshot."""
    source = Path(entry.get("source", ""))
    dest = provider_path(name)
    if (
        source.parent != home() / "openapi" / name
        or source.parent.is_symlink()
        or source.is_symlink()
        or not source.is_file()
        or dest.is_symlink()
        or not dest.is_file()
        or (os.name == "posix" and dest.stat().st_mode & 0o777 != 0o755)
    ):
        return False
    if dest.stat().st_size > 4096:
        return False
    script = dest.read_bytes()
    # A prior package installation may have used a different Python interpreter.
    first = script.partition(b"\n")[0]
    executable = os.fsdecode(first[2:]) if first.startswith(b"#!") else ""
    if first == b"#!/bin/sh":
        # clitw uses its shell trampoline when the installed Python path contains spaces.
        header = script.splitlines()[1:2]
        words = []
        if header:
            try:
                words = shlex.split(os.fsdecode(header[0]).replace("'''exec'", "exec", 1))
            except ValueError:
                return False
        if len(words) != 4 or words[0] != "exec" or words[2:] != ["$0", "$@"]:
            return False
        executable = words[1]
    return (
        Path(executable).is_absolute()
        and script == wrapper(name, executable)
        and entry.get("wrapper_sha256") == hashlib.sha256(script).hexdigest()
        and source.name == hashlib.sha256(source.read_bytes()).hexdigest() + ".json"
    )


def reconcile(only: set[str] | None = None) -> list[str]:
    """Remove only genuine obsolete default launchers. Leave snapshots and user bindings in place."""
    if not any(former_default(n, e) for n, e in registry().items() if only is None or n in only):
        return []
    removed = []
    with install_lock():
        entries = registry()
        for name, entry in list(entries.items()):
            if (only is not None and name not in only) or not former_default(name, entry) or not intact(name, entry):
                continue
            path = provider_path(name)
            old_script = path.read_bytes()
            path.unlink()
            del entries[name]
            try:
                atomic(registry_dir() / "openapi.json", (json.dumps(entries, indent=2) + "\n").encode())
            except OSError:
                atomic(path, old_script, 0o755)
                raise
            removed.append(name)
    return removed


def _target(old: str, entry: dict) -> str:
    # A tenant-specific or user-modified binding may authenticate differently from its public recipe.
    service = legacy_aliases().get(old, old) if entry.get("preset") else old
    return provider_name(service, "api", auth_kind(entry.get("auth", "")))


def _rename(old: str, new: str, entries: dict, other: dict) -> bool:
    entry = entries[old]
    if not intact(old, entry):
        return False
    src = Path(entry["source"])
    dest = provider_path(new)
    snap = home() / "openapi" / new / src.name
    prior = entries.get(new)
    if new in other or dest.is_symlink() or snap.parent.is_symlink() or snap.is_symlink():
        return False
    if prior is not None:
        if (
            not intact(new, prior)
            or prior["source"] != str(snap)
            or any(prior.get(k) != entry.get(k) for k in ("base_url", "auth", "description", "preset", "customized"))
        ):
            return False
    elif dest.exists():
        return False
    if snap.exists() and snap.read_bytes() != src.read_bytes():
        return False
    if not snap.exists():
        snap.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.link(src, snap)
        except OSError as exc:
            if exc.errno not in (errno.EXDEV, errno.EPERM, errno.ENOTSUP):
                raise
            atomic(snap, src.read_bytes())
    script = wrapper(new)
    old_script = provider_path(old).read_bytes()
    if prior is None:
        atomic(dest, script, 0o755)
    updated = {k: v for k, v in entries.items() if k != old}
    updated[new] = prior or dict(
        entry, previous_name=old, source=str(snap), wrapper_sha256=hashlib.sha256(script).hexdigest()
    )
    try:
        provider_path(old).unlink()
        atomic(registry_dir() / "openapi.json", (json.dumps(updated, indent=2) + "\n").encode())
    except OSError:
        if not provider_path(old).exists():
            atomic(provider_path(old), old_script, 0o755)
        if prior is None:
            dest.unlink(missing_ok=True)
        raise
    entries.clear()
    entries.update(updated)
    return True


def migrate_names(names=None, *, require_exports: bool = True) -> dict[str, str]:
    """Rename intact provider installations offline and update receipts for their external exports."""
    selected = {"mcx-" + n.removeprefix("mcx-").removeprefix("tw-") for n in names or ()}
    migrated = {}
    if not selected or selected & LMNT_NAMES:
        for name in reconcile(LMNT_NAMES):
            print(f"{name}: removed retired LMNT wrapper", flush=True)
    if not registry():
        return migrated
    with install_lock():
        entries = registry()
        baked = registry_dir() / "baked.json"
        other = json.loads(baked.read_bytes()) if baked.exists() else {}
        for old, entry in list(entries.items()):
            if not NAME.fullmatch(old) or old in LMNT_NAMES or former_default(old, entry):
                continue
            new = _target(old, entry)
            if (
                new == old
                or selected
                and not selected.intersection((old, new, new.rsplit("-", 1)[0], legacy_aliases().get(old)))
            ):
                continue
            if _rename(old, new, entries, other):
                migrated[old] = new
            else:
                print(f"Preserved {old}: wrapper, snapshot or destination differs from its receipt.", file=sys.stderr)
    from clitw.builders import migrate_exports

    try:
        migrate_exports("openapi", migrated)
    except (OSError, ValueError) as exc:
        if require_exports:
            raise
        print(f"OpenAPI PATH export still needs attention: {exc}", file=sys.stderr)
    return migrated


def migrate_for_cli(names=None, *, require_exports: bool = True) -> dict[str, str] | None:
    """Keep an export conflict retryable without leaking a native traceback into a CLI."""
    try:
        return migrate_names(names, require_exports=require_exports)
    except (OSError, TypeError, ValueError) as exc:
        reason = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
        print(f"OpenAPI name migration failed: {reason}", file=sys.stderr)
        return None
