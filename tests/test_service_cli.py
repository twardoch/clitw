# this_file: tests/test_service_cli.py
"""Exercise service listing, search filters and pricing without calling provider endpoints."""

import json

import pytest

from clitwy import service_cli


@pytest.fixture
def records(monkeypatch):
    rows = {
        "mcx-alfa-api": {
            "name": "mcx-alfa-api",
            "kind": "api",
            "auth": "anonymous",
            "info": "Public weather observations",
            "tags": ["Weather", "data"],
            "price": 0.02,
            "currency": "USD",
            "price_unit": "request",
            "price_model": "usage",
            "price_note": "Billed monthly",
            "price_as_of": "2026-01-01",
            "pricing_url": "https://alfa.example/prices",
            "url": "https://z.example",
            "aliases": ["mcx-legacy-weather"],
            "account_secret": "fixture-secret",
        },
        "mcx-beta-mcpo": {
            "name": "mcx-beta-mcpo",
            "kind": "mcp",
            "auth": "oauth",
            "info": "Straße map service",
            "tags": ["Geography", "weather"],
            "note": "Satellite imagery",
            "price_model": "subscription",
            "url": "https://a.example",
        },
        "mcx-gamma-mcp": {"name": "mcx-gamma-mcp", "kind": "mcp", "auth": "anonymous", "info": "Weather station"},
        "mcx-zeta-api": {
            "name": "mcx-zeta-api",
            "kind": "api",
            "auth": "anonymous",
            "info": "Weather station",
            "price": 0,
            "tags": ["Climatology"],
        },
    }
    monkeypatch.setattr(service_cli, "service_catalog", lambda path=None: rows)
    return rows


def test_info_summary_details_and_source_price_fields(records, capsys):
    records["mcx-alfa-api"]["info"] += "\n" + "Second sentence. " * 20
    assert service_cli.info([]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == len(records) and "mcx-alfa-api" in lines[0] and "0.02" in lines[0]
    assert "subscription" in lines[1] and "unknown" in lines[2].lower()
    assert len(lines[0].split("\t")[1]) <= 128 and "…" in lines[0]
    assert service_cli.info(["alfa", "beta"]) == 0
    out = capsys.readouterr().out
    for value in ("USD", "request", "2026-01-01", "Billed monthly", "https://alfa.example/prices", "subscription"):
        assert value in out
    assert "fixture-secret" not in out and "Weather station" not in out
    assert service_cli.info(["mcx-legacy-weather", "--json"]) == 0
    selected = json.loads(capsys.readouterr().out)
    assert list(selected) == ["mcx-alfa-api"]
    assert "account_secret" not in selected["mcx-alfa-api"] and selected["mcx-alfa-api"]["price"] == 0.02


def test_search_combines_casefolded_words_substring_and_exact_tag(records, capsys):
    args = ["strasse", "imagery", "--tag", "WEATHER", "--substring", "satell", "--json"]
    assert service_cli.search(args) == 0
    assert list(json.loads(capsys.readouterr().out)) == ["mcx-beta-mcpo"]
    assert service_cli.search(["--tag", "wea", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == {}
    assert service_cli.search(["--substring", "fixture-secret", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == {}


@pytest.mark.parametrize(
    ("field", "order"),
    [
        ("key", ["alfa", "beta", "gamma", "zeta"]),
        ("price", ["zeta", "alfa", "beta", "gamma"]),
        ("tag", ["zeta", "beta", "alfa", "gamma"]),
        ("url", ["beta", "alfa", "gamma", "zeta"]),
    ],
)
def test_search_sort_numeric_text_and_missing_last(field, order, records, capsys):
    assert service_cli.search(["--sort", field, "--json"]) == 0
    assert [name.split("-")[1] for name in json.loads(capsys.readouterr().out)] == order


def test_path_help_and_atomic_errors(tmp_path, records, monkeypatch, capsys):
    path = tmp_path / "services.yaml"
    monkeypatch.setattr(service_cli, "user_metadata_path", lambda: path)
    assert service_cli.info(["--data-path"]) == 0
    assert capsys.readouterr().out.strip() == str(path)
    assert service_cli.search(["--file", str(path), "--data-path"]) == 0
    assert capsys.readouterr().out.strip() == str(path)
    with pytest.raises(SystemExit) as err:
        service_cli.info(["alfa", "missing", "--json"])
    assert err.value.code == 2 and capsys.readouterr().out == ""
    with pytest.raises(SystemExit) as err:
        service_cli.search(["--file", str(path)])
    assert err.value.code == 2 and capsys.readouterr().out == ""
    with pytest.raises(SystemExit) as err:
        service_cli.search(["--tag", "  "])
    assert err.value.code == 2
    path.write_text("private fixture data")

    def broken(path):
        raise ValueError("fixture-secret from bad input")

    monkeypatch.setattr(service_cli, "service_catalog", broken)
    with pytest.raises(SystemExit) as err:
        service_cli.info(["--file", str(path)])
    result = capsys.readouterr()
    assert err.value.code == 2 and not result.out and "fixture-secret" not in result.err
