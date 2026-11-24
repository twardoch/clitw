# this_file: tests/test_openapi.py
"""Verify the shipped bindings and the native client against an isolated loopback provider."""

from __future__ import annotations

import hashlib
import json
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from clitwy.openapi import catalog, cli_install, cli_try
from clitwy.openapi.catalogue import providers
from clitwy.openapi.state import header_args, registry, valid_url
from clitwy.paths import provider_argv, provider_path, registry_dir

FAKE = "synthetic-test-credential"


@pytest.fixture
def provider(tmp_path, monkeypatch):
    monkeypatch.setenv("CLITW_HOME", str(tmp_path / "private"))
    monkeypatch.setenv("FIXTURE_TOKEN", FAKE)
    events = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), BaseHTTPRequestHandler)
    base = f"http://127.0.0.1:{server.server_port}"
    spec = {"openapi": "3.0.3", "info": {"title": "Fixture", "version": "1"}, "paths": {}}
    for route, operation in (("/meta", "metadata"), ("/failure", "appFailure")):
        spec["paths"][route] = {"get": {"operationId": operation, "responses": {"200": {"description": "OK"}}}}
    spec["paths"]["/meta"]["get"]["parameters"] = [{"in": "query", "name": "handle", "schema": {"type": "string"}}]
    spec["paths"]["/other"] = {"get": {"operationId": "elsewhere", "servers": [{"url": "https://example.invalid"}]}}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            url = urlsplit(self.path)
            events.append((url.path, parse_qs(url.query), self.headers.get("Authorization")))
            body = json.dumps(
                spec
                if url.path == "/spec"
                else {"success": False, "error": "private-provider-detail"}
                if url.path == "/failure"
                else {"message": "private-provider-detail"}
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server.RequestHandlerClass = Handler
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield base, events, spec
    server.shutdown()
    thread.join()
    server.server_close()


def install(base, name="sample"):
    return cli_install(
        [
            "--name",
            name,
            "--spec",
            base + "/spec",
            "--base-url",
            base,
            "--auth-header",
            "Authorization:Bearer env:FIXTURE_TOKEN",
        ]
    )


def test_public_catalogue_when_offline_then_all_bindings_are_safe():
    data = catalog()
    assert len(data) == 257
    assert sum(bool(tool.base) for tool, _ in providers().values()) == 235
    from clitwy.openapi.upgrade import RETIRED

    assert len(RETIRED) == 14 and not (set(RETIRED) & data.keys())
    assert {
        "mcx-chutes-apik",
        "mcx-google-custom-search-apik",
        "mcx-g-analyticsadmin-apio",
        "mcx-g-analyticsdata-apio",
    } <= data.keys()
    assert "mcx-archive-wayback-api" in data and "mcx-elevenlabs-apik" in data
    for name, (tool, read) in providers().items():
        assert name.startswith("mcx-")
        assert tool.source == "package:wayback.json" or valid_url(tool.source)
        assert not tool.base or valid_url(tool.base, base=True)
        header_args(tool.auth, {})
        assert read.method in {"-", "GET", "POST"}


def test_install_and_native_execution_when_authenticated_then_protect_source_and_pins(provider):
    base, events, _ = provider
    assert install(base) == 0
    assert events == [("/spec", {}, None)]
    exe = provider_argv("mcx-sample-apik")
    listing = subprocess.run([*exe, "--list", "--json"], check=True, capture_output=True)
    assert {op["name"] for op in json.loads(listing.stdout)} == {"metadata", "app-failure"}
    probe = subprocess.run([*exe, "metadata", "--handle", "@someone"], check=True, capture_output=True)
    assert "private-provider-detail" in probe.stdout.decode()
    assert events[-1] == ("/meta", {"handle": ["@someone"]}, "Bearer " + FAKE)
    count = len(events)
    for args in [["--base-url", base, "metadata"], ["--sp", base + "/spec", "metadata"], ["@unrelated"]]:
        rejected = subprocess.run([*exe, *args], check=False, capture_output=True)
        assert rejected.returncode != 0 and len(events) == count
    reg = registry_dir() / "openapi.json"
    assert FAKE not in reg.read_text() and FAKE not in provider_path("mcx-sample-apik").read_text()
    assert not (registry_dir() / "baked.json").exists()
    snapshot = Path(registry()["mcx-sample-apik"]["source"])
    snapshot.write_text("{}")
    assert subprocess.run([*exe, "metadata"], check=False, capture_output=True).returncode == 2
    assert len(events) == count


def test_try_when_provider_error_inside_http_200_then_fails_without_bodies(provider, tmp_path):
    base, events, _ = provider
    assert install(base) == 0
    report = tmp_path / "report.jsonl"
    args = ["tw-sample", "--output", str(report)]
    assert cli_try(args) == 0
    assert json.loads(report.read_text())["phase"] == "discovery"
    assert events == [("/spec", {}, None)]
    assert cli_try([*args, "--probe-get", "/meta"]) == 0
    result = json.loads(report.read_text())
    assert result["status"] == "passed" and result["http_status"] == 200 and result["credential_supplied"]
    assert cli_try([*args, "--probe-get", "/failure"]) == 1
    result = json.loads(report.read_text())
    assert result["status"] == "failed" and result["http_status"] == 200
    assert FAKE not in report.read_text() and "private-provider-detail" not in report.read_text()


def test_install_when_existing_wrapper_is_changed_then_preserves_owner_copy(provider):
    base, _, _ = provider
    assert install(base) == 0
    path = provider_path("mcx-sample-apik")
    path.write_text("this file now belongs to the caller")
    before = (registry_dir() / "openapi.json").read_bytes()
    assert install(base) == 1
    assert path.read_text() == "this file now belongs to the caller"
    assert (registry_dir() / "openapi.json").read_bytes() == before


def test_custom_local_file_when_no_origin_then_only_local_discovery(provider, tmp_path, monkeypatch):
    _, events, spec = provider
    monkeypatch.chdir(tmp_path)
    Path("spec.json").write_text(json.dumps(spec))
    assert cli_install(["sample", "--spec", "spec.json"]) == 0
    exe = provider_argv("mcx-sample-api")
    assert subprocess.run([*exe, "--list"], check=False, capture_output=True).returncode == 0
    assert subprocess.run([*exe, "metadata"], check=False, capture_output=True).returncode == 2
    assert not events


def test_invalid_custom_url_and_auth_fail_before_download(provider):
    base, events, _ = provider
    assert cli_install(["sample", "--spec", base + "/spec", "--base-url", "http://external.example"]) == 1
    assert cli_install(["sample", "--spec", base + "/spec", "--base-url", base, "--auth-header", "X-Key:literal"]) == 1
    assert not events


def test_shared_resolver_when_multiple_cycles_then_matches_native_graph_and_reuses_equal_branches():
    from mcp2cli import resolve_refs

    from clitwy.openapi.references import resolve_shared

    ref = lambda name: {"$ref": "#/components/schemas/" + name}
    schema = {
        "paths": {"/one": ref("A"), "/two": ref("A"), "/three": ref("B"), "/dangling": ref("absent")},
        "components": {
            "schemas": {
                "A": {"fields": [ref("B"), ref("D"), ref("C")]},
                "B": {"fields": [ref("A"), ref("D")]},
                "C": {"fields": [ref("B"), ref("D")]},
                "D": {"type": "string"},
            }
        },
    }
    actual = resolve_shared(schema)
    assert actual == resolve_refs(schema)
    assert actual["paths"]["/one"] is actual["paths"]["/two"]
    assert actual["components"]["schemas"]["A"]["fields"][1] is actual["components"]["schemas"]["B"]["fields"][1]


def test_customer_base_when_missing_then_discovery_is_explicitly_skipped(provider, tmp_path):
    _, events, spec = provider
    source = tmp_path / "fixture.json"
    source.write_text(json.dumps(spec))
    assert cli_install(["sample", "--spec", str(source)]) == 0
    report = tmp_path / "result.jsonl"
    assert cli_try(["sample", "--probe-get", "/meta", "--output", str(report)]) == 0
    assert json.loads(report.read_text())["status"] == "skipped"
    assert not events


def _select_presets(monkeypatch, rows):
    from clitwy.openapi import install as installer
    from clitwy.openapi.catalogue import Probe

    monkeypatch.setattr(
        installer, "providers", lambda: {tool.name: (tool, Probe("-", "-", "fixture")) for tool in rows}
    )


@pytest.mark.parametrize(
    "document",
    [
        None,
        "<html>Login required</html>",
        "unbalanced: [1",
        "{}",
        '{"openapi":"bogus","paths":{}}',
        '{"openapi":"3.1.0","paths":{}}',
        '{"openapi":"3.1.0","paths":{"/x":{"get":null}}}',
    ],
)
def test_unusable_preset_when_selected_then_skip_without_any_install_state(tmp_path, monkeypatch, capsys, document):
    from clitwy.openapi.catalogue import Tool

    root = tmp_path / "private"
    monkeypatch.setenv("CLITW_HOME", str(root))
    path = tmp_path / "fixture"
    if document is not None:
        path.write_text(document)
    _select_presets(monkeypatch, [Tool("mcx-fixture-api", str(path), "https://example.invalid", "", "example")])
    assert cli_install(["fixture"]) == 0
    assert "mcx-fixture-api: skipped" in capsys.readouterr().out
    assert not root.exists()
    assert cli_install(["fixture", "--spec", str(path)]) == 1
    assert not root.exists()


def test_bulk_skips_bad_schema_and_refresh_preserves_last_working_snapshot(provider, monkeypatch, tmp_path, capsys):
    from clitwy.openapi.catalogue import Tool
    from clitwy.paths import home

    base, events, spec = provider
    good = Tool("mcx-good-api", base + "/spec", base, "", "good")
    bad = Tool("mcx-bad-api", str(tmp_path / "missing"), base, "", "bad")
    _select_presets(monkeypatch, [good, bad])
    assert cli_install([]) == 0
    output = capsys.readouterr().out
    assert "mcx-good-api: 2 commands" in output and "mcx-bad-api: skipped" in output
    original = (registry_dir() / "openapi.json").read_bytes()
    files = set((home() / "openapi").rglob("*"))
    assert cli_install([]) == 0
    assert (registry_dir() / "openapi.json").read_bytes() == original
    spec["paths"].clear()
    assert cli_install([]) == 0
    assert "mcx-good-api: skipped" in capsys.readouterr().out
    assert (registry_dir() / "openapi.json").read_bytes() == original
    assert set((home() / "openapi").rglob("*")) == files
    assert subprocess.run([*provider_argv(good.name), "metadata"], capture_output=True, check=False).returncode == 0
    assert events[-1][0] == "/meta" and not provider_path(bad.name).exists()


def test_bulk_preserves_explicit_base_and_custom_name_even_if_the_catalogue_has_a_default(
    provider, monkeypatch, tmp_path
):
    from clitwy.openapi.catalogue import Tool

    base, events, _ = provider
    needs_base = Tool("mcx-tenant-api", base + "/spec", "", "", "tenant")
    custom = Tool("mcx-custom-apik", base + "/spec", "https://example.invalid", "", "custom")
    changed = Tool("mcx-configured-api", base + "/spec", "https://example.invalid", "", "configured")
    _select_presets(monkeypatch, [needs_base, custom, changed])
    assert cli_install(["tenant"]) == 0
    assert not provider_path(needs_base.name).exists() and not events
    assert cli_install(["tenant", "--base-url", base]) == 0
    assert install(base, "custom") == 0
    assert cli_install(["configured", "--base-url", base]) == 0
    before, loads = registry(), len(events)
    assert cli_install([]) == 0
    assert registry() == before and len(events) == loads


def test_obsolete_default_needs_both_receipts_and_preserves_custom_and_modified_files(provider, monkeypatch, tmp_path):
    from clitwy.openapi.catalogue import Tool
    from clitwy.openapi.state import wrapper
    from clitwy.paths import home

    _, _, spec = provider
    path = tmp_path / "retired.json"
    path.write_text(json.dumps(spec))
    # Simulate prior installations, including their original registry receipts and generated executables.
    names = ("mcx-imdb-apidev", "mcx-proxycurl", "mcx-msft-imagesearch", "mcx-msft-websearch")
    entries = {name: _pre_naming(name, path.read_bytes()) for name in names}
    for name in names[:3]:
        entries[name].update(preset=True)
        entries[name].pop("customized")
    provider_path(names[1]).write_text("owner replacement")
    snap = Path(entries[names[2]]["source"])
    snap.write_text("owner snapshot")
    target = registry_dir() / "openapi.json"
    target.write_text(json.dumps(entries))
    assert subprocess.run([*provider_argv(names[0]), "--list"], capture_output=True, check=False).returncode == 2
    _select_presets(monkeypatch, [Tool("mcx-tenant-api", "https://example.invalid/spec", "", "", "tenant")])
    assert cli_install([]) == 0
    assert not provider_path(names[0]).exists() and names[0] not in registry()
    assert Path(entries[names[0]]["source"]).is_file()
    typed_custom = names[3] + "-api"
    assert {names[1], names[2], typed_custom} == registry().keys()
    assert provider_path(names[1]).read_text() == "owner replacement"
    assert snap.read_text() == "owner snapshot"
    assert provider_path(typed_custom).read_bytes() == wrapper(typed_custom)
    assert (home() / "openapi").exists()
    assert cli_install([]) == 0


def test_no_durable_state_when_native_rejects_an_apparently_valid_schema(provider, monkeypatch, capsys):
    from clitwy.openapi import schema
    from clitwy.openapi.catalogue import Tool
    from clitwy.paths import home

    base, _, _ = provider
    _select_presets(monkeypatch, [Tool("mcx-fixture-api", base + "/spec", base, "", "example")])

    def reject_native_command(*args, **kwargs):
        raise ValueError("invalid operation schema")

    monkeypatch.setattr(schema, "run", reject_native_command)
    assert cli_install(["fixture"]) == 0
    assert "skipped (native client could not discover usable operations)" in capsys.readouterr().out
    assert not home().exists()


def test_documentation_only_refs_and_missing_release_metadata_allow_working_commands(provider):
    from clitwy.openapi.catalogue import Tool
    from clitwy.openapi.schema import normalized

    base, _, spec = provider
    spec["info"].pop("version")
    spec["paths"]["/meta"]["get"]["x-fern-examples"] = [{"code": {"$ref": "./sample.md"}}]
    tool = Tool("mcx-fixture-api", base + "/spec", base, "", "example")
    doc, *_ = normalized(tool, json.dumps(spec).encode())
    assert "get" in json.loads(doc)["paths"]["/meta"]
    spec["paths"]["/meta"]["get"]["parameters"][0]["schema"] = {"$ref": "./external.yaml"}
    with pytest.raises(ValueError, match="external references"):
        normalized(tool, json.dumps(spec).encode())


def _pre_naming(name, body, auth="", base="", preset=False, executable=None):
    from clitwy.openapi.state import atomic, wrapper
    from clitwy.paths import home

    snapshot = home() / "openapi" / name / (hashlib.sha256(body).hexdigest() + ".json")
    atomic(snapshot, body)
    script = wrapper(name, executable)
    atomic(provider_path(name), script, 0o755)
    registry_dir().mkdir(parents=True, exist_ok=True)
    return {
        "source": str(snapshot),
        "auth": auth,
        "base_url": base,
        "preset": preset,
        "customized": not preset,
        "description": "Test API",
        "command_count": 1,
        "wrapper_sha256": hashlib.sha256(script).hexdigest(),
    }


def test_offline_rename_keeps_custom_bindings_and_user_files(provider):
    from clitwy.openapi.state import wrapper

    base, events, spec = provider
    body = json.dumps(spec).encode()
    auth = "X-Key:env:FIXTURE_TOKEN"
    entries = {name: _pre_naming(name, body, auth, base) for name in ("mcx-private", "mcx-changed", "mcx-blocked")}
    owner = provider_path("mcx-changed")
    owner.write_text("owner command")
    existing = provider_path("mcx-blocked-apik")
    existing.write_text("owner destination")
    target = registry_dir() / "openapi.json"
    target.write_text(json.dumps(entries))
    assert cli_install(["--migrate-names"]) == 0
    data = registry()
    result = data["mcx-private-apik"]
    assert result["auth"] == auth and result["base_url"] == base and result["previous_name"] == "mcx-private"
    assert Path(result["source"]).read_bytes() == body and provider_path("mcx-private-apik").read_bytes() == wrapper(
        "mcx-private-apik"
    )
    assert not provider_path("mcx-private").exists() and "mcx-private" not in data
    assert data["mcx-changed"] == entries["mcx-changed"] and owner.read_text() == "owner command"
    assert data["mcx-blocked"] == entries["mcx-blocked"] and existing.read_text() == "owner destination"
    assert not events
    assert cli_install(["--migrate-names"]) == 0 and data == registry()


def test_catalogue_and_auth_names_match_the_shipped_wrapper_binding():
    from clitwy.names import provider_name
    from clitwy.openapi.catalogue import auth_kind, legacy_aliases

    for tool, _ in providers().values():
        assert tool.name == provider_name(tool.name, "api", auth_kind(tool.auth))
    assert auth_kind("User-Agent:clitwy/1.0") == ""
    assert auth_kind("Authorization:Bearer env:GOOGLE_OAUTH_ACCESS_TOKEN") == "o"
    assert auth_kind("Authorization:Bearer env:OPENAI_API_KEY") == "k"
    assert (
        legacy_aliases()["mcx-mailchimp"]
        == legacy_aliases()["mcx-mailchimp-marketing"]
        == legacy_aliases()["mcx-mailchimp-oas"]
    )


def test_migrate_from_another_python_path_preserves_the_native_schema(provider, tmp_path):
    import os

    from clitwy.openapi.upgrade import migrate_names

    base, events, spec = provider
    executable = str(tmp_path / "prior env" / "python") if os.name == "posix" else str(tmp_path / "python.exe")
    previous = _pre_naming("mcx-local-old", json.dumps(spec).encode(), base=base, executable=executable)
    (registry_dir() / "openapi.json").write_text(json.dumps({"mcx-local-old": previous}))
    assert migrate_names() == {"mcx-local-old": "mcx-local-old-api"}
    cmd = [*provider_argv("mcx-local-old-api"), "--list", "--json"]
    assert subprocess.run(cmd, capture_output=True, check=False).returncode == 0 and not events


@pytest.mark.parametrize("name", ["mcx-lmnt", "mcx-lmnt-apik"])
@pytest.mark.parametrize("modified", [None, "snapshot", "registration", "launcher", "custom"])
def test_lmnt_migration_retires_only_frozen_stock(name, modified, tmp_path, monkeypatch):
    from clitwy.openapi import upgrade

    monkeypatch.setenv("CLITW_HOME", str(tmp_path))
    payload = b'{"info": {"title": "historical test schema"}}'
    entry = _pre_naming(name, payload)
    stock = "LMNT — low-latency speech synthesis"
    entry.update(
        base_url="https://api.lmnt.com",
        auth="x-api-key:env:LMNT_API_KEY",
        description=stock,
        preset=modified != "custom",
        customized=modified == "custom",
    )
    source = Path(entry["source"])
    monkeypatch.setattr(upgrade, "LMNT_SNAPSHOT", source.stem if modified != "snapshot" else "f" * 64)
    if modified == "registration":
        entry["description"] = "owner description"
    if modified == "launcher":
        provider_path(name).write_text("owner executable")
    registry_dir().mkdir(exist_ok=True, parents=True)
    (registry_dir() / "openapi.json").write_text(json.dumps({name: entry}))
    assert cli_install(["--migrate-names"]) == 0
    assert (name in registry()) == bool(modified)
    assert provider_path(name).exists() == bool(modified)
    assert source.read_bytes() == payload
    if modified:
        assert registry()[name] == entry
    assert cli_install(["--migrate-names"]) == 0
