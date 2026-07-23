"""Site-level scraping: discover URLs, then walk them.

Everything above a single page. :mod:`~scrapper_tool.crawl.map` answers "what
pages does this site have?", :mod:`~scrapper_tool.crawl.crawl` walks them
breadth-first through the caller's cascade, and
:mod:`~scrapper_tool.crawl.robots` finally enforces the ``respect_robots``
setting that had existed as configuration without an implementation.

Both entrypoints take the fetch/scrape function as an argument rather than
importing the cascade. That keeps this package free of the browser and LLM extras
— it works on a bare install — and means a crawl automatically inherits whatever
anti-bot handling, proxy rotation, and recipe replay the caller's cascade has.
"""

from __future__ import annotations

from scrapper_tool.crawl.batch import (
    BatchPage,
    BatchResult,
    batch_fetch,
    obscura_available,
)
from scrapper_tool.crawl.crawl import CrawlPage, CrawlStats, crawl, crawl_to_list
from scrapper_tool.crawl.map import (
    MapResult,
    extract_links,
    make_ladder_fetch,
    map_site,
    normalise_url,
    same_site,
)
from scrapper_tool.crawl.robots import RobotsCache

__all__ = [
    "BatchPage",
    "BatchResult",
    "CrawlPage",
    "CrawlStats",
    "MapResult",
    "RobotsCache",
    "batch_fetch",
    "crawl",
    "crawl_to_list",
    "extract_links",
    "make_ladder_fetch",
    "map_site",
    "normalise_url",
    "obscura_available",
    "same_site",
]
