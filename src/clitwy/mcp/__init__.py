# this_file: src/clitwy/mcp/__init__.py
"""Discover, install and exercise MCP server command-line clients."""

from .config import catalog
from .install import cli_install
from .probe import cli_try

__all__ = ["catalog", "cli_install", "cli_try"]
