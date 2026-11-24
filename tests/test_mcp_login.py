# this_file: tests/test_mcp_login.py
"""Exercise the guide against an installed-wrapper stand-in without opening a browser."""

import hashlib
import io
import json
import sys
import time
from dataclasses import replace
from pathlib import Path

import pytest

from clitw import mcp
from clitw.mcp import login


class Terminal(io.StringIO):
    def isatty(self):
        return True


@pytest.fixture
def wrapper(tmp_path, monkeypatch):
    path = tmp_path / "client.py"
    path.write_text(
        """import json, os, sys
from pathlib import Path

state, profile, *args = sys.argv[1:]
state = Path(state)
events = state.with_suffix('.events')
with events.open('a') as log:
    print(json.dumps([args, os.getenv('CLITW_MCP_NONINTERACTIVE')]), file=log)
if '--oauth' in args:
    assert os.getenv('CLITW_MCP_NONINTERACTIVE') is None
    print('Browser fallback is visible on the terminal.', file=sys.stderr)
    print('SECRET-TOOL-CONTENTS')
    if profile != 'unchecked':
        state.touch()
    sys.exit(0)
assert os.getenv('CLITW_MCP_NONINTERACTIVE') == '1'
if profile == 'rate':
    sys.exit('HTTP 429, unauthorized later')
if profile == 'offline':
    sys.exit('ConnectError')
public = profile in ('pika', 'google', 'unchecked')
if '--list' in args and (public or state.exists()):
    print(json.dumps(['list-apps','list-labels','list-recent-files']))
elif state.exists():
    print(json.dumps({'content': [{'type':'text','text':'SECRET-TOOL-CONTENTS'}]}))
else:
    sys.exit('HTTP 401 PRIVATE-DIAGNOSTIC')
"""
    )
    monkeypatch.setattr(login, "advertises_oauth", lambda *_: True)
    monkeypatch.delenv("CLITW_MCP_NONINTERACTIVE", raising=False)
    monkeypatch.delenv("MCX_MCP_NONINTERACTIVE", raising=False)

    def create(name, profile, url="https://fixture.example/mcp", **attrs):
        return login.Binding(
            f"mcx-{name}",
            (sys.executable, str(path), str(tmp_path / name), profile),
            url,
            **attrs,
        )

    return create


def events(tmp_path, name):
    fn = tmp_path / f"{name}.events"
    return [json.loads(line) for line in fn.read_text().splitlines()] if fn.exists() else []


def guide(monkeypatch, inputs):
    monkeypatch.setattr(sys, "stdin", Terminal(inputs))
    monkeypatch.setattr(sys.stderr, "isatty", lambda: True)


def test_public_pika_discovery_needs_protected_login(wrapper, tmp_path, monkeypatch, capfd):
    item = wrapper("pika", "pika", login.PIKA)
    guide(monkeypatch, "login\n")
    assert login.workflow(["pika"], lambda: [item]) == 0
    out, err = capfd.readouterr()
    assert "authentication required" in out and "protected list-apps verified" in out
    assert "Browser fallback" in err
    assert "PRIVATE-DIAGNOSTIC" not in out + err and "SECRET-TOOL-CONTENTS" not in out + err
    args = events(tmp_path, "pika")
    assert [x[0][-1] for x in args] == ["--json", "list-apps", "list-apps", "--json", "list-apps"]
    assert [x[1] for x in args] == ["1", "1", None, "1", "1"]
    assert "--oauth" in args[2][0] and "authorization_code" in args[2][0]
    assert login.workflow(["pika", "--list"], lambda: [item]) == 0
    assert len(events(tmp_path, "pika")) == 7


@pytest.mark.parametrize("url", list(login.GOOGLE_READS))
def test_google_account_read_requires_consent_and_registered_client(wrapper, tmp_path, monkeypatch, capfd, url):
    item = wrapper("service", "google", url)
    guide(monkeypatch, "login\n")
    assert login.workflow([], lambda: [item]) == 0
    assert len(events(tmp_path, "service")) == 1 and "oauth.client_id" in capfd.readouterr().out
    item = replace(item, client_configured=True)
    assert login.workflow(["--list"], lambda: [item]) == 0
    assert len(events(tmp_path, "service")) == 2
    assert login.workflow([], lambda: [item]) == 0
    out, err = capfd.readouterr()
    assert "verified" in out and "discards the response" in err and "SECRET-TOOL-CONTENTS" not in out + err
    calls = events(tmp_path, "service")
    read = list(login.GOOGLE_READS[url])
    assert calls[3][0][-len(read) :] == read and calls[5][0] == ["--json", *read]


def test_sequential_per_provider_choices(wrapper, tmp_path, monkeypatch, capfd):
    rows = [wrapper(n, "auth") for n in ("a", "b", "c", "d")]
    guide(monkeypatch, "skip\nlogin\nquit\n")
    assert login.workflow([], lambda: rows, quit_code=130) == 130
    assert [len(events(tmp_path, n)) for n in ("a", "b", "c", "d")] == [1, 3, 1, 0]
    assert "mcx-b: verified" in capfd.readouterr().out


@pytest.mark.parametrize("guard", ["MCX_MCP_NONINTERACTIVE", "CLITW_MCP_NONINTERACTIVE", "pipe", "list"])
def test_unattended_never_authorizes(wrapper, tmp_path, monkeypatch, guard):
    guide(monkeypatch, "yes\n")
    if guard == "pipe":
        monkeypatch.setattr(sys, "stdin", io.StringIO("yes\n"))
    elif guard != "list":
        monkeypatch.setenv(guard, "1")
    assert login.workflow(["--list"] if guard == "list" else [], lambda: [wrapper("one", "auth")]) == 0
    assert len(events(tmp_path, "one")) == 1


def test_ineligible_and_unavailable_never_open_browser(wrapper, tmp_path, monkeypatch, capfd):
    guide(monkeypatch, "yes\n" * 6)
    rows = [
        wrapper("rate", "rate"),
        wrapper("offline", "offline"),
        wrapper("held", "auth", skip="quarantined"),
        wrapper("proxy", "auth", skip="proxy group"),
        wrapper("local", "auth", "https://host.home.arpa/mcp"),
        wrapper("key", "auth"),
    ]
    monkeypatch.setattr(login, "advertises_oauth", lambda *_: False)
    assert login.workflow([], lambda: rows) == 0
    out, err = capfd.readouterr()
    for msg in ("HTTP 429", "unavailable", "quarantined", "proxy group", "private endpoint", "OAuth not advertised"):
        assert msg in out
    assert "Authorize" not in err
    assert [len(events(tmp_path, x)) for x in ("rate", "offline", "held", "proxy", "local", "key")] == [
        1,
        1,
        0,
        0,
        0,
        1,
    ]


def test_schema_only_never_claims_login_and_ignores_stale_grant(wrapper, tmp_path, monkeypatch, capfd):
    item = wrapper("schema", "unchecked", cache_dir=str(tmp_path))
    cache = tmp_path / "oauth" / hashlib.sha256(item.url.encode()).hexdigest()[:16]
    cache.mkdir(parents=True)
    (cache / "tokens.json").write_text('{"access_token":"SECRET"}')
    (cache / "tokens_meta.json").write_text('{"expires_at": 0}')
    guide(monkeypatch, "login\n")
    assert login.workflow([], lambda: [item]) == 1
    out, err = capfd.readouterr()
    assert "no saved OAuth grant confirmed" in out and "SECRET" not in out + err
    (cache / "tokens_meta.json").write_text(json.dumps({"expires_at": time.time() + 600}))
    assert login.workflow([], lambda: [item]) == 0
    assert "saved OAuth grant" in capfd.readouterr().out


def test_metadata_requires_matching_resource_and_public_issuer(monkeypatch):
    url = "https://fixture.example/mcp/v1"
    document = {"resource": url, "authorization_servers": ["https://auth.vendor.example/issuer"]}
    seen = []

    def get(endpoint, timeout):
        seen.append(endpoint)
        return document

    monkeypatch.setattr(login, "metadata", get)
    assert login.advertises_oauth(url, 1)
    assert seen == ["https://fixture.example/.well-known/oauth-protected-resource/mcp/v1"]
    document["resource"] = "https://other.example/mcp/v1"
    assert not login.advertises_oauth(url, 1)
    assert all("/.well-known/" in u for u in seen)


def test_installed_adapter_checks_config_pins_and_ownership(tmp_path, monkeypatch):
    monkeypatch.setenv("CLITW_HOME", str(tmp_path))
    assert login.packaged_bindings() == []
    conf = tmp_path / "servers.json"
    monkeypatch.setenv("GOOGLE_ID", "fixture-user-client")
    row = {"url": "https://server.example/mcp", "oauth": {"client_id": "${env:GOOGLE_ID}"}}
    conf.write_text(json.dumps({"mcpServers": {"demo": row}}))
    assert mcp.cli_install(["--config", str(conf), "mcx-demo-mcpo"]) == 0
    (item,) = login.packaged_bindings()
    assert item.url == row["url"] and item.command and item.client_configured
    Path(item.command[-1]).write_text("foreign")
    assert "modified" in login.packaged_bindings()[0].skip
    row["url"] = "https://changed.example/mcp"
    conf.write_text(json.dumps({"mcpServers": {"demo": row}}))
    assert login.packaged_bindings()[0].skip == "endpoint changed"
    row["quarantined"] = True
    conf.write_text(json.dumps({"mcpServers": {"demo": row}}))
    assert login.packaged_bindings()[0].skip == "quarantined"


def test_native_google_authorization_url_honors_exact_scope(tmp_path, monkeypatch):
    """Feed the real SDK a resource advertising modify+send and check its actual PKCE browser URL."""
    import asyncio
    from urllib.parse import parse_qs, urlsplit

    import httpx2
    import mcp2cli
    from mcp.shared.auth import AuthorizationCodeResult

    from clitw.mcp.oauth import preserve_requested_scope

    monkeypatch.setattr(mcp2cli, "OAUTH_DIR", tmp_path / "oauth")
    url = "https://gmailmcp.googleapis.com/mcp/v1"
    origin = "https://gmailmcp.googleapis.com"
    issuer = "https://accounts.fixture.test"
    scope = "https://www.googleapis.com/auth/gmail.metadata"
    broad = "https://www.googleapis.com/auth/gmail.modify"
    prm = origin + "/.well-known/oauth-protected-resource/mcp/v1"
    params = {}

    async def run():
        def transport(req):
            path = str(req.url)
            if path == url:
                if req.headers.get("authorization") == "Bearer synthetic-token":
                    return httpx2.Response(200)
                return httpx2.Response(401, headers={"WWW-Authenticate": f'Bearer resource_metadata="{prm}"'})
            if path == prm:
                return httpx2.Response(
                    200, json={"resource": url, "authorization_servers": [issuer], "scopes_supported": [broad, scope]}
                )
            if path == issuer + "/.well-known/oauth-authorization-server":
                return httpx2.Response(
                    200,
                    json={
                        "issuer": issuer,
                        "authorization_endpoint": issuer + "/authorize",
                        "token_endpoint": issuer + "/token",
                        "response_types_supported": ["code"],
                        "code_challenge_methods_supported": ["S256"],
                    },
                )
            if path == issuer + "/token":
                return httpx2.Response(
                    200, json={"access_token": "synthetic-token", "token_type": "Bearer", "expires_in": 3600}
                )
            return httpx2.Response(404)

        async def redirect(uri):
            params.update(parse_qs(urlsplit(uri).query))

        async def callback():
            return AuthorizationCodeResult(code="synthetic-code", state=params["state"][0])

        with preserve_requested_scope(url):
            auth = mcp2cli.build_oauth_provider(
                url,
                client_id="fixture-client",
                client_secret="fixture-secret",
                scope=scope,
                flow="authorization_code",
                manual_callback=True,
            )
            auth.context.redirect_handler = redirect
            auth.context.callback_handler = callback
            async with httpx2.AsyncClient(transport=httpx2.MockTransport(transport), auth=auth) as client:
                assert (await client.get(url)).status_code == 200
        assert params["scope"] == [scope] and params["code_challenge_method"] == ["S256"]

    asyncio.run(run())


@pytest.mark.parametrize(
    "mode, expected", [("anonymous", None), ("key", "Bearer chosen-key"), ("oauth", "Bearer scoped-token")]
)
def test_auth_mode_separates_key_oauth_and_anonymous(tmp_path, monkeypatch, mode, expected):
    import os

    import mcp2cli

    from clitw.mcp import config, runtime

    monkeypatch.setattr(os, "environ", os.environ.copy())
    url = "https://mcp.example/mcp"
    name = {"anonymous": "mcx-service-mcp", "key": "mcx-service-mcpk", "oauth": "mcx-service-mcpo"}[mode]
    row = {"name": "service", "url": url, "auth_mode": mode}
    monkeypatch.setattr(runtime, "read_config", lambda *_: ({}, {name: row}))
    monkeypatch.setattr(runtime, "presets", lambda: {name: {"url": url, "auth_env": "FIXTURE_SERVICE_KEY"}})
    monkeypatch.setenv("FIXTURE_SERVICE_KEY", "chosen-key")
    hits = []

    def cached(*args):
        hits.append(True)
        return "scoped-token"

    monkeypatch.setattr(runtime, "cached_bearer", cached)
    monkeypatch.setattr(runtime, "safe_transport", lambda *_: None)
    headers = {}

    def native(args, **kwargs):
        if "--auth-header" in args:
            key, _, var = args[args.index("--auth-header") + 1].split(":")
            headers[key] = os.environ[var]

    monkeypatch.setattr(mcp2cli, "_main_impl", native)
    pin = config.connection_pin("direct", url, "streamable")
    runtime.invoke(str(tmp_path), "catalog", name, pin, ["--list"])
    assert headers.get("Authorization") == expected and bool(hits) == (mode == "oauth")
    if mode != "oauth":
        with pytest.raises(ValueError, match="not configured for OAuth"):
            runtime.invoke(str(tmp_path), "catalog", name, pin, ["--oauth", "--list"])


def test_guide_resolves_old_names_and_reports_canonical(wrapper, capfd):
    bindings = [wrapper("a-mcpk", "auth", skip="API-key binding"), wrapper("b-mcp", "auth", skip="anonymous binding")]
    assert login.workflow(["tw-a", "mcx-b", "mcx-a-mcpk", "--list"], lambda: bindings) == 0
    out, _ = capfd.readouterr()
    assert out.splitlines() == ["mcx-a-mcpk: skipped (API-key binding)", "mcx-b-mcp: skipped (anonymous binding)"]
    assert login.workflow(["a", "--list"], lambda: [*bindings, wrapper("a-mcpo", "auth")]) == 2


def test_bundled_google_clients_share_saved_app_keys_but_distinct_scopes(tmp_path, monkeypatch, capfd):
    import logging
    import os

    import mcp2cli

    from clitw import credentials
    from clitw.mcp import config, runtime
    from clitw.paths import registry_dir

    monkeypatch.setattr(os, "environ", os.environ.copy())
    monkeypatch.setenv("CLITW_HOME", str(tmp_path))
    for key in ("GOOGLE_MCP_CLIENT_ID", "GOOGLE_MCP_CLIENT_SECRET", "CLITW_DOTENV"):
        monkeypatch.delenv(key, raising=False)
    assert mcp.cli_install(["mcx-gmail-mcpo", "mcx-google-drive-mcpo"]) == 0
    before = login.packaged_bindings()
    assert len(before) == 2 and all(not b.client_configured and "tw auth keys" in b.client_hint for b in before)
    credentials.save_key("GOOGLE_MCP_CLIENT_ID", "fixture-google-id")
    credentials.save_key("GOOGLE_MCP_CLIENT_SECRET", "fixture-google-secret")
    bindings = login.packaged_bindings()
    assert all(item.client_configured for item in bindings)
    registry = config.load(registry_dir() / "baked.json")

    def capture(args, **kwargs):
        assert args[args.index("--oauth-client-id") + 1] == "fixture-google-id"
        assert args[args.index("--oauth-client-secret") + 1] == "fixture-google-secret"
        scope = args[args.index("--oauth-scope") + 1]
        assert scope == "https://www.googleapis.com/auth/" + (
            "gmail.metadata" if "gmailmcp" in args[1] else "drive.metadata.readonly"
        )
        print("fixture-google-secret", file=sys.stderr)

    monkeypatch.setattr(mcp2cli, "_main_impl", capture)
    monkeypatch.setattr(logging, "disable", lambda *_: None)
    for item in bindings:
        runtime.invoke(
            str(tmp_path),
            "catalog",
            item.name,
            registry[item.name]["clitw_mcp"]["pin"],
            ["--oauth", "--oauth-flow", "authorization_code", "--refresh", "--list"],
        )
    out, err = capfd.readouterr()
    assert "fixture-google-secret" not in out + err and err.count("[redacted]") == 2
    assert "fixture-google-secret" not in json.dumps(registry)


def test_nonexpiring_native_grant_works_only_at_its_scoped_endpoint(tmp_path):
    from clitw.mcp.oauth import cached_bearer

    endpoint = "https://scope.example/mcp"
    item = login.Binding("mcx-scope-mcpo", (), endpoint, cache_dir=str(tmp_path))
    directory = tmp_path / "oauth" / hashlib.sha256(endpoint.encode()).hexdigest()[:16]
    directory.mkdir(parents=True)
    state = directory / "tokens.json"
    state.write_text(json.dumps({"token_type": "bearer", "access_token": "fixture"}))
    assert cached_bearer(endpoint, tmp_path) == "fixture" and login.saved_grant(item)
    assert cached_bearer(endpoint + "/other", tmp_path) is None
    state.write_text(json.dumps({"token_type": "bearer", "access_token": "fixture", "expires_in": 10}))
    assert cached_bearer(endpoint, tmp_path) is None and not login.saved_grant(item)


def test_only_exact_documented_query_flag_and_path_are_public():
    from clitw.mcp.config import PUBLIC_OAUTH_URLS

    assert PUBLIC_OAUTH_URLS == login.PUBLIC_OAUTH_URLS
    assert all(login.public_https(url) for url in PUBLIC_OAUTH_URLS)
    assert not login.public_https("https://huggingface.co/mcp?login=private")


def test_custom_key_reads_only_explicit_configured_credential(tmp_path, monkeypatch):
    import os

    import mcp2cli

    from clitw.mcp import config, runtime

    monkeypatch.setattr(os, "environ", os.environ.copy())
    monkeypatch.setenv("TEST_CUSTOM_MCP_KEY", "fixture-only")
    monkeypatch.setattr(runtime, "available_keys", lambda: pytest.fail("custom source read the public key store"))
    monkeypatch.setattr(runtime, "safe_transport", lambda *_: None)
    monkeypatch.setattr(runtime, "cached_bearer", lambda *_: pytest.fail("custom key read an OAuth grant"))
    url = "https://custom.example/mcp"
    source = tmp_path / "private.json"
    source.write_text(
        json.dumps(
            {"mcpServers": {"custom": {"url": url, "auth_env": "TEST_CUSTOM_MCP_KEY", "auth_style": "X-Fixture-Key"}}}
        )
    )

    def native(args, **kwargs):
        assert args[args.index("--mcp") + 1] == url
        header, _, variable = args[args.index("--auth-header") + 1].split(":")
        assert header == "X-Fixture-Key" and os.getenv(variable) == "fixture-only"
        assert "fixture-only" not in json.dumps(args)

    monkeypatch.setattr(mcp2cli, "_main_impl", native)
    runtime.invoke(
        str(tmp_path), str(source), "mcx-custom-mcpk", config.connection_pin("direct", url, "streamable"), ["--list"]
    )
