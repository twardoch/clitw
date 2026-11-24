# this_file: src/clitw/mcp/runtime.py
"""Invoke an installed MCP wrapper without placing configured secrets in process arguments."""

from __future__ import annotations

import contextlib
import logging
import os
import re
import sys
from urllib.parse import parse_qs, unquote, urlencode

from clitw.credentials import available_keys, custom_mcp_value, oauth_client_variables
from clitw.paths import home, registry_dir

from .config import (
    PUBLIC_OAUTH_URLS,
    SAFE_PATH,
    connection_pin,
    endpoint,
    held,
    parts,
    presets,
    read_config,
    server_slug,
)
from .oauth import cached_bearer, preserve_requested_scope


def expand(value: str, conf: dict, seen: tuple = ()) -> str:
    """Resolve only named MCPProxy/environment references and reject cycles."""

    def replace(match):
        key = match.group(1)
        if key in seen or len(seen) >= 24:
            raise ValueError(f"cyclic environment reference for {key}")
        val = custom_mcp_value(key, conf)
        if not val:
            raise ValueError(f"missing configured environment variable {key}")
        return expand(val, conf, (*seen, key))

    result = re.sub(r"\$\{(?:env:)?([A-Za-z_][A-Za-z0-9_]*)\}", replace, value)
    if "${" in result or "\r" in result or "\n" in result:
        raise ValueError("unsupported secret reference or newline in connection settings")
    return result


def safe_transport(url: str, failures: list[int], challenge=None) -> None:
    """The MCP SDK uses httpx in v1 and httpx2 in v2. Pin redirects and SSE message destinations in either."""
    from importlib.util import find_spec

    import httpx

    clients = [httpx.AsyncClient]
    if find_spec("httpx2"):
        import httpx2

        if httpx2.AsyncClient not in clients:
            clients.append(httpx2.AsyncClient)
    expected = httpx.URL(url)
    for cls in clients:
        send = cls._send_single_request

        async def pinned(client, request, send=send):
            if (request.url.scheme, request.url.host, request.url.port) != (
                expected.scheme,
                expected.host,
                expected.port,
            ):
                raise ValueError("MCP server attempted a cross-origin connection")
            try:
                response = await send(client, request)
            except Exception as exc:
                if type(exc).__name__ == "ConnectError":
                    print("MCP endpoint connection failed (ConnectError).", file=sys.stderr)
                raise
            if (
                challenge
                and response.status_code == 401
                and re.search(
                    r"(?:^|,)\s*Bearer(?:\s|,|$)", response.headers.get("WWW-Authenticate", ""), re.IGNORECASE
                )
            ):
                challenge()
            if response.is_error:
                failures.append(response.status_code)
            return response

        cls._send_single_request = pinned


class Redact:
    """Retain diagnostics, removing private URLs and provider credentials before writing them."""

    def __init__(self, stream, values):
        self.stream = stream
        self.values = sorted(set(filter(None, values)), key=len, reverse=True)

    def write(self, text):
        for secret in self.values:
            text = text.replace(secret, "[redacted]")
        return self.stream.write(text)

    def __getattr__(self, name):
        return getattr(self.stream, name)


def invoke(state: str, source: str, name: str, pin: str, args: list[str], *, challenge=None) -> None:
    # mcp2cli binds its cache/config paths at import time; each installed wrapper is a separate process.
    os.environ["CLITW_HOME"] = state
    cache_dir = home() / "cache" / "mcp2cli"
    (cache_dir / "oauth").mkdir(mode=0o700, parents=True, exist_ok=True)
    os.environ["MCP2CLI_CACHE_DIR"] = str(cache_dir)
    os.environ["MCP2CLI_CONFIG_DIR"] = str(registry_dir())
    import mcp2cli

    conf, rows = read_config(source, name)
    row = rows.get(name)
    if row is None or held(row):
        raise ValueError("server is removed, disabled or quarantined")
    mode, raw, transport = endpoint(row, conf)
    auth_mode = row["auth_mode"]
    if connection_pin(mode, raw, transport) != pin:
        raise ValueError("server endpoint changed; rerun tw mcp install")
    url = expand(raw, conf)
    u = parts(url)
    # Subcommand arguments with these names still belong to the upstream tool.
    globals_, _ = mcp2cli._split_at_subcommand(args, mcp2cli._build_main_parser())
    flags = {arg.split("=", 1)[0] for arg in globals_}
    if flags & {
        "--mcp",
        "--spec",
        "--mcp-stdio",
        "--graphql",
        "--base-url",
        "--session",
        "--session-start",
        "--transport",
    }:
        raise ValueError("the installed wrapper pins its endpoint and transport")
    oauth = any(flag.startswith("--oauth") for flag in flags)
    if oauth and auth_mode != "oauth":
        raise ValueError("this MCP binding is not configured for OAuth")
    if oauth and (os.getenv("CLITW_MCP_NONINTERACTIVE") == "1" or mode == "proxy"):
        raise ValueError("interactive OAuth is disabled for this invocation")
    if mode == "proxy":
        key = conf.get("api_key")
        headers = {"X-API-Key": expand(key, conf)} if key else {}
    else:
        headers = {key: expand(val, conf) for key, val in (row.get("headers") or {}).items()}
        if auth_mode == "oauth" and not oauth and not any(k.lower() == "authorization" for k in headers):
            if token := cached_bearer(url, cache_dir):
                headers["Authorization"] = "Bearer " + token
        elif auth_mode == "key" and not headers:
            catalogue = presets()
            preset = next((p for p in catalogue.values() if p["url"] == raw and p.get("auth_env")), {})
            binding = row if row.get("auth_env") else preset
            variable = binding.get("auth_env")
            key = None
            if variable:
                if not isinstance(variable, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", variable):
                    raise ValueError("invalid auth_env variable name")
                if source == "catalog" and catalogue.get(name) == preset and variable == preset.get("auth_env"):
                    key = available_keys().get(variable)
                else:
                    value = custom_mcp_value(variable, conf)
                    key = expand(value, conf) if value else None
            if key:
                if any(ord(c) < 32 or ord(c) == 127 for c in key):
                    raise ValueError("credential contains a control character")
                style = binding.get("auth_style", "bearer")
                if style.startswith("query:"):
                    url += ("&" if "?" in url else "?") + urlencode({style.removeprefix("query:"): key})
                    u = parts(url)
                elif style == "bearer":
                    headers["Authorization"] = "Bearer " + key
                else:
                    headers[style] = key
    if any(not re.fullmatch(r"[!#$%&'*+.^_`|~A-Za-z0-9-]+", key) for key in headers):
        raise ValueError("invalid configured header name")
    public_urls = {p["url"] for p in presets().values()}
    private_url = raw not in PUBLIC_OAUTH_URLS and bool(
        u.query or (raw not in public_urls and not SAFE_PATH.fullmatch(u.path))
    )
    if oauth and (headers or private_url):
        raise ValueError("use explicit OAuth only on public endpoint URLs without configured credentials")
    # Permit the provider's standard OAuth metadata discovery and token endpoints only during explicit login.
    # All normal requests, including redirects and SSE posts, are origin-bound.
    failures = []
    if not oauth:
        safe_transport(url, failures, challenge)
    argv = ["--mcp", url, "--transport", transport]
    oauth_secrets = []
    if oauth:
        opts = row.get("oauth") or {}
        preset = presets().get(name, {})
        app_keys = oauth_client_variables(name, preset) if source == "catalog" and raw == preset.get("url") else {}
        for key, flag in (
            ("client_id", "--oauth-client-id"),
            ("client_secret", "--oauth-client-secret"),
            ("scopes", "--oauth-scope"),
            ("redirect_uri", "--oauth-redirect-uri"),
        ):
            if opts.get(key) and flag not in flags:
                v = opts[key]
                if key in app_keys:
                    variable = app_keys[key]
                    resolved = available_keys().get(variable)
                    if not resolved:
                        raise ValueError(f"missing configured environment variable {variable}; run tw auth keys {name}")
                else:
                    resolved = " ".join(v) if isinstance(v, list) else expand(v, conf)
                argv.extend([flag, resolved])
                if key == "client_secret":
                    oauth_secrets.append(resolved)
    for idx, (key, val) in enumerate(headers.items()):
        variable = f"_CLITW_MCP_HEADER_{idx}"
        os.environ[variable] = val
        argv.extend(["--auth-header", f"{key}:env:{variable}"])
    redactions = [*headers.values(), *oauth_secrets]
    redactions += [v.split(" ", 1)[1] for v in headers.values() if " " in v]
    if private_url:
        redactions.extend([url, *[v for values in parse_qs(u.query).values() for v in values]])
        redactions.extend(unquote(part) for part in u.path.split("/") if len(part) > 12)
    profile = mcp2cli.BakeConfig(prog=name, include=[server_slug(row) + "--*"] if mode == "proxy" else [])
    with (
        contextlib.redirect_stderr(Redact(sys.stderr, redactions)),
        contextlib.redirect_stdout(Redact(sys.stdout, redactions)),
        preserve_requested_scope(url) if oauth else contextlib.nullcontext(),
    ):
        # Prevent an SDK logger retaining the original stderr from emitting an unredacted URL.
        if redactions:
            logging.disable(logging.CRITICAL)
        try:
            mcp2cli._main_impl(argv + args, bake_config=profile)
        except SystemExit as exc:
            if exc.code and failures:
                status = failures[-1]
                if status in (401, 403):
                    hint = " Check configured server credentials."
                    if auth_mode == "oauth" and not u.query and mode == "direct":
                        hint = f" To authorize, run tw auth oauth {name}."
                else:
                    hint = ""
                print(f"MCP endpoint returned HTTP {status}.{hint}", file=sys.stderr)
            raise
        except Exception as exc:  # noqa: BLE001 -- upstream MCP servers can raise arbitrary SDK errors.
            # Uncaught SDK groups can contain private URLs. Extract the HTTP status without displaying the URL.
            leaves = mcp2cli._exc_leaves(exc)
            for leaf in leaves:
                code = getattr(getattr(leaf, "response", None), "status_code", None)
                if code is not None:
                    hint = " Check configured server credentials." if code in (401, 403) else ""
                    if code in (401, 403) and auth_mode == "oauth":
                        hint = f" To authorize, run tw auth oauth {name}."
                    raise ValueError(f"MCP request returned HTTP {code}.{hint}") from None
            reason = ", ".join(dict.fromkeys(type(leaf).__name__ for leaf in leaves))
            raise ValueError(f"MCP request failed ({reason}); check endpoint availability and credentials") from None


def main(argv: list[str]) -> int:
    if argv[:1] == ["--private"]:
        print(f"Run the installed {argv[1]} executable to use its private MCP endpoint.", file=sys.stderr)
        return 1
    if len(argv) < 4:
        print("Use tw mcp install to create an MCP wrapper.", file=sys.stderr)
        return 2
    try:
        invoke(*argv[:4], argv[4:])
        return 0
    except (OSError, ValueError, TypeError, KeyError) as exc:
        print(f"MCP wrapper: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
