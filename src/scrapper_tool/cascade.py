"""The one autonomous entrypoint (F1) — ``scrape`` and ``crawl_site``.

"Handles anything automatically" is meant to be *one call*. This is that call,
for library callers who don't want to stand up the REST sidecar or the MCP
server just to run the cascade:

    from scrapper_tool import scrape

    result = await scrape("https://store.example.com/p/123", schema=my_schema)
    print(result["pattern_used"], result["product"])

It runs the whole self-driving cascade — replay → HTTP ladder → Pattern D →
stealth render → E1 → (E2 if ``interactive``) — with recipe learning, per-domain
tier memory, challenge detection, and proxy rotation, exactly as ``/scrape``
does, because it delegates to the same implementation rather than a parallel
one. The REST endpoint and the MCP tool are thin adapters over this cascade;
this is a third thin adapter, for in-process use.

The one deliberate coupling: the cascade body lives in
:mod:`scrapper_tool.http_server` (it grew up there with the REST endpoint). That
module imports FastAPI *lazily*, so importing and calling the cascade needs no
``[http]`` extra — only the tiers a given scrape actually reaches pull their own
dependencies. Keeping the body there rather than moving it avoids a large, risky
lift of two heavily-tested surfaces for a purely cosmetic relocation; the public
name lives here so callers never import ``http_server`` to scrape.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable


async def scrape(
    url: str,
    schema: dict[str, Any] | list[Any] | str | None = None,
    *,
    interactive: bool = False,
    mode: str = "auto",
    browser: str | None = None,
    model: str | None = None,
    timeout_s: float | None = None,
    instruction: str | None = None,
    persist_browser_profile_dir: str | None = None,
) -> dict[str, Any]:
    """Run the autonomous cascade for one URL and return the result dict.

    Parameters mirror the ``/scrape`` request body; the return value is the same
    payload the REST endpoint produces, including ``pattern_used`` (which tier
    won), ``challenge_detected``, and ``escalation_log``.

    ``interactive=True`` permits escalation to the E2 browser-use agent for
    login / pagination / dynamic-form flows — off by default because E2 is the
    most expensive tier and a merely-walled page will wall the agent too.

    Raises the same taxonomy as the endpoint: ``BlockedError`` when every tier is
    walled, ``ConfigurationError`` (503-class) on a bad backend name or a missing
    extra, ``AgentTimeoutError`` on an agent-loop timeout.
    """
    from scrapper_tool.http_server import ScrapeRequest, _do_scrape  # noqa: PLC0415

    req = ScrapeRequest(
        url=url,
        schema_json=schema,
        mode=mode,
        interactive=interactive,
        browser=browser,
        model=model,
        timeout_s=timeout_s,
        instruction=instruction,
        persist_browser_profile_dir=persist_browser_profile_dir,
    )
    return await _do_scrape(req)


async def crawl_site(
    seed: str,
    *,
    schema: dict[str, Any] | list[Any] | str | None = None,
    depth: int = 2,
    max_pages: int = 50,
    concurrency: int = 4,
    same_domain: bool = True,
    respect_robots: bool = True,
    interactive: bool = False,
    timeout_s: float | None = None,
) -> AsyncIterator[Any]:
    """Crawl a site, running the full autonomous cascade on every page.

    Yields :class:`~scrapper_tool.crawl.crawl.CrawlPage` as each page completes —
    so a long crawl can be consumed as a stream — with each page benefiting from
    recipe replay, the render tier, and per-domain memory. The recipe and policy
    learned on the first page make the rest of the crawl progressively cheaper.

    See :func:`scrapper_tool.crawl.crawl.crawl` for the bound semantics; this is a
    thin wrapper that supplies :func:`scrape` as the per-page handler.
    """
    from scrapper_tool.crawl.crawl import crawl as _crawl  # noqa: PLC0415

    async def scrape_one(page_url: str) -> dict[str, Any]:
        return await scrape(page_url, schema=schema, interactive=interactive, timeout_s=timeout_s)

    async for page in _crawl(
        seed,
        scrape=scrape_one,
        depth=depth,
        max_pages=max_pages,
        concurrency=concurrency,
        same_domain=same_domain,
        respect_robots=respect_robots,
    ):
        yield page


def _scrape_fn() -> Callable[..., Awaitable[dict[str, Any]]]:
    """Return the bound scrape callable (used by adapters that want a handle)."""
    return scrape


__all__ = [
    "crawl_site",
    "scrape",
]
