"""Cookies reaching the tiers — P4 threading, and the guardrails around it.

Three separate claims are tested here, because they fail in different ways:

1. **Cookies actually arrive.** The render tier calls ``add_cookies`` *before*
   ``goto``, and the HTTP ladder loads a real jar rather than a static header.
2. **They arrive scoped.** Only cookies matching the URL are sent, on every
   path.
3. **They never leak.** Not into a response body, not into a log record, not
   into ``repr``, and not through an untrusted proxy pool or an
   unauthenticated sidecar.

Claim 3 is the one worth the most care: a bug there is a credential disclosure,
and unlike a scraping bug it is silent.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest
from pydantic import SecretStr

from scrapper_tool import cookies as cookies_mod
from scrapper_tool.cookies import CookieIn
from scrapper_tool.ladder import IMPERSONATE_LADDER

SECRET = "SESSION-TOKEN-DO-NOT-LEAK"


def cookie(domain: str = "example.com", name: str = "session", **kw: Any) -> CookieIn:
    return CookieIn(name=name, value=SecretStr(SECRET), domain=domain, **kw)


# ---------------------------------------------------------------------------
# Render tier
# ---------------------------------------------------------------------------


class _FakePage:
    def __init__(self) -> None:
        self.url = "https://example.com/"
        self.goto_calls: list[str] = []

    async def goto(self, url: str, **_kw: Any) -> Any:
        self.goto_calls.append(url)
        return type("Resp", (), {"status": 200})()

    async def wait_for_timeout(self, _ms: float) -> None:
        return None

    async def content(self) -> str:
        return "<html><body>ok</body></html>"


class _FakeContext:
    """Duck-types a Playwright BrowserContext and records call ordering."""

    def __init__(self) -> None:
        self.page = _FakePage()
        self.pages = [self.page]
        self.added: list[dict[str, Any]] = []
        self.order: list[str] = []

    async def add_cookies(self, entries: list[dict[str, Any]]) -> None:
        self.order.append("add_cookies")
        self.added.extend(entries)

    async def cookies(self) -> list[dict[str, Any]]:
        return []


@pytest.fixture
def fake_render(monkeypatch: pytest.MonkeyPatch) -> _FakeContext:
    """Drive render_html against a fake browser that records what it received."""
    from contextlib import asynccontextmanager

    from scrapper_tool.patterns import render as render_mod

    context = _FakeContext()

    original_goto = context.page.goto

    async def _tracking_goto(url: str, **kw: Any) -> Any:
        context.order.append("goto")
        return await original_goto(url, **kw)

    context.page.goto = _tracking_goto  # type: ignore[method-assign]

    class _Handle:
        name = "camoufox"
        playwright_browser = object()

    @asynccontextmanager
    async def _open_browser(*_a: Any, **_kw: Any) -> Any:
        yield _Handle()

    async def _resolve_context(_browser: Any) -> _FakeContext:
        return context

    monkeypatch.setattr(render_mod, "open_browser", _open_browser)
    monkeypatch.setattr(render_mod, "resolve_context", _resolve_context)
    monkeypatch.setattr(render_mod, "get_browser_backend", lambda *a, **k: object())
    monkeypatch.setattr(render_mod, "resolve_proxy", lambda pool, proxy: (None, None))
    return context


class TestRenderTier:
    @pytest.mark.asyncio
    async def test_cookies_are_injected_before_navigation(self, fake_render: _FakeContext) -> None:
        """The request that decides logged-in vs logged-out is goto's."""
        from scrapper_tool.patterns.render import render_html

        await render_html("https://example.com/x", cookies=[cookie()], settle_s=0)
        assert fake_render.order.index("add_cookies") < fake_render.order.index("goto")

    @pytest.mark.asyncio
    async def test_only_matching_cookies_are_injected(self, fake_render: _FakeContext) -> None:
        from scrapper_tool.patterns.render import render_html

        await render_html(
            "https://example.com/x",
            cookies=[cookie(name="keep"), cookie(domain="evil-example.com", name="drop")],
            settle_s=0,
        )
        assert [c["name"] for c in fake_render.added] == ["keep"]

    @pytest.mark.asyncio
    async def test_no_cookies_means_no_add_cookies_call(self, fake_render: _FakeContext) -> None:
        from scrapper_tool.patterns.render import render_html

        await render_html("https://example.com/x", settle_s=0)
        assert fake_render.added == []
        assert "add_cookies" not in fake_render.order

    @pytest.mark.asyncio
    async def test_injection_uses_playwright_shape(self, fake_render: _FakeContext) -> None:
        from scrapper_tool.patterns.render import render_html

        await render_html("https://example.com/x", cookies=[cookie(http_only=True)], settle_s=0)
        entry = fake_render.added[0]
        assert entry["httpOnly"] is True
        assert entry["value"] == SECRET

    @pytest.mark.asyncio
    async def test_render_does_not_log_cookie_values(
        self, fake_render: _FakeContext, caplog: pytest.LogCaptureFixture
    ) -> None:
        from scrapper_tool.patterns.render import render_html

        with caplog.at_level(logging.DEBUG):
            await render_html("https://example.com/x", cookies=[cookie()], settle_s=0)
        assert SECRET not in caplog.text


# ---------------------------------------------------------------------------
# HTTP ladder
# ---------------------------------------------------------------------------


class _FakeJar:
    def __init__(self) -> None:
        self.entries: list[dict[str, Any]] = []

    def set(self, name: str, value: str, **kw: Any) -> None:
        self.entries.append({"name": name, "value": value, **kw})


class TestLadderTier:
    def test_cookies_go_into_the_jar_with_domain_scope(self) -> None:
        """A static Cookie: header would survive a cross-domain redirect. A jar won't."""
        from scrapper_tool.ladder import _load_cookie_jar

        session = type("S", (), {"cookies": _FakeJar()})()
        _load_cookie_jar(session, [cookie(path="/app")])
        entry = session.cookies.entries[0]
        assert entry["domain"] == "example.com"
        assert entry["path"] == "/app"
        assert entry["value"] == SECRET

    def test_a_broken_jar_does_not_fail_the_fetch(self, caplog: pytest.LogCaptureFixture) -> None:
        """A lost login should degrade to a public-content fetch, not an exception."""
        from scrapper_tool.ladder import _load_cookie_jar

        class _AngryJar:
            def set(self, *_a: Any, **_kw: Any) -> None:
                msg = "jar exploded"
                raise RuntimeError(msg)

        session = type("S", (), {"cookies": _AngryJar()})()
        with caplog.at_level(logging.DEBUG):
            _load_cookie_jar(session, [cookie()])
        assert SECRET not in caplog.text

    def test_session_without_a_jar_is_tolerated(self) -> None:
        from scrapper_tool.ladder import _load_cookie_jar

        session = type("S", (), {"cookies": None})()
        _load_cookie_jar(session, [cookie()])  # must not raise


# ---------------------------------------------------------------------------
# Request plumbing and reporting
# ---------------------------------------------------------------------------


class _Req:
    """Minimal stand-in for ScrapeRequest as the helpers see it."""

    def __init__(self, cookies: list[CookieIn] | None = None) -> None:
        self.cookies = cookies
        self.url = "https://example.com/"


class TestRequestJar:
    def test_absent_cookies_yield_none_not_empty_list(self) -> None:
        from scrapper_tool.http_server import _request_cookies

        assert _request_cookies(_Req()) is None

    def test_jar_is_stashed_once_and_reused(self) -> None:
        from scrapper_tool.http_server import _request_cookies

        req = _Req([cookie()])
        first = _request_cookies(req)
        assert first is _request_cookies(req)

    def test_applied_and_skipped_are_recorded_without_duplicates(self) -> None:
        from scrapper_tool.http_server import _mark_cookies_applied, _mark_cookies_skipped

        req = _Req([cookie()])
        _mark_cookies_applied(req, "a_b_c")
        _mark_cookies_applied(req, "a_b_c")
        _mark_cookies_applied(req, "render")
        _mark_cookies_skipped(req, "e1", "obscura_external_browser")
        _mark_cookies_skipped(req, "e1", "something_else")
        assert req.__dict__["_cookies_applied"] == ["a_b_c", "render"]
        assert req.__dict__["_cookies_skipped"] == [
            {"tier": "e1", "reason": "obscura_external_browser"}
        ]

    def test_marking_is_a_noop_when_no_cookies_were_supplied(self) -> None:
        from scrapper_tool.http_server import _mark_cookies_applied

        req = _Req()
        _mark_cookies_applied(req, "a_b_c")
        assert "_cookies_applied" not in req.__dict__


class TestUntrustedProxyGuard:
    def test_refuses_credentialed_traffic_over_a_free_proxy_pool(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """assert_safe_for_credentials existed with zero call sites. Now it has one."""
        from scrapper_tool.errors import ConfigurationError
        from scrapper_tool.http_server import _assert_cookies_safe_to_send
        from scrapper_tool.proxy import ProxyPool

        monkeypatch.setattr(
            ProxyPool,
            "from_env",
            classmethod(
                lambda cls, **kw: ProxyPool.from_urls(["http://free.example"], untrusted=True)
            ),
        )
        with pytest.raises(ConfigurationError, match="untrusted"):
            _assert_cookies_safe_to_send(_Req([cookie()]))

    def test_allows_a_trusted_pool(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from scrapper_tool.http_server import _assert_cookies_safe_to_send
        from scrapper_tool.proxy import ProxyPool

        monkeypatch.setattr(
            ProxyPool,
            "from_env",
            classmethod(lambda cls, **kw: ProxyPool.from_urls(["http://paid.example"])),
        )
        _assert_cookies_safe_to_send(_Req([cookie()]))

    def test_no_cookies_means_the_guard_never_fires(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Anonymous scraping over a free pool stays perfectly legal."""
        from scrapper_tool.http_server import _assert_cookies_safe_to_send
        from scrapper_tool.proxy import ProxyPool

        monkeypatch.setattr(
            ProxyPool,
            "from_env",
            classmethod(
                lambda cls, **kw: ProxyPool.from_urls(["http://free.example"], untrusted=True)
            ),
        )
        _assert_cookies_safe_to_send(_Req())


class TestNoLeakIntoTheApiSurface:
    def test_scrape_request_masks_values_in_json(self) -> None:
        from scrapper_tool.http_server import ScrapeRequest

        req = ScrapeRequest(url="https://example.com", cookies=[cookie()])
        assert SECRET not in req.model_dump_json()

    def test_scrape_request_masks_values_in_repr(self) -> None:
        from scrapper_tool.http_server import ScrapeRequest

        req = ScrapeRequest(url="https://example.com", cookies=[cookie()])
        assert SECRET not in repr(req)

    def test_redacted_view_is_what_reporting_uses(self) -> None:
        redacted = cookies_mod.redact([cookie()])
        assert SECRET not in str(redacted)
        assert redacted[0]["name"] == "session"


# ---------------------------------------------------------------------------
# The REST gate
# ---------------------------------------------------------------------------


_PRODUCT_HTML = (
    '<html><head><script type="application/ld+json">'
    '{"@context":"https://schema.org","@type":"Product","name":"Widget",'
    '"offers":{"@type":"Offer","price":"9.99","priceCurrency":"USD"}}'
    "</script></head><body><p>ok</p></body></html>"
)


def _serve_a_page(monkeypatch: pytest.MonkeyPatch) -> None:
    """Let A/B/C win so the gate tests never escalate into a browser tier.

    These tests are about the 403 gate, not about scraping, but they drive the
    whole ``/scrape`` cascade to reach it. Without a stand-in ladder that means a
    real curl-cffi fetch of ``example.com`` and — when the fetch doesn't satisfy
    the classifier — a real browser in Pattern D or E1. ``tests/unit`` is
    hermetic by contract (see ``tests/conftest.py``), and the assertions here
    (``status_code != 403``) are loose enough that the leak was invisible.
    """
    from unittest.mock import MagicMock

    async def fake_ladder(method: str, url: str, **_kwargs: Any) -> Any:
        response = MagicMock()
        response.status_code = 200
        response.text = _PRODUCT_HTML
        response.url = url
        response.headers = {"content-type": "text/html"}
        return response, IMPERSONATE_LADDER[0]

    monkeypatch.setattr("scrapper_tool.ladder.request_with_ladder", fake_ladder)


class TestUnauthenticatedCookieGate:
    """`/scrape` is open by default. That must stop being true for credentials."""

    @staticmethod
    def _body() -> dict[str, Any]:
        return {
            "url": "https://example.com",
            "cookies": [{"name": "session", "value": SECRET, "domain": "example.com"}],
        }

    @pytest.mark.asyncio
    async def test_cookies_without_an_api_key_are_403(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from httpx import ASGITransport, AsyncClient

        from scrapper_tool import http_server

        monkeypatch.delenv("SCRAPPER_TOOL_HTTP_ALLOW_UNAUTH_COOKIES", raising=False)
        app = http_server._build_app(api_key=None, cors_origins=["*"])
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post("/scrape", json=self._body())
        assert resp.status_code == 403
        assert "SCRAPPER_TOOL_HTTP_API_KEY" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_the_403_body_never_echoes_the_cookie(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from httpx import ASGITransport, AsyncClient

        from scrapper_tool import http_server

        monkeypatch.delenv("SCRAPPER_TOOL_HTTP_ALLOW_UNAUTH_COOKIES", raising=False)
        app = http_server._build_app(api_key=None, cors_origins=["*"])
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post("/scrape", json=self._body())
        assert SECRET not in resp.text

    @pytest.mark.asyncio
    async def test_the_localhost_escape_hatch_allows_it(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from httpx import ASGITransport, AsyncClient

        from scrapper_tool import http_server

        monkeypatch.setenv("SCRAPPER_TOOL_HTTP_ALLOW_UNAUTH_COOKIES", "1")
        monkeypatch.setattr(http_server, "_hostile_available", lambda: False)
        _serve_a_page(monkeypatch)
        app = http_server._build_app(api_key=None, cors_origins=["*"])
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post("/scrape", json=self._body())
        assert resp.status_code != 403

    @pytest.mark.asyncio
    async def test_a_request_without_cookies_is_unaffected(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The gate must not break the open-by-default anonymous path."""
        from httpx import ASGITransport, AsyncClient

        from scrapper_tool import http_server

        monkeypatch.delenv("SCRAPPER_TOOL_HTTP_ALLOW_UNAUTH_COOKIES", raising=False)
        monkeypatch.setattr(http_server, "_hostile_available", lambda: False)
        _serve_a_page(monkeypatch)
        app = http_server._build_app(api_key=None, cors_origins=["*"])
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post("/scrape", json={"url": "https://example.com"})
        assert resp.status_code != 403


# ---------------------------------------------------------------------------
# Harvest-forward (P5)
# ---------------------------------------------------------------------------


class TestHarvestForward:
    """A cf_clearance won by an expensive render used to be dropped on the floor."""

    @staticmethod
    def _rows() -> list[dict[str, Any]]:
        return [
            {
                "name": "cf_clearance",
                "value": "CLEARANCE-TOKEN",
                "domain": "example.com",
                "path": "/",
                "secure": True,
            }
        ]

    def test_harvested_cookies_enter_the_request_jar(self) -> None:
        from scrapper_tool.http_server import _harvest_cookies, _request_cookies

        req = _Req()
        _harvest_cookies(req, self._rows(), tier="render")
        jar = _request_cookies(req)
        assert jar is not None
        assert [c.name for c in jar] == ["cf_clearance"]

    def test_caller_cookies_and_harvested_cookies_coexist(self) -> None:
        from scrapper_tool.http_server import _harvest_cookies, _request_cookies

        req = _Req([cookie(name="session")])
        _request_cookies(req)  # materialise the jar from the caller's cookies first
        _harvest_cookies(req, self._rows(), tier="render")
        jar = _request_cookies(req)
        assert jar is not None
        assert {c.name for c in jar} == {"session", "cf_clearance"}

    def test_harvest_records_the_contributing_tier(self) -> None:
        from scrapper_tool.http_server import _harvest_cookies

        req = _Req()
        _harvest_cookies(req, self._rows(), tier="render")
        _harvest_cookies(req, self._rows(), tier="render")
        assert req.__dict__["_cookies_harvested_from"] == ["render"]

    def test_empty_harvest_is_a_noop(self) -> None:
        from scrapper_tool.http_server import _harvest_cookies

        req = _Req()
        _harvest_cookies(req, [], tier="render")
        assert "_cookies_harvested_from" not in req.__dict__

    def test_malformed_rows_do_not_fail_the_tier(self) -> None:
        """A bad row from a browser must not turn a successful render into an error."""
        from scrapper_tool.http_server import _harvest_cookies

        req = _Req()
        _harvest_cookies(req, [{"nonsense": True}], tier="render")
        assert "_cookies_harvested_from" not in req.__dict__

    def test_later_harvest_supersedes_an_earlier_value(self) -> None:
        from scrapper_tool.http_server import _harvest_cookies, _request_cookies

        req = _Req()
        _harvest_cookies(req, self._rows(), tier="render")
        refreshed = self._rows()
        refreshed[0]["value"] = "FRESHER-TOKEN"
        _harvest_cookies(req, refreshed, tier="d")
        jar = _request_cookies(req)
        assert jar is not None
        assert jar[0].value.get_secret_value() == "FRESHER-TOKEN"

    def test_harvest_never_logs_a_value(self, caplog: pytest.LogCaptureFixture) -> None:
        from scrapper_tool.http_server import _harvest_cookies

        with caplog.at_level(logging.DEBUG):
            _harvest_cookies(_Req(), self._rows(), tier="render")
        assert "CLEARANCE-TOKEN" not in caplog.text

    def test_nothing_is_written_to_the_recipe_store(self, tmp_path: Any) -> None:
        """Cookies are per-identity; that store is keyed by domain. Never persist."""
        from scrapper_tool.http_server import _harvest_cookies
        from scrapper_tool.recipe.store import default_cache_dir

        req = _Req()
        _harvest_cookies(req, self._rows(), tier="render")
        cache_dir = default_cache_dir()
        blob = ""
        if cache_dir.is_dir():
            for path in cache_dir.rglob("*"):
                if path.is_file():
                    blob += path.read_text(encoding="utf-8", errors="ignore")
        assert "CLEARANCE-TOKEN" not in blob
