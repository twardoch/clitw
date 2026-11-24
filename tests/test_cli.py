# this_file: tests/test_cli.py
"""Public Python and console operations against an isolated wrapper directory."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.fixture
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    store = home / "state with spaces"
    (store / "bin").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CLITW_HOME", str(store))
    monkeypatch.setenv("CLITW_ENV_FILE", str(home / ".env"))
    monkeypatch.setenv("MCP2CLI_CACHE_DIR", str(home / "native-cache"))
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    for var in ("MCP2CLI_CONFIG_DIR", "MCX_MCP_CONFIG", "MCPPROXY_API_URL"):
        monkeypatch.delenv(var, raising=False)
    return store


def invoke(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "clitwy", *args],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )


def test_catalog_matches_python_api_and_kind_filters(isolated_home: Path) -> None:
    from clitwy import catalog

    for kind in ("openapi", "mcp"):
        result = invoke("catalog", kind, "--json")
        assert result.returncode == 0, result.stderr
        entries = json.loads(result.stdout)
        assert isinstance(entries, dict) and entries == catalog(kind)
        assert all(name.startswith("mcx-") and isinstance(description, str) for name, description in entries.items())
    all_entries = invoke("catalog", "--json")
    assert all_entries.returncode == 0, all_entries.stderr
    assert json.loads(all_entries.stdout) == catalog()
    assert "mcx-deepseek-apik" in catalog("openapi")
    assert "mcx-runway-mcpo" in catalog("mcp")
    assert set(catalog()) == set(catalog("mcp")) | set(catalog("openapi"))


def test_run_preserves_arguments_and_child_exit_status(isolated_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from clitwy import run

    outfile = isolated_home / "args.json"
    wrapper = isolated_home / "bin" / ("mcx-qa-fixture.py" if os.name == "nt" else "mcx-qa-fixture")
    wrapper.write_text(
        f"#!{sys.executable}\nimport json, os, sys\nfrom pathlib import Path\n"
        'Path(os.environ["CLITW_TEST_OUTPUT"]).write_text(json.dumps(sys.argv[1:]))\nraise SystemExit(23)\n'
    )
    wrapper.chmod(0o755)
    monkeypatch.setenv("CLITW_TEST_OUTPUT", str(outfile))
    args = ["--query", "two words", "", "literal $EXAMPLE", "--json"]
    assert run("qa-fixture", args) == 23
    assert json.loads(outfile.read_text()) == args
    for spelling in ("qa-fixture", "mcx-qa-fixture", "tw-qa-fixture"):
        outfile.unlink()
        result = invoke("run", spelling, *args)
        assert result.returncode == 23, result.stderr
        assert json.loads(outfile.read_text()) == args


def test_missing_or_invalid_tool_has_actionable_exit_status(
    isolated_home: Path,
) -> None:
    from clitwy import run

    assert run("no-such-fixture", []) == 127
    assert run("../outside", []) == 2
    result = invoke("run", "no-such-fixture")
    assert result.returncode == 127 and result.stderr.strip()
    assert invoke("unknown-command-fixture").returncode != 0


def test_front_door_documents_each_integration_command(isolated_home: Path) -> None:
    help_result = invoke("--help")
    assert help_result.returncode == 0, help_result.stderr
    for name in ("catalog", "openapi", "mcp", "run", "auth", "info", "search"):
        assert name in help_result.stdout
    for kind in ("openapi", "mcp"):
        for action in ("install", "try"):
            result = invoke(kind, action, "--help")
            assert result.returncode == 0, result.stderr
            assert result.stdout.strip()


def test_wayback_quickstart_installs_and_lists_bundled_schema_offline(isolated_home: Path) -> None:
    result = invoke("openapi", "install", "mcx-archive-wayback")
    assert result.returncode == 0, result.stderr
    listing = invoke("run", "archive-wayback", "--list", "--json")
    assert listing.returncode == 0, listing.stderr
    operation = next(item for item in json.loads(listing.stdout) if item["path"] == "/wayback/available")
    assert operation["method"].upper() == "GET" and operation["name"] == "get-wayback-v1-available"
    help_result = invoke("run", "archive-wayback", operation["name"], "--help")
    assert help_result.returncode == 0 and "--url" in help_result.stdout


def test_auth_routes_methods_and_checks_both_without_a_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    from clitwy import cli, credentials
    from clitwy.mcp import login

    calls = []
    monkeypatch.setattr(login, "cli_login", lambda args, **kwargs: calls.append(("oauth", args)) or 0)
    monkeypatch.setattr(credentials, "cli_keys", lambda args: calls.append(("keys", args)) or 0)
    assert cli.main(["auth", "--list"]) == 0
    assert cli.main(["auth", "oauth", "pika"]) == 0
    assert cli.main(["auth", "keys", "mcx-openai-api"]) == 0
    assert cli.main(["mcp", "login", "--list"]) == 0
    assert calls == [
        ("oauth", ["--list"]),
        ("keys", ["--list"]),
        ("oauth", ["pika"]),
        ("keys", ["mcx-openai-api"]),
        ("oauth", ["--list"]),
    ]


def test_service_names_preserve_acronyms_and_resolve_ambiguous_interfaces():
    from clitwy.names import provider_name, resolve_provider

    key = provider_name("ExampleService API", "api", "k")
    oauth = provider_name("ExampleService", "mcp", "o")
    assert key == "mcx-example-service-apik"
    assert provider_name(key, "api", "k") == key
    assert oauth == "mcx-example-service-mcpo"
    assert resolve_provider("tw-example-service-mcpo", [key, oauth]) == oauth
    with pytest.raises(ValueError, match="Choose a provider"):
        resolve_provider("example-service", [key, oauth])
