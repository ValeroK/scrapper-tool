"""Cookies reaching each cascade tier — D, E1, E2 and replay.

Companion to ``test_cookies_threading.py``, which covers the render and HTTP
ladder tiers. Split into its own module because it exists to close a specific
hole rather than to re-test the same ground.

**Every test here drives a real tier function.** That is the point. The original
threading work unit-tested ``_mark_cookies_applied`` and ``_mark_cookies_skipped``
by calling them directly, which passed while no tier in ``src/`` ever called
them — so four of six tiers ran logged-out, reported ``cookies_applied: []`` and
``cookies_skipped: []``, and gave the caller no signal at all. A test that pokes
the bookkeeping helper cannot catch that; a test that drives the tier can.

The three tiers carry a session by three different mechanisms, which is why they
need separate coverage rather than one parametrised case:

* **D** passes ``cookies`` to Scrapling's ``StealthyFetcher``. Its ``__init__``
  is ``(*args, **kwargs)``, so nothing is knowable statically and the
  kwarg-rejected path is real.
* **E1** lets Crawl4AI launch its own browser, so the session rides on
  ``BrowserConfig(cookies=...)``.
* **E2** attaches over CDP to a browser we already launched, so cookies go onto
  the live context — no browser-use kwarg is involved.
"""

from __future__ import annotations

import sys
import types
from typing import Any

import pytest
from pydantic import SecretStr

from scrapper_tool.cookies import CookieIn

SECRET = "SESSION-TOKEN-DO-NOT-LEAK"


def cookie(domain: str = "example.com", name: str = "session", **kw: Any) -> CookieIn:
    return CookieIn(name=name, value=SecretStr(SECRET), domain=domain, **kw)


class _TierReq:
    """Stand-in for ScrapeRequest as the tier functions see it."""

    def __init__(
        self,
        cookies: list[CookieIn] | None = None,
        url: str = "https://example.com/dashboard",
    ) -> None:
        self.cookies = cookies
        self.url = url
        self.timeout_s = 5.0
        self.pattern_d_network_idle = False
        self.solve_cloudflare = False
        self.mode = "auto"
        self.schema_json = None
        self.browser = None
        self.model = None
        self.max_steps = None
        self.headful = None


class _FakeContext:
    """Duck-types a Playwright BrowserContext and records what was set on it."""

    def __init__(self) -> None:
        self.added: list[dict[str, Any]] = []

    async def add_cookies(self, entries: list[dict[str, Any]]) -> None:
        self.added.extend(entries)


# ---------------------------------------------------------------------------
# D tier (Scrapling)
# ---------------------------------------------------------------------------


def _install_fake_scrapling(
    monkeypatch: pytest.MonkeyPatch, *, reject_cookies: bool = False
) -> list[dict[str, Any]]:
    """Install a fake ``scrapling.fetchers``; return the init kwargs it observes.

    ``reject_cookies`` imitates a Scrapling build whose ``StealthyFetcher`` does
    not accept the kwarg. Not hypothetical: the real ``__init__`` is declared
    ``(*args, **kwargs)``, so no signature probe can rule it out and the
    rejection path has to work.
    """
    seen: list[dict[str, Any]] = []

    class _FakeResponse:
        html_content = '<html><body><div class="account">signed in</div></body></html>'
        status = 200
        url = "https://example.com/dashboard"

    class _FakeFetcher:
        def __init__(self, **kwargs: Any) -> None:
            seen.append(kwargs)
            if reject_cookies and "cookies" in kwargs:
                msg = "__init__() got an unexpected keyword argument 'cookies'"
                raise TypeError(msg)

        async def async_fetch(self, url: str, **_kw: Any) -> Any:
            return _FakeResponse()

        async def aclose(self) -> None:
            return None

    fetchers = types.SimpleNamespace(StealthyFetcher=_FakeFetcher)
    monkeypatch.setitem(sys.modules, "scrapling", types.SimpleNamespace(fetchers=fetchers))
    monkeypatch.setitem(sys.modules, "scrapling.fetchers", fetchers)
    return seen


class TestDTier:
    @pytest.mark.asyncio
    async def test_cookies_reach_the_fetcher(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from scrapper_tool.http_server import _d_fetch_with_smart_defaults

        seen = _install_fake_scrapling(monkeypatch)
        await _d_fetch_with_smart_defaults(_TierReq([cookie()]))

        assert seen, "StealthyFetcher was never constructed"
        assert [c["name"] for c in seen[0]["cookies"]] == ["session"]
        assert seen[0]["cookies"][0]["value"] == SECRET

    @pytest.mark.asyncio
    async def test_marks_the_tier_applied(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from scrapper_tool.http_server import _d_fetch_with_smart_defaults

        _install_fake_scrapling(monkeypatch)
        req = _TierReq([cookie()])
        await _d_fetch_with_smart_defaults(req)
        assert req.__dict__["_cookies_applied"] == ["d"]

    @pytest.mark.asyncio
    async def test_only_url_matching_cookies_are_sent(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from scrapper_tool.http_server import _d_fetch_with_smart_defaults

        seen = _install_fake_scrapling(monkeypatch)
        jar = [cookie(), cookie(domain="evil-example.com", name="attacker")]
        await _d_fetch_with_smart_defaults(_TierReq(jar))
        assert [c["name"] for c in seen[0]["cookies"]] == ["session"]

    @pytest.mark.asyncio
    async def test_no_cookies_means_no_kwarg(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """An unsupported build must not be poked when there is nothing to send."""
        from scrapper_tool.http_server import _d_fetch_with_smart_defaults

        seen = _install_fake_scrapling(monkeypatch)
        await _d_fetch_with_smart_defaults(_TierReq())
        assert "cookies" not in seen[0]

    @pytest.mark.asyncio
    async def test_rejected_kwarg_is_reported_and_the_fetch_still_completes(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from scrapper_tool.http_server import _d_fetch_with_smart_defaults

        seen = _install_fake_scrapling(monkeypatch, reject_cookies=True)
        req = _TierReq([cookie()])
        html, status, _url = await _d_fetch_with_smart_defaults(req)

        # The tier still produced a page rather than failing outright...
        assert status == 200
        assert "signed in" in html
        # ...the first attempt carried cookies and the retry did not...
        assert "cookies" in seen[0]
        assert "cookies" not in seen[1]
        # ...and the caller can see exactly why the result is anonymous.
        assert req.__dict__["_cookies_skipped"] == [
            {"tier": "d", "reason": "scrapling_rejected_cookies_kwarg"}
        ]
        assert "d" not in req.__dict__.get("_cookies_applied", [])

    @pytest.mark.asyncio
    async def test_a_non_cookie_type_error_still_propagates(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Only cookie-shaped rejections are translated; real kwarg bugs must surface."""

        class _Exploding:
            def __init__(self, **_kw: Any) -> None:
                msg = "__init__() got an unexpected keyword argument 'headless'"
                raise TypeError(msg)

        fetchers = types.SimpleNamespace(StealthyFetcher=_Exploding)
        monkeypatch.setitem(sys.modules, "scrapling", types.SimpleNamespace(fetchers=fetchers))
        monkeypatch.setitem(sys.modules, "scrapling.fetchers", fetchers)

        from scrapper_tool.http_server import _d_fetch_with_smart_defaults

        with pytest.raises(TypeError, match="headless"):
            await _d_fetch_with_smart_defaults(_TierReq([cookie()]))


# ---------------------------------------------------------------------------
# E1 (Crawl4AI)
# ---------------------------------------------------------------------------


def _agent_cfg(**kw: Any) -> Any:
    from scrapper_tool.agent.types import AgentConfig

    return AgentConfig(**kw)


class TestE1Tier:
    def test_cookies_land_on_browser_config(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from scrapper_tool import _extras
        from scrapper_tool.agent.extract import _browser_cfg_kwargs

        monkeypatch.setattr(_extras, "crawl4ai_accepts", lambda _p: True)
        kwargs = _browser_cfg_kwargs(
            _agent_cfg(cookies=[cookie()]), "https://example.com/dashboard"
        )
        assert [c["name"] for c in kwargs["cookies"]] == ["session"]

    def test_only_url_matching_cookies_are_sent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from scrapper_tool import _extras
        from scrapper_tool.agent.extract import _browser_cfg_kwargs

        monkeypatch.setattr(_extras, "crawl4ai_accepts", lambda _p: True)
        jar = [cookie(), cookie(domain="evil-example.com", name="attacker")]
        kwargs = _browser_cfg_kwargs(_agent_cfg(cookies=jar), "https://example.com/dashboard")
        assert [c["name"] for c in kwargs["cookies"]] == ["session"]

    def test_a_build_without_the_param_gets_no_kwarg(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Crawl4AI raises on unexpected kwargs, so a blind pass would fail the tier."""
        from scrapper_tool import _extras
        from scrapper_tool.agent.extract import _browser_cfg_kwargs

        monkeypatch.setattr(_extras, "crawl4ai_accepts", lambda _p: False)
        kwargs = _browser_cfg_kwargs(
            _agent_cfg(cookies=[cookie()]), "https://example.com/dashboard"
        )
        assert "cookies" not in kwargs

    def test_no_cookies_means_no_kwarg(self) -> None:
        from scrapper_tool.agent.extract import _browser_cfg_kwargs

        assert "cookies" not in _browser_cfg_kwargs(_agent_cfg(), "https://example.com/")

    def test_outcome_is_skipped_on_an_unsupported_build(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from scrapper_tool import _extras, http_server

        monkeypatch.setattr(_extras, "crawl4ai_accepts", lambda _p: False)
        req = _TierReq([cookie()])
        http_server._record_e1_cookie_outcome(req, _agent_cfg(cookies=[cookie()]))
        assert req.__dict__["_cookies_skipped"] == [
            {"tier": "e1", "reason": "crawl4ai_browserconfig_has_no_cookies_param"}
        ]

    def test_outcome_is_applied_on_a_supported_build(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from scrapper_tool import _extras, http_server

        monkeypatch.setattr(_extras, "crawl4ai_accepts", lambda _p: True)
        req = _TierReq([cookie()])
        http_server._record_e1_cookie_outcome(req, _agent_cfg(cookies=[cookie()]))
        assert req.__dict__["_cookies_applied"] == ["e1"]


# ---------------------------------------------------------------------------
# E2 (browser-use over CDP)
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_context(monkeypatch: pytest.MonkeyPatch) -> _FakeContext:
    """Point resolve_context at a recording fake, as the live browser would be."""
    from scrapper_tool.agent.backends import browser as browser_mod

    context = _FakeContext()

    async def _resolve(_browser: Any) -> _FakeContext:
        return context

    monkeypatch.setattr(browser_mod, "resolve_context", _resolve)
    return context


class TestE2Tier:
    @pytest.mark.asyncio
    async def test_cookies_are_set_on_the_live_context(self, fake_context: _FakeContext) -> None:
        from scrapper_tool.agent import browse as browse_mod

        await browse_mod._inject_cookies(
            object(), _agent_cfg(cookies=[cookie()]), "https://example.com/dashboard"
        )
        assert [c["name"] for c in fake_context.added] == ["session"]
        assert fake_context.added[0]["value"] == SECRET

    @pytest.mark.asyncio
    async def test_only_url_matching_cookies_are_set(self, fake_context: _FakeContext) -> None:
        from scrapper_tool.agent import browse as browse_mod

        jar = [cookie(), cookie(domain="evil-example.com", name="attacker")]
        await browse_mod._inject_cookies(
            object(), _agent_cfg(cookies=jar), "https://example.com/dashboard"
        )
        assert [c["name"] for c in fake_context.added] == ["session"]

    @pytest.mark.asyncio
    async def test_no_cookies_never_touches_the_context(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from scrapper_tool.agent import browse as browse_mod
        from scrapper_tool.agent.backends import browser as browser_mod

        async def _resolve(_browser: Any) -> Any:
            raise AssertionError("resolve_context must not be called with an empty jar")

        monkeypatch.setattr(browser_mod, "resolve_context", _resolve)
        await browse_mod._inject_cookies(object(), _agent_cfg(), "https://example.com/")

    @pytest.mark.asyncio
    async def test_a_jar_scoped_elsewhere_never_touches_the_context(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from scrapper_tool.agent import browse as browse_mod
        from scrapper_tool.agent.backends import browser as browser_mod

        async def _resolve(_browser: Any) -> Any:
            raise AssertionError("resolve_context must not be called when nothing matches")

        monkeypatch.setattr(browser_mod, "resolve_context", _resolve)
        await browse_mod._inject_cookies(
            object(),
            _agent_cfg(cookies=[cookie(domain="other.example.net")]),
            "https://example.com/",
        )

    def test_camoufox_is_recorded_as_skipped_not_applied(self) -> None:
        """Firefox dropped CDP, so E2 can never carry a session on the default backend."""
        from scrapper_tool import http_server

        req = _TierReq([cookie()])
        http_server._record_e2_cookie_outcome(
            req, _agent_cfg(browser="camoufox", cookies=[cookie()])
        )
        assert req.__dict__["_cookies_skipped"] == [
            {"tier": "e2", "reason": "camoufox_exposes_no_cdp_endpoint"}
        ]
        assert "_cookies_applied" not in req.__dict__

    def test_a_cdp_backend_is_recorded_as_applied(self) -> None:
        from scrapper_tool import http_server

        req = _TierReq([cookie()])
        http_server._record_e2_cookie_outcome(
            req, _agent_cfg(browser="patchright", cookies=[cookie()])
        )
        assert req.__dict__["_cookies_applied"] == ["e2"]


# ---------------------------------------------------------------------------
# Replay tier
# ---------------------------------------------------------------------------


def _patch_ladder(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    from scrapper_tool import ladder as ladder_mod

    captured: dict[str, Any] = {}

    async def _fake_ladder(method: str, url: str, **kw: Any) -> Any:
        captured.update(kw)
        response = type("R", (), {"text": "<html>ok</html>", "status_code": 200, "url": url})()
        return response, "chrome146"

    monkeypatch.setattr(ladder_mod, "request_with_ladder", _fake_ladder)
    return captured


class TestReplayTier:
    @pytest.mark.asyncio
    async def test_the_http_leg_carries_cookies(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from scrapper_tool.http_server import _make_ladder_fetch

        captured = _patch_ladder(monkeypatch)
        req = _TierReq([cookie()])
        await _make_ladder_fetch(req)()

        assert [c.name for c in captured["cookies"]] == ["session"]
        assert req.__dict__["_cookies_applied"] == ["replay"]

    @pytest.mark.asyncio
    async def test_a_jar_scoped_elsewhere_is_reported_not_silently_dropped(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from scrapper_tool.http_server import _make_ladder_fetch

        captured = _patch_ladder(monkeypatch)
        req = _TierReq([cookie(domain="other.example.net")])
        await _make_ladder_fetch(req)()

        assert captured["cookies"] is None
        assert req.__dict__["_cookies_skipped"] == [
            {"tier": "replay", "reason": "no_cookie_matched_this_url"}
        ]


class TestScopedSelection:
    def test_a_jar_that_matches_nothing_is_reported_as_skipped(self) -> None:
        """'I passed cookies and nothing happened' must never be unfalsifiable."""
        from scrapper_tool.http_server import _cookies_for_tier

        req = _TierReq([cookie(domain="other.example.net")])
        assert _cookies_for_tier(req, "render") is None
        assert req.__dict__["_cookies_skipped"] == [
            {"tier": "render", "reason": "no_cookie_matched_this_url"}
        ]

    def test_an_absent_jar_is_not_reported_at_all(self) -> None:
        from scrapper_tool.http_server import _cookies_for_tier

        req = _TierReq()
        assert _cookies_for_tier(req, "render") is None
        assert "_cookies_skipped" not in req.__dict__
