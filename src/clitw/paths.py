# this_file: src/clitw/paths.py
"""Resolve the private clitw installation independently from the user's mcp2cli registry."""

import os
import sys
from pathlib import Path

from filelock import FileLock


def home() -> Path:
    default = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "clitw"
    return Path(os.environ.get("CLITW_HOME", default)).expanduser().resolve()


def bin_dir() -> Path:
    return home() / "bin"


def provider_path(name: str) -> Path:
    return bin_dir() / (f"{name}.py" if os.name == "nt" else name)


def provider_argv(name: str) -> list[str]:
    path = str(provider_path(name))
    return [sys.executable, path] if os.name == "nt" else [path]


def registry_dir() -> Path:
    return home() / "registry"


def install_lock() -> FileLock:
    home().mkdir(mode=0o700, parents=True, exist_ok=True)
    return FileLock(home() / "install.lock", timeout=180)
