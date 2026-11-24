# this_file: tests/test_package_rename.py
"""A new installation must reuse the previous registries without importing the old package."""

import hashlib
import json
import os
import shlex

import pytest

from clitwy.mcp import install as mcp
from clitwy.openapi import upgrade
from clitwy.openapi.state import atomic, wrapper
from clitwy.paths import home, provider_argv, provider_path, registry_dir


@pytest.mark.parametrize("lazy", [False, True])
@pytest.mark.parametrize("kind", ["api", "mcp"])
def test_package_rename_updates_owned_launcher_and_existing_receipt(tmp_path, monkeypatch, kind, lazy):
    monkeypatch.setenv("CLITW_HOME", str(tmp_path / "state"))
    name = f"mcx-fixture-{kind}"
    obsolete = str(tmp_path / "removed venv" / "python")
    path = provider_path(name)
    path.parent.mkdir(parents=True)
    registry_dir().mkdir()
    if kind == "mcp":
        config, pin = str(tmp_path / "user-config.json"), "pinned"
        old = mcp.wrapper(name, config, pin, executable=obsolete, package="clitw")
        expected = mcp.wrapper(name, config, pin)
        row = {
            "source_type": "mcp_stdio",
            "source": shlex.join([obsolete, "-m", "clitw.mcp.runtime", "--private", name]),
            "clitw_mcp": {"server": "fixture", "config": config, "auth_mode": "anonymous", "pin": pin},
            "wrapper_path": str(path),
        }
        meta = row["clitw_mcp"]
        state = registry_dir() / "baked.json"
        migrate = mcp.migrate_names
    else:
        old = wrapper(name, obsolete, package="clitw")
        expected = wrapper(name)
        body = b'{"paths":{}}'
        snapshot = home() / "openapi" / name / (hashlib.sha256(body).hexdigest() + ".json")
        atomic(snapshot, body)
        row = {"source": str(snapshot), "base_url": "https://fixture.example", "customized": True}
        meta = row
        state = registry_dir() / "openapi.json"
        migrate = upgrade.migrate_names
    meta["wrapper_sha256"] = hashlib.sha256(old).hexdigest()
    state.write_text(json.dumps({name: row}))
    atomic(path, old, 0o755)
    if lazy:
        assert provider_argv(name)[-1] == str(path)
    else:
        migrate([name])
    receipt = json.loads(state.read_bytes())[name]
    assert path.read_bytes() == expected
    assert receipt.get("clitw_mcp", receipt)["wrapper_sha256"] == hashlib.sha256(expected).hexdigest()
    if kind == "mcp":
        assert "clitwy.mcp.runtime" in receipt["source"] and obsolete not in receipt["source"]
    assert receipt.get("source") and (os.name == "nt" or os.access(path, os.X_OK))
    before = state.read_bytes()
    migrate([name])
    assert state.read_bytes() == before


def test_saved_header_supports_both_distribution_names():
    from clitwy.openapi.state import header_args

    for scheme in ("clitw", "clitwy"):
        header = f"User-Agent:{scheme}/1.0"
        assert header_args(header, {}) == ["--auth-header", header]


@pytest.mark.skipif(os.name == "nt", reason="POSIX custom shell launcher")
def test_run_preserves_unregistered_commands_and_reports_old_import_without_traceback(tmp_path, monkeypatch, capsys):
    from clitwy import run

    monkeypatch.setenv("CLITW_HOME", str(tmp_path))
    name = "mcx-user-mcp"
    path = provider_path(name)
    atomic(path, b"#!/bin/sh\n# managed by clitw mcp\nexit 19\n", 0o755)
    assert run(name) == 19
    body = mcp.wrapper(name, "config", "pin", package="clitw")
    atomic(path, body, 0o755)
    assert run(name) == 126
    assert "Cannot start" in capsys.readouterr().err
    assert path.read_bytes() == body
