# this_file: tests/test_utilities.py
"""Exercise the auxiliary CLIs through their public argv dispatcher, without provider access."""

import json
import sys
from pathlib import Path

import pytest
import yaml

from clitwy import paths
from clitwy.utilities import cli


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("CLITW_HOME", str(tmp_path / "clitwy"))
    monkeypatch.setenv("CLITW_ENV_FILE", str(tmp_path / "dotenv"))


def test_groups_disk_round_trip_and_wildcard(tmp_path, capsys):
    cfg = tmp_path / "mcp_config.json"
    cfg.write_text(
        json.dumps({"api_key": "fixture-value", "mcpServers": [{"name": n, "enabled": False} for n in ("a", "b", "c")]})
    )
    tags = tmp_path / "mcpp.yaml"
    tags.write_text("a: [art]\nb: ['*']\n")
    cmd = ["groups", "--bridge-dir", str(tmp_path)]
    assert cli(cmd + ["list"]) == cli(cmd + ["tag"]) == 0
    initial = cfg.read_bytes()
    assert cli(cmd + ["tag", "undefined"]) == 1 and cfg.read_bytes() == initial
    assert cli(cmd + ["scan"]) == 0 and yaml.safe_load(tags.read_text())["c"] == ["new"]
    assert cli(cmd + ["on", "@art"]) == 0
    assert [s["enabled"] for s in json.loads(cfg.read_text())["mcpServers"]] == [True, True, False]
    assert cli(cmd + ["off", ""]) == 0
    assert [s["enabled"] for s in json.loads(cfg.read_text())["mcpServers"]] == [True, True, False]
    assert cli(cmd + ["all"]) == cli(cmd + ["off", "b"]) == cli(cmd + ["tag", "new"]) == 0
    assert [s["enabled"] for s in json.loads(cfg.read_text())["mcpServers"]] == [False, True, True]
    assert cli(cmd + ["off"]) == 0 and not any(s["enabled"] for s in json.loads(cfg.read_text())["mcpServers"])
    assert "fixture-value" not in capsys.readouterr().out


def test_doctor_checks_command_help_without_executing_command(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("CLITW_HOME", str(tmp_path))
    paths.registry_dir().mkdir()
    paths.bin_dir().mkdir()
    calls = tmp_path / "calls"
    shim = paths.provider_path("tw-fixture")
    shim.write_text(
        f"#!{sys.executable}\nimport sys\nwith open({str(calls)!r},'a') as f: print(sys.argv[1:],file=f)\nprint('[\"remove-everything\"]')\n"
    )
    shim.chmod(0o755)
    (paths.registry_dir() / "openapi.json").write_text('{"tw-fixture":{},"missing":{}}')
    assert cli(["doctor", "--only", "fixture", "--kind", "spec"]) == 0
    assert calls.read_text().splitlines() == ["['--list', '--compact', '--json']", "['remove-everything', '--help']"]
    assert cli(["doctor", "--only", "missing"]) == 1
    assert "FileNotFoundError" in capsys.readouterr().out


def test_groups_unix_socket_get_and_patch(tmp_path, monkeypatch, capsys):
    import socketserver
    import threading
    from http.server import BaseHTTPRequestHandler

    class Bridge(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            assert self.headers["X-API-Key"] == "test-key" and self.path == "/api/v1/config"
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"data":{"config":{"mcpServers":[{"name":"remote","enabled":false}]}}}')

        def do_PATCH(self):
            self.server.body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"data":{"requires_restart":true}}')

    import tempfile

    # macOS sockaddr_un has a short path limit, so allocate the socket directly under /tmp.
    with tempfile.TemporaryDirectory(dir="/tmp") as short:
        socket_path = Path(short) / "bridge.sock"
        with socketserver.UnixStreamServer(str(socket_path), Bridge) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            cfg = tmp_path / "mcp_config.json"
            original = '{"api_key":"test-key","mcpServers":[]}'
            cfg.write_text(original)
            try:
                assert cli(["groups", "--config", str(cfg), "--socket", str(socket_path), "on", "remote"]) == 0
                assert cfg.read_text() == original
                assert server.body == {"mcpServers": [{"name": "remote", "enabled": True}]}
                assert "Restart" in capsys.readouterr().out
            finally:
                server.shutdown()
                thread.join()
