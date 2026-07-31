"""Unit tests for the stealth-browser render tier (`patterns/render.py`).

The render tier is the "rendered HTML, no LLM" seam the cascade escalates to when
the cheap HTTP tier hits a JS wall. Contract exercised:

- Playwright-backed backends (camoufox/patchright/obscura) are driven page-wise
  and return `(html, status, final_url)` plus cookies.
- `BrowserLaunchOptions` (incl. the persistent profile dir) reach the backend.
- `network_idle` / `settle_s` map onto the real Playwright calls.
- The Scrapling backend delegates to Pattern D's fetcher instead.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from scrapper_tool.agent.backends.browser import BrowserLaunchOptions
from scrapper_tool.patterns import render as render_mod


class _FakeResponse:
    status = 203


class _FakePage:
    def __init__(self, html: str = "<html><body>ok</body></html>") -> None:
        self.url = "https://final.example/landed"
        self._html = html
        self.goto = AsyncMock(return_value=_FakeResponse())
        self.wait_for_timeout = AsyncMock()

    async def content(self) -> str:
        return self._html


class _FakeContext:
    def __init__(self, page: _FakePage) -> None:
        self.pages = [page]
        self.cookies = AsyncMock(return_value=[{"name": "cf_clearance", "value": "abc"}])


class _FakeBrowser:
    def __init__(self, page: _FakePage) -> None:
        self.contexts = [_FakeContext(page)]


@pytest.fixture
def fake_render_backend(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Patch the backend resolver so render_html drives a fake Playwright page."""
    page = _FakePage()
    browser = _FakeBrowser(page)
    captured: dict[str, Any] = {}

    class _Backend:
        name = "camoufox"

        async def launch(self, *, options: Any, fingerprint: Any, behavior: Any) -> Any:
            captured["options"] = options

            async def shutdown() -> None:
                captured["closed"] = True

            from scrapper_tool.agent.backends.browser import BrowserHandle

            return BrowserHandle(
                name="camoufox", playwright_browser=browser, raw=browser, shutdown=shutdown
            )

    monkeypatch.setattr(render_mod, "get_browser_backend", lambda name, cdp_url=None: _Backend())
    return {"page": page, "captured": captured}


async def test_render_html_returns_html_status_url(fake_render_backend: dict[str, Any]) -> None:
    result = await render_mod.render_html("https://start.example", settle_s=0)
    assert result.html == "<html><body>ok</body></html>"
    assert result.status == 203
    assert result.final_url == "https://final.example/landed"
    assert result.cookies[0]["name"] == "cf_clearance"
    # browser was closed via the handle
    assert fake_render_backend["captured"]["closed"] is True


async def test_render_html_threads_launch_options(fake_render_backend: dict[str, Any]) -> None:
    opts = BrowserLaunchOptions(user_data_dir="/tmp/prof", headless_mode="virtual")
    await render_mod.render_html("https://x.example", options=opts, settle_s=0)
    passed = fake_render_backend["captured"]["options"]
    assert passed.user_data_dir == "/tmp/prof"
    assert passed.headless_mode == "virtual"


async def test_render_html_network_idle_and_settle(fake_render_backend: dict[str, Any]) -> None:
    page = fake_render_backend["page"]
    await render_mod.render_html("https://x.example", network_idle=True, settle_s=1.5)
    assert page.goto.await_args.kwargs["wait_until"] == "networkidle"
    page.wait_for_timeout.assert_awaited_once_with(1500.0)


async def test_render_html_domcontentloaded_when_not_network_idle(
    fake_render_backend: dict[str, Any],
) -> None:
    page = fake_render_backend["page"]
    await render_mod.render_html("https://x.example", network_idle=False, settle_s=0)
    assert page.goto.await_args.kwargs["wait_until"] == "domcontentloaded"
    page.wait_for_timeout.assert_not_awaited()


async def test_scrapling_backend_delegates_to_pattern_d(monkeypatch: pytest.MonkeyPatch) -> None:
    """browser='scrapling' must go through hostile_client, not the page API."""
    seen: dict[str, Any] = {}

    class _Resp:
        html_content = "<html>scrapling</html>"
        status = 200
        url = "https://d.example/final"

    class _Fetcher:
        async def async_fetch(self, url: str, **kwargs: Any) -> Any:
            seen["url"] = url
            seen["kwargs"] = kwargs
            return _Resp()

    class _Ctx:
        async def __aenter__(self) -> Any:
            return _Fetcher()

        async def __aexit__(self, *a: Any) -> None:
            return None

    import scrapper_tool.patterns.d as d_mod

    monkeypatch.setattr(d_mod, "hostile_client", lambda **kw: _Ctx())

    result = await render_mod.render_html(
        "https://d.example",
        browser="scrapling",
        options=BrowserLaunchOptions(user_data_dir="/tmp/p"),
    )
    assert result.html == "<html>scrapling</html>"
    assert result.final_url == "https://d.example/final"
    # the persistent profile dir is forwarded so clearance cookies carry over
    assert seen["kwargs"]["user_data_dir"] == "/tmp/p"
    assert seen["kwargs"]["solve_cloudflare"] is True


# --- proxy rotation (D1) --------------------------------------------------


async def test_render_uses_pool_proxy_when_none_pinned(
    fake_render_backend: dict[str, Any],
) -> None:
    """A stealth browser on a burned IP still gets walled — the browser tier
    needs the IP dimension too, not just the HTTP ladder."""
    from scrapper_tool.proxy import ProxyPool

    pool = ProxyPool.from_urls(["http://p1:1"])
    await render_mod.render_html("https://x.example", settle_s=0, proxy_pool=pool)
    assert fake_render_backend["captured"]["options"].proxy == "http://p1:1"
    # 203 is not a block status, so the proxy stays healthy.
    assert pool.entries[0].successes == 1


async def test_render_marks_proxy_blocked_on_403(
    fake_render_backend: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    from scrapper_tool.proxy import ProxyPool

    class _Blocked:
        status = 403

    fake_render_backend["page"].goto = AsyncMock(return_value=_Blocked())
    pool = ProxyPool.from_urls(["http://p1:1"])

    result = await render_mod.render_html("https://x.example", settle_s=0, proxy_pool=pool)
    assert result.status == 403
    assert pool.entries[0].failures == 1
    assert pool.available_count() == 0  # cooling down


async def test_render_pinned_proxy_beats_pool(fake_render_backend: dict[str, Any]) -> None:
    from scrapper_tool.agent.backends.browser import BrowserLaunchOptions as _Opts
    from scrapper_tool.proxy import ProxyPool

    pool = ProxyPool.from_urls(["http://pool:1"])
    await render_mod.render_html(
        "https://x.example",
        settle_s=0,
        options=_Opts(proxy="http://pinned:1"),
        proxy_pool=pool,
    )
    assert fake_render_backend["captured"]["options"].proxy == "http://pinned:1"
    assert pool.entries[0].successes == 0  # pinned choice isn't pool-managed


class _FakePersistentContext:
    """Camoufox with ``user_data_dir`` returns a BrowserContext, not a Browser.

    Deliberately exposes neither ``.contexts`` nor ``.new_context`` — that is what
    a real Playwright ``BrowserContext`` looks like, and what made the old
    ``browser.contexts[0] if ... else await browser.new_context()`` idiom raise.
    """

    def __init__(self, page: _FakePage) -> None:
        self.pages = [page]
        self.cookies = AsyncMock(return_value=[{"name": "cf_clearance", "value": "persisted"}])


@pytest.fixture
def fake_persistent_backend(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Backend whose ``playwright_browser`` is a *context*, as Camoufox returns."""
    page = _FakePage()
    context = _FakePersistentContext(page)

    class _Backend:
        name = "camoufox"

        async def launch(self, *, options: Any, fingerprint: Any, behavior: Any) -> Any:
            from scrapper_tool.agent.backends.browser import BrowserHandle

            async def shutdown() -> None:
                return None

            return BrowserHandle(
                name="camoufox", playwright_browser=context, raw=context, shutdown=shutdown
            )

    monkeypatch.setattr(render_mod, "get_browser_backend", lambda name, cdp_url=None: _Backend())
    return {"page": page, "context": context}


async def test_render_html_handles_camoufox_persistent_context(
    fake_persistent_backend: dict[str, Any],
) -> None:
    """Regression: a persistent-profile render used to raise AttributeError.

    ``persistent_context=True`` (set whenever ``user_data_dir`` is present) makes
    Camoufox return a BrowserContext. The cascade sets that dir on every
    ``mode="auto"`` run once ``[hostile]`` is installed, so this was the default
    path — and it failed before ``resolve_context`` existed.
    """
    result = await render_mod.render_html(
        "https://x.example",
        options=BrowserLaunchOptions(user_data_dir="/tmp/prof"),
        settle_s=0,
    )
    assert result.html == "<html><body>ok</body></html>"
    assert result.status == 203
    assert result.cookies[0]["value"] == "persisted"


class TestResolveContext:
    """The Browser-or-BrowserContext normalizer used by every Playwright tier."""

    async def test_prefers_existing_context(self) -> None:
        from scrapper_tool.agent.backends.browser import resolve_context

        page = _FakePage()
        browser = _FakeBrowser(page)
        assert await resolve_context(browser) is browser.contexts[0]

    async def test_creates_one_when_browser_has_none(self) -> None:
        from scrapper_tool.agent.backends.browser import resolve_context

        made = _FakeContext(_FakePage())

        class _Empty:
            contexts: list[Any] = []
            new_context = AsyncMock(return_value=made)

        browser = _Empty()
        assert await resolve_context(browser) is made
        browser.new_context.assert_awaited_once()

    async def test_returns_a_bare_context_unchanged(self) -> None:
        """The Camoufox persistent case — no .contexts, no .new_context."""
        from scrapper_tool.agent.backends.browser import resolve_context

        ctx = _FakePersistentContext(_FakePage())
        assert await resolve_context(ctx) is ctx
