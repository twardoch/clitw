# this_file: src/clitw/mcp/login.py
"""Guide one interactive MCP authorization at a time, with fresh tools/list verification."""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import ipaddress
import json
import math
import os
import re
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


@dataclass(frozen=True)
class Binding:
    """A verified installed executable and its public URL, or a reason to skip it."""

    name: str
    command: tuple[str, ...]
    url: str = ""
    skip: str = ""
    cache_dir: str = ""
    key_configured: bool = False
    client_configured: bool = False
    client_hint: str = ""
    legacy: str = ""


@dataclass(frozen=True)
class Status:
    text: str
    needs_login: bool = False
    works: bool = False


# The query flag on this documented route requests OAuth; it never contains a credential.
PUBLIC_OAUTH_URLS = {"https://huggingface.co/mcp?login", "https://mcp.context7.com/mcp/oauth"}


def public_https(url: str) -> bool:
    """Only public HTTPS origins can be selected for browser authorization."""
    if url in PUBLIC_OAUTH_URLS:
        return True
    try:
        u = urlsplit(url)
        host = (u.hostname or "").lower().rstrip(".")
        if not host or u.scheme != "https" or u.username is not None or u.password is not None or u.query or u.fragment:
            return False
        if (
            host in ("localhost", "localhost.localdomain", "home.arpa")
            or host.endswith((".localhost", ".local", ".localdomain", ".internal", ".home.arpa"))
            or "$" in u.netloc
        ):
            return False
        if u.port == 0:
            return False
        try:
            return ipaddress.ip_address(host).is_global
        except ValueError:
            return "." in host and bool(re.fullmatch(r"[a-z0-9.-]+", host))
    except (TypeError, ValueError):
        return False


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None


def metadata(url: str, timeout: float) -> dict:
    """Retrieve a public discovery document without authorization, cookies or redirects."""
    req = Request(url, headers={"Accept": "application/json", "User-Agent": "mcxadmin-oauth/1"})
    try:
        with build_opener(_NoRedirect()).open(req, timeout=timeout) as response:
            data = response.read(262145)
    except HTTPError as exc:
        exc.close()
        if exc.code == 429 or exc.code >= 500:
            raise OSError("OAuth metadata unavailable") from None
        return {}
    if len(data) > 262144:
        return {}
    try:
        obj = json.loads(data)
    except (ValueError, UnicodeError):
        return {}
    return obj if isinstance(obj, dict) else {}


def advertises_oauth(url: str, timeout: float) -> bool | None:
    """Check RFC 9728 or RFC 8414 metadata. No authorization, registration or token endpoint is called."""
    u = urlsplit(url)
    origin = f"{u.scheme}://{u.netloc}"
    rp = "/.well-known/oauth-protected-resource"
    paths = dict.fromkeys((rp + u.path.rstrip("/"), rp, "/.well-known/oauth-authorization-server"))
    deadline = time.monotonic() + timeout
    for path in paths:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        try:
            data = metadata(origin + path, remaining)
        except (OSError, URLError):
            return None
        if path.startswith(rp):
            resource = data.get("resource")
            issuers = data.get("authorization_servers")
            if (
                isinstance(resource, str)
                and public_https(resource)
                and ((origin + u.path).rstrip("/") + "/").startswith(resource.rstrip("/") + "/")
                and isinstance(issuers, list)
                and any(isinstance(item, str) and public_https(item) for item in issuers)
            ):
                return True
        elif (
            isinstance(data.get("issuer"), str)
            and data["issuer"].rstrip("/") == origin
            and all(
                isinstance(data.get(k), str) and public_https(data[k])
                for k in ("authorization_endpoint", "token_endpoint")
            )
        ):
            return True
    return False


def _env(interactive: bool) -> dict[str, str]:
    env = dict(os.environ)
    env.pop("MCP2CLI_DEBUG", None)
    for name in ("CLITW_MCP_NONINTERACTIVE", "MCX_MCP_NONINTERACTIVE"):
        if interactive:
            env.pop(name, None)
        else:
            env[name] = "1"
    return env


# Pika publishes its tools/list anonymously but protects list-apps. This exact operation is a read.
PIKA = "https://mcp.pika.art/mcp"
# Google exposes public schemas. Its account metadata is read only after explicit per-provider consent.
GOOGLE_READS = {
    "https://gmailmcp.googleapis.com/mcp/v1": ("list-labels",),
    "https://drivemcp.googleapis.com/mcp/v1": ("list-recent-files", "--page-size", "1", "--exclude-content-snippets"),
}
PROTECTED_READS = {PIKA: ("list-apps",), **GOOGLE_READS}
GOOGLE_CLIENT_HINT = (
    "configure a registered Google OAuth desktop client in this server's oauth.client_id and oauth.client_secret"
)


def cached(binding: Binding, file: str) -> dict:
    if not binding.cache_dir:
        return {}
    key = hashlib.sha256(binding.url.encode()).hexdigest()[:16]
    try:
        data = json.loads((Path(binding.cache_dir) / "oauth" / key / file).read_bytes())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def saved_grant(binding: Binding) -> bool:
    grant = cached(binding, "tokens.json")
    expiry = cached(binding, "tokens_meta.json").get("expires_at")
    current = (expiry is None and grant.get("expires_in") is None) or (
        isinstance(expiry, (int, float)) and expiry > time.time() + 10
    )
    return bool(grant.get("access_token") and str(grant.get("token_type", "bearer")).lower() == "bearer" and current)


def failure(message: str) -> Status:
    err = message.lower()
    codes = re.findall(
        r"\bhttp(?:/\d(?:\.\d)?)?\s+(\d{3})\b|\b(401|403|429)\s+(?:unauthorized|forbidden|too many requests)", err
    )
    code = next((a or b for a, b in reversed(codes)), None)
    if code in ("401", "403") or (not code and ("unauthorized" in err or "authentication required" in err)):
        return Status("authentication required" if code != "403" else "access forbidden (HTTP 403)", needs_login=True)
    if code:
        return Status("rate limited (HTTP 429)" if code == "429" else f"provider returned HTTP {code}")
    if re.search(r"server-sent event exceeded the \d+ byte limit", err):
        return Status("tool schema exceeded the SSE event size limit")
    if any(
        key in err
        for key in ("connecterror", "connection refused", "connection failed", "name resolution", "could not connect")
    ):
        return Status("endpoint unavailable")
    if "missing configured environment variable" in err:
        return Status("configured credential unavailable")
    if "quarantined" in err or "disabled" in err:
        return Status("disabled or quarantined")
    return Status("request failed")


def _query(binding: Binding, args: list[str], timeout: float):
    try:
        proc = subprocess.run(
            [*binding.command, *args],
            env=_env(False),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return None, Status("request timed out")
    except OSError:
        return None, Status("wrapper unavailable")
    if proc.returncode:
        err = proc.stderr[-8192:].decode(errors="replace")
        return None, failure(err)
    try:
        payload = json.loads(proc.stdout)
    except (ValueError, UnicodeError):
        return None, Status("invalid response")
    return payload, None


def verify_access(binding: Binding, timeout: float, *, consented: bool = False) -> Status:
    """Refresh discovery and verify the exact provider-approved nonmutating check when available."""
    names, error = _query(binding, ["--refresh", "--list", "--compact", "--json"], timeout)
    if error:
        return error
    if not isinstance(names, list) or not names or not all(isinstance(n, str) and n for n in names):
        return Status("empty or invalid tool list")
    read = PROTECTED_READS.get(binding.url)
    if not read or (binding.url in GOOGLE_READS and not consented):
        return Status(f"working discovery ({len(names)} tools); protected operations unverified", works=True)
    if read[0] not in names:
        return Status("protected verification operation unavailable")
    result, error = _query(binding, ["--json", *read], timeout)
    if error:
        return error
    if not isinstance(result, dict) or not isinstance(result.get("content"), list) or not result["content"]:
        return Status("invalid protected operation response")
    body = result.get("structuredContent") or {}
    for block in result["content"]:
        text = block.get("text", "") if isinstance(block, dict) else ""
        if not isinstance(text, str):
            continue
        if text.lstrip().startswith("{"):
            try:
                inner = json.loads(text)
                if isinstance(inner, dict) and (
                    inner.get("error") or inner.get("success") is False or inner.get("ok") is False
                ):
                    return failure(text)
            except ValueError:
                pass
        if (
            text.lower()
            .lstrip()
            .startswith(("error", "unauthorized", "authentication required", "http 401", "http 403"))
        ):
            return failure(text)
    if result.get("isError") or isinstance(body, dict) and (body.get("error") or body.get("success") is False):
        return failure(json.dumps(result))
    return Status(f"working ({len(names)} tools; protected {read[0]} verified)", works=True)


def check(binding: Binding, timeout: float) -> Status:
    if binding.skip:
        return Status(f"skipped ({binding.skip})")
    if not public_https(binding.url) or not binding.command:
        return Status("skipped (local or private endpoint)")
    result = verify_access(binding, timeout)
    if result.works:
        if binding.url == PIKA:
            return result
        if saved_grant(binding):
            return Status(f"{result.text}; saved OAuth grant", works=True)
        if binding.key_configured:
            return Status(f"{result.text}; configured API key", works=True)
        if binding.url == "https://huggingface.co/mcp":
            return Status(f"{result.text}; configure HF_TOKEN for account access")
        if binding.url == "https://mcp.context7.com/mcp":
            return Status(f"{result.text}; browser OAuth uses https://mcp.context7.com/mcp/oauth")
    if (
        (result.works or result.needs_login)
        and binding.url in GOOGLE_READS
        and not binding.client_configured
        and not cached(binding, "client.json").get("client_id")
    ):
        return Status(f"{result.text}; {binding.client_hint or GOOGLE_CLIENT_HINT}")
    if not result.needs_login and not result.works:
        if saved_grant(binding):
            return Status(f"OAuth grant saved; verification pending ({result.text})")
        return result
    advertised = advertises_oauth(binding.url, timeout)
    if advertised is None:
        return Status(f"{result.text}; OAuth metadata unavailable")
    if not advertised:
        return Status(f"{result.text}; OAuth not advertised")
    evidence = (
        "OAuth advertised; public discovery may not trigger sign-in"
        if result.works and binding.url not in GOOGLE_READS
        else "OAuth available"
    )
    return Status(f"{result.text}; {evidence}", needs_login=True, works=result.works)


def consent(binding: Binding) -> str:
    if binding.url in GOOGLE_READS:
        detail = (
            "your label names"
            if GOOGLE_READS[binding.url][0] == "list-labels"
            else "one recent file’s metadata without content snippets"
        )
        print(f"The authorization check reads {detail} and discards the response.", file=sys.stderr)
    while True:
        print(
            f"Authorize {binding.name} at {urlsplit(binding.url).hostname}? [l]ogin / [s]kip / [q]uit (default skip): ",
            file=sys.stderr,
            end="",
            flush=True,
        )
        response = sys.stdin.readline()
        if not response:
            return "quit"
        choice = response.strip().lower()
        if choice in ("l", "login", "y", "yes"):
            return "login"
        if choice in ("s", "skip", "", "n", "no"):
            return "skip"
        if choice in ("q", "quit"):
            return "quit"


def login(binding: Binding) -> bool:
    """mcp2cli writes the browser authorization fallback link to stderr; only expose it on the consenting terminal."""
    try:
        p = subprocess.run(
            [
                *binding.command,
                "--oauth",
                "--oauth-flow",
                "authorization_code",
                "--refresh",
                *PROTECTED_READS.get(binding.url, ("--list",)),
            ],
            env=_env(True),
            stdout=subprocess.DEVNULL,
            timeout=360,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError):
        return False
    return p.returncode == 0


def workflow(
    argv: list[str], discover: Callable[[], list[Binding]], *, prog: str = "mcxadmin-oauth", quit_code: int = 0
) -> int:
    parser = argparse.ArgumentParser(prog=prog, description=__doc__)
    parser.add_argument("names", nargs="*", metavar="NAME", help="Choose installed MCP providers (default: all)")
    parser.add_argument(
        "--list", action="store_true", help="Check installed wrappers without prompting or opening a browser"
    )
    parser.add_argument(
        "--timeout", type=float, default=15, metavar="SECONDS", help="Timeout for discovery checks (default: 15)"
    )
    opts = parser.parse_args(argv)
    if not math.isfinite(opts.timeout) or opts.timeout <= 0:
        parser.error("timeout must be positive and finite")
    try:
        bindings = {b.name: b for b in discover() if re.fullmatch(r"mcx-[a-z0-9-]+", b.name)}
    except (OSError, KeyError, ValueError, TypeError):
        print("Cannot read the installed MCP registry.", file=sys.stderr)
        return 1
    names = []
    for selector in opts.names:
        wanted = "mcx-" + selector.removeprefix("mcx-").removeprefix("tw-")
        matches = [
            name for name, binding in bindings.items() if wanted in (binding.legacy, re.sub(r"-mcp[ko]?$", "", name))
        ]
        if wanted in bindings:
            matches = [wanted]
        if len(matches) != 1:
            print(
                f"Unknown or ambiguous installed MCP wrapper. Run {prog} --list to see installed names.",
                file=sys.stderr,
            )
            return 2
        if matches[0] not in names:
            names.append(matches[0])
    names = names or sorted(bindings)
    if not names:
        print("No installed MCP wrappers.")
        return 0
    interactive = (
        not opts.list
        and sys.stdin.isatty()
        and sys.stderr.isatty()
        and not any(os.getenv(k) == "1" for k in ("CLITW_MCP_NONINTERACTIVE", "MCX_MCP_NONINTERACTIVE"))
    )
    if not interactive:
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            statuses = pool.map(lambda n: check(bindings[n], opts.timeout), names)
            for n, status in zip(names, statuses, strict=True):
                print(f"{n}: {status.text}", flush=True)
        return 0
    failures = 0
    try:
        for n in names:
            binding = bindings[n]
            status = check(binding, opts.timeout)
            print(f"{n}: {status.text}", flush=True)
            if not status.needs_login:
                continue
            choice = consent(binding)
            if choice == "quit":
                return quit_code or int(bool(failures))
            if choice == "skip":
                print(f"{n}: skipped", flush=True)
                continue
            completed = login(binding)
            grant = saved_grant(binding)
            if not completed and not grant:
                print(f"{n}: authorization failed or timed out", flush=True)
                failures += 1
                continue
            verified = verify_access(binding, opts.timeout, consented=True)
            if verified.works and (binding.url in PROTECTED_READS or grant or not status.works):
                print(f"{n}: verified {verified.text}", flush=True)
            elif verified.works:
                print(f"{n}: {verified.text}; no saved OAuth grant confirmed", flush=True)
                failures += 1
            elif grant:
                print(f"{n}: OAuth grant saved; verification pending ({verified.text})", flush=True)
                failures += 1
            else:
                print(f"{n}: authorization did not verify ({verified.text})", flush=True)
                failures += 1
    except KeyboardInterrupt:
        print("\nStopped.", file=sys.stderr)
        return 130
    return int(bool(failures))


def packaged_bindings() -> list[Binding]:
    """Adapt clitw's registry using the same configuration and ownership checks as its installed wrapper."""
    from clitw.credentials import available_keys, oauth_client_variables
    from clitw.mcp.config import SAFE_PATH, connection_pin, endpoint, held, load, presets, read_config
    from clitw.mcp.install import owned
    from clitw.mcp.runtime import expand
    from clitw.paths import home, provider_argv, provider_path, registry_dir

    registry = registry_dir() / "baked.json"
    if not registry.is_file():
        return []
    catalog = presets()
    public = {p["url"]: p for p in catalog.values()}
    bindings = []
    for name, row in load(registry).items():
        if not re.fullmatch(r"mcx-[a-z0-9-]+", name) or not isinstance(row, dict) or "clitw_mcp" not in row:
            continue
        meta = row["clitw_mcp"]
        if not isinstance(meta, dict):
            bindings.append(Binding(name, (), skip="invalid configuration"))
            continue
        skip = "disabled or quarantined" if meta.get("hold") else ""
        url = ""
        server = {}
        if not skip:
            try:
                conf, rows = read_config(meta["config"], name)
                server = rows.get(name)
                if server is None or (reason := held(server)):
                    skip = "removed" if server is None else reason
                else:
                    mode, url, transport = endpoint(server, conf)
                    if connection_pin(mode, url, transport) != meta["pin"]:
                        skip = "endpoint changed"
                    elif mode != "direct":
                        skip = "proxy group"
                    elif not public_https(url):
                        skip = "local or private endpoint"
                    elif (
                        url not in public
                        and url not in PUBLIC_OAUTH_URLS
                        and not SAFE_PATH.fullmatch(urlsplit(url).path)
                    ):
                        skip = "private endpoint"
                    elif server.get("headers"):
                        skip = "configured authentication headers"
                    elif server["auth_mode"] != "oauth":
                        skip = "API-key binding" if server["auth_mode"] == "key" else "anonymous binding"
                    elif not provider_path(name).is_file() or not owned(name, row, provider_path(name)):
                        skip = "missing or modified wrapper"
            except (OSError, KeyError, TypeError, ValueError):
                skip = "invalid configuration"
        client_configured = False
        client_hint = ""
        if not skip:
            preset = public.get(url, {})
            oauth = server.get("oauth") or {}
            if meta["config"] == "catalog" and catalog.get(name) == preset:
                if url in GOOGLE_READS and oauth_client_variables(name, preset):
                    stored = available_keys()
                    client_hint = f"run tw auth keys {name[4:]} to configure a registered Google OAuth desktop client"
                    client_configured = bool(
                        stored.get("GOOGLE_MCP_CLIENT_ID") and stored.get("GOOGLE_MCP_CLIENT_SECRET")
                    )
            else:
                try:
                    client_configured = bool(oauth.get("client_id") and expand(oauth["client_id"], conf))
                except ValueError:
                    client_configured = False
        bindings.append(
            Binding(
                name,
                tuple(provider_argv(name)) if not skip else (),
                url if not skip else "",
                skip or "",
                str(home() / "cache" / "mcp2cli"),
                False,
                client_configured,
                client_hint,
                (server or {}).get("legacy", ""),
            )
        )
    return bindings


def cli_login(argv: list[str], *, quit_code: int = 0) -> int:
    return workflow(argv, packaged_bindings, quit_code=quit_code)


def main(argv: list[str] | None = None) -> int:
    return cli_login(sys.argv[1:] if argv is None else argv)
