"""``scrapper-tool canary`` CLI — fingerprint-health probe.

Walks the impersonation ladder against a target URL and reports which
profiles return 200 vs 403/blocked. Designed to run from cron / GitHub
Actions to surface "chrome146 is starting to 403" before any consumer
adapter notices.

Usage::

    scrapper-tool canary https://example.com/api/health
    scrapper-tool canary https://example.com/api/health --json
    scrapper-tool canary https://example.com/api/health \\
        --profiles chrome146,chrome142,safari260

Exit codes
----------

- ``0`` — at least one profile returned ≠ 403/503.
- ``1`` — all profiles blocked (informs the caller that Pattern D is
  needed, the URL is hostile, or the canary URL itself moved).
- ``2`` — argument parsing / runtime error (network down, bad URL).

Output (default, human-readable)::

    URL: https://example.com/api/health
    Effective profile: chrome146
    Profile  | Status | Time (ms)
    -------- | ------ | ---------
    chrome146 | 200  |   234
    chrome142  | -    |     -    (skipped — earlier profile won)
    safari260 | -    |     -
    firefox147 | -    |     -

Output (--json, machine-readable)::

    {
      "url": "https://example.com/api/health",
      "winning_profile": "chrome146",
      "exit_code": 0,
      "results": [
        {"profile": "chrome146", "status": 200, "elapsed_ms": 234, "skipped": false},
        {"profile": "chrome142", "status": null, "elapsed_ms": null, "skipped": true},
        ...
      ]
    }
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from typing import TYPE_CHECKING, Any, cast

from scrapper_tool._logging import get_logger
from scrapper_tool.errors import BlockedError, VendorHTTPError
from scrapper_tool.http import request_with_retry
from scrapper_tool.ladder import (
    IMPERSONATE_LADDER,
    _curl_cffi_session,
)

if TYPE_CHECKING:
    import argparse
    from collections.abc import Sequence

    import httpx

_logger = get_logger(__name__)


_ROTATE_STATUS_CODES = frozenset({403, 503})


async def probe_profile(
    profile: str,
    url: str,
    *,
    timeout: float = 10.0,  # noqa: ASYNC109 — passed to curl_cffi, not asyncio.timeout
    proxy: str | None = None,
) -> tuple[int | None, float | None, str | None]:
    """Issue one GET against ``url`` impersonating ``profile``.

    Returns ``(status_code, elapsed_ms, error_message)``. On transport
    error, status is ``None`` and ``error_message`` carries the
    failure reason; otherwise ``error_message`` is ``None``.
    """
    started = time.perf_counter()
    try:
        async with _curl_cffi_session(
            profile, timeout=timeout, proxy=proxy, extra_headers=None
        ) as session:
            resp = await request_with_retry(
                cast("httpx.AsyncClient", session),
                "GET",
                url,
            )
        elapsed_ms = (time.perf_counter() - started) * 1000.0
    except VendorHTTPError as exc:
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        return None, elapsed_ms, str(exc)
    return int(resp.status_code), elapsed_ms, None


async def run_canary(
    url: str,
    *,
    ladder: tuple[str, ...] = IMPERSONATE_LADDER,
    timeout: float = 10.0,  # noqa: ASYNC109 — passed through to curl_cffi
    proxy: str | None = None,
) -> dict[str, object]:
    """Walk ``ladder`` against ``url``. Stop at the first ≠ 403/503.

    Returns a structured result dict (see module docstring's --json
    example). Profiles tried *after* the winning one are recorded
    with ``skipped=True``.
    """
    if not ladder:
        msg = "ladder must contain at least one profile"
        raise ValueError(msg)

    results: list[dict[str, object]] = []
    winning_profile: str | None = None

    for i, profile in enumerate(ladder):
        if winning_profile is not None:
            results.append(
                {
                    "profile": profile,
                    "status": None,
                    "elapsed_ms": None,
                    "skipped": True,
                    "error": None,
                }
            )
            continue

        status, elapsed_ms, error = await probe_profile(profile, url, timeout=timeout, proxy=proxy)
        results.append(
            {
                "profile": profile,
                "status": status,
                "elapsed_ms": (round(elapsed_ms, 1) if elapsed_ms is not None else None),
                "skipped": False,
                "error": error,
            }
        )

        if status is not None and status not in _ROTATE_STATUS_CODES:
            winning_profile = profile
            # Mark remaining profiles as skipped.
            for skipped_profile in ladder[i + 1 :]:
                results.append(
                    {
                        "profile": skipped_profile,
                        "status": None,
                        "elapsed_ms": None,
                        "skipped": True,
                        "error": None,
                    }
                )
            break

    return {
        "url": url,
        "winning_profile": winning_profile,
        "exit_code": 0 if winning_profile is not None else 1,
        "results": results,
    }


def _format_text(report: dict[str, object]) -> str:
    """Render the canary report as a human-readable text table."""
    lines = [
        f"URL: {report['url']}",
        f"Effective profile: {report['winning_profile'] or '(none — all blocked)'}",
        "",
        "Profile     | Status | Time (ms) | Skipped",
        "----------- | ------ | --------- | -------",
    ]
    results = report["results"]
    assert isinstance(results, list)
    for row in results:
        assert isinstance(row, dict)
        status = row["status"]
        elapsed = row["elapsed_ms"]
        skipped = row["skipped"]
        status_cell = "-" if status is None else str(status)
        elapsed_cell = "-" if elapsed is None else f"{elapsed:.1f}"
        skipped_cell = "yes" if skipped else "no"
        profile_str = str(row["profile"])
        lines.append(f"{profile_str:<11} | {status_cell:<6} | {elapsed_cell:<9} | {skipped_cell}")
        error = row.get("error")
        if error:
            lines.append(f"            error: {error}")
    return "\n".join(lines)


def add_subparser(sub: Any) -> None:
    """Register the ``canary`` subcommand on ``scrapper-tool``'s dispatcher."""
    canary = sub.add_parser(
        "canary",
        help="Probe a URL through the impersonation ladder; report which profile won.",
        description=(
            "Walks the four-profile impersonation ladder "
            "(chrome146 -> chrome142 -> safari260 -> firefox147) against "
            "URL. Stops at the first non-403/503. Exit 0 on success, 1 if "
            "all profiles 403, 2 on error."
        ),
    )
    canary.add_argument("url", help="Target URL to probe.")
    canary.add_argument(
        "--profiles",
        type=str,
        default=None,
        help=(
            "Comma-separated impersonation profiles to walk in order. "
            "Default: the lib's IMPERSONATE_LADDER."
        ),
    )
    canary.add_argument(
        "--timeout",
        type=float,
        default=10.0,
        help="Per-request timeout in seconds. Default: 10.0",
    )
    canary.add_argument(
        "--proxy",
        type=str,
        default=None,
        help="Proxy URL to route requests through. Default: none.",
    )
    canary.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON instead of a text table.",
    )


def run_cli(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    """Handler invoked by the ``scrapper-tool`` dispatcher."""
    ladder: tuple[str, ...]
    if args.profiles:
        ladder = tuple(p.strip() for p in args.profiles.split(",") if p.strip())
        if not ladder:
            parser.error("--profiles must contain at least one non-empty entry")
    else:
        ladder = IMPERSONATE_LADDER

    try:
        report = asyncio.run(
            run_canary(
                args.url,
                ladder=ladder,
                timeout=args.timeout,
                proxy=args.proxy,
            )
        )
    except (BlockedError, ValueError) as exc:
        sys.stderr.write(f"canary error: {exc}\n")
        return 2

    if args.json:
        sys.stdout.write(json.dumps(report, indent=2))
        sys.stdout.write("\n")
    else:
        sys.stdout.write(_format_text(report))
        sys.stdout.write("\n")

    exit_code = report["exit_code"]
    assert isinstance(exit_code, int)
    return exit_code


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point for the ``scrapper-tool`` console script.

    Kept as a forwarding shim after the dispatcher moved to
    :mod:`scrapper_tool.cli`. Two reasons it stays: an editable install still
    has ``scrapper-tool`` pointing here until it is reinstalled, and this is a
    documented public entry point. The import is function-local because
    ``cli`` imports *this* module to register the subcommand.
    """
    from scrapper_tool.cli import main as _cli_main  # noqa: PLC0415

    return _cli_main(argv)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = [
    "add_subparser",
    "main",
    "probe_profile",
    "run_canary",
    "run_cli",
]
