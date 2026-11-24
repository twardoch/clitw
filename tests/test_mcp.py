# this_file: tests/test_mcp.py
"""Exercise installation and real mcp2cli discovery against a local streamable HTTP server."""

import json
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from clitwy import mcp
from clitwy.paths import provider_argv, provider_path, registry_dir


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("CLITW_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("CLITW_DOTENV", raising=False)
    return tmp_path


@pytest.fixture
def upstream():
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            method = request["method"]
            if self.path == "/redirect":
                self.send_response(307)
                self.send_header("Location", f"http://localhost:{self.server.server_port}/sink")
                self.end_headers()
                return
            calls.append((self.path, method, self.headers.get("Authorization"), self.headers.get("X-API-Key")))
            if method == "initialize":
                data = {
                    "protocolVersion": request["params"]["protocolVersion"],
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "fixture", "version": "1"},
                }
            elif method == "notifications/initialized":
                self.send_response(202)
                self.end_headers()
                return
            elif method == "tools/list":
                names = ["local__ping", "other__ping"] if self.path == "/mcp/all" else ["ping"]
                data = {
                    "tools": [{"name": name, "inputSchema": {"type": "object", "properties": {}}} for name in names]
                }
            elif method == "tools/call":
                data = {"content": [{"type": "text", "text": "pong"}]}
            else:
                self.send_error(404)
                return
            body = json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": data}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", calls
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def invoke(name, *args):
    return subprocess.run([*provider_argv(name), *args], capture_output=True, text=True, timeout=15, check=False)


def test_public_catalog_and_selected_install(isolated):
    assert {"mcx-pika-mcpo", "mcx-runway-mcpo", "mcx-huggingface-mcpk", "mcx-brevo-email-mcpk"} <= mcp.catalog().keys()
    assert mcp.cli_install(["mcx-pika"]) == 0
    reg = json.loads((registry_dir() / "baked.json").read_text())
    assert set(reg) == {"mcx-pika-mcpo"} and reg["mcx-pika-mcpo"]["source"] == "https://mcp.pika.art/mcp"
    assert mcp.cli_install(["pika"]) == 0


def test_private_and_proxy_wrappers_use_scoped_auth_and_real_tools(isolated, upstream, monkeypatch):
    base, calls = upstream
    cfg = isolated / "mcp.json"
    cfg.write_text(
        json.dumps(
            {
                "listen": base.split("://")[1],
                "api_key": "proxy-fixture",
                "mcpServers": [
                    {
                        "name": "fixture",
                        "url": base + "/private/demo?token=query-fixture",
                        "headers": {"Authorization": "Bearer ${TOKEN}"},
                        "enabled": True,
                    },
                    {"name": "local", "protocol": "stdio", "command": "never-run", "enabled": True},
                ],
            }
        )
    )
    dot = isolated / ".env"
    dot.write_text("OTHER=header-fixture\nTOKEN=${OTHER}\n")
    assert mcp.cli_install(["--config", str(cfg)]) == 0 and not calls
    denied = invoke("mcx-fixture-mcpk", "--refresh", "--list", "--compact", "--json")
    assert denied.returncode and not calls
    monkeypatch.setenv("CLITW_DOTENV", str(dot))
    direct = invoke("mcx-fixture-mcpk", "--refresh", "--list", "--compact", "--json")
    assert direct.returncode == 0, direct.stderr
    assert json.loads(direct.stdout) == ["ping"]
    assert any(
        path == "/private/demo?token=query-fixture" and auth == "Bearer header-fixture" for path, _, auth, _ in calls
    )
    local = invoke("mcx-local-mcp", "--refresh", "--list", "--compact", "--json")
    assert local.returncode == 0, local.stderr
    assert json.loads(local.stdout) == ["local--ping"]
    assert any(path == "/mcp/all" and key == "proxy-fixture" for path, _, _, key in calls)
    assert all(key is None for path, _, _, key in calls if path != "/mcp/all")
    assert invoke("mcx-fixture-mcpk", "--mcp", "http://127.0.0.1:1", "--list").returncode
    written = (registry_dir() / "baked.json").read_text() + provider_path("mcx-fixture-mcpk").read_text()
    assert all(secret not in written for secret in ["header-fixture", "query-fixture", "proxy-fixture"])


def test_endpoint_probe_reports_fresh_discovery_without_guessing_operation(isolated, upstream):
    base, calls = upstream
    assert mcp.cli_install(["--endpoint", "sample", base + "/mcp"]) == 0
    report = isolated / "probe.jsonl"
    assert mcp.cli_try(["sample", "--output", str(report), "--timeout", "15", "--jobs", "1"]) == 0
    row = json.loads(report.read_text())
    assert (row["name"], row["tool_count"], row["reason"]) == ("mcx-sample-mcp", 1, "discovery_only")
    assert "tools/list" in [m for _, m, _, _ in calls]
    assert "tools/call" not in [m for _, m, _, _ in calls]
    assert mcp.cli_install(["--endpoint", "questionable", base + "/private?credential=fixture"]) == 1


def test_explicit_hold_prevents_automatic_preset_and_owned_cleanup(isolated):
    config = isolated / "mcp.json"
    config.write_text(json.dumps({"mcpServers": {"pika": {"url": "https://mcp.pika.art/mcp", "quarantined": True}}}))
    assert mcp.cli_install(["pika"]) == 0
    assert mcp.cli_install(["--config", str(config)]) == 0
    assert not provider_path("mcx-pika-mcpo").exists()
    assert mcp.cli_install(["pika"]) == 1
    report = isolated / "probe.jsonl"
    assert mcp.cli_try(["pika", "--output", str(report)]) == 0
    assert json.loads(report.read_text())["reason"] == "quarantined"


def test_endpoint_drift_fails_closed_and_modified_executable_survives(isolated, upstream):
    base, calls = upstream
    config = isolated / "mcp.json"

    def write(url):
        config.write_text(json.dumps({"mcpServers": {"test": {"url": url}}}))

    write(base + "/mcp")
    assert mcp.cli_install(["--config", str(config)]) == 0
    write(base + "/changed")
    result = invoke("mcx-test-mcp", "--list", "--refresh")
    assert result.returncode != 0 and "endpoint changed" in result.stderr and not calls
    path = provider_path("mcx-test-mcp")
    body = path.read_bytes() + b"\n# user edit\n"
    path.write_bytes(body)
    assert mcp.cli_install(["--config", str(config)]) != 0 and path.read_bytes() == body


def test_cross_origin_redirect_does_not_forward_configured_header(isolated, upstream):
    base, calls = upstream
    cfg = isolated / "mcp.json"
    cfg.write_text(
        json.dumps(
            {"mcpServers": {"bound": {"url": base + "/redirect", "headers": {"X-API-Key": "redirection-fixture"}}}}
        )
    )
    assert mcp.cli_install(["--config", str(cfg)]) == 0
    result = invoke("mcx-bound-mcpk", "--refresh", "--list", "--compact")
    assert result.returncode and "redirection-fixture" not in result.stderr
    assert not any(path == "/sink" for path, _, _, _ in calls)


def test_probe_runs_only_verified_read_recipe_at_matching_endpoint(isolated, upstream, monkeypatch):
    from clitwy.mcp import probe

    base, calls = upstream
    name = "mcx-approved-mcp"
    assert mcp.cli_install(["--endpoint", "approved", base + "/mcp"]) == 0
    monkeypatch.setattr(probe, "presets", lambda: {name: {"url": base + "/mcp", "read": ["ping"]}})
    report = isolated / "probe.jsonl"
    assert mcp.cli_try([name, "--output", str(report)]) == 0
    assert json.loads(report.read_text())["reason"] == "read_only_call"
    assert [m for _, m, _, _ in calls].count("tools/call") == 1


@pytest.mark.parametrize("same_issuer", [True, False])
def test_saved_refresh_uses_registered_issuer_and_never_registers(isolated, monkeypatch, same_issuer):
    import hashlib
    import time
    from functools import partial
    from urllib.parse import parse_qs

    import httpx2
    import mcp2cli

    from clitwy.mcp.oauth import cached_bearer

    url = "https://resource.example/mcp"
    issuer = "https://issuer.example"
    cache = isolated / "cache"
    monkeypatch.setattr(mcp2cli, "OAUTH_DIR", cache / "oauth")
    saved = cache / "oauth" / hashlib.sha256(url.encode()).hexdigest()[:16]
    saved.mkdir(parents=True)
    (saved / "client.json").write_text(
        json.dumps({"client_id": "test-client", "issuer": issuer, "token_endpoint_auth_method": "none"})
    )
    (saved / "tokens.json").write_text(
        json.dumps(
            {"access_token": "expired", "token_type": "Bearer", "refresh_token": "fixture-refresh", "expires_in": 30}
        )
    )
    (saved / "tokens_meta.json").write_text(json.dumps({"expires_at": time.time() - 60}))
    methods = []

    def send(request):
        methods.append(request.method)
        assert request.url.host == "issuer.example"
        if request.method == "GET":
            return httpx2.Response(
                200,
                json={
                    "issuer": issuer,
                    "authorization_endpoint": issuer + "/authorize",
                    "token_endpoint": (issuer if same_issuer else "https://other.example") + "/token",
                },
            )
        assert request.url.path == "/token" and parse_qs(request.content.decode())["grant_type"] == ["refresh_token"]
        return httpx2.Response(200, json={"access_token": "renewed", "token_type": "Bearer", "expires_in": 3600})

    monkeypatch.setattr(httpx2, "AsyncClient", partial(httpx2.AsyncClient, transport=httpx2.MockTransport(send)))
    assert cached_bearer(url, cache) == ("renewed" if same_issuer else None)
    assert methods == (["GET", "POST"] if same_issuer else ["GET"])
    assert cached_bearer("https://other.example/mcp", cache) is None


def test_same_name_private_override_does_not_inherit_public_host_credential(isolated, upstream, monkeypatch):
    base, calls = upstream
    monkeypatch.setenv("HF_TOKEN", "fixture-for-official-endpoint-only")
    assert mcp.cli_install(["huggingface"]) == 0
    config = isolated / "integration.json"
    config.write_text(json.dumps({"mcpServers": {"huggingface": {"url": base + "/mcp"}}}))
    assert mcp.cli_install(["--config", str(config)]) == 0
    assert invoke("mcx-huggingface-mcp", "--refresh", "--list", "--json").returncode == 0
    assert calls and all(auth is None for _, _, auth, _ in calls)
    assert mcp.cli_install(["huggingface"]) == 0
    assert provider_path("mcx-huggingface-mcpk").exists()


def test_explicit_endpoint_modes_coexist_and_preserve_discovery(isolated, upstream):
    """Select a configured mode once and retain it when a qualified executable runs."""
    base, calls = upstream
    for mode in ("oauth", "anonymous"):
        assert mcp.cli_install(["--endpoint", "sample", base + "/mcp", "--auth", mode]) == 0
    registry = json.loads((registry_dir() / "baked.json").read_text())
    assert set(registry) == {"mcx-sample-mcp", "mcx-sample-mcpo"}
    assert invoke("mcx-sample-mcpo", "--refresh", "--list", "--compact", "--json").returncode == 0
    assert any(method == "tools/list" for _, method, _, _ in calls)
    assert not provider_path("mcx-sample").exists()


def test_configured_auth_preserves_group_and_bundled_aliases(isolated):
    from clitwy.mcp.config import binding_auth, legacy_aliases, read_config, resolve_name

    assert binding_auth({"oauth": {"pkce_enabled": True}}, "") == "anonymous"
    assert binding_auth({"oauth": {"client_id": "example"}}, "") == "oauth"
    assert binding_auth({"headers": {"X-Api-Key": "fixture"}}, "https://mcp.pika.art/mcp") == "key"
    assert binding_auth({}, "https://huggingface.co/mcp?login") == "oauth"
    with pytest.raises(ValueError, match="conflicts"):
        binding_auth({"auth_mode": "anonymous", "headers": {"X-Api-Key": "fixture"}}, "")
    _, rows = read_config("catalog")
    for old, new in legacy_aliases().items():
        assert resolve_name(old, rows) == new
        assert rows[new]["name"] == old[4:]


def old_registration(name, new, config, *, hold=False):
    """Build the previous release's registry and launcher format in the temporary CLITW_HOME."""
    from clitwy.mcp.config import read_config
    from clitwy.mcp.install import held_registration, registration

    conf, rows = read_config(config, name)
    row = rows[new]
    path = provider_path(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    if hold:
        entry = held_registration(name, row, config, "quarantined")
    else:
        entry, body = registration(name, row, conf, config)
        path.write_bytes(body)
        path.chmod(0o755)
    for k in ("legacy", "auth_mode"):
        entry["clitw_mcp"].pop(k)
    registry_dir().mkdir(parents=True, exist_ok=True)
    (registry_dir() / "baked.json").write_text(json.dumps({name: entry}))


@pytest.mark.parametrize("changed", ["", "old", "destination", "pin"])
def test_offline_migration_preserves_ownership_and_endpoint(isolated, upstream, changed):
    from clitwy.builders import mcp_main

    base, calls = upstream
    cfg = isolated / "mcp.json"
    cfg.write_text(json.dumps({"mcpServers": {"custom": {"url": base + "/mcp"}}}))
    old, new = "mcx-custom", "mcx-custom-mcp"
    old_registration(old, new, str(cfg))
    if changed == "old":
        provider_path(old).write_text("user edit")
    if changed == "destination":
        provider_path(new).write_text("user edit")
    if changed == "pin":
        cfg.write_text(json.dumps({"mcpServers": {"custom": {"url": base + "/sse"}}}))
    folder = isolated / "exports"
    rc = mcp_main(["--migrate-names", old, "--bin-dir", str(folder)])
    reg = json.loads((registry_dir() / "baked.json").read_text())
    assert not calls
    if changed:
        assert rc == 1 and set(reg) == {old}
        assert provider_path(old).is_file()
    else:
        assert rc == 0 and set(reg) == {new} and reg[new]["clitw_mcp"]["previous_name"] == old
        assert not provider_path(old).exists()
        provider_path(new).unlink()
        assert mcp_main(["--migrate-names", old, "--bin-dir", str(folder)]) == 0
        assert any(p.stem == new for p in folder.iterdir())
        assert invoke(new, "--refresh", "--list", "--compact").returncode == 0


def test_historical_quarantine_survives_regular_preset_install(isolated):
    config = isolated / "mcp.json"
    config.write_text(json.dumps({"mcpServers": {"pika": {"url": "https://mcp.pika.art/mcp", "quarantined": True}}}))
    old, new = "mcx-pika", "mcx-pika-mcpo"
    old_registration(old, new, str(config), hold=True)
    assert mcp.cli_install(["pika"]) == 1
    reg = json.loads((registry_dir() / "baked.json").read_text())
    assert set(reg) == {new} and reg[new]["clitw_mcp"]["hold"] == "quarantined"
    assert not provider_path(old).exists() and not provider_path(new).exists()
