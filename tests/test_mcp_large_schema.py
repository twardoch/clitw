# this_file: tests/test_mcp_large_schema.py
"""Exercise the SDK's real SSE framing and guided recovery after a persisted grant."""

import asyncio
import json
import sys

import httpx2
import mcp2cli
import pytest
from mcp import ClientSession
from mcp.client import streamable_http
from mcp.shared import _httpx_utils
from mcp.shared.exceptions import MCPError

from clitw.mcp import login
from clitw.mcp.sse import HIGGSFIELD_EVENT_BYTES, HIGGSFIELD_URL, higgsfield_schema_stream


@pytest.mark.parametrize(
    ("endpoint", "size", "success"),
    [
        (HIGGSFIELD_URL, 1100000, True),
        ("https://other.example/mcp", 1100000, False),
        (HIGGSFIELD_URL + "/other", 1100000, False),
        (HIGGSFIELD_URL, HIGGSFIELD_EVENT_BYTES + 1, False),
    ],
)
def test_sse_list_uses_bounded_size_only_for_exact_endpoint(monkeypatch, endpoint, size, success):
    def respond(request):
        assert str(request.url) == endpoint
        obj = json.loads(request.content)
        if obj["method"] == "notifications/initialized":
            return httpx2.Response(202)
        if obj["method"] == "initialize":
            result = {
                "protocolVersion": obj["params"]["protocolVersion"],
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "fixture", "version": "1"},
            }
        else:
            assert obj["method"] == "tools/list"
            result = {"tools": [{"name": "large", "description": "x" * size, "inputSchema": {"type": "object"}}]}
        message = json.dumps({"jsonrpc": "2.0", "id": obj["id"], "result": result})
        return httpx2.Response(200, headers={"content-type": "text/event-stream"}, content=f"data: {message}\n\n")

    monkeypatch.setattr(
        _httpx_utils, "create_mcp_http_client", lambda **_: httpx2.AsyncClient(transport=httpx2.MockTransport(respond))
    )
    original = streamable_http.streamable_http_client

    async def discover():
        # Force a second endpoint through the installed adapter to check its per-request URL gate.
        with higgsfield_schema_stream(HIGGSFIELD_URL):
            async with mcp2cli._streamable_streams(endpoint) as (r, w), ClientSession(r, w) as session:
                await session.initialize()
                if success:
                    assert [t.name for t in (await session.list_tools()).tools] == ["large"]
                else:
                    cap = HIGGSFIELD_EVENT_BYTES if endpoint == HIGGSFIELD_URL else 1024 * 1024
                    with pytest.raises(MCPError, match=f"exceeded the {cap} byte limit"):
                        await session.list_tools()

    asyncio.run(discover())
    assert streamable_http.streamable_http_client is original


def test_saved_grant_with_failed_discovery_does_not_prompt(monkeypatch):
    item = login.Binding("mcx-fixture-mcpo", ("fixture",), "https://fixture.example/mcp")
    error = login.failure("SSE stream failed: Server-sent event exceeded the 1048576 byte limit.")
    monkeypatch.setattr(login, "verify_access", lambda *a: error)
    monkeypatch.setattr(login, "saved_grant", lambda b: True)
    status = login.check(item, 1)
    assert not status.needs_login and not status.works
    assert "grant saved" in status.text and "SSE event size limit" in status.text


@pytest.mark.parametrize("verified", [True, False])
def test_login_rechecks_persisted_grant_after_subprocess_error(monkeypatch, capsys, verified):
    item = login.Binding("mcx-fixture-mcpo", ("fixture",), "https://fixture.example/mcp")
    for name in ("CLITW_MCP_NONINTERACTIVE", "MCX_MCP_NONINTERACTIVE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stderr, "isatty", lambda: True)
    monkeypatch.setattr(login, "check", lambda *a: login.Status("authentication required", needs_login=True))
    monkeypatch.setattr(login, "consent", lambda b: "login")
    monkeypatch.setattr(login, "login", lambda b: False)
    monkeypatch.setattr(login, "saved_grant", lambda b: True)
    monkeypatch.setattr(login, "verify_access", lambda *a, **k: login.Status("discovery", works=verified))
    assert login.workflow([], lambda: [item]) == (0 if verified else 1)
    assert ("verified discovery" if verified else "grant saved; verification pending") in capsys.readouterr().out
