# this_file: tests/test_mcp_token_auth.py
"""A strict token endpoint accepts exactly the authentication method on its cached registration."""

import asyncio
import base64
import hashlib
import json
import time
from functools import partial
from urllib.parse import parse_qs, urlsplit

import httpx2
import mcp2cli
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.auth import AuthorizationCodeResult

from clitwy.mcp.oauth import cached_bearer, preserve_requested_scope


@pytest.mark.parametrize("method", ["client_secret_basic", "client_secret_post", "none"])
def test_pkce_discovery_and_refresh_use_one_registered_method(tmp_path, monkeypatch, method):
    url = "https://resource.fixture.test/mcp"
    issuer = "https://issuer.fixture.test"
    prm = "https://resource.fixture.test/.well-known/oauth-protected-resource/mcp"
    monkeypatch.setattr(mcp2cli, "OAUTH_DIR", tmp_path / "oauth")
    storage = mcp2cli.FileTokenStorage(url)
    registration = {"client_id": "fixture-id", "issuer": issuer, "token_endpoint_auth_method": method}
    if method != "none":
        registration["client_secret"] = "fixture-secret"
    original = json.dumps(registration)
    storage._client_path.write_text(original)
    browser = {}
    grants = []

    def respond(req):
        target = str(req.url)
        if target == prm:
            return httpx2.Response(200, json={"resource": url, "authorization_servers": [issuer]})
        if target == issuer + "/.well-known/oauth-authorization-server":
            return httpx2.Response(
                200,
                json={
                    "issuer": issuer,
                    "authorization_endpoint": issuer + "/authorize",
                    "token_endpoint": issuer + "/token",
                    "response_types_supported": ["code"],
                    "token_endpoint_auth_methods_supported": [method],
                    "code_challenge_methods_supported": ["S256"],
                },
            )
        if target == issuer + "/token":
            data = parse_qs(req.content.decode())
            auth = req.headers.get("authorization")
            # workers-oauth-provider rejects either form credential if Basic is present.
            if auth and ("client_id" in data or "client_secret" in data):
                return httpx2.Response(
                    400,
                    json={"error": "invalid_request", "error_description": "multiple client authentication methods"},
                )
            if method == "client_secret_basic":
                assert auth == "Basic " + base64.b64encode(b"fixture-id:fixture-secret").decode()
            else:
                assert auth is None and data["client_id"] == ["fixture-id"]
                assert data.get("client_secret") == (["fixture-secret"] if method == "client_secret_post" else None)
            kind = data["grant_type"][0]
            if kind == "authorization_code":
                challenge = (
                    base64.urlsafe_b64encode(hashlib.sha256(data["code_verifier"][0].encode()).digest())
                    .rstrip(b"=")
                    .decode()
                )
                assert data["code"] == ["fixture-code"] and browser["code_challenge"] == [challenge]
                assert browser["code_challenge_method"] == ["S256"] and data["redirect_uri"] == browser["redirect_uri"]
            else:
                assert kind == "refresh_token" and data["refresh_token"] == ["fixture-refresh"]
            grants.append(kind)
            return httpx2.Response(
                200,
                json={
                    "access_token": f"fixture-token-{len(grants)}",
                    "refresh_token": "fixture-refresh",
                    "expires_in": 3600,
                    "token_type": "Bearer",
                },
            )
        assert target == url, "a cached registration must not register again"
        if req.headers.get("Authorization") != f"Bearer fixture-token-{len(grants)}":
            return httpx2.Response(401, headers={"WWW-Authenticate": f'Bearer resource_metadata="{prm}"'})
        message = json.loads(req.content)
        if message["method"] == "notifications/initialized":
            return httpx2.Response(202)
        if message["method"] == "initialize":
            result = {
                "protocolVersion": message["params"]["protocolVersion"],
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "fixture", "version": "1"},
            }
        else:
            assert message["method"] == "tools/list"
            result = {"tools": [{"name": "ping", "inputSchema": {"type": "object"}}]}
        return httpx2.Response(200, json={"jsonrpc": "2.0", "id": message["id"], "result": result})

    http = partial(httpx2.AsyncClient, transport=httpx2.MockTransport(respond))
    monkeypatch.setattr(httpx2, "AsyncClient", http)

    async def discover(**options):
        async with (
            http(**options) as client,
            streamable_http_client(url, http_client=client) as streams,
            ClientSession(*streams) as session,
        ):
            await session.initialize()
            assert [tool.name for tool in await mcp2cli._list_all_tools(session)] == ["ping"]

    async def redirect(uri):
        browser.update(parse_qs(urlsplit(uri).query))

    async def callback():
        return AuthorizationCodeResult(code="fixture-code", state=browser["state"][0])

    with preserve_requested_scope(url):
        provider = mcp2cli.build_oauth_provider(url, flow="authorization_code", manual_callback=True)
        provider.context.redirect_handler, provider.context.callback_handler = redirect, callback
        asyncio.run(discover(auth=provider))
    assert storage._client_path.read_text() == original
    access = cached_bearer(url, tmp_path)
    asyncio.run(discover(headers={"Authorization": "Bearer " + access}))
    storage._tokens_meta_path.write_text(json.dumps({"expires_at": time.time() - 30}))
    renewed = cached_bearer(url, tmp_path)
    assert renewed == "fixture-token-2"
    asyncio.run(discover(headers={"Authorization": "Bearer " + renewed}))
    assert grants == ["authorization_code", "refresh_token"] and storage._client_path.read_text() == original
