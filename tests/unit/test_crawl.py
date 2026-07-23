"""Unit tests for recursive crawling and robots.txt enforcement (D3).

Two things get pinned hardest:

**Bounds actually bound.** A crawler whose depth or page limit leaks runs the full
cascade — potentially a browser launch and an LLM call — on every page it finds.
An off-by-one here is a real bill, not a style issue.

**Politeness is real.** ``respect_robots`` existed as configuration for several
releases while nothing read it. These tests are what make it a behaviour.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest

from scrapper_tool.crawl.crawl import CrawlStats, crawl, crawl_to_list
from scrapper_tool.crawl.robots import RobotsCache, parse_crawl_delay

# A small fixture site: root links to /a and /b; /a links to /a1; /b links to /b1.
_SITE: dict[str, str] = {
    "https://site.test/": '<a href="/a">a</a><a href="/b">b</a>',
    "https://site.test/a": '<a href="/a1">a1</a><a href="/">home</a>',
    "https://site.test/b": '<a href="/b1">b1</a>',
    "https://site.test/a1": "<p>leaf</p>",
    "https://site.test/b1": "<p>leaf</p>",
}


def _make_scrape(site: dict[str, str] | None = None, *, calls: list[str] | None = None) -> Any:
    pages = site if site is not None else _SITE

    async def scrape(url: str) -> dict[str, Any]:
        if calls is not None:
            calls.append(url)
        if url not in pages:
            raise RuntimeError(f"404 {url}")
        return {"url": url, "raw_text": pages[url], "pattern_used": "a_b_c"}

    return scrape


@pytest.fixture
def robots_allow_all(monkeypatch: pytest.MonkeyPatch) -> RobotsCache:
    """A RobotsCache that answers "allowed, no delay" without network I/O."""
    cache = RobotsCache()

    async def allowed(url: str, **_kwargs: Any) -> bool:
        return True

    async def crawl_delay(url: str, **_kwargs: Any) -> float:
        return 0.0

    monkeypatch.setattr(cache, "allowed", allowed)
    monkeypatch.setattr(cache, "crawl_delay", crawl_delay)
    return cache


# --- traversal --------------------------------------------------------------


@pytest.mark.asyncio
async def test_crawls_the_whole_small_site(robots_allow_all: RobotsCache) -> None:
    pages, stats = await crawl_to_list(
        "https://site.test/", scrape=_make_scrape(), depth=2, robots=robots_allow_all
    )
    assert {p.url for p in pages} == set(_SITE)
    assert stats.visited == 5
    assert stats.failed == 0


@pytest.mark.asyncio
async def test_depth_zero_visits_only_the_seed(robots_allow_all: RobotsCache) -> None:
    pages, stats = await crawl_to_list(
        "https://site.test/", scrape=_make_scrape(), depth=0, robots=robots_allow_all
    )
    assert [p.url for p in pages] == ["https://site.test/"]
    assert stats.hit_depth_limit is True


@pytest.mark.asyncio
async def test_depth_one_stops_before_the_leaves(robots_allow_all: RobotsCache) -> None:
    pages, _stats = await crawl_to_list(
        "https://site.test/", scrape=_make_scrape(), depth=1, robots=robots_allow_all
    )
    assert {p.url for p in pages} == {
        "https://site.test/",
        "https://site.test/a",
        "https://site.test/b",
    }


@pytest.mark.asyncio
async def test_each_page_is_scraped_once(robots_allow_all: RobotsCache) -> None:
    """``/a`` links back to the root — a crawler without dedup loops forever."""
    calls: list[str] = []
    await crawl_to_list(
        "https://site.test/",
        scrape=_make_scrape(calls=calls),
        depth=3,
        robots=robots_allow_all,
    )
    assert len(calls) == len(set(calls)) == 5


@pytest.mark.asyncio
async def test_max_pages_is_enforced_and_reported(robots_allow_all: RobotsCache) -> None:
    calls: list[str] = []
    pages, stats = await crawl_to_list(
        "https://site.test/",
        scrape=_make_scrape(calls=calls),
        depth=3,
        max_pages=3,
        robots=robots_allow_all,
    )
    assert stats.visited == 3
    assert len(calls) == 3, "the budget must stop work, not just filter results"
    assert len(pages) == 3
    assert stats.hit_page_limit is True
    assert stats.queued_but_unvisited > 0, "what was left undone must be visible"


@pytest.mark.asyncio
async def test_offsite_links_are_not_followed(robots_allow_all: RobotsCache) -> None:
    site = {
        "https://site.test/": '<a href="https://elsewhere.test/x">off</a><a href="/a">a</a>',
        "https://site.test/a": "<p>leaf</p>",
    }
    pages, _stats = await crawl_to_list(
        "https://site.test/", scrape=_make_scrape(site), depth=2, robots=robots_allow_all
    )
    assert all("elsewhere.test" not in p.url for p in pages)


@pytest.mark.asyncio
async def test_a_failing_page_is_reported_not_dropped(robots_allow_all: RobotsCache) -> None:
    """40-of-50 and 50-of-50 need different follow-up, so failures are yielded."""
    site = {"https://site.test/": '<a href="/missing">gone</a>'}
    pages, stats = await crawl_to_list(
        "https://site.test/", scrape=_make_scrape(site), depth=1, robots=robots_allow_all
    )
    failed = [p for p in pages if p.error]
    assert len(failed) == 1
    assert failed[0].url == "https://site.test/missing"
    assert "RuntimeError" in (failed[0].error or "")
    assert stats.failed == 1


@pytest.mark.asyncio
async def test_one_failure_does_not_abort_the_crawl(robots_allow_all: RobotsCache) -> None:
    site = {
        "https://site.test/": '<a href="/missing">x</a><a href="/ok">y</a>',
        "https://site.test/ok": "<p>fine</p>",
    }
    pages, stats = await crawl_to_list(
        "https://site.test/", scrape=_make_scrape(site), depth=1, robots=robots_allow_all
    )
    assert stats.failed == 1
    assert any(p.url == "https://site.test/ok" and p.ok for p in pages)


@pytest.mark.asyncio
async def test_results_stream_rather_than_batch(robots_allow_all: RobotsCache) -> None:
    """A 500-page crawl takes minutes; the caller shouldn't wait for the end."""
    seen: list[str] = []
    async for page in crawl(
        "https://site.test/", scrape=_make_scrape(), depth=2, robots=robots_allow_all
    ):
        seen.append(page.url)
        if len(seen) == 1:
            # The first result arrived before the crawl finished.
            assert seen == ["https://site.test/"]
    assert len(seen) == 5


@pytest.mark.asyncio
async def test_concurrency_is_bounded(robots_allow_all: RobotsCache) -> None:
    in_flight = 0
    peak = 0

    async def slow_scrape(url: str) -> dict[str, Any]:
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0)
        in_flight -= 1
        return {"url": url, "raw_text": _SITE.get(url, "")}

    await crawl_to_list(
        "https://site.test/",
        scrape=slow_scrape,
        depth=3,
        concurrency=2,
        robots=robots_allow_all,
    )
    assert peak <= 2


@pytest.mark.asyncio
async def test_links_are_followed_from_the_rendered_dom(
    robots_allow_all: RobotsCache,
) -> None:
    """A JS-rendered listing is only traversable via the browser tier's HTML."""

    async def scrape(url: str) -> dict[str, Any]:
        if url == "https://site.test/":
            # raw_text is what a render tier produced; a raw fetch had nothing.
            return {
                "url": url,
                "pattern_used": "render",
                "raw_text": '<a href="/js-only">only in the rendered DOM</a>',
            }
        return {"url": url, "raw_text": "<p>leaf</p>"}

    pages, _stats = await crawl_to_list(
        "https://site.test/", scrape=scrape, depth=1, robots=robots_allow_all
    )
    assert "https://site.test/js-only" in {p.url for p in pages}


@pytest.mark.asyncio
async def test_stats_object_is_observable_during_the_crawl(
    robots_allow_all: RobotsCache,
) -> None:
    stats = CrawlStats()
    async for _page in crawl(
        "https://site.test/", scrape=_make_scrape(), depth=2, stats=stats, robots=robots_allow_all
    ):
        assert stats.visited >= 1
    assert stats.visited == 5
    assert stats.elapsed_s >= 0


# --- robots.txt enforcement -------------------------------------------------


def _robots_transport(body: str, status: int = 200) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/robots.txt"
        return httpx.Response(status, text=body)

    return httpx.MockTransport(handler)


@pytest.mark.asyncio
async def test_disallowed_paths_are_skipped() -> None:
    cache = RobotsCache()
    async with httpx.AsyncClient(
        transport=_robots_transport("User-agent: *\nDisallow: /private/\n")
    ) as client:
        assert await cache.allowed("https://site.test/public", client=client) is True
        assert await cache.allowed("https://site.test/private/x", client=client) is False


@pytest.mark.asyncio
async def test_crawl_skips_disallowed_urls_and_reports_them() -> None:
    cache = RobotsCache()
    site = {
        "https://site.test/": '<a href="/private/x">no</a><a href="/ok">yes</a>',
        "https://site.test/ok": "<p>fine</p>",
        "https://site.test/private/x": "<p>should never be fetched</p>",
    }
    calls: list[str] = []

    async def allowed(url: str, **_kwargs: Any) -> bool:
        return "/private/" not in url

    async def crawl_delay(url: str, **_kwargs: Any) -> float:
        return 0.0

    cache.allowed = allowed  # type: ignore[method-assign]
    cache.crawl_delay = crawl_delay  # type: ignore[method-assign]

    pages, stats = await crawl_to_list(
        "https://site.test/",
        scrape=_make_scrape(site, calls=calls),
        depth=1,
        respect_robots=True,
        robots=cache,
    )
    assert "https://site.test/private/x" not in calls, "a disallowed page must not be fetched"
    assert stats.skipped_robots == 1
    skipped = [p for p in pages if p.skipped_reason == "robots"]
    assert len(skipped) == 1


@pytest.mark.asyncio
async def test_robots_is_not_consulted_by_default() -> None:
    """Enforcement is opt-in, so nothing should even ask robots.txt."""
    calls: list[str] = []
    site = {
        "https://site.test/": '<a href="/private/x">x</a>',
        "https://site.test/private/x": "<p>ok</p>",
    }
    cache = RobotsCache()

    async def never(url: str, **_kwargs: Any) -> bool:
        raise AssertionError("robots.txt must not be fetched unless respect_robots=True")

    cache.allowed = never  # type: ignore[method-assign]

    _pages, stats = await crawl_to_list(
        "https://site.test/", scrape=_make_scrape(site, calls=calls), depth=1, robots=cache
    )
    assert "https://site.test/private/x" in calls
    assert stats.skipped_robots == 0


@pytest.mark.asyncio
async def test_missing_robots_txt_allows_everything() -> None:
    """RFC 9309: 4xx means no rules published."""
    cache = RobotsCache()
    async with httpx.AsyncClient(transport=_robots_transport("", status=404)) as client:
        assert await cache.allowed("https://site.test/anything", client=client) is True


@pytest.mark.asyncio
async def test_a_403_on_robots_txt_allows_everything() -> None:
    """Anti-bot systems commonly 403 robots.txt; that's still a 4xx."""
    cache = RobotsCache()
    async with httpx.AsyncClient(transport=_robots_transport("", status=403)) as client:
        assert await cache.allowed("https://site.test/anything", client=client) is True


@pytest.mark.asyncio
async def test_a_500_on_robots_txt_disallows_everything() -> None:
    """RFC 9309 treats unreachable as full disallow.

    The one case where guessing "allow" means hammering a server that just told
    us it's struggling.
    """
    cache = RobotsCache()
    async with httpx.AsyncClient(transport=_robots_transport("", status=503)) as client:
        assert await cache.allowed("https://site.test/anything", client=client) is False


@pytest.mark.asyncio
async def test_unreachable_robots_txt_disallows_everything() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host")

    cache = RobotsCache()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        assert await cache.allowed("https://site.test/x", client=client) is False


@pytest.mark.asyncio
async def test_robots_txt_is_fetched_once_per_origin() -> None:
    """500 crawled pages must not mean 500 robots.txt requests."""
    fetches: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        fetches.append(str(request.url))
        return httpx.Response(200, text="User-agent: *\nDisallow:\n")

    cache = RobotsCache()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        for path in ("a", "b", "c", "d"):
            await cache.allowed(f"https://site.test/{path}", client=client)
    assert len(fetches) == 1


@pytest.mark.asyncio
async def test_crawl_delay_is_read() -> None:
    cache = RobotsCache()
    body = "User-agent: *\nCrawl-delay: 2.5\nDisallow:\n"
    async with httpx.AsyncClient(transport=_robots_transport(body)) as client:
        assert await cache.crawl_delay("https://site.test/x", client=client) == pytest.approx(2.5)


class TestCrawlDelayParsing:
    """``RobotFileParser`` parses this directive with ``int()``.

    So a site asking for ``Crawl-delay: 0.5`` is read by the stdlib as asking for
    nothing at all. Fractional delays are common, and silently discarding them
    makes us less polite than the docs claim — hence a small parser of our own.
    """

    def test_fractional_delay_survives(self) -> None:
        assert parse_crawl_delay("User-agent: *\nCrawl-delay: 0.5\n", "bot") == pytest.approx(0.5)

    def test_integer_delay(self) -> None:
        assert parse_crawl_delay("User-agent: *\nCrawl-delay: 3\n", "bot") == pytest.approx(3.0)

    def test_absent_delay_is_zero(self) -> None:
        assert parse_crawl_delay("User-agent: *\nDisallow: /x\n", "bot") == 0.0

    def test_an_exact_user_agent_rule_beats_the_wildcard(self) -> None:
        """A rule written for us specifically is the one that applies."""
        body = "User-agent: *\nCrawl-delay: 10\n\nUser-agent: scrapper-tool\nCrawl-delay: 1\n"
        assert parse_crawl_delay(body, "scrapper-tool") == pytest.approx(1.0)
        assert parse_crawl_delay(body, "someone-else") == pytest.approx(10.0)

    def test_comments_and_blank_lines_are_ignored(self) -> None:
        body = "# hello\n\nUser-agent: *   # everyone\nCrawl-delay: 2  # be nice\n"
        assert parse_crawl_delay(body, "bot") == pytest.approx(2.0)

    def test_garbage_value_is_ignored_not_raised(self) -> None:
        assert parse_crawl_delay("User-agent: *\nCrawl-delay: soon\n", "bot") == 0.0

    def test_negative_delay_is_clamped(self) -> None:
        assert parse_crawl_delay("User-agent: *\nCrawl-delay: -5\n", "bot") == 0.0

    def test_delay_before_any_user_agent_is_ignored(self) -> None:
        assert parse_crawl_delay("Crawl-delay: 9\nUser-agent: *\n", "bot") == 0.0

    def test_empty_body(self) -> None:
        assert parse_crawl_delay("", "bot") == 0.0


@pytest.mark.asyncio
async def test_a_hostile_crawl_delay_is_capped() -> None:
    """``Crawl-delay: 86400`` would mean the crawl never finishes.

    Honouring it literally is indistinguishable from hanging, so the crawler caps
    what it will actually wait for while still slowing down.
    """
    from scrapper_tool.crawl.crawl import _MAX_HONOURED_CRAWL_DELAY_S

    cache = RobotsCache()
    slept: list[float] = []

    async def allowed(url: str, **_kwargs: Any) -> bool:
        return True

    async def crawl_delay(url: str, **_kwargs: Any) -> float:
        return 86_400.0

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    cache.allowed = allowed  # type: ignore[method-assign]
    cache.crawl_delay = crawl_delay  # type: ignore[method-assign]

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(asyncio, "sleep", fake_sleep)
        await crawl_to_list(
            "https://site.test/",
            scrape=_make_scrape(),
            depth=0,
            respect_robots=True,
            robots=cache,
        )
    assert slept == [_MAX_HONOURED_CRAWL_DELAY_S]


@pytest.mark.asyncio
async def test_absent_crawl_delay_is_zero() -> None:
    cache = RobotsCache()
    async with httpx.AsyncClient(
        transport=_robots_transport("User-agent: *\nDisallow:\n")
    ) as client:
        assert await cache.crawl_delay("https://site.test/x", client=client) == 0.0


@pytest.mark.asyncio
async def test_sitemaps_are_read_from_robots_txt() -> None:
    cache = RobotsCache()
    body = "User-agent: *\nSitemap: https://site.test/sm.xml\nSitemap: https://site.test/sm2.xml\n"
    async with httpx.AsyncClient(transport=_robots_transport(body)) as client:
        assert await cache.sitemaps("https://site.test/", client=client) == [
            "https://site.test/sm.xml",
            "https://site.test/sm2.xml",
        ]


@pytest.mark.asyncio
async def test_an_enormous_robots_txt_is_capped() -> None:
    """A multi-MB robots.txt is a misconfiguration or a trap, not rules."""
    cache = RobotsCache()
    body = "User-agent: *\n" + ("# padding\n" * 200_000) + "Disallow: /late/\n"
    async with httpx.AsyncClient(transport=_robots_transport(body)) as client:
        # The rule past the cap is never seen — that's the intended trade.
        assert await cache.allowed("https://site.test/late/x", client=client) is True
