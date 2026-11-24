# this_file: src/clitwy/service_cli.py
"""Show and search service metadata without invoking a provider."""

import argparse
import json
from pathlib import Path
from textwrap import shorten

from yaml import YAMLError

from .service_info import resolve_service, service_catalog, user_metadata_path

LABELS = {
    "kind": "Interface",
    "availability": "Availability",
    "aliases": "Aliases",
    "auth": "Authentication",
    "info": "Description",
    "note": "Note",
    "tags": "Tags",
    "url": "Service URL",
    "openapi": "OpenAPI",
    "docs": "Documentation",
    "price_note": "Pricing details",
    "price_as_of": "Price as of",
    "pricing_url": "Pricing URL",
    "origin": "Record source",
}
PUBLIC_FIELDS = {*LABELS, "name", "price", "currency", "price_unit", "price_model", "sources"}
SEARCH_TEXT = (
    "name",
    "info",
    "url",
    "note",
    "kind",
    "auth",
    "docs",
    "openapi",
    "price_model",
    "price_note",
    "price_unit",
    "currency",
    "availability",
    "pricing_url",
)


def filter_text(value: str) -> str:
    if not value.strip():
        raise argparse.ArgumentTypeError("the filter must contain text")
    return value.strip().casefold()


def parser_for(command: str, description: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=f"tw {command}", description=description, allow_abbrev=False)
    parser.add_argument("--file", type=Path, metavar="FILE", help="select a local service metadata overlay")
    parser.add_argument("--data-path", action="store_true", help="print the selected metadata file path and exit")
    parser.add_argument("--json", action="store_true", help="write a JSON object keyed by provider name")
    return parser


def read_catalogue(args: argparse.Namespace, parser: argparse.ArgumentParser) -> dict | None:
    path = args.file.expanduser().resolve() if args.file is not None else None
    if args.data_path:
        print(path or user_metadata_path())
        return None
    if path is not None and not path.is_file():
        parser.error("the selected metadata file does not exist or is not a regular file")
    try:
        return service_catalog(path)
    except (ValueError, TypeError, OSError, YAMLError):
        # A YAML exception can embed the offending line from a private inventory.
        parser.error("cannot read service metadata; check the file format and permissions")


def pricing(row: dict) -> str:
    amount = row.get("price")
    model = row.get("price_model")
    unit = row.get("price_unit")
    if amount is None:
        if model and not unit:
            return model if model.casefold() == "free" else f"{model} (amount unknown)"
        price = f"price unknown ({unit})" if unit else "price unknown"
    else:
        price = " ".join(str(part) for part in (amount, row.get("currency")) if part is not None)
        price += f" / {unit}" if unit else " (unit unspecified)"
    return f"{price} · {model}" if model else price


def render(rows: list[dict], *, as_json: bool, detail: bool = False, sort_field: str = "key") -> None:
    if as_json:
        print(
            json.dumps(
                {r["name"]: {k: v for k, v in r.items() if k in PUBLIC_FIELDS} for r in rows},
                indent=2,
                ensure_ascii=False,
            )
        )
        return
    for i, row in enumerate(rows):
        if not detail:
            brief = shorten(row.get("info") or "Description unavailable", width=128, placeholder="…")
            columns = [row["name"], brief, pricing(row)]
            if sort_field == "tag":
                columns.append(", ".join(row.get("tags", [])))
            if sort_field == "url":
                columns.append(row.get("url", ""))
            print("\t".join(columns).rstrip())
            continue
        if i:
            print()
        print(row["name"])
        print(f"  Price\t{pricing(row)}")
        for key, label in LABELS.items():
            if (value := row.get(key)) not in ("", None, []):
                text = ", ".join(value) if isinstance(value, list) else value
                print(f"  {label}\t{text}")
        if row.get("sources"):
            fields = (f"{k}={v}" for k, v in sorted(row["sources"].items()) if k in PUBLIC_FIELDS)
            print(f"  Field sources\t{', '.join(fields)}")


def info(argv: list[str]) -> int:
    parser = parser_for("info", "List all services or show details for named providers.")
    parser.add_argument(
        "names", nargs="*", metavar="NAME", help="canonical name, historical name or unique service name"
    )
    args = parser.parse_intermixed_args(argv)
    catalogue = read_catalogue(args, parser)
    if catalogue is None:
        return 0
    if args.names:
        try:
            selected = [resolve_service(name, catalogue) for name in args.names]
        except ValueError as exc:
            parser.error(str(exc))
        rows = list({row["name"]: row for row in selected}.values())
    else:
        rows = [catalogue[name] for name in sorted(catalogue, key=str.casefold)]
    render(rows, as_json=args.json, detail=bool(args.names))
    return 0


def matches(row: dict, tag: str | None, terms: list[str]) -> bool:
    tags = [t.casefold() for t in row.get("tags", [])]
    if tag and tag not in tags:
        return False
    values = [v.casefold() for key in SEARCH_TEXT if isinstance(v := row.get(key), str)]
    values.extend(tags)
    values.extend(alias.casefold() for alias in row.get("aliases", []))
    return all(any(term in value for value in values) for term in terms)


def sort_value(row: dict, field: str) -> tuple:
    name = row["name"].casefold()
    if field == "key":
        return (name,)
    if field == "price":
        value = row.get("price")
        return value is None, value if value is not None else 0, name
    value = next(iter(row.get("tags", [])), "") if field == "tag" else row.get("url", "")
    return not bool(value), value.casefold(), name


def search(argv: list[str]) -> int:
    parser = parser_for("search", "Find providers by words, tag or substring. All supplied filters must match.")
    parser.add_argument("words", nargs="*", type=filter_text, metavar="WORD", help="case-insensitive search terms")
    parser.add_argument("--tag", type=filter_text, help="exact tag, ignoring case")
    parser.add_argument(
        "--substring", type=filter_text, help="text occurring in a service name, tag or description field"
    )
    parser.add_argument(
        "--sort",
        choices=("key", "tag", "url", "price"),
        default="key",
        help="sort ascending by name, first tag, URL or numeric price; missing values last",
    )
    args = parser.parse_intermixed_args(argv)
    catalogue = read_catalogue(args, parser)
    if catalogue is None:
        return 0
    terms = args.words + ([args.substring] if args.substring else [])
    rows = [r for r in catalogue.values() if matches(r, args.tag, terms)]
    render(sorted(rows, key=lambda r: sort_value(r, args.sort)), as_json=args.json, sort_field=args.sort)
    return 0
