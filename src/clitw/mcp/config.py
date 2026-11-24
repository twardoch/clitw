# this_file: src/clitw/mcp/config.py
"""Read the bundled public MCP catalogue or an explicitly supplied MCP configuration."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import re
from importlib.resources import files
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

from clitw.names import provider_name, resolve_provider

SAFE_PATH = re.compile(r"/(?:mcp|sse|mcp-oauth|v[0-9]+)(?:/(?:mcp|sse|v[0-9]+))?/?|/?")
PROTOCOLS = {
    "http": "streamable",
    "streamable-http": "streamable",
    "streamable": "streamable",
    "sse": "sse",
    "auto": "auto",
}


def presets() -> dict[str, dict]:
    return json.loads(files(__package__).joinpath("catalog.json").read_text(encoding="utf-8"))


def catalog() -> dict[str, str]:
    """Return canonical wrapper names and their public descriptions."""
    return {name: row["description"] for name, row in presets().items()}


def slug(name: str) -> str:
    group = re.sub(r"([a-z0-9])([A-Z])", r"\1-\2", name).replace("_", "-").lower()
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", group):
        raise ValueError("invalid MCP server name")
    return group


AUTH_CODES = {"anonymous": "", "key": "k", "oauth": "o"}
PUBLIC_OAUTH_URLS = frozenset({"https://mcp.context7.com/mcp/oauth", "https://huggingface.co/mcp?login"})


def legacy_aliases() -> dict[str, str]:
    """Historical packaged names accepted by the administration commands."""
    return {row["legacy"]: name for name, row in presets().items()}


def server_slug(row: dict) -> str:
    """Return the original group prefix used by MCPProxy tools."""
    return slug(row["name"])


def resolve_name(value: str, rows: dict) -> str:
    """Resolve an exact typed name, historical registration, or unique short name."""
    aliases = {}
    for name, row in rows.items():
        meta = row.get("clitw_mcp", row)
        if old := meta.get("previous_name") or meta.get("legacy"):
            aliases.setdefault(old, set()).add(name)
    unique = {k: next(iter(v)) for k, v in aliases.items() if len(v) == 1}
    return resolve_provider(value, rows, {**legacy_aliases(), **unique})


def binding_auth(row: dict, url: str) -> str:
    """Classify the installed service binding independently of the proxy transport key."""
    mode = row.get("auth_mode")
    key_names = [*(row.get("headers") or {}), *(row.get("env") or {})]
    key_names.extend(key for key, _ in parse_qsl(urlsplit(url).query, keep_blank_values=True))
    key_bound = (
        row.get("auth_env")
        or "${" in url
        or any(
            re.search(r"(?i)(api[-_]?key|token|secret|credential|^(authorization|cookie|key)$)", key)
            for key in key_names
        )
    )
    if mode:
        if mode not in AUTH_CODES:
            raise ValueError("auth_mode must be anonymous, key or oauth")
        if key_bound and mode != "key":
            raise ValueError("the declared auth_mode conflicts with a configured key")
        return mode
    if key_bound:
        return "key"
    oauth = row.get("oauth") or {}
    if isinstance(oauth, dict) and any(
        oauth.get(k)
        for k in (
            "enabled",
            "client_id",
            "client_secret",
            "scopes",
            "redirect_uri",
            "authorization_url",
            "token_url",
        )
    ):
        return "oauth"
    if url in PUBLIC_OAUTH_URLS:
        return "oauth"
    return next((p["auth_mode"] for p in presets().values() if p["url"] == url), "anonymous")


def digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def load(path: Path) -> dict:
    data = json.loads(path.read_bytes())
    if not isinstance(data, dict):
        raise TypeError("expected a JSON object")
    return data


def read_config(source: str, direct_name: str = "", *, auth_mode: str | None = None) -> tuple[dict, dict[str, dict]]:
    """The source is 'catalog', an absolute configuration path, or a public endpoint URL."""
    if source == "catalog":
        conf = {}
        rows = [dict(row, name=row["legacy"].removeprefix("mcx-"), enabled=True) for row in presets().values()]
    elif source.startswith(("https://", "http://")):
        conf = {}
        u = parts(source)
        if source not in PUBLIC_OAUTH_URLS and (u.query or not SAFE_PATH.fullmatch(u.path)):
            raise ValueError("use --config for endpoint URLs with custom paths or query parameters")
        service = direct_name.removeprefix("mcx-")
        if qualified := re.search(r"-mcp([ko]?)$", service):
            by_code = {code: mode for mode, code in AUTH_CODES.items()}
            selected = by_code[qualified[1]]
            if auth_mode and auth_mode != selected:
                raise ValueError("the endpoint name and --auth option specify different credential methods")
            auth_mode = selected
            service = service[: qualified.start()]
        rows = [{"name": service, "url": source, "enabled": True, "auth_mode": auth_mode}]
    else:
        conf = load(Path(source))
        entries = conf.get("mcpServers")
        if isinstance(entries, dict):
            rows = [dict(row, name=n) for n, row in entries.items()]
        elif isinstance(entries, list):
            rows = entries
        else:
            raise ValueError("expected mcpServers as an object or list")
    result = {}
    for row in rows:
        row = dict(row)
        group = server_slug(row)
        url = row.get("url") or ""
        if not url and row.get("command"):
            url = remote_stdio_url(row) or ""
        auth = binding_auth(row, url)
        name = provider_name(row["name"], "mcp", AUTH_CODES[auth])
        row.update(auth_mode=auth, legacy=row.get("legacy", "mcx-" + group.removeprefix("mcx-")))
        if name in result:
            raise ValueError("server names collide after CLI normalization")
        result[name] = row
    return conf, result


def parts(url: str):
    u = urlsplit(url)
    if not u.hostname or "$" in u.netloc or u.fragment or u.username is not None or u.password is not None:
        raise ValueError("MCP endpoint requires a fixed hostname without URL userinfo or fragment")
    if u.scheme != "https":
        try:
            local = ipaddress.ip_address(u.hostname).is_loopback
        except ValueError:
            local = u.hostname == "localhost"
        if u.scheme != "http" or not local:
            raise ValueError("MCP requires HTTPS or loopback HTTP")
    return u


def held(row: dict) -> str | None:
    if row.get("quarantined"):
        return "quarantined"
    if row.get("enabled", True) is not True:
        return "disabled"
    return None


def remote_stdio_url(row: dict) -> str | None:
    """Unwrap only stock mcp-remote arguments with no additional credentials or approval mode."""
    if row.get("trust_mode") not in (None, "", "auto") or row.get("env") or row.get("working_dir"):
        return None
    argv = row.get("args") or []
    cmd = Path(row.get("command", "")).name
    if cmd in ("npx", "bunx") and argv[:1] == ["-y"]:
        argv = argv[1:]
    if cmd in ("npx", "bunx") and len(argv) == 2 and argv[0] == "mcp-remote" and argv[1].startswith("https://"):
        parts(argv[1])
        return argv[1]
    return None


def endpoint(row: dict, conf: dict) -> tuple[str, str, str]:
    protocol = row.get("protocol", row.get("type", "stdio" if "command" in row else "http"))
    managed_trust = row.get("trust_mode") not in (None, "", "auto")
    if protocol == "stdio" and (remote := remote_stdio_url(row)):
        return "direct", remote, "auto"
    if managed_trust or protocol == "stdio":
        if "listen" not in conf:
            raise ValueError("stdio and approval-managed MCP groups require an MCPProxy configuration with listen")
        url = f"{'https' if (conf.get('tls') or {}).get('enabled') else 'http'}://{conf['listen']}/mcp/all"
        mode, transport = "proxy", "streamable"
    else:
        if protocol not in PROTOCOLS or not isinstance(row.get("url"), str):
            raise ValueError("unsupported MCP transport or missing endpoint")
        url, mode, transport = row["url"], "direct", PROTOCOLS[protocol]
    parts(url)
    return mode, url, transport


def connection_pin(mode: str, url: str, transport: str) -> str:
    return digest(json.dumps([mode, url, transport]))
