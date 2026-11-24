# this_file: src/clitw/names.py
"""Name a provider executable from its service, interface and configured authentication."""

import re
import unicodedata
from collections.abc import Collection


def provider_name(service: str, kind: str, auth: str = "") -> str:
    """Return an idempotent mcx name, retaining meaningful hyphens in the service slug."""
    if kind not in ("api", "mcp") or auth not in ("", "k", "o"):
        raise ValueError("invalid provider interface or authentication type")
    value = unicodedata.normalize("NFKD", service.strip()).encode("ascii", "ignore").decode()
    value = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1-\2", value)
    value = re.sub(r"([a-z0-9])([A-Z])", r"\1-\2", value)
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    slug = re.sub(r"^(?:mcx|tw)-", "", slug)
    slug = re.sub(rf"-{kind}[ko]?$", "", slug)
    if not slug:
        raise ValueError("empty service name")
    return f"mcx-{slug}-{kind}{auth}"


class UnknownProvider(ValueError):
    """The requested name has no matching provider."""


def resolve_provider(value: str, candidates: Collection[str], aliases: dict[str, str] | None = None) -> str:
    """Resolve an installed name, a historical alias, or one unambiguous service slug."""
    short = value.removeprefix("mcx-").removeprefix("tw-")
    if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", short):
        raise ValueError("Invalid provider name.")
    key = "mcx-" + short
    if key in candidates:
        return key
    if aliases and (alias := aliases.get(key)) in candidates:
        return alias
    matches = sorted(n for n in candidates if re.sub(r"-(?:api|mcp)[ko]?$", "", n) == key)
    if len(matches) == 1:
        return matches[0]
    if matches:
        raise ValueError(f"Choose a provider for {value}: {', '.join(matches)}")
    raise UnknownProvider(f"No installed or bundled integration named {key}.")
