# this_file: tests/test_service_info.py
"""Offline catalogue, provenance and private metadata mapping tests."""

import json
import os

import pytest
import yaml

from clitwy import service_info as si
from clitwy.mcp.config import presets
from clitwy.openapi.catalogue import providers


@pytest.fixture
def clean(tmp_path, monkeypatch):
    state = tmp_path / "state"
    folder = tmp_path / "executables"
    folder.mkdir()
    monkeypatch.setenv("PATH", str(folder))
    monkeypatch.setenv("CLITW_HOME", str(state))
    monkeypatch.setattr(si, "user_metadata_path", lambda: tmp_path / "profile" / "envinfo.yaml")
    return state, folder


def write_yaml(tmp_path, data):
    file = tmp_path / "meta.yaml"
    file.write_text(yaml.safe_dump(data), encoding="utf-8")
    return file


def test_clean_offline_catalog_uses_all_bindings_and_does_not_create_state(clean):
    rows = si.service_catalog()
    assert set(rows) == presets().keys() | providers().keys()
    assert all(row["origin"] == "bundled" and row["info"] for row in rows.values())
    assert rows["mcx-aimlapi-mcpk"]["auth"] == "key"
    assert not clean[0].exists() and not si.user_metadata_path().parent.exists()


def test_yaml_matches_named_commands_and_variables_without_executing(tmp_path, clean, monkeypatch):
    monkeypatch.setenv("GODADDY_API_KEY", "fixture-environment-value")
    marker = tmp_path / "would-run"
    values = {
        "OTHER_VAR": {
            "cli": f"mcx-pika; touch {marker}; mcx-absent",
            "note": "matched through historical CLI",
            "key": "private-key-field",
            "hidden": "extra-private-field",
        },
        "GODADDY_API_KEY": {"tags": ["DNS", "DNS"], "note": "matched through variable"},
        "mcx-pika-mcpo": {
            "tags": ["personal"],
            "info": "my integration",
            "price": 0,
            "price_unit": "per test",
            "pricing_url": "https://vendor.example/pricing?key=unsafe",
        },
    }
    rows = si.service_catalog(write_yaml(tmp_path, values))
    pika = si.resolve_service("tw-pika", rows)
    assert (pika["price"], pika["price_unit"]) == (0, "per test")
    assert pika["tags"] == ["personal"] and pika["info"] == "my integration" and pika["note"].startswith("matched")
    assert pika["sources"]["price"] == pika["origin"] == "user" and pika["sources"]["url"] == "bundled"
    assert "pricing_url" not in pika and not marker.exists()
    assert rows["mcx-godaddy-apik"]["note"] == "matched through variable"
    assert rows["mcx-godaddy-apik"]["tags"] == ["DNS"]
    content = json.dumps(rows)
    assert not any(
        s in content
        for s in ("private-key-field", "extra-private-field", "fixture-environment-value", "key=unsafe", "would-run")
    )


def test_price_reset_invalid_values_and_date_provenance(tmp_path, clean, monkeypatch):
    packages = tmp_path / "package"
    packages.mkdir()
    key = "mcx-pika-mcpo"
    (packages / "services.yaml").write_text(
        yaml.safe_dump(
            {
                key: {
                    "price": 1.25,
                    "price_unit": "per request",
                    "currency": "USD",
                    "price_as_of": "2026-11-24",
                    "price_note": "public plan",
                    "pricing_url": "https://provider.example/pricing",
                }
            }
        )
    )
    monkeypatch.setattr(si, "files", lambda package: packages)
    for invalid in (-1, float("nan"), float("inf"), True, "not numeric", None, "-"):
        file = write_yaml(tmp_path, {key: {"price": invalid, "note": "personal"}})
        rec = si.service_catalog(file)[key]
        assert rec["price"] == 1.25 and rec["sources"]["price"] == "bundled" and rec["sources"]["note"] == "user"
    rec = si.service_catalog(write_yaml(tmp_path, {key: {"price": 0, "price_as_of": "2025-06-19"}}))[key]
    assert rec["price"] == 0 and rec["price_as_of"] == "2025-06-19"
    assert not ({"price_note", "currency", "price_unit", "pricing_url"} & rec.keys())


def test_installed_path_registry_alias_and_future_records(tmp_path, clean):
    state, folder = clean
    registry = state / "registry"
    registry.mkdir(parents=True)
    key = "mcx-foreign-mcp"
    (registry / "baked.json").write_text(
        json.dumps(
            {
                key: {
                    "source_type": "mcp_stdio",
                    "source": "private-shell-command",
                    "description": "Local bridge",
                    "clitw_mcp": {"previous_name": "mcx-old-foreign", "config": "private-file"},
                }
            }
        )
    )
    standalone = folder / ("mcx-foreign-api.cmd" if os.name == "nt" else "mcx-foreign-api")
    standalone.write_text("never run")
    standalone.chmod(0o755)
    promised = "mcx-prototype-api"
    file = write_yaml(tmp_path, {promised: {"info": "Preview"}, "GHOST_API_KEY": {"info": "no such executable"}})
    rows = si.service_catalog(file)
    assert si.resolve_service("old-foreign", rows)["info"] == "Local bridge"
    assert rows["mcx-foreign-api"]["availability"] == rows[key]["availability"] == "installed"
    assert rows[promised]["availability"] == "user-defined"
    with pytest.raises(ValueError, match="mcx-foreign-api, mcx-foreign-mcp"):
        si.resolve_service("foreign", rows)
    with pytest.raises(ValueError):
        si.resolve_service("ghost", rows)
    public = si.service_catalog(file, include_user=False, include_installed=False)
    assert not ({key, promised, "mcx-foreign-api"} & public.keys())
    assert "private-shell-command" not in json.dumps(rows) and "private-file" not in json.dumps(rows)


def test_missing_and_invalid_user_files_only_report_generic_errors(tmp_path, clean):
    assert si.service_catalog()
    with pytest.raises(ValueError, match="does not exist"):
        si.service_catalog(tmp_path / "absent")
    for text in ("!unsafe secret", "key: [private-fragment", "- private-fragment", "field: 2025-19-31"):
        file = tmp_path / "bad.yaml"
        file.write_text(text)
        with pytest.raises(ValueError, match="YAML mapping") as exc:
            si.service_catalog(file)
        assert "private-fragment" not in str(exc.value) and "!unsafe" not in str(exc.value)


def test_platform_directory_uses_reverse_dns_application_id(monkeypatch, tmp_path):
    def location(application, appauthor):
        assert application == "com.twardoch.clitw" and appauthor is False
        return tmp_path

    monkeypatch.setattr(si, "user_data_path", location)
    assert si.user_metadata_path() == tmp_path / "data" / "envinfo.yaml"


def test_customized_registered_endpoint_does_not_inherit_public_price_or_url(tmp_path, clean):
    state, _ = clean
    registry = state / "registry"
    registry.mkdir(parents=True)
    name = "mcx-anthropic-apik"
    (registry / "openapi.json").write_text(
        json.dumps(
            {
                name: {
                    "base_url": "https://custom.example/unrecognizable-secret-in-path/v1",
                    "source": "/private/snapshot.json",
                    "auth": "X-Key:env:PRIVATE_FIXTURE_KEY",
                    "description": "Different hosted service",
                    "customized": True,
                    "preset": False,
                }
            }
        )
    )
    rec = si.service_catalog(write_yaml(tmp_path, {"PRIVATE_FIXTURE_KEY": {"note": "configured"}}))[name]
    assert "url" not in rec and rec["info"] == "Different hosted service"
    assert rec["sources"]["info"] == "installed" and rec["note"] == "configured"
    assert "unrecognizable-secret-in-path" not in json.dumps(rec)
    assert "openapi" not in rec and "price" not in rec and "pricing_url" not in rec


@pytest.mark.parametrize(
    "segment",
    [
        "fc-EXAMPLEONLY1234567890",
        "fc%252DEXAMPLEONLY1234567890",
        "shared%2ftoken%2fprivate",
        "bF9yVkx4lA1zTjRsQk3pHm7d",
        "sk-testonlyXXXXXXXXXXXXXXXX",
    ],
)
def test_private_urls_with_credential_paths_are_excluded(tmp_path, clean, segment):
    unsafe = "https://mcp.example/" + segment + "/mcp"
    name = "mcx-secure-example-api"
    rec = si.service_catalog(
        write_yaml(
            tmp_path,
            {
                name: {
                    "note": "My public note",
                    **{field: unsafe for field in ("url", "openapi", "docs", "pricing_url")},
                }
            },
        )
    )[name]
    assert rec["note"] == "My public note"
    assert not ({"url", "openapi", "docs", "pricing_url"} & rec.keys())
    assert segment not in json.dumps(rec)


def test_legacy_yaml_can_reference_custom_command_declared_later(tmp_path, clean):
    file = tmp_path / "meta.yaml"
    file.write_text("EXAMPLE_VAR:\n  cli: mcx-upcoming-api\n  note: matched\nmcx-upcoming-api:\n  info: New CLI\n")
    rec = si.service_catalog(file)["mcx-upcoming-api"]
    assert rec["info"] == "New CLI" and rec["note"] == "matched"
