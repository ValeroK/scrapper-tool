"""URL discovery — what pages does this site have?

Feeds the crawler and is useful alone ("give me every product URL on this
vendor"). Two independent sources, because each finds things the other misses:

- **Sitemaps** declare URLs the site *wants* indexed, including ones no page
  links to. Found via robots.txt ``Sitemap:`` directives first (the canonical
  location, and where large sites point at their real indexes) and
  ``/sitemap.xml`` as a fallback. Sitemap indexes are followed one level.
- **Page links** find what's actually reachable, including the JS-rendered
  listing pages that never make it into a sitemap.

Nothing here is silently capped: when a limit truncates the result the count is
logged and reported on :class:`MapResult`, because "200 URLs" and "200 URLs out
of 40,000" are very different answers to build a crawl on.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING
from urllib.parse import urldefrag, urljoin, urlsplit

import httpx
from selectolax.lexbor import LexborHTMLParser

from scrapper_tool._logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterable

_logger = get_logger(__name__)

_DEFAULT_MAX_URLS = 200
_SITEMAP_TIMEOUT_S = 15.0
_CLIENT_ERROR_FLOOR = 400
_MAX_SITEMAP_BYTES = 8 * 1024 * 1024
# One level of index-following only. Sitemap indexes can nest arbitrarily and a
# deep chase is a lot of requests for diminishing returns.
_MAX_SITEMAP_INDEX_FOLLOW = 20

# Deliberately regex rather than an XML parser: sitemaps are frequently served
# with a wrong content type, truncated, or gzip-mislabelled, and a strict parse
# throws away every URL over one malformed tag. We only want the <loc> values.
_LOC_RE = re.compile(r"<loc>\s*([^<\s]+)\s*</loc>", re.IGNORECASE)
_SITEMAP_INDEX_RE = re.compile(r"<sitemapindex", re.IGNORECASE)

# Links that are never pages.
_SKIP_SCHEMES = frozenset({"mailto", "tel", "javascript", "data", "blob", "about", "ftp"})
_SKIP_SUFFIXES = (
    ".jpg",
    ".jpeg",
    ".png",
    ".gif",
    ".webp",
    ".svg",
    ".ico",
    ".bmp",
    ".avif",
    ".css",
    ".js",
    ".mjs",
    ".map",
    ".woff",
    ".woff2",
    ".ttf",
    ".eot",
    ".pdf",
    ".zip",
    ".gz",
    ".tar",
    ".rar",
    ".7z",
    ".mp4",
    ".webm",
    ".mp3",
    ".wav",
)


@dataclass(frozen=True)
class MapResult:
    """Discovered URLs plus what it took to find them."""

    urls: list[str]
    seed: str
    from_sitemap: int = 0
    from_links: int = 0
    dropped_by_limit: int = 0
    sitemaps_read: tuple[str, ...] = field(default_factory=tuple)

    @property
    def truncated(self) -> bool:
        return self.dropped_by_limit > 0


def extract_links(html: str, base_url: str) -> list[str]:
    """Absolute, de-fragmented, page-like links from one document, in order."""
    if not html:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for node in LexborHTMLParser(html).css("a[href]"):
        href = (node.attributes.get("href") or "").strip()
        if not href:
            continue
        normalised = normalise_url(href, base_url)
        if normalised and normalised not in seen:
            seen.add(normalised)
            out.append(normalised)
    return out


def normalise_url(href: str, base_url: str) -> str | None:
    """Absolutise and canonicalise one href, or None if it isn't a page.

    Fragments are stripped because ``/p#reviews`` and ``/p`` are the same fetch —
    keeping them would make a crawler revisit one page once per anchor on it.
    """
    scheme = href.split(":", 1)[0].lower() if ":" in href[:12] else ""
    if scheme in _SKIP_SCHEMES:
        return None
    absolute, _frag = urldefrag(urljoin(base_url, href))
    parts = urlsplit(absolute)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return None
    if parts.path.lower().endswith(_SKIP_SUFFIXES):
        return None
    return absolute


def same_site(url: str, seed: str) -> bool:
    """Whether ``url`` is on the seed's site — its host or a subdomain of it.

    Anchored on the *seed's* host rather than a computed registrable domain, which
    sidesteps the public-suffix problem entirely. Stripping labels to find a
    "registrable domain" without a PSL is actively dangerous: ``yad2.co.il`` would
    reduce to ``co.il`` and every Israeli commercial site would count as one site.

    So the rule is a label-boundary suffix match against the seed. Seeding
    ``example.com`` includes ``shop.example.com`` (a site's subdomain split is its
    own implementation detail); seeding ``shop.example.com`` stays on that
    subdomain; and ``notexample.com`` never matches ``example.com``. Use
    ``same_domain=False`` to follow links anywhere.
    """
    host = _host(url)
    seed_host = _host(seed)
    if not host or not seed_host:
        return False
    return host == seed_host or host.endswith(f".{seed_host}")


def _host(url: str) -> str:
    host = (urlsplit(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


async def fetch_sitemap_urls(
    seed: str,
    *,
    client: httpx.AsyncClient | None = None,
    timeout_s: float = _SITEMAP_TIMEOUT_S,
    candidates: Iterable[str] | None = None,
) -> tuple[list[str], list[str]]:
    """Read sitemaps for ``seed``. Returns ``(urls, sitemaps_read)``.

    Follows a sitemap *index* one level down. Failures are non-fatal: a site with
    no sitemap is normal, and link discovery covers it.
    """
    owns_client = client is None
    http = client or httpx.AsyncClient(timeout=timeout_s, follow_redirects=True)
    urls: list[str] = []
    read: list[str] = []
    try:
        queue = list(candidates) if candidates is not None else await _default_sitemaps(seed, http)
        depth_budget = _MAX_SITEMAP_INDEX_FOLLOW
        while queue:
            target = queue.pop(0)
            body = await _get_text(http, target)
            if body is None:
                continue
            read.append(target)
            found = _LOC_RE.findall(body)
            if _SITEMAP_INDEX_RE.search(body):
                # An index lists more sitemaps, not pages.
                take = min(len(found), depth_budget)
                if len(found) > take:
                    _logger.info(
                        "crawl.map.sitemap_index_truncated",
                        sitemap=target,
                        followed=take,
                        skipped=len(found) - take,
                    )
                queue.extend(found[:take])
                depth_budget -= take
                continue
            urls.extend(found)
    finally:
        if owns_client:
            await http.aclose()
    return urls, read


async def _default_sitemaps(seed: str, client: httpx.AsyncClient) -> list[str]:
    """robots.txt ``Sitemap:`` lines, falling back to the conventional path."""
    from scrapper_tool.crawl.robots import RobotsCache  # noqa: PLC0415

    declared = await RobotsCache().sitemaps(seed, client=client)
    if declared:
        return declared
    parts = urlsplit(seed)
    return [f"{parts.scheme or 'https'}://{parts.netloc}/sitemap.xml"]


async def _get_text(client: httpx.AsyncClient, url: str) -> str | None:
    try:
        response = await client.get(url)
    except httpx.HTTPError as exc:
        _logger.info("crawl.map.sitemap_fetch_failed", url=url, error=str(exc)[:120])
        return None
    if response.status_code >= _CLIENT_ERROR_FLOOR:
        return None
    return response.text[:_MAX_SITEMAP_BYTES]


async def map_site(
    seed: str,
    *,
    max_urls: int = _DEFAULT_MAX_URLS,
    same_domain: bool = True,
    include_sitemap: bool = True,
    fetch: Callable[[str], Awaitable[tuple[str, int, str]]] | None = None,
    client: httpx.AsyncClient | None = None,
) -> MapResult:
    """Discover URLs on ``seed``'s site.

    ``fetch`` is how the seed page is retrieved — inject the cascade's ladder or
    render so discovery inherits whatever anti-bot handling the caller has, and
    so this module needs no browser dependency of its own. When omitted, only
    sitemaps are read (no page fetch happens at all).
    """
    ordered: list[str] = []
    seen: set[str] = set()
    from_sitemap = 0
    from_links = 0
    sitemaps_read: list[str] = []

    def add(candidate: str) -> bool:
        if same_domain and not same_site(candidate, seed):
            return False
        if candidate in seen:
            return False
        seen.add(candidate)
        ordered.append(candidate)
        return True

    add(seed)

    if include_sitemap:
        sitemap_urls, sitemaps_read = await fetch_sitemap_urls(seed, client=client)
        for raw in sitemap_urls:
            normalised = normalise_url(raw, seed)
            if normalised and add(normalised):
                from_sitemap += 1

    if fetch is not None:
        try:
            html, _status, final_url = await fetch(seed)
        except Exception as exc:
            _logger.info("crawl.map.seed_fetch_failed", url=seed, error=str(exc)[:160])
        else:
            for link in extract_links(html, final_url or seed):
                if add(link):
                    from_links += 1

    dropped = max(0, len(ordered) - max_urls)
    if dropped:
        # Never silently truncate: a caller planning a crawl needs to know the
        # difference between "the whole site" and "the first page of it".
        _logger.info("crawl.map.truncated", seed=seed, kept=max_urls, dropped=dropped)
    _logger.info(
        "crawl.map.done",
        seed=seed,
        total=min(len(ordered), max_urls),
        from_sitemap=from_sitemap,
        from_links=from_links,
    )
    return MapResult(
        urls=ordered[:max_urls],
        seed=seed,
        from_sitemap=from_sitemap,
        from_links=from_links,
        dropped_by_limit=dropped,
        sitemaps_read=tuple(sitemaps_read),
    )


def make_ladder_fetch(timeout_s: float = 30.0) -> Callable[[str], Awaitable[tuple[str, int, str]]]:
    """Default page fetcher for :func:`map_site` — the TLS-impersonation ladder."""

    async def fetch(url: str) -> tuple[str, int, str]:
        from scrapper_tool.ladder import request_with_ladder  # noqa: PLC0415

        response, _profile = await request_with_ladder("GET", url, timeout=timeout_s)
        return response.text or "", response.status_code, str(response.url)

    return fetch


__all__ = [
    "MapResult",
    "extract_links",
    "fetch_sitemap_urls",
    "make_ladder_fetch",
    "map_site",
    "normalise_url",
    "same_site",
]
