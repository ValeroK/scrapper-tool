"""Unit tests for URL discovery (D2).

The interesting cases are all normalisation: a crawler fed un-normalised links
revisits the same page once per anchor on it, chases image and PDF URLs through
the full cascade, and wanders off-site. Each of those is a wasted browser launch
per bad URL, so they get pinned here rather than discovered in a crawl bill.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from scrapper_tool.crawl.map import (
    extract_links,
    fetch_sitemap_urls,
    map_site,
    normalise_url,
    same_site,
)

_PAGE = """
<html><body>
  <a href="/products/1">One</a>
  <a href="products/2">Two (relative)</a>
  <a href="https://shop.example.com/products/3">Three (subdomain)</a>
  <a href="https://other.test/x">Off-site</a>
  <a href="/products/1#reviews">Same page, anchor</a>
  <a href="mailto:sales@example.com">Mail</a>
  <a href="tel:+1234">Phone</a>
  <a href="javascript:void(0)">JS</a>
  <a href="/brochure.pdf">PDF</a>
  <a href="/logo.png">Image</a>
  <a href="/style.css">CSS</a>
  <a href="">Empty</a>
</body></html>
"""


def _transport(routes: dict[str, tuple[int, str]]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        status, body = routes.get(str(request.url), (404, ""))
        return httpx.Response(status, text=body)

    return httpx.MockTransport(handler)


# --- link extraction --------------------------------------------------------


def test_extracts_and_absolutises_page_links() -> None:
    links = extract_links(_PAGE, "https://example.com/catalog/")
    assert "https://example.com/products/1" in links
    assert "https://example.com/catalog/products/2" in links


def test_fragment_only_duplicates_are_collapsed() -> None:
    """``/p#reviews`` and ``/p`` are one fetch — keeping both revisits the page."""
    links = extract_links(_PAGE, "https://example.com/")
    assert links.count("https://example.com/products/1") == 1


@pytest.mark.parametrize(
    "href",
    ["mailto:a@b.com", "tel:+1", "javascript:void(0)", "data:text/html,x", "about:blank"],
)
def test_non_http_schemes_are_dropped(href: str) -> None:
    assert normalise_url(href, "https://example.com/") is None


@pytest.mark.parametrize("href", ["/a.pdf", "/b.PNG", "/c.css", "/d.woff2", "/e.mp4", "/f.zip"])
def test_asset_urls_are_dropped(href: str) -> None:
    """Each of these would otherwise cost a full cascade run to fetch a binary."""
    assert normalise_url(href, "https://example.com/") is None


def test_extract_links_on_empty_html() -> None:
    assert extract_links("", "https://example.com/") == []


@pytest.mark.parametrize(
    ("url", "seed", "expected"),
    [
        # A site's subdomain split is its own implementation detail.
        ("https://shop.example.com/p", "https://www.example.com/", True),
        ("https://www.example.com/p", "https://example.com/", True),
        ("https://other.test/p", "https://example.com/", False),
        # Suffix matching is label-anchored, so a lookalike host can't sneak in.
        ("https://notexample.com/p", "https://example.com/", False),
        ("https://example.com.evil.test/p", "https://example.com/", False),
        # Seeding a subdomain stays on it.
        ("https://blog.example.com/p", "https://shop.example.com/", False),
        # The public-suffix trap: these must NOT be one site.
        ("https://other.co.il/p", "https://yad2.co.il/", False),
    ],
)
def test_same_site_scope(url: str, seed: str, expected: bool) -> None:
    assert same_site(url, seed) is expected


# --- sitemaps ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_reads_a_flat_sitemap() -> None:
    routes = {
        "https://example.com/robots.txt": (200, "User-agent: *\nDisallow:\n"),
        "https://example.com/sitemap.xml": (
            200,
            "<urlset><url><loc>https://example.com/a</loc></url>"
            "<url><loc>https://example.com/b</loc></url></urlset>",
        ),
    }
    async with httpx.AsyncClient(transport=_transport(routes)) as client:
        urls, read = await fetch_sitemap_urls("https://example.com/", client=client)
    assert urls == ["https://example.com/a", "https://example.com/b"]
    assert read == ["https://example.com/sitemap.xml"]


@pytest.mark.asyncio
async def test_prefers_sitemaps_declared_in_robots_txt() -> None:
    """The canonical location — large sites point at their real index there."""
    routes = {
        "https://example.com/robots.txt": (
            200,
            "User-agent: *\nSitemap: https://example.com/sitemap-products.xml\n",
        ),
        "https://example.com/sitemap-products.xml": (
            200,
            "<urlset><url><loc>https://example.com/p/1</loc></url></urlset>",
        ),
        # Would be found by the fallback path; must not be used when robots
        # declares something else.
        "https://example.com/sitemap.xml": (
            200,
            "<urlset><url><loc>https://example.com/WRONG</loc></url></urlset>",
        ),
    }
    async with httpx.AsyncClient(transport=_transport(routes)) as client:
        urls, read = await fetch_sitemap_urls("https://example.com/", client=client)
    assert urls == ["https://example.com/p/1"]
    assert read == ["https://example.com/sitemap-products.xml"]


@pytest.mark.asyncio
async def test_follows_a_sitemap_index_one_level() -> None:
    routes = {
        "https://example.com/robots.txt": (404, ""),
        "https://example.com/sitemap.xml": (
            200,
            "<sitemapindex><sitemap><loc>https://example.com/sm-1.xml</loc></sitemap>"
            "<sitemap><loc>https://example.com/sm-2.xml</loc></sitemap></sitemapindex>",
        ),
        "https://example.com/sm-1.xml": (
            200,
            "<urlset><url><loc>https://example.com/a</loc></url></urlset>",
        ),
        "https://example.com/sm-2.xml": (
            200,
            "<urlset><url><loc>https://example.com/b</loc></url></urlset>",
        ),
    }
    async with httpx.AsyncClient(transport=_transport(routes)) as client:
        urls, _read = await fetch_sitemap_urls("https://example.com/", client=client)
    assert set(urls) == {"https://example.com/a", "https://example.com/b"}


@pytest.mark.asyncio
async def test_malformed_sitemap_still_yields_the_parseable_urls() -> None:
    """A strict XML parse throws away every URL over one broken tag.

    Real sitemaps are frequently truncated or mis-served, so extraction is
    tag-oriented rather than a document parse.
    """
    routes = {
        "https://example.com/robots.txt": (404, ""),
        "https://example.com/sitemap.xml": (
            200,
            "<urlset><url><loc>https://example.com/a</loc></url>"
            "<url><loc>https://example.com/b</loc><unclosed>",
        ),
    }
    async with httpx.AsyncClient(transport=_transport(routes)) as client:
        urls, _read = await fetch_sitemap_urls("https://example.com/", client=client)
    assert urls == ["https://example.com/a", "https://example.com/b"]


@pytest.mark.asyncio
async def test_missing_sitemap_is_not_an_error() -> None:
    routes = {"https://example.com/robots.txt": (404, "")}
    async with httpx.AsyncClient(transport=_transport(routes)) as client:
        urls, read = await fetch_sitemap_urls("https://example.com/", client=client)
    assert urls == []
    assert read == []


# --- map_site ---------------------------------------------------------------


async def _fake_fetch(url: str) -> tuple[str, int, str]:
    return _PAGE, 200, url


@pytest.mark.asyncio
async def test_map_site_combines_sitemap_and_links() -> None:
    routes = {
        "https://example.com/robots.txt": (404, ""),
        "https://example.com/sitemap.xml": (
            200,
            "<urlset><url><loc>https://example.com/from-sitemap</loc></url></urlset>",
        ),
    }
    async with httpx.AsyncClient(transport=_transport(routes)) as client:
        result = await map_site("https://example.com/", fetch=_fake_fetch, client=client)

    assert "https://example.com/from-sitemap" in result.urls
    assert "https://example.com/products/1" in result.urls
    assert result.from_sitemap == 1
    assert result.from_links >= 2
    assert result.urls[0] == "https://example.com/", "the seed itself is always included"


@pytest.mark.asyncio
async def test_map_site_excludes_other_domains_by_default() -> None:
    result = await map_site("https://example.com/", fetch=_fake_fetch, include_sitemap=False)
    assert not any("other.test" in u for u in result.urls)
    assert "https://shop.example.com/products/3" in result.urls, (
        "a subdomain is part of the same site"
    )


@pytest.mark.asyncio
async def test_map_site_can_cross_domains_when_asked() -> None:
    result = await map_site(
        "https://example.com/", fetch=_fake_fetch, include_sitemap=False, same_domain=False
    )
    assert "https://other.test/x" in result.urls


@pytest.mark.asyncio
async def test_truncation_is_reported_not_silent() -> None:
    """ "200 URLs" and "200 of 40,000" are different answers to plan a crawl on."""
    result = await map_site(
        "https://example.com/", fetch=_fake_fetch, include_sitemap=False, max_urls=2
    )
    assert len(result.urls) == 2
    assert result.truncated is True
    assert result.dropped_by_limit > 0


@pytest.mark.asyncio
async def test_seed_fetch_failure_still_returns_sitemap_urls() -> None:
    """A blocked seed page must not lose the sitemap we already read."""

    async def failing_fetch(url: str) -> tuple[str, int, str]:
        raise RuntimeError("403 from every profile")

    routes = {
        "https://example.com/robots.txt": (404, ""),
        "https://example.com/sitemap.xml": (
            200,
            "<urlset><url><loc>https://example.com/still-here</loc></url></urlset>",
        ),
    }
    async with httpx.AsyncClient(transport=_transport(routes)) as client:
        result = await map_site("https://example.com/", fetch=failing_fetch, client=client)

    assert "https://example.com/still-here" in result.urls
    assert result.from_links == 0


@pytest.mark.asyncio
async def test_no_fetch_function_means_sitemap_only() -> None:
    """No page request at all — useful when you only trust the sitemap."""
    routes = {
        "https://example.com/robots.txt": (404, ""),
        "https://example.com/sitemap.xml": (
            200,
            "<urlset><url><loc>https://example.com/a</loc></url></urlset>",
        ),
    }
    async with httpx.AsyncClient(transport=_transport(routes)) as client:
        result = await map_site("https://example.com/", client=client)
    assert result.urls == ["https://example.com/", "https://example.com/a"]
    assert result.from_links == 0


@pytest.mark.asyncio
async def test_map_result_is_deduplicated() -> None:
    calls: list[str] = []

    async def fetch(url: str) -> tuple[str, int, str]:
        calls.append(url)
        return _PAGE + _PAGE, 200, url  # every link twice

    result = await map_site("https://example.com/", fetch=fetch, include_sitemap=False)
    assert len(result.urls) == len(set(result.urls))
    assert len(calls) == 1


def test_map_result_dict_shape_is_stable() -> None:
    """Guard the payload REST/MCP hand back."""
    from scrapper_tool.crawl.map import MapResult

    result = MapResult(urls=["https://a.test/"], seed="https://a.test/")
    payload: dict[str, Any] = {
        "urls": result.urls,
        "truncated": result.truncated,
        "from_sitemap": result.from_sitemap,
    }
    assert payload == {"urls": ["https://a.test/"], "truncated": False, "from_sitemap": 0}
