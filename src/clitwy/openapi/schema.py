# this_file: src/clitwy/openapi/schema.py
"""Fetch and normalize immutable OpenAPI snapshots for the native mcp2cli parser."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
from http.client import HTTPException
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import unquote, urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, build_opener

import yaml

from .catalogue import Tool
from .state import NO_BASE, atomic, scoped_environment, valid_url

METHODS = {"get", "post", "put", "delete", "patch"}
LIMIT = 64_000_000


class UnusableSchema(ValueError):
    """A provider schema could not supply any verified native commands."""


def run(args: list[str], timeout: int = 60) -> bytes:
    with tempfile.TemporaryDirectory(prefix="clitwy-preflight-") as work:
        result = subprocess.run(
            args,
            env=scoped_environment(private=Path(work)),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            check=False,
            timeout=timeout,
        )
    if result.returncode:
        raise ValueError(f"{Path(args[0]).name} exited {result.returncode} preparing the API schema")
    return result.stdout


class _PublicRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not valid_url(newurl) or (urlsplit(req.full_url).scheme == "https" and urlsplit(newurl).scheme != "https"):
            raise ValueError("unsafe public schema redirect")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def load_source(source: str) -> bytes:
    if source == "package:wayback.json":
        path = Path(__file__).with_name("wayback.json")
    elif valid_url(source):
        from urllib.request import Request

        req = Request(source, headers={"User-Agent": "clitwy/1.0 (OpenAPI schema)"})
        with build_opener(_PublicRedirect()).open(req, timeout=35) as response:
            body = response.read(LIMIT + 1)
        if len(body) > LIMIT:
            raise ValueError("OpenAPI document exceeds 64 MB")
        return body
    elif not urlsplit(source).scheme or Path(source).is_absolute():
        path = Path(source).expanduser()
    else:
        raise ValueError("spec must be a file, HTTPS URL, or loopback URL")
    if not path.is_file() or path.stat().st_size > LIMIT:
        raise ValueError("OpenAPI document is missing or exceeds 64 MB")
    return path.read_bytes()


def reference_target(node: dict, spec: dict) -> dict:
    """Look through local references without expanding recursive model graphs."""
    seen = set()
    while (
        isinstance(node, dict) and isinstance(ref := node.get("$ref"), str) and ref.startswith("#/") and ref not in seen
    ):
        seen.add(ref)
        node = spec
        for part in unquote(ref)[2:].split("/"):
            token = part.replace("~1", "/").replace("~0", "~")
            if isinstance(node, list) and token.isdecimal() and int(token) < len(node):
                node = node[int(token)]
            else:
                node = node.get(token, {}) if isinstance(node, dict) else {}
    return node if isinstance(node, dict) else {}


def is_bound_header(param: dict, spec: dict, names: set[str]) -> bool:
    param = reference_target(param, spec)
    return param.get("in") == "header" and str(param.get("name", "")).lower() in names


def convert_swagger(spec: dict) -> dict:
    """Use the standard converter for body, formData, parameter and schema references."""
    converter = shutil.which("swagger2openapi")
    command = (
        [converter]
        if converter
        else ["npm", "exec", "--yes", "--package=swagger2openapi@7.0.8", "--", "swagger2openapi"]
    )
    with tempfile.TemporaryDirectory() as tmp:
        source = Path(tmp) / "swagger.json"
        source.write_text(json.dumps(spec, default=str), encoding="utf-8")
        return json.loads(run([*command, "--patch", str(source)], timeout=90))


def composed_properties(schema: dict, spec: dict) -> dict:
    """Find unambiguous object fields in unions so mcp2cli also exposes its raw JSON options."""
    found, inconsistent, visited = {}, set(), set()
    pending = [schema]
    while pending:
        branch = reference_target(pending.pop(), spec)
        if id(branch) in visited:
            continue
        visited.add(id(branch))
        for name, field in branch.get("properties", {}).items():
            if name in found and found[name] != field:
                inconsistent.add(name)
            found[name] = field
        for keyword in ("allOf", "oneOf", "anyOf"):
            pending.extend(branch.get(keyword, []))
    return {name: field for name, field in found.items() if name not in inconsistent}


def prepare_body(operation: dict, method: str, spec: dict) -> bool:
    """Keep only bodies mcp2cli can actually send using the published content type."""
    if "requestBody" not in operation:
        return True
    body = reference_target(operation["requestBody"], spec)
    if method == "get":
        return False
    # Without an actual file, httpx serializes multipart text fields as urlencoded.
    for media in ("multipart/form-data", "application/json"):
        content = body.get("content", {}).get(media)
        if not isinstance(content, dict):
            continue
        schema = reference_target(content.get("schema", {}), spec)
        fields = schema.get("properties") or composed_properties(schema, spec)
        if not fields:
            continue
        if media == "multipart/form-data" and not any(
            reference_target(field, spec).get("format") == "binary" for field in fields.values()
        ):
            continue
        if not schema.get("properties"):
            content = {**content, "schema": {**schema, "properties": fields}}
        if content is not body["content"][media] or len(body["content"]) != 1:
            operation["requestBody"] = {**body, "content": {media: content}}
        return True
    return False


def normalize_literal_ref_properties(spec: dict) -> None:
    """mcp2cli treats a literal JSON member named $ref as a reference when walking properties."""
    stack = [spec]
    pattern = r"^\$ref$"
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            properties = node.get("properties")
            if isinstance(properties, dict) and isinstance(properties.get("$ref"), (bool, dict)):
                constraint = properties.pop("$ref")
                patterns = node.setdefault("patternProperties", {})
                patterns[pattern] = {"allOf": [patterns[pattern], constraint]} if pattern in patterns else constraint
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)


def add_documented_reads(tool: Tool, spec: dict) -> None:
    """Fill omissions in upstream specs using vendor-documented GETs on the reviewed API base."""
    catalog = Path(__file__).with_name("reads.tsv")
    for line in catalog.read_text().splitlines():
        if not line or line.startswith("#"):
            continue
        name, base, path, command, summary, docs = line.split("\t")
        if (name, base) != (tool.name, tool.base.rstrip("/")):
            continue
        item = spec["paths"].setdefault(path, {})
        item.setdefault(
            "get",
            {
                "operationId": command,
                "summary": summary,
                "externalDocs": {"url": docs},
                "parameters": [
                    {"name": name, "in": "path", "required": True, "schema": {"type": "string"}}
                    for name in re.findall(r"{(\w+)}", path)
                ],
                "responses": {"200": {"description": summary}},
            },
        )


def normalized(tool: Tool, body: bytes) -> tuple[bytes, int, int]:
    """Parse and validate the schema before staging or publishing any provider state."""
    try:
        spec = json.loads(body)
    except (ValueError, UnicodeError):
        try:
            spec = yaml.load(body, Loader=getattr(yaml, "CSafeLoader", yaml.SafeLoader))
            json.dumps(spec, default=str)  # Reject recursive YAML aliases before walking schema objects.
        except (yaml.YAMLError, ValueError, RecursionError, TypeError) as exc:
            raise ValueError("invalid OpenAPI YAML") from exc
    if (
        not isinstance(spec, dict)
        or not (
            re.fullmatch(r"3\.\d+\.\d+(?:[-+][\w.-]+)?", str(spec.get("openapi", ""))) or spec.get("swagger") == "2.0"
        )
        or not isinstance(spec.get("paths"), dict)
    ):
        raise ValueError("download is not an OpenAPI document")
    # Reject external pointers before passing a user-supplied Swagger document to the converter.
    if spec.get("swagger") == "2.0":
        raw = json.dumps(spec, default=str, separators=(",", ":")).encode()
        if re.search(rb'"\$ref":"(?!#)[^"]+', raw):
            raise ValueError("document uses unbundled external references")
        spec = convert_swagger(spec)
    if not any(isinstance(item, dict) and METHODS.intersection(item) for item in spec["paths"].values()):
        raise ValueError("schema contains no supported operations")
    add_documented_reads(tool, spec)
    removed = 0
    unsupported = 0
    bound = {h.partition(":")[0].lower() for h in tool.auth.split(",")}
    for path, item in spec["paths"].items():
        if not path.startswith("/") or path.startswith("//") or not isinstance(item, dict):
            raise ValueError("invalid or non-local OpenAPI path")
        for holder in (item, *(item[m] for m in METHODS.intersection(item))):
            holder.pop("x-fern-examples", None)  # Documentation snippets can reference remote example files.
            if "parameters" in holder:
                holder["parameters"] = [p for p in holder["parameters"] if not is_bound_header(p, spec, bound)]
        for method in METHODS.intersection(item):
            servers = item[method].get("servers", item.get("servers", []))
            # mcp2cli applies one base URL to every operation, even when the spec overrides it.
            if servers and not any(
                urljoin(tool.source, s["url"]).rstrip("/") == tool.base.rstrip("/") for s in servers
            ):
                del item[method]
                removed += 1
            elif not prepare_body(item[method], method, spec):
                del item[method]
                unsupported += 1
    normalize_literal_ref_properties(spec)
    body = json.dumps(spec, default=str, separators=(",", ":")).encode()
    if re.search(rb'"\$ref":"(?!#)[^"]+', body):
        raise ValueError("document uses unbundled external references")
    if not any(METHODS.intersection(item) for item in spec["paths"].values()):
        raise ValueError("schema contains no supported operations for this API base")
    return body, removed, unsupported


def prepare(tool: Tool, cache: Path) -> tuple[Path, int, int, int]:
    try:
        data = load_source(tool.source)
    except HTTPError as exc:
        raise UnusableSchema(f"schema download returned HTTP {exc.code}") from exc
    except (OSError, HTTPException) as exc:
        raise UnusableSchema(f"schema source unavailable ({type(exc).__name__})") from exc
    except ValueError as exc:
        raise UnusableSchema(str(exc)) from exc
    try:
        body, removed, unsupported = normalized(tool, data)
    except ValueError as exc:
        raise UnusableSchema(str(exc)) from exc
    except (AttributeError, KeyError, TypeError, RecursionError, OverflowError) as exc:
        raise UnusableSchema("malformed OpenAPI operations or schema definitions") from exc
    folder = cache / tool.name
    folder.mkdir(parents=True, exist_ok=True)
    snapshot = folder / (hashlib.sha256(body).hexdigest() + ".json")
    if snapshot.is_symlink() or (snapshot.exists() and snapshot.read_bytes() != body):
        raise ValueError("existing snapshot failed content verification")
    if not snapshot.exists():
        atomic(snapshot, body)
    args = [sys.executable, "-m", "clitwy.openapi.native", "--spec", str(snapshot), "--base-url", tool.base or NO_BASE]
    try:
        commands = json.loads(run([*args, "--list", "--json"], timeout=55))
        if not isinstance(commands, list) or not commands:
            raise ValueError("native client found no supported operations")
        run([*args, commands[0]["name"], "--help"], timeout=55)
    except (ValueError, TypeError, KeyError, subprocess.TimeoutExpired) as exc:
        raise UnusableSchema("native client could not discover usable operations") from exc
    return snapshot, len(commands), removed, unsupported
