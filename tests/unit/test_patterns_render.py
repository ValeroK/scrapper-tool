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
    def __init__(
        self, html: str = "<html><body>ok</body></html>", order: list[str] | None = None
    ) -> None:
        self.url = "https://final.example/landed"
        self._html = html
        self._order = order if order is not None else []

        async def _goto(*_a: Any, **_k: Any) -> _FakeResponse:
            self._order.append("goto")
            return _FakeResponse()

        self.goto = AsyncMock(side_effect=_goto)
        self.wait_for_timeout = AsyncMock()

    async def content(self) -> str:
        return self._html


class _FakeRoute:
    """Records whether a request was let through or aborted."""

    def __init__(self) -> None:
        self.outcome: str | None = None

    async def continue_(self) -> None:
        self.outcome = "continue"

    async def abort(self) -> None:
        self.outcome = "abort"


class _FakeRequest:
    def __init__(self, url: str, resource_type: str = "document") -> None:
        self.url = url
        self.resource_type = resource_type


class _FakeContext:
    def __init__(self, page: _FakePage, order: list[str] | None = None) -> None:
        self.pages = [page]
        self.cookies = AsyncMock(return_value=[{"name": "cf_clearance", "value": "abc"}])
        self.routes: list[tuple[Any, Any]] = []
        self._order = order if order is not None else []

    async def route(self, pattern: Any, handler: Any) -> None:
        self._order.append("route")
        self.routes.append((pattern, handler))


class _FakeBrowser:
    def __init__(self, page: _FakePage, order: list[str] | None = None) -> None:
        self.contexts = [_FakeContext(page, order)]


@pytest.fixture
def fake_render_backend(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Patch the backend resolver so render_html drives a fake Playwright page."""
    order: list[str] = []
    page = _FakePage(order=order)
    browser = _FakeBrowser(page, order)
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
    return {"page": page, "captured": captured, "browser": browser, "order": order}


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


class TestRenderUrlGuard:
    """The render tier aborts refused requests instead of merely hiding them.

    A pre-flight check cannot see where a 302 sends the browser, and a
    post-flight check on the final URL only lets us decline to return the body
    -- by which point the request happened. Routing is the only hook that stops
    it, and it has to be installed before the first navigation.
    """

    @pytest.mark.asyncio
    async def test_route_is_registered_before_the_first_goto(
        self, fake_render_backend: dict[str, Any]
    ) -> None:
        """The whole point: a route installed after goto misses the navigation."""
        await render_mod.render_html("https://start.example", settle_s=0)
        order = fake_render_backend["order"]
        assert "route" in order, "no route was installed; the render is unguarded"
        assert order.index("route") < order.index("goto"), f"route must precede goto, got {order}"

    @pytest.mark.asyncio
    async def test_pattern_is_scheme_anchored_not_catch_all(
        self, fake_render_backend: dict[str, Any]
    ) -> None:
        """`**/*` also matches about:blank and strands the browser.

        Learned the hard way in this repo once already: a catch-all pattern
        aborted the internal navigation and turned an intermittent failure into
        a deterministic 30s timeout.
        """
        await render_mod.render_html("https://start.example", settle_s=0)
        pattern, _handler = fake_render_backend["browser"].contexts[0].routes[0]
        assert pattern.search("https://example.com/x")
        assert pattern.search("http://example.com/x")
        assert not pattern.search("about:blank"), "internal navigation must not be routed"
        assert not pattern.search("data:text/html,x")

    @pytest.mark.asyncio
    async def test_handler_aborts_a_private_target(
        self, fake_render_backend: dict[str, Any]
    ) -> None:
        await render_mod.render_html("https://start.example", settle_s=0)
        _pattern, handler = fake_render_backend["browser"].contexts[0].routes[0]

        route = _FakeRoute()
        await handler(route, _FakeRequest("http://169.254.169.254/latest/meta-data/"))
        assert route.outcome == "abort"

    @pytest.mark.asyncio
    async def test_handler_allows_a_public_target(
        self, fake_render_backend: dict[str, Any]
    ) -> None:
        await render_mod.render_html("https://start.example", settle_s=0)
        _pattern, handler = fake_render_backend["browser"].contexts[0].routes[0]

        route = _FakeRoute()
        await handler(route, _FakeRequest("https://example.com/page"))
        assert route.outcome == "continue"

    @pytest.mark.asyncio
    async def test_page_initiated_subresources_are_the_surface_this_closes(
        self, fake_render_backend: dict[str, Any]
    ) -> None:
        """A page can aim the browser at anything it names; that is the SSRF here.

        Confirmed against a real Camoufox: an <img> at the metadata endpoint, an
        <iframe> at 10.0.0.1 and a fetch() at 127.0.0.53 were all aborted, and
        the page still rendered.
        """
        await render_mod.render_html("https://start.example", settle_s=0)
        _pattern, handler = fake_render_backend["browser"].contexts[0].routes[0]

        for url, kind in (
            ("http://169.254.169.254/latest/meta-data/", "image"),
            ("http://10.0.0.1/admin", "document"),
            ("http://127.0.0.53/internal", "xhr"),
        ):
            route = _FakeRoute()
            await handler(route, _FakeRequest(url, kind))
            assert route.outcome == "abort", f"{kind} at {url} was let through"

    def test_navigation_redirect_hops_are_a_known_gap(self) -> None:
        """Playwright's route does NOT fire for redirect targets.

        Encoded as a test rather than left in prose because it is the kind of
        limitation that quietly gets forgotten and then over-claimed. Measured:
        with a local redirector pointing at the metadata endpoint, the handler
        saw only the seed URL and the browser attempted the metadata connection
        itself. Navigation redirects stay blind SSRF on this tier, caught by the
        cascade's post-flight check on the final URL and nothing earlier.

        If a future change makes ``route`` see redirect hops -- a Playwright
        behaviour change, or a switch to ``route.fetch(max_redirects=0)`` --
        this test should be replaced by one asserting the hop is aborted, and
        the docstrings and SETTINGS matrix updated to match.
        """
        source = render_mod._install_url_guard.__doc__ or ""
        assert "does not close" in source.lower(), (
            "the redirect-hop limitation must stay documented where the code is"
        )

    @pytest.mark.asyncio
    async def test_documents_are_not_exempt(self, fake_render_backend: dict[str, Any]) -> None:
        """An <iframe src> IS a document, so exempting documents waves through
        exactly the third-party frames worth stopping."""
        await render_mod.render_html("https://start.example", settle_s=0)
        _pattern, handler = fake_render_backend["browser"].contexts[0].routes[0]

        for resource_type in ("document", "xhr", "script", "image"):
            route = _FakeRoute()
            await handler(route, _FakeRequest("http://10.0.0.1/x", resource_type))
            assert route.outcome == "abort", f"{resource_type} was let through"

    @pytest.mark.asyncio
    async def test_no_route_installed_when_the_guard_is_off(
        self, fake_render_backend: dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SCRAPPER_TOOL_URL_GUARD", "0")
        await render_mod.render_html("https://start.example", settle_s=0)
        assert fake_render_backend["browser"].contexts[0].routes == []

    @pytest.mark.asyncio
    async def test_a_context_without_route_is_reported_not_silently_unguarded(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A backend that cannot be hooked must say so, not pretend it was."""

        class _NoRouteContext:
            """Stands in for a backend whose context exposes no route()."""

        with caplog.at_level("WARNING"):
            installed = await render_mod._install_url_guard(_NoRouteContext(), "https://x.example")
        assert installed is False
        assert any("guard_unavailable" in r.getMessage() for r in caplog.records)
