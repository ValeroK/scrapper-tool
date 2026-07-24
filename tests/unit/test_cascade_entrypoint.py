"""Unit tests for the unified library entrypoint (F1).

The value of ``scrape()`` is that it's the *same* cascade the REST endpoint runs,
not a parallel one that could drift. So these tests check two things: that the
call delegates to the shared implementation (a schema/mode/interactive flag set
on the call reaches the cascade), and that the "one call handles anything"
promise holds end to end against the real tier logic with only the network faked.
"""

from __future__ import annotations

from typing import Any

import pytest

from scrapper_tool import crawl_site, scrape

_PRODUCT_HTML = (
    '<html><head><script type="application/ld+json">'
    '{"@context":"https://schema.org","@type":"Product","name":"Widget",'
    '"offers":{"@type":"Offer","price":"9.99","priceCurrency":"USD"}}'
    "</script></head><body></body></html>"
)


def _fake_ladder(monkeypatch: pytest.MonkeyPatch, html: str = _PRODUCT_HTML) -> None:
    class _Resp:
        status_code = 200
        text = html
        url = "https://example.com/p"
        headers = {"content-type": "text/html"}

    async def ladder(method: str, url: str, **kwargs: Any) -> tuple[Any, str]:
        _Resp.url = url
        return _Resp(), "chrome146"

    monkeypatch.setattr("scrapper_tool.ladder.request_with_ladder", ladder)


# --- the promise: one call, real cascade ------------------------------------


@pytest.mark.asyncio
async def test_scrape_runs_the_full_cascade_and_wins_at_tier_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake_ladder(monkeypatch)
    result = await scrape("https://example.com/p")
    assert result["pattern_used"] == "a_b_c"
    assert result["product"]["name"] == "Widget"
    assert "escalation_log" in result
    assert result["challenge_detected"] is None


@pytest.mark.asyncio
async def test_scrape_is_the_same_implementation_as_rest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Delegation check: the payload shape is byte-for-byte the endpoint's."""
    from scrapper_tool import http_server

    seen: dict[str, Any] = {}
    real = http_server._do_scrape

    async def spy(req: Any) -> dict[str, Any]:
        seen["url"] = req.url
        seen["mode"] = req.mode
        seen["interactive"] = req.interactive
        seen["schema_json"] = req.schema_json
        return await real(req)

    monkeypatch.setattr(http_server, "_do_scrape", spy)
    _fake_ladder(monkeypatch)

    schema = {"baseSelector": "div", "fields": [{"name": "x", "selector": "p"}]}
    await scrape("https://example.com/p", schema=schema, interactive=True, mode="auto")

    assert seen == {
        "url": "https://example.com/p",
        "mode": "auto",
        "interactive": True,
        "schema_json": schema,
    }


@pytest.mark.asyncio
async def test_scrape_reaches_the_replay_tier(monkeypatch: pytest.MonkeyPatch) -> None:
    """A pre-seeded recipe must be served by scrape() — i.e. it runs tier 0.

    The full learn→replay round trip is proven in test_http_server; here the
    point is only that the library call goes through the same tier-0 replay step,
    not a cascade that skips it.
    """
    from datetime import UTC, datetime

    from scrapper_tool.recipe.derive import Recipe
    from scrapper_tool.recipe.store import cache_key, get_store

    listing = (
        '<html><body><div class="feed">'
        '<div class="feed-item"><h2 class="t">Mazda</h2><span class="p">45,000</span></div>'
        '<div class="feed-item"><h2 class="t">Toyota</h2><span class="p">52,000</span></div>'
        "</div></body></html>"
    )
    _fake_ladder(monkeypatch, html=listing)
    schema = {
        "baseSelector": "div.feed-item",
        "fields": [
            {"name": "title", "selector": "h2.t", "type": "text"},
            {"name": "price", "selector": "span.p", "type": "text"},
        ],
    }
    get_store().put(
        cache_key("https://cars.test/list", schema),
        Recipe(
            domain="cars.test",
            schema=schema,
            source_tier="a_b_c",  # fetch-replayable, no browser needed
            sample_url="https://cars.test/list",
            multi_row=True,
            created_at=datetime.now(UTC).isoformat(),
            schema_hash="h",
            field_names=("title", "price"),
        ),
    )

    result = await scrape("https://cars.test/list", schema=schema)
    assert result["pattern_used"] == "replay"
    assert result["data"] == [
        {"title": "Mazda", "price": "45,000"},
        {"title": "Toyota", "price": "52,000"},
    ]


@pytest.mark.asyncio
async def test_scrape_mode_fetch_stays_at_tier_one(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_ladder(monkeypatch)
    result = await scrape("https://example.com/p", mode="fetch")
    assert result["pattern_used"] == "a_b_c"
    assert result["pattern_attempts"] == ["a_b_c"]


# --- crawl_site -------------------------------------------------------------


@pytest.mark.asyncio
async def test_crawl_site_streams_pages(monkeypatch: pytest.MonkeyPatch) -> None:
    site = {
        "https://site.test/": _PRODUCT_HTML.replace(
            "</body>", '<a href="/a">a</a><a href="/b">b</a></body>'
        ),
        "https://site.test/a": _PRODUCT_HTML,
        "https://site.test/b": _PRODUCT_HTML,
    }

    class _Resp:
        def __init__(self, url: str) -> None:
            self.status_code = 200 if url in site else 404
            self.text = site.get(url, "")
            self.url = url
            self.headers = {"content-type": "text/html"}

    async def ladder(method: str, url: str, **kwargs: Any) -> tuple[Any, str]:
        return _Resp(url), "chrome146"

    monkeypatch.setattr("scrapper_tool.ladder.request_with_ladder", ladder)

    seen: list[str] = []
    # respect_robots=False keeps this test about traversal, not robots.txt (which
    # for the fake host is unreachable and would disallow everything).
    async for page in crawl_site("https://site.test/", depth=1, respect_robots=False):
        seen.append(page.url)
        assert page.ok

    assert set(seen) == set(site)


@pytest.mark.asyncio
async def test_crawl_site_yields_incrementally(monkeypatch: pytest.MonkeyPatch) -> None:
    """It's an async iterator, not a batch — the first page arrives early."""
    _fake_ladder(monkeypatch)

    agen = crawl_site("https://example.com/", depth=0, respect_robots=False)
    first = await agen.__anext__()
    assert first.url == "https://example.com/"
    await agen.aclose()
