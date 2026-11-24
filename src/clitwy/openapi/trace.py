# this_file: src/clitwy/openapi/trace.py
"""Observe a single native HTTP transaction without persisting credentials, URLs or bodies."""

from __future__ import annotations

import json
import re
from pathlib import Path
from urllib.parse import urlsplit

import httpx


def observe(policy: dict) -> None:
    """Constrain every hop to the reviewed destination, including native redirects."""
    single, send = httpx.Client._send_single_request, httpx.Client.send
    target = urlsplit(policy["target"])
    parts = re.split(r"(\{[^{}]+\})", target.path)
    path_pattern = "".join("[^/]+" if p.startswith("{") and p.endswith("}") else re.escape(p) for p in parts)
    receipt = Path(policy["receipt"])

    def guarded(client, request, *args, **kwargs):
        actual = urlsplit(str(request.url))
        if (
            request.method != policy["method"]
            or (actual.scheme, actual.netloc) != (target.scheme, target.netloc)
            or not re.fullmatch(path_pattern, actual.path)
        ):
            raise RuntimeError("request outside reviewed OpenAPI probe")
        receipt.write_text('{"attempted":true}')
        try:
            return single(client, request, *args, **kwargs)
        except httpx.TransportError as exc:
            cause = exc
            seen = set()
            while id(cause) not in seen and (cause.__cause__ or cause.__context__):
                seen.add(id(cause))
                cause = cause.__cause__ or cause.__context__
            receipt.write_text(json.dumps({"attempted": True, "network_error": type(cause).__name__}))
            raise

    def record(client, request, *args, **kwargs):
        response = send(client, request, *args, **kwargs)
        media = response.headers.get("content-type", "").lower()
        kind = "html" if "text/html" in media else "api"
        if "json" in media and response.is_success:
            try:
                data = response.json()
                if isinstance(data, dict):
                    metadata = data.get("base_resp")
                    if (
                        data.get("success") is False
                        or data.get("ok") is False
                        or data.get("error")
                        or data.get("status") == "error"
                        or isinstance(metadata, dict)
                        and metadata.get("status_code", 0) not in (0, "0")
                    ):
                        kind = "error"
            except (ValueError, httpx.ResponseNotRead):
                pass
        receipt.write_text(json.dumps({"attempted": True, "http_status": response.status_code, "kind": kind}))
        return response

    httpx.Client._send_single_request = guarded
    httpx.Client.send = record
