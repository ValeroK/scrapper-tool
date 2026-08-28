"""Unit tests for ``scrapper_tool.mcp``.

The MCP server is built lazily (``_build_server`` imports
``mcp.server.fastmcp.FastMCP`` only when called) so consumers without
the ``[agent]`` extra installed can still ``import scrapper_tool.mcp``.

These tests run **with** the ``[agent]`` extra installed (CI matrix
includes this case). Real MCP transport (stdio, HTTP/SSE) is NOT
exercised — the in-process server's tool dispatch is what we verify.
End-to-end transport tests live in
``tests/integration/test_mcp_live.py`` (opt-in via the ``live`` marker).

Tools exercised
---------------

- ``fetch_with_ladder`` — happy path (chrome146 wins) + blocked path
  (all-403 → BlockedError → returns ``blocked: True``).
- ``extract_product`` — JSON-LD Product → ProductOffer dict; no
  Product block → returns null.
- ``extract_microdata_price`` — microdata price+currency → dict; no
  microdata → returns null.
- ``canary`` — happy path + custom profiles.

Plus the CLI-style ``main()`` entrypoint:

- ``--help`` exits 0.
- Default startup (no args) calls ``server.run()`` once.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import MagicMock

import pytest

# The MCP server requires the `[agent]` optional extra. When it's not
# installed (the default `extras=dev` CI matrix entry), skip this whole
# module — the tests can't construct the FastMCP server. The
# `extras=dev,hostile` entry doesn't pull mcp either; only the matrix
# row that adds `agent` has the SDK. CI runs both, so this skip
# correctly differentiates them.
pytest.importorskip(
    "mcp.server.fastmcp",
    reason="MCP tests require the [agent] extra (pip install scrapper-tool[agent]).",
)

from scrapper_tool import ladder as ladder_module
from scrapper_tool import mcp as mcp_module
from scrapper_tool.ladder import IMPERSONATE_LADDER
from scrapper_tool.testing import FakeCurlSession

# ---- Fixtures -------------------------------------------------------------


@pytest.fixture
def fake_curl(monkeypatch: pytest.MonkeyPatch) -> type[FakeCurlSession]:
    FakeCurlSession.reset()
    monkeypatch.setattr(ladder_module, "_CurlCffiAsyncSession", FakeCurlSession)
    return FakeCurlSession


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _instant(_seconds: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", _instant)


@pytest.fixture
def server() -> object:
    """Build a fresh FastMCP server with all tools registered."""
    return mcp_module._build_server()


def _get_tool(server: object, name: str) -> object:
    """Pull a registered tool out of the FastMCP server by name.

    FastMCP's tool registry shape is ``server._tool_manager._tools`` — a
    private path, but it's stable across the 1.x line. If this breaks
    on an SDK bump, the M12 quarterly review catches it.
    """
    tools = server._tool_manager._tools  # type: ignore[attr-defined]
    if name not in tools:
        msg = f"Tool {name!r} not registered. Available: {list(tools)}"
        raise KeyError(msg)
    return tools[name]


# ---- fetch_with_ladder ----------------------------------------------------


class TestFetchWithLadder:
    @pytest.mark.asyncio
    async def test_happy_path_chrome146_wins(
        self,
        server: object,
        fake_curl: type[FakeCurlSession],
    ) -> None:
        fake_curl.STATUS_FOR_PROFILE = {"chrome146": 200}
        fake_curl.RESPONSE_TEXT_FOR_PROFILE = {"chrome146": "<html>ok</html>"}
        tool = _get_tool(server, "fetch_with_ladder")

        result = await tool.fn(url="https://example.test/x")  # type: ignore[attr-defined]
        assert result["status"] == 200
        assert result["winning_profile"] == "chrome146"
        assert result["blocked"] is False
        assert "<html>ok</html>" in result["body"]
        assert result["truncated"] is False

    @pytest.mark.asyncio
    async def test_all_blocked_returns_blocked_true(
        self,
        server: object,
        fake_curl: type[FakeCurlSession],
    ) -> None:
        fake_curl.STATUS_FOR_PROFILE = dict.fromkeys(IMPERSONATE_LADDER, 403)
        tool = _get_tool(server, "fetch_with_ladder")

        result = await tool.fn(url="https://example.test/blocked")  # type: ignore[attr-defined]
        assert result["blocked"] is True
        assert result["winning_profile"] is None
        assert result["status"] is None
        assert "Pattern D" in result["error"]


# ---- extract_product ------------------------------------------------------


_PRODUCT_HTML = """<html><head>
<script type="application/ld+json">
{"@context":"https://schema.org","@type":"Product","name":"Widget",
 "sku":"X1","offers":{"@type":"Offer","price":"19.99","priceCurrency":"USD"}}
</script></head><body></body></html>"""


_NO_PRODUCT_HTML = """<html><body><h1>Plain page</h1></body></html>"""


class TestExtractProduct:
    @pytest.mark.asyncio
    async def test_jsonld_product_returns_dict(self, server: object) -> None:
        tool = _get_tool(server, "extract_product")
        result = await tool.fn(html=_PRODUCT_HTML)  # type: ignore[attr-defined]
        assert result is not None
        assert result["name"] == "Widget"
        assert result["sku"] == "X1"
        assert result["price"] == "19.99"
        assert result["currency"] == "USD"

    @pytest.mark.asyncio
    async def test_no_product_returns_null(self, server: object) -> None:
        tool = _get_tool(server, "extract_product")
        result = await tool.fn(html=_NO_PRODUCT_HTML)  # type: ignore[attr-defined]
        assert result is None


# ---- extract_microdata_price ----------------------------------------------


_MICRODATA_HTML = """<html><body>
<meta itemprop="price" content="6.84">
<meta itemprop="priceCurrency" content="USD">
</body></html>"""


class TestExtractMicrodataPrice:
    @pytest.mark.asyncio
    async def test_microdata_returns_price_currency(self, server: object) -> None:
        tool = _get_tool(server, "extract_microdata_price")
        result = await tool.fn(html=_MICRODATA_HTML)  # type: ignore[attr-defined]
        assert result == {"price": "6.84", "currency": "USD"}

    @pytest.mark.asyncio
    async def test_no_microdata_returns_null(self, server: object) -> None:
        tool = _get_tool(server, "extract_microdata_price")
        result = await tool.fn(html=_NO_PRODUCT_HTML)  # type: ignore[attr-defined]
        assert result is None


# ---- canary ---------------------------------------------------------------


class TestCanaryTool:
    @pytest.mark.asyncio
    async def test_canary_default_ladder(
        self,
        server: object,
        fake_curl: type[FakeCurlSession],
    ) -> None:
        fake_curl.STATUS_FOR_PROFILE = {"chrome146": 200}
        tool = _get_tool(server, "canary")
        result = await tool.fn(url="https://example.test/x")  # type: ignore[attr-defined]
        assert result["winning_profile"] == "chrome146"
        assert result["exit_code"] == 0

    @pytest.mark.asyncio
    async def test_canary_custom_profiles(
        self,
        server: object,
        fake_curl: type[FakeCurlSession],
    ) -> None:
        fake_curl.STATUS_FOR_PROFILE = {"chrome142": 200}
        tool = _get_tool(server, "canary")
        result = await tool.fn(  # type: ignore[attr-defined]
            url="https://example.test/x",
            profiles=["chrome142"],
        )
        assert result["winning_profile"] == "chrome142"


# ---- v1.1.0 additions: extract_structured + auto_scrape -----------------


class TestFetchWithLadderStructured:
    @pytest.mark.asyncio
    async def test_extract_structured_true_runs_pattern_b(
        self,
        server: object,
        fake_curl: type[FakeCurlSession],
    ) -> None:
        product_html = (
            '<html><head><script type="application/ld+json">'
            '{"@context":"https://schema.org","@type":"Product","name":"Widget X",'
            '"sku":"X1","offers":{"@type":"Offer","price":"19.99","priceCurrency":"USD"}}'
            "</script></head><body></body></html>"
        )
        fake_curl.STATUS_FOR_PROFILE = {"chrome146": 200}
        fake_curl.RESPONSE_TEXT_FOR_PROFILE = {"chrome146": product_html}

        tool = _get_tool(server, "fetch_with_ladder")
        result = await tool.fn(  # type: ignore[attr-defined]
            url="https://example.test/p", extract_structured=True
        )
        assert result["status"] == 200
        assert result["product"] is not None
        assert result["product"]["name"] == "Widget X"
        assert result["product"]["price"] == "19.99"
        assert result["product"]["currency"] == "USD"

    @pytest.mark.asyncio
    async def test_extract_structured_false_omits_product(
        self,
        server: object,
        fake_curl: type[FakeCurlSession],
    ) -> None:
        fake_curl.STATUS_FOR_PROFILE = {"chrome146": 200}
        fake_curl.RESPONSE_TEXT_FOR_PROFILE = {"chrome146": "<html>plain</html>"}

        tool = _get_tool(server, "fetch_with_ladder")
        result = await tool.fn(url="https://example.test/p")  # type: ignore[attr-defined]
        # Default is extract_structured=False so 'product' should not be in keys
        assert "product" not in result


class TestAutoScrape:
    @pytest.mark.asyncio
    async def test_auto_scrape_succeeds_on_a_b_c(
        self,
        server: object,
        fake_curl: type[FakeCurlSession],
    ) -> None:
        product_html = (
            '<html><head><script type="application/ld+json">'
            '{"@context":"https://schema.org","@type":"Product","name":"Widget Y",'
            '"sku":"Y1","offers":{"@type":"Offer","price":"29.99","priceCurrency":"USD"}}'
            "</script></head><body></body></html>"
        )
        fake_curl.STATUS_FOR_PROFILE = {"chrome146": 200}
        fake_curl.RESPONSE_TEXT_FOR_PROFILE = {"chrome146": product_html}

        tool = _get_tool(server, "auto_scrape")
        result = await tool.fn(url="https://example.test/p")  # type: ignore[attr-defined]
        assert result["pattern_used"] == "a_b_c"
        assert result["pattern_attempts"] == ["a_b_c"]
        assert result["product"] is not None
        assert result["product"]["name"] == "Widget Y"
        assert result["blocked"] is False
        assert result["hostile_skipped"] is False

    @pytest.mark.asyncio
    async def test_auto_scrape_accepts_a_b_c_with_schema(
        self,
        server: object,
        fake_curl: type[FakeCurlSession],
    ) -> None:
        # v1.5.0 parity fix: with a schema supplied AND a B/C signal present,
        # MCP must accept A/B/C (like REST /scrape) instead of always burning
        # an LLM call (E1). Regression guard for the MCP/REST divergence.
        product_html = (
            '<html><head><script type="application/ld+json">'
            '{"@context":"https://schema.org","@type":"Product","name":"Widget Z",'
            '"sku":"Z1","offers":{"@type":"Offer","price":"9.99","priceCurrency":"USD"}}'
            "</script></head><body></body></html>"
        )
        fake_curl.STATUS_FOR_PROFILE = {"chrome146": 200}
        fake_curl.RESPONSE_TEXT_FOR_PROFILE = {"chrome146": product_html}

        tool = _get_tool(server, "auto_scrape")
        result = await tool.fn(  # type: ignore[attr-defined]
            url="https://example.test/p",
            schema_json={"type": "object", "properties": {"name": {"type": "string"}}},
        )
        # No LLM call — accepted at A/B/C, not escalated to E1.
        assert result["pattern_used"] == "a_b_c"
        assert result["product"]["name"] == "Widget Z"


# ---- v1.1.3: auto_scrape now invokes Pattern D between A/B/C and E1 ------


class _FakeMcpScraplingResponse:
    """Stand-in for Scrapling's StealthyFetcher response object."""

    def __init__(self, *, html: str, status: int = 200, url: str = "https://hostile.com/p"):
        self.html_content = html
        self.status = status
        self.url = url


class _FakeMcpFetcher:
    """Async context manager mimicking patterns.d.hostile_client."""

    def __init__(self, response: object | BaseException) -> None:
        self._response = response

    async def __aenter__(self) -> _FakeMcpFetcher:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def async_fetch(self, url: str, **kwargs: object) -> object:
        # Accept any kwargs (solve_cloudflare, network_idle, user_data_dir, ...).
        if isinstance(self._response, BaseException):
            raise self._response
        return self._response


class TestAutoScrapeWithPatternD:
    """v1.1.3 — auto_scrape now invokes Pattern D between A/B/C and E1.

    Pre-1.1.3 the cascade went straight A/B/C -> E1 -> E2; Pattern D was
    unreachable from the MCP tool even when [hostile] was installed.
    """

    @pytest.mark.asyncio
    async def test_d_succeeds_when_a_b_c_blocked_and_hostile_installed(
        self,
        server: object,
        fake_curl: type[FakeCurlSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # All A/B/C profiles return 403 -> raises BlockedError -> cascade
        # advances to D. Mock D to return a readable product page.
        fake_curl.STATUS_FOR_PROFILE = dict.fromkeys(IMPERSONATE_LADDER, 403)
        product_html = (
            '<html><head><script type="application/ld+json">'
            '{"@context":"https://schema.org","@type":"Product","name":"Widget Z",'
            '"sku":"Z1","offers":{"@type":"Offer","price":"39.99","priceCurrency":"USD"}}'
            "</script></head><body></body></html>"
        )
        import scrapper_tool.patterns.d as d_mod

        def fake_hostile_client(**_kwargs: object) -> _FakeMcpFetcher:
            return _FakeMcpFetcher(_FakeMcpScraplingResponse(html=product_html))

        monkeypatch.setattr(d_mod, "hostile_client", fake_hostile_client)

        tool = _get_tool(server, "auto_scrape")
        result = await tool.fn(url="https://hostile.com/p")  # type: ignore[attr-defined]
        assert result["pattern_used"] == "d"
        assert result["pattern_attempts"] == ["a_b_c", "d"]
        assert result["winning_profile"] == "scrapling"
        assert result["product"] is not None
        assert result["product"]["name"] == "Widget Z"
        assert result["blocked"] is False
        assert result["hostile_skipped"] is False

    @pytest.mark.asyncio
    async def test_d_skipped_when_hostile_extra_missing(
        self,
        server: object,
        fake_curl: type[FakeCurlSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Force `from scrapper_tool.patterns.d import hostile_client` to
        # raise ImportError inside the auto_scrape body so the cascade
        # falls through to E1 with hostile_skipped=true. Done by removing
        # the cached module and patching __import__.
        import builtins
        import sys

        # Use monkeypatch so pytest restores sys.modules after the test —
        # a raw pop/assign here leaks a fake ``scrapper_tool.agent`` into
        # later tests (breaks test_http_server when it runs after this file).
        monkeypatch.delitem(sys.modules, "scrapper_tool.patterns.d", raising=False)
        real_import = builtins.__import__

        def patched_import(
            name: str,
            globals: object = None,
            locals: object = None,
            fromlist: object = (),
            level: int = 0,
        ) -> object:
            if name == "scrapper_tool.patterns.d":
                raise ImportError("simulated: [hostile] extra not installed")
            return real_import(name, globals, locals, fromlist, level)

        monkeypatch.setattr(builtins, "__import__", patched_import)

        # A/B/C blocked → cascade tries D (skipped) → E1.
        fake_curl.STATUS_FOR_PROFILE = dict.fromkeys(IMPERSONATE_LADDER, 403)

        # Mock the agent layer so E1 returns a result.
        from unittest.mock import AsyncMock

        fake_result = MagicMock()
        fake_result.mode = "extract"
        fake_result.data = {"name": "via_e1"}
        fake_result.final_url = "https://protected.com/p"
        fake_result.rendered_markdown = "# E1 result"
        fake_result.screenshots = None
        fake_result.actions = []
        fake_result.tokens_used = 50
        fake_result.steps_used = 1
        fake_result.blocked = False
        fake_result.error = None
        fake_result.duration_s = 1.0

        agent_module = MagicMock()
        agent_module.AgentConfig = MagicMock()
        agent_module.AgentConfig.from_env = MagicMock(
            return_value=MagicMock(merged=lambda **_: MagicMock())
        )
        agent_module.agent_extract = AsyncMock(return_value=fake_result)
        agent_module.agent_browse = AsyncMock(return_value=fake_result)
        monkeypatch.setitem(sys.modules, "scrapper_tool.agent", agent_module)

        tool = _get_tool(server, "auto_scrape")
        result = await tool.fn(url="https://protected.com/p")  # type: ignore[attr-defined]
        assert result["pattern_used"] == "e1"
        assert result["pattern_attempts"] == ["a_b_c", "e1"], (
            "When [hostile] is missing, the D step appends nothing to attempts"
        )
        assert result["hostile_skipped"] is True


# ---- B2: stealth-render tier (MCP parity with REST) -----------------------


_RENDER_PRODUCT_HTML = (
    '<html><head><script type="application/ld+json">'
    '{"@context":"https://schema.org","@type":"Product","name":"Rendered Widget",'
    '"sku":"R1","offers":{"@type":"Offer","price":"12.34","priceCurrency":"USD"}}'
    "</script></head><body></body></html>"
)


def _install_fake_render_mcp(
    monkeypatch: pytest.MonkeyPatch,
    *,
    html: str = _RENDER_PRODUCT_HTML,
    status: int = 200,
    error: BaseException | None = None,
) -> None:
    """Enable the render tier (off by default in tests) with a fake browser."""
    import scrapper_tool.patterns.render as render_mod

    monkeypatch.setenv("SCRAPPER_TOOL_RENDER_TIER", "1")

    async def fake_render_html(url: str, **_kwargs: Any) -> Any:
        if error is not None:
            raise error
        return render_mod.RenderResult(html=html, status=status, final_url=url)

    monkeypatch.setattr(render_mod, "render_html", fake_render_html)


class TestAutoScrapeRenderTier:
    """The MCP cascade must have the same tiers as REST, in the same order.

    REST and MCP have drifted before (Pattern D was reachable from one and not
    the other for a whole release), so parity gets pinned explicitly rather than
    assumed.
    """

    @pytest.mark.asyncio
    async def test_render_wins_before_the_llm_tier(
        self,
        server: object,
        fake_curl: type[FakeCurlSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        fake_curl.STATUS_FOR_PROFILE = dict.fromkeys(IMPERSONATE_LADDER, 403)
        monkeypatch.setattr(mcp_module, "_try_pattern_d_for_auto_scrape", _skip_d_for_auto_scrape)
        _install_fake_render_mcp(monkeypatch)

        tool = _get_tool(server, "auto_scrape")
        result = await tool.fn(url="https://walled.com/p")  # type: ignore[attr-defined]

        assert result["pattern_used"] == "render"
        assert result["pattern_attempts"] == ["a_b_c", "render"]
        assert result["product"]["name"] == "Rendered Widget"
        assert result["blocked"] is False
        assert result["is_structured"] is True

    @pytest.mark.asyncio
    async def test_render_accepts_a_403_carrying_real_content(
        self,
        server: object,
        fake_curl: type[FakeCurlSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Same store.mopar.com case the REST tier pins — content, not status."""
        fake_curl.STATUS_FOR_PROFILE = dict.fromkeys(IMPERSONATE_LADDER, 403)
        monkeypatch.setattr(mcp_module, "_try_pattern_d_for_auto_scrape", _skip_d_for_auto_scrape)
        _install_fake_render_mcp(monkeypatch, status=403)

        tool = _get_tool(server, "auto_scrape")
        result = await tool.fn(url="https://walled.com/p")  # type: ignore[attr-defined]

        assert result["pattern_used"] == "render"
        assert result["product"]["name"] == "Rendered Widget"

    @pytest.mark.asyncio
    async def test_render_without_signal_escalates_to_e1(
        self,
        server: object,
        fake_curl: type[FakeCurlSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import sys

        fake_curl.STATUS_FOR_PROFILE = dict.fromkeys(IMPERSONATE_LADDER, 403)
        monkeypatch.setattr(mcp_module, "_try_pattern_d_for_auto_scrape", _skip_d_for_auto_scrape)
        _install_fake_render_mcp(monkeypatch, html="<html><body>nothing</body></html>")

        agent_module = _fake_agent_module_for_e1()
        monkeypatch.setitem(sys.modules, "scrapper_tool.agent", agent_module)

        tool = _get_tool(server, "auto_scrape")
        result = await tool.fn(url="https://walled.com/p")  # type: ignore[attr-defined]

        assert result["pattern_used"] == "e1"
        assert result["pattern_attempts"] == ["a_b_c", "render", "e1"]

    @pytest.mark.asyncio
    async def test_render_failure_falls_through_to_e1(
        self,
        server: object,
        fake_curl: type[FakeCurlSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import sys

        fake_curl.STATUS_FOR_PROFILE = dict.fromkeys(IMPERSONATE_LADDER, 403)
        monkeypatch.setattr(mcp_module, "_try_pattern_d_for_auto_scrape", _skip_d_for_auto_scrape)
        _install_fake_render_mcp(monkeypatch, error=RuntimeError("camoufox crashed"))

        agent_module = _fake_agent_module_for_e1()
        monkeypatch.setitem(sys.modules, "scrapper_tool.agent", agent_module)

        tool = _get_tool(server, "auto_scrape")
        result = await tool.fn(url="https://walled.com/p")  # type: ignore[attr-defined]

        assert result["pattern_used"] == "e1"
        assert result["pattern_attempts"] == ["a_b_c", "render", "e1"]

    @pytest.mark.asyncio
    async def test_tier_can_be_disabled(
        self,
        server: object,
        fake_curl: type[FakeCurlSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import sys

        fake_curl.STATUS_FOR_PROFILE = dict.fromkeys(IMPERSONATE_LADDER, 403)
        monkeypatch.setattr(mcp_module, "_try_pattern_d_for_auto_scrape", _skip_d_for_auto_scrape)
        _install_fake_render_mcp(monkeypatch)
        monkeypatch.setenv("SCRAPPER_TOOL_RENDER_TIER", "0")

        agent_module = _fake_agent_module_for_e1()
        monkeypatch.setitem(sys.modules, "scrapper_tool.agent", agent_module)

        tool = _get_tool(server, "auto_scrape")
        result = await tool.fn(url="https://walled.com/p")  # type: ignore[attr-defined]

        assert result["pattern_attempts"] == ["a_b_c", "e1"]


async def _skip_d_for_auto_scrape(*_args: Any, **_kwargs: Any) -> tuple[None, None, bool]:
    """Stand in for a missing [hostile] extra: D contributes nothing."""
    return None, None, True


def _fake_agent_module_for_e1() -> MagicMock:
    """A ``scrapper_tool.agent`` stand-in whose E1 extract always succeeds."""
    fake_result = MagicMock()
    fake_result.mode = "extract"
    fake_result.data = {"name": "Salvaged"}
    fake_result.final_url = "https://walled.com/p"
    fake_result.rendered_markdown = "# Salvaged"
    fake_result.screenshots = None
    fake_result.actions = []
    fake_result.tokens_used = 10
    fake_result.steps_used = 1
    fake_result.blocked = False
    fake_result.error = None
    fake_result.duration_s = 1.0

    agent_module = MagicMock()
    agent_module.AgentConfig = MagicMock()
    agent_module.AgentConfig.from_env = MagicMock(
        return_value=MagicMock(merged=lambda **_: MagicMock())
    )

    async def fake_extract(*_args: Any, **_kwargs: Any) -> Any:
        return fake_result

    agent_module.agent_extract = fake_extract
    return agent_module


class TestAutoScrapeE1FailureHandling:
    """MCP shares both E1 rules with REST, so it shares both fixes.

    The accept rule is one flag — ``if not result.blocked`` — so an E1 result
    carrying a navigation failure scored as a win on both surfaces. That is
    fixed in ``run_extract`` rather than in either cascade, precisely so one
    change covers both. The handoff rule is the same story: a hard E1 failure
    aborted the cascade here exactly as it did in REST.
    """

    @pytest.mark.asyncio
    async def test_a_navigation_failure_is_not_an_e1_win(
        self,
        server: object,
        fake_curl: type[FakeCurlSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import sys

        from scrapper_tool.errors import AgentError

        fake_curl.STATUS_FOR_PROFILE = dict.fromkeys(IMPERSONATE_LADDER, 403)
        monkeypatch.setattr(mcp_module, "_try_pattern_d_for_auto_scrape", _skip_d_for_auto_scrape)

        agent_module = MagicMock()
        agent_module.AgentConfig = MagicMock()
        agent_module.AgentConfig.from_env = MagicMock(
            return_value=MagicMock(merged=lambda **_: MagicMock())
        )

        async def dead_host(*_args: Any, **_kwargs: Any) -> Any:
            raise AgentError(
                "agent_extract failed at https://gone.test/p: Page.goto: net::ERR_NAME_NOT_RESOLVED"
            )

        agent_module.agent_extract = dead_host
        monkeypatch.setitem(sys.modules, "scrapper_tool.agent", agent_module)

        tool = _get_tool(server, "auto_scrape")
        with pytest.raises(AgentError, match="ERR_NAME_NOT_RESOLVED"):
            await tool.fn(url="https://gone.test/p")  # type: ignore[attr-defined]

    @pytest.mark.asyncio
    async def test_a_hard_e1_failure_hands_off_to_e2_when_interactive(
        self,
        server: object,
        fake_curl: type[FakeCurlSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """REST parity: the rung below gets its turn on a hard E1 failure."""
        import sys

        from scrapper_tool.errors import AgentError

        fake_curl.STATUS_FOR_PROFILE = dict.fromkeys(IMPERSONATE_LADDER, 403)
        monkeypatch.setattr(mcp_module, "_try_pattern_d_for_auto_scrape", _skip_d_for_auto_scrape)

        agent_module = MagicMock()
        agent_module.AgentConfig = MagicMock()
        agent_module.AgentConfig.from_env = MagicMock(
            return_value=MagicMock(merged=lambda **_: MagicMock())
        )

        async def crashed(*_args: Any, **_kwargs: Any) -> Any:
            raise AgentError("agent_extract failed: browser crashed")

        browse_result = MagicMock()
        browse_result.mode = "browse"
        browse_result.data = {"name": "Salvaged by E2"}
        browse_result.final_url = "https://walled.test/p"
        browse_result.rendered_markdown = None
        browse_result.screenshots = None
        browse_result.actions = []
        browse_result.tokens_used = 200
        browse_result.steps_used = 4
        browse_result.blocked = False
        browse_result.error = None
        browse_result.duration_s = 9.0

        async def fake_browse(*_args: Any, **_kwargs: Any) -> Any:
            return browse_result

        agent_module.agent_extract = crashed
        agent_module.agent_browse = fake_browse
        monkeypatch.setitem(sys.modules, "scrapper_tool.agent", agent_module)

        tool = _get_tool(server, "auto_scrape")
        result = await tool.fn(url="https://walled.test/p", interactive=True)  # type: ignore[attr-defined]

        assert result["pattern_used"] == "e2"
        assert result["pattern_attempts"] == ["a_b_c", "e1", "e2"]
        assert result["data"] == {"name": "Salvaged by E2"}

    @pytest.mark.asyncio
    async def test_an_unreachable_llm_never_hands_off(
        self,
        server: object,
        fake_curl: type[FakeCurlSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """E2 shares the LLM backend, so this one stays a fault, not an escalation."""
        import sys

        from scrapper_tool.errors import AgentLLMError

        fake_curl.STATUS_FOR_PROFILE = dict.fromkeys(IMPERSONATE_LADDER, 403)
        monkeypatch.setattr(mcp_module, "_try_pattern_d_for_auto_scrape", _skip_d_for_auto_scrape)

        agent_module = MagicMock()
        agent_module.AgentConfig = MagicMock()
        agent_module.AgentConfig.from_env = MagicMock(
            return_value=MagicMock(merged=lambda **_: MagicMock())
        )

        async def llm_down(*_args: Any, **_kwargs: Any) -> Any:
            raise AgentLLMError("ollama unreachable at localhost:11434")

        async def never(*_args: Any, **_kwargs: Any) -> Any:
            raise AssertionError("E2 shares the LLM backend — must not run")

        agent_module.agent_extract = llm_down
        agent_module.agent_browse = never
        monkeypatch.setitem(sys.modules, "scrapper_tool.agent", agent_module)

        tool = _get_tool(server, "auto_scrape")
        with pytest.raises(AgentLLMError, match="ollama unreachable"):
            await tool.fn(url="https://walled.test/p", interactive=True)  # type: ignore[attr-defined]


# ---- F2: per-domain tier memory (MCP parity) ------------------------------


class TestAutoScrapePolicySkip:
    """MCP must self-tune the same way REST does: a domain that has repeatedly
    needed render stops paying for the ladder on every call."""

    @pytest.mark.asyncio
    async def test_confident_policy_skips_the_ladder(
        self, server: object, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from datetime import UTC, datetime

        from scrapper_tool.recipe.policy import DomainPolicy, get_policy_store

        get_policy_store()._write(  # type: ignore[attr-defined]
            "walled.test",
            DomainPolicy(
                domain="walled.test",
                best_tier="render",
                updated_at=datetime.now(UTC).isoformat(),
                observations=3,
            ),
        )

        async def spy_ladder(method: str, url: str, **kwargs: Any) -> Any:
            raise AssertionError("ladder must be skipped when policy says render")

        monkeypatch.setattr(mcp_module, "request_with_ladder", spy_ladder)
        monkeypatch.setattr(mcp_module, "_try_pattern_d_for_auto_scrape", _skip_d_for_auto_scrape)

        product = (
            '<html><head><script type="application/ld+json">'
            '{"@context":"https://schema.org","@type":"Product","name":"W",'
            '"offers":{"@type":"Offer","price":"9.99","priceCurrency":"USD"}}'
            "</script></head><body></body></html>"
        )

        import scrapper_tool.patterns.render as render_mod

        monkeypatch.setenv("SCRAPPER_TOOL_RENDER_TIER", "1")

        async def fake_render(url: str, **_kwargs: Any) -> Any:
            return render_mod.RenderResult(html=product, status=200, final_url=url)

        monkeypatch.setattr(render_mod, "render_html", fake_render)

        tool = _get_tool(server, "auto_scrape")
        result = await tool.fn(url="https://walled.test/p")  # type: ignore[attr-defined]

        assert result["pattern_used"] == "render"
        assert "a_b_c" not in result["pattern_attempts"]

    @pytest.mark.asyncio
    async def test_an_ab_c_win_is_recorded(
        self, server: object, fake_curl: type[FakeCurlSession], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from scrapper_tool.recipe.policy import get_policy_store

        fake_curl.STATUS_FOR_PROFILE = {"chrome146": 200}
        fake_curl.RESPONSE_TEXT_FOR_PROFILE = {
            "chrome146": (
                '<html><head><script type="application/ld+json">'
                '{"@context":"https://schema.org","@type":"Product","name":"W",'
                '"offers":{"@type":"Offer","price":"1.00","priceCurrency":"USD"}}'
                "</script></head><body></body></html>"
            )
        }

        tool = _get_tool(server, "auto_scrape")
        await tool.fn(url="https://plain.test/p")  # type: ignore[attr-defined]

        policy = get_policy_store().get("https://plain.test/p")
        assert policy is not None
        assert policy.best_tier == "a_b_c"


# ---- B3: challenge detection drives escalation (MCP parity) ---------------


_RADWARE_WALL = (
    "<html><head><title>Loading</title></head><body>"
    "<script>window.location='https://validate.perfdrive.com/xyz'</script>"
    "</body></html>"
)
_CF_WALL = "<html><head><title>Just a moment...</title></head><body></body></html>"


class TestAutoScrapeChallengeDetection:
    """MCP had no challenge detection at all before B3 — REST's was private.

    Same rule as REST: Cloudflare still goes through Pattern D (Scrapling can
    solve it), every other vendor skips straight to the render tier.
    """

    @pytest.mark.asyncio
    async def test_non_cloudflare_wall_skips_d_and_renders(
        self,
        server: object,
        fake_curl: type[FakeCurlSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        fake_curl.STATUS_FOR_PROFILE = {"chrome146": 200}
        fake_curl.RESPONSE_TEXT_FOR_PROFILE = {"chrome146": _RADWARE_WALL}
        # D is available and would run — the point is that it doesn't.
        import scrapper_tool.patterns.d as d_mod

        def fake_hostile_client(**_kwargs: object) -> _FakeMcpFetcher:
            raise AssertionError("Pattern D must be skipped on a non-Cloudflare wall")

        monkeypatch.setattr(d_mod, "hostile_client", fake_hostile_client)
        _install_fake_render_mcp(monkeypatch)

        tool = _get_tool(server, "auto_scrape")
        result = await tool.fn(url="https://walled.com/p")  # type: ignore[attr-defined]

        assert result["challenge_detected"] == "radware"
        assert result["pattern_used"] == "render"
        assert result["pattern_attempts"] == ["a_b_c", "render"]

    @pytest.mark.asyncio
    async def test_cloudflare_wall_still_runs_d(
        self,
        server: object,
        fake_curl: type[FakeCurlSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        fake_curl.STATUS_FOR_PROFILE = {"chrome146": 200}
        fake_curl.RESPONSE_TEXT_FOR_PROFILE = {"chrome146": _CF_WALL}
        import scrapper_tool.patterns.d as d_mod

        def fake_hostile_client(**_kwargs: object) -> _FakeMcpFetcher:
            return _FakeMcpFetcher(_FakeMcpScraplingResponse(html=_RENDER_PRODUCT_HTML))

        monkeypatch.setattr(d_mod, "hostile_client", fake_hostile_client)

        tool = _get_tool(server, "auto_scrape")
        result = await tool.fn(url="https://cf.com/p")  # type: ignore[attr-defined]

        assert result["challenge_detected"] == "cloudflare"
        assert result["pattern_used"] == "d"
        assert result["pattern_attempts"] == ["a_b_c", "d"]

    @pytest.mark.asyncio
    async def test_no_challenge_reports_null(
        self,
        server: object,
        fake_curl: type[FakeCurlSession],
    ) -> None:
        fake_curl.STATUS_FOR_PROFILE = {"chrome146": 200}
        fake_curl.RESPONSE_TEXT_FOR_PROFILE = {"chrome146": _RENDER_PRODUCT_HTML}

        tool = _get_tool(server, "auto_scrape")
        result = await tool.fn(url="https://plain.com/p")  # type: ignore[attr-defined]

        assert result["pattern_used"] == "a_b_c"
        assert result["challenge_detected"] is None


# ---- B4: E2 is gated behind interactive=true (MCP parity) -----------------


def _fake_agent_module_e1_blocked(*, browse_result: Any = None) -> MagicMock:
    """A ``scrapper_tool.agent`` stand-in whose E1 always reports blocked."""
    blocked = MagicMock()
    blocked.mode = "extract"
    blocked.data = None
    blocked.final_url = "https://protected.com/p"
    blocked.rendered_markdown = None
    blocked.screenshots = None
    blocked.actions = []
    blocked.tokens_used = 10
    blocked.steps_used = 1
    blocked.blocked = True
    blocked.error = "hit a captcha"
    blocked.duration_s = 1.0

    agent_module = MagicMock()
    agent_module.AgentConfig = MagicMock()
    agent_module.AgentConfig.from_env = MagicMock(return_value=MagicMock())

    async def fake_extract(*_args: Any, **_kwargs: Any) -> Any:
        return blocked

    async def fake_browse(*_args: Any, **_kwargs: Any) -> Any:
        if browse_result is None:
            raise AssertionError("E2 must not run without interactive=true")
        return browse_result

    agent_module.agent_extract = fake_extract
    agent_module.agent_browse = fake_browse
    return agent_module


class TestAutoScrapeE2Gate:
    """MCP parity for the interactive gate — and MCP's first E2 coverage at all."""

    @staticmethod
    def _all_blocked(fake_curl: type[FakeCurlSession], monkeypatch: pytest.MonkeyPatch) -> None:
        fake_curl.STATUS_FOR_PROFILE = dict.fromkeys(IMPERSONATE_LADDER, 403)
        monkeypatch.setattr(mcp_module, "_try_pattern_d_for_auto_scrape", _skip_d_for_auto_scrape)
        monkeypatch.setenv("SCRAPPER_TOOL_RENDER_TIER", "0")

    @pytest.mark.asyncio
    async def test_blocked_e1_stops_without_interactive(
        self,
        server: object,
        fake_curl: type[FakeCurlSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import sys

        self._all_blocked(fake_curl, monkeypatch)
        monkeypatch.setitem(sys.modules, "scrapper_tool.agent", _fake_agent_module_e1_blocked())

        tool = _get_tool(server, "auto_scrape")
        result = await tool.fn(url="https://protected.com/p")  # type: ignore[attr-defined]

        assert result["pattern_attempts"] == ["a_b_c", "e1"]
        assert result["pattern_used"] == "e1"
        assert result["blocked"] is True

    @pytest.mark.asyncio
    async def test_blocked_e1_escalates_with_interactive(
        self,
        server: object,
        fake_curl: type[FakeCurlSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import sys

        self._all_blocked(fake_curl, monkeypatch)
        browsed = MagicMock()
        browsed.mode = "browse"
        browsed.data = {"name": "Reached via agent"}
        browsed.final_url = "https://protected.com/p"
        browsed.rendered_markdown = None
        browsed.screenshots = None
        browsed.actions = []
        browsed.tokens_used = 900
        browsed.steps_used = 5
        browsed.blocked = False
        browsed.error = None
        browsed.duration_s = 9.0
        monkeypatch.setitem(
            sys.modules,
            "scrapper_tool.agent",
            _fake_agent_module_e1_blocked(browse_result=browsed),
        )

        tool = _get_tool(server, "auto_scrape")
        result = await tool.fn(  # type: ignore[attr-defined]
            url="https://protected.com/p", interactive=True
        )

        assert result["pattern_used"] == "e2"
        assert result["pattern_attempts"] == ["a_b_c", "e1", "e2"]


# ---- Truncation -----------------------------------------------------------


class TestBodyTruncation:
    def test_short_body_not_truncated(self) -> None:
        text, truncated = mcp_module._truncate("hello world")
        assert text == "hello world"
        assert truncated is False

    def test_long_body_truncated_to_64kb(self) -> None:
        body = "x" * (70 * 1024)  # 70 KB
        text, truncated = mcp_module._truncate(body)
        assert truncated is True
        # Encoded length matches the cap (64 KB).
        assert len(text.encode("utf-8")) <= 64 * 1024


# ---- v1.2.0: is_structured response field ---------------------------------


class TestAutoScrapeIsStructured:
    """v1.2.0 — auto_scrape responses carry the sidecar's success verdict."""

    def test_helper_truth_table(self) -> None:
        assert mcp_module._is_e_tier_structured({"name": "x"}, False) is True
        assert mcp_module._is_e_tier_structured({"_raw": "..."}, False) is False
        assert mcp_module._is_e_tier_structured(None, False) is False
        assert mcp_module._is_e_tier_structured({"name": "x"}, True) is False

    @pytest.mark.asyncio
    async def test_a_b_c_success_carries_is_structured_true(
        self,
        server: object,
        fake_curl: type[FakeCurlSession],
    ) -> None:
        product_html = (
            '<html><head><script type="application/ld+json">'
            '{"@context":"https://schema.org","@type":"Product","name":"Widget Y",'
            '"sku":"Y1","offers":{"@type":"Offer","price":"29.99","priceCurrency":"USD"}}'
            "</script></head><body></body></html>"
        )
        fake_curl.STATUS_FOR_PROFILE = {"chrome146": 200}
        fake_curl.RESPONSE_TEXT_FOR_PROFILE = {"chrome146": product_html}

        tool = _get_tool(server, "auto_scrape")
        result = await tool.fn(url="https://example.test/p")  # type: ignore[attr-defined]
        assert result["pattern_used"] == "a_b_c"
        assert result["is_structured"] is True

    @pytest.mark.asyncio
    async def test_d_success_carries_is_structured_true(
        self,
        server: object,
        fake_curl: type[FakeCurlSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # All A/B/C profiles 403 -> BlockedError -> D step.
        fake_curl.STATUS_FOR_PROFILE = dict.fromkeys(IMPERSONATE_LADDER, 403)
        product_html = (
            '<html><head><script type="application/ld+json">'
            '{"@context":"https://schema.org","@type":"Product","name":"Widget Z",'
            '"sku":"Z1","offers":{"@type":"Offer","price":"39.99","priceCurrency":"USD"}}'
            "</script></head><body></body></html>"
        )
        import scrapper_tool.patterns.d as d_mod

        def fake_hostile_client(**_kwargs: object) -> _FakeMcpFetcher:
            return _FakeMcpFetcher(_FakeMcpScraplingResponse(html=product_html))

        monkeypatch.setattr(d_mod, "hostile_client", fake_hostile_client)

        tool = _get_tool(server, "auto_scrape")
        result = await tool.fn(url="https://hostile.com/p")  # type: ignore[attr-defined]
        assert result["pattern_used"] == "d"
        assert result["is_structured"] is True


# ---- main() entrypoint ----------------------------------------------------


class TestMain:
    def test_help_exits_0(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.setattr("sys.argv", ["scrapper-tool-mcp", "--help"])
        exit_code = mcp_module.main()
        assert exit_code == 0
        captured = capsys.readouterr()
        assert "MCP server" in captured.out

    def test_default_startup_calls_run(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Patch the server build to return a mock whose run() we can assert.
        fake_server = MagicMock()
        fake_server.run = MagicMock()
        monkeypatch.setattr(mcp_module, "_build_server", MagicMock(return_value=fake_server))
        # Simulate sys.argv with just the program name.
        monkeypatch.setattr("sys.argv", ["scrapper-tool-mcp"])
        exit_code = mcp_module.main()
        assert exit_code == 0
        fake_server.run.assert_called_once()

    def test_extra_not_installed_exits_1(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # Force _build_server to raise ImportError as if [agent] missing.
        def _missing(*_args: object, **_kwargs: object) -> None:
            msg = "scrapper-tool MCP server requires the [agent] extra"
            raise ImportError(msg)

        monkeypatch.setattr(mcp_module, "_build_server", _missing)
        monkeypatch.setattr("sys.argv", ["scrapper-tool-mcp"])
        exit_code = mcp_module.main()
        assert exit_code == 1
        captured = capsys.readouterr()
        assert "[agent] extra" in captured.err


class TestParseArgs:
    """Unit tests for ``mcp_module._parse_args`` - transport flag plumbing.

    Important contract: the same set of values is reachable via either
    --transport / --host / --port flags OR the matching env vars, and
    flags win over env.
    """

    def test_default_returns_stdio_localhost_8000(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for k in (
            "SCRAPPER_TOOL_MCP_TRANSPORT",
            "SCRAPPER_TOOL_MCP_HOST",
            "SCRAPPER_TOOL_MCP_PORT",
        ):
            monkeypatch.delenv(k, raising=False)
        result = mcp_module._parse_args([])
        assert result == ("stdio", "127.0.0.1", 8000)

    def test_help_exits_0(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.delenv("SCRAPPER_TOOL_MCP_TRANSPORT", raising=False)
        result = mcp_module._parse_args(["--help"])
        assert result == 0
        captured = capsys.readouterr()
        assert "USAGE" in captured.out

    def test_flags_override_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SCRAPPER_TOOL_MCP_TRANSPORT", "sse")
        monkeypatch.setenv("SCRAPPER_TOOL_MCP_HOST", "10.0.0.1")
        monkeypatch.setenv("SCRAPPER_TOOL_MCP_PORT", "9999")
        result = mcp_module._parse_args(
            ["--transport", "streamable-http", "--host", "0.0.0.0", "--port", "8765"]
        )
        assert result == ("streamable-http", "0.0.0.0", 8765)

    def test_env_used_when_no_flags(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SCRAPPER_TOOL_MCP_TRANSPORT", "sse")
        monkeypatch.setenv("SCRAPPER_TOOL_MCP_HOST", "0.0.0.0")
        monkeypatch.setenv("SCRAPPER_TOOL_MCP_PORT", "8765")
        result = mcp_module._parse_args([])
        assert result == ("sse", "0.0.0.0", 8765)

    def test_invalid_transport_returns_2(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        for k in (
            "SCRAPPER_TOOL_MCP_TRANSPORT",
            "SCRAPPER_TOOL_MCP_HOST",
            "SCRAPPER_TOOL_MCP_PORT",
        ):
            monkeypatch.delenv(k, raising=False)
        result = mcp_module._parse_args(["--transport", "telegram"])
        assert result == 2
        captured = capsys.readouterr()
        assert "telegram" in captured.err

    def test_unknown_arg_returns_2(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        for k in (
            "SCRAPPER_TOOL_MCP_TRANSPORT",
            "SCRAPPER_TOOL_MCP_HOST",
            "SCRAPPER_TOOL_MCP_PORT",
        ):
            monkeypatch.delenv(k, raising=False)
        result = mcp_module._parse_args(["--frobnicate"])
        assert result == 2

    def test_non_int_port_via_flag_returns_2(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        for k in (
            "SCRAPPER_TOOL_MCP_TRANSPORT",
            "SCRAPPER_TOOL_MCP_HOST",
            "SCRAPPER_TOOL_MCP_PORT",
        ):
            monkeypatch.delenv(k, raising=False)
        result = mcp_module._parse_args(["--port", "eight"])
        assert result == 2

    def test_non_int_port_via_env_returns_2(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("SCRAPPER_TOOL_MCP_PORT", "not-an-int")
        result = mcp_module._parse_args([])
        assert result == 2


class TestMainTransportPlumbing:
    """End-to-end main() with the new --transport flag wired into server.run."""

    def test_streamable_http_transport_calls_run_with_correct_kwargs(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        for k in (
            "SCRAPPER_TOOL_MCP_TRANSPORT",
            "SCRAPPER_TOOL_MCP_HOST",
            "SCRAPPER_TOOL_MCP_PORT",
        ):
            monkeypatch.delenv(k, raising=False)
        fake_server = MagicMock()
        build_mock = MagicMock(return_value=fake_server)
        monkeypatch.setattr(mcp_module, "_build_server", build_mock)
        monkeypatch.setattr(
            "sys.argv",
            [
                "scrapper-tool-mcp",
                "--transport",
                "streamable-http",
                "--host",
                "0.0.0.0",
                "--port",
                "8765",
            ],
        )
        exit_code = mcp_module.main()
        assert exit_code == 0
        # _build_server gets host/port so SSE/HTTP knows where to bind.
        build_mock.assert_called_once_with(host="0.0.0.0", port=8765)
        # server.run() gets the transport name.
        fake_server.run.assert_called_once_with(transport="streamable-http")
        # Listening banner went to stderr (so stdio JSON-RPC consumers
        # never see it on stdin).
        captured = capsys.readouterr()
        assert "streamable-http" in captured.err
        assert "0.0.0.0:8765" in captured.err


# ---- Module surface -------------------------------------------------------


class TestModuleSurface:
    def test_module_docstring_present(self) -> None:
        # Even with the importorskip in place at module top, when the
        # [agent] extra IS installed (this matrix entry), the docstring
        # should be readable and explain the MCP server.
        assert mcp_module.__doc__ is not None
        assert "MCP server" in mcp_module.__doc__

    def test_main_is_callable(self) -> None:
        # ``main`` is the console-script entry; just verify the symbol
        # exists and is a callable. End-to-end behaviour is covered by
        # TestMain above.
        assert callable(mcp_module.main)


# Note: the "_build_server raises ImportError when [agent] not installed"
# scenario is covered by the module-level `pytest.importorskip(...)` at
# the top of this file: when mcp.server.fastmcp can't be imported, the
# whole test module is skipped — exactly the behaviour the lib promises.


# ---- v1.2.0: hostile_only fast-path ---------------------------------------


class TestAutoScrapeHostileOnly:
    """v1.2.0 — auto_scrape(hostile_only=True) skips A/B/C and starts at D."""

    @pytest.mark.asyncio
    async def test_hostile_only_invokes_d_skips_a_b_c(
        self,
        server: object,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # NO fake_curl fixture — if A/B/C is invoked the test would attempt
        # a real HTTP call. Lack of mock IS the assertion.
        product_html = (
            '<html><head><script type="application/ld+json">'
            '{"@context":"https://schema.org","@type":"Product","name":"Hostile Widget",'
            '"sku":"H1","offers":{"@type":"Offer","price":"49.99","priceCurrency":"USD"}}'
            "</script></head><body></body></html>"
        )
        import scrapper_tool.patterns.d as d_mod

        def fake_hostile_client(**_kwargs: object) -> _FakeMcpFetcher:
            return _FakeMcpFetcher(_FakeMcpScraplingResponse(html=product_html))

        monkeypatch.setattr(d_mod, "hostile_client", fake_hostile_client)

        tool = _get_tool(server, "auto_scrape")
        result = await tool.fn(  # type: ignore[attr-defined]
            url="https://hostile.com/p", hostile_only=True
        )
        assert result["pattern_used"] == "d"
        assert result["pattern_attempts"] == ["d"], "no a_b_c noise"
        assert result["is_structured"] is True
        assert result["hostile_skipped"] is False

    @pytest.mark.asyncio
    async def test_hostile_only_no_fallback_returns_blocked_when_extra_missing(
        self,
        server: object,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Force the import inside _try_pattern_d_for_auto_scrape to raise.
        import builtins
        import sys

        # monkeypatch.delitem so pytest restores the real module afterwards.
        monkeypatch.delitem(sys.modules, "scrapper_tool.patterns.d", raising=False)
        real_import = builtins.__import__

        def patched_import(
            name: str,
            globals: object = None,
            locals: object = None,
            fromlist: object = (),
            level: int = 0,
        ) -> object:
            if name == "scrapper_tool.patterns.d":
                raise ImportError("simulated: [hostile] not installed")
            return real_import(name, globals, locals, fromlist, level)

        monkeypatch.setattr(builtins, "__import__", patched_import)

        tool = _get_tool(server, "auto_scrape")
        result = await tool.fn(  # type: ignore[attr-defined]
            url="https://hostile.com/p", hostile_only=True, hostile_fallback=False
        )
        assert result["blocked"] is True
        assert result["pattern_used"] is None
        assert result["hostile_skipped"] is True
        assert result["is_structured"] is False
        assert "hostile_only" in result["error"].lower() or "d failed" in result["error"].lower()


# ---- v1.3.0: shared CF clearance via per-cascade user_data_dir (MCP) -----


class TestAutoScrapeSharedProfileDir:
    """v1.3.0 - MCP auto_scrape allocates per-request user_data_dir."""

    @pytest.mark.asyncio
    async def test_d_receives_user_data_dir(
        self,
        server: object,
        fake_curl: type[FakeCurlSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # All A/B/C profiles 403 -> BlockedError -> D step.
        fake_curl.STATUS_FOR_PROFILE = dict.fromkeys(IMPERSONATE_LADDER, 403)
        captured_kwargs: dict[str, object] = {}
        product_html = (
            '<html><head><script type="application/ld+json">'
            '{"@context":"https://schema.org","@type":"Product","name":"X",'
            '"sku":"X1","offers":{"@type":"Offer","price":"9.99","priceCurrency":"USD"}}'
            "</script></head><body></body></html>"
        )

        class CapturingFetcher(_FakeMcpFetcher):
            async def async_fetch(self, url: str, **kwargs: object) -> object:
                captured_kwargs.clear()
                captured_kwargs.update(kwargs)
                return _FakeMcpScraplingResponse(html=product_html)

        import scrapper_tool.mcp as mcp_mod
        import scrapper_tool.patterns.d as d_mod

        def fake_hostile_client(**_kwargs: object) -> CapturingFetcher:
            return CapturingFetcher(_FakeMcpScraplingResponse(html=product_html))

        monkeypatch.setattr(d_mod, "hostile_client", fake_hostile_client)
        # Simulate [hostile] being installed so the cascade allocates a
        # per-request user_data_dir even without scrapling on the path.
        monkeypatch.setattr(mcp_mod, "_hostile_available_for_mcp", lambda: True)

        tool = _get_tool(server, "auto_scrape")
        result = await tool.fn(url="https://hostile.com/p")  # type: ignore[attr-defined]

        assert result["pattern_used"] == "d"
        assert "user_data_dir" in captured_kwargs, (
            "MCP cascade must forward user_data_dir to Scrapling"
        )
        assert "scrapper-cascade-mcp-" in str(captured_kwargs["user_data_dir"])

    @pytest.mark.asyncio
    async def test_caller_provided_persist_dir_honored(
        self,
        server: object,
        fake_curl: type[FakeCurlSession],
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Any,
    ) -> None:
        fake_curl.STATUS_FOR_PROFILE = dict.fromkeys(IMPERSONATE_LADDER, 403)
        captured_kwargs: dict[str, object] = {}
        caller_dir = str(tmp_path / "vendor-tasca-profile")
        product_html = (
            '<html><head><script type="application/ld+json">'
            '{"@context":"https://schema.org","@type":"Product","name":"Y","sku":"Y1",'
            '"offers":{"@type":"Offer","price":"1.00","priceCurrency":"USD"}}'
            "</script></head><body></body></html>"
        )

        class CapturingFetcher(_FakeMcpFetcher):
            async def async_fetch(self, url: str, **kwargs: object) -> object:
                captured_kwargs.clear()
                captured_kwargs.update(kwargs)
                return _FakeMcpScraplingResponse(html=product_html)

        import scrapper_tool.patterns.d as d_mod

        def fake_hostile_client(**_kwargs: object) -> CapturingFetcher:
            return CapturingFetcher(_FakeMcpScraplingResponse(html=product_html))

        monkeypatch.setattr(d_mod, "hostile_client", fake_hostile_client)

        tool = _get_tool(server, "auto_scrape")
        result = await tool.fn(  # type: ignore[attr-defined]
            url="https://hostile.com/p",
            persist_browser_profile_dir=caller_dir,
        )

        assert result["pattern_used"] == "d"
        assert captured_kwargs.get("user_data_dir") == caller_dir, (
            "caller-provided dir must be honored verbatim, not replaced with ephemeral"
        )
