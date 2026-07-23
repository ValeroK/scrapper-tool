"""REST /map and /crawl, and their MCP counterparts (D2/D3 surface wiring).

The module-level tests cover the traversal logic; these cover the thing that
actually breaks in practice — the wiring. Specifically that /crawl runs the *real*
cascade per page (so a crawl inherits replay, render, and challenge handling
rather than reimplementing a fetch), and that neither surface returns megabytes of
HTML by default.
"""

from __future__ import annotations

from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from scrapper_tool import http_server
from scrapper_tool import mcp as mcp_module

_SITE: dict[str, str] = {
    "https://site.test/": '<a href="/a">a</a><a href="/b">b</a>',
    "https://site.test/a": "<p>leaf a</p>",
    "https://site.test/b": "<p>leaf b</p>",
}

_PRODUCT_HTML = (
    '<html><head><script type="application/ld+json">'
    '{"@context":"https://schema.org","@type":"Product","name":"Widget",'
    '"offers":{"@type":"Offer","price":"9.99","priceCurrency":"USD"}}'
    "</script></head><body>"
    '<a href="/a">a</a><a href="/b">b</a>'
    "</body></html>"
)


@pytest.fixture
def app_no_auth() -> Any:
    return http_server._build_app(api_key=None, cors_origins=["*"])


def _client(app: Any) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.fixture(autouse=True)
def _allow_all_robots(monkeypatch: pytest.MonkeyPatch) -> None:
    """No network for robots.txt; traversal tests aren't about robots."""
    from scrapper_tool.crawl.robots import RobotsCache

    async def allowed(self: Any, url: str, **_kwargs: Any) -> bool:
        return True

    async def crawl_delay(self: Any, url: str, **_kwargs: Any) -> float:
        return 0.0

    monkeypatch.setattr(RobotsCache, "allowed", allowed)
    monkeypatch.setattr(RobotsCache, "crawl_delay", crawl_delay)


def _serve_site(monkeypatch: pytest.MonkeyPatch, site: dict[str, str] | None = None) -> list[str]:
    """Make the impersonation ladder serve a fixture site. Returns the call log."""
    pages = site if site is not None else _SITE
    calls: list[str] = []

    async def fake_ladder(method: str, url: str, **kwargs: Any) -> Any:
        from unittest.mock import MagicMock

        calls.append(url)
        response = MagicMock()
        response.status_code = 200 if url in pages else 404
        response.text = pages.get(url, "")
        response.url = url
        response.headers = {"content-type": "text/html"}
        return response, "chrome133a"

    monkeypatch.setattr("scrapper_tool.ladder.request_with_ladder", fake_ladder)
    # mcp.py imports the symbol directly, so patching the ladder module alone
    # leaves its binding pointing at the real thing.
    monkeypatch.setattr(mcp_module, "request_with_ladder", fake_ladder, raising=False)
    return calls


# --- /map -------------------------------------------------------------------


class TestMapEndpoint:
    @pytest.mark.asyncio
    async def test_returns_discovered_urls(
        self, app_no_auth: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _serve_site(monkeypatch)

        async with _client(app_no_auth) as client:
            resp = await client.post(
                "/map", json={"url": "https://site.test/", "include_sitemap": False}
            )

        assert resp.status_code == 200
        body = resp.json()
        assert set(body["urls"]) == {
            "https://site.test/",
            "https://site.test/a",
            "https://site.test/b",
        }
        assert body["count"] == 3
        assert body["from_links"] == 2
        assert body["truncated"] is False

    @pytest.mark.asyncio
    async def test_truncation_is_visible_in_the_payload(
        self, app_no_auth: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _serve_site(monkeypatch)

        async with _client(app_no_auth) as client:
            body = (
                await client.post(
                    "/map",
                    json={
                        "url": "https://site.test/",
                        "include_sitemap": False,
                        "max_urls": 2,
                    },
                )
            ).json()

        assert body["count"] == 2
        assert body["truncated"] is True
        assert body["dropped_by_limit"] == 1

    @pytest.mark.asyncio
    async def test_fetch_seed_false_makes_no_page_request(
        self, app_no_auth: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls = _serve_site(monkeypatch)

        async with _client(app_no_auth) as client:
            body = (
                await client.post(
                    "/map",
                    json={
                        "url": "https://site.test/",
                        "include_sitemap": False,
                        "fetch_seed": False,
                    },
                )
            ).json()

        assert calls == []
        assert body["urls"] == ["https://site.test/"]

    @pytest.mark.asyncio
    async def test_rejects_an_out_of_range_limit(self, app_no_auth: Any) -> None:
        async with _client(app_no_auth) as client:
            resp = await client.post("/map", json={"url": "https://site.test/", "max_urls": 0})
        assert resp.status_code == 422


# --- /crawl -----------------------------------------------------------------


class TestCrawlEndpoint:
    @pytest.mark.asyncio
    async def test_crawls_and_extracts_per_page(
        self, app_no_auth: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Each page runs the real cascade, so each page gets real extraction."""
        site = dict.fromkeys(_SITE, _PRODUCT_HTML)
        _serve_site(monkeypatch, site)

        async with _client(app_no_auth) as client:
            resp = await client.post("/crawl", json={"url": "https://site.test/", "depth": 1})

        assert resp.status_code == 200
        body = resp.json()
        assert body["stats"]["visited"] == 3
        assert all(page["ok"] for page in body["pages"])
        assert body["pages"][0]["result"]["pattern_used"] == "a_b_c"
        assert body["pages"][0]["result"]["product"]["name"] == "Widget"

    @pytest.mark.asyncio
    async def test_html_is_omitted_by_default(
        self, app_no_auth: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A 50-page crawl of rendered pages is tens of MB of JSON otherwise."""
        _serve_site(monkeypatch, dict.fromkeys(_SITE, _PRODUCT_HTML))

        async with _client(app_no_auth) as client:
            body = (
                await client.post("/crawl", json={"url": "https://site.test/", "depth": 0})
            ).json()

        result = body["pages"][0]["result"]
        assert "raw_text" not in result
        assert result["product"] is not None, "the extracted data must survive"

    @pytest.mark.asyncio
    async def test_include_html_returns_it(
        self, app_no_auth: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _serve_site(monkeypatch, dict.fromkeys(_SITE, _PRODUCT_HTML))

        async with _client(app_no_auth) as client:
            body = (
                await client.post(
                    "/crawl",
                    json={"url": "https://site.test/", "depth": 0, "include_html": True},
                )
            ).json()

        assert "schema.org" in body["pages"][0]["result"]["raw_text"]

    @pytest.mark.asyncio
    async def test_depth_and_page_bounds_are_reported(
        self, app_no_auth: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _serve_site(monkeypatch, dict.fromkeys(_SITE, _PRODUCT_HTML))

        async with _client(app_no_auth) as client:
            body = (
                await client.post(
                    "/crawl", json={"url": "https://site.test/", "depth": 2, "max_pages": 2}
                )
            ).json()

        assert body["stats"]["visited"] == 2
        assert body["stats"]["hit_page_limit"] is True
        assert body["stats"]["queued_but_unvisited"] >= 1

    @pytest.mark.asyncio
    async def test_a_failing_page_does_not_fail_the_crawl(
        self, app_no_auth: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        site = {
            "https://site.test/": _PRODUCT_HTML.replace('href="/b"', 'href="/missing"'),
            "https://site.test/a": _PRODUCT_HTML,
        }
        _serve_site(monkeypatch, site)

        async with _client(app_no_auth) as client:
            resp = await client.post("/crawl", json={"url": "https://site.test/", "depth": 1})

        assert resp.status_code == 200, "one bad page must not 5xx the whole crawl"
        body = resp.json()
        failed = [p for p in body["pages"] if not p["ok"]]
        assert len(failed) == 1
        assert body["stats"]["failed"] == 1

    @pytest.mark.asyncio
    async def test_a_crawl_learns_a_recipe_once_and_replays_it(
        self, app_no_auth: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The compounding win: page 1 pays for the browser, the rest don't.

        This is why /crawl delegates to the real cascade instead of doing its own
        fetch — everything Phase C built applies per page for free. Here the raw
        fetch returns an unhydrated shell (so tier 1 can't win) and only the
        render tier sees the listing, which is the shape where a recipe pays off
        most across a crawl.
        """
        shell = '<html><body><div id="root"></div>{links}</body></html>'
        listing = (
            '<html><body><div class="feed">'
            '<div class="feed-item"><h2 class="t">Mazda 3</h2><span class="p">45,000</span></div>'
            '<div class="feed-item"><h2 class="t">Toyota</h2><span class="p">52,000</span></div>'
            "</div>{links}</body></html>"
        )
        links = '<a href="/a">a</a><a href="/b">b</a>'
        ladder_calls = _serve_site(monkeypatch, dict.fromkeys(_SITE, shell.format(links=links)))

        renders: list[str] = []
        import scrapper_tool.patterns.render as render_mod

        monkeypatch.setenv("SCRAPPER_TOOL_RENDER_TIER", "1")

        async def fake_render(url: str, **_kwargs: Any) -> Any:
            renders.append(url)
            return render_mod.RenderResult(
                html=listing.format(links=links), status=200, final_url=url
            )

        monkeypatch.setattr(render_mod, "render_html", fake_render)
        monkeypatch.setattr(http_server, "_hostile_available", lambda: False)

        schema = {
            "baseSelector": "div.feed-item",
            "fields": [
                {"name": "title", "selector": "h2.t", "type": "text"},
                {"name": "price", "selector": "span.p", "type": "text"},
            ],
        }

        async with _client(app_no_auth) as client:
            body = (
                await client.post(
                    "/crawl",
                    json={"url": "https://site.test/", "depth": 1, "schema_json": schema},
                )
            ).json()

        used = [p["result"]["pattern_used"] for p in body["pages"] if p["result"]]
        assert used[0] == "render", "the first page pays for the full cascade"
        assert used[1:] == ["replay", "replay"], "later pages ride the learned recipe"
        # This recipe was learned from a rendered DOM, so replaying it still needs
        # a render — the selectors only exist after JS. What replay saves is
        # everything above it: no ladder attempt, no Pattern D, no extraction
        # guesswork, and no possibility of escalating to an LLM.
        assert ladder_calls == ["https://site.test/"], (
            "only the first page should touch the HTTP ladder"
        )
        assert len(renders) == 3
        for page in body["pages"][1:]:
            assert page["result"]["pattern_attempts"] == ["replay"]

    @pytest.mark.asyncio
    async def test_robots_disallow_skips_pages(
        self, app_no_auth: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from scrapper_tool.crawl.robots import RobotsCache

        calls = _serve_site(monkeypatch, dict.fromkeys(_SITE, _PRODUCT_HTML))

        async def allowed(self: Any, url: str, **_kwargs: Any) -> bool:
            return not url.endswith("/b")

        monkeypatch.setattr(RobotsCache, "allowed", allowed)

        async with _client(app_no_auth) as client:
            body = (
                await client.post(
                    "/crawl",
                    json={
                        "url": "https://site.test/",
                        "depth": 1,
                        "respect_robots": True,
                    },
                )
            ).json()

        assert "https://site.test/b" not in calls
        assert body["stats"]["skipped_robots"] == 1
        assert any(p["skipped_reason"] == "robots" for p in body["pages"])

    @pytest.mark.asyncio
    async def test_robots_is_not_consulted_by_default(
        self, app_no_auth: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Enforcement is opt-in, so a default crawl fetches everything."""
        from scrapper_tool.crawl.robots import RobotsCache

        calls = _serve_site(monkeypatch, dict.fromkeys(_SITE, _PRODUCT_HTML))

        async def never(self: Any, url: str, **_kwargs: Any) -> bool:
            raise AssertionError("robots.txt must not be fetched unless asked for")

        monkeypatch.setattr(RobotsCache, "allowed", never)

        async with _client(app_no_auth) as client:
            body = (
                await client.post("/crawl", json={"url": "https://site.test/", "depth": 1})
            ).json()

        assert body["stats"]["skipped_robots"] == 0
        assert sorted(calls) == sorted(_SITE)

    @pytest.mark.asyncio
    async def test_rejects_an_absurd_depth(self, app_no_auth: Any) -> None:
        async with _client(app_no_auth) as client:
            resp = await client.post("/crawl", json={"url": "https://site.test/", "depth": 99})
        assert resp.status_code == 422


# --- MCP parity -------------------------------------------------------------

pytest.importorskip(
    "mcp.server.fastmcp",
    reason="MCP parity tests require the [agent] extra.",
)


def _get_tool(server: object, name: str) -> Any:
    tools = server._tool_manager._tools  # type: ignore[attr-defined]
    return tools[name]


class TestMcpParity:
    @pytest.mark.asyncio
    async def test_map_site_tool(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _serve_site(monkeypatch)
        tool = _get_tool(mcp_module._build_server(), "map_site")
        result = await tool.fn(url="https://site.test/", include_sitemap=False)
        assert set(result["urls"]) == set(_SITE)
        assert result["truncated"] is False

    @pytest.mark.asyncio
    async def test_crawl_site_tool(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _serve_site(monkeypatch, dict.fromkeys(_SITE, _PRODUCT_HTML))
        tool = _get_tool(mcp_module._build_server(), "crawl_site")
        result = await tool.fn(url="https://site.test/", depth=1)
        assert result["stats"]["visited"] == 3
        assert result["pages"][0]["result"]["pattern_used"] == "a_b_c"

    @pytest.mark.asyncio
    async def test_crawl_site_omits_page_bodies(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Page bodies would swamp the agent's context window."""
        _serve_site(monkeypatch, dict.fromkeys(_SITE, _PRODUCT_HTML))
        tool = _get_tool(mcp_module._build_server(), "crawl_site")
        result = await tool.fn(url="https://site.test/", depth=0)
        assert "body" not in result["pages"][0]["result"]
        assert result["pages"][0]["result"]["product"] is not None
