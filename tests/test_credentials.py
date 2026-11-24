# this_file: tests/test_credentials.py
"""Credential enrollment and runtime scoping with private, offline fixtures."""

import hashlib
import json
import os
import sys
from dataclasses import replace

import pytest

from clitwy import credentials
from clitwy.mcp.install import cli_install
from clitwy.openapi.catalogue import providers
from clitwy.openapi.install import publish
from clitwy.openapi.native import execute
from clitwy.paths import home, registry_dir


@pytest.fixture
def bindings(tmp_path, monkeypatch):
    monkeypatch.setenv("CLITW_HOME", str(tmp_path / "private"))
    monkeypatch.delenv("CLITW_DOTENV", raising=False)
    monkeypatch.delenv("AIMLAPI_KEY", raising=False)
    monkeypatch.delenv("CLITW_OPENAPI_ANONYMOUS", raising=False)
    assert cli_install(["aimlapi", "pika"]) == 0
    body = b'{"openapi":"3.0.3","paths":{}}'
    staged = tmp_path / (hashlib.sha256(body).hexdigest() + ".json")
    staged.write_bytes(body)
    tool = providers()["mcx-aimlapi-apik"][0]
    assert publish(tool, staged, 1, True, False)
    return tool, staged


def test_listing_noninteractive_and_opt_in(bindings, monkeypatch, capsys):
    def unexpected(*args):
        pytest.fail("read attempted in list mode or from noninteractive input")

    monkeypatch.setattr(credentials.getpass, "getpass", unexpected)
    assert credentials.cli_keys(["--list"]) == 0
    out = capsys.readouterr().out
    line = next(line for line in out.splitlines() if "AIMLAPI_KEY" in line)
    assert line == "AIMLAPI_KEY\tmissing\tmcx-aimlapi-apik, mcx-aimlapi-mcpk"
    assert credentials.cli_keys(["tw-aimlapi"]) == 0
    assert not (home() / "credentials.json").exists()
    for stream in (sys.stdin, sys.stderr):
        monkeypatch.setattr(stream, "isatty", lambda: True)
    assert credentials.cli_keys(["--list", "pika", "not-installed"]) == 0
    monkeypatch.setattr(credentials.getpass, "getpass", lambda prompt: "")
    assert credentials.cli_keys([]) == 0
    assert not (home() / "credentials.json").exists()
    prompts = []

    def answer(prompt):
        prompts.append(prompt)
        return "fixture-hidden-value"

    monkeypatch.setattr(credentials.getpass, "getpass", answer)
    assert credentials.cli_keys(["mcx-aimlapi-api", "aimlapi"]) == 0
    assert len(prompts) == 1 and "AIMLAPI_KEY" in prompts[0]
    assert credentials.saved_values() == {"AIMLAPI_KEY": "fixture-hidden-value"}
    store = home() / "credentials.json"
    if os.name == "posix":
        assert store.stat().st_mode & 0o777 == 0o600
    assert credentials.cli_keys([]) == 0
    output = capsys.readouterr()
    assert "AIMLAPI_KEY\tconfigured" in output.out
    assert "fixture-hidden-value" not in output.out + output.err
    assert len(prompts) == 1


def test_runtime_resolution_respects_origin_and_source_precedence(bindings, monkeypatch, tmp_path):
    tool, staged = bindings
    credentials.save_key("AIMLAPI_KEY", "saved-fixture")
    recorded = []

    def capture(executable, args, env):
        assert not any("fixture" in word for word in args if word.startswith("Bearer "))
        recorded.append(env.get("CLITW_OPENAPI_HEADER_0"))

    monkeypatch.setattr(os, "execve", capture)
    execute(tool.name, ["--list"])
    assert recorded[-1] == "Bearer saved-fixture"
    env_file = tmp_path / "selected.env"
    env_file.write_text("AIMLAPI_KEY=dotenv-fixture\n")
    monkeypatch.setenv("CLITW_DOTENV", str(env_file))
    execute(tool.name, ["--list"])
    assert recorded[-1] == "Bearer dotenv-fixture"
    monkeypatch.setenv("AIMLAPI_KEY", "env-fixture")
    execute(tool.name, ["--list"])
    assert recorded[-1] == "Bearer env-fixture"
    monkeypatch.delenv("AIMLAPI_KEY")
    assert publish(replace(tool, base="https://custom.example"), staged, 1, True, False)
    execute(tool.name, ["--list"])
    assert recorded[-1] is None
    assert tool.name not in credentials.installed_keys()


def test_mcp_uses_saved_secret_for_original_preset(bindings, monkeypatch):
    import logging

    import mcp2cli

    from clitwy.mcp import runtime

    literal = "fixture-$" + "{LITERAL}"
    credentials.save_key("AIMLAPI_KEY", literal)
    registry = json.loads((registry_dir() / "baked.json").read_text())
    entry = registry["mcx-aimlapi-mcpk"]["clitw_mcp"]
    recorded = []
    monkeypatch.setattr(runtime, "safe_transport", lambda *args: None)
    monkeypatch.setattr(logging, "disable", lambda *args: None)
    for name in ("MCP2CLI_CACHE_DIR", "MCP2CLI_CONFIG_DIR", "_CLITW_MCP_HEADER_0"):
        monkeypatch.setenv(name, "")
    monkeypatch.setattr(
        mcp2cli, "_main_impl", lambda args, **kw: recorded.append((args, os.getenv("_CLITW_MCP_HEADER_0")))
    )
    runtime.invoke(str(home()), "catalog", "mcx-aimlapi-mcpk", entry["pin"], ["--list"])
    args, header = recorded[0]
    assert "Authorization:env:_CLITW_MCP_HEADER_0" in args
    assert header == "Bearer " + literal and all(literal not in arg for arg in args)


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission and symlink semantics")
def test_key_store_rejects_links_and_public_permissions(tmp_path, monkeypatch):
    monkeypatch.setenv("CLITW_HOME", str(tmp_path))
    outside = tmp_path / "another-file"
    outside.write_text("{}")
    store = tmp_path / "credentials.json"
    store.symlink_to(outside)
    with pytest.raises(ValueError):
        credentials.save_key("A_KEY", "test")
    assert outside.read_text() == "{}"
    store.unlink()
    store.write_text("{}")
    store.chmod(0o644)
    with pytest.raises(ValueError, match="600"):
        credentials.saved_values()


def test_google_app_credentials_and_empty_dotenv_fallback(bindings, monkeypatch, tmp_path, capsys):
    monkeypatch.delenv("GOOGLE_MCP_CLIENT_SECRET", raising=False)
    monkeypatch.setenv("GOOGLE_MCP_CLIENT_ID", "")
    dotfile = tmp_path / "google.env"
    dotfile.write_text("GOOGLE_MCP_CLIENT_SECRET=\nGOOGLE_MCP_CLIENT_ID=fixture-desktop-app\n")
    monkeypatch.setenv("CLITW_DOTENV", str(dotfile))
    credentials.save_key("GOOGLE_MCP_CLIENT_SECRET", "fixture-saved-app-secret")
    assert cli_install(["gmail", "google-drive"]) == 0
    assert credentials.cli_keys(["--list", "gmail", "tw-google-drive"]) == 0
    lines = [s for s in capsys.readouterr().out.splitlines() if s.startswith("GOOGLE_MCP_")]
    assert lines == [
        "GOOGLE_MCP_CLIENT_ID\tconfigured\tmcx-gmail-mcpo, mcx-google-drive-mcpo",
        "GOOGLE_MCP_CLIENT_SECRET\tconfigured\tmcx-gmail-mcpo, mcx-google-drive-mcpo",
    ]
    assert credentials.available_keys()["GOOGLE_MCP_CLIENT_SECRET"] == "fixture-saved-app-secret"
    assert credentials.oauth_client_variables("mcx-gmail", {}) == {}
    assert credentials.oauth_client_variables("mcx-gmail", {"oauth": {"client_secret": "unrelated"}}) == {}
    assert (
        credentials.oauth_client_variables("mcx-other", {"oauth": {"client_id": "$" + "{env:GOOGLE_MCP_CLIENT_ID}"}})
        == {}
    )


def test_store_blocks_unrelated_users_from_reading_sensitive_credentials(tmp_path, monkeypatch):
    monkeypatch.setenv("CLITW_HOME", str(tmp_path))
    credentials.save_key("A_KEY", "test")
    if os.name == "posix":
        monkeypatch.setattr(os, "getuid", lambda: (tmp_path / "credentials.json").stat().st_uid + 1)
        with pytest.raises(ValueError, match="private"):
            credentials.saved_values()


def test_private_mcp_templates_do_not_read_central_dotenv_or_saved_key(bindings, tmp_path, monkeypatch):
    from clitwy.mcp.runtime import expand

    credentials.save_key("AIMLAPI_KEY", "fixture-saved")
    dotfile = tmp_path / "selected.env"
    dotfile.write_text("AIMLAPI_KEY=fixture-external\n")
    monkeypatch.setenv("CLITW_DOTENV", str(dotfile))
    ref = "$" + "{env:AIMLAPI_KEY}"
    with pytest.raises(ValueError, match="missing configured environment variable AIMLAPI_KEY"):
        expand(ref, {})
    assert expand(ref, {"environment": {"custom_vars": {"AIMLAPI_KEY": "fixture-local"}}}) == "fixture-local"
    assert credentials.available_keys()["AIMLAPI_KEY"] == "fixture-external"
