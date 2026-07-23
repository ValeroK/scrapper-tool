"""Batch fetching via the ``obscura`` CLI (D4) — a fast path, not the default.

``obscura scrape <urls...> --concurrency N`` renders many URLs in one process.
For a list of *lightly-protected* pages that's a large win over launching a
Camoufox instance per page: one process, ~30 MB, and the concurrency is handled
by the Rust side rather than by us holding N browsers open.

**When not to use it.** It gives up everything the cascade provides: no TLS
ladder, no recipe replay, no challenge detection, no proxy rotation, no captcha
handling, no per-page escalation. The measured detection rate of the stealth
build (see ``docs/research/2026-camoufox-obscura-capabilities.md``) is well short
of Camoufox's. So this is for bulk retrieval of pages you already know are
unprotected — a sitemap sweep of a plain catalogue — and
:func:`~scrapper_tool.crawl.crawl.crawl` remains the right call for anything else.

Output is parsed defensively: the CLI is a young external binary, so a run that
returns some usable records and some junk yields the usable ones rather than
raising. What was dropped is counted and logged, never silently discarded.
"""

from __future__ import annotations

import asyncio
import json
import shutil
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from scrapper_tool._logging import get_logger
from scrapper_tool.errors import ConfigurationError

if TYPE_CHECKING:
    from collections.abc import Sequence

_logger = get_logger(__name__)

_DEFAULT_CONCURRENCY = 10
_DEFAULT_TIMEOUT_S = 300.0
# Guard against a runaway CLI filling memory with a multi-GB JSON blob.
_MAX_OUTPUT_BYTES = 256 * 1024 * 1024

_OBSCURA_NOT_FOUND = (
    "The `obscura` CLI is not on PATH. Batch mode shells out to it — either "
    "install it (cargo install --features stealth, or use the project's "
    "Dockerfile.obscura image) or use crawl()/scrape(), which need no external "
    "binary."
)


@dataclass(frozen=True)
class BatchPage:
    """One URL's outcome from a batch run."""

    url: str
    html: str = ""
    status: int = 0
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and bool(self.html)


@dataclass(frozen=True)
class BatchResult:
    """Everything a batch run produced, including what it couldn't parse."""

    pages: list[BatchPage]
    requested: int
    unparsed_records: int = 0
    missing_urls: tuple[str, ...] = field(default_factory=tuple)

    @property
    def ok_count(self) -> int:
        return sum(1 for page in self.pages if page.ok)


def obscura_available() -> bool:
    """Whether the ``obscura`` CLI is on PATH."""
    return shutil.which("obscura") is not None


def _coerce_page(record: Any) -> BatchPage | None:
    """Read one output record, tolerating unknown key names.

    The CLI's exact field names aren't a stable contract we control, so each
    value is looked up under the plausible aliases rather than assuming one
    shape. A record with no URL is unusable and returns None.
    """
    if not isinstance(record, dict):
        return None
    url = _first(record, "url", "requestedUrl", "request_url", "finalUrl", "final_url")
    if not isinstance(url, str) or not url:
        return None
    html = _first(record, "html", "content", "body", "text", "markdown") or ""
    status = _first(record, "status", "statusCode", "status_code") or 0
    error = _first(record, "error", "err", "message")
    try:
        status_int = int(status)
    except (TypeError, ValueError):
        status_int = 0
    return BatchPage(
        url=url,
        html=html if isinstance(html, str) else "",
        status=status_int,
        error=str(error) if error else None,
    )


def _first(record: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = record.get(key)
        if value is not None:
            return value
    return None


def parse_batch_output(raw: str, requested: Sequence[str]) -> BatchResult:
    """Parse ``obscura scrape --format json`` output.

    Accepts a JSON array, a single object, or JSON Lines — all three have been
    reasonable guesses for a tool this young, and accepting each costs nothing
    while a wrong guess costs the whole batch.
    """
    records: list[Any] = []
    text = raw.strip()
    if text:
        try:
            parsed = json.loads(text)
            records = parsed if isinstance(parsed, list) else [parsed]
        except json.JSONDecodeError:
            for raw_line in text.splitlines():
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    records.append(None)  # counted as unparsed below

    pages: list[BatchPage] = []
    unparsed = 0
    for record in records:
        page = _coerce_page(record)
        if page is None:
            unparsed += 1
        else:
            pages.append(page)

    returned = {page.url for page in pages}
    missing = tuple(url for url in requested if url not in returned)
    if unparsed or missing:
        _logger.info(
            "crawl.batch.partial",
            requested=len(requested),
            parsed=len(pages),
            unparsed=unparsed,
            missing=len(missing),
        )
    return BatchResult(
        pages=pages,
        requested=len(requested),
        unparsed_records=unparsed,
        missing_urls=missing,
    )


async def batch_fetch(
    urls: Sequence[str],
    *,
    concurrency: int = _DEFAULT_CONCURRENCY,
    timeout_s: float = _DEFAULT_TIMEOUT_S,
    stealth: bool = True,
    proxy: str | None = None,
    executable: str = "obscura",
) -> BatchResult:
    """Render ``urls`` in one ``obscura scrape`` run.

    Raises :class:`ConfigurationError` when the CLI is absent — that's a
    misconfiguration the caller should see, not something to paper over, since
    the whole point of choosing this path was to use it.

    A non-zero exit code is *not* fatal on its own: the CLI can fail some URLs
    and still emit usable records for the rest, and throwing away good pages
    because one URL 404'd would be worse than reporting both.
    """
    if not urls:
        return BatchResult(pages=[], requested=0)
    if shutil.which(executable) is None:
        raise ConfigurationError(_OBSCURA_NOT_FOUND)

    args = [
        executable,
        "scrape",
        *urls,
        "--concurrency",
        str(max(1, concurrency)),
        "--format",
        "json",
    ]
    if stealth:
        args.append("--stealth")
    if proxy:
        args.extend(["--proxy", proxy])

    _logger.info("crawl.batch.start", count=len(urls), concurrency=concurrency)
    process = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout_s)
    except TimeoutError:
        # Kill it: an orphaned browser process per timed-out batch would leak
        # until the host runs out of handles.
        process.kill()
        await process.wait()
        _logger.warning("crawl.batch.timeout", count=len(urls), timeout_s=timeout_s)
        return BatchResult(pages=[], requested=len(urls), missing_urls=tuple(urls))

    if process.returncode:
        _logger.warning(
            "crawl.batch.nonzero_exit",
            code=process.returncode,
            stderr=(stderr or b"").decode("utf-8", errors="replace")[:300],
        )

    raw = (stdout or b"")[:_MAX_OUTPUT_BYTES].decode("utf-8", errors="replace")
    result = parse_batch_output(raw, urls)
    _logger.info(
        "crawl.batch.done", requested=result.requested, ok=result.ok_count, exit=process.returncode
    )
    return result


__all__ = [
    "BatchPage",
    "BatchResult",
    "batch_fetch",
    "obscura_available",
    "parse_batch_output",
]
