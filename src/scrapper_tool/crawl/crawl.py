"""Recursive site traversal — breadth-first, bounded, polite.

Streams results as they complete rather than collecting them: a crawl of 500
pages takes minutes, and a caller that can start writing rows at page 3 is far
more useful than one that waits for page 500. It also means a crawl that hits its
budget mid-run still hands back everything it did get.

Breadth-first on purpose. Depth-first on a real site walks straight down a
pagination chain or a facet-filter rabbit hole and spends the whole page budget
in one corner; BFS spends it across the site, which is what "crawl this vendor"
means.

Politeness is not optional here. Unlike a single scrape — a user asking for one
page they could have opened themselves — a crawler visits pages nobody asked for,
so robots.txt is honoured by default (including ``Crawl-delay``) and concurrency
is capped. ``respect_robots=False`` exists because some authorised work needs it
(you own the site, or you have written permission), and it logs loudly.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from scrapper_tool._logging import get_logger
from scrapper_tool.crawl.map import extract_links, normalise_url, same_site
from scrapper_tool.crawl.robots import RobotsCache

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable

_logger = get_logger(__name__)

_DEFAULT_DEPTH = 2
_DEFAULT_MAX_PAGES = 50
_DEFAULT_CONCURRENCY = 4
# A crawler that ignores Crawl-delay is a nuisance; one that honours a hostile
# `Crawl-delay: 86400` never finishes. Cap what we'll actually wait for.
_MAX_HONOURED_CRAWL_DELAY_S = 10.0


@dataclass(frozen=True)
class CrawlPage:
    """One visited page.

    ``error`` set means the page failed; a crawl reports failures rather than
    dropping them, because "40 of 50 pages worked" and "50 of 50 worked" call for
    different follow-up.
    """

    url: str
    depth: int
    payload: dict[str, Any] | None = None
    error: str | None = None
    skipped_reason: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and self.skipped_reason is None


@dataclass
class CrawlStats:
    """Running totals — mutated as the crawl streams, final once it ends."""

    visited: int = 0
    failed: int = 0
    skipped_robots: int = 0
    queued_but_unvisited: int = 0
    hit_page_limit: bool = False
    hit_depth_limit: bool = False
    elapsed_s: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "visited": self.visited,
            "failed": self.failed,
            "skipped_robots": self.skipped_robots,
            "queued_but_unvisited": self.queued_but_unvisited,
            "hit_page_limit": self.hit_page_limit,
            "hit_depth_limit": self.hit_depth_limit,
            "elapsed_s": round(self.elapsed_s, 3),
        }


@dataclass
class _Frontier:
    """BFS queue with dedup. Separate class so the crawl loop stays readable."""

    seen: set[str] = field(default_factory=set)
    queue: deque[tuple[str, int]] = field(default_factory=deque)

    def push(self, url: str, depth: int) -> bool:
        if url in self.seen:
            return False
        self.seen.add(url)
        self.queue.append((url, depth))
        return True

    def pop_batch(self, size: int) -> list[tuple[str, int]]:
        batch: list[tuple[str, int]] = []
        while self.queue and len(batch) < size:
            batch.append(self.queue.popleft())
        return batch

    def __len__(self) -> int:
        return len(self.queue)


async def crawl(
    seed: str,
    *,
    scrape: Callable[[str], Awaitable[dict[str, Any]]],
    depth: int = _DEFAULT_DEPTH,
    max_pages: int = _DEFAULT_MAX_PAGES,
    concurrency: int = _DEFAULT_CONCURRENCY,
    same_domain: bool = True,
    respect_robots: bool = True,
    stats: CrawlStats | None = None,
    robots: RobotsCache | None = None,
) -> AsyncIterator[CrawlPage]:
    """Crawl from ``seed``, yielding each page as it completes.

    ``scrape`` handles one URL and returns a cascade payload — inject the full
    auto cascade so every crawled page benefits from replay, render, and the
    rest. Links are followed from the payload's ``raw_text``, so the crawler sees
    the *rendered* DOM when a browser tier produced it, which is the only way to
    traverse a JS-rendered listing.

    Pass ``stats`` to observe totals during the crawl (it's mutated in place);
    otherwise read the final numbers off the last yielded page's crawl.

    Bounds are reported, never silent: hitting ``max_pages`` or ``depth`` sets a
    flag on the stats and leaves ``queued_but_unvisited`` non-zero.
    """
    counters = stats if stats is not None else CrawlStats()
    started = time.perf_counter()
    robots_cache = robots or RobotsCache()
    if not respect_robots:
        _logger.warning(
            "crawl.robots_disabled",
            seed=seed,
            detail="respect_robots=False — only for sites you own or are authorised to crawl",
        )

    frontier = _Frontier()
    frontier.push(seed, 0)
    limiter = asyncio.Semaphore(max(1, concurrency))

    async def visit(url: str, current_depth: int) -> CrawlPage:
        if respect_robots and not await robots_cache.allowed(url):
            _logger.info("crawl.skipped_robots", url=url)
            return CrawlPage(url=url, depth=current_depth, skipped_reason="robots")
        async with limiter:
            delay = (
                min(await robots_cache.crawl_delay(url), _MAX_HONOURED_CRAWL_DELAY_S)
                if respect_robots
                else 0.0
            )
            if delay:
                await asyncio.sleep(delay)
            try:
                payload = await scrape(url)
            except Exception as exc:
                return CrawlPage(
                    url=url, depth=current_depth, error=f"{type(exc).__name__}: {exc!s}"[:300]
                )
        return CrawlPage(url=url, depth=current_depth, payload=payload)

    while frontier and counters.visited < max_pages:
        budget = max_pages - counters.visited
        batch = frontier.pop_batch(min(max(1, concurrency), budget))
        results = await asyncio.gather(*(visit(u, d) for u, d in batch))

        for page in results:
            if page.skipped_reason == "robots":
                counters.skipped_robots += 1
                yield page
                continue
            counters.visited += 1
            if page.error is not None:
                counters.failed += 1
                yield page
                continue

            if page.depth < depth:
                _enqueue_links(frontier, page, seed=seed, same_domain=same_domain)
            else:
                counters.hit_depth_limit = True
            yield page

    counters.hit_page_limit = counters.visited >= max_pages and len(frontier) > 0
    counters.queued_but_unvisited = len(frontier)
    counters.elapsed_s = time.perf_counter() - started
    if counters.queued_but_unvisited:
        _logger.info(
            "crawl.bounded",
            seed=seed,
            visited=counters.visited,
            unvisited=counters.queued_but_unvisited,
            reason="max_pages" if counters.hit_page_limit else "depth",
        )
    _logger.info("crawl.done", seed=seed, **counters.as_dict())


def _enqueue_links(frontier: _Frontier, page: CrawlPage, *, seed: str, same_domain: bool) -> None:
    """Queue this page's outbound links for the next depth level."""
    payload = page.payload or {}
    # The two surfaces name the HTML field differently — REST uses `raw_text`,
    # MCP uses `body` (truncated for the agent's context). Reading only one
    # silently reduces a crawl to visiting the seed.
    html = (
        payload.get("raw_text") or payload.get("body") or payload.get("intermediate_raw_text") or ""
    )
    if not isinstance(html, str) or not html:
        return
    base = str(payload.get("url") or page.url)
    for link in extract_links(html, base):
        candidate = normalise_url(link, base)
        if candidate is None:
            continue
        if same_domain and not same_site(candidate, seed):
            continue
        frontier.push(candidate, page.depth + 1)


async def crawl_to_list(
    seed: str,
    *,
    scrape: Callable[[str], Awaitable[dict[str, Any]]],
    **kwargs: Any,
) -> tuple[list[CrawlPage], CrawlStats]:
    """Convenience wrapper for callers that genuinely want everything at once.

    REST needs this (one request, one response), but prefer :func:`crawl` in
    library code — streaming is why it's an async iterator.
    """
    stats = CrawlStats()
    pages = [page async for page in crawl(seed, scrape=scrape, stats=stats, **kwargs)]
    return pages, stats


__all__ = [
    "CrawlPage",
    "CrawlStats",
    "crawl",
    "crawl_to_list",
]
