# this_file: src/clitwy/openapi/native.py
"""Entrypoint for package-managed native OpenAPI wrapper files."""

from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path

from clitwy.credentials import available_keys, reviewed_openapi
from clitwy.paths import home

from .state import HEADER, NO_BASE, header_args, registry, scoped_environment, valid_url


def blocked(arg: str) -> bool:
    option = arg.partition("=")[0]
    sources = ("--spec", "--base-url", "--mcp", "--mcp-stdio", "--graphql", "--graphql-endpoint")
    return (
        arg.startswith("@")
        or option.startswith("--mcp")
        or (option.startswith("--") and len(option) > 2 and any(s.startswith(option) for s in sources))
    )


def execute(name: str, argv: list[str]) -> int:
    try:
        entry = registry()[name]
        from .upgrade import RETIRED, former_default, intact

        if name in RETIRED and former_default(name, entry) and intact(name, entry):
            raise ValueError("this bundled integration was retired; configure a new --spec to use a custom service")
        snapshot = Path(entry["source"])
        if (
            snapshot.parent != home() / "openapi" / name
            or snapshot.is_symlink()
            or (hashlib.sha256(snapshot.read_bytes()).hexdigest() + ".json" != snapshot.name)
        ):
            raise ValueError("OpenAPI snapshot failed content verification; reinstall the wrapper")
        base = entry["base_url"]
        if base and not valid_url(base, base=True):
            raise ValueError("invalid configured API base")
        from mcp2cli import _build_main_parser, _split_at_subcommand

        globals_, operation = _split_at_subcommand(argv, _build_main_parser())
        if any(blocked(arg) for arg in globals_) or (operation and operation[0].startswith("@")):
            raise ValueError("a named wrapper only accepts its configured API source and destination")
        if not base and not ({"-h", "--help", "--list", "--version"} & set(argv)):
            raise ValueError("configure this wrapper's API origin with tw openapi install NAME --base-url URL")
        env = scoped_environment()
        auth = entry.get("auth", "")
        if not os.getenv("CLITW_OPENAPI_ANONYMOUS"):
            values = available_keys() if auth and reviewed_openapi(name, entry) else os.environ
            for template in auth.split(","):
                match = HEADER.fullmatch(template)
                if match:
                    for key in (match[3], match[4]):
                        if key and key in values:
                            env[key] = values[key]
        if policy := os.environ.get("CLITW_OPENAPI_TRACE"):
            env["CLITW_OPENAPI_TRACE"] = policy
        native = [
            sys.executable,
            "-m",
            "clitwy.openapi.native",
            "--spec",
            str(snapshot),
            "--base-url",
            base or NO_BASE,
            *header_args(auth, env),
            *argv,
        ]
        os.execve(sys.executable, native, env)
    except (KeyError, OSError, ValueError) as exc:
        print(f"{name}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    if sys.argv[1:2] == ["--wrapper"]:
        raise SystemExit(execute(sys.argv[2], sys.argv[3:]))
    if os.getenv("CLITW_OPENAPI_TRACE"):
        from .trace import observe

        observe(json.loads(os.environ["CLITW_OPENAPI_TRACE"]))
    import mcp2cli

    if sys.argv[1:2] == ["--spec"] and Path(sys.argv[2]).parent.name in {"mcx-docusign", "mcx-docusign-api"}:
        from .references import resolve_shared

        mcp2cli.resolve_refs = resolve_shared

    raise SystemExit(mcp2cli.main())
