# this_file: src/clitwy/openapi/probe.py
"""Run a reviewed read through each installed OpenAPI CLI and emit body-free JSON Lines."""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import logging
import os
import re
import subprocess
import sys
import tempfile
import time
from collections import Counter
from contextlib import nullcontext
from pathlib import Path

from clitwy.credentials import available_keys, reviewed_openapi
from clitwy.paths import provider_path

from .catalogue import Probe, Tool, providers
from .install import canonical
from .schema import prepare
from .state import HEADER, NO_BASE, header_args, launcher, registry, scoped_environment


def scoped_keys(auth: str, source: dict[str, str]) -> dict[str, str]:
    keys = {}
    for header in auth.split(","):
        match = HEADER.fullmatch(header)
        if match:
            for key in (match[3], match[4]):
                if key and source.get(key):
                    keys[key] = source[key]
    return keys


def arguments(recipe: Probe, op: dict, values: dict[str, str]) -> list[str]:
    args = recipe.arguments
    if not isinstance(args, list) or len(args) % 2:
        raise ValueError("invalid reviewed arguments")
    supported = {"--" + p["name"].replace("_", "-") for p in op.get("parameters", [])}
    out = []
    for flag, val in zip(args[::2], args[1::2]):
        if flag not in supported or val.startswith("--"):
            raise ValueError("reviewed argument is absent from native command")
        if val.startswith("@env:"):
            key = val[5:]
            if key not in {"GOOGLE_DOC_ID", "GOOGLE_CSE_ID", "CLOUDINARY_API_CLOUD", "FLY_ORG_SLUG"}:
                raise ValueError("unsupported account identifier")
            val = values.get(key, "")
            if not re.fullmatch(r"[a-zA-Z0-9_:-]+", val):
                raise ValueError("required provider account identifier is missing or invalid")
        out.extend((flag, val))
    return out


def check(name: str, entry: dict, secrets: dict, explicit: str | None, timeout: float, work: Path) -> dict:
    row = {
        "name": name,
        "status": "failed",
        "phase": "discovery",
        "configuration": "installed",
        "credential_supplied": False,
        "http_status": None,
        "cli_invoked": False,
    }
    deadline = time.monotonic() + timeout
    work.mkdir()
    receipt = work / "receipt.json"
    env = scoped_environment()
    base, auth = entry["base_url"], entry["auth"]
    env["CLITW_OPENAPI_ANONYMOUS"] = "1"
    env["CLITW_OPENAPI_TRACE"] = json.dumps({"method": "DISCOVERY", "target": NO_BASE, "receipt": str(receipt)})
    command = launcher(name)

    def call(args, capture=False, environment=None):
        return subprocess.run(
            args,
            env=environment or env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=max(0.01, deadline - time.monotonic()),
        )

    def listing(cmd):
        discovery = call([*cmd, "--list", "--json"], True)
        if discovery.returncode:
            raise ValueError("native CLI could not list its local schema")
        return json.loads(discovery.stdout)

    try:
        exe = provider_path(name)
        if exe.is_symlink() or hashlib.sha256(exe.read_bytes()).hexdigest() != entry.get("wrapper_sha256"):
            raise ValueError("installed wrapper is missing or modified")
        row["cli_invoked"] = True
        ops = listing(command)
        row["tool_count"] = len(ops)
        preset = providers().get(name) if entry.get("preset") else None
        if explicit:
            recipe = Probe("GET", explicit, [])
        elif preset and (not base or not preset[0].base or base.rstrip("/") == preset[0].base.rstrip("/")):
            recipe = preset[1]
        else:
            return {**row, "status": "skipped", "reason": "no reviewed read for this user configuration"}
        if recipe.method == "-":
            return {**row, "status": "skipped", "reason": recipe.arguments}
        if not base and not recipe.base:
            return {**row, "status": "skipped", "reason": "configure an API base URL to try this read"}
        if recipe.method not in {"GET", "POST"}:
            raise ValueError("unsupported probe method")
        row.update(method=recipe.method, route=recipe.route)
        predicate = lambda op: op.get("method", "").upper() == recipe.method and op.get("path") == recipe.route
        matches = [op for op in ops if predicate(op)]
        alt = bool(recipe.source and recipe.base and (not matches or base.rstrip("/") != recipe.base.rstrip("/")))
        if alt:
            base, auth = recipe.base, recipe.auth if recipe.auth != "=" else auth
            row.update(configuration="alternate-reviewed-schema", phase="schema")
            prep = call(
                [sys.executable, "-m", "clitwy.openapi.probe", "--stage", name, recipe.source, base, auth, str(work)],
                True,
            )
            if prep.returncode:
                raise ValueError("could not prepare the reviewed alternate schema")
            source = json.loads(prep.stdout)
            command = [sys.executable, "-m", "clitwy.openapi.native", "--spec", source, "--base-url", base]
            matches = [op for op in listing(command) if predicate(op)]
        if len(matches) != 1 or not base:
            raise ValueError("live read requires a configured destination and matching operation")
        op = matches[0]
        row["operation"] = op["name"]
        args = arguments(recipe, op, secrets)
        # Public probes can omit a credential even if other operations on this CLI require it.
        auth = auth if recipe.auth == "=" else recipe.auth
        keys = scoped_keys(auth, secrets if reviewed_openapi(name, entry) else os.environ)
        live = dict(env, **keys)
        if keys:
            live.pop("CLITW_OPENAPI_ANONYMOUS", None)
        live["CLITW_OPENAPI_TRACE"] = json.dumps(
            {"method": recipe.method, "target": base.rstrip("/") + recipe.route, "receipt": str(receipt)}
        )
        if alt:
            command += header_args(auth, live)
        row.update(phase="live", status="failed", credential_supplied=bool(keys))
        proc = call([*command, op["name"], *args], environment=live)
        data = json.loads(receipt.read_bytes()) if receipt.exists() else {}
        # Both providers document a public model list. Record credential failure before the public attempt.
        if (
            name in {"mcx-opencode-zen-apik", "mcx-orcarouter-apik"}
            and recipe.route == "/models"
            and not alt
            and keys
            and data.get("http_status") in (401, 403)
        ):
            row.update(credential_http_status=data["http_status"], credential_supplied=False, public_fallback=True)
            live["CLITW_OPENAPI_ANONYMOUS"] = "1"
            receipt.unlink()
            proc = call([*command, op["name"], *args], environment=live)
            data = json.loads(receipt.read_bytes()) if receipt.exists() else {}
        code, media = data.get("http_status"), data.get("kind")
        row["http_status"] = code
        if code is None:
            return {
                **row,
                "reason": "native transport returned no HTTP response",
                **({"network_error": data["network_error"]} if data.get("network_error") else {}),
            }
        if proc.returncode or not 200 <= code < 300 or media != "api":
            return {**row, "reason": f"provider returned HTTP {code} ({media})"}
        return {**row, "status": "passed", "reason": f"provider returned HTTP {code}"}
    except subprocess.TimeoutExpired:
        return {**row, "reason": "probe deadline expired"}
    except (ValueError, KeyError, OSError, TypeError) as exc:
        return {
            **row,
            "reason": str(exc)
            if isinstance(exc, ValueError) and not isinstance(exc, json.JSONDecodeError)
            else f"could not complete {row['phase']} ({type(exc).__name__})",
        }


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="tw openapi try", description=__doc__)
    parser.add_argument("names", nargs="*")
    parser.add_argument("--jobs", type=int, default=8)
    parser.add_argument("--timeout", type=float, default=60)
    parser.add_argument("--output", type=Path, help="JSONL report path (default: stdout)")
    parser.add_argument(
        "--env-file", type=Path, help="optional dotenv credentials; process environment takes precedence"
    )
    parser.add_argument("--probe-get", metavar="/PATH", help="explicit read-only GET for a single user-selected API")
    opts = parser.parse_args(argv)
    if opts.jobs <= 0 or opts.timeout <= 0:
        parser.error("jobs and timeout must be positive")
    from .upgrade import migrate_for_cli

    if migrate_for_cli(opts.names, require_exports=False) is None:
        return 1
    entries = registry()
    try:
        wanted = sorted({canonical(n, entries) for n in opts.names} or entries.keys())
    except ValueError as error:
        parser.error(str(error))
    if any(n not in entries for n in wanted):
        parser.error("requested OpenAPI integration is not installed")
    if opts.probe_get and (len(wanted) != 1 or not re.fullmatch(r"/(?!/)[a-zA-Z0-9_./-]*", opts.probe_get)):
        parser.error("--probe-get needs one selected integration and a literal OpenAPI GET path")
    logging.getLogger("dotenv.main").setLevel(logging.CRITICAL)
    try:
        secrets = available_keys(opts.env_file)
    except ValueError as exc:
        parser.error(str(exc))
    counts = Counter()
    with (
        (
            open(opts.output, "w", encoding="utf-8", opener=lambda p, f: os.open(p, f, 0o600))
            if opts.output
            else nullcontext(sys.stdout)
        ) as out,
        tempfile.TemporaryDirectory(prefix="clitwy-check-") as tmp,
        concurrent.futures.ThreadPoolExecutor(max_workers=opts.jobs) as pool,
    ):
        futures = [
            pool.submit(check, name, entries[name], secrets, opts.probe_get, opts.timeout, Path(tmp) / name)
            for name in wanted
        ]
        for future in concurrent.futures.as_completed(futures):
            row = future.result()
            counts[row["status"]] += 1
            print(json.dumps(row), file=out, flush=True)
    print(" ".join(f"{k}={counts[k]}" for k in ("passed", "failed", "skipped")), file=sys.stderr)
    return int(bool(counts["failed"]))


if __name__ == "__main__":
    if len(sys.argv) == 7 and sys.argv[1] == "--stage":
        _, _, name, url, base, auth, directory = sys.argv
        snapshot, *_ = prepare(Tool(name, url, base, auth, ""), Path(directory))
        print(json.dumps(str(snapshot)))
    else:
        raise SystemExit(main(sys.argv[1:]))
