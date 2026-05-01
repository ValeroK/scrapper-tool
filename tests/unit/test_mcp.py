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

- ``fetch_with_ladder`` — happy path (chrome133a wins) + blocked path
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
    async def test_happy_path_chrome133a_wins(
        self,
        server: object,
        fake_curl: type[FakeCurlSession],
    ) -> None:
        fake_curl.STATUS_FOR_PROFILE = {"chrome133a": 200}
        fake_curl.RESPONSE_TEXT_FOR_PROFILE = {"chrome133a": "<html>ok</html>"}
        tool = _get_tool(server, "fetch_with_ladder")

        result = await tool.fn(url="https://example.test/x")  # type: ignore[attr-defined]
        assert result["status"] == 200
        assert result["winning_profile"] == "chrome133a"
        assert result["blocked"] is False
        assert "<html>ok</html>" in result["body"]
        assert result["truncated"] is False

    @pytest.mark.asyncio
    async def test_all_blocked_returns_blocked_true(
        self,
        server: object,
        fake_curl: type[FakeCurlSession],
    ) -> None:
        fake_curl.STATUS_FOR_PROFILE = {
            "chrome133a": 403,
            "chrome124": 403,
            "safari18_0": 403,
            "firefox135": 403,
        }
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
        fake_curl.STATUS_FOR_PROFILE = {"chrome133a": 200}
        tool = _get_tool(server, "canary")
        result = await tool.fn(url="https://example.test/x")  # type: ignore[attr-defined]
        assert result["winning_profile"] == "chrome133a"
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
