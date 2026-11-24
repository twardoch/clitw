# this_file: src/clitwy/mcp/oauth.py
"""Refresh a previously authorized native mcp2cli login without interactive OAuth or registration."""

import asyncio
import contextlib
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

from filelock import FileLock

from .config import digest, load


def use_single_token_auth(provider) -> None:
    """Let the SDK select client auth, removing form credentials when it emits HTTP Basic."""
    context = provider.context
    native = context.prepare_token_auth

    def prepare(data, headers=None):
        form, outgoing = native(data, headers)
        if outgoing.get("Authorization", "").lower().startswith("basic "):
            form = {key: value for key, value in form.items() if key not in ("client_id", "client_secret")}
        return form, outgoing

    context.prepare_token_auth = prepare


@contextlib.contextmanager
def preserve_requested_scope(url: str):
    """Keep the selected permission scope when the SDK processes a broad provider challenge."""
    import mcp2cli

    original = mcp2cli.build_oauth_provider

    def build(server_url: str, **opts):
        auth = original(server_url, **opts)
        if server_url == url:
            use_single_token_auth(auth)
        scope = opts.get("scope")
        if server_url == url and scope and hasattr(auth, "_perform_authorization"):
            authorize = auth._perform_authorization

            async def scoped_authorize():
                # SDK 2.3 can replace the caller's scope with every scope the resource advertises.
                auth.context.client_metadata.scope = scope
                return await authorize()

            auth._perform_authorization = scoped_authorize
        return auth

    mcp2cli.build_oauth_provider = build
    try:
        yield
    finally:
        mcp2cli.build_oauth_provider = original


async def refresh(url: str) -> int | None:
    import httpx2
    from mcp.client.auth import utils
    from mcp.client.auth.oauth2 import OAuthClientProvider
    from mcp.types import LATEST_PROTOCOL_VERSION
    from mcp2cli import FileTokenStorage

    storage = FileTokenStorage(url)
    client = await storage.get_client_info()
    if not client or not getattr(client, "issuer", None):
        return None
    issuer = str(client.issuer)
    u = urlsplit(issuer)
    if u.scheme != "https" or not u.hostname or u.username or u.password or u.query or u.fragment:
        return None
    # Only the issuer on this endpoint's existing client registration can receive the refresh token.
    provider = OAuthClientProvider(server_url=url, client_metadata=client, storage=storage)
    use_single_token_auth(provider)
    await provider._initialize()
    if not provider.context.can_refresh_token():
        return None
    async with httpx2.AsyncClient(follow_redirects=False, timeout=8) as http:
        for path in utils.build_oauth_authorization_server_metadata_discovery_urls(issuer, url):
            response = await http.send(utils.create_oauth_metadata_request(path))
            if response.status_code != 200:
                continue
            _, metadata = await utils.handle_auth_metadata_response(response)
            if not metadata:
                continue
            utils.validate_metadata_issuer(metadata, issuer)
            target = urlsplit(str(metadata.token_endpoint))
            if (target.scheme, target.netloc) != (u.scheme, u.netloc) or target.query or target.fragment:
                return None
            provider.context.auth_server_url = issuer
            provider.context.oauth_metadata = metadata
            provider.context.protocol_version = LATEST_PROTOCOL_VERSION
            request = await provider._refresh_token()
            response = await http.send(request)
            if response.status_code == 200:
                return 200 if await provider._handle_refresh_response(response) else None
            return response.status_code
    return None


def cached_bearer(url: str, cache_dir: Path) -> str | None:
    cache = cache_dir / "oauth" / digest(url)[:16]

    def saved():
        token = load(cache / "tokens.json")
        if str(token.get("token_type", "bearer")).lower() != "bearer":
            return {}, None
        try:
            expiry = load(cache / "tokens_meta.json").get("expires_at")
        except FileNotFoundError:
            # mcp2cli deliberately removes the sidecar when the issuer returns a token without an expiry.
            return token, token.get("access_token") if token.get("expires_in") is None else None
        return token, token.get("access_token") if expiry and float(expiry) > time.time() + 10 else None

    try:
        token, access = saved()
        if access or not token.get("refresh_token") or not (cache / "client.json").is_file():
            return access
        with FileLock(str(cache / ".refresh.lock"), timeout=20):
            _, access = saved()
            if access:
                return access

            try:
                status = asyncio.run(refresh(url))
            except Exception:  # noqa: BLE001 -- OAuth exceptions may contain private request details.
                status = None
            if status == 200:
                return saved()[1]
            print(
                "Cached OAuth refresh unavailable" + (f" (HTTP {status})" if status else "") + ".",
                file=sys.stderr,
            )
    except (FileNotFoundError, ValueError, TypeError):
        pass
    return None
