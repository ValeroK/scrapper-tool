"""``scrapper-tool diagnose <url>`` — why did this one URL not work?

:mod:`doctor` answers "is this install healthy?". This answers a different
question that used to be answerable only by hand: *for this specific URL, what
actually happens?*

It exists because of a real two-day investigation. A vendor was reported as
blocking for months. It was not: two of the five symptoms were a wrong URL, one
was a real challenge shown only to the container while the host got a clean 200
in the same minute, one was the same page reported as a *success* while sitting
on a captcha, and one was a recon verdict from five months earlier hardcoded into
a call site. Every one of those is visible in under a minute if you probe the
three axes that matter and print what came back.

The three axes, which is the whole design:

* **Impersonation profile** — does any TLS fingerprint get through?
* **Cascade tier** — does the answer change with more machinery?
* **URL shape** — is the path even right? A 404 and a challenge look identical
  once you have decided the vendor is hostile.

Deliberately an escape hatch. If the result fields this release added are doing
their job, nobody should need this; when they do, it should take thirty seconds
rather than a morning.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any

from scrapper_tool._logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Sequence

    import httpx

_logger = get_logger(__name__)

#: How long any single probe may take. Short on purpose: this is a diagnostic,
#: and twelve probes that each wait 30s is not a thirty-second answer.
_PROBE_TIMEOUT_S = 12.0

#: Bodies below this are almost never content. Reported, never judged on.
_THIN_BODY_BYTES = 5_000


@dataclass
class ProbeResult:
    """One cell of the diagnosis matrix."""

    axis: str
    name: str
    outcome: str
    detail: str
    status: int | None = None
    bytes_: int | None = None
    final_url: str | None = None

    def to_dict(self) -> dict[str, Any]:
        out = asdict(self)
        out["bytes"] = out.pop("bytes_")
        return out


@dataclass
class Diagnosis:
    """Everything the probes learned about one URL."""

    url: str
    probes: list[ProbeResult] = field(default_factory=list)
    verdict: str = "unknown"
    reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "verdict": self.verdict,
            "reasons": self.reasons,
            "probes": [p.to_dict() for p in self.probes],
        }


def _describe(html: str, status: int, requested: str, final: str) -> tuple[str, str]:
    """Classify one fetched body. Returns ``(outcome, detail)``.

    Uses the shared classifier rather than a private copy, so a diagnosis and a
    scrape can never disagree about the same body — the failure mode that made
    the original investigation so slow was two components describing one page
    differently.
    """
    from scrapper_tool._challenge import is_interstitial, landed_on_challenge  # noqa: PLC0415

    size = len(html)
    vendor = is_interstitial(html, status)
    if vendor is not None:
        return "challenge", f"{vendor} wall, {size:,} b"
    if landed_on_challenge(requested, final, html):
        return "challenge", f"redirected to {final}"
    if status == 404:  # noqa: PLR2004 — the URL-shape signal this exists to catch
        return "not_found", f"HTTP 404, {size:,} b - check the path, not the vendor"
    if status >= 400:  # noqa: PLR2004
        return "http_error", f"HTTP {status}, {size:,} b"
    if size < _THIN_BODY_BYTES:
        return "thin", f"HTTP {status}, only {size:,} b"
    return "ok", f"HTTP {status}, {size:,} b"


async def _fetch(url: str, *, impersonate: str | None = None) -> tuple[str, int, str]:
    """One request, no ladder, no retry rotation. Returns ``(html, status, final_url)``.

    Reuses the ladder's own curl_cffi session so a probe and a real Pattern A/B/C
    fetch are the same request — a diagnostic that fetches differently from the
    thing it is diagnosing is worse than none.
    """
    from typing import cast  # noqa: PLC0415

    from scrapper_tool.http import request_with_retry  # noqa: PLC0415
    from scrapper_tool.ladder import IMPERSONATE_LADDER, _curl_cffi_session  # noqa: PLC0415

    profile = impersonate or IMPERSONATE_LADDER[0]
    async with _curl_cffi_session(
        profile, timeout=_PROBE_TIMEOUT_S, proxy=None, extra_headers=None
    ) as session:
        response = await request_with_retry(
            cast("httpx.AsyncClient", session), "GET", url, max_attempts=1
        )
    return response.text or "", response.status_code, str(response.url)


async def _probe_profiles(url: str) -> list[ProbeResult]:
    """Every impersonation profile, one at a time.

    The ladder normally stops at the first profile that works, which is correct
    for scraping and useless for diagnosis: "chrome150 worked" does not tell you
    whether the vendor is fingerprinting at all, and "all of them failed" is a
    completely different finding from "the first one did".
    """
    from scrapper_tool.ladder import IMPERSONATE_LADDER  # noqa: PLC0415

    results: list[ProbeResult] = []
    for profile in IMPERSONATE_LADDER:
        try:
            html, status, final = await asyncio.wait_for(
                _fetch(url, impersonate=profile), timeout=_PROBE_TIMEOUT_S + 3
            )
        except TimeoutError:
            results.append(
                ProbeResult("profile", profile, "timeout", f"no answer in {_PROBE_TIMEOUT_S:.0f}s")
            )
            continue
        except Exception as exc:
            results.append(ProbeResult("profile", profile, "error", f"{type(exc).__name__}: {exc}"))
            continue
        outcome, detail = _describe(html, status, url, final)
        results.append(
            ProbeResult(
                "profile",
                profile,
                outcome,
                detail,
                status=status,
                bytes_=len(html),
                final_url=final,
            )
        )
    return results


async def _probe_url_shapes(url: str) -> list[ProbeResult]:
    """Obvious URL variants, because two of the five reported symptoms were paths.

    Cheap, and it catches the class of bug that is most expensive to mistake for
    hostility: a wrong path returns an error page, and an error page read through
    the assumption "this vendor blocks us" looks exactly like a wall.
    """
    from urllib.parse import urlsplit, urlunsplit  # noqa: PLC0415

    parts = urlsplit(url)
    variants: dict[str, str] = {}
    if parts.path.endswith("/"):
        variants["no trailing slash"] = urlunsplit(parts._replace(path=parts.path.rstrip("/")))
    else:
        variants["trailing slash"] = urlunsplit(parts._replace(path=parts.path + "/"))
    if parts.query:
        variants["no query"] = urlunsplit(parts._replace(query=""))

    results: list[ProbeResult] = []
    for name, variant in variants.items():
        try:
            html, status, final = await asyncio.wait_for(
                _fetch(variant), timeout=_PROBE_TIMEOUT_S + 3
            )
        except Exception as exc:
            results.append(ProbeResult("url", name, "error", f"{type(exc).__name__}: {exc}"))
            continue
        outcome, detail = _describe(html, status, variant, final)
        results.append(
            ProbeResult(
                "url", name, outcome, detail, status=status, bytes_=len(html), final_url=final
            )
        )
    return results


def _verdict(probes: list[ProbeResult]) -> tuple[str, list[str]]:
    """Turn the matrix into one sentence and the evidence behind it.

    Ordered so the cheap explanations are ruled out first — which is exactly the
    order the original investigation failed to follow.
    """
    reasons: list[str] = []
    by_axis: dict[str, list[ProbeResult]] = {}
    for probe in probes:
        by_axis.setdefault(probe.axis, []).append(probe)

    profiles = by_axis.get("profile", [])
    working = [p for p in profiles if p.outcome == "ok"]
    challenged = [p for p in profiles if p.outcome == "challenge"]
    not_found = [p for p in profiles if p.outcome == "not_found"]

    if not_found and not working:
        reasons.append(
            "Every profile got HTTP 404. This is a URL problem, not an anti-bot "
            "problem - check the path before anything else."
        )
        return "wrong_url", reasons

    if working:
        reasons.append(
            f"{len(working)} of {len(profiles)} impersonation profiles fetched real "
            f"content ({', '.join(p.name for p in working)})."
        )
        if challenged:
            reasons.append(
                "Some profiles were challenged and some were not, so the vendor is "
                "fingerprinting rather than refusing outright. The ladder handles this."
            )
        return "reachable", reasons

    if challenged:
        reasons.append(
            f"All {len(profiles)} profiles were challenged. This looks like a genuine "
            "wall from this network path - try the render tier, and compare against a "
            "different egress before concluding the vendor blocks you everywhere."
        )
        return "challenged", reasons

    reasons.append(
        "No profile returned usable content, and none saw a recognisable challenge. "
        "Suspect the network path or the host itself rather than anti-bot."
    )
    return "unreachable", reasons


async def run_diagnose(url: str) -> dict[str, Any]:
    """Probe ``url`` across profiles and URL shapes, and return the report."""
    from scrapper_tool._urlguard import assert_url_allowed  # noqa: PLC0415

    # The guard runs first and is not optional. A diagnostic that reaches hosts a
    # scrape would refuse is a scanning tool, and this one takes a URL straight
    # from a command line.
    await assert_url_allowed(url)

    diagnosis = Diagnosis(url=url)
    diagnosis.probes.extend(await _probe_profiles(url))
    diagnosis.probes.extend(await _probe_url_shapes(url))
    diagnosis.verdict, diagnosis.reasons = _verdict(diagnosis.probes)
    _logger.info("diagnose.complete", url=url, verdict=diagnosis.verdict)
    return diagnosis.to_dict()


_OUTCOME_MARK = {
    "ok": "ok",
    "challenge": "WALL",
    "not_found": "404",
    "http_error": "ERR",
    "thin": "thin",
    "timeout": "slow",
    "error": "fail",
}


def _format_text(report: dict[str, Any]) -> str:
    """A verdict table. Plain ASCII — this is read in a terminal, often over ssh."""
    lines = [f"diagnose: {report['url']}", f"verdict:  {report['verdict']}", ""]
    axis_titles = {"profile": "impersonation profiles", "url": "url shapes"}
    seen: set[str] = set()
    for probe in report["probes"]:
        axis = probe["axis"]
        if axis not in seen:
            seen.add(axis)
            lines.append(f"-- {axis_titles.get(axis, axis)} --")
        mark = _OUTCOME_MARK.get(probe["outcome"], probe["outcome"])
        lines.append(f"  {mark:<5} {probe['name']:<14} {probe['detail']}")
    if report["reasons"]:
        lines.append("")
        lines.append("-- what this means --")
        lines.extend(f"  {reason}" for reason in report["reasons"])
    return "\n".join(lines)


#: Verdicts that mean "the tool could reach this page". Anything else exits 1 so
#: the command is usable in a shell conditional.
_OK_VERDICTS = frozenset({"reachable"})


def add_subparser(sub: Any) -> None:
    """Register the ``diagnose`` subcommand on ``scrapper-tool``'s dispatcher."""
    diagnose = sub.add_parser(
        "diagnose",
        help="Probe one URL across impersonation profiles and URL shapes, and say why it failed.",
        description=(
            "Answers 'why did this URL not work?'. Fetches it with every "
            "impersonation profile and a couple of URL variants, then prints a "
            "verdict table separating a wrong path from a real anti-bot wall. "
            "Exit 0 when the page is reachable, 1 otherwise."
        ),
    )
    diagnose.add_argument("url", help="The URL to diagnose.")
    diagnose.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON instead of a text table.",
    )


def run_cli(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    """Handler invoked by the ``scrapper-tool`` dispatcher."""
    url = getattr(args, "url", "")
    if not url.lower().startswith(("http://", "https://")):
        parser.error("url must start with http:// or https://")

    try:
        report = asyncio.run(run_diagnose(url))
    except Exception as exc:
        sys.stderr.write(f"diagnose error: {exc}\n")
        return 2

    if args.json:
        sys.stdout.write(json.dumps(report, indent=2))
    else:
        sys.stdout.write(_format_text(report))
    sys.stdout.write("\n")
    return 0 if report["verdict"] in _OK_VERDICTS else 1


def main(argv: Sequence[str] | None = None) -> int:
    """Direct entry point — ``python -m scrapper_tool.diagnose <url>``."""
    parser = argparse.ArgumentParser(prog="scrapper-tool diagnose")
    sub = parser.add_subparsers(dest="command")
    add_subparser(sub)
    args = parser.parse_args(["diagnose", *(argv or sys.argv[1:])])
    return run_cli(args, parser)


__all__ = ["add_subparser", "main", "run_cli", "run_diagnose"]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
