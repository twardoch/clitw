# this_file: tests/test_builders.py
"""Run the standalone provider builders against local fixtures and temporary PATH directories."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from clitwy.builders import mcp_main, openapi_main
from clitwy.paths import home, provider_path, registry_dir


@pytest.fixture
def layout(tmp_path, monkeypatch):
    monkeypatch.setenv("CLITW_HOME", str(tmp_path / "state"))
    return tmp_path / "published", tmp_path / "servers.json"


@pytest.fixture
def endpoints():
    methods = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_error(401 if self.path == "/oauth" else 404)

        def do_POST(self):
            req = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            method = req["method"]
            methods.append((self.path, method))
            if self.path in ("/html", "/oauth", "/denied", "/basic"):
                self.send_response(200 if self.path == "/html" else 401)
                self.send_header("Content-Type", "text/html" if self.path == "/html" else "application/json")
                if self.path in ("/oauth", "/basic"):
                    self.send_header(
                        "WWW-Authenticate",
                        'Bearer realm="fixture"' if self.path == "/oauth" else 'Basic realm="fixture"',
                    )
                self.end_headers()
                self.wfile.write(b"<html>private text</html>" if self.path == "/html" else b'{"error":"private text"}')
                return
            if method == "initialize":
                result = {
                    "protocolVersion": req["params"]["protocolVersion"],
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "fixture", "version": "1"},
                }
            elif method == "notifications/initialized":
                self.send_response(202)
                self.end_headers()
                return
            elif method == "tools/list":
                tools = [{"name": "ping", "inputSchema": {"type": "object"}}]
                if self.path == "/invalid":
                    tools = [{"name": "bad", "inputSchema": "invalid"}]
                if self.path == "/empty":
                    tools = []
                result = {"tools": tools}
            else:
                self.send_error(404)
                return
            body = json.dumps({"jsonrpc": "2.0", "id": req["id"], "result": result}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=srv.serve_forever, daemon=True)
    worker.start()
    yield f"http://127.0.0.1:{srv.server_port}", methods
    srv.shutdown()
    srv.server_close()
    worker.join()


def published(directory, name):
    return directory / (name + ".exe" if os.name == "nt" else name)


def test_standalone_build_and_invoke_without_tw(layout):
    publish, _ = layout
    flags = ["--bin-dir", str(publish), "archive-wayback"]
    assert openapi_main(flags) == 0
    exe = published(publish, "mcx-archive-wayback-api")
    if os.name != "nt":
        assert exe.is_symlink() and exe.resolve() == provider_path("mcx-archive-wayback-api").resolve()
    assert json.loads(subprocess.check_output([str(exe), "--list", "--json"]))[0]["path"] == "/wayback/available"
    before = exe.lstat()
    assert openapi_main(flags) == 0 and exe.lstat().st_mtime_ns == before.st_mtime_ns


def test_skip_does_not_export_old_binding_to_new_destination(layout, tmp_path):
    directory, _ = layout
    source = tmp_path / "user.json"
    source.write_text(
        json.dumps(
            {
                "openapi": "3.0.0",
                "info": {"title": "Test", "version": "1"},
                "paths": {"/ok": {"get": {"operationId": "status", "responses": {"200": {"description": "OK"}}}}},
            }
        )
    )
    flags = ["--spec", str(source), "--base-url", "https://example.invalid", "--name", "example"]
    assert openapi_main(["--bin-dir", str(directory), *flags]) == 0
    exe = published(directory, "mcx-example-api")
    source.write_text("<html>bad</html>")
    other = tmp_path / "other"
    assert openapi_main(["--bin-dir", str(other), *flags]) == 1
    assert not other.exists()
    assert json.loads(subprocess.check_output([str(exe), "--list", "--json"]))[0]["path"] == "/ok"
    assert openapi_main(["--bin-dir", str(other), "agno"]) == 0
    assert not other.exists()


def test_unowned_destination_is_never_replaced(layout):
    directory, _ = layout
    directory.mkdir()
    exe = published(directory, "mcx-archive-wayback-api")
    exe.write_text("someone else's executable")
    assert openapi_main(["archive-wayback", "--bin-dir", str(directory)]) == 1
    assert exe.read_text() == "someone else's executable"


def test_mcp_valid_empty_html_dead_and_oauth_challenge(layout, endpoints, capsys):
    directory, conf = layout
    url, calls = endpoints
    with socket.socket() as unused:
        unused.bind(("127.0.0.1", 0))
        dead = f"http://127.0.0.1:{unused.getsockname()[1]}/mcp"
    names = ["good", "invalid", "empty", "html", "denied", "basic", "oauth"]
    rows = {n: {"url": url + "/" + n} for n in names}
    rows["oauth"]["auth_mode"] = "oauth"
    rows["dead"] = {"url": dead}
    conf.write_text(json.dumps({"mcpServers": rows}))
    args = ["--bin-dir", str(directory), "--config", str(conf), "--jobs", "6", "--timeout", "8"]
    assert mcp_main(args) == 0
    assert {p.name.removesuffix(".exe") for p in directory.iterdir()} == {"mcx-good-mcp", "mcx-oauth-mcpo"}
    assert all(m in {"initialize", "notifications/initialized", "tools/list"} for _, m in calls)
    assert not provider_path("mcx-empty-mcp").exists()
    out = capsys.readouterr()
    assert "pending sign-in" in out.out and "private text" not in out.out + out.err and url not in out.out + out.err
    exe = published(directory, "mcx-good-mcp")
    assert json.loads(subprocess.check_output([str(exe), "--refresh", "--list", "--compact", "--json"])) == ["ping"]
    # A held provider's tracked export is removed even if a subsequent build selects a different PATH directory.
    conf.write_text(json.dumps({"mcpServers": {"good": {"url": url + "/good", "enabled": False}}}))
    other = directory.parent / "different-bin"
    assert mcp_main(["--config", str(conf), "--bin-dir", str(other)]) == 0
    assert not os.path.lexists(exe) and not provider_path("mcx-good-mcp").exists()
    assert not other.exists()
    assert registry_dir() == home() / "registry"


def test_mcp_custom_endpoint(layout, endpoints):
    directory, _ = layout
    base, _ = endpoints
    assert mcp_main(["--bin-dir", str(directory), "--endpoint", "explicit", base + "/mcp", "--timeout", "8"]) == 0
    assert published(directory, "mcx-explicit-mcp").exists()
