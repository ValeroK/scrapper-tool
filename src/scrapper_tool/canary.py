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

- ``0`` — ``ok`` (a profile reached the target) or ``unreachable`` (nothing
  answered, so there is nothing to say about our fingerprints).
- ``1`` — ``blocked``: every profile that got an answer was refused. Nothing
  gets through, and this is the event worth waking up for.

``degraded`` (some profiles refused, at least one still winning) exits ``0``. It
is the early warning this tool was written to give, and it is carried in
``verdict`` -- but the ladder is doing exactly its job, so it is information
rather than an alarm.
- ``2`` — argument parsing / runtime error.

The ``verdict`` field carries the distinction. It matters because 503 means both
"Cloudflare is challenging you" and "this origin is overloaded", and a canary
that cannot separate them reports an outage as a fingerprinting event.

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


#: Statuses that mean "this profile was refused". 503 sits here because
#: Cloudflare serves challenges with it and, from a body we never got, a
#: challenge and an overloaded origin are indistinguishable. That conflation is
#: correct inside the ladder -- rotating costs one request -- and wrong in a
#: canary, which exists to answer *why*.


def _verdict(results: list[dict[str, object]]) -> tuple[str, str]:
    """``(verdict, detail)`` for a completed walk.

    The distinction this draws is the whole point of the canary and it was
    missing. Over twelve scheduled runs it failed five times, every one of them
    because ``httpbin.org`` -- a free public echo service -- was overloaded, and
    every one of them filed an issue advising that an impersonation profile had
    probably been fingerprinted. It had not. Nothing was fingerprinted, because
    nothing got a response.

    The signals were both present and simply collapsed together:

    * **unreachable** -- every profile timed out or errored at the transport
      layer. Nothing reached the target, so nothing can have judged us. This is
      the target's problem, or the network's.
    * **blocked** -- profiles got *answers*, and every answer was a refusal.
      Something on the other end looked at us and said no. This is the event the
      canary exists to catch.
    * **degraded** -- some profiles won and some were refused, which is what
      fingerprinting looks like before it becomes total.
    """
    attempted = [r for r in results if not r["skipped"]]
    if not attempted:
        return "ok", "no profile needed to be tried"

    answered = [r for r in attempted if r["status"] is not None]
    refused = [r for r in answered if r["status"] in _ROTATE_STATUS_CODES]
    won = [r for r in answered if r["status"] not in _ROTATE_STATUS_CODES]

    if won:
        if refused:
            names = ", ".join(str(r["profile"]) for r in refused)
            return "degraded", f"refused by {names}; at least one profile still works"
        return "ok", f"{won[0]['profile']} reached the target"

    if not answered:
        return (
            "unreachable",
            "no profile got a response at all -- the target or the network is down, "
            "and nothing was fingerprinted because nothing was answered",
        )
    return (
        "blocked",
        f"every profile that got a response was refused ({len(refused)} of {len(answered)})",
    )


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

    verdict, detail = _verdict(results)
    return {
        "url": url,
        "winning_profile": winning_profile,
        # Only `blocked` fails. The other three are reported, not alarmed:
        #
        # `unreachable` -- the target never answered, so there is nothing to say
        # about our fingerprints. Failing here turns a third-party outage into a
        # weekly false alarm, which is what happened five times in twelve runs.
        #
        # `degraded` -- a profile was refused and another still won, so the
        # ladder did its job and scraping works. It is the early warning the
        # canary exists to give, and it belongs in the verdict and the artefact
        # rather than in a failed build: failing a job while the system works is
        # the same false alarm from the other side.
        "exit_code": 1 if verdict == "blocked" else 0,
        "verdict": verdict,
        "detail": detail,
        "results": results,
    }


def _format_text(report: dict[str, object]) -> str:
    """Render the canary report as a human-readable text table."""
    lines = [
        f"Verdict: {report.get('verdict', '?')} - {report.get('detail', '')}",
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
