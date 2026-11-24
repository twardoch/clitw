# this_file: src/clitw/openapi/references.py
"""Resolve DocuSign's repeatedly shared schema branches without duplicating their expanded trees."""

from __future__ import annotations

from functools import cache

from mcp2cli import _json_pointer_lookup, _json_pointer_tokens


def resolve_shared(spec: dict) -> dict:
    """Match mcp2cli's cycle cutoffs and JSON pointers, sharing equal expanded reference results."""

    @cache
    def target(ref: str):
        try:
            return _json_pointer_lookup(spec, _json_pointer_tokens(ref[2:]))
        except LookupError:
            return None

    @cache
    def direct(ref: str) -> set[str]:
        pending, found = [target(ref)], set()
        while pending:
            node = pending.pop()
            if isinstance(node, dict):
                pointer = node.get("$ref")
                if isinstance(pointer, str) and pointer.startswith("#/"):
                    found.add(pointer)
                else:
                    pending.extend(node.values())
            elif isinstance(node, list):
                pending.extend(node)
        return found

    @cache
    def reachable(ref: str) -> frozenset[str]:
        pending, visited = [ref], set()
        while pending:
            item = pending.pop()
            if item not in visited:
                visited.add(item)
                pending.extend(direct(item) - visited)
        return frozenset(visited)

    @cache
    def expanded(ref: str, ancestors: frozenset[str]):
        return walk(target(ref), ancestors | {ref})

    def walk(node, ancestors: frozenset[str]):
        if isinstance(node, dict):
            ref = node.get("$ref")
            if isinstance(ref, str):
                if ref in ancestors or not ref.startswith("#/") or target(ref) is None:
                    return node
                # Only an ancestor reachable from this target could change a cycle cutoff.
                return expanded(ref, ancestors & reachable(ref))
            return {k: walk(value, ancestors) for k, value in node.items()}
        if isinstance(node, list):
            return [walk(value, ancestors) for value in node]
        return node

    return walk(spec, frozenset())
