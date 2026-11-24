# this_file: src/clitwy/__init__.py
"""Discover named integrations and run them with the same argument list as the CLI."""

from .api import catalog, run

__all__ = ["catalog", "run"]
