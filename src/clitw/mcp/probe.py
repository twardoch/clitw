# this_file: src/clitw/mcp/probe.py
"""Probe installed MCP clients with fresh discovery and a vetted read-only operation where available."""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import math
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
from collections import Counter
from contextlib import nullcontext
from pathlib import Path

from clitw.paths import provider_argv, provider_path, registry_dir

from .config import connection_pin, endpoint, held, load, presets, read_config, resolve_name
from .install import owned


def failure(stderr: str) -> str:
    error = stderr.lower()
    for fragment, reason in (
        ("quarantined", "quarantined"),
        ("disabled", "disabled"),
        ("403", "forbidden"),
        ("401", "authentication_required"),
        ("unauthorized", "authentication_required"),
        ("oauth", "authentication_required"),
        ("is not set", "missing_credential"),
        ("missing configured environment variable", "missing_credential"),
        ("429", "rate_limited"),
        ("400", "bad_request"),
        ("404", "endpoint_not_found"),
        ("405", "method_not_allowed"),
        ("406", "not_acceptable"),
        ("500", "internal_server_error"),
        ("502", "bad_gateway"),
        ("503", "service_unavailable"),
        ("504", "upstream_timeout"),
        ("server returned an error response", "mcp_server_error"),
        ("timed out", "timeout"),
        ("connection refused", "connection_refused"),
        ("connecterror", "connection_failed"),
        ("no such file or directory", "missing_dependency"),
        ("could not connect", "connection_failed"),
        ("failed to connect", "connection_failed"),
        ("unexpected extra argument", "invalid_arguments"),
    ):
        if fragment in error:
            return reason
    return "wrapper_error"


def invoke(command: list[str], args: list[str], env: dict, deadline: float) -> tuple:
    """Return parsed JSON and a bounded reason. Keep raw output out of the report."""
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return None, "timeout", None
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        try:
            proc = subprocess.Popen(
                [*command, *args],
                stdin=subprocess.DEVNULL,
                stdout=out,
                stderr=err,
                env=env,
                start_new_session=os.name != "nt",
            )
        except OSError:
            return None, "wrapper_unavailable", None
        try:
            proc.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            try:
                if os.name == "nt":
                    proc.kill()
                else:
                    os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.wait()
            return None, "timeout", None
        if out.tell() > 8 * 1024 * 1024:
            return None, "response_too_large", None
        err.seek(max(0, err.tell() - 8192))
        out.seek(0)
        try:
            payload = json.load(out)
        except (ValueError, UnicodeError):
            payload = None
        if isinstance(payload, dict) and payload.get("isError"):
            return None, "tool_error", None
        if proc.returncode:
            diagnostic = err.read().decode(errors="replace")
            endpoint = re.search(r"MCP (?:endpoint|request) returned HTTP ([45]\d\d)\b", diagnostic)
            if endpoint:
                status = int(endpoint[1])
                return None, failure(f"HTTP {status}"), status
            return None, failure(diagnostic), None
        return payload, None, None


def tool_failed(data: object) -> bool:
    """Some servers encode application errors as successful text blocks containing JSON."""
    if not isinstance(data, dict):
        return False
    return bool(
        data.get("error")
        or data.get("success") is False
        or data.get("ok") is False
        or data.get("status") in ("error", "failed")
    )


def probe(name: str, row: dict, timeout: float) -> dict:
    result = {
        "name": name,
        "phase": "discovery",
        "operation": "tools/list",
        "status": "failed",
        "reason": "",
        "tool_count": 0,
    }
    if row["clitw_mcp"].get("hold"):
        return dict(result, status="skipped", reason=row["clitw_mcp"]["hold"])
    path = provider_path(name)
    if not path.is_file() or not owned(name, row, path):
        return dict(result, reason="unmanaged_wrapper")
    meta = row["clitw_mcp"]
    try:
        conf, rows = read_config(meta["config"], name)
        configured = rows.get(name)
        if configured is None:
            return dict(result, status="skipped", reason="removed")
        if reason := held(configured):
            return dict(result, status="skipped", reason=reason)
        mode, url, transport = endpoint(configured, conf)
        if meta["pin"] != connection_pin(mode, url, transport):
            return dict(result, reason="endpoint_changed")
    except (OSError, KeyError, ValueError):
        return dict(result, reason="invalid_config")
    # Check the exact configured public URL instead of trusting a selected provider name.
    preset = presets().get(name, {})
    plan = preset.get("read") if mode == "direct" and url == preset.get("url") else None
    env = dict(os.environ, CLITW_MCP_NONINTERACTIVE="1", CI="1")
    deadline = time.monotonic() + timeout
    names, reason, status = invoke(provider_argv(name), ["--refresh", "--list", "--compact", "--json"], env, deadline)
    if status:
        result["http_status"] = status
    if reason:
        return dict(result, reason=reason)
    if not isinstance(names, list) or not all(isinstance(n, str) and n for n in names):
        return dict(result, reason="invalid_tool_list")
    if not names:
        return dict(result, reason="empty_tool_list")
    result["tool_count"] = len(names)
    command = plan[0] if plan and plan[0] in names else None
    if not command:
        return dict(result, status="passed", reason="discovery_only")
    result.update(phase="operation", operation=command)
    payload, reason, status = invoke(provider_argv(name), ["--json", command, *plan[1:]], env, deadline)
    if status:
        result["http_status"] = status
    if reason:
        return dict(result, reason=reason)
    if not isinstance(payload, dict) or not isinstance(payload.get("content"), list):
        return dict(result, reason="invalid_tool_result")
    data = payload.get("structuredContent")
    if tool_failed(data):
        return dict(result, reason="tool_error")
    for block in payload["content"]:
        text = block.get("text", "") if isinstance(block, dict) else ""
        if not isinstance(text, str):
            continue
        text = text.strip()
        if text.lower().startswith(("error:", "authentication required", "unauthorized:")):
            return dict(result, reason="tool_error")
        if text.startswith("{"):
            try:
                if tool_failed(json.loads(text)):
                    return dict(result, reason="tool_error")
            except ValueError:
                pass
    return dict(result, status="passed", reason="read_only_call")


def cli_try(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="tw mcp try", description=__doc__)
    parser.add_argument("names", nargs="*", metavar="NAME")
    parser.add_argument("--output", type=Path, help="Write JSON Lines to a file (default: stdout)")
    parser.add_argument(
        "--timeout", type=float, default=30, help="Seconds per wrapper, including its optional operation"
    )
    parser.add_argument("--jobs", type=int, default=3, help="Concurrent wrappers (1–16)")
    opts = parser.parse_args(argv)
    if not math.isfinite(opts.timeout) or opts.timeout <= 0 or not 1 <= opts.jobs <= 16:
        parser.error("use a positive finite timeout and 1–16 jobs")
    registry_file = registry_dir() / "baked.json"
    try:
        registry = load(registry_file) if registry_file.exists() else {}
    except (OSError, ValueError):
        parser.error("could not read clitw registry")
    wrappers = {
        n: r
        for n, r in registry.items()
        if re.fullmatch(r"mcx-[a-z0-9-]+", n) and isinstance(r, dict) and "clitw_mcp" in r
    }
    try:
        opts.names = [resolve_name(n, wrappers) for n in opts.names]
    except ValueError as exc:
        parser.error(str(exc))
    if not wrappers:
        parser.error("no matching installed MCP wrapper")
    counts = Counter()
    stream = opts.output.open("w") if opts.output else nullcontext(sys.stdout)
    with stream as output, concurrent.futures.ThreadPoolExecutor(opts.jobs) as pool:
        tasks = [pool.submit(probe, n, wrappers[n], opts.timeout) for n in sorted(set(opts.names) or wrappers)]
        for task in concurrent.futures.as_completed(tasks):
            result = task.result()
            counts[result["status"]] += 1
            if result["reason"] == "authentication_required":
                counts["auth_required"] += 1
            print(json.dumps(result), file=output, flush=True)
    print(
        "MCP wrappers:",
        ", ".join(f"{counts[k]} {k}" for k in ("passed", "failed", "skipped")),
        file=sys.stderr,
    )
    if counts["auth_required"]:
        print(f"{counts['auth_required']} require authentication. Run tw auth to configure access.", file=sys.stderr)
    return 1 if counts["failed"] else 0
