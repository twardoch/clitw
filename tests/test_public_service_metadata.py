# this_file: tests/test_public_service_metadata.py
"""Verify the packaged public inventory independently of credentials or user overrides."""

from __future__ import annotations

import json
import math
import re
from datetime import date
from importlib.resources import files
from urllib.parse import urlsplit

import yaml

from clitw.mcp.config import presets
from clitw.openapi.catalogue import providers


def _public_rows() -> dict:
    body = files("clitw").joinpath("services.yaml").read_text(encoding="utf-8")
    # safe_load alone accepts duplicate keys and silently discards the previous row.
    object_node = yaml.compose(body)
    keys = [key.value for key, _ in object_node.value]
    assert len(keys) == len(set(keys))
    return yaml.safe_load(body)


def test_public_metadata_covers_each_canonical_binding_and_uses_only_public_fields():
    entries = _public_rows()
    assert set(entries) == providers().keys() | presets().keys()
    public_fields = {
        "info",
        "tags",
        "url",
        "pricing_url",
        "price",
        "currency",
        "price_unit",
        "price_as_of",
        "price_note",
        "price_model",
    }
    for name, record in entries.items():
        assert record.keys() <= public_fields, name
        tags = record["tags"]
        assert tags and len(tags) == len(set(tags)), name
        assert all(re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", tag) for tag in tags), name
        for key in ("url", "pricing_url"):
            if key in record:
                url = urlsplit(record[key])
                assert url.scheme == "https" and url.hostname, name
                assert not (url.username or url.password or url.query or url.fragment), name


def test_each_public_amount_has_a_date_unit_currency_first_party_source_and_scope():
    entries = _public_rows()
    for name, record in entries.items():
        if "price_note" in record or "price_model" in record:
            assert record.get("pricing_url"), name
        if "price" in record:
            amount = record["price"]
            assert type(amount) in (int, float) and math.isfinite(amount) and amount >= 0, name
            assert date.fromisoformat(record["price_as_of"]), name
            assert re.fullmatch(r"[A-Z]{3}", record["currency"]), name
            assert record["price_unit"] and record["currency"] not in record["price_unit"], name
            assert record["price_note"] and record["pricing_url"], name
    public_search = entries["mcx-deepwiki-github-search-mcp"]
    assert public_search["price"] == 0 and "public" in public_search["price_note"].lower()
    assert entries["mcx-tavily-apik"]["price"] == entries["mcx-tavily-search-mcpk"]["price"]
    assert entries["mcx-upstash-qstash-apik"]["price_unit"] != entries["mcx-upstash-redis-api"]["price_unit"]


def test_research_tag_finds_bundled_services_in_a_clean_profile(tmp_path, monkeypatch, capsys):
    from clitw import service_cli, service_info

    monkeypatch.setenv("CLITW_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(service_info, "user_metadata_path", lambda: tmp_path / "envinfo.yaml")
    assert service_cli.search(["--tag", "research", "--json"]) == 0
    assert set(json.loads(capsys.readouterr().out)) == {
        "mcx-exa-apik",
        "mcx-exa-search-mcpk",
        "mcx-linkup-apik",
        "mcx-parallel-apik",
        "mcx-parallel-search-mcpk",
        "mcx-parallel-task-mcpk",
        "mcx-perplexity-apik",
        "mcx-tavily-apik",
        "mcx-tavily-search-mcpk",
        "mcx-ydc-apik",
        "mcx-youcom-search-mcpk",
    }
