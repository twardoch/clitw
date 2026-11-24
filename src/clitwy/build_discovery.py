# this_file: src/clitwy/build_discovery.py
"""Noninteractive, bounded MCP schema discovery for the standalone builder."""

from __future__ import annotations

import concurrent.futures
import json
import os
import sys
import tempfile
import time
from pathlib import Path

from .mcp.config import connection_pin, endpoint
from .mcp.probe import invoke as capture
from .paths import home


def check(source: str, conf: dict, name: str, row: dict, timeout: float) -> str:
    """Validate tools/list using the pinned runtime; no installed provider is required."""
    try:
        pin = connection_pin(*endpoint(row, conf))
    except (ValueError, TypeError, KeyError):
        return "invalid connection configuration"
    with tempfile.TemporaryDirectory(prefix="mcx-schema-") as tmp:
        work = Path(tmp)
        request = work / "request.json"
        request.write_text(json.dumps([source, name, pin]), encoding="utf-8")
        command = [sys.executable, "-m", "clitwy.build_discovery", str(request)]
        env = dict(os.environ, CLITW_MCP_NONINTERACTIVE="1", CI="1")
        tools, reason, status = capture(command, [], env, time.monotonic() + timeout)
        if reason:
            if status == 401 and (work / "bearer").is_file():
                if name.endswith("-mcpo"):
                    return "sign-in required"
                if name.endswith("-mcpk"):
                    return "key required"
                return "authentication required; configure an explicit key or OAuth binding"
            return f"HTTP {status}" if status else reason
        if not isinstance(tools, list) or not tools or not all(isinstance(n, str) and n.strip() for n in tools):
            return "empty or invalid tool list"
        return "ready"


def discover(source: str, conf: dict, rows: dict, jobs: int, timeout: float) -> set[str]:
    accepted = set()
    with concurrent.futures.ThreadPoolExecutor(max_workers=jobs) as pool:
        futures = {pool.submit(check, source, conf, name, row, timeout): name for name, row in rows.items()}
        for done in concurrent.futures.as_completed(futures):
            name, result = futures[done], done.result()
            if result in {"ready", "sign-in required", "key required"}:
                accepted.add(name)
                if result == "sign-in required":
                    print(f"{name}: pending sign-in (HTTP 401 Bearer challenge)", flush=True)
                elif result == "key required":
                    print(f"{name}: API key required (HTTP 401)", flush=True)
            else:
                print(f"{name}: skipped ({result})", flush=True)
    return accepted


def _child(request: Path) -> int:
    from .mcp.runtime import invoke

    source, name, pin = json.loads(request.read_bytes())
    try:
        invoke(
            str(home()),
            source,
            name,
            pin,
            ["--refresh", "--list", "--compact", "--json"],
            challenge=(request.parent / "bearer").touch,
        )
        return 0
    except (ValueError, KeyError, OSError) as exc:
        # The runtime redacts errors. Forward only its HTTP status, never server output or URLs.
        import re

        if m := re.search(r"MCP (endpoint|request) returned HTTP ([45]\d\d)\b", str(exc)):
            print(f"MCP request returned HTTP {m[2]}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(_child(Path(sys.argv[1])))
