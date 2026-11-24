# this_file: src/clitwy/openapi/__init__.py
"""Install OpenAPI command wrappers and run read-only integration checks."""

from .catalogue import catalog

__all__ = ["catalog", "cli_install", "cli_try"]


def cli_install(argv: list[str]) -> int:
    from .install import main

    return main(argv)


def cli_try(argv: list[str]) -> int:
    from .probe import main

    return main(argv)
