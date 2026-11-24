# this_file: src/clitwy/utilities/doctor.py
"""Discover commands on package-installed wrappers and inspect command help."""

import argparse
import json
import math
import os
import re
import signal
import subprocess
import tempfile
import time

from clitwy import paths


def _inventory() -> dict[str, str]:
    result = {}
    for filename, kind in (("openapi.json", "openapi"), ("baked.json", "mcp")):
        path = paths.registry_dir() / filename
        if not path.is_file():
            continue
        rows = json.loads(path.read_text())
        for name, row in rows.items():
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", name):
                continue
            if kind == "mcp" and (not row.get("clitw_mcp") or row["clitw_mcp"].get("hold")):
                continue
            result[name] = kind
    return result


def _invoke(prefix: list[str], args: list[str], deadline: float, capture: bool = False) -> bytes:
    env = dict(os.environ, CLITW_MCP_NONINTERACTIVE="1", MCX_MCP_NONINTERACTIVE="1", BROWSER="false", CI="1")
    with tempfile.TemporaryFile() as output:
        proc = subprocess.Popen(
            [*prefix, *args],
            stdin=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdout=output if capture else subprocess.DEVNULL,
            env=env,
            start_new_session=True,
        )
        try:
            code = proc.wait(timeout=max(0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            try:
                if os.name == "posix":
                    os.killpg(proc.pid, signal.SIGKILL)
                else:
                    proc.kill()
            except ProcessLookupError:
                pass
            proc.wait()
            raise ValueError("timed out") from None
        if code:
            raise ValueError(f"exit {code}")
        if output.tell() > 8 * 1024 * 1024:
            raise ValueError("tool list exceeds 8 MiB")
        output.seek(0)
        return output.read()


def cli(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="tw doctor", description=__doc__)
    parser.add_argument("--kind", choices=["all", "mcp", "openapi", "spec"], default="all")
    parser.add_argument("--only", help="Comma-separated name substrings")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--timeout", type=float, default=60, help="Maximum seconds per wrapper")
    opt = parser.parse_args(argv)
    if not math.isfinite(opt.timeout) or opt.timeout <= 0 or (opt.limit is not None and opt.limit < 0):
        parser.error("timeout must be positive; limit must be nonnegative")
    kind = "openapi" if opt.kind == "spec" else opt.kind
    filters = [v.strip() for v in (opt.only or "").split(",") if v.strip()]
    names = sorted(
        n for n, k in _inventory().items() if (kind in (k, "all")) and (not filters or any(f in n for f in filters))
    )
    if not names:
        print("No matching installed wrappers.")
    failed = False
    for name in names[: opt.limit]:
        deadline = time.monotonic() + opt.timeout
        try:
            prefix = paths.provider_argv(name)
            raw = _invoke(prefix, ["--list", "--compact", "--json"], deadline, capture=True)
            commands = json.loads(raw)
            if not isinstance(commands, list) or not commands:
                raise ValueError("no commands")
            # mcp2cli compact MCP is a list of strings. OpenAPI versions may supply named rows.
            first = commands[0]
            command = first.get("name", first.get("command")) if isinstance(first, dict) else first
            if not isinstance(command, str) or not re.fullmatch(r"[\w][\w.:-]*", command):
                raise ValueError("invalid command list")
            _invoke(prefix, [command, "--help"], deadline)
            print(f"{name}\tok\t{len(commands)} commands\t{command} --help")
        except (OSError, ValueError) as exc:
            # Never echo arbitrary wrapper output (which may include authorization or personal data).
            reason = type(exc).__name__ if isinstance(exc, (OSError, json.JSONDecodeError, UnicodeError)) else str(exc)
            print(f"{name}\tfailed\t{reason}")
            failed = True
    return int(failed)
