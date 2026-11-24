# this_file: src/clitwy/mcp/sse.py
"""Select the MCP SDK event size for Higgsfield's large tools/list response."""

from collections.abc import Iterator
from contextlib import contextmanager
from functools import wraps

HIGGSFIELD_URL = "https://mcp.higgsfield.ai/mcp"
HIGGSFIELD_EVENT_BYTES = 8 * 1024 * 1024


@contextmanager
def higgsfield_schema_stream(url: str) -> Iterator[None]:
    """mcp2cli imports the SDK transport at call time without exposing its event-size option."""
    if url != HIGGSFIELD_URL:
        yield
        return
    from mcp.client import streamable_http

    original = streamable_http.streamable_http_client

    @wraps(original)
    def open_stream(url: str, **options):
        if url == HIGGSFIELD_URL:
            options.setdefault("max_sse_event_size", HIGGSFIELD_EVENT_BYTES)
        return original(url, **options)

    streamable_http.streamable_http_client = open_stream
    try:
        yield
    finally:
        streamable_http.streamable_http_client = original
