"""``scrapper-tool`` subcommand dispatcher.

The CLI grew a second subcommand (``doctor``), and will grow a third
(``cookies``). This module owns the argument parser and the dispatch table so
that adding one doesn't mean editing another.

Why not extend ``canary.py``? Because ``scrapper_tool.canary`` is a documented
public module re-exported from the package ``__init__``, so anything living
there loads on every ``from scrapper_tool import canary`` — including the cookie
code, which has no business being imported by an SDK user probing fingerprint
health.

Command modules
---------------
Each subcommand is a module exposing two functions, so there is no central
argument table to drift out of sync with the handler:

``add_subparser(sub)``
    Register the subcommand and its flags on the shared subparsers action.

``run_cli(args, parser) -> int``
    Do the work and return the process exit code.

``parser`` is threaded into the handler rather than captured, because handlers
need ``parser.error(...)`` for argument validation that argparse can't express
declaratively — and ``parser.error`` is what makes those exit ``2`` instead of
raising.
"""

from __future__ import annotations

import argparse
from typing import TYPE_CHECKING, Any, Protocol

from scrapper_tool import _cookies_cli as _cookies_cmd
from scrapper_tool import canary as _canary_cmd
from scrapper_tool import doctor as _doctor_cmd

if TYPE_CHECKING:
    from collections.abc import Sequence

__all__ = ["main"]


class _Command(Protocol):
    """Structural contract every subcommand module satisfies."""

    def add_subparser(self, sub: Any) -> None: ...

    def run_cli(self, args: argparse.Namespace, parser: argparse.ArgumentParser) -> int: ...


#: Registration order is the order subcommands appear in ``--help``.
_COMMANDS: dict[str, _Command] = {
    "canary": _canary_cmd,
    "cookies": _cookies_cmd,
    "doctor": _doctor_cmd,
}


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="scrapper-tool",
        description="Reusable web-scraping toolkit — Pattern A/B/C/D ladder.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    for command in _COMMANDS.values():
        command.add_subparser(sub)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point for the ``scrapper-tool`` console script."""
    parser = _build_parser()
    args = parser.parse_args(argv)

    command = _COMMANDS.get(args.command)
    if command is None:  # pragma: no cover — argparse rejects unknown commands first
        parser.error(f"unknown command: {args.command}")
    return command.run_cli(args, parser)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
